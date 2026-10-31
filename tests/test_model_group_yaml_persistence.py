"""#61 的落盘行为必须与文档一致：走**真实启动序列**断言 YAML 文件里到底是什么

## 为什么需要这份测试

`test_model_group_credential_hygiene.py` 钉了「掩码这个动作本身」，但它的几条关键用例是
**直接**把占位符塞给 `import_config_group` / 直接 monkeypatch `config.MODEL_GROUPS`，
绕过了生产上真正发生的那条路径。真实序列是：

    YAML 落在磁盘上
      → `CoreConfig.load_config()`            （把占位符归一成空串）
      → 模块导入期 `config.dump_config(mask_model_group_secrets=False)`   （写回）
      → `bootstrap_model_groups()`            （导入数据库、重建视图、落盘）

按这条序列走，旧实现有四处与文档不符（round-27 复核，已逐条复现）：

1. `load_config` 把 `nkmask:1` 归一成 `""`，导入期那次 dump 又把 `""` 原样写回——
   **每次重启 YAML 里的占位符都变成 `API_KEY: ''`**，「有 Key」与「没配 Key」从此不可区分；
2. `bootstrap` 用 `persist=bool(imported)`：普通重启不落盘，升级部署的**明文 YAML 一直
   留到下一次配置落盘**才被掩码，而文档说的是「升级后第一次启动就掩码」；
3. `import_config_group` 的占位符告警在生产上走不到——占位符在 load 时已被归一，
   它看到的永远是空串，「拿占位符 YAML 配全新库恢复」时运维得不到任何提示；
4. `dump_config` 靠**临时替换运行中的 `self.MODEL_GROUPS`** 做掩码，换进去又换回来。

## 目标行为（本文件逐条钉）

- 重启后占位符组仍是 `nkmask:1`，空 Key 的组仍是 `''`，两者不混淆；
- 占位符 YAML 配**全新**数据库恢复：bootstrap 点名 WARNING；
- bootstrap 完成数据库接管、且**没有解不开的行**时总是落盘一次——明文在升级后首启被掩码；
- 存在解不开的行时**不落盘**（YAML 里那份明文可能是唯一副本），并在日志里说明；
- 掩码做在序列化结果上：dump 期间 `config.MODEL_GROUPS` 对象不被替换、不被重新赋值；
- 其他 `ConfigBase` 子类的落盘输出逐字不变。
"""

import ast
import importlib
import os
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml
from tortoise import Tortoise, connections

from nekro_agent.core.config import (
    MODEL_GROUP_API_KEY_PLACEHOLDER,
    CoreConfig,
)
from nekro_agent.core.core_utils import ConfigBase
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_model_group import DBModelGroup
from nekro_agent.services import model_group_service
from nekro_agent.tools.credential_cipher import (
    credential_cipher_status,
    is_encrypted_envelope,
    reset_credential_cipher_cache,
)

# 哨兵：自己编的，不是任何真实凭据
SENTINEL_KEY = "sk-yamlpersist-SENTINEL-5e91b0"
SENTINEL_KEY_2 = "sk-yamlpersist-SENTINEL-2-c4417d"
SENTINEL_BASE_URL = "https://sentinel-yamlpersist-upstream.example/v1"
KEY_MATERIAL_A = "yamlpersist-SENTINEL-key-material-A"
KEY_MATERIAL_B = "yamlpersist-SENTINEL-key-material-B-totally-unrelated"

PLACEHOLDER = MODEL_GROUP_API_KEY_PLACEHOLDER

# ⚠️ 不能写 `import nekro_agent.core.config as core_config_module`：`nekro_agent/core/__init__.py`
# 把包属性 `config` 重绑成了 `CoreConfig` **实例**，`import a.b.c as x` 按属性链取到的是那个实例而不是模块。
# 要拿模块本身必须走 `sys.modules`。
core_config_module = importlib.import_module("nekro_agent.core.config")


# ============================================================
# 夹具与「真实序列」助手
# ============================================================


@pytest.fixture
async def boot_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """与本机无关的加密环境 + 一张空的 `model_group` 表；落盘路径全部在 tmp 下

    与 hygiene 文件的差别：那边是把**全局** `config` 的落盘路径指到 tmp，再塞假的
    `MODEL_GROUPS`；这里要的是**从一份磁盘上的 YAML 真的加载出一个 `CoreConfig`**，
    再让 `model_group_service` 与 `core.config.save_config` 都指向它（见 `start_process`）。
    """
    from nekro_agent.core.config import CONFIG_PATH

    real_before = CONFIG_PATH.read_bytes() if CONFIG_PATH.is_file() else None
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_MATERIAL_A)
    reset_credential_cipher_cache()
    # `load_config_to_env` 会把标记字段灌进 os.environ，与本文件无关，掐掉以免污染进程环境
    monkeypatch.setattr(CoreConfig, "load_config_to_env", lambda self: None)

    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield tmp_path
    finally:
        await Tortoise.close_connections()
        reset_credential_cipher_cache()
        # 看门人：本文件任何一步都不该碰开发机上的真实配置文件（§2.29 的教训）
        if real_before is not None:
            assert CONFIG_PATH.read_bytes() == real_before, f"用例改写了开发机上的真实配置文件 {CONFIG_PATH}"


@pytest.fixture
def logs():
    """抓住产品代码真正打出去的日志（loguru 不进 stdlib logging，caplog 恒空）"""
    from loguru import logger

    captured: List[Tuple[str, str]] = []
    sink_id = logger.add(
        lambda message: captured.append((message.record["level"].name, message.record["message"])),
        level="INFO",
    )
    try:
        yield captured
    finally:
        logger.remove(sink_id)


