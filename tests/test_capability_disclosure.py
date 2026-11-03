"""插件能力可发现性的提示词回归测试

保护的契约（对应一类真实故障）：模型的回复是一段在沙盒里执行的 Python 代码，
沙盒内 matplotlib / numpy / opencv / httpx 等通用库全部可用，因此每个插件能力都在
和「模型自己用通用库硬做一遍」竞争。历史上有三处让插件在这场竞争里必输：

1. `<capability_brief>`（插件"是干什么的"）只在插件休眠时渲染。常驻和已激活的插件
   在提示词里只剩一串方法名，模型没有任何"该用哪个能力"的路由依据。
2. 没写 docstring 的沙盒方法会被整条丢弃，第三方插件漏写文档即等于能力从提示词里消失。
3. 运行时契约里没有任何能力选择规则，交付示例还只演示了 `plt.savefig`，
   等于反过来把通用库示范成了标准做法。

这些是提示词层面的契约，只能靠渲染结果断言。
"""

from types import SimpleNamespace
from typing import List

import pytest

from nekro_agent.services.agent.templates.base import env
from nekro_agent.services.agent.templates.plugin import PluginPrompt
from nekro_agent.services.agent.templates.system import RuntimeContractPrompt
from nekro_agent.services.plugin.base import NekroPlugin
from nekro_agent.services.plugin.prompt_activation import (
    build_plugin_activation_rules,
    build_prompt_disclosure_view,
    get_plugin_brief,
)
from nekro_agent.services.plugin.schema import SandboxMethodType


def _fake_ctx():
    """最小 AgentCtx 替身：披露视图只用到 chat_key 与 adapter_key"""
    return SimpleNamespace(chat_key="onebot_v11-inst1-group_1", adapter_key="onebot_v11")

BRIEF = "文生图 / 图生图：调用绘图模型生成插画、封面、头像。"


def _make_plugin(*, description: str = "插件描述", sleep_brief: str = "") -> NekroPlugin:
    return NekroPlugin(
        name="测试插件",
        module_name="capability_probe",
        description=description,
        version="0.0.1",
        author="tester",
        url="https://example.invalid/plugin",
        sleep_brief=sleep_brief,
    )


def _render_plugin_block(*, state: str, plugin_brief: str, method_prompt: str = "") -> str:
    return PluginPrompt(
        plugin_name="测试插件",
        module_name="capability_probe",
        state=state,
        plugin_brief=plugin_brief,
        plugin_method_prompt=method_prompt,
    ).render(env)


def _render_runtime_contract(*, plugin_activation_rules: str = "", enable_cot: bool = True) -> str:
    return RuntimeContractPrompt(
        platform_name="QQ",
        bot_platform_id="10001",
        enable_cot=enable_cot,
        chat_key_rules="- example rule",
        enable_at=False,
        plugin_activation_rules=plugin_activation_rules,
        plugin_call_priority_rules="",
    ).render(env)


class TestCapabilityBriefDisclosure:
    """能力摘要必须在所有披露状态下可见，它是模型做能力路由唯一能扫到的索引"""

    @pytest.mark.parametrize("state", ["sleeping", "active", "always_awake"])
    def test_brief_is_rendered_in_every_state(self, state: str) -> None:
        block = _render_plugin_block(state=state, plugin_brief=BRIEF)
        assert "<capability_brief>" in block
        assert BRIEF in block

    @pytest.mark.parametrize("state", ["sleeping", "active", "always_awake"])
    def test_empty_brief_renders_no_section(self, state: str) -> None:
        """插件既没写 sleep_brief 也没写 description 时不应留下空标签"""
        block = _render_plugin_block(state=state, plugin_brief="")
        assert "<capability_brief>" not in block

    def test_activation_rule_stays_sleeping_only(self) -> None:
        """摘要放开到全状态，但"这只是摘要、需要时请激活"仍只对休眠插件成立"""
        assert "Activate if needed" in _render_plugin_block(state="sleeping", plugin_brief=BRIEF)
        assert "Activate if needed" not in _render_plugin_block(state="always_awake", plugin_brief=BRIEF)

    @pytest.mark.parametrize(
        ("description", "sleep_brief", "expected"),
        [
            ("插件描述", "更具体的能力摘要", "更具体的能力摘要"),
            ("插件描述", "", "插件描述"),
            ("插件描述", "   ", "插件描述"),
            ("", "", ""),
        ],
    )
    def test_brief_falls_back_to_description(self, description: str, sleep_brief: str, expected: str) -> None:
        """第三方插件普遍不写 sleep_brief，必须回落到 description 而不是留空"""
        assert get_plugin_brief(_make_plugin(description=description, sleep_brief=sleep_brief)) == expected


