import inspect
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, Set

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.chat_message import SELF_SENT_SENDER_ID, ChatMessage
from nekro_agent.services.command.base import CommandPermission
from nekro_agent.services.message_service import message_service
from nekro_agent.services.unclaimed_dispatch import is_inbound_dispatch_allowed
from nekro_agent.services.user.util import register_platform_user

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
    #
    # 未认领实例另有一道判据（`services/unclaimed_dispatch.py`）：只有凭当前未曝光的部署级令牌连上来的才分发
    if not await is_inbound_dispatch_allowed(adapter.key, platform_channel.instance_key):
        logger.debug(
            f"实例已停用 / 所属租户已停用 / 未认领且连接不合格，丢弃入站消息: adapter={adapter.key}, "
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

    chat_key = db_chat_channel.chat_key

    # 排在命令识别之前：Yunzai 发一句 `/na_clear`，或者挂着 wait 交互时它随便说一句，都不是在对 NA 下命令
    if platform_message.self_sent_external:
        if db_chat_channel.is_active:
            await _record_self_sent(adapter, db_chat_channel, platform_channel, platform_user, platform_message)
        return

    # 命令检测与执行（在 is_active 检查之前，确保 na_on 等命令在频道关闭时仍可用）
    content_text = platform_message.content_text.strip()

    # 适配器可声明"命令原文也写进聊天记录"（`record_command_input`，网页适配器为真）：
    # 命令会被命令系统消费、不再走下面的 `push_human_message`，不在这里补一条，
    # 用户看到的历史里就只有命令的输出、没有他自己发的那句话。
    # 用 `getattr` 读：不认识这个属性的适配器（含第三方插件里手写的假适配器）照旧不记录。
    if content_text and getattr(adapter, "record_command_input", False) and adapter.detect_command(content_text):
        await message_service.record_human_message(
            _build_chat_message(adapter, db_chat_channel, platform_channel, platform_user, platform_message),
            db_chat_channel=db_chat_channel,
        )

    if content_text and await _dispatch_try_handle_command(
        adapter, chat_key, platform_channel, platform_user, platform_message, content_text,
    ):
        return

    if not db_chat_channel.is_active:
        return

    # 用户处理：存在即取回、不存在才建；同一新用户的并发首批消息由
    # 联合键唯一索引 + IntegrityError 回读兜底（见 register_platform_user）
    try:
        user: Optional[DBUser]
        user, created = await register_platform_user(
            adapter_key=adapter.key,
            platform_userid=platform_user.user_id,
            username=platform_user.user_name,
        )
    except Exception:
        logger.exception(f"注册用户失败: {platform_user.user_name} - {platform_user.user_id}")
        return

    if not user:
        return

    if created:
        await _persist_registered_user_command_permission(
            adapter,
            platform_channel,
            platform_user,
            platform_message,
        )

    if not user.is_active:
        logger.info(f"用户 {platform_user.user_id} 被封禁，封禁结束时间: {user.ban_until}")
        return

    if platform_message.is_self:
        logger.info(f'接收自身消息 "{platform_message.content_text}"，跳过...')
        return

    chat_message: ChatMessage = _build_chat_message(adapter, db_chat_channel, platform_channel, platform_user, platform_message)

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
    )


def _build_chat_message(
    adapter: "BaseAdapter",
    db_chat_channel: DBChatChannel,
    platform_channel: "PlatformChannel",
    platform_user: "PlatformUser",
    platform_message: "PlatformMessage",
) -> ChatMessage:
    """把一条平台消息转成入库用的 `ChatMessage`

    命令原文入库与正常入站两条路径共用同一份构造，避免两边字段（尤其 `instance_key`）不一致。
    """
    return ChatMessage(
        message_id=platform_message.message_id,
        sender_id=platform_user.user_id,
        sender_name=platform_user.user_name,
        sender_nickname=platform_message.sender_nickname,
        adapter_key=adapter.key,
        instance_key=db_chat_channel.instance_key,
        platform_userid=platform_user.user_id,
        is_tome=platform_message.is_tome,
        is_recalled=False,
        chat_key=db_chat_channel.chat_key,
        chat_type=platform_channel.channel_type,
        content_text=platform_message.content_text,
        content_data=platform_message.content_data,
        raw_cq_code="",
        ext_data=platform_message.ext_data.model_dump() if platform_message.ext_data else {},
        send_timestamp=int(time.time()),
    )


