"""
# 消息撤回 (Message Recall)

给 AI 一只「手」：把当前群里的一条消息撤回。常见用途是别人误发的密码、私钥、API Key、
证件号，或者 Bot 自己刚发错的话。

用不用、什么时候用、撤完说不说，全由 AI 自己判断；宿主只挡几种乱用：只能撤当前群、
目标必须是这个群库里真有的消息、每个群每小时有次数上限、失败时把协议端的原话照实交回。
不挂提示词注入、没有定时器与消息钩子：插件在哪个频道没生效，它在那里就什么都不做。
"""

import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

from pydantic import Field

from nekro_agent.adapters.onebot_v11.core.message_id import parse_message_id
from nekro_agent.adapters.onebot_v11.core.recall import (
    RECALL_BAD_ID,
    RECALL_FAILED,
    RECALL_OFFLINE,
    RECALL_OK,
    RECALL_TIMEOUT,
    RecallResult,
    get_group_role,
    recall_group_message,
)
from nekro_agent.api import i18n
from nekro_agent.api.plugin import ConfigBase, NekroPlugin, SandboxMethodType
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.services.agent.round_trigger_accounts import current_round_trigger_accounts
from nekro_agent.tools.history_time import format_history_time, history_time_candidates
from nekro_agent.tools.untrusted_content import echo_inline

# 插件自己的结果码。前五个与 `core/recall.py` 的撤回结果一一对应，其余是宿主在查库、
# 分拣、限流阶段给出的结论
PARTIAL = "partial"
NOT_FOUND = "not_found"
AMBIGUOUS_TIME = "ambiguous_time"
NO_MESSAGE_ID = "no_message_id"
ALREADY_RECALLED = "already_recalled"
RATE_LIMITED = "rate_limited"
BAD_ARGS = "bad_args"

# 每小时上限的滑动窗口（秒）
RATE_LIMIT_WINDOW_SECONDS = 3600
# 配置被写成负数时退回的上限（与字段默认值相同）。后台写插件配置只做类型转换、不跑 `ge=0`，
# 负数不能被当成「不限」
DEFAULT_MAX_PER_HOUR = 10
# 进程内最多同时留多少个频道的窗口；超过时把窗口已经整段过期的频道清掉
MAX_TRACKED_CHANNELS = 2000

# 日志里理由与协议端原话的长度上限
MAX_LOG_REASON_LENGTH = 80
MAX_LOG_DETAIL_LENGTH = 200
# 部分撤回时，返回文字里最多列几种不同的失败原因
MAX_REPORTED_FAILURE_DETAILS = 3

# 理由里连续这么多字符出现在目标正文里，就认为模型把原文抄进了理由
ORIGINAL_OVERLAP_CHARS = 6
REASON_REDACTED = "（理由里带了原文，已丢弃）"

# 每个频道的撤回时刻窗口（`time.monotonic()`）。进程内状态，重启清零；
# 上限挡的是「被人反复哄着撤」，不是跨天的配额。只有真正调到协议端的调用才进窗口
_recall_windows: Dict[str, Deque[float]] = {}

plugin = NekroPlugin(
    name="消息撤回",
    module_name="message_recall",
    description="让 AI 撤回当前群里的一条消息，例如别人误发的密码、私钥等敏感信息，或自己发错的消息",
    version="0.1.0",
    # 本分支自有的内置插件署本分支（同 `cold_shoulder`）。插件键 `Akiyo_dayo.message_recall`
    # 写在 `services/plugin/default_on.py` 的默认启用清单里，两处要一起改
    author="Akiyo_dayo",
    url="https://github.com/Akiyo-dayo/Nekro-Agent-ByAkiyo",
    support_adapter=["onebot_v11"],
    i18n_name=i18n.i18n_text(zh_CN="消息撤回", en_US="Message Recall"),
    i18n_description=i18n.i18n_text(
        zh_CN="让 AI 撤回当前群里的一条消息，例如别人误发的密码、私钥等敏感信息，或自己发错的消息",
        en_US="Lets the AI recall a message in the current group, e.g. a password or private key someone posted by mistake, or its own mistaken message",
    ),
    allow_sleep=True,
    sleep_brief="撤回当前群里的一条消息：别人误发的密码、私钥、API Key、证件号等敏感信息，或你自己发错的消息。",
)