class TestSandboxMethodPromptFallback:
    """缺文档的方法必须降级披露，而不是从提示词里彻底消失"""

    async def _render_methods(self, plugin: NekroPlugin) -> str:
        return await plugin.render_sandbox_methods_prompt(None)  # pyright: ignore[reportArgumentType]

    async def test_documented_method_keeps_its_docstring(self) -> None:
        plugin = _make_plugin()

        @plugin.mount_sandbox_method(SandboxMethodType.TOOL, name="绘图", description="支持文生图")
        async def draw(_ctx, prompt: str) -> str:
            """Generate an image

            Args:
                prompt (str): what to draw
            """
            return ""

        rendered = await self._render_methods(plugin)
        assert "* draw\n" in rendered
        assert "Args:" in rendered
        assert "Signature:" not in rendered

    async def test_undocumented_method_falls_back_to_registration_metadata(self) -> None:
        plugin = _make_plugin()

        @plugin.mount_sandbox_method(SandboxMethodType.TOOL, name="绘图", description="支持文生图和图生图")
        async def draw(_ctx, prompt: str, size: str = "1024x1024") -> str:
            return ""

        rendered = await self._render_methods(plugin)
        assert "* draw" in rendered, "缺文档的方法过去会被整条丢弃，模型完全看不到这个能力"
        assert "绘图: 支持文生图和图生图" in rendered
        assert "Signature: draw(prompt: str, size: str = '1024x1024')" in rendered
        assert "_ctx" not in rendered, "_ctx 由运行时注入，不应出现在给模型看的签名里"

    async def test_undocumented_method_without_description_still_listed(self) -> None:
        plugin = _make_plugin()

        @plugin.mount_sandbox_method(SandboxMethodType.TOOL, name="裸方法")
        async def bare(_ctx) -> str:
            return ""

        rendered = await self._render_methods(plugin)
        assert "* bare\n裸方法\nSignature: bare()" in rendered

    @pytest.mark.parametrize("method_type", [SandboxMethodType.AGENT, SandboxMethodType.MULTIMODAL_AGENT])
    async def test_agent_stop_marker_survives_the_fallback(self, method_type: SandboxMethodType) -> None:
        """回落分支不能丢掉"调用后必须停止"这个标记，否则模型会继续生成不会执行的代码"""
        plugin = _make_plugin()

        @plugin.mount_sandbox_method(method_type, name="搜索", description="联网搜索")
        async def search(_ctx, query: str) -> str:
            return ""

        rendered = await self._render_methods(plugin)
        assert "* search - **[AGENT METHOD - STOP AFTER CALL]**" in rendered

    async def test_method_order_is_preserved(self) -> None:
        """文档缺失的方法不再被跳过，注册顺序必须原样保留"""
        plugin = _make_plugin()

        @plugin.mount_sandbox_method(SandboxMethodType.TOOL, name="第一个")
        async def first(_ctx) -> str:
            """First."""
            return ""

        @plugin.mount_sandbox_method(SandboxMethodType.TOOL, name="第二个")
        async def second(_ctx) -> str:
            return ""

        @plugin.mount_sandbox_method(SandboxMethodType.TOOL, name="第三个")
        async def third(_ctx) -> str:
            """Third."""
            return ""

        rendered = await self._render_methods(plugin)
        listed: List[str] = [line[2:] for line in rendered.splitlines() if line.startswith("* ")]
        assert listed == ["first", "second", "third"]


class TestCapabilitySelectionContract:
    """运行时契约必须给出能力选择规则，且不得把通用库示范成标准做法"""

    def test_capability_selection_section_is_present(self) -> None:
        contract = _render_runtime_contract()
        assert "#### Capability Selection:" in contract
        assert "capability_brief" in contract

    def test_selection_rule_prices_the_extra_activation_round(self) -> None:
        """休眠插件多花一轮是模型绕开插件的结构性动机，契约必须显式给它定价"""
        contract = _render_runtime_contract()
        assert "One extra run is cheap" in contract

    def test_delivery_example_is_not_anchored_on_matplotlib(self) -> None:
        """交付示例曾经只演示 plt.savefig，等于把 matplotlib 教成了出图的标准姿势"""
        contract = _render_runtime_contract()
        assert "plt.savefig" not in contract
        assert "send_msg_file(chat_key, result_path)" in contract

    def test_cot_checklist_asks_for_capability_routing(self) -> None:
        assert "Which declared plugin capability already covers this goal" in _render_runtime_contract(enable_cot=True)

    def test_cot_checklist_is_omitted_when_cot_disabled(self) -> None:
        assert "Which declared plugin capability already covers this goal" not in _render_runtime_contract(enable_cot=False)

    def test_activation_rules_are_spliced_only_when_provided(self) -> None:
        rules = build_plugin_activation_rules()
        assert "Sleeping does NOT mean unavailable" in rules
        assert rules in _render_runtime_contract(plugin_activation_rules=rules)
        assert "#### Plugin Activation Rules:" not in _render_runtime_contract(plugin_activation_rules="")