async def _record_self_sent(
    adapter: "BaseAdapter",
    db_chat_channel: DBChatChannel,
    platform_channel: "PlatformChannel",
    platform_user: "PlatformUser",
    platform_message: "PlatformMessage",
) -> None:
    """记录账号自身由其他程序发出的消息

    发送者是 Bot 账号自己，不注册用户、不判封禁。`sender_id` 用保留值，理由见 `SELF_SENT_SENDER_ID`。
    """
    chat_message = ChatMessage(
        message_id=platform_message.message_id,
        sender_id=SELF_SENT_SENDER_ID,
        sender_name=platform_user.user_name,
        sender_nickname=platform_message.sender_nickname,
        adapter_key=adapter.key,
        instance_key=db_chat_channel.instance_key,
        platform_userid=platform_user.user_id,
        is_tome=0,
        is_recalled=False,
        chat_key=db_chat_channel.chat_key,
        chat_type=platform_channel.channel_type,
        content_text=platform_message.content_text,
        content_data=platform_message.content_data,
        raw_cq_code="",
        ext_data=platform_message.ext_data.model_dump() if platform_message.ext_data else {},
        send_timestamp=int(time.time()),
    )
    await message_service.push_human_message(message=chat_message, db_chat_channel=db_chat_channel, self_sent=True)


async def _persist_registered_user_command_permission(
    adapter: "BaseAdapter",
    platform_channel: "PlatformChannel",
    platform_user: "PlatformUser",
    platform_message: "PlatformMessage",
) -> None:
    """注册新用户后检测并登记适配器授予的平台权限

    登记的是展示用的快照（用户管理页的「命令权限」）：真正执行命令时 OneBot 每次实时查群角色，
    平台来源的记录本身不放行任何命令。与上游的差别：

    - 已有**手动**记录时不登记。上游会把它改写成平台来源，此人以后卸任群管理时，
      角色丢失的清理会连平台方给的手动授权一起删掉；
    - 任何一步出错都只记日志：这里位于消息入口，登记失败不能让这条消息丢掉。
    """
    from nekro_agent.services.command.manager import (
        USER_PERMISSION_SOURCE_MANUAL,
        USER_PERMISSION_SOURCE_PLATFORM,
        command_manager,
    )
    from nekro_agent.services.user.super_users import is_super_user

    if await is_super_user(adapter.key, platform_channel.instance_key, platform_user.user_id):
        logger.info(
            f"注册用户命令权限检测跳过持久化: adapter={adapter.key}, "
            f"user_id={platform_user.user_id}, permission={CommandPermission.SUPER_USER.value}, source=SUPER_USERS",
        )
        return

    def has_manual_grant() -> bool:
        existing = command_manager.get_user_permission_record(adapter.key, platform_user.user_id)
        return existing.permission != CommandPermission.USER and existing.source == USER_PERMISSION_SOURCE_MANUAL

    try:
        if has_manual_grant():
            logger.debug(
                f"注册用户已有手动命令授权，不登记平台来源: adapter={adapter.key}, user_id={platform_user.user_id}",
            )
            return

        user_permission = await adapter.get_user_command_permission(
            platform_user,
            platform_channel,
            platform_message,
        )
        if user_permission not in {CommandPermission.ADVANCED, CommandPermission.SUPER_USER}:
            logger.debug(
                f"注册用户命令权限检测完成，保持普通权限: adapter={adapter.key}, "
                f"user_id={platform_user.user_id}, permission={user_permission.value}",
            )
            return

        # 上面那次查询要等协议端，期间平台方可能刚好手动授了权：写之前再看一眼，不把它改写成平台来源
        if has_manual_grant():
            return

        persisted_permission = CommandPermission.ADVANCED
        await adapter.set_user_command_permission(
            platform_user.user_id,
            persisted_permission,
            source=USER_PERMISSION_SOURCE_PLATFORM,
            channel_id=platform_channel.channel_id,
        )
        logger.info(
            f"注册用户时自动登记命令权限: adapter={adapter.key}, "
            f"channel_id={platform_channel.channel_id}, user_id={platform_user.user_id}, "
            f"permission={persisted_permission.value}, detected_permission={user_permission.value}, "
            f"source={USER_PERMISSION_SOURCE_PLATFORM}",
        )
    except Exception:
        logger.warning(
            f"注册用户后检测命令权限失败，保持普通权限: adapter={adapter.key}, user_id={platform_user.user_id}",
            exc_info=True,
        )


