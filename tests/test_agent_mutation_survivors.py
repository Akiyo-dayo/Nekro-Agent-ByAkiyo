"""变异测试打出来的缺口：这几处行为改动**改回旧样子测试也不红**

## 由来

本轮对三组改动（解析器 / 迭代收尾 / LLM 拦截分类）做了 72 个定点变异——把每一处行为
逐个删掉或改回改造前的样子，跑定向用例看会不会红。历史教训是「零件都有测试、接线一根都没测」
（实测过 9 个组件变异全死、6 个接线变异活了 5 个），所以变异刻意压在接线上。

72 个里活下来 7 个，其中 1 个证明是**等价变异**（见本文件最后一节），其余 6 个是真缺口：

| 变异 | 改了什么 | 为什么原来杀不掉 |
|---|---|---|
| S4 | 删掉 `_notify_round_failure` 里的 `SESSION_ENABLE_FAILED_LLM_FEEDBACK` 早退 | `message_service` 的分派**全部**由源码文本断言覆盖，而现有断言只看「函数体里调过哪些函数」，读配置属性不是调用 |
| B9 | `describe_llm_failure(e.kinds)` 改成 `describe_llm_failure([])` | 同上：文本断言只验证「`describe_llm_failure` 这个名字出现在实参里」，实参**喂进去的是什么**没人验 |
| B6 | 流式记账去掉 `if chunk_finish_reason:` 保护 | 既有流式用例的最后一块都自带 finish_reason，没有一条在收尾块**之后**再来一块 |
| B19 | 流式拦截异常的 `stream_mode=True` 传成 `False` | 没有用例区分两条分支产出的错误消息 |
| B13 | `_finalize_code` 不再剃掉残留的尾随 ``` | 没有用例走「不带围栏的裸代码，最后一行拖着一截 ``` 」这条形态 |
| C8 | `_finalize_code` 开头那次 `.strip()` 删掉 | 三个调用点都已经各自 strip 过，端到端观测不到差别；只有直接钉这个函数自己的契约才杀得掉 |

## 这份测试与既有那几份的分工

`test_agent_round_control.py::TestMessageServiceDispatch` 与
`test_llm_capability_and_failure.py::TestTheFixesAreActuallyWiredIn` 对 `message_service`
做的是**源码文本/AST 断言**。那一层能挡住「分支被整个删掉」，但挡不住「分支还在、数据流断了」
（B9）也挡不住「不是函数调用的那些判据」（S4）。所以这里把 `_run_chat_agent_task` 真的
**跑起来**：替身只替到 DB / 适配器 / 广播 / `run_agent` 这一圈边界上，分派链、冷却闸门、
开关、文案全部走真实代码。

全部确定性：不起网络、不睡眠、不随机。冷却相关的用例靠把窗口设成 300s / 0s 两档来区分，
不靠「等时间过去」——300s 那档在任何机器上都不可能在两次调用之间自然过期。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import List, Optional, Tuple

import pytest

# 复用 test_agent_llm_content_block.py 的 openai SDK 替身，不另造一套
from test_agent_llm_content_block import (  # type: ignore[import-not-found]
    _FakeChunk,
    _FakeUsage,
    _install_fake_openai,
)

from nekro_agent.models.db_exec_code import ExecStopType
from nekro_agent.services.agent import run_agent as run_agent_module
from nekro_agent.services.agent.failure_feedback import (
    NOTICE_LLM_BLOCKED,
    NOTICE_LLM_EMPTY,
    NOTICE_LLM_FAILED,
    NOTICE_ROUND_ERROR,
    NOTICE_ROUND_NO_CODE,
    NOTICE_ROUND_STALLED,
    NOTICE_ROUND_UNFINISHED,
    failure_notice_throttle,
)
from nekro_agent.services.agent.model_capability import LLMFailureKind
from nekro_agent.services.agent.openai import (
    LLMContentBlockedError,
    gen_openai_chat_response,
)
from nekro_agent.services.agent.resolver import (
    ParsedCodeRunData,
    _finalize_code,
    _pick_longest_code_block,
    _pick_longest_legacy_span,
    parse_chat_response,
)
from nekro_agent.services.agent.run_agent import (
    AgentNoExecutableCodeError,
    AgentRoundStalledError,
    AgentRoundUnfinishedError,
    AllLLMRequestsFailedError,
    RoundStallDetector,
    normalize_error_signature,
)

# ======================================================================================
# 运行期驱动 message_service._run_chat_agent_task 的替身环境
# ======================================================================================


class _MessageServiceHarness:
    """把 `_run_chat_agent_task` 真的跑一遍，收集它实际发进聊天的回执

    替身只装在四个外部边界上：频道（DB）、适配器、系统事件广播、`run_agent` 本身。
    被测的分派链 / `_notify_round_failure` / 冷却闸门 / 文案常量全部是真实实现。
    """

    def __init__(
        self,
        *,
        enable_feedback: bool = True,
        cooldown_seconds: float = 0.0,
        chat_key: str = "onebot_v11-inst1-group_10001",
    ) -> None:
        self.chat_key = chat_key
        self.sent: List[Tuple[str, str]] = []
        self.run_agent_calls = 0
        self.config = SimpleNamespace(
            AI_GENERATE_TIMEOUT=1,
            AI_SCRIPT_MAX_RETRY_TIMES=3,
            SESSION_ENABLE_FAILED_LLM_FEEDBACK=enable_feedback,
            SESSION_FAILED_FEEDBACK_COOLDOWN_SECONDS=cooldown_seconds,
        )

    def install(self, monkeypatch, *, raises: Optional[BaseException], reset_throttle: bool = True) -> None:
        from nekro_agent.adapters.utils import adapter_utils
        from nekro_agent.services import message_service as msgsvc_module
        from nekro_agent.services import system_broadcast
        from nekro_agent.services.chat import universal_chat_service as ucs_module

        config = self.config

        class _Channel:
            channel_name = "测试群"
            channel_type = "group"

            async def get_preset(self):
                return SimpleNamespace(id=1, name="测试人设")

            async def get_effective_config(self):
                return config

        async def _get_channel(**_kwargs):
            return _Channel()

        async def _get_adapter(_chat_key):
            return SimpleNamespace(config=SimpleNamespace(SESSION_PROCESSING_WITH_EMOJI=False))

        async def _publish(_event):
            return None

        async def _send(chat_key: str, text: str, *_args, **_kwargs):
            self.sent.append((chat_key, text))

        async def _run_agent(**_kwargs):
            self.run_agent_calls += 1
            if raises is not None:
                raise raises

        monkeypatch.setattr(msgsvc_module.DBChatChannel, "get_channel", _get_channel)
        monkeypatch.setattr(adapter_utils, "get_adapter_for_chat", _get_adapter)
        monkeypatch.setattr(system_broadcast, "publish_system_event", _publish)
        monkeypatch.setattr(ucs_module.universal_chat_service, "send_operation_message", _send)
        monkeypatch.setattr(run_agent_module, "run_agent", _run_agent)
        if reset_throttle:
            failure_notice_throttle.reset()
        self._service = msgsvc_module.message_service

    async def run(self, chat_key: Optional[str] = None) -> None:
        await self._service._run_chat_agent_task(chat_key=chat_key or self.chat_key)  # noqa: SLF001


@pytest.fixture(autouse=True)
def _clean_throttle():
    """冷却闸门是进程内单例：不清干净的话用例之间会互相压制"""
    failure_notice_throttle.reset()
    yield
    failure_notice_throttle.reset()


# ======================================================================================
# 缺口 1（变异 S4）：回执总开关只挡住了源码文本断言看不见的那一半
# ======================================================================================


class TestFeedbackSwitchIsHonouredAtRuntime:
    """`SESSION_ENABLE_FAILED_LLM_FEEDBACK` 是运维**关掉刷屏**的唯一手段

    它在 `_notify_round_failure` 里是一条读配置属性的早退，不是函数调用，所以
    `_calls_made_inside(source, "_notify_round_failure")` 那类断言天生看不见它：
    实测把这两行删掉，定向用例 601 条全绿。关掉的开关照旧发消息，对运维就是「开关是假的」。
    """

    async def test_switch_off_sends_nothing(self, monkeypatch) -> None:
        harness = _MessageServiceHarness(enable_feedback=False)
        harness.install(monkeypatch, raises=AgentRoundStalledError("x", signature="ERROR|boom", repeats=2))

        await harness.run()

        assert harness.run_agent_calls == 1, "开关只该管回执，不该把整轮对话也一起跳过"
        assert harness.sent == [], "开关关着却发了回执"

    async def test_switch_on_sends_exactly_one(self, monkeypatch) -> None:
        """上一条的对照组

        没有它，上一条就可能是在测「这套替身根本发不出消息」——那种断言对任何变异都不红。
        """
        harness = _MessageServiceHarness(enable_feedback=True)
        harness.install(monkeypatch, raises=AgentRoundStalledError("x", signature="ERROR|boom", repeats=2))

        await harness.run()

        assert harness.sent == [(harness.chat_key, NOTICE_ROUND_STALLED)]


# ======================================================================================
# 缺口 2（变异 B9）：分类结果到用户文案之间的那根线没人验
# ======================================================================================


class TestLLMFailureKindsReachTheUserNotice:
    """`AllLLMRequestsFailedError.kinds` → `describe_llm_failure` → 聊天里那句话

    既有断言验的是「`describe_llm_failure` 出现在 `_notify_round_failure` 的实参里」。
    把实参换成一个恒定的空列表，这条断言照样成立，而所有用户拿到的都退回成通用文案
    ——「安全拦截要告诉用户换个说法」这件事就整条失效了（实测该变异 601 条全绿）。

    表驱动：每一行的期望文案都与其它行不同，任何一行喂错都会被单独抓出来。
    """

    @pytest.mark.parametrize(
        ("kinds", "expected"),
        [
            pytest.param([LLMFailureKind.CONTENT_BLOCKED] * 3, NOTICE_LLM_BLOCKED, id="全部被安全策略拦截"),
            pytest.param([LLMFailureKind.EMPTY_RESPONSE] * 3, NOTICE_LLM_EMPTY, id="全部是空响应"),
            pytest.param(
                [LLMFailureKind.CONTENT_BLOCKED, LLMFailureKind.OTHER],
                NOTICE_LLM_FAILED,
                id="混了网络错误就退回通用文案",
            ),
            pytest.param([], NOTICE_LLM_FAILED, id="没有分类信息时退回通用文案"),
        ],
    )
    async def test_notice_follows_the_kinds_carried_on_the_exception(
        self,
        monkeypatch,
        kinds: List[LLMFailureKind],
        expected: str,
    ) -> None:
        harness = _MessageServiceHarness()
        harness.install(monkeypatch, raises=AllLLMRequestsFailedError("所有 LLM 请求失败", kinds=kinds))

        await harness.run()

        assert harness.sent == [(harness.chat_key, expected)]


# ======================================================================================
# 缺口 3：四条回合失败路径的运行期分派（源码文本断言的运行期对照）
# ======================================================================================


class TestRoundFailureDispatchAtRuntime:
    """既有覆盖是读 `message_service.py` 的源码文本；这里补上真的跑一遍

    文本断言挡不住「分支还在但 isinstance 链被先命中的分支吃掉」，也挡不住
    「两条分支的文案对调」之外的任何数据流问题。
    """

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            pytest.param(
                AgentNoExecutableCodeError("模型连续 2 次未返回可执行代码块", attempts=2),
                NOTICE_ROUND_NO_CODE,
                id="没给出可执行代码",
            ),
            pytest.param(
                AgentRoundStalledError("无进展", signature="ERROR|boom", repeats=2),
                NOTICE_ROUND_STALLED,
                id="同一错误重复到底",
            ),
            pytest.param(AgentRoundUnfinishedError("烧满迭代"), NOTICE_ROUND_UNFINISHED, id="烧满迭代"),
            pytest.param(ValueError("多模态结果解析失败"), NOTICE_ROUND_ERROR, id="其它异常"),
        ],
    )
    async def test_each_failure_type_gets_its_own_notice(self, monkeypatch, exc: Exception, expected: str) -> None:
        harness = _MessageServiceHarness()
        harness.install(monkeypatch, raises=exc)

        await harness.run()

        assert harness.sent == [(harness.chat_key, expected)]

    async def test_a_successful_round_sends_no_notice(self, monkeypatch) -> None:
        """反向对照：`run_agent` 正常返回时一条回执都不该发

        没有它，上面那组就可能是在测「这套替身对什么都发一条」。
        """
        harness = _MessageServiceHarness()
        harness.install(monkeypatch, raises=None)

        await harness.run()

        assert harness.run_agent_calls == 1
        assert harness.sent == []


# ======================================================================================
# 缺口 4（变异 S3 的运行期版本）：冷却闸门
# ======================================================================================


class TestNoticeCooldownAtRuntime:
    """回执本身会变成新的洪水——08-11 那天真实群里被刷了十几条

    既有覆盖是「分支体里出现过 `_notify_round_failure(` 且没有直接 `send_operation_message`」，
    属于结构断言。这里验的是行为：同一会话连着两轮失败，第二条**不发**。
    """

    async def test_second_failure_inside_the_window_is_suppressed(self, monkeypatch) -> None:
        harness = _MessageServiceHarness(cooldown_seconds=300.0)
        harness.install(monkeypatch, raises=AgentRoundUnfinishedError("烧满迭代"))

        await harness.run()
        await harness.run()

        assert harness.run_agent_calls == 2, "被压下去的只是回执，对话本身照跑"
        assert harness.sent == [(harness.chat_key, NOTICE_ROUND_UNFINISHED)]
        assert failure_notice_throttle.suppressed_count(harness.chat_key) == 1

    async def test_zero_cooldown_lets_every_failure_through(self, monkeypatch) -> None:
        """对照组：冷却设成 0 时两条都发，证明上一条压下去的是**冷却**而不是别的什么"""
        harness = _MessageServiceHarness(cooldown_seconds=0.0)
        harness.install(monkeypatch, raises=AgentRoundUnfinishedError("烧满迭代"))

        await harness.run()
        await harness.run()

        assert harness.sent == [
            (harness.chat_key, NOTICE_ROUND_UNFINISHED),
            (harness.chat_key, NOTICE_ROUND_UNFINISHED),
        ]

    async def test_a_different_chat_is_not_suppressed(self, monkeypatch) -> None:
        """冷却按会话分桶：A 群炸了不该让 B 群收不到提示

        同一个 harness 连跑三次，只有 chat_key 变——冷却闸门是进程内单例，
        所以这里跑的就是真实的分桶行为。
        """
        harness = _MessageServiceHarness(cooldown_seconds=300.0)
        harness.install(monkeypatch, raises=AgentRoundUnfinishedError("烧满迭代"))

        await harness.run(chat_key="onebot_v11-inst1-group_A")
        await harness.run(chat_key="onebot_v11-inst1-group_B")
        await harness.run(chat_key="onebot_v11-inst1-group_A")  # A 仍在冷却窗口内

        assert harness.sent == [
            ("onebot_v11-inst1-group_A", NOTICE_ROUND_UNFINISHED),
            ("onebot_v11-inst1-group_B", NOTICE_ROUND_UNFINISHED),
        ]


# ======================================================================================
# 缺口 5（变异 B6）：流式 finish_reason 会被后一块冲掉
# ======================================================================================


class TestStreamFinishReasonIsNotClobbered:
    """收尾原因记下来之后，**后面还可能再来块**

    产品代码写的是 `if chunk_finish_reason: finish_reason = chunk_finish_reason` ——
    那个 `if` 就是为了不让后续块把已经记下的理由冲成空串。现场的流式收尾常常是
    「带 finish_reason 的块」之后再跟一块「只带 usage、finish_reason 为 null」的收尾块
    （`stream_options={"include_usage": True}` 就是产品代码自己开的）。

    既有流式用例的最后一块都自带 finish_reason，所以去掉那个 `if` 一条都不红：
    实测该变异 601 条全绿，而它的后果是整条流式拦截判据在真实供应商上失效。
    """

    async def test_trailing_usage_chunk_does_not_erase_the_block_reason(self, monkeypatch) -> None:
        _install_fake_openai(
            monkeypatch,
            result=[
                _FakeChunk(content=None, finish_reason="prohibited_content"),
                # include_usage 打开后供应商补发的收尾块：有 choices、finish_reason 为 null
                _FakeChunk(content=None, finish_reason=None, usage=_FakeUsage()),
            ],
        )

        with pytest.raises(LLMContentBlockedError) as excinfo:
            await gen_openai_chat_response(model="gemini-3.6-flash", messages=[], stream_mode=True)

        assert excinfo.value.finish_reason == "prohibited_content"

    async def test_a_later_real_reason_still_wins(self, monkeypatch) -> None:
        """反向对照：后面那块**真的**带了理由时，用的是后面那个

        没有这条，上面那条可以被「第一个理由永远优先」的实现骗过去，
        而那种实现会在「中途 content_filter、收尾 stop」的流上判错。
        """
        _install_fake_openai(
            monkeypatch,
            result=[
                _FakeChunk(content=None, finish_reason="stop"),
                _FakeChunk(content=None, finish_reason="content_filter", usage=_FakeUsage()),
            ],
        )

        with pytest.raises(LLMContentBlockedError) as excinfo:
            await gen_openai_chat_response(model="gemini-3.6-flash", messages=[], stream_mode=True)

        assert excinfo.value.finish_reason == "content_filter"


# ======================================================================================
# 缺口 6（变异 B19）：两条分支产出的错误消息分不出来
# ======================================================================================


class TestBlockedErrorSaysWhichBranchProducedIt:
    """733 份错误文件是按消息文本分类的，「流式 / 非流式」是排查时的第一个分叉

    注意断言写法：`"非流式"` 里**包含**子串 `"流式"`，所以
    `assert "流式" in message` 对两条分支都成立——那是一条恒真断言，任何变异都杀不掉。
    必须按「不含 `非流式`」来钉流式那一条（实测把 `stream_mode=True` 传成 `False`，
    601 条全绿）。
    """

    async def test_stream_branch_message_is_not_labelled_non_stream(self, monkeypatch) -> None:
        _install_fake_openai(monkeypatch, result=[_FakeChunk(content=None, finish_reason="prohibited_content")])

        with pytest.raises(LLMContentBlockedError) as excinfo:
            await gen_openai_chat_response(model="gemini-3.6-flash", messages=[], stream_mode=True)

        message = str(excinfo.value)
        assert "流式" in message
        assert "非流式" not in message, f"流式分支的错误消息被标成了非流式: {message}"

    async def test_non_stream_branch_message_is_labelled_non_stream(self, monkeypatch) -> None:
        from test_agent_llm_content_block import _FakeCompletion  # type: ignore[import-not-found]

        _install_fake_openai(monkeypatch, result=_FakeCompletion(content=None, finish_reason="prohibited_content"))

        with pytest.raises(LLMContentBlockedError) as excinfo:
            await gen_openai_chat_response(model="gemini-3.6-flash", messages=[], stream_mode=False)

        assert "非流式" in str(excinfo.value)


# ======================================================================================
# 缺口 7（变异 B13）：裸代码尾巴上那截 ``` 没人剃
# ======================================================================================


class TestTrailingFenceResidueIsTrimmed:
    """`_finalize_code` 剃尾随 ``` 的那两行，只在「不带围栏的裸代码」这条路上还有作用

    逐块正则是非贪婪的，捕获组在第一个 ``` 处就停了，所以走围栏那条路的候选永远不会以 ```
    结尾——剃尾巴对它是空操作。真正还会命中的是模型不带围栏直接输出代码、最后一行拖着半截
    ``` 的形态：不剃掉的话，沙盒里跑的就是一份多了三个反引号的脚本。

    实测把那两行删掉，定向用例 601 条全绿。
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param('send_msg_text(_ck, "在的")  # ```', 'send_msg_text(_ck, "在的")  #', id="行尾注释后拖着围栏"),
            pytest.param("x = 1  # ```", "x = 1  #", id="赋值语句后拖着围栏"),
        ],
    )
    def test_residue_is_removed_from_bare_code(self, raw: str, expected: str) -> None:
        parsed = parse_chat_response(raw)
        assert parsed.code_content == expected
        assert parsed.code_missing is False, "这条路径的前提是内容确实是可执行代码"

    def test_fenced_code_is_unaffected(self) -> None:
        """对照：同样的正文包进围栏里时，剃尾巴不参与——结果由逐块正则决定"""
        parsed = parse_chat_response('```python\nsend_msg_text(_ck, "在的")\n```')
        assert parsed.code_content == 'send_msg_text(_ck, "在的")'

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("  print(1)  ", "print(1)", id="前后空白"),
            pytest.param("  ```python\nprint(1)\n```  ", "print(1)", id="空白包着一整对围栏"),
            pytest.param("```python  \n\nprint(1)", "print(1)", id="只有开围栏"),
            pytest.param("print(1)\n```  ", "print(1)", id="只有闭围栏"),
        ],
    )
    def test_finalize_code_normalises_its_own_input(self, raw: str, expected: str) -> None:
        """`_finalize_code` 自己的契约：**不假设**入参已经 strip 过

        说清楚这条用例的性质：今天没有任何一条端到端输入能让入参带上前后空白——
        三个调用点（逐块候选 / 旧跨度候选 / 去掉 think 后的整段）都已经各自 `.strip()` 过，
        实测把开头那次 `.strip()` 删掉，8580 条构造输入下 `parse_chat_response` 的输出逐字不变。

        钉它是因为这个函数的存在理由是「让可编译判定发生在**最终形态**上」：
        `startswith("```python")` / `endswith("```")` 两道判据都对空白敏感，
        哪天多一个不 strip 的调用点，围栏残留就会带着进沙盒而这里一条都不红。
        """
        assert _finalize_code(raw) == expected


