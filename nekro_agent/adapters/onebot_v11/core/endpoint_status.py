"""OneBot 协议端识别与「上报自身消息」自检

## 判据

OneBot 标准动作 `get_version_info` 会回 `app_name` / `app_version` / `protocol_version`，
据此认出连上来的是 SnowLuma、NapCat 还是别的实现。

「上报自身消息」（`reportSelfMessage`）**没有任何标准动作能查**。能观察到的是它的效果：
开着时，NA 通过 `send_msg` / `send_group_msg` / `send_private_msg` 发出的每条消息，
协议端都会以 `post_type=message_sent` 回传一份；关着时什么都不会回来。因此这里记两件事：

- NA 自己成功发出、带 `message_id` 的消息条数（`own_sends`）；
- 本次连接以来收到的自身消息回传条数（`echoes`，含 NA 自己那部分）。

判断看的是**上一条回传之后**的发送：从那以后 NA 又发了至少 `min_unechoed_sends` 条、
其中第一条距今满 `grace_seconds`，还是一条回传都没来，就判成「疑似未开启」；否则只要收到过
回传就是「开启」。不按「这次连接收到过就一直算开」：SnowLuma 改这个开关是热重载、不断线，
中途关掉不会触发重连，只有按「最近有没有回传」才看得出来。等满 `grace_seconds` 是因为回传
可能晚到。「疑似未开启」是推测，不是事实，文案也按推测写。

## 为什么只在内存

这份状态描述的是**这一次连接**：连接一断，回声还会不会来就重新变成了未知数，
旧连接的计数留着只会让重连后的判断继续引用一份作废的证据。进程重启同理。
因此不落库，新连接时 `reset` 该账号的全部计数与版本信息。

上传文件、合并转发、AI 语音这些发送不计入 `own_sends`：它们的回传形态在各实现之间
并不统一（有的只回 `file_id`、有的回 `message_id: 0`），计进去会把「没开」误判成「开了」。
"""

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional, Set

from nonebot.adapters.onebot.v11 import Bot

from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("adapter.onebot_v11")

# 查询版本的超时。协议端没响应时不能让连接回调跟着挂住
VERSION_QUERY_TIMEOUT_SECONDS = 10.0


class SelfReportState(str, Enum):
    """「上报自身消息」的判断结果"""

    ON = "on"
    OFF_SUSPECTED = "off_suspected"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EndpointInfo:
    """协议端自报的身份"""

    app_name: str = ""
    app_version: str = ""
    protocol_version: str = ""


@dataclass(frozen=True)
class EndpointSnapshot:
    """某个账号在这一刻的状态快照"""

    info: Optional[EndpointInfo]
    implementation: str
    self_report: SelfReportState
    own_sends: int
    echoes: int
    connected_at: Optional[float]
    last_echo_at: Optional[float]


@dataclass
class _AccountState:
    info: Optional[EndpointInfo] = None
    own_sends: int = 0
    echoes: int = 0
    connected_at: Optional[float] = None
    last_echo_at: Optional[float] = None
    # 上一条回传之后计入的发送条数，以及其中第一条的时刻（单调时钟）；收到回传时清零
    unechoed_sends: int = 0
    first_unechoed_send_at: Optional[float] = None
    # 一段「没有回传」只提醒一次，否则每发一条消息都会再告警；收到回传后可以再提醒
    warned: bool = False


def _implementation_of(info: Optional[EndpointInfo]) -> str:
    if info is None or not info.app_name:
        return "unknown"
    name = info.app_name.lower()
    if "snowluma" in name:
        return "snowluma"
    if "napcat" in name:
        return "napcat"
    return "other"


def parse_endpoint_info(payload: Any) -> Optional[EndpointInfo]:
    """把 `get_version_info` 的返回值收敛成 `EndpointInfo`；不是字典时返回 None"""
    if not isinstance(payload, dict):
        return None
    return EndpointInfo(
        app_name=str(payload.get("app_name") or ""),
        app_version=str(payload.get("app_version") or ""),
        protocol_version=str(payload.get("protocol_version") or ""),
    )


async def query_endpoint_info(bot: Bot, timeout: float = VERSION_QUERY_TIMEOUT_SECONDS) -> Optional[EndpointInfo]:
    """调一次 `get_version_info`

    连不上、超时、返回非字典都按「查不到」处理：这是附加信息，失败不该影响连接本身。
    """
    account = str(bot.self_id)
    try:
        payload = await asyncio.wait_for(bot.call_api("get_version_info"), timeout=timeout)
    except Exception as e:
        logger.debug(f"查询账号 {account} 的协议端版本失败: {e}")
        return None
    info = parse_endpoint_info(payload)
    if info is None:
        logger.debug(f"账号 {account} 的 get_version_info 返回了非字典值: {type(payload).__name__}")
    return info


