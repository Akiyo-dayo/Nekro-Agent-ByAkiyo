"""模型组 `api_key` 的静态加密（数据模型要求 `api_key` 加密存储）

## 这里钉的是什么

改造前 `db_model_group.api_key = TextField()`，全仓无加解密：**一次库泄露或一份备份
= 所有客户的 Key 全裸**。现在 `api_key` 是 `EncryptedTextField`，库里恒为密文、
Python 属性恒为明文。

## 每条断言都必须能因"修复缺失"而变红

本文件不做空断言（掩码空串还是空串，断言恒成立）。做法统一是
**先写哨兵值造出差异**：

- 每个模型组都带一个 `sk-...` 形态的哨兵 Key；
- 判"库里是不是密文"用的是**原始列**（`execute_query_dict`，绕开 ORM 的字段转换），
  而不是 ORM 属性 —— 后者永远是明文，拿它断言什么都证明不了；
- 每一条"库里看不到明文"都配一条**正向对照**：同一行的 `base_url`（普通 `TextField`）
  在同一份原始列里**逐字可见**。没有这条对照的话，一个查错了表/查错了列的探针
  也能让"看不到明文"通过（§2.22：对照组和被测项一起失败时，那不是证据）。

## 测试对环境变量封闭（§2.9 / §2.13）

密钥来源是 `OsEnv.CREDENTIAL_SECRET_KEY` 与 `OsEnv.DATA_DIR`，两者在真部署上都会被设成
非默认值。所有用例一律 `monkeypatch` 这两项到 tmp 目录，并在改完之后调
`reset_credential_cipher_cache()`；没有任何一条用例依赖开发机上那份 `data/` 目录。
"""

import base64
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional

import pytest
from tortoise import Tortoise, connections

from nekro_agent.core.config import ModelConfigGroup
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_model_group import DBModelGroup, EncryptedTextField
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.tenant import CREDENTIAL_MASK, TenantRole
from nekro_agent.services import model_group_service
from nekro_agent.services.model_group_service import (
    build_model_group_info,
    resolve_console_identity,
    update_group,
    upgrade_credentials_at_rest,
)
from nekro_agent.services.user.role import Role
from nekro_agent.tools.credential_cipher import (
    ENVELOPE_PREFIX,
    KEY_FILE_NAME,
    credential_cipher_status,
    decrypt_credential,
    decrypt_credential_with_source,
    encrypt_credential,
    is_encrypted_envelope,
    needs_reencrypt,
    reset_credential_cipher_cache,
)

# 哨兵：自己编的，不是任何真实凭据（§2.13 那条教训里踩过"样例凭据其实是真口令"）
SENTINEL_KEY = "sk-atrest-SENTINEL-7Q4b91xz"
SENTINEL_BASE_URL = "https://sentinel-upstream.example/v1"
SENTINEL_PROXY = "http://sentinel-proxy.example:8080"

ACME_TENANT_ID = 1

# 一批形态各异的明文，用来钉"round-trip 逐字相等"
ROUND_TRIP_SAMPLES: List[str] = [
    SENTINEL_KEY,
    "sk-proj-0123456789abcdefghijklmnopqrstuvwxyz",
    "带中文的凭据-哨兵",
    "with space and = padding chars",
    "x",
    "y" * 4096,
]

# "明文不出现在密文里"只对足够长的样例有意义：单个字符（"x"）会以约 1/64 的概率
# 出现在任意一段 base64 里，拿它断言等于给自己埋一条随机变红的用例 ——
# 那是假红，不是信号（不要用随机输入做伪 fuzz）。
NON_LEAK_SAMPLES: List[str] = [value for value in ROUND_TRIP_SAMPLES if len(value) >= 8]


