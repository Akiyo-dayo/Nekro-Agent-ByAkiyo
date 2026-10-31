"""OneBot V11 多实例路由与插件兼容性的回归测试

一个实例对应一个 Bot 账号，多个实例共用同一套数据库。保护两组契约：

**路由正确性**
- 同一个群在两个实例下必须是两个独立会话
- 出站消息必须由该会话所属账号发出，账号离线时显式失败而不是换号发送

**插件兼容性**（这组同样重要：破坏它会静默影响整个插件生态）
- `ctx.channel_id` 保持干净格式，插件既有的 `split("_")` 写法必须继续可用
- `adapter.parse_chat_key()` 必须剥掉实例段返回真实 channel_id
- 无参 `get_bot()` 必须能按当前实例上下文解析
"""

import pytest

from nekro_agent.adapters.onebot_v11.core.channel import (
    build_chat_key,
    is_valid_instance_key,
    parse_channel_id,
    parse_chat_key,
)

INSTANCE_A = "ob10001"
INSTANCE_B = "ob10002"
ACCOUNT_A = "10001"
ACCOUNT_B = "10002"
GROUP_CHANNEL = "group_123456"
PRIVATE_CHANNEL = "private_654321"


class TestChatKeyEncoding:
    """chat_key 承载实例段，channel_id 保持干净"""

    @pytest.mark.parametrize("channel_id", [GROUP_CHANNEL, PRIVATE_CHANNEL])
    def test_round_trip(self, channel_id: str) -> None:
        chat_key = build_chat_key(INSTANCE_A, channel_id)
        assert chat_key == f"onebot_v11-{INSTANCE_A}-{channel_id}"
        assert parse_chat_key(chat_key) == (INSTANCE_A, channel_id)

    def test_same_group_under_two_instances_yields_distinct_chat_keys(self) -> None:
        """核心不变量：同一个群在两个实例下必须是两个不同的会话标识"""
        assert build_chat_key(INSTANCE_A, GROUP_CHANNEL) != build_chat_key(INSTANCE_B, GROUP_CHANNEL)

    def test_single_instance_deployment_keeps_legacy_shape(self) -> None:
        """单实例适配器不带实例段，与改造前完全一致"""
        assert build_chat_key("", GROUP_CHANNEL) == f"onebot_v11-{GROUP_CHANNEL}"


class TestPluginCompatibility:
    """这组是插件生态的护栏，破坏会静默影响大量第三方插件"""

    def test_channel_id_stays_clean(self) -> None:
        """插件通过 ctx.channel_id 直接读取，必须保持无实例段的干净格式

        若把实例段编码进 channel_id，插件既有的 `split("_")[0]` 会拿到
        `"ob10001-group"` 而不是 `"group"` —— 不报错，只是走错分支。
        """
        chat_key = build_chat_key(INSTANCE_A, GROUP_CHANNEL)
        channel_id = parse_channel_id(chat_key)
        assert channel_id == GROUP_CHANNEL
        assert channel_id.split("_")[0] == "group"
        assert channel_id.split("_")[1] == "123456"
        assert channel_id.partition("_")[2] == "123456"

    def test_adapter_parse_chat_key_strips_instance(self) -> None:
        """走官方解析函数的插件必须拿到真实 channel_id，而不是带实例段的中间态"""
        from nekro_agent.adapters.onebot_v11.adapter import OnebotV11Adapter

        adapter = OnebotV11Adapter()
        chat_key = build_chat_key(INSTANCE_A, GROUP_CHANNEL)
        assert adapter.parse_chat_key(chat_key) == ("onebot_v11", GROUP_CHANNEL)
        assert adapter.parse_channel_id(chat_key) == GROUP_CHANNEL

    def test_adapter_parse_chat_key_handles_legacy(self) -> None:
        from nekro_agent.adapters.onebot_v11.adapter import OnebotV11Adapter

        adapter = OnebotV11Adapter()
        assert adapter.parse_chat_key(f"onebot_v11-{GROUP_CHANNEL}") == ("onebot_v11", GROUP_CHANNEL)

    def test_adapter_key_prefix_is_preserved(self) -> None:
        """resolve_adapter_key_from_chat_key 按第一个 `-` 切分，新格式不能破坏它"""
        from nekro_agent.adapters import resolve_adapter_key_from_chat_key

        chat_key = build_chat_key(INSTANCE_A, GROUP_CHANNEL)
        assert resolve_adapter_key_from_chat_key(chat_key) == "onebot_v11"

    def test_path_sanitization_keeps_instances_distinct(self) -> None:
        """chat_key 会成为上传目录与沙盒挂载路径的一部分，不同实例不能碰撞"""
        from nekro_agent.tools.path_convertor import sanitize_chat_key_for_path

        key_a = build_chat_key(INSTANCE_A, GROUP_CHANNEL)
        key_b = build_chat_key(INSTANCE_B, GROUP_CHANNEL)
        assert sanitize_chat_key_for_path(key_a) == key_a  # 未被改写
        assert sanitize_chat_key_for_path(key_a) != sanitize_chat_key_for_path(key_b)


