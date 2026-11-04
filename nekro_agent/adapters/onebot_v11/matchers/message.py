import time
from typing import Optional, Union

from nonebot import on, on_message, on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    GroupUploadNoticeEvent,
    MessageEvent,
)
from nonebot.matcher import Matcher

from nekro_agent.adapters.interface import (
    BaseAdapter,
    PlatformChannel,
    PlatformMessage,
    PlatformUser,
    collect_message,
)
from nekro_agent.adapters.interface.schemas.extra import PlatformMessageExt
from nekro_agent.adapters.onebot_v11.core.endpoint_status import endpoint_status
from nekro_agent.adapters.onebot_v11.core.instance import resolve_instance_key, set_current_instance
from nekro_agent.adapters.onebot_v11.core.message_id import parse_message_id
from nekro_agent.adapters.onebot_v11.core.self_sent import (
    VOICE_SEGMENT_TYPES,
    GroupMessageSentEvent,
    PrivateMessageSentEvent,
    self_sent_channel,
    self_sent_registry,
)
from nekro_agent.adapters.onebot_v11.tools.convertor import convert_chat_message
from nekro_agent.adapters.onebot_v11.tools.display_name import clean_display_name
from nekro_agent.adapters.onebot_v11.tools.onebot_util import (
    gen_chat_text,
    get_chat_info,
    get_message_reply_info,
    get_user_name,
)
from nekro_agent.adapters.onebot_v11.tools.video import build_video_segment, quoted_video_from_record
from nekro_agent.core import config, logger
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_user import DBUser
from nekro_agent.services.unclaimed_dispatch import is_inbound_dispatch_allowed
from nekro_agent.schemas.chat_message import (
    SELF_SENT_NICKNAME_SUFFIX,
    SELF_SENT_SOURCE_EXTERNAL,
    ChatMessageSegment,
    ChatMessageSegmentForward,
    ChatMessageSegmentVideo,
)


def _build_content_text_from_segments(content_data: list[ChatMessageSegment]) -> str:
    """没有文字时拿各段的文本拼一个；视频占位不算，它在落库时才补（`merge_video_placeholders`）"""
    return "".join(seg.text for seg in content_data if seg.text and not isinstance(seg, ChatMessageSegmentVideo)).strip()


def _merge_voice_content_text(content_text: str, content_data: list[ChatMessageSegment]) -> str:
    voice_texts = [seg.text.strip() for seg in content_data if seg.text and seg.text.startswith("[语音")]
    if not voice_texts:
        return content_text

    merged_parts: list[str] = [content_text.strip()] if content_text.strip() else []
    for voice_text in voice_texts:
        if voice_text not in merged_parts and voice_text not in content_text:
            merged_parts.append(voice_text)
    return "".join(merged_parts).strip()


def _is_ignored_text(content_text: str) -> bool:
    ignored_prefixes = (
        [config.AI_COMMAND_OUTPUT_PREFIX, *config.AI_IGNORED_PREFIXES]
        if config.AI_COMMAND_OUTPUT_PREFIX
        else config.AI_IGNORED_PREFIXES
    )
    return any(content_text.startswith(prefix) for prefix in ignored_prefixes)


async def _quoted_video_segment(event: MessageEvent, ref_msg_id: str, chat_key: str) -> Optional[ChatMessageSegmentVideo]:
    """引用回复指向视频时补一个占位，让 AI 不用翻历史就知道说的是哪个视频

    被引用的视频可能根本不在上下文窗口里，甚至不在记录里（升级前被跳过的、NA 离线时发的）。
    `event.reply` 是 NoneBot 替我们 `get_msg` 回来的；那一步失败时再查记录。
    """
    if parse_message_id(ref_msg_id) is None:
        return None
    reply = event.reply
    if reply is not None:
        for seg in reply.message:
            if seg.type == "video":
                sender_id = str(reply.sender.user_id) if reply.sender.user_id is not None else ""
                return build_video_segment(ref_msg_id, seg.data, quoted=True, quoted_sender_id=sender_id)
    return await quoted_video_from_record(chat_key, ref_msg_id)