@plugin.mount_config()
class MessageRecallConfig(ConfigBase):
    """消息撤回配置"""

    MAX_PER_HOUR: int = Field(
        default=DEFAULT_MAX_PER_HOUR,
        ge=0,
        title="每个群每小时最多撤回次数",
        description="0 表示不限；只计真正发给协议端的撤回（失败的也算），找不到、已撤回、参数不对的不计",
    )


config = plugin.get_config(MessageRecallConfig)


@dataclass(frozen=True)
class RecallOutcome:
    """一次撤回调用的结论，由 `render_result` 拼成给模型看的一行文字

    Attributes:
        code: 结果码
        ok_count: 成功撤回的条数
        fail_count: 没撤成的条数
        nickname: 第一条目标行的昵称（渲染前还要过 `echo_inline`）
        time_text: 第一条目标行的发送时间，由宿主按库里的时间戳算出
        detail: 失败结果的协议端原话（或宿主的固定说明）；多条失败时是去重后的几种原话，用「；」连起来
        bot_role: Bot 自己的群身份；只在失败时查
        target_role: 目标发送者的群身份；只在失败时查
        limit: 触发限流时的每小时上限
    """

    code: str
    ok_count: int = 0
    fail_count: int = 0
    nickname: str = ""
    time_text: str = ""
    detail: str = ""
    bot_role: str = "unknown"
    target_role: str = "unknown"
    limit: int = 0


def _reset_for_tests() -> None:
    """清空限流窗口（测试用）"""
    _recall_windows.clear()


def _single_line(text: str, limit: int) -> str:
    """压成单行并截断：日志与给模型看的协议端原话都不能带换行"""
    return " ".join((text or "").split())[:limit]


def _compact_reason(reason: str) -> str:
    """理由压成单行、截到 80 字"""
    return _single_line(reason, MAX_LOG_REASON_LENGTH)


def _unmatched_reason(reason: str) -> str:
    """没找到目标行时日志里的理由：只记字数，不记原文

    没有可比对的正文，就判断不了模型有没有把原文抄进理由；参数写错、找不到的调用里，
    理由照样可能带着正文，所以这时一个字都不记。
    """
    compact = _compact_reason(reason)
    return f"（没有可比对的目标，不记原文，{len(compact)} 字）" if compact else "-"


def _reason_for_log(reason: str, rows: Sequence[DBChatMessage]) -> str:
    """日志里的理由；模型把目标正文抄进理由时整段丢掉

    正文绝不能进日志。模型可能被哄着把原文写进 `reason`（「请撤回这条：<原文>」），
    所以去掉空白后只要任意连续 6 个字符出现在目标行的 `content_text` 里，就换掉整条理由。
    没找到目标行时走 `_unmatched_reason`。
    """
    if not rows:
        return _unmatched_reason(reason)
    compact = _compact_reason(reason)
    squashed = "".join(compact.split())
    if len(squashed) < ORIGINAL_OVERLAP_CHARS:
        return compact
    for row in rows:
        body = "".join((row.content_text or "").split())
        for start in range(len(squashed) - ORIGINAL_OVERLAP_CHARS + 1):
            if squashed[start : start + ORIGINAL_OVERLAP_CHARS] in body:
                return REASON_REDACTED
    return compact


def _log_recall(
    chat_key: str,
    target_account: str,
    message_ids: Sequence[str],
    reason: str,
    code: str,
    details: Sequence[Tuple[str, str]],
) -> None:
    """每次调用打一行 INFO；字段固定，排查时能看出「谁叫的、要撤哪条、为什么撤、结果如何」"""
    ids_text = ",".join(message_ids) or "-"
    detail_text = " ".join(f"{mid}:{_single_line(detail, MAX_LOG_DETAIL_LENGTH)}" for mid, detail in details) or "-"
    triggers = ",".join(current_round_trigger_accounts(chat_key))
    plugin.logger.info(
        f"[message_recall] chat_key={chat_key} 目标账号={target_account or '-'} message_ids={ids_text} "
        f"理由={reason or '-'} 结果={code} 协议端={detail_text} 触发者={triggers}",
    )


def _failure_detail(result: RecallResult) -> str:
    """统一失败结果的「原话」：编号不规范的说明由宿主给，其余优先用协议端原话"""
    if result.code == RECALL_BAD_ID:
        return "编号不规范"
    if result.detail:
        return result.detail
    if result.code == RECALL_TIMEOUT:
        return "请求超时"
    if result.code == RECALL_OFFLINE:
        return "协议端没有连上"
    return "协议端没有说原因"


