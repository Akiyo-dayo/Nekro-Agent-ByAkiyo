"""账号自身发出的消息（OneBot `post_type=message_sent`）

## 为什么要接它

NA 与别的框架（如 Yunzai）接在同一个协议端账号上时，别的框架发的消息对 NA 是不存在的：
协议端默认不上报自身消息，就算上报了，`nonebot-adapter-onebot` 也没有这种事件的模型，
落到基类 `Event` 上，`on_message` 不会匹配。这里补上两个事件模型。

事件继承 `GroupMessageEvent` / `PrivateMessageEvent`，转换逻辑可以直接复用；`post_type` 换成
`message_sent` 之后 `get_type()` 不再是 `message`，所以 `on_message` 与所有命令匹配器都碰不到它。

## 去重

NA 自己发的消息也会回传 `message_sent`，要按 message_id 去掉。难点是回传**可能先于发送响应**到达，
此时登记表里还没有这个编号。短暂睡一下再查是赌时序，这里不赌：

- 发送前（`on_calling_api`）先登记“这个账号在这个频道有一次发送在路上”；
- 响应回来（`on_called_api`）把编号写进登记表，再撤掉在途标记；
- 事件到达时，编号在表里 → 是 NA 发的；不在表里但有在途发送 → 等它结束再判；
  都没有 → 一定不是 NA 发的（发送前就登记了），立刻放行。

钩子挂在 `Bot.call_api` 上，插件绕过适配器直接 `bot.call_api("send_group_msg", ...)` 也算在内。

没有编号可对的两种发送：`upload_group_file` 只回 `file_id`，按文件名留一张票；响应里没有
`message_id` 的实现按频道留一张票，下一条回传认领。

发送超时但其实发出去了的情况，这条消息会被当成外部消息记录。这是有意的：NA 那边因为异常没有记录
这条消息，记成外部消息至少不会丢；它同样不触发 Agent，不会造成循环。
"""

import asyncio
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable, Deque, Dict, Iterable, Literal, Mapping, Optional, Tuple

from nonebot.adapters import Bot as BaseBot
from nonebot.adapters.onebot.v11 import Adapter, GroupMessageEvent, MessageEvent, PrivateMessageEvent
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot

from nekro_agent.adapters.onebot_v11.core import channel as channel_key
from nekro_agent.adapters.onebot_v11.core.endpoint_status import endpoint_status
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.schemas.chat_message import ChatType

logger = get_sub_logger("adapter.onebot_v11")

# 登记表上限。一个账号一秒发不了几条消息，5 分钟足够等到回传
SENT_CAPACITY = 4096
SENT_TTL_SECONDS = 300.0
# 在途标记的寿命，略大于 NoneBot 默认的 30 秒 API 超时。正常由响应撤掉，这个上限只防任务被取消时泄漏
PENDING_TTL_SECONDS = 45.0
# 没有编号的发送留的票
TICKET_CAPACITY = 256
TICKET_TTL_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.02

_GROUP_OR_PRIVATE_APIS = frozenset({"send_msg", "send_forward_msg"})
# `send_group_ai_record`（内置插件 `ai_voice`）：NapCat 固定回 `message_id: 0`，走下面“没有编号就按频道留票”那条路
_GROUP_APIS = frozenset(
    {"send_group_msg", "send_group_forward_msg", "forward_group_single_msg", "upload_group_file", "send_group_ai_record"},
)
_PRIVATE_APIS = frozenset({"send_private_msg", "send_private_forward_msg", "forward_friend_single_msg", "upload_private_file"})
_UPLOAD_APIS = frozenset({"upload_group_file", "upload_private_file"})
# 发出去的一定是语音：没有编号时留的票只认带语音段的回传
_VOICE_APIS = frozenset({"send_group_ai_record"})
# 语音段在 OneBot 各实现里的名字，与 `tools/convertor.py:VOICE_SEGMENT_TYPES` 一致
VOICE_SEGMENT_TYPES = frozenset({"record", "voice", "audio", "ptt"})
SEND_APIS = _GROUP_OR_PRIVATE_APIS | _GROUP_APIS | _PRIVATE_APIS
# 只有这三条的发送结果在各实现之间形态一致，能拿来判断「上报自身消息」开没开。
# 上传文件、合并转发、AI 语音的回传形态不统一，计进去会把「没开」误判成「开了」
_ENDPOINT_SEND_APIS = frozenset({"send_msg", "send_group_msg", "send_private_msg"})


