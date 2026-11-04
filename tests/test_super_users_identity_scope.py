"""聊天命令的超级用户不能被冒充：名单匹配 ∧ 来源可信

修复前：超级用户的判据只有一句 `platform_user.user_id in SUPER_USERS`，不看消息来自哪个适配器、
哪个实例，也不看这个实例上报的发送者身份是不是平台方能担保的。于是：

- 另一个平台上恰好同号的人、或者发送者 ID 由调用方自己填的适配器（SSE 的 `from_id`），都会被当成超级用户；
- 租户自己运行协议端的远程 OneBot 实例里，事件的 `user_id` 由租户随意填写；
  部署级访问令牌为空时，匿名连入的协议端还会被自动登记成「未认领」实例，消息照常分发；
- 租户能从配置接口明文读到 `SUPER_USERS`，冒充所需的情报是现成的。

超级用户能执行 `sh`（宿主上的命令）、跨频道跨租户批量操作，所以上面任何一条都是租户 / 匿名者到宿主的提权。
修复前这份用例 12 红 / 4 绿；按审定的规则，「划给租户的托管 NapCat」从对照改成了「应当不是超级用户」。

OneBot 一条连接属于哪个实例，只和握手令牌一样可靠：可信要求这条事件所在的连接凭的是平台方独占的令牌——
实例不属于租户、已认领，应当出示的令牌非空且从没对租户可见过；连接握手时账号就属于这个实例，
出示的就是这个实例现在应当出示的那枚。独立复核补出三条：实例从租户收回、部署级令牌交到过租户手里、
认领 / 并入之后的旧连接，修法见各用例组。
"""

import asyncio
import dataclasses
import datetime
import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Dict, FrozenSet, Iterator, List, MutableMapping, Optional, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.adapters.interface.schemas.platform import PlatformChannel, PlatformMessage, PlatformUser
from nekro_agent.adapters.onebot_v11.core import access_token as gate
from nekro_agent.adapters.onebot_v11.core.access_token import (
    HandshakeRecord,
    OneBotAccessTokenMiddleware,
    TokenSource,
    current_handshake,
)
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.schemas.errors import PermissionDeniedError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.command.base import CommandPermission
from nekro_agent.services.command.schemas import CommandExecutionContext
from nekro_agent.services.user import token_exposure
from nekro_agent.services.user.role import Role
from nekro_agent.services.user.super_users import OneBotSourceFacts, judge_onebot_source
from nekro_agent.services.user.token_exposure import (
    exposure_file,
    load_exposed_fingerprints,
    record_exposed_tokens,
    scan_tenant_token_exposure,
    token_fingerprint,
)

SUPER_QQ = "10086"
TENANT_ID = 7
DEPLOYMENT_TOKEN = "deployment-token"
ONEBOT = "onebot_v11"