def write_yaml(path: Path, groups: Dict[str, Dict[str, Any]]) -> Path:
    """把一份模型组 YAML 写到磁盘上（只含 MODEL_GROUPS，其余字段走默认）"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump({"MODEL_GROUPS": groups}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def group_yaml(api_key: str, chat_model: str = "gpt-4o") -> Dict[str, Any]:
    return {"API_KEY": api_key, "BASE_URL": SENTINEL_BASE_URL, "CHAT_MODEL": chat_model}


def start_process(monkeypatch: pytest.MonkeyPatch, yaml_path: Path) -> CoreConfig:
    """模拟进程启动的前两步，返回加载出来的 `CoreConfig`（此刻已是「进程里的全局 config」）

    1. `CoreConfig.load_config()`：从磁盘 YAML 真加载；
    2. 模块导入期那次 `config.dump_config(mask_model_group_secrets=False)`
       （`core/config.py` 文件底部；下面 `TestTheImportTimeDumpIsStillWiredTheWayWeSimulateIt`
       钉住生产代码里确实是这一句，免得这里模拟的和生产的悄悄分叉）。

    随后把 `core.config.config`（`save_config()` 读的那个全局）与 `model_group_service.config`
    都换成它：生产上这两者本来就是同一个对象。
    """
    loaded = CoreConfig.load_config(file_path=yaml_path, auto_register=False)
    monkeypatch.setattr(core_config_module, "config", loaded)
    monkeypatch.setattr(model_group_service, "config", loaded)
    loaded.dump_config(mask_model_group_secrets=False)
    return loaded


async def bootstrap() -> None:
    await model_group_service.bootstrap_model_groups()


def read_yaml_groups(path: Path) -> Dict[str, Dict[str, Any]]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["MODEL_GROUPS"]


async def create_db_group(name: str, api_key: str) -> DBModelGroup:
    return await DBModelGroup.create(
        name=name,
        ownership="platform",
        owner_tenant_id=None,
        visibility="private",
        group_name=name,
        chat_model="gpt-4o",
        base_url=SENTINEL_BASE_URL,
        api_key=api_key,
        model_type="chat",
    )


async def raw_api_key(name: str) -> str:
    conn = connections.get("default")
    rows = await conn.execute_query_dict('SELECT "api_key" FROM "model_group" WHERE "name" = ?', [name])
    assert len(rows) == 1, f"期望恰好一行 name={name}，实际 {len(rows)} 行"
    return rows[0]["api_key"]


# ============================================================
# 1. 占位符重启不丢，空 Key 保持空，两者不混淆
# ============================================================


class TestPlaceholderSurvivesRestart:
    async def test_placeholder_group_stays_placeholder_through_the_whole_startup(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """正常重启：库里有 Key、YAML 是占位符。每一步之后文件里都必须仍是占位符"""
        await create_db_group("has_key", SENTINEL_KEY)
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"has_key": group_yaml(PLACEHOLDER)})

        loaded = start_process(monkeypatch, yaml_path)
        # 导入期那次原样写回之后：旧实现在这里就已经把占位符写成了 ''
        assert read_yaml_groups(yaml_path)["has_key"]["API_KEY"] == PLACEHOLDER
        # 进程内：占位符归一成空串（bootstrap 之前的窗口 fail-closed），不是占位文本
        assert loaded.MODEL_GROUPS["has_key"].API_KEY == ""

        await bootstrap()

        assert read_yaml_groups(yaml_path)["has_key"]["API_KEY"] == PLACEHOLDER
        assert loaded.MODEL_GROUPS["has_key"].API_KEY == SENTINEL_KEY  # 运行时视图是明文
        assert SENTINEL_KEY not in yaml_path.read_text(encoding="utf-8")

    async def test_a_second_restart_is_a_fixed_point(self, boot_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """连续重启不漂移：第二次启动落盘的字节与第一次逐字相同"""
        await create_db_group("has_key", SENTINEL_KEY)
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"has_key": group_yaml(PLACEHOLDER)})

        start_process(monkeypatch, yaml_path)
        await bootstrap()
        first = yaml_path.read_bytes()
        # 不动点必须落在**对的**那一点上：旧实现把占位符退化成 '' 之后同样「稳定」，
        # 只比两次字节相等会让它冒充通过
        assert read_yaml_groups(yaml_path)["has_key"]["API_KEY"] == PLACEHOLDER

        start_process(monkeypatch, yaml_path)
        await bootstrap()
        assert yaml_path.read_bytes() == first

    async def test_placeholder_and_empty_key_are_never_confused(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """同一份 YAML 里三种形态并存：有 Key（占位符）/ 没配 Key（空串）/ 手写明文——各归各位"""
        await create_db_group("has_key", SENTINEL_KEY)
        await create_db_group("no_key", "")
        yaml_path = write_yaml(
            boot_env / "configs" / "nekro-agent.yaml",
            {
                "has_key": group_yaml(PLACEHOLDER),
                "no_key": group_yaml(""),
                "hand_written": group_yaml(SENTINEL_KEY_2),
            },
        )

        start_process(monkeypatch, yaml_path)
        groups_after_import_dump = read_yaml_groups(yaml_path)
        # 导入期那次是「原样写回」：三种形态一个都不许变
        assert groups_after_import_dump["has_key"]["API_KEY"] == PLACEHOLDER
        assert groups_after_import_dump["no_key"]["API_KEY"] == ""
        assert groups_after_import_dump["hand_written"]["API_KEY"] == SENTINEL_KEY_2

        await bootstrap()

        groups = read_yaml_groups(yaml_path)
        assert groups["has_key"]["API_KEY"] == PLACEHOLDER
        assert groups["no_key"]["API_KEY"] == ""
        assert groups["hand_written"]["API_KEY"] == PLACEHOLDER  # 手写明文被导入后掩码
        # 正向对照：文件确实包含这几个组、且别的字段没被动
        assert all(g["BASE_URL"] == SENTINEL_BASE_URL for g in groups.values())


# ============================================================
# 2. 升级部署：明文在首启就被掩码；首次部署：明文不会在导入前丢掉
# ============================================================


class TestPlaintextYamlIsMaskedOnTheFirstStartAfterUpgrade:
    async def test_existing_db_rows_plus_plaintext_yaml_gets_masked_without_any_import(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """升级部署的典型形态：库里早就有这些组（所以 `imported` 为空），YAML 里还是明文

        旧实现 `persist=bool(imported)` → 不落盘 → 明文一直留到下一次配置落盘。
        """
        await create_db_group("legacy", SENTINEL_KEY)
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"legacy": group_yaml(SENTINEL_KEY)})

        start_process(monkeypatch, yaml_path)
        # 前置对照：导入期那次是原样写回，此刻文件里确实还是明文——后面的断言才有意义
        assert read_yaml_groups(yaml_path)["legacy"]["API_KEY"] == SENTINEL_KEY

        await bootstrap()

        assert read_yaml_groups(yaml_path)["legacy"]["API_KEY"] == PLACEHOLDER
        assert SENTINEL_KEY not in yaml_path.read_text(encoding="utf-8")

    async def test_first_deployment_plaintext_survives_until_the_database_owns_it(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """首次部署（空库 + 手写明文 YAML）：导入前文件里的明文一个字不许丢——
        先抹后崩 = Key 永久丢失；导入加密入库之后才掩码"""
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"fresh": group_yaml(SENTINEL_KEY)})

        start_process(monkeypatch, yaml_path)
        assert read_yaml_groups(yaml_path)["fresh"]["API_KEY"] == SENTINEL_KEY  # 导入前：唯一载体

        await bootstrap()

        assert is_encrypted_envelope(await raw_api_key("fresh"))
        assert (await DBModelGroup.get(name="fresh")).api_key == SENTINEL_KEY
        assert read_yaml_groups(yaml_path)["fresh"]["API_KEY"] == PLACEHOLDER


class TestEmptyKeyStaysEmpty:
    async def test_a_group_without_a_key_keeps_an_empty_string(
        self, boot_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await create_db_group("no_key", "")
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"no_key": group_yaml("")})

        start_process(monkeypatch, yaml_path)
        await bootstrap()

        assert read_yaml_groups(yaml_path)["no_key"]["API_KEY"] == ""
        assert (await DBModelGroup.get(name="no_key")).api_key == ""


# ============================================================
# 3. 存在解不开的行：不落盘，保住 YAML 里可能是唯一副本的明文
# ============================================================


class TestUndecryptableRowsHoldBackThePersist:
    async def _make_undecryptable(self, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
        """用密钥 A 加密落库，再换成无关的 B —— 这一行从此解不开"""
        await create_db_group(name, SENTINEL_KEY)
        assert is_encrypted_envelope(await raw_api_key(name))
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_MATERIAL_B)
        reset_credential_cipher_cache()

    async def test_yaml_plaintext_is_left_untouched_and_the_log_says_why(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        logs: List[Tuple[str, str]],
    ) -> None:
        await self._make_undecryptable(monkeypatch, "locked_out")
        # `healthy` 在换密钥之后才写入，用的是当前密钥，解得开
        await create_db_group("healthy", SENTINEL_KEY_2)
        yaml_path = write_yaml(
            boot_env / "configs" / "nekro-agent.yaml",
            {"locked_out": group_yaml(SENTINEL_KEY), "healthy": group_yaml(SENTINEL_KEY_2)},
        )

        start_process(monkeypatch, yaml_path)
        before = yaml_path.read_bytes()
        assert SENTINEL_KEY.encode() in before  # 前置对照：YAML 里这份明文确实在

        await bootstrap()

        # 整份文件逐字节不动：解不开的行存在时，YAML 里那份明文可能是它唯一的副本
        assert yaml_path.read_bytes() == before
        explained = [m for level, m in logs if "locked_out" in m and "未落盘" in m]
        assert explained, f"日志里没有说明为什么没落盘: {logs}"
        # 内存里的视图照常重建（只是不写盘）：healthy 是明文可用的，解不开的那个是 fail-closed 的空串
        assert model_group_service.config.MODEL_GROUPS["healthy"].API_KEY == SENTINEL_KEY_2
        assert model_group_service.config.MODEL_GROUPS["locked_out"].API_KEY == ""

    async def test_leftover_ciphertext_with_no_usable_key_at_all_also_holds_back_the_persist(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """密钥既没配、又建不出来（只读数据目录）时，库里旧密钥时代留下的密文同样解不开：
        整批升级在这一档按设计不重写任何行，但「扫出有解不开的行」不能因此被一起跳过——
        否则这一档上升级后的首启会把 YAML 里可能是唯一副本的明文掩码掉"""
        await create_db_group("legacy_cipher", SENTINEL_KEY)  # 用密钥 A 加密落库
        assert is_encrypted_envelope(await raw_api_key("legacy_cipher"))

        # 造「密钥建不出来」：数据目录的父路径是个文件，mkdir 必然失败（同 at_rest 文件的造法）
        blocker = boot_env / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(blocker / "nested"))
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "")
        reset_credential_cipher_cache()
        assert credential_cipher_status().available is False  # 前置对照

        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"legacy_cipher": group_yaml(SENTINEL_KEY)})
        start_process(monkeypatch, yaml_path)
        before = yaml_path.read_bytes()

        await bootstrap()

        assert yaml_path.read_bytes() == before
        assert SENTINEL_KEY in yaml_path.read_text(encoding="utf-8")

    async def test_control_the_same_yaml_with_a_readable_row_does_persist(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """反向对照：没有解不开的行时同一份 YAML 会被落盘掩码——证明上面那条的「不落盘」
        是被「解不开」这一个条件挡住的，而不是落盘这条路径本身坏了"""
        await create_db_group("locked_out", SENTINEL_KEY)
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"locked_out": group_yaml(SENTINEL_KEY)})

        start_process(monkeypatch, yaml_path)
        before = yaml_path.read_bytes()
        await bootstrap()

        assert yaml_path.read_bytes() != before
        assert read_yaml_groups(yaml_path)["locked_out"]["API_KEY"] == PLACEHOLDER


# ============================================================
# 4. 占位符 YAML 配全新数据库：真实路径上要有点名 WARNING
# ============================================================


class TestHandEditedKeyAgainstAnExistingRowIsBackedUpBeforeItIsMasked:
    """库里已有同名行，YAML 里手填了一把不同的 Key（round-27 复核 P2-3，第二轮复核 P2-6 改方案）

    导入以 `name` 是否已存在为准，同名行不会被覆盖——手填的 Key 没导入。上一版只跳过启动那一次落盘，
    可告警建议的「后台编辑该模型组」正会触发落盘、把这份明文掩码掉：保护是漏的，告警说的也不准。

    现在的做法（取舍见 DECISIONS「手填 Key 与库内同名行分歧」）：首次掩码落盘**之前**，把分歧组手填的原值
    备份到 `<DATA_DIR>/configs/` 下一个 0600 权限的文件，然后照常掩码——YAML 里不再留明文，也不会反复告警。
    备份文件是一份新的明文副本，所以日志逐字点名路径并要求取出后删除；备份失败则退回旧做法（不落盘、
    明文留在 YAML 里），并如实说明后台编辑会抹掉它。
    """

    @staticmethod
    def _backups(boot_env: Path) -> List[Path]:
        directory = boot_env / "data" / "configs"
        return sorted(directory.glob("*handwritten*")) if directory.is_dir() else []

    async def test_diverging_yaml_key_is_backed_up_and_then_the_yaml_is_masked(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        logs: List[Tuple[str, str]],
    ) -> None:
        await create_db_group("edited", SENTINEL_KEY)
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"edited": group_yaml(SENTINEL_KEY_2)})

        start_process(monkeypatch, yaml_path)
        assert SENTINEL_KEY_2.encode() in yaml_path.read_bytes()  # 前置对照：手填的明文确实在文件里

        await bootstrap()

        assert read_yaml_groups(yaml_path)["edited"]["API_KEY"] == PLACEHOLDER
        assert SENTINEL_KEY_2 not in yaml_path.read_text(encoding="utf-8"), "YAML 里还留着明文"
        backups = self._backups(boot_env)
        assert len(backups) == 1, f"期望恰好一份备份: {backups}"
        saved = yaml.safe_load(backups[0].read_text(encoding="utf-8"))
        assert saved["MODEL_GROUPS"]["edited"]["API_KEY"] == SENTINEL_KEY_2, "备份里没有手填的原值"
        # 库内行不被覆盖（既有约束），运行时视图用库里的值
        assert (await DBModelGroup.get(name="edited")).api_key == SENTINEL_KEY
        assert model_group_service.config.MODEL_GROUPS["edited"].API_KEY == SENTINEL_KEY

    async def test_backup_file_is_owner_only(self, boot_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        if sys.platform == "win32":
            pytest.skip("Windows 上 POSIX 权限位不生效")
        await create_db_group("edited", SENTINEL_KEY)
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"edited": group_yaml(SENTINEL_KEY_2)})
        start_process(monkeypatch, yaml_path)

        await bootstrap()

        (backup,) = self._backups(boot_env)
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600

    def test_the_file_is_created_with_mode_0600_and_never_overwrites(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """跨平台钉住「创建那一刻就是 0600、O_EXCL 不覆盖」：Windows 上 stat 读不出权限位，改为截获 os.open 的参数"""
        calls: List[Tuple[int, int]] = []
        real_open = os.open

        def spy(path: Any, flags: int, mode: int = 0o777, *args: Any, **kwargs: Any) -> int:
            calls.append((flags, mode))
            return real_open(path, flags, mode, *args, **kwargs)

        monkeypatch.setattr(model_group_service.os, "open", spy)
        target = tmp_path / "private.yaml"

        model_group_service._write_private_file(target, "x: 1\n")

        assert calls and calls[0][1] == 0o600
        assert calls[0][0] & os.O_EXCL and calls[0][0] & os.O_CREAT
        with pytest.raises(FileExistsError):
            model_group_service._write_private_file(target, "overwritten\n")
        assert target.read_text(encoding="utf-8") == "x: 1\n"

    async def test_the_warning_is_accurate_names_the_backup_and_asks_to_delete_it(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        logs: List[Tuple[str, str]],
    ) -> None:
        await create_db_group("edited", SENTINEL_KEY)
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"edited": group_yaml(SENTINEL_KEY_2)})
        start_process(monkeypatch, yaml_path)

        await bootstrap()

        (backup,) = self._backups(boot_env)
        warned = [m for level, m in logs if level == "WARNING" and "edited" in m and "手填" in m]
        assert len(warned) == 1, f"没有点名的 WARNING: {logs}"
        text = warned[0]
        assert str(backup) in text, "没有说备份在哪"
        assert "明文" in text and "删除" in text, "没有提醒这是一份明文副本、取出后要删"
        assert "保持原样" not in text and "未落盘" not in text, "已经落盘掩码了，告警不能再说保持原样"
        assert SENTINEL_KEY_2 not in text and SENTINEL_KEY not in text, "告警本身不能带出 Key"

    async def test_the_group_with_an_empty_database_key_is_covered_too(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        logs: List[Tuple[str, str]],
    ) -> None:
        """库里那行没有 Key、YAML 里手填了一把：同样是「手填没导入」"""
        await create_db_group("blank", "")
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"blank": group_yaml(SENTINEL_KEY)})

        start_process(monkeypatch, yaml_path)
        await bootstrap()

        (backup,) = self._backups(boot_env)
        assert yaml.safe_load(backup.read_text(encoding="utf-8"))["MODEL_GROUPS"]["blank"]["API_KEY"] == SENTINEL_KEY
        assert any(level == "WARNING" and "blank" in m for level, m in logs)

    async def test_only_the_diverging_groups_are_in_the_backup(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """最小暴露：与库值一致的组（明文本来就在库里）不进备份；另一个正常组照常被掩码"""
        await create_db_group("edited", SENTINEL_KEY)
        await create_db_group("normal", SENTINEL_KEY_2)
        yaml_path = write_yaml(
            boot_env / "configs" / "nekro-agent.yaml",
            {"edited": group_yaml("sk-yamlpersist-SENTINEL-other-1"), "normal": group_yaml(SENTINEL_KEY_2)},
        )

        start_process(monkeypatch, yaml_path)
        await bootstrap()

        (backup,) = self._backups(boot_env)
        saved = yaml.safe_load(backup.read_text(encoding="utf-8"))["MODEL_GROUPS"]
        assert set(saved) == {"edited"}
        assert SENTINEL_KEY_2 not in backup.read_text(encoding="utf-8")
        assert read_yaml_groups(yaml_path)["normal"]["API_KEY"] == PLACEHOLDER

    async def test_a_backend_edit_after_startup_no_longer_loses_the_key(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """告警原先建议的「后台编辑模型组」= 触发落盘：此刻手填的 Key 仍然只在备份里、没有丢"""
        await create_db_group("edited", SENTINEL_KEY)
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"edited": group_yaml(SENTINEL_KEY_2)})
        start_process(monkeypatch, yaml_path)
        await bootstrap()

        await model_group_service.refresh_runtime_view()  # 后台任何一次写入都走这里落盘

        (backup,) = self._backups(boot_env)
        assert yaml.safe_load(backup.read_text(encoding="utf-8"))["MODEL_GROUPS"]["edited"]["API_KEY"] == SENTINEL_KEY_2
        assert read_yaml_groups(yaml_path)["edited"]["API_KEY"] == PLACEHOLDER

    async def test_a_restart_afterwards_is_quiet_and_makes_no_second_copy(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        logs: List[Tuple[str, str]],
    ) -> None:
        """不制造越攒越多的明文副本：YAML 已掩码，重启不再分歧、不再备份、不再告警"""
        await create_db_group("edited", SENTINEL_KEY)
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"edited": group_yaml(SENTINEL_KEY_2)})
        start_process(monkeypatch, yaml_path)
        await bootstrap()
        logs.clear()

        start_process(monkeypatch, yaml_path)
        await bootstrap()

        assert len(self._backups(boot_env)) == 1
        # 不再有「分歧」告警；留着的备份只由「残留」告警提醒（见 TestLeftoverBackupsAreFlaggedOnEveryStartup）
        assert not [m for level, m in logs if level == "WARNING" and "手填" in m and "残留" not in m]

    async def test_if_the_backup_cannot_be_written_nothing_is_masked_and_the_log_says_so(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        logs: List[Tuple[str, str]],
    ) -> None:
        """备份失败 = 无从保证手填的 Key 不丢：退回旧做法，不落盘，并如实说明后台编辑会抹掉它"""
        await create_db_group("edited", SENTINEL_KEY)
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"edited": group_yaml(SENTINEL_KEY_2)})
        start_process(monkeypatch, yaml_path)
        before = yaml_path.read_bytes()

        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise OSError("disk says no")

        monkeypatch.setattr(model_group_service, "_write_private_file", refuse)

        await bootstrap()

        assert yaml_path.read_bytes() == before, "备份失败时不该掩码"
        assert not self._backups(boot_env)
        warned = [m for level, m in logs if level == "WARNING" and "edited" in m and "手填" in m]
        assert warned and "未落盘" in warned[0] and "后台" in warned[0], f"没有如实说明: {warned}"

    async def test_undecryptable_rows_still_skip_the_persist_and_make_no_backup(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """同时有解不开的行：本来就不落盘（YAML 里明文保持原样），再备份只会多出一份明文"""
        await create_db_group("locked_out", SENTINEL_KEY)
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_MATERIAL_B)
        reset_credential_cipher_cache()
        await create_db_group("edited", SENTINEL_KEY_2)
        yaml_path = write_yaml(
            boot_env / "configs" / "nekro-agent.yaml",
            {"locked_out": group_yaml(SENTINEL_KEY), "edited": group_yaml("sk-yamlpersist-SENTINEL-other-1")},
        )
        start_process(monkeypatch, yaml_path)
        before = yaml_path.read_bytes()

        await bootstrap()

        assert yaml_path.read_bytes() == before
        assert not self._backups(boot_env)

    async def test_control_matching_key_still_gets_masked_without_a_warning_or_a_backup(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        logs: List[Tuple[str, str]],
    ) -> None:
        """反向对照：YAML 明文与库值一致 = 升级部署的典型形态，照常掩码、不告警、不备份"""
        await create_db_group("same", SENTINEL_KEY)
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"same": group_yaml(SENTINEL_KEY)})

        start_process(monkeypatch, yaml_path)
        await bootstrap()

        assert read_yaml_groups(yaml_path)["same"]["API_KEY"] == PLACEHOLDER
        assert not [m for level, m in logs if level == "WARNING" and "手填" in m]
        assert not self._backups(boot_env)

    async def test_control_placeholder_in_yaml_is_not_a_hand_edit(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await create_db_group("masked", SENTINEL_KEY)
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"masked": group_yaml(PLACEHOLDER)})

        start_process(monkeypatch, yaml_path)
        await bootstrap()

        assert read_yaml_groups(yaml_path)["masked"]["API_KEY"] == PLACEHOLDER
        assert model_group_service.config.MODEL_GROUPS["masked"].API_KEY == SENTINEL_KEY
        assert not self._backups(boot_env)


class TestFreshDatabaseRestoreWarnsOnTheRealPath:
    async def test_placeholder_yaml_against_an_empty_database_names_the_group(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        logs: List[Tuple[str, str]],
    ) -> None:
        """旧实现的告警分支在生产上走不到：load 已把占位符归一成空串，
        `import_config_group` 看到的永远是 ''。这里从磁盘 YAML 一路走到 bootstrap。"""
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"restored": group_yaml(PLACEHOLDER)})

        loaded = start_process(monkeypatch, yaml_path)
        assert loaded.MODEL_GROUPS["restored"].API_KEY == ""  # 前置：进程里确实已经是空串，不是占位符
        await bootstrap()

        row = await DBModelGroup.get(name="restored")
        assert row.chat_model == "gpt-4o"  # 正向对照：组本身导入成功
        assert await raw_api_key("restored") == ""  # 占位串绝不当凭据入库
        named = [m for level, m in logs if level == "WARNING" and "restored" in m and "占位符" in m]
        assert len(named) == 1, f"期望恰好一条点名 WARNING，实际 {named}"
        # 库接管之后文件反映的是库里的真值：这个组确实没有 Key，所以是空串而不是占位符
        # （占位符的意思是「Key 在数据库里」，这里不是）
        assert read_yaml_groups(yaml_path)["restored"]["API_KEY"] == ""

    async def test_no_warning_on_a_normal_restart(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        logs: List[Tuple[str, str]],
    ) -> None:
        """对照：库里已有这个组（正常重启）时不该吓运维——占位符告警只属于「库里没有」的恢复场景"""
        await create_db_group("has_key", SENTINEL_KEY)
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"has_key": group_yaml(PLACEHOLDER)})

        start_process(monkeypatch, yaml_path)
        await bootstrap()

        assert not [m for level, m in logs if level == "WARNING" and "占位符" in m]


# ============================================================
# 5. 掩码做在序列化结果上：运行中的 MODEL_GROUPS 不被替换
# ============================================================


class TestDumpNeverSwapsTheLiveView:
    async def test_the_view_object_and_its_keys_are_untouched_during_a_masked_dump(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await create_db_group("has_key", SENTINEL_KEY)
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"has_key": group_yaml(PLACEHOLDER)})
        loaded = start_process(monkeypatch, yaml_path)
        await bootstrap()

        view = loaded.MODEL_GROUPS
        group = view["has_key"]
        seen: List[Tuple[int, int, str]] = []

        import nekro_agent.core.core_utils as core_utils

        real_dump = core_utils.yaml.dump

        def spying_dump(*args: Any, **kwargs: Any) -> str:
            # 序列化发生的那一刻：运行中的视图仍是同一个对象、里头仍是明文
            seen.append(
                (id(loaded.MODEL_GROUPS), id(loaded.MODEL_GROUPS["has_key"]), loaded.MODEL_GROUPS["has_key"].API_KEY)
            )
            return real_dump(*args, **kwargs)

        monkeypatch.setattr(core_utils.yaml, "dump", spying_dump)
        loaded.dump_config()

        assert seen == [(id(view), id(group), SENTINEL_KEY)]
        assert loaded.MODEL_GROUPS is view and loaded.MODEL_GROUPS["has_key"] is group
        assert group.API_KEY == SENTINEL_KEY
        assert read_yaml_groups(yaml_path)["has_key"]["API_KEY"] == PLACEHOLDER  # 对照：这次 dump 确实掩码了

    async def test_model_groups_is_never_reassigned_while_dumping(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """比「对象身份没变」更强：换进去再换回来也算——属性赋值本身就不该发生"""
        await create_db_group("has_key", SENTINEL_KEY)
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"has_key": group_yaml(PLACEHOLDER)})
        loaded = start_process(monkeypatch, yaml_path)
        await bootstrap()

        assigned: List[str] = []
        original_setattr = CoreConfig.__setattr__

        def recording_setattr(self: CoreConfig, name: str, value: Any) -> None:
            assigned.append(name)
            original_setattr(self, name, value)

        monkeypatch.setattr(CoreConfig, "__setattr__", recording_setattr)
        loaded.dump_config()
        loaded.dump_config(mask_model_group_secrets=False)

        assert "MODEL_GROUPS" not in assigned, f"dump 期间赋值了字段: {assigned}"


class TestOtherConfigBaseSubclassesAreUnchanged:
    """抽出的序列化钩子对不覆盖它的子类是恒等的：落盘字节与改造前的公式逐字相同"""

    def _make_toy(self):
        from pydantic import Field

        class ToyConfig(ConfigBase):
            NAME: str = Field(default="东方")
            SECRET: str = Field(default="s3cr3t-toy")
            NESTED: Dict[str, Dict[str, str]] = Field(default_factory=lambda: {"a": {"API_KEY": "toy-nested-key"}})

        return ToyConfig()

    def test_yaml_bytes_are_identical_to_the_pre_hook_formula(self, tmp_path: Path) -> None:
        toy = self._make_toy()
        target = tmp_path / "toy.yaml"
        toy.dump_config(target)

        reference = yaml.dump(data=toy.model_dump(), default_flow_style=False, sort_keys=False, allow_unicode=True)
        assert target.read_text(encoding="utf-8") == reference
        # 正向对照：确实写了东西、且嵌套里的「像凭据」的字段没有被任何人掩码（掩码只属于 CoreConfig）
        assert "toy-nested-key" in reference

    def test_json_bytes_are_identical_to_the_pre_hook_formula(self, tmp_path: Path) -> None:
        toy = self._make_toy()
        target = tmp_path / "toy.json"
        toy.dump_config(target)
        assert target.read_text(encoding="utf-8") == toy.model_dump_json(indent=2)

    def test_the_default_hook_is_model_dump(self) -> None:
        toy = self._make_toy()
        assert toy._dump_data() == toy.model_dump()  # noqa: SLF001

    def test_core_config_overrides_the_hook(self) -> None:
        """接线：掩码必须挂在钩子上；钩子没被覆盖时 CoreConfig 的落盘就是明文"""
        assert CoreConfig._dump_data is not ConfigBase._dump_data  # noqa: SLF001


class TestCoreConfigRefusesNonYamlTargets:
    def test_json_target_is_refused_instead_of_silently_writing_plaintext(self, tmp_path: Path) -> None:
        """钩子只作用于 YAML 序列化；允许 `.json` 落盘就等于给明文开了一扇没人看的门"""
        core = CoreConfig()
        target = tmp_path / "core.json"
        with pytest.raises(ValueError, match="YAML"):
            core.dump_config(target)
        assert not target.exists()


# ============================================================
# 6. 接线：生产代码里的那几句确实是我们模拟的样子（§2.7）
# ============================================================


def _module_level_calls(source: str) -> List[ast.Call]:
    tree = ast.parse(source)
    return [node.value for node in tree.body if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)]


class TestTheImportTimeDumpIsStillWiredTheWayWeSimulateIt:
    def test_core_config_dumps_unmasked_once_at_import_time(self) -> None:
        """`start_process` 模拟的导入期 dump 必须与 `core/config.py` 底部那一句一致"""
        source = Path(core_config_module.__file__).read_text(encoding="utf-8")
        dumps = [
            call
            for call in _module_level_calls(source)
            if isinstance(call.func, ast.Attribute) and call.func.attr == "dump_config"
        ]
        assert len(dumps) == 1
        keywords = {kw.arg: kw.value for kw in dumps[0].keywords}
        flag = keywords.get("mask_model_group_secrets")
        assert isinstance(flag, ast.Constant) and flag.value is False

    def test_bootstrap_persists_through_refresh_runtime_view_not_a_second_path(self) -> None:
        """落盘必须由 `refresh_runtime_view(persist=...)` 一条路径完成，且 persist 取自
        「有没有解不开的行」而不是 `bool(imported)`"""
        source = (Path(model_group_service.__file__)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        func = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "bootstrap_model_groups"
        )
        refresh_calls: List[Optional[ast.expr]] = []
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "refresh_runtime_view"
            ):
                refresh_calls.append(next((kw.value for kw in node.keywords if kw.arg == "persist"), None))
        assert len(refresh_calls) == 1
        persist = refresh_calls[0]
        assert persist is not None
        assert "imported" not in ast.dump(persist), "persist 不能再取决于「这次有没有导入」"


# ============================================================
# 5. 手填 Key 备份文件：平台口径如实、残留备份启动时告警（round-27 第三轮复核 P2-5）
# ============================================================


class _FakeIcacls:
    """截获 `subprocess.run`：记下调用，按预设返回码应答（不真的改 ACL）"""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.calls: List[List[str]] = []

    def __call__(self, args: Any, *unused: Any, **kwargs: Any) -> Any:
        import subprocess

        self.calls.append([str(a) for a in args])
        return subprocess.CompletedProcess(args, self.returncode, stdout="", stderr="denied")


def _simulate_windows_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟 Windows 时连当前账户的环境变量一并模拟：实现靠 `USERNAME` 拼 icacls 的授权主体，
    Linux 上没有这个变量，不设的话实现会在调用 icacls 之前就判定「取不到用户名」。"""
    monkeypatch.setenv("USERNAME", "na-test-user")
    monkeypatch.setenv("USERDOMAIN", "NA-TEST-HOST")


