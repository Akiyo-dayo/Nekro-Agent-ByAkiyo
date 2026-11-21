"""运维工具的准入判定：配置快照、回落与判定矩阵

## 为什么这份判定必须逐条钉死

运维工具背后是平台方的实例、租户与日志。判定的每一格错法都不是"少一个功能"，而是
"租户的群里能查到平台方的家底"或"群里随便谁能借运维人员的一句话把工具调起来"。因此
`evaluate_round` 是一个不碰数据库、不碰 IO 的纯函数，这里用表驱动把每一种拒绝理由各钉一条。

最要紧的一格是「每一条触发都要可信」：防抖会把同一窗口的多条触发并成一轮，只要其中一条
来自群里另一个人，这一轮就不能当作运维人员的指令执行。反例排在**最后一条**——只看最后
一条的实现会放行，而这里的断言会红。
"""

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from nekro_agent.services.ops_agent.policy import (
    OpsAgentPolicy,
    OpsRoundDecision,
    RoundFacts,
    evaluate_round,
    is_operator,
    private_counterpart_from_channel,
)
from nekro_agent.services.ops_agent.triggers import (
    TRIGGER_KIND_HUMAN,
    TRIGGER_KIND_OTHER,
    TRIGGER_KIND_SYSTEM_PUSH,
    TRIGGER_KIND_TIMER,
    TRIGGER_KIND_UNTRUSTED_PUSH,
    RoundTrigger,
    TriggerBatch,
)

ADAPTER = "onebot_v11"
INSTANCE = "opsinst"
OPS_CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_10001"
OTHER_GROUP_KEY = f"{ADAPTER}-{INSTANCE}-group_20002"
PRIVATE_CHAT_KEY = f"{ADAPTER}-{INSTANCE}-private_10001"
OPERATOR = "10001"
OTHER_OPERATOR = "20002"
OUTSIDER = "30003"


def build_policy(**overrides: Any) -> OpsAgentPolicy:
    defaults: Dict[str, Any] = {
        "enabled": True,
        "operators": (OPERATOR, OTHER_OPERATOR),
        "groups": frozenset({OPS_CHAT_KEY}),
        "private_enabled": True,
        "write_limit_per_round": 5,
        "write_limit_per_hour": 30,
    }
    defaults.update(overrides)
    return OpsAgentPolicy(**defaults)


def build_facts(**overrides: Any) -> RoundFacts:
    defaults: Dict[str, Any] = {
        "adapter_key": ADAPTER,
        "instance_key": INSTANCE,
        "chat_key": OPS_CHAT_KEY,
        "is_group": True,
        "is_private": False,
        "private_counterpart": "",
        "instance_platform_owned": True,
    }
    defaults.update(overrides)
    return RoundFacts(**defaults)


def human(sender_id: str = OPERATOR, *, trusted: bool = True) -> RoundTrigger:
    return RoundTrigger(
        kind=TRIGGER_KIND_HUMAN,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        sender_id=sender_id,
        operator_trusted=trusted,
    )


def private_facts(**overrides: Any) -> RoundFacts:
    defaults: Dict[str, Any] = {
        "chat_key": PRIVATE_CHAT_KEY,
        "is_group": False,
        "is_private": True,
        "private_counterpart": OPERATOR,
    }
    defaults.update(overrides)
    return build_facts(**defaults)


def other_facts(**overrides: Any) -> RoundFacts:
    defaults: Dict[str, Any] = {
        "chat_key": f"{ADAPTER}-{INSTANCE}-webui_abc",
        "is_group": False,
        "is_private": False,
    }
    defaults.update(overrides)
    return build_facts(**defaults)


