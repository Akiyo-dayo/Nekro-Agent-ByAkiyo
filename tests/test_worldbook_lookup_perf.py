"""模型主动查询世界书的性能与懒加载：方法清单不读条目、首次加载去重、缓存按大小封顶、
打分进线程、模板条目只在渲染后按正文补分

`tests/test_worldbook_lookup.py` 覆盖安全判据；这里只钉这次为性能改的几条，外加拒绝码、
安全边界在改造之后仍然成立：

1. `collect` / `availability` 不读条目：只有可查的书时给两个方法，只有只推送的书时给空表，
   拒绝码与 `resolve_scope` 逐项一致；已收尾的令牌不缓存；
2. 同一个令牌并发首次加载只查一次库；加载抛异常不写缓存，下一次能重试；一个调用被取消
   不连带取消别的调用；
3. 作用域缓存除键数外还按条目正文总字数封顶，最旧的先丢、刚放进去的保留；
4. 打分走 `asyncio.to_thread`，两万条长正文时事件循环不被卡住；
5. 模板条目原文里没走到的分支不参与打分；渲染后的正文参与第二段打分，能改变排名。
"""

from __future__ import annotations

import asyncio
import json
import time
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
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services.affection.suppression import lift_narrative_suppression, suppress_narrative
from nekro_agent.services.agent.session_token import register_session_token
from nekro_agent.services.worldbook import lookup
from nekro_agent.services.worldbook import service as wb_service
from nekro_agent.services.worldbook import variables as wb_variables
from plugins.builtin.worldbook_tools import main as plugin_main

ADAPTER = "onebot_v11"
INSTANCE_KEY = "pf2"
CHAT_KEY = f"{ADAPTER}-{INSTANCE_KEY}-group_61"
OTHER_CHAT_KEY = f"{ADAPTER}-{INSTANCE_KEY}-group_62"
BOT_ACCOUNT = "bot_pf2"
PRESET_NAME = "性能用例人设"
CODE = "perf-round-code-1"
BASE_TIMESTAMP = int(time.time()) - 5


def _config(**overrides: Any) -> SimpleNamespace:
    """频道生效配置的最小替身：开关、两项查询上限与预算口径都齐"""
    values: dict[str, Any] = {
        "WORLDBOOK_ENABLED": True,
        "WORLDBOOK_BUDGET_PERCENT": 100,
        "WORLDBOOK_BUDGET_CAP_TOKENS": 0,
        "WORLDBOOK_LOOKUP_MAX_TOKENS": 3000,
        "WORLDBOOK_LOOKUP_MAX_CALLS_PER_ROUND": 4,
        "AI_CHAT_CONTEXT_EXPIRE_SECONDS": 10**9,
        "USE_MODEL_GROUP": "",
        "FALLBACK_MODEL_GROUP": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """每条用例前后清空进程内缓存、抑制状态与变量来源：它们都是模块级单例"""
    monkeypatch.setattr(config, "MEMORY_INSTANCE_SHARING_GROUPS", {})
    monkeypatch.setattr(config, "AI_CHAT_CONTEXT_EXPIRE_SECONDS", 10**9)
    lookup.reset_lookup_state()
    wb_variables.clear_variable_sources()
    lift_narrative_suppression(CHAT_KEY)
    yield
    lookup.reset_lookup_state()
    wb_variables.clear_variable_sources()
    lift_narrative_suppression(CHAT_KEY)


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SimpleNamespace]:
    """一个没有书的频道：可查的书由各条用例按需创建（频道级，只对这个群生效）"""
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE_KEY,
            display_name="性能用例实例",
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
            channel_id="group_61",
            channel_name="性能用例群",
            channel_type="group",
            preset_id=preset.id,
            data="{}",
            workspace_id=None,
        )
        await DBChatChannel.filter(id=channel.id).update(
            conversation_start_time=datetime.fromtimestamp(BASE_TIMESTAMP - 10, tz=timezone.utc),
        )
        await channel.refresh_from_db()
        await DBChatMessage.create(
            sender_id="u1",
            sender_name="u1",
            sender_nickname="u1",
            is_tome=0,
            is_recalled=False,
            adapter_key=ADAPTER,
            instance_key=INSTANCE_KEY,
            message_id="m-perf-1",
            chat_key=CHAT_KEY,
            chat_type="group",
            platform_userid="u1",
            content_text="灯塔在哪儿？",
            content_data="{}",
            raw_cq_code="",
            ext_data="",
            send_timestamp=BASE_TIMESTAMP,
        )
        cfg = _config()

        async def _effective(_self: DBChatChannel) -> SimpleNamespace:
            return cfg

        monkeypatch.setattr(DBChatChannel, "get_effective_config", _effective)
        yield SimpleNamespace(channel=channel, preset=preset, config=cfg)
    finally:
        await Tortoise.close_connections()


