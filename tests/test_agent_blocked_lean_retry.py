"""首次请求被上游按内容拦下：同一轮里少带内容重发一次

这里钉住的是「重发」这一层：两种拦截形态（空正文 + `cont_blocked` 的
`AllLLMRequestsFailedError`、拒绝页被当成正文的供应商拒答）在**首次请求**上都要先试一次
少带内容的重发；重发成功就照常继续这一轮，重发仍被拦才照现状收尾。

「少带」不是另写一套开关：先记「被拦」标记，让好感块、插件不可信注入、记忆块、频道摘要、
世界书不可信条目与变量、世界书查询工具按现有机制自动跳过；再丢掉第一次缓存 / 暂存的全量
结果；最后用 `build_round_prompt(lean_history=n)` 重建提示词。历史消息只带最后 n 条，世界书
只留常驻条目且不做语义检索，不带预填。

全部确定性：无网络、无 sleep、无随机。
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List, Tuple

import pytest

# 复用兄弟用例已经搭好的可注入环境，不再抄一份替身（项目 §0.5）。
# `tests/` 下没有 `__init__.py`，pytest 的 prepend 导入模式会把该目录放进 sys.path。
from test_agent_mutation_survivors import _MessageServiceHarness
from test_agent_refusal_round_exit import NORMAL_CODE_RESPONSE, REAL_PROVIDER_REFUSAL
from test_agent_round_control import CHAT_KEY as HARNESS_CHAT_KEY
from test_agent_round_control import _Harness, _SandboxResult
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_exec_code import ExecStopType
from nekro_agent.models.db_mem_paragraph import CognitiveType, DBMemParagraph, KnowledgeType, OriginKind
from nekro_agent.models.db_mem_prompt_round import DBMemPromptRound
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.schemas.chat_message import ChatMessageSegmentImage, ChatMessageSegmentType
from nekro_agent.services.affection.suppression import (
    is_narrative_suppressed,
    reset_narrative_suppression,
)
from nekro_agent.services.agent import run_agent as run_agent_module
from nekro_agent.services.agent.failure_feedback import (
    NOTICE_ROUND_PROVIDER_REFUSAL,
    failure_notice_throttle,
)
from nekro_agent.services.agent.model_capability import LLMFailureKind
from nekro_agent.services.agent.resolver import parse_chat_response
from nekro_agent.services.agent.run_agent import (
    AgentProviderRefusalError,
    AllLLMRequestsFailedError,
)
from nekro_agent.services.agent.templates import history as history_module
from nekro_agent.services.memory import prompt_rounds
from nekro_agent.services.memory.round_recall import reset_round_recalls
from nekro_agent.services.worldbook import runtime as wb_runtime
from nekro_agent.services.worldbook import tokens as wb_tokens
from nekro_agent.services.worldbook import vectors as wb_vectors
from nekro_agent.services.worldbook.budget import resolve_budget
from nekro_agent.services.worldbook.types import EntryStatus, WindowMessage

ADAPTER = "onebot_v11"
INSTANCE = "lean"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_1"


@pytest.fixture(autouse=True)
def _clean_process_state() -> Any:
    """回执冷却、叙事抑制与召回记录都是进程内单例，用例之间互相影响"""
    failure_notice_throttle.reset()
    reset_narrative_suppression()
    reset_round_recalls()
    wb_runtime._rounds.clear()  # noqa: SLF001
    try:
        yield
    finally:
        failure_notice_throttle.reset()
        reset_narrative_suppression()
        reset_round_recalls()
        wb_runtime._rounds.clear()  # noqa: SLF001


def _message_blob(messages: List[Any]) -> str:
    return json.dumps([message.to_dict() for message in messages], ensure_ascii=False)


def _blocked_error() -> AllLLMRequestsFailedError:
    return AllLLMRequestsFailedError(
        "所有 LLM 请求失败",
        kinds=[LLMFailureKind.CONTENT_BLOCKED],
    )


async def _drive(
    monkeypatch: pytest.MonkeyPatch,
    responses: List[Any],
    *,
    max_retry: int = 3,
    sandbox_results: List[_SandboxResult] | None = None,
    retry_messages: int = 6,
) -> Tuple[_Harness, List[str]]:
    """驱动**真** `run_agent`，按脚本喂正文或异常，返回 (harness, 每次请求的上下文快照)"""
    harness = _Harness(
        max_retry=max_retry,
        parses=[],
        sandbox_results=list(sandbox_results or []),
    )
    harness.install(monkeypatch)
    # 假配置显式设上这一项：不设时按 0 处理，测不到重发。
    effective_config = await harness.ctx.db_chat_channel.get_effective_config()
    effective_config.AI_CHAT_BLOCKED_RETRY_MESSAGES = retry_messages

    contexts: List[str] = []

    async def _send(**kwargs: Any) -> Tuple[Any, Any, List[str]]:
        index = harness.llm_calls
        if index >= len(responses):
            raise AssertionError(f"LLM 脚本只有 {len(responses)} 条，却被取用了第 {index + 1} 次")
        contexts.append(_message_blob(kwargs["messages"]))
        harness.llm_calls += 1
        item = responses[index]
        if isinstance(item, BaseException):
            raise item
        return (
            SimpleNamespace(
                response_content=item,
                use_model="lean-model",
                thought_chain="",
            ),
            SimpleNamespace(CHAT_MODEL="lean-model"),
            [],
        )

    monkeypatch.setattr(run_agent_module, "send_agent_request", _send)
    monkeypatch.setattr(run_agent_module, "parse_chat_response", parse_chat_response)
    return harness, contexts


def _all_logs(harness: _Harness) -> str:
    return "\n".join(record.text for record in harness.logger.records)


# ======================================================================================
# 1 / 2. 两种形态的首次拦截都会先少带内容重发一次，成功就继续这一轮
# ======================================================================================


class TestFirstBlockedRequestIsResentLean:
    async def test_provider_refusal_then_normal_code_finishes_the_round(self, monkeypatch) -> None:
        harness, contexts = await _drive(
            monkeypatch,
            [REAL_PROVIDER_REFUSAL, NORMAL_CODE_RESPONSE],
            sandbox_results=[_SandboxResult(output="", stop_type=ExecStopType.NORMAL)],
        )

        await harness.run()

        assert harness.llm_calls == 2, "首次供应商拒答之后没有少带内容重发"
        assert harness.sandbox_calls == 1, "重发成功之后这一轮没有照常继续"
        assert "could not be submitted" not in contexts[1].lower(), "拒答原文进了重发请求的上下文"
        assert "policies.google.com" not in contexts[1].lower(), "供应商链接进了重发请求的上下文"
        assert is_narrative_suppressed(HARNESS_CHAT_KEY, 3600) is False, "这一轮最终成功，抑制标记应该已清掉"
        assert "少带内容重发" in _all_logs(harness), "重发没有留下可诊断的 INFO 日志"

    async def test_content_blocked_failure_then_normal_code_finishes_the_round(self, monkeypatch) -> None:
        harness, contexts = await _drive(
            monkeypatch,
            [_blocked_error(), NORMAL_CODE_RESPONSE],
            sandbox_results=[_SandboxResult(output="", stop_type=ExecStopType.NORMAL)],
        )

        await harness.run()

        assert harness.llm_calls == 2, "首次 CONTENT_BLOCKED 之后没有少带内容重发"
        assert harness.sandbox_calls == 1
        assert is_narrative_suppressed(HARNESS_CHAT_KEY, 3600) is False
        assert "content_blocked" in _all_logs(harness)


# ======================================================================================
# 3 / 4. 重发仍被拦 / 配置为 0：收尾与现状一致
# ======================================================================================


class TestBlockedAgainOrDisabledKeepsCurrentBehaviour:
    async def test_provider_refusal_twice_raises_and_notifies_as_before(self, monkeypatch) -> None:
        harness, _ = await _drive(
            monkeypatch,
            [REAL_PROVIDER_REFUSAL, REAL_PROVIDER_REFUSAL],
        )

        with pytest.raises(AgentProviderRefusalError) as excinfo:
            await harness.run()

        assert harness.llm_calls == 2, "每轮最多重发一次：两次都被拦时不该再发第三次"
        assert is_narrative_suppressed(HARNESS_CHAT_KEY, 3600) is True
        raised = excinfo.value

        monkeypatch.undo()
        service_harness = _MessageServiceHarness()
        service_harness.install(monkeypatch, raises=raised)
        await service_harness.run(chat_key=HARNESS_CHAT_KEY)

        assert [text for _chat_key, text in service_harness.sent] == [NOTICE_ROUND_PROVIDER_REFUSAL], (
            "重发仍被拦时的回执文案变了"
        )

    async def test_zero_disables_the_retry_for_provider_refusal(self, monkeypatch) -> None:
        harness, _ = await _drive(
            monkeypatch,
            [REAL_PROVIDER_REFUSAL],
            retry_messages=0,
        )

        with pytest.raises(AgentProviderRefusalError):
            await harness.run()

        assert harness.llm_calls == 1, "配置为 0 时不该重发"
        assert "零重试提前收尾" in "\n".join(harness.logger.texts("ERROR"))
        assert "少带内容重发" not in _all_logs(harness)

    async def test_zero_disables_the_retry_for_content_blocked(self, monkeypatch) -> None:
        harness, _ = await _drive(
            monkeypatch,
            [_blocked_error()],
            retry_messages=0,
        )

        with pytest.raises(AllLLMRequestsFailedError):
            await harness.run()

        assert harness.llm_calls == 1, "配置为 0 时不该重发"
        assert "少带内容重发" not in _all_logs(harness)


# ======================================================================================
# 5. 迭代中的拦截不重发
# ======================================================================================


class TestIterationBlockIsNotResent:
    async def test_refusal_on_the_second_request_terminates_without_retry(self, monkeypatch) -> None:
        harness, _ = await _drive(
            monkeypatch,
            [NORMAL_CODE_RESPONSE, REAL_PROVIDER_REFUSAL],
            sandbox_results=[_SandboxResult(output="boom", stop_type=ExecStopType.ERROR)],
        )

        with pytest.raises(AgentProviderRefusalError):
            await harness.run()

        assert harness.llm_calls == 2, "迭代中的拦截被当成首次请求重发了"
        assert harness.sandbox_calls == 1
        assert "少带内容重发" not in _all_logs(harness)


# ======================================================================================
# 6. 重发时确实把「少带」参数传下去了
# ======================================================================================


class TestLeanArgumentsAreWired:
    async def test_build_and_resolve_receive_lean_parameters(self, monkeypatch) -> None:
        harness, _ = await _drive(
            monkeypatch,
            [REAL_PROVIDER_REFUSAL, NORMAL_CODE_RESPONSE],
            sandbox_results=[_SandboxResult(output="", stop_type=ExecStopType.NORMAL)],
        )

        build_calls: List[Dict[str, Any]] = []
        resolve_calls: List[Dict[str, Any]] = []
        real_build = run_agent_module.build_round_prompt
        real_resolve = wb_runtime.resolve_round

        async def spy_build(**kwargs: Any) -> Any:
            build_calls.append(dict(kwargs))
            return await real_build(**kwargs)

        async def spy_resolve(*args: Any, **kwargs: Any) -> Any:
            resolve_calls.append(dict(kwargs))
            return await real_resolve(*args, **kwargs)

        monkeypatch.setattr(run_agent_module, "build_round_prompt", spy_build)
        monkeypatch.setattr(wb_runtime, "resolve_round", spy_resolve)

        await harness.run()

        assert [call.get("lean_history", 0) for call in build_calls] == [0, 6], (
            "重发那次没有把 lean_history=6 交给 build_round_prompt"
        )
        assert [call.get("constant_only", False) for call in resolve_calls] == [False, True], (
            "重发那次没有让 resolve_round 只留常驻条目"
        )


# ======================================================================================
# 7. 历史只带最后 N 条，历史图片也跟着只剩这几条
# ======================================================================================


class _FakeExtData:
    ref_msg_id = ""


class _FakeHistoryMessage:
    """只够 `render_history_data` 走完流程的假消息行"""

    is_system = False
    ext_data_obj = _FakeExtData()

    def __init__(self, index: int) -> None:
        self.index = index
        self.message_id = f"m{index}"
        self._segments = [
            ChatMessageSegmentImage(
                type=ChatMessageSegmentType.IMAGE,
                text=f"[Image: pic-{index}.png]",
                file_name="",
                local_path="",
                remote_url=f"https://example.invalid/pic-{index}.png",
            ),
        ]

    def parse_content_data(self) -> List[Any]:
        return list(self._segments)

    def parse_chat_history_prompt(self, *_args: Any, **_kwargs: Any) -> str:
        return f"[message {self.index}]"


class _FakeHistoryChannel:
    chat_key = CHAT_KEY
    conversation_start_time = datetime(2000, 1, 1)
    workspace_id = None

    async def get_preset(self) -> Any:
        return None


class _FakeHistoryConfig:
    AI_CHAT_CONTEXT_EXPIRE_SECONDS = 3600
    AI_CHAT_CONTEXT_MAX_LENGTH = 10
    AI_SYSTEM_NOTIFY_WINDOW_SIZE = 0
    AI_SYSTEM_NOTIFY_LIMIT = 0
    AI_VISION_IMAGE_LIMIT = 10
    AI_VISION_IMAGE_SIZE_LIMIT_KB = 1024 * 1024
    MEMORY_ENABLE_ENHANCED_RETRIEVAL = False
    AI_ALWAYS_INCLUDE_MSG_ID = False
    AI_CONTEXT_LENGTH_PER_SESSION = 100000


def _history_text(message: Any) -> str:
    content = message.to_dict()["content"]
    if isinstance(content, str):
        return content
    return "".join(segment.get("text", "") for segment in content or [] if isinstance(segment, dict))


def _install_fake_history(
    monkeypatch: pytest.MonkeyPatch,
    *,
    message_count: int = 5,
) -> None:
    # 真实查询按 `-send_timestamp` 返回（新的在前），渲染层随后反转成时间正序。
    rows = [_FakeHistoryMessage(index) for index in reversed(range(message_count))]

    def fake_filter(*_args: Any, **_kwargs: Any) -> Any:
        class _QS:
            def order_by(self, *_a: Any, **_k: Any) -> "_QS":
                return self

            def limit(self, *_a: Any, **_k: Any) -> "_QS":
                return self

            async def __aiter__(self) -> Any:
                for row in rows:
                    yield row

            def __await__(self) -> Any:
                async def _inner() -> List[_FakeHistoryMessage]:
                    return rows

                return _inner().__await__()

        return _QS()

    async def fake_memory(**_kwargs: Any) -> str:
        return ""

    async def fake_summary(*_args: Any, **_kwargs: Any) -> str:
        return ""

    monkeypatch.setattr(history_module.DBChatMessage, "filter", staticmethod(fake_filter))
    monkeypatch.setattr(history_module, "_inject_memory_context", fake_memory)
    monkeypatch.setattr(history_module, "_inject_channel_summary", fake_summary)
    monkeypatch.setattr(history_module, "supports_vision", lambda *_args, **_kwargs: True)


_OMIT = object()


async def _render_history(monkeypatch: pytest.MonkeyPatch, *, max_messages: Any = _OMIT) -> str:
    _install_fake_history(monkeypatch)
    # 当前时间不冻结的话，两次渲染跨秒会比出「不传时逐字节不变」的假红。
    monkeypatch.setattr(history_module.time, "strftime", lambda *_args: "2020-01-01 00:00:00 UTC Wednesday")
    kwargs: Dict[str, Any] = {
        "chat_key": CHAT_KEY,
        "db_chat_channel": _FakeHistoryChannel(),
        "one_time_code": "lean-history-token",
        "config": _FakeHistoryConfig(),
        "model_group": SimpleNamespace(CHAT_MODEL="lean-model"),
    }
    if max_messages is not _OMIT:
        kwargs["max_messages"] = max_messages
    rendered = await history_module.render_history_data(**kwargs)
    return _history_text(rendered)


class TestHistoryWindow:
    async def test_max_messages_keeps_only_the_tail_and_its_images(self, monkeypatch) -> None:
        text = await _render_history(monkeypatch, max_messages=3)

        assert "[message 2]" in text
        assert "[message 3]" in text
        assert "[message 4]" in text
        assert "[message 0]" not in text
        assert "[message 1]" not in text
        assert "pic-2.png" in text
        assert "pic-3.png" in text
        assert "pic-4.png" in text
        assert "pic-0.png" not in text
        assert "pic-1.png" not in text

    async def test_not_passing_it_is_byte_identical(self, monkeypatch) -> None:
        with_none = await _render_history(monkeypatch, max_messages=None)
        without_argument = await _render_history(monkeypatch)
        assert with_none == without_argument


# ======================================================================================
# 8. 世界书：只留常驻条目、不跑语义检索
# ======================================================================================


@pytest.fixture
async def worldbook_db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[DBChatChannel]:
    monkeypatch.setattr(config, "MEMORY_INSTANCE_SHARING_GROUPS", {})
    monkeypatch.setattr(config, "AI_CHAT_CONTEXT_EXPIRE_SECONDS", 10**9)
    await wb_tokens.ensure_loaded_async()
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        display_name="实例",
        tenant_id=None,
        provider_account_id="bot-lean",
    )
    channel = await DBChatChannel.create(
        chat_key=CHAT_KEY,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id="group_1",
        channel_name="频道",
        channel_type="group",
        preset_id=0,
        data="{}",
        workspace_id=None,
    )
    try:
        yield channel
    finally:
        await Tortoise.close_connections()


async def _book(name: str = "书") -> DBWbBook:
    return await DBWbBook.create(
        name=name,
        description="",
        scope_kind="domain",
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        enabled=True,
        scan_depth=4,
        budget_tokens=0,
    )


async def _entry(
    book: DBWbBook,
    *,
    uid: int,
    content: str,
    constant: bool,
    keys: Tuple[str, ...] = (),
) -> DBWbEntry:
    return await DBWbEntry.create(
        book=book,
        uid=uid,
        comment="",
        keys=list(keys),
        secondary_keys=[],
        selective_logic="and_any",
        use_regex=False,
        constant=constant,
        content=content,
        order=100,
        position="before_history",
        depth=4,
        scan_depth=None,
        enabled=True,
        trusted=True,
        status=EntryStatus.ACTIVE.value,
        ext={},
    )


class TestWorldbookConstantOnly:
    async def test_keyword_entries_are_dropped_and_semantic_is_not_called(
        self,
        worldbook_db: DBChatChannel,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        book = await _book()
        constant = await _entry(book, uid=1, content="常驻正文", constant=True)
        keyword = await _entry(book, uid=2, content="关键词正文", constant=False, keys=("龙",))
        window = [WindowMessage(id=1, sender_id="u1", platform_userid="u1", content_text="龙")]
        budget = resolve_budget(SimpleNamespace(WORLDBOOK_BUDGET_PERCENT=100, WORLDBOOK_BUDGET_CAP_TOKENS=0))

        async def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("constant_only=True 仍然调用了语义检索")

        monkeypatch.setattr(wb_vectors, "semantic_hits", boom)

        lean = await wb_runtime.evaluate(
            worldbook_db,
            preset_id=0,
            window=window,
            budget=budget,
            max_entries=20,
            use_timed_state=False,
            suppress_untrusted=False,
            constant_only=True,
        )
        selected_ids = {hit.entry.id for hit in lean.chosen.selected}
        assert selected_ids == {constant.id}, "constant_only=True 时关键词命中的条目不该出现"

        normal = await wb_runtime.evaluate(
            worldbook_db,
            preset_id=0,
            window=window,
            budget=budget,
            max_entries=20,
            use_timed_state=False,
            suppress_untrusted=False,
            semantic=False,
        )
        normal_ids = {hit.entry.id for hit in normal.chosen.selected}
        assert normal_ids == {constant.id, keyword.id}, "不传 constant_only 时关键词条目应照常命中"


# ======================================================================================
# 9. 明细与撞甲计数：重发过的成功轮也记一次
# ======================================================================================


class _DraftChannel:
    chat_key = CHAT_KEY
    adapter_key = ADAPTER
    instance_key = INSTANCE

    async def get_effective_preset_id(self) -> int:
        return 1


@pytest.fixture
async def prompt_db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setattr(config, "MEMORY_PROMPT_ROUNDS_KEEP", 20)
    monkeypatch.setattr(config, "MEMORY_AUTO_QUARANTINE_BLOCKED_ROUNDS", 3)
    monkeypatch.setattr(config, "WORLDBOOK_AUTO_QUARANTINE_BLOCKED_ROUNDS", 2)
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    prompt_rounds.reset_prompt_rounds()
    reset_round_recalls()
    try:
        yield
    finally:
        await prompt_rounds.drain()
        prompt_rounds.reset_prompt_rounds()
        reset_round_recalls()
        await Tortoise.close_connections()


async def _paragraph() -> DBMemParagraph:
    return await DBMemParagraph.create(
        workspace_id=0,
        instance_key=INSTANCE,
        preset_id=0,
        adapter_key=ADAPTER,
        origin_chat_key=CHAT_KEY,
        memory_source="na",
        cognitive_type=CognitiveType.EPISODIC,
        knowledge_type=KnowledgeType.CONVERSATION,
        origin_kind=OriginKind.MESSAGE,
        content="可能触发拦截的段落",
        summary="摘要",
        is_protected=False,
    )


class TestLeanRetryDetails:
    async def test_successful_lean_round_writes_one_row_and_counts_once(
        self,
        prompt_db: None,
    ) -> None:
        paragraph = await _paragraph()
        book = await _book()
        entry = await _entry(book, uid=1, content="正文", constant=True)

        code = "lean-ok"
        prompt_rounds.begin(code, _DraftChannel())
        # 第一次全量渲染：记忆段落与世界书条目都进过提示词。
        prompt_rounds.note_memory(
            code,
            state="injected",
            items=[("paragraph", paragraph.id, 10)],
            block_chars=20,
        )
        prompt_rounds.note_worldbook(code, state="injected")
        prompt_rounds.note_worldbook_items(code, [(entry.id, book.id, 4, "before_history", True)])
        # 首次请求被拦、少带内容重发过。
        prompt_rounds.note_blocked_attempt(code)
        prompt_rounds.finish(code, "ok")
        await prompt_rounds.drain()

        assert await DBMemPromptRound.all().count() == 1, "重发成功的一轮没有落 1 行明细"
        row = await DBMemPromptRound.get()
        assert row.tavern_items.get("lean_retry") is True, "明细里没有 lean_retry 标记"

        stored_paragraph = await DBMemParagraph.get(id=paragraph.id)
        stored_entry = await DBWbEntry.get(id=entry.id)
        assert stored_paragraph.blocked_round_hits == 1, "第一次渲染里出现过的记忆没有记 1 次撞甲"
        assert stored_entry.blocked_round_hits == 1, "第一次渲染里出现过的世界书条目没有记 1 次撞甲"

        # 没重发的成功轮：同一条记忆与世界书条目再次注入，但不该再加撞甲。
        normal_code = "normal-ok"
        prompt_rounds.begin(normal_code, _DraftChannel())
        prompt_rounds.note_memory(
            normal_code,
            state="injected",
            items=[("paragraph", paragraph.id, 10)],
            block_chars=20,
        )
        prompt_rounds.note_worldbook(normal_code, state="injected")
        prompt_rounds.note_worldbook_items(normal_code, [(entry.id, book.id, 4, "before_history", True)])
        prompt_rounds.finish(normal_code, "ok")
        await prompt_rounds.drain()

        assert (await DBMemParagraph.get(id=paragraph.id)).blocked_round_hits == 1
        assert (await DBWbEntry.get(id=entry.id)).blocked_round_hits == 1
        normal_row = await DBMemPromptRound.all().order_by("-id").first()
        assert normal_row is not None and normal_row.tavern_items.get("lean_retry") is None