async def _try_handle_command(
    adapter: "BaseAdapter",
    chat_key: str,
    platform_channel: "PlatformChannel",
    platform_user: "PlatformUser",
    platform_message: "PlatformMessage",
    content_text: str,
) -> bool:
    """尝试检测和执行命令，返回 True 表示消息已被命令系统消费

    **参数形状与上游 NekroAgent 2.4.x 逐位一致，不要增删、不要调换位置。**
    市场里的第三方插件会把本函数整个替换成自己的包装函数（例如在命令执行期间记下触发消息），
    包装函数照上游签名写成固定的 6 个位置参数。这里少传或多传一个，插件的包装函数就会在调用时
    抛 `TypeError`，而本函数的调用点位于消息入口：所有频道的文字消息都会因此丢失。
    确实需要新参数时，只能加在末尾并带默认值。

    检测顺序:
    1. 检测命令前缀 → 执行命令
    2. 检测挂起的 wait 交互 → 路由到回调命令

    权限只在真的要执行时才查（`_resolve_user_command_flags`）：普通聊天消息不该为此多问一次协议端。
    """
    # 1. 命令前缀检测
    cmd_result = adapter.detect_command(content_text)
    if cmd_result:
        is_super, is_advanced = await _resolve_user_command_flags(
            adapter,
            platform_channel,
            platform_user,
            platform_message,
            chat_key=chat_key,
        )
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
    from nekro_agent.services.command.wait_manager import wait_manager

    if not wait_manager.has_pending(chat_key, platform_user.user_id):
        return False

    is_super, is_advanced = await _resolve_user_command_flags(
        adapter,
        platform_channel,
        platform_user,
        platform_message,
        chat_key=chat_key,
    )
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