class TestLegacyCompatibility:
    """升级前写入的标识必须仍可解析，不能抛异常"""

    @pytest.mark.parametrize("channel_id", [GROUP_CHANNEL, PRIVATE_CHANNEL])
    def test_legacy_chat_key_yields_empty_instance(self, channel_id: str) -> None:
        assert parse_chat_key(f"onebot_v11-{channel_id}") == ("", channel_id)

    @pytest.mark.parametrize("garbage", ["garbage", "", "onebot_v11-weird-shape", "onebot_v11-"])
    def test_malformed_chat_key_does_not_raise(self, garbage: str) -> None:
        """协议端可能送来意料之外的标识，解析必须降级而不是崩掉整条入站链路"""
        instance_key, _channel_id = parse_chat_key(garbage)
        assert instance_key == ""

    @pytest.mark.parametrize(
        "hostile",
        [
            "onebot_v11-..-group_123456",
            "onebot_v11-../../etc-group_123456",
            "onebot_v11-A_B-group_123456",
            "onebot_v11-" + "x" * 40 + "-group_123456",
        ],
    )
    def test_hostile_instance_segment_is_rejected(self, hostile: str) -> None:
        """实例标识会被用作目录名，解析期必须拒绝不合法的段

        chat_key 正常由本模块构建，但它也会从数据库、插件参数、AI 生成的沙盒调用流入。
        不校验的话 `onebot_v11-..-group_1` 会解析出 `..` 并穿出包目录。
        """
        instance_key, _channel_id = parse_chat_key(hostile)
        assert instance_key == ""


class TestInstanceKeyConstraints:
    """实例标识的字符集约束保证 chat_key 可无歧义解析、且路径安全"""

    @pytest.mark.parametrize("valid", ["ob10001", "inst1", "custom_bot", "a"])
    def test_accepts_safe_slugs(self, valid: str) -> None:
        assert is_valid_instance_key(valid)

    @pytest.mark.parametrize(
        ("invalid", "why"),
        [
            ("has-dash", "包含 `-` 会让 chat_key 无法无歧义分段"),
            ("Upper", "大写会与路径清洗规则产生歧义"),
            ("has space", "空格会被路径清洗改写"),
            ("has:colon", "冒号会被路径清洗改写"),
            ("", "空标识"),
            ("x" * 33, "超长会挤占 chat_key 的 256 字符预算"),
        ],
    )
    def test_rejects_unsafe_slugs(self, invalid: str, why: str) -> None:
        assert not is_valid_instance_key(invalid)


