"""空间清理路由 —— 整组平台方专属

## 为什么这里没有"租户作用域"这一档

本模块操作的是**宿主机上的共享目录**，一个都切不开：
`USER_UPLOAD_DIR`（全部租户的聊天文件）、`SANDBOX_SHARED_HOST_DIR`（全部工作区）、
`SANDBOX_PIP_CACHE_DIR`、`PLUGIN_DYNAMIC_PACKAGE_DIR`、`NAPCAT_TEMPFILE_DIR`、
以及 `PROMPT_LOG_DIR` / `PROMPT_ERROR_LOG_DIR`（**全部租户的完整 LLM 提示词原文**）。
扫描结果直接把 `DATA_DIR` 整棵树的路径与体积吐出来，清理任务直接 `rmtree` 它们。

因此这不是"按归属过滤"能解决的问题：既没有可用的归属维度，
读到的内容本身也是跨租户的。9 个端点整体提到 `Role.Super`。

改前 9 个端点全是 `Role.Admin`，而租户管理员就是 `Role.Admin` ——
`POST /cleanup/start` 让客户能删掉平台方与其他客户的沙盒共享目录与上传文件。

（模块原有的改造说明：移除 Ret 包装直接返回 Pydantic 模型；移除宽泛 try-catch
由全局异常处理器统一处理；业务错误抛具体 AppError 子类；响应一律用明确的模型。）
"""

from fastapi import APIRouter, Depends

from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import (
    CleanupTaskNotFoundError,
    ScanNotStartedError,
)
from nekro_agent.schemas.space_cleanup import (
    CleanupProgress,
    CleanupRequest,
    CleanupResult,
    CleanupStartResponse,
    DiskInfo,
    ScanProgressResponse,
    ScanResult,
    ScanStartResponse,
    ScanStatusResponse,
)
from nekro_agent.services.space_cleanup.cleaner import cleanup_service
from nekro_agent.services.space_cleanup.scanner import scanner_service
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role

router = APIRouter(prefix="/space-cleanup", tags=["Space Cleanup"])


@router.post("/scan/start", summary="启动扫描")
@require_role(Role.Super)
async def start_scan(
    _current_user: DBUser = Depends(get_current_active_user),
) -> ScanStartResponse:
    """启动空间扫描任务

    Returns:
        扫描启动响应，包含扫描任务ID
    """
    scan_id = await scanner_service.start_scan()
    return ScanStartResponse(scan_id=scan_id)


@router.get("/scan/status", summary="获取扫描状态")
@require_role(Role.Super)
async def get_scan_status(
    _current_user: DBUser = Depends(get_current_active_user),
) -> ScanStatusResponse:
    """获取扫描状态和进度

    Returns:
        扫描状态响应
    """
    return await scanner_service.get_scan_status()


@router.get("/scan/progress", summary="获取扫描进度")
@require_role(Role.Super)
async def get_scan_progress(
    _current_user: DBUser = Depends(get_current_active_user),
) -> ScanProgressResponse:
    """获取扫描进度

    Returns:
        扫描进度响应
    """
    progress_data = await scanner_service.get_scan_progress()
    return ScanProgressResponse(
        status=progress_data["status"],
        progress=progress_data["progress"],
        message=progress_data.get("message"),
    )


@router.get("/scan/result", summary="获取扫描结果")
@require_role(Role.Super)
async def get_scan_result(
    _current_user: DBUser = Depends(get_current_active_user),
) -> ScanResult:
    """获取扫描结果

    Returns:
        扫描结果

    Raises:
        ScanNotStartedError: 暂无扫描结果
    """
    result = await scanner_service.get_scan_result()
    if result is None:
        raise ScanNotStartedError
    return result


@router.get("/scan/load-cache", summary="从缓存加载扫描结果")
@require_role(Role.Super)
async def load_scan_result_from_cache(
    _current_user: DBUser = Depends(get_current_active_user),
) -> ScanResult:
    """从缓存文件加载最新的空间扫描结果

    Returns:
        扫描结果

    Raises:
        ScanNotStartedError: 缓存中无扫描结果
    """
    result = await scanner_service.load_scan_result_from_cache()
    if result is None:
        raise ScanNotStartedError
    return result


@router.post("/cleanup/start", summary="启动清理任务")
@require_role(Role.Super)
async def start_cleanup(
    request: CleanupRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> CleanupStartResponse:
    """启动清理任务

    Args:
        request: 清理请求

    Returns:
        清理启动响应，包含清理任务ID
    """
    task_id = await cleanup_service.create_cleanup_task(request)
    return CleanupStartResponse(task_id=task_id)


@router.get("/cleanup/progress/{task_id}", summary="获取清理进度")
@require_role(Role.Super)
async def get_cleanup_progress(
    task_id: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> CleanupProgress:
    """获取清理进度

    Args:
        task_id: 任务ID

    Returns:
        清理进度

    Raises:
        CleanupTaskNotFoundError: 清理任务不存在
    """
    progress = await cleanup_service.get_cleanup_progress(task_id)
    if progress is None:
        raise CleanupTaskNotFoundError
    return progress


@router.get("/cleanup/result/{task_id}", summary="获取清理结果")
@require_role(Role.Super)
async def get_cleanup_result(
    task_id: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> CleanupResult:
    """获取清理结果

    Args:
        task_id: 任务ID

    Returns:
        清理结果

    Raises:
        CleanupTaskNotFoundError: 清理任务不存在或未完成
    """
    result = await cleanup_service.get_cleanup_result(task_id)
    if result is None:
        raise CleanupTaskNotFoundError
    return result


@router.get("/disk-info", summary="获取磁盘信息")
@require_role(Role.Super)
async def get_disk_info(
    _current_user: DBUser = Depends(get_current_active_user),
) -> DiskInfo:
    """获取磁盘信息

    Returns:
        磁盘信息

    Raises:
        ScanNotStartedError: 暂无磁盘信息
    """
    scan_result = await scanner_service.get_scan_result()
    if scan_result and scan_result.disk_info:
        return scan_result.disk_info
    raise ScanNotStartedError
