import json
from typing import Optional, Tuple, Union, cast

from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import (
    GroupIncreaseNoticeEvent,
    GroupMessageEvent,
    GroupUploadNoticeEvent,
    MessageEvent,
    NoticeEvent,
)
from nonebot.exception import ActionFailed, ApiNotAvailable, NetworkError

from nekro_agent.adapters.onebot_v11.core import channel
from nekro_agent.adapters.onebot_v11.core.bot import get_bot_by_chat_key
from nekro_agent.adapters.onebot_v11.core.instance import resolve_instance_key
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.tools.common_util import limited_text_output

logger = get_sub_logger("adapter.onebot_v11")
async def gen_chat_text(event: MessageEvent, bot: Bot, db_chat_channel: DBChatChannel) -> Tuple[str, int]:
    """生成合适的聊天消息内容(eg. 将cq at 解析为真实的名字)

    Args:
        event (MessageEvent): 事件对象
        bot (Bot): 机器人对象

    Returns:
        Tuple[str, int]: 聊天消息内容, 是否与自身相关
    """
    if not isinstance(event, GroupMessageEvent):
        return event.get_plaintext(), 0

    is_tome: int = 0
    msg = ""
    for seg in event.message:
        if seg.is_text():
            msg += seg.data.get("text", "")
        elif seg.type == "at":
            qq = seg.data.get("qq", None)
            if qq:
                if qq == "all":
                    msg += "[@id:all;nickname:全体成员@]"
                    is_tome = 1
                else:
                    user_name = await get_user_name(
                        event=event,
                        bot=bot,
                        user_id=int(qq),
                        db_chat_channel=db_chat_channel,
                    )
                    if user_name:
                        msg += f"[@id:{qq};nickname:{user_name}@]"  # 保持给bot看到的内容与真实用户看到的一致
        elif seg.type == "json":
            # 处理JSON卡片消息（延迟导入避免循环依赖）
            try:
                from nekro_agent.adapters.onebot_v11.tools.convertor import (
                    JSON_CARD_FALLBACK_TEXT,
                    parse_onebot_json_segment,
                )

                text_summary, _, _ = parse_onebot_json_segment(seg.data)
                msg += text_summary
            except json.JSONDecodeError as e:
                logger.warning(f"JSON卡片解析失败（格式错误）: {e}")
                from nekro_agent.adapters.onebot_v11.tools.convertor import (
                    JSON_CARD_FALLBACK_TEXT,
                )
                msg += JSON_CARD_FALLBACK_TEXT
            except Exception as e:
                logger.error(f"处理JSON卡片时发生意外错误: {e}", exc_info=True)
                from nekro_agent.adapters.onebot_v11.tools.convertor import (
                    JSON_CARD_FALLBACK_TEXT,
                )
                msg += JSON_CARD_FALLBACK_TEXT
        elif seg.type == "forward":
            msg += "[合并转发消息]"
    return msg, is_tome


# 查群成员信息只是为了拿最新的群名片：协议端连接半死时调用会超时，对方已退群时会 ActionFailed。
# 这两种情况都不该让整条消息 / 通知失败丢掉。消息事件退回发送者自带的群名片 / 昵称；
# 通知事件不带 sender，查的也常常不是发送者，退回 QQ 号。
_MEMBER_INFO_ERRORS = (ActionFailed, ApiNotAvailable, NetworkError)


def _fallback_user_name(event: object, user_id: Union[int, str]) -> str:
    """群成员信息查不到时用的名字：事件发送者就是这个人时用他的群名片 / 昵称，否则用 QQ 号"""
    sender = getattr(event, "sender", None)
    if sender is not None and str(getattr(sender, "user_id", "")) == str(user_id):
        name = getattr(sender, "card", None) or getattr(sender, "nickname", None)
        if name:
            return name
    return str(user_id)


async def get_user_name(
    event: Union[MessageEvent, GroupIncreaseNoticeEvent, GroupUploadNoticeEvent, NoticeEvent],
    bot: Bot,
    user_id: Union[int, str],
    db_chat_channel: DBChatChannel,
) -> str:
    """获取QQ用户名"""
    if str(user_id) == (await db_chat_channel.adapter.get_self_info(db_chat_channel.chat_key)).user_id:
        return (await db_chat_channel.get_preset()).name

    if isinstance(event, GroupMessageEvent) and event.sub_type == "anonymous" and event.anonymous:  # 匿名消息
        return f"[匿名]{event.anonymous.name}"

    if isinstance(event, (GroupMessageEvent, GroupIncreaseNoticeEvent, NoticeEvent)):
        group_id = getattr(event, "group_id", None)
        if group_id:
            try:
                user_info = await bot.get_group_member_info(
                    group_id=group_id,
                    user_id=user_id,
                    no_cache=False,
                )
            except _MEMBER_INFO_ERRORS as e:
                logger.warning(f"获取群成员信息失败，改用事件自带的昵称或 QQ 号: {e!r}")
                return _fallback_user_name(event, user_id)
        else:
            raise ValueError("获取群成员信息失败")
        user_name = user_info.get("nickname", None)
        user_name = user_info.get("card", None) or user_name or _fallback_user_name(event, user_id)
    else:
        user_name = (
            event.sender.nickname if not isinstance(event, GroupUploadNoticeEvent) and event.sender else event.get_user_id()
        )

    if not user_name:
        raise ValueError("获取用户名失败")

    return user_name