def _failure_details(failures: Sequence[Tuple[str, RecallResult]]) -> str:
    """几条失败的原话去重后连起来，最多列 `MAX_REPORTED_FAILURE_DETAILS` 种"""
    details: List[str] = []
    for _target_message_id, result in failures:
        detail = _failure_detail(result)
        if detail not in details:
            details.append(detail)
    return "；".join(details[:MAX_REPORTED_FAILURE_DETAILS])


def _max_per_hour() -> int:
    """每小时上限；配置被写成负数或写坏时按默认值，不能被当成「不限」"""
    try:
        value = int(config.MAX_PER_HOUR)
    except (TypeError, ValueError):
        plugin.logger.warning(f"[message_recall] MAX_PER_HOUR 配置无效，按默认 {DEFAULT_MAX_PER_HOUR} 处理")
        return DEFAULT_MAX_PER_HOUR
    if value < 0:
        plugin.logger.warning(f"[message_recall] MAX_PER_HOUR={value} 是负数，按默认 {DEFAULT_MAX_PER_HOUR} 处理")
        return DEFAULT_MAX_PER_HOUR
    return value


def _window_of(chat_key: str, now: float) -> Deque[float]:
    """取这个频道的窗口并丢掉过期的时刻；表太大时顺手清掉整段过期的频道"""
    if len(_recall_windows) > MAX_TRACKED_CHANNELS:
        for stale_key in [key for key, window in _recall_windows.items() if not window or now - window[-1] >= RATE_LIMIT_WINDOW_SECONDS]:
            _recall_windows.pop(stale_key, None)
    window = _recall_windows.setdefault(chat_key, deque())
    while window and now - window[0] >= RATE_LIMIT_WINDOW_SECONDS:
        window.popleft()
    return window