def _make_user(*, user_id: int, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    user = DBUser(
        username=f"user-{tenant_role.value}-{tenant_id}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm_level,
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )
    user.id = user_id
    return user


def platform_owner() -> DBUser:
    return _make_user(user_id=1, tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


def acme_admin() -> DBUser:
    return _make_user(user_id=11, tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


async def _raw_rows() -> List[Dict[str, Any]]:
    """直接读原始列，绕开 ORM 的字段转换

    这是本文件唯一有资格回答"库里存的到底是什么"的入口：`row.api_key` 已经被
    `EncryptedTextField.to_python_value()` 解过密，拿它断言等于什么都没测。
    """
    conn = connections.get("default")
    return await conn.execute_query_dict(
        'SELECT "id", "name", "api_key", "base_url" FROM "model_group" ORDER BY "id"',
    )


async def _raw_api_key(name: str) -> str:
    rows = [row for row in await _raw_rows() if row["name"] == name]
    assert len(rows) == 1, f"期望恰好一行 name={name}，实际 {len(rows)} 行"
    return rows[0]["api_key"]


async def _insert_raw(name: str, api_key: str, base_url: str = SENTINEL_BASE_URL) -> None:
    """绕开 ORM 直接写一行（用来造"存量明文"这种 ORM 现在已经写不出来的形态）"""
    conn = connections.get("default")
    await conn.execute_query(
        'INSERT INTO "model_group" '
        '("name", "ownership", "owner_tenant_id", "visibility", "group_name", "chat_model", '
        '"chat_proxy", "base_url", "api_key", "model_type", "enable_vision", "enable_cot", '
        '"token_input_rate", "token_completion_rate", "model_price_rate") '
        f"VALUES ('{name}', 'platform', NULL, 'private', '{name}', 'gpt-4o', "
        f"'', '{base_url}', '{api_key}', 'chat', 0, 0, 1, 1, 1)",
    )


def _real_config_file() -> Path:
    """开发机上那份真实的配置文件（可能不存在）

    `refresh_runtime_view(persist=True)` 会把运行时视图 dump 进它。本文件里
    `create_group` / `update_group` 都会走到那一步，所以必须把这条落盘路径掐掉——
    掐不掉的后果不是"测试脏"，是**把开发机的配置文件真的改掉**：
    第一版没掐，跑完之后 `MODEL_GROUPS` 只剩本文件的哨兵组，`USE_MODEL_GROUP: default`
    从此指空，全量跑出来 17 条 `KeyError: 模型组 'default' 不存在`（全在 `test_affection_*`，
    因为它们按字母序排在前面、先读到被改坏的那份配置）。

    ⚠️ 路径取 `core.config.CONFIG_PATH` 本人，不自己按 `OsEnv.DATA_DIR` 拼一遍：
    那个常量是**导入期**就从 `OsEnv.DATA_DIR` 算好的，测试把 `OsEnv.DATA_DIR`
    monkeypatch 到 tmp **不会**让它跟着变 —— 而这正是第一版漏掉这条落盘路径的原因。
    """
    from nekro_agent.core.config import CONFIG_PATH

    return CONFIG_PATH


@pytest.fixture
async def cipher_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """一套与本机环境无关的加密环境 + 一张空的 `model_group` 表

    数据目录钉到 tmp（密钥文件会落在这里），环境变量密钥默认清空 ——
    也就是让每条用例都从"什么都不配的部署"这一档起跑。

    另外两件事都是为了不碰开发机上的真实状态：
    `save_config` 换成 no-op（理由见 `_real_config_file`），
    `config.MODEL_GROUPS` 用 monkeypatch 接管（`refresh_runtime_view` 会就地覆盖它，
    而它是个**进程级全局**，不还原就会漏给同一次会话里后面的每一条用例）。
    """
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "")
    monkeypatch.setattr(model_group_service, "save_config", lambda: None)
    monkeypatch.setattr(model_group_service.config, "MODEL_GROUPS", {})
    reset_credential_cipher_cache()

    config_file = _real_config_file()
    before = config_file.read_bytes() if config_file.is_file() else None

    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield tmp_path
    finally:
        await Tortoise.close_connections()
        reset_credential_cipher_cache()
        # 看门人：任何一条用例把真实配置写坏，都在**它自己**这里变红，
        # 而不是留给按字母序排在后面的别人去红（那次排查花了整整一轮全量跑）
        if before is not None:
            assert config_file.read_bytes() == before, (
                f"用例改写了开发机上的真实配置文件 {config_file}；"
                "`refresh_runtime_view(persist=True)` 的落盘路径没有被掐掉"
            )


@pytest.fixture
def error_logs():
    """抓住产品代码真正打出去的 ERROR

    **不能用 `caplog`**：`get_sub_logger()` 回的是 loguru 的 logger，不进 stdlib logging，
    `caplog.records` 恒为空 —— 拿它断言"打了 ERROR"会对任何实现都通过
    （项目里已有同款注释，见 `tests/test_identity_scope_foundation.py`）。
    """
    from loguru import logger

    captured: List[str] = []
    sink_id = logger.add(lambda message: captured.append(message.record["message"]), level="ERROR")
    try:
        yield captured
    finally:
        logger.remove(sink_id)


async def _create_group(name: str = "sentinel_group", api_key: str = SENTINEL_KEY) -> DBModelGroup:
    return await DBModelGroup.create(
        name=name,
        ownership="platform",
        owner_tenant_id=None,
        visibility="shared",
        group_name=name,
        chat_model="gpt-4o",
        base_url=SENTINEL_BASE_URL,
        chat_proxy=SENTINEL_PROXY,
        api_key=api_key,
        model_type="chat",
    )


class TestCipherRoundTrip:
    """信封本身：密文里看不见明文，解回来逐字相等"""

    @pytest.mark.parametrize("plain", ROUND_TRIP_SAMPLES)
    def test_round_trip_is_lossless(self, cipher_env: Path, plain: str) -> None:
        envelope = encrypt_credential(plain)
        assert envelope != plain
        assert envelope.startswith(ENVELOPE_PREFIX)
        assert decrypt_credential(envelope) == plain

    @pytest.mark.parametrize("plain", NON_LEAK_SAMPLES)
    def test_plaintext_does_not_appear_in_the_envelope(self, cipher_env: Path, plain: str) -> None:
        assert plain not in encrypt_credential(plain)

    def test_same_plaintext_encrypts_differently_each_time(self, cipher_env: Path) -> None:
        """随机 nonce：同一个 Key 写两次得到两个不同的密文

        这条同时是"不要按这一列做等值过滤"那条注释的依据。
        """
        first = encrypt_credential(SENTINEL_KEY)
        second = encrypt_credential(SENTINEL_KEY)
        assert first != second
        assert decrypt_credential(first) == decrypt_credential(second) == SENTINEL_KEY

    def test_empty_stays_empty(self, cipher_env: Path) -> None:
        """空凭据不封信封：没有东西要保护，库里保持可读的空串更好排障"""
        assert encrypt_credential("") == ""
        assert decrypt_credential("") == ""
        assert decrypt_credential_with_source("")[1] == "empty"

    def test_tampered_ciphertext_is_rejected(self, cipher_env: Path) -> None:
        """GCM 的完整性：改一个字节就解不开，而不是解出一段乱码

        对照组是同一个信封原样解得开 —— 没有它的话，一个"永远返回空串"的实现也能通过。
        """
        envelope = encrypt_credential(SENTINEL_KEY)
        assert decrypt_credential(envelope) == SENTINEL_KEY

        payload = envelope[len(ENVELOPE_PREFIX) :]
        raw = bytearray(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        raw[-1] ^= 0x01
        tampered = ENVELOPE_PREFIX + base64.urlsafe_b64encode(bytes(raw)).decode("ascii").rstrip("=")
        assert decrypt_credential(tampered) == ""


class TestStoredFormatIsAContractWithDataAlreadyOnDisk:
    """信封格式是**别人库里已经躺着的数据**的契约，不是本进程的内部实现

    ## 为什么单靠 round-trip 不够（复核变异实测：四条全部存活）

    上面 `TestCipherRoundTrip` 全是"本进程加密 → 本进程解密"。把
    `ENVELOPE_PREFIX` / `_KDF_SALT` / `_KDF_CONTEXT` / `_AAD` 任意一个改掉之后，
    新参数照样自洽，**76 条用例一条都不红** —— 正是典型的
    `a = f(x); b = f(x); assert a == b` 的形态：对照组和被测项一起变了，什么都没测到。

    而这四个常量每一个都是**跨版本的持久化契约**，改动的真实后果是：

    - 改 `ENVELOPE_PREFIX`：存量密文不再被认成信封 → 落进"存量明文"分支被**原样返回**，
      于是一串 `nkenc1:...` 被当成 API Key 塞进 `Authorization` 发给上游 —— 模块文档里
      逐字承诺过"**不会**把密文当成 Key 发给上游"的那件事；同时 `needs_reencrypt` 判它
      需要升级，启动时把密文当明文再封一层，套娃之后连原密钥也解不回来了。
    - 改 KDF 参数或 AAD：同一把主密钥派生出**不同**的 AES 密钥 → 每一套已经跑起来的
      部署，存量凭据在升级后的第一次启动当场全灭（表现是全部模型组的 Key 变空）。

    所以这里钉一条**离线金标准向量**：密钥材料、密文、明文三者全是写死的字面量，
    与本进程的加密函数无关。"过去写进库的那一串还解不解得出原文"因此有了判据。

    ⚠️ 这条红了**不代表把常量改回去就行**——如果那次改动是有意的（换算法/换版本），
    正确做法是新增一个 `nkenc2:` 前缀并在读侧同时支持两代，然后补一条新的金标准向量，
    而不是把这条删掉。
    """

    # 密钥材料是自己编的（§2.13），且**只**用于这条向量
    GOLDEN_KEY_MATERIAL = "golden-vector-key-material-do-not-use-in-production"
    GOLDEN_PLAINTEXT = "sk-golden-vector-SENTINEL-0f3a"
    # 由当时的实现产出、此后**不允许再变**的一条密文。
    # 它同时钉住了：`nkenc1:` 前缀、base64url 去 padding、nonce|tag|ct 的排列、
    # AES-256-GCM、AAD 的取值、以及 HKDF 的 salt / context / 摘要 / 输出长度。
    GOLDEN_ENVELOPE = (
        "nkenc1:7OHy9mH5G-MjnpPXvjg26q_dKuFmw_y9Eg6yXkpIqV5ZM96O199aMR4LE1nPpG3v-MMk2JVJlGbVtQ"
    )

    def test_a_ciphertext_written_by_an_older_version_still_decrypts(
        self,
        cipher_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", self.GOLDEN_KEY_MATERIAL)
        reset_credential_cipher_cache()

        assert decrypt_credential_with_source(self.GOLDEN_ENVELOPE) == (self.GOLDEN_PLAINTEXT, "primary")

    def test_the_golden_vector_is_not_something_this_process_just_produced(
        self,
        cipher_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """反向对照：那条字面量确实是"上一版写下的"，不是当场算出来的

        没有这条的话，一个"把金标准向量换成 `encrypt_credential(明文)`"的重构
        会让上一条重新退化成自洽 round-trip，而它看起来照样是绿的。
        随机 nonce 保证同一明文两次加密必然不同，所以这条断言是确定性的。
        """
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", self.GOLDEN_KEY_MATERIAL)
        reset_credential_cipher_cache()

        assert encrypt_credential(self.GOLDEN_PLAINTEXT) != self.GOLDEN_ENVELOPE

    def test_a_different_key_material_does_not_open_the_golden_vector(
        self,
        cipher_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """正向对照：这条向量确实绑在那把密钥材料上

        没有它的话，一个"忽略密钥、恒返回明文"的实现也能让第一条通过。
        """
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", f"{self.GOLDEN_KEY_MATERIAL}-but-different")
        reset_credential_cipher_cache()

        assert decrypt_credential_with_source(self.GOLDEN_ENVELOPE) == ("", "undecryptable")

    def test_envelope_prefix_literal_is_pinned(self) -> None:
        """前缀本身也要写成字面量

        上面那条金标准向量已经覆盖了它，但前缀是**唯一**决定"库里这个值算密文还是
        算明文"的东西，值得单独给一条能一眼看懂的红：改它 = 一次数据迁移，不是重命名。
        """
        assert ENVELOPE_PREFIX == "nkenc1:"

    def test_envelope_layout_sizes_are_pinned(self, cipher_env: Path) -> None:
        """信封长度 = nonce(12) + tag(16) + 明文字节数

        字面量 12/16 是有意重复 `_NONCE_LEN` / `_TAG_LEN` 的：拿模块常量来算等于
        "用被测代码验被测代码"，改小 nonce 时两边一起变、这条照样绿。
        """
        plain = "sk-layout-SENTINEL-0123456789"
        payload = encrypt_credential(plain)[len(ENVELOPE_PREFIX) :]
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        assert len(raw) == 12 + 16 + len(plain.encode("utf-8"))


class TestEnvelopeDetectionIsStructural:
    """"这是不是密文"是结构判定，不是"以前缀开头"

    否则一把恰好以 `nkenc1:` 开头的明文 Key 会被当成坏掉的密文丢掉。
    """

    @pytest.mark.parametrize(
        ("value", "expected_envelope"),
        [
            ("sk-plain-key", False),
            ("", False),
            (f"{ENVELOPE_PREFIX}", False),  # 前缀后什么都没有
            (f"{ENVELOPE_PREFIX}short", False),  # 解得出 base64 但装不下 nonce+tag
            (f"{ENVELOPE_PREFIX}!!!not-base64!!!", False),
        ],
    )
    def test_non_envelopes_are_not_envelopes(self, cipher_env: Path, value: str, expected_envelope: bool) -> None:
        assert is_encrypted_envelope(value) is expected_envelope

    def test_real_envelope_is_recognised(self, cipher_env: Path) -> None:
        """正向对照：真信封必须被认出来，否则上一条用例对任何实现都成立"""
        assert is_encrypted_envelope(encrypt_credential(SENTINEL_KEY)) is True

    def test_plaintext_that_looks_like_prefix_is_returned_verbatim(self, cipher_env: Path) -> None:
        weird = f"{ENVELOPE_PREFIX}not-really-a-ciphertext"
        plain, source = decrypt_credential_with_source(weird)
        assert (plain, source) == (weird, "plaintext")


class TestStoredValueIsCiphertextNotPlaintext:
    """库里那一列不是明文 —— 带正向对照的那一版"""

    async def test_api_key_column_holds_ciphertext_while_base_url_holds_plaintext(
        self,
        cipher_env: Path,
    ) -> None:
        await _create_group()
        rows = await _raw_rows()
        assert len(rows) == 1
        stored_key = rows[0]["api_key"]
        stored_base_url = rows[0]["base_url"]

        # 被测项：库里读不到哨兵 Key
        assert SENTINEL_KEY not in stored_key
        assert is_encrypted_envelope(stored_key)
        # 正向对照：同一条 SQL、同一行，普通 TextField 的哨兵**逐字可见**。
        # 这条一旦也失败，说明探针根本没查到东西，上面那条断言不构成证据
        assert stored_base_url == SENTINEL_BASE_URL

    async def test_whole_row_dump_contains_no_plaintext_key(self, cipher_env: Path) -> None:
        """模拟"一份库 dump 落在别人手里"：整行拼起来搜哨兵，0 命中"""
        await _create_group()
        rows = await _raw_rows()
        dump = " ".join(str(value) for row in rows for value in row.values())
        assert SENTINEL_KEY not in dump
        # 正向对照：这份 dump 确实包含这一行的内容
        assert SENTINEL_BASE_URL in dump

    async def test_orm_attribute_is_plaintext(self, cipher_env: Path) -> None:
        await _create_group()
        row = await DBModelGroup.get(name="sentinel_group")
        assert row.api_key == SENTINEL_KEY

    async def test_values_query_is_also_decrypted(self, cipher_env: Path) -> None:
        """`.values()` 这条读路径同样过 `to_python_value`，不会漏出密文"""
        await _create_group()
        values = await DBModelGroup.filter(name="sentinel_group").values("name", "api_key")
        assert values == [{"name": "sentinel_group", "api_key": SENTINEL_KEY}]

    async def test_update_rewrites_ciphertext(self, cipher_env: Path) -> None:
        """改一次 Key，库里仍然是密文，而且是**新** Key 的密文"""
        row = await _create_group()
        new_key = "sk-atrest-SENTINEL-rotated-2"
        row.api_key = new_key
        await row.save()

        stored = await _raw_api_key("sentinel_group")
        assert new_key not in stored
        assert SENTINEL_KEY not in stored
        assert is_encrypted_envelope(stored)
        assert (await DBModelGroup.get(name="sentinel_group")).api_key == new_key


class TestUpperLayersNeedNoChange:
    """解密在模型层完成：可见性判定、掩码、运行时取 Key 一个字都不用改

    每条都走**一次真实的库往返**（写进去再查出来），否则测的只是内存里那个对象。
    """

    async def test_to_config_group_gets_plaintext(self, cipher_env: Path) -> None:
        await _create_group()
        row = await DBModelGroup.get(name="sentinel_group")
        assert row.to_config_group().API_KEY == SENTINEL_KEY

    async def test_platform_owner_sees_plaintext_tenant_sees_mask(self, cipher_env: Path) -> None:
        await _create_group()
        row = await DBModelGroup.get(name="sentinel_group")

        owner_info = build_model_group_info(row, resolve_console_identity(platform_owner()))
        tenant_info = build_model_group_info(row, resolve_console_identity(acme_admin()))

        # 平台方这一侧是"这条通道确实会回显"的正向对照（§2.11）
        assert owner_info.api_key == SENTINEL_KEY
        assert owner_info.config.API_KEY == SENTINEL_KEY
        # 租户这一侧拿到的既不是明文、也不是密文，而是掩码
        assert tenant_info.api_key == CREDENTIAL_MASK
        assert tenant_info.config.API_KEY == CREDENTIAL_MASK
        assert ENVELOPE_PREFIX not in tenant_info.api_key

    async def test_mask_sentinel_keeps_the_real_key(self, cipher_env: Path) -> None:
        """把掩码原样提交回来 = 保持不变

        这条路径读的正是 `row.api_key`（`update_group` 里的 `group.API_KEY = row.api_key`）。
        模型层要是没解密，"保持不变"会把**密文当明文**再加密一次，套两层信封 ——
        表现是 Key 静默失效，且下一次读出来是一串 `nkenc1:` 开头的东西。
        """
        row = await _create_group()
        cfg = row.to_config_group()
        cfg.API_KEY = CREDENTIAL_MASK
        cfg.CHAT_MODEL = "gpt-4o-mini"

        await update_group(resolve_console_identity(platform_owner()), row, cfg)

        again = await DBModelGroup.get(name="sentinel_group")
        assert again.api_key == SENTINEL_KEY
        assert again.chat_model == "gpt-4o-mini"
        stored = await _raw_api_key("sentinel_group")
        assert is_encrypted_envelope(stored)
        # 没有套娃：解一层就得到明文
        assert decrypt_credential(stored) == SENTINEL_KEY

    async def test_runtime_view_carries_plaintext(self, cipher_env: Path) -> None:
        """运行时视图（`config.MODEL_GROUPS`）拿到的必须是能直接发给上游的明文"""
        await _create_group()
        view = await model_group_service.refresh_runtime_view(persist=False)
        assert view["sentinel_group"].API_KEY == SENTINEL_KEY


class TestLegacyPlaintextRows:
    """存量明文：读时兼容、写时升级（测试部署上就是这种数据）"""

    async def test_legacy_plaintext_is_still_usable(self, cipher_env: Path) -> None:
        await _insert_raw("legacy", SENTINEL_KEY)
        row = await DBModelGroup.get(name="legacy")
        assert row.api_key == SENTINEL_KEY
        assert row.to_config_group().API_KEY == SENTINEL_KEY

    async def test_upgrade_reencrypts_and_preserves_value(self, cipher_env: Path) -> None:
        await _insert_raw("legacy", SENTINEL_KEY)
        # 前置对照：升级之前库里**确实是明文**（否则下面那句断言没有信息量）
        assert await _raw_api_key("legacy") == SENTINEL_KEY

        upgraded = await upgrade_credentials_at_rest()

        assert upgraded == ["legacy"]
        stored = await _raw_api_key("legacy")
        assert SENTINEL_KEY not in stored
        assert is_encrypted_envelope(stored)
        assert (await DBModelGroup.get(name="legacy")).api_key == SENTINEL_KEY

    async def test_upgrade_is_idempotent(self, cipher_env: Path) -> None:
        await _insert_raw("legacy", SENTINEL_KEY)
        assert await upgrade_credentials_at_rest() == ["legacy"]
        assert await upgrade_credentials_at_rest() == []

    async def test_upgrade_leaves_empty_keys_alone(self, cipher_env: Path) -> None:
        """空 Key 不需要升级，也不该被封成信封"""
        await _insert_raw("no_key", "")
        assert await upgrade_credentials_at_rest() == []
        assert await _raw_api_key("no_key") == ""

    async def test_a_normal_save_also_upgrades(self, cipher_env: Path) -> None:
        """不跑升级、只是在后台改了一下这个组，凭据同样会升级成密文"""
        await _insert_raw("legacy", SENTINEL_KEY)
        row = await DBModelGroup.get(name="legacy")
        row.chat_model = "gpt-4o-mini"
        await row.save()
        assert is_encrypted_envelope(await _raw_api_key("legacy"))


class TestKeySources:
    """密钥来源、优先级与轮换"""

    async def test_unconfigured_deployment_generates_a_key_file(self, cipher_env: Path) -> None:
        """什么都不配的部署落在**安全**那一侧：自动生成密钥文件，库里仍是密文"""
        await _create_group()
        status = credential_cipher_status()
        assert status.source == "file"
        assert status.available is True
        assert (Path(OsEnv.DATA_DIR) / KEY_FILE_NAME).is_file()
        assert is_encrypted_envelope(await _raw_api_key("sentinel_group"))

    async def test_env_key_wins_and_no_file_is_created(
        self,
        cipher_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "env-sentinel-key-A")
        reset_credential_cipher_cache()

        await _create_group()

        assert credential_cipher_status().source == "env"
        # 配了环境变量就不该往数据目录里写密钥
        assert not (Path(OsEnv.DATA_DIR) / KEY_FILE_NAME).exists()
        assert is_encrypted_envelope(await _raw_api_key("sentinel_group"))

    async def test_moving_the_key_from_file_to_env_keeps_old_rows_readable(
        self,
        cipher_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """先跑起来（文件密钥）→ 再把密钥挪进环境变量：存量密文不能因此全部作废"""
        await _create_group()
        stored_before = await _raw_api_key("sentinel_group")

        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "env-sentinel-key-A")
        reset_credential_cipher_cache()

        status = credential_cipher_status()
        assert (status.source, status.fallback_count) == ("env", 1)
        # 旧密文用历史密钥解得开
        assert (await DBModelGroup.get(name="sentinel_group")).api_key == SENTINEL_KEY
        assert needs_reencrypt(stored_before) is True

        # 启动期的升级把它重写成主密钥的密文
        assert await upgrade_credentials_at_rest() == ["sentinel_group"]
        stored_after = await _raw_api_key("sentinel_group")
        assert stored_after != stored_before
        assert decrypt_credential_with_source(stored_after)[1] == "primary"

        # 之后把数据目录里那把旧密钥删掉，照样读得出来
        (Path(OsEnv.DATA_DIR) / KEY_FILE_NAME).unlink()
        reset_credential_cipher_cache()
        assert (await DBModelGroup.get(name="sentinel_group")).api_key == SENTINEL_KEY

    async def test_wrong_key_fails_closed_not_open(
        self,
        cipher_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        error_logs: List[str],
    ) -> None:
        """密钥换成对不上的一把：返回空串 + ERROR，**绝不**把密文当 Key 发出去"""
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "env-sentinel-key-A")
        reset_credential_cipher_cache()
        await _create_group()
        stored = await _raw_api_key("sentinel_group")
        # 正向对照：换密钥之前这条通道确实解得开（否则下面的"解不开"对任何实现都成立）
        assert decrypt_credential_with_source(stored) == (SENTINEL_KEY, "primary")
        assert error_logs == []

        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "env-sentinel-key-B-unrelated")
        reset_credential_cipher_cache()

        plain, source = decrypt_credential_with_source(stored)
        assert (plain, source) == ("", "undecryptable")
        assert any("解密失败" in message for message in error_logs)
        row = await DBModelGroup.get(name="sentinel_group")
        assert row.api_key == ""
        assert ENVELOPE_PREFIX not in row.to_config_group().API_KEY
        # 解不开的行**不**参与重写：重写等于把还可能救回来的值彻底抹掉
        assert needs_reencrypt(stored) is False
        assert await upgrade_credentials_at_rest() == []

    async def test_unreadable_rows_are_named_in_the_startup_log(
        self,
        cipher_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        error_logs: List[str],
    ) -> None:
        """解不开的行要在启动日志里**逐个点名**

        读取时那条 ERROR 只说"有一行解不开"，不说是哪个组 —— 十个组的部署拿着它
        没法行动。而整批升级本来就查着整张表，组名就在手上。

        正向对照：同一次调用里另一个**解得开**的组不能被点名，否则"把所有组都列一遍"
        的实现也能通过。
        """
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "env-sentinel-key-A")
        reset_credential_cipher_cache()
        await _create_group("locked_out")
        await _create_group("still_fine")
        # 只把其中一个组的密文换成"用另一把密钥加密"的，另一个保持可解
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "env-sentinel-key-B-unrelated")
        reset_credential_cipher_cache()
        conn = connections.get("default")
        await conn.execute_query(
            f"UPDATE \"model_group\" SET \"api_key\" = '{encrypt_credential(SENTINEL_KEY)}' "
            "WHERE \"name\" = 'still_fine'",
        )

        assert await upgrade_credentials_at_rest() == []

        named = [message for message in error_logs if "不会**重写" in message]
        assert len(named) == 1, f"期望恰好一条点名 ERROR，实际 {len(named)} 条"
        assert "locked_out" in named[0]
        assert "still_fine" not in named[0]
        # 那条 ERROR 必须给得出下一步动作，否则点名也没用
        assert "NEKRO_CREDENTIAL_SECRET_KEY" in named[0]

    async def test_no_unreadable_rows_means_no_such_error(
        self,
        cipher_env: Path,
        error_logs: List[str],
    ) -> None:
        """反向对照：一切正常时不许打这条 ERROR（否则上一条对任何实现都成立）"""
        await _create_group()
        assert await upgrade_credentials_at_rest() == []
        assert [message for message in error_logs if "不会**重写" in message] == []

    async def test_unavailable_cipher_is_loud(
        self,
        cipher_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        error_logs: List[str],
    ) -> None:
        """密钥既没配也建不出来时：退回明文，但**每一次**都打 ERROR，不静默

        造法是把数据目录指到一个"父路径是文件"的位置，`mkdir` 必然失败
        （POSIX 抛 NotADirectoryError、Windows 抛 FileExistsError，都是 OSError）。
        """
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(blocker / "nested"))
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "")
        reset_credential_cipher_cache()

        status = credential_cipher_status()
        assert (status.source, status.available) == ("none", False)

        assert encrypt_credential(SENTINEL_KEY) == SENTINEL_KEY
        await _create_group()
        # 明文落库：这一档的行为与改造前逐字相同
        assert await _raw_api_key("sentinel_group") == SENTINEL_KEY
        # 但它不是静默的：两条 ERROR 各自说清了"为什么没有密钥"和"这次写的是明文"，
        # 且都点名了要配哪个环境变量
        assert any("目录创建失败" in message for message in error_logs)
        assert any("以明文" in message for message in error_logs)
        assert all("NEKRO_CREDENTIAL_SECRET_KEY" in message for message in error_logs)
        assert await upgrade_credentials_at_rest() == []


