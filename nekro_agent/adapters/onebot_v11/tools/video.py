"""OneBot 视频消息：占位段、按消息编号取最新地址、流式转发

`video_analysis` 插件和 WebUI 预览接口共用这里，所以它不依赖插件，也不看插件开关。

## NapCat 的视频段长什么样（v4.18.42 源码）

    {"type": "video", "data": {"file": "<文件名>.mp4", "url": "...", "file_size": "1048576"}}

- `file`：`FileNapCatOneBotUUID.encode()` 的返回值。视频传了 `customUUID=element.fileName`，
  所以它就是文件名，同时是 `get_file` 的取件键，只活在协议端进程内存里，24 小时、最多 5000 条。
- `url`：事件与 `get_msg` 都会现取，优先 `getVideoUrl`，其次 Packet 接口，**都失败时回落到
  `element.filePath`，也就是协议端本机的路径**，不是 http 链接。
- `file_size`：字符串。视频段没有 `path` 和 `file_id`。
- `get_file`：传 `file` 或 `file_id`，先把视频下载到协议端本地，返回本地路径 `file` 和 `url`
  （拿不到在线链接时 `url` 也是本地路径），以及 `file_name`、`file_size`。
  **它按文件名取件**（`action/file/GetFile.ts`）：先查 `FileNapCatOneBotUUID` 的内存缓存，键就是文件名，
  后到的同名文件会覆盖前一个；查不到时调 `FileApi.searchForFile([文件名])`，在文件助手里全局按名搜索、
  取第一条。文件名由发送方决定，所以 `get_file` 拿回来的可能是别的会话里的同名文件，用之前要核对
  文件名与大小和记录里的一致（`_same_file`）。
- `nc_get_rkey`：只有图片用的 `private_rkey` / `group_rkey`，视频链接换不了，这里不用。

## 为什么每次都现取

链接带 `rkey`，有时效，不该进日志、聊天记录和提示词，也不缓存，用的时候按消息编号向协议端要。
`get_msg` 能取到这个账号看得见的任何消息，所以先确认这条消息确实在当前频道、确实是视频、没有
被撤回，取回来之后再核对归属。

## 本机文件后备

协议端给的是它自己机器上的路径。只有 NA 在本机起的内置 NapCat（实例 `provider=builtin_napcat`）
的路径能换算：容器里的 `/app/.config/QQ` 就是宿主上的 `napcat_data/<实例>/QQ`。远程协议端报的路径
一律不读，哪怕本机同名位置上真有文件：多租户下协议端可能归租户控制。有实例键时不回落单实例年代的
`napcat_data/QQ`；没有实例键（升级前的会话标识）时判断不了协议端是不是内置的，也不读。
只认视频后缀；换算后 `resolve()` 必须仍在那个实例的目录里；打开时不跟随符号链接，打开后按已打开的
fd 再核对一次，挡住“检查之后把文件换成链接”。
"""

import asyncio
import os
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Mapping, Optional, Set, Tuple, TypeVar
from urllib.parse import urljoin

import anyio
import httpx
from nonebot.adapters.onebot.v11 import Message
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from nekro_agent.adapters.onebot_v11.core import channel as channel_key
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_adapter_instance import TOMBSTONE_STATUS, DBAdapterInstance
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.schemas.chat_message import ChatMessageSegmentType, ChatMessageSegmentVideo
from nekro_agent.tools.common_util import (
    UnsafeDownloadUrlError,
    assert_download_url_safe,
)
from nekro_agent.tools.path_convertor import LocalFileRefused, is_url_path, open_regular_file_within
from nekro_agent.tools.tenant_egress import download_http_transport
from nekro_agent.tools.untrusted_content import sanitize_inline_text

logger = get_sub_logger("adapter.onebot_v11")

ADAPTER_KEY = "onebot_v11"
MAX_VIDEO_NAME_LENGTH = 128

# 内置 NapCat 把 QQ 数据目录挂在这里，宿主侧是 `napcat_data/<实例>/QQ`
NAPCAT_CONTAINER_QQ_DIR = PurePosixPath("/app/.config/QQ")

# 单次转发的上限：与插件大小限额的硬上限同一个数
MAX_PROXY_BYTES = 512 * 1024 * 1024
MAX_PROXY_REDIRECTS = 3
# 一条转发的总时长上限。暂停时浏览器不读，由 `SEND_TIMEOUT_SECONDS` 先断开；继续播放时浏览器带 Range 重新请求
MAX_PROXY_SECONDS = 1800.0
# 浏览器多久不收数据就断开。暂停播放、缓冲够了都会停读；60 秒是上游读超时的 3 倍，
# 网络抖动不至于误断，又不会让一条停读的连接把上游连接和名额占上半小时
SEND_TIMEOUT_SECONDS = 60.0
CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 20.0
CHUNK_BYTES = 256 * 1024
# 判文件头要的字节数。浏览器在文件开头只要几个字节（探测、Safari 的 `bytes=0-1`）时，向上游多要到这么多，
# 判完再按原区间裁给浏览器；`_looks_like_video` 至少要 8 个字节
HEAD_CHECK_BYTES = 32
# 上游不认 Range 时，NA 要先读掉起点之前的字节再丢弃。超过这个数直接 416，不替请求方下载：
# 与视频分析插件的默认大小限额同为 64 MB，常见的 QQ 视频整段都在这个范围内，仍能任意拖动
MAX_SKIP_BYTES = 64 * 1024 * 1024
# 同时在转发的流（含 `video-access` 的探测）。每条流占一条上游连接、一次 `get_msg`，内存里最多一块 `CHUNK_BYTES`。
# - 每个用户 6 条：浏览器对同一主机最多 6 条 HTTP/1.1 连接，还要分给接口与 SSE，正常播放碰不到，
#   碰到的只有脚本或 HTTP/2 下的滥用。
# - 全局 32 条：约 8 MB 缓冲，协议端同时最多 32 个 `get_msg`。
# - 每个租户最多 8 条，其中前 2 条是保底：一个 `<video>` 播放加拖动时重叠的那一次 Range 请求。
# - 保底之外的名额只在全局还剩 8 条以上时发（租户合计最多占到 24 条）。不管多少个租户同时撑满，
#   最后 8 条只留给还没用到保底的租户（每个 2 条，够 4 个租户各看一个视频）和平台方。
# - 平台方（部署方）只受每用户与全局上限约束，单租户部署照旧能用满 32 条。
MAX_STREAMS_PER_USER = 6
MAX_STREAMS_PER_TENANT = 8
TENANT_GUARANTEED_STREAMS = 2
RESERVED_STREAMS = 8
MAX_STREAMS_TOTAL = 32