# ======================================================================================
# 等价变异 R9：删掉「作用域里至少三个 ``` 」这道前置，行为逐字不变
# ======================================================================================


class TestLegacySpanGuardPremise:
    """R9（删掉 `search_scope.count("```") >= 3` 这道前置）是**等价变异**，杀不掉

    求证过程不是读代码推断：把原版与去掉前置的版本同时加载，对 2606 条构造语料
    （fence 数 0..8 x 可编译/不可编译/中文散文/正文含 ``` ）逐条对拍，
    再对 19607 条 token 穷举串对拍，`code_content` / `code_missing` / `thought_chain`
    **零差异**。

    原因是结构性的：走到旧跨度回退那一支的前提是「逐块候选非空且编译不过」；
    作用域里只有一对围栏时，逐块候选与旧跨度候选是同一段文本（两者都 `.strip()`），
    于是回退候选必然同样编译不过、选不上；只有一个围栏时旧跨度正则压根不匹配。

    所以这里**不**去编一条杀得掉它的用例（编不出来），改为把那道前置**赖以成立的前提**
    钉住：作用域里不足三个 ``` 时，旧跨度候选要么是空串、要么与逐块候选逐字相同。
    这个前提哪天被正则改动破坏，前置就不再是安全的性能收窄，而这条用例会红。
    """

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param('```python\nsend_msg_text(_ck, "在的")\n```', id="标准单块"),
            pytest.param("```\nprint(1)\n```\n以上就是我的回复", id="闭合围栏后有尾随文字"),
            pytest.param("```python\nprint(1)", id="未闭合围栏"),
            pytest.param("我不能帮你写 ``` 这种东西", id="散文里孤立围栏"),
            pytest.param("```\n抱歉，我不能协助这个请求。\n```", id="围栏里装的是拒答"),
            pytest.param("```python\n\n```", id="空代码块"),
        ],
    )
    def test_legacy_span_adds_nothing_below_three_fences(self, raw: str) -> None:
        assert raw.count("```") < 3, "这条用例的前提是作用域里不足三个围栏"
        block_candidate = _finalize_code(_pick_longest_code_block(raw))
        legacy_candidate = _finalize_code(_pick_longest_legacy_span(raw))
        assert legacy_candidate in ("", block_candidate), (
            f"不足三个围栏时旧跨度给出了第三种结果 {legacy_candidate!r}（逐块给的是 {block_candidate!r}），"
            f"`search_scope.count('```') >= 3` 那道前置就不再是安全的收窄"
        )


