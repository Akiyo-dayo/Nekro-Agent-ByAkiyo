
"""空间清理服务

## 删除为什么不能按路径来

清理的目录里有几类是容器能写的：沙盒共享目录（`sandboxes/<container_key>`，挂进沙盒）、
沙盒的 pip 缓存与动态包目录，NapCat 的临时文件目录。收集要删的文件之后、真正 `unlink` 之前，
容器可以把路径中间任何一级目录换成指向宿主别处的符号链接；按路径 `unlink` 会顺着那个链接
删掉宿主上的任意文件（只看最后一级是不是链接挡不住）。

所以删除与清空目录一律从资源根目录（NA 自己建的那一级）出发，用 `safe_fs` 的目录句柄逐级
不跟随链接地打开，途中任何一级是链接就跳过这个文件；最后一级是链接只删链接本身。统计大小也
按 `lstat` 走同一条路，不跟着链接去量宿主别处的文件。收集阶段同样用目录句柄往下列
（`safe_fs.walk_regular_files`：链接不跟随，嵌套有深度上限）。

## 关于 NapCat

NapCat 容器读写挂着自己的 QQ 数据目录（临时文件目录在里面），数据目录本身只挂 `uploads`、只读
（内置 NapCat 的建容器参数与随项目发布的 compose 文件都是这样）。它的临时目录照样按上面的规则
清理。升级前建的 NapCat 还读写挂着整个 `DATA_DIR`，重建之前一个被攻破的 NapCat 能直接改写
数据目录里的任何东西，清理器这一层挡不住它。
"""

import asyncio
import json
import os
import stat
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import (
    APP_SYSTEM_DIR,
    MEMORY_PARSE_LOG_DIR,
    NAPCAT_TEMPFILE_DIR,
    PLUGIN_DYNAMIC_PACKAGE_DIR,
    PROMPT_ERROR_LOG_DIR,
    PROMPT_LOG_DIR,
    SANDBOX_PACKAGE_DIR,
    SANDBOX_PIP_CACHE_DIR,
    SANDBOX_SHARED_HOST_DIR,
    USER_UPLOAD_DIR,
    OsEnv,
)
from nekro_agent.schemas.space_cleanup import (
    CleanupProgress,
    CleanupRequest,
    CleanupResult,
    CleanupStatus,
    ResourceType,
)
from nekro_agent.services.workspace import safe_fs

# 清理任务缓存目录

logger = get_sub_logger("space_cleanup")
CLEANUP_CACHE_DIR = Path(APP_SYSTEM_DIR) / "space_cleanup" / "tasks"
CLEANUP_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _resource_base_dirs() -> List[Path]:
    """各类可清理资源的根目录（解析成真实路径）

    这些根目录本身是 NA 建的、容器换不掉的那一级，解析它们自己路径里的链接是安全的；
    收集文件时也是先把根目录 `resolve()` 再往下列，所以收集到的路径都落在这里某一个下面。
    """
    roots = (
        USER_UPLOAD_DIR,
        SANDBOX_SHARED_HOST_DIR,
        SANDBOX_PIP_CACHE_DIR,
        SANDBOX_PACKAGE_DIR,
        PLUGIN_DYNAMIC_PACKAGE_DIR,
        PROMPT_LOG_DIR,
        PROMPT_ERROR_LOG_DIR,
        MEMORY_PARSE_LOG_DIR,
        NAPCAT_TEMPFILE_DIR,
    )
    return [Path(root).resolve() for root in roots]


def _locate(file_path: Path, bases: Sequence[Path]) -> Optional[Tuple[Path, List[str], str]]:
    """把一个要删的文件拆成 `(资源根目录, 中间各级目录名, 文件名)`；不在任何资源根目录下返回 None

    候选路径只按字面展开、不解析链接；落在多个根目录下（`sandboxes` 与它下面的 `.pip_cache`）
    时取最具体的那个。
    """
    candidate = Path(os.path.abspath(str(file_path)))
    best: Optional[Tuple[Path, Path]] = None
    for base in bases:
        try:
            rel = candidate.relative_to(base)
        except ValueError:
            continue
        if best is None or len(base.parts) > len(best[0].parts):
            best = (base, rel)
    if best is None:
        return None
    parts = list(best[1].parts)
    if not parts:
        return None
    return best[0], parts[:-1], parts[-1]