_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
_RANGE_PATTERN = re.compile(r"^bytes=(\d*)-(\d*)$")
_CONTENT_RANGE_TOTAL = re.compile(r"^bytes (?:\d+-\d+|\*)/(\d+)$")
_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")
# 上游说是什么就转什么等于让任意响应以同源内容渲染，所以只放行这张表，转发时也写表里的值
_VIDEO_MEDIA_TYPES = frozenset({"video/mp4", "video/webm", "video/quicktime", "video/x-matroska", "video/ogg", "video/mpeg"})
_OPAQUE_MEDIA_TYPES = frozenset({"application/octet-stream", "binary/octet-stream"})
_SUFFIX_MEDIA_TYPES: Mapping[str, str] = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
}
PROXY_SECURITY_HEADERS: Mapping[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Cache-Control": "private, no-store",
}

T = TypeVar("T")


class VideoUnavailableReason(str, Enum):
    """前端按这个值选文案，所以取值是接口契约"""

    NOT_FOUND = "not_found"
    RECALLED = "recalled"
    OFFLINE = "offline"
    EXPIRED = "expired"
    UNAVAILABLE = "unavailable"


class OnebotVideoError(Exception):
    """取不到视频。文案写给人看，不带链接"""

    def __init__(self, reason: VideoUnavailableReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class StaleVideoLocation(Exception):
    """这个地址用不了，换下一个。文案不带链接"""


@dataclass(frozen=True)
class VideoRecord:
    """聊天记录里能证明“这个频道有这条视频”的那一行"""

    message_id: int
    sender_id: str
    video_name: str = ""
    file_size: int = 0


@dataclass(frozen=True)
class VideoLocation:
    """协议端给出的一个地址：http 链接，或换算到本机的文件"""

    url: str = ""
    local_file: Optional[Path] = None
    local_root: Optional[Path] = None


def video_display_name(data: Mapping[str, Any]) -> str:
    """从视频段里取给人看的文件名；`file` 可能是链接或路径，那种不算文件名"""
    for key in ("name", "filename", "file"):
        raw = data.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        if is_url_path(raw) or raw.startswith(("file:", "base64:")):
            continue
        return sanitize_inline_text(PurePosixPath(raw.replace("\\", "/")).name, MAX_VIDEO_NAME_LENGTH)
    return ""


def video_file_size(data: Mapping[str, Any]) -> int:
    """NapCat 的 `file_size` 是字符串，别的实现可能是数字或叫 `size`"""
    for key in ("file_size", "size"):
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.isascii() and value.isdigit():
            return int(value)
    return 0


def build_video_segment(
    message_id: str,
    data: Mapping[str, Any],
    *,
    quoted: bool = False,
    quoted_sender_id: str = "",
) -> ChatMessageSegmentVideo:
    """视频段 -> 占位段。只留编号与元数据，`url` 不进记录"""
    return ChatMessageSegmentVideo(
        type=ChatMessageSegmentType.VIDEO,
        text=ChatMessageSegmentVideo.placeholder_text(message_id),
        video_name=video_display_name(data),
        file_size=video_file_size(data),
        quoted=quoted,
        quoted_sender_id=quoted_sender_id,
    )


def _video_segment_of(row: DBChatMessage) -> Optional[ChatMessageSegmentVideo]:
    for segment in row.parse_content_data():
        if isinstance(segment, ChatMessageSegmentVideo):
            return segment
    return None


async def quoted_video_from_record(chat_key: str, message_id: str) -> Optional[ChatMessageSegmentVideo]:
    """被引用的消息在记录里是视频时，给引用它的那条消息生成占位段"""
    for row in await DBChatMessage.filter(chat_key=chat_key, message_id=message_id).order_by("id"):
        segment = _video_segment_of(row)
        if segment is not None:
            return segment.model_copy(update={"quoted": True, "quoted_sender_id": str(row.platform_userid or "")})
    return None


async def find_video_record(chat_key: str, message_id: int) -> VideoRecord:
    """确认这个频道里有这条视频且没被撤回

    除了视频消息自己那一行，引用回复里带的占位段也算：升级前插件关着时视频没进记录，
    NA 离线时发的也没有，但用户引用它的时候协议端把内容带回来了。
    """
    rows = await DBChatMessage.filter(chat_key=chat_key, message_id=str(message_id)).order_by("id")
    if any(row.is_recalled for row in rows):
        raise OnebotVideoError(VideoUnavailableReason.RECALLED, f"编号 {message_id} 的视频已被撤回，无法使用")
    for row in rows:
        segment = _video_segment_of(row)
        if segment is not None:
            return VideoRecord(message_id, str(row.platform_userid), segment.video_name, segment.file_size)
    marker = ChatMessageSegmentVideo.placeholder_text(str(message_id))
    for row in await DBChatMessage.filter(chat_key=chat_key, content_data__contains=marker).order_by("-id").limit(20):
        for segment in row.parse_content_data():
            if isinstance(segment, ChatMessageSegmentVideo) and segment.quoted and segment.text == marker:
                return VideoRecord(message_id, segment.quoted_sender_id, segment.video_name, segment.file_size)
    raise OnebotVideoError(VideoUnavailableReason.NOT_FOUND, f"这个频道里没有编号 {message_id} 的视频消息")


_UNSET_IDS = (None, "", 0, "0")


def _origin_mismatch(message: object, *, channel_id: str, sender_id: str, self_id: str = "") -> bool:
    """协议端返回的消息是否明显不属于当前频道 / 原发送者

    `get_msg` 只按消息编号取，能取到这个账号看得见的任何消息；库里的记录只能证明“这个频道里有过这个编号”，
    编号复用或碰撞时协议端给的可能是另一条。所以带了归属字段的就核对（标准 OneBot 的 `get_msg` 只有
    `message_type` 与 `sender`，不少实现还附带 `group_id` / `user_id`），没带的不拦。

    `group_id` 只在群消息上比：NapCat 的临时会话是 `message_type=private`，`group_id` 记来源群。

    私聊里还要求发送者是对方或 Bot 自己：引用回复带出来的占位（`find_video_record` 的后备）记的发送者
    取自协议端对回复段的 `get_msg`，而 NapCat 的消息编号整个账号共用，回复段里填一个别的私聊的编号，
    “发送者一致”照样成立，播出来的却是另一个人的私聊视频。
    """
    if not isinstance(message, dict):
        return False
    kind, _, target = channel_id.partition("_")
    message_type = message.get("message_type")
    if message_type in ("group", "private") and message_type != kind:
        return True
    group_id = message.get("group_id")
    if group_id not in _UNSET_IDS and message_type != "private":
        if kind != "group" or str(group_id) != target:
            return True
    sender = message.get("sender")
    claimed = [message.get("user_id"), sender.get("user_id") if isinstance(sender, dict) else None]
    if kind == "private":
        allowed = {target, self_id} - {""}
        if sender_id and sender_id not in allowed:
            return True
        if any(user_id not in _UNSET_IDS and str(user_id) not in allowed for user_id in claimed):
            return True
    if not sender_id:
        return False
    return any(user_id not in _UNSET_IDS and str(user_id) != sender_id for user_id in claimed)


def _segments_of(message: object) -> Optional[List[Tuple[str, Dict[str, Any]]]]:
    """`get_msg` 里的 `message` 有两种形态：段数组，或（协议端配成字符串格式时）CQ 码串"""
    raw = message.get("message") if isinstance(message, dict) else None
    if isinstance(raw, str):
        return [(segment.type, dict(segment.data)) for segment in Message(raw)]
    if isinstance(raw, list):
        return [
            (str(segment.get("type")), segment["data"] if isinstance(segment.get("data"), dict) else {})
            for segment in raw
            if isinstance(segment, dict)
        ]
    return None


async def builtin_qq_root(instance_key: str) -> Optional[Path]:
    """这个实例是 NA 在本机起的内置 NapCat 时，返回它的 QQ 数据目录在宿主上的**真实路径**；否则 None

    判据与容器管理同一套：`instance_manager.map_provider` 归一化 `provider`，目录取
    `container_manager.get_qq_state_dir`，也就是建容器时挂进 `/app/.config/QQ` 的那个目录。
    没有实例键（升级前的会话标识）时判断不了，按远程处理。

    根目录本身也要核对：内置 NapCat 的容器能写 `napcat_data/<实例>`，把 `QQ` 换成指向 `/` 的链接，
    “必须在根目录里”就失去了意义。所以 `resolve()` 之后必须正好等于按 `DATA_DIR` 拼出来的位置
    （`DATA_DIR` 自己是链接没关系，它由部署方决定）；此后一律拿这个真实路径比较，不再 `resolve()` 根目录。
    """
    if not instance_key or not channel_key.is_valid_instance_key(instance_key):
        return None
    instance = (
        await DBAdapterInstance.filter(adapter_key=ADAPTER_KEY, instance_key=instance_key)
        .exclude(status=TOMBSTONE_STATUS)
        .first()
    )
    if instance is None:
        return None
    # 延迟导入：容器管理与实例管理会拖进 aiodocker / qdrant，并在模块级导入 onebot 的实例注册表，放到模块级会成环
    from nekro_agent.schemas.console import InstanceProvider
    from nekro_agent.services.console.container_manager import get_qq_state_dir
    from nekro_agent.services.console.instance_manager import map_provider

    if map_provider(instance.provider) is not InstanceProvider.BUILTIN_NAPCAT:
        return None
    try:
        real_root = get_qq_state_dir(instance_key).resolve(strict=True)
        expected = Path(OsEnv.DATA_DIR).resolve(strict=True) / "napcat_data" / instance_key / "QQ"
    except (OSError, RuntimeError):
        return None
    if real_root != expected:
        logger.warning(f"内置实例的 QQ 数据目录经过了符号链接，不读本机文件: instance_key={instance_key}")
        return None
    return real_root


def _safe_path_part(part: str) -> bool:
    """容器路径里的一段能不能原样拼到宿主路径上

    协议端给的是 POSIX 路径，按 `/` 拆开后每段原样拼到宿主路径上。宿主是 Windows 时，一段里带盘符或以
    两个反斜杠开头会让 `Path` 换掉整个前缀（`…/QQ/nt_qq/\\\\host\\share\\x.mp4` 拼出来就是 UNC 路径），
    随后的 `resolve()` 会先去访问那台主机，包含校验再拒也晚了（SMB 会带上 NA 服务账户的凭据）。
    所以拼接之前就拒：反斜杠、冒号、NUL。NapCat 给的路径里不会有这些。
    `..` 不在这里拒：它只能在本机往上走，换不了盘也到不了别的主机，交给 `resolve()` 之后的包含校验。
    """
    return bool(part) and not any(ch in part for ch in ("\\", ":", "\x00"))


def _map_protocol_path(qq_root: Optional[Path], raw_path: str) -> Optional[Path]:
    """把内置 NapCat 容器里的路径换算成本机的真实路径；换算不了、越界、不是视频都返回 None

    `qq_root` 是 `builtin_qq_root` 核对过的真实路径；为 None 表示协议端不是内置的，它报的路径一律不读。
    先逐段做词法检查，再 `resolve()`，最后看真实位置是否仍在根目录里。
    """
    if qq_root is None:
        return None
    text = raw_path[len("file://") :] if raw_path.startswith("file://") else raw_path
    try:
        relative = PurePosixPath(text).relative_to(NAPCAT_CONTAINER_QQ_DIR)
    except ValueError:
        return None
    if not relative.parts or not all(_safe_path_part(part) for part in relative.parts):
        logger.warning("协议端给的本机路径含有不该出现的片段，已拒绝")
        return None
    try:
        candidate = qq_root.joinpath(*relative.parts).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    # `resolve()` 消解符号链接：真实位置必须仍在这个实例的目录里；后缀看真实文件的，
    # 名字叫 .mp4 的链接指向聊天数据库也不行
    if not candidate.is_relative_to(qq_root):
        logger.warning("协议端给的本机路径越出了实例的 QQ 数据目录，已拒绝")
        return None
    if candidate.suffix.lower() not in _SUFFIX_MEDIA_TYPES or not candidate.is_file():
        return None
    return candidate


def _location_of(raw: object, *, qq_root: Optional[Path], notes: List[str]) -> Optional[VideoLocation]:
    if not isinstance(raw, str) or not raw:
        return None
    if is_url_path(raw):
        return VideoLocation(url=raw)
    mapped = _map_protocol_path(qq_root, raw)
    if mapped is None or qq_root is None:
        notes.append("协议端只给了它本机的文件路径，NA 读不到（远程协议端，或不是可播放的视频文件）")
        return None
    return VideoLocation(local_file=mapped, local_root=qq_root)


def _same_file(info: Mapping[str, Any], record: VideoRecord) -> bool:
    """`get_file` 拿回来的是不是记录里那个文件：文件名与大小都要对上

    QQ 视频的文件名一般是内容摘要，名字和大小都对上基本就是同一个文件。记录里缺这两项时调用方根本不调 `get_file`。
    """
    return video_display_name({"file": info.get("file_name")}) == record.video_name and video_file_size(info) == record.file_size


async def resolve_with_locations(
    bot: Any,
    record: VideoRecord,
    *,
    chat_key: str,
    attempt: Callable[[VideoLocation], Awaitable[T]],
) -> T:
    """依次把候选地址交给 `attempt`，它抛 `StaleVideoLocation` 就换下一个

    顺序：`get_msg` 现取的地址 → `get_file` 给的地址。`get_file` 要让协议端先下载，放在后面，
    前面成功时不调用。链接过期也走这条路：`get_file` 会重新取一次。`get_file` 按文件名取件，
    拿回来的文件名与大小要和记录对上才用（`_same_file`）；记录里没有这两项时不调它。
    """
    channel_id = channel_key.parse_channel_id(chat_key)
    instance_key = channel_key.parse_instance_key(chat_key)
    message_id = record.message_id
    try:
        message = await bot.call_api("get_msg", message_id=message_id)
    except Exception as e:  # noqa: BLE001 - 协议端的异常类型五花八门，这里只需要一句人话
        raise OnebotVideoError(
            VideoUnavailableReason.EXPIRED,
            f"向协议端取视频失败（{type(e).__name__}），消息可能已被撤回，或协议端重启后不再认识这个编号",
        ) from e
    self_id = str(getattr(bot, "self_id", "") or "")
    if _origin_mismatch(message, channel_id=channel_id, sender_id=record.sender_id, self_id=self_id):
        logger.warning(f"协议端返回的消息与频道记录对不上，已拒绝: chat_key={chat_key} message_id={message_id}")
        raise OnebotVideoError(VideoUnavailableReason.UNAVAILABLE, f"协议端返回的编号 {message_id} 的消息不属于当前频道，已拒绝")
    segments = _segments_of(message)
    if segments is None:
        raise OnebotVideoError(VideoUnavailableReason.UNAVAILABLE, "协议端返回的消息格式无法识别，无法取视频")
    videos = [data for kind, data in segments if kind == "video"]
    if not videos:
        raise OnebotVideoError(VideoUnavailableReason.EXPIRED, "协议端返回的消息里没有视频段，这条消息可能已被撤回或删除")

    notes: List[str] = []
    tried: Set[str] = set()
    roots: List[Optional[Path]] = []

    async def location_of(raw: object) -> Optional[VideoLocation]:
        # 只有协议端给了本机路径时才查实例：链接能用时不多一次查库
        if isinstance(raw, str) and raw and not is_url_path(raw) and not roots:
            roots.append(await builtin_qq_root(instance_key))
        return _location_of(raw, qq_root=roots[0] if roots else None, notes=notes)

    async def try_location(location: Optional[VideoLocation]) -> Tuple[bool, Any]:
        if location is None:
            return False, None
        key = location.url or str(location.local_file)
        if key in tried:
            return False, None
        tried.add(key)
        try:
            return True, await attempt(location)
        except StaleVideoLocation as e:
            notes.append(str(e))
            return False, None

    for data in videos:
        done, result = await try_location(await location_of(data.get("url")))
        if done:
            return result

    verifiable = bool(record.video_name) and record.file_size > 0
    if not verifiable:
        notes.append("记录里没有文件名和大小，无法核对 get_file 的结果，没有使用它")
    for data in videos if verifiable else []:
        file_ref = data.get("file_id") or data.get("file")
        if not isinstance(file_ref, str) or not file_ref or is_url_path(file_ref):
            continue
        try:
            info = await bot.call_api("get_file", file=file_ref, file_id=file_ref)
        except Exception as e:  # noqa: BLE001 - 同上；老协议端没有这个接口时也走到这里
            notes.append(f"get_file 失败（{type(e).__name__}）")
            continue
        if not isinstance(info, dict):
            continue
        if not _same_file(info, record):
            logger.warning(f"get_file 返回的文件与记录对不上，已丢弃: chat_key={chat_key} message_id={message_id}")
            notes.append("get_file 返回的文件名或大小与记录对不上，可能是别处的同名文件")
            continue
        for key in ("url", "file"):
            done, result = await try_location(await location_of(info.get(key)))
            if done:
                return result

    detail = f"（{'；'.join(dict.fromkeys(notes))}）" if notes else ""
    raise OnebotVideoError(VideoUnavailableReason.EXPIRED, f"协议端没有返回视频链接，这条视频可能已过期{detail}")


# ---------------------------------------------------------------------------
# 流式转发
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ByteRange:
    start: Optional[int]
    end: Optional[int]

    def bounds(self, total: int) -> Optional[Tuple[int, int]]:
        """在已知总长时算出闭区间；越界返回 None"""
        if total <= 0:
            return None
        if self.start is None:
            length = self.end or 0
            return (max(total - length, 0), total - 1) if length else None
        if self.start >= total:
            return None
        return self.start, min(self.end if self.end is not None else total - 1, total - 1)

    def header(self) -> str:
        """转给上游的规范写法：不原样转发请求头"""
        return f"bytes={'' if self.start is None else self.start}-{'' if self.end is None else self.end}"


def parse_range_header(raw: Optional[str]) -> Optional[ByteRange]:
    """只认单段 `bytes=a-b`，别的按没带处理（多段没有浏览器用在视频上）"""
    match = _RANGE_PATTERN.match((raw or "").strip())
    if match is None or not any(match.groups()):
        return None
    start = int(match.group(1)) if match.group(1) else None
    end = int(match.group(2)) if match.group(2) else None
    if start is not None and end is not None and end < start:
        return None
    return ByteRange(start, end)


def _looks_like_video(head: bytes) -> bool:
    """文件头：ISO-BMFF（mp4 / mov）的 `ftyp`、Matroska / WebM 的 EBML、MPEG-TS 不收"""
    return head[4:8] == b"ftyp" or head[:4] == b"\x1a\x45\xdf\xa3"


def _declared_media_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _total_size(response: httpx.Response) -> int:
    match = _CONTENT_RANGE_TOTAL.match(response.headers.get("content-range", ""))
    if match:
        return int(match.group(1))
    declared = response.headers.get("content-length", "")
    return int(declared) if response.status_code == 200 and declared.isdigit() else 0


@dataclass
class VideoStream:
    """转发结果：`body` 是字节流（上游或本机文件），`closer` 负责收尾，可以重复调用"""

    status_code: int = 200
    media_type: str = "video/mp4"
    headers: Dict[str, str] = field(default_factory=dict)
    body: Optional[AsyncIterator[bytes]] = None
    closer: Optional[Callable[[], Awaitable[None]]] = None

    async def close(self) -> None:
        """收尾不能只靠生成器的 finally：没启动过的异步生成器 `aclose()` 不会执行它"""
        if self.closer is not None:
            await self.closer()


async def _open_upstream(url: str, range_header: Optional[str]) -> Tuple[httpx.AsyncClient, httpx.Response]:
    """逐跳校验地打开上游

    与插件同一套出站校验：每一跳先预检，建连接那一步由 `download_http_transport` 再解析一次、按同一判据校验、
    直接连校验过的地址并核对对端（两次解析之间改口也连不进内网）。环境代理由传输层处理（经 HTTP(S) 代理时
    `CONNECT` 校验过的地址），所以客户端自己 `trust_env=False`。
    """
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
        follow_redirects=False,
        transport=download_http_transport(),
        trust_env=False,
    )
    headers = {"Accept-Encoding": "identity"}
    if range_header:
        headers["Range"] = range_header
    current = url
    try:
        for _ in range(MAX_PROXY_REDIRECTS + 1):
            await assert_download_url_safe(current)
            response = await client.send(client.build_request("GET", current, headers=headers), stream=True)
            if response.status_code not in _REDIRECT_STATUS:
                return client, response
            location = response.headers.get("location")
            await response.aclose()
            if not location:
                raise StaleVideoLocation("视频链接返回了重定向，但没有目标地址")
            current = urljoin(current, location)
        raise StaleVideoLocation("视频链接重定向次数过多")
    except UnsafeDownloadUrlError as e:
        await client.aclose()
        logger.warning(f"视频预览的上游地址被出站防护拒绝: {type(e).__name__}")
        raise StaleVideoLocation("视频链接被出站防护拒绝") from e
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:
        # `InvalidURL` 不是 `HTTPError` 的子类；`ValueError` 来自 urlparse（如 `http://[::1`）。
        # 协议端给的链接什么样都有，接不住就是一条 500，客户端还没关
        await client.aclose()
        raise StaleVideoLocation(f"连接视频地址失败（{type(e).__name__}）") from e
    except StaleVideoLocation:
        await client.aclose()
        raise


