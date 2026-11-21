"""pytest 打完汇总后进程要能自己退出

## 由来

本仓库的 pytest 跑完、打印完汇总后常常不退出（本机与 CI 都见过），一直靠 `os._exit` 插件或 CI 里的
「写完报告 60 秒后强杀」绕过去。诊断（收尾时列出还活着的线程、faulthandler 倒栈）显示卡在
`threading._shutdown`：它要 join 所有非守护线程，而留下来的是 aiosqlite 的连接线程，停在
`self._tx.get()` 上等下一条指令。aiosqlite 每条连接一条非守护线程，只有 `close()` 会让它退出；
用例或用例里起的后台任务懒建了 Tortoise 连接又没关，事件循环随用例关掉，线程就一直留着。
同一组用例有时挂、有时不挂，取决于后台任务在事件循环关掉前跑到了哪一步。

`tests/conftest.py` 在会话收尾时（排在所有固件收尾之后）把这些连接正常关掉。这里钉住三件事：

- 机制本身：用例结束时留一条没关的 aiosqlite 连接，不挂收尾插件的 pytest 退不出去；
- 挂上 `tests/conftest.py` 的收尾插件，同一组用例能在限时内自己退出、退出码 0，并在终端上说明关了几条；
- 收尾函数是正常关闭：连接线程退出，返回的名字对得上。

子进程在临时目录里跑，带自己的 `pytest.ini`，不会读到仓库的 conftest 与配置；只从仓库的
conftest 借 `pytest_configure`（它负责挂上收尾插件），别的固件不带过去。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_CONFTEST = Path(__file__).resolve().parent / "conftest.py"

# 不挂收尾插件时最多等几秒：起 pytest、跑一条用例只要一两秒，挂住的话永远等不到
HANG_PROBE_SECONDS = 15
# 挂上收尾插件时的限时：留足机器繁忙时的余量
EXIT_DEADLINE_SECONDS = 120

LEAKING_TEST = """
import asyncio

import aiosqlite


def test_leaves_a_connection_open():
    async def _open():
        conn = await aiosqlite.connect(":memory:")
        await conn.execute("select 1")
        return conn

    # 事件循环随 asyncio.run 关掉，连接没关：与用例里懒建的 Tortoise 连接同一个形态
    asyncio.run(_open())
"""

CONFTEST_BORROWING_THE_CLOSER = f"""
import importlib.util

_spec = importlib.util.spec_from_file_location("na_repo_conftest_under_test", {str(REPO_CONFTEST)!r})
_repo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_repo)

pytest_configure = _repo.pytest_configure
"""


def _load_repo_conftest() -> ModuleType:
    spec = importlib.util.spec_from_file_location("na_repo_conftest_for_test", REPO_CONFTEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_project(root: Path, *, with_closer: bool) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "test_leak.py").write_text(LEAKING_TEST, encoding="utf-8")
    if with_closer:
        (root / "conftest.py").write_text(CONFTEST_BORROWING_THE_CLOSER, encoding="utf-8")


def _run_pytest(root: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    # 外层可能经环境变量塞了插件（比如收尾时直接 os._exit 的那个），子进程里一律不要
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        env.pop(name, None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(root), str(root)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def test_a_leaked_connection_keeps_plain_pytest_from_exiting(tmp_path: Path) -> None:
    """对照：不挂收尾插件时，留一条没关的连接就退不出去（证明下一条用例测的是真问题）"""
    root = tmp_path / "without_closer"
    _write_project(root, with_closer=False)
    with pytest.raises(subprocess.TimeoutExpired):
        _run_pytest(root, timeout=HANG_PROBE_SECONDS)


def test_the_session_closer_lets_pytest_exit_on_its_own(tmp_path: Path) -> None:
    root = tmp_path / "with_closer"
    _write_project(root, with_closer=True)
    result = _run_pytest(root, timeout=EXIT_DEADLINE_SECONDS)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "1 passed" in output, output
    assert "关掉了 1 条" in output, output


def test_closing_is_a_real_close_and_the_thread_ends() -> None:
    """收尾函数用新的事件循环正常 close：连接线程退出，返回的名字对得上"""
    import aiosqlite

    async def _open() -> aiosqlite.Connection:
        conn = await aiosqlite.connect(":memory:")
        await conn.execute("select 1")
        return conn

    conn = asyncio.run(_open())
    assert conn.is_alive(), "前提：建连接的事件循环关了，连接线程还活着"

    closer = _load_repo_conftest().close_leaked_aiosqlite_connections
    closed = closer([conn])

    assert closed == [conn.name]
    assert not conn.is_alive()
    assert closer([conn]) == [], "线程已经退出，再调一次什么都不做"


def test_a_thread_that_never_connected_is_stopped_too() -> None:
    """线程起了、连接还没连上就被丢下：`close()` 直接返回不停线程，要靠停止标记让它退出

    全量里实际挂住进程的正是这一种：用例收尾时取消了一个正在 `await aiosqlite.connect()`
    的后台任务，`start()` 已经执行、连接那一步还没排进队列，线程就空等在队列上。
    """
    import aiosqlite

    conn = aiosqlite.connect(":memory:")
    conn.start()  # `await aiosqlite.connect()` 里先 start()、再排连接；这里停在两步之间
    assert conn.is_alive()

    closer = _load_repo_conftest().close_leaked_aiosqlite_connections
    assert closer([conn]) == [conn.name]
    assert not conn.is_alive()
