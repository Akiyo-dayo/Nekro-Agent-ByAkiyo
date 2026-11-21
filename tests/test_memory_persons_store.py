"""「涉及的人」的存储层函数：账号筛选、代号、私聊对象、关联行与 payload。"""

from __future__ import annotations

from typing import Any

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.services.memory import persons as persons_module
from nekro_agent.services.memory.persons import (
    assign_participant_codes,
    human_accounts,
    load_person_accounts,
    replace_paragraph_persons,
    resolve_bot_account,
    resolve_private_counterpart,
)


@pytest.fixture
async def memory_db() -> Any:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def memory_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)


def _message(sender_id: str, account: str) -> DBChatMessage:
    """只在内存里用的消息对象，不落库"""
    return DBChatMessage(sender_id=sender_id, platform_userid=account)


async def _stored_message(
    chat_key: str,
    *,
    sender_id: str,
    account: str,
    index: int,
) -> DBChatMessage:
    return await DBChatMessage.create(
        sender_id=sender_id,
        sender_name=f"用户{index}",
        sender_nickname=f"用户{index}",
        is_tome=0,
        is_recalled=False,
        adapter_key="onebot_v11",
        instance_key="inst",
        message_id=f"m{index}",
        chat_key=chat_key,
        chat_type="private",
        platform_userid=account,
        content_text="内容",
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=index,
    )


async def _paragraph(content: str = "一条记忆") -> DBMemParagraph:
    return await DBMemParagraph.create(
        workspace_id=0,
        instance_key="inst",
        adapter_key="onebot_v11",
        origin_chat_key="onebot_v11-inst-group_1",
        origin_chat_type="group",
        memory_source="na",
        cognitive_type="episodic",
        knowledge_type="fact",
        content=content,
        summary=content,
        origin_kind="consolidation",
    )


class TestHumanAccounts:
    async def test_excludes_bot_blank_and_the_bot_own_account(self, memory_db: None) -> None:
        messages = [
            _message("-1", "ubot"),
            _message("u151", "u151"),
            _message("uxw", "uxw"),
            _message("u151", "u151"),
            _message("uxh", "   "),
            _message("ubot", "ubot"),
            _message("uxh", "uxh"),
        ]

        assert human_accounts(messages, bot_account="ubot") == ["u151", "uxw", "uxh"]

    async def test_without_a_bot_account_only_skips_bot_rows_and_blanks(self, memory_db: None) -> None:
        messages = [_message("-1", "ubot"), _message("ubot", "ubot"), _message("u151", "u151")]

        assert human_accounts(messages, bot_account=None) == ["ubot", "u151"]

    async def test_assigns_codes_in_input_order(self, memory_db: None) -> None:
        assert assign_participant_codes(["u151", "uxw", "uxh"]) == {
            "P1": "u151",
            "P2": "uxw",
            "P3": "uxh",
        }


class TestResolveBotAccount:
    async def test_returns_the_instance_account(self, memory_db: None) -> None:
        await DBAdapterInstance.create(
            adapter_key="onebot_v11",
            instance_key="inst",
            provider_account_id="ubot",
        )

        assert await resolve_bot_account("onebot_v11", "inst") == "ubot"

    async def test_blank_or_missing_is_none(self, memory_db: None) -> None:
        await DBAdapterInstance.create(
            adapter_key="onebot_v11",
            instance_key="blank",
            provider_account_id="   ",
        )

        assert await resolve_bot_account("onebot_v11", "blank") is None
        assert await resolve_bot_account("onebot_v11", "missing") is None