async def _close_upstream(client: httpx.AsyncClient, response: httpx.Response) -> None:
    """可以重复调用；被取消时也要关完，否则连接留在池外"""
    with anyio.CancelScope(shield=True):
        await response.aclose()
        await client.aclose()


class StreamCut(Exception):
    """正文没转完就停下：响应不能以结束帧收尾，否则分块传输下浏览器会把截短的内容当成完整的"""


async def _pump(
    client: httpx.AsyncClient,
    response: httpx.Response,
    chunks: AsyncIterator[bytes],
    first: bytes,
    *,
    skip: int,
    limit: int,
    cut_at_limit: bool,
) -> AsyncIterator[bytes]:
    """把上游字节转给浏览器。`skip` 只在上游不认 Range 时非零；`limit` 是最多转多少字节

    `cut_at_limit`：浏览器不知道总长（没有 `Content-Length`）时，`limit` 只是大小上限而不是正文的长度，
    到了上限后面还有数据就是切断，抛 `StreamCut`；知道总长时 `limit` 就是要的那一段，转够了正常结束。
    时长与发送超时由 `VideoStreamResponse` 管：浏览器停读时生成器挂在 yield 上，这里查不到。
    """
    remaining = limit
    try:
        chunk = first
        while True:
            if chunk:
                if skip >= len(chunk):
                    skip -= len(chunk)
                else:
                    piece = chunk[skip:]
                    skip = 0
                    take = piece[:remaining]
                    if take:
                        remaining -= len(take)
                        yield take
                    if len(piece) > len(take):
                        if cut_at_limit:
                            raise StreamCut(f"视频超过转发上限 {MAX_PROXY_BYTES // (1024 * 1024)} MB，后面的不再转发")
                        return
                    if remaining == 0 and not cut_at_limit:
                        return
            try:
                chunk = await chunks.__anext__()
            except StopAsyncIteration:
                return
    finally:
        await _close_upstream(client, response)


