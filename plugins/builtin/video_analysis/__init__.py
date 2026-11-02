"""
# 视频读取与分析 (video_analysis)

让 AI 读取并分析视频：抽帧交给视觉模型，可选转写音轨，把结果以文字返回给主 Agent。

## 沙盒方法

- `analyze_video(source, question)`: `source` 是聊天里 OneBot 视频的 `message:<编号>`、
  收到的文件路径（`/app/uploads/...`）、沙盒共享目录路径（`/app/shared/...`）或 http(s) 链接。

## 处理流程

1. 把来源落成隔离工作目录里的一份私有副本（链接走现有出站防护，每一跳重定向都校验，边下边计数）。
2. `ffprobe` 读元数据，查时长与分辨率限额。
3. 默认均匀采样并含首尾帧，可选场景切分。逐个时间点 seek 抽帧，不解码全片。
4. 可选：抽音轨，走配置的转写模型组（OpenAI 兼容 `/audio/transcriptions`）。
5. 分批发给视觉模型，多于一批或有转写时再汇总一次。
6. 工作目录在 `finally` 里删除，异常路径同样删除。

## 配置

全部在插件配置里，见 `plugin.py`。限额都有保守默认值与不可越过的硬上限，实例覆盖只能收紧；
`MAX_CONCURRENT_JOBS` 与 `MAX_JOBS_PER_INSTANCE` 是进程级的，不可覆盖。

## 依赖

需要 PATH 上有 `ffmpeg` 与 `ffprobe`，官方镜像已内置。缺失时插件在调用时报错，不影响启动。

## 原生视频模型

本期不做：一律抽帧走通用多模态。要接时在 `inference.analyze_frames` 之前加一条分支，
按模型组能力把原视频直接发给上游，抽帧链路是回落路径，配置与限额不用动。
"""

from . import main
from .plugin import plugin

__all__ = ["main", "plugin"]
