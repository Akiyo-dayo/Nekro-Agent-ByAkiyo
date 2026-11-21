"""频道摘要的编辑、回滚、暂停与自动更新撞车。

摘要的自动更新由沉淀模型驱动，手动编辑 / 回滚 / 暂停由管理端驱动。两边的交集只有
一处：保存自动结果时要确认「生成时看到的版本号还是最新」。这些用例把那条判据、
暂停对提示词的影响，以及暂停不影响注入的边界都钉住。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Callable

import pytest
from qdrant_client import AsyncQdrantClient
from tortoise import Tortoise

from nekro_agent.core.config import ModelConfigGroup, config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_channel_summary import DBMemChannelSummary
from nekro_agent.models.db_mem_channel_summary_state import DBMemChannelSummaryState
from nekro_agent.schemas.errors import ConflictError, ValidationError
from nekro_agent.services.agent.templates import history as history_module
from nekro_agent.services.memory import consolidator as consolidator_module
from nekro_agent.services.memory import embedding_service as embedding_service_module
from nekro_agent.services.memory import qdrant_manager as qdrant_manager_module
from nekro_agent.services.memory.channel_summary import (
    SUMMARY_KEEP_VERSIONS,
    edit_summary,
    get_latest_summary,
    is_paused,
    rollback_summary,
    save_summary,
    set_paused,
)
from nekro_agent.services.memory.consolidator import EpisodicConsolidator

ADAPTER = "onebot_v11"
INSTANCE = "sumadmin"

# 闸关时的沉淀提示词模板。这里刻意冻结一份旧拼法：闸关路径不许被本轮改动碰到，
# 只断言「不含摘要字样」抓不到模板里其他字句被顺手改掉。
_FROZEN_GATE_OFF_PROMPT = """请分析以下对话，提取值得记住的重要信息。

要求：
1. 使用第三人称视角描述
2. 只提取有价值的信息（事实、偏好、重要讨论、决策等）
3. 过滤掉闲聊和无意义内容
4. 每条记忆应该简洁但完整{persona_hint}

对话内容：
{conversation_text}

请以 JSON 格式返回，格式如下：
```json
{{
  "memories": [
    {{
      "content": "第三人称的记忆描述，必须保留关键对象、行为、结论，不要写成过短标签",
      "summary": "可读性摘要（建议 20-40 字，必须能独立看懂）",
      "knowledge_type": "conversation|preference|fact|experience|decision|emotion",
      "entities": ["涉及的人名或概念"],
      "relations": [
        {{
          "subject": "实体A",
          "predicate": "喜欢|使用|创建|提到|讨论|属于",
          "object": "实体B"
        }}
      ]
    }}
  ]
}}
```

额外要求：
1. 不要输出“某人提到某事”“讨论了某话题”这种空泛句，必须带上具体内容。
2. 优先保留可复用的信息：能力、偏好、决策、结论、承诺、计划、问题定位、技术事实。
3. summary 不能只是项目名或动词短语，单独展示时必须有参考价值。
4. 如果多条信息属于同一事件，优先合并成一条更完整的记忆，而不是切成多个碎片。