async def handle_self_sent_event(adapter: BaseAdapter, event: MessageEvent, bot: Bot) -> None:
    """记录账号自身发出、但不是 NA 发的消息；NA 自己的回传在这里丢掉

    先去重再做别的：回传里 NA 发的图片不该再下载一遍。
    """
    endpoint_status.note_echo(str(bot.self_id))
    channel = self_sent_channel(event)
    if channel is None:
        logger.debug(f"自身消息没有可归属的频道，已跳过: message_id={event.message_id}")
        return
    channel_id, chat_type = channel
    file_names = [str(seg.data.get("file") or "") for seg in event.original_message if seg.type == "file"]
    has_voice = any(seg.type in VOICE_SEGMENT_TYPES for seg in event.original_message)
    if await self_sent_registry.is_own(bot.self_id, channel_id, str(event.message_id), file_names, has_voice=has_voice):
        return

    instance_key = await resolve_instance_key(bot.self_id)
    set_current_instance(instance_key, bot.self_id)
    if not await is_inbound_dispatch_allowed(adapter.key, instance_key):
        return

    plt_channel = PlatformChannel(channel_id=channel_id, channel_name="", channel_type=chat_type, instance_key=instance_key)
    db_chat_channel: DBChatChannel = await plt_channel.get_db_chat_channel(adapter)
    if not (await db_chat_channel.get_effective_config()).ONEBOT_RECORD_SELF_SENT:
        return
    if not db_chat_channel.channel_name:
        plt_channel = await adapter.get_channel_info(channel_id)
        plt_channel.instance_key = instance_key

    content_data, _, message_id = await convert_chat_message(event, False, bot, db_chat_channel, adapter)
    if not content_data:
        return

    content_text, _ = await gen_chat_text(event=event, bot=bot, db_chat_channel=db_chat_channel)
    content_text = content_text or _build_content_text_from_segments(content_data)
    if _is_ignored_text(content_text):
        return
    for seg in content_data:
        if isinstance(seg, ChatMessageSegmentForward):
            content_text = seg.text
            break

    ref_msg_id = await get_message_reply_info(event=event)
    quoted = await _quoted_video_segment(event, ref_msg_id, db_chat_channel.chat_key)
    if quoted is not None:
        content_data.append(quoted)

    # 先清理再走回退链：协议端上报的彩色群名片是原始字节，清理后可能变成空串
    nickname = (
        clean_display_name(event.sender.card)
        or clean_display_name(event.sender.nickname)
        or (await db_chat_channel.get_preset()).name
    )
    plt_user = PlatformUser(platform_name="qq", user_id=str(bot.self_id), user_name=nickname)
    plt_msg = PlatformMessage(
        message_id=message_id,
        sender_id=plt_user.user_id,
        sender_name=nickname,
        sender_nickname=f"{nickname}{SELF_SENT_NICKNAME_SUFFIX}",
        content_data=content_data,
        content_text=content_text,
        is_tome=False,
        timestamp=int(time.time()),
        ext_data=PlatformMessageExt(ref_msg_id=ref_msg_id, self_sent_source=SELF_SENT_SOURCE_EXTERNAL),
        self_sent_external=True,
    )
    await collect_message(adapter, plt_channel, plt_user, plt_msg)


def register_self_sent_matcher(adapter: BaseAdapter) -> None:
    """接 `message_sent`。协议端不上报时这个匹配器永远不会被调用"""

    @on("message_sent", priority=99999, block=False).handle()
    async def _(_: Matcher, event: Union[GroupMessageSentEvent, PrivateMessageSentEvent], bot: Bot):
        await handle_self_sent_event(adapter, event, bot)