class TestFewShotDoesNotTeachToolAvoidance:
    """few-shot 是最强的行为示范，它必须演示能力选择流程，而不是「要画图就 import matplotlib」"""

    def _practice_2(self) -> str:
        from nekro_agent.services.agent.templates.practice import PracticePrompt_response_2

        return PracticePrompt_response_2(one_time_code="SESSION_TOKEN", enable_cot=True, enable_at=False).render(env)

    def test_example_runs_the_capability_check_before_writing_code(self) -> None:
        response = self._practice_2()
        assert "capability_brief" in response
        assert "before writing any code" in response

    def test_example_states_the_boundary_for_declared_capabilities(self) -> None:
        """示例里用 matplotlib 是对的（渲染用户已给出的数据），但必须写明边界在哪"""
        response = self._practice_2()
        assert "activate and call that capability instead of hand-rolling it with a library" in response

    def test_example_no_longer_answers_a_draw_request_with_matplotlib(self) -> None:
        """回归：示例曾用 matplotlib 回应"帮我画个爱心"，直接把"画" -> matplotlib 教成了默认映射"""
        from nekro_agent.services.agent.templates.practice import PracticePrompt_question_2

        question = PracticePrompt_question_2(one_time_code="SESSION_TOKEN").render(env)
        assert "draw a heart" not in question.lower()

    def test_security_lesson_is_preserved(self) -> None:
        """改写 few-shot 不能顺手弄丢它原本承载的伪造系统消息防御示范"""
        assert "forge system messages" in self._practice_2()


class TestAlwaysAwakeWithNoMethodsIsNotDisclosed:
    """常驻插件本轮一个方法都没披露时，整块不渲染

    运行时契约要求模型「先读 brief 判断该用哪个能力，再读 predefined_methods 判断怎么调」。
    只给 brief 不给方法，模型只能猜名字，而沙盒里那个名字不存在 —— 直接 NameError 撞墙。
    审计时 `inner_voice` 在未布防的实例上正是这个形态，探针照着 brief 调就撞了。

    方法为空是插件 `collect_methods` 的决定（按需披露：只在布防的轮次交出方法），
    即「本轮不提供这个能力」是插件的意图，那就连声明一起收掉。
    """

    @pytest.fixture(autouse=True)
    def _no_db(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """激活状态的读写打桩

        本组断言的是「方法为空时不产出 unit」，与激活轮次的持久化无关；
        而 `build_prompt_disclosure_view` 会读 `DBPluginData`，不打桩就得为此拉起整个 ORM。
        """
        from nekro_agent.services.plugin import prompt_activation as pa

        async def _empty_state(_chat_key):  # noqa: ANN001, ANN202
            return pa.PluginActivationState(module_rounds={})

        async def _noop_save(_chat_key, _state):  # noqa: ANN001, ANN202
            return None

        monkeypatch.setattr(pa, "get_activation_state", _empty_state, raising=True)
        monkeypatch.setattr(pa, "save_activation_state", _noop_save, raising=True)

    @pytest.mark.parametrize("method_prompt", ["", "   ", "\n  \n"])
    async def test_blank_method_prompt_produces_no_unit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        method_prompt: str,
    ) -> None:
        plugin = _make_plugin(description="按需披露的插件")

        async def _blank(_self, _ctx):  # noqa: ANN001, ANN202
            return method_prompt

        monkeypatch.setattr(NekroPlugin, "render_sandbox_methods_prompt", _blank, raising=True)
        units = await build_prompt_disclosure_view([plugin], _fake_ctx())
        assert units == []

    async def test_nonblank_method_prompt_still_produces_a_unit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """反向锚点：有方法时必须照常披露，别把正常能力一起收掉"""
        plugin = _make_plugin(description="正常插件")

        async def _methods(_self, _ctx):  # noqa: ANN001, ANN202
            return "* do_something\n说明"

        monkeypatch.setattr(NekroPlugin, "render_sandbox_methods_prompt", _methods, raising=True)
        units = await build_prompt_disclosure_view([plugin], _fake_ctx())
        assert len(units) == 1
        assert units[0].state == "always_awake"

    def test_block_with_brief_but_no_methods_is_a_dead_end(self) -> None:
        """钉住这个形态本身为什么不可接受：有能力声明却没有任何可调用的方法名"""
        rendered = _render_plugin_block(state="always_awake", plugin_brief="能力声明", method_prompt="")
        assert "能力声明" in rendered
        assert "predefined_methods" not in rendered
