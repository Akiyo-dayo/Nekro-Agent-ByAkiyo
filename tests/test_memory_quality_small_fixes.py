"""记忆沉淀的三处小修

1. 沉淀时 Bot 的名字取频道此刻实际生效的人设名。改造前只从旧配置项 `AI_CHAT_PRESET_SETTING`
   里用正则抠名字，抠不到就是 "Assistant"：对话文本里 Bot 的行被标成「Assistant〔Bot〕」，
   记忆里满是「Assistant（角色设定为……）」。
2. 「（Bot 自己说过）」只给 Bot 单方面的承诺。用户提出、Bot 答应的约定被模型标成 Bot 自己说过，
   查下来对话文本的说话人标得没错（用户的行没有〔Bot〕标记），是提示词把「Bot 的承诺」和
   「双方的约定」混在一起了。这里钉住提示词里的区分；模型照不照做要在实际部署上看。
3. 好感里程碑正文称呼对方时，用已经记下的称呼，平台昵称兜底。
"""

from __future__ import annotations

from typing import Any

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_affection import DBAffection
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.services.affection.stage import AffectionEventSource
from nekro_agent.services.affection.store import set_call_name, set_score
from nekro_agent.services.memory.consolidator import EpisodicConsolidator, PersonaContext
from nekro_agent.services.preset_service import DEFAULT_PRESET_NAME
from tests.test_affection_milestones_flow import (  # noqa: F401 - 夹具按名字注入
    NICKNAME,
    embed_calls,
    flow_db,
    memory_on,
    milestone_paragraphs,
    model_write,
    prepare,
)

CHAT_WITH_PRESET = "onebot_v11-persona-group_1"
CHAT_WITHOUT_PRESET = "onebot_v11-persona-group_2"


@pytest.fixture
async def persona_db() -> Any:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def make_channel(chat_key: str, preset_id: int | None) -> DBChatChannel:
    return await DBChatChannel.create(
        chat_key=chat_key,
        adapter_key="onebot_v11",
        instance_key="persona",
        channel_id=chat_key.rsplit("_", 1)[-1],
        channel_name=chat_key,
        channel_type="group",
        is_active=True,
        preset_id=preset_id,
        data="{}",
    )


async def make_preset(name: str, description: str) -> DBPreset:
    return await DBPreset.create(
        name=name,
        title=name,
        avatar="",
        content=f"你是{name}，说话带点撒娇。",
        description=description,
        tags="",
        author="测试",
    )


class TestPersonaNameComesFromTheEffectivePreset:
    async def test_channel_preset_name_and_description_are_used(self, persona_db: None) -> None:
        del persona_db
        preset = await make_preset("晴音", "会撒娇的后辈")
        await make_channel(CHAT_WITH_PRESET, preset.id)
        consolidator = EpisodicConsolidator(workspace_id=0)

        context = await consolidator.load_persona_context(CHAT_WITH_PRESET)

        assert context is not None
        assert context.persona_name == "晴音"
        assert context.persona_brief == "会撒娇的后辈"

    async def test_channel_without_preset_falls_back_to_the_built_in_persona(self, persona_db: None) -> None:
        del persona_db
        await make_channel(CHAT_WITHOUT_PRESET, None)
        consolidator = EpisodicConsolidator(workspace_id=0)

        context = await consolidator.load_persona_context(CHAT_WITHOUT_PRESET)

        assert context is not None
        assert context.persona_name == DEFAULT_PRESET_NAME, "回退链与主对话相同，不再落到 Assistant"
        assert context.persona_name != "Assistant"

    async def test_bot_lines_are_labelled_with_the_persona_name(
        self,
        persona_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del persona_db
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        preset = await make_preset("晴音", "会撒娇的后辈")
        await make_channel(CHAT_WITH_PRESET, preset.id)
        consolidator = EpisodicConsolidator(workspace_id=0)
        await consolidator.load_persona_context(CHAT_WITH_PRESET)
        bot_line = DBChatMessage(
            sender_id="-1",
            sender_name="Bot",
            sender_nickname="Bot",
            platform_userid="",
            content_text="好呀，到时候我提醒你",
            send_timestamp=1_700_000_000,
        )

        text = consolidator._build_conversation_text([bot_line])  # noqa: SLF001

        assert "晴音〔Bot〕" in text
        assert "Assistant" not in text


class TestBotSaidIsOnlyForTheBotsOwnPromises:
    def test_prompt_separates_mutual_arrangements_from_one_sided_promises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        consolidator = EpisodicConsolidator(workspace_id=0)
        consolidator.persona_context = PersonaContext(bot_sender_id="-1", persona_name="晴音", persona_brief="会撒娇的后辈")
        consolidator.origin_chat_type = "private"

        prompt = consolidator._build_domain_prompt(  # noqa: SLF001
            persona_hint=consolidator._domain_persona_hint(),  # noqa: SLF001
            conversation_text="[20:00] 小砚: 我们约好一起看直播吧",
            previous_summary=None,
            one_time_code="a1b2c3d4",
            include_summary=False,
        )

        assert "单方面" in prompt
        assert "别人提出、Bot 答应的约定" in prompt
        assert "bot_said 为 false" in prompt
        # 改造前那句把「Bot 对别人作出的约定」整个归给 bot_said，用户提的约定也被标成 Bot 自己说过
        assert "只有 Bot 对别人作出的约定或承诺可以记" not in prompt


@pytest.mark.usefixtures("flow_db", "memory_on", "embed_calls")
class TestMilestoneUsesTheCallName:
    async def test_recorded_call_name_wins_over_the_platform_nickname(self) -> None:
        channel, cfg, scope, key = await prepare()
        await set_score(key, score=29, reason="管理员先把分调到门槛下", source=AffectionEventSource.MANUAL)
        await set_call_name(key, call_name="阿砚", nickname=NICKNAME)

        _, _, written = await model_write(key, scope=scope, channel=channel, cfg=cfg, delta=1, reason="聊得很投机")

        assert written is not None
        paragraph = (await milestone_paragraphs())[0]
        assert "「阿砚」" in paragraph.content
        assert f"「{NICKNAME}」" not in paragraph.content

    async def test_platform_nickname_is_used_when_no_call_name_is_recorded(self) -> None:
        channel, cfg, scope, key = await prepare()
        await set_score(key, score=29, reason="管理员先把分调到门槛下", source=AffectionEventSource.MANUAL)
        assert not (await DBAffection.get(**key.as_columns())).call_name

        await model_write(key, scope=scope, channel=channel, cfg=cfg, delta=1, reason="聊得很投机")

        paragraph = (await milestone_paragraphs())[0]
        assert f"「{NICKNAME}」" in paragraph.content
