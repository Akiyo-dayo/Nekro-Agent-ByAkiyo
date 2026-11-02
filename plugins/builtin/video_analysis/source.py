"""把 `source` 变成工作目录里的一份私有副本

无论来源是链接还是沙盒路径，后面的 ffmpeg 只碰工作目录里这一份副本：

- 链接走 `assert_download_url_safe`（与聊天图片下载同一道 SSRF 校验），**每一跳重定向都重新校验**，
  边下边计数；不用 `download_file` 是因为它不跟重定向、整段读进内存、上限写死、还把文件落进上传目录。
- 沙盒路径走 `FileSystem.get_file`（挡 `..` 与符号链接），再复制出来。不直接把原路径交给 ffmpeg：
  `/app/shared` 对沙盒是可写的，而沙盒里跑的是模型生成的代码，校验之后把文件换成指向
  `<数据目录>/configs/...` 的符号链接就能让校验失效（#73 是同一类窗口）。所以打开之后按**已打开的 fd**
  复核真实路径，再从这个 fd 复制，此后沙盒怎么改都影响不到分析。

DNS 重绑定：校验解析一次、httpx 连接时再解析一次，所以读响应之前按实际对端再判一次
（`assert_connected_peer_safe`）。这要求连接是直连的，走代理时对端是代理，因此 `trust_env=False`。
"""

import asyncio
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import aiofiles
import httpx
from nonebot.adapters.onebot.v11 import Message

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.chat_message import ChatMessageSegmentType
from nekro_agent.tools.common_util import (
    UnresolvableDownloadHostError,
    UnsafeDownloadUrlError,
    assert_connected_peer_safe,
    assert_download_url_safe,
)
from nekro_agent.tools.file_utils import FileSystem
from nekro_agent.tools.path_convertor import is_url_path
from nekro_agent.tools.untrusted_content import sanitize_inline_text

from .media import INPUT_NAME, VideoAnalysisError

logger = get_sub_logger("video_analysis")

_DOWNLOAD_READ_TIMEOUT_SECONDS = 20.0
_COPY_CHUNK_BYTES = 1024 * 1024
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
# 聊天里 OneBot 视频的占位是 `[Video: message:12345]`
MESSAGE_SOURCE_PREFIX = "message:"
ONEBOT_ADAPTER_KEY = "onebot_v11"