def _ctx(channel: Optional[DBChatChannel], *, code: str = CODE) -> AgentCtx:
    """构造本轮上下文；`channel` 为空时按一个不存在的 from_chat_key 构造"""
    if channel is None:
        ctx = AgentCtx(container_key=f"container-{code}", from_chat_key=f"{ADAPTER}-{INSTANCE_KEY}-group_99")
    else:
        ctx = AgentCtx.create_by_db_chat_channel(channel, container_key=f"container-{code}")
    if code:
        register_session_token(ctx.container_key or "", code)
    return ctx


def _set_config(
    monkeypatch: pytest.MonkeyPatch,
    channel: DBChatChannel,
    cfg: SimpleNamespace,
) -> None:
    """换成这一条用例要的生效配置"""

    async def _effective(_self: DBChatChannel) -> SimpleNamespace:
        return cfg

    monkeypatch.setattr(channel, "get_effective_config", _effective.__get__(channel, DBChatChannel))


async def _book(
    *,
    inject_mode: str = "both",
    chat_key: str = CHAT_KEY,
    name: str = "查询用书",
) -> DBWbBook:
    return await DBWbBook.create(
        name=name,
        description="",
        scope_kind="channel",
        adapter_key=ADAPTER,
        instance_key=INSTANCE_KEY,
        preset_id=0,
        chat_key=chat_key,
        person_account="",
        enabled=True,
        scan_depth=4,
        budget_tokens=0,
        inject_mode=inject_mode,
        active_scene="both",
    )


async def _entry(
    book: DBWbBook,
    *,
    uid: int,
    comment: str = "",
    keys: tuple[str, ...] = (),
    content: str = "正文",
    order: int = 100,
) -> DBWbEntry:
    return await DBWbEntry.create(
        book=book,
        uid=uid,
        comment=comment,
        keys=list(keys),
        secondary_keys=[],
        content=content,
        constant=False,
        order=order,
        enabled=True,
        status="active",
        ext={},
    )


async def _extra_channel(preset_id: int, chat_key: str = OTHER_CHAT_KEY) -> DBChatChannel:
    """同一个实例下的另一个频道：本用例的书都是频道级，对它不生效"""
    channel = await DBChatChannel.create(
        chat_key=chat_key,
        adapter_key=ADAPTER,
        instance_key=INSTANCE_KEY,
        channel_id="group_62",
        channel_name="另一个群",
        channel_type="group",
        preset_id=preset_id,
        data="{}",
        workspace_id=None,
    )
    await DBChatChannel.filter(id=channel.id).update(
        conversation_start_time=datetime.fromtimestamp(BASE_TIMESTAMP - 10, tz=timezone.utc),
    )
    await channel.refresh_from_db()
    return channel


def _assert_same_denial(available: Any, scope: Any, expected: Optional[str]) -> None:
    if expected is None:
        assert available is None
        assert isinstance(scope, lookup.LookupScope)
        return
    assert isinstance(available, lookup.LookupDenied), available
    assert isinstance(scope, lookup.LookupDenied), scope
    assert available.code == expected
    assert scope.code == expected


