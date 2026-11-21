"""运维 Bot 受理判定与指令解析的回归测试

保护三组契约：

**准入判定**（`services/ops_bot/policy.py`）
运维通道是平台方直连宿主级操作的入口，任何一条闸门失效都等于把总后台开放出去。
这里逐条钉死拒绝理由，尤其是两个会无条件把 `is_super_user` 置真的伪来源：
模型的 Tool-Use 调用（`adapter_key="agent"`）与 WebUI 的命令执行端点
（`adapter_key="webui"`，该端点只要求 `Role.Admin`，租户管理员正好够）。

**指令解析**（`services/ops_bot/actions.py`）
解析结果直接决定"这条指令走读路径还是写路径"，错一个分支就是把写操作
变成了在聊天里直接执行。

**读写分类**
分类是安全边界。新增指令时必须显式声明类别，本组测试会挡住漏声明。
"""

import pytest

from nekro_agent.services.ops_bot.actions import (
    OPS_ACTIONS,
    OpsActionKind,
    parse_ops_command,
    resolve_action,
)
from nekro_agent.services.ops_bot.policy import (
    OpsAccessDecision,
    OpsBotPolicy,
    evaluate_ops_access,
)
from nekro_agent.services.ops_bot.reports import READ_RENDERERS

OPS_ADAPTER = "onebot_v11"
OPS_INSTANCE = "opsbot"
OPS_CHANNEL = "group_900001"
OPS_CHAT_KEY = f"{OPS_ADAPTER}-{OPS_INSTANCE}-{OPS_CHANNEL}"
OPERATOR = "10001"
OUTSIDER = "20002"
IM_ADAPTERS = frozenset({OPS_ADAPTER, "telegram"})


def build_policy(**overrides: object) -> OpsBotPolicy:
    defaults: dict[str, object] = {
        "enabled": True,
        "adapter_key": OPS_ADAPTER,
        "instance_key": OPS_INSTANCE,
        "channel_id": OPS_CHANNEL,
        "operators": frozenset({OPERATOR}),
    }
    defaults.update(overrides)
    return OpsBotPolicy(**defaults)  # type: ignore[arg-type]