# (用例标识, policy 覆盖, facts, triggers, 期望判定)
DECISION_CASES: List[Any] = [
    ("switch-off", {"enabled": False}, build_facts(), [human()], OpsRoundDecision.DISABLED),
    (
        "switch-off-wins-over-everything-else",
        {"enabled": False},
        other_facts(),
        [],
        OpsRoundDecision.DISABLED,
    ),
    (
        "tenant-instance",
        {},
        build_facts(instance_platform_owned=False),
        [human()],
        OpsRoundDecision.TENANT_INSTANCE,
    ),
    (
        "instance-checked-before-location",
        {},
        build_facts(instance_platform_owned=False, chat_key=OTHER_GROUP_KEY),
        [human()],
        OpsRoundDecision.TENANT_INSTANCE,
    ),
    ("group-not-listed", {}, build_facts(chat_key=OTHER_GROUP_KEY), [human()], OpsRoundDecision.NOT_OPS_GROUP),
    ("private-disabled", {"private_enabled": False}, private_facts(), [human()], OpsRoundDecision.PRIVATE_DISABLED),
    (
        "private-counterpart-is-outsider",
        {},
        private_facts(private_counterpart=OUTSIDER),
        [human(OUTSIDER)],
        OpsRoundDecision.NOT_OPERATOR_PRIVATE,
    ),
    (
        "private-counterpart-unresolvable",
        {},
        private_facts(private_counterpart=""),
        [human()],
        OpsRoundDecision.NOT_OPERATOR_PRIVATE,
    ),
    ("unsupported-channel", {}, other_facts(), [human()], OpsRoundDecision.UNSUPPORTED_CHANNEL),
    ("no-trigger", {}, build_facts(), [], OpsRoundDecision.NO_TRIGGER),
    ("no-trigger-in-private", {}, private_facts(), [], OpsRoundDecision.NO_TRIGGER),
    (
        "timer-in-the-batch",
        {},
        build_facts(),
        [human(), RoundTrigger(kind=TRIGGER_KIND_TIMER)],
        OpsRoundDecision.NON_HUMAN_TRIGGER,
    ),
    (
        "system-push-in-the-batch",
        {},
        build_facts(),
        [RoundTrigger(kind=TRIGGER_KIND_SYSTEM_PUSH), human()],
        OpsRoundDecision.NON_HUMAN_TRIGGER,
    ),
    (
        "untrusted-push-in-the-batch",
        {},
        build_facts(),
        [human(), RoundTrigger(kind=TRIGGER_KIND_UNTRUSTED_PUSH)],
        OpsRoundDecision.NON_HUMAN_TRIGGER,
    ),
    (
        "unclassified-scheduler-in-the-batch",
        {},
        build_facts(),
        [human(), RoundTrigger(kind=TRIGGER_KIND_OTHER)],
        OpsRoundDecision.NON_HUMAN_TRIGGER,
    ),
    (
        "outsider-is-the-last-trigger",
        {},
        build_facts(),
        [human(OPERATOR), human(OUTSIDER, trusted=False)],
        OpsRoundDecision.UNTRUSTED_TRIGGER,
    ),
    (
        "outsider-is-the-first-trigger",
        {},
        build_facts(),
        [human(OUTSIDER, trusted=False), human(OPERATOR)],
        OpsRoundDecision.UNTRUSTED_TRIGGER,
    ),
    (
        "operator-with-an-untrusted-source",
        {},
        build_facts(),
        [human(OPERATOR, trusted=False)],
        OpsRoundDecision.UNTRUSTED_TRIGGER,
    ),
    (
        "incomplete-batch",
        {},
        build_facts(),
        TriggerBatch(triggers=[human()], incomplete=True),
        OpsRoundDecision.UNTRUSTED_TRIGGER,
    ),
    (
        "trusted-without-a-sender",
        {},
        build_facts(),
        [human("", trusted=True)],
        OpsRoundDecision.UNTRUSTED_TRIGGER,
    ),
    ("all-trusted", {}, build_facts(), [human()], OpsRoundDecision.ALLOWED),
    (
        "two-operators",
        {},
        build_facts(),
        [human(OPERATOR), human(OTHER_OPERATOR)],
        OpsRoundDecision.ALLOWED,
    ),
    ("private-operator", {}, private_facts(), [human()], OpsRoundDecision.ALLOWED),
]


