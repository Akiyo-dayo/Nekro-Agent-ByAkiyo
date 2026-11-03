import asyncio
import random
import shutil
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Set, Tuple

import json5
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from tortoise.expressions import Q
from tortoise.queryset import QuerySet

from nekro_agent.adapters import get_adapter
from nekro_agent.adapters.onebot_v11.core.bot import BotInstanceOfflineError, get_bot_by_chat_key
from nekro_agent.adapters.onebot_v11.core.message_id import parse_message_id
from nekro_agent.adapters.onebot_v11.tools import video as onebot_video
from nekro_agent.adapters.onebot_v11.tools.video import (
    ADAPTER_KEY as ONEBOT_ADAPTER_KEY,
)
from nekro_agent.adapters.onebot_v11.tools.video import (
    PROXY_SECURITY_HEADERS,
    OnebotVideoError,
    VideoRecord,
    VideoStream,
    VideoStreamResponse,
    VideoUnavailableReason,
    find_video_record,
    open_message_video,
)
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import SANDBOX_SHARED_HOST_DIR, USER_UPLOAD_DIR
from nekro_agent.models.db_announcement import (
    TERMINAL_ANNOUNCEMENT_STATUSES,
    AnnouncementScopeType,
    AnnouncementStatus,
    DBAnnouncement,
    DBAnnouncementDelivery,
)
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.models.db_plugin_data import DBPluginData
from nekro_agent.models.db_recurring_timer_job import DBRecurringTimerJob
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.agent_message import AgentMessageSegment, AgentMessageSegmentType
from nekro_agent.schemas.errors import (
    AdapterUnavailableError,
    MessageVideoBusyError,
    MessageVideoGoneError,
    MessageVideoUnavailableError,
    NotFoundError,
    ValidationError,
)
from nekro_agent.services.announcement import (
    AnnouncementDetail,
    AnnouncementInstanceOption,
    AnnouncementInstanceRef,
    AnnouncementListResponse,
    AnnouncementPreview,
    AnnouncementScope,
    AnnouncementSubmitResponse,
    announcement_service,
)
from nekro_agent.services.channel_broadcaster import channel_broadcaster
from nekro_agent.services.command.base import CommandPermission
from nekro_agent.services.command.manager import (
    USER_PERMISSION_SOURCE_MANUAL,
    UserPermissionRecord,
    command_manager,
    is_group_admin_detection_enabled,
)
from nekro_agent.services.config_resolver import config_resolver
from nekro_agent.services.identity_admin import PRESET_SWITCH_REVERSIBLE_NOTE, preview_preset_switch
from nekro_agent.services.message_service import message_service
from nekro_agent.services.user.auth import expected_pwd_gen_claim
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, check_role, require_role
from nekro_agent.services.user.super_users import is_super_user_source_trusted_for_display, matches_super_users
from nekro_agent.services.user.tenant_perm import (
    InstanceRef,
    accessible_instance_refs,
    assert_instance_accessible,
    get_tenant_scope,
    instance_refs_filter,
)
from nekro_agent.tools.path_convertor import sanitize_chat_key_for_path
from nekro_agent.tools.video_access import (
    build_video_url,
    issue_video_access_token,
    verify_video_access_token,
    video_token_expiry,
    video_token_matches_generation,
)

router = APIRouter(prefix="/chat-channel", tags=["ChatChannel"])

logger = get_sub_logger("chat_channel_api")


async def visible_channels(user: DBUser) -> QuerySet[DBChatChannel]:
    """按当前身份的租户作用域筛出可见频道

    平台方不受限（查询集不加任何条件，与改造前完全一致）；
    租户只能看到自己名下实例的频道，实例列表为空时结果自然为空。
    """
    if get_tenant_scope(user) is None:
        return DBChatChannel.all()
    return DBChatChannel.filter(instance_refs_filter(await accessible_instance_refs(user)))


async def assert_channel_accessible(user: DBUser, chat_key: str) -> None:
    """校验当前身份能否访问某个频道

    频道不存在、或属于别的租户的实例，一律 404：403 会把平台方的频道变成可枚举的。
    归属按 `(adapter_key, instance_key)` 成对比较，理由见 `accessible_instance_refs`。
    """
    if get_tenant_scope(user) is None:
        return
    channel = await DBChatChannel.filter(chat_key=chat_key).first()
    if channel is None or (channel.adapter_key, channel.instance_key) not in await accessible_instance_refs(user):
        raise NotFoundError(resource="聊天频道")


class ChatChannelItem(BaseModel):
    id: int
    chat_key: str
    channel_name: Optional[str]
    custom_channel_name: Optional[str]
    is_active: bool
    status: str
    chat_type: str
    message_count: int
    create_time: str
    update_time: str
    last_message_time: Optional[str]


class ChatChannelListResponse(BaseModel):
    total: int
    items: List[ChatChannelItem]


class ChatChannelDirectoryItem(BaseModel):
    id: int
    chat_key: str
    channel_name: Optional[str]
    custom_channel_name: Optional[str]
    is_active: bool
    status: str
    chat_type: str


class ChatChannelDirectoryResponse(BaseModel):
    items: List[ChatChannelDirectoryItem]


class ChatChannelDetail(ChatChannelItem):
    unique_users: int
    conversation_start_time: str
    preset_id: Optional[int]
    can_send: bool = False
    ai_always_include_msg_id: bool = False


class ChatMessage(BaseModel):
    id: int
    sender_id: str
    sender_name: str
    sender_nickname: str
    platform_userid: str
    content: str
    content_data: List[Dict[str, Any]]
    chat_key: str
    create_time: str
    message_id: str = ""
    ref_msg_id: str = ""


class ChatMessageListResponse(BaseModel):
    total: int
    items: List[ChatMessage]


class ActionResponse(BaseModel):
    ok: bool = True


class UpdateCustomChannelNameRequest(BaseModel):
    custom_channel_name: Optional[str] = None


class ChannelDeletePreview(BaseModel):
    message_count: int
    timer_job_count: int
    plugin_data_count: int
    mem_paragraph_count: int
    mem_episode_count: int
    upload_dir_exists: bool
    sandbox_dir_exists: bool