async def get_user_group_card_name(
    group_id: Union[int, str],
    user_id: Union[int, str],
    db_chat_channel: DBChatChannel,
) -> str:
    """获取用户所在群的群名片"""
    if str(user_id) == (await db_chat_channel.adapter.get_self_info(db_chat_channel.chat_key)).user_id:
        return (await db_chat_channel.get_preset()).name
    if str(user_id) == "all" or str(user_id) == "0":
        return "全体成员"
    bot = await get_bot_by_chat_key(db_chat_channel.chat_key)
    try:
        user_info = await bot.get_group_member_info(
            group_id=int(group_id),
            user_id=int(user_id),
            no_cache=False,
        )
    except _MEMBER_INFO_ERRORS as e:
        logger.warning(f"获取群名片失败，改用 QQ 号: {e!r}")
        return str(user_id)
    return user_info.get("card") or user_info.get("nickname", "未知")


def _resolve_channel_body(
    event: Union[MessageEvent, GroupIncreaseNoticeEvent, GroupUploadNoticeEvent, NoticeEvent],
) -> Tuple[str, ChatType]:
    """从事件解析出频道标识，形如 `group_123456` / `private_123456`"""
    if isinstance(event, (GroupUploadNoticeEvent, GroupIncreaseNoticeEvent, NoticeEvent)):
        raw_chat_type = "group"
    else:
        raw_chat_type = event.message_type
    if raw_chat_type == "friend" or raw_chat_type == "private":
        event = cast(MessageEvent, event)
        return f"private_{event.user_id}", ChatType.PRIVATE
    if raw_chat_type == "group":
        event = cast(GroupMessageEvent, event)
        return f"group_{event.group_id}", ChatType.GROUP
    raise ValueError("未知的消息类型")


async def get_chat_info(
    event: Union[MessageEvent, GroupIncreaseNoticeEvent, GroupUploadNoticeEvent, NoticeEvent],
    bot: Optional[Bot] = None,  # noqa: ARG001
) -> Tuple[str, ChatType]:
    """获取频道信息

    `channel_id` 是插件通过 `ctx.channel_id` 直接读到的一等公开字段，必须保持干净形态，
    账号维度由调用方以 `instance_key` 单独携带（见 `PlatformChannel.instance_key`）。

    上游 NekroAgent 的签名只有 `event`，第三方插件按那个形状调用，所以 `bot` 必须可省略。

    Args:
        event: OneBot 事件
        bot: 收到该事件的 Bot 连接（此处不再参与频道标识构建，保留以维持调用方签名）

    Returns:
        (channel_id, chat_type)，channel_id 形如 `group_123456`
    """
    return _resolve_channel_body(event)


async def get_chat_info_old(
    event: Union[MessageEvent, GroupIncreaseNoticeEvent, GroupUploadNoticeEvent, NoticeEvent],
    bot: Optional[Bot] = None,
) -> Tuple[str, ChatType]:
    """获取频道信息(旧版)

    直接返回完整会话标识适配旧功能需求，形如 `onebot_v11-inst1-group_123456`。

    上游 NekroAgent 的签名只有 `event`，第三方插件会照那个形状调用 `get_chat_info_old(event=event)`，
    所以 `bot` 必须可省略。省略时按事件自带的 `self_id`（收到这条事件的账号）解析实例，
    与传入 `bot` 时取 `bot.self_id` 的结果相同。
    """
    channel_body, chat_type = _resolve_channel_body(event)

    # 「频道 ID 为 0」的判据必须看前缀后面那一段：`_resolve_channel_body` 自多实例改造起
    # 返回的是 `group_0` / `private_0` 而不再是裸的 `0`，旧写法 `str(channel_body) == "0"`
    # 从那天起永不成立。协议端会把不属于任何会话的通知（私聊「对方正在输入」即
    # `notice.notify.input_status`，带 user_id 而 group_id=0）也发过来，于是它们一路走到
    # `DBChatChannel.get_channel` 才以 `聊天频道不存在: ...-group_0` 炸出整条 traceback。
    if not channel.is_addressable_channel_id(channel_body):
        raise ValueError(f"接收到频道ID为 0 的消息，源消息数据 -> {limited_text_output(event.model_dump_json(), 1024)}")

    self_id = bot.self_id if bot is not None else str(event.self_id)
    instance_key = await resolve_instance_key(self_id)
    return channel.build_chat_key(instance_key, channel_body), chat_type


async def get_message_reply_info(event: MessageEvent) -> str:
    """获取消息回复信息"""
    if not event.original_message:
        return ""
    for seg in event.original_message:
        if seg.type == "reply":
            return str(seg.data.get("id") or "")
    return ""