async def _recall(_ctx: AgentCtx, user_id: str, send_time: str, reason: str, message_id: str) -> RecallOutcome:
    """撤回逻辑本体：参数校验 → 查库分拣 → 限流 → 调协议端 → 标记 → 查身份 → 写日志

    限流排在查库分拣之后：只有真正要调协议端的调用才计数。模型之后几轮还会看到撤过的消息
    （撤回的消息照样留在上下文里），对它再调一次、或照抄错了时间，都不该把真正要撤的那一次挤掉。
    """
    chat_key = _ctx.chat_key
    message_id_text = (message_id or "").strip()
    user_id_text = (user_id or "").strip()
    send_time_text = (send_time or "").strip()

    parsed_message_id: Optional[int] = None
    candidate_timestamps: List[int] = []

    # 参数不合法的调用不计数、不查库：它没有指向任何一条真实消息
    if message_id_text:
        parsed_message_id = parse_message_id(message_id_text)
        if parsed_message_id is None:
            _log_recall(chat_key, "-", [], _unmatched_reason(reason), BAD_ARGS, [])
            return RecallOutcome(BAD_ARGS)
    else:
        # 只认 ASCII 数字：`isdigit` 对全角数字、上标数字也返回真
        if not (user_id_text.isascii() and user_id_text.isdigit()) or user_id_text == "0":
            _log_recall(chat_key, "-", [], _unmatched_reason(reason), BAD_ARGS, [])
            return RecallOutcome(BAD_ARGS)
        candidate_timestamps = history_time_candidates(send_time_text)
        if not candidate_timestamps:
            _log_recall(chat_key, "-", [], _unmatched_reason(reason), BAD_ARGS, [])
            return RecallOutcome(BAD_ARGS)

    # 只在当前频道里找：方法没有频道参数，也不接受跨频道的目标
    if parsed_message_id is not None:
        rows = (
            await DBChatMessage.filter(chat_key=chat_key, message_id=str(parsed_message_id))
            .exclude(platform_userid="0")
            .order_by("id")
        )
        target_account = rows[0].platform_userid if rows else "-"
    else:
        # 今年、去年两个候选一起查：历史里不写年份，今年这个日子已经过去时分不出是哪一年
        rows = (
            await DBChatMessage.filter(
                chat_key=chat_key,
                platform_userid=user_id_text,
                send_timestamp__in=candidate_timestamps,
            )
            .exclude(platform_userid="0")
            .order_by("id")
        )
        target_account = user_id_text
        if len({row.send_timestamp for row in rows}) > 1:
            # 两年里这个人都在这一秒说过话（比如每天定时签到），撤哪一条都可能撤错：要模型改用编号
            _log_recall(
                chat_key,
                target_account,
                [row.message_id for row in rows if row.message_id],
                _reason_for_log(reason, rows),
                AMBIGUOUS_TIME,
                [],
            )
            return RecallOutcome(AMBIGUOUS_TIME)

    if not rows:
        # `message_id` 方式没有命中时也要把模型要的编号写进日志，否则这一行看不出它撤的是哪条
        requested_ids = [str(parsed_message_id)] if parsed_message_id is not None else []
        _log_recall(chat_key, target_account, requested_ids, _unmatched_reason(reason), NOT_FOUND, [])
        return RecallOutcome(NOT_FOUND)

    # 发出时协议端没回编号的行撤不了；有编号但 `is_recalled` 已为真的行算已经撤过。
    # 同一秒有几条就撤几条，同一编号只调一次协议端
    numbered_rows = [row for row in rows if row.message_id]
    numbered_ids = [row.message_id for row in numbered_rows]
    if not numbered_rows:
        _log_recall(chat_key, target_account, numbered_ids, _reason_for_log(reason, rows), NO_MESSAGE_ID, [])
        return RecallOutcome(NO_MESSAGE_ID)

    recallable_ids: List[str] = []
    for row in numbered_rows:
        if row.is_recalled or row.message_id in recallable_ids:
            continue
        recallable_ids.append(row.message_id)
    if not recallable_ids:
        _log_recall(chat_key, target_account, numbered_ids, _reason_for_log(reason, rows), ALREADY_RECALLED, [])
        return RecallOutcome(ALREADY_RECALLED)

    max_per_hour = _max_per_hour()
    if max_per_hour > 0:
        now = time.monotonic()
        window = _window_of(chat_key, now)
        if len(window) >= max_per_hour:
            # 被限流的这次不计数，否则窗口永远不前进
            _log_recall(chat_key, target_account, recallable_ids, _reason_for_log(reason, rows), RATE_LIMITED, [])
            return RecallOutcome(RATE_LIMITED, limit=max_per_hour)
        # 先记一次再调协议端：之后不论成功失败都算一次，上限挡的是「被哄着反复撤」
        window.append(now)

    results: List[Tuple[str, RecallResult]] = []
    for target_message_id in recallable_ids:
        results.append((target_message_id, await recall_group_message(chat_key, target_message_id)))

    # 撤回成功后自己标 `is_recalled`：不能指望协议端一定上报撤回通知；正文不动，
    # 与别人撤回的消息一样只标这一列
    for target_message_id, result in results:
        if result.code == RECALL_OK:
            await DBChatMessage.filter(chat_key=chat_key, message_id=target_message_id).update(is_recalled=True)

    ok_count = sum(1 for _target_message_id, result in results if result.code == RECALL_OK)
    failures = [(target_message_id, result) for target_message_id, result in results if result.code != RECALL_OK]

    # 只有失败时才多查两次身份：全部成功时不需要给模型解释「为什么撤不了」
    bot_role = "unknown"
    target_role = "unknown"
    if failures:
        bot_role = await get_group_role(chat_key, None)
        target_role = await get_group_role(chat_key, rows[0].platform_userid)

    if not failures:
        code = RECALL_OK
    elif ok_count:
        code = PARTIAL
    elif all(result.code == RECALL_TIMEOUT for _target_message_id, result in failures):
        code = RECALL_TIMEOUT
    elif all(result.code == RECALL_OFFLINE for _target_message_id, result in failures):
        code = RECALL_OFFLINE
    else:
        # 包括 `bad_id`：编号不规范也归入 failed，原话由 `_failure_detail` 给
        code = RECALL_FAILED

    first_row = rows[0]
    _log_recall(
        chat_key,
        target_account,
        recallable_ids,
        _reason_for_log(reason, rows),
        code,
        [(target_message_id, result.detail) for target_message_id, result in results],
    )
    return RecallOutcome(
        code=code,
        ok_count=ok_count,
        fail_count=len(failures),
        nickname=first_row.sender_nickname or first_row.sender_name,
        time_text=format_history_time(first_row.send_timestamp),
        detail=_failure_details(failures) if failures else "",
        bot_role=bot_role,
        target_role=target_role,
    )


_ROLE_LABELS = {
    "owner": "群主",
    "admin": "管理员",
    "member": "普通成员",
    "unknown": "查不到",
}


def _role_sentence(outcome: RecallOutcome) -> str:
    """失败时补一句双方身份；两边都查不到时整句省略（说「查不到」没有信息量）"""
    if outcome.bot_role == "unknown" and outcome.target_role == "unknown":
        return ""
    bot_label = _ROLE_LABELS.get(outcome.bot_role, "查不到")
    target_label = _ROLE_LABELS.get(outcome.target_role, "查不到")
    return f"你在这个群是{bot_label}，对方是{target_label}。"