def safe_url_for_log(url: str) -> str:
    """日志里只留协议、主机与路径

    查询串里常有签名与令牌（QQ 的 `rkey`、对象存储的 `X-Amz-Signature`），userinfo 里可能有口令。
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "<invalid-url>"
    return f"{parts.scheme}://{host}{port}{parts.path}"


def _refused(url: str, error: UnsafeDownloadUrlError) -> VideoAnalysisError:
    """给模型 / 用户的是固定文案，原因只进日志

    校验的异常文本里有 `主机 -> IP`，那是内网探测的回显，不能回显给调用方；但运维要能分清
    是链接指向了内网、对端校验没过，还是单纯解析不出主机名（后者与策略无关，文案也不该说成“被拒绝”）。
    日志里只放协议、主机、路径（`safe_url_for_log`），查询串里的签名不进日志。
    """
    logger.warning(f"视频下载被拒绝: {safe_url_for_log(url)}: {error}")
    if isinstance(error, UnresolvableDownloadHostError):
        return VideoAnalysisError("视频链接的主机名无法解析，请检查链接是否正确")
    return VideoAnalysisError("视频链接被出站防护拒绝")


def _peer_ip(response: httpx.Response) -> Optional[str]:
    stream = response.extensions.get("network_stream")
    address = stream.get_extra_info("server_addr") if stream is not None else None
    return address[0] if address else None


def _too_large(max_bytes: int) -> VideoAnalysisError:
    return VideoAnalysisError(f"视频超过大小上限 {max_bytes // (1024 * 1024)} MB")


async def download_video(url: str, dest: Path, *, max_bytes: int, max_redirects: int) -> None:
    """流式下载到 dest，超限立刻中止

    错误文案不带 URL：httpx 的异常文本里是完整链接，签名会顺着错误进模型与日志。
    """
    current = url
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(_DOWNLOAD_READ_TIMEOUT_SECONDS),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        for _ in range(max_redirects + 1):
            try:
                await assert_download_url_safe(current)
            except UnsafeDownloadUrlError as e:
                raise _refused(current, e) from e
            try:
                async with client.stream("GET", current) as response:
                    try:
                        assert_connected_peer_safe(_peer_ip(response))
                    except UnsafeDownloadUrlError as e:
                        raise _refused(current, e) from e
                    if response.status_code in _REDIRECT_STATUS:
                        location = response.headers.get("location")
                        if not location:
                            raise VideoAnalysisError("视频链接返回了重定向，但没有目标地址")
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise VideoAnalysisError(f"下载视频失败，HTTP {response.status_code}")
                    declared = response.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        raise _too_large(max_bytes)
                    received = 0
                    async with aiofiles.open(dest, "wb") as output:
                        async for chunk in response.aiter_bytes():
                            received += len(chunk)
                            if received > max_bytes:
                                raise _too_large(max_bytes)
                            await output.write(chunk)
                    return
            except httpx.HTTPError as e:
                raise VideoAnalysisError(f"下载视频失败（{type(e).__name__}）") from e
    raise VideoAnalysisError(f"视频链接重定向超过 {max_redirects} 次")


def _opened_real_path(fd: int) -> Optional[Path]:
    """已打开 fd 的真实路径；没有 /proc 的平台返回 None"""
    link = f"/proc/self/fd/{fd}"
    return Path(os.readlink(link)) if os.path.exists(link) else None


def _copy_within_roots(source: Path, dest: Path, roots: Iterable[Path], max_bytes: int) -> None:
    """把 source 复制到 dest；打开后复核真实路径，边读边计数"""
    # O_NONBLOCK：换成 FIFO 时不带它，open 会一直等写端，把工作线程永久占住
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(source, flags)
    except OSError as e:
        raise VideoAnalysisError("无法读取该文件") from e
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise VideoAnalysisError("该路径不是普通文件")
        real_path = _opened_real_path(handle.fileno())
        if real_path is not None and not any(real_path.is_relative_to(root.resolve()) for root in roots):
            raise VideoAnalysisError("路径不在当前会话的沙盒目录内")
        if info.st_size > max_bytes:
            raise _too_large(max_bytes)
        copied = 0
        with dest.open("wb") as output:
            while chunk := handle.read(_COPY_CHUNK_BYTES):
                copied += len(chunk)
                if copied > max_bytes:
                    raise _too_large(max_bytes)
                output.write(chunk)


def resolve_sandbox_path(fs: FileSystem, source: str) -> Path:
    """沙盒路径 -> 宿主路径；相对路径按沙盒工作目录 /app 解析

    校验失败的报错里只放沙盒路径：`assert_path_within` 的异常文本带宿主绝对路径。
    """
    shown = sanitize_inline_text(source, 200)
    posix = PurePosixPath(source)
    candidate = posix if posix.is_absolute() else PurePosixPath("/app") / posix
    try:
        host_path = fs.get_file(str(candidate))
    except (ValueError, OSError) as e:
        raise VideoAnalysisError(f"路径 {shown} 不在当前会话的沙盒目录内，请使用 /app/uploads 或 /app/shared 下的路径") from e
    if not host_path.is_file():
        raise VideoAnalysisError(f"文件不存在: {shown}")
    return host_path


def _parse_message_id(source: str) -> int:
    raw = source[len(MESSAGE_SOURCE_PREFIX) :]
    if not raw.isascii() or not raw.isdigit():
        raise VideoAnalysisError("message: 后面要跟聊天里显示的消息编号，如 message:12345")
    return int(raw)


_UNSET_IDS = (None, "", 0, "0")


def _origin_mismatch(message: object, *, channel_id: str, sender_id: str) -> bool:
    """协议端返回的消息是否明显不属于当前频道 / 原发送者

    `get_msg` 只按消息编号取，能取到这个账号看得见的任何消息；库里的记录只能证明“这个频道里有过这个编号”，
    编号复用或碰撞时协议端给的可能是另一条。所以带了归属字段的就核对（标准 OneBot 的 `get_msg` 只有
    `message_type` 与 `sender`，不少实现还附带 `group_id` / `user_id`），没带的不拦。
    """
    if not isinstance(message, dict):
        return False
    kind, _, target = channel_id.partition("_")
    message_type = message.get("message_type")
    if message_type in ("group", "private") and message_type != kind:
        return True
    group_id = message.get("group_id")
    if group_id not in _UNSET_IDS:
        if kind != "group" or str(group_id) != target:
            return True
    sender = message.get("sender")
    claimed = [message.get("user_id"), sender.get("user_id") if isinstance(sender, dict) else None]
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


def _video_url_from_message(message: object) -> str:
    segments = _segments_of(message)
    if segments is None:
        raise VideoAnalysisError("协议端返回的消息格式无法识别，无法取视频")
    videos = [data for kind, data in segments if kind == "video"]
    if not videos:
        raise VideoAnalysisError("协议端返回的消息里没有视频段，这条消息可能已被撤回或删除")
    for data in videos:
        url = data.get("url")
        if isinstance(url, str) and is_url_path(url):
            return url
    raise VideoAnalysisError("协议端没有返回视频链接，这条视频可能已过期")


async def resolve_message_video(ctx: AgentCtx, source: str) -> str:
    """把 `message:<id>` 换成新鲜的下载链接

    链接带 `rkey`，有时效，也不该进日志和聊天记录，所以聊天里只留消息编号，用的时候现取。
    协议端的 `get_msg` 能取到这个账号看得见的任何消息，所以先确认这条消息确实在当前频道、确实是视频、
    没有被撤回，取回来之后再核对归属。
    """
    message_id = _parse_message_id(source)
    if ctx.adapter_key != ONEBOT_ADAPTER_KEY:
        raise VideoAnalysisError("当前平台不支持 message: 来源，请传文件路径或链接")
    rows = await DBChatMessage.filter(chat_key=ctx.chat_key, message_id=str(message_id)).order_by("id")
    video_rows = [row for row in rows if any(segment.type == ChatMessageSegmentType.VIDEO.value for segment in row.parse_content_data())]
    if not video_rows:
        raise VideoAnalysisError(f"这个频道里没有编号 {message_id} 的视频消息")
    if any(row.is_recalled for row in video_rows):
        raise VideoAnalysisError(f"编号 {message_id} 的视频已被撤回，无法分析")
    try:
        bot = await ctx.get_onebot_v11_bot()
    except Exception as e:  # noqa: BLE001 - 账号没连上、定位不到连接，都只需要一句人话
        raise VideoAnalysisError(f"当前账号的协议端不在线或无法定位（{type(e).__name__}），无法取视频") from e
    try:
        message = await bot.call_api("get_msg", message_id=message_id)
    except Exception as e:  # noqa: BLE001 - 协议端的异常类型五花八门，这里只需要一句人话
        raise VideoAnalysisError(f"向协议端取视频失败（{type(e).__name__}）") from e
    if _origin_mismatch(message, channel_id=ctx.channel_id or "", sender_id=str(video_rows[0].platform_userid)):
        logger.warning(f"协议端返回的消息与频道记录对不上，已拒绝: chat_key={ctx.chat_key} message_id={message_id}")
        raise VideoAnalysisError(f"协议端返回的编号 {message_id} 的消息不属于当前频道，已拒绝")
    return _video_url_from_message(message)


async def materialize_source(
    fs: FileSystem,
    source: str,
    workdir: Path,
    *,
    max_bytes: int,
    max_redirects: int,
) -> None:
    """把 source 落成 `<workdir>/input.video`"""
    dest = workdir / INPUT_NAME
    if is_url_path(source):
        logger.info(f"下载视频: {safe_url_for_log(source)}")
        await download_video(source, dest, max_bytes=max_bytes, max_redirects=max_redirects)
    else:
        host_path = resolve_sandbox_path(fs, source)
        await asyncio.to_thread(_copy_within_roots, host_path, dest, (fs.shared_path, fs.upload_path), max_bytes)
    if dest.stat().st_size == 0:
        raise VideoAnalysisError("视频文件是空的")
