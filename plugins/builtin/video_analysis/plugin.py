"""插件声明与配置

限额字段标了 `overridable=True`，租户能写自己实例的覆盖层，而 CPU、磁盘、带宽是整个进程共用的，
所以每个限额都有写死的硬上限：`main.compute_limits` 在使用时把取值钳进 `HARD_RANGES`，
而且实例覆盖只能收紧，取 `min(覆盖, 全局)`。
两个例外：`FRAMES_PER_BATCH` 取 `max(覆盖, 全局)`（批越小请求越多，花的是平台方的模型费用，所以只许调大）；
`ENABLE_SCENE_DETECTION` / `ENABLE_TRANSCRIPTION` 全局关着时实例开不了（转写会让音频离开本机，决定权在平台方）。
不用 pydantic 的 `le=`：WebUI 写配置走 `setattr`，不校验，越界值落盘之后下次启动 `load_config` 抛异常，
插件直接加载失败。钳制没有这个问题，租户把 `MAX_FRAMES` 写成 10000 拿到的也只是 64。

`MAX_CONCURRENT_JOBS` / `MAX_JOBS_PER_INSTANCE` 不标 overridable：并发槽是进程级的，让某个实例改它等于让这个实例决定
别的实例还能不能用（与 `PLUGIN_CALL_TIMEOUT_SECONDS` 不标 overridable 同一条理由）。
"""

from typing import Dict, Tuple

from pydantic import Field

from nekro_agent.api import i18n
from nekro_agent.api.plugin import ConfigBase, ExtraField, NekroPlugin

# 字段名 -> (下限, 硬上限)
HARD_RANGES: Dict[str, Tuple[float, float]] = {
    "MAX_VIDEO_SIZE_MB": (1, 512),
    "MAX_VIDEO_DURATION_SECONDS": (1, 3600),
    "MAX_FRAMES": (1, 64),
    "FRAME_MAX_EDGE": (64, 1568),
    "FRAMES_PER_BATCH": (1, 32),
    "TOTAL_TIMEOUT_SECONDS": (10, 600),
    "MAX_CONCURRENT_JOBS": (1, 16),
    "MAX_JOBS_PER_INSTANCE": (1, 16),
    "MAX_REDIRECTS": (0, 10),
    "SCENE_THRESHOLD": (0.01, 0.99),
    "MAX_TRANSCRIPT_CHARS": (100, 32000),
}

plugin = NekroPlugin(
    name="视频读取与分析",
    module_name="video_analysis",
    description="让 AI 读取并分析聊天里的视频、沙盒文件或视频链接：抽帧后交给视觉模型，可选转写音轨",
    version="0.1.0",
    author="Akiyo_dayo",
    url="https://github.com/Akiyo-dayo/Nekro-Agent-ByAkiyo",
    i18n_name=i18n.i18n_text(
        zh_CN="视频读取与分析",
        en_US="Video Analysis",
    ),
    i18n_description=i18n.i18n_text(
        zh_CN="让 AI 读取并分析聊天里的视频、沙盒文件或视频链接：抽帧后交给视觉模型，可选转写音轨",
        en_US="Lets the AI read and analyze videos from chat, sandbox files or links: frames go to a vision model, the audio track can optionally be transcribed",
    ),
    allow_sleep=True,
    sleep_brief="用于读取并分析视频内容，仅在用户发来视频或视频链接、且需要理解其内容时激活。",
)