class TestDecisionMatrix:
    @pytest.mark.parametrize(("overrides", "facts", "triggers", "expected"), [case[1:] for case in DECISION_CASES], ids=[case[0] for case in DECISION_CASES])
    def test_each_decision(self, overrides: Dict[str, Any], facts: RoundFacts, triggers: Any, expected: OpsRoundDecision) -> None:
        assert evaluate_round(build_policy(**overrides), facts, triggers).decision is expected

    def test_every_decision_has_a_case(self) -> None:
        """判定枚举是封闭的：新增一种拒绝理由必须同时补一条用例"""
        covered = {case[4] for case in DECISION_CASES}
        assert covered == set(OpsRoundDecision), sorted({item.value for item in set(OpsRoundDecision) - covered})

    def test_allowed_carries_the_recognised_operators(self) -> None:
        result = evaluate_round(build_policy(), build_facts(), [human(OPERATOR), human(OTHER_OPERATOR)])
        assert result.allowed
        assert result.operators == frozenset({OPERATOR, OTHER_OPERATOR})

    def test_allowed_in_private_is_just_the_counterpart(self) -> None:
        """私聊只看对方是谁：触发记录里的发送者可能缺失或与频道不一致，不能拿它当操作者"""
        result = evaluate_round(build_policy(), private_facts(), [human(OPERATOR)])
        assert result.operators == frozenset({OPERATOR})

    def test_every_trigger_is_checked_not_only_the_last_one(self) -> None:
        """反例放在最后：只看最后一条的实现会放行"""
        batch = [human(OUTSIDER, trusted=False), human(OPERATOR)]
        assert evaluate_round(build_policy(), build_facts(), batch).decision is OpsRoundDecision.UNTRUSTED_TRIGGER

    def test_a_plain_sequence_is_accepted_as_well(self) -> None:
        """调用方可能已经取过批次、只要判定；两种入参必须给出同一个答案"""
        triggers = [human(), human(OTHER_OPERATOR)]
        assert evaluate_round(build_policy(), build_facts(), triggers).decision is OpsRoundDecision.ALLOWED
        assert evaluate_round(build_policy(), build_facts(), TriggerBatch(triggers=triggers)).decision is OpsRoundDecision.ALLOWED

    def test_denied_results_carry_no_operators(self) -> None:
        for overrides, facts, triggers, expected in [case[1:] for case in DECISION_CASES]:
            result = evaluate_round(build_policy(**overrides), facts, triggers)
            if expected is not OpsRoundDecision.ALLOWED:
                assert not result.operators, expected
                assert not result.allowed


