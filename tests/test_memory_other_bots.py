"""群里别的 Bot 的人设台词不当成群友的事实；提示词讲清知识类型怎么选

验收里看到一个角色扮演号说的「住在学生公寓、周四有课」被记成了这个群友的事实。NA 看它就是
普通账号。本机其他 NA 实例的 Bot 账号是认得出来的：它们的行标〔他 Bot〕，不分参与者代号
（于是不会被写进「涉及的人」），按 Bot 台词处理。第三方 Bot 认不出来，由提示词交给模型判断。

知识类型决定半衰期（偏好 / 事实 / 决定按语义半衰期保留），提示词里讲清时效内容该归哪类。
"""

from __future__ import annotations

import json

import pytest
from qdrant_client import AsyncQdrantClient

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.services.memory import consolidator as consolidator_module
from nekro_agent.services.memory.consolidator import EpisodicConsolidator, PersonaContext
from nekro_agent.services.memory.persons import (
    human_accounts,
    load_person_accounts,
    merge_other_bot_accounts,
    resolve_other_bot_accounts,
)
from tests.test_memory_consolidation_quality import (  # noqa: F401 - 夹具按名字注入
    ADAPTER,
    BOT_ACCOUNT,
    INSTANCE,
    XIAOMAN,
    _channel,
    _install_llm,
    _install_persona,
    _item,
    _message,
    _run,
)
from tests.test_memory_consolidation_quality import memory_env as _quality_env

quality_env = _quality_env

OTHER_BOT = "8000002"
FOREIGN_ADAPTER_BOT = "8000003"
THIRD_BOT = "8000004"
CHAT = "onebot_v11-quality-group_bots"


async def _other_instances() -> None:
    await DBAdapterInstance.create(adapter_key=ADAPTER, instance_key="other", provider_account_id=OTHER_BOT)
    # 别的适配器上的账号与本群的号码不是一回事，不算
    await DBAdapterInstance.create(adapter_key="telegram", instance_key="tg", provider_account_id=FOREIGN_ADAPTER_BOT)


def _patch_channel_list(monkeypatch: pytest.MonkeyPatch, accounts: list[str]) -> None:
    """模拟频道生效配置：四层合并后的那一份里带着名单"""

    async def fake_effective_config(_self: DBChatChannel) -> object:
        return config.model_copy(deep=True, update={"MEMORY_OTHER_BOT_ACCOUNTS": accounts})

    monkeypatch.setattr(DBChatChannel, "get_effective_config", fake_effective_config)


class TestOtherBotAccounts:
    async def test_same_adapter_other_instances_only(self, quality_env: AsyncQdrantClient) -> None:
        del quality_env
        await _other_instances()
        assert await resolve_other_bot_accounts(ADAPTER, INSTANCE) == {OTHER_BOT}
        assert await resolve_other_bot_accounts(ADAPTER, "other") == {BOT_ACCOUNT}

    async def test_unclaimed_archived_and_deleted_instances_do_not_count(self, quality_env: AsyncQdrantClient) -> None:
        del quality_env
        await _other_instances()
        # 未认领实例是协议端按 self_id 自动登记的：谁连上来都有一行，不能借此把任意账号标成〔他 Bot〕
        for index, status in enumerate(("unclaimed", "archived", "deleted"), start=1):
            await DBAdapterInstance.create(
                adapter_key=ADAPTER,
                instance_key=f"gone{index}",
                provider_account_id=f"900000{index}",
                status=status,
            )
        assert await resolve_other_bot_accounts(ADAPTER, INSTANCE) == {OTHER_BOT}

    def test_human_accounts_skip_other_bots(self) -> None:
        rows = [
            DBChatMessage(sender_id=XIAOMAN, platform_userid=XIAOMAN, content_text="a"),
            DBChatMessage(sender_id=OTHER_BOT, platform_userid=OTHER_BOT, content_text="b"),
        ]
        assert human_accounts(rows, bot_account=BOT_ACCOUNT, other_bots={OTHER_BOT}) == [XIAOMAN]
        assert human_accounts(rows, bot_account=BOT_ACCOUNT) == [XIAOMAN, OTHER_BOT], "不传时与改造前相同"


