"""换事件循环之后，进程里共用的东西不能把下一个循环挂住

每条用例各跑在自己的事件循环里，用例结束时循环关掉，进程级的状态（连接、线程池、缓存）却留了下来。
一个循环关掉时还在半途的操作，结果交不回去了；这时候共用的东西必须照样能被下一个循环用，至少要
干脆地报错，而不是让下一个循环里的调用方永远等下去。

这里每条用例都是同一个套路：在第一个循环里起一个操作，让它卡在线程里（等一个 `threading.Event`），
关掉这个循环；放行之后，线程把结果交给已经关掉的循环；再在第二个循环里用同一份共享状态，限时等它
完成。用 `Event` 卡住而不是靠「查询慢一点」，所以结果不看机器快慢。

- aiosqlite：连接线程把结果交回已经关掉的循环时会抛 `RuntimeError`，原版的工作线程就此退出，但连接
  还标着「在跑」；之后谁在这条连接上 `close()`（比如下一条用例里 `Tortoise.init` 先关掉上一份配置
  遗留的连接），就把活交给一条已经不在了的线程，永远等不到回复。`tests/conftest.py` 让工作线程丢掉
  这个交不出去的结果、接着处理队列。
- Tortoise 懒建连接时被取消：`ConnectionWrapper.__aenter__` 先拿锁再建连接，建连接被取消时不放锁，
  这个客户端上之后的查询永远等不到锁。`tests/conftest.py` 在每条用例开始前把之前留下的客户端摘掉。
- 主机名解析的合并查询、书详情分析的执行器槽位、HTTP 客户端缓存：按循环分表、槽位跟着线程池任务
  释放、缓存按循环核对，换循环后都不会被上一个循环的残留挂住。
"""

from __future__ import annotations

import asyncio
import importlib.util
import socket
import sqlite3
import threading
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Coroutine, List

import aiosqlite
import pytest
from aiosqlite import core as aiosqlite_core
from tortoise import Tortoise
from tortoise.connection import connections

from nekro_agent.services.agent import openai as openai_service
from nekro_agent.services.worldbook import purpose
from nekro_agent.tools import common_util

# 第二个循环里最多等多久；正常情况下都是毫秒级，挂住时就是这么久之后报超时，而不是一直等下去
SECOND_LOOP_TIMEOUT = 5.0
# 卡在线程里的那一步最多等放行多久（只是防止用例写错时线程永远不退）
GATE_TIMEOUT = 10.0
REPO_CONFTEST = Path(__file__).resolve().parent / "conftest.py"


