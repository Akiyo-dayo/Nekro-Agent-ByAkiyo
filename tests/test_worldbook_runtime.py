"""世界书运行时：每轮求值、缓存、抑制、定时状态与三处注入的文字落点。

前半部分跑在真表（SQLite 内存库）上，直接调服务层函数，不起 Qdrant、不调模型；后半部分
（渲染、系统提示词、历史消息）是纯函数与模板层，用固定的条目行拼出来比对完整字符串。

要钉住的事：

1. 闸关时不发任何世界书 / 消息查询——关掉世界书必须能从查询上看得出来；
2. 一轮只解析一次（同一个 `code` 第二次不查库），且同一轮多次渲染注入同一批条目；
3. 预算按频道生效配置的 token 预算算（百分比、上限、主模型组），被跳过的条目进 `dropped`，且不设锚点；
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
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services.affection.suppression import (
    lift_narrative_suppression,
    suppress_narrative,
)
from nekro_agent.services.worldbook import matcher, runtime, selection, tokens
from nekro_agent.services.worldbook import purpose as wb_purpose
from nekro_agent.services.worldbook.timed_state import timed_state_store
from nekro_agent.services.worldbook.types import ActiveScene, InjectMode, MatchEntry, Position, WindowMessage

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


def wb_config(*, enabled: bool = True, cap_tokens: int = 0) -> SimpleNamespace:
    """一个「频道生效配置」的最小替身：开关、预算百分比与 token 上限（主模型组缺省，按默认上下文与系数 1.0）

    百分比取 100、上限默认 0：预算是（32768 − 4096）= 28672 token，用例里的正文都放得下；
    要测「放不下」就给 `cap_tokens`。
    """
    return SimpleNamespace(
        WORLDBOOK_ENABLED=enabled,
        WORLDBOOK_BUDGET_PERCENT=100,
        WORLDBOOK_BUDGET_CAP_TOKENS=cap_tokens,
    )


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
    budget_tokens: int = 0,
    inject_mode: str = "both",
    active_scene: str = "both",
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
        budget_tokens=budget_tokens,
        inject_mode=inject_mode,
        active_scene=active_scene,
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
        # 生效配置的 token 上限比条目正文还少：整条被跳过，并记下原因与 token 数。
        await _message("随便说点什么")

        result = await runtime.resolve_round("code-budget", world.channel, wb_config(cap_tokens=1))

        assert result.state == "none"
        assert result.groups.empty
        assert result.dropped == (
            (
                world.entries["constant"].id,
                world.books["domain"].id,
                len("常驻正文"),
                tokens.count_tokens("常驻正文"),
                "global_tokens",
            ),
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

        budget = wb_config(cap_tokens=tokens.count_tokens("租户正文"))
        normal = await runtime.resolve_round("code-sup-d", world.channel, budget)
        assert _group_ids(normal.groups) == {tenant.id}
        assert normal.dropped == (
            (host.id, host.book_id, len("宿主正文"), tokens.count_tokens("宿主正文"), "global_tokens"),
        )

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

        result = await runtime.resolve_round("code-anchor", world.channel, wb_config(cap_tokens=1))
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

        short = SimpleNamespace(**vars(wb_config()), AI_CHAT_CONTEXT_EXPIRE_SECONDS=3600)
        expired = await runtime.resolve_round("code-expired", world.channel, short)
        assert expired.state == "none"

        long = SimpleNamespace(**vars(wb_config()), AI_CHAT_CONTEXT_EXPIRE_SECONDS=10**9)
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


async def _tagged_entry(
    book: DBWbBook,
    *,
    uid: int,
    comment: str,
    content: str,
    constant: bool = True,
    keys: tuple[str, ...] = (),
    sticky: Optional[int] = None,
) -> DBWbEntry:
    """带标题的条目：用途按标题与正文现算，基线夹具里的 `_entry` 不带标题"""
    return await DBWbEntry.create(
        book=book,
        uid=uid,
        comment=comment,
        keys=list(keys),
        secondary_keys=[],
        selective_logic="and_any",
        use_regex=False,
        constant=constant,
        content=content,
        order=100,
        position="before_history",
        depth=4,
        sticky=sticky,
        enabled=True,
        trusted=True,
        status="active",
        ext={},
    )


_TRAP_FILLER = "这是一段与设定无关的说明文字，用来凑够长度。" * 60


class TestTemplateRouting:
    """按用途路由与模板求值（样本照 MVU 卡片的结构现写，不取自真实卡片）"""

    @pytest.fixture(autouse=True)
    def _clean_purpose_cache(self) -> Any:
        wb_purpose.clear_cache()
        yield
        wb_purpose.clear_cache()

    async def test_state_only_entries_never_reach_the_prompt(self, world: World) -> None:
        book = world.books["domain"]
        update = await _tagged_entry(book, uid=10, comment="[mvu_update]变量输出格式", content="每轮末尾写 <UpdateVariable>")
        initvar = await _tagged_entry(book, uid=11, comment="[initvar]初值", content='{"stat_data": {}}')
        await _message("随便说点什么")

        result = await runtime.resolve_round("code-state-only", world.channel, wb_config())

        assert _group_ids(result.groups) == {world.entries["constant"].id}
        dropped = {(entry_id, reason) for entry_id, _book, _chars, _tokens, reason in result.dropped}
        assert dropped == {(update.id, "state_only"), (initvar.id, "state_only")}

    async def test_template_is_rendered_and_budget_counts_the_rendered_text(self, world: World) -> None:
        book = world.books["domain"]
        long_branch = "很长的后期剧情。" * 500
        content = (
            "<%_ if (getvar('stat_data.剧情权重') >= 3) { _%>\n" + long_branch + "\n<%_ } else { _%>\n"
            "初到此地。\n<%_ } _%>\n"
        )
        entry = await _tagged_entry(book, uid=12, comment="角色", content=content)
        await _message("随便说点什么")

        # 原文远超 200 token，算完只剩「初到此地。」：按算完的正文数 token 进预算。
        result = await runtime.resolve_round("code-render", world.channel, wb_config(cap_tokens=200))

        hits = {hit.entry.id: hit for hit in result.groups.before_history}
        assert hits[entry.id].entry.content == "初到此地。\n"
        assert "<%" not in hits[entry.id].entry.content

    async def test_plain_entries_are_untouched(self, world: World) -> None:
        await _message("随便说点什么")

        result = await runtime.resolve_round("code-plain", world.channel, wb_config())

        (hit,) = result.groups.after_persona
        assert hit.entry.content == "常驻正文"
        assert result.dropped == ()

    async def test_trap_and_dead_branch_entries_are_not_injected(self, world: World) -> None:
        book = world.books["domain"]
        env_trap = await _tagged_entry(
            book,
            uid=13,
            comment="海事法典摘编",
            content=f"<% if (localStorage.getItem('piupiu_k') || window.__p) {{ %>{_TRAP_FILLER}<% }} %>",
            constant=False,
            keys=("海边",),
        )
        dead = await _tagged_entry(
            book,
            uid=14,
            comment="排序",
            content=f"<% if (false) {{ %>{_TRAP_FILLER}<% }} %>",
        )
        await _message("今天去海边")

        result = await runtime.resolve_round("code-trap", world.channel, wb_config(cap_tokens=20000))

        assert _group_ids(result.groups) == {world.entries["constant"].id}
        dropped = {(entry_id, reason) for entry_id, _book, _chars, _tokens, reason in result.dropped}
        assert dropped == {(env_trap.id, "template_unsupported"), (dead.id, "template_empty")}

    async def test_routed_out_entries_do_not_set_anchors(self, world: World) -> None:
        book = world.books["domain"]
        update = await _tagged_entry(book, uid=15, comment="[mvu_update]规则", content="规则", sticky=3)
        await _message("随便说点什么")

        await runtime.resolve_round("code-anchor", world.channel, wb_config())
        runtime.finish_round("code-anchor", "ok")

        assert timed_state_store.get(CHAT_KEY, update.id) is None

    async def test_template_error_is_dropped(self, world: World) -> None:
        book = world.books["domain"]
        broken = await _tagged_entry(
            book,
            uid=16,
            comment="地点",
            content="<% if (getvar('地点').includes('冰原')) { %>冷<% } %>",
        )
        await _message("随便说点什么")

        result = await runtime.resolve_round("code-error-tpl", world.channel, wb_config())

        dropped = {(entry_id, reason) for entry_id, _book, _chars, _tokens, reason in result.dropped}
        assert dropped == {(broken.id, "template_error")}

    async def test_missing_variable_bait_is_not_injected(self, world: World) -> None:
        book = world.books["domain"]
        bait = await _tagged_entry(
            book,
            uid=17,
            comment="排序",
            content=f"<% if (!getvar('stat_data')) {{ %>{_TRAP_FILLER}<% }} %>正常设定",
        )
        await _message("随便说点什么")

        result = await runtime.resolve_round("code-bait", world.channel, wb_config(cap_tokens=20000))

        assert _group_ids(result.groups) == {world.entries["constant"].id}
        dropped = {(entry_id, reason) for entry_id, _book, _chars, _tokens, reason in result.dropped}
        assert dropped == {(bait.id, "template_trap")}

    async def test_template_budget_per_round(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        book = world.books["domain"]
        content = "<% if (true) { %>" + "模板正文" * 50 + "<% } %>"
        first = await _tagged_entry(book, uid=18, comment="甲", content=content)
        second = await _tagged_entry(book, uid=19, comment="乙", content=content)
        await _message("随便说点什么")
        # 只够一条的分析（编译加对比渲染，每次至少按 512 字记账；注入用分析时算好的文字，不另扣）
        charge = max(wb_purpose.MIN_ANALYSIS_CHARGE, wb_purpose._analysis_charge(content))
        monkeypatch.setattr(wb_purpose, "TEMPLATE_ROUND_CHARS", charge + 5)

        result = await runtime.resolve_round("code-budget-tpl", world.channel, wb_config(cap_tokens=20000))

        injected = _group_ids(result.groups)
        reasons = {entry_id: reason for entry_id, _book, _chars, _tokens, reason in result.dropped}
        assert len({first.id, second.id} & injected) == 1
        assert sorted(reasons.values()) == ["template_budget"]

    async def test_known_routed_entries_are_deferred_without_spending_the_budget(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        book = world.books["domain"]
        menu = await _tagged_entry(book, uid=21, comment="菜单", content="<% for (const n of names) { %>x<% } %>")
        await _message("随便说点什么")
        # 书详情已经分析过这条（只进摘要缓存）：运行时匹配前就知道要分流，不再占模板预算
        wb_purpose.summary_for(menu)
        monkeypatch.setattr(wb_purpose, "TEMPLATE_ROUND_CHARS", 0)

        result = await runtime.resolve_round("code-known-route", world.channel, wb_config())

        dropped = {(entry_id, reason) for entry_id, _book, _chars, _tokens, reason in result.dropped}
        assert dropped == {(menu.id, "template_unsupported")}
        assert wb_purpose.cache_stats()[0] == 0

    async def test_templated_hits_per_round_are_capped(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        book = world.books["domain"]
        content = "<% for (const n of names) { %>x<% } %>"
        rows = [await _tagged_entry(book, uid=30 + index, comment=f"模板{index}", content=content) for index in range(10)]
        await _message("随便说点什么")
        monkeypatch.setattr(runtime.config, "WORLDBOOK_MAX_ENTRIES_PER_ROUND", 2)

        # 条数上限 2 条：一轮最多分析 8 条带模板的命中（都被分流、不占条数），其余记 `template_budget`
        result = await runtime.resolve_round("code-cap", world.channel, wb_config(cap_tokens=20000))

        ids = {row.id for row in rows}
        reasons = sorted(reason for entry_id, _book, _chars, _tokens, reason in result.dropped if entry_id in ids)
        assert reasons == ["template_budget"] * 2 + ["template_unsupported"] * 8
        assert _group_ids(result.groups) == {world.entries["constant"].id}

    async def test_empty_renders_stop_taking_template_slots(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        book = world.books["domain"]
        empties = [
            await _tagged_entry(book, uid=60 + index, comment=f"空{index}", content="<% if (getvar('x')) { %>有<% } %>")
            for index in range(10)
        ]
        real = await _tagged_entry(book, uid=80, comment="实", content="<% if (true) { %>实打实<% } %>")
        await _message("随便说点什么")
        monkeypatch.setattr(runtime.config, "WORLDBOOK_MAX_ENTRIES_PER_ROUND", 2)

        # 第一轮：8 个名额（4 × 条数上限）被前 8 条空条目用掉，后面的与有内容的那条记 `template_budget`
        first = await runtime.resolve_round("code-empty-1", world.channel, wb_config())
        reasons = {entry_id: reason for entry_id, _book, _chars, _tokens, reason in first.dropped}
        assert reasons.get(real.id) == "template_budget"
        assert sorted(reasons[row.id] for row in empties) == ["template_budget"] * 2 + ["template_empty"] * 8
        # 第二轮之前剩下两条也分析过了（就像书详情打开过）：空条目都成了「已知要分流」，
        # 匹配前就放到最后，不再占名额，有内容的那条照常注入
        for row in empties:
            wb_purpose.summary_for(row)
        second = await runtime.resolve_round("code-empty-2", world.channel, wb_config())
        reasons = {entry_id: reason for entry_id, _book, _chars, _tokens, reason in second.dropped}
        assert {reasons[row.id] for row in empties} == {"template_empty"}
        assert real.id not in reasons
        assert real.id in _group_ids(second.groups)

    async def test_analysis_crash_only_drops_that_entry(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        book = world.books["domain"]
        bad = await _tagged_entry(book, uid=20, comment="坏条目", content="<%= 1 %>")
        await _message("随便说点什么")

        def explode(_source: str) -> None:
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr(wb_purpose.template_lite, "compile_template", explode)

        result = await runtime.resolve_round("code-crash", world.channel, wb_config())

        assert result.state == "injected"
        assert _group_ids(result.groups) == {world.entries["constant"].id}
        dropped = {(entry_id, reason) for entry_id, _book, _chars, _tokens, reason in result.dropped}
        assert dropped == {(bad.id, "template_unsupported")}


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


class TestInjectModeAndSceneEnums:
    def test_enum_values(self) -> None:
        assert {mode.value for mode in InjectMode} == {"push", "lookup", "both"}
        assert {scene.value for scene in ActiveScene} == {"both", "private", "group"}


@pytest.mark.parametrize(
    ("stored", "chat_type", "expected"),
    [
        ("both", ChatType.PRIVATE, True),
        ("both", ChatType.GROUP, True),
        ("both", ChatType.UNKNOWN, True),
        ("private", ChatType.PRIVATE, True),
        ("private", ChatType.GROUP, False),
        ("private", ChatType.UNKNOWN, False),
        ("group", ChatType.PRIVATE, False),
        ("group", ChatType.GROUP, True),
        ("group", ChatType.UNKNOWN, False),
        # 空串与不认识的取值都按 `both` 处理：存量书与读坏的行不该整本消失。
        ("", ChatType.PRIVATE, True),
        ("", ChatType.GROUP, True),
        ("", ChatType.UNKNOWN, True),
        ("nope", ChatType.PRIVATE, True),
        ("nope", ChatType.GROUP, True),
        ("nope", ChatType.UNKNOWN, True),
    ],
)
def test_scene_allows_truth_table(stored: str, chat_type: ChatType, expected: bool) -> None:
    assert runtime.scene_allows(stored, chat_type) is expected


@pytest.mark.parametrize("channel_type", ["group", "private", "unknown"])
class TestEffectiveBooksScene:
    async def test_scene_filters_the_book_list(self, world: World, channel_type: str) -> None:
        # 基线那本域书也是 `both`，先停用，让断言只看这一条用例放的三本。
        await DBWbBook.filter(id=world.books["domain"].id).update(enabled=False)
        books = {
            "both": await _book("both 书", scope_kind="domain", active_scene="both"),
            "private": await _book("private 书", scope_kind="domain", active_scene="private"),
            "group": await _book("group 书", scope_kind="domain", active_scene="group"),
        }
        await DBChatChannel.filter(id=world.channel.id).update(channel_type=channel_type)
        await world.channel.refresh_from_db()
        await _message("随便说点什么")

        window = await runtime.load_window(world.channel)
        effective = await runtime.effective_books(world.channel, preset_id=world.preset.id, window=window)

        expected_ids = {books["both"].id}
        if channel_type in ("private", "group"):
            expected_ids.add(books[channel_type].id)
        assert {book.id for book in effective.books} == expected_ids

    async def test_person_book_still_needs_the_person_present(self, world: World, channel_type: str) -> None:
        # 场景过滤不改变个人书的在场判定：人没发言时这本书照旧不生效。
        await DBWbBook.filter(id=world.books["domain"].id).update(enabled=False)
        person = await _book("个人书", scope_kind="person", person_account="u1", active_scene="both")
        await DBChatChannel.filter(id=world.channel.id).update(channel_type=channel_type)
        await world.channel.refresh_from_db()

        window = await runtime.load_window(world.channel)
        absent = await runtime.effective_books(world.channel, preset_id=world.preset.id, window=window)
        assert person.id not in {book.id for book in absent.books}

        await _message("我来了", account="u1", sender_id="u1")
        window = await runtime.load_window(world.channel)
        present = await runtime.effective_books(world.channel, preset_id=world.preset.id, window=window)
        assert person.id in {book.id for book in present.books}


class TestLookupInjection:
    async def test_lookup_book_injects_only_constant_entries(self, world: World) -> None:
        await DBWbBook.filter(id=world.books["domain"].id).update(enabled=False)
        book = await _book("只能查的书", scope_kind="domain", inject_mode="lookup")
        constant = await _entry(book, uid=0, constant=True, content="常驻设定", position="after_persona")
        keyword = await _entry(book, uid=1, keys=("龙",), content="关键词设定")
        await _message("这里有一条龙")

        result = await runtime.resolve_round("code-lookup", world.channel, wb_config())

        assert _group_ids(result.groups) == {constant.id}
        # 关键词条目按设计不参与这一轮，不该出现在「命中但没注入」的明细里。
        assert keyword.id not in {item[0] for item in result.dropped}

    async def test_both_book_injects_constant_and_keyword_entries(self, world: World) -> None:
        await DBWbBook.filter(id=world.books["domain"].id).update(enabled=False)
        book = await _book("都推的书", scope_kind="domain", inject_mode="both")
        constant = await _entry(book, uid=0, constant=True, content="常驻设定", position="after_persona")
        keyword = await _entry(book, uid=1, keys=("龙",), content="关键词设定")
        await _message("这里有一条龙")

        result = await runtime.resolve_round("code-both", world.channel, wb_config())

        assert _group_ids(result.groups) == {constant.id, keyword.id}
class TestScanNames:
    """书级「扫描时带上说话人名字」：Bot 自己的消息带人设名，群友带清洗过的昵称"""

    async def test_the_persona_name_triggers_only_when_the_book_asks_for_names(self, world: World) -> None:
        await DBWbEntry.all().update(enabled=False)
        book = await _book("人物书")
        entry = await _entry(book, uid=1, keys=(PRESET_NAME,), content="人设名条目", trusted=True)
        # Bot 自己的这句话里没有人设名：只有带上名字扫描，关键词才对得上
        await _message("今天天气不错", account=BOT_ACCOUNT, sender_id=runtime.BOT_SENDER_ID)

        without = await runtime.resolve_round("code-names-off", world.channel, wb_config())
        assert entry.id not in _group_ids(without.groups)

        await DBWbBook.filter(id=book.id).update(scan_names=True)
        with_names = await runtime.resolve_round("code-names-on", world.channel, wb_config())
        assert entry.id in _group_ids(with_names.groups)

    async def test_a_member_nickname_triggers_with_names_on(self, world: World) -> None:
        await DBWbEntry.all().update(enabled=False)
        book = await _book("人物书")
        await DBWbBook.filter(id=book.id).update(scan_names=True)
        entry = await _entry(book, uid=1, keys=("龙骑士",), content="昵称条目", trusted=True)
        await _message("大家好", account="龙骑士")

        result = await runtime.resolve_round("code-nickname", world.channel, wb_config())

        assert entry.id in _group_ids(result.groups)

    async def test_names_from_one_book_do_not_leak_into_another(self, world: World) -> None:
        """名字是按书判的：只有打开开关的那本书扫得到名字"""
        await DBWbEntry.all().update(enabled=False)
        named = await _book("带名字的书")
        await DBWbBook.filter(id=named.id).update(scan_names=True)
        plain = await _book("不带名字的书")
        in_named = await _entry(named, uid=1, keys=("龙骑士",), content="甲", trusted=True)
        in_plain = await _entry(plain, uid=1, keys=("龙骑士",), content="乙", trusted=True)
        await _message("大家好", account="龙骑士")

        result = await runtime.resolve_round("code-per-book", world.channel, wb_config())

        ids = _group_ids(result.groups)
        assert in_named.id in ids
        assert in_plain.id not in ids


class TestIgnoreBudget:
    """「忽略预算」：只有宿主条目生效，书预算挡不住它，仍计入已用量"""

    async def test_a_trusted_entry_passes_a_full_book_budget(self, world: World) -> None:
        await DBWbEntry.all().update(enabled=False)
        book = await _book("小预算的书", budget_tokens=1)
        exempt = await _entry(book, uid=1, constant=True, content="必进的设定正文", trusted=True)
        await DBWbEntry.filter(id=exempt.id).update(ignore_budget=True)
        normal = await _entry(book, uid=2, constant=True, content="普通的设定正文", trusted=True)
        await _message("随便说点什么")

        result = await runtime.resolve_round("code-ignore", world.channel, wb_config())

        ids = _group_ids(result.groups)
        assert exempt.id in ids
        assert normal.id not in ids
        assert (normal.id, "book_tokens") in {(item[0], item[4]) for item in result.dropped}

    async def test_a_tenant_entry_gets_no_exemption(self, world: World) -> None:
        await DBWbEntry.all().update(enabled=False)
        book = await _book("小预算的书", budget_tokens=1)
        tenant_entry = await _entry(book, uid=1, constant=True, content="租户写的设定正文", trusted=False)
        await DBWbEntry.filter(id=tenant_entry.id).update(ignore_budget=True)
        await _message("随便说点什么")

        result = await runtime.resolve_round("code-ignore-tenant", world.channel, wb_config())

        assert tenant_entry.id not in _group_ids(result.groups)
        assert (tenant_entry.id, "book_tokens") in {(item[0], item[4]) for item in result.dropped}


class TestPersonLabels:
    """个人书在本轮里该标的账号串（「这条关于谁」）：挂在轮次结果上，渲染层再用"""

    async def test_person_book_carries_the_speaking_account(self, world: World) -> None:
        await DBWbEntry.all().update(enabled=False)
        person = await _book("个人书", scope_kind="person", person_account="u1")
        await _entry(person, uid=1, constant=True, content="她喜欢下雨天")
        await _message("我来了", account="u1")

        result = await runtime.resolve_round("code-label", world.channel, wb_config())

        assert result.person_labels == {int(person.id): "u1"}

    async def test_normal_books_are_not_labeled(self, world: World) -> None:
        await _message("随便说点什么")

        result = await runtime.resolve_round("code-label-none", world.channel, wb_config())

        assert result.person_labels == {}

    async def test_the_owner_speaking_with_an_alias_labels_the_alias(self, world: World) -> None:
        """主人用副号发言时标的是副号（模型要按聊天记录里的账号对得上人）"""
        from nekro_agent.models.db_identity import DBIdentityAlias
        from nekro_agent.services.identity_scope import resolve_memory_domain

        await DBWbEntry.all().update(enabled=False)
        person = await _book("个人书", scope_kind="person", person_account="u-main")
        await _entry(person, uid=1, constant=True, content="设定")
        domain = await resolve_memory_domain(ADAPTER, INSTANCE_KEY)
        await DBIdentityAlias.create(
            domain_id=domain.domain_id,
            platform_userid="u-alt",
            person_uid="u-main",
        )
        await _message("我来啦", account="u-alt")

        result = await runtime.resolve_round("code-label-alt", world.channel, wb_config())

        assert result.person_labels == {int(person.id): "u-alt"}

    async def test_an_unsafe_account_name_is_dropped(self, world: World) -> None:
        """适配器账号可能是用户自己起的名字：不能原样进设定块"""
        await DBWbEntry.all().update(enabled=False)
        person = await _book("个人书", scope_kind="person", person_account="某个名字")
        await _entry(person, uid=1, constant=True, content="设定")
        await _message("我来啦", account="某个名字")

        result = await runtime.resolve_round("code-label-unsafe", world.channel, wb_config())

        assert result.person_labels == {}

    async def test_at_most_three_accounts_are_kept(self) -> None:
        from types import SimpleNamespace

        book = SimpleNamespace(id=5, scope_kind="person", person_account="u1", scan_depth=10)
        window_messages = [
            WindowMessage(id=index + 1, sender_id="s1", platform_userid=f"u{index}", content_text="发言")
            for index in range(5)
        ]
        aliases = {f"u{index}": frozenset({"u1", f"u{index}"}) for index in range(5)}

        labels = runtime.person_labels_of([book], window_messages, bot_account=None, alias_groups=aliases)  # type: ignore[list-item]

        assert labels == {5: "u4 / id:u3 / id:u2"}

    async def test_round_result_defaults_to_an_empty_mapping(self) -> None:
        result = runtime.RoundWorldbook(state="none", groups=selection.RenderGroups())

        assert result.person_labels == {}


class TestSpeakerName:
    def test_an_empty_nickname_has_no_name(self) -> None:
        assert runtime.speaker_name("") == ""
        assert runtime.speaker_name(None) == ""
        assert runtime.speaker_name("   ") == ""

    def test_a_nickname_is_cleaned_like_the_user_macro(self) -> None:
        assert runtime.speaker_name("龙\n骑<士>​") == "龙 骑士"
        assert len(runtime.speaker_name("名" * 100)) == 24

    def test_a_literal_user_name_is_kept(self) -> None:
        """清洗后为空才给空串；昵称本来就叫「用户」的照留"""
        assert runtime.speaker_name("用户") == "用户"
        assert runtime.speaker_name("​​") == ""

    async def test_the_persona_name_comes_from_the_preset(self, world: World) -> None:
        assert await runtime.persona_speaker(world.preset.id) == PRESET_NAME
        assert await runtime.persona_speaker(0) == ""
