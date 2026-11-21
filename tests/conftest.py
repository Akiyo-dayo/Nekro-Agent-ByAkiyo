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
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

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

    `candidates` 只给回归用例用：限定只处理这几条，免得在会话中途把别的用例的连接也关了。
    缺省处理进程里所有还活着的 aiosqlite 连接线程。

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


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int | pytest.ExitCode) -> None:
    target = os.environ.get(SESSION_STATUS_FILE_ENV)
    if target:
        Path(target).write_text(str(int(exitstatus)), encoding="utf-8")


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
def _isolated_web_chat_workspace_taint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """网页聊天 MCP 的「曾经不归平台方」标记按用例隔离

    置位现在挂在归属推导上，任何用例推导出租户归属都会写这个文件；不隔离就会写进真实的
    `data/system`。模块级的内存集合跨用例残留，还会让下一条用例里同 id 的工作区被误判成被标记。
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
def _isolated_plugin_scope_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """插件按实例 / 按频道的设置（启停覆盖与实例级配置目录）按用例隔离

    彻底删除实例、划转、换会话标识、实例迁移都会删或搬这些文件；不隔离的话，任何一条走到这些路径的
    用例都会改动 `DATA_DIR/plugin_data` 下的真实设置（删掉开发机上同名实例的插件配置）。
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


# 开发机上真实的数据目录：在 conftest 导入时（任何用例、任何模块级固件改它之前）记下。
# 记在第一条用例里的话，模块级固件（`scope="module"` / 导入时就改了 `DATA_DIR` 的测试模块）先跑，
# 记下的就成了那个临时目录，之后没改道的用例被当成「改道过」，照样写进真实目录
_ORIGINAL_DATA_DIR = _data_dir_at_import()


@pytest.fixture(autouse=True)
def _isolated_lifecycle_file_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """生命周期会删 / 写的那几处数据目录按用例隔离：邮箱附件、表情文件、核心配置的适配器层 / 实例层 /
    频道层覆盖、适配器自己的配置文件

    彻底删除实例、划转、删账户、换会话标识与实例迁移都会删或搬这些目录下的文件；建实例前的兜底清扫
    还会改写平台方邮箱的共享名单（写 `configs/email/config.yaml`）。它们的根目录有的是导入时算好的常量
    （表情文件、频道覆盖），有的在调用时按 `OsEnv.DATA_DIR` 现算（附件、各层覆盖、适配器配置）：
    用例没改道 `DATA_DIR` 时一律落进开发机的真实数据目录，删掉、改掉的就是真数据。

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