class TestResolvePrivateCounterpart:
    async def test_batch_with_exactly_one_human(self, memory_db: None) -> None:
        await DBAdapterInstance.create(
            adapter_key="onebot_v11",
            instance_key="inst",
            provider_account_id="ubot",
        )
        batch = [_message("-1", "ubot"), _message("uxw", "uxw"), _message("ubot", "ubot")]

        counterpart = await resolve_private_counterpart(
            "onebot_v11-inst-private_1",
            adapter_key="onebot_v11",
            instance_key="inst",
            batch=batch,
        )

        assert counterpart == "uxw"

    async def test_batch_with_two_humans_warns_and_returns_none(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await DBAdapterInstance.create(
            adapter_key="onebot_v11",
            instance_key="inst",
            provider_account_id="ubot",
        )
        warnings: list[str] = []

        def fake_warning(message: str, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            warnings.append(message)

        monkeypatch.setattr(persons_module.logger, "warning", fake_warning)
        batch = [_message("u151", "u151"), _message("uxw", "uxw")]

        counterpart = await resolve_private_counterpart(
            "onebot_v11-inst-private_2",
            adapter_key="onebot_v11",
            instance_key="inst",
            batch=batch,
        )

        assert counterpart is None
        assert len(warnings) == 1
        assert "多个" in warnings[0]

    async def test_empty_batch_falls_back_to_the_latest_human_message(self, memory_db: None) -> None:
        await DBAdapterInstance.create(
            adapter_key="onebot_v11",
            instance_key="inst",
            provider_account_id="ubot",
        )
        chat_key = "onebot_v11-inst-private_3"
        await _stored_message(chat_key, sender_id="u151", account="u151", index=1)
        await _stored_message(chat_key, sender_id="uxw", account="uxw", index=2)
        await _stored_message(chat_key, sender_id="-1", account="ubot", index=3)
        await _stored_message(chat_key, sender_id="ubot", account="ubot", index=4)
        await _stored_message(chat_key, sender_id="uxh", account="   ", index=5)

        counterpart = await resolve_private_counterpart(
            chat_key,
            adapter_key="onebot_v11",
            instance_key="inst",
            batch=[],
        )

        assert counterpart == "uxw"

    async def test_only_bot_accounts_means_no_counterpart(self, memory_db: None) -> None:
        await DBAdapterInstance.create(
            adapter_key="onebot_v11",
            instance_key="inst",
            provider_account_id="ubot",
        )
        chat_key = "onebot_v11-inst-private_4"
        await _stored_message(chat_key, sender_id="-1", account="ubot", index=1)
        await _stored_message(chat_key, sender_id="ubot", account="ubot", index=2)

        assert (
            await resolve_private_counterpart(
                chat_key,
                adapter_key="onebot_v11",
                instance_key="inst",
            )
            is None
        )


class TestPersonRows:
    async def test_replace_dedupes_sorts_and_overwrites(self, memory_db: None) -> None:
        paragraph = await _paragraph()

        written = await replace_paragraph_persons(paragraph.id, ["uxh", "u151", " uxw ", "u151", " "])
        assert written == ["u151", "uxh", "uxw"]
        assert (await load_person_accounts([paragraph.id]))[paragraph.id] == ["u151", "uxh", "uxw"]

        written = await replace_paragraph_persons(paragraph.id, ["uxw"])
        assert written == ["uxw"]
        assert (await load_person_accounts([paragraph.id]))[paragraph.id] == ["uxw"]

    async def test_load_has_an_entry_for_every_id(self, memory_db: None) -> None:
        with_people = await _paragraph("有人的段落")
        without_people = await _paragraph("没有人的段落")
        await replace_paragraph_persons(with_people.id, ["uxw", "u151"])

        loaded = await load_person_accounts([with_people.id, without_people.id, 999])

        assert set(loaded) == {with_people.id, without_people.id, 999}
        assert loaded[with_people.id] == ["u151", "uxw"]
        assert loaded[without_people.id] == []
        assert loaded[999] == []


class TestPayload:
    async def test_person_accounts_are_sorted_and_deduped(self, memory_db: None) -> None:
        paragraph = await _paragraph()

        payload = paragraph.to_qdrant_payload(person_accounts=["uxw", "u151", "uxw"])

        assert payload["person_accounts"] == ["u151", "uxw"]

    async def test_default_is_empty(self, memory_db: None) -> None:
        paragraph = await _paragraph()

        assert paragraph.to_qdrant_payload()["person_accounts"] == []