async def _resolve_user_command_flags(
    adapter: "BaseAdapter",
    platform_channel: "PlatformChannel",
    platform_user: "PlatformUser",
    platform_message: "PlatformMessage",
    *,
    chat_key: Optional[str] = None,
) -> tuple[bool, bool]:
    """解析命令执行上下文中的 `(is_super_user, is_advanced_user)`

    前 4 个参数与上游 2.4.x 的同名函数一致：照上游函数体整个替换 `_try_handle_command` 的插件
    会按 4 参调用它。本分支额外要的会话标识放在末尾、带默认值，没传时按频道现算。

    两层权限叠加，任何一层都不能把另一层拒绝的东西放行：

    - **超级用户只认 `SUPER_USERS`，且只认平台方能担保的来源**（`services/user/super_users.py`：
      名单匹配 ∧ 来源可信——号码属于哪个平台、这条消息的发送者身份是不是平台方能担保的）；
      两个标志的取值与引入用户级权限之前一致（advanced 仍跟 `ENABLE_ADVANCED_COMMAND`）。
      适配器或存储给出的 `SUPER_USER` 一律压成 `ADVANCED`：超级用户能跨频道、跨租户批量操作、
      在宿主上执行命令，多租户部署里它只能是平台方；
    - 用户级权限（上游）最多把人抬到 `ADVANCED`，来源有三个：本频道授权（只按完整 `chat_key`
      匹配，租户在自己频道里给的授权换个频道就不存在）、平台方的全局授权、适配器实时识别
      （OneBot 群主 / 管理员，要该频道开了 `COMMAND_GROUP_ADMIN_AS_CHANNEL_ADMIN`）。
      `ADVANCED` 能做的事由命令自己收在当前频道（`built_in/_scope.py`），命令权限覆盖照旧生效。

    查询抛错（包括授权文件读不出来）按普通用户处理：这里位于消息入口，不能把异常抛给它。
    """
    from nekro_agent.core.config import config
    from nekro_agent.services.command.manager import command_manager
    from nekro_agent.services.user.super_users import is_super_user

    if await is_super_user(adapter.key, platform_channel.instance_key, platform_user.user_id):
        return True, bool(config.ENABLE_ADVANCED_COMMAND)

    try:
        resolved_chat_key = chat_key if chat_key is not None else platform_channel.build_chat_key(adapter)
        # 本频道授权先查：不需要问协议端
        if (
            command_manager.get_channel_user_permission(resolved_chat_key, adapter.key, platform_user.user_id)
            == CommandPermission.ADVANCED
        ):
            return False, True

        user_permission = await adapter.get_user_command_permission(
            platform_user,
            platform_channel,
            platform_message,
        )
    except Exception:
        logger.warning(
            f"获取用户命令权限失败，回退普通权限: adapter={adapter.key}, user_id={platform_user.user_id}",
            exc_info=True,
        )
        return False, False

    if user_permission == CommandPermission.SUPER_USER:
        logger.info(
            f"适配器给出的超级用户权限按频道管理员处理（超级用户只认 SUPER_USERS）: "
            f"adapter={adapter.key}, user_id={platform_user.user_id}",
        )
        return False, True
    return False, user_permission == CommandPermission.ADVANCED


# 框架自带的命令钩子实现。插件可以替换模块属性 `_try_handle_command`，但这一份引用不会变，
# 是签名不符时的退路。
_FRAMEWORK_TRY_HANDLE_COMMAND = _try_handle_command

# 已告警过的替换者（模块名 + 限定名），同一个替换者只告警一次，避免每条消息刷一次日志
_warned_command_hooks: Set[str] = set()


def _resolve_command_hook(args: tuple) -> Callable[..., Awaitable[bool]]:
    """取当前生效的命令钩子；被替换且签名接不住这组参数时退回框架实现

    用 `inspect.signature().bind()` 在调用**之前**判断，而不是捕获调用时的 `TypeError`：
    插件函数体内自己抛出的 `TypeError` 属于插件的正常报错，不能被误判成签名不符而吞掉。
    """
    hook: Any = globals()["_try_handle_command"]
    if hook is _FRAMEWORK_TRY_HANDLE_COMMAND:
        return hook
    try:
        inspect.signature(hook).bind(*args)
    except ValueError:
        # 取不到签名（C 实现等），无从预检，按原样调用
        return hook
    except TypeError:
        owner = f"{getattr(hook, '__module__', '?')}.{getattr(hook, '__qualname__', type(hook).__name__)}"
        if owner not in _warned_command_hooks:
            _warned_command_hooks.add(owner)
            try:
                actual = str(inspect.signature(hook))
            except (TypeError, ValueError):
                actual = "<无法获取>"
            logger.warning(
                f"命令钩子 _try_handle_command 被 {owner} 替换，但它的签名接不住框架传入的 {len(args)} 个参数，"
                f"已改用框架自带实现；该插件依赖这个钩子的附加功能不会生效。"
                f"期望签名: (adapter, chat_key, platform_channel, platform_user, platform_message, content_text)，"
                f"实际签名: {actual}",
            )
        return _FRAMEWORK_TRY_HANDLE_COMMAND
    return hook


async def _dispatch_try_handle_command(*args: Any) -> bool:
    """调用当前生效的命令钩子

    插件替换钩子时写错签名，最多让它自己的附加功能失效，不能拖垮整个消息入口。
    """
    return await _resolve_command_hook(args)(*args)
