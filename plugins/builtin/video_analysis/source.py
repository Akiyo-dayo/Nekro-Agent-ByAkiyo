"""把 `source` 变成工作目录里的一份私有副本

无论来源是链接还是沙盒路径，后面的 ffmpeg 只碰工作目录里这一份副本：

- 链接走 `assert_download_url_safe`（与聊天图片下载同一道 SSRF 校验），**每一跳重定向都重新校验**，
  边下边计数；不用 `download_file` 是因为它不跟重定向、整段读进内存、上限写死、还把文件落进上传目录。
- 沙盒路径走 `FileSystem.get_file`（挡 `..` 与符号链接），再复制出来。不直接把原路径交给 ffmpeg：
  `/app/shared` 对沙盒是可写的，而沙盒里跑的是模型生成的代码，校验之后把文件换成指向
  `<数据目录>/configs/...` 的符号链接就能让校验失效（#73 是同一类窗口）。所以打开之后按**已打开的 fd**
  复核真实路径，再从这个 fd 复制，此后沙盒怎么改都影响不到分析。

DNS 重绑定：每一跳先预检（`assert_download_url_safe`），真正建连接时由 `download_http_transport`
（`tools/tenant_egress.py`）再解析一次、按同一判据校验、直接连校验过的地址并核对对端，两次解析之间改口
也连不进内网（连都不会连）。环境变量里的代理照用（与 `download_file` 一样），经 HTTP(S) 代理时让它
`CONNECT` 校验过的地址。被拒时不管是预检还是建连接那一步、不管是解析不到还是指向内网，给出去的都是同一句话。
"""

import asyncio
import os
from pathlib import Path, PurePosixPath
from typing import Awaitable, Callable, Iterable, TypeVar
from urllib.parse import urljoin, urlsplit

import aiofiles
import httpx

from nekro_agent.adapters.onebot_v11.core.message_id import parse_message_id
from nekro_agent.adapters.onebot_v11.tools.video import (
    OnebotVideoError,
    StaleVideoLocation,
    VideoLocation,
    find_video_record,
    resolve_with_locations,
)
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.tools.common_util import (
    UnsafeDownloadUrlError,
    assert_download_url_safe,
    tenant_target_unavailable_message,
)
from nekro_agent.tools.file_utils import FileSystem
from nekro_agent.tools.path_convertor import LocalFileRefused, is_url_path, open_regular_file_within
from nekro_agent.tools.tenant_egress import download_http_transport
from nekro_agent.tools.untrusted_content import sanitize_inline_text

from .media import INPUT_NAME, VideoAnalysisError

logger = get_sub_logger("video_analysis")

T = TypeVar("T")

_DOWNLOAD_READ_TIMEOUT_SECONDS = 20.0
_COPY_CHUNK_BYTES = 1024 * 1024
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
# 聊天里 OneBot 视频的占位是 `[Video: message:12345]`
MESSAGE_SOURCE_PREFIX = "message:"
ONEBOT_ADAPTER_KEY = "onebot_v11"


