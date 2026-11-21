"""世界书在提示词里的两处注入：系统提示词（人设之后）与历史消息

这里钉的是「位置」与「闸关时逐字不变」：

1. `worldbook_prompt` 为空时，系统提示词与不传这个参数时逐字相同——一个字节都不多；
2. 有内容时，它落在人设之后、好感块之前；
3. 历史消息里 `before_history` 一组落在 `Recent Messages:` 之前，`at_depth` 一组插进
   裁剪之后的聊天记录；
4. 没有解析过这一轮（缓存里没有）时，历史消息一个字都不加。
"""

from __future__ import annotations

from typing import Any

from nekro_agent.services.agent.templates.compiler import PromptCompiler
from nekro_agent.services.worldbook import runtime, selection
from nekro_agent.services.worldbook.types import MatchEntry, Position


def _compiler(**overrides: Any) -> PromptCompiler:
    kwargs: dict[str, Any] = {
        "platform_name": "qq",
        "bot_platform_id": "10001",
        "chat_preset": "你是薄荷，说话简洁。",
        "plugins_prompt": "",
        "plugins_runtime_prompt": "",
        "plugin_activation_rules": "",
        "plugin_call_priority_rules": "",
        "enable_cot": False,
        "chat_key_rules": "",
        "enable_at": False,
    }
    kwargs.update(overrides)
    return PromptCompiler(**kwargs)


def _content(compiler: PromptCompiler) -> str:
    return str(compiler.render_system_message().to_dict()["content"])


class TestSystemPromptSlot:
    """系统提示词里的那一槽"""

    def test_empty_slot_is_byte_identical(self) -> None:
        baseline = _content(_compiler())
        with_empty = _content(_compiler(worldbook_prompt=""))

        assert with_empty == baseline

    def test_slot_sits_after_persona_and_before_affection(self) -> None:
        content = _content(
            _compiler(
                worldbook_prompt="## World Info\n宿主正文",
                affection_prompt="## Affection\n好感正文",
            ),
        )

        assert content.index("你是薄荷") < content.index("宿主正文")
        assert content.index("宿主正文") < content.index("好感正文")

    def test_slot_is_omitted_when_affection_is_present(self) -> None:
        # 槽为空、好感非空：不能让空槽在好感块之前留下一个空行。
        baseline = _content(_compiler(affection_prompt="## Affection\n好感正文"))
        with_empty = _content(_compiler(affection_prompt="## Affection\n好感正文", worldbook_prompt=""))

        assert with_empty == baseline

    def test_empty_slot_matches_the_template_before_the_slot_existed(self) -> None:
        """槽为空时与加槽之前的模板逐字相同

        上面两条比的是「不传」与「传空串」，两边都由改过的模板渲染，模板本身多出一个空行时
        它们照样相等。这里把加槽之前的模板原文现场渲染一遍当基准。
        """
        from jinja2 import Environment

        from nekro_agent.services.agent.templates.base import env
        from nekro_agent.services.agent.templates.system import SystemPrompt

        before_slot = Environment().from_string(
            "{% macro system_prompt(stable_static, channel_static, runtime_dynamic, affection_prompt, plugins_prompt) %}\n"
            "{{ stable_static }}\n"
            "\n"
            "{{ channel_static }}\n"
            "{% if affection_prompt %}\n"
            "{{ affection_prompt }}\n"
            "{% endif %}\n"
            "{{ runtime_dynamic }}\n"
            "\n"
            "### Your Plugins:\n"
            "<plugins>\n"
            "{{ plugins_prompt }}\n"
            "</plugins>\n"
            "{% endmacro %}",
        )
        for affection in ("", "## Affection\n好感正文"):
            fields: dict[str, Any] = {
                "stable_static": "稳定段",
                "channel_static": "频道段",
                "runtime_dynamic": "动态段",
                "affection_prompt": affection,
                "plugins_prompt": "插件段",
            }
            expected = before_slot.module.system_prompt(**fields)  # type: ignore[attr-defined]

            assert SystemPrompt(**fields).render(env) == expected


