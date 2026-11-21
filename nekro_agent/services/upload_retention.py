"""上传图片保留期：按天清理聊天上传目录里的过期图片

## 开关

`UPLOADS_IMAGE_RETENTION_DAYS`（系统配置，平台方专属，0 = 关，默认 0）。只读全局配置：
这一项不标 `overridable`，实例 / 频道覆盖层里没有它；租户读写系统配置时它被当作平台方边界项
挡掉（`routers/config.py` 的 `PLATFORM_ONLY_SYSTEM_CONFIG_KEYS`）。

关着的时候什么都不做：不扫描、不删、不写日志。

## 清什么

上传目录（`USER_UPLOAD_DIR`）的布局：

- `<规整后的 chat_key>/…`：各频道（含网页会话）入站的图片与文件、Bot 发出的图片、命令输出物化的附件；
- 根目录下的散文件：没带频道的下载与落盘；
- `email_attachment/<账户>/<邮件 UID>/…`：邮件附件。

只删扩展名在 `IMAGE_SUFFIXES` 里（不分大小写）、修改时间早于「现在 - N 天」的普通文件，
其它类型（视频、文档、压缩包……）一律不动。邮件附件整棵跳过（`EXCLUDED_TOP_LEVEL_DIRS`）：
邮件页按邮件记录列附件，文件没了附件就从列表里消失，而它不是这个开关要管的聊天图片。

## 不越界

- 不跟随符号链接：链接本身不删，指向目录的链接不进；Windows 的目录联接同样不进。
- 根目录先取一次 realpath（部署方把数据目录放在链接后面是正常做法），之后每进一个子目录都确认
  它的 realpath 仍在根目录里，不在就整棵跳过。这兜住了目录联接和扫描途中目录被换成链接的情形；
  「确认之后、删除之前」这一瞬间再被换掉的竞态没有兜（要做到得改用目录 fd 逐级打开），
  能写上传目录的只有 NA 自己（沙盒里是只读挂载），不值得为它换一套平台相关的实现。

## 怎么跑

- 进程启动后等 `STARTUP_DELAY_SECONDS` 先跑一次，之后每 `RUN_INTERVAL_SECONDS`（一天）一次。
  循环每 `TICK_SECONDS` 醒来看一眼开关，所以运行中打开也会在一个 tick 内开始，不用重启；
  跑到一半被关掉，在下一批之前停下。
- 扫描和删除都在线程里，每次 `asyncio.to_thread` 只处理 `BATCH_SIZE` 个目录项，不阻塞事件循环。
- 单个文件删不掉记一条警告（每轮最多 `MAX_FAILURE_WARNINGS` 条，再多只计数），接着删别的。
- 每轮结束写一行 INFO：删了多少个、多少字节、失败几个、用了多久；不打印文件名。

## 影响

超过保留期的图片在控制台聊天记录、网页会话里显示不出来（取件接口 404），沙盒里 `/app/uploads`
下也读不到；拼 Agent 上下文时文件不存在的历史图片本来就会跳过。表情包收藏在收藏时已复制到插件
自己的数据目录，知识库、记忆、人设头像都不在上传目录里，不受影响。
"""

from __future__ import annotations

import asyncio
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import USER_UPLOAD_DIR

logger = get_sub_logger("space_cleanup")

# 按扩展名认图片（小写、带点）。名字由内容嗅探得出的文件后缀是 MIME 子类型（`.jpeg`、`.webp`），
# 平台原名保留的多是 `.jpg` / `.png` / `.gif`
IMAGE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".jfif", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif", ".avif", ".tif", ".tiff"},
)
# 根目录下整棵跳过的子目录
EXCLUDED_TOP_LEVEL_DIRS = frozenset({"email_attachment"})
# Windows 目录联接的 reparse tag（`stat.IO_REPARSE_TAG_MOUNT_POINT`，取不到时用它的值）
_IO_REPARSE_TAG_MOUNT_POINT: int = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)

STARTUP_DELAY_SECONDS = 300.0
RUN_INTERVAL_SECONDS = 86400.0
TICK_SECONDS = 600.0
BATCH_SIZE = 500
MAX_FAILURE_WARNINGS = 20
SECONDS_PER_DAY = 86400