class TestOpsAccessGate:
    def test_operator_in_ops_channel_is_allowed(self) -> None:
        decision = evaluate_ops_access(
            build_policy(),
            adapter_key=OPS_ADAPTER,
            chat_key=OPS_CHAT_KEY,
            user_id=OPERATOR,
            im_adapter_keys=IM_ADAPTERS,
        )
        assert decision is OpsAccessDecision.ALLOWED

    def test_disabled_by_default_shape(self) -> None:
        """默认关闭：即便其余条件全部满足也不受理"""
        decision = evaluate_ops_access(
            build_policy(enabled=False),
            adapter_key=OPS_ADAPTER,
            chat_key=OPS_CHAT_KEY,
            user_id=OPERATOR,
            im_adapter_keys=IM_ADAPTERS,
        )
        assert decision is OpsAccessDecision.DISABLED

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("adapter_key", ""),
            ("instance_key", ""),
            ("channel_id", ""),
            ("operators", frozenset()),
        ],
    )
    def test_incomplete_config_is_rejected(self, field: str, value: object) -> None:
        """配置缺一角就整条拒绝，绝不退化成「少一个条件也能过」"""
        decision = evaluate_ops_access(
            build_policy(**{field: value}),
            adapter_key=OPS_ADAPTER,
            chat_key=OPS_CHAT_KEY,
            user_id=OPERATOR,
            im_adapter_keys=IM_ADAPTERS,
        )
        assert decision is OpsAccessDecision.NOT_CONFIGURED

    @pytest.mark.parametrize("pseudo_adapter", ["agent", "webui"])
    def test_pseudo_origins_are_rejected(self, pseudo_adapter: str) -> None:
        """模型 Tool-Use 与 WebUI 命令执行都把 is_super_user 置真，必须在这里挡住

        它们即使伪造出完全正确的 chat_key 与白名单账号也过不去，
        因为来源标识不是真实加载的适配器。
        """
        decision = evaluate_ops_access(
            build_policy(),
            adapter_key=pseudo_adapter,
            chat_key=OPS_CHAT_KEY,
            user_id=OPERATOR,
            im_adapter_keys=IM_ADAPTERS,
        )
        assert decision is OpsAccessDecision.NON_IM_ORIGIN

    def test_adapter_not_loaded_is_rejected(self) -> None:
        """配置里写的适配器没被加载时同样拒绝，不给"名字对上就算"的机会"""
        decision = evaluate_ops_access(
            build_policy(),
            adapter_key=OPS_ADAPTER,
            chat_key=OPS_CHAT_KEY,
            user_id=OPERATOR,
            im_adapter_keys=frozenset({"telegram"}),
        )
        assert decision is OpsAccessDecision.NON_IM_ORIGIN

    @pytest.mark.parametrize(
        "chat_key",
        [
            f"{OPS_ADAPTER}-{OPS_INSTANCE}-group_900002",
            f"{OPS_ADAPTER}-otherinst-{OPS_CHANNEL}",
            f"{OPS_ADAPTER}-{OPS_CHANNEL}",
            f"{OPS_ADAPTER}-{OPS_INSTANCE}-{OPS_CHANNEL}x",
        ],
    )
    def test_other_channels_are_rejected(self, chat_key: str) -> None:
        """运维实例被拉进别的群、或别的实例进了运维群，都不受理

        判定是三段全等而不是前缀匹配，最后一个用例（多一个字符）就是防前缀匹配的。
        """
        decision = evaluate_ops_access(
            build_policy(),
            adapter_key=OPS_ADAPTER,
            chat_key=chat_key,
            user_id=OPERATOR,
            im_adapter_keys=IM_ADAPTERS,
        )
        assert decision is OpsAccessDecision.WRONG_CHANNEL

    def test_non_operator_in_ops_channel_is_rejected(self) -> None:
        """核心不变量：权限主体是人不是实例

        运维频道里的任何其他人（包括被拉进群的陌生人）都无权指挥运维 Bot。
        """
        decision = evaluate_ops_access(
            build_policy(),
            adapter_key=OPS_ADAPTER,
            chat_key=OPS_CHAT_KEY,
            user_id=OUTSIDER,
            im_adapter_keys=IM_ADAPTERS,
        )
        assert decision is OpsAccessDecision.NOT_OPERATOR

    def test_operator_outside_ops_channel_is_rejected(self) -> None:
        """白名单成员在别的群里也不行：两个条件是与不是或"""
        decision = evaluate_ops_access(
            build_policy(),
            adapter_key=OPS_ADAPTER,
            chat_key=f"{OPS_ADAPTER}-tenantinst-group_777",
            user_id=OPERATOR,
            im_adapter_keys=IM_ADAPTERS,
        )
        assert decision is OpsAccessDecision.WRONG_CHANNEL

    def test_operator_id_is_compared_after_trim(self) -> None:
        """配置里手填的账号常带空格，不能因此把合法运维人员挡在外面"""
        decision = evaluate_ops_access(
            build_policy(),
            adapter_key=OPS_ADAPTER,
            chat_key=OPS_CHAT_KEY,
            user_id=f"  {OPERATOR} ",
            im_adapter_keys=IM_ADAPTERS,
        )
        assert decision is OpsAccessDecision.ALLOWED


class TestOpsChatKey:
    def test_ops_chat_key_matches_adapter_encoding(self) -> None:
        """运维频道标识必须与适配器构造 chat_key 的规则一致，否则永远匹配不上"""
        from nekro_agent.adapters.onebot_v11.core.channel import build_chat_key

        assert build_policy().ops_chat_key == build_chat_key(OPS_INSTANCE, OPS_CHANNEL)


class TestCommandParsing:
    @pytest.mark.parametrize(
        ("raw_text", "action", "target"),
        [
            ("instances", "instances", ""),
            ("  health  ", "health", ""),
            ("INSTANCES", "instances", ""),
            ("disable-instance inst1", "disable-instance", "inst1"),
            ("suspend-tenant 7", "suspend-tenant", "7"),
        ],
    )
    def test_valid_inputs(self, raw_text: str, action: str, target: str) -> None:
        parsed = parse_ops_command(raw_text)
        assert parsed.error == ""
        assert (parsed.action, parsed.target) == (action, target)

    def test_empty_input_is_help(self) -> None:
        parsed = parse_ops_command("   ")
        assert parsed.is_help
        assert parsed.error == ""

    @pytest.mark.parametrize(
        "raw_text",
        [
            "definitely_not_a_command",
            "instances extra",
            "disable-instance",
            "disable-instance a b",
        ],
    )
    def test_invalid_inputs_report_error_without_action(self, raw_text: str) -> None:
        """解析失败必须同时清空 action：留着 action 会让调用方误以为可以继续执行"""
        parsed = parse_ops_command(raw_text)
        assert parsed.error
        assert parsed.action == ""

    def test_target_with_colon_is_not_split(self) -> None:
        """通用参数解析器会把 `a:b` 当 K-V 拆走，运维指令自己切就是为了避免这一点"""
        parsed = parse_ops_command("disable-instance weird:key")
        assert (parsed.action, parsed.target) == ("disable-instance", "weird:key")