# ======================================================================================
# 第二轮变异（N 系列）打出来的缺口
#
# 上一轮 71 个变异按「整块删掉 / 整体改回旧样子」来切，于是**逐条**归一化规则、
# 以及闸门里**单个** isinstance 分支这一层没有被切到。这一轮补 24 个细粒度变异，
# 又活下来 7 个：N7/N8/N9/N11（签名归一化的四条规则各自无人验）、N6（思维链标签清理）、
# 以及 N17/N18 两个**证明为等价/死代码**的（见本节最后）。
# ======================================================================================


class TestErrorSignatureNormalisationRules:
    """签名归一化的每一条规则各自都要有独立判据

    上一轮只验了「裸数字」（A8）与「路径」（A9）两条，于是 UUID / 时间戳 / 时钟 / 空白
    四条规则删掉之后 632 条定向用例**一条都不红**。

    这里分两种钉法，因为这四条规则的性质并不一样——实测（`probe_sig_layers.py` 对拍）：

    * **UUID**：真闸门。删掉它以后裸数字规则会把 UUID 拆成
      `<N>e<N>-e<N>b-<N>d<N>-a<N>-<N>` 这种**逐个 UUID 都不同**的形状，两轮换一个 UUID
      就换一个签名，无进展检测直接失效 → 用「两次只差 UUID 的输出必须同签名」来钉。
    * **时间戳 / 时钟**：删掉它们，后面的裸数字规则照样把 `2026-08-18 03:20:11` 折成
      `<N>-<N>-<N> <N>:<N>:<N>`，**两次仍然同签名**。也就是说单看「判等」这一个用途它们是
      冗余层，编不出「同签名 → 不同签名」的反例。所以改钉**产物形状**（`<TS>` 占位符）：
      这既是 docstring 写明的契约，也是日志可读性依赖的东西，更是它们真正的价值所在——
      哪天有人收窄裸数字规则（那条规则的注释自己就承认「吃掉所有数字是有代价的」），
      时间戳的折叠还有自己的用例兜着。
    """

    @pytest.mark.parametrize(
        ("left", "right", "volatile"),
        [
            pytest.param(
                "RuntimeError: task 550e8400-e29b-41d4-a716-446655440000 failed",
                "RuntimeError: task f47ac10b-58cc-4372-a567-0e02b2c3d479 failed",
                "UUID",
                id="两轮换一个请求 UUID",
            ),
            pytest.param(
                "TypeError: <object at 0x7f3a1c2d4e00> is not callable",
                "TypeError: <object at 0x7f9b2d3e5f11> is not callable",
                "对象地址",
                id="两轮换一个对象地址",
            ),
            pytest.param(
                "生成嵌入向量失败，已重试 3 次: Illegal header value b'Bearer '",
                "生成嵌入向量失败，已重试 7 次: Illegal header value b'Bearer '",
                "重试次数",
                id="线上真实样本：重试次数每轮递增",
            ),
        ],
    )
    def test_volatile_fragments_fold_into_one_signature(self, left: str, right: str, volatile: str) -> None:
        """只差一个易变片段的两次收尾必须折成同一个签名，否则无进展检测永远攒不够连续次数"""
        assert left != right, "这条用例的前提是两次输出确实不同"
        assert normalize_error_signature(ExecStopType.ERROR, left) == normalize_error_signature(
            ExecStopType.ERROR,
            right,
        ), f"{volatile}没有被归一化：两轮同一个错会被判成两个签名，无进展检测失效"

    @pytest.mark.parametrize(
        ("sandbox_output", "expected_signature"),
        [
            pytest.param(
                "RuntimeError: request at 2026-08-18 03:20:11 failed",
                "ERROR|RuntimeError: request at <TS> failed",
                id="日期时间戳",
            ),
            pytest.param(
                "RuntimeError: request at 2026-08-18T03:20:11.123456+08:00 failed",
                "ERROR|RuntimeError: request at <TS> failed",
                id="带小数秒与时区的 ISO 时间戳",
            ),
            pytest.param(
                "RuntimeError: deadline 03:20:11 exceeded",
                "ERROR|RuntimeError: deadline <TS> exceeded",
                id="裸时钟",
            ),
            pytest.param(
                "RuntimeError: task 550e8400-e29b-41d4-a716-446655440000 failed",
                "ERROR|RuntimeError: task <UUID> failed",
                id="UUID 占位符",
            ),
        ],
    )
    def test_placeholder_tokens_are_produced(self, sandbox_output: str, expected_signature: str) -> None:
        """易变片段要折成**具名占位符**，而不是被裸数字规则拆成一串 `<N>`

        钉逐字全等而不是 `"<TS>" in sig`：后者在「时间戳规则没跑、但正文里本来就有 `<TS>`」
        这种情况下会假绿。
        """
        assert normalize_error_signature(ExecStopType.ERROR, sandbox_output) == expected_signature

    @pytest.mark.parametrize(
        ("sandbox_output", "expected_signature"),
        [
            pytest.param(
                "ValueError:   多个   空格   分隔",
                "ERROR|ValueError: 多个 空格 分隔",
                id="连续空格折成一个",
            ),
            pytest.param(
                "ValueError:\t制表符\t分隔",
                "ERROR|ValueError: 制表符 分隔",
                id="制表符折成空格",
            ),
        ],
    )
    def test_whitespace_is_collapsed(self, sandbox_output: str, expected_signature: str) -> None:
        """空白折叠：同一条错误在不同轮里缩进/对齐不同也要判成同一个签名

        它同时是签名进日志时的可读性契约——签名整行会被打进 ERROR 日志。
        """
        assert normalize_error_signature(ExecStopType.ERROR, sandbox_output) == expected_signature

    def test_whitespace_variants_fold_together(self) -> None:
        """折叠的行为后果：只差空白的两次收尾同签名"""
        assert normalize_error_signature(ExecStopType.ERROR, "ValueError:   a   b") == normalize_error_signature(
            ExecStopType.ERROR,
            "ValueError:\ta\tb",
        )