@router.get("/list", summary="获取聊天频道列表")
@require_role(Role.Admin)
async def get_chat_channel_list(
    page: int = 1,
    page_size: int = 20,
    search: Optional[str] = None,
    chat_type: Optional[str] = None,
    status: Optional[str] = None,
    is_active: Optional[bool] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ChatChannelListResponse:
    """获取聊天频道列表（租户只看得到自己实例下的会话）"""
    query = await visible_channels(_current_user)

    if search:
        query = query.filter(
            Q(chat_key__contains=search) | Q(channel_name__contains=search) | Q(data__contains=search),
        )
    if chat_type:
        query = query.filter(channel_type=chat_type)
    if status is not None:
        if status == "active":
            query = query.filter(is_active=True, observe_mode=False)
        elif status == "observe":
            query = query.filter(is_active=True, observe_mode=True)
        elif status == "disabled":
            query = query.filter(is_active=False)
        else:
            raise ValidationError(reason="无效的频道状态筛选值")
    elif is_active is not None:
        query = query.filter(is_active=is_active)

    channels = await query.all()

    channel_info_list = []
    for channel in channels:
        message_count = await DBChatMessage.filter(
            chat_key=channel.chat_key,
            create_time__gte=channel.conversation_start_time,
        ).count()

        last_message = await DBChatMessage.filter(chat_key=channel.chat_key).order_by("-create_time").first()

        conversation_start_time = channel.conversation_start_time
        if conversation_start_time.tzinfo is not None:
            conversation_start_time = conversation_start_time.replace(tzinfo=None)

        if last_message:
            last_message_time = last_message.create_time
            if last_message_time.tzinfo is not None:
                last_message_time = last_message_time.replace(tzinfo=None)
            last_active_time = max(conversation_start_time, last_message_time)
        else:
            last_active_time = conversation_start_time

        channel_info_list.append(
            {
                "channel": channel,
                "message_count": message_count,
                "last_active_time": last_active_time,
                "last_message_time": last_message_time if last_message else None,
            },
        )

    channel_info_list.sort(key=lambda x: x["last_active_time"], reverse=True)

    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    paged_channels = channel_info_list[start_idx:end_idx]

    result: List[ChatChannelItem] = []
    for info in paged_channels:
        channel = info["channel"]
        result.append(
            ChatChannelItem(
                id=channel.id,
                chat_key=channel.chat_key,
                channel_name=channel.channel_name,
                custom_channel_name=channel.get_custom_channel_name(),
                is_active=channel.is_active,
                status=channel.channel_status,
                chat_type=channel.chat_type.value,
                message_count=info["message_count"],
                create_time=channel.create_time.strftime("%Y-%m-%d %H:%M:%S"),
                update_time=channel.update_time.strftime("%Y-%m-%d %H:%M:%S"),
                last_message_time=(
                    info["last_message_time"].strftime("%Y-%m-%d %H:%M:%S") if info["last_message_time"] is not None else None
                ),
            ),
        )

    return ChatChannelListResponse(
        total=len(channels),
        items=result,
    )


@router.get("/directory", summary="获取聊天频道目录")
@require_role(Role.Admin)
async def get_chat_channel_directory(
    _current_user: DBUser = Depends(get_current_active_user),
) -> ChatChannelDirectoryResponse:
    """获取全量频道目录。

    用于前端全局复用 chat_key -> 频道显示信息映射，不包含消息统计等重字段。
    租户只看得到自己实例下的会话。
    """
    channels = await (await visible_channels(_current_user)).order_by("-update_time")
    items = [
        ChatChannelDirectoryItem(
            id=channel.id,
            chat_key=channel.chat_key,
            channel_name=channel.channel_name,
            custom_channel_name=channel.get_custom_channel_name(),
            is_active=channel.is_active,
            status=channel.channel_status,
            chat_type=channel.chat_type.value,
        )
        for channel in channels
    ]
    return ChatChannelDirectoryResponse(items=items)


@router.get("/list/stream", summary="获取聊天频道列表实时流")
@require_role(Role.Admin)
async def stream_chat_channel_list(
    request: Request,
    _current_user: DBUser = Depends(get_current_active_user),
):
    """获取聊天频道列表的实时更新流，使用 Server-Sent Events (SSE)

    Returns:
        StreamingResponse: SSE 流，每条频道事件作为一个数据

    Events:
        - created: 新频道创建
        - updated: 频道信息更新
        - deleted: 频道被删除
        - activated: 频道被激活
        - deactivated: 频道被停用
    """
    import json

    from nekro_agent.services.channel_broadcaster import channel_broadcaster
    from nekro_agent.services.runtime_state import is_shutting_down

    subscription = channel_broadcaster.subscribe()
    # 广播是全局的，租户订阅时必须逐条按作用域过滤，否则列表接口挡住的频道会从实时流里漏出去
    tenant_scoped = get_tenant_scope(_current_user) is not None
    scoped_instances = await accessible_instance_refs(_current_user) if tenant_scoped else set()

    def cleanup_subscription() -> None:
        subscription.close()

    async def handle_client_disconnect(_: object) -> None:
        cleanup_subscription()

    async def event_generator():
        """生成 SSE 事件流"""
        try:
            while not is_shutting_down():
                if await request.is_disconnected():
                    return
                try:
                    event = await subscription.get(timeout=1.0)
                except asyncio.TimeoutError:
                    yield {"comment": "ping"}
                    continue

                if tenant_scoped:
                    channel = await DBChatChannel.filter(chat_key=event.chat_key).first()
                    if channel is None or (channel.adapter_key, channel.instance_key) not in scoped_instances:
                        continue

                yield {"data": json.dumps(event.model_dump())}
        finally:
            cleanup_subscription()

    return EventSourceResponse(
        event_generator(),
        client_close_handler_callable=handle_client_disconnect,
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/detail/{chat_key}", summary="获取聊天频道详情")
@require_role(Role.Admin)
async def get_chat_channel_detail(chat_key: str, _current_user: DBUser = Depends(get_current_active_user)) -> ChatChannelDetail:
    """获取聊天频道详情"""
    await assert_channel_accessible(_current_user, chat_key)

    channel = await DBChatChannel.filter(chat_key=chat_key).first()
    if not channel:
        raise NotFoundError(resource="聊天频道")

    message_count = await DBChatMessage.filter(chat_key=chat_key, create_time__gte=channel.conversation_start_time).count()
    last_message = await DBChatMessage.filter(chat_key=chat_key).order_by("-create_time").first()
    last_message_time = last_message.create_time if last_message else None
    unique_users = await DBChatMessage.filter(chat_key=chat_key).distinct().values_list("sender_id", flat=True)

    # 检测适配器是否支持 WebUI 发送
    can_send = False
    try:
        adapter = get_adapter(channel.adapter_key)
        can_send = adapter.supports_webui_send
    except Exception:
        pass

    # 获取频道有效配置
    ai_always_include_msg_id = False
    try:
        effective_config = await config_resolver.get_effective_config(chat_key)
        ai_always_include_msg_id = effective_config.AI_ALWAYS_INCLUDE_MSG_ID
    except Exception:
        pass

    return ChatChannelDetail(
        id=channel.id,
        chat_key=channel.chat_key,
        channel_name=channel.channel_name,
        custom_channel_name=channel.get_custom_channel_name(),
        is_active=channel.is_active,
        status=channel.channel_status,
        chat_type=channel.chat_type.value,
        message_count=message_count,
        unique_users=len(unique_users),
        create_time=channel.create_time.strftime("%Y-%m-%d %H:%M:%S"),
        update_time=channel.update_time.strftime("%Y-%m-%d %H:%M:%S"),
        last_message_time=last_message_time.strftime("%Y-%m-%d %H:%M:%S") if last_message_time else None,
        conversation_start_time=channel.conversation_start_time.strftime("%Y-%m-%d %H:%M:%S"),
        preset_id=channel.preset_id,
        can_send=can_send,
        ai_always_include_msg_id=ai_always_include_msg_id,
    )


@router.post("/{chat_key}/active", summary="设置聊天频道激活状态")
@require_role(Role.Admin)
async def set_chat_channel_active(
    chat_key: str,
    is_active: bool,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """设置聊天频道激活状态（兼容旧接口）"""
    await assert_channel_accessible(_current_user, chat_key)

    channel = await DBChatChannel.filter(chat_key=chat_key).first()
    if not channel:
        raise NotFoundError(resource="聊天频道")

    await channel.set_channel_status("active" if is_active else "disabled")
    await channel_broadcaster.publish_update(
        event_type="updated",
        chat_key=channel.chat_key,
        channel_name=channel.channel_name,
        custom_channel_name=channel.get_custom_channel_name(),
        is_active=channel.is_active,
        status=channel.channel_status,
    )
    return ActionResponse(ok=True)


@router.post("/{chat_key}/status", summary="设置聊天频道状态")
@require_role(Role.Admin)
async def set_chat_channel_status(
    chat_key: str,
    status: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """设置聊天频道状态

    Args:
        status: 频道状态，可选值: active（激活）, observe（旁观）, disabled（停用）
    """
    await assert_channel_accessible(_current_user, chat_key)

    channel = await DBChatChannel.filter(chat_key=chat_key).first()
    if not channel:
        raise NotFoundError(resource="聊天频道")

    from nekro_agent.models.db_chat_channel import ChannelStatus

    try:
        ChannelStatus(status)
    except ValueError:
        return ActionResponse(ok=False)

    await channel.set_channel_status(status)
    await channel_broadcaster.publish_update(
        event_type="updated",
        chat_key=channel.chat_key,
        channel_name=channel.channel_name,
        custom_channel_name=channel.get_custom_channel_name(),
        is_active=channel.is_active,
        status=channel.channel_status,
    )
    return ActionResponse(ok=True)


@router.put("/{chat_key}/custom-name", summary="设置聊天频道自定义名称")
@require_role(Role.Admin)
async def set_chat_channel_custom_name(
    chat_key: str,
    payload: UpdateCustomChannelNameRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    await assert_channel_accessible(_current_user, chat_key)

    channel = await DBChatChannel.filter(chat_key=chat_key).first()
    if not channel:
        raise NotFoundError(resource="聊天频道")

    custom_channel_name = payload.custom_channel_name.strip() if payload.custom_channel_name else ""
    if len(custom_channel_name) > 64:
        raise ValidationError(reason="频道自定义名称不能超过 64 个字符")

    await channel.set_custom_channel_name(custom_channel_name or None)
    await channel_broadcaster.publish_update(
        event_type="updated",
        chat_key=channel.chat_key,
        channel_name=channel.channel_name,
        custom_channel_name=channel.get_custom_channel_name(),
        is_active=channel.is_active,
        status=channel.channel_status,
    )
    return ActionResponse(ok=True)


@router.post("/{chat_key}/reset", summary="重置聊天频道状态")
@require_role(Role.Admin)
async def reset_chat_channel(
    chat_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """重置聊天频道状态"""
    await assert_channel_accessible(_current_user, chat_key)

    channel = await DBChatChannel.filter(chat_key=chat_key).first()
    if not channel:
        raise NotFoundError(resource="聊天频道")

    await channel.reset_channel()
    return ActionResponse(ok=True)


@router.get("/{chat_key}/messages", summary="获取聊天频道消息列表")
@require_role(Role.Admin)
async def get_chat_channel_messages(
    chat_key: str,
    before_id: Optional[int] = None,
    page_size: int = 32,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ChatMessageListResponse:
    """获取聊天频道消息列表"""
    await assert_channel_accessible(_current_user, chat_key)

    channel = await DBChatChannel.filter(chat_key=chat_key).first()
    if not channel:
        raise NotFoundError(resource="聊天频道")

    query = DBChatMessage.filter(chat_key=chat_key, create_time__gte=channel.conversation_start_time)
    if before_id:
        query = query.filter(id__lt=before_id)

    total = await query.count()
    messages = await query.order_by("-id").limit(page_size)

    def _parse_content_data(raw: str) -> List[Dict[str, Any]]:
        try:
            return json5.loads(raw) if raw else []
        except Exception:
            return []

    def _safe_ref_msg_id(msg: DBChatMessage) -> str:
        try:
            return msg.ext_data_obj.ref_msg_id or ""
        except (AttributeError, KeyError, ValueError) as e:
            logger.debug(f"Failed to parse ref_msg_id for msg {msg.id}: {e}")
            return ""

    items: List[ChatMessage] = []
    for msg in messages:
        try:
            items.append(
                ChatMessage(
                    id=msg.id,
                    sender_id=str(msg.sender_id),
                    sender_name=msg.sender_name,
                    sender_nickname=msg.sender_nickname or msg.sender_name,
                    platform_userid=msg.platform_userid or "",
                    content=msg.content_text,
                    content_data=_parse_content_data(msg.content_data),
                    chat_key=msg.chat_key,
                    create_time=msg.create_time.strftime("%Y-%m-%d %H:%M:%S"),
                    message_id=getattr(msg, "message_id", "") or "",
                    ref_msg_id=_safe_ref_msg_id(msg),
                )
            )
        except Exception:
            logger.warning(f"构建消息响应失败, msg_id={msg.id}, 跳过")

    return ChatMessageListResponse(total=total, items=items)


class MessageVideoAccess(BaseModel):
    """视频预览的探测结果

    `status` 是 `ok` 或 `VideoUnavailableReason` 的取值。这些都是业务状态而不是请求错误，走 200，
    前端按它选文案：`<video>` 的 `onError` 读不到响应体，只能靠这条接口说清楚为什么放不了。
    """

    status: str
    url: str = ""
    expires_at: int = 0
    file_name: str = ""
    file_size: int = 0


def _parse_video_message_id(message_id: str) -> int:
    parsed = parse_message_id(message_id)
    if parsed is None:
        raise NotFoundError(resource="视频")
    return parsed


async def _video_channel_or_404(chat_key: str) -> DBChatChannel:
    """只有 OneBot 的占位视频走这条路，别的适配器的视频段是落盘文件，走上传目录"""
    channel = await DBChatChannel.filter(chat_key=chat_key, adapter_key=ONEBOT_ADAPTER_KEY).first()
    if channel is None:
        raise NotFoundError(resource="视频")
    return channel


async def _open_channel_video(chat_key: str, record: VideoRecord, range_header: Optional[str]) -> VideoStream:
    try:
        bot = await get_bot_by_chat_key(chat_key)
    except BotInstanceOfflineError as e:
        raise OnebotVideoError(VideoUnavailableReason.OFFLINE, "协议端不在线") from e
    return await open_message_video(bot, record, chat_key=chat_key, range_header=range_header)


# 探测只要一个字节；判文件头需要的那几个字节由 `open_video_stream` 自己向上游多要
VIDEO_PROBE_RANGE = "bytes=0-0"


def _tenant_slot_key(user: DBUser) -> Optional[str]:
    """转发名额按租户分桶的键；平台方（`tenant_id` 为空）只受每用户与全局上限约束"""
    return None if user.tenant_id is None else str(user.tenant_id)


@router.get("/{chat_key}/messages/{message_id}/video-access", summary="探测消息视频并签发播放地址")
@require_role(Role.Admin)
async def get_message_video_access(
    chat_key: str,
    message_id: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> MessageVideoAccess:
    """这是视频预览唯一的授权点：能拿到地址 == 能看这个频道

    真的向上游要一个字节再签发，这样过期、离线、撤回都能在这里说清楚，而不是让 `<video>` 报一个读不到原因的错。
    探测同样要协议端 `get_msg`、连上游，所以和转发共用名额；没名额时回 `unavailable`（稍后可重试）。
    """
    await assert_channel_accessible(_current_user, chat_key)
    await _video_channel_or_404(chat_key)
    numeric_id = _parse_video_message_id(message_id)
    release = onebot_video.video_stream_slots.try_acquire(str(_current_user.id), _tenant_slot_key(_current_user))
    if release is None:
        logger.info(f"视频转发名额已满，探测被拒: user_id={_current_user.id}")
        return MessageVideoAccess(status=VideoUnavailableReason.UNAVAILABLE.value)
    try:
        try:
            record = await find_video_record(chat_key, numeric_id)
            stream = await _open_channel_video(chat_key, record, VIDEO_PROBE_RANGE)
        except OnebotVideoError as e:
            if e.reason is VideoUnavailableReason.NOT_FOUND:
                raise NotFoundError(resource="视频") from e
            logger.info(f"视频不可预览: chat_key={chat_key} message_id={numeric_id}: {e}")
            return MessageVideoAccess(status=e.reason.value)
        await stream.close()
    finally:
        release()
    token = issue_video_access_token(
        chat_key, str(numeric_id), int(_current_user.id), generation=expected_pwd_gen_claim(_current_user),
    )
    return MessageVideoAccess(
        status="ok",
        url=build_video_url(chat_key, str(numeric_id), token),
        expires_at=video_token_expiry(token),
        file_name=record.video_name,
        file_size=record.file_size,
    )


@router.get("/{chat_key}/messages/{message_id}/video", summary="转发消息里的视频（凭签名）")
async def stream_message_video(
    chat_key: str,
    message_id: str,
    request: Request,
    sig: str = Query(default="", description="取件签名，见 tools/video_access.py"),
) -> Response:
    """供 `<video src>` 直接访问

    每次请求都现取链接，不落盘，不缓存。签名错、过期、越权都是同一个 404，不泄露频道与消息是否存在。

    签名里的用户此刻还得是一个有权看的人。签发要过 `require_role(Role.Admin)` 与
    `get_current_active_user`（含口令世代），这里逐项再过一遍，令牌有效期内的改密、降级、封禁、
    移出租户都立刻生效：先验不需要查库的那段签名，再按用户号取行、用它当前的口令世代验第二段签名；
    再看未封禁、至少 Admin、能访问这个频道。

    名额在碰协议端之前拿，满了回 429；名额与上游连接由 `VideoStreamResponse` 在任何结束方式下归还。
    """
    # 第一段签名不查库：伪造的令牌在这里就挡掉，匿名请求碰不到数据库
    user_id = verify_video_access_token(sig, chat_key, message_id)
    user = await DBUser.get_or_none(id=user_id) if user_id is not None else None
    if (
        user is None
        or not video_token_matches_generation(sig, chat_key, message_id, generation=expected_pwd_gen_claim(user))
        or not user.is_active
        or not check_role(user, Role.Admin)
    ):
        raise NotFoundError(resource="视频")
    await assert_channel_accessible(user, chat_key)
    await _video_channel_or_404(chat_key)
    numeric_id = _parse_video_message_id(message_id)
    release = onebot_video.video_stream_slots.try_acquire(str(user.id), _tenant_slot_key(user))
    if release is None:
        logger.info(f"视频转发名额已满，拒绝: user_id={user.id}")
        raise MessageVideoBusyError()
    try:
        record = await find_video_record(chat_key, numeric_id)
        stream = await _open_channel_video(chat_key, record, request.headers.get("range"))
    except OnebotVideoError as e:
        release()
        if e.reason is VideoUnavailableReason.NOT_FOUND:
            raise NotFoundError(resource="视频") from e
        logger.info(f"视频转发失败: chat_key={chat_key} message_id={numeric_id}: {e}")
        if e.reason in (VideoUnavailableReason.RECALLED, VideoUnavailableReason.EXPIRED):
            raise MessageVideoGoneError() from e
        raise MessageVideoUnavailableError() from e
    except BaseException:
        release()
        raise

    headers = {**PROXY_SECURITY_HEADERS, **stream.headers}
    if stream.body is None:
        release()
        await stream.close()
        return Response(status_code=stream.status_code, headers=headers)
    return VideoStreamResponse(stream, headers=headers, on_close=release)


class PresetSwitchImpactResponse(BaseModel):
    """换人设的影响面

    **人设就是不同的人**：隔离键里有人设这一维，换掉它等于把整个频道切到另一份好感
    与记忆上。运营看到的是"全群好感当场归零"，而实际是切到了另一份、旧的那份还在。
    没有这段说明，那一刻会被当成数据丢失——这是本轮唯一一处"不做就必然变成工单"的配套。
    """

    scope_label: str
    domain_id: str
    current_preset_id: int
    current_preset_name: str
    target_preset_id: int
    target_preset_name: str
    is_noop: bool
    affected_people: int
    returning_people: int
    affected_channels: int
    domain_memory_paragraphs: int
    memory_scope_note: str
    # 旧数据保留、换回来就回来。措辞跟着响应走，前端不各写一份
    reversible_note: str


async def _preset_switch_impact_of(
    channel: DBChatChannel,
    target_preset_id: Optional[int],
) -> PresetSwitchImpactResponse:
    """算出"这次换人设会影响多少人"

    当前人设取 `get_effective_preset_id()`（频道 → 实例默认 → 系统默认 → 内置那条回退链），
    **不是** `channel.preset_id`：隔离键用的就是前者，只读后者会让"把实例默认人设显式绑到
    频道上"这个纯运维动作看起来像一次换人设。目标人设同理要走一次同样的解析——
    传 `None`（改回默认）之后生效的可能仍然是同一个人设，那种情况必须显示成"无影响"。
    """
    current_preset_id = await channel.get_effective_preset_id()
    # `-1` 与 `None` 在 `set_preset` 里是同一件事（"改回默认"），预览必须用同一条判据，
    # 否则前端传 -1 时预览算出的目标人设与真正落库后的不是同一个。
    wanted = None if target_preset_id in (None, -1) else target_preset_id
    probe = await DBChatChannel.filter(chat_key=channel.chat_key).first()
    target_preset_id_resolved = current_preset_id
    if probe is not None:
        # 只在内存里改一份副本再解析，绝不落库：这是预览，不能有副作用
        probe.preset_id = wanted  # type: ignore[assignment]  # 模型允许 null
        target_preset_id_resolved = await probe.get_effective_preset_id()

    impact = await preview_preset_switch(
        adapter_key=channel.adapter_key,
        instance_key=channel.instance_key,
        scope_label=channel.channel_name or channel.chat_key,
        current_preset_id=current_preset_id,
        target_preset_id=target_preset_id_resolved,
        affected_channels=1,
    )
    return PresetSwitchImpactResponse(
        scope_label=impact.scope_label,
        domain_id=impact.domain_id,
        current_preset_id=impact.current_preset_id,
        current_preset_name=impact.current_preset_name,
        target_preset_id=impact.target_preset_id,
        target_preset_name=impact.target_preset_name,
        is_noop=impact.is_noop,
        affected_people=impact.affected_people,
        returning_people=impact.returning_people,
        affected_channels=impact.affected_channels,
        domain_memory_paragraphs=impact.domain_memory_paragraphs,
        memory_scope_note=impact.memory_scope_note,
        reversible_note=PRESET_SWITCH_REVERSIBLE_NOTE,
    )


@router.get("/{chat_key}/preset-impact", summary="预览换人设的影响面")
@require_role(Role.Admin)
async def get_chat_channel_preset_impact(
    chat_key: str,
    preset_id: Optional[int] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> PresetSwitchImpactResponse:
    """二次确认弹窗要显示的那段内容：本次将影响 N 人，旧数据保留、换回来就回来"""
    await assert_channel_accessible(_current_user, chat_key)
    channel = await DBChatChannel.filter(chat_key=chat_key).first()
    if not channel:
        raise NotFoundError(resource="聊天频道")
    return await _preset_switch_impact_of(channel, preset_id)


@router.post("/{chat_key}/preset", summary="设置聊天频道人设")
@require_role(Role.Admin)
async def set_chat_channel_preset(
    chat_key: str,
    preset_id: Optional[int] = None,
    confirmed: bool = False,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """设置聊天频道人设，传入 preset_id=None 则使用默认人设

    **真的换了人设时必须带 `confirmed=true`**，否则 400 并把影响面写进错误原因里。
    这道闸不是给前端用的（前端自己会先拉 `/preset-impact` 弹确认框），是给**别的调用方**
    用的：这个接口在 curl、脚本、以后新加的页面里同样可达，而"换人设 = 全群好感归零"
    这件事必须在每一条路径上都被看见一次。

    生效人设没变时不拦——那不是换人设（把实例默认人设显式绑到频道上就是这种情况）。

    **人设对本实例不可见时按 400 回，不让它变成 500。** 判据在模型层
    （`DBChatChannel.set_preset` 按 `DBPreset.owner_instance_key` 抛 `ValueError`），
    这里只负责把它翻译成契约里的形状——在路由层复算一遍可见性就是"同一个判据的第二处实现"，
    正是本项目反复栽跟头的那个形态。不翻译的代价是实打实的：确认弹窗刚说完"本次影响 N 人"，
    下一个请求就回一个没有原因的 500，同时往日志里写一条 ERROR + 完整调用栈
    （"启动/运行窗口 0 ERROR"这条验收由此变得不可用）。
    """
    await assert_channel_accessible(_current_user, chat_key)

    channel = await DBChatChannel.filter(chat_key=chat_key).first()
    if not channel:
        raise NotFoundError(resource="聊天频道")

    impact = await _preset_switch_impact_of(channel, preset_id)
    if not impact.is_noop and not confirmed:
        raise ValidationError(
            reason=(
                f"换人设会把本频道切到另一份好感与记忆：当前「{impact.current_preset_name}」"
                f"下有 {impact.affected_people} 人的好感记录，切到「{impact.target_preset_name}」后"
                f"这些人在本频道看起来会归零（目标人设下已有 {impact.returning_people} 人的旧记录）。"
                f"{PRESET_SWITCH_REVERSIBLE_NOTE} 确认后请带 confirmed=true 重试。"
            ),
        )

    try:
        await channel.set_preset(preset_id)
    except ValueError as error:
        raise ValidationError(reason=str(error)) from error
    if not impact.is_noop:
        logger.warning(
            f"频道换人设: chat_key={chat_key}, {impact.current_preset_id} -> {impact.target_preset_id}, "
            f"affected_people={impact.affected_people}, operator={_current_user.username}",
        )
    return ActionResponse(ok=True)


class ChatChannelUser(BaseModel):
    """聊天频道用户"""

    platform_userid: str
    nickname: str
    # 此人在**本频道**执行聊天命令时的档位：user / advanced / super_user
    command_permission: str = CommandPermission.USER.value
    # 档位的来源：channel（本频道授权）/ group_role（群角色，注册时的快照；执行命令时按实时群角色）；
    # 平台方调用者额外分得出 super_users（平台方名单）与 global（平台方的全局授权），
    # 租户调用者看到的这两类一律是 platform。档位本身照实给租户：这些人确实能在租户的频道里
    # 执行对应的命令，租户该知道谁能操作自己的机器人；隐去的只是「平台方用的是名单还是授权」
    command_permission_source: Optional[str] = None
    # 是否有本频道授权（与来源分开给：超级用户名下也可能挂着一条本频道授权，界面要能撤掉它）
    channel_granted: bool = False


class ChatChannelUsersResponse(BaseModel):
    """聊天频道用户列表"""

    total: int
    items: List[ChatChannelUser]


def _channel_command_permission(
    channel: DBChatChannel,
    platform_userid: str,
    super_users: Set[str],
    channel_grants: Dict[str, UserPermissionRecord],
    *,
    group_role_enabled: bool,
) -> Tuple[CommandPermission, Optional[str]]:
    """某人在这个频道里的命令档位与来源，判据与 `collector._resolve_user_command_flags` 一致

    平台来源的全局记录只在本频道开了群角色识别、且 `channel_id` 等于本频道时才算：
    它是群角色的快照，别的群里的角色不该出现在这里，更不该出现在别的租户的频道里。
    """
    if platform_userid in super_users:
        return CommandPermission.SUPER_USER, "super_users"
    if platform_userid in channel_grants:
        return CommandPermission.ADVANCED, "channel"
    record = command_manager.get_user_permission_record(channel.adapter_key, platform_userid)
    if record.permission in (CommandPermission.ADVANCED, CommandPermission.SUPER_USER):
        if record.source == USER_PERMISSION_SOURCE_MANUAL:
            return CommandPermission.ADVANCED, "global"
        if group_role_enabled and record.channel_id == channel.channel_id:
            return CommandPermission.ADVANCED, "group_role"
    return CommandPermission.USER, None


# 租户调用者看不到平台方内部的区分：「在平台方名单里」与「平台方全局授权」一律显示成平台方设置。
# 档位不隐去（超级用户 / 频道管理员照实显示），理由见 `ChatChannelUser.command_permission_source`
_TENANT_VISIBLE_SOURCE = {"super_users": "platform", "global": "platform"}


@router.get("/{chat_key}/users", summary="获取聊天频道用户列表")
@require_role(Role.Admin)
async def get_chat_channel_users(
    chat_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ChatChannelUsersResponse:
    """获取聊天频道内的所有用户（按昵称）

    作用域一直有（`assert_channel_accessible`），缺的是角色门槛：
    没有 `@require_role` 时任何登录账号（含只读的租户普通用户）都能读出群成员昵称，
    与同族其它频道接口的 `Role.Admin` 不一致。
    """
    await assert_channel_accessible(_current_user, chat_key)

    channel = await DBChatChannel.filter(chat_key=chat_key).first()
    if not channel:
        raise NotFoundError(resource="聊天频道")

    # 从消息表查询该频道的所有独特用户
    messages = await DBChatMessage.filter(chat_key=chat_key).distinct().values_list('platform_userid', 'sender_nickname')

    # 去重并排序
    users_dict: Dict[str, str] = {}
    for userid, nickname in messages:
        if userid and userid != '-1' and nickname and nickname != 'SYSTEM':
            users_dict[userid] = nickname

    # 本频道授权过、但还没在这里说过话的人也列出来：否则这份授权在界面上看不见、也撤不掉
    channel_grants = command_manager.list_channel_user_permissions(chat_key, channel.adapter_key)
    for userid in channel_grants:
        users_dict.setdefault(userid, "")

    # 与聊天命令同一套判定（名单匹配 ∧ 来源可信）：名单里的号码在这个频道是不是超级用户，
    # 取决于这个频道的适配器与实例；来源只判一次，名单匹配逐人做。
    # 这里没有事件、也就没有连接，只判实例这一侧（连接这一侧只在真正执行命令时才判得了）
    source_trusted = await is_super_user_source_trusted_for_display(channel.adapter_key, channel.instance_key)
    super_users = (
        {uid for uid in users_dict if matches_super_users(channel.adapter_key, uid)} if source_trusted else set()
    )
    group_role_enabled = await is_group_admin_detection_enabled(chat_key)
    is_tenant_caller = get_tenant_scope(_current_user) is not None
    items: List[ChatChannelUser] = []
    # 按昵称排序
    for uid, nickname in sorted(users_dict.items(), key=lambda x: x[1]):
        permission, source = _channel_command_permission(
            channel, uid, super_users, channel_grants, group_role_enabled=group_role_enabled,
        )
        if is_tenant_caller and source is not None:
            source = _TENANT_VISIBLE_SOURCE.get(source, source)
        items.append(
            ChatChannelUser(
                platform_userid=uid,
                nickname=nickname,
                command_permission=permission.value,
                command_permission_source=source,
                channel_granted=uid in channel_grants,
            ),
        )

    return ChatChannelUsersResponse(total=len(items), items=items)


class SetChannelUserCommandPermissionRequest(BaseModel):
    # 只有这两档：频道管理员（授予）与普通用户（撤销）。超级用户只认平台方的 SUPER_USERS
    permission: Literal["advanced", "user"]


@router.put(
    "/{chat_key}/users/{platform_userid}/command-permission",
    summary="设置用户在本频道的命令权限",
)
@require_role(Role.Admin)
async def set_chat_channel_user_command_permission(
    chat_key: str,
    platform_userid: str,
    body: SetChannelUserCommandPermissionRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """授予或撤销某人在**这一个频道**里的频道管理员档位

    授权只在这个 `chat_key` 上生效，换到任何别的频道都不存在，所以租户在这里改不到别的租户。
    别人的频道与不存在的频道一律 404（`assert_channel_accessible`），与「频道不存在」不可区分；
    租户普通成员被 `Role.Admin` 挡在外面。平台方的全局授权与 `SUPER_USERS` 不在这里改。
    """
    await assert_channel_accessible(_current_user, chat_key)

    channel = await DBChatChannel.filter(chat_key=chat_key).first()
    if channel is None:
        raise NotFoundError(resource="聊天频道")

    target_userid = platform_userid.strip()
    if not target_userid or len(target_userid) > 128:
        raise ValidationError(reason="平台用户 ID 不能为空，且不超过 128 个字符")

    await command_manager.set_channel_user_permission(
        chat_key,
        channel.adapter_key,
        target_userid,
        CommandPermission(body.permission),
    )
    logger.info(
        f"频道用户命令授权: chat_key={chat_key}, platform_userid={target_userid}, "
        f"permission={body.permission}, operator={_current_user.username}",
    )
    return ActionResponse(ok=True)


class PokeRequest(BaseModel):
    target_user_id: str


@router.post("/{chat_key}/poke", summary="发送戳一戳")
@require_role(Role.Admin)
async def send_poke(
    chat_key: str,
    req: PokeRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """双击头像触发戳一戳"""
    await assert_channel_accessible(_current_user, chat_key)

    channel = await DBChatChannel.filter(chat_key=chat_key).first()
    if not channel:
        raise NotFoundError(resource="聊天频道")

    try:
        adapter = get_adapter(channel.adapter_key)
    except AdapterUnavailableError:
        return ActionResponse(ok=False)

    if not hasattr(adapter, "send_poke"):
        return ActionResponse(ok=False)

    ok = await adapter.send_poke(chat_key, req.target_user_id)
    return ActionResponse(ok=ok)


class ChatPluginDataItem(BaseModel):
    id: int
    plugin_key: str
    data_key: str
    data_value: str
    target_user_id: str
    create_time: str
    update_time: str


class ChatPluginDataResponse(BaseModel):
    total: int
    items: List[ChatPluginDataItem]
    plugin_keys: List[str]
    plugin_names: Dict[str, str]


class UpdatePluginDataRequest(BaseModel):
    data_value: str


@router.get("/{chat_key}/plugin-data", summary="获取聊天频道的插件数据")
@require_role(Role.Admin)
async def get_chat_plugin_data(
    chat_key: str,
    plugin_key: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ChatPluginDataResponse:
    """获取指定聊天频道下所有插件存储的数据"""
    from nekro_agent.models.db_plugin_data import DBPluginData

    await assert_channel_accessible(_current_user, chat_key)

    query = DBPluginData.filter(target_chat_key=chat_key)
    if plugin_key:
        query = query.filter(plugin_key=plugin_key)

    total = await query.count()
    items = await query.order_by("-update_time").offset((page - 1) * page_size).limit(page_size)

    # 获取该频道涉及的所有插件 key（用于前端筛选下拉）
    all_keys = await DBPluginData.filter(target_chat_key=chat_key).distinct().values_list("plugin_key", flat=True)

    # 构建 plugin_key → 插件显示名称的映射
    from nekro_agent.services.plugin.collector import plugin_collector

    plugin_names: Dict[str, str] = {}
    for pk in all_keys:
        plugin = plugin_collector.get_plugin(pk)
        plugin_names[pk] = plugin.name if plugin else pk

    return ChatPluginDataResponse(
        total=total,
        items=[
            ChatPluginDataItem(
                id=item.id,
                plugin_key=item.plugin_key,
                data_key=item.data_key,
                data_value=item.data_value,
                target_user_id=item.target_user_id,
                create_time=item.create_time.strftime("%Y-%m-%d %H:%M:%S"),
                update_time=item.update_time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            for item in items
        ],
        plugin_keys=list(all_keys),
        plugin_names=plugin_names,
    )


@router.put("/{chat_key}/plugin-data/{data_id}", summary="修改插件数据")
@require_role(Role.Admin)
async def update_chat_plugin_data(
    chat_key: str,
    data_id: int,
    body: UpdatePluginDataRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """修改指定聊天频道下的某条插件数据"""
    from nekro_agent.models.db_plugin_data import DBPluginData

    await assert_channel_accessible(_current_user, chat_key)

    data = await DBPluginData.filter(id=data_id, target_chat_key=chat_key).first()
    if not data:
        raise NotFoundError(resource="插件数据")
    data.data_value = body.data_value
    await data.save(update_fields=["data_value"])
    return ActionResponse(ok=True)


@router.delete("/{chat_key}/plugin-data/{data_id}", summary="删除插件数据")
@require_role(Role.Admin)
async def delete_chat_plugin_data(
    chat_key: str,
    data_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """删除指定聊天频道下的某条插件数据"""
    from nekro_agent.models.db_plugin_data import DBPluginData

    await assert_channel_accessible(_current_user, chat_key)

    data = await DBPluginData.filter(id=data_id, target_chat_key=chat_key).first()
    if not data:
        raise NotFoundError(resource="插件数据")
    await data.delete()
    return ActionResponse(ok=True)


class SendMessageRequest(BaseModel):
    message: str


class SendMessageResponse(BaseModel):
    ok: bool = True
    error: str = ""


class AnnouncementSendRequest(BaseModel):
    chat_keys: List[str]
    message: str


class AnnouncementSendResultItem(BaseModel):
    chat_key: str
    channel_name: Optional[str]
    ok: bool = True
    error: str = ""


class AnnouncementSendResponse(BaseModel):
    ok: bool = True
    total: int
    success_count: int
    failure_count: int
    results: List[AnnouncementSendResultItem]


async def _send_message_via_webui(
    channel: DBChatChannel,
    message: str,
    file: Optional[UploadFile] = None,
    sender_type: str = "bot",
) -> SendMessageResponse:
    """复用 WebUI 消息发送逻辑，供单发与批量发送共用。"""
    chat_key = channel.chat_key

    if sender_type not in {"bot", "system", "none"}:
        return SendMessageResponse(ok=False, error="不支持的发送身份")

    # system 类型不需要适配器转发，直接写入数据库
    if sender_type == "system":
        text = message.strip()
        if not text:
            return SendMessageResponse(ok=False, error="SYSTEM 消息内容不能为空")
        try:
            await message_service.push_system_message(
                chat_key=chat_key,
                agent_messages=text,
                trigger_agent=True,
                db_chat_channel=channel,
            )
            return SendMessageResponse(ok=True)
        except Exception as e:
            logger.error(f"WebUI 发送 SYSTEM 消息到 {chat_key} 失败: {e}")
            return SendMessageResponse(ok=False, error=str(e))

    # bot / none 类型需要适配器转发
    try:
        adapter = get_adapter(channel.adapter_key)
        if not adapter.supports_webui_send:
            return SendMessageResponse(ok=False, error="当前适配器不支持从 WebUI 发送消息")
    except AdapterUnavailableError as e:
        return SendMessageResponse(ok=False, error=str(e))

    text = message.strip()

    # none 类型：添加命令输出前缀，确保不进入上下文
    if sender_type == "none" and text:
        effective_config = await config_resolver.get_effective_config(chat_key)
        text = f"{effective_config.AI_COMMAND_OUTPUT_PREFIX}{text}"

    if not text and not file:
        return SendMessageResponse(ok=False, error="消息内容不能为空")

    try:
        from nekro_agent.services.chat.universal_chat_service import universal_chat_service

        segments: list[AgentMessageSegment] = []

        if text:
            segments.append(AgentMessageSegment(type=AgentMessageSegmentType.TEXT, content=text))

        is_file_mode = False
        if file and file.filename:
            safe_chat_key = sanitize_chat_key_for_path(Path(chat_key).name)
            safe_filename = Path(file.filename).name
            upload_dir = Path(USER_UPLOAD_DIR) / safe_chat_key
            upload_dir.mkdir(parents=True, exist_ok=True)
            save_path = upload_dir / safe_filename
            max_upload_size = 100 * 1024 * 1024
            total_size = 0
            with save_path.open("wb") as f:
                while chunk := await file.read(1024 * 1024):
                    total_size += len(chunk)
                    if total_size > max_upload_size:
                        save_path.unlink(missing_ok=True)
                        return SendMessageResponse(ok=False, error="文件大小超过 100MB 限制")
                    f.write(chunk)
            segments.append(AgentMessageSegment(type=AgentMessageSegmentType.FILE, content=f"/app/uploads/{safe_filename}"))
            is_file_mode = not (file.content_type or "").startswith("image/")

        await universal_chat_service.send_agent_message(
            chat_key=chat_key,
            messages=segments,
            adapter=adapter,
            record=sender_type != "none",
            file_mode=is_file_mode,
        )
        return SendMessageResponse(ok=True)
    except Exception as e:
        logger.error(f"WebUI 发送消息到 {chat_key} 失败: {e}")
        return SendMessageResponse(ok=False, error=str(e))


class AnnouncementSubmitRequest(BaseModel):
    """提交公告任务"""

    content: str
    scope: AnnouncementScope


class AnnouncementScopeRequest(BaseModel):
    """公告范围预览请求"""

    scope: AnnouncementScope


class AnnouncementInstanceOptionsResponse(BaseModel):
    """可选实例列表"""

    items: List[AnnouncementInstanceOption]


async def _announcement_instance_refs(
    announcements: Sequence[DBAnnouncement],
) -> Dict[int, Optional[Set[InstanceRef]]]:
    """算出每条公告触达了哪些适配器实例，作为它的归属判据

    Returns:
        公告 ID -> `(adapter_key, instance_key)` 集合；`None` 表示这条公告的覆盖面**收不敛到具体实例**。
        成对给出是因为只有标识的话，另一个适配器下归别的租户的同名实例会被当成同一个。

    公告表没有租户列，也不可能有：历史行是在多租户之前写下的，加一列也只会全是 NULL。
    所以归属只能从"它打到了谁的实例上"反推，与工作区按引用它的频道反推归属是同一条路子。

    两个来源取并集，缺一不可：申报范围（`scope_value`）在任务还没开始投递时是唯一线索；
    投递明细则在目标频道事后被删掉后仍留着当时的 `instance_key`，
    否则租户删掉一个群就会把自己的公告历史一起弄丢。

    `scope_type=all` 恒为 `None`：它的语义是"当前及以后的全部实例"，那是平台方的意图，
    不能因为此刻恰好只有某个租户的实例就判给它。判不出归属就不给任何租户，
    与工作区归属不唯一时判平台方专属是同一条理由。

    只有 `instances` / `channels` 两种范围能收敛到具体实例，因此判据写成**白名单**而不是
    "排除 all"：`scope_type` 落到枚举之外时（版本回退后读到新版本写下的范围类型，或手改过
    数据库）同样判平台方专属。写成黑名单的话这一行会掉进 `load_scope`，
    而那里的 `AnnouncementScopeType(...)` 会抛 `ValueError` —— 租户一侧是对**全表**逐行调用的，
    一行脏数据就会让租户的每一页历史都 500，而平台方只有正好落在那一页时才受影响。
    """
    instances: Dict[int, Set[InstanceRef]] = {}
    chat_keys_of: Dict[int, List[str]] = {}
    unattributable: Set[int] = set()
    attributable_scope_types = {AnnouncementScopeType.INSTANCES.value, AnnouncementScopeType.CHANNELS.value}

    for announcement in announcements:
        if announcement.scope_type not in attributable_scope_types:
            unattributable.add(announcement.id)
            continue
        scope = announcement_service.load_scope(announcement)
        instances[announcement.id] = {(ref.adapter_key, ref.instance_key) for ref in scope.instances if ref.instance_key}
        chat_keys_of[announcement.id] = list(scope.chat_keys)

    wanted_chat_keys = {chat_key for chat_keys in chat_keys_of.values() for chat_key in chat_keys}
    if wanted_chat_keys:
        channel_instances: Dict[str, InstanceRef] = {
            chat_key: (adapter_key, instance_key)
            for chat_key, adapter_key, instance_key in await DBChatChannel.filter(
                chat_key__in=wanted_chat_keys,
            ).values_list("chat_key", "adapter_key", "instance_key")
            if instance_key
        }
        for announcement_id, chat_keys in chat_keys_of.items():
            instances[announcement_id].update(
                channel_instances[chat_key] for chat_key in chat_keys if chat_key in channel_instances
            )

    if instances:
        delivered = await DBAnnouncementDelivery.filter(announcement_id__in=list(instances)).values_list(
            "announcement_id",
            "adapter_key",
            "instance_key",
        )
        for announcement_id, adapter_key, instance_key in delivered:
            if instance_key and announcement_id in instances:
                instances[announcement_id].add((adapter_key, instance_key))

    return {
        announcement.id: None if announcement.id in unattributable else instances[announcement.id]
        for announcement in announcements
    }


async def _accessible_announcements(user: DBUser, announcements: Sequence[DBAnnouncement]) -> List[DBAnnouncement]:
    """筛出当前身份看得到的公告

    租户只看得到"全部目标实例都在自己名下"的公告。部分重叠一律不给：
    一条同时打到两个租户的公告属于平台方，露给任一方都等于替另一方做决定。
    """
    if get_tenant_scope(user) is None:
        return list(announcements)
    allowed = await accessible_instance_refs(user)
    reached = await _announcement_instance_refs(announcements)
    return [announcement for announcement in announcements if (refs := reached[announcement.id]) and refs <= allowed]


async def _accessible_announcement_or_404(user: DBUser, announcement_id: int) -> DBAnnouncement:
    """取一条当前身份够得到的公告

    不存在与越权同为 404，理由与频道守卫一致：403 会把别的租户的公告 ID 变成可枚举的。
    """
    announcement = await DBAnnouncement.get_or_none(id=announcement_id)
    if not announcement:
        raise NotFoundError(resource="公告任务")
    if get_tenant_scope(user) is not None and not await _accessible_announcements(user, [announcement]):
        raise NotFoundError(resource="公告任务")
    return announcement


async def _scope_within_reach(user: DBUser, scope: AnnouncementScope) -> AnnouncementScope:
    """把发送范围收敛到当前身份真正够得到的实例

    公告是唯一"能往群里发言"的批量写入面，所以范围必须在**进入服务层之前**就收紧，
    而不是靠服务层事后过滤 —— `announcement_service` 是按 (适配器, 实例) 分组并发投递的，
    它拿到什么就发什么。

    平台方原样返回，未启用多租户的部署一个分支都不进。租户身份下：

    - `channels`：逐个 `chat_key` 过 `assert_channel_accessible`，越权与不存在同为 404；
    - `instances`：逐个过 `assert_instance_accessible`，同样 404；
    - `all`：**改写**成"本租户当前全部可选实例"的显式清单。不改写不行 —— `all` 落库后
      `scope_value` 是空数组，事后无从判断这条公告当初覆盖了谁，历史与取消接口就再也
      判不出归属。改写的代价是历史里显示成"指定实例"，那反而更贴近实际发生的事。
    """
    if get_tenant_scope(user) is None:
        return scope

    if scope.scope_type == AnnouncementScopeType.CHANNELS:
        for chat_key in scope.chat_keys:
            await assert_channel_accessible(user, chat_key)
        return scope

    if scope.scope_type == AnnouncementScopeType.INSTANCES:
        for ref in scope.instances:
            # 投递按 (适配器, 实例) 分组，所以校验也必须落在同一对上：只校验标识的话，
            # 租户拿自己实例的标识配上另一个适配器，就能把公告发进别人同名实例的群里
            await assert_instance_accessible(user, ref.instance_key, adapter_key=ref.adapter_key)
        return scope

    allowed = await accessible_instance_refs(user)
    return AnnouncementScope(
        scope_type=AnnouncementScopeType.INSTANCES,
        instances=[
            AnnouncementInstanceRef(adapter_key=option.adapter_key, instance_key=option.instance_key)
            for option in await announcement_service.list_instance_options()
            if (option.adapter_key, option.instance_key) in allowed
        ],
    )


@router.post("/announcement", summary="提交机器人公告任务")
@require_role(Role.Admin)
async def submit_bot_announcement(
    body: AnnouncementSubmitRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> AnnouncementSubmitResponse:
    """提交公告群发任务，立即返回任务 ID，实际投递在服务端后台进行。

    投递过程不依赖浏览器保持在线，进度通过 `/announcement/{id}/events` 订阅。
    租户只能对自己名下实例发送，范围在提交前就已收敛（见 `_scope_within_reach`）。
    """
    content = body.content.strip()
    if not content:
        raise ValidationError(reason="公告内容不能为空")

    scope = await _scope_within_reach(_current_user, body.scope)
    preview = await announcement_service.build_preview(scope)
    if preview.channel_count == 0:
        raise ValidationError(reason="当前范围没有可发送的会话")
    if preview.exceeds_limit:
        raise ValidationError(reason=f"单次公告最多覆盖 {preview.max_channels} 个会话，请缩小发送范围")

    announcement = await announcement_service.submit(
        content=content,
        scope=scope,
        created_by=_current_user.username,
    )
    return AnnouncementSubmitResponse(
        announcement_id=announcement.id,
        total=announcement.total,
        status=AnnouncementStatus(announcement.status),
    )


@router.post("/announcement/preview", summary="预览公告影响范围")
@require_role(Role.Admin)
async def preview_bot_announcement(
    body: AnnouncementScopeRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> AnnouncementPreview:
    """群发属于账号风控高危操作，提交前先返回将影响的实例数与会话数。

    范围与提交走同一条收敛（`_scope_within_reach`），否则预览会把别的租户的会话数报出来。
    """
    return await announcement_service.build_preview(await _scope_within_reach(_current_user, body.scope))


@router.get("/announcement/instances", summary="获取公告可选实例列表")
@require_role(Role.Admin)
async def list_announcement_instances(
    _current_user: DBUser = Depends(get_current_active_user),
) -> AnnouncementInstanceOptionsResponse:
    """列出可作为公告范围的适配器实例及其活跃会话数量（租户只看得到自己名下的实例）。"""
    options = await announcement_service.list_instance_options()
    if get_tenant_scope(_current_user) is not None:
        allowed = await accessible_instance_refs(_current_user)
        options = [option for option in options if (option.adapter_key, option.instance_key) in allowed]
    return AnnouncementInstanceOptionsResponse(items=options)


@router.get("/announcement", summary="获取公告历史列表")
@require_role(Role.Admin)
async def list_bot_announcements(
    page: int = 1,
    page_size: int = 20,
    _current_user: DBUser = Depends(get_current_active_user),
) -> AnnouncementListResponse:
    """分页获取历史公告任务（租户只看得到打在自己名下实例上的任务）。

    租户一侧先按归属筛完再分页：归属是从范围与投递明细反推的，落不到 SQL 的 WHERE 里，
    在数据库层分页会让每页数量参差不齐、`total` 也对不上。公告是人工触发的低频操作，
    表规模允许这么做；平台方一侧仍走原来的数据库分页，行为逐字不变。
    """
    if page < 1 or page_size < 1 or page_size > 100:
        raise ValidationError(reason="分页参数无效")

    if get_tenant_scope(_current_user) is None:
        total = await DBAnnouncement.all().count()
        announcements = await DBAnnouncement.all().order_by("-id").offset((page - 1) * page_size).limit(page_size)
    else:
        visible = await _accessible_announcements(_current_user, await DBAnnouncement.all().order_by("-id"))
        total = len(visible)
        announcements = visible[(page - 1) * page_size : page * page_size]

    return AnnouncementListResponse(
        total=total,
        items=[announcement_service.to_summary(announcement) for announcement in announcements],
    )


@router.get("/announcement/{announcement_id}", summary="获取公告任务详情")
@require_role(Role.Admin)
async def get_bot_announcement(
    announcement_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> AnnouncementDetail:
    """获取公告任务详情及逐会话投递明细。"""
    return await announcement_service.build_detail(await _accessible_announcement_or_404(_current_user, announcement_id))


@router.get("/announcement/{announcement_id}/events", summary="订阅公告任务进度")
@require_role(Role.Admin)
async def stream_bot_announcement_events(
    announcement_id: int,
    request: Request,
    _current_user: DBUser = Depends(get_current_active_user),
):
    """订阅公告任务进度流 (SSE)

    连接建立后先推送 `snapshot` 事件还原当前进度，随后推送 `delivery` 与 `status` 增量事件，
    因此断线重连或中途打开页面都能得到完整状态。

    归属必须在建流**之前**判：`EventSourceResponse` 一旦返回状态码就是 200，
    在生成器里再抛 404 只会变成一条建好又断的连接。
    """
    from nekro_agent.services.runtime_state import is_shutting_down

    await _accessible_announcement_or_404(_current_user, announcement_id)
    subscription = announcement_service.subscribe(announcement_id)

    async def handle_client_disconnect(_: object) -> None:
        subscription.close()

    async def event_generator():
        try:
            snapshot = announcement_service.build_snapshot_event(
                await _accessible_announcement_or_404(_current_user, announcement_id),
            )
            yield {"event": snapshot.type, "data": snapshot.model_dump_json()}
            if snapshot.announcement.status in TERMINAL_ANNOUNCEMENT_STATUSES:
                return

            while not is_shutting_down():
                if await request.is_disconnected():
                    return
                try:
                    event = await subscription.get(timeout=1.0)
                except asyncio.TimeoutError:
                    yield {"comment": "ping"}
                    continue

                yield {"event": event.type, "data": event.model_dump_json()}
                if event.type == "status" and event.status in TERMINAL_ANNOUNCEMENT_STATUSES:
                    return
        finally:
            subscription.close()

    return EventSourceResponse(
        event_generator(),
        client_close_handler_callable=handle_client_disconnect,
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/announcement/{announcement_id}/cancel", summary="取消公告任务")
@require_role(Role.Admin)
async def cancel_bot_announcement(
    announcement_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """取消进行中的公告任务，已经发出的消息不会撤回。"""
    announcement = await _accessible_announcement_or_404(_current_user, announcement_id)
    if AnnouncementStatus(announcement.status) in TERMINAL_ANNOUNCEMENT_STATUSES:
        raise ValidationError(reason="公告任务已结束，无法取消")
    if not await announcement_service.request_cancel(announcement_id):
        raise ValidationError(reason="公告任务不在运行中，无法取消")
    return ActionResponse(ok=True)


@router.post("/announcement/send", summary="批量发送机器人公告消息", deprecated=True)
@require_role(Role.Admin)
async def send_bot_announcement(
    body: AnnouncementSendRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> AnnouncementSendResponse:
    """批量向多个聊天频道发送机器人公告（已废弃）。

    该接口同步阻塞直到全部频道发送完毕，频道较多时必然超过前端请求超时，
    且不留历史记录。请改用 `POST /chat-channel/announcement` 提交异步任务，
    并通过 `GET /chat-channel/announcement/{id}/events` 订阅进度。

    目标频道取自 `visible_channels`：对租户来说别的租户的 `chat_key` 与压根不存在的
    `chat_key` 得到完全一样的逐条结果（"聊天频道不存在"），既挡住越权投递，
    又不让这个批量接口变成枚举别人频道的探针。
    """
    chat_keys = list(dict.fromkeys(chat_key.strip() for chat_key in body.chat_keys if chat_key.strip()))
    if not chat_keys:
        raise ValidationError(reason="至少选择一个聊天频道")

    message = body.message.strip()
    if not message:
        raise ValidationError(reason="消息内容不能为空")

    channels = await (await visible_channels(_current_user)).filter(chat_key__in=chat_keys)
    channel_map = {channel.chat_key: channel for channel in channels}
    results: List[AnnouncementSendResultItem] = []

    for index, chat_key in enumerate(chat_keys):
        channel = channel_map.get(chat_key)
        if not channel:
            results.append(
                AnnouncementSendResultItem(
                    chat_key=chat_key,
                    channel_name=None,
                    ok=False,
                    error="聊天频道不存在",
                )
            )
            continue

        if index > 0:
            delay_seconds = random.uniform(1, 5)
            logger.info(f"批量公告等待 {delay_seconds:.2f}s 后发送到 {chat_key}")
            await asyncio.sleep(delay_seconds)

        result = await _send_message_via_webui(
            channel=channel,
            message=message,
            sender_type="none",
        )
        results.append(
            AnnouncementSendResultItem(
                chat_key=chat_key,
                channel_name=channel.channel_name,
                ok=result.ok,
                error=result.error,
            )
        )

    success_count = sum(1 for item in results if item.ok)
    failure_count = len(results) - success_count

    return AnnouncementSendResponse(
        ok=failure_count == 0,
        total=len(results),
        success_count=success_count,
        failure_count=failure_count,
        results=results,
    )


@router.post("/{chat_key}/send", summary="向聊天频道发送消息")
@require_role(Role.Admin)
async def send_message_to_channel(
    chat_key: str,
    message: str = Form(default=""),
    file: Optional[UploadFile] = File(default=None),
    sender_type: str = Form(default="bot"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> SendMessageResponse:
    """从 WebUI 向聊天频道发送消息（支持文本和/或文件）

    sender_type:
        - bot: 以机器人身份发送（默认）
        - system: 以 SYSTEM 身份发送，类似节日祝福触发
        - none: 消息带 ≡NA≡ 前缀，不进入上下文
    """
    await assert_channel_accessible(_current_user, chat_key)

    channel = await DBChatChannel.filter(chat_key=chat_key).first()
    if not channel:
        raise NotFoundError(resource="聊天频道")

    return await _send_message_via_webui(
        channel=channel,
        message=message,
        file=file,
        sender_type=sender_type,
    )


@router.get("/{chat_key}/stream", summary="获取聊天频道消息实时流")
@require_role(Role.Admin)
async def stream_chat_channel_messages(
    chat_key: str,
    request: Request,
    _current_user: DBUser = Depends(get_current_active_user),
):
    """获取聊天频道消息的实时流，使用 Server-Sent Events (SSE)

    Args:
        chat_key: 聊天频道唯一标识

    Returns:
        StreamingResponse: SSE 流，每条新消息作为一个事件

    Raises:
        NotFoundError: 当频道不存在时
    """
    import json

    from nekro_agent.services.message_broadcaster import message_broadcaster
    from nekro_agent.services.runtime_state import is_shutting_down

    await assert_channel_accessible(_current_user, chat_key)

    channel = await DBChatChannel.filter(chat_key=chat_key).first()
    if not channel:
        raise NotFoundError(resource="聊天频道")

    async def event_generator():
        """生成 SSE 事件流"""
        subscription = message_broadcaster.subscribe(chat_key)
        try:
            while not is_shutting_down():
                if await request.is_disconnected():
                    return
                try:
                    message = await subscription.get(timeout=1.0)
                except asyncio.TimeoutError:
                    yield {"comment": "ping"}
                    continue

                # 直接使用广播的消息对象，转换为可序列化的字典格式
                try:
                    content_data = []
                    if message.content_data:
                        if isinstance(message.content_data, str):
                            try:
                                content_data = json5.loads(message.content_data)
                            except Exception:
                                content_data = []
                        elif isinstance(message.content_data, list):
                            for item in message.content_data:
                                if hasattr(item, 'model_dump'):
                                    content_data.append(item.model_dump())
                                elif isinstance(item, dict):
                                    content_data.append(item)
                                else:
                                    content_data.append(str(item))

                    message_dict = {
                        "id": getattr(message, 'message_id', '') or str(hash(message.message_id + str(message.send_timestamp))),
                        "sender_id": str(message.sender_id),
                        "sender_name": message.sender_name,
                        "sender_nickname": message.sender_nickname or message.sender_name,
                        "platform_userid": message.platform_userid or "",
                        "content": message.content_text,
                        "content_data": content_data,
                        "chat_key": message.chat_key,
                        "create_time": "",
                        "message_id": message.message_id or "",
                        "ref_msg_id": getattr(message, "ref_msg_id", "") or "",
                    }

                    yield {"data": json.dumps(message_dict, ensure_ascii=False)}
                except Exception as e:
                    logger.error(f"SSE消息序列化失败: {e}")
                    continue
        finally:
            subscription.close()

    return EventSourceResponse(
        event_generator(),
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{chat_key}/delete-preview", summary="获取频道删除预览")
@require_role(Role.Admin)
async def get_channel_delete_preview(
    chat_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ChannelDeletePreview:
    """获取指定频道的删除预览信息，包含将被删除的各类资源统计"""
    await assert_channel_accessible(_current_user, chat_key)

    channel = await DBChatChannel.filter(chat_key=chat_key).first()
    if not channel:
        raise NotFoundError(resource="聊天频道")

    msg_count, timer_count, plugin_count, para_count, ep_count = await asyncio.gather(
        DBChatMessage.filter(chat_key=chat_key).count(),
        DBRecurringTimerJob.filter(chat_key=chat_key).count(),
        DBPluginData.filter(target_chat_key=chat_key).count(),
        DBMemParagraph.filter(origin_chat_key=chat_key).count(),
        DBMemEpisode.filter(origin_chat_key=chat_key).count(),
    )

    upload_dir = Path(USER_UPLOAD_DIR) / sanitize_chat_key_for_path(chat_key)
    sandbox_dir = Path(SANDBOX_SHARED_HOST_DIR) / f"sandbox_{sanitize_chat_key_for_path(chat_key)}"

    return ChannelDeletePreview(
        message_count=msg_count,
        timer_job_count=timer_count,
        plugin_data_count=plugin_count,
        mem_paragraph_count=para_count,
        mem_episode_count=ep_count,
        upload_dir_exists=upload_dir.exists(),
        sandbox_dir_exists=sandbox_dir.exists(),
    )


@router.delete("/{chat_key}", summary="永久删除频道")
@require_role(Role.Admin)
async def delete_chat_channel(
    chat_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """永久删除指定频道及其所有关联数据，包括消息、定时任务、插件数据及文件目录"""
    await assert_channel_accessible(_current_user, chat_key)

    channel = await DBChatChannel.filter(chat_key=chat_key).first()
    if not channel:
        raise NotFoundError(resource="聊天频道")

    # 1. 立即删除定时任务，防止继续消耗 token
    await DBRecurringTimerJob.filter(chat_key=chat_key).delete()

    # 2. 删除插件数据
    await DBPluginData.filter(target_chat_key=chat_key).delete()

    # 3. 记忆数据保留，仅清除来源频道标识
    await DBMemParagraph.filter(origin_chat_key=chat_key).update(origin_chat_key=None)
    await DBMemEpisode.filter(origin_chat_key=chat_key).update(origin_chat_key=None)

    # 4. 分批删除消息，防止大频道超时
    while await DBChatMessage.filter(chat_key=chat_key).limit(1000).delete():
        pass

    # 5. 删除频道主记录
    await DBChatChannel.filter(chat_key=chat_key).delete()

    # 6. 清理文件系统（容错处理）
    upload_dir = Path(USER_UPLOAD_DIR) / sanitize_chat_key_for_path(chat_key)
    if upload_dir.exists():
        try:
            shutil.rmtree(upload_dir)
        except Exception as e:
            logger.warning(f"清理频道上传目录失败 {upload_dir}: {e}")

    sandbox_dir = Path(SANDBOX_SHARED_HOST_DIR) / f"sandbox_{sanitize_chat_key_for_path(chat_key)}"
    if sandbox_dir.exists():
        try:
            shutil.rmtree(sandbox_dir)
        except Exception as e:
            logger.warning(f"清理频道沙盒目录失败 {sandbox_dir}: {e}")

    # 7. 广播 SSE 删除事件，前端自动更新频道列表
    from nekro_agent.services.channel_broadcaster import channel_broadcaster

    await channel_broadcaster.publish_update(event_type="deleted", chat_key=chat_key)

    return ActionResponse(ok=True)
