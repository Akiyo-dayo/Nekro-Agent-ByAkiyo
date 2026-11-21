"""自动注入前的记忆筛选

相似度阈值调到头之后，注入的条目里仍然只有一部分对题。这里换成让一个便宜的模型看
「最近几句对话 + 候选」，只留对眼下话题有帮助的。用例钉住：

- 提示词的形状（眼下这句带标记、候选从 1 编号、素材被压平截断并替换尖括号）；
- 回复只接受一个整体合规的 JSON 数组；
- 模型组留空或域记忆闸关时整条新路径不走；
- 模型留下的条目才注入、返回空就记 empty、失败按默认阈值兜底；
- 低于默认注入下限但过了检索准入的候选能进候选池；
- 同一轮增量渲染不重复问已经判过的候选；
- 被冷却的候选不进候选池；
- 字符预算没放进去的条目不进冷却、不参与轮末强化。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, List

import pytest

from nekro_agent.core.config import ModelConfigGroup, config
from nekro_agent.services.agent.templates import history as history_module
from nekro_agent.services.memory import instance_scope as instance_scope_module
from nekro_agent.services.memory import prompt_rounds, round_recall
from nekro_agent.services.memory import recall_cooldown as cooldown_module
from nekro_agent.services.memory import recall_judge as judge_module
from nekro_agent.services.memory import retriever as retriever_module
from nekro_agent.services.memory.instance_scope import MemoryScope
from nekro_agent.services.memory.recall_contract import MemoryRecallPlan, MemoryRecallQuerySpec
from nekro_agent.services.memory.recall_cooldown import mark_injected, reset_recall_cooldown
from nekro_agent.services.memory.recall_judge import (
    JUDGE_POOL,
    JUDGE_TEXT_CHARS,
    build_judge_prompt,
    judge_candidates,
    parse_judge_reply,
)
from nekro_agent.services.memory.retriever import RetrievedMemory
from nekro_agent.services.memory.round_recall import injected_keys, reset_round_recalls
from tests.test_memory_recall_cooldown import _channel, _memory

CHAT_KEY = "onebot_v11-cooldown-group_1"
PRESET_ID = 7
CODE = "judge-code"


@pytest.fixture(autouse=True)
def clean_process_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    reset_recall_cooldown()
    reset_round_recalls()
    prompt_rounds.reset_prompt_rounds()
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_ENABLE_ENHANCED_RETRIEVAL", False)
    monkeypatch.setattr(config, "MEMORY_RECALL_COOLDOWN_BYPASS_SIMILARITY", 0.8)
    monkeypatch.setattr(config, "MEMORY_RECALL_JUDGE_MODEL_GROUP", "")

    async def fake_scope(_channel: Any) -> MemoryScope:
        return MemoryScope(allowed_instance_keys=None, preset_id=PRESET_ID)

    monkeypatch.setattr(instance_scope_module, "resolve_memory_scope", fake_scope)
    try:
        yield
    finally:
        reset_recall_cooldown()
        reset_round_recalls()
        prompt_rounds.reset_prompt_rounds()


@pytest.fixture
def warning_logs() -> Any:
    """收集 WARNING 及以上的日志正文（产品代码用 loguru，不经过 stdlib logging）。"""
    from loguru import logger

    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(message.record["message"]), level="WARNING")
    try:
        yield captured
    finally:
        logger.remove(sink_id)


def _plan(focus: str = "眼下这句", contexts: List[str] | None = None) -> MemoryRecallPlan:
    return MemoryRecallPlan(
        queries=[
            MemoryRecallQuerySpec(
                query_text=focus,
                focus_text=focus,
                context_texts=list(contexts or []),
            ),
        ],
    )


def _install_retrieval(
    monkeypatch: pytest.MonkeyPatch,
    batches: List[List[RetrievedMemory]],
    *,
    focus: str = "眼下这句",
    contexts: List[str] | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {"compiled": [], "limits": []}
    cursor = {"index": 0}

    async def fake_plan(_messages: List[Any]) -> MemoryRecallPlan:
        return _plan(focus, contexts)

    async def fake_retrieve(
        workspace_id: int,
        requests: List[Any],
        **_kwargs: Any,
    ) -> List[List[RetrievedMemory]]:
        del workspace_id
        state["limits"] = [request.limit for request in requests]
        index = min(cursor["index"], len(batches) - 1)
        cursor["index"] += 1
        return [list(batches[index]) for _ in requests]

    async def fake_compile(
        *,
        recall_query: Any,
        memories: List[RetrievedMemory],
        max_length: int,
        included: Any = None,
    ) -> str:
        del recall_query, max_length
        state["compiled"].append([memory.target_id for memory in memories])
        if included is not None:
            included.extend((memory.source_type, memory.target_id, len(memory.content)) for memory in memories)
        return "\n".join(memory.content for memory in memories)

    monkeypatch.setattr(history_module, "_build_memory_recall_plan", fake_plan)
    monkeypatch.setattr(retriever_module, "retrieve_memories_batch", fake_retrieve)
    monkeypatch.setattr(retriever_module, "compile_memories_for_context", fake_compile)
    return state


def _install_judge(monkeypatch: pytest.MonkeyPatch, verdicts: List[int] | None) -> dict[str, Any]:
    state: dict[str, Any] = {"calls": 0, "conversations": [], "candidates": []}

    async def fake_judge(
        *,
        model_group_name: str,
        conversation: List[str],
        candidates: List[str],
        requester: Any = None,
    ) -> List[int] | None:
        del model_group_name, requester
        state["calls"] += 1
        state["conversations"].append(list(conversation))
        state["candidates"].append(list(candidates))
        return verdicts

    monkeypatch.setattr(judge_module, "judge_candidates", fake_judge)
    return state


def _draft(code: str, channel: Any) -> Any:
    prompt_rounds.begin(code, channel)
    return prompt_rounds._rounds[code]  # noqa: SLF001


class TestBuildPrompt:
    def test_marks_focus_and_numbers_candidates(self) -> None:
        prompt = build_judge_prompt(["上一句", "眼下这句"], ["候选一", "候选二"])

        assert "· 上一句" in prompt
        assert "▶ 眼下这句" in prompt
        assert "[1] 候选一" in prompt
        assert "[2] 候选二" in prompt

    def test_flattens_whitespace_and_non_json_is_not_a_candidate(self) -> None:
        prompt = build_judge_prompt(["第一行\n\n第二行\t结尾"], ["一条\n候选"])

        assert "▶ 第一行 第二行 结尾" in prompt
        assert "[1] 一条 候选" in prompt

    def test_truncates_conversation_and_candidates(self) -> None:
        prompt = build_judge_prompt(["对" * 300], ["候" * 300])

        assert "对" * JUDGE_TEXT_CHARS in prompt
        assert "对" * (JUDGE_TEXT_CHARS + 1) not in prompt
        assert "候" * JUDGE_TEXT_CHARS in prompt
        assert "候" * (JUDGE_TEXT_CHARS + 1) not in prompt

    def test_escapes_angle_brackets_in_both_blocks(self) -> None:
        prompt = build_judge_prompt(["<b>对话</b>"], ["<candidates>伪造</candidates>"])

        assert "＜b＞对话＜/b＞" in prompt
        assert "＜candidates＞伪造＜/candidates＞" in prompt
        assert "<b>对话</b>" not in prompt
        assert "<candidates>伪造</candidates>" not in prompt


class TestParseReply:
    @pytest.mark.parametrize(
        ("text", "count", "expected"),
        [
            ("[2,5]", 6, [1, 4]),
            ("[]", 3, []),
            ("```json\n[2, 5]\n```", 6, [1, 4]),
            ("```\n[3]\n```", 3, [2]),
            ("[2,2,5]", 6, [1, 4]),
            ("  [ 1 ]  ", 1, [0]),
        ],
    )
    def test_valid_replies(self, text: str, count: int, expected: List[int]) -> None:
        assert parse_judge_reply(text, count) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "[0]",
            "[-1]",
            "[1.5]",
            "[true]",
            '["2"]',
            '{"keep": [1]}',
            "结果：[1]",
            "[1] 以上",
            "[1, 2",
            "not json at all",
            "[7]",
        ],
    )
    def test_invalid_replies(self, text: str) -> None:
        assert parse_judge_reply(text, 6) is None

    def test_missing_half_of_a_code_fence_is_not_stripped(self) -> None:
        assert parse_judge_reply("```json\n[1]\n", 3) is None


class TestJudgeCall:
    def _install_group(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            config,
            "MODEL_GROUPS",
            {"judge": ModelConfigGroup(CHAT_MODEL="m", BASE_URL="http://x", API_KEY="k")},
        )

    async def _call(self) -> Any:
        return await judge_candidates(
            model_group_name="judge",
            conversation=["最近一句"],
            candidates=["候选一", "候选二"],
            requester=("onebot_v11", "inst"),
        )

    async def test_timeout_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
        warning_logs: List[str],
    ) -> None:
        import nekro_agent.services.agent.openai as openai_module

        self._install_group(monkeypatch)
        monkeypatch.setattr(judge_module, "JUDGE_TIMEOUT_SECONDS", 0.05)

        async def hang(**_kwargs: Any) -> Any:
            await asyncio.Event().wait()

        monkeypatch.setattr(openai_module, "gen_openai_chat_response", hang)

        assert await asyncio.wait_for(self._call(), timeout=2.0) is None
        assert warning_logs == ["记忆筛选失败，按相似度取: reason=超时"]

    async def test_exception_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
        warning_logs: List[str],
    ) -> None:
        import nekro_agent.services.agent.openai as openai_module

        self._install_group(monkeypatch)

        async def explode(**_kwargs: Any) -> Any:
            raise RuntimeError("上游挂了")

        monkeypatch.setattr(openai_module, "gen_openai_chat_response", explode)

        assert await self._call() is None
        assert len(warning_logs) == 1
        assert "记忆筛选失败，按相似度取" in warning_logs[0]
        assert "上游挂了" in warning_logs[0]
        assert "候选一" not in warning_logs[0] and "最近一句" not in warning_logs[0]

    async def test_unparseable_reply_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
        warning_logs: List[str],
    ) -> None:
        import nekro_agent.services.agent.openai as openai_module

        self._install_group(monkeypatch)

        async def reply(**_kwargs: Any) -> Any:
            return SimpleNamespace(response_content="我觉得第二条不错")

        monkeypatch.setattr(openai_module, "gen_openai_chat_response", reply)

        assert await self._call() is None
        assert warning_logs == ["记忆筛选失败，按相似度取: reason=解析失败"]

    async def test_missing_model_group_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
        warning_logs: List[str],
    ) -> None:
        monkeypatch.setattr(config, "MODEL_GROUPS", {})

        assert await self._call() is None
        assert len(warning_logs) == 1
        assert "记忆筛选失败，按相似度取" in warning_logs[0]
        assert "judge" in warning_logs[0]

    async def test_valid_reply_returns_zero_based_indices(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import nekro_agent.services.agent.openai as openai_module

        self._install_group(monkeypatch)

        async def reply(**_kwargs: Any) -> Any:
            return SimpleNamespace(response_content="[2]")

        monkeypatch.setattr(openai_module, "gen_openai_chat_response", reply)

        assert await self._call() == [1]


class TestInjection:
    async def test_empty_model_group_never_calls_the_judge(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = _install_retrieval(monkeypatch, [[_memory(1, similarity=0.9), _memory(2, similarity=0.8)]])
        calls = _install_judge(monkeypatch, None)

        rendered = await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=None,
            recent_messages=[],
            channel=_channel(cooldown_rounds=0),
            one_time_code=CODE,
        )

        assert calls["calls"] == 0
        assert "记忆内容 1" in rendered and "记忆内容 2" in rendered
        assert state["compiled"] == [[1, 2]]

    async def test_keeps_only_the_candidates_the_model_left(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_RECALL_JUDGE_MODEL_GROUP", "judge")
        channel = _channel(cooldown_rounds=0)
        state = _install_retrieval(
            monkeypatch,
            [[_memory(1, similarity=0.9), _memory(2, similarity=0.8), _memory(3, similarity=0.7)]],
            contexts=["上一句", "眼下这句"],
        )
        calls = _install_judge(monkeypatch, [0, 1])
        draft = _draft(CODE, channel)

        rendered = await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=None,
            recent_messages=[],
            channel=channel,
            one_time_code=CODE,
        )

        assert "记忆内容 1" in rendered and "记忆内容 2" in rendered
        assert "记忆内容 3" not in rendered
        assert state["compiled"] == [[1, 2]], "按相似度顺序，只留模型留下的两条"
        assert calls["candidates"] == [["记忆内容 1", "记忆内容 2", "记忆内容 3"]]
        assert calls["conversations"][0][-1] == "眼下这句", "眼下这句必须在最后"
        assert draft.memory_judge == {"mode": "model", "pool": 3, "kept": 2}

    async def test_empty_verdicts_leave_nothing_to_inject(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_RECALL_JUDGE_MODEL_GROUP", "judge")
        channel = _channel(cooldown_rounds=0)
        state = _install_retrieval(monkeypatch, [[_memory(1, similarity=0.9), _memory(2, similarity=0.8)]])
        _install_judge(monkeypatch, [])
        draft = _draft(CODE, channel)

        rendered = await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=None,
            recent_messages=[],
            channel=channel,
            one_time_code=CODE,
        )

        assert rendered == ""
        assert state["compiled"] == []
        assert draft.memory_state == "empty"
        assert draft.memory_judge == {"mode": "model", "pool": 2, "kept": 0}

    async def test_model_failure_falls_back_to_default_similarity(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_RECALL_JUDGE_MODEL_GROUP", "judge")
        channel = _channel(cooldown_rounds=0)
        state = _install_retrieval(
            monkeypatch,
            [[_memory(1, similarity=0.9), _memory(2, similarity=0.85), _memory(3, similarity=0.52)]],
        )
        calls = _install_judge(monkeypatch, None)
        draft = _draft(CODE, channel)

        rendered = await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=None,
            recent_messages=[],
            channel=channel,
            one_time_code=CODE,
        )

        assert calls["calls"] == 1
        assert "记忆内容 1" in rendered and "记忆内容 2" in rendered
        assert "记忆内容 3" not in rendered, "兜底走默认阈值，0.52 被 0.55 挡住"
        assert state["compiled"] == [[1, 2]]
        assert draft.memory_judge == {"mode": "fallback", "pool": 3, "kept": 2}
        assert round_recall.judged_verdicts(CODE) == {}

    async def test_low_similarity_candidate_enters_the_pool_only_when_judging(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        channel = _channel(cooldown_rounds=0)
        memories = [_memory(1, similarity=0.9), _memory(2, similarity=0.52)]

        monkeypatch.setattr(config, "MEMORY_RECALL_JUDGE_MODEL_GROUP", "judge")
        state = _install_retrieval(monkeypatch, [memories])
        calls = _install_judge(monkeypatch, [1])
        rendered = await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=None,
            recent_messages=[],
            channel=channel,
            one_time_code="judge-pool-on",
        )
        assert calls["candidates"] == [["记忆内容 1", "记忆内容 2"]]
        assert state["compiled"] == [[2]]
        assert "记忆内容 2" in rendered

        monkeypatch.setattr(config, "MEMORY_RECALL_JUDGE_MODEL_GROUP", "")
        state = _install_retrieval(monkeypatch, [memories])
        calls = _install_judge(monkeypatch, [1])
        rendered = await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=None,
            recent_messages=[],
            channel=channel,
            one_time_code="judge-pool-off",
        )
        assert calls["calls"] == 0
        assert state["compiled"] == [[1]]
        assert "记忆内容 2" not in rendered

    async def test_incremental_render_does_not_ask_twice(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_RECALL_JUDGE_MODEL_GROUP", "judge")
        channel = _channel(cooldown_rounds=0)
        first = [_memory(1, similarity=0.9), _memory(2, similarity=0.8)]
        second = [*first, _memory(3, similarity=0.7)]
        _install_retrieval(monkeypatch, [first, second, second])
        calls = _install_judge(monkeypatch, [0])

        await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=None,
            recent_messages=[],
            channel=channel,
            one_time_code=CODE,
        )
        assert calls["calls"] == 1

        await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=None,
            recent_messages=[],
            channel=channel,
            one_time_code=CODE,
            exclude_keys=injected_keys(CODE),
        )
        assert calls["calls"] == 2
        assert calls["candidates"][-1] == ["记忆内容 3"], "已经判过的 2 不再发给模型"

        rendered = await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=None,
            recent_messages=[],
            channel=channel,
            one_time_code=CODE,
            exclude_keys=injected_keys(CODE),
        )
        assert rendered == ""
        assert calls["calls"] == 2, "全都有结论时不再调模型"

    async def test_cooled_candidate_does_not_enter_the_pool(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_RECALL_JUDGE_MODEL_GROUP", "judge")
        channel = _channel(cooldown_rounds=3)
        cooled = _memory(1, similarity=0.6)
        fresh = _memory(2, similarity=0.7)
        mark_injected((CHAT_KEY, PRESET_ID), "judge-cooled-0", [cooled])
        state = _install_retrieval(monkeypatch, [[cooled, fresh]])
        calls = _install_judge(monkeypatch, [0])

        rendered = await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=None,
            recent_messages=[],
            channel=channel,
            one_time_code="judge-cooled-1",
        )

        assert calls["candidates"] == [["记忆内容 2"]]
        assert state["compiled"] == [[2]]
        assert "记忆内容 1" not in rendered

    async def test_budget_keeps_only_the_really_rendered_items(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        channel = _channel(cooldown_rounds=3)
        _install_retrieval(monkeypatch, [[_memory(1, similarity=0.9), _memory(2, similarity=0.8)]])

        async def compile_one(
            *,
            recall_query: Any,
            memories: List[RetrievedMemory],
            max_length: int,
            included: Any = None,
        ) -> str:
            del recall_query, max_length
            first = memories[0]
            if included is not None:
                included.append((first.source_type, first.target_id, len(first.content)))
            return first.content

        monkeypatch.setattr(retriever_module, "compile_memories_for_context", compile_one)

        rendered = await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=None,
            recent_messages=[],
            channel=channel,
            one_time_code=CODE,
        )

        assert "记忆内容 1" in rendered
        staged = round_recall._round_recalls[CODE]  # noqa: SLF001
        assert [memory.target_id for memory in staged.memories] == [1]
        injected = cooldown_module._cooldowns[(CHAT_KEY, PRESET_ID)].injected  # noqa: SLF001
        assert set(injected) == {("paragraph", 1)}

    async def test_gate_off_never_calls_the_judge(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
        monkeypatch.setattr(config, "MEMORY_RECALL_JUDGE_MODEL_GROUP", "judge")
        _install_retrieval(monkeypatch, [[_memory(1, similarity=0.9)]])
        calls = _install_judge(monkeypatch, [0])

        rendered = await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=9401,
            recent_messages=[],
            channel=_channel(cooldown_rounds=0),
            one_time_code=CODE,
        )

        assert "记忆内容 1" in rendered
        assert calls["calls"] == 0


class TestDetails:
    def test_judge_result_is_written_to_the_existing_json_column(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, Any] = {}

        class _FakeQuery:
            def order_by(self, *_args: Any, **_kwargs: Any) -> "_FakeQuery":
                return self

            def offset(self, *_args: Any, **_kwargs: Any) -> "_FakeQuery":
                return self

            def limit(self, *_args: Any, **_kwargs: Any) -> "_FakeQuery":
                return self

            async def values_list(self, *_args: Any, **_kwargs: Any) -> List[int]:
                return []

        class _FakeRound:
            @classmethod
            def filter(cls, **_kwargs: Any) -> _FakeQuery:
                return _FakeQuery()

            @staticmethod
            async def create(**kwargs: Any) -> None:
                captured.update(kwargs)

        monkeypatch.setattr(prompt_rounds, "DBMemPromptRound", _FakeRound)
        channel = _channel(cooldown_rounds=0)
        prompt_rounds.begin(CODE, channel)
        draft = prompt_rounds._rounds[CODE]  # noqa: SLF001
        draft.preset_id = PRESET_ID
        prompt_rounds.note_memory(
            CODE,
            state="injected",
            judge={"mode": "model", "pool": 9, "kept": 0},
        )

        async def run() -> None:
            await prompt_rounds._write_round(draft, "ok")  # noqa: SLF001

        asyncio.run(run())

        assert captured["tavern_items"]["memory_judge"] == {"mode": "model", "pool": 9, "kept": 0}
        prompt_rounds._rounds.pop(CODE, None)  # noqa: SLF001

    def test_incremental_render_does_not_overwrite_the_first_judge_result(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        channel = _channel(cooldown_rounds=0)
        prompt_rounds.begin(CODE, channel)
        prompt_rounds.note_memory(
            CODE,
            state="injected",
            judge={"mode": "model", "pool": 4, "kept": 2},
        )
        prompt_rounds.note_memory(
            CODE,
            state="injected",
            judge={"mode": "fallback", "pool": 1, "kept": 1},
        )
        draft = prompt_rounds._rounds[CODE]  # noqa: SLF001
        assert draft.memory_judge == {"mode": "model", "pool": 4, "kept": 2}
        prompt_rounds._rounds.pop(CODE, None)  # noqa: SLF001


def test_judge_pool_is_the_documented_limit() -> None:
    assert JUDGE_POOL == 16
