"""`memory_tools` 的闸开改名与按 ID 取不回失活条目。

闸关是回滚路径：方法清单、提示词、旧方法的查询都不能变。闸开交给模型的是三个不带
「工作区」字样的新方法；它们按 ID 取详情与追溯时额外排除已隔离 / 已软删的行，避免
模型用旧对话里记住的 ID 把已经藏起来的内容重新取回来。
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import (
    CognitiveType,
    DBMemParagraph,
    KnowledgeType,
    OriginKind,
)
from nekro_agent.services.memory.instance_scope import MemoryScope
from plugins.builtin.memory_tools import main as memory_tools
from plugins.builtin.memory_tools.plugin import plugin

_OLD_PROMPT = (
    "[Memory Tools]\n"
    "Use workspace memory tools when you need prior decisions, earlier solutions, historical preferences, or earlier task context."
)
_NEW_PROMPT = (
    "[Memory Tools]\n"
    "Use memory tools when you need prior decisions, earlier solutions, historical preferences, or earlier task context."
)
_OLD_METHOD_NAMES = [
    "search_workspace_memories_tool",
    "get_memory_detail_tool",
    "trace_memory_origin_tool",
]
_NEW_METHOD_NAMES = [
    "search_memories_tool",
    "view_memory_tool",
    "trace_memory_source_tool",
]


class _StubCtx:
    """只实现 `memory_tools` 用到的两个口子。"""

    def __init__(self, workspace: Any, channel: DBChatChannel | None) -> None:
        self._workspace = workspace
        self.db_chat_channel = channel
        self.from_chat_key = channel.chat_key if channel is not None else ""

    async def get_bound_workspace(self) -> Any:
        return self._workspace


@pytest.fixture
async def tool_env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_ENABLE_FOR_CHAT", True)
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _paragraph(content: str, *, is_inactive: bool) -> DBMemParagraph:
    return await DBMemParagraph.create(
        workspace_id=0,
        adapter_key="",
        instance_key="",
        preset_id=0,
        origin_chat_key="",
        origin_chat_type="group",
        memory_source="na",
        cognitive_type=CognitiveType.SEMANTIC,
        knowledge_type=KnowledgeType.FACT,
        content=content,
        summary=content[:20],
        origin_kind=OriginKind.CONSOLIDATION,
        event_time=datetime.now(timezone.utc),
        is_inactive=is_inactive,
    )


async def _seed_rows() -> dict[str, Any]:
    active_paragraph = await _paragraph("正常段落的内容", is_inactive=False)
    inactive_paragraph = await _paragraph("隔离段落的隐藏内容", is_inactive=True)

    active_episode = await DBMemEpisode.create(
        workspace_id=0,
        adapter_key="",
        instance_key="",
        preset_id=0,
        origin_chat_key="",
        origin_chat_type="group",
        title="正常 episode",
        narrative_summary="正常 episode 的叙事",
        paragraph_ids=[],
        participant_entity_ids=[],
        phase_mapping={},
        is_inactive=False,
    )
    inactive_episode = await DBMemEpisode.create(
        workspace_id=0,
        adapter_key="",
        instance_key="",
        preset_id=0,
        origin_chat_key="",
        origin_chat_type="group",
        title="隔离 episode",
        narrative_summary="隔离 episode 的隐藏叙事",
        paragraph_ids=[],
        participant_entity_ids=[],
        phase_mapping={},
        is_inactive=True,
    )
    return {
        "active_paragraph": active_paragraph,
        "inactive_paragraph": inactive_paragraph,
        "active_episode": active_episode,
        "inactive_episode": inactive_episode,
    }


def _install_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_scope(_ctx: Any) -> tuple[int, MemoryScope]:
        return 0, MemoryScope(allowed_instance_keys=None, preset_id=0)

    monkeypatch.setattr(memory_tools, "_require_bound_memory_scope", fake_scope)


async def _exception_of(awaitable: Any) -> tuple[type[BaseException], str]:
    with pytest.raises(Exception) as exc:
        await awaitable
    return type(exc.value), str(exc.value)


class TestPluginBriefInSystemPrompt:
    """插件名与能力摘要会进系统提示词（常驻插件那一段），闸关时必须与改造前逐字相同。"""

    # 改造前 `plugin.py` 的原文，冻结在这里而不是从模块里读，免得改了源码用例跟着变。
    _OLD_NAME = "主动记忆工具"
    _OLD_BRIEF = (
        "用于在工作区内主动搜索历史记忆、展开记忆详情和追溯记忆来源，"
        "适合处理“之前说过什么”“以前怎么解决过”“历史偏好/决策/经验”这类问题。"
    )

    def test_gate_off_name_and_brief_are_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.plugin.prompt_activation import get_plugin_brief

        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
        assert plugin.name == self._OLD_NAME
        assert get_plugin_brief(plugin) == self._OLD_BRIEF

    def test_gate_on_brief_has_no_workspace_wording(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.plugin.prompt_activation import get_plugin_brief

        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        brief = get_plugin_brief(plugin)
        assert brief
        assert "工作区" not in brief
        assert "workspace" not in brief.lower()
        assert "工作区" not in plugin.name

    def test_resolver_failure_falls_back_to_static_brief(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.plugin.prompt_activation import get_plugin_brief

        def broken() -> str:
            raise RuntimeError("boom")

        monkeypatch.setattr(plugin, "brief_resolver", broken)
        assert get_plugin_brief(plugin) == self._OLD_BRIEF


class TestGateOff:
    async def test_methods_and_prompt_are_the_old_ones(self, tool_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
        ctx = _StubCtx(SimpleNamespace(id=1), None)

        methods = await memory_tools.collect_memory_methods(ctx)
        assert [method.__name__ for method in methods] == _OLD_METHOD_NAMES
        assert await memory_tools.memory_tools_prompt(ctx) == _OLD_PROMPT

    async def test_old_methods_still_return_quarantined_rows(
        self,
        tool_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
        rows = await _seed_rows()
        _install_scope(monkeypatch)
        ctx = _StubCtx(SimpleNamespace(id=1), None)

        detail = await memory_tools.get_memory_detail_tool(
            ctx,
            memory_type="paragraph",
            memory_id=rows["inactive_paragraph"].id,
        )
        trace = await memory_tools.trace_memory_origin_tool(
            ctx,
            memory_type="paragraph",
            memory_id=rows["inactive_paragraph"].id,
        )

        assert "隔离段落的隐藏内容" in detail.body
        assert str(rows["inactive_paragraph"].id) in trace.body


class TestGateOn:
    async def test_new_methods_have_no_workspace_wording(self, tool_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        channel = await DBChatChannel.create(
            chat_key="onebot_v11-rename-group_1",
            adapter_key="onebot_v11",
            instance_key="rename",
            channel_id="group_1",
            channel_name="改名用例",
            channel_type="group",
            is_active=True,
            workspace_id=None,
            data="{}",
        )
        ctx = _StubCtx(SimpleNamespace(id=1), channel)

        methods = await memory_tools.collect_memory_methods(ctx)
        assert [method.__name__ for method in methods] == _NEW_METHOD_NAMES
        assert await memory_tools.memory_tools_prompt(ctx) == _NEW_PROMPT

        for method in methods:
            metadata = next(item for item in plugin.sandbox_methods if item.func is method)
            docstring = inspect.getdoc(method) or ""
            surfaces = (method.__name__, metadata.name, metadata.description, docstring)
            for surface in surfaces:
                lowered = surface.lower()
                assert "工作区" not in surface
                assert "workspace" not in lowered

    async def test_new_methods_match_missing_id_for_inactive_paragraphs(
        self,
        tool_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rows = await _seed_rows()
        _install_scope(monkeypatch)
        ctx = _StubCtx(SimpleNamespace(id=1), None)
        inactive_id = rows["inactive_paragraph"].id

        # 同一个 ID：先是隔离行，再被真删成「不存在」，两边的异常要逐字相同。
        inactive_errors = {
            method.__name__: await _exception_of(method(ctx, memory_type="paragraph", memory_id=inactive_id))
            for method in (memory_tools.view_memory_tool, memory_tools.trace_memory_source_tool)
        }
        await DBMemParagraph.filter(id=inactive_id).delete()
        missing_errors = {
            method.__name__: await _exception_of(method(ctx, memory_type="paragraph", memory_id=inactive_id))
            for method in (memory_tools.view_memory_tool, memory_tools.trace_memory_source_tool)
        }

        assert inactive_errors == missing_errors
        assert all(error_type is ValueError for error_type, _ in inactive_errors.values())

    async def test_new_methods_match_missing_id_for_inactive_episodes(
        self,
        tool_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rows = await _seed_rows()
        _install_scope(monkeypatch)
        ctx = _StubCtx(SimpleNamespace(id=1), None)
        inactive_id = rows["inactive_episode"].id

        inactive_errors = {
            method.__name__: await _exception_of(method(ctx, memory_type="episode", memory_id=inactive_id))
            for method in (memory_tools.view_memory_tool, memory_tools.trace_memory_source_tool)
        }
        await DBMemEpisode.filter(id=inactive_id).delete()
        missing_errors = {
            method.__name__: await _exception_of(method(ctx, memory_type="episode", memory_id=inactive_id))
            for method in (memory_tools.view_memory_tool, memory_tools.trace_memory_source_tool)
        }

        assert inactive_errors == missing_errors
        assert all(error_type is ValueError for error_type, _ in inactive_errors.values())

    async def test_new_methods_still_return_active_rows(
        self,
        tool_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rows = await _seed_rows()
        _install_scope(monkeypatch)
        ctx = _StubCtx(SimpleNamespace(id=1), None)

        paragraph_detail = await memory_tools.view_memory_tool(
            ctx,
            memory_type="paragraph",
            memory_id=rows["active_paragraph"].id,
        )
        paragraph_trace = await memory_tools.trace_memory_source_tool(
            ctx,
            memory_type="paragraph",
            memory_id=rows["active_paragraph"].id,
        )
        episode_detail = await memory_tools.view_memory_tool(
            ctx,
            memory_type="episode",
            memory_id=rows["active_episode"].id,
        )

        assert "正常段落的内容" in paragraph_detail.body
        assert str(rows["active_paragraph"].id) in paragraph_trace.body
        assert "正常 episode" in episode_detail.body