class TestRunAgentWiring:
    """对话主链路把「人设之后」那一组交给编译器

    组装已整体搬进 `round_prompt`，这里跟着检查搬家后的那个函数；断言本身不变：
    编译器只出现一次、拿到的必须是由 `resolve_round` 结果渲染出来的 `worldbook_prompt`。
    """

    def test_compiler_receives_the_rendered_block(self) -> None:
        import ast
        import inspect
        import textwrap

        from nekro_agent.services.agent import round_prompt as round_prompt_module

        tree = ast.parse(textwrap.dedent(inspect.getsource(round_prompt_module._build_round_prompt)))  # noqa: SLF001
        compiler_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "PromptCompiler"
        ]
        assert len(compiler_calls) == 1
        keywords = {keyword.arg: keyword.value for keyword in compiler_calls[0].keywords}
        passed = keywords.get("worldbook_prompt")
        assert isinstance(passed, ast.Name) and passed.id == "worldbook_prompt", "编译器没拿到渲染好的世界书块"
        resolve_lines = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "resolve_round"
        ]
        assert resolve_lines and min(resolve_lines) < compiler_calls[0].lineno


def _hit(entry_id: int, *, content: str, trusted: bool, position: Position, depth: int = 4) -> selection.Hit:
    from nekro_agent.services.worldbook import matcher

    return selection.Hit(
        entry=MatchEntry(
            id=entry_id,
            book_id=1,
            keys=(),
            secondary_keys=(),
            selective_logic=None,
            use_regex=False,
            constant=True,
            content=content,
            order=100,
            position=position,
            depth=depth,
            scan_depth=4,
            case_sensitive=False,
            match_whole_words=False,
            sticky=0,
            cooldown=0,
            delay=0,
            trusted=trusted,
            invalid_keys=(),
        ),
        reason=matcher.HitReason(kind="constant"),
    )


class TestCachedRoundIsReadOnly:
    """历史消息只读缓存：没解析过就不注入，也不在渲染路径里查库"""

    def test_missing_round_means_no_injection(self) -> None:
        runtime._rounds.clear()  # noqa: SLF001

        assert runtime.cached_round("never-resolved") is None

    def test_cached_round_is_returned_as_is(self) -> None:
        from nekro_agent.services.worldbook.runtime import RoundWorldbook

        result = RoundWorldbook(state="injected", groups=selection.RenderGroups())
        runtime._rounds["cached-code"] = result  # noqa: SLF001
        try:
            assert runtime.cached_round("cached-code") is result
        finally:
            runtime._rounds.clear()  # noqa: SLF001

    def test_finish_round_pops_the_cache(self) -> None:
        from nekro_agent.services.worldbook.runtime import RoundWorldbook

        runtime._rounds["popped-code"] = RoundWorldbook(state="injected", groups=selection.RenderGroups())  # noqa: SLF001
        runtime.finish_round("popped-code", "ok")

        assert runtime.cached_round("popped-code") is None


class TestGroupsFeedTheRenderer:
    """分组与渲染的接缝：`after_persona` 进系统提示词，另外两组进历史"""

    def test_group_for_render_splits_by_position(self) -> None:
        hits = [
            _hit(1, content="人设后", trusted=True, position=Position.AFTER_PERSONA),
            _hit(2, content="记录前", trusted=True, position=Position.BEFORE_HISTORY),
            _hit(3, content="深度二", trusted=False, position=Position.AT_DEPTH, depth=2),
        ]

        groups = selection.group_for_render(hits)

        assert [hit.entry.id for hit in groups.after_persona] == [1]
        assert [hit.entry.id for hit in groups.before_history] == [2]
        assert [hit.entry.id for hit in groups.at_depth[2]] == [3]
        assert groups.empty is False


class _FakeExtData:
    ref_msg_id = ""


class _FakeDbMessage:
    """只够 `render_history_data` 走完流程的假消息行"""

    is_system = False
    ext_data_obj = _FakeExtData()

    def __init__(self, message_id: str) -> None:
        self.message_id = message_id

    def parse_content_data(self) -> list[Any]:
        return []

    def parse_chat_history_prompt(self, *_args: Any, **_kwargs: Any) -> str:
        return f"[{self.message_id}]"


class _FakeChannel:
    conversation_start_time = __import__("datetime").datetime(2000, 1, 1)
    workspace_id = None

    async def get_preset(self) -> Any:
        return None

    async def get_effective_config(self) -> Any:
        return None


