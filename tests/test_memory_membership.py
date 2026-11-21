"""群成员判定与别名展开：窗口边界、缓存、以及合并 / 撤销后的即时生效。

成员集是「相关」档的判据，它允许进程内缓存原始账号集合；别名展开则必须每次现算，
否则撤销合并之后的一段时间里记忆会继续挂在错误的人名下。
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_identity import DBIdentityAlias
from nekro_agent.services.identity_scope import resolve_memory_domain
from nekro_agent.services.memory.persons import (
    BOT_SENDER_ID,
    MEMBER_CACHE_TTL_SECONDS,
    active_member_accounts,
    expand_accounts,
    reset_member_cache,
)

CHAT_KEY = "onebot_v11-inst-group_151"
BOT_ACCOUNT = "ubot"
ADAPTER = "onebot_v11"
INSTANCE = "inst"


@pytest.fixture
async def memory_db() -> Any:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    reset_member_cache()
    yield
    reset_member_cache()


async def _seed_bot() -> None:
    await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        provider_account_id=BOT_ACCOUNT,
    )


async def _message(
    *,
    index: int,
    account: str,
    seconds_ago: int,
    sender_id: str | None = None,
    chat_key: str = CHAT_KEY,
) -> DBChatMessage:
    return await DBChatMessage.create(
        sender_id=sender_id if sender_id is not None else account,
        sender_name=account or "空账号",
        sender_nickname=account or "空账号",
        is_tome=0,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        message_id=f"m{index}",
        chat_key=chat_key,
        chat_type="group",
        platform_userid=account,
        content_text="一条消息",
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=int(time.time()) - seconds_ago,
    )


class TestActiveMemberAccounts:
    async def test_window_boundary_and_account_filtering(self, memory_db: None) -> None:
        await _seed_bot()
        five_days = 5 * 86400
        thirty_one_days = 31 * 86400
        await _message(index=1, account="u151", seconds_ago=five_days)
        await _message(index=2, account="u151b", seconds_ago=thirty_one_days)
        # Bot 自己的发言（sender_id = -1）
        await _message(index=3, account=BOT_ACCOUNT, seconds_ago=60, sender_id=BOT_SENDER_ID)
        # Bot 账号被别人以普通用户身份发出
        await _message(index=4, account=BOT_ACCOUNT, seconds_ago=60)
        # 空账号与纯空白账号
        await _message(index=5, account="", seconds_ago=60)
        await _message(index=6, account="   ", seconds_ago=60)

        accounts = await active_member_accounts(CHAT_KEY, days=30, bot_account=BOT_ACCOUNT)

        assert accounts == frozenset({"u151"})
        assert "u151b" not in accounts, "31 天前发过言的人不在 30 天窗口里"
        assert BOT_ACCOUNT not in accounts, "Bot 自己的号不算群成员"

    async def test_only_the_given_chat_is_counted(self, memory_db: None) -> None:
        await _seed_bot()
        await _message(index=1, account="u151", seconds_ago=60)
        await _message(index=2, account="uxw", seconds_ago=60, chat_key="other-chat")

        accounts = await active_member_accounts(CHAT_KEY, days=30, bot_account=BOT_ACCOUNT)

        assert accounts == frozenset({"u151"})

    async def test_cache_avoids_a_second_query_until_reset(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_bot()
        await _message(index=1, account="u151", seconds_ago=60)

        calls: list[str] = []
        original_filter = DBChatMessage.filter

        def counting_filter(*args: Any, **kwargs: Any) -> Any:
            calls.append(CHAT_KEY)
            return original_filter(*args, **kwargs)

        monkeypatch.setattr(DBChatMessage, "filter", counting_filter)

        first = await active_member_accounts(CHAT_KEY, days=30, bot_account=BOT_ACCOUNT)
        second = await active_member_accounts(CHAT_KEY, days=30, bot_account=BOT_ACCOUNT)

        assert first == second == frozenset({"u151"})
        assert len(calls) == 1, "300 秒内同一键的第二次调用不应再查库"
        assert MEMBER_CACHE_TTL_SECONDS == 300

        reset_member_cache()
        third = await active_member_accounts(CHAT_KEY, days=30, bot_account=BOT_ACCOUNT)

        assert third == frozenset({"u151"})
        assert len(calls) == 2, "reset 之后必须重新查库"


class TestExpandAccounts:
    async def test_merge_adds_aliases_and_revoke_takes_effect_immediately(
        self,
        memory_db: None,
    ) -> None:
        domain = await resolve_memory_domain(ADAPTER, INSTANCE)

        before = await expand_accounts(domain, ["u151b"])
        assert before == frozenset({"u151b"})

        await DBIdentityAlias.create(
            domain_id=domain.domain_id,
            platform_userid="u151b",
            person_uid="u151",
        )
        await DBIdentityAlias.create(
            domain_id=domain.domain_id,
            platform_userid="u151c",
            person_uid="u151",
        )

        merged = await expand_accounts(domain, ["u151b"])
        assert merged == frozenset({"u151", "u151b", "u151c"})

        # 撤销合并：只删别名行，不清任何缓存
        await DBIdentityAlias.filter(platform_userid="u151b").delete()
        await DBIdentityAlias.filter(platform_userid="u151c").delete()

        revoked = await expand_accounts(domain, ["u151b"])
        assert revoked == frozenset({"u151b"}), "撤销合并必须在下一次调用就生效"

    async def test_empty_input_short_circuits(self, memory_db: None) -> None:
        domain = await resolve_memory_domain(ADAPTER, INSTANCE)
        assert await expand_accounts(domain, []) == frozenset()
        assert await expand_accounts(domain, ["", "   "]) == frozenset()