def _upstream_range(byte_range: Optional[ByteRange]) -> Tuple[Optional[str], Optional[int]]:
    """转给上游的 Range，以及（放宽过时）要裁回给浏览器的最后一个字节

    浏览器在文件开头只要几个字节时（探测的 `bytes=0-0`、Safari 的 `bytes=0-1`），上游只回这几个字节，
    二进制流的文件头判不了。所以向上游多要到 `HEAD_CHECK_BYTES`，判完再裁。
    """
    if byte_range is None:
        return None, None
    if byte_range.start == 0 and byte_range.end is not None and byte_range.end < HEAD_CHECK_BYTES - 1:
        return f"bytes=0-{HEAD_CHECK_BYTES - 1}", byte_range.end
    return byte_range.header(), None


async def _read_head(chunks: AsyncIterator[bytes]) -> bytes:
    """第一块：`aiter_raw` 会攒满 `CHUNK_BYTES` 才交出来，所以只在正文结束时才会不足 8 字节"""
    try:
        return await anext(chunks, b"")
    except httpx.HTTPError as e:
        raise StaleVideoLocation(f"读取视频地址失败（{type(e).__name__}）") from e


def _too_large() -> OnebotVideoError:
    return OnebotVideoError(VideoUnavailableReason.UNAVAILABLE, f"视频超过预览上限 {MAX_PROXY_BYTES // (1024 * 1024)} MB")