def load_repo_conftest() -> ModuleType:
    spec = importlib.util.spec_from_file_location("na_repo_conftest_for_loop_switch", REPO_CONFTEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_in_own_loop(make_coro: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    """在一个新的事件循环里跑完，跟 pytest-asyncio 收尾一样：取消还没跑完的任务，然后关掉循环"""
    with asyncio.Runner() as runner:
        return runner.run(make_coro())


def gated(gate: threading.Event) -> Callable[[Any], int]:
    def wait_for_gate(_arg: Any) -> int:
        gate.wait(GATE_TIMEOUT)
        return 1

    return wait_for_gate


def test_the_connection_thread_patch_is_installed() -> None:
    """aiosqlite 换了内部结构时补丁会跳过不装；这一条先红，提示回来看 `tests/conftest.py`"""
    assert getattr(aiosqlite_core.Connection.run, "survives_closed_loops", False)


class TestSqliteConnectionAcrossLoops:
    def test_a_query_left_running_when_its_loop_closed_does_not_break_the_connection(self) -> None:
        gate = threading.Event()

        async def open_and_leave_a_query_running() -> aiosqlite.Connection:
            conn = await aiosqlite.connect(":memory:")
            await conn.create_function("wait_for_gate", 1, gated(gate))

            async def query() -> None:
                await conn.execute_fetchall("SELECT wait_for_gate(1)")

            asyncio.get_running_loop().create_task(query())
            await asyncio.sleep(0.05)  # 查询已经交给连接线程，卡在 gate 上
            return conn

        conn = run_in_own_loop(open_and_leave_a_query_running)
        gate.set()  # 线程跑完查询，把结果交给已经关掉的循环

        async def close_from_a_new_loop() -> None:
            await asyncio.wait_for(conn.close(), SECOND_LOOP_TIMEOUT)

        run_in_own_loop(close_from_a_new_loop)
        conn.join(SECOND_LOOP_TIMEOUT)
        assert not conn.is_alive(), "关掉之后连接线程应当退出"

    def test_tortoise_init_after_a_loop_closed_mid_query_does_not_hang(self) -> None:
        """全量里实际挂住的那条路：上一份配置遗留的连接在循环关掉时还有查询在跑，下一次 init 先关它"""
        gate = threading.Event()

        async def leave_a_query_running() -> None:
            await Tortoise.init(db_url="sqlite://:memory:", modules={"models": []})
            client = connections.get("default")
            async with client.acquire_connection() as conn:
                await conn.create_function("wait_for_gate", 1, gated(gate))
            asyncio.get_running_loop().create_task(client.execute_query("SELECT wait_for_gate(1)"))
            await asyncio.sleep(0.05)

        try:
            run_in_own_loop(leave_a_query_running)
        finally:
            gate.set()

        async def init_again() -> None:
            await asyncio.wait_for(
                Tortoise.init(db_url="sqlite://:memory:", modules={"models": []}),
                SECOND_LOOP_TIMEOUT,
            )
            await Tortoise.close_connections()

        run_in_own_loop(init_again)

    def test_a_lock_left_held_by_a_cancelled_lazy_connect_does_not_reach_the_next_test(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """后台任务懒建连接时被取消，Tortoise 不放锁；下一条用例开始前这个客户端被摘掉"""
        gate = threading.Event()
        real_connect = sqlite3.connect

        def gated_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            gate.wait(GATE_TIMEOUT)
            return real_connect(*args, **kwargs)

        async def leave_a_lazy_connect_hanging() -> None:
            await Tortoise.init(db_url="sqlite://:memory:", modules={"models": []})
            await Tortoise.close_connections()  # 像用例收尾一样：连接关了，配置还在
            monkeypatch.setattr(sqlite3, "connect", gated_connect)
            client = connections.get("default")  # 后台任务懒建出来的那个客户端
            asyncio.get_running_loop().create_task(client.execute_query("SELECT 1"))
            await asyncio.sleep(0.05)  # 任务拿着锁，卡在建连接那一步

        try:
            run_in_own_loop(leave_a_lazy_connect_hanging)
        finally:
            gate.set()
            monkeypatch.setattr(sqlite3, "connect", real_connect)
        assert connections.get("default")._lock.locked(), "前提：取消落在建连接那一步，锁没放"  # noqa: SLF001

        assert load_repo_conftest().drop_tortoise_connections_left_by_earlier_tests() == ["default"]

        async def query_again() -> None:
            await asyncio.wait_for(connections.get("default").execute_query("SELECT 1"), SECOND_LOOP_TIMEOUT)
            await Tortoise.close_connections()

        run_in_own_loop(query_again)


class TestOtherSharedEntriesAcrossLoops:
    def test_a_lookup_abandoned_with_its_loop_does_not_hold_up_the_next_loop(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        gate = threading.Event()
        calls: List[str] = []

        def fake_getaddrinfo(host: str, port: int, *args: Any, **kwargs: Any) -> list:
            calls.append(host)
            if len(calls) == 1:
                gate.wait(GATE_TIMEOUT)
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("203.0.113.7", port))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

        async def start_a_lookup_and_leave() -> None:
            asyncio.get_running_loop().create_task(common_util._getaddrinfo_shared("probe.example.test", 443))
            await asyncio.sleep(0.05)

        async def look_up_again() -> list:
            return await asyncio.wait_for(
                common_util._getaddrinfo_shared("probe.example.test", 443),
                SECOND_LOOP_TIMEOUT,
            )

        try:
            run_in_own_loop(start_a_lookup_and_leave)
            result = run_in_own_loop(look_up_again)
        finally:
            gate.set()
        assert result[0][4][0] == "203.0.113.7"
        assert calls == ["probe.example.test", "probe.example.test"], "第二个循环另起一次查询，不等上一个循环那一次"

    def test_the_listing_slot_is_freed_when_a_job_finishes_after_its_loop_closed(self) -> None:
        gate = threading.Event()

        def first_job() -> str:
            gate.wait(GATE_TIMEOUT)
            return "first"

        async def submit_and_leave() -> None:
            asyncio.get_running_loop().create_task(purpose._submit_listing(first_job))
            await asyncio.sleep(0.05)

        try:
            run_in_own_loop(submit_and_leave)
        finally:
            gate.set()

        async def submit_again() -> object:
            # 上一个任务在线程里收尾、释放槽位要一点时间；这段时间里拿不到槽位返回 None（页面标「分析中」）
            deadline = asyncio.get_running_loop().time() + SECOND_LOOP_TIMEOUT
            while asyncio.get_running_loop().time() < deadline:
                result = await asyncio.wait_for(purpose._submit_listing(lambda: "second"), SECOND_LOOP_TIMEOUT)
                if result is not None:
                    return result
                await asyncio.sleep(0.02)
            return None

        assert run_in_own_loop(submit_again) == "second"

    def test_the_http_client_cache_does_not_hand_out_a_client_from_a_closed_loop(self) -> None:
        async def get_client() -> Any:
            return openai_service._get_http_client(base_url="https://probe.example.test/v1")

        first = run_in_own_loop(get_client)

        async def get_and_close() -> Any:
            client = openai_service._get_http_client(base_url="https://probe.example.test/v1")
            await client.aclose()
            return client

        second = run_in_own_loop(get_and_close)
        assert second is not first