class _LinkFailed(VideoAnalysisError):
    """这个链接下不下来（过期、被拒、连不上）。来源是消息时可以换协议端给的下一个地址"""


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

    「解析不到」与「指向不允许的地址」说成同一句：分开说的话，任何能让 bot 下视频的人都能拿它查 NA 所在网络的
    DNS（这个名字在不在、指不指向内网）。具体原因只进日志，运维照样分得清；日志里只放协议、主机、路径
    （`safe_url_for_log`），查询串里的签名不进日志。
    """
    logger.warning(f"视频下载被拒绝: {safe_url_for_log(url)}: {error}")
    return VideoAnalysisError(tenant_target_unavailable_message("视频链接"))


def _too_large(max_bytes: int) -> VideoAnalysisError:
    return VideoAnalysisError(f"视频超过大小上限 {max_bytes // (1024 * 1024)} MB")


async def download_video(url: str, dest: Path, *, max_bytes: int, max_redirects: int) -> None:
    """流式下载到 dest，超限立刻中止

    错误文案不带 URL：httpx 的异常文本里是完整链接，签名会顺着错误进模型与日志。
    """
    current = url
    # 建连接那一步固定校验过的地址并核对对端；环境代理由传输层处理，所以客户端自己 `trust_env=False`
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(_DOWNLOAD_READ_TIMEOUT_SECONDS),
        follow_redirects=False,
        transport=download_http_transport(),
        trust_env=False,
    ) as client:
        for _ in range(max_redirects + 1):
            try:
                await assert_download_url_safe(current)
            except UnsafeDownloadUrlError as e:
                raise _refused(current, e) from e
            try:
                async with client.stream("GET", current) as response:
                    if response.status_code in _REDIRECT_STATUS:
                        location = response.headers.get("location")
                        if not location:
                            raise VideoAnalysisError("视频链接返回了重定向，但没有目标地址")
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise _LinkFailed(f"下载视频失败，HTTP {response.status_code}")
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
            except UnsafeDownloadUrlError as e:
                # 建连接那一步拒的（两次解析之间改口、对端不对）：与预检拒的同一句
                raise _refused(current, e) from e
            except httpx.HTTPError as e:
                raise _LinkFailed(f"下载视频失败（{type(e).__name__}）") from e
    raise VideoAnalysisError(f"视频链接重定向超过 {max_redirects} 次")


_REFUSED_MESSAGES = {
    "unreadable": "无法读取该文件",
    "not_regular": "该路径不是普通文件",
    "swapped": "路径不在当前会话的沙盒目录内",
    "outside": "路径不在当前会话的沙盒目录内",
}


class _LocalFileRejected(VideoAnalysisError):
    """本机文件打不开或没通过打开后的复核；`reason` 是 `LocalFileRefused` 的说明"""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _copy_within_roots(source: Path, dest: Path, roots: Iterable[Path], max_bytes: int, *, resolve_roots: bool = True) -> None:
    """把 source 复制到 dest；打开后按已打开的 fd 复核真实路径，边读边计数

    打开与复核用核心的 `open_regular_file_within`（WebUI 读内置 NapCat 的本机文件用的是同一个）：
    Linux 读 `/proc/self/fd`，Windows 问文件句柄，macOS 用 `F_GETPATH`，都取不到时核对路径上的文件与 fd 是同一个。
    """
    try:
        fd, size = open_regular_file_within(source, tuple(roots), resolve_roots=resolve_roots)
    except LocalFileRefused as e:
        raise _LocalFileRejected(_REFUSED_MESSAGES.get(e.kind, "无法读取该文件"), str(e)) from e
    with os.fdopen(fd, "rb") as handle:
        if size > max_bytes:
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
    parsed = parse_message_id(source[len(MESSAGE_SOURCE_PREFIX) :])
    if parsed is None:
        raise VideoAnalysisError("message: 后面要跟聊天里显示的消息编号，如 message:12345 或 message:-12345")
    return parsed


async def _with_message_video(ctx: AgentCtx, source: str, attempt: Callable[[VideoLocation], Awaitable[T]]) -> T:
    """把 `message:<id>` 换成新鲜的地址并交给 `attempt`

    校验、取链接与后备都在 `adapters/onebot_v11/tools/video.py`，WebUI 预览走的是同一条路。
    """
    message_id = _parse_message_id(source)
    if ctx.adapter_key != ONEBOT_ADAPTER_KEY:
        raise VideoAnalysisError("当前平台不支持 message: 来源，请传文件路径或链接")
    try:
        record = await find_video_record(ctx.chat_key, message_id)
    except OnebotVideoError as e:
        raise VideoAnalysisError(str(e)) from e
    try:
        bot = await ctx.get_onebot_v11_bot()
    except Exception as e:  # noqa: BLE001 - 账号没连上、定位不到连接，都只需要一句人话
        raise VideoAnalysisError(f"当前账号的协议端不在线或无法定位（{type(e).__name__}），无法取视频") from e
    try:
        return await resolve_with_locations(bot, record, chat_key=ctx.chat_key, attempt=attempt)
    except OnebotVideoError as e:
        raise VideoAnalysisError(str(e)) from e


async def resolve_message_video(ctx: AgentCtx, source: str) -> str:
    """把 `message:<id>` 换成新鲜的下载链接"""

    async def url_only(location: VideoLocation) -> str:
        if not location.url:
            raise StaleVideoLocation("协议端只给了本机路径，没有链接")
        return location.url

    return await _with_message_video(ctx, source, url_only)


async def materialize_message_video(ctx: AgentCtx, source: str, workdir: Path, *, max_bytes: int, max_redirects: int) -> None:
    """把 `message:<id>` 落成 `<workdir>/input.video`；链接过期时换协议端给的下一个地址"""
    dest = workdir / INPUT_NAME

    async def attempt(location: VideoLocation) -> None:
        if location.local_file is not None and location.local_root is not None:
            # `local_root` 是核对过的真实路径，不再 resolve：根目录事后被换成链接时不能跟着它走出去。
            # 被拒（打不开、没通过打开后的复核）与 WebUI 一样当作这个地址用不了，换协议端给的下一个
            try:
                await asyncio.to_thread(
                    _copy_within_roots, location.local_file, dest, (location.local_root,), max_bytes, resolve_roots=False,
                )
            except _LocalFileRejected as e:
                raise StaleVideoLocation(f"协议端本机的视频文件不可用（{e.reason}）") from e
            return
        try:
            logger.info(f"下载视频: {safe_url_for_log(location.url)}")
            await download_video(location.url, dest, max_bytes=max_bytes, max_redirects=max_redirects)
        except _LinkFailed as e:
            raise StaleVideoLocation(str(e)) from e

    await _with_message_video(ctx, source, attempt)
    if dest.stat().st_size == 0:
        raise VideoAnalysisError("视频文件是空的")


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