如果没有值得记住的内容，返回空数组：{{"memories": []}}"""


@pytest.fixture
async def memory_env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_ENABLE_FOR_CHAT", True)
    monkeypatch.setattr(config, "MEMORY_CHANNEL_SUMMARY_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_CHANNEL_SUMMARY_MAX_CHARS", 400)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MIN_CONTENT_LENGTH", 0)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS", 0.0)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_PARSE_MAX_RETRIES", 0)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MODEL_GROUP", "mem-primary")
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_FALLBACK_MODEL_GROUP", "mem-primary")
    monkeypatch.setattr(config, "USE_MODEL_GROUP", "mem-primary")
    monkeypatch.setattr(
        config,
        "MODEL_GROUPS",
        {"mem-primary": ModelConfigGroup(GROUP_NAME="mem-primary", CHAT_MODEL="mem-model")},
    )
    monkeypatch.setattr(config, "OUTPUT_FILTER_ENABLE", False)
    monkeypatch.setattr(config, "OUTPUT_FILTER_BEFORE_MEMORY", False)

    async def fake_embed(_text: str, **_kwargs: object) -> list[float]:
        return [0.1, 0.2, 0.3, 0.4]

    async def fake_embed_batch(texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    monkeypatch.setattr(consolidator_module, "embed_text", fake_embed)
    monkeypatch.setattr(embedding_service_module, "embed_text", fake_embed)
    monkeypatch.setattr(embedding_service_module, "embed_batch", fake_embed_batch)
    monkeypatch.setattr(qdrant_manager_module, "embed_batch", fake_embed_batch)

    client = AsyncQdrantClient(location=":memory:")

    async def fake_get_client() -> AsyncQdrantClient:
        return client

    monkeypatch.setattr(qdrant_manager_module, "get_qdrant_client", fake_get_client)

    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        provider_account_id="ubot",
    )
    await qdrant_manager_module.memory_qdrant_manager.ensure_collection(4)
    try:
        yield
    finally:
        await client.close()
        await Tortoise.close_connections()


async def _channel(chat_key: str, *, chat_type: str = "group") -> DBChatChannel:
    return await DBChatChannel.create(
        chat_key=chat_key,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id=chat_key.rsplit("-", 1)[-1],
        channel_name=chat_key,
        channel_type=chat_type,
        is_active=True,
        workspace_id=None,
        preset_id=None,
        data="{}",
    )


async def _message(chat_key: str, *, index: int, account: str, nickname: str, chat_type: str = "group") -> None:
    await DBChatMessage.create(
        sender_id=account,
        sender_name=nickname,
        sender_nickname=nickname,
        is_tome=0,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        message_id=f"m{index}",
        chat_key=chat_key,
        chat_type=chat_type,
        platform_userid=account,
        content_text=f"消息 {index}",
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=1_700_000 + index * 60,
    )


async def _seed_batch(chat_key: str, *, chat_type: str = "group") -> DBChatChannel:
    channel = await _channel(chat_key, chat_type=chat_type)
    await _message(chat_key, index=1, account="-1", nickname="助手", chat_type=chat_type)
    await _message(chat_key, index=2, account="ua", nickname="小满", chat_type=chat_type)
    return channel


def _response(*, channel_summary: Any = None, include_summary: bool = True) -> str:
    payload: dict[str, Any] = {"memories": []}
    if include_summary:
        payload["channel_summary"] = channel_summary
    return json.dumps(payload, ensure_ascii=False)


def _install_llm(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[str], str],
) -> list[str]:
    prompts: list[str] = []

    async def fake_call(_self: EpisodicConsolidator, prompt: str, *, use_fallback_model: bool = False) -> str:
        del use_fallback_model
        prompts.append(prompt)
        return handler(prompt)

    monkeypatch.setattr(EpisodicConsolidator, "_call_llm", fake_call)
    return prompts


async def _write(chat_key: str, content: str, *, last_message_db_id: int = 1) -> DBMemChannelSummary:
    row = await save_summary(
        chat_key=chat_key,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        preset_id=0,
        content=content,
        last_message_db_id=last_message_db_id,
    )
    assert row is not None
    return row


class TestEdit:
    async def test_edit_creates_a_manual_version(self, memory_env: None) -> None:
        chat_key = "onebot_v11-" + INSTANCE + "-group_e1"
        first = await _write(chat_key, "第一版摘要", last_message_db_id=7)

        edited = await edit_summary(
            chat_key=chat_key,
            preset_id=0,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            content="手动改过的摘要",
            base_version=first.version,
            editor="admin",
        )

        assert edited.version == 2
        assert edited.content == "手动改过的摘要"
        assert edited.source == "manual"
        assert edited.edited_by == "admin"
        assert edited.last_message_db_id == 7
        assert edited.source_version is None

    async def test_edit_truncates_and_sanitizes(self, memory_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "MEMORY_CHANNEL_SUMMARY_MAX_CHARS", 10)
        chat_key = "onebot_v11-" + INSTANCE + "-group_e2"
        first = await _write(chat_key, "旧版")

        edited = await edit_summary(
            chat_key=chat_key,
            preset_id=0,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            content="\u200b长" * 30,
            base_version=first.version,
            editor="admin",
        )

        assert len(edited.content) == 11
        assert edited.content.endswith("…")
        assert "\u200b" not in edited.content

    async def test_edit_with_empty_content_is_rejected(self, memory_env: None) -> None:
        chat_key = "onebot_v11-" + INSTANCE + "-group_e3"
        first = await _write(chat_key, "旧版")

        with pytest.raises(ValidationError):
            await edit_summary(
                chat_key=chat_key,
                preset_id=0,
                adapter_key=ADAPTER,
                instance_key=INSTANCE,
                content="\u200b\x00",
                base_version=first.version,
                editor="admin",
            )
        assert await DBMemChannelSummary.filter(chat_key=chat_key).count() == 1

    async def test_stale_base_version_is_a_conflict(self, memory_env: None) -> None:
        chat_key = "onebot_v11-" + INSTANCE + "-group_e4"
        first = await _write(chat_key, "第一版")
        await _write(chat_key, "第二版")

        with pytest.raises(ConflictError):
            await edit_summary(
                chat_key=chat_key,
                preset_id=0,
                adapter_key=ADAPTER,
                instance_key=INSTANCE,
                content="基于旧版本的手动修改",
                base_version=first.version,
                editor="admin",
            )
        latest = await get_latest_summary(chat_key, 0)
        assert latest is not None and latest.content == "第二版"

    async def test_identical_content_does_not_create_a_version(self, memory_env: None) -> None:
        chat_key = "onebot_v11-" + INSTANCE + "-group_e5"
        first = await _write(chat_key, "同一段话")

        result = await edit_summary(
            chat_key=chat_key,
            preset_id=0,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            content="同一段话",
            base_version=first.version,
            editor="admin",
        )

        assert result.version == first.version
        assert await DBMemChannelSummary.filter(chat_key=chat_key).count() == 1


class TestRollback:
    async def test_default_rollback_uses_the_previous_version(self, memory_env: None) -> None:
        chat_key = "onebot_v11-" + INSTANCE + "-group_r1"
        await _write(chat_key, "第一版")
        await _write(chat_key, "第二版")

        rolled = await rollback_summary(
            chat_key=chat_key,
            preset_id=0,
            to_version=None,
            editor="admin",
        )

        assert rolled.version == 3
        assert rolled.content == "第一版"
        assert rolled.source == "rollback"
        assert rolled.source_version == 1
        assert rolled.edited_by == "admin"

    async def test_rollback_to_a_specific_version(self, memory_env: None) -> None:
        chat_key = "onebot_v11-" + INSTANCE + "-group_r2"
        await _write(chat_key, "第一版")
        await _write(chat_key, "第二版")
        await _write(chat_key, "第三版")

        rolled = await rollback_summary(
            chat_key=chat_key,
            preset_id=0,
            to_version=1,
            editor="admin",
        )

        assert rolled.content == "第一版"
        assert rolled.source_version == 1
        assert rolled.version == 4

    async def test_missing_target_version_is_rejected(self, memory_env: None) -> None:
        chat_key = "onebot_v11-" + INSTANCE + "-group_r3"
        await _write(chat_key, "只有一版")

        with pytest.raises(ValidationError):
            await rollback_summary(chat_key=chat_key, preset_id=0, to_version=9, editor="admin")
        with pytest.raises(ValidationError):
            await rollback_summary(chat_key=chat_key, preset_id=0, to_version=None, editor="admin")
        assert await DBMemChannelSummary.filter(chat_key=chat_key).count() == 1

    async def test_only_five_versions_are_kept(self, memory_env: None) -> None:
        chat_key = "onebot_v11-" + INSTANCE + "-group_r4"
        for index in range(SUMMARY_KEEP_VERSIONS):
            await _write(chat_key, f"第 {index} 版")

        rolled = await rollback_summary(chat_key=chat_key, preset_id=0, to_version=4, editor="admin")

        rows = await DBMemChannelSummary.filter(chat_key=chat_key).order_by("version")
        assert [row.version for row in rows] == [2, 3, 4, 5, 6]
        assert rolled.version == 6


class TestPause:
    async def test_set_paused_creates_and_updates_the_state_row(self, memory_env: None) -> None:
        chat_key = "onebot_v11-" + INSTANCE + "-group_p1"
        assert await is_paused(chat_key, 0) is False

        paused = await set_paused(
            chat_key=chat_key,
            preset_id=0,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            paused=True,
            editor="admin",
        )
        assert paused.paused is True
        assert paused.paused_by == "admin"
        assert paused.paused_at is not None
        assert await is_paused(chat_key, 0) is True
        assert await DBMemChannelSummaryState.filter(chat_key=chat_key).count() == 1

        resumed = await set_paused(
            chat_key=chat_key,
            preset_id=0,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            paused=False,
            editor="admin",
        )
        assert resumed.paused is False
        assert resumed.paused_at is None
        assert await is_paused(chat_key, 0) is False
        assert await DBMemChannelSummaryState.filter(chat_key=chat_key).count() == 1

    async def test_paused_consolidation_has_no_summary_and_ignores_the_result(
        self,
        memory_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-" + INSTANCE + "-group_p2"
        channel = await _seed_batch(chat_key)
        await _write(chat_key, "暂停前的摘要")
        await set_paused(
            chat_key=chat_key,
            preset_id=0,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            paused=True,
            editor="admin",
        )
        prompts = _install_llm(monkeypatch, lambda _p: _response(channel_summary="暂停期间不该写入的摘要"))

        await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        assert "PreviousChannelSummary" not in prompts[0]
        assert "上一版频道摘要" not in prompts[0]
        assert '"channel_summary"' not in prompts[0]
        latest = await get_latest_summary(chat_key, 0)
        assert latest is not None and latest.content == "暂停前的摘要"
        assert await DBMemChannelSummary.filter(chat_key=chat_key).count() == 1
        # 暂停只挡自动更新，注入照常使用最新一版。
        assert "暂停前的摘要" in await history_module._inject_channel_summary(channel, "paused-code")  # noqa: SLF001

    async def test_resume_updates_the_summary_again(
        self,
        memory_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-" + INSTANCE + "-group_p3"
        await _seed_batch(chat_key)
        await _write(chat_key, "暂停前的摘要")
        await set_paused(
            chat_key=chat_key,
            preset_id=0,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            paused=True,
            editor="admin",
        )
        _install_llm(monkeypatch, lambda _p: _response(channel_summary="暂停期间不该写入的摘要"))
        await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        await set_paused(
            chat_key=chat_key,
            preset_id=0,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            paused=False,
            editor="admin",
        )
        prompts = _install_llm(monkeypatch, lambda _p: _response(channel_summary="恢复后的摘要"))
        await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        assert "PreviousChannelSummary" in prompts[0]
        assert '"channel_summary"' in prompts[0]
        latest = await get_latest_summary(chat_key, 0)
        assert latest is not None
        assert latest.content == "恢复后的摘要"
        assert latest.version == 2


class TestCollision:
    async def test_manual_edit_discards_the_stale_automatic_result(self, memory_env: None) -> None:
        chat_key = "onebot_v11-" + INSTANCE + "-group_c1"
        await _write(chat_key, "第一版")
        await _write(chat_key, "第二版")

        # 沉淀读到版本 2 之后，管理员手动编辑成版本 3。
        manual = await edit_summary(
            chat_key=chat_key,
            preset_id=0,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            content="手动第三版",
            base_version=2,
            editor="admin",
        )
        assert manual.version == 3

        discarded = await save_summary(
            chat_key=chat_key,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            preset_id=0,
            content="基于第二版的自动结果",
            last_message_db_id=9,
            base_version=2,
        )

        assert discarded is None
        latest = await get_latest_summary(chat_key, 0)
        assert latest is not None
        assert latest.version == 3
        assert latest.content == "手动第三版"
        assert latest.source == "manual"


class TestGateOff:
    async def test_gate_off_consolidation_prompt_is_unchanged(
        self,
        memory_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
        chat_key = "onebot_v11-" + INSTANCE + "-group_g1"
        await _seed_batch(chat_key)

        monkeypatch.setattr(
            EpisodicConsolidator,
            "_build_conversation_text",
            lambda _self, _messages: "固定对话素材",
        )

        async def no_persona(_self: EpisodicConsolidator, _chat_key: str) -> None:
            _self.persona_context = None
            return None

        monkeypatch.setattr(EpisodicConsolidator, "load_persona_context", no_persona)
        prompts = _install_llm(monkeypatch, lambda _p: _response(include_summary=False))

        await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        assert prompts[0] == _FROZEN_GATE_OFF_PROMPT.format(persona_hint="", conversation_text="固定对话素材")
