"""模型组凭据的两个卫生残留：#64 解不开的密文不因普通保存被抹掉、#61 YAML 镜像不落明文

## 这里钉的是什么

`test_model_group_credential_at_rest.py` 钉的是「库里是密文」本身；本文件钉它上游两条
残留路径（PROJECT_STATE §3 #61 / #64，round-22 登记时均**已实测复现**）：

1. **#64**：密钥丢了之后那一行的 ORM 属性是空串（fail-closed），改造前任何一次普通保存
   （后台改个名字、`update_group()` 走一遍"掩码=保持不变"）都会把空串加密写回，
   **原密文永久丢失**——而它本来还有机会被原密钥救回来。
2. **#61**：`refresh_runtime_view(persist=True)` 把运行时视图 dump 回
   `<DATA_DIR>/configs/nekro-agent.yaml`，改造前那份镜像里的 `API_KEY` 是明文——
   整目录打包的备份照样带出全部 Key。

## 探针写法（沿用 at_rest 文件的约定）

- 「库里存的是什么」一律用**原始列**（`execute_query_dict`，绕开 ORM 字段转换）回答，
  拿 ORM 属性断言等于什么都没测；
- 每条"看不到明文/密文没被动"都带**正向对照**：同一行/同一份文件里非敏感字段
  （`base_url`、`chat_model`）确实可见/确实被改，否则探针空转也能绿；
- 样例凭据全部自造（含 `SENTINEL` 字样），不碰真实 Key。

## 与 at_rest 文件的一个关键差别

at_rest 的 `cipher_env` 把 `model_group_service.save_config` 整个换成 no-op（它不需要
落盘）。本文件**恰恰要断言落盘内容**，所以不掐 `save_config`，而是把配置文件路径
（`config._config_file_path`，导入期就从 `OsEnv.DATA_DIR` 算好的那个常量的实例级副本）
指到 tmp 目录——指不对的后果不是"测试脏"，是把开发机上的真实配置写坏
（at_rest 文件 §2.29 的教训，那边为此留了逐字节看门人，这里照搬）。
"""

from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml
from tortoise import Tortoise, connections

from nekro_agent.core.config import (
    MODEL_GROUP_API_KEY_PLACEHOLDER,
    CoreConfig,
    ModelConfigGroup,
    config,
    is_model_group_api_key_placeholder,
    reload_config,
)
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_model_group import DBModelGroup
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.tenant import CREDENTIAL_MASK, TenantRole
from nekro_agent.services import model_group_service
from nekro_agent.services.model_group_service import (
    create_group,
    resolve_console_identity,
    update_group,
    upsert_platform_group,
)
from nekro_agent.services.user.role import Role
from nekro_agent.tools.credential_cipher import (
    ENVELOPE_PREFIX,
    decrypt_credential,
    is_encrypted_envelope,
    reset_credential_cipher_cache,
)
from nekro_agent.tools.redaction import is_credential_mask

# 哨兵：自己编的，不是任何真实凭据
SENTINEL_KEY = "sk-hygiene-SENTINEL-4d81ca"
SENTINEL_BASE_URL = "https://sentinel-hygiene-upstream.example/v1"

# 造"解不开"用的两把互不相关的密钥材料（#64 复现探针的核心道具）
KEY_MATERIAL_A = "hygiene-SENTINEL-key-material-A"
KEY_MATERIAL_B = "hygiene-SENTINEL-key-material-B-totally-unrelated"


def _make_platform_owner() -> DBUser:
    user = DBUser(
        username="platform-owner",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Super,
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )
    user.id = 1
    return user


async def _raw_rows() -> List[Dict[str, Any]]:
    """直接读原始列，绕开 ORM 的字段转换（理由见 at_rest 文件的同名助手）"""
    conn = connections.get("default")
    return await conn.execute_query_dict(
        'SELECT "id", "name", "api_key", "base_url", "update_time" FROM "model_group" ORDER BY "id"',
    )