# (instance_key, provider, status, tenant_id, 自己的令牌)
INSTANCES = [
    ("napcat_platform", "builtin_napcat", "online", None, "own-np"),
    ("remote_platform", "remote_onebot", "online", None, "own-rp"),
    ("platform_dep", "remote_onebot", "online", None, ""),
    ("napcat_tenant", "builtin_napcat", "online", TENANT_ID, "own-nt"),
    ("remote_tenant", "remote_onebot", "online", TENANT_ID, "own-rt"),
    ("anon", "onebot_v11", "unclaimed", None, ""),
    ("gone", "builtin_napcat", "deleted", None, "own-gone"),
]


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from nekro_agent.core.config import config
    from nekro_agent.core.os_env import OsEnv
    from nekro_agent.services.command import manager as manager_mod

    monkeypatch.setattr(manager_mod, "COMMAND_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(manager_mod, "COMMAND_USER_PERMISSION_FILE", str(tmp_path / "user_permissions.json"))
    monkeypatch.setattr(
        manager_mod, "COMMAND_CHANNEL_USER_PERMISSION_FILE", str(tmp_path / "channel_user_permissions.json"),
    )
    # 「曾对租户可见」的令牌记录落在 DATA_DIR 下；默认是一份已完成首次建档的空记录（启动扫描之后的常态）
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
    token_exposure._write([], seeded=True)  # noqa: SLF001
    monkeypatch.setattr(token_exposure, "_rebuild_pending", False)
    monkeypatch.setattr(token_exposure, "_rebuild_lock", None)  # 锁绑在第一次用它的事件循环上，每条用例各有一个循环
    gate._presented_by_unbound_account.clear()  # noqa: SLF001
    monkeypatch.setattr(config, "SUPER_USERS", [SUPER_QQ])
    monkeypatch.setattr(config, "ENABLE_ADVANCED_COMMAND", True)
    set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
    manager_mod.command_manager.invalidate_cache()
    yield
    manager_mod.command_manager.invalidate_cache()


def set_deployment_token(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    """改环境变量而不是替换函数：握手中间件与 `container_manager` 都在模块级导入了那个函数

    测试进程里没有注册 NoneBot 适配器，`resolve_deployment_onebot_access_token()` 读的就是这个环境变量。
    """
    if token:
        monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", token)
    else:
        monkeypatch.delenv("ONEBOT_ACCESS_TOKEN", raising=False)


_LEGIT = object()


async def legit_handshake(instance_key: str) -> Optional[HandshakeRecord]:
    """这个实例自己的协议端正常握手时，中间件会记下的那条记录；库里没有这个实例时没有记录"""
    from nekro_agent.services.console.container_manager import read_container_metadata, resolve_access_token

    row = await DBAdapterInstance.filter(adapter_key=ONEBOT, instance_key=instance_key).first() if instance_key else None
    if row is None:
        return None
    return HandshakeRecord(
        self_id="",
        source=TokenSource.INSTANCE if read_container_metadata(row).access_token else TokenSource.DEPLOYMENT,
        instance_key=instance_key,
        token_fingerprint=token_fingerprint(resolve_access_token(row)),
        presented_fingerprint=token_fingerprint(resolve_access_token(row)),
    )


@contextmanager
def connection(record: Optional[HandshakeRecord]) -> Iterator[None]:
    """让下面的代码像是跑在「凭这条握手记录连上来的连接」派发的事件里"""
    reset = gate._current_handshake.set(record)  # noqa: SLF001
    try:
        yield
    finally:
        gate._current_handshake.reset(reset)  # noqa: SLF001


async def as_connection(adapter_key: str, instance_key: str, handshake: Any) -> Optional[HandshakeRecord]:
    """`_LEGIT` = 这个实例自己的协议端；显式传 None = 没有握手记录"""
    if handshake is _LEGIT:
        return await legit_handshake(instance_key) if adapter_key == ONEBOT else None
    return handshake


async def add_instance(instance_key: str, provider: str, status: str, tenant_id: Optional[int], token: str) -> None:
    await DBAdapterInstance.create(
        adapter_key=ONEBOT,
        instance_key=instance_key,
        display_name=instance_key,
        status=status,
        provider=provider,
        tenant_id=tenant_id,
        metadata_json=json.dumps({"access_token": token}) if token else "",
    )


async def add_channel(instance_key: str) -> str:
    chat_key = f"{ONEBOT}-{instance_key}-group_1"
    await DBChatChannel.create(
        adapter_key=ONEBOT, instance_key=instance_key, channel_id="group_1", chat_key=chat_key, channel_type="group", data="{}",
    )
    return chat_key


@pytest.fixture
def isolated_config_files(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """读频道 / 实例覆盖配置时会顺手落盘：改道到临时目录，结束后注销本用例登记的配置键

    不改道的话，这些读口会在开发机的 `./data` 下留出覆盖配置文件。
    """
    from nekro_agent.core.core_utils import ConfigManager
    from nekro_agent.core.os_env import OsEnv
    from nekro_agent.services import config_service
    from nekro_agent.services.config_resolver import config_resolver

    data_dir = tmp_path / "data"
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config_service, "CHANNEL_CONFIG_DIR", data_dir / "configs" / "channels")
    ours = [config_service.instance_override_config_key(ONEBOT, row[0]) for row in INSTANCES]
    ours += [f"adapter_override_{ONEBOT}"] + [f"channel_config_{ONEBOT}-{row[0]}-group_1" for row in INSTANCES]
    for key in ours:
        ConfigManager.unregister_config(key)
    before = set(ConfigManager.get_all_config_keys())
    config_resolver.bump_version()
    yield data_dir
    for key in set(ConfigManager.get_all_config_keys()) - before:
        ConfigManager.unregister_config(key)
    config_resolver.bump_version()


@pytest.fixture
def caplog_messages(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """记下令牌记录模块打出的 WARNING / ERROR 文本（项目日志不走标准 logging，caplog 收不到）"""
    messages: List[str] = []

    class _Recorder:
        def __getattr__(self, _name: str) -> Any:
            return lambda *_a, **_k: None

        def warning(self, message: str, *_a: Any, **_k: Any) -> None:
            messages.append(message)

        def error(self, message: str, *_a: Any, **_k: Any) -> None:
            messages.append(message)

    monkeypatch.setattr(token_exposure, "logger", _Recorder())
    return messages


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await DBTenant.create(id=TENANT_ID, name="acme", display_name="acme")
        for row in INSTANCES:
            await add_instance(*row)
        yield
    finally:
        await Tortoise.close_connections()


class _Adapter:
    def __init__(self, key: str) -> None:
        self.key = key

    async def get_user_command_permission(self, *_args: Any) -> CommandPermission:
        return CommandPermission.USER


async def flags(adapter_key: str, instance_key: str = "", user_id: str = SUPER_QQ, handshake: Any = _LEGIT) -> tuple:
    from nekro_agent.adapters.interface.collector import _resolve_user_command_flags

    channel = PlatformChannel(channel_id="group_1", channel_name="", channel_type=ChatType.GROUP, instance_key=instance_key)
    user = PlatformUser(platform_name="x", user_id=user_id, user_name="x")
    message = PlatformMessage(message_id="1", sender_id=user_id, sender_name="x", content_text="/sh id")
    chat_key = f"{adapter_key}-{instance_key}-group_1" if instance_key else f"{adapter_key}-group_1"
    with connection(await as_connection(adapter_key, instance_key, handshake)):
        return await _resolve_user_command_flags(_Adapter(adapter_key), channel, user, message, chat_key=chat_key)  # type: ignore[arg-type]


async def is_super(adapter_key: str, instance_key: str = "", user_id: str = SUPER_QQ, handshake: Any = _LEGIT) -> bool:
    return (await flags(adapter_key, instance_key, user_id, handshake))[0]


class TestCrossAdapterCollision:
    """名单里的 QQ 号到了别的适配器上，不再是超级用户"""

    @pytest.mark.parametrize("adapter_key", ["telegram", "discord", "sse", "email", "minecraft", "bilibili_live"])
    async def test_should_not_be_super_on_another_adapter(self, adapter_key: str) -> None:
        assert await is_super(adapter_key) is False


class TestListFormat:
    async def test_prefixed_entry_works_on_its_own_platform_only(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.core.config import config

        monkeypatch.setattr(config, "SUPER_USERS", ["telegram:555"])

        assert await is_super("telegram", user_id="555") is True
        assert await is_super("discord", user_id="555") is False
        assert await is_super(ONEBOT, "napcat_platform", user_id="555") is False

    @pytest.mark.parametrize("adapter_key", ["sse", "email", "bilibili_live", "minecraft", "wechatpad"])
    async def test_prefix_cannot_open_an_untrusted_adapter(self, monkeypatch: pytest.MonkeyPatch, adapter_key: str) -> None:
        """发送者 ID 由调用方或中继填写的适配器：名单里写了带前缀的条目也不算"""
        from nekro_agent.core.config import config
        from nekro_agent.services.user.super_users import matches_super_users

        monkeypatch.setattr(config, "SUPER_USERS", [f"{adapter_key}:555"])

        # 名单本身是匹配上的，挡住它的是来源判定
        assert matches_super_users(adapter_key, "555") is True
        assert await is_super(adapter_key, user_id="555") is False

    async def test_prefix_ignores_case_and_spaces(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """手填的名单常带空格、首字母大写；用户 ID 只去两侧空白"""
        from nekro_agent.core.config import config

        monkeypatch.setattr(config, "SUPER_USERS", [" Telegram : 555 ", f" {SUPER_QQ} "])

        assert await is_super("telegram", user_id="555") is True
        assert await is_super(ONEBOT, "napcat_platform") is True
        assert await is_super(ONEBOT, "napcat_platform", user_id=f" {SUPER_QQ}") is True

    async def test_a_colon_that_is_not_an_adapter_prefix_stays_part_of_the_id(
        self, db, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.core.config import config

        monkeypatch.setattr(config, "SUPER_USERS", ["nosuchadapter:555"])

        assert await is_super(ONEBOT, "napcat_platform", user_id="nosuchadapter:555") is True
        assert await is_super(ONEBOT, "napcat_platform", user_id="555") is False

    async def test_empty_list_has_no_super_users(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.core.config import config

        monkeypatch.setattr(config, "SUPER_USERS", [])

        assert await is_super(ONEBOT, "napcat_platform") is False


class TestOneBotInstanceTrust:
    """OneBot 的发送者 ID 由协议端上报：只有平台方能担保的协议端上报的才算"""

    @pytest.mark.parametrize(
        "instance_key",
        ["remote_tenant", "napcat_tenant", "anon", "gone", "never_registered", ""],
        ids=["tenant-remote", "tenant-napcat", "unclaimed", "deleted", "unknown", "empty-key"],
    )
    async def test_should_not_be_super_through_an_untrusted_instance(self, db, instance_key: str) -> None:
        assert await is_super(ONEBOT, instance_key) is False

    @pytest.mark.parametrize("instance_key", ["napcat_platform", "remote_platform", "platform_dep"])
    async def test_control_platform_instances_make_super_users(self, db, instance_key: str) -> None:
        """对照：平台方的实例，背后的令牌只有平台方持有（自己的令牌，或没交给租户的部署级令牌）"""
        assert await is_super(ONEBOT, instance_key) is True

    async def test_control_other_users_are_not_super(self, db) -> None:
        assert await is_super(ONEBOT, "napcat_platform", user_id="20002") is False

    async def test_empty_deployment_token_makes_instances_relying_on_it_untrusted(
        self, db, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        set_deployment_token(monkeypatch, "")

        assert await is_super(ONEBOT, "platform_dep") is False
        assert await is_super(ONEBOT, "napcat_platform") is True

    async def test_a_tenant_instance_without_its_own_token_compromises_the_deployment_token(
        self, db, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """租户实例没有自己的令牌 = 它的协议端配的是部署级令牌 = 部署级令牌已经在租户手里"""
        await add_instance("tenant_on_deployment_token", "remote_onebot", "online", TENANT_ID, "")

        assert await is_super(ONEBOT, "platform_dep") is False
        assert await is_super(ONEBOT, "napcat_platform") is True
        assert await is_super(ONEBOT, "remote_platform") is True

    async def test_the_deployment_token_stays_compromised_after_the_tenant_instance_is_gone(
        self, db, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """租户实例后来被删了，可它当年拿到的部署级令牌还在租户手里"""
        await add_instance("tenant_on_deployment_token", "remote_onebot", "online", TENANT_ID, "")
        assert await is_super(ONEBOT, "platform_dep") is False

        await DBAdapterInstance.filter(instance_key="tenant_on_deployment_token").update(status="deleted")
        assert await is_super(ONEBOT, "platform_dep") is False

        # 换了部署级令牌就恢复：新令牌的指纹不在记录里，协议端带着新令牌重新握手
        set_deployment_token(monkeypatch, "rotated-deployment-token")
        assert await is_super(ONEBOT, "platform_dep") is True

    async def test_the_deployment_token_stays_compromised_after_the_tenant_instance_gets_its_own(self, db) -> None:
        await add_instance("tenant_on_deployment_token", "remote_onebot", "online", TENANT_ID, "")
        assert await is_super(ONEBOT, "platform_dep") is False

        await DBAdapterInstance.filter(instance_key="tenant_on_deployment_token").update(
            metadata_json=json.dumps({"access_token": "tenant-own-later"}),
        )
        assert await is_super(ONEBOT, "platform_dep") is False

    async def test_a_failing_lookup_counts_as_untrusted(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        def broken(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("数据库不可用")

        handshake = await legit_handshake("napcat_platform")
        monkeypatch.setattr(DBAdapterInstance, "filter", broken)

        assert await is_super(ONEBOT, "napcat_platform", handshake=handshake) is False


class TestInstanceRecalledFromATenant:
    """实例从租户收回，前租户手里还有它的令牌：换令牌之前一直不可信"""

    @staticmethod
    async def bind(instance_keys: List[str]) -> None:
        from nekro_agent.routers.console.tenants import bind_tenant_instances
        from nekro_agent.schemas.tenant import TenantInstanceBindRequest

        await bind_tenant_instances(
            tenant_id=TENANT_ID,
            body=TenantInstanceBindRequest(instance_keys=instance_keys),
            _current_user=platform_owner(),
        )

    async def test_recalled_instance_stays_untrusted(self, db) -> None:
        await self.bind(["remote_tenant", "remote_platform"])
        await self.bind(["remote_tenant"])

        row = await DBAdapterInstance.get(instance_key="remote_platform")
        assert row.tenant_id is None
        assert await is_super(ONEBOT, "remote_platform") is False

    async def test_changing_the_token_restores_trust(self, db) -> None:
        await self.bind(["remote_tenant", "remote_platform"])
        await self.bind(["remote_tenant"])
        old_connection = await legit_handshake("remote_platform")

        await DBAdapterInstance.filter(instance_key="remote_platform").update(
            metadata_json=json.dumps({"access_token": "own-rp-rotated"}),
        )
        assert await is_super(ONEBOT, "remote_platform") is True
        # 前租户那条凭旧令牌握手的连接，换令牌之后同样不算
        assert await is_super(ONEBOT, "remote_platform", handshake=old_connection) is False

    async def test_assigning_records_the_token_in_force(self, db) -> None:
        """划入时记的是它当时生效的令牌：没有自己的令牌就是部署级令牌"""
        await self.bind(["remote_tenant", "platform_dep"])

        exposed = load_exposed_fingerprints()
        assert exposed is not None
        assert token_fingerprint("own-rt") in exposed
        assert token_fingerprint(DEPLOYMENT_TOKEN) in exposed
        # 对照：没动过的平台方实例不受影响
        assert token_fingerprint("own-np") not in exposed

    async def test_recalling_an_instance_that_was_never_recorded_records_it(self, db) -> None:
        """升级前就在租户名下的实例没经过「划入」这一步，收回时那一刻也要记下"""
        await self.bind([])

        assert (await DBAdapterInstance.get(instance_key="remote_tenant")).tenant_id is None
        assert await is_super(ONEBOT, "remote_tenant") is False

    async def test_control_untouched_platform_instance_stays_trusted(self, db) -> None:
        await self.bind(["remote_tenant"])

        assert await is_super(ONEBOT, "remote_platform") is True


class TestLegacyCommandGuard:
    """旧版插件命令的守卫不能绕开框架算好的标志，自己再按名单比一次"""

    @pytest.mark.parametrize("framework_flag", [False, True])
    def test_trusts_only_the_framework_flag(self, framework_flag: bool) -> None:
        from nekro_agent.adapters.onebot_v11.matchers.command import _is_legacy_super_user

        context = CommandExecutionContext(
            user_id=SUPER_QQ,
            chat_key="sse-group_1",
            username="x",
            adapter_key="sse",
            is_super_user=framework_flag,
            is_advanced_user=False,
        )
        assert _is_legacy_super_user(context) is framework_flag


class TestQuotaExemption:
    """配额豁免的「管理员」与配额白名单，都与聊天命令同一套判定

    白名单原先只比 `sender_id`：别的平台恰好同号的人、SSE 自填的 `from_id`、邮件的 `From`、租户自运行协议端
    上报的号码，都能填成名单里的值免配额。现在白名单与超级用户名单同一种格式（纯号码 = QQ 号，其他平台带前缀），
    同样要求来源可信。
    """

    @staticmethod
    async def exempt(
        adapter_key: str,
        instance_key: str,
        sender_id: str = SUPER_QQ,
        whitelist: Optional[List[str]] = None,
        *,
        super_users_exempt: bool = True,
        handshake: Any = _LEGIT,
    ) -> bool:
        from nekro_agent.services.message_service import is_quota_exempt

        message = SimpleNamespace(adapter_key=adapter_key, instance_key=instance_key, sender_id=sender_id)
        quota_config = SimpleNamespace(
            AI_CHAT_QUOTA_WHITELIST_USERS=whitelist or [],
            AI_CHAT_QUOTA_SUPER_USERS_EXEMPT=super_users_exempt,
        )
        with connection(await as_connection(adapter_key, instance_key, handshake)):
            return await is_quota_exempt(message, quota_config)  # type: ignore[arg-type]

    async def test_super_user_from_a_trusted_instance_is_exempt(self, db) -> None:
        assert await self.exempt(ONEBOT, "napcat_platform") is True

    @pytest.mark.parametrize(("adapter_key", "instance_key"), [("email", ""), ("sse", ""), (ONEBOT, "remote_tenant")])
    async def test_forged_super_user_is_not_exempt(self, db, adapter_key: str, instance_key: str) -> None:
        assert await self.exempt(adapter_key, instance_key) is False

    async def test_whitelisted_qq_number_from_a_trusted_instance_is_exempt(self, db) -> None:
        assert await self.exempt(ONEBOT, "napcat_platform", sender_id="20202", whitelist=["20202"], super_users_exempt=False) is True

    @pytest.mark.parametrize(
        ("adapter_key", "instance_key", "entry"),
        [
            ("email", "", "someone"),
            ("email", "", "email:someone"),
            ("sse", "", "20202"),
            ("sse", "", "sse:20202"),
            (ONEBOT, "remote_tenant", "20202"),
            (ONEBOT, "napcat_tenant", "20202"),
            (ONEBOT, "anon", "20202"),
        ],
    )
    async def test_forged_whitelisted_sender_is_not_exempt(self, db, adapter_key: str, instance_key: str, entry: str) -> None:
        """发送者 ID 由调用方填写的适配器、租户名下的实例、待认领实例：名单写没写前缀都不算"""
        sender_id = entry.partition(":")[2] or entry
        assert await self.exempt(adapter_key, instance_key, sender_id=sender_id, whitelist=[entry], super_users_exempt=False) is False

    @pytest.mark.parametrize("adapter_key", ["telegram", "discord"])
    async def test_same_number_on_another_platform_is_not_exempt(self, adapter_key: str) -> None:
        """纯号码只当 QQ 号：别的平台恰好同号的人不免配额"""
        assert await self.exempt(adapter_key, "", sender_id="20202", whitelist=["20202"], super_users_exempt=False) is False

    async def test_prefixed_entry_works_on_its_own_platform_only(self, db) -> None:
        assert await self.exempt("telegram", "", sender_id="555", whitelist=["telegram:555"], super_users_exempt=False) is True
        assert await self.exempt("discord", "", sender_id="555", whitelist=["telegram:555"], super_users_exempt=False) is False
        assert await self.exempt(ONEBOT, "napcat_platform", sender_id="555", whitelist=["telegram:555"], super_users_exempt=False) is False

    async def test_whitelisted_number_on_a_forged_connection_is_not_exempt(self, db) -> None:
        """平台方实例的账号，被别人凭部署级令牌连上来（握手对不上这个实例）：同样不算"""
        forged = HandshakeRecord(
            self_id="",
            source=TokenSource.DEPLOYMENT,
            instance_key="napcat_platform",
            token_fingerprint=token_fingerprint(DEPLOYMENT_TOKEN),
            presented_fingerprint=token_fingerprint(DEPLOYMENT_TOKEN),
        )
        assert (
            await self.exempt(
                ONEBOT, "napcat_platform", sender_id="20202", whitelist=["20202"], super_users_exempt=False, handshake=forged,
            )
            is False
        )

    async def test_control_empty_whitelist_exempts_nobody(self, db) -> None:
        assert await self.exempt(ONEBOT, "napcat_platform", sender_id="20202", whitelist=[], super_users_exempt=False) is False

    def test_whitelist_format_problems_are_reported_at_startup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """存量白名单里给其他平台写的纯号码改判成 QQ 号之后不再生效，启动时要说出来"""
        from nekro_agent.core.config import config
        from nekro_agent.services.user import super_users as super_users_mod

        monkeypatch.setattr(config, "AI_CHAT_QUOTA_WHITELIST_USERS", ["20202", "telegrm:555", "telegram:556"])
        monkeypatch.setattr(super_users_mod, "_enabled_non_onebot_adapters", lambda: ["telegram"])

        reported = super_users_mod.collect_quota_whitelist_warnings()
        unmatchable = [item for item in reported if "不会匹配任何人" in item]
        assert len(unmatchable) == 1
        assert "配额白名单" in unmatchable[0] and "telegrm:555" in unmatchable[0]
        assert "20202" not in unmatchable[0] and "telegram:556" not in unmatchable[0]
        assert any("配额白名单" in item and "telegram:123456" in item for item in reported)

    def test_control_well_formed_whitelist_reports_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.core.config import config
        from nekro_agent.services.user import super_users as super_users_mod

        monkeypatch.setattr(config, "AI_CHAT_QUOTA_WHITELIST_USERS", ["20202", "telegram:556"])
        monkeypatch.setattr(super_users_mod, "_enabled_non_onebot_adapters", lambda: [])

        assert super_users_mod.collect_quota_whitelist_warnings() == []

    def test_untrusted_adapter_prefix_in_the_whitelist_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`sse:` / `email:` 这类前缀永远不生效（发送者 ID 由调用方填写），启动时说出来"""
        from nekro_agent.core.config import config
        from nekro_agent.services.user import super_users as super_users_mod

        monkeypatch.setattr(config, "AI_CHAT_QUOTA_WHITELIST_USERS", ["sse:20202", "email:a@b.c", "20202"])
        monkeypatch.setattr(super_users_mod, "_enabled_non_onebot_adapters", lambda: [])

        reported = [item for item in super_users_mod.collect_quota_whitelist_warnings() if "由调用方填写" in item]
        assert len(reported) == 1
        assert "sse:20202" in reported[0] and "email:a@b.c" in reported[0]
        assert reported[0].count("20202") == 1  # 纯号码那条（QQ 号）不在里面

    async def test_untrusted_adapter_prefix_in_super_users_names_what_does_not_work(
        self, db, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """超级用户名单里的 `sse:` 条目：命令权限不生效（只做名单匹配的展示口、冷处理豁免仍认它），措辞只说前者"""
        from nekro_agent.core.config import config
        from nekro_agent.services.user.super_users import collect_super_user_warnings

        monkeypatch.setattr(config, "SUPER_USERS", [SUPER_QQ, "sse:555"])

        reported = [item for item in await collect_super_user_warnings() if "由调用方填写" in item]
        assert len(reported) == 1
        assert "sse:555" in reported[0] and "超级用户权限不生效" in reported[0]

    async def test_startup_logs_the_whitelist_warnings(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.core.config import config
        from nekro_agent.services.user import super_users as super_users_mod

        logged: List[str] = []

        class _Recorder:
            def __getattr__(self, _name: str) -> Any:
                return lambda *_a, **_k: None

            def warning(self, message: str, *_a: Any, **_k: Any) -> None:
                logged.append(message)

        monkeypatch.setattr(super_users_mod, "logger", _Recorder())
        monkeypatch.setattr(config, "AI_CHAT_QUOTA_WHITELIST_USERS", ["telegrm:555"])

        await super_users_mod.log_super_user_warnings()

        assert any("配额白名单" in item and "telegrm:555" in item for item in logged)

    async def test_exemption_switch_off(self, db) -> None:
        from nekro_agent.services.message_service import is_quota_exempt

        message = SimpleNamespace(adapter_key=ONEBOT, instance_key="napcat_platform", sender_id=SUPER_QQ)
        quota_config = SimpleNamespace(AI_CHAT_QUOTA_WHITELIST_USERS=[], AI_CHAT_QUOTA_SUPER_USERS_EXEMPT=False)
        with connection(await legit_handshake("napcat_platform")):
            assert await is_quota_exempt(message, quota_config) is False  # type: ignore[arg-type]

    def test_message_entry_uses_it(self) -> None:
        """消息入口走的是这一个判定，没有自己再按名单比一次"""
        import inspect

        from nekro_agent.services import message_service

        source = inspect.getsource(message_service.MessageService.push_human_message)
        assert "await is_quota_exempt(message, config)" in source
        assert "SUPER_USERS" not in source


class TestQuotaWhitelistCommand:
    """`/quota_whitelist add`：永远不会生效的写法当场拒绝，不回「已添加」"""

    @pytest.fixture
    def whitelist(self, monkeypatch: pytest.MonkeyPatch) -> List[str]:
        import sys

        from nekro_agent.core.config import config

        # `nekro_agent.core` 包把 `config` 实例导了出来，按属性取拿到的是实例而不是模块
        config_mod = sys.modules["nekro_agent.core.config"]
        entries: List[str] = []
        monkeypatch.setattr(config, "AI_CHAT_QUOTA_WHITELIST_USERS", entries)
        monkeypatch.setattr(config_mod, "save_config", lambda *_a, **_k: None)
        return entries

    @staticmethod
    async def add(entry: str, adapter_key: str = ONEBOT) -> Any:
        from nekro_agent.services.command.built_in.quota_cmd import QuotaWhitelistCommand

        context = CommandExecutionContext(
            user_id=SUPER_QQ,
            chat_key=f"{adapter_key}-group_1",
            username="x",
            adapter_key=adapter_key,
            is_super_user=True,
            is_advanced_user=True,
        )
        return await QuotaWhitelistCommand().execute(context, f"add {entry}")

    @pytest.mark.parametrize(
        "entry",
        ["telegrm:555", "sse:20202", "email:a@b.c", "someone", "onebot_v11:abc", "０１２３", "²"],
    )
    async def test_entries_that_never_apply_are_refused(self, whitelist: List[str], entry: str) -> None:
        from nekro_agent.services.command.schemas import CommandResponseStatus

        response = await self.add(entry)

        assert response.status != CommandResponseStatus.SUCCESS
        assert "没有添加" in response.message
        assert whitelist == []

    async def test_bare_number_added_from_another_platform_says_it_is_a_qq_number(self, whitelist: List[str]) -> None:
        response = await self.add("555", adapter_key="telegram")

        assert whitelist == ["555"]
        assert "telegram:555" in response.message

    @pytest.mark.parametrize(("entry", "adapter_key"), [("20202", ONEBOT), ("telegram:555", "telegram")])
    async def test_control_well_formed_entries_are_added(self, whitelist: List[str], entry: str, adapter_key: str) -> None:
        response = await self.add(entry, adapter_key=adapter_key)

        assert whitelist == [entry]
        assert "纯号码只匹配 QQ 号" not in response.message


class TestQuotaThroughTheMessageEntry:
    """从消息入口走到底：今日配额已用完的频道里，冒用白名单号码的消息照样被配额拦下

    频道每日上限 1、今天已经回过 1 条；发送者 20202 在白名单里（纯号码 = QQ 号），超级用户豁免关掉。
    """

    WHITELISTED = "20202"

    @pytest.fixture
    def entry(self, monkeypatch: pytest.MonkeyPatch, isolated_config_files):  # noqa: ARG002 - 改道配置落盘
        from nekro_agent.adapters.utils import adapter_utils
        from nekro_agent.core.config import config
        from nekro_agent.services.message_service import message_service

        quota_config = config.model_copy(
            update={
                "AI_CHAT_DAILY_REPLY_LIMIT": 1,
                "AI_CHAT_ENABLE_HOURLY_LIMIT": False,
                "AI_CHAT_QUOTA_WHITELIST_USERS": [self.WHITELISTED],
                "AI_CHAT_QUOTA_SUPER_USERS_EXEMPT": False,
                "AI_CHAT_RANDOM_REPLY_PROBABILITY": 0.0,
            },
        )

        async def _effective_config(_self: Any) -> Any:
            return quota_config

        scheduled: List[str] = []
        notices: List[str] = []

        async def _schedule(*, message: Any, ctx: Any = None) -> None:  # noqa: ARG001
            scheduled.append(message.chat_key)

        class _Adapter:
            async def forward_message(self, request: Any) -> Any:
                from nekro_agent.adapters.interface.schemas.platform import PlatformSendResponse

                notices.append(request.segments[0].content)
                return PlatformSendResponse(success=True, message_id="1")

        async def _adapter_for_chat(_chat_key: str) -> Any:
            return _Adapter()

        monkeypatch.setattr(DBChatChannel, "get_effective_config", _effective_config)
        monkeypatch.setattr(message_service, "schedule_agent_task", _schedule)
        monkeypatch.setattr(adapter_utils, "get_adapter_for_chat", _adapter_for_chat)
        return SimpleNamespace(service=message_service, scheduled=scheduled, notices=notices)

    async def push(self, entry: Any, instance_key: str) -> None:
        from nekro_agent.schemas.chat_message import ChatMessage

        chat_key = await add_channel(instance_key)
        await DBChatMessage.create(
            message_id="r1", sender_id=-1, sender_name="bot", sender_nickname="bot", adapter_key=ONEBOT,
            instance_key=instance_key, platform_userid="-1", is_tome=0, is_recalled=False, chat_key=chat_key,
            chat_type="group", content_text="今天已经回过一条", content_data="[]", raw_cq_code="", ext_data="{}",
            send_timestamp=int(datetime.datetime.now().timestamp()),
        )
        message = ChatMessage(
            message_id="m1", sender_id=self.WHITELISTED, sender_name="x", sender_nickname="x", adapter_key=ONEBOT,
            instance_key=instance_key, platform_userid=self.WHITELISTED, is_tome=1, is_recalled=False,
            chat_key=chat_key, chat_type=ChatType.GROUP, content_text="在吗", content_data=[], raw_cq_code="",
            ext_data={}, send_timestamp=0,
        )
        with connection(await legit_handshake(instance_key)):
            await entry.service.push_human_message(message=message)

    @pytest.mark.parametrize("instance_key", ["remote_tenant", "napcat_tenant", "anon"])
    async def test_whitelisted_number_on_a_tenant_or_unclaimed_instance_is_still_limited(
        self, db, entry, instance_key: str,
    ) -> None:
        """租户名下的实例、待认领实例：协议端是别人在跑，上报的号码不能用来免配额，即使连接本身是这个实例自己的"""
        await self.push(entry, instance_key)

        assert entry.scheduled == []
        assert len(entry.notices) == 1 and "配额已用完" in entry.notices[0]

    async def test_control_whitelisted_number_on_a_platform_instance_is_exempt(self, db, entry) -> None:
        await self.push(entry, "napcat_platform")

        assert entry.scheduled == [f"{ONEBOT}-napcat_platform-group_1"]
        assert entry.notices == []


class TestRegistrationSkip:
    """注册新用户时只有真正的超级用户跳过平台权限检测；冒充的照常检测，不会被当成超级用户放过"""

    @staticmethod
    async def queried(instance_key: str) -> bool:
        from nekro_agent.adapters.interface.collector import _persist_registered_user_command_permission

        calls: List[str] = []

        class Recording(_Adapter):
            async def get_user_command_permission(self, *_args: Any) -> CommandPermission:
                calls.append("queried")
                return CommandPermission.USER

        channel = PlatformChannel(channel_id="group_1", channel_name="", channel_type=ChatType.GROUP, instance_key=instance_key)
        user = PlatformUser(platform_name="x", user_id=SUPER_QQ, user_name="x")
        message = PlatformMessage(message_id="1", sender_id=SUPER_QQ, sender_name="x", content_text="hi")
        with connection(await legit_handshake(instance_key)):
            await _persist_registered_user_command_permission(Recording(ONEBOT), channel, user, message)  # type: ignore[arg-type]
        return calls == ["queried"]

    async def test_super_user_on_a_trusted_instance_is_skipped(self, db) -> None:
        assert await self.queried("napcat_platform") is False

    async def test_forged_super_user_is_checked_like_anyone_else(self, db) -> None:
        assert await self.queried("remote_tenant") is True


def _user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    user = DBUser(
        username=f"user-{tenant_role.value}-{perm_level}",
        password="",
        adapter_key="",
        platform_userid="",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )
    user.id = 100 + int(perm_level)
    return user


def tenant_admin() -> DBUser:
    return _user(tenant_id=TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def platform_admin() -> DBUser:
    """平台方一侧、但不是超级管理员的账号"""
    return _user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Admin)


def platform_owner() -> DBUser:
    return _user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


class TestReadingTheList:
    """租户读不到超级用户名单（与运维 Bot 名单同一理由）；平台方非 Super 的管理员读得到"""

    async def test_tenant_get_is_not_found(self, db) -> None:
        """与运维 Bot 名单同一口径：以「配置项不存在」掩盖存在性"""
        from nekro_agent.routers import config as config_router
        from nekro_agent.schemas.errors import ConfigNotFoundError

        with pytest.raises(ConfigNotFoundError):
            await config_router.get_config_item(config_key="system", item_key="SUPER_USERS", _current_user=tenant_admin())

    async def test_tenant_list_omits_it(self, db) -> None:
        from nekro_agent.routers import config as config_router

        items = await config_router.get_config_list(config_key="system", _current_user=tenant_admin())
        assert all(item.key != "SUPER_USERS" for item in items)
        # 对照：别的系统项照常可见，说明不是整张表都没返回
        assert any(item.key == "COMMAND_ENABLED" for item in items)

    async def test_platform_admin_can_read_it(self, db) -> None:
        from nekro_agent.routers import config as config_router

        item = await config_router.get_config_item(config_key="system", item_key="SUPER_USERS", _current_user=platform_admin())
        assert item.value == [SUPER_QQ]

    @pytest.mark.usefixtures("isolated_config_files")
    async def test_tenant_effective_trace_omits_platform_only_items(self, db) -> None:
        """频道有效配置溯源逐字段遍历整个核心配置，同样不能把名单与运维 Bot 配置带给租户"""
        from nekro_agent.routers import config as config_router

        chat_key = await add_channel("remote_tenant")
        trace = await config_router.get_effective_config_trace(chat_key=chat_key, _current_user=tenant_admin())

        keys = {item.key for item in trace.items}
        assert "SUPER_USERS" not in keys
        assert not any(key.startswith("OPS_BOT_") for key in keys)
        assert "COMMAND_ENABLED" in keys

    @pytest.mark.usefixtures("isolated_config_files")
    async def test_control_platform_effective_trace_keeps_them(self, db) -> None:
        from nekro_agent.routers import config as config_router

        chat_key = await add_channel("napcat_platform")
        trace = await config_router.get_effective_config_trace(chat_key=chat_key, _current_user=platform_admin())

        keys = {item.key for item in trace.items}
        assert "SUPER_USERS" in keys
        assert any(key.startswith("OPS_BOT_") for key in keys)


class TestWritingTheList:
    """写名单要 `Role.Super`：写进名单就能在聊天里 `sh`"""

    @pytest.fixture
    def writes(self, monkeypatch: pytest.MonkeyPatch) -> List[tuple]:
        """拦住真正的写盘：只记下写了什么，不碰开发机上的配置文件"""
        from nekro_agent.routers import config as config_router

        calls: List[tuple] = []

        def record_set(config_key: str, item_key: str, value: str) -> tuple:
            calls.append(("set", config_key, item_key, value))
            return True, ""

        def record_batch(config_key: str, configs: dict) -> tuple:
            calls.append(("batch", config_key, dict(configs)))
            return True, ""

        def record_save(config_key: str) -> tuple:
            calls.append(("save", config_key))
            return True, ""

        monkeypatch.setattr(config_router.UnifiedConfigService, "set_config_value", record_set)
        monkeypatch.setattr(config_router.UnifiedConfigService, "batch_update_config", record_batch)
        monkeypatch.setattr(config_router.UnifiedConfigService, "save_config", record_save)
        return calls

    async def test_platform_admin_cannot_change_it(self, db, writes: List[tuple]) -> None:
        from nekro_agent.routers import config as config_router

        with pytest.raises(PermissionDeniedError):
            await config_router.set_config_value(
                config_key="system", item_key="SUPER_USERS", value=json.dumps(["20002"]), _current_user=platform_admin(),
            )
        with pytest.raises(PermissionDeniedError):
            await config_router.batch_update_config(
                config_key="system",
                body=config_router.BatchUpdateConfig(configs={"SUPER_USERS": json.dumps([SUPER_QQ, "20002"])}),
                _current_user=platform_admin(),
            )
        assert writes == []

    async def test_platform_admin_can_save_the_page_with_the_list_unchanged(self, db, writes: List[tuple]) -> None:
        """配置页整页提交时名单原样带回来，不算修改；否则非 Super 的管理员连别的系统设置都存不了"""
        from nekro_agent.routers import config as config_router

        await config_router.batch_update_config(
            config_key="system",
            body=config_router.BatchUpdateConfig(configs={"SUPER_USERS": json.dumps([SUPER_QQ]), "COMMAND_ENABLED": "true"}),
            _current_user=platform_admin(),
        )
        assert writes[0][0] == "batch"

    async def test_super_admin_can_change_it(self, db, writes: List[tuple]) -> None:
        from nekro_agent.routers import config as config_router

        await config_router.set_config_value(
            config_key="system", item_key="SUPER_USERS", value=json.dumps(["20002"]), _current_user=platform_owner(),
        )
        assert writes == [("set", "system", "SUPER_USERS", json.dumps(["20002"]))]

    async def test_tenant_cannot_write_it(self, db, writes: List[tuple]) -> None:
        from nekro_agent.routers import config as config_router

        with pytest.raises(PermissionDeniedError):
            await config_router.set_config_value(
                config_key="system", item_key="SUPER_USERS", value=json.dumps(["20002"]), _current_user=tenant_admin(),
            )
        assert writes == []

    async def test_tenant_write_hits_the_platform_boundary_itself(self, db) -> None:
        """上一条先被「共享作用域对租户 403」挡住；这里直接走租户写入的逐项判据，确认名单本身在平台方边界上"""
        from nekro_agent.routers import config as config_router

        with pytest.raises(PermissionDeniedError) as excinfo:
            await config_router._assert_tenant_may_write(tenant_admin(), "system", {"SUPER_USERS": json.dumps(["20002"])})
        assert "平台方权限边界" in str(excinfo.value.detail)

    @pytest.fixture
    def reloads(self, monkeypatch: pytest.MonkeyPatch) -> List[str]:
        from nekro_agent.routers import config as config_router

        calls: List[str] = []

        def record_reload(config_key: str, file_path: Any = None) -> tuple:
            calls.append(config_key)
            return True, None

        monkeypatch.setattr(config_router.UnifiedConfigService, "reload_config", record_reload)
        return calls

    async def test_platform_admin_cannot_reset_the_system_config(self, db, reloads: List[str]) -> None:
        """重置会把磁盘上的名单读回内存：超级管理员刚移走、还没落盘的人会被恢复"""
        from nekro_agent.routers import config as config_router

        with pytest.raises(PermissionDeniedError):
            await config_router.reload_config_api(config_key="system", _current_user=platform_admin())
        assert reloads == []

    async def test_super_admin_can_reset_the_system_config(self, db, reloads: List[str]) -> None:
        from nekro_agent.routers import config as config_router

        await config_router.reload_config_api(config_key="system", _current_user=platform_owner())
        assert reloads == ["system"]

    async def test_control_platform_admin_can_reset_other_configs(self, db, reloads: List[str]) -> None:
        from nekro_agent.routers import config as config_router

        await config_router.reload_config_api(config_key=f"adapter_override_{ONEBOT}", _current_user=platform_admin())
        assert reloads == [f"adapter_override_{ONEBOT}"]


@pytest.mark.usefixtures("isolated_config_files")
class TestDisplay:
    def test_user_manager_matches_by_platform(self) -> None:
        from nekro_agent.routers import user_manager

        assert user_manager._resolve_effective_command_permission(ONEBOT, SUPER_QQ) == CommandPermission.SUPER_USER
        assert user_manager._resolve_effective_command_permission("telegram", SUPER_QQ) == CommandPermission.USER

    @pytest.mark.parametrize(("instance_key", "expected"), [("napcat_platform", "super_user"), ("remote_tenant", "user")])
    async def test_channel_list_follows_the_channel_source(self, db, instance_key: str, expected: str) -> None:
        from nekro_agent.routers import chat_channel as chat_module

        chat_key = await add_channel(instance_key)
        await DBChatMessage.create(
            sender_id=SUPER_QQ, sender_name="x", sender_nickname="x", is_tome=0, is_recalled=False, adapter_key=ONEBOT,
            instance_key=instance_key, message_id="m1", chat_key=chat_key, chat_type=ChatType.GROUP,
            platform_userid=SUPER_QQ, content_text="hi", content_data="[]", raw_cq_code="", ext_data="", send_timestamp=0,
        )

        response = await chat_module.get_chat_channel_users(chat_key=chat_key, _current_user=platform_owner())

        assert {item.platform_userid: item.command_permission for item in response.items} == {SUPER_QQ: expected}

    async def test_channel_list_rebuilds_a_missing_record_first(self, db) -> None:
        from nekro_agent.routers import chat_channel as chat_module

        chat_key = await add_channel("napcat_platform")
        await DBChatMessage.create(
            sender_id=SUPER_QQ, sender_name="x", sender_nickname="x", is_tome=0, is_recalled=False, adapter_key=ONEBOT,
            instance_key="napcat_platform", message_id="m1", chat_key=chat_key, chat_type=ChatType.GROUP,
            platform_userid=SUPER_QQ, content_text="hi", content_data="[]", raw_cq_code="", ext_data="", send_timestamp=0,
        )
        exposure_file().unlink()

        response = await chat_module.get_chat_channel_users(chat_key=chat_key, _current_user=platform_owner())

        assert {item.platform_userid: item.command_permission for item in response.items} == {SUPER_QQ: "super_user"}


class TestOpsBot:
    """运维 Bot 的白名单同样只比用户 ID：运维频道所在实例还得是可信来源"""

    @staticmethod
    async def check(monkeypatch: pytest.MonkeyPatch, instance_key: str, handshake: Any = _LEGIT) -> tuple:
        from nekro_agent.core.config import config
        from nekro_agent.services.command.built_in import ops_bot_cmd

        monkeypatch.setattr(config, "OPS_BOT_ENABLED", True)
        monkeypatch.setattr(config, "OPS_BOT_ADAPTER_KEY", ONEBOT)
        monkeypatch.setattr(config, "OPS_BOT_INSTANCE_KEY", instance_key)
        monkeypatch.setattr(config, "OPS_BOT_CHANNEL_ID", "group_1")
        monkeypatch.setattr(config, "OPS_BOT_OPERATORS", [SUPER_QQ])
        monkeypatch.setattr(ops_bot_cmd, "loaded_im_adapter_keys", lambda: frozenset({ONEBOT}))
        context = CommandExecutionContext(
            user_id=SUPER_QQ,
            chat_key=f"{ONEBOT}-{instance_key}-group_1",
            username="x",
            adapter_key=ONEBOT,
        )
        with connection(await as_connection(ONEBOT, instance_key, handshake)):
            return await ops_bot_cmd.OpsBotCommand().check_permission(context)

    async def test_ops_channel_on_a_tenant_run_endpoint_is_refused(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.command.built_in.ops_bot_cmd import DENY_MESSAGES
        from nekro_agent.services.ops_bot.policy import OpsAccessDecision

        assert await self.check(monkeypatch, "remote_tenant") == (False, DENY_MESSAGES[OpsAccessDecision.UNTRUSTED_SOURCE])

    async def test_ops_command_on_a_connection_that_never_handshook_is_refused(
        self, db, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services.command.built_in.ops_bot_cmd import DENY_MESSAGES
        from nekro_agent.services.ops_bot.policy import OpsAccessDecision

        result = await self.check(monkeypatch, "napcat_platform", handshake=None)
        assert result == (False, DENY_MESSAGES[OpsAccessDecision.UNTRUSTED_SOURCE])

    async def test_control_ops_channel_on_a_platform_instance_is_allowed(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        assert await self.check(monkeypatch, "napcat_platform") == (True, None)


class TestStartupWarnings:
    @staticmethod
    async def warnings() -> List[str]:
        from nekro_agent.services.user.super_users import collect_super_user_warnings

        return await collect_super_user_warnings()

    async def test_empty_deployment_token_is_reported_with_the_affected_instances(
        self, db, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        set_deployment_token(monkeypatch, "")

        joined = "\n".join(await self.warnings())
        assert "ONEBOT_ACCESS_TOKEN 为空" in joined
        assert "platform_dep" in joined

    async def test_exposed_deployment_token_is_reported_with_the_affected_instances(self, db) -> None:
        """租户实例靠部署级令牌认证：启动扫描把它记下，告警说要换的是部署级令牌"""
        await add_instance("tenant_on_deployment_token", "remote_onebot", "online", TENANT_ID, "")
        await scan_tenant_token_exposure()

        reported = [item for item in await self.warnings() if "部署级 ONEBOT_ACCESS_TOKEN 曾对租户可见" in item]
        assert len(reported) == 1
        assert "platform_dep" in reported[0]
        assert "napcat_platform" not in reported[0]

    async def test_exposed_instance_token_is_reported(self, db) -> None:
        record_exposed_tokens(["own-rp"], reason="用例")

        reported = [item for item in await self.warnings() if "实例的令牌曾对租户可见" in item]
        assert len(reported) == 1
        assert "remote_platform" in reported[0]
        assert "napcat_platform" not in reported[0]

    async def test_control_nothing_exposed_nothing_reported(self, db) -> None:
        joined = "\n".join(await self.warnings())
        assert "曾对租户可见" not in joined

    async def test_unclaimed_instances_are_reported(self, db) -> None:
        assert any("anon" in item and "待认领" in item for item in await self.warnings())

    async def test_plain_entries_with_other_adapters_enabled_are_reported(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.user import super_users as super_users_mod

        monkeypatch.setattr(super_users_mod, "_enabled_non_onebot_adapters", lambda: ["telegram"])

        assert any("telegram:123456" in item for item in await self.warnings())

    def test_web_adapter_is_not_listed_in_the_prefix_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`web` 默认启用、用户 ID 由服务端拼出：列进提示只会给每个部署加一条无意义告警"""
        import nekro_agent.adapters as adapters_mod
        from nekro_agent.services.user import super_users as super_users_mod

        monkeypatch.setattr(
            adapters_mod,
            "loaded_adapters",
            {"onebot_v11": object(), "web": object(), "telegram": object()},
        )

        assert super_users_mod._enabled_non_onebot_adapters() == ["telegram"]

    async def test_entries_that_can_never_match_are_reported(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """前缀拼错的条目会落成 OneBot 号码，而 OneBot 的号码只有数字：它永远匹配不上任何人"""
        from nekro_agent.core.config import config

        monkeypatch.setattr(config, "SUPER_USERS", [SUPER_QQ, "telegrm:555", "telegram:556"])

        reported = [item for item in await self.warnings() if "不会匹配任何人" in item]
        assert len(reported) == 1
        assert "telegrm:555" in reported[0]
        assert SUPER_QQ not in reported[0]
        assert "telegram:556" not in reported[0]

    async def test_without_a_list_only_the_empty_deployment_token_is_reported(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """名单为空时没有人会失去超级用户；部署级令牌为空那一条与名单无关（未认领实例的消息不处理），仍然报"""
        from nekro_agent.core.config import config

        monkeypatch.setattr(config, "SUPER_USERS", [])
        set_deployment_token(monkeypatch, "")

        reported = await self.warnings()
        assert len(reported) == 1
        assert "ONEBOT_ACCESS_TOKEN 为空" in reported[0]
        assert "超级用户" not in reported[0]

    def test_startup_scans_then_logs_them_after_registering_commands(self) -> None:
        import inspect

        import nekro_agent

        source = inspect.getsource(nekro_agent)
        assert source.index("register_built_in_commands()") < source.index("await scan_tenant_token_exposure()")
        assert source.index("await scan_tenant_token_exposure()") < source.index("await log_super_user_warnings()")
        assert source.index("await log_super_user_warnings()") < source.index("await init_plugins()")


def fresh_install() -> None:
    """升级后第一次启动之前的样子：还没有记录文件"""
    exposure_file().unlink()


class TestTokenExposureRecord:
    """曾对租户可见的令牌：只记指纹、首次建档、缺失 / 损坏 / 没建完档按全部暴露"""

    async def test_only_fingerprints_are_stored(self, db) -> None:
        record_exposed_tokens(["own-rp", ""], reason="用例")

        text = exposure_file().read_text(encoding="utf-8")
        assert "own-rp" not in text
        assert json.loads(text)["fingerprints"] == [token_fingerprint("own-rp")]

    async def test_first_scan_includes_deleted_tenant_instances_and_warns(self, db, caplog_messages: List[str]) -> None:
        """首次建档：软删的租户实例当年靠的部署级令牌也交出去过；升级前的收回记录无从追溯，提示换令牌"""
        fresh_install()
        await add_instance("tenant_gone", "remote_onebot", "deleted", TENANT_ID, "")

        await scan_tenant_token_exposure()

        exposed = load_exposed_fingerprints()
        assert exposed is not None
        assert token_fingerprint(DEPLOYMENT_TOKEN) in exposed
        assert token_fingerprint("own-rt") in exposed
        assert token_fingerprint("own-nt") in exposed
        assert any("历史无从追溯" in message for message in caplog_messages)

    async def test_later_scans_skip_deleted_tenant_instances(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """否则换过之后的新部署级令牌也会因为一行陈年的软删记录被判成暴露过，换令牌就永远恢复不了"""
        await scan_tenant_token_exposure()
        await add_instance("tenant_gone", "remote_onebot", "deleted", TENANT_ID, "")
        set_deployment_token(monkeypatch, "rotated-deployment-token")

        await scan_tenant_token_exposure()

        exposed = load_exposed_fingerprints()
        assert exposed is not None
        assert token_fingerprint("rotated-deployment-token") not in exposed
        assert await is_super(ONEBOT, "platform_dep") is True

    async def test_first_scan_without_tenants_does_not_warn(self, db, caplog_messages: List[str]) -> None:
        fresh_install()
        await DBAdapterInstance.filter(tenant_id=TENANT_ID).delete()
        await DBTenant.filter(id=TENANT_ID).delete()

        await scan_tenant_token_exposure()

        assert not any("历史无从追溯" in message for message in caplog_messages)
        assert await is_super(ONEBOT, "napcat_platform") is True

    async def test_a_failed_first_scan_is_retried_on_the_next_start(
        self, db, monkeypatch: pytest.MonkeyPatch, caplog_messages: List[str],
    ) -> None:
        """有一行读不出来：建档不算完成，下次启动仍按首次建档扫（含软删行），在此之前一律不可信"""
        from nekro_agent.services.console import container_manager

        fresh_install()
        await add_instance("tenant_gone", "remote_onebot", "deleted", TENANT_ID, "")
        real = container_manager.resolve_access_token

        def broken_for_one_row(row: DBAdapterInstance) -> str:
            if row.instance_key == "napcat_tenant":
                raise ValueError("元数据坏了")
            return real(row)

        monkeypatch.setattr(container_manager, "resolve_access_token", broken_for_one_row)
        await scan_tenant_token_exposure()

        assert load_exposed_fingerprints() is None
        assert await is_super(ONEBOT, "napcat_platform") is False
        assert not any("历史无从追溯" in message for message in caplog_messages)

        monkeypatch.setattr(container_manager, "resolve_access_token", real)
        await scan_tenant_token_exposure()

        exposed = load_exposed_fingerprints()
        assert exposed is not None
        assert token_fingerprint(DEPLOYMENT_TOKEN) in exposed
        assert any("历史无从追溯" in message for message in caplog_messages)

    async def test_a_runtime_write_before_the_first_scan_does_not_count_as_seeded(self, db) -> None:
        fresh_install()
        record_exposed_tokens(["own-rp"], reason="用例")

        assert load_exposed_fingerprints() is None

        await add_instance("tenant_gone", "remote_onebot", "deleted", TENANT_ID, "")
        await scan_tenant_token_exposure()
        exposed = load_exposed_fingerprints()
        assert exposed is not None
        assert {token_fingerprint("own-rp"), token_fingerprint(DEPLOYMENT_TOKEN)} <= exposed

    async def test_missing_record_is_rebuilt_before_judging(self, db, caplog_messages: List[str]) -> None:
        """记录文件丢了（数据目录迁移、被删）：判定前马上全量重建，并提示之前的历史补不回来"""
        fresh_install()

        assert await is_super(ONEBOT, "napcat_platform") is True
        exposed = load_exposed_fingerprints()
        assert exposed is not None
        assert {token_fingerprint("own-nt"), token_fingerprint("own-rt")} <= exposed
        assert any("历史无从追溯" in message for message in caplog_messages)

    async def test_unreadable_record_is_moved_aside_and_rebuilt(self, db) -> None:
        exposure_file().write_text("{broken", encoding="utf-8")

        assert await is_super(ONEBOT, "napcat_platform") is True
        assert [path.name for path in exposure_file().parent.glob(f"{exposure_file().name}.broken-*")] != []
        assert load_exposed_fingerprints() is not None

    async def test_everything_is_untrusted_until_a_rebuild_succeeds(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        def disk_full(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("磁盘满了")

        real_write = token_exposure._write  # noqa: SLF001
        fresh_install()
        monkeypatch.setattr(token_exposure, "_write", disk_full)
        assert await is_super(ONEBOT, "napcat_platform") is False

        monkeypatch.setattr(token_exposure, "_write", real_write)
        assert await is_super(ONEBOT, "napcat_platform") is True

    async def test_an_automatic_write_failure_keeps_the_fingerprint_until_the_next_write(
        self, db, monkeypatch: pytest.MonkeyPatch, caplog_messages: List[str],
    ) -> None:
        """改绑这类自动路径：写失败时操作照常完成、置为待重建，这一笔留在内存里，重建成功时一起落盘"""
        from nekro_agent.schemas.console import InstanceUpdateRequest
        from nekro_agent.services.console import instance_manager

        real_write = token_exposure._write  # noqa: SLF001

        def disk_full(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("磁盘满了")

        await DBAdapterInstance.filter(instance_key="anon").update(provider_account_id="30003", status="deleted")
        target = await DBAdapterInstance.get(instance_key="remote_tenant")
        monkeypatch.setattr(token_exposure, "_write", disk_full)

        await instance_manager.update_instance(target, InstanceUpdateRequest(provider_account_id="30003"))

        assert (await DBAdapterInstance.get(instance_key="remote_tenant")).provider_account_id == "30003"
        assert any("待重建" in message for message in caplog_messages)
        assert await is_super(ONEBOT, "napcat_platform") is False

        monkeypatch.setattr(token_exposure, "_write", real_write)
        assert await is_super(ONEBOT, "napcat_platform") is True
        # 重建本身找不到这一笔（待认领行不属于租户），是内存里留着的那一笔被一起落了盘
        exposed = load_exposed_fingerprints()
        assert exposed is not None
        assert token_fingerprint(DEPLOYMENT_TOKEN) in exposed
        assert await is_super(ONEBOT, "platform_dep") is False

    async def test_the_next_successful_write_carries_the_unwritten_fingerprint(
        self, db, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        real_write = token_exposure._write  # noqa: SLF001

        def disk_full(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("磁盘满了")

        monkeypatch.setattr(token_exposure, "_write", disk_full)
        record_exposed_tokens(["lost-once"], reason="用例")
        monkeypatch.setattr(token_exposure, "_write", real_write)

        record_exposed_tokens(["own-rp"], reason="用例")

        stored = json.loads(exposure_file().read_text(encoding="utf-8"))["fingerprints"]
        assert token_fingerprint("lost-once") in stored


class TestOwnershipChangesRefuseWhenTheRecordCannotBeWritten:
    """管理员发起的归属变更先记后改：记录写不进去就拒绝，库里什么都不改"""

    @pytest.fixture
    def disk_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("磁盘满了")

        monkeypatch.setattr(token_exposure, "_write", refuse)

    @pytest.mark.usefixtures("disk_full")
    async def test_recalling_is_refused(self, db) -> None:
        from nekro_agent.schemas.errors import OperationFailedError

        with pytest.raises(OperationFailedError) as excinfo:
            await TestInstanceRecalledFromATenant.bind([])

        assert "数据目录" in str(excinfo.value.detail)
        assert (await DBAdapterInstance.get(instance_key="remote_tenant")).tenant_id == TENANT_ID
        assert await is_super(ONEBOT, "remote_tenant") is False
        # 拒绝不等于记录坏了：别的实例照常判定
        assert await is_super(ONEBOT, "napcat_platform") is True

    @pytest.mark.usefixtures("disk_full")
    async def test_assigning_is_refused(self, db) -> None:
        from nekro_agent.schemas.errors import OperationFailedError

        with pytest.raises(OperationFailedError):
            await TestInstanceRecalledFromATenant.bind(["remote_tenant", "napcat_tenant", "remote_platform"])

        assert (await DBAdapterInstance.get(instance_key="remote_platform")).tenant_id is None

    @pytest.mark.usefixtures("disk_full")
    async def test_adopting_into_a_tenant_instance_is_refused(self, db) -> None:
        from nekro_agent.schemas.errors import OperationFailedError
        from nekro_agent.services.console import instance_manager

        await bind_account("anon", "30001")
        source = await DBAdapterInstance.get(instance_key="anon")
        target = await DBAdapterInstance.get(instance_key="remote_tenant")

        with pytest.raises(OperationFailedError):
            await instance_manager.adopt_unclaimed_instance(source, target)

        source = await DBAdapterInstance.get(instance_key="anon")
        assert (source.provider_account_id, source.status) == ("30001", "unclaimed")
        assert (await DBAdapterInstance.get(instance_key="remote_tenant")).provider_account_id == ""

    @pytest.mark.usefixtures("disk_full")
    async def test_reviving_a_deleted_tenant_instance_is_refused(self, db) -> None:
        from nekro_agent.schemas.errors import OperationFailedError
        from nekro_agent.services.console import instance_manager

        await add_instance("tenant_gone", "remote_onebot", "deleted", TENANT_ID, "")
        row = await DBAdapterInstance.get(instance_key="tenant_gone")

        with pytest.raises(OperationFailedError):
            await instance_manager.set_instance_enabled(row, enabled=True)

        assert (await DBAdapterInstance.get(instance_key="tenant_gone")).status == "deleted"


class TestAwaitingAccountHandshake:
    """账号还没绑定时凭待绑实例自己的令牌握手：只匹配上一个、之后账号绑到的正是它，就和凭实例令牌握手等价"""

    @staticmethod
    async def awaiting(instance_key: str, token: str, tenant_id: Optional[int] = None) -> DBAdapterInstance:
        await add_instance(instance_key, "builtin_napcat", "unbound", tenant_id, token)
        return await DBAdapterInstance.get(instance_key=instance_key)

    async def test_new_builtin_napcat_is_trusted_after_login_without_reconnecting(self, db) -> None:
        from nekro_agent.services.console import container_manager

        row = await self.awaiting("napcat_new", "own-new")
        connection = await handshake_via_gate("websocket", "50001", "own-new")
        assert connection is not None
        assert (connection.source, connection.instance_key) == (TokenSource.AWAITING_ACCOUNT, "napcat_new")

        assert await container_manager.bind_login_account(row, "50001") is True

        assert await is_super(ONEBOT, "napcat_new", handshake=connection) is True

    async def test_tenant_awaiting_token_grabbing_a_platform_account_is_untrusted(self, db) -> None:
        """租户拿自家待绑实例的令牌，抢先带着平台方即将绑定的账号连上来"""
        from nekro_agent.schemas.console import InstanceUpdateRequest
        from nekro_agent.services.console import instance_manager

        await self.awaiting("napcat_tenant_new", "own-tnew", tenant_id=TENANT_ID)
        connection = await handshake_via_gate("websocket", "50002", "own-tnew")
        assert connection is not None and connection.instance_key == "napcat_tenant_new"

        target = await DBAdapterInstance.get(instance_key="remote_platform")
        await instance_manager.update_instance(target, InstanceUpdateRequest(provider_account_id="50002"))

        assert await is_super(ONEBOT, "remote_platform", handshake=connection) is False

    async def test_a_token_matching_several_awaiting_instances_is_untrusted(self, db) -> None:
        from nekro_agent.services.console import container_manager

        row = await self.awaiting("napcat_twin_a", "twin-token")
        await self.awaiting("napcat_twin_b", "twin-token")
        connection = await handshake_via_gate("websocket", "50003", "twin-token")
        assert connection is not None and connection.instance_key == ""

        assert await container_manager.bind_login_account(row, "50003") is True

        assert await is_super(ONEBOT, "napcat_twin_a", handshake=connection) is False

    async def test_an_awaiting_instance_whose_token_was_exposed_is_untrusted(self, db) -> None:
        from nekro_agent.services.console import container_manager

        row = await self.awaiting("napcat_new", "own-new")
        record_exposed_tokens(["own-new"], reason="用例")
        connection = await handshake_via_gate("websocket", "50004", "own-new")

        assert await container_manager.bind_login_account(row, "50004") is True

        assert await is_super(ONEBOT, "napcat_new", handshake=connection) is False


class TestAccountLandingOnATenantInstance:
    """租户自己跑的协议端先凭部署级令牌连上来、被登记成待认领，再被并进 / 改绑到租户实例：部署级令牌在租户手里了"""

    async def test_adopting_into_a_tenant_instance_records_the_deployment_token(self, db) -> None:
        from nekro_agent.services.console import instance_manager

        await bind_account("anon", "30001")
        await handshake_via_gate("websocket", "30001", DEPLOYMENT_TOKEN)
        source = await DBAdapterInstance.get(instance_key="anon")
        target = await DBAdapterInstance.get(instance_key="remote_tenant")

        await instance_manager.adopt_unclaimed_instance(source, target)

        # 租户拿着部署级令牌、带平台方 platform_dep 的账号上报：不再可信
        await bind_account("platform_dep", "40001")
        forged = await handshake_via_gate("http", "40001", DEPLOYMENT_TOKEN)
        assert forged is not None and forged.instance_key == "platform_dep"
        assert await is_super(ONEBOT, "platform_dep", handshake=forged) is False

    async def test_control_adopting_into_a_platform_instance_records_nothing(self, db) -> None:
        from nekro_agent.services.console import instance_manager

        await bind_account("anon", "30001")
        source = await DBAdapterInstance.get(instance_key="anon")
        target = await DBAdapterInstance.get(instance_key="remote_platform")

        await instance_manager.adopt_unclaimed_instance(source, target)

        assert load_exposed_fingerprints() == frozenset()
        assert await is_super(ONEBOT, "platform_dep") is True

    async def test_rebinding_the_account_onto_a_tenant_instance_records_what_it_presented(self, db) -> None:
        """先把待认领行软删让出账号，再在租户实例上填这个账号：走的是改绑，不是并入"""
        from nekro_agent.schemas.console import InstanceUpdateRequest
        from nekro_agent.services.console import instance_manager

        await handshake_via_gate("websocket", "30002", DEPLOYMENT_TOKEN)
        target = await DBAdapterInstance.get(instance_key="remote_tenant")

        await instance_manager.update_instance(target, InstanceUpdateRequest(provider_account_id="30002"))

        exposed = load_exposed_fingerprints()
        assert exposed is not None
        assert token_fingerprint(DEPLOYMENT_TOKEN) in exposed

    async def test_rebinding_records_tokens_of_rows_that_held_the_account(self, db) -> None:
        """进程重启过、内存里没有握手痕迹：库里软删的待认领行还带着这个账号，它生效的就是部署级令牌"""
        from nekro_agent.schemas.console import InstanceUpdateRequest
        from nekro_agent.services.console import instance_manager

        await DBAdapterInstance.filter(instance_key="anon").update(provider_account_id="30003", status="deleted")
        target = await DBAdapterInstance.get(instance_key="remote_tenant")

        await instance_manager.update_instance(target, InstanceUpdateRequest(provider_account_id="30003"))

        exposed = load_exposed_fingerprints()
        assert exposed is not None
        assert token_fingerprint(DEPLOYMENT_TOKEN) in exposed

    async def test_login_write_back_onto_a_tenant_instance_records_what_it_presented(self, db) -> None:
        from nekro_agent.services.console import container_manager

        await handshake_via_gate("websocket", "30004", DEPLOYMENT_TOKEN)
        await DBAdapterInstance.filter(instance_key="napcat_tenant").update(status="unbound")
        target = await DBAdapterInstance.get(instance_key="napcat_tenant")

        assert await container_manager.bind_login_account(target, "30004") is True

        exposed = load_exposed_fingerprints()
        assert exposed is not None
        assert token_fingerprint(DEPLOYMENT_TOKEN) in exposed

    async def test_reviving_a_deleted_tenant_instance_records_its_token(self, db) -> None:
        from nekro_agent.services.console import instance_manager

        await add_instance("tenant_gone", "remote_onebot", "deleted", TENANT_ID, "")
        row = await DBAdapterInstance.get(instance_key="tenant_gone")

        await instance_manager.set_instance_enabled(row, enabled=True)

        exposed = load_exposed_fingerprints()
        assert exposed is not None
        assert token_fingerprint(DEPLOYMENT_TOKEN) in exposed


# ---------------------------------------------------------------------------
# OneBot 来源的全部判据：一个纯函数，逐格钉住
# ---------------------------------------------------------------------------

_TOKEN = "platform-own-token"
_FACTS = OneBotSourceFacts(instance_key="p", tenant_id=None, status="online", effective_token=_TOKEN)
_HANDSHAKE = HandshakeRecord(self_id="1", source=TokenSource.INSTANCE, instance_key="p", token_fingerprint=token_fingerprint(_TOKEN))


def _facts(**changes: Any) -> OneBotSourceFacts:
    return dataclasses.replace(_FACTS, **changes)


def _handshake(**changes: Any) -> HandshakeRecord:
    return dataclasses.replace(_HANDSHAKE, **changes)


JUDGE_TABLE: List[Tuple[str, Optional[OneBotSourceFacts], Optional[FrozenSet[str]], Optional[HandshakeRecord], bool]] = [
    ("对照：凭实例自己的令牌握手", _FACTS, frozenset(), _HANDSHAKE, True),
    ("对照：凭这个实例名下的部署级令牌握手", _FACTS, frozenset(), _handshake(source=TokenSource.DEPLOYMENT), True),
    ("实例不存在", None, frozenset(), _HANDSHAKE, False),
    ("实例属于租户", _facts(tenant_id=TENANT_ID), frozenset(), _HANDSHAKE, False),
    ("实例待认领", _facts(status="unclaimed"), frozenset(), _HANDSHAKE, False),
    ("实例待认领（大小写与空白）", _facts(status=" Unclaimed "), frozenset(), _HANDSHAKE, False),
    ("实例已软删", _facts(status="deleted"), frozenset(), _HANDSHAKE, False),
    ("应当出示的令牌为空", _facts(effective_token=""), frozenset(), _handshake(token_fingerprint=""), False),
    ("令牌曾对租户可见", _FACTS, frozenset({token_fingerprint(_TOKEN)}), _HANDSHAKE, False),
    ("可见记录读不出来", _FACTS, None, _HANDSHAKE, False),
    ("没有握手记录", _FACTS, frozenset(), None, False),
    ("对照：账号未绑定时凭唯一匹配的待绑实例令牌握手", _FACTS, frozenset(), _handshake(source=TokenSource.AWAITING_ACCOUNT), True),
    ("备选令牌匹配上多个待绑实例", _FACTS, frozenset(), _handshake(source=TokenSource.AWAITING_ACCOUNT, instance_key=""), False),
    ("备选令牌匹配的是别的待绑实例", _FACTS, frozenset(), _handshake(source=TokenSource.AWAITING_ACCOUNT, instance_key="other"), False),
    ("查库失败退回部署级令牌", _FACTS, frozenset(), _handshake(source=TokenSource.DEPLOYMENT_FALLBACK), False),
    ("握手时账号还没绑定", _FACTS, frozenset(), _handshake(instance_key=""), False),
    ("握手时账号属于别的实例", _FACTS, frozenset(), _handshake(instance_key="other"), False),
    ("握手令牌不是现在该出示的那枚", _FACTS, frozenset(), _handshake(token_fingerprint=token_fingerprint("old")), False),
]


@pytest.mark.parametrize(("facts", "exposed", "handshake", "expected"), [row[1:] for row in JUDGE_TABLE], ids=[row[0] for row in JUDGE_TABLE])
def test_judge_onebot_source(
    facts: Optional[OneBotSourceFacts],
    exposed: Optional[FrozenSet[str]],
    handshake: Optional[HandshakeRecord],
    expected: bool,
) -> None:
    assert judge_onebot_source(facts, exposed, handshake) is expected


# ---------------------------------------------------------------------------
# 握手上下文：中间件放行时记下，同一条连接上派发的事件读得到，别的连接读不到
# ---------------------------------------------------------------------------


def _scope(kind: str, self_id: str, token: Optional[str]) -> Dict[str, Any]:
    headers: List[Tuple[bytes, bytes]] = [(b"host", b"nekro_agent:8021"), (b"x-self-id", self_id.encode())]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    if kind == "websocket":
        return {"type": "websocket", "path": "/onebot/v11/ws", "headers": headers}
    return {"type": "http", "method": "POST", "path": "/onebot/v11/", "headers": headers}


async def _through_gate(scope: Dict[str, Any], downstream: Any) -> None:
    async def receive() -> Dict[str, Any]:
        return {"type": "websocket.connect"} if scope["type"] == "websocket" else {"type": "http.request"}

    async def send(_message: Dict[str, Any]) -> None:
        return None

    await OneBotAccessTokenMiddleware(downstream)(scope, receive, send)


async def _dispatched_event_sees() -> Optional[HandshakeRecord]:
    """NoneBot 的派发方式：`asyncio.create_task(bot.handle_event(event))`"""

    async def handle_event() -> Optional[HandshakeRecord]:
        await asyncio.sleep(0)
        return current_handshake()

    return await asyncio.create_task(handle_event())


async def handshake_via_gate(kind: str, self_id: str, token: Optional[str]) -> Optional[HandshakeRecord]:
    """走一遍真实的握手中间件，返回这条连接上派发的事件读到的握手记录（被拒时为 None）"""
    seen: List[Optional[HandshakeRecord]] = []

    async def downstream(_scope: MutableMapping[str, Any], _receive: Any, _send: Any) -> None:
        seen.append(await _dispatched_event_sees())

    await _through_gate(_scope(kind, self_id, token), downstream)
    return seen[0] if seen else None


async def bind_account(instance_key: str, account: str) -> None:
    await DBAdapterInstance.filter(instance_key=instance_key).update(provider_account_id=account)


class TestHandshakeContext:
    async def test_events_dispatched_with_create_task_still_see_it(self, db) -> None:
        await bind_account("napcat_platform", "20001")

        record = await handshake_via_gate("websocket", "20001", "own-np")

        assert record == HandshakeRecord(
            self_id="20001",
            source=TokenSource.INSTANCE,
            instance_key="napcat_platform",
            token_fingerprint=token_fingerprint("own-np"),
            presented_fingerprint=token_fingerprint("own-np"),
        )
        # 用完复位，外面读不到
        assert current_handshake() is None

    async def test_concurrent_connections_do_not_see_each_other(self, db) -> None:
        await bind_account("napcat_platform", "20001")
        await bind_account("remote_platform", "20002")
        seen: Dict[str, Optional[HandshakeRecord]] = {}
        both_inside = asyncio.Event()
        inside: List[str] = []

        async def downstream(scope: MutableMapping[str, Any], _receive: Any, _send: Any) -> None:
            self_id = dict(scope["headers"])[b"x-self-id"].decode()
            inside.append(self_id)
            if len(inside) == 2:
                both_inside.set()
            # 两条连接都进到下游之后才派发事件：先进来的那条读的时候，后一条的握手已经记下了
            await both_inside.wait()
            seen[self_id] = await _dispatched_event_sees()

        await asyncio.gather(
            _through_gate(_scope("websocket", "20001", "own-np"), downstream),
            _through_gate(_scope("websocket", "20002", "own-rp"), downstream),
        )

        assert seen["20001"] is not None and seen["20001"].instance_key == "napcat_platform"
        assert seen["20002"] is not None and seen["20002"].instance_key == "remote_platform"

    async def test_http_reports_are_judged_per_request(self, db) -> None:
        """同一个任务里先后两次上报（HTTP keep-alive）：后一次不继承前一次的握手"""
        await bind_account("napcat_platform", "20001")

        first = await handshake_via_gate("http", "20001", "own-np")
        second = await handshake_via_gate("http", "29999", DEPLOYMENT_TOKEN)

        assert first is not None and first.instance_key == "napcat_platform"
        assert second is not None and second.instance_key == ""
        assert second.source == TokenSource.DEPLOYMENT
        assert await is_super(ONEBOT, "napcat_platform", handshake=first) is True
        assert await is_super(ONEBOT, "napcat_platform", handshake=second) is False

    async def test_no_handshake_means_untrusted(self, db) -> None:
        """没经过中间件的路径（NA 主动连出去的正向 WS、管理接口）一律落到这一格"""
        assert await is_super(ONEBOT, "napcat_platform", handshake=None) is False


class TestClaimAndReconnect:
    """认领 / 并入之后只提示、不断开：旧连接判不可信，协议端重连一次就恢复"""

    async def test_adopted_connection_is_untrusted_until_it_reconnects(self, db) -> None:
        from nekro_agent.services.console import instance_manager

        # 部署级令牌连上来、被自动登记成待认领实例的协议端
        await bind_account("anon", "30001")
        old_connection = await handshake_via_gate("websocket", "30001", DEPLOYMENT_TOKEN)
        assert old_connection is not None and old_connection.instance_key == "anon"

        # 平台方把它并进一个有自己令牌的实例
        source = await DBAdapterInstance.get(instance_key="anon")
        target = await DBAdapterInstance.get(instance_key="napcat_platform")
        await instance_manager.adopt_unclaimed_instance(source, target)

        assert await is_super(ONEBOT, "napcat_platform", handshake=old_connection) is False

        reconnected = await handshake_via_gate("websocket", "30001", "own-np")
        assert await is_super(ONEBOT, "napcat_platform", handshake=reconnected) is True

    async def test_claimed_in_place_connection_is_untrusted_when_it_handshook_with_an_empty_token(
        self, db, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """部署级令牌为空时匿名连上来的协议端，原地认领之后照样不可信：这条连接什么都没出示过"""
        set_deployment_token(monkeypatch, "")
        await bind_account("anon", "30001")
        old_connection = await handshake_via_gate("websocket", "30001", None)
        assert old_connection is not None and old_connection.token_fingerprint == ""

        await DBAdapterInstance.filter(instance_key="anon").update(status="online")

        assert await is_super(ONEBOT, "anon", handshake=old_connection) is False
