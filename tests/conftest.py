"""全局测试固件。

## 为什么需要它

`services/memory/text_terms` 有两档 CJK 切分（词典档 `jieba` / 无词典的启发式档）。生产上
「词典还没加载完就先用启发式档、后台线程加载好了再切换」是**有意**的：加载是约 0.85s 的纯
CPU 过程，压在事件循环上等于单进程里的几十个实例一起停摆。

但同一条规则在测试里会变成不确定性：一条用例切出什么词，取决于那个后台线程有没有跑完，
于是同一份断言可能红也可能绿、还与用例执行顺序有关。所以这里在**任何用例开始之前**一次性
把词典加载好，让整个会话恒定跑在生产稳态（词典档）上。

jieba 缺席时（它是 `pyproject.toml` 里的正式依赖，正常不会）这个固件是空操作，依赖词典档的
用例由 `tests/test_memory_chinese_segmentation.py:require_jieba()` 自行 skip。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 整个测试会话的数据目录：在导入 nekro_agent 之前指到一个临时目录
# ---------------------------------------------------------------------------
#
# NA 的数据目录取自环境变量 `NEKRO_DATA_DIR`（缺省 `./data/nekro_agent`，按工作目录解析），而且是在
# 导入 `nekro_agent.core.os_env` 时一次定下的：配置文件路径、系统数据、上传、日志、工作区等几十个
# 路径常量都在导入时由它拼出来，核心配置导入时还会读、并写回 `<数据目录>/configs/nekro-agent.yaml`。
# 不改道的话，用例读写的就是仓库里的 `data/`：以前跑用例时生成的配置文件留着旧的默认值，改了代码里
# 的默认值之后，读到的还是文件里的旧值；同一份用例换个 worktree、或者前后跑两遍，结果可以不一样。
#
# 所以 conftest 一被导入（早于收集、早于任何测试模块导入 nekro_agent）就建一个本会话专用的临时目录，
# 设成 `NEKRO_DATA_DIR`。子进程（用例里起的 pytest、python）继承环境变量：看到 `NA_PYTEST_DATA_DIR`
# 就沿用同一个目录，不另建，也不在退出时删它；只有建它的那个进程在会话结束时删掉。
# 已经按用例改道的那些（下面的各个 `_isolated_*` 固件）照旧，它们管的是「用例之间互不影响」。

DATA_DIR_ENV = "NEKRO_DATA_DIR"
SESSION_DATA_DIR_ENV = "NA_PYTEST_DATA_DIR"
REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_DATA_DIR = REPO_ROOT / "data"


def point_data_dir_away_from_the_repo() -> tuple[Path, bool]:
    """把本进程的 `NEKRO_DATA_DIR` 指到本会话的临时数据目录

    Returns:
        (数据目录, 是否由本进程新建)；沿用上级会话的目录时第二项为 False

    Raises:
        RuntimeError: nekro_agent 已经先被导入了：数据目录已经按别的值定下，改环境变量也来不及
    """
    inherited = os.environ.get(SESSION_DATA_DIR_ENV)
    if inherited and Path(inherited).is_dir():
        os.environ[DATA_DIR_ENV] = inherited
        return Path(inherited), False
    if "nekro_agent.core.os_env" in sys.modules:
        raise RuntimeError(
            "nekro_agent 在 tests/conftest.py 之前就被导入了，数据目录已经按 "
            f"{sys.modules['nekro_agent.core.os_env'].OsEnv.DATA_DIR!r} 定下，测试会读写那里；"
            "检查是不是有 pytest 插件或 -p 参数提前导入了 nekro_agent",
        )
    data_dir = Path(tempfile.mkdtemp(prefix="na-pytest-")) / "nekro_agent"
    data_dir.mkdir()
    os.environ[DATA_DIR_ENV] = str(data_dir)
    os.environ[SESSION_DATA_DIR_ENV] = str(data_dir)
    return data_dir, True


SESSION_DATA_DIR, _OWNS_SESSION_DATA_DIR = point_data_dir_away_from_the_repo()


def snapshot_repo_data_dir(root: Path | None = None) -> dict[str, tuple[int, int] | None]:
    """仓库 `data/`（或给定目录）下每一项：文件记（大小, 修改时间），目录记 None；不跟随链接

    缺省时现取模块里的 `REPO_DATA_DIR`，回归用例在子进程里把它换成一个临时目录来验这道检查。
    """
    root = REPO_DATA_DIR if root is None else root
    entries: dict[str, tuple[int, int] | None] = {}
    if not root.is_dir():
        return entries
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for name in dirnames:
            entries[(base / name).relative_to(root).as_posix() + "/"] = None
        for name in filenames:
            path = base / name
            try:
                stat = path.lstat()
            except OSError:
                continue
            entries[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return entries


def compare_repo_data_dir(
    before: dict[str, tuple[int, int] | None],
    after: dict[str, tuple[int, int] | None],
) -> list[str]:
    """两份快照之间新增、改动、删除了哪些（路径相对仓库根）"""
    changes = [f"新增 data/{key}" for key in sorted(after.keys() - before.keys())]
    changes += [f"改动 data/{key}" for key in sorted(before.keys() & after.keys()) if before[key] != after[key]]
    changes += [f"删除 data/{key}" for key in sorted(before.keys() - after.keys())]
    return changes


# 会话开始时（导入 nekro_agent 之前）仓库 data/ 的样子；会话结束时比一次，见 `pytest_sessionfinish`
_REPO_DATA_AT_START = snapshot_repo_data_dir()

# CI 的「Run Backend Tests」步骤在 pytest 写完报告后进程不退出时会强杀它，那之后 shell 拿到的退出码
# 是杀出来的、不再代表 pytest 自己的结论。这里让 pytest 在会话结束时把**自己的**退出状态落盘
# （路径由环境变量给，未设则什么都不做），判定步骤据此区分「正常收尾后挂住被杀」与「中途被中断」。
# 挂住的根因（用例里开了没关的 aiosqlite 连接）由下面的 `close_leaked_aiosqlite_connections` 收掉，
# CI 的强杀留作保险。
SESSION_STATUS_FILE_ENV = "NA_PYTEST_SESSION_STATUS_FILE"

# 关一条泄漏连接最多等几秒；sqlite 的 close 是本地调用，正常远用不到
_LEAKED_CONNECTION_CLOSE_TIMEOUT = 5.0


def close_leaked_aiosqlite_connections(candidates: list[threading.Thread] | None = None) -> list[str]:
    """把用例里开了没关的 aiosqlite 连接关掉，返回关掉的连接线程名

    `candidates` 限定只处理这几条，免得在会话中途把别的连接也关了（每条用例开始前摘掉遗留连接时、
    回归用例里用）。缺省处理进程里所有还活着的 aiosqlite 连接线程。

    ## 为什么 pytest 打完汇总后进程不退出

    aiosqlite 的每条连接是一条**非守护**线程，平时阻塞在队列上等下一条指令，只有 `close()`
    （或连接失败）会让它退出。解释器退出时要 join 所有非守护线程，所以只要还有一条连接没关，
    pytest 打完汇总后就一直挂着。

    泄漏的来路：用例收尾时 `Tortoise.close_connections()` 只丢掉连接，库配置还在；之后别的用例
    （或用例里起的后台任务，比如用量记账的后台写库）一碰模型，Tortoise 就按旧配置懒建一条新连接。
    这条连接没人关，它所在的事件循环随用例关掉，线程却留了下来。连上与否取决于后台任务在
    事件循环关掉前跑到哪一步，所以同一组用例有时挂、有时不挂。

    关闭用一个新的事件循环：aiosqlite 的线程只是把结果投回调用方那个 future 所在的循环，
    与建连接时的循环无关，所以在这里 `close()` 是正常关闭（sqlite 连接在它自己的线程里关），
    不是强行结束线程。
    """
    try:
        from aiosqlite.core import Connection
    except ImportError:
        return []

    pool = threading.enumerate() if candidates is None else candidates
    leaked = [t for t in pool if isinstance(t, Connection) and t.is_alive()]
    if not leaked:
        return []

    async def _close_all() -> None:
        # gather 必须在协程里调：会话收尾时主线程上没有「当前事件循环」
        await asyncio.gather(
            *(asyncio.wait_for(conn.close(), _LEAKED_CONNECTION_CLOSE_TIMEOUT) for conn in leaked),
            return_exceptions=True,
        )

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close_all())
    except Exception:  # noqa: BLE001 - 收尾尽力而为，下面照样让线程退出
        pass
    finally:
        loop.close()
    for conn in leaked:
        if conn.is_alive():
            # 发起了连接、还没连上就被丢下的（任务在 `await aiosqlite.connect()` 半途被取消），
            # `close()` 直接返回、不会停线程；关失败的也走这里。让线程从队列里取到停止标记
            # 自行退出，aiosqlite 自己在 close 与连接失败时也是这么做的
            stop = getattr(conn, "_stop_running", None)
            if stop is not None:
                stop()
        conn.join(timeout=_LEAKED_CONNECTION_CLOSE_TIMEOUT)
    return [conn.name for conn in leaked]


def keep_aiosqlite_threads_alive_after_their_loop_closes() -> bool:
    """aiosqlite 的连接线程把结果交回一个已经关掉的事件循环时，丢掉结果、接着干活，不要退出

    ## 为什么全量会偶发卡死

    aiosqlite 每条连接一条工作线程，查询结果用 `future.get_loop().call_soon_threadsafe(...)` 交回发起
    调用的那个事件循环。循环已经关了的话这一步抛 `RuntimeError`；原版在 `except` 里用同一个办法交异常，
    又抛一次，工作线程就此退出，连接却还标着「在跑」。之后在这条连接上的任何调用（包括 `close()`）
    都只是把活放进一个没人取的队列，调用方永远等不到回复：主线程停在事件循环的 `select` 里，没有报错，
    也没有超时。

    测试里正好有这条路：每条用例一个事件循环，用例结束时循环关掉；用例（或它起的后台任务）在 Tortoise
    上一份配置留下的连接上还有查询在跑，循环就关了，工作线程退出。下一条用 `Tortoise.init` 的用例会先
    关掉这些遗留连接——关到那一条就挂住。查询赶不赶得上在循环关掉前跑完看时机，所以是偶发的，而且
    挂住的位置在离出事的用例最近的下一次 `Tortoise.init`。

    这里把工作线程换成「交不出去就丢掉」的版本：结果本来就没人收了，线程留着，之后的 `close()` 照常
    关掉连接、照常回复。只有交结果这一步不同，其余与 aiosqlite 0.20 的 `Connection.run` 一致。aiosqlite
    换了内部结构（取不到下面这几个名字）就不打补丁，`tests/test_shared_state_across_event_loops.py`
    会红，提示回来看。

    Returns:
        是否装上了补丁
    """
    try:
        from aiosqlite import core
    except ImportError:
        return False

    sentinel = getattr(core, "_STOP_RUNNING_SENTINEL", None)
    set_result = getattr(core, "set_result", None)
    set_exception = getattr(core, "set_exception", None)
    if sentinel is None or set_result is None or set_exception is None or not hasattr(core.Connection, "run"):
        return False
    if getattr(core.Connection.run, "survives_closed_loops", False):
        return True

    def deliver(future: asyncio.Future, setter: Callable[..., object], value: object) -> None:
        loop = future.get_loop()
        try:
            loop.call_soon_threadsafe(setter, future, value)
        except RuntimeError:
            if not loop.is_closed():
                raise
            # 发起这次调用的事件循环已经关了：结果没人收，丢掉

    def run(self: "core.Connection") -> None:
        while True:
            tx_item = self._tx.get()
            if tx_item is sentinel:
                break
            future, function = tx_item
            try:
                result = function()
            except BaseException as exc:  # noqa: BLE001 - 与 aiosqlite 一样原样交给调用方
                deliver(future, set_exception, exc)
            else:
                deliver(future, set_result, result)

    run.survives_closed_loops = True  # type: ignore[attr-defined]
    core.Connection.run = run  # type: ignore[method-assign]
    return True


keep_aiosqlite_threads_alive_after_their_loop_closes()


def drop_tortoise_connections_left_by_earlier_tests() -> list[str]:
    """把之前的用例（或它们的后台任务）留在 Tortoise 里的连接关掉、摘掉，返回摘掉的连接别名

    ## 为什么每条用例开始前都要做

    用例收尾时 `Tortoise.close_connections()` 只关连接、不清配置；之后后台任务（用量记账、审计队列、
    撤销工作区凭据等）一碰模型，Tortoise 就按旧配置懒建一个新的客户端，放进全进程共用的连接表。
    这个客户端属于一个随用例关掉的事件循环，留到后面的用例里有两种坏法，都是让调用方永远等下去：

    - 循环关掉时它还有查询在连接线程里跑：见 `keep_aiosqlite_threads_alive_after_their_loop_closes`；
    - 后台任务正在懒建连接时被取消：Tortoise 的 `ConnectionWrapper.__aenter__` 先拿锁、再建连接，
      建连接这一步被取消时不放锁。锁就一直锁着，下一个在这个客户端上查询的调用方（不调 `Tortoise.init`、
      直接用模型的用例）永远等不到锁。

    所以每条用例开始前（还没有事件循环在跑，表里的客户端只可能是之前留下的）把它们摘掉，连接线程用
    `close_leaked_aiosqlite_connections` 正常关掉。配置照旧留着：之后要懒建的照样能建，拿到的是新的
    客户端。用例自己的 Tortoise 固件都是函数级的，没有跨用例共用连接的，摘掉不会误伤。
    """
    try:
        from aiosqlite.core import Connection as SqliteConnectionThread
        from tortoise.connection import connections
    except ImportError:
        return []

    storage = connections._get_storage()  # noqa: SLF001 - Tortoise 没有公开「列出已建客户端」的接口
    if not storage:
        return []
    leftovers = list(storage.items())
    storage.clear()
    threads: list[threading.Thread] = []
    for _alias, client in leftovers:
        conn = getattr(client, "_connection", None)
        if isinstance(conn, SqliteConnectionThread):
            threads.append(conn)
    if threads:
        close_leaked_aiosqlite_connections(threads)
    return [alias for alias, _client in leftovers]


@pytest.fixture(autouse=True)
def _no_tortoise_connection_from_an_earlier_test() -> None:
    """见 `drop_tortoise_connections_left_by_earlier_tests`"""
    drop_tortoise_connections_left_by_earlier_tests()


def report_writes_to_the_repo_data_dir(session: pytest.Session) -> list[str]:
    """会话结束时仓库 `data/` 和开始时比：有新增、改动或删除就报出路径，并让本次会话判失败"""
    changes = compare_repo_data_dir(_REPO_DATA_AT_START, snapshot_repo_data_dir())
    if not changes:
        return changes
    title = f"用例读写了仓库里的 data/ 目录（{len(changes)} 处），应当只用本会话的临时数据目录"
    shown = changes[:50] + ([f"……另有 {len(changes) - 50} 处"] if len(changes) > 50 else [])
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_sep("!", title, red=True)
        for line in shown:
            reporter.write_line(line, red=True)
    else:
        print(title, *shown, sep="\n", file=sys.stderr)
    if session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    return changes


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int | pytest.ExitCode) -> None:  # noqa: ARG001
    report_writes_to_the_repo_data_dir(session)
    target = os.environ.get(SESSION_STATUS_FILE_ENV)
    if target:
        # 用 `session.exitstatus`：上面的检查可能把本次会话改判为失败
        Path(target).write_text(str(int(session.exitstatus)), encoding="utf-8")


def pytest_unconfigure(config: pytest.Config) -> None:  # noqa: ARG001
    """建临时数据目录的那个进程在最后删掉它；沿用上级会话目录的子进程不删"""
    if not _OWNS_SESSION_DATA_DIR:
        return
    try:
        from loguru import logger

        logger.remove()  # 日志文件开在数据目录里，Windows 上开着的文件删不掉
    except Exception:  # noqa: BLE001 - 收尾尽力而为
        pass
    shutil.rmtree(SESSION_DATA_DIR.parent, ignore_errors=True)


class _LeakedConnectionCloser:
    """会话收尾时关掉泄漏的 aiosqlite 连接

    单独挂成一个插件，是为了用 `trylast`：用例被中途打断（`-x`、Ctrl+C）时，会话级固件是在
    pytest 自己的 `pytest_sessionfinish` 里收尾的，关连接必须排在它后面，不能抢在固件之前。
    同一个 conftest 里一个钩子名只能定义一次，而上面落盘退出状态的那个要 `tryfirst`。
    """

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        closed = close_leaked_aiosqlite_connections()
        if closed:
            reporter = session.config.pluginmanager.get_plugin("terminalreporter")
            if reporter is not None:
                reporter.write_line(
                    f"会话结束时关掉了 {len(closed)} 条用例里开了没关的 aiosqlite 连接（不关的话进程退不出去）",
                    yellow=True,
                )


def pytest_configure(config: pytest.Config) -> None:
    config.pluginmanager.register(_LeakedConnectionCloser(), "na-leaked-aiosqlite-closer")


@pytest.fixture(scope="session", autouse=True)
def _warm_up_memory_tokenizer() -> None:
    """在收集之后、第一条用例之前把切词词典加载好，消除「哪一档生效」的时序依赖。"""
    from nekro_agent.services.memory.text_terms import warm_up_tokenizer

    warm_up_tokenizer()


@pytest.fixture(autouse=True)
def _fresh_rpc_decode_budget() -> None:
    """沙盒 RPC 的解码时间预算按容器键记账；各用例共用同几个容器键，上一条用例的欠账不该让下一条被拒"""
    from nekro_agent.services.rpc_service import decode_budget

    decode_budget.reset()


@pytest.fixture(autouse=True)
def _fresh_embedding_health(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """嵌入健康状态按模型组记在进程内存里：各用例共用同几个测试组名，上一条用例的失败串不该带进下一条，
    更不该在跑用例时凑够次数、真的往外发一条提醒或去查库定归属。要验提醒与归属的用例自己换
    `alert_sender` / `owner_resolver`。缺向量段数的进程内缓存也按用例清掉。"""
    from nekro_agent.services import embedding_health

    async def _no_alert(*_args: object) -> None:
        return None

    async def _no_owner(_model_group: str) -> tuple[str, None]:
        return "", None

    embedding_health.tracker.reset()
    embedding_health.clear_missing_vectors_cache()
    monkeypatch.setattr(embedding_health.tracker, "alert_sender", _no_alert)
    monkeypatch.setattr(embedding_health.tracker, "owner_resolver", _no_owner)
    yield
    embedding_health.tracker.reset()
    embedding_health.clear_missing_vectors_cache()


@pytest.fixture(autouse=True)
def _no_plugin_registered_tavern_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    """发送路径的变量块接收方、导入卡片的状态模板钩子都是插件在模块导入时登记的，登记了就一直在进程里：
    哪条用例先导入了角色状态插件，后面所有用例的发送与导入就都会走插件那条路（取块、多查一次人设、报告多一段），
    用例的断言因此可能被插件代码兜住的异常悄悄放过。这里每条用例都从空注册表开始；要验插件那条路的用例自己登记。"""
    from nekro_agent.services.tavern import state_hook, variable_blocks

    monkeypatch.setattr(variable_blocks, "_sinks", {})
    monkeypatch.setattr(state_hook, "_hooks", {})


@pytest.fixture(autouse=True)
def _isolated_web_chat_workspace_taint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """网页聊天 MCP 的「曾经不归平台方」标记按用例隔离

    置位现在挂在归属推导上，任何用例推导出租户归属都会写这个文件；不隔离就会写进会话共用数据目录的
    `system` 下，带进后面的用例。模块级的内存集合跨用例残留，还会让下一条用例里同 id 的工作区被误判成被标记。
    所以落盘改道到临时目录，内存集合与待重建标志、重建锁一并重置；收尾取消没跑完的清理任务。
    """
    from nekro_agent.services.mcp import web_chat_workspace_taint as taint

    monkeypatch.setattr(taint, "taint_file", lambda: tmp_path / "web_chat_mcp_tainted_workspaces.json")
    monkeypatch.setattr(taint, "_memory_tainted", set())
    monkeypatch.setattr(taint, "_rebuild_pending", False)
    monkeypatch.setattr(taint, "_rebuild_lock", None)

    yield

    for task in list(taint._scrub_tasks):
        if not task.done():
            task.cancel()
    taint._scrub_tasks.clear()


@pytest.fixture(autouse=True)
def _isolated_memory_restore_marker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """「按旧半衰期捞回已做完」的标记（段落与关系各一份）按用例隔离

    记忆调度器启动时、清理循环每轮都会调一次性的捞回；任何一条起了调度器又建了库的用例完整走完一遍，
    就会把标记写进数据目录的 `system/memory`，之后别的用例里的捞回直接返回、测不到东西。改道到本用例的
    临时目录。
    """
    from nekro_agent.services.memory import relation_retention, retention

    monkeypatch.setattr(retention, "RESTORE_MARKER_PATH", tmp_path / "retention_restore_v1.json")
    monkeypatch.setattr(relation_retention, "RELATION_RESTORE_MARKER_PATH", tmp_path / "relation_restore_v1.json")


@pytest.fixture(autouse=True)
def _isolated_plugin_scope_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """插件按实例 / 按频道的设置（启停覆盖与实例级配置目录）按用例隔离

    彻底删除实例、划转、换会话标识、实例迁移都会删或搬这些文件；不隔离的话，任何一条走到这些路径的
    用例都会改动 `DATA_DIR/plugin_data` 下的设置（删掉别的用例里同名实例的插件配置）。
    用例自己的固件再改道一次不受影响（函数级固件在这个之后执行）。两份进程内缓存前后各清一次。
    """
    from nekro_agent.services.plugin import scope as plugin_scope

    data_dir = tmp_path / "isolated_plugin_data"
    monkeypatch.setattr(plugin_scope, "PLUGIN_DATA_DIR", data_dir)
    monkeypatch.setattr(plugin_scope, "SCOPE_OVERRIDE_DIR", data_dir / "_overrides")
    plugin_scope._scope_override_cache.clear()  # noqa: SLF001
    plugin_scope._scoped_config_cache.clear()  # noqa: SLF001

    yield

    plugin_scope._scope_override_cache.clear()  # noqa: SLF001
    plugin_scope._scoped_config_cache.clear()  # noqa: SLF001


def _data_dir_at_import() -> str:
    from nekro_agent.core.os_env import OsEnv

    return OsEnv.DATA_DIR


# 本会话共用的数据目录（见文件开头的 `point_data_dir_away_from_the_repo`）：在 conftest 导入时（任何用例、
# 任何模块级固件改它之前）记下。记在第一条用例里的话，模块级固件（`scope="module"` / 导入时就改了
# `DATA_DIR` 的测试模块）先跑，记下的就成了那个临时目录，之后没改道的用例被当成「改道过」，照样写进
# 会话共用的目录、互相影响
_ORIGINAL_DATA_DIR = _data_dir_at_import()


@pytest.fixture(autouse=True)
def _isolated_lifecycle_file_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """生命周期会删 / 写的那几处数据目录按用例隔离：邮箱附件、表情文件、核心配置的适配器层 / 实例层 /
    频道层覆盖、适配器自己的配置文件

    彻底删除实例、划转、删账户、换会话标识与实例迁移都会删或搬这些目录下的文件；建实例前的兜底清扫
    还会改写平台方邮箱的共享名单（写 `configs/email/config.yaml`）。它们的根目录有的是导入时算好的常量
    （表情文件、频道覆盖），有的在调用时按 `OsEnv.DATA_DIR` 现算（附件、各层覆盖、适配器配置）：
    用例没改道 `DATA_DIR` 时一律落进会话共用的数据目录，一条用例删掉、改掉的，下一条用例就读到了。

    - 用例自己把 `OsEnv.DATA_DIR` 改到了临时目录：按它现算（这些用例本来就在那下面放文件、核对路径）；
    - 没改：改到本用例的临时目录。
    用例自己的固件再改道一次不受影响（函数级固件在这个之后执行）。
    """
    from nekro_agent import adapters
    from nekro_agent.core.os_env import OsEnv
    from nekro_agent.services import config_service, email_accounts, emotion_library

    isolated = tmp_path / "isolated_data"

    def data_root() -> Path:
        return Path(OsEnv.DATA_DIR) if OsEnv.DATA_DIR != _ORIGINAL_DATA_DIR else isolated

    monkeypatch.setattr(email_accounts, "email_attachment_root", lambda: data_root() / "uploads" / "email_attachment")
    monkeypatch.setattr(
        config_service,
        "instance_override_root_dir",
        lambda adapter_key: data_root() / "configs" / adapter_key / "instances",
    )
    monkeypatch.setattr(
        config_service,
        "adapter_override_config_path",
        lambda adapter_key: data_root() / "configs" / adapter_key / "overrides.yaml",
    )
    monkeypatch.setattr(
        adapters,
        "get_adapter_config_path",
        lambda adapter_key: data_root() / "configs" / adapter_key / "config.yaml",
    )
    monkeypatch.setattr(config_service, "CHANNEL_CONFIG_DIR", isolated / "configs" / "channels")
    monkeypatch.setattr(emotion_library, "EMOTION_FILES_DIR", isolated / "plugin_data" / "emotions")
    yield


def _workspace_root_at_import() -> str:
    from nekro_agent.services.workspace import manager as workspace_manager

    return workspace_manager.WORKSPACE_ROOT_DIR


# 同上：会话共用的 CC 工作区根目录（在会话的数据目录下），在任何用例改它之前记下
_ORIGINAL_WORKSPACE_ROOT_DIR = _workspace_root_at_import()


@pytest.fixture(autouse=True)
def _isolated_workspace_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """CC 工作区根目录按用例隔离

    新建工作区会清掉同号残留的 CC home，删除工作区会删掉它的 CC home（工作区根下的
    `.nk-homes/<id>`、工作区目录里的 `.claude_home`）。走到这两条路径、又没改道工作区根的用例
    （例如经 HTTP 删工作区的级联用例）会删掉别的用例里同号工作区的会话历史。用例自己改道的
    （函数级固件在这个之后执行）不受影响。
    """
    from nekro_agent.services.workspace import manager as workspace_manager

    if workspace_manager.WORKSPACE_ROOT_DIR == _ORIGINAL_WORKSPACE_ROOT_DIR:
        monkeypatch.setattr(workspace_manager, "WORKSPACE_ROOT_DIR", str(tmp_path / "isolated_workspaces"))
    yield