class TestConfiguredOtherBots:
    async def test_channel_list_is_marked_and_kept_out_of_persons(
        self,
        quality_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del quality_env
        chat_key = f"{CHAT}_third"
        await _channel(chat_key)
        _install_persona(monkeypatch)
        _patch_channel_list(monkeypatch, [THIRD_BOT])
        await _message(chat_key, index=1, account=THIRD_BOT, nickname="晴音", content="我住在学生公寓，周四有课")
        await _message(chat_key, index=2, account=XIAOMAN, nickname="小满", content="晴音周四陪我去食堂")

        payload = {"memories": [_item("小满约晴音周四去食堂", persons=["P1"])]}
        prompts = _install_llm(monkeypatch, lambda _p: json.dumps(payload, ensure_ascii=False))
        await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        assert "晴音〔他 Bot〕: 我住在学生公寓，周四有课" in prompts[0]
        [row] = await DBMemParagraph.all()
        assert (await load_person_accounts([row.id]))[row.id] == [XIAOMAN], "第三方 Bot 不进 persons"

    async def test_own_account_in_the_list_is_ignored(
        self,
        quality_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del quality_env
        chat_key = f"{CHAT}_own"
        await _channel(chat_key)
        _patch_channel_list(monkeypatch, [BOT_ACCOUNT, " ", THIRD_BOT])
        await _message(chat_key, index=1, account=XIAOMAN, nickname="小满", content="随便聊聊")
        consolidator = EpisodicConsolidator(0)
        await consolidator._load_channel(chat_key)  # noqa: SLF001
        messages = list(await DBChatMessage.filter(chat_key=chat_key).order_by("id"))
        await consolidator._prepare_batch_participants(messages)  # noqa: SLF001

        assert consolidator.batch_other_bots == frozenset({THIRD_BOT}), "本 Bot 自己的号不生效"
        assert merge_other_bot_accounts(set(), [BOT_ACCOUNT], bot_account=BOT_ACCOUNT) == frozenset()

    async def test_empty_list_keeps_auto_detection(
        self,
        quality_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del quality_env
        chat_key = f"{CHAT}_auto"
        await _other_instances()
        await _channel(chat_key)
        _patch_channel_list(monkeypatch, [])
        await _message(chat_key, index=1, account=XIAOMAN, nickname="小满", content="随便聊聊")
        consolidator = EpisodicConsolidator(0)
        await consolidator._load_channel(chat_key)  # noqa: SLF001
        messages = list(await DBChatMessage.filter(chat_key=chat_key).order_by("id"))
        await consolidator._prepare_batch_participants(messages)  # noqa: SLF001

        assert consolidator.batch_other_bots == frozenset({OTHER_BOT}), "空名单时与改造前相同"

    async def test_config_error_does_not_stop_consolidation(
        self,
        quality_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del quality_env
        chat_key = f"{CHAT}_error"
        await _channel(chat_key)
        _install_persona(monkeypatch)
        await _message(chat_key, index=1, account=XIAOMAN, nickname="小满", content="周末见")

        async def broken_effective_config(_self: DBChatChannel) -> object:
            raise RuntimeError("config unavailable")

        async def always_enabled(_channel: DBChatChannel) -> bool:
            # 这里只验证「名单读不到」不阻断沉淀；频道开关本身由 feature_flags 的用例覆盖
            return True

        monkeypatch.setattr(DBChatChannel, "get_effective_config", broken_effective_config)
        monkeypatch.setattr(consolidator_module, "is_memory_enabled_for_channel", always_enabled)

        result = await _run(chat_key, monkeypatch, _item("小满周末去看展"))

        assert result.paragraphs_created == 1
        assert await DBMemParagraph.all().count() == 1, "取配置出错按空名单处理，不影响沉淀"


class TestConversationText:
    async def test_other_bot_lines_are_marked_and_not_coded(
        self,
        quality_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del quality_env
        await _other_instances()
        await _channel(CHAT)
        await _message(CHAT, index=1, account=OTHER_BOT, nickname="晴音", content="我住在学生公寓，周四有课")
        await _message(
            CHAT,
            index=2,
            account=XIAOMAN,
            nickname="小满",
            content=f"[@id:{OTHER_BOT};nickname:晴音@] 周四一起去食堂吗",
        )
        consolidator = EpisodicConsolidator(0)
        await consolidator._load_channel(CHAT)  # noqa: SLF001
        consolidator.persona_context = PersonaContext(bot_sender_id="-1", persona_name="助手", persona_brief="")
        messages = list(await DBChatMessage.filter(chat_key=CHAT).order_by("id"))
        await consolidator._prepare_batch_participants(messages)  # noqa: SLF001
        text = consolidator._build_conversation_text(messages)  # noqa: SLF001

        assert "晴音〔他 Bot〕: 我住在学生公寓，周四有课" in text
        assert "小满〔P1〕: @晴音〔他 Bot〕 周四一起去食堂吗" in text
        assert set(consolidator.batch_participant_codes.values()) == {XIAOMAN}, "别的 Bot 不分代号，进不了 persons"
        assert OTHER_BOT not in text

    async def test_marker_is_scrubbed_before_storage(
        self,
        quality_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del quality_env
        await _other_instances()
        await _channel(CHAT)
        _install_persona(monkeypatch)
        await _message(CHAT, index=1, account=OTHER_BOT, nickname="晴音", content="今天也要元气满满")
        await _message(CHAT, index=2, account=XIAOMAN, nickname="小满", content="晴音周四陪我去食堂")
        await _run(CHAT, monkeypatch, _item("小满约晴音〔他 Bot〕周四去食堂", persons=["P1"]))

        [row] = await DBMemParagraph.all()
        assert row.content == "小满约晴音周四去食堂"


class TestPromptGuidance:
    @pytest.mark.parametrize("chat_type", ["group", "private"])
    async def test_knowledge_type_guidance_and_other_bot_rule(
        self,
        quality_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
        chat_type: str,
    ) -> None:
        del quality_env
        chat_key = f"onebot_v11-quality-{chat_type}_guide"
        await _channel(chat_key, chat_type=chat_type)
        _install_persona(monkeypatch)
        await _message(chat_key, index=1, account=XIAOMAN, nickname="小满", content="随便聊聊", chat_type=chat_type)
        prompts = _install_llm(monkeypatch, lambda _p: json.dumps({"memories": []}))
        await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)
        prompt = prompts[0]

        assert "preference、fact、decision 只用于关于人的稳定信息" in prompt
        assert "时效内容照样可以记" in prompt, "讲清类型，不是叫模型少记"
        assert ("〔他 Bot〕" in prompt) is (chat_type == "group"), "私聊里只有对方与 Bot，没有别的 Bot"
        assert ("角色扮演台词" in prompt) is (chat_type == "group")