def _range_not_satisfiable(total: int) -> VideoStream:
    return VideoStream(status_code=416, headers={"Content-Range": f"bytes */{total}"})


# ---------------------------------------------------------------------------
# 本机文件（内置 NapCat）
# ---------------------------------------------------------------------------


def _open_within(path: Path, root: Path) -> Tuple[int, int]:
    """打开 `path` 并确认打开的就是 `root` 里的那个普通文件，返回 (fd, 大小)

    `_map_protocol_path` 查过一次，但查完到打开之间协议端可以把文件或上级目录换成符号链接，
    所以判据落在已打开的 fd 上（`open_regular_file_within`，插件复制本机文件用的是同一个）。
    `root` 是 `builtin_qq_root` 核对过的真实路径，按原样比较，不再 `resolve()`。
    """
    try:
        return open_regular_file_within(path, (root,), resolve_roots=False)
    except LocalFileRefused as e:
        raise StaleVideoLocation(f"协议端本机的视频文件不可用（{e}）") from e


class _LocalVideoFile:
    """一个已打开的 fd；读与关在同一把锁下，关掉之后线程里迟到的读不会碰到被复用的 fd 号"""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._lock = threading.Lock()

    def read_at(self, offset: int, size: int) -> bytes:
        with self._lock:
            if self._fd < 0:
                return b""
            os.lseek(self._fd, offset, os.SEEK_SET)
            return os.read(self._fd, size)

    def close(self) -> None:
        with self._lock:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1