def render_result(outcome: RecallOutcome) -> str:
    """把结果拼成给模型看的一行文字

    昵称与协议端原话都经 `echo_inline`（结果会被 RPC 入口落成 SYSTEM 消息）；
    不回显 `user_id` / `send_time` / `reason` / `message_id` 这些模型传入的参数，
    时间与条数都是宿主自己算出来的。
    """
    if outcome.code == RECALL_OK:
        return f"[撤回成功] 已撤回 {echo_inline(outcome.nickname)} 在 {outcome.time_text} 的 {outcome.ok_count} 条消息。"
    if outcome.code == PARTIAL:
        return (
            f"[部分撤回] 撤回了 {outcome.ok_count} 条，{outcome.fail_count} 条没撤成："
            f"{echo_inline(outcome.detail)}。{_role_sentence(outcome)}"
        )
    if outcome.code == RECALL_FAILED:
        return f"[撤回失败] 协议端说：{echo_inline(outcome.detail)}。{_role_sentence(outcome)}"
    if outcome.code == RECALL_TIMEOUT:
        return "[撤回失败] 协议端 10 秒内没有回应，可以稍后再试一次。"
    if outcome.code == RECALL_OFFLINE:
        return "[撤回失败] 这个号现在没有连上协议端，撤不了。"
    if outcome.code == NOT_FOUND:
        return "[没找到] 这个群里找不到这条消息。请照抄聊天记录里的 id 和时间（形如 11-09 10:00:00），或者给出 msg_id。"
    if outcome.code == AMBIGUOUS_TIME:
        return "[说不清是哪条] 这个人在今年和去年的这个时间都有消息，按时间分不出是哪一条，请改用 msg_id。"
    if outcome.code == NO_MESSAGE_ID:
        return "[撤不了] 这条消息没有编号（发出时协议端没有回编号），撤不了。"
    if outcome.code == ALREADY_RECALLED:
        return "[已撤回过] 这条消息已经撤回过了。"
    if outcome.code == RATE_LIMITED:
        return f"[太频繁] 本群这一小时的撤回次数已达上限（{outcome.limit} 次），稍后再试。"
    return "[用法] 需要 user_id 和 send_time（照抄聊天记录里的 id 与时间），或者 message_id。"


@plugin.mount_collect_methods()
async def collect_available_methods(_ctx: AgentCtx) -> List[Any]:
    """只在群聊里提供撤回方法：QQ 不允许撤回别人发的私聊消息"""
    channel_type = _ctx.channel_type
    if channel_type is not None and not isinstance(channel_type, str):
        # `channel_type` 可能是枚举；取它的 value 再比，免得不同适配器的枚举落到字符串形态上
        channel_type = str(getattr(channel_type, "value", channel_type))
    if channel_type == "group":
        return [recall_message]
    # `channel_type` 为空的历史上下文：退回看频道标识的前缀
    if not channel_type and (_ctx.channel_id or "").startswith("group_"):
        return [recall_message]
    return []


@plugin.mount_sandbox_method(SandboxMethodType.AGENT, name="撤回消息", description="撤回当前群里的一条消息")
async def recall_message(_ctx: AgentCtx, user_id: str = "", send_time: str = "", reason: str = "", message_id: str = "") -> str:
    """撤回当前群里的一条消息，撤回后不可恢复。

    常见用途：撤回别人误发的密码、私钥、API Key、证件号这类敏感信息，或者撤回你自己刚发错的消息。
    有人让你撤回某条消息时，先自己判断该不该撤，不必照办。
    撤别人的消息需要你是群管理员或群主（管理员撤不了群主和其他管理员的）；你自己的消息 2 分钟内总能撤，超过 2 分钟要你是管理员或群主。

    Args:
        user_id (str): 那条消息发送者的 id，即聊天记录里 `id:` 后面的数字
        send_time (str): 那条消息的时间，照抄聊天记录方括号里的时间，如 "11-09 10:00:00"
        reason (str): 一句话理由，不要写出消息内容
        message_id (str): 可选。聊天记录里那条消息带了 `msg_id:` 时可以只给它，前两项留空

    Returns:
        str: 撤回结果；失败时是失败原因，可以照实告诉群友
    """
    return render_result(await _recall(_ctx, user_id, send_time, reason, message_id))