class TestConfigSnapshot:
    def test_defaults_leave_the_feature_off(self) -> None:
        from nekro_agent.core.config import CoreConfig

        fields = {name: field for name, field in CoreConfig.model_fields.items() if name.startswith("OPS_AGENT_")}
        assert set(fields) == {
            "OPS_AGENT_TOOLS_ENABLED",
            "OPS_AGENT_OPERATORS",
            "OPS_AGENT_GROUPS",
            "OPS_AGENT_PRIVATE_ENABLED",
            "OPS_AGENT_WRITE_LIMIT_PER_ROUND",
            "OPS_AGENT_WRITE_LIMIT_PER_HOUR",
        }
        assert fields["OPS_AGENT_TOOLS_ENABLED"].default is False
        assert fields["OPS_AGENT_OPERATORS"].default == []
        assert fields["OPS_AGENT_GROUPS"].default == []
        assert fields["OPS_AGENT_PRIVATE_ENABLED"].default is True
        assert fields["OPS_AGENT_WRITE_LIMIT_PER_ROUND"].default == 5
        assert fields["OPS_AGENT_WRITE_LIMIT_PER_HOUR"].default == 30

    def test_no_field_is_overridable(self) -> None:
        """一旦可覆盖，租户就能在自己的频道里把工具打开，等于绕过平台方的权限体系"""
        from nekro_agent.core.config import CoreConfig

        for name, field in CoreConfig.model_fields.items():
            if not name.startswith("OPS_AGENT_"):
                continue
            extra = field.json_schema_extra or {}
            assert not extra.get("overridable"), name

    @pytest.mark.parametrize("name", [name for name in ("OPS_AGENT_TOOLS_ENABLED", "OPS_AGENT_OPERATORS", "OPS_AGENT_GROUPS", "OPS_AGENT_PRIVATE_ENABLED", "OPS_AGENT_WRITE_LIMIT_PER_ROUND", "OPS_AGENT_WRITE_LIMIT_PER_HOUR")])
    def test_every_field_is_hidden_from_tenants(self, name: str) -> None:
        from nekro_agent.core.config import CoreConfig
        from nekro_agent.routers.config import _is_platform_only_config

        assert name in CoreConfig.model_fields
        assert _is_platform_only_config("system", name), name

    def test_the_prefix_does_not_leak_into_other_config_keys(self) -> None:
        """守护范围要精确：插件里同名的一项不因此变成平台方专属"""
        from nekro_agent.routers.config import _is_platform_only_config

        assert not _is_platform_only_config("plugin_basic", "OPS_AGENT_TOOLS_ENABLED")
        assert not _is_platform_only_config("adapter_onebot_v11", "OPS_AGENT_GROUPS")

    def test_snapshot_reads_the_global_config(self) -> None:
        raw = SimpleNamespace(
            OPS_AGENT_TOOLS_ENABLED=True,
            OPS_AGENT_OPERATORS=[f" {OPERATOR} ", "", "  "],
            OPS_AGENT_GROUPS=[f" {OPS_CHAT_KEY} "],
            OPS_AGENT_PRIVATE_ENABLED=False,
            OPS_AGENT_WRITE_LIMIT_PER_ROUND=2,
            OPS_AGENT_WRITE_LIMIT_PER_HOUR=7,
            OPS_BOT_ENABLED=False,
            OPS_BOT_OPERATORS=[OUTSIDER],
            OPS_BOT_ADAPTER_KEY=ADAPTER,
            OPS_BOT_INSTANCE_KEY=INSTANCE,
            OPS_BOT_CHANNEL_ID="group_10001",
        )
        policy = OpsAgentPolicy.from_config(raw)  # type: ignore[arg-type]

        assert policy.enabled is True
        assert policy.operators == (OPERATOR,), "空白条目去掉，两侧空白也去掉"
        assert policy.groups == frozenset({OPS_CHAT_KEY})
        assert policy.private_enabled is False
        assert (policy.write_limit_per_round, policy.write_limit_per_hour) == (2, 7)

    def test_negative_limits_are_clamped_to_zero(self) -> None:
        raw = SimpleNamespace(
            OPS_AGENT_TOOLS_ENABLED=True,
            OPS_AGENT_OPERATORS=[OPERATOR],
            OPS_AGENT_GROUPS=[OPS_CHAT_KEY],
            OPS_AGENT_PRIVATE_ENABLED=True,
            OPS_AGENT_WRITE_LIMIT_PER_ROUND=-3,
            OPS_AGENT_WRITE_LIMIT_PER_HOUR=-1,
            OPS_BOT_ENABLED=False,
            OPS_BOT_OPERATORS=[],
            OPS_BOT_ADAPTER_KEY="",
            OPS_BOT_INSTANCE_KEY="",
            OPS_BOT_CHANNEL_ID="",
        )
        policy = OpsAgentPolicy.from_config(raw)  # type: ignore[arg-type]
        assert (policy.write_limit_per_round, policy.write_limit_per_hour) == (0, 0)