async def _open_local_stream(path: Path, root: Path, byte_range: Optional[ByteRange]) -> VideoStream:
    media_type = _SUFFIX_MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        raise StaleVideoLocation("协议端本机的文件不是可播放的视频格式")
    fd, size = await asyncio.to_thread(_open_within, path, root)
    handle = _LocalVideoFile(fd)
    try:
        if size > MAX_PROXY_BYTES:
            raise _too_large()
        headers = {"Accept-Ranges": "bytes"}
        status_code = 200
        start, length = 0, size
        if byte_range is not None:
            bounds = byte_range.bounds(size)
            if bounds is None:
                handle.close()
                return _range_not_satisfiable(size)
            start, last = bounds
            length = last - start + 1
            status_code = 206
            headers["Content-Range"] = f"bytes {start}-{last}/{size}"
        headers["Content-Length"] = str(length)
    except BaseException:
        handle.close()
        raise

    async def body() -> AsyncIterator[bytes]:
        offset, remaining = start, length
        try:
            while remaining > 0:
                piece = await asyncio.to_thread(handle.read_at, offset, min(CHUNK_BYTES, remaining))
                if not piece:
                    return
                offset += len(piece)
                remaining -= len(piece)
                yield piece
        finally:
            handle.close()

    async def closer() -> None:
        handle.close()

    return VideoStream(status_code=status_code, media_type=media_type, headers=headers, body=body(), closer=closer)