class TestActionCatalog:
    def test_every_action_declares_a_kind(self) -> None:
        for name, spec in OPS_ACTIONS.items():
            assert spec.name == name
            assert spec.kind in (OpsActionKind.READ, OpsActionKind.WRITE)
            assert spec.summary

    def test_every_read_action_has_a_renderer(self) -> None:
        """读指令必须有取数实现，否则只会在运行时暴露成「命令存在但一按就炸」"""
        read_actions = {name for name, spec in OPS_ACTIONS.items() if spec.kind is OpsActionKind.READ}
        assert read_actions == set(READ_RENDERERS)

    def test_read_actions_take_no_target(self) -> None:
        for spec in OPS_ACTIONS.values():
            if spec.kind is OpsActionKind.READ:
                assert not spec.requires_target

    @pytest.mark.parametrize(
        "action",
        ["enable-instance", "disable-instance", "restart-container", "suspend-tenant", "resume-tenant", "tools"],
    )
    def test_state_changing_actions_are_classified_as_write(self, action: str) -> None:
        """核心不变量：任何会改变系统状态的指令都必须是 WRITE

        写指令走共用的执行层、写审计、回复回执；分错成 READ 就会绕过这三样。
        """
        spec = resolve_action(action)
        assert spec is not None
        assert spec.kind is OpsActionKind.WRITE
        assert spec.requires_target

    def test_unknown_action_resolves_to_none(self) -> None:
        assert resolve_action("rm-rf") is None


class TestOpsConfigIsPlatformOnly:
    """运维配置必须对租户身份不可读不可写

    这一组守的是一条实测发现的提权路径：`/config/batch/system` 只挡凭据类字段，
    租户管理员（`Role.Admin`，够得着该端点）可以把自己的账号写进 `OPS_BOT_OPERATORS`，
    从而拿到平台方的运维通道，而通道背后是宿主级的实例与容器操作。
    """

    def test_all_ops_config_fields_are_platform_only(self) -> None:
        from nekro_agent.core.config import CoreConfig
        from nekro_agent.routers.config import _is_platform_only_config

        ops_fields = [name for name in CoreConfig.model_fields if name.startswith("OPS_BOT")]
        assert ops_fields, "运维配置项不应为空，否则本测试形同虚设"
        for name in ops_fields:
            assert _is_platform_only_config("system", name), name

    def test_policy_inputs_are_all_covered(self) -> None:
        """逐一钉死判定所依赖的配置项，新增一项漏挡时这里会红"""
        from nekro_agent.routers.config import _is_platform_only_config

        for name in (
            "OPS_BOT_ENABLED",
            "OPS_BOT_ADAPTER_KEY",
            "OPS_BOT_INSTANCE_KEY",
            "OPS_BOT_CHANNEL_ID",
            "OPS_BOT_OPERATORS",
        ):
            assert _is_platform_only_config("system", name), name

    @pytest.mark.parametrize(
        ("config_key", "item_key"),
        [
            ("system", "AI_CHAT_PRESET_NAME"),
            ("system", "SUPER_USERS_EXTRA"),
            ("plugin_basic", "OPS_BOT_ENABLED"),
            ("plugin_basic", "SUPER_USERS"),
        ],
    )
    def test_unrelated_config_stays_writable(self, config_key: str, item_key: str) -> None:
        """守护范围要精确：只锁 system 配置里的运维项与超级用户名单，不误伤插件/适配器同名项、不按前缀误伤"""
        from nekro_agent.routers.config import _is_platform_only_config

        assert not _is_platform_only_config(config_key, item_key)

    def test_super_user_list_is_platform_only(self) -> None:
        """超级用户名单与运维人员名单同一档：它是在聊天里冒充超级用户（能执行 `sh`）所需的全部情报"""
        from nekro_agent.routers.config import _is_platform_only_config

        assert _is_platform_only_config("system", "SUPER_USERS")

    def test_ops_config_is_not_overridable(self) -> None:
        """运维配置不得进入覆盖模型

        一旦某项标记 `overridable`，它就会出现在适配器/实例/频道的覆盖配置里，
        租户可以在自己的频道把运维 Bot 打开——那是另一条自助提权路径。
        """
        from nekro_agent.core.overridable_config import OverridableConfig

        assert not [name for name in OverridableConfig.model_fields if name.startswith("OPS_BOT")]
