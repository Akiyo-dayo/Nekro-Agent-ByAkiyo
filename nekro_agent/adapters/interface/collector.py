import time
from typing import TYPE_CHECKING, Optional

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.chat_message import ChatMessage
from nekro_agent.services.message_service import message_service
from nekro_agent.services.user.util import get_or_create_platform_user

logger = get_sub_logger("adapter.interface")
if TYPE_CHECKING:
    from nekro_agent.adapters.interface import (
        BaseAdapter,
        PlatformChannel,
        PlatformMessage,
        PlatformUser,
    )


async def collect_message(
    adapter: "BaseAdapter",
    platform_channel: "PlatformChannel",
    platform_user: "PlatformUser",
    platform_message: "PlatformMessage",
) -> None:
    """适配器消息收集器"""

    # 实例闸门排在本函数最前面，**早于命令识别**。
    #
    # 位置是有讲究的：被停用的实例必须整个哑掉，而不是"只是不触发 Agent"。
    # 放在命令识别之后的话，`/na_on`、`/na_info` 之类的命令仍会执行并回话——
    # 用户看到的是一个被停用却还在说话的 Bot。租户维度的闸门原本就在命令识别之后，
    # 一并提到这里；未启用多租户的部署不受影响（那种部署里判据恒为 True）。
    #
    # 判据取 `platform_channel.instance_key`（本条事件的账号权威来源）而不是频道行上的
    # 那一列：升级前留下的历史频道行 `instance_key` 可能仍是空串（要等第一次被认领才补上），
    # 用它做判据会让停用对这些频道恰好失效。
    #
    # 覆盖不到的一段：适配器的匹配器在调用本函数**之前**就会为了组装上下文去建频道行、
    # 并可能向协议端问一次群信息/成员信息（`adapters/onebot_v11/matchers/message.py`）。
    # 那是每个适配器各自的入口，收在这里收不住；实测被停用的实例仍会发出一次
    # `get_group_member_info`，但一条消息也发不出去。要连这段也堵上，得在每个适配器的
    # 匹配器最前面各加一次同样的判断。
    if not await DBAdapterInstance.is_instance_active(adapter.key, platform_channel.instance_key):
        logger.debug(
            f"实例或其所属租户已停用，丢弃入站消息: adapter={adapter.key}, "
            f"instance_key={platform_channel.instance_key}, channel_id={platform_channel.channel_id}",
        )
        return

    try:
        chat_key = platform_channel.build_chat_key(adapter)
    except Exception:
        # build_chat_key 默认为纯字符串拼接、正常不会抛错；若某适配器重写后异常，此处不打断消息收集，
        # 回退到 DBChatChannel 的默认 chat_key。但必须记日志暴露问题，避免静默写入不一致的 chat_key。
        logger.warning(
            f"适配器 build_chat_key 失败，回退默认 chat_key: adapter={adapter.key}, "
            f"channel_id={platform_channel.channel_id}",
            exc_info=True,
        )
        chat_key = None

    db_chat_channel: DBChatChannel = await DBChatChannel.get_or_create(
        adapter_key=adapter.key,
        channel_id=platform_channel.channel_id,
        channel_type=platform_channel.channel_type,
        channel_name=platform_channel.channel_name,
        chat_key=chat_key,
        instance_key=platform_channel.instance_key,
    )

    # 命令检测与执行（在 is_active 检查之前，确保 na_on 等命令在频道关闭时仍可用）
    chat_key = db_chat_channel.chat_key
    content_text = platform_message.content_text.strip()
    if content_text and await _try_handle_command(
        adapter, chat_key, platform_user, platform_message, content_text,
    ):
        return

    if not db_chat_channel.is_active:
        return

    # 用户处理：存在即取回、不存在才建；同一新用户的并发首批消息由
    # 联合键唯一索引 + IntegrityError 回读兜底（见 get_or_create_platform_user）
    try:
        user: Optional[DBUser] = await get_or_create_platform_user(
            adapter_key=adapter.key,
            platform_userid=platform_user.user_id,
            username=platform_user.user_name,
        )
    except Exception:
        logger.exception(f"注册用户失败: {platform_user.user_name} - {platform_user.user_id}")
        return

    if not user:
        return

    if not user.is_active:
        logger.info(f"用户 {platform_user.user_id} 被封禁，封禁结束时间: {user.ban_until}")
        return

    if platform_message.is_self:
        logger.info(f'接收自身消息 "{platform_message.content_text}"，跳过...')
        return

    chat_message: ChatMessage = ChatMessage(
        message_id=platform_message.message_id,
        sender_id=platform_user.user_id,
        sender_name=platform_user.user_name,
        sender_nickname=platform_message.sender_nickname,
        adapter_key=adapter.key,
        instance_key=db_chat_channel.instance_key,
        platform_userid=platform_user.user_id,
        is_tome=platform_message.is_tome,
        is_recalled=False,
        chat_key=chat_key,
        chat_type=platform_channel.channel_type,
        content_text=platform_message.content_text,
        content_data=platform_message.content_data,
        raw_cq_code="",
        ext_data=platform_message.ext_data.model_dump() if platform_message.ext_data else {},
        send_timestamp=int(time.time()),
    )

    ref_str: str = (
        f" (ref: {platform_message.ext_data.ref_msg_id})"
        if platform_message.ext_data and platform_message.ext_data.ref_msg_id
        else ""
    )

    logger.info(
        f"Message Collect: [{chat_message.chat_key}] {platform_user.platform_name} {chat_message.sender_nickname}: {chat_message.content_text}{ref_str}",
    )

    await message_service.push_human_message(
        message=chat_message,
        user=user,
        db_chat_channel=db_chat_channel,
        suppress_trigger=platform_message.suppress_trigger,
    )


async def _try_handle_command(
    adapter: "BaseAdapter",
    chat_key: str,
    platform_user: "PlatformUser",
    platform_message: "PlatformMessage",
    content_text: str,
) -> bool:
    """尝试检测和执行命令，返回 True 表示消息已被命令系统消费

    检测顺序:
    1. 检测命令前缀 → 执行命令
    2. 检测挂起的 wait 交互 → 路由到回调命令
    """
    from nekro_agent.core.config import config

    is_super = platform_user.user_id in config.SUPER_USERS
    is_advanced = is_super and config.ENABLE_ADVANCED_COMMAND

    # 1. 命令前缀检测
    cmd_result = adapter.detect_command(content_text)
    if cmd_result:
        command_name, raw_args = cmd_result
        logger.info(f"Command Detect: [{chat_key}] {platform_user.user_name}: /{command_name} {raw_args}")

        await adapter.execute_command(
            chat_key=chat_key,
            user_id=platform_user.user_id,
            username=platform_user.user_name,
            command_name=command_name,
            raw_args=raw_args,
            is_super_user=is_super,
            is_advanced_user=is_advanced,
        )

        return True

    # 2. 挂起的 wait 交互检测
    consumed = await adapter.try_handle_wait_input(
        chat_key=chat_key,
        user_id=platform_user.user_id,
        username=platform_user.user_name,
        text=content_text,
        is_super_user=is_super,
        is_advanced_user=is_advanced,
    )
    if consumed:
        logger.info(f"Wait Consumed: [{chat_key}] {platform_user.user_name}: {content_text}")
        return True

    return False