async def open_video_stream(location: VideoLocation, range_header: Optional[str]) -> VideoStream:
    """打开一个地址。上游拒绝、过期、类型不对都抛 `StaleVideoLocation`，由调用方换下一个"""
    byte_range = parse_range_header(range_header)
    if location.local_file is not None:
        if location.local_root is None:
            raise StaleVideoLocation("协议端本机的文件没有可核对的实例目录")
        return await _open_local_stream(location.local_file, location.local_root, byte_range)

    upstream_range, trim_last = _upstream_range(byte_range)
    client, response = await _open_upstream(location.url, upstream_range)
    try:
        if response.status_code == 416:
            content_range = response.headers.get("content-range", "")
            await _close_upstream(client, response)
            return VideoStream(status_code=416, headers={"Content-Range": content_range} if content_range else {})
        if response.status_code not in (200, 206):
            raise StaleVideoLocation(f"视频地址返回 HTTP {response.status_code}，可能已过期")

        total = _total_size(response)
        if total > MAX_PROXY_BYTES:
            raise _too_large()

        chunks = response.aiter_raw(CHUNK_BYTES)
        first = b""
        media_type = _declared_media_type(response)
        at_file_start = response.status_code == 200 or response.headers.get("content-range", "").startswith("bytes 0-")
        if media_type in _OPAQUE_MEDIA_TYPES:
            # 对象存储常把视频标成二进制流。文件开头能验就验；中间的分片验不了，但下面固定写
            # `video/mp4` 加 nosniff，浏览器只会当视频解码，不会当页面渲染
            if at_file_start:
                first = await _read_head(chunks)
                if not _looks_like_video(first):
                    raise StaleVideoLocation("视频地址返回的内容不是视频")
            media_type = "video/mp4"
        elif media_type not in _VIDEO_MEDIA_TYPES:
            logger.warning(f"视频预览拦下了非视频响应: content-type={media_type or '(空)'}")
            raise StaleVideoLocation("视频地址返回的内容不是视频")

        headers = {"Accept-Ranges": "bytes"}
        status_code = response.status_code
        skip = 0
        limit = MAX_PROXY_BYTES
        if status_code == 206 and trim_last is not None:
            # 为了判文件头向上游多要了几个字节，按浏览器原来要的区间裁回去
            match = _CONTENT_RANGE.match(response.headers.get("content-range", ""))
            if match is None or match.group(1) != "0":
                raise StaleVideoLocation("视频地址返回的区间与请求对不上")
            last = min(trim_last, int(match.group(2)))
            limit = last + 1
            headers["Content-Range"] = f"bytes 0-{last}/{match.group(3)}"
            headers["Content-Length"] = str(limit)
        elif status_code == 206:
            for name in ("content-range", "content-length"):
                value = response.headers.get(name)
                if value:
                    headers[name.title()] = value
        elif byte_range is not None and total:
            # 上游不认 Range：跳过前面的字节自己裁出来，否则进度条拖不动
            bounds = byte_range.bounds(total)
            if bounds is None:
                await _close_upstream(client, response)
                return _range_not_satisfiable(total)
            skip, last = bounds
            if skip > MAX_SKIP_BYTES:
                # 要先替请求方读掉 skip 个字节再丢弃：超过上限不读，直接告诉浏览器这一段给不了
                logger.info(f"上游不认 Range，起点 {skip} 超过可跳过的上限，返回 416")
                await _close_upstream(client, response)
                return _range_not_satisfiable(total)
            limit = last - skip + 1
            status_code = 206
            headers["Content-Range"] = f"bytes {skip}-{last}/{total}"
            headers["Content-Length"] = str(limit)
        elif total:
            headers["Content-Length"] = str(total)
    except BaseException:
        await _close_upstream(client, response)
        raise

    return VideoStream(
        status_code=status_code,
        media_type=media_type,
        headers=headers,
        body=_pump(client, response, chunks, first, skip=skip, limit=limit, cut_at_limit="Content-Length" not in headers),
        closer=lambda: _close_upstream(client, response),
    )


