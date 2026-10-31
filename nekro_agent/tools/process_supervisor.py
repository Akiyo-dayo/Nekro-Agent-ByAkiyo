"""「重启系统」到底做不做得到 —— 进程监管者探测

## 为什么需要这个模块

`POST /api/restart` 原来只有一条能走通的路：`OsEnv.RUN_IN_DOCKER` 时调 Docker API 重启自己的容器。
其余部署一律落到最后一行 `raise OperationFailedError(operation="重启系统")` ——
HTTP 500、文案只有「重启系统失败」，还带一整条 `logger.exception` 堆栈。
管理员看到的是「坏了」，实际是「这种部署形态压根没有这个能力」，于是去翻日志找一个不存在的 bug。

## 「重启」的前提是有人能把进程拉回来

进程自己退出之后不能自己再启动。所以这个动作**只有在存在监管者时才成立**：

- Docker：`docker restart` 由 daemon 执行，容器停了它负责再起来（沿用既有实现）。
- systemd：unit 配了 `Restart=always` 时，主进程无论以什么状态退出都会被重新拉起。

裸机 `uv run bot` / `setsid nohup .venv/bin/bot` 起来的进程**两者都不是**：
它的父进程确实是 PID 1，但那只是 `setsid` 之后被 init 收养，systemd 并不管它的生死
（实测 `/proc/<pid>/cgroup` 是 `0::/user.slice/user-0.slice/session-3017.scope` —— 一个 SSH
会话的 scope，不是 `*.service`）。这种部署下「重启」只能是**明确拒绝**：
做一个"退出之后再也起不来"的假重启，比报错糟得多。

## 判据刻意保守

宁可把一个其实能重启的部署判成不支持（管理员手动重启一次即可），也不能把一个起不来的部署
判成支持（服务永久下线）。因此 systemd 一侧同时要求三条，缺一即不支持：

1. `/proc/self/cgroup` 解析得出一个 `*.service` unit —— 说明我们真的跑在某个 unit 里；
2. `systemctl show` 读到的 `Restart` 恰好是 `always` —— `on-failure` 之流在**正常退出**时
   不会拉起来，而我们发的是优雅退出，判成支持就等于让服务死掉；
3. 该 unit 的 `MainPID` 就是本进程 —— 我们是被包在 wrapper 脚本里的子进程时，
   自己退出未必等于 unit 退出，也就未必触发重启。

触发方式是给自己发 `SIGTERM`（等价于 `systemctl restart` 停止阶段做的事），
让 uvicorn 走正常的优雅退出，剩下的交给 systemd。不用 `systemctl restart`：
那条路要求进程有权限操作 systemd（非 root 部署会被 polkit 拒绝），
而拒绝发生在我们已经准备好退出之后，失败得很难看。
"""

import asyncio
import os
import signal
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv

logger = get_sub_logger("system_control")

CGROUP_PATH = Path("/proc/self/cgroup")

# systemd 只有这个取值能保证「优雅退出后被重新拉起」。
# `on-failure` / `on-abnormal` / `on-abort` 都不覆盖 exit 0，`on-success` 不覆盖异常退出，
# `no` 显然不行 —— 全部按不支持处理，并在文案里告诉管理员该改成什么。
RESTART_ALWAYS = "always"

# 发出 SIGTERM 前留给响应体写回客户端的时间。Docker 一侧的 `container.restart()` 同样是
# 立刻返回、稍后才真的停容器，这里对齐成一个显式的量。
SELF_TERMINATE_DELAY_SECONDS = 1.0


class RestartMode(str, Enum):
    """当前部署形态下「重启」由谁来兜底"""

    DOCKER = "docker"
    SYSTEMD = "systemd"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class RestartCapability:
    """探测结果

    `reason_code` 给前端做 i18n 键，`detail` 是后端直接可读的中文说明（前端翻不到键时回落到它）。
    """

    mode: RestartMode
    supported: bool
    reason_code: str = ""
    detail: str = ""
    unit: Optional[str] = None


_UNSUPPORTED_DETAILS = {
    "not_posix": (
        "当前进程不在 Linux 上运行，后台无法接管重启。请在宿主上手动重启 Nekro Agent 进程。"
    ),
    "no_supervisor": (
        "当前是裸机部署，进程没有任何监管者（既不在 Docker 容器里，也不属于任何 systemd 服务），"
        "退出之后没有人能把它拉起来，因此后台不提供重启。"
        "请在宿主上手动重启；若希望后台可以重启，请把 Nekro Agent 托管为 systemd 服务"
        "并设置 Restart=always，或改用 Docker 部署。"
    ),
    "systemd_query_failed": (
        "进程属于某个 systemd 服务，但无法读取它的重启策略（systemctl 不可用或查询失败），"
        "无法确认退出后会被拉起来，因此后台不提供重启。请在宿主上手动执行 systemctl restart。"
    ),
    "systemd_restart_policy": (
        "所属 systemd 服务的 Restart 策略是 {restart}，正常退出后不会被重新拉起，"
        "因此后台不提供重启。把该服务的 Restart 改成 always 并 daemon-reload 之后即可从后台重启。"
    ),
    "systemd_not_main_process": (
        "当前进程不是所属 systemd 服务的主进程（服务主进程是 {main_pid}，本进程是 {pid}），"
        "自己退出未必会触发服务重启，因此后台不提供重启。请在宿主上手动执行 systemctl restart。"
    ),
}