def current_retention_days() -> int:
    """当前保留天数；0、负数或写坏的值都按关处理"""
    try:
        days = int(config.UPLOADS_IMAGE_RETENTION_DAYS)
    except (TypeError, ValueError):
        return 0
    return max(days, 0)


@dataclass
class SweepStats:
    """一轮清理的结果"""

    deleted_files: int = 0
    deleted_bytes: int = 0
    failed_files: int = 0
    stopped_early: bool = False


def _resolve_root(root: Path) -> Optional[Path]:
    """根目录的 realpath；不存在或不是目录时返回 None"""
    try:
        real = Path(os.path.realpath(root))
    except OSError:
        return None
    return real if real.is_dir() else None


class _ImageSweep:
    """一轮清理的扫描状态

    `step()` 在线程里调用，每次最多处理 `batch_size` 个目录项，处理完返回，让出事件循环；
    返回 True 表示整棵树扫完了。目录按深度优先逐个读，同一时刻只在内存里留一个目录的条目。
    """

    def __init__(self, root: Path, cutoff: float, batch_size: int = BATCH_SIZE) -> None:
        self._root = root
        self._cutoff = cutoff
        self._batch_size = max(1, batch_size)
        # (目录路径, 是否根目录)
        self._pending_dirs: List[Tuple[str, bool]] = [(str(root), True)]
        self._entries: List[os.DirEntry[str]] = []
        self._cursor = 0
        self._in_root = False
        self._warned = 0
        self.stats = SweepStats()

    def step(self) -> bool:
        examined = 0
        while examined < self._batch_size:
            if self._cursor >= len(self._entries):
                if not self._pending_dirs:
                    return True
                self._open_next_dir()
                continue
            entry = self._entries[self._cursor]
            self._cursor += 1
            examined += 1
            self._handle_entry(entry)
        return False

    def _open_next_dir(self) -> None:
        path, is_root = self._pending_dirs.pop()
        self._entries = []
        self._cursor = 0
        self._in_root = is_root
        if not is_root and not self._within_root(path):
            logger.warning(f"上传图片保留期：子目录实际指向上传目录之外，已跳过: {self._relative(path)}")
            return
        try:
            with os.scandir(path) as iterator:
                self._entries = list(iterator)
        except OSError as exc:
            logger.warning(f"上传图片保留期：读不了目录，已跳过: {self._relative(path)} | {type(exc).__name__}")

    def _within_root(self, path: str) -> bool:
        try:
            real = Path(os.path.realpath(path))
        except OSError:
            return False
        return real.is_relative_to(self._root)

    def _relative(self, path: str) -> str:
        try:
            return str(Path(path).relative_to(self._root))
        except ValueError:
            return Path(path).name

    def _handle_entry(self, entry: os.DirEntry[str]) -> None:
        try:
            if entry.is_symlink():
                return
            if entry.is_dir(follow_symlinks=False):
                # 目录联接在 Windows 上不算符号链接，`is_dir(follow_symlinks=False)` 照样为真
                if _is_junction(entry):
                    return
                if not (self._in_root and entry.name in EXCLUDED_TOP_LEVEL_DIRS):
                    self._pending_dirs.append((entry.path, False))
                return
            if not entry.is_file(follow_symlinks=False):
                return
            if os.path.splitext(entry.name)[1].lower() not in IMAGE_SUFFIXES:
                return
            info = entry.stat(follow_symlinks=False)
        except OSError:
            return
        if not stat.S_ISREG(info.st_mode) or info.st_mtime >= self._cutoff:
            return

        try:
            os.unlink(entry.path)
        except FileNotFoundError:
            # 扫描之后被别处删掉了，结果一样，不算失败
            return
        except OSError as exc:
            self.stats.failed_files += 1
            if self._warned < MAX_FAILURE_WARNINGS:
                self._warned += 1
                logger.warning(f"上传图片保留期：删除失败，跳过: {self._relative(entry.path)} | {type(exc).__name__}: {exc.strerror}")
            return
        self.stats.deleted_files += 1
        self.stats.deleted_bytes += info.st_size