class TestBotResolution:
    """出站连接定位：绝不允许在目标账号离线时换号发出去"""

    @staticmethod
    def _fake_bots(*account_ids: str) -> dict:
        class _FakeBot:
            def __init__(self, self_id: str) -> None:
                self.self_id = self_id

        return {aid: _FakeBot(aid) for aid in account_ids}

    def _patch(self, monkeypatch: pytest.MonkeyPatch, bots: dict) -> None:
        from nekro_agent.adapters.onebot_v11.core import bot as bot_module

        monkeypatch.setattr(bot_module, "get_bots", lambda: bots)
        monkeypatch.setattr(bot_module, "OneBotV11Bot", type(next(iter(bots.values()))))

    def test_resolves_explicit_account(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.adapters.onebot_v11.core import bot as bot_module

        self._patch(monkeypatch, self._fake_bots(ACCOUNT_A, ACCOUNT_B))
        assert bot_module.get_bot(ACCOUNT_B).self_id == ACCOUNT_B

    def test_offline_account_raises_instead_of_falling_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """账号 B 离线时，绝不能回落到在线的账号 A"""
        from nekro_agent.adapters.onebot_v11.core import bot as bot_module

        self._patch(monkeypatch, self._fake_bots(ACCOUNT_A))
        with pytest.raises(bot_module.BotInstanceOfflineError):
            bot_module.get_bot(ACCOUNT_B)

    def test_bare_call_uses_current_instance_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """第三方插件里既有的无参 get_bot() 必须解析到当前实例，而不是第一个连接

        这是插件兼容的关键：改造前无参调用返回 get_bots() 的第一个实例，
        多账号接入时必然串号。
        """
        from nekro_agent.adapters.onebot_v11.core import bot as bot_module
        from nekro_agent.adapters.onebot_v11.core import instance as instance_module

        self._patch(monkeypatch, self._fake_bots(ACCOUNT_A, ACCOUNT_B))
        instance_module.set_current_instance(INSTANCE_B, ACCOUNT_B)
        try:
            assert bot_module.get_bot().self_id == ACCOUNT_B
        finally:
            instance_module._current_instance.set(None)  # noqa: SLF001

    def test_bare_call_without_context_refuses_when_ambiguous(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """无上下文且多账号在线时必须拒绝，不能猜"""
        from nekro_agent.adapters.onebot_v11.core import bot as bot_module

        self._patch(monkeypatch, self._fake_bots(ACCOUNT_A, ACCOUNT_B))
        with pytest.raises(bot_module.BotInstanceOfflineError):
            bot_module.get_bot()

    def test_bare_call_without_context_resolves_when_unambiguous(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """单账号部署下无上下文也能工作，保证旧插件零改动可用"""
        from nekro_agent.adapters.onebot_v11.core import bot as bot_module

        self._patch(monkeypatch, self._fake_bots(ACCOUNT_A))
        assert bot_module.get_bot().self_id == ACCOUNT_A


class TestMemoryInstanceScope:
    """记忆检索的实例可见范围与共享组"""

    def test_single_instance_deployment_is_unfiltered(self) -> None:
        """单实例部署必须零影响：返回 None 表示不附加任何过滤条件"""
        from nekro_agent.services.memory.instance_scope import resolve_allowed_instance_keys

        assert resolve_allowed_instance_keys("") is None

    def test_isolated_instance_sees_itself_and_legacy(self) -> None:
        """未配置共享组时只能看自己；空串是升级前的存量记忆，必须一并放行"""
        from nekro_agent.services.memory.instance_scope import resolve_allowed_instance_keys

        allowed = resolve_allowed_instance_keys(INSTANCE_A)
        assert allowed is not None
        assert set(allowed) == {INSTANCE_A, ""}

    def test_sharing_group_members_see_each_other(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """配置了共享组的实例之间可以互相检索记忆"""
        from nekro_agent.core.config import config
        from nekro_agent.services.memory.instance_scope import resolve_allowed_instance_keys

        monkeypatch.setattr(config, "MEMORY_INSTANCE_SHARING_GROUPS", {"family": [INSTANCE_A, INSTANCE_B]})
        allowed = resolve_allowed_instance_keys(INSTANCE_A)
        assert allowed is not None
        assert set(allowed) == {INSTANCE_A, INSTANCE_B, ""}

    def test_instance_outside_group_stays_isolated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """共享组之外的实例不受影响，仍然只能看自己"""
        from nekro_agent.core.config import config
        from nekro_agent.services.memory.instance_scope import resolve_allowed_instance_keys

        monkeypatch.setattr(config, "MEMORY_INSTANCE_SHARING_GROUPS", {"family": [INSTANCE_A, INSTANCE_B]})
        allowed = resolve_allowed_instance_keys("ob99999")
        assert allowed is not None
        assert set(allowed) == {"ob99999", ""}