def parse_systemd_unit(cgroup_text: str) -> Optional[str]:
    """从 `/proc/self/cgroup` 的内容里解析出所属的 systemd service unit

    cgroup v2 是 `0::/system.slice/nekro-agent.service`，
    v1 是 `1:name=systemd:/system.slice/nekro-agent.service`，两者都取最后一段冒号之后的路径。

    只认 `*.service`：SSH 会话的 `session-*.scope`、Docker 的 `docker-*.scope` 都不是服务，
    退出后没有人管；`user@0.service` 这类用户管理器所在的 slice 里也不会出现业务 unit 名。
    """
    for line in cgroup_text.splitlines():
        path = line.rpartition(":")[2].strip()
        if not path:
            continue
        for segment in reversed(path.split("/")):
            if segment.endswith(".service"):
                return segment
    return None


def parse_systemctl_show(output: str) -> dict[str, str]:
    """`systemctl show <unit> -p Restart -p MainPID` 的 `KEY=VALUE` 输出"""
    properties: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            properties[key.strip()] = value.strip()
    return properties


def evaluate_systemd_unit(unit: str, properties: dict[str, str], pid: int) -> RestartCapability:
    """三条判据都成立才算支持（判据的理由见模块 docstring）"""
    restart = properties.get("Restart", "")
    main_pid = properties.get("MainPID", "")
    if restart != RESTART_ALWAYS:
        return RestartCapability(
            mode=RestartMode.UNSUPPORTED,
            supported=False,
            reason_code="systemd_restart_policy",
            detail=_UNSUPPORTED_DETAILS["systemd_restart_policy"].format(restart=restart or "未知"),
            unit=unit,
        )
    if main_pid != str(pid):
        return RestartCapability(
            mode=RestartMode.UNSUPPORTED,
            supported=False,
            reason_code="systemd_not_main_process",
            detail=_UNSUPPORTED_DETAILS["systemd_not_main_process"].format(
                main_pid=main_pid or "未知",
                pid=pid,
            ),
            unit=unit,
        )
    return RestartCapability(mode=RestartMode.SYSTEMD, supported=True, unit=unit)


async def _read_systemctl_properties(unit: str) -> Optional[dict[str, str]]:
    """查 unit 的重启策略与主进程；systemctl 不存在或查询失败都返回 None"""
    try:
        process = await asyncio.create_subprocess_exec(
            "systemctl",
            "show",
            unit,
            "-p",
            "Restart",
            "-p",
            "MainPID",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=5)
    except (OSError, asyncio.TimeoutError) as e:
        logger.warning(f"查询 systemd unit {unit} 的重启策略失败: {e!s}")
        return None
    if process.returncode != 0:
        logger.warning(f"查询 systemd unit {unit} 的重启策略失败: systemctl 退出码 {process.returncode}")
        return None
    return parse_systemctl_show(stdout.decode("utf-8", "replace"))


def _read_cgroup() -> Optional[str]:
    try:
        return CGROUP_PATH.read_text(encoding="utf-8")
    except OSError:
        return None


async def probe_restart_capability() -> RestartCapability:
    """当前部署形态支不支持从后台重启

    Docker 优先：那条路由容器 daemon 兜底，与进程自身所在的 cgroup 无关。
    """
    if OsEnv.RUN_IN_DOCKER:
        return RestartCapability(mode=RestartMode.DOCKER, supported=True)

    if not sys.platform.startswith("linux"):
        return RestartCapability(
            mode=RestartMode.UNSUPPORTED,
            supported=False,
            reason_code="not_posix",
            detail=_UNSUPPORTED_DETAILS["not_posix"],
        )

    cgroup_text = _read_cgroup()
    unit = parse_systemd_unit(cgroup_text) if cgroup_text else None
    if not unit:
        return RestartCapability(
            mode=RestartMode.UNSUPPORTED,
            supported=False,
            reason_code="no_supervisor",
            detail=_UNSUPPORTED_DETAILS["no_supervisor"],
        )

    properties = await _read_systemctl_properties(unit)
    if properties is None:
        return RestartCapability(
            mode=RestartMode.UNSUPPORTED,
            supported=False,
            reason_code="systemd_query_failed",
            detail=_UNSUPPORTED_DETAILS["systemd_query_failed"],
            unit=unit,
        )
    return evaluate_systemd_unit(unit, properties, os.getpid())


async def _terminate_self_after_delay(delay: float) -> None:
    await asyncio.sleep(delay)
    logger.info("正在退出以触发 systemd 重启当前服务")
    os.kill(os.getpid(), signal.SIGTERM)


# 事件循环只弱引用任务，调用方不持有就可能在 `sleep` 期间被 GC —— 那会变成
# 「接口回了 ok、进程却没退出」，与 `docker_util._spawn_detached` 是同一个坑。
_PENDING_RESTART: set["asyncio.Task[None]"] = set()


def _on_restart_task_done(task: "asyncio.Task[None]") -> None:
    """任务无声无息地失败 = 接口回了 ok、进程却还在跑，与被 GC 掉是同一种事故

    正常路径下这个回调根本不会看到异常（进程在 `os.kill` 之后就没了），所以它只有在
    「本该退出却没退出」时才说话 —— 那正是唯一需要留下痕迹的时刻。
    """
    _PENDING_RESTART.discard(task)
    if task.cancelled():
        logger.error("重启任务在退出前被取消，进程不会重启")
        return
    exception = task.exception()
    if exception is not None:
        logger.error(f"触发进程退出失败，进程不会重启: {exception!s}")


def schedule_self_restart(delay: float = SELF_TERMINATE_DELAY_SECONDS) -> "asyncio.Task[None]":
    """安排一次优雅退出，由 systemd 把服务重新拉起来

    与 Docker 一侧一样立刻返回：先让 HTTP 响应写回去，再让进程退出。
    """
    task = asyncio.create_task(_terminate_self_after_delay(delay))
    _PENDING_RESTART.add(task)
    task.add_done_callback(_on_restart_task_done)
    return task