async def open_message_video(bot: Any, record: VideoRecord, *, chat_key: str, range_header: Optional[str]) -> VideoStream:
    """取最新地址并打开，一个地址不行就换下一个"""

    async def attempt(location: VideoLocation) -> VideoStream:
        return await open_video_stream(location, range_header)

    return await resolve_with_locations(bot, record, chat_key=chat_key, attempt=attempt)


# ---------------------------------------------------------------------------
# 资源上限：名额与响应
# ---------------------------------------------------------------------------


class StreamSlots:
    """转发名额，按用户、租户、全局三级计数

    拿不到名额就立刻拒绝，不排队：排队的请求同样占着连接。只在事件循环线程里用，不需要锁。
    租户键为 None（平台方）时不受租户上限约束，只看每用户与全局。
    租户用到保底（`TENANT_GUARANTEED_STREAMS`）之后，再要名额得全局还剩 `RESERVED_STREAMS` 条以上：
    几个租户同时撑满也只能占到 `MAX_STREAMS_TOTAL - RESERVED_STREAMS` 条，剩下的留给别的租户的保底与平台方。
    """

    def __init__(self) -> None:
        self._per_user: Dict[str, int] = {}
        self._per_tenant: Dict[str, int] = {}
        self._total = 0

    @staticmethod
    def _take(counts: Dict[str, int], key: str) -> None:
        counts[key] = counts.get(key, 0) + 1

    @staticmethod
    def _give_back(counts: Dict[str, int], key: str) -> None:
        left = counts.get(key, 1) - 1
        if left > 0:
            counts[key] = left
        else:
            counts.pop(key, None)

    def _tenant_may_take(self, used: int) -> bool:
        if used >= MAX_STREAMS_PER_TENANT:
            return False
        return used < TENANT_GUARANTEED_STREAMS or self._total < MAX_STREAMS_TOTAL - RESERVED_STREAMS

    def try_acquire(self, user_key: str, tenant_key: Optional[str] = None) -> Optional[Callable[[], None]]:
        """拿一个名额，返回归还函数（重复调用只还一次）；任何一级满了返回 None"""
        if self._total >= MAX_STREAMS_TOTAL or self._per_user.get(user_key, 0) >= MAX_STREAMS_PER_USER:
            return None
        if tenant_key is not None and not self._tenant_may_take(self._per_tenant.get(tenant_key, 0)):
            return None
        self._total += 1
        self._take(self._per_user, user_key)
        if tenant_key is not None:
            self._take(self._per_tenant, tenant_key)
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            self._total -= 1
            self._give_back(self._per_user, user_key)
            if tenant_key is not None:
                self._give_back(self._per_tenant, tenant_key)

        return release

    def in_use(self) -> int:
        return self._total


video_stream_slots = StreamSlots()


class VideoStreamResponse(StreamingResponse):
    """视频转发的流式响应

    四件 `StreamingResponse` 自己管不到的事：

    - 收尾不依赖生成器：浏览器在第一个字节之前断开时，生成器一次都没被迭代过，它的 finally 不会跑。
      这里不管怎么结束都关上游、还名额。
    - 发送有超时：浏览器停读时 `send` 一直挂着，生成器停在 yield 上，两块数据之间的检查查不到。
    - 总时长有上限，连同读上游、等浏览器一起算。
    - **被切断就不能看起来像正常结束**：发送超时、到时长上限时不发结束帧，让服务器直接断开连接。
      分块传输（上游没给长度）时，补一个 `more_body: False` 就等于告诉浏览器“视频到此为止”，
      截短的视频会被当成完整的。这也要求外面不能套 `BaseHTTPMiddleware`：它会替内层补结束帧，
      所以计时中间件改成了纯 ASGI（`routers/__init__.py:RequestTimingMiddleware`）。
    """

    def __init__(self, stream: VideoStream, *, headers: Mapping[str, str], on_close: Callable[[], None]) -> None:
        if stream.body is None:
            raise ValueError("没有正文的结果不走流式响应")
        super().__init__(stream.body, status_code=stream.status_code, media_type=stream.media_type, headers=dict(headers))
        self._stream = stream
        self._on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._on_close()
            with anyio.CancelScope(shield=True):
                await self._stream.close()

    async def stream_response(self, send: Send) -> None:
        with anyio.move_on_after(MAX_PROXY_SECONDS):
            try:
                with anyio.fail_after(SEND_TIMEOUT_SECONDS):
                    await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
                async for chunk in self.body_iterator:
                    with anyio.fail_after(SEND_TIMEOUT_SECONDS):
                        await send({"type": "http.response.body", "body": chunk, "more_body": True})
                # 只有正文真的读完了才发结束帧
                with anyio.fail_after(SEND_TIMEOUT_SECONDS):
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                return
            except TimeoutError:
                logger.info(f"浏览器 {SEND_TIMEOUT_SECONDS:.0f} 秒没有收数据，视频转发已断开（不发结束帧）")
                return
            except StreamCut as e:
                logger.info(f"{e}（不发结束帧）")
                return
        logger.info("视频转发到达时长上限，已断开（不发结束帧）；浏览器继续播放时会带 Range 重新请求")

