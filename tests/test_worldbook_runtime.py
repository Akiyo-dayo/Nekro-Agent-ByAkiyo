"""世界书运行时：每轮求值、缓存、抑制、定时状态与三处注入的文字落点。

前半部分跑在真表（SQLite 内存库）上，直接调服务层函数，不起 Qdrant、不调模型；后半部分
（渲染、系统提示词、历史消息）是纯函数与模板层，用固定的条目行拼出来比对完整字符串。

要钉住的事：

1. 闸关时不发任何世界书 / 消息查询——关掉世界书必须能从查询上看得出来；
2. 一轮只解析一次（同一个 `code` 第二次不查库），且同一轮多次渲染注入同一批条目；
3. 预算按频道生效配置的字数上限算，被跳过的条目进 `dropped`，且不设锚点；
4. 下一轮抑制去掉不可信条目、把让出来的预算给宿主条目；
5. 定时状态只在结局 `ok` 的轮次提交（sticky / cooldown / delay）；
6. 渲染格式：宿主组在前、租户组包在不可信块里，深度注入的落点与顺序；
7. 闸关、以及开闸但库里没书时，提示词与改造前逐字相同。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, AsyncIterator, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.services.affection.suppression import (
    lift_narrative_suppression,
    suppress_narrative,
)
from nekro_agent.services.worldbook import matcher, runtime, selection
from nekro_agent.services.worldbook.timed_state import timed_state_store
from nekro_agent.services.worldbook.types import MatchEntry, Position

ADAPTER = "onebot_v11"
CHAT_KEY = f"{ADAPTER}-t1a-group_1"
INSTANCE_KEY = "t1a"
BOT_ACCOUNT = "bot_t1a"
PRESET_NAME = "租户一甲人设"

# 消息时间戳取「刚刚过去」的固定偏移：`load_window` 的窗口下限是
# `max(now - 过期秒数, 频道起始时间)`，用 1970 年的常量会被下限整个过滤掉（那些消息在
# 真实部署里也确实早就不在历史里了）。用例把过期秒数放大到 10 亿秒（约 31 年），
# 下限因此只由频道起始时间决定。
BASE_TIMESTAMP = int(time.time()) - 5
# 频道的对话起始时间：必须早于所有测试消息，否则窗口下限会把它们全过滤掉。
WINDOW_START_TIMESTAMP = BASE_TIMESTAMP - 10


def wb_config(*, enabled: bool = True, max_chars: int = 3000) -> SimpleNamespace:
    """一个「频道生效配置」的最小替身：世界书只读这两项"""
    return SimpleNamespace(WORLDBOOK_ENABLED=enabled, WORLDBOOK_MAX_CHARS=max_chars)


@dataclass
class World:
    channel: DBChatChannel
    preset: DBPreset
    books: dict[str, DBWbBook]
    entries: dict[str, DBWbEntry]


@pytest.fixture(autouse=True)
def _clean_round_state() -> AsyncIterator[None]:
    """每条用例前后清空进程内的轮次缓存与定时状态：它们是模块级单例，会跨用例残留"""
    runtime._rounds.clear()  # noqa: SLF001
    timed_state_store.clear()
    lift_narrative_suppression(CHAT_KEY)
    yield
    runtime._rounds.clear()  # noqa: SLF001
    timed_state_store.clear()
    lift_narrative_suppression(CHAT_KEY)


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    """一个租户实例 + 一个频道 + 一个域书（两个条目）

    每个用例自己往 `world.books` / `world.entries` 里加东西：基线只放一条常驻宿主条目，
    这样「开闸有书有命中」与「开闸无书」两种形态都能从同一份夹具派生。
    """
    monkeypatch.setattr(config, "MEMORY_INSTANCE_SHARING_GROUPS", {})
    # `load_window` 用 `AI_CHAT_CONTEXT_EXPIRE_SECONDS` 当下限；放大到能覆盖测试消息。
    monkeypatch.setattr(config, "AI_CHAT_CONTEXT_EXPIRE_SECONDS", 10**9)

    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE_KEY,
            display_name="租户一甲",
            tenant_id=1,
            provider_account_id=BOT_ACCOUNT,
        )
        preset = await DBPreset.create(
            name=PRESET_NAME,
            title=PRESET_NAME,
            avatar="",
            content="人设正文",
            description="",
            tags="",
            author="test",
            owner_instance_key=INSTANCE_KEY,
        )
        channel = await DBChatChannel.create(
            chat_key=CHAT_KEY,
            adapter_key=ADAPTER,
            instance_key=INSTANCE_KEY,
            channel_id="group_1",
            channel_name="租户一甲群",
            channel_type="group",
            preset_id=preset.id,
            data="{}",
            workspace_id=None,
        )
        # 频道的对话起始时间由 `auto_now_add` 写成"现在"，那样固定时间戳的消息会落在
        # 窗口下限之外、一条都读不到。这里把它挪到测试消息之前，窗口的下限就只由
        # `AI_CHAT_CONTEXT_EXPIRE_SECONDS` 决定。
        await DBChatChannel.filter(id=channel.id).update(
            conversation_start_time=datetime.fromtimestamp(WINDOW_START_TIMESTAMP, tz=timezone.utc),
        )
        await channel.refresh_from_db()
        book = await _book("域书", scope_kind="domain")
        entry = await _entry(book, uid=0, constant=True, content="常驻正文", position="after_persona")
        yield World(channel=channel, preset=preset, books={"domain": book}, entries={"constant": entry})
    finally:
        await Tortoise.close_connections()


async def _book(
    name: str,
    *,
    scope_kind: str = "domain",
    preset_id: int = 0,
    chat_key: str = "",
    person_account: str = "",
    enabled: bool = True,
    scan_depth: int = 4,
    budget_chars: int = 0,
) -> DBWbBook:
    return await DBWbBook.create(
        name=name,
        description="",
        scope_kind=scope_kind,
        adapter_key=ADAPTER,
        instance_key=INSTANCE_KEY,
        preset_id=preset_id,
        chat_key=chat_key,
        person_account=person_account,
        enabled=enabled,
        scan_depth=scan_depth,
        budget_chars=budget_chars,
    )


async def _entry(
    book: DBWbBook,
    *,
    uid: int,
    keys: tuple[str, ...] = (),
    content: str = "正文",
    constant: bool = False,
    order: int = 100,
    position: str = "before_history",
    depth: int = 4,
    scan_depth: Optional[int] = None,
    trusted: bool = False,
    enabled: bool = True,
    status: str = "active",
    sticky: Optional[int] = None,
    cooldown: Optional[int] = None,
    delay: Optional[int] = None,
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
        order=order,
        position=position,
        depth=depth,
        scan_depth=scan_depth,
        sticky=sticky,
        cooldown=cooldown,
        delay=delay,
        enabled=enabled,
        trusted=trusted,
        status=status,
        ext={},
    )


async def _message(
    content: str,
    *,
    account: str = "u1",
    sender_id: str = "u1",
    chat_key: str = CHAT_KEY,
    send_timestamp: int = BASE_TIMESTAMP,
) -> DBChatMessage:
    return await DBChatMessage.create(
        sender_id=sender_id,
        sender_name=account,
        sender_nickname=account,
        is_tome=0,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key=INSTANCE_KEY,
        message_id=f"m-{content[:6]}-{send_timestamp}",
        chat_key=chat_key,
        chat_type="group",
        platform_userid=account,
        content_text=content,
        content_data="{}",
        raw_cq_code="",
        ext_data="",
        send_timestamp=send_timestamp,
    )


def _group_ids(groups: selection.RenderGroups) -> set[int]:
    ids = {hit.entry.id for hit in groups.after_persona}
    ids |= {hit.entry.id for hit in groups.before_history}
    for items in groups.at_depth.values():
        ids |= {hit.entry.id for hit in items}
    return ids


class TestEvaluateGateOff:
    """闸关：一次世界书查询、一次消息查询都不发"""

    async def test_disabled_round_queries_nothing(
        self,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def forbidden_window(_channel: DBChatChannel, **_kwargs: Any) -> list[Any]:
            raise AssertionError("闸关时不该查消息")

        monkeypatch.setattr(runtime, "load_window", forbidden_window)

        result = await runtime.resolve_round("code-off", world.channel, wb_config(enabled=False))

        assert result.state == "disabled"
        assert result.groups.empty
        assert result.dropped == ()
        assert runtime.cached_round("code-off") is result

    async def test_disabled_round_does_not_touch_worldbook_tables(
        self,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[str] = []

        def forbidden_condition(*_args: Any, **_kwargs: Any) -> Any:
            calls.append("worldbook_condition")
            raise AssertionError("闸关时不该算世界书范围条件")

        monkeypatch.setattr(runtime, "worldbook_condition", forbidden_condition)

        result = await runtime.resolve_round("code-off-2", world.channel, wb_config(enabled=False))

        assert result.state == "disabled"
        assert calls == []


class TestEvaluateStates:
    """三种正常状态：没有书 / 有命中 / 出错"""

    async def test_no_books_means_none(self, world: World) -> None:
        # 基线那条宿主常驻条目先停用：这一轮既没有书命中，也没有被抑制的条目。
        await DBWbEntry.all().update(enabled=False)
        await _message("随便说点什么")

        result = await runtime.resolve_round("code-none", world.channel, wb_config())

        assert result.state == "none"
        assert result.groups.empty
        assert result.dropped == ()

    async def test_hit_with_no_books_at_all_is_none(self, world: World) -> None:
        await DBWbBook.all().update(enabled=False)
        await _message("随便说点什么")

        result = await runtime.resolve_round("code-none-2", world.channel, wb_config())

        assert result.state == "none"

    async def test_hit_goes_into_the_matching_group(self, world: World) -> None:
        await _message("随便说点什么")

        result = await runtime.resolve_round("code-injected", world.channel, wb_config())

        assert result.state == "injected"
        assert _group_ids(result.groups) == {world.entries["constant"].id}

    async def test_cache_hit_does_not_query_again(
        self,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _message("随便说点什么")
        first = await runtime.resolve_round("code-cache", world.channel, wb_config())

        async def forbidden_window(_channel: DBChatChannel, **_kwargs: Any) -> list[Any]:
            raise AssertionError("同一个 code 第二次不该再查库")

        monkeypatch.setattr(runtime, "load_window", forbidden_window)
        second = await runtime.resolve_round("code-cache", world.channel, wb_config())

        assert second is first

    async def test_channel_config_budget_is_used(self, world: World) -> None:
        # 生效配置的字数上限比条目正文还短：整条被跳过，并记下原因。
        await _message("随便说点什么")

        result = await runtime.resolve_round("code-budget", world.channel, wb_config(max_chars=1))

        assert result.state == "none"
        assert result.groups.empty
        assert result.dropped == (
            (world.entries["constant"].id, world.books["domain"].id, len("常驻正文"), "global_chars"),
        )

    async def test_failure_returns_error_state(
        self,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def boom(_channel: DBChatChannel, **_kwargs: Any) -> list[Any]:
            raise RuntimeError("数据库挂了")

        monkeypatch.setattr(runtime, "load_window", boom)

        result = await runtime.resolve_round("code-error", world.channel, wb_config())

        assert result.state == "error"
        assert result.groups.empty
        assert runtime.cached_round("code-error") is result


class TestSuppression:
    """被拦的下一轮：不可信条目不进分组，宿主条目照常，让出来的预算给宿主条目"""

    async def test_tenant_entry_is_skipped_host_entry_stays(self, world: World) -> None:
        host = await _entry(world.books["domain"], uid=1, keys=("龙",), content="宿主正文", trusted=True)
        tenant = await _entry(world.books["domain"], uid=2, keys=("龙",), content="租户正文", trusted=False)
        await _message("龙来了")

        with_tenant = await runtime.resolve_round("code-sup-a", world.channel, wb_config())
        assert {host.id, tenant.id} <= _group_ids(with_tenant.groups)

        suppress_narrative(CHAT_KEY)
        suppressed = await runtime.resolve_round("code-sup-b", world.channel, wb_config())

        assert tenant.id not in _group_ids(suppressed.groups)
        assert host.id in _group_ids(suppressed.groups)

    async def test_only_tenant_entries_means_suppressed(self, world: World) -> None:
        # 基线那条宿主常驻条目停用，只留一条租户关键词条目。
        await DBWbEntry.all().update(enabled=False)
        await _entry(world.books["domain"], uid=3, keys=("龙",), content="租户正文", trusted=False)
        await _message("龙来了")
        suppress_narrative(CHAT_KEY)

        result = await runtime.resolve_round("code-sup-c", world.channel, wb_config())

        assert result.state == "suppressed"
        assert result.groups.empty

    async def test_freed_budget_goes_to_the_host_entry(self, world: World) -> None:
        # 两条都命中、预算只够一条；租户条目 `order` 更大，正常时先占预算。
        await DBWbEntry.all().update(enabled=False)
        host = await _entry(world.books["domain"], uid=4, keys=("龙",), content="宿主正文", order=10, trusted=True)
        tenant = await _entry(world.books["domain"], uid=5, keys=("龙",), content="租户正文", order=90, trusted=False)
        await _message("龙来了")

        budget = wb_config(max_chars=len("租户正文"))
        normal = await runtime.resolve_round("code-sup-d", world.channel, budget)
        assert _group_ids(normal.groups) == {tenant.id}
        assert normal.dropped == ((host.id, host.book_id, len("宿主正文"), "global_chars"),)

        suppress_narrative(CHAT_KEY)
        suppressed = await runtime.resolve_round("code-sup-e", world.channel, budget)
        assert _group_ids(suppressed.groups) == {host.id}


class TestTimedState:
    """定时效果：只在 `outcome == "ok"` 的轮次提交锚点"""

    async def test_sticky_survives_one_message_after_commit(self, world: World) -> None:
        await DBWbEntry.all().update(enabled=False)
        sticky = await _entry(world.books["domain"], uid=6, keys=("龙",), content="粘住的正文", sticky=2)
        await _message("龙来了")

        first = await runtime.resolve_round("code-sticky-1", world.channel, wb_config())
        assert _group_ids(first.groups) == {sticky.id}
        runtime.finish_round("code-sticky-1", "ok")

        # 第二轮只留一条不含关键词的新消息：命中只可能来自 sticky。
        await DBChatMessage.all().delete()
        await _message("风马牛不相及")
        second = await runtime.resolve_round("code-sticky-2", world.channel, wb_config())

        assert _group_ids(second.groups) == {sticky.id}
        assert {hit.reason.kind for hit in second.groups.before_history} == {"sticky"}

    async def test_sticky_does_not_start_after_a_blocked_round(self, world: World) -> None:
        await DBWbEntry.all().update(enabled=False)
        sticky = await _entry(world.books["domain"], uid=7, keys=("龙",), content="粘住的正文", sticky=2)
        await _message("龙来了")

        first = await runtime.resolve_round("code-sticky-b1", world.channel, wb_config())
        assert _group_ids(first.groups) == {sticky.id}
        runtime.finish_round("code-sticky-b1", "blocked")

        await DBChatMessage.all().delete()
        await _message("风马牛不相及")
        second = await runtime.resolve_round("code-sticky-b2", world.channel, wb_config())

        assert second.groups.empty

    async def test_cooldown_blocks_the_next_rounds(self, world: World) -> None:
        await DBWbEntry.all().update(enabled=False)
        cooled = await _entry(world.books["domain"], uid=8, keys=("龙",), content="冷却的正文", cooldown=3)
        await _message("龙来了")

        first = await runtime.resolve_round("code-cd-1", world.channel, wb_config())
        assert _group_ids(first.groups) == {cooled.id}
        runtime.finish_round("code-cd-1", "ok")

        await DBChatMessage.all().delete()
        await _message("龙又来了")
        second = await runtime.resolve_round("code-cd-2", world.channel, wb_config())

        assert second.groups.empty

    async def test_delay_counts_the_whole_channel_not_the_window(self, world: World) -> None:
        """`delay` 按频道总条数判：窗口时间下限之外的旧消息也算"""
        await DBWbEntry.all().update(enabled=False)
        delayed = await _entry(world.books["domain"], uid=9, constant=True, content="延迟的正文", delay=3)
        # 三条旧消息落在窗口下限之外（`load_window` 的时间下限会过滤掉它们），
        # 但 `_channel_count` 不加时间下限，因此它们仍然算进「频道一共几条」。
        await _message("很久以前一", send_timestamp=1)
        await _message("很久以前二", send_timestamp=2)
        await _message("最近的一条", send_timestamp=BASE_TIMESTAMP)

        result = await runtime.resolve_round("code-delay", world.channel, wb_config())

        assert result.state == "injected"
        assert _group_ids(result.groups) == {delayed.id}

    async def test_dropped_entry_gets_no_anchor(self, world: World) -> None:
        await DBWbEntry.all().update(enabled=False)
        skipped = await _entry(world.books["domain"], uid=10, keys=("龙",), content="太长的正文" * 10, sticky=5)
        await _message("龙来了")

        result = await runtime.resolve_round("code-anchor", world.channel, wb_config(max_chars=1))
        runtime.finish_round("code-anchor", "ok")

        assert result.state == "none"
        assert timed_state_store.get(CHAT_KEY, skipped.id) is None

    async def test_anchor_is_the_newest_window_message(self, world: World) -> None:
        # 关键词在最早那条里，锚点却要落在窗口里最新的一条上：「过了几条」从模型看到的
        # 最后一条往后数，落在更早的消息上会让 sticky 提前结束、cooldown 提前放开。
        await DBWbEntry.all().update(enabled=False)
        sticky = await _entry(world.books["domain"], uid=12, keys=("龙",), content="粘住的正文", sticky=2)
        await _message("龙来了", send_timestamp=BASE_TIMESTAMP - 2)
        await _message("又一条", send_timestamp=BASE_TIMESTAMP - 1)
        newest = await _message("最新一条", send_timestamp=BASE_TIMESTAMP)

        await runtime.resolve_round("code-anchor-newest", world.channel, wb_config())
        runtime.finish_round("code-anchor-newest", "ok")

        assert timed_state_store.get(CHAT_KEY, sticky.id) == newest.id


class TestWindow:
    """扫描窗口：时间下限与历史消息用同一份（频道生效）配置，结果按 id 升序"""

    async def test_expired_message_does_not_trigger(self, world: World) -> None:
        # 频道起始时间挪到很早，下限只由过期秒数决定。全局过期秒数被夹具放大到 10 亿秒，
        # 频道生效配置里是 1 小时：两小时前的关键词消息只该被后者挡掉——读全局值的话它会触发。
        await DBChatChannel.filter(id=world.channel.id).update(
            conversation_start_time=datetime.fromtimestamp(BASE_TIMESTAMP - 100_000, tz=timezone.utc),
        )
        await world.channel.refresh_from_db()
        await DBWbEntry.all().update(enabled=False)
        keyed = await _entry(world.books["domain"], uid=11, keys=("龙",), content="龙的设定")
        await _message("龙来了", send_timestamp=BASE_TIMESTAMP - 7200)

        short = SimpleNamespace(WORLDBOOK_ENABLED=True, WORLDBOOK_MAX_CHARS=3000, AI_CHAT_CONTEXT_EXPIRE_SECONDS=3600)
        expired = await runtime.resolve_round("code-expired", world.channel, short)
        assert expired.state == "none"

        long = SimpleNamespace(WORLDBOOK_ENABLED=True, WORLDBOOK_MAX_CHARS=3000, AI_CHAT_CONTEXT_EXPIRE_SECONDS=10**9)
        fresh = await runtime.resolve_round("code-fresh", world.channel, long)
        assert _group_ids(fresh.groups) == {keyed.id}

    async def test_window_is_in_ascending_id_order(self, world: World) -> None:
        first = await _message("第一条", send_timestamp=BASE_TIMESTAMP - 2)
        second = await _message("第二条", send_timestamp=BASE_TIMESTAMP - 1)
        third = await _message("第三条", send_timestamp=BASE_TIMESTAMP)

        window = await runtime.load_window(world.channel)

        assert [message.id for message in window] == [first.id, second.id, third.id]


class TestRunAgentSettlesTheRound:
    """对话主链路的收尾：定时锚点跟着轮次结局走，缓存一律弹出"""

    @pytest.mark.parametrize(("mode", "committed"), [("ok", True), ("refused", False), ("failed", False)])
    async def test_timed_anchor_follows_the_round_outcome(
        self,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
        committed: bool,
    ) -> None:
        from nekro_agent.services.agent import run_agent as run_agent_module
        from nekro_agent.services.memory import prompt_rounds

        code = f"code-settle-{mode}"

        async def fake_inner(**kwargs: Any) -> None:
            # 真实链路里 `resolve_round` 暂存锚点、写缓存；这里直接摆出同样的状态。
            timed_state_store.stage(str(kwargs["one_time_code"]), CHAT_KEY, 42, 7)
            runtime._rounds[str(kwargs["one_time_code"])] = runtime.RoundWorldbook(  # noqa: SLF001
                state="injected",
                groups=selection.RenderGroups(),
            )
            if mode == "refused":
                raise run_agent_module.AgentProviderRefusalError("refused")
            if mode == "failed":
                raise RuntimeError("boom")

        monkeypatch.setattr(run_agent_module, "_run_agent_in_instance_scope", fake_inner)
        kwargs: dict[str, Any] = {
            "chat_key": CHAT_KEY,
            "chat_message": None,
            "ctx": SimpleNamespace(),
            "db_chat_channel": world.channel,
            "one_time_code": code,
        }
        try:
            if mode == "ok":
                await run_agent_module._run_agent_with_affection_suppression(**kwargs)  # noqa: SLF001
            else:
                with pytest.raises((run_agent_module.AgentProviderRefusalError, RuntimeError)):
                    await run_agent_module._run_agent_with_affection_suppression(**kwargs)  # noqa: SLF001
        finally:
            await prompt_rounds.drain()

        assert (timed_state_store.get(CHAT_KEY, 42) == 7) is committed
        assert runtime.cached_round(code) is None


def _match_entry(**overrides: Any) -> MatchEntry:
    """一个只带判据字段的引擎条目：渲染与分组的用例不需要 ORM 行"""
    defaults: dict[str, Any] = {
        "id": 1,
        "book_id": 1,
        "keys": ("龙",),
        "secondary_keys": (),
        "selective_logic": None,
        "use_regex": False,
        "constant": False,
        "content": "正文",
        "order": 100,
        "position": Position.BEFORE_HISTORY,
        "depth": 4,
        "scan_depth": 4,
        "case_sensitive": False,
        "match_whole_words": False,
        "sticky": 0,
        "cooldown": 0,
        "delay": 0,
        "trusted": False,
        "invalid_keys": (),
    }
    defaults.update(overrides)
    return MatchEntry(**defaults)


def _hit(entry_id: int, *, content: str = "正文", trusted: bool = False, order: int = 100, position: Position = Position.BEFORE_HISTORY, depth: int = 4) -> selection.Hit:
    return selection.Hit(
        entry=_match_entry(
            id=entry_id,
            book_id=1,
            content=content,
            trusted=trusted,
            order=order,
            position=position,
            depth=depth,
        ),
        reason=matcher.HitReason(kind="key", key="龙"),
    )