class TestThoughtChainTagCleanup:
    """思维链里的嵌套 `<think>` / `</think>` 标签要被清掉

    这是上游既有行为（本轮没改），但 632 条定向用例里没有一条走过「思维链正文里还夹着
    标签」的形态——把那行 `re.sub` 删掉全绿。思维链会进提示词上下文与错误日志，
    残留标签会让下一轮的模型看到半截标签结构。
    """

    @pytest.mark.parametrize(
        ("raw", "expected_chain"),
        [
            pytest.param(
                # `<think>(.*?)</think>` 是非贪婪的，捕获到**第一个** `</think>` 为止，
                # 于是组里是 `先想一下<think>再想一层` —— 残留的那个开标签正是这行 re.sub 要清掉的
                "<think>先想一下<think>再想一层</think>然后动手</think>\n```python\nprint(1)\n```",
                "先想一下再想一层",
                id="嵌套开标签被清掉",
            ),
            pytest.param(
                "<think>前半段</think>后半段</think>\n```python\nprint(1)\n```",
                "前半段",
                id="只取第一段思维链",
            ),
        ],
    )
    def test_nested_tags_are_stripped(self, raw: str, expected_chain: str) -> None:
        parsed = parse_chat_response(raw)
        assert parsed.thought_chain == expected_chain
        assert "<think>" not in parsed.thought_chain
        assert "</think>" not in parsed.thought_chain
        assert parsed.code_content == "print(1)", "清标签不该影响代码提取"