def _is_junction(entry: os.DirEntry[str]) -> bool:
    """这个目录项是不是 Windows 目录联接

    Python 3.12 起 `DirEntry` 才有 `is_junction`；3.11 在 Windows 上看不跟随链接的 stat 里的
    reparse tag（`DirEntry` 在 Windows 上缓存了这份信息，不多一次系统调用）。其它平台没有联接。
    """
    checker = getattr(entry, "is_junction", None)
    if callable(checker):
        return bool(checker())
    if os.name != "nt":
        return False
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    return getattr(info, "st_reparse_tag", 0) == _IO_REPARSE_TAG_MOUNT_POINT


class UploadImageRetention:
    """上传图片保留期的后台循环

    进程内记一个「上次跑的时间」（单调时钟）：重启之后是空的，所以启动延迟过后会先跑一次。
    这一轮开跑时就记下时间，跑出异常也等到明天：能漏出循环的异常是根目录读不了这一类，
    十分钟后再试只会多一条一样的警告。
    """

    def __init__(self) -> None:
        self._task: Optional["asyncio.Task[None]"] = None
        self._running = False
        self._lock = asyncio.Lock()
        self._last_run_at: Optional[float] = None

    def reset(self) -> None:
        """忘掉上次跑的时间（**仅供测试**）"""
        self._last_run_at = None

    async def run_once(self, *, root: Optional[Path] = None, now: Optional[float] = None) -> Optional[SweepStats]:
        """按当前配置清一轮；开关关着时什么都不做，返回 None

        Args:
            root: 上传目录，默认 `USER_UPLOAD_DIR`（用例传临时目录）
            now: 计算截止时间用的「现在」（墙上时钟秒数），默认 `time.time()`
        """
        days = current_retention_days()
        if days <= 0:
            return None

        async with self._lock:
            started = time.monotonic()
            real_root = await asyncio.to_thread(_resolve_root, Path(USER_UPLOAD_DIR) if root is None else Path(root))
            if real_root is None:
                logger.debug("上传图片保留期：上传目录不存在，本轮不用清")
                return SweepStats()

            cutoff = (time.time() if now is None else now) - days * SECONDS_PER_DAY
            sweep = _ImageSweep(real_root, cutoff, BATCH_SIZE)
            while not await asyncio.to_thread(sweep.step):
                if current_retention_days() <= 0:
                    sweep.stats.stopped_early = True
                    break

            stats = sweep.stats
            elapsed = time.monotonic() - started
            suffix = "；开关中途被关闭，本轮提前结束" if stats.stopped_early else ""
            logger.info(
                f"上传图片保留期清理完成（保留 {days} 天）：删除 {stats.deleted_files} 个图片、"
                f"{stats.deleted_bytes} 字节（{stats.deleted_bytes / 1024 / 1024:.1f} MB），"
                f"失败 {stats.failed_files} 个，耗时 {elapsed:.1f} 秒{suffix}",
            )
            return stats

    async def tick(self) -> Optional[SweepStats]:
        """循环每次醒来做的事：开关开着、且距上次已满一天（或还没跑过）就跑一轮"""
        if current_retention_days() <= 0:
            return None
        now = time.monotonic()
        if self._last_run_at is not None and now - self._last_run_at < RUN_INTERVAL_SECONDS:
            return None
        self._last_run_at = now
        return await self.run_once()

    # ==== 后台循环 ====

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        # 先等一会儿：启动时迁移、插件加载、向量库都在抢磁盘
        await asyncio.sleep(STARTUP_DELAY_SECONDS)
        while self._running:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - 清理失败不能打掉后台循环
                logger.warning(f"上传图片保留期清理出错（明天再试）: {type(exc).__name__}: {exc}")
            await asyncio.sleep(TICK_SECONDS)


upload_image_retention = UploadImageRetention()


__all__ = [
    "BATCH_SIZE",
    "EXCLUDED_TOP_LEVEL_DIRS",
    "IMAGE_SUFFIXES",
    "MAX_FAILURE_WARNINGS",
    "RUN_INTERVAL_SECONDS",
    "STARTUP_DELAY_SECONDS",
    "TICK_SECONDS",
    "SweepStats",
    "UploadImageRetention",
    "current_retention_days",
    "upload_image_retention",
]