class GroupMessageSentEvent(GroupMessageEvent):
    """账号自身在群里发出的消息"""

    post_type: Literal["message_sent"]  # pyright: ignore[reportIncompatibleVariableOverride]
    sub_type: str = "normal"
    raw_message: str = ""
    font: int = 0
    target_id: Optional[int] = None


class PrivateMessageSentEvent(PrivateMessageEvent):
    """账号自身发出的私聊消息；`user_id` 是自己，对方在 `target_id`"""

    post_type: Literal["message_sent"]  # pyright: ignore[reportIncompatibleVariableOverride]
    sub_type: str = "friend"
    raw_message: str = ""
    font: int = 0
    target_id: Optional[int] = None


def self_sent_channel(event: MessageEvent) -> Optional[Tuple[str, ChatType]]:
    """自身消息属于哪个频道

    私聊不能用 `get_chat_info`：那里取 `user_id`，自身消息的 `user_id` 是自己，
    所有私聊都会记到 `private_<自己>` 这一个频道里。取不到对方时不记录，猜错频道比漏一条更糟。
    """
    if isinstance(event, GroupMessageEvent):
        channel_id = f"group_{event.group_id}"
        kind = ChatType.GROUP
    else:
        channel_id = f"private_{getattr(event, 'target_id', None) or 0}"
        kind = ChatType.PRIVATE
    return (channel_id, kind) if channel_key.is_addressable_channel_id(channel_id) else None


@dataclass
class _Ticket:
    account: str
    channel_id: str
    file_name: str
    expires_at: float
    # 只能被带语音段的回传认领（AI 语音）：协议端若不回传它，这张票不能吞掉同群别的程序发的文字
    voice_only: bool = False