class TestTheCipherIsActuallyWiredIn:
    """接线用例（零件全对、调用点没了 = 测试全绿）

    上面所有行为用例都建立在"`api_key` 这个字段真的是加密字段"之上。把字段换回
    `TextField` 之后，行为用例确实会红 —— 这一组是为**另外两个调用点**准备的：
    启动时的升级调用，以及"别的字段没有被顺手加密"。
    """

    def test_api_key_field_is_the_encrypted_one(self, cipher_env: Path) -> None:
        field = DBModelGroup._meta.fields_map["api_key"]  # noqa: SLF001
        assert isinstance(field, EncryptedTextField)

    def test_tortoise_classifies_it_as_a_converting_field(self, cipher_env: Path) -> None:
        """Tortoise 只对"重写了 `to_python_value`"的字段在读取时调用转换

        这条盯的是加密字段的**装配方式**：一旦有人把 `to_python_value` 删掉只留
        `to_db_value`（"写的时候加密就行了"），字段会被归进 `db_default_fields`，
        读出来就是密文 —— 而那是个静默故障。
        """
        complex_fields = {name for _, name, _ in DBModelGroup._meta.db_complex_fields}  # noqa: SLF001
        assert "api_key" in complex_fields

    def test_only_the_credential_column_is_encrypted(self, cipher_env: Path) -> None:
        """`base_url` / `chat_proxy` 仍是普通字段

        它们对租户同样掩码，但**不**加密：这是有意的（可读性 / 可排障），
        写在这里免得下一轮有人以为漏了。真要改是一次显式决定，不是顺手。
        """
        encrypted = {
            name
            for name, field in DBModelGroup._meta.fields_map.items()  # noqa: SLF001
            if isinstance(field, EncryptedTextField)
        }
        assert encrypted == {"api_key"}

    async def test_bootstrap_runs_the_upgrade(self, cipher_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """启动路径上真的会调升级 —— 行为化验证，不是数源码字符串

        存量明文行 + 一次 `bootstrap_model_groups()`，库里那一列必须变成密文。
        把 `bootstrap_model_groups` 里那句 `upgrade_credentials_at_rest()` 删掉即红。
        """
        await _insert_raw("legacy", SENTINEL_KEY)
        assert await _raw_api_key("legacy") == SENTINEL_KEY

        # `save_config` 与 `config.MODEL_GROUPS` 已由 `cipher_env` 接管，这里不再重复
        await model_group_service.bootstrap_model_groups()

        assert is_encrypted_envelope(await _raw_api_key("legacy"))
        assert (await DBModelGroup.get(name="legacy")).api_key == SENTINEL_KEY

    async def test_import_from_config_file_lands_encrypted(
        self,
        cipher_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """配置文件里的存量模型组导入数据库时，Key 也是加密落库的"""
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


REPO_ROOT = Path(__file__).resolve().parents[1]

# 运维要配的那个环境变量名。它出现在 `.env.example`、`docker/.env.example`、三份 compose、
# `docs/operations.md §1.5`、以及三条运行期日志文案里 —— 一处写错就是「运维配了、
# 应用没读到，日志还在劝他去配他刚配过的那一项」
ENV_VAR_NAME = "NEKRO_CREDENTIAL_SECRET_KEY"


def _load_isolated_os_env(data_dir: Path) -> ModuleType:
    """在**隔离的模块实例**上重跑一遍 `core/os_env.py` 的模块体

    为什么必须这么做：`OsEnv` 的字段是**类定义期**（也就是 import 期）从 `os.environ`
    取值的（`OsEnvTypes.Str`），进程里那一份的取值来自 pytest 启动时的环境，
    `monkeypatch.setenv` 改不动它。而本文件其余用例统一 `monkeypatch.setattr(OsEnv, ...)`
    —— 那恰好**绕过**了「代码到底读哪个环境变量名」这一段：把
    `OsEnvTypes.Str("CREDENTIAL_SECRET_KEY")` 改成任何别的键名，那些用例一条都不会红
    （复核变异 R2 因此存活）。

    模块名带包前缀是必须的：`os_env.py` 里有 `from .core_utils import ...`，
    没有 `__package__` 会 ImportError。`nekro_agent.core` 已在 `sys.modules` 里，
    重跑的只有 `os_env` 自己的模块体。
    """
    import nekro_agent.core.os_env as live_os_env

    name = "nekro_agent.core._os_env_probe_for_credential_cipher"
    spec = importlib.util.spec_from_file_location(name, live_os_env.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    # 自检：这次重跑确实读到了我们钉的数据目录，否则下面对密钥的断言可能来自别处
    assert module.OsEnv.DATA_DIR == str(data_dir)
    return module


class TestTheEnvVarNameIsTheOneWeDocument:
    """`OsEnv.CREDENTIAL_SECRET_KEY` 读的必须正是 `NEKRO_CREDENTIAL_SECRET_KEY`"""

    def test_the_documented_name_is_the_one_that_is_read(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("NEKRO_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv(ENV_VAR_NAME, "env-name-SENTINEL-9f31d0")
        module = _load_isolated_os_env(tmp_path / "data")
        assert module.OsEnv.CREDENTIAL_SECRET_KEY == "env-name-SENTINEL-9f31d0"

    def test_without_that_variable_it_falls_back_to_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """反向对照：不设这个变量时必须是空串

        没有它的话，一个「恒返回上一条那个哨兵」的实现（或者哨兵是从别的变量漏进来的）
        同样能让上一条通过。
        """
        monkeypatch.setenv("NEKRO_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.delenv(ENV_VAR_NAME, raising=False)
        module = _load_isolated_os_env(tmp_path / "data")
        assert module.OsEnv.CREDENTIAL_SECRET_KEY == ""


class TestDeploymentListsDeclareTheKey:
    """`.env*` / compose 是**三份清单**，只改一份 = 运维配了却进不了容器

    这正是 §2.12（`pyproject` force-include vs `dockerfile` COPY）那条教训的同款形态：
    `.env.example` 里把环境变量那一档写成"推荐形态"，而 compose 的 `environment:`
    是**显式白名单** —— 少那一行时运维在 `docker/.env` 里填了也没有任何东西会告诉他没生效，
    应用照旧走文件密钥那一档，还在启动日志里劝他去配他刚配过的那一项。
    """

    COMPOSE_FILES = (
        "docker/docker-compose.yml",
        "docker/docker-compose-x-napcat.yml",
        "docker/docker-compose-x-hardened.yml",
    )

    @pytest.mark.parametrize("name", [".env.example", "docker/.env.example"])
    def test_env_examples_declare_it(self, name: str) -> None:
        content = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert re.search(rf"^{ENV_VAR_NAME}=", content, re.MULTILINE), name

    @pytest.mark.parametrize("name", COMPOSE_FILES)
    def test_compose_passes_it_through(self, name: str) -> None:
        content = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert f"- {ENV_VAR_NAME}=${{{ENV_VAR_NAME}:-}}" in content, name

    @pytest.mark.parametrize("name", COMPOSE_FILES)
    def test_compose_still_passes_the_other_deployment_keys(self, name: str) -> None:
        """正向对照：这份清单里既有的项还在

        没有它的话，一个把整个 `environment:` 块删光的改动也能让上一条…… 变红，
        但一个**查错了文件**的探针会让上一条与下一条一起失败而看不出区别（§2.22）。
        """
        content = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "- NEKRO_DATA_DIR=${NEKRO_DATA_DIR}" in content, name

    def test_env_prod_must_not_declare_it(self) -> None:
        """`.env.prod` **不能**登记这一项 —— 登记了就是个静默清空的陷阱

        `.env.prod` 被 `COPY` 进镜像，而 `run_bot.py` 把该文件里 `NEKRO_` 前缀的键
        **无条件**写进 `os.environ`。于是镜像里一行 `NEKRO_CREDENTIAL_SECRET_KEY=`（空）
        会把 compose 刚传进来的真密钥覆盖成空串：应用退回文件密钥那一档，
        存量密文全部解不开，而没有任何一处会说「你配的那把被覆盖了」。
        """
        content = (REPO_ROOT / ".env.prod").read_text(encoding="utf-8")
        assert ENV_VAR_NAME not in content

    def test_run_bot_really_does_that_unconditional_override(self) -> None:
        """上一条的**理由**本身要被钉住：理由一旦不成立，那条禁令就该重估"""
        source = (REPO_ROOT / "run_bot.py").read_text(encoding="utf-8")
        assert re.search(r'if\s+key\.startswith\("NEKRO_"\):\s*\n\s*os\.environ\[key\]\s*=\s*value', source)


class TestNoRealCredentialInThisFile:
    """本文件里的样例凭据必须是自己编的（§2.13 踩过：样例口令逐字是测试部署的真口令）"""

    def test_sentinels_are_obviously_fake(self) -> None:
        source = Path(__file__).read_text(encoding="utf-8")
        assert "SENTINEL" in SENTINEL_KEY
        assert "sentinel" in SENTINEL_BASE_URL
        # 判据要看得见文件里的字面量，否则上面两句只是在断言常量自己
        assert SENTINEL_KEY in source
