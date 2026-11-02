"""ffprobe / ffmpeg 调用：探测、抽帧、抽音轨

## 把媒体文件当敌意输入

ffmpeg 的 demuxer 会听文件内容的话：HLS 播放列表能指向任意 URL，`ffconcat` 能指向本地其他文件，
这是「传一个视频，读走服务器文件 / 打内网」的经典路径。所以 `INPUT_GUARD_ARGS` 里有两道：

- `-protocol_whitelist file`：只许本地文件协议，http / tcp / concat 协议全部进不来；
- `-format_whitelist`：只许普通视频容器。**只有第一道是不够的**：`hls` / `concat` / `image2` 这几个
  demuxer 用 `file` 协议同样能读别的本地文件，要在 demuxer 这一层就拒掉。

输入永远是工作目录里固定名字的私有副本（见 `source.py`），由 `cwd` 定位，所以路径里没有任何
外部可控字符，也不会被当成 URL 或选项解析。

## 子进程

一律参数列表，不经 shell。环境变量只给最小集合，进程里的 `NEKRO_*` 凭据不下放给解析敌意文件的进程。
超时靠外层的 `asyncio.timeout` 取消，`run_tool` 在取消路径上杀进程并等它退出。

进程树与内存上限在 `process.py`。用 `subprocess.Popen` 放进线程，而不是 `asyncio.create_subprocess_exec`：NA 进程在 Windows 上的
事件循环是 Selector（依赖在导入时设的策略），它不支持 asyncio 子进程，裸机部署会直接 NotImplementedError。
"""

import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import IO, Dict, List, Optional, Sequence, Tuple

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.tools.untrusted_content import sanitize_inline_text

from . import process as tool_process

logger = get_sub_logger("video_analysis")

# 工作目录里输入文件的固定名字：不带可被外部影响的字符，也不让 ffmpeg 按扩展名猜格式
INPUT_NAME = "input.video"
AUDIO_NAME = "audio.m4a"

# 只放普通视频容器。名字对应 demuxer 的逗号分段（`mov,mp4,m4a,3gp,3g2,mj2` 命中 `mov` 即可）。
# 故意不含 hls / concat / image2 / lavfi / sdp / dash / gif：前几个能引用外部资源，gif 用不上。
ALLOWED_DEMUXERS = "mov,matroska,webm,avi,flv,mpegts,mpeg,asf,ogg"
INPUT_GUARD_ARGS: Tuple[str, ...] = ("-protocol_whitelist", "file", "-format_whitelist", ALLOWED_DEMUXERS)

# 解码上限。声明 8K 以上分辨率的文件几乎只会是解压炸弹：一帧就能吃掉几个 GB
MAX_SOURCE_PIXELS = 7680 * 4320
# 单次分配上限，挡「几百字节的文件声明几千万条索引」这一类。总量由 `process.py` 的进程上限兜
MAX_ALLOC_BYTES = 512 * 1024 * 1024
MEMORY_GUARD_ARGS: Tuple[str, ...] = ("-max_alloc", str(MAX_ALLOC_BYTES), "-max_pixels", str(MAX_SOURCE_PIXELS))
# 解码线程数：一个进程跑几十个实例，单次分析不该吃满所有核
_DECODE_THREADS = "2"
# 子进程输出的保留上限。多出来的继续读掉、不保留，否则管道写满会把子进程卡死
_MAX_TOOL_OUTPUT_BYTES = 1024 * 1024
# 转写接口普遍限 25MB，留一点余量
_MAX_AUDIO_BYTES = 24 * 1000 * 1000

# 抽帧间隔的下限：1 秒的视频抽 16 帧只是在给模型看 16 张一样的图
_MIN_SAMPLE_INTERVAL_SECONDS = 1.0
# 尾帧不能正好落在时长上：seek 到末尾取不到帧
_TAIL_MARGIN_SECONDS = 0.25
# 两个场景切点的最小间隔，闪烁画面不该占满帧数预算
_MIN_SCENE_GAP_SECONDS = 0.5
# 场景检测缩到这个宽度再算，分数够用且快得多
_SCENE_PROBE_WIDTH = "160"
_SCENE_TIME_RE = re.compile(r"pts_time:(\d+(?:\.\d+)?)")
# showinfo 的输出受文件内容影响，切点数量不能无上限
_MAX_SCENE_CUTS = 500


class VideoAnalysisError(ValueError):
    """给 AI 看的、可据以修正调用的错误

    沙盒方法 raise 才能触发模型的修正机制，返回错误字符串模型会当成成功结果转述。
    """