class TestBackupFileProtectionIsReportedHonestlyPerPlatform:
    """Windows 上 `os.open(..., 0o600)` 只影响只读位，不是 ACL：日志写死「权限 0600」在那里不成立。

    现在：Windows 上显式用 `icacls` 收紧为仅当前用户（成功则如实说「已收紧」，失败则如实告警「未能收紧」）；
    POSIX 上仍是 0600。日志里的说法随实际结果走。
    """

    @staticmethod
    async def _run_backup(boot_env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        await create_db_group("edited", SENTINEL_KEY)
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"edited": group_yaml(SENTINEL_KEY_2)})
        start_process(monkeypatch, yaml_path)
        await bootstrap()
        return yaml_path

    @staticmethod
    def _backup_warning(logs: List[Tuple[str, str]]) -> str:
        warned = [m for level, m in logs if level == "WARNING" and "edited" in m and "手填" in m]
        assert len(warned) == 1, logs
        return warned[0]

    async def test_windows_tightens_the_acl_with_icacls_and_says_so(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        logs: List[Tuple[str, str]],
    ) -> None:
        fake = _FakeIcacls(returncode=0)
        monkeypatch.setattr(model_group_service, "_is_windows", lambda: True)
        _simulate_windows_account(monkeypatch)
        monkeypatch.setattr(model_group_service.subprocess, "run", fake)

        await self._run_backup(boot_env, monkeypatch)

        (backup,) = TestHandEditedKeyAgainstAnExistingRowIsBackedUpBeforeItIsMasked._backups(boot_env)
        assert len(fake.calls) == 1
        command = fake.calls[0]
        assert Path(command[0]).stem.lower() == "icacls" and command[1] == str(backup)
        assert "/inheritance:r" in command and "/grant:r" in command
        assert "NA-TEST-HOST\\na-test-user:F" in command, "授权主体应是当前账户（域\\用户），而不是别人"
        text = self._backup_warning(logs)
        assert "0600" not in text, "Windows 上不存在 0600，不能这么说"
        assert "仅当前用户" in text

    async def test_windows_failure_to_tighten_is_a_loud_warning_not_a_silent_claim(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        logs: List[Tuple[str, str]],
    ) -> None:
        monkeypatch.setattr(model_group_service, "_is_windows", lambda: True)
        _simulate_windows_account(monkeypatch)
        monkeypatch.setattr(model_group_service.subprocess, "run", _FakeIcacls(returncode=5))

        await self._run_backup(boot_env, monkeypatch)

        text = self._backup_warning(logs)
        assert "0600" not in text and "仅当前用户" not in text
        assert "未能收紧" in text and "其他账户" in text
        assert "删除" in text and "明文" in text

    async def test_posix_keeps_0600_and_never_calls_icacls(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        logs: List[Tuple[str, str]],
    ) -> None:
        fake = _FakeIcacls(returncode=0)
        monkeypatch.setattr(model_group_service, "_is_windows", lambda: False)
        monkeypatch.setattr(model_group_service.subprocess, "run", fake)

        await self._run_backup(boot_env, monkeypatch)

        assert fake.calls == []
        assert "0600" in self._backup_warning(logs)

    @pytest.mark.skipif(sys.platform != "win32", reason="真实 ACL 只在 Windows 上有意义")
    async def test_real_windows_acl_leaves_only_the_current_user(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import subprocess

        await self._run_backup(boot_env, monkeypatch)

        (backup,) = TestHandEditedKeyAgainstAnExistingRowIsBackedUpBeforeItIsMasked._backups(boot_env)
        listing = subprocess.run(["icacls", str(backup)], capture_output=True, text=True, check=True).stdout
        entries = [line.strip() for line in listing.splitlines() if "(" in line and ":" in line]
        assert entries, listing
        user = os.environ["USERNAME"].lower()
        for entry in entries:
            assert user in entry.lower(), f"ACL 里还有别人: {entry}"
            assert "(i)" not in entry.lower(), f"仍在继承目录权限: {entry}"
        for broad in ("everyone", "users", "authenticated users"):
            assert not any(entry.lower().startswith(broad) or f"\\{broad}:" in entry.lower() for entry in entries)


class TestLeftoverBackupsAreFlaggedOnEveryStartup:
    """备份是一份明文副本，只在日志里叮嘱「取出后删除」一次不够：运维错过那一条之后就再没人提醒。
    每次启动发现数据目录里还留着备份，就点名路径再提醒一次。"""

    @staticmethod
    def _plant(boot_env: Path, stamp: str = "20250101T000000000000Z") -> Path:
        directory = boot_env / "data" / "configs"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{model_group_service.HANDWRITTEN_KEYS_BACKUP_PREFIX}.{stamp}.yaml"
        path.write_text("MODEL_GROUPS: {}\n", encoding="utf-8")
        return path

    @staticmethod
    def _leftover_warnings(logs: List[Tuple[str, str]]) -> List[str]:
        return [m for level, m in logs if level == "WARNING" and "残留" in m and "备份" in m]

    async def test_a_leftover_backup_is_named_with_a_request_to_delete_it(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        logs: List[Tuple[str, str]],
    ) -> None:
        planted = self._plant(boot_env)
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {})
        start_process(monkeypatch, yaml_path)

        await bootstrap()

        warned = self._leftover_warnings(logs)
        assert len(warned) == 1, logs
        assert str(planted) in warned[0]
        assert "明文" in warned[0] and "删除" in warned[0]

    async def test_every_leftover_is_named(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        logs: List[Tuple[str, str]],
    ) -> None:
        first = self._plant(boot_env, "20250101T000000000000Z")
        second = self._plant(boot_env, "20250202T000000000000Z")
        start_process(monkeypatch, write_yaml(boot_env / "configs" / "nekro-agent.yaml", {}))

        await bootstrap()

        (text,) = self._leftover_warnings(logs)
        assert str(first) in text and str(second) in text

    async def test_no_backup_means_no_warning(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        logs: List[Tuple[str, str]],
    ) -> None:
        start_process(monkeypatch, write_yaml(boot_env / "configs" / "nekro-agent.yaml", {}))

        await bootstrap()

        assert self._leftover_warnings(logs) == []

    async def test_the_backup_made_in_this_very_startup_is_not_reported_as_a_leftover(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        logs: List[Tuple[str, str]],
    ) -> None:
        """这次启动刚写出的备份由「手填」那条告警负责说明；残留告警只说启动之前就在的"""
        await create_db_group("edited", SENTINEL_KEY)
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"edited": group_yaml(SENTINEL_KEY_2)})
        start_process(monkeypatch, yaml_path)

        await bootstrap()

        assert self._leftover_warnings(logs) == []

    async def test_a_restart_after_the_backup_was_made_flags_it(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        logs: List[Tuple[str, str]],
    ) -> None:
        await create_db_group("edited", SENTINEL_KEY)
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"edited": group_yaml(SENTINEL_KEY_2)})
        start_process(monkeypatch, yaml_path)
        await bootstrap()
        (backup,) = TestHandEditedKeyAgainstAnExistingRowIsBackedUpBeforeItIsMasked._backups(boot_env)
        logs.clear()

        start_process(monkeypatch, yaml_path)
        await bootstrap()

        (text,) = self._leftover_warnings(logs)
        assert str(backup) in text