async def _raw_api_key(name: str) -> str:
    rows = [row for row in await _raw_rows() if row["name"] == name]
    assert len(rows) == 1, f"期望恰好一行 name={name}，实际 {len(rows)} 行"
    return rows[0]["api_key"]


async def _create_group(name: str = "sentinel_group", api_key: str = SENTINEL_KEY) -> DBModelGroup:
    return await DBModelGroup.create(
        name=name,
        ownership="platform",
        owner_tenant_id=None,
        visibility="shared",
        group_name=name,
        chat_model="gpt-4o",
        base_url=SENTINEL_BASE_URL,
        api_key=api_key,
        model_type="chat",
    )


def _real_config_file() -> Path:
    """开发机上那份真实的配置文件（可能不存在），本文件的落盘路径必须避开它"""
    from nekro_agent.core.config import CONFIG_PATH

    return CONFIG_PATH


@pytest.fixture
async def hygiene_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """一套与本机环境无关的加密环境 + 一张空的 `model_group` 表 + 落盘路径指到 tmp

    与 at_rest 的 `cipher_env` 相比：`save_config` **不**掐（本文件要读它写出的 YAML），
    改为把 `config._config_file_path`（实例级路径，优先于类级 `CONFIG_PATH`）指到 tmp。
    """
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "")
    reset_credential_cipher_cache()

    config_file = _real_config_file()
    before = config_file.read_bytes() if config_file.is_file() else None
    dumped = tmp_path / "configs" / "nekro-agent.yaml"
    monkeypatch.setattr(config, "_config_file_path", dumped)

    monkeypatch.setattr(model_group_service.config, "MODEL_GROUPS", {})

    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield tmp_path
    finally:
        await Tortoise.close_connections()
        reset_credential_cipher_cache()
        # 看门人：落盘路径指歪了会让用例改写开发机上的真实配置文件（at_rest §2.29 的教训）
        if before is not None:
            assert config_file.read_bytes() == before, (
                f"用例改写了开发机上的真实配置文件 {config_file}；"
                "dump 重定向（config._config_file_path）没有生效"
            )


@pytest.fixture
def warn_logs():
    """抓住产品代码真正打出去的 WARNING（loguru 不进 stdlib logging，caplog 恒空）"""
    from loguru import logger

    captured: List[str] = []
    sink_id = logger.add(lambda message: captured.append(message.record["message"]), level="WARNING")
    try:
        yield captured
    finally:
        logger.remove(sink_id)