@plugin.mount_config()
class VideoAnalysisConfig(ConfigBase):
    """视频分析配置"""

    VIDEO_MODEL_GROUP: str = Field(
        default="",
        title="视频分析模型组",
        description="用于看帧的视觉模型组。留空时依次尝试当前频道的主模型组与备用模型组，二者都没有视觉能力时报错",
        json_schema_extra=ExtraField(
            ref_model_groups=True,
            model_type="chat",
            overridable=True,
            i18n_title=i18n.i18n_text(zh_CN="视频分析模型组", en_US="Video Analysis Model Group"),
            i18n_description=i18n.i18n_text(
                zh_CN="用于看帧的视觉模型组。留空时依次尝试当前频道的主模型组与备用模型组，二者都没有视觉能力时报错",
                en_US="Vision model group used for the frames. When empty, the channel's primary and fallback model groups are tried in that order; an error is raised if neither has vision",
            ),
        ).model_dump(),
    )
    MAX_VIDEO_SIZE_MB: int = Field(
        default=64,
        title="单个视频大小上限 (MB)",
        description="超过即拒绝；下载链接时边下边计数，超限立刻中止。硬上限 512",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_title=i18n.i18n_text(zh_CN="单个视频大小上限 (MB)", en_US="Max Video Size (MB)"),
            i18n_description=i18n.i18n_text(
                zh_CN="超过即拒绝；下载链接时边下边计数，超限立刻中止。硬上限 512",
                en_US="Larger videos are refused; downloads are counted while streaming and aborted at the limit. Hard ceiling 512",
            ),
        ).model_dump(),
    )
    MAX_VIDEO_DURATION_SECONDS: int = Field(
        default=600,
        title="单个视频时长上限 (秒)",
        description="超过即拒绝。硬上限 3600",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_title=i18n.i18n_text(zh_CN="单个视频时长上限 (秒)", en_US="Max Video Duration (s)"),
            i18n_description=i18n.i18n_text(
                zh_CN="超过即拒绝。硬上限 3600",
                en_US="Longer videos are refused. Hard ceiling 3600",
            ),
        ).model_dump(),
    )
    MAX_FRAMES: int = Field(
        default=16,
        title="单次抽帧上限",
        description="默认均匀采样并包含首尾帧；这个数直接决定视觉模型的花费。硬上限 64",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_title=i18n.i18n_text(zh_CN="单次抽帧上限", en_US="Max Frames"),
            i18n_description=i18n.i18n_text(
                zh_CN="默认均匀采样并包含首尾帧；这个数直接决定视觉模型的花费。硬上限 64",
                en_US="Frames are sampled evenly, first and last included; this number directly drives the vision model's cost. Hard ceiling 64",
            ),
        ).model_dump(),
    )
    FRAME_MAX_EDGE: int = Field(
        default=768,
        title="单帧最大边长 (像素)",
        description="抽出的帧按比例缩小到长边不超过此值，只缩不放。硬上限 1568",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_title=i18n.i18n_text(zh_CN="单帧最大边长 (像素)", en_US="Max Frame Edge (px)"),
            i18n_description=i18n.i18n_text(
                zh_CN="抽出的帧按比例缩小到长边不超过此值，只缩不放。硬上限 1568",
                en_US="Frames are scaled down so the long edge fits this value; they are never scaled up. Hard ceiling 1568",
            ),
        ).model_dump(),
    )
    FRAMES_PER_BATCH: int = Field(
        default=8,
        title="每批发送帧数",
        description="每次请求视觉模型最多带多少帧；多于一批时再做一次汇总，所以调小会多一次请求。实例覆盖只能调大，不能比全局值小。硬上限 32",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_title=i18n.i18n_text(zh_CN="每批发送帧数", en_US="Frames per Batch"),
            i18n_description=i18n.i18n_text(
                zh_CN="每次请求视觉模型最多带多少帧；多于一批时再做一次汇总，所以调小会多一次请求。实例覆盖只能调大，不能比全局值小。硬上限 32",
                en_US="Most frames sent in one vision request; more than one batch adds a summary request, so lowering this adds requests. An instance override can only raise it, never go below the global value. Hard ceiling 32",
            ),
        ).model_dump(),
    )
    TOTAL_TIMEOUT_SECONDS: int = Field(
        default=100,
        title="单次分析总超时 (秒)",
        description="含下载、抽帧、转写与全部模型请求。实际取值不超过系统的沙盒超时减 10 秒：沙盒方法运行在沙盒容器的一次执行里，容器被杀之后结果无人接收",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_title=i18n.i18n_text(zh_CN="单次分析总超时 (秒)", en_US="Total Timeout (s)"),
            i18n_description=i18n.i18n_text(
                zh_CN="含下载、抽帧、转写与全部模型请求。实际取值不超过系统的沙盒超时减 10 秒：沙盒方法运行在沙盒容器的一次执行里，容器被杀之后结果无人接收",
                en_US="Covers download, frame extraction, transcription and every model request. Capped at the sandbox timeout minus 10 s, because nobody receives the result once the sandbox container is gone",
            ),
        ).model_dump(),
    )
    MAX_CONCURRENT_JOBS: int = Field(
        default=2,
        title="同时进行的分析数",
        description="全进程共用，满了直接拒绝而不排队。不可按实例覆盖。硬上限 16",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(zh_CN="同时进行的分析数", en_US="Max Concurrent Jobs"),
            i18n_description=i18n.i18n_text(
                zh_CN="全进程共用，满了直接拒绝而不排队。不可按实例覆盖。硬上限 16",
                en_US="Shared by the whole process; further calls are refused rather than queued. Cannot be overridden per instance. Hard ceiling 16",
            ),
        ).model_dump(),
    )
    MAX_JOBS_PER_INSTANCE: int = Field(
        default=1,
        title="单个实例同时进行的分析数",
        description="防止一个实例占满全部并发。满了直接拒绝而不排队。不可按实例覆盖。硬上限 16",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(zh_CN="单个实例同时进行的分析数", en_US="Max Concurrent Jobs per Instance"),
            i18n_description=i18n.i18n_text(
                zh_CN="防止一个实例占满全部并发。满了直接拒绝而不排队。不可按实例覆盖。硬上限 16",
                en_US="Keeps one instance from taking every slot; further calls are refused rather than queued. Cannot be overridden per instance. Hard ceiling 16",
            ),
        ).model_dump(),
    )
    MAX_REDIRECTS: int = Field(
        default=3,
        title="下载链接最大重定向次数",
        description="每一跳都重新过一次出站校验。硬上限 10",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_title=i18n.i18n_text(zh_CN="下载链接最大重定向次数", en_US="Max Download Redirects"),
            i18n_description=i18n.i18n_text(
                zh_CN="每一跳都重新过一次出站校验。硬上限 10",
                en_US="Every hop goes through the outbound check again. Hard ceiling 10",
            ),
        ).model_dump(),
    )
    ENABLE_SCENE_DETECTION: bool = Field(
        default=False,
        title="启用场景切分抽帧",
        description="开启后按镜头切换选帧，要完整解码一遍视频，比均匀采样慢；检测不到切换时回落到均匀采样。全局关闭时实例不能自行开启",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_title=i18n.i18n_text(zh_CN="启用场景切分抽帧", en_US="Enable Scene Detection"),
            i18n_description=i18n.i18n_text(
                zh_CN="开启后按镜头切换选帧，要完整解码一遍视频，比均匀采样慢；检测不到切换时回落到均匀采样。全局关闭时实例不能自行开启",
                en_US="Picks frames at scene changes. Decodes the whole video, so it is slower than even sampling; falls back to even sampling when no change is found. An instance cannot turn it on while it is off globally",
            ),
        ).model_dump(),
    )
    SCENE_THRESHOLD: float = Field(
        default=0.3,
        title="场景切分阈值",
        description="0~1，越大越不敏感",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_title=i18n.i18n_text(zh_CN="场景切分阈值", en_US="Scene Change Threshold"),
            i18n_description=i18n.i18n_text(
                zh_CN="0~1，越大越不敏感",
                en_US="Between 0 and 1; higher is less sensitive",
            ),
        ).model_dump(),
    )
    ENABLE_TRANSCRIPTION: bool = Field(
        default=False,
        title="启用音轨转写",
        description="开启后抽出音轨，发给转写模型组。音频会离开本机，默认关闭。全局关闭时实例不能自行开启",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_title=i18n.i18n_text(zh_CN="启用音轨转写", en_US="Enable Audio Transcription"),
            i18n_description=i18n.i18n_text(
                zh_CN="开启后抽出音轨，发给转写模型组。音频会离开本机，默认关闭。全局关闭时实例不能自行开启",
                en_US="Extracts the audio track and sends it to the transcription model group. Audio leaves this host, so it is off by default. An instance cannot turn it on while it is off globally",
            ),
        ).model_dump(),
    )
    TRANSCRIBE_MODEL_GROUP: str = Field(
        default="",
        title="转写模型组",
        description="OpenAI 兼容转写接口（POST {BASE_URL}/audio/transcriptions）。模型组类型选聊天，模型名填转写模型，如 whisper-1。留空时跳过转写并在结果里说明",
        json_schema_extra=ExtraField(
            ref_model_groups=True,
            model_type="chat",
            overridable=True,
            i18n_title=i18n.i18n_text(zh_CN="转写模型组", en_US="Transcription Model Group"),
            i18n_description=i18n.i18n_text(
                zh_CN="OpenAI 兼容转写接口（POST {BASE_URL}/audio/transcriptions）。模型组类型选聊天，模型名填转写模型，如 whisper-1。留空时跳过转写并在结果里说明",
                en_US="OpenAI-compatible transcription endpoint (POST {BASE_URL}/audio/transcriptions). Use a chat-type model group whose model name is the transcription model, such as whisper-1. When empty, transcription is skipped and the result says so",
            ),
        ).model_dump(),
    )
    MAX_TRANSCRIPT_CHARS: int = Field(
        default=8000,
        title="转写文本长度上限",
        description="进入汇总提示词的转写文本最多保留多少字符。硬上限 32000",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_title=i18n.i18n_text(zh_CN="转写文本长度上限", en_US="Max Transcript Length"),
            i18n_description=i18n.i18n_text(
                zh_CN="进入汇总提示词的转写文本最多保留多少字符。硬上限 32000",
                en_US="Most characters of the transcript kept for the summary prompt. Hard ceiling 32000",
            ),
        ).model_dump(),
    )