def _raw(**overrides: Any) -> Any:
    defaults: Dict[str, Any] = {
        "OPS_AGENT_TOOLS_ENABLED": True,
        "OPS_AGENT_OPERATORS": [],
        "OPS_AGENT_GROUPS": [],
        "OPS_AGENT_PRIVATE_ENABLED": True,
        "OPS_AGENT_WRITE_LIMIT_PER_ROUND": 5,
        "OPS_AGENT_WRITE_LIMIT_PER_HOUR": 30,
        "OPS_BOT_ENABLED": True,
        "OPS_BOT_OPERATORS": [],
        "OPS_BOT_ADAPTER_KEY": "",
        "OPS_BOT_INSTANCE_KEY": "",
        "OPS_BOT_CHANNEL_ID": "",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestFallbacks:
    def test_empty_operators_fall_back_to_the_ops_bot_list(self) -> None:
        policy = OpsAgentPolicy.from_config(_raw(OPS_BOT_OPERATORS=[OPERATOR, OTHER_OPERATOR]))  # type: ignore[arg-type]
        assert policy.operators == (OPERATOR, OTHER_OPERATOR)

    def test_a_filled_list_does_not_merge_with_the_ops_bot_list(self) -> None:
        """并集会让「把人从运维工具名单里删掉」静默失效"""
        policy = OpsAgentPolicy.from_config(_raw(OPS_AGENT_OPERATORS=[OPERATOR], OPS_BOT_OPERATORS=[OTHER_OPERATOR]))  # type: ignore[arg-type]
        assert policy.operators == (OPERATOR,)
        assert not is_operator(policy, ADAPTER, OTHER_OPERATOR)

    def test_empty_groups_fall_back_to_the_ops_bot_channel(self) -> None:
        policy = OpsAgentPolicy.from_config(
            _raw(
                OPS_BOT_ADAPTER_KEY=ADAPTER,
                OPS_BOT_INSTANCE_KEY=INSTANCE,
                OPS_BOT_CHANNEL_ID="group_10001",
                OPS_BOT_OPERATORS=[OPERATOR],
            ),
        )  # type: ignore[arg-type]
        assert policy.groups == frozenset({OPS_CHAT_KEY})

    @pytest.mark.parametrize(
        "incomplete",
        [
            {"OPS_BOT_ADAPTER_KEY": "", "OPS_BOT_INSTANCE_KEY": INSTANCE, "OPS_BOT_CHANNEL_ID": "group_10001"},
            {"OPS_BOT_ADAPTER_KEY": ADAPTER, "OPS_BOT_INSTANCE_KEY": "", "OPS_BOT_CHANNEL_ID": "group_10001"},
            {"OPS_BOT_ADAPTER_KEY": ADAPTER, "OPS_BOT_INSTANCE_KEY": INSTANCE, "OPS_BOT_CHANNEL_ID": ""},
            {"OPS_BOT_ADAPTER_KEY": ADAPTER, "OPS_BOT_INSTANCE_KEY": INSTANCE, "OPS_BOT_CHANNEL_ID": "group_10001", "OPS_BOT_OPERATORS": []},
        ],
        ids=["no-adapter", "no-instance", "no-channel", "no-operators"],
    )
    def test_incomplete_ops_bot_config_does_not_fall_back(self, incomplete: Dict[str, Any]) -> None:
        """拼不出会话标识时回落会得到 `--` 这种谁也匹配不上的值，落进集合反而更危险"""
        policy = OpsAgentPolicy.from_config(_raw(**incomplete))  # type: ignore[arg-type]
        assert policy.groups == frozenset()

    def test_a_filled_group_list_wins(self) -> None:
        policy = OpsAgentPolicy.from_config(
            _raw(
                OPS_AGENT_GROUPS=[OTHER_GROUP_KEY],
                OPS_BOT_ADAPTER_KEY=ADAPTER,
                OPS_BOT_INSTANCE_KEY=INSTANCE,
                OPS_BOT_CHANNEL_ID="group_10001",
                OPS_BOT_OPERATORS=[OPERATOR],
            ),
        )  # type: ignore[arg-type]
        assert policy.groups == frozenset({OTHER_GROUP_KEY})


class TestOperatorMatching:
    @pytest.mark.parametrize(
        ("entry", "adapter_key", "user_id", "expected"),
        [
            (OPERATOR, ADAPTER, OPERATOR, True),
            (OPERATOR, ADAPTER, OTHER_OPERATOR, False),
            (OPERATOR, "telegram", OPERATOR, False),
            (f"telegram:{OPERATOR}", "telegram", OPERATOR, True),
            ("Telegram : 10001", "telegram", OPERATOR, True),
            ("telegram:10001", ADAPTER, OPERATOR, False),
        ],
    )
    def test_entries_are_parsed_like_the_super_user_list(self, entry: str, adapter_key: str, user_id: str, expected: bool) -> None:
        """纯号码只当 OneBot 的 QQ 号：别的平台恰好同号的人不能命中"""
        policy = build_policy(operators=(entry,))
        assert is_operator(policy, adapter_key, user_id) is expected


class TestPrivateCounterpart:
    @pytest.mark.parametrize(
        ("adapter_key", "channel_id", "expected"),
        [
            (ADAPTER, f"private_{OPERATOR}", OPERATOR),
            (ADAPTER, "private_10001-extra", ""),
            (ADAPTER, "private_", ""),
            (ADAPTER, "group_10001", ""),
            ("telegram", f"private_{OPERATOR}", ""),
            ("sse", f"private_{OPERATOR}", ""),
            (ADAPTER, "private_１０００１", ""),
        ],
    )
    def test_only_onebot_private_identifiers_are_deterministic(self, adapter_key: str, channel_id: str, expected: str) -> None:
        """别的适配器的对方标识由调用方填写，拿它授权等于把名单交给攻击者"""
        assert private_counterpart_from_channel(adapter_key, channel_id) == expected


class TestTenantCannotSeeTheConfig:
    """租户读系统配置时这六项既不在列表里、也单项读不到

    它们决定"谁能操作平台方"，读得到就等于把冒充运维人员所需的清单递过去。
    """

    @pytest.fixture
    def spy(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        from nekro_agent.routers import config as config_router
        from nekro_agent.services.config_resolver import config_resolver
        from nekro_agent.services.config_service import UnifiedConfigService

        names = [
            "OPS_AGENT_TOOLS_ENABLED",
            "OPS_AGENT_OPERATORS",
            "OPS_AGENT_GROUPS",
            "OPS_AGENT_PRIVATE_ENABLED",
            "OPS_AGENT_WRITE_LIMIT_PER_ROUND",
            "OPS_AGENT_WRITE_LIMIT_PER_HOUR",
        ]
        items = [{"key": name, "value": "x", "title": name, "type": "str"} for name in names]
        items.append({"key": "AI_CHAT_PRESET_NAME", "value": "默认", "title": "人设", "type": "str"})

        async def no_inherited(_config_key: str) -> None:
            return None

        async def no_groups(_user: Any) -> Dict[str, Any]:
            return {}

        monkeypatch.setattr(UnifiedConfigService, "get_config_list", lambda _key: [dict(item) for item in items])
        monkeypatch.setattr(UnifiedConfigService, "get_config_item", lambda _key, item_key: next((dict(item) for item in items if item["key"] == item_key), None))
        monkeypatch.setattr(config_resolver, "inherited_values", no_inherited)
        # 模型组的按归属裁剪是另一条正交特性（见 `test_config_read_scope_and_model_group_view.py`），
        # 它会去读库；与本文件要钉的「平台方专属项不可见」无关，换掉它以免在这里拉进数据库
        monkeypatch.setattr(config_router, "_visible_model_groups", no_groups)
        return config_router

    def _tenant(self) -> Any:
        from nekro_agent.models.db_user import DBUser
        from nekro_agent.schemas.tenant import TenantRole
        from nekro_agent.services.user.role import Role

        user = DBUser(
            username="tenant-admin",
            password="",
            adapter_key="webui",
            platform_userid="0",
            perm_level=Role.Admin,
            tenant_id=1,
            tenant_role=TenantRole.TENANT_ADMIN.value,
        )
        user.id = 11
        return user

    async def test_the_list_omits_every_field(self, spy: Any) -> None:
        listed = await spy.get_config_list(config_key="system", _current_user=self._tenant())
        keys = {item.key for item in listed}
        assert not {key for key in keys if key.startswith("OPS_AGENT_")}
        assert "AI_CHAT_PRESET_NAME" in keys, "其余系统配置照常可见"

    @pytest.mark.parametrize("name", ["OPS_AGENT_TOOLS_ENABLED", "OPS_AGENT_OPERATORS", "OPS_AGENT_GROUPS"])
    async def test_a_single_read_reports_it_as_missing(self, spy: Any, name: str) -> None:
        """以"不存在"掩盖存在性：403 会告诉租户这一项确实有"""
        from nekro_agent.schemas.errors import ConfigNotFoundError

        with pytest.raises(ConfigNotFoundError):
            await spy.get_config_item(config_key="system", item_key=name, _current_user=self._tenant())

    async def test_the_platform_owner_still_sees_them(self, spy: Any) -> None:
        from nekro_agent.models.db_user import DBUser
        from nekro_agent.schemas.tenant import TenantRole
        from nekro_agent.services.user.role import Role

        owner = DBUser(
            username="owner",
            password="",
            adapter_key="webui",
            platform_userid="0",
            perm_level=Role.Super,
            tenant_id=None,
            tenant_role=TenantRole.PLATFORM_OWNER.value,
        )
        owner.id = 1
        listed = await spy.get_config_list(config_key="system", _current_user=owner)
        assert {item.key for item in listed} >= {"OPS_AGENT_TOOLS_ENABLED", "OPS_AGENT_OPERATORS"}


def _tool_functions() -> Dict[str, Any]:
    from nekro_agent.services.ops_agent.tools import OPS_TOOL_FUNCS

    return dict(OPS_TOOL_FUNCS)


class TestToolSignatures:
    """判据只看宿主侧事实：工具签名里不许有"操作者 / 身份"这类参数"""

    IDENTITY_WORDS = ("operator", "identity", "user", "sender", "trusted", "token", "grant", "container")

    @pytest.mark.parametrize("name", sorted(_tool_functions()))
    def test_no_identity_parameter(self, name: str) -> None:
        import inspect

        parameters = list(inspect.signature(_tool_functions()[name]).parameters)
        assert parameters[0] == "ctx", parameters
        offenders = [item for item in parameters if any(word in item.lower() for word in self.IDENTITY_WORDS)]
        assert not offenders, (name, offenders)

    @pytest.mark.parametrize("name", sorted(_tool_functions()))
    def test_every_tool_is_named_with_the_prefix(self, name: str) -> None:
        """`rpc_exec` 靠前缀决定要不要在执行前复核授权，名字漏了前缀就等于跳过了复核"""
        from nekro_agent.services.ops_agent.provider import METHOD_PREFIX

        assert name.startswith(METHOD_PREFIX)

    def test_write_tools_are_exactly_the_declared_ones(self) -> None:
        """分类写错会让写操作绕过次数上限与污染判定"""
        from nekro_agent.services.ops_agent.tools import OPS_TOOL_FUNCS, OPS_WRITE_TOOLS

        expected = {
            "ops_set_instance_enabled",
            "ops_restart_protocol",
            "ops_set_tenant_status",
            "ops_set_channel_active",
            "ops_quota_bonus",
            "ops_tools_off",
        }
        assert OPS_WRITE_TOOLS == frozenset(expected)
        assert expected <= set(OPS_TOOL_FUNCS)
        assert len(OPS_TOOL_FUNCS) == 15
        assert len(set(OPS_TOOL_FUNCS) - OPS_WRITE_TOOLS) == 9

    def test_each_tool_documents_itself(self) -> None:
        """提示词里的工具说明取自 docstring 首段，缺了就是把一个没有说明的工具交给模型"""
        for name, func in _tool_functions().items():
            assert (func.__doc__ or "").strip(), name


def test_round_facts_defaults_are_closed() -> None:
    """`RoundFacts` 的默认值必须是"不归平台方""没有私聊对方"这类保守值

    少传一个字段时应当拿到拒绝，而不是拿到一次沉默的放行。
    """
    facts = RoundFacts(adapter_key=ADAPTER, instance_key=INSTANCE, chat_key=OPS_CHAT_KEY)
    assert facts.is_group is False and facts.is_private is False
    assert facts.instance_platform_owned is False
    assert facts.private_counterpart == ""
    assert evaluate_round(build_policy(), facts, [human()]).decision is OpsRoundDecision.TENANT_INSTANCE


def test_a_policy_without_operators_never_allows() -> None:
    policy = build_policy(operators=())
    assert not is_operator(policy, ADAPTER, OPERATOR)
    assert evaluate_round(policy, build_facts(), [human()]).decision is OpsRoundDecision.UNTRUSTED_TRIGGER