def register_matcher(adapter: BaseAdapter):

    @on_message(priority=99999, block=False).handle()
    async def _(_: Matcher, event: Union[MessageEvent, GroupMessageEvent], bot: Bot):
        """消息匹配器"""

        # 有的协议端把自身消息也按 `post_type=message` 上报。当成用户消息的话，NA 自己的回复
        # 会再触发一轮 Agent，所以这条分流要排在最前面
        if str(event.user_id) == str(bot.self_id):
            await handle_self_sent_event(adapter, event, bot)
            return

        # bot.self_id 是本条消息由哪个账号收到的唯一权威来源。先解析为稳定的实例标识并写入上下文，
        # 之后派生的异步任务与插件里既有的无参 get_bot() 才能解析到正确账号，而不是静默串号
        instance_key = await resolve_instance_key(bot.self_id)
        set_current_instance(instance_key, bot.self_id)
        # 未认领实例的闸门要挡在建频道行、向协议端问群成员信息之前（collector 里那道是兜底）
        if not await is_inbound_dispatch_allowed(adapter.key, instance_key):
            return

        # 频道信息处理：channel_id 保持干净形态，账号维度由 instance_key 单独携带
        channel_id, chat_type = await get_chat_info(event=event, bot=bot)
        plt_channel: PlatformChannel = PlatformChannel(
            channel_id=channel_id,
            channel_name="",
            channel_type=chat_type,
            instance_key=instance_key,
        )
        db_chat_channel: DBChatChannel = await plt_channel.get_db_chat_channel(adapter)

        if not db_chat_channel.channel_name:
            plt_channel = await adapter.get_channel_info(channel_id)
            # get_channel_info 只认识 channel_id，实例归属必须由本条事件的账号决定
            plt_channel.instance_key = instance_key

        # 用户信息处理：先清理再走回退链，清理后为空也要落到下一级
        user_qq = str(event.sender.user_id)
        # 昵称与名片都清理成空串时退回 QQ 号：只由控制字符组成的名字以前会原样通过，
        # 清理之后变成空串，不兜底的话这条消息会被下面的断言整条丢掉
        sender_name: str = clean_display_name(event.sender.nickname) or clean_display_name(event.sender.card) or user_qq
        assert sender_name

        user_avatar: str = f"https://q1.qlogo.cn/g?b=qq&nk={user_qq}&s=640"

        plt_user: PlatformUser = PlatformUser(
            platform_name="qq",
            user_id=user_qq,
            user_name=sender_name,
            user_avatar=user_avatar,
        )

        # 消息内容处理
        content_data, msg_tome, message_id = await convert_chat_message(event, event.to_me, bot, db_chat_channel, adapter)
        if not content_data:  # 忽略无法转换的消息
            logger.warning(f"无法转换的消息: {event.get_plaintext()}")
            return

        sender_nickname: str = await get_user_name(
            event=event,
            bot=bot,
            user_id=plt_user.user_id,
            db_chat_channel=db_chat_channel,
        )
        content_text, is_tome = await gen_chat_text(event=event, bot=bot, db_chat_channel=db_chat_channel)

        if not content_text:
            content_text = _build_content_text_from_segments(content_data)
        else:
            content_text = _merge_voice_content_text(content_text, content_data)

        # 前缀过滤必须基于消息自身文本判定。若先用转发载荷覆盖 content_text，
        # 攻击者既能把任意内容包进合并转发绕过忽略前缀，也能构造忽略前缀让整条消息被静默丢弃。
        if _is_ignored_text(content_text):
            logger.info(f"忽略前缀匹配的消息: {content_text[:32]}...")
            return

        # 合并转发消息：使用 FORWARD 段的完整文本替代 gen_chat_text 的简略占位
        for seg in content_data:
            if isinstance(seg, ChatMessageSegmentForward):
                content_text = seg.text
                break

        ref_msg_id = await get_message_reply_info(event=event)
        quoted_video = await _quoted_video_segment(event, ref_msg_id, db_chat_channel.chat_key)
        if quoted_video is not None:
            content_data.append(quoted_video)

        plt_msg: PlatformMessage = PlatformMessage(
            message_id=message_id,
            sender_id=plt_user.user_id,
            sender_name=sender_name,
            sender_nickname=sender_nickname,
            content_data=content_data,
            # 不带视频占位：伪造检查、关键词、禁词都看这一份，占位在落库时才补
            content_text=content_text,
            is_tome=bool(is_tome or msg_tome),
            timestamp=int(time.time()),
            ext_data=PlatformMessageExt(ref_msg_id=ref_msg_id),
        )

        # 提交收集消息
        await collect_message(adapter, plt_channel, plt_user, plt_msg)

    @on_notice(priority=99999, block=False).handle()
    async def _(_: Matcher, event: GroupUploadNoticeEvent, bot: Bot):
        """上传事件匹配器"""
        instance_key = await resolve_instance_key(bot.self_id)
        set_current_instance(instance_key, bot.self_id)
        if not await is_inbound_dispatch_allowed(adapter.key, instance_key):
            return

        # 频道信息处理
        channel_id, chat_type = await get_chat_info(event=event, bot=bot)
        plt_channel: PlatformChannel = PlatformChannel(
            channel_id=channel_id,
            channel_name="",
            channel_type=chat_type,
            instance_key=instance_key,
        )
        db_chat_channel: DBChatChannel = await plt_channel.get_db_chat_channel(adapter)

        # 用户信息处理
        platform_userid: str = str(event.user_id)
        # 账号自己上传的文件：NA 发的已经记过，别的程序发的走 `message_sent`，这里再记一遍就重复了
        if platform_userid == str(bot.self_id):
            return
        user: Optional[DBUser] = await DBUser.get_or_none(adapter_key=adapter.key, platform_userid=platform_userid)

        if not user:
            raise ValueError(f"用户 {platform_userid} 尚未注册，请先发送任意消息注册后即可上传文件") from None

        if not user.is_active:
            logger.info(f"用户 {platform_userid} 被封禁，封禁结束时间: {user.ban_until}")
            return

        # 用户信息处理
        sender_name: Optional[str] = user.username
        plt_user: PlatformUser = PlatformUser(platform_name="qq", user_id=platform_userid, user_name=sender_name)

        # 消息内容处理
        content_data, msg_tome, message_id = await convert_chat_message(event, False, bot, db_chat_channel, adapter)
        if not content_data:  # 忽略无法转换的消息
            return

        sender_nickname: str = await get_user_name(
            event=event,
            bot=bot,
            user_id=platform_userid,
            db_chat_channel=db_chat_channel,
        )

        plt_msg: PlatformMessage = PlatformMessage(
            message_id=message_id,
            sender_id=platform_userid,
            sender_name=sender_name,
            sender_nickname=sender_nickname,
            content_data=content_data,
            content_text=_build_content_text_from_segments(content_data),
            is_tome=bool(msg_tome),
            timestamp=int(time.time()),
        )

        # 提交收集消息
        await collect_message(adapter, plt_channel, plt_user, plt_msg)