class TestAvailability:
    async def test_collect_offers_methods_without_reading_entries(
        self,
        world: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _book()
        book = await _book(name="第二本书")
        await _entry(book, uid=0, keys=("灯塔",), content="灯塔的说明。")
        calls = 0

        async def counting(book_ids: Any) -> list[DBWbEntry]:
            nonlocal calls
            calls += 1
            raise AssertionError("方法清单不该读条目")

        monkeypatch.setattr(wb_service, "active_entries_for_books", counting)
        ctx = _ctx(world.channel)

        assert await plugin_main.collect_worldbook_methods(ctx) == [
            plugin_main.search_worldbook,
            plugin_main.read_worldbook_entries,
        ]
        assert await lookup.availability(ctx) is None
        assert calls == 0

    async def test_a_push_only_book_offers_nothing(self, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
        await _book(inject_mode="push")
        calls = 0

        async def counting(book_ids: Any) -> list[DBWbEntry]:
            nonlocal calls
            calls += 1
            raise AssertionError("只有只推送的书时不该读条目")

        monkeypatch.setattr(wb_service, "active_entries_for_books", counting)
        ctx = _ctx(world.channel)

        assert await plugin_main.collect_worldbook_methods(ctx) == []
        assert await lookup.availability(ctx) is not None
        assert calls == 0

    async def test_the_availability_result_is_cached_by_token(
        self,
        world: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _book()
        calls = 0
        original = lookup._round_gate  # noqa: SLF001 - 只数共用的判定跑了几次

        async def counting(ctx: AgentCtx) -> Any:
            nonlocal calls
            calls += 1
            return await original(ctx)

        monkeypatch.setattr(lookup, "_round_gate", counting)
        ctx = _ctx(world.channel)

        assert await plugin_main.collect_worldbook_methods(ctx)
        assert await plugin_main.collect_worldbook_methods(ctx)
        assert calls == 1

    async def test_a_closed_token_is_refused_and_never_cached(
        self,
        world: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _book()
        code = "perf-closed-code"
        ctx = _ctx(world.channel, code=code)
        lookup.discard_round(code)

        async def forbidden(book_ids: Any) -> list[DBWbEntry]:
            raise AssertionError("收尾之后不该再读条目")

        monkeypatch.setattr(wb_service, "active_entries_for_books", forbidden)

        for _ in range(2):  # 第二次仍然拒绝，说明第一次也没有把拒绝写进缓存
            assert await plugin_main.collect_worldbook_methods(ctx) == []
            denied = await lookup.availability(ctx)
            assert isinstance(denied, lookup.LookupDenied) and denied.code == "round_closed"
            assert code not in lookup._availability_cache  # noqa: SLF001
        assert code not in lookup._scope_cache  # noqa: SLF001

    async def test_availability_and_resolve_scope_agree_on_every_denial(
        self,
        world: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _book()
        book = await _book(name="第二本书")
        await _entry(book, uid=0, keys=("灯塔",), content="灯塔的说明。")

        async def check(code: str, expected: Optional[str], *, missing_channel: bool = False) -> None:
            target = None if missing_channel else world.channel
            ctx = _ctx(target, code=code)
            available = await lookup.availability(ctx)
            scope = await lookup.resolve_scope(_ctx(target, code=code))
            _assert_same_denial(available, scope, expected)

        # 有可查的书
        await check("avail-ok", None)
        # 世界书关着
        _set_config(monkeypatch, world.channel, _config(WORLDBOOK_ENABLED=False))
        await check("avail-worldbook-off", "worldbook_disabled")
        # 查询次数上限为 0
        _set_config(monkeypatch, world.channel, _config(WORLDBOOK_LOOKUP_MAX_CALLS_PER_ROUND=0))
        await check("avail-lookup-off", "lookup_disabled")
        # 上一轮被拦
        _set_config(monkeypatch, world.channel, _config())
        suppress_narrative(CHAT_KEY)
        await check("avail-suppressed", "round_suppressed")
        lift_narrative_suppression(CHAT_KEY)
        # 没有令牌
        await check("", "no_round_token")
        # 频道取不到
        await check("avail-no-channel", "no_channel", missing_channel=True)
        # 没有可查的书
        other = await _extra_channel(world.preset.id)
        other_ctx = _ctx(other, code="avail-no-books")
        _assert_same_denial(
            await lookup.availability(other_ctx),
            await lookup.resolve_scope(_ctx(other, code="avail-no-books")),
            "no_books",
        )
        # 已收尾
        lookup.discard_round("avail-closed")
        await check("avail-closed", "round_closed")


class TestFirstLoad:
    async def test_concurrent_first_calls_share_one_query(
        self,
        world: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_config(monkeypatch, world.channel, _config(WORLDBOOK_LOOKUP_MAX_CALLS_PER_ROUND=10))
        book = await _book()
        await _entry(book, uid=0, keys=("灯塔",), content="灯塔的守塔人住在海港。")
        calls = 0
        original = wb_service.active_entries_for_books

        async def slow(book_ids: Any) -> list[DBWbEntry]:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return await original(book_ids)

        monkeypatch.setattr(wb_service, "active_entries_for_books", slow)
        ctx = _ctx(world.channel)

        results = await asyncio.gather(*(lookup.search(ctx, "灯塔", 5) for _ in range(5)))
        await asyncio.sleep(0)  # 让完成回调把任务从「进行中」拿掉

        assert calls == 1
        assert all(isinstance(result, lookup.LookupResult) for result in results)
        assert lookup._loading_tasks == {}  # noqa: SLF001

    async def test_a_failed_load_is_retried_on_the_next_call(
        self,
        world: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        book = await _book()
        await _entry(book, uid=0, keys=("灯塔",), content="灯塔的守塔人住在海港。")
        calls = 0
        original = wb_service.active_entries_for_books

        async def flaky(book_ids: Any) -> list[DBWbEntry]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("读条目失败")
            return await original(book_ids)

        monkeypatch.setattr(wb_service, "active_entries_for_books", flaky)
        ctx = _ctx(world.channel)

        with pytest.raises(RuntimeError, match="读条目失败"):
            await lookup.search(ctx, "灯塔", 5)
        await asyncio.sleep(0)  # 让完成回调把任务从「进行中」拿掉
        # 失败不写缓存，也不留在「进行中」里
        assert lookup._loading_tasks == {}  # noqa: SLF001
        assert CODE not in lookup._scope_cache  # noqa: SLF001

        result = await lookup.search(ctx, "灯塔", 5)

        assert isinstance(result, lookup.LookupResult)
        assert calls == 2

    async def test_cancelling_one_caller_keeps_the_shared_load(
        self,
        world: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_config(monkeypatch, world.channel, _config(WORLDBOOK_LOOKUP_MAX_CALLS_PER_ROUND=10))
        book = await _book()
        await _entry(book, uid=0, keys=("灯塔",), content="灯塔的守塔人住在海港。")
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0
        original = wb_service.active_entries_for_books

        async def slow(book_ids: Any) -> list[DBWbEntry]:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return await original(book_ids)

        monkeypatch.setattr(wb_service, "active_entries_for_books", slow)
        ctx = _ctx(world.channel)
        first = asyncio.create_task(lookup.search(ctx, "灯塔", 5))
        await started.wait()
        second = asyncio.create_task(lookup.search(ctx, "灯塔", 5))
        await asyncio.sleep(0)  # 让第二个调用也挂到同一个加载任务上

        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        release.set()
        result = await second

        assert isinstance(result, lookup.LookupResult)
        assert calls == 1, "一个调用被取消，加载任务仍只查一次库"


class TestScopeCacheSize:
    async def test_the_oldest_scope_is_dropped_when_chars_overflow(
        self,
        world: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        book = await _book()
        await _entry(book, uid=0, keys=("灯塔",), content="灯塔的守塔人住在海港。" * 5)
        first = await lookup.resolve_scope(_ctx(world.channel, code="size-code-1"))

        assert isinstance(first, lookup.LookupScope)
        assert first.content_chars > 0
        monkeypatch.setattr(lookup, "MAX_SCOPE_CACHE_CHARS", first.content_chars)

        second = await lookup.resolve_scope(_ctx(world.channel, code="size-code-2"))

        assert isinstance(second, lookup.LookupScope)
        assert "size-code-1" not in lookup._scope_cache  # noqa: SLF001
        assert lookup._scope_cache["size-code-2"] is second  # noqa: SLF001

    async def test_a_single_oversized_scope_is_kept(
        self,
        world: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        book = await _book()
        await _entry(book, uid=0, keys=("灯塔",), content="灯塔的守塔人住在海港。" * 5)
        monkeypatch.setattr(lookup, "MAX_SCOPE_CACHE_CHARS", 0)

        scope = await lookup.resolve_scope(_ctx(world.channel, code="size-code-3"))

        assert isinstance(scope, lookup.LookupScope)
        assert scope.content_chars > 0
        # 刚放进去的那一个不丢，哪怕它自己就超了
        assert lookup._scope_cache["size-code-3"] is scope  # noqa: SLF001


def _large_rows(book: DBWbBook, count: int = 20_000, size: int = 1_000) -> list[SimpleNamespace]:
    """两万条、每条约一千字的轻量条目；正文各不相同，避免共享同一份字符串"""
    filler = "雪原风霜雨雪交加" * (size // 8 + 1)
    rows: list[SimpleNamespace] = []
    for index in range(count):
        rows.append(
            SimpleNamespace(
                id=100_000 + index,
                book_id=book.id,
                comment=f"北方条目{index}",
                keys=["灯塔"] if index % 2000 == 0 else [f"无关词{index}"],
                secondary_keys=[],
                content=f"第{index}条" + filler,
                constant=False,
                order=100,
                enabled=True,
                status="active",
                ext={},
            ),
        )
    return rows


class TestSearchOffLoop:
    async def test_scoring_runs_in_a_thread_and_the_loop_keeps_ticking(
        self,
        world: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_config(monkeypatch, world.channel, _config(WORLDBOOK_LOOKUP_MAX_CALLS_PER_ROUND=10))
        book = await _book()
        rows = _large_rows(book)

        async def fake_entries(book_ids: Any) -> list[SimpleNamespace]:
            return rows

        monkeypatch.setattr(wb_service, "active_entries_for_books", fake_entries)
        ctx = _ctx(world.channel)
        # 加载与分词先预热好，计时窗口里只剩 search 本身
        scope = await lookup.resolve_scope(ctx)
        assert isinstance(scope, lookup.LookupScope)

        thread_calls = 0
        real_to_thread = asyncio.to_thread

        async def counting_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal thread_calls
            thread_calls += 1
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", counting_to_thread)

        gaps: list[float] = []
        stop = asyncio.Event()

        async def ticker() -> None:
            last = time.perf_counter()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        tick = asyncio.create_task(ticker())
        try:
            result = await lookup.search(ctx, "灯塔", 5)
        finally:
            stop.set()
            await tick

        assert isinstance(result, lookup.LookupResult)
        assert thread_calls == 1, "打分应该正好走一次 asyncio.to_thread"
        assert gaps, "计时协程应该至少醒过一次"
        print(f"打分线程期间事件循环最大间隔: {max(gaps) * 1000:.0f} ms")
        assert max(gaps) < 0.2, f"事件循环最长停顿 {max(gaps) * 1000:.0f} ms"


class TestTemplateScoring:
    async def test_a_dead_branch_cannot_be_probed_and_the_rendered_body_reranks(
        self,
        world: SimpleNamespace,
    ) -> None:
        book = await _book()
        dead = "禁地"
        target = await _entry(
            book,
            uid=0,
            comment="灯塔的守塔人",
            content=(
                "<% if (getvar('door_open')) { %>" + dead + "的秘密<% } %>"
                "灯塔的守塔人住在海港。"
            ),
            order=50,
        )
        # 只命中标题里的 Y、正文与查询无关的普通条目；order 更高，第一段排在前面
        ordinary = await _entry(
            book,
            uid=1,
            comment="灯塔的旧闻",
            content="与查询无关的正文。",
            order=100,
        )

        probed = await lookup.search(_ctx(world.channel), dead, 5)

        assert isinstance(probed, lookup.LookupResult)
        assert target.id not in {item.id for item in probed.items}, "没走到的分支不该被查询词命中"

        ranked = await lookup.search(_ctx(world.channel, code="perf-template-code-2"), "灯塔 海港", 5)

        assert isinstance(ranked, lookup.LookupResult)
        assert [item.id for item in ranked.items] == [target.id, ordinary.id]
        [first, second] = ranked.items
        assert first.content == "灯塔的守塔人住在海港。"
        assert "if" not in first.content and "getvar" not in first.content
        assert second.content == "与查询无关的正文。"
        # 结果里不带模板原文，也不带没走到的分支
        payload = json.dumps(ranked.as_dict(), ensure_ascii=False)
        assert dead not in payload