# ============================================================
# 6. A4（#61 残留）：解不开的行存在时，之后别的配置落盘不再把这些组的 YAML 明文抹成空串
# ============================================================


class TestLaterSavesKeepTheYamlValueOfGroupsTheDatabaseCannotOpen:
    """启动时因「库里有解不开的行」不落盘，保住了 YAML 里可能是唯一副本的明文——但那只保护「启动那一趟」。
    启动之后**任何一次**别的配置落盘（WebUI 保存任意系统配置、后台改任一模型组）都走 `dump_config`，而内存视图里
    解不开的组 Key 是 fail-closed 的空串，于是 YAML 里的明文被写成 `API_KEY: ''`，再也找不回来。

    修法（最小可行，取舍见 DECISIONS）：启动时记下这些组 YAML 里原本的值（明文或占位符），落盘时对「内存里 Key 为空
    且被记下」的组原样写回；其余组照常掩码。库接管之后（密钥恢复重启、或后台填入新 Key、或组被删）记账自然失效。
    """

    @staticmethod
    async def _make_locked_out(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
        """用密钥 A 加密落库，再换成无关的 B —— 这一行从此解不开"""
        await create_db_group(name, SENTINEL_KEY)
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_MATERIAL_B)
        reset_credential_cipher_cache()

    async def _boot_with_a_locked_out_group(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        yaml_key: str = SENTINEL_KEY,
        with_healthy: bool = True,
    ) -> Path:
        await self._make_locked_out(monkeypatch, "locked_out")
        groups = {"locked_out": group_yaml(yaml_key)}
        if with_healthy:
            await create_db_group("healthy", SENTINEL_KEY_2)  # 换密钥之后写入，解得开
            groups["healthy"] = group_yaml(SENTINEL_KEY_2)
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", groups)
        start_process(monkeypatch, yaml_path)
        await bootstrap()
        return yaml_path

    async def test_a_later_config_save_does_not_wipe_the_plaintext(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        yaml_path = await self._boot_with_a_locked_out_group(boot_env, monkeypatch)
        assert read_yaml_groups(yaml_path)["locked_out"]["API_KEY"] == SENTINEL_KEY  # 前置：启动不落盘，明文还在

        core_config_module.save_config()  # WebUI 里保存任意一项系统配置走的就是它

        groups = read_yaml_groups(yaml_path)
        assert groups["locked_out"]["API_KEY"] == SENTINEL_KEY, "别的配置落盘把解不开的组的明文抹成了空串"
        assert groups["healthy"]["API_KEY"] == PLACEHOLDER, "解得开的组照常掩码，不能被一并留成明文"
        assert SENTINEL_KEY_2 not in yaml_path.read_text(encoding="utf-8")

    async def test_other_settings_are_saved_normally_alongside(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        yaml_path = await self._boot_with_a_locked_out_group(boot_env, monkeypatch)
        loaded = model_group_service.config
        loaded.AI_CHAT_PRESET_NAME = "改名后的预设"

        core_config_module.save_config()

        saved = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        assert saved["AI_CHAT_PRESET_NAME"] == "改名后的预设"
        assert saved["MODEL_GROUPS"]["locked_out"]["API_KEY"] == SENTINEL_KEY

    async def test_a_backend_edit_of_another_group_does_not_wipe_it_either(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """后台改任一模型组都走 `refresh_runtime_view()` 落盘"""
        yaml_path = await self._boot_with_a_locked_out_group(boot_env, monkeypatch)

        await model_group_service.refresh_runtime_view()

        assert read_yaml_groups(yaml_path)["locked_out"]["API_KEY"] == SENTINEL_KEY

    async def test_a_placeholder_in_the_yaml_stays_a_placeholder(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """YAML 里本来就是占位符（Key 在库里）、库里那行解不开：后续落盘不能把它退化成「没配 Key」的空串"""
        yaml_path = await self._boot_with_a_locked_out_group(boot_env, monkeypatch, yaml_key=PLACEHOLDER)

        core_config_module.save_config()

        assert read_yaml_groups(yaml_path)["locked_out"]["API_KEY"] == PLACEHOLDER

    async def test_a_group_with_no_key_in_the_yaml_is_still_written_as_empty(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """不发明内容：YAML 里本来就是空串的组，记账里没有它，仍写空串"""
        yaml_path = await self._boot_with_a_locked_out_group(boot_env, monkeypatch, yaml_key="")

        core_config_module.save_config()

        assert read_yaml_groups(yaml_path)["locked_out"]["API_KEY"] == ""

    async def test_restoring_the_key_and_restarting_masks_the_yaml_and_ends_the_hold(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 不带 healthy：它是用密钥 B 写的，换回 A 之后它自己就解不开了
        yaml_path = await self._boot_with_a_locked_out_group(boot_env, monkeypatch, with_healthy=False)
        core_config_module.save_config()
        assert read_yaml_groups(yaml_path)["locked_out"]["API_KEY"] == SENTINEL_KEY

        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_MATERIAL_A)  # 原密钥放回去
        reset_credential_cipher_cache()
        start_process(monkeypatch, yaml_path)
        await bootstrap()

        assert read_yaml_groups(yaml_path)["locked_out"]["API_KEY"] == PLACEHOLDER
        assert SENTINEL_KEY not in yaml_path.read_text(encoding="utf-8")
        core_config_module.save_config()
        assert read_yaml_groups(yaml_path)["locked_out"]["API_KEY"] == PLACEHOLDER
        assert model_group_service.config.held_model_group_names() == frozenset()

    async def test_an_explicit_new_key_replaces_the_hold(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """后台显式填入新 Key 是覆盖指令：之后落盘写掩码，不再把旧明文钉在 YAML 里"""
        yaml_path = await self._boot_with_a_locked_out_group(boot_env, monkeypatch)
        row = await DBModelGroup.get(name="locked_out")
        row.api_key = SENTINEL_KEY_2 + "-new"
        await row.save()

        await model_group_service.refresh_runtime_view()

        assert read_yaml_groups(yaml_path)["locked_out"]["API_KEY"] == PLACEHOLDER
        assert SENTINEL_KEY not in yaml_path.read_text(encoding="utf-8")

    async def test_clearing_the_key_afterwards_does_not_resurrect_the_old_plaintext(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """换了新 Key 之后记账就该清掉；否则日后把 Key 清空，旧明文会从记账里复活写回 YAML"""
        yaml_path = await self._boot_with_a_locked_out_group(boot_env, monkeypatch)
        row = await DBModelGroup.get(name="locked_out")
        row.api_key = SENTINEL_KEY_2 + "-new"
        await row.save()
        await model_group_service.refresh_runtime_view()

        row = await DBModelGroup.get(name="locked_out")
        row.api_key = ""
        await row.save()
        await model_group_service.refresh_runtime_view()

        assert read_yaml_groups(yaml_path)["locked_out"]["API_KEY"] == ""
        assert SENTINEL_KEY not in yaml_path.read_text(encoding="utf-8")

    async def test_a_deleted_group_leaves_no_hold_behind(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """组删掉后记账一并清掉；同名重建一个没配 Key 的组，YAML 里不会冒出旧明文"""
        yaml_path = await self._boot_with_a_locked_out_group(boot_env, monkeypatch)
        await (await DBModelGroup.get(name="locked_out")).delete()
        await model_group_service.refresh_runtime_view()
        await create_db_group("locked_out", "")

        await model_group_service.refresh_runtime_view()

        assert read_yaml_groups(yaml_path)["locked_out"]["API_KEY"] == ""
        assert SENTINEL_KEY not in yaml_path.read_text(encoding="utf-8")

    async def test_control_without_undecryptable_rows_a_later_save_masks_as_before(
        self,
        boot_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await create_db_group("healthy", SENTINEL_KEY)
        yaml_path = write_yaml(boot_env / "configs" / "nekro-agent.yaml", {"healthy": group_yaml(SENTINEL_KEY)})
        start_process(monkeypatch, yaml_path)
        await bootstrap()

        core_config_module.save_config()

        assert read_yaml_groups(yaml_path)["healthy"]["API_KEY"] == PLACEHOLDER
        assert model_group_service.config.held_model_group_names() == frozenset()