import hashlib
import re
import uuid
from enum import Enum
from pathlib import Path
from typing import Optional

from nekro_agent.core.logger import logger
from nekro_agent.core.os_env import SANDBOX_SHARED_HOST_DIR, USER_UPLOAD_DIR


def sanitize_chat_key_for_path(chat_key: str) -> str:
    """将 chat_key 规整为文件系统 / Docker 卷路径安全的目录名。

    多数适配器的 chat_key 仅含 ``[a-zA-Z0-9_.-]``，此函数对其为恒等变换；
    但部分适配器（如 wechat_ilink_multi）的 chat_key 含 ``:`` 等字符，直接作为
    Docker 卷 ``源:目标:模式`` 的路径片段会破坏挂载语义，且与已做清洗的挂载源不一致，
    导致写入目录与沙盒挂载目录错位、入站文件在沙盒内不可见。

    注意：此处的正则必须与 ``services/sandbox/runner.py:_sanitize_docker_name_part``
    保持一致，否则上传写入路径与沙盒挂载源会再次错位。
    """
    sanitized = re.sub(r"[^a-zA-Z0-9_.-]", "_", chat_key)
    return sanitized or "unknown"


class PathLocation(Enum):
    """路径位置枚举"""

    UPLOADS = "uploads"
    SHARED = "shared"


def assert_path_within(base_dir: Path, target_path: Path) -> Path:
    """确认宿主机路径确实落在允许的根目录内，否则拒绝

    沙盒路径由模型生成的代码提供，`/app/shared/../../configs/nekro-agent.yaml`
    这类写法在纯词法拼接下会指向 NekroAgent 自己的配置文件——里面是平台方全部模型组的
    `API_KEY`。`Path.is_relative_to()` 只做词法比较、`Path(*parts)` 也不消解 `..`，
    因此必须在拼接完成后统一 `resolve()` 再做归属校验：`resolve()` 同时消解 `..`
    与符号链接，能一并挡住「在共享目录里放一个指向宿主机任意位置的软链」这条绕过。

    Args:
        base_dir: 允许访问的根目录
        target_path: 拼接得到的宿主机路径

    Returns:
        校验通过的宿主机路径（保持传入形态，不替换为 resolve 结果，避免影响既有的路径比较）

    Raises:
        ValueError: 目标路径逃出了根目录
    """
    resolved_base = base_dir.resolve()
    resolved_target = target_path.resolve()
    if not resolved_target.is_relative_to(resolved_base):
        logger.warning(f"拒绝越界路径访问: {target_path} 不在 {base_dir} 内")
        raise ValueError(f'路径 "{target_path}" 越出了允许访问的目录范围')
    return target_path


def _detect_path_location(path: Path) -> Optional[PathLocation]:
    """
    检测路径所属位置

    Args:
        path (Path): 需要检测的路径

    Returns:
        Optional[PathLocation]: 路径位置，如果不匹配则返回 None
    """
    try:
        path_parts = path.parts

        for part in path_parts:
            if part == PathLocation.UPLOADS.value:
                return PathLocation.UPLOADS
            if part == PathLocation.SHARED.value:
                return PathLocation.SHARED
        else:
            return None
    except Exception as e:
        logger.error(f"Path location detection error: {e}")
        return None


def convert_to_host_path(
    sandbox_path: Path,
    chat_key: str,
    container_key: Optional[str] = None,
    uploads_dir: Path = Path(USER_UPLOAD_DIR),
    shared_dir: Path = Path(SANDBOX_SHARED_HOST_DIR),
) -> Path:
    """
    将沙盒内的路径转换为宿主机路径

    Args:
        sandbox_path (Path): 沙盒内的路径
        chat_key (str): 聊天标识
        container_key (Optional[str]): 容器标识，shared 路径必需
        uploads_dir (Path): 上传文件根目录，默认使用 USER_UPLOAD_DIR
        shared_dir (Path): 共享文件根目录，默认使用 SANDBOX_SHARED_HOST_DIR

    Returns:
        Optional[Path]: 转换后的宿主机路径，如果转换失败返回 None

    Raises:
        ValueError: 当 shared 路径没有提供 container_key 时

    Examples:
        >>> # 转换绝对路径
        >>> convert_to_host_path(Path("/app/uploads/test.txt"), chat_key)
        Path("/data/uploads/nonebot-group_123456/test.txt")

        >>> # 转换相对路径
        >>> convert_to_host_path(Path("./uploads/test.txt"), chat_key)
        Path("/data/uploads/nonebot-group_123456/test.txt")

        >>> # 转换 shared 路径
        >>> convert_to_host_path(
        ...     Path("/app/shared/test.txt"),
        ...     chat_key,
        ...     container_key="container_789"
        ... )
        Path("/data/shared/container_789/test.txt")
    """

    def _validate_shared_path(key: Optional[str]) -> None:
        if not key:
            raise ValueError("Container key is required for shared paths")

    # 设置默认工作目录为 /app
    base_path = Path("/app")

    # 如果是相对路径，将其转换为绝对路径
    if not sandbox_path.is_absolute():
        sandbox_path = base_path / sandbox_path

    # 标准化路径
    clean_path = Path(*sandbox_path.parts)

    # 检测路径位置
    location = _detect_path_location(clean_path)
    if not location:
        logger.warning(f"Unable to detect path location for: {sandbox_path}")
        raise ValueError(
            f"Unable to detect path location for: {sandbox_path}, make sure your path is valid shared path or upload path",
        )

    # 获取相对路径部分
    try:
        relative_path = Path(*clean_path.parts[clean_path.parts.index(location.value) + 1 :])
        logger.debug(f"Converting path: {sandbox_path}, detected location: {location}, relative path: {relative_path}")
    except ValueError as e:
        logger.warning(f"Unable to extract relative path from: {sandbox_path}")
        raise ValueError(f"Unable to extract relative path from: {sandbox_path}") from e

    # 根据位置类型构建宿主机路径
    if location == PathLocation.UPLOADS:
        base_dir = uploads_dir / sanitize_chat_key_for_path(chat_key)
        return assert_path_within(base_dir, base_dir / relative_path)
    if location == PathLocation.SHARED:
        _validate_shared_path(container_key)
        base_dir = shared_dir / str(container_key)
        return assert_path_within(base_dir, base_dir / relative_path)
    raise ValueError(f"Invalid path location: {location}, make sure your path is valid shared path or upload path")


def is_url_path(path: str) -> bool:
    """
    检查路径是否为URL

    Args:
        path (str): 要检查的路径

    Returns:
        bool: 是否为URL
    """
    return path.startswith(("http://", "https://"))


def convert_filename_to_sandbox_upload_path(filename: str | Path) -> Path:
    """将文件名转换为沙盒内上传文件路径

    注意：不支持多级路径

    Args:
        filename (Union[str, Path]): 文件名

    Returns:
        Path: 沙盒内路径
    """
    return Path("/app/uploads") / Path(filename).name


def convert_filename_to_sandbox_shared_path(filename: str | Path) -> Path:
    """将文件名转换为沙盒内共享文件路径

    Args:
        filename (Union[str, Path]): 文件名

    Returns:
        Path: 沙盒内路径
    """
    return Path("/app/shared") / Path(filename).name


def convert_filepath_to_sandbox_shared_path(filepath: str | Path) -> Path:
    """将文件路径转换为沙盒内共享文件路径

    Args:
        filename (Union[str, Path]): 文件名

    Returns:
        Path: 沙盒内路径
    """
    return Path("/app/shared") / filepath


def convert_filepath_to_sandbox_upload_path(filepath: str | Path) -> Path:
    """将文件路径转换为沙盒内上传文件路径

    Args:
        filepath (Union[str, Path]): 文件路径

    Returns:
        Path: 沙盒内路径
    """
    return Path("/app/uploads") / filepath


def convert_filename_to_access_path(filename: str | Path, chat_key: str) -> Path:
    """将文件名转换为访问路径

    Args:
        filename (Union[str, Path]): 文件名
        chat_key (str): 聊天频道键名

    Returns:
        Path: 访问路径
    """
    return Path(USER_UPLOAD_DIR) / sanitize_chat_key_for_path(chat_key) / Path(filename).name


def get_upload_file_path(from_chat_key: str, file_name: str = "", use_suffix: str = "", seed: str = "") -> str:
    """生成一个可用的上传文件路径

    Args:
        from_chat_key (str): 聊天频道ID
        file_name (str): 文件名
        use_suffix (str): 文件后缀
    """
    if not file_name:
        if not seed:
            seed = str(uuid.uuid4())
        file_name = f"{hashlib.md5(seed.encode()).hexdigest()}{use_suffix}"
    save_path = Path(USER_UPLOAD_DIR) / sanitize_chat_key_for_path(from_chat_key) / Path(file_name)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    return str(save_path)