class _FakeConfig:
    AI_CHAT_CONTEXT_EXPIRE_SECONDS = 0
    AI_CHAT_CONTEXT_MAX_LENGTH = 10
    AI_SYSTEM_NOTIFY_WINDOW_SIZE = 0
    AI_SYSTEM_NOTIFY_LIMIT = 0
    AI_VISION_IMAGE_LIMIT = 10
    AI_VISION_IMAGE_SIZE_LIMIT_KB = 1024 * 1024
    MEMORY_ENABLE_ENHANCED_RETRIEVAL = False
    AI_ALWAYS_INCLUDE_MSG_ID = False
    AI_CONTEXT_LENGTH_PER_SESSION = 100000


class _FakeModelGroup:
    CHAT_MODEL = "fake-model"


async def _render_history(
    monkeypatch: Any,
    *,
    code: str,
    message_ids: tuple[str, ...] = ("m1", "m2", "m3"),
    inject_worldbook: bool = True,
) -> str:
    """跑一遍 `render_history_data`，只关心拼出来的正文"""
    from nekro_agent.services.agent.templates import history as history_mod

    messages = [_FakeDbMessage(message_id) for message_id in message_ids]
    # 真实查询是 `order_by("-send_timestamp")`，即倒序；`render_history_data` 自己会翻回来。
    # 假查询也按倒序给，否则「翻回来」这一步在用例里等于没被覆盖。
    descending = list(reversed(messages))

    def fake_filter(*_args: Any, **_kwargs: Any) -> Any:
        class _QS:
            def order_by(self, *_a: Any, **_k: Any) -> _QS:
                return self

            def limit(self, *_a: Any, **_k: Any) -> _QS:
                return self

            async def __aiter__(self):
                for message in descending:
                    yield message

            def __await__(self):
                async def _inner() -> list[_FakeDbMessage]:
                    return descending

                return _inner().__await__()

        return _QS()

    async def fake_inject_memory_context(**_kwargs: Any) -> str:
        return ""

    monkeypatch.setattr(history_mod, "supports_vision", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(history_mod.DBChatMessage, "filter", staticmethod(fake_filter))
    monkeypatch.setattr(history_mod, "_inject_memory_context", fake_inject_memory_context)
    monkeypatch.setattr(history_mod, "is_domain_scope_enabled", lambda: False)

    rendered = await history_mod.render_history_data(
        chat_key="onebot_v11-t1a-group_1",
        db_chat_channel=_FakeChannel(),  # type: ignore[arg-type]
        one_time_code=code,
        config=_FakeConfig(),  # type: ignore[arg-type]
        model_group=_FakeModelGroup(),  # type: ignore[arg-type]
        inject_worldbook=inject_worldbook,
    )
    return str(rendered.to_dict()["content"])


class TestHistoryInjection:
    """历史消息里的两组：`before_history` 与 `at_depth`"""

    async def test_no_cached_round_leaves_history_untouched(self, monkeypatch: Any) -> None:
        runtime._rounds.clear()  # noqa: SLF001
        baseline = await _render_history(monkeypatch, code="no-round")

        assert "World Info" not in baseline

    async def test_before_history_block_precedes_recent_messages(self, monkeypatch: Any) -> None:
        from nekro_agent.services.worldbook.runtime import RoundWorldbook

        code = "with-before-history"
        runtime._rounds[code] = RoundWorldbook(  # noqa: SLF001
            state="injected",
            groups=selection.RenderGroups(
                before_history=[_hit(1, content="记录前正文", trusted=True, position=Position.BEFORE_HISTORY)],
            ),
        )
        try:
            content = await _render_history(monkeypatch, code=code)
        finally:
            runtime._rounds.clear()  # noqa: SLF001

        assert "记录前正文" in content
        assert content.index("记录前正文") < content.index("Recent Messages:")
        # 闭合标签与 `Recent Messages:` 之间正好空一行。
        assert f"记录前正文\n<{code} | /WorldInfo>\n\nRecent Messages:\n" in content

    async def test_rendered_history_entries_are_noted_for_the_round_detail(self, monkeypatch: Any) -> None:
        from nekro_agent.services.memory import prompt_rounds
        from nekro_agent.services.worldbook.runtime import RoundWorldbook

        code = "noted-history"
        runtime._rounds[code] = RoundWorldbook(  # noqa: SLF001
            state="injected",
            groups=selection.RenderGroups(
                before_history=[_hit(1, content="记录前正文", trusted=True, position=Position.BEFORE_HISTORY)],
                at_depth={1: [_hit(2, content="深度正文", trusted=False, position=Position.AT_DEPTH, depth=1)]},
            ),
        )
        prompt_rounds.begin(code, _FakeChannel())
        try:
            await _render_history(monkeypatch, code=code)
            draft = prompt_rounds._rounds[code]  # noqa: SLF001
            noted = {entry_id: (item.position, item.renders) for entry_id, item in draft.worldbook_items.items()}
        finally:
            runtime._rounds.clear()  # noqa: SLF001
            prompt_rounds._rounds.pop(code, None)  # noqa: SLF001

        assert noted == {1: ("before_history", 1), 2: ("at_depth", 1)}

    async def test_iteration_render_does_not_repeat_the_worldbook(self, monkeypatch: Any) -> None:
        """迭代时的增量渲染不再带世界书：两组都已经在本轮第一次渲染的历史里"""
        from nekro_agent.services.memory import prompt_rounds
        from nekro_agent.services.worldbook.runtime import RoundWorldbook

        code = "iteration-render"
        runtime._rounds[code] = RoundWorldbook(  # noqa: SLF001
            state="injected",
            groups=selection.RenderGroups(
                before_history=[_hit(1, content="记录前正文", trusted=True, position=Position.BEFORE_HISTORY)],
                at_depth={1: [_hit(2, content="深度正文", trusted=False, position=Position.AT_DEPTH, depth=1)]},
            ),
        )
        prompt_rounds.begin(code, _FakeChannel())
        try:
            first = await _render_history(monkeypatch, code=code)
            again = await _render_history(monkeypatch, code=code, inject_worldbook=False)
            draft = prompt_rounds._rounds[code]  # noqa: SLF001
            renders = {entry_id: item.renders for entry_id, item in draft.worldbook_items.items()}
        finally:
            runtime._rounds.clear()  # noqa: SLF001
            prompt_rounds._rounds.pop(code, None)  # noqa: SLF001

        assert "记录前正文" in first and "深度正文" in first
        assert "World Info" not in again
        assert "记录前正文" not in again and "深度正文" not in again
        # 增量渲染不算一次渲染：明细里每条仍是 1 次。
        assert renders == {1: 1, 2: 1}

    async def test_at_depth_block_lands_inside_the_history(self, monkeypatch: Any) -> None:
        from nekro_agent.services.worldbook.runtime import RoundWorldbook

        code = "with-depth"
        runtime._rounds[code] = RoundWorldbook(  # noqa: SLF001
            state="injected",
            groups=selection.RenderGroups(
                at_depth={1: [_hit(2, content="倒数第一条之前", trusted=False, position=Position.AT_DEPTH, depth=1)]},
            ),
        )
        try:
            content = await _render_history(monkeypatch, code=code)
        finally:
            runtime._rounds.clear()  # noqa: SLF001

        # 深度 1 落在最后一条消息之前：在 `Recent Messages:` 之后、`[m3]` 之前。
        assert content.index("Recent Messages:") < content.index("倒数第一条之前")
        assert content.index("倒数第一条之前") < content.index("[m3]")
        assert content.index("[m2]") < content.index("倒数第一条之前")

    async def test_gate_on_without_any_entry_is_byte_identical_to_gate_off(self, monkeypatch: Any) -> None:
        """开闸但一条都没入选：历史消息与闸关时逐字相同

        这一条对应设计稿里「闸关逐字」的延伸：闸开着、库里却没有书（或没有命中）时，
        提示词不该多出任何字符——包括一个空行。两次渲染必须用**同一个令牌**，否则
        `message separator` 里的令牌本身就会让两份文字不同，比较就失去意义。
        """
        from nekro_agent.services.worldbook.runtime import RoundWorldbook

        code = "same-code-both-ways"
        runtime._rounds.clear()  # noqa: SLF001
        gate_off = await _render_history(monkeypatch, code=code)
        runtime._rounds[code] = RoundWorldbook(state="none", groups=selection.RenderGroups())  # noqa: SLF001
        try:
            gate_on = await _render_history(monkeypatch, code=code)
        finally:
            runtime._rounds.clear()  # noqa: SLF001

        assert gate_on == gate_off

    def test_gate_on_without_any_entry_renders_the_same_system_prompt(self) -> None:
        baseline = _content(_compiler())
        # 求值返回 `none` 时调用方拿到的 `after_persona` 是空列表，渲染出空串。
        from nekro_agent.services.worldbook import render

        empty_block = render.render_block([], "code", in_history=False)

        assert _content(_compiler(worldbook_prompt=empty_block)) == baseline