def _read_dumped_yaml(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ============================================================
# #64：一次普通保存不得抹掉解不开的密文
# ============================================================


class TestUndecryptableCiphertextSurvivesNormalSaves:
    """复现探针：密钥换掉之后，那一行的普通保存路径全部不得触碰原密文"""

    async def _make_undecryptable_row(
        self,
        monkeypatch: pytest.MonkeyPatch,
        name: str = "locked_out",
    ) -> str:
        """造一行「当前密钥解不开」的密文，返回库内原始值

        步骤即 #64 的实测复现路径：用 A 加密落库 → 换成无关的 B → 原密文对 B 解不开。
        """
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_MATERIAL_A)
        reset_credential_cipher_cache()
        await _create_group(name)
        stored = await _raw_api_key(name)
        assert is_encrypted_envelope(stored), "前置对照：换密钥之前这行确实是密文"

        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_MATERIAL_B)
        reset_credential_cipher_cache()
        row = await DBModelGroup.get(name=name)
        # 复现起点：fail-closed，属性是空串——改造前下一步 save 就会把密文抹掉
        assert row.api_key == ""
        return stored

    async def test_rename_save_keeps_undecryptable_ciphertext(
        self,
        hygiene_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#64 路径 ①：后台只改了个模型名，原密文必须逐字保留"""
        stored = await self._make_undecryptable_row(monkeypatch)

        row = await DBModelGroup.get(name="locked_out")
        row.chat_model = "gpt-4o-mini"
        await row.save()

        assert await _raw_api_key("locked_out") == stored
        # 正向对照：这次保存真的执行了（其他字段写进去了），不是"根本没保存"的假绿
        assert (await DBModelGroup.get(name="locked_out")).chat_model == "gpt-4o-mini"
        # 换回原密钥仍能解出原值——密文确实还"活着"，这正是守卫要保住的东西
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_MATERIAL_A)
        reset_credential_cipher_cache()
        assert (await DBModelGroup.get(name="locked_out")).api_key == SENTINEL_KEY

    async def test_mask_resubmit_keeps_undecryptable_ciphertext(
        self,
        hygiene_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#64 的另一形态：前端把掩码原样提交回来（"掩码=保持不变"那条路径）

        `update_group` 对掩码的处理是 `group.API_KEY = row.api_key`，而解不开时
        `row.api_key` 恰是空串——改造前这照样是一次覆盖。
        """
        stored = await self._make_undecryptable_row(monkeypatch)

        row = await DBModelGroup.get(name="locked_out")
        cfg = row.to_config_group()
        cfg.API_KEY = CREDENTIAL_MASK
        cfg.CHAT_MODEL = "gpt-4o-mini"
        await update_group(resolve_console_identity(_make_platform_owner()), row, cfg)

        assert await _raw_api_key("locked_out") == stored
        assert (await DBModelGroup.get(name="locked_out")).chat_model == "gpt-4o-mini"

    async def test_api_key_only_update_fields_is_a_noop(
        self,
        hygiene_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`update_fields` 只含 `api_key` 且被守卫裁空：整次保存无事可做，一行都不该写

        对照组是 `update_time`：跳过保存它就不该前进。
        """
        stored = await self._make_undecryptable_row(monkeypatch)
        before = {row["name"]: (row["api_key"], row["update_time"]) for row in await _raw_rows()}

        row = await DBModelGroup.get(name="locked_out")
        row.api_key = ""
        await row.save(update_fields=["api_key"])

        after = {row["name"]: (row["api_key"], row["update_time"]) for row in await _raw_rows()}
        assert after == before
        assert after["locked_out"][0] == stored

    async def test_guard_names_the_group_in_a_warning(
        self,
        hygiene_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        warn_logs: List[str],
    ) -> None:
        """守卫触发要点名是哪个组——不然运维只知道"有个组被跳过"，不知道该去恢复谁的密钥"""
        await self._make_undecryptable_row(monkeypatch, name="named_in_log")
        row = await DBModelGroup.get(name="named_in_log")
        row.chat_model = "gpt-4o-mini"
        await row.save()

        named = [message for message in warn_logs if "named_in_log" in message and "保留" in message]
        assert len(named) == 1, f"期望恰好一条点名 WARNING，实际 {named}"
        assert "NEKRO_CREDENTIAL_SECRET_KEY" in named[0]


# ============================================================
# #64 的另一半：显式清空不能被守卫误堵
# ============================================================


class TestExplicitClearIsNotBlocked:
    """守卫只拦"解不开"的行；正常部署里清空 Key 的合法路径必须逐字保持改造前行为"""

    async def test_submitting_empty_key_on_a_readable_row_clears_it(self, hygiene_env: Path) -> None:
        """解得开的行上提交空串 = 显式清空，必须真的清掉"""
        row = await _create_group()
        assert is_encrypted_envelope(await _raw_api_key("sentinel_group"))

        cfg = row.to_config_group()
        cfg.API_KEY = ""
        await update_group(resolve_console_identity(_make_platform_owner()), row, cfg)

        assert await _raw_api_key("sentinel_group") == ""
        assert (await DBModelGroup.get(name="sentinel_group")).api_key == ""

    async def test_clearing_a_legacy_plaintext_row_still_works(self, hygiene_env: Path) -> None:
        """存量明文行（测试部署上的真实形态）同样不在守卫范围内"""
        conn = connections.get("default")
        await conn.execute_query(
            'INSERT INTO "model_group" '
            '("name", "ownership", "owner_tenant_id", "visibility", "group_name", "chat_model", '
            '"chat_proxy", "base_url", "api_key", "model_type", "enable_vision", "enable_cot", '
            '"token_input_rate", "token_completion_rate", "model_price_rate") '
            f"VALUES ('legacy', 'platform', NULL, 'private', 'legacy', 'gpt-4o', "
            f"'', '{SENTINEL_BASE_URL}', '{SENTINEL_KEY}', 'chat', 0, 0, 1, 1, 1)",
        )
        row = await DBModelGroup.get(name="legacy")
        assert row.api_key == SENTINEL_KEY  # 前置对照：明文行读得到明文

        cfg = row.to_config_group()
        cfg.API_KEY = ""
        await update_group(resolve_console_identity(_make_platform_owner()), row, cfg)

        assert await _raw_api_key("legacy") == ""


# ============================================================
# #61：YAML 运行时镜像不再落明文
# ============================================================


class TestYamlMirrorCarriesNoPlaintext:
    """`refresh_runtime_view(persist=True)` 之后，配置文件里没有任何明文 Key"""

    async def test_refresh_persists_placeholders_not_plaintext(self, hygiene_env: Path) -> None:
        await _create_group("has_key", SENTINEL_KEY)
        await _create_group("no_key", "")

        view = await model_group_service.refresh_runtime_view(persist=True)

        # 运行时视图（进程内的数据源）仍是明文——几十个同步读取点零改动
        assert view["has_key"].API_KEY == SENTINEL_KEY
        assert config.MODEL_GROUPS["has_key"].API_KEY == SENTINEL_KEY

        data = _read_dumped_yaml(hygiene_env / "configs" / "nekro-agent.yaml")
        groups = data["MODEL_GROUPS"]
        # 有 Key 的组：占位符，且与密文/明文/前端掩码三种形态都不同
        assert groups["has_key"]["API_KEY"] == MODEL_GROUP_API_KEY_PLACEHOLDER
        # 没 Key 的组：保持空串——"未配置"是可读的合法状态，不是需要遮蔽的秘密
        assert groups["no_key"]["API_KEY"] == ""
        text = (hygiene_env / "configs" / "nekro-agent.yaml").read_text(encoding="utf-8")
        assert SENTINEL_KEY not in text
        # 正向对照：这份文件确实包含这些组（否则上面的断言只是探针空转）
        assert groups["has_key"]["BASE_URL"] == SENTINEL_BASE_URL
        assert groups["no_key"]["BASE_URL"] == SENTINEL_BASE_URL

    async def test_direct_dump_of_the_global_config_also_masks(
        self,
        hygiene_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """第二条 dump 路径：绕过 `refresh_runtime_view`、直接 `config.dump_config()`

        `/config/save/system`（WebUI 保存按钮）走的就是这一条。掩码必须收在
        `CoreConfig.dump_config` 这一层的理由：调用方不止一个。
        """
        monkeypatch.setattr(
            config,
            "MODEL_GROUPS",
            {"leaky": ModelConfigGroup(CHAT_MODEL="gpt-4o", BASE_URL=SENTINEL_BASE_URL, API_KEY=SENTINEL_KEY)},
        )
        config.dump_config()

        groups = _read_dumped_yaml(hygiene_env / "configs" / "nekro-agent.yaml")["MODEL_GROUPS"]
        assert groups["leaky"]["API_KEY"] == MODEL_GROUP_API_KEY_PLACEHOLDER
        # 掩码发生在序列化输出上，内存对象不受影响
        assert config.MODEL_GROUPS["leaky"].API_KEY == SENTINEL_KEY

    def test_placeholder_is_distinguishable_from_every_real_shape(self, hygiene_env: Path) -> None:
        """占位符的形态契约：与密文信封、前端掩码互不混淆，钉成字面量"""
        assert MODEL_GROUP_API_KEY_PLACEHOLDER == "nkmask:1"
        assert is_model_group_api_key_placeholder(MODEL_GROUP_API_KEY_PLACEHOLDER)
        assert not is_model_group_api_key_placeholder(SENTINEL_KEY)
        # 不是密文信封；万一漏进解密路径，按"存量明文原样返回"处理而不是当坏密文丢掉
        assert is_encrypted_envelope(MODEL_GROUP_API_KEY_PLACEHOLDER) is False
        # 不是前端掩码（那是另一套"保持不变"哨兵，两者不能互相冒充）
        assert is_credential_mask(MODEL_GROUP_API_KEY_PLACEHOLDER) is False
        assert is_credential_mask(CREDENTIAL_MASK)
        assert ENVELOPE_PREFIX not in MODEL_GROUP_API_KEY_PLACEHOLDER


class TestMaskedYamlRoundTrip:
    """掩码 YAML 再导入：不覆盖库内密文；明文 YAML 首次部署：照常加密入库"""

    async def test_reimport_of_masked_mirror_does_not_touch_db_ciphertext(
        self,
        hygiene_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """稳态启动：YAML 里是占位符 → bootstrap 逐字不碰库内行"""
        await _create_group("sentinel_group")
        stored = await _raw_api_key("sentinel_group")

        monkeypatch.setattr(
            model_group_service.config,
            "MODEL_GROUPS",
            {
                "sentinel_group": ModelConfigGroup(
                    CHAT_MODEL="gpt-4o",
                    BASE_URL=SENTINEL_BASE_URL,
                    API_KEY=MODEL_GROUP_API_KEY_PLACEHOLDER,
                ),
            },
        )
        await model_group_service.bootstrap_model_groups()

        assert await _raw_api_key("sentinel_group") == stored
        assert decrypt_credential(stored) == SENTINEL_KEY

    async def test_new_group_from_masked_yaml_lands_keyless_with_warning(
        self,
        hygiene_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        warn_logs: List[str],
    ) -> None:
        """拿运行时镜像恢复一个**空库**：组照常建，Key 为空、绝不把占位串当凭据入库

        WARNING 必须点名组——恢复部署的人需要知道哪些组的 Key 要去后台重填。
        """
        monkeypatch.setattr(
            model_group_service.config,
            "MODEL_GROUPS",
            {
                "restored_from_mirror": ModelConfigGroup(
                    CHAT_MODEL="gpt-4o",
                    BASE_URL=SENTINEL_BASE_URL,
                    API_KEY=MODEL_GROUP_API_KEY_PLACEHOLDER,
                ),
            },
        )
        await model_group_service.bootstrap_model_groups()

        assert await _raw_api_key("restored_from_mirror") == ""
        row = await DBModelGroup.get(name="restored_from_mirror")
        assert row.chat_model == "gpt-4o"  # 正向对照：组本身导入成功
        assert row.api_key == ""
        named = [m for m in warn_logs if "restored_from_mirror" in m and "占位符" in m]
        assert len(named) == 1, f"期望恰好一条点名 WARNING，实际 {named}"

    async def test_plaintext_yaml_first_deploy_imports_encrypted(
        self,
        hygiene_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """首次部署（手写明文 YAML）：导入流程不变，Key 加密入库"""
        monkeypatch.setattr(
            model_group_service.config,
            "MODEL_GROUPS",
            {"from_yaml": ModelConfigGroup(CHAT_MODEL="gpt-4o", BASE_URL=SENTINEL_BASE_URL, API_KEY=SENTINEL_KEY)},
        )
        await model_group_service.bootstrap_model_groups()

        stored = await _raw_api_key("from_yaml")
        assert SENTINEL_KEY not in stored
        assert is_encrypted_envelope(stored)
        assert (await DBModelGroup.get(name="from_yaml")).api_key == SENTINEL_KEY

    def test_load_config_blanks_placeholder_keys(
        self,
        hygiene_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`CoreConfig.load_config` 把 YAML 里的占位符归一成空串，明文原样进内存

        归一保证 bootstrap 之前的窗口里，任何读取点拿到的都是 fail-closed 的空串，
        而不是一串会被原样发往上游鉴权的占位文本。
        """
        target = hygiene_env / "mini-config.yaml"
        target.write_text(
            yaml.dump(
                {
                    "MODEL_GROUPS": {
                        "from_mask": {"API_KEY": MODEL_GROUP_API_KEY_PLACEHOLDER, "BASE_URL": "https://from-mask.example/v1"},
                        "from_plain": {"API_KEY": SENTINEL_KEY, "BASE_URL": SENTINEL_BASE_URL},
                    },
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        # load_config_to_env 会把标记字段灌进 os.environ，与本用例无关，掐掉以免污染进程环境
        monkeypatch.setattr(CoreConfig, "load_config_to_env", lambda self: None)

        loaded = CoreConfig.load_config(file_path=target, auto_register=False)

        assert loaded.MODEL_GROUPS["from_mask"].API_KEY == ""
        # 正向对照：真正的明文（首次部署手写）不受归一影响，随后由 bootstrap 加密入库
        assert loaded.MODEL_GROUPS["from_plain"].API_KEY == SENTINEL_KEY


class TestServiceWritePathsHonourThePlaceholder:
    """占位符从服务写路径进来时（`/config/model-groups/{name}` 只还原前端掩码），
    一律按"这次没有提供新 Key"处理，绝不覆盖库内密文。"""

    async def test_upsert_with_placeholder_keeps_the_current_key(self, hygiene_env: Path) -> None:
        """整体覆盖路径：占位符不是一次改 Key 指令"""
        await _create_group("sentinel_group")

        await upsert_platform_group(
            "sentinel_group",
            ModelConfigGroup(API_KEY=MODEL_GROUP_API_KEY_PLACEHOLDER, CHAT_MODEL="ups-new", BASE_URL=SENTINEL_BASE_URL),
        )

        stored = await _raw_api_key("sentinel_group")
        assert is_encrypted_envelope(stored)
        assert decrypt_credential(stored) == SENTINEL_KEY
        row = await DBModelGroup.get(name="sentinel_group")
        assert row.api_key == SENTINEL_KEY
        assert row.chat_model == "ups-new"  # 正向对照：这条路径的其他字段确实被覆盖

    async def test_create_with_placeholder_lands_keyless(self, hygiene_env: Path) -> None:
        await create_group(
            resolve_console_identity(_make_platform_owner()),
            "from_placeholder",
            ModelConfigGroup(API_KEY=MODEL_GROUP_API_KEY_PLACEHOLDER, CHAT_MODEL="gpt-4o", BASE_URL=SENTINEL_BASE_URL),
        )
        assert await _raw_api_key("from_placeholder") == ""


class TestReloadKeepsTheRuntimeView:
    """`reload_config`（/reload 聊天命令）不得把 YAML 里的占位视图灌进运行时视图"""

    def test_module_reload_does_not_copy_yaml_groups_into_the_view(
        self,
        hygiene_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_reloaded = CoreConfig(
            MODEL_GROUPS={"from_yaml": ModelConfigGroup(API_KEY=MODEL_GROUP_API_KEY_PLACEHOLDER)},
        )
        monkeypatch.setattr(CoreConfig, "load_config", classmethod(lambda cls, **kwargs: fake_reloaded))

        view_before = config.MODEL_GROUPS
        snapshot = {name: getattr(config, name) for name in CoreConfig.model_fields}
        try:
            reload_config()
            # 被测项：MODEL_GROUPS 没被 YAML 版本顶掉（对象身份都没变）
            assert config.MODEL_GROUPS is view_before
            assert "from_yaml" not in config.MODEL_GROUPS
        finally:
            # reload 会把其余字段整体替换成新实例的值（这里即全默认值），
            # 不还原会漏给同一次会话里后面的用例
            for name, value in snapshot.items():
                if name != "MODEL_GROUPS":
                    setattr(config, name, value)


# ============================================================
# 哨兵自检（§2.13：样例凭据必须是自己编的）
# ============================================================


class TestNoRealCredentialInThisFile:
    def test_sentinels_are_obviously_fake(self) -> None:
        source = Path(__file__).read_text(encoding="utf-8")
        assert "SENTINEL" in SENTINEL_KEY
        assert "sentinel" in SENTINEL_BASE_URL
        assert SENTINEL_KEY in source
        # 密钥材料同样必须看得见字面量，避免常量被"顺手"换成真值
        assert KEY_MATERIAL_A in source and "totally-unrelated" in KEY_MATERIAL_B