class EndpointStatusTracker:
    """按账号记录协议端身份与自身消息回传情况；进程重启即清空"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        min_unechoed_sends: int = 3,
        grace_seconds: float = 60.0,
    ) -> None:
        self._clock = clock
        self._wall_clock = wall_clock
        self._min_unechoed_sends = min_unechoed_sends
        self._grace_seconds = grace_seconds
        self._states: Dict[str, _AccountState] = {}

    def reset(self, account: str) -> None:
        """新连接：清空该账号的计数与版本信息，记下连接时刻"""
        self._states[account] = _AccountState(connected_at=self._wall_clock())

    def note_info(self, account: str, info: Optional[EndpointInfo]) -> None:
        """记下协议端自报的身份"""
        self._states.setdefault(account, _AccountState()).info = info

    def note_own_send(self, account: str) -> None:
        """NA 自己成功发出一条带编号的消息"""
        state = self._states.setdefault(account, _AccountState())
        state.own_sends += 1
        state.unechoed_sends += 1
        if state.first_unechoed_send_at is None:
            state.first_unechoed_send_at = self._clock()
        self._maybe_warn(account, state)

    def note_echo(self, account: str) -> None:
        """收到一条自身消息回传（NA 自己发的也算证据）"""
        state = self._states.setdefault(account, _AccountState())
        state.echoes += 1
        state.last_echo_at = self._wall_clock()
        state.unechoed_sends = 0
        state.first_unechoed_send_at = None
        state.warned = False

    def snapshot(self, account: str) -> EndpointSnapshot:
        """取某一刻的状态；没见过的账号返回全空的 UNKNOWN，不抛异常"""
        state = self._states.get(account)
        if state is None:
            return EndpointSnapshot(
                info=None,
                implementation="unknown",
                self_report=SelfReportState.UNKNOWN,
                own_sends=0,
                echoes=0,
                connected_at=None,
                last_echo_at=None,
            )
        self._maybe_warn(account, state)
        return EndpointSnapshot(
            info=state.info,
            implementation=_implementation_of(state.info),
            self_report=self._self_report_of(state),
            own_sends=state.own_sends,
            echoes=state.echoes,
            connected_at=state.connected_at,
            last_echo_at=state.last_echo_at,
        )

    def _self_report_of(self, state: _AccountState) -> SelfReportState:
        # 先看最近这一段：收到过回传、后来被热重载关掉的，也要能认出来
        if (
            state.unechoed_sends >= self._min_unechoed_sends
            and state.first_unechoed_send_at is not None
            and self._clock() - state.first_unechoed_send_at >= self._grace_seconds
        ):
            return SelfReportState.OFF_SUSPECTED
        if state.echoes > 0:
            return SelfReportState.ON
        return SelfReportState.UNKNOWN

    def _maybe_warn(self, account: str, state: _AccountState) -> None:
        """一段没有回传的时间里第一次判成「疑似未开启」时提醒一次

        `note_own_send` 与 `snapshot` 都会走到这里：先发够条数的那一方负责告警，
        之后无论谁再问都不再重复，直到再收到回传。
        """
        if state.warned or self._self_report_of(state) is not SelfReportState.OFF_SUSPECTED:
            return
        state.warned = True
        info = state.info
        endpoint = f"{info.app_name} {info.app_version}".strip() if info and info.app_name else "类型未知"
        logger.warning(
            f"账号 {account} 的协议端（{endpoint}）似乎没开「上报自身消息」："
            f"NA 连续发出 {state.unechoed_sends} 条消息，一条回传都没收到。"
            "同账号其他程序发的消息不会进聊天记录"
        )


endpoint_status = EndpointStatusTracker()

# `asyncio.create_task()` 的返回值只被事件循环弱引用，不持有就可能在查询途中被 GC 掉
_background_tasks: Set[asyncio.Task] = set()


def _forget_task(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    if not task.cancelled():
        # 异常已在任务内吞掉；这里只是把结果取出来，避免「Task exception was never retrieved」
        task.exception()


async def _probe_endpoint(bot: Bot) -> None:
    account = str(bot.self_id)
    info = await query_endpoint_info(bot, VERSION_QUERY_TIMEOUT_SECONDS)
    if info is None:
        return
    endpoint_status.note_info(account, info)
    logger.info(f"账号 {account} 的协议端：{info.app_name} {info.app_version}")


async def on_onebot_connect(bot: Bot) -> None:
    """连接建立时清空旧状态，并在后台问一次协议端版本

    查询必须放在后台：连接回调阻塞在协议端身上时，消息接收也会跟着停住。
    """
    account = str(bot.self_id)
    endpoint_status.reset(account)
    task = asyncio.create_task(_probe_endpoint(bot))
    _background_tasks.add(task)
    task.add_done_callback(_forget_task)