@dataclass(frozen=True)
class MediaTools:
    ffmpeg: str
    ffprobe: str
    ffmpeg_memory_args: Tuple[str, ...] = MEMORY_GUARD_ARGS
    ffprobe_memory_args: Tuple[str, ...] = MEMORY_GUARD_ARGS


@dataclass(frozen=True)
class VideoMeta:
    duration: float
    width: int
    height: int
    has_audio: bool


@dataclass(frozen=True)
class Frame:
    path: Path
    timestamp: float


@dataclass(frozen=True)
class ToolResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def locate_tools() -> Optional[MediaTools]:
    """在 PATH 上找 ffmpeg / ffprobe，缺任何一个返回 None"""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        return None
    return MediaTools(ffmpeg=ffmpeg, ffprobe=ffprobe)


def require_tools() -> MediaTools:
    tools = locate_tools()
    if tools is None:
        raise VideoAnalysisError(
            "服务器上没有找到 ffmpeg / ffprobe，视频分析不可用。"
            "官方镜像已内置；裸机部署请安装 ffmpeg 并确认它们在 NekroAgent 进程的 PATH 上。",
        )
    return tools


def _accepts(binary: str, args: Sequence[str]) -> bool:
    try:
        result = subprocess.run(  # noqa: S603 - 参数列表，不经 shell
            [binary, "-hide_banner", *args, "-version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


@lru_cache(maxsize=8)
def detect_memory_args(binary: str) -> Tuple[str, ...]:
    """这个版本认得哪些内存参数

    ffmpeg 4.3 到 9.0 都认得这两个。真遇到不认识的版本，整条命令报错退出比没有这道参数更糟，
    所以逐个探测、认不得的不传并告警。此时仍有 `process.py` 的进程内存上限，
    以及 `assert_within_limits` 的分辨率上限。
    """
    pairs = [MEMORY_GUARD_ARGS[index : index + 2] for index in range(0, len(MEMORY_GUARD_ARGS), 2)]
    accepted: List[str] = []
    for pair in pairs:
        if _accepts(binary, pair):
            accepted.extend(pair)
        else:
            logger.warning(f"{binary} 不支持 {pair[0]}，退回只靠进程内存上限")
    return tuple(accepted)


async def prepare_tools() -> MediaTools:
    """找到工具并探测它们支持的内存参数（要跑子进程，放线程里）"""
    tools = require_tools()
    ffmpeg_args, ffprobe_args = await asyncio.gather(
        asyncio.to_thread(detect_memory_args, tools.ffmpeg),
        asyncio.to_thread(detect_memory_args, tools.ffprobe),
    )
    return replace(tools, ffmpeg_memory_args=ffmpeg_args, ffprobe_memory_args=ffprobe_args)


def _tool_env() -> Dict[str, str]:
    """子进程环境：只给跑起来所需的最小集合"""
    env = {"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"}
    # Windows 上进程启动依赖这两个
    for key in ("SYSTEMROOT", "TEMP"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _drain(stream: Optional[IO[bytes]], kept: bytearray) -> None:
    """读到 EOF，只保留前 `_MAX_TOOL_OUTPUT_BYTES` 字节；多出来的也要读掉，否则子进程写满管道就卡死"""
    if stream is None:
        return
    while chunk := stream.read(64 * 1024):
        if len(kept) < _MAX_TOOL_OUTPUT_BYTES:
            kept.extend(chunk[: _MAX_TOOL_OUTPUT_BYTES - len(kept)])


def _wait_and_collect(process: "subprocess.Popen[bytes]") -> ToolResult:
    """在工作线程里同时读两条管道并等进程退出"""
    stdout = bytearray()
    stderr = bytearray()
    with process:
        reader = threading.Thread(target=_drain, args=(process.stderr, stderr), daemon=True)
        reader.start()
        _drain(process.stdout, stdout)
        returncode = process.wait()
        reader.join()
    return ToolResult(returncode=returncode, stdout=bytes(stdout), stderr=bytes(stderr))


async def run_tool(argv: Sequence[str], *, cwd: Path) -> ToolResult:
    """跑一个外部工具直到退出；结束时清掉整棵进程树，被取消时也一样

    杀掉之后工作线程里的读取与等待自然结束，所以这里不用单独收拾它。
    """
    try:
        tool = await asyncio.to_thread(tool_process.spawn_tool, argv, cwd=cwd, env=_tool_env())
    except OSError as e:
        raise VideoAnalysisError(f"无法启动媒体工具（{type(e).__name__}）") from e
    try:
        return await asyncio.to_thread(_wait_and_collect, tool.process)
    finally:
        tool.kill_tree()
        await asyncio.to_thread(tool.process.wait)
        tool.release()


def _log_tool_failure(what: str, result: ToolResult) -> None:
    """工具的 stderr 里可能带文件元数据，只进日志、且清洗截断，不进给 AI 的错误"""
    detail = sanitize_inline_text(result.stderr.decode("utf-8", errors="replace"), 300)
    logger.warning(f"{what} 退出码 {result.returncode}: {detail}")


def build_probe_command(ffprobe: str, memory_args: Sequence[str] = MEMORY_GUARD_ARGS) -> List[str]:
    return [
        ffprobe,
        "-v",
        "error",
        *memory_args,
        *INPUT_GUARD_ARGS,
        "-show_entries",
        "format=duration:stream=codec_type,width,height,duration",
        "-of",
        "json",
        INPUT_NAME,
    ]


def _scale_filter(max_edge: int) -> str:
    """长边缩到 max_edge 以内，只缩不放，保持宽高比"""
    return f"scale='min({max_edge},iw)':'min({max_edge},ih)':force_original_aspect_ratio=decrease"


def build_frame_command(
    ffmpeg: str,
    *,
    timestamp: float,
    max_edge: int,
    output_name: str,
    memory_args: Sequence[str] = MEMORY_GUARD_ARGS,
) -> List[str]:
    return [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-threads",
        _DECODE_THREADS,
        *memory_args,
        *INPUT_GUARD_ARGS,
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        INPUT_NAME,
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-frames:v",
        "1",
        "-vf",
        _scale_filter(max_edge),
        "-q:v",
        "4",
        "-f",
        "image2",
        "-update",
        "1",
        output_name,
    ]


def build_scene_command(
    ffmpeg: str,
    *,
    threshold: float,
    max_seconds: int,
    memory_args: Sequence[str] = MEMORY_GUARD_ARGS,
) -> List[str]:
    return [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "info",
        "-threads",
        _DECODE_THREADS,
        *memory_args,
        *INPUT_GUARD_ARGS,
        "-i",
        INPUT_NAME,
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-t",
        str(max_seconds),
        "-vf",
        f"scale={_SCENE_PROBE_WIDTH}:-2,select='gt(scene,{threshold:.3f})',showinfo",
        "-f",
        "null",
        "-",
    ]


def build_audio_command(ffmpeg: str, *, max_seconds: int, memory_args: Sequence[str] = MEMORY_GUARD_ARGS) -> List[str]:
    return [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-threads",
        _DECODE_THREADS,
        *memory_args,
        *INPUT_GUARD_ARGS,
        "-i",
        INPUT_NAME,
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "aac",
        "-b:a",
        "32k",
        "-t",
        str(max_seconds),
        "-fs",
        str(_MAX_AUDIO_BYTES),
        AUDIO_NAME,
    ]


def _as_positive_float(value: object) -> Optional[float]:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def parse_probe_output(raw: bytes) -> VideoMeta:
    """把 ffprobe 的 JSON 解析成元数据

    时长取不到时**拒绝**而不是猜：时长限额建立在它之上，取不到就等于这条限额失效。
    """
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as e:
        raise VideoAnalysisError("无法读取视频信息，文件可能已损坏或不是视频") from e
    streams = data.get("streams") if isinstance(data, dict) else None
    if not isinstance(streams, list):
        raise VideoAnalysisError("无法读取视频信息，文件可能已损坏或不是视频")

    video = next((s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"), None)
    if video is None:
        raise VideoAnalysisError("文件里没有视频画面")
    has_audio = any(isinstance(s, dict) and s.get("codec_type") == "audio" for s in streams)

    fmt = data.get("format")
    duration = _as_positive_float(fmt.get("duration")) if isinstance(fmt, dict) else None
    if duration is None:
        duration = _as_positive_float(video.get("duration"))
    if duration is None:
        raise VideoAnalysisError("无法确定视频时长，已拒绝处理")

    return VideoMeta(
        duration=duration,
        width=_as_int(video.get("width")),
        height=_as_int(video.get("height")),
        has_audio=has_audio,
    )


def assert_within_limits(meta: VideoMeta, *, max_duration: int) -> None:
    if meta.duration > max_duration:
        raise VideoAnalysisError(f"视频时长 {meta.duration:.0f} 秒，超过上限 {max_duration} 秒")
    if meta.width * meta.height > MAX_SOURCE_PIXELS:
        raise VideoAnalysisError(f"视频分辨率 {meta.width}x{meta.height} 超过上限（最高 8K）")


async def probe(tools: MediaTools, workdir: Path) -> VideoMeta:
    result = await run_tool(build_probe_command(tools.ffprobe, tools.ffprobe_memory_args), cwd=workdir)
    if result.returncode != 0:
        _log_tool_failure("ffprobe", result)
        raise VideoAnalysisError("无法解析该文件：不是受支持的视频格式，或文件已损坏")
    return parse_probe_output(result.stdout)


def plan_uniform_timestamps(duration: float, max_frames: int) -> List[float]:
    """均匀采样，含首尾帧"""
    if max_frames <= 1:
        return [round(duration / 2, 3)]
    last = max(0.0, duration - min(_TAIL_MARGIN_SECONDS, duration / 2))
    count = min(max_frames, max(2, int(last // _MIN_SAMPLE_INTERVAL_SECONDS) + 1))
    return [round(last * index / (count - 1), 3) for index in range(count)]


def thin_evenly(values: Sequence[float], count: int) -> List[float]:
    """均匀抽取 count 个，保留首尾"""
    if len(values) <= count:
        return list(values)
    if count <= 1:
        return [values[0]]
    return [values[round(index * (len(values) - 1) / (count - 1))] for index in range(count)]


def parse_scene_times(stderr: bytes, duration: float) -> List[float]:
    """从 showinfo 的输出里取切点。时间钳到视频时长内：输出受文件内容影响，只当作 seek 位置用"""
    text = stderr.decode("utf-8", errors="replace")
    times = [float(match) for match in _SCENE_TIME_RE.findall(text)[:_MAX_SCENE_CUTS]]
    return sorted({round(t, 3) for t in times if 0 < t < duration})


def merge_scene_timestamps(cuts: Sequence[float], duration: float, max_frames: int) -> List[float]:
    """首帧 + 场景切点 + 尾帧；没有切点时回落到均匀采样"""
    if not cuts:
        return plan_uniform_timestamps(duration, max_frames)
    last = max(0.0, duration - min(_TAIL_MARGIN_SECONDS, duration / 2))
    merged: List[float] = [0.0]
    for cut in [*cuts, last]:
        if cut - merged[-1] >= _MIN_SCENE_GAP_SECONDS:
            merged.append(cut)
    return thin_evenly(merged, max_frames)


async def plan_timestamps(
    tools: MediaTools,
    workdir: Path,
    meta: VideoMeta,
    *,
    max_frames: int,
    scene_detection: bool,
    scene_threshold: float,
) -> List[float]:
    if not scene_detection:
        return plan_uniform_timestamps(meta.duration, max_frames)
    command = build_scene_command(
        tools.ffmpeg,
        threshold=scene_threshold,
        max_seconds=math.ceil(meta.duration),
        memory_args=tools.ffmpeg_memory_args,
    )
    result = await run_tool(command, cwd=workdir)
    if result.returncode != 0:
        _log_tool_failure("ffmpeg 场景检测", result)
        return plan_uniform_timestamps(meta.duration, max_frames)
    return merge_scene_timestamps(parse_scene_times(result.stderr, meta.duration), meta.duration, max_frames)


async def extract_frames(
    tools: MediaTools,
    workdir: Path,
    timestamps: Sequence[float],
    *,
    max_edge: int,
) -> List[Frame]:
    """逐个时间点 seek 抽帧。个别时间点取不到帧只跳过，一帧都没有才算失败"""
    frames: List[Frame] = []
    for index, timestamp in enumerate(timestamps):
        name = f"frame_{index:04d}.jpg"
        command = build_frame_command(
            tools.ffmpeg,
            timestamp=timestamp,
            max_edge=max_edge,
            output_name=name,
            memory_args=tools.ffmpeg_memory_args,
        )
        result = await run_tool(command, cwd=workdir)
        path = workdir / name
        if result.returncode != 0:
            _log_tool_failure("ffmpeg 抽帧", result)
        elif path.is_file() and path.stat().st_size > 0:
            frames.append(Frame(path=path, timestamp=timestamp))
    if not frames:
        raise VideoAnalysisError("未能从视频里抽取到任何画面，文件可能已损坏")
    return frames


async def extract_audio(tools: MediaTools, workdir: Path, *, max_seconds: int) -> Optional[Path]:
    """抽音轨；失败返回 None，由调用方在结果里说明"""
    command = build_audio_command(tools.ffmpeg, max_seconds=max_seconds, memory_args=tools.ffmpeg_memory_args)
    result = await run_tool(command, cwd=workdir)
    path = workdir / AUDIO_NAME
    if result.returncode != 0 or not path.is_file() or path.stat().st_size == 0:
        _log_tool_failure("ffmpeg 抽音轨", result)
        return None
    return path