class TestProvenEquivalentMutants:
    """N17 / N18：这两个变异**不应该**被造出用例来杀，理由各自不同

    * **N17**（`stalled` 去掉 `self._seen and` 保护）：等价变异。`threshold` 在构造函数里被
      `max(2, ...)` 锁了下限，而 `repeats` 初值是 0，所以「没记过任何一次」时
      `repeats >= threshold` 恒为 False —— `_seen` 这一项永远改变不了结果。
      编不出反例，只能把它成立的**前提**钉住。
    * **N18**（`is_code_missing` 的 `getattr` 默认值改成 True）：不是等价变异，是**死代码**。
      默认值只在 `ParsedCodeRunData` 还没有 `code_missing` 字段时才取得到，而字段已经合进来了。
      正确处置不是编一条用例去杀它，而是**把死分支删掉**（已改为直接取属性），
      让这个变异点不复存在。这里钉住「字段是声明字段」这个前提。
    """

    @pytest.mark.parametrize("requested_threshold", [-5, 0, 1, 2, 3, 10])
    def test_stall_threshold_floor_makes_seen_guard_unreachable(self, requested_threshold: int) -> None:
        detector = RoundStallDetector(threshold=requested_threshold)
        assert detector.repeats == 0
        assert detector.stalled is False
        if detector.enabled:
            assert detector.threshold >= 2, "阈值下限被破坏，`_seen` 保护就不再是冗余的"
            # 前提：没记过任何一次时，去掉 `_seen` 也判不出无进展
            assert (detector.repeats >= detector.threshold) is False
        else:
            # 关闭态（`SESSION_STALL_REPEAT_THRESHOLD <= 0`，运维的止血开关）下 `threshold` 是 0，
            # `repeats >= threshold` 反过来恒**真** —— 此时把 N17 挡住的是 `enabled` 而不是 `_seen`，
            # 等价性的前提换了一条，所以这里分开钉，免得「关闭态」悄悄变成「第一次报错就放弃」
            assert detector.threshold == 0
            assert (detector.repeats >= detector.threshold) is True

    def test_code_missing_is_a_declared_field(self) -> None:
        """`code_missing` 是声明字段 —— 所以 `is_code_missing` 里不需要任何默认值兜底"""
        assert "code_missing" in ParsedCodeRunData.model_fields
        assert ParsedCodeRunData(raw_content="x", code_content="", thought_chain="").code_missing is False