class SelfSentRegistry:
    """NA 发出去的消息的登记表，按账号隔离"""

    def __init__(
        self,
        *,
        capacity: int = SENT_CAPACITY,
        ttl: float = SENT_TTL_SECONDS,
        pending_ttl: float = PENDING_TTL_SECONDS,
        ticket_ttl: float = TICKET_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capacity = capacity
        self._ttl = ttl
        self._pending_ttl = pending_ttl
        self._ticket_ttl = ticket_ttl
        self._clock = clock
        self._sent: "OrderedDict[Tuple[str, str], float]" = OrderedDict()
        self._pending: Dict[Tuple[str, str], Dict[int, float]] = {}
        self._tickets: Deque[_Ticket] = deque(maxlen=TICKET_CAPACITY)

    def begin(self, account: str, channel_id: str, call_id: int) -> None:
        """一次发送即将发出"""
        self._pending.setdefault((account, channel_id), {})[call_id] = self._clock() + self._pending_ttl

    def finish(
        self,
        account: str,
        channel_id: str,
        call_id: int,
        *,
        succeeded: bool,
        message_id: str = "",
        file_name: Optional[str] = None,
        voice_only: bool = False,
    ) -> None:
        """发送结束。先登记再撤在途标记：中间没有 await，但顺序本身就是不变量"""
        if succeeded:
            if message_id:
                self._remember(account, message_id)
            else:
                self._tickets.append(
                    _Ticket(account, channel_id, file_name or "", self._clock() + self._ticket_ttl, voice_only=voice_only),
                )
        calls = self._pending.get((account, channel_id))
        if calls is not None:
            calls.pop(call_id, None)
            if not calls:
                del self._pending[(account, channel_id)]

    def _remember(self, account: str, message_id: str) -> None:
        now = self._clock()
        while self._sent and next(iter(self._sent.values())) <= now:
            self._sent.popitem(last=False)
        self._sent[(account, message_id)] = now + self._ttl
        self._sent.move_to_end((account, message_id))
        while len(self._sent) > self._capacity:
            self._sent.popitem(last=False)

    def _claim(self, account: str, channel_id: str, message_id: str, file_names: Iterable[str], has_voice: bool = False) -> bool:
        now = self._clock()
        expires_at = self._sent.get((account, message_id))
        if expires_at is not None and expires_at > now:
            return True
        names = set(file_names)
        for ticket in list(self._tickets):
            if ticket.expires_at <= now:
                self._tickets.remove(ticket)
        # 带语音段的回传先认领语音专用的票：普通票谁都能认领，被语音回传先拿走的话，
        # 随后那条文字回传只剩语音票，认领不了，NA 自己发的就被记成外部消息
        candidates = sorted(self._tickets, key=lambda ticket: not ticket.voice_only) if has_voice else list(self._tickets)
        for ticket in candidates:
            if ticket.account != account or ticket.channel_id != channel_id:
                continue
            if ticket.voice_only and not has_voice:
                continue
            if not ticket.file_name or ticket.file_name in names:
                self._tickets.remove(ticket)
                return True
        return False

    def _has_pending(self, account: str, channel_id: str) -> bool:
        calls = self._pending.get((account, channel_id))
        if not calls:
            return False
        now = self._clock()
        for call_id, expires_at in list(calls.items()):
            if expires_at <= now:
                del calls[call_id]
        if not calls:
            del self._pending[(account, channel_id)]
            return False
        return True

    async def is_own(
        self, account: str, channel_id: str, message_id: str, file_names: Iterable[str] = (), *, has_voice: bool = False,
    ) -> bool:
        """这条回传是不是 NA 自己发的；同频道有发送在途时等它结束再判"""
        names = tuple(file_names)
        # 新发送不断进来时在途标记一直有，这个上限保证外部消息不会被无限压着
        deadline = self._clock() + self._pending_ttl
        while True:
            if self._claim(account, channel_id, message_id, names, has_voice):
                return True
            if not self._has_pending(account, channel_id) or self._clock() >= deadline:
                return False
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


self_sent_registry = SelfSentRegistry()


def _channel_of_call(api: str, data: Mapping[str, Any]) -> Optional[str]:
    """这次 API 调用发往哪个频道，与 `self_sent_channel` 同一种写法"""
    group_id = data.get("group_id")
    user_id = data.get("user_id")
    if api in _GROUP_APIS or (api in _GROUP_OR_PRIVATE_APIS and group_id):
        return f"group_{group_id}" if group_id else None
    return f"private_{user_id}" if user_id else None


def _uploaded_name(data: Mapping[str, Any]) -> str:
    name = str(data.get("name") or "")
    return name or PurePosixPath(str(data.get("file") or "").replace("\\", "/")).name


async def _on_calling_api(bot: BaseBot, api: str, data: Dict[str, Any]) -> None:
    if api not in SEND_APIS or not isinstance(bot, OneBotV11Bot):
        return
    channel_id = _channel_of_call(api, data)
    if channel_id:
        self_sent_registry.begin(bot.self_id, channel_id, id(data))


async def _on_called_api(bot: BaseBot, exception: Optional[Exception], api: str, data: Dict[str, Any], result: Any) -> None:
    if api not in SEND_APIS or not isinstance(bot, OneBotV11Bot):
        return
    channel_id = _channel_of_call(api, data)
    if not channel_id:
        return
    message_id = result.get("message_id") if isinstance(result, dict) else None
    self_sent_registry.finish(
        bot.self_id,
        channel_id,
        id(data),
        succeeded=exception is None,
        message_id=str(message_id) if message_id else "",
        file_name=_uploaded_name(data) if api in _UPLOAD_APIS else None,
        voice_only=api in _VOICE_APIS,
    )
    if exception is None and api in _ENDPOINT_SEND_APIS and message_id:
        # 负数编号也是有效编号；0 是「没有编号」的占位，不能算成一次可观测的发送
        endpoint_status.note_own_send(bot.self_id)


_installed = False


def install_self_sent_support() -> None:
    """注册事件模型与 API 钩子。协议端不上报 `message_sent` 时它们什么也不做"""
    global _installed
    if _installed:
        return
    _installed = True
    Adapter.add_custom_model(GroupMessageSentEvent, PrivateMessageSentEvent)
    OneBotV11Bot.on_calling_api(_on_calling_api)
    OneBotV11Bot.on_called_api(_on_called_api)