def _normalize_datetime(dt: datetime) -> datetime:
    """规范化 datetime，确保是 timezone-naive（本地时间）

    Args:
        dt: datetime 对象

    Returns:
        datetime: timezone-naive 的 datetime
    """
    if dt.tzinfo is not None:
        # 如果是 timezone-aware，转换为本地时间（timezone-naive）
        return dt.astimezone().replace(tzinfo=None)
    return dt


def _remove_readonly(func, path, _):
    """错误处理函数，用于删除只读文件"""
    try:
        Path(path).chmod(stat.S_IWRITE)
        func(path)
    except Exception as e:
        logger.warning(f"无法删除文件 {path}: {e}")


class CleanupService:
    """空间清理服务"""

    def __init__(self):
        self._tasks: Dict[str, asyncio.Task] = {}
        self._task_progress: Dict[str, CleanupProgress] = {}
        self._task_results: Dict[str, CleanupResult] = {}

    async def create_cleanup_task(self, request: CleanupRequest) -> str:
        """创建清理任务

        Args:
            request: 清理请求

        Returns:
            str: 任务ID
        """
        task_id = str(uuid.uuid4())

        # 初始化进度
        progress = CleanupProgress(
            task_id=task_id,
            status=CleanupStatus.PENDING,
            progress=0.0,
            processed_files=0,
            total_files=0,
            freed_space=0,
            current_file=None,
            message=None,
        )
        self._task_progress[task_id] = progress
        await self._save_task_progress(task_id, progress)

        # 启动清理任务
        task = asyncio.create_task(self._execute_cleanup(task_id, request))
        self._tasks[task_id] = task

        return task_id

    async def get_cleanup_progress(self, task_id: str) -> Optional[CleanupProgress]:
        """获取清理进度

        Args:
            task_id: 任务ID

        Returns:
            Optional[CleanupProgress]: 清理进度，如果任务不存在则返回None
        """
        # 先从内存中获取
        if task_id in self._task_progress:
            return self._task_progress[task_id]

            # 从文件中读取
        progress_file = CLEANUP_CACHE_DIR / f"{task_id}_progress.json"
        if progress_file.exists():
            try:
                data = json.loads(progress_file.read_text(encoding="utf-8"))
                return CleanupProgress.model_validate(data)
            except Exception as e:
                logger.error(f"读取清理进度失败: {e}")

        return None

    async def get_cleanup_result(self, task_id: str) -> Optional[CleanupResult]:
        """获取清理结果

        Args:
            task_id: 任务ID

        Returns:
            Optional[CleanupResult]: 清理结果，如果任务未完成则返回None
        """
        # 先从内存中获取
        if task_id in self._task_results:
            return self._task_results[task_id]

        # 从文件中读取
        result_file = CLEANUP_CACHE_DIR / f"{task_id}_result.json"
        if result_file.exists():
            try:
                data = json.loads(result_file.read_text(encoding="utf-8"))
                return CleanupResult.model_validate(data)
            except Exception as e:
                logger.error(f"读取清理结果失败: {e}")

        return None

    async def _execute_cleanup(self, task_id: str, request: CleanupRequest) -> None:
        """执行清理任务

        Args:
            task_id: 任务ID
            request: 清理请求
        """
        start_time = datetime.now()
        deleted_files = 0
        failed_files = 0
        freed_space = 0
        failed_file_list: List[str] = []

        try:
            # 更新状态为运行中
            progress = self._task_progress[task_id]
            progress.status = CleanupStatus.RUNNING
            await self._save_task_progress(task_id, progress)

            # 收集要清理的文件
            files_to_clean = await self._collect_files_to_clean(request)
            total_files = len(files_to_clean)

            progress.total_files = total_files
            await self._save_task_progress(task_id, progress)

            logger.info(f"开始清理任务 {task_id}, 共 {total_files} 个文件")

            # 不支持时间过滤的资源类型（删除文件后需要清理空目录）
            no_time_filter_types = {
                ResourceType.SANDBOX_SHARED,
                ResourceType.SANDBOX_PIP_CACHE,
                ResourceType.SANDBOX_PACKAGES,
                ResourceType.PLUGIN_DYNAMIC_PACKAGES,
            }

            # 资源类型到目录的映射（用于清理空目录）
            resource_dirs = {
                ResourceType.USER_UPLOADS: Path(USER_UPLOAD_DIR),
                ResourceType.SANDBOX_SHARED: Path(SANDBOX_SHARED_HOST_DIR),
                ResourceType.SANDBOX_PIP_CACHE: Path(SANDBOX_PIP_CACHE_DIR),
                ResourceType.SANDBOX_PACKAGES: Path(SANDBOX_PACKAGE_DIR),
                ResourceType.PLUGIN_DYNAMIC_PACKAGES: Path(PLUGIN_DYNAMIC_PACKAGE_DIR),
                ResourceType.PROMPT_LOGS: Path(PROMPT_LOG_DIR),
                ResourceType.PROMPT_ERROR_LOGS: Path(PROMPT_ERROR_LOG_DIR),
                ResourceType.MEMORY_PARSE_LOGS: Path(MEMORY_PARSE_LOG_DIR),
                ResourceType.NAPCAT_TEMP: Path(NAPCAT_TEMPFILE_DIR),
            }

            # 逐个删除文件。删与量大小都从资源根目录逐级不跟随链接地走，见模块文档
            bases = _resource_base_dirs()
            for idx, file_path in enumerate(files_to_clean):
                progress.current_file = str(file_path)
                progress.processed_files = idx + 1
                progress.progress = (idx + 1) / total_files * 100 if total_files > 0 else 100

                if not request.dry_run:
                    try:
                        file_size = self._safe_file_size(file_path, bases)
                        success = self._safe_remove_file(file_path, bases)
                        if success:
                            deleted_files += 1
                            freed_space += file_size
                            progress.freed_space = freed_space
                        else:
                            failed_files += 1
                            failed_file_list.append(str(file_path))
                    except Exception as e:
                        logger.warning(f"删除文件失败 {file_path}: {e}")
                        failed_files += 1
                        failed_file_list.append(str(file_path))
                else:
                    # 模拟运行，只统计不删除
                    try:
                        file_size = self._safe_file_size(file_path, bases)
                        deleted_files += 1
                        freed_space += file_size
                        progress.freed_space = freed_space
                    except Exception as e:
                        logger.warning(f"统计文件失败 {file_path}: {e}")
                        failed_files += 1
                        failed_file_list.append(str(file_path))

                # 每处理10个文件保存一次进度
                if idx % 10 == 0:
                    await self._save_task_progress(task_id, progress)

            # 对于不支持时间过滤的资源类型，删除文件后清理空目录
            if not request.dry_run:
                for resource_type in request.resource_types:
                    if resource_type not in no_time_filter_types:
                        continue

                    if resource_type not in resource_dirs:
                        continue

                    base_directory = resource_dirs[resource_type]
                    if not base_directory.exists():
                        continue

                    try:
                        await self._cleanup_empty_subdirectories(base_directory)
                    except Exception as e:
                        logger.warning(f"清理空目录失败 {base_directory}: {e}")

            # 完成
            end_time = datetime.now()
            result = CleanupResult(
                task_id=task_id,
                status=CleanupStatus.COMPLETED,
                total_files=total_files,
                deleted_files=deleted_files,
                failed_files=failed_files,
                freed_space=freed_space,
                start_time=start_time,
                end_time=end_time,
                error_message=None,
                duration_seconds=(end_time - start_time).total_seconds(),
                failed_file_list=failed_file_list[:100],  # 最多保存100个失败文件
            )

            self._task_results[task_id] = result
            await self._save_task_result(task_id, result)

            progress.status = CleanupStatus.COMPLETED
            progress.progress = 100.0
            await self._save_task_progress(task_id, progress)

            logger.info(
                f"清理任务完成 {task_id}: 删除 {deleted_files}/{total_files} 个文件, "
                f"释放 {freed_space} 字节, 失败 {failed_files} 个",
            )

        except Exception as e:
            logger.exception(f"清理任务失败 {task_id}: {e}")

            # 保存失败结果
            end_time = datetime.now()
            result = CleanupResult(
                task_id=task_id,
                status=CleanupStatus.FAILED,
                total_files=0,
                deleted_files=deleted_files,
                failed_files=failed_files,
                freed_space=freed_space,
                start_time=start_time,
                end_time=end_time,
                duration_seconds=(end_time - start_time).total_seconds(),
                error_message=str(e),
                failed_file_list=failed_file_list[:100],
            )

            self._task_results[task_id] = result
            await self._save_task_result(task_id, result)

            progress = self._task_progress[task_id]
            progress.status = CleanupStatus.FAILED
            progress.message = str(e)
            await self._save_task_progress(task_id, progress)

    async def _collect_files_to_clean(self, request: CleanupRequest) -> List[Path]:
        """收集要清理的文件

        Args:
            request: 清理请求

        Returns:
            List[Path]: 文件路径列表
        """
        files_to_clean: List[Path] = []

        # 资源类型到目录的映射
        resource_dirs = {
            ResourceType.USER_UPLOADS: Path(USER_UPLOAD_DIR),
            ResourceType.SANDBOX_SHARED: Path(SANDBOX_SHARED_HOST_DIR),
            ResourceType.SANDBOX_PIP_CACHE: Path(SANDBOX_PIP_CACHE_DIR),
            ResourceType.SANDBOX_PACKAGES: Path(SANDBOX_PACKAGE_DIR),
            ResourceType.PLUGIN_DYNAMIC_PACKAGES: Path(PLUGIN_DYNAMIC_PACKAGE_DIR),
            ResourceType.PROMPT_LOGS: Path(PROMPT_LOG_DIR),
            ResourceType.PROMPT_ERROR_LOGS: Path(PROMPT_ERROR_LOG_DIR),
            ResourceType.MEMORY_PARSE_LOGS: Path(MEMORY_PARSE_LOG_DIR),
            ResourceType.NAPCAT_TEMP: Path(NAPCAT_TEMPFILE_DIR),
        }

        # 按chat_key组织的资源类型
        chat_based_types = {
            ResourceType.USER_UPLOADS,
            ResourceType.SANDBOX_SHARED,
        }

        data_dir = Path(OsEnv.DATA_DIR).resolve()

        for resource_type in request.resource_types:
            if resource_type not in resource_dirs:
                continue

            directory = resource_dirs[resource_type]
            if not directory.exists():
                continue

            # 验证路径在DATA_DIR内
            try:
                directory = directory.resolve()
                if not directory.is_relative_to(data_dir):
                    logger.warning(f"跳过非法路径: {directory}")
                    continue
            except Exception as e:
                logger.warning(f"路径验证失败 {directory}: {e}")
                continue

            # 按chat_key组织的资源
            if resource_type in chat_based_types:
                files = await self._collect_chat_based_files(directory, request)
            else:
                files = await self._collect_simple_files(directory, request)

            files_to_clean.extend(files)

        return files_to_clean

    async def _collect_chat_based_files(self, directory: Path, request: CleanupRequest) -> List[Path]:
        """收集按chat_key组织的文件

        Args:
            directory: 目录路径
            request: 清理请求

        Returns:
            List[Path]: 文件路径列表
        """
        files: List[Path] = []

        try:
            for chat_dir in directory.iterdir():
                if not chat_dir.is_dir():
                    continue

                chat_key = chat_dir.name

                # 如果是沙盒共享目录，跳过 .pip_cache 和 .packages
                if directory.name == "sandboxes" and chat_key in {".pip_cache", ".packages"}:
                    continue

                # 检查chat_key过滤
                if request.chat_keys and chat_key not in request.chat_keys:
                    continue

                # 收集该聊天目录下的文件：目录句柄往下列，不跟随链接（链接本身也收进来，删的时候
                # 只删链接），时间按 `lstat` 判，嵌套有深度上限
                for file_path, file_stat in safe_fs.walk_regular_files(chat_dir, include_links=True):
                    if request.before_date:
                        file_mtime = datetime.fromtimestamp(file_stat.st_mtime)
                        if file_mtime >= _normalize_datetime(request.before_date):
                            continue
                    files.append(file_path)

        except Exception as e:
            logger.warning(f"收集文件失败 {directory}: {e}")

        return files

    async def _collect_simple_files(self, directory: Path, request: CleanupRequest) -> List[Path]:
        """收集简单目录下的文件

        Args:
            directory: 目录路径
            request: 清理请求

        Returns:
            List[Path]: 文件路径列表
        """
        files: List[Path] = []

        try:
            # 同 `_collect_chat_based_files`：目录句柄往下列，不跟随链接，时间按 `lstat` 判
            for file_path, file_stat in safe_fs.walk_regular_files(directory, include_links=True):
                if request.before_date:
                    file_mtime = datetime.fromtimestamp(file_stat.st_mtime)
                    if file_mtime >= _normalize_datetime(request.before_date):
                        continue
                files.append(file_path)

        except Exception as e:
            logger.warning(f"收集文件失败 {directory}: {e}")

        return files

    def _safe_remove_file(self, file_path: Path, bases: Optional[Sequence[Path]] = None) -> bool:
        """从资源根目录出发逐级不跟随链接地删一个文件

        途中任何一级目录是链接（收集之后被换掉的）就不删、返回 False；最后一级是链接只删
        链接本身；已经不在了算删成功。不在任何资源根目录下的路径一律不删。

        Args:
            file_path: 收集阶段得到的文件路径
            bases: 资源根目录；不给时现算（见 `_resource_base_dirs`）

        Returns:
            bool: 是否成功删除
        """
        located = _locate(file_path, bases if bases is not None else _resource_base_dirs())
        if located is None:
            logger.warning(f"跳过不在任何清理目录下的路径: {file_path}")
            return False
        base, parents, name = located
        try:
            # 删掉了、或者已经不在了，都算这个文件清理完了
            safe_fs.remove_file(base, [*parents, name], allow_chmod=True)
        except safe_fs.UnsafePathError as e:
            logger.warning(f"清理时路径上有符号链接或不是普通文件，没有删（不跟随链接）: {file_path}: {e}")
            return False
        except Exception as e:
            logger.warning(f"删除文件失败 {file_path}: {e}")
            return False
        return True

    def _safe_file_size(self, file_path: Path, bases: Optional[Sequence[Path]] = None) -> int:
        """量一个要删的文件有多大：逐级不跟随链接，链接与读不了的一律按 0 算"""
        located = _locate(file_path, bases if bases is not None else _resource_base_dirs())
        if located is None:
            return 0
        base, parents, name = located
        try:
            with safe_fs.open_dir(base, parents) as directory:
                if directory.kind(name) != "file":
                    return 0
                st = directory.lstat(name)
                return int(st.st_size) if st is not None else 0
        except (OSError, ValueError):
            return 0

    async def _cleanup_empty_subdirectories(self, base_directory: Path) -> None:
        """清理基础目录下的空子目录（不包括基础目录本身）

        从基础目录出发用目录句柄往下走，链接不进去也不删；按路径 `rmdir` 的话，中间某一级
        被换成链接就会删到宿主别处的空目录。

        Args:
            base_directory: 基础目录路径
        """
        if not base_directory.exists():
            return

        try:
            safe_fs.remove_empty_dirs(base_directory.resolve())
        except Exception as e:
            logger.warning(f"清理空目录失败 {base_directory}: {e}")

    async def _cleanup_empty_directories(self, request: CleanupRequest) -> None:
        """清理空目录

        Args:
            request: 清理请求
        """
        resource_dirs = {
            ResourceType.USER_UPLOADS: Path(USER_UPLOAD_DIR),
            ResourceType.SANDBOX_SHARED: Path(SANDBOX_SHARED_HOST_DIR),
            ResourceType.SANDBOX_PIP_CACHE: Path(SANDBOX_PIP_CACHE_DIR),
            ResourceType.SANDBOX_PACKAGES: Path(SANDBOX_PACKAGE_DIR),
            ResourceType.PLUGIN_DYNAMIC_PACKAGES: Path(PLUGIN_DYNAMIC_PACKAGE_DIR),
            ResourceType.PROMPT_LOGS: Path(PROMPT_LOG_DIR),
            ResourceType.PROMPT_ERROR_LOGS: Path(PROMPT_ERROR_LOG_DIR),
            ResourceType.MEMORY_PARSE_LOGS: Path(MEMORY_PARSE_LOG_DIR),
            ResourceType.NAPCAT_TEMP: Path(NAPCAT_TEMPFILE_DIR),
        }

        for resource_type in request.resource_types:
            if resource_type not in resource_dirs:
                continue

            directory = resource_dirs[resource_type]
            if not directory.exists():
                continue

            try:
                await self._cleanup_empty_subdirectories(directory)
            except Exception as e:
                logger.warning(f"清理空目录失败 {directory}: {e}")

    async def _save_task_progress(self, task_id: str, progress: CleanupProgress) -> None:
        """保存任务进度

        Args:
            task_id: 任务ID
            progress: 清理进度
        """
        try:
            progress_file = CLEANUP_CACHE_DIR / f"{task_id}_progress.json"
            progress_file.write_text(
                json.dumps(progress.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"保存任务进度失败: {e}")

    async def _save_task_result(self, task_id: str, result: CleanupResult) -> None:
        """保存任务结果

        Args:
            task_id: 任务ID
            result: 清理结果
        """
        try:
            result_file = CLEANUP_CACHE_DIR / f"{task_id}_result.json"
            result_file.write_text(
                json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"保存任务结果失败: {e}")


# 全局清理服务实例
cleanup_service = CleanupService()
