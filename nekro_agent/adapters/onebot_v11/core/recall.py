"""OneBot V11 撤回与群身份查询的宿主封装

插件（`plugins/builtin/message_recall.py`）只认结果码，不直接碰协议端：撤回的失败形态
（协议端拒绝、超时、号没连上、编号不规范）与「身份查没查到」都在这里归一成稳定的返回值。
这样插件里不用 import NoneBot 的异常类型，换协议端时也只改这一层。

## 为什么结果码里要区分 timeout 与 failed

协议端拒绝与超时对模型是两种说法：前者要把协议端原话照实告诉群友，后者只需要说
「稍后再试」。混成一个 `failed`，模型会把「10 秒没回应」说成「协议端拒绝撤回」。

## 为什么角色查询只看返回字典里的 role

`get_group_member_info` 是协议端的通用接口，别的实现可能不回 `role`、回别的字符串。
身份只用来给失败结果补一句「你在这个群是普通成员」，查不到就不说，绝不能因为一个
非标准回复把撤回本身的结论盖掉，所以这里任何异常与任何不在三个标准值里的取值都收敛成
`unknown`。

## 缓存

身份在群里十分钟内不会变，而一次失败最多查两次；按 `(chat_key, 账号)` 缓存 600 秒，
避免模型被哄着连续撤同几条时每次都多打两次协议端接口。`unknown` 不缓存：那多半是
一次网络抖动，下一次应该重试。
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from nonebot.adapters.onebot.v11.exception import ActionFailed

from .bot import BotInstanceOfflineError, get_bot_by_chat_key
from .channel import NonNumericChannelIdError, parse_channel_id, parse_numeric_target_id
from .message_id import parse_message_id

# 协议端返回成功
RECALL_OK = "ok"
# 协议端返回错误（ActionFailed 或别的异常），detail 是协议端原话
RECALL_FAILED = "failed"
# 等协议端回应超时
RECALL_TIMEOUT = "timeout"
# 取不到这个频道的 Bot 连接
RECALL_OFFLINE = "offline"
# message_id 不是规范编号，或频道不是数字群号
RECALL_BAD_ID = "bad_id"

# 撤回调用等协议端回应的上限（秒）。SnowLuma / NapCat 正常都在百毫秒级返回，
# 10 秒是「网络抖一下还能等到」与「别让模型干等」之间的折中
RECALL_TIMEOUT_SECONDS = 10.0

# 群身份查询的上限（秒）：它只在撤回失败后补一句说明，卡住还不如没有
ROLE_QUERY_TIMEOUT_SECONDS = 5.0

# 身份缓存的存活时间（秒）
ROLE_CACHE_SECONDS = 600
# 身份缓存最多留多少项；写入时超过就先丢掉过期的，还超过就整个清空（只是少一次缓存命中）
ROLE_CACHE_MAX_ENTRIES = 4096

# 协议端约定的三种群身份；其余一律按查不到处理
VALID_GROUP_ROLES = frozenset({"owner", "admin", "member"})

# (chat_key, 账号) -> (身份, 写入时刻)。写入时刻用单调时钟，系统时间被调整不会让缓存提前失效
_role_cache: Dict[Tuple[str, str], Tuple[str, float]] = {}


@dataclass(frozen=True)
class RecallResult:
    """一次撤回的结果

    Attributes:
        code: 上列五个结果码之一
        detail: 协议端原话或异常文字，只用于给模型看和写日志，宿主不据此做判断
    """

    code: str
    detail: str = ""


async def recall_group_message(chat_key: str, message_id: str, timeout: float = RECALL_TIMEOUT_SECONDS) -> RecallResult:
    """撤回当前频道里的一条群消息

    Args:
        chat_key: 会话标识，形如 `onebot_v11-inst1-group_123456`
        message_id: 消息编号；负数是正常编号，不能另加「必须为正」的校验
        timeout: 等协议端回应的秒数

    Returns:
        `RecallResult`；本函数不写库、不打日志，两件事都由插件统一做
    """
    parsed_message_id = parse_message_id(message_id)
    if parsed_message_id is None:
        return RecallResult(RECALL_BAD_ID)

    # 群号只用于确认频道标识是数字形态（消息撤回接口本身只要编号）。这里刻意不取返回值：
    # 频道标识不是 `group_<数字>` 时应当按「编号不规范」直接拒绝，而不是把脏标识拼进调用里。
    # 绝不能用 int() 解析频道标识：既有的静态契约就是盯这个的
    try:
        parse_numeric_target_id(parse_channel_id(chat_key))
    except NonNumericChannelIdError:
        return RecallResult(RECALL_BAD_ID)

    try:
        bot = await get_bot_by_chat_key(chat_key)
    except BotInstanceOfflineError:
        return RecallResult(RECALL_OFFLINE)

    try:
        await asyncio.wait_for(bot.call_api("delete_msg", message_id=parsed_message_id), timeout)
    except asyncio.TimeoutError:
        return RecallResult(RECALL_TIMEOUT)
    except ActionFailed as exc:
        info = exc.info if isinstance(exc.info, dict) else {}
        # 协议端原话的优先级：wording（给人看的）→ msg（机器字段）→ 整个 info 兜底
        detail = str(info.get("wording") or info.get("msg") or exc.info)
        return RecallResult(RECALL_FAILED, detail)
    except Exception as exc:  # noqa: BLE001 - 协议端/网络任何异常都收敛成 failed，不向插件抛
        return RecallResult(RECALL_FAILED, f"{type(exc).__name__}: {exc}")
    return RecallResult(RECALL_OK)


async def get_group_role(chat_key: str, user_id: Optional[str]) -> str:
    """查一个账号在当前群的群身份：`owner` / `admin` / `member`，查不到返回 `unknown`

    Args:
        chat_key: 会话标识
        user_id: 要查的账号；传 None 表示查 Bot 自己（用连接的 `self_id`）
    """
    # 账号不是纯数字时连连接都不用取：这个请求注定发不出去，也不该占缓存
    if user_id is not None and not (user_id.isascii() and user_id.isdigit()):
        return "unknown"

    try:
        group_id = parse_numeric_target_id(parse_channel_id(chat_key))
    except NonNumericChannelIdError:
        return "unknown"

    try:
        bot = await get_bot_by_chat_key(chat_key)
    except Exception:  # noqa: BLE001 - 身份只是失败时的补充说明，任何异常都按查不到处理
        return "unknown"

    account = str(getattr(bot, "self_id", "")) if user_id is None else user_id
    if not (account.isascii() and account.isdigit()):
        return "unknown"

    cached = _role_cache.get((chat_key, account))
    if cached is not None and time.monotonic() - cached[1] < ROLE_CACHE_SECONDS:
        return cached[0]

    try:
        member = await asyncio.wait_for(
            bot.call_api(
                "get_group_member_info",
                group_id=group_id,
                user_id=int(account),
                no_cache=False,
            ),
            ROLE_QUERY_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001
        return "unknown"

    role = member.get("role") if isinstance(member, dict) else None
    if not isinstance(role, str) or role not in VALID_GROUP_ROLES:
        return "unknown"

    now = time.monotonic()
    if len(_role_cache) >= ROLE_CACHE_MAX_ENTRIES:
        for stale_key in [key for key, (_role, written) in _role_cache.items() if now - written >= ROLE_CACHE_SECONDS]:
            _role_cache.pop(stale_key, None)
        if len(_role_cache) >= ROLE_CACHE_MAX_ENTRIES:
            _role_cache.clear()
    _role_cache[(chat_key, account)] = (role, now)
    return role


def reset_role_cache() -> None:
    """清空身份缓存（测试用）"""
    _role_cache.clear()
