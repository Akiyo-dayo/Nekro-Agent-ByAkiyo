import asyncio
import os

import aiodocker
from aiodocker.docker import DockerContainer

from nekro_agent.core.logger import logger
from nekro_agent.core.os_env import OsEnv


async def get_docker_client() -> aiodocker.Docker:
    """获取Docker客户端"""
    return aiodocker.Docker()


async def get_container(container_name: str) -> DockerContainer:
    """获取指定名称的容器实例

    Args:
        container_name: 容器名称

    Returns:
        DockerContainer: 容器实例

    Raises:
        Exception: 获取容器失败时抛出异常
    """
    try:
        docker = await get_docker_client()
        return await docker.containers.get(container_name)
    except Exception as e:
        logger.error(f"获取容器失败: {e!s}")
        raise


async def get_self_container() -> DockerContainer:
    """通过 HOSTNAME 直接获取当前容器实例。

    说明:
        在 Docker 中若设置了 container_name, HOSTNAME 即该名称；
        若未显式设置, HOSTNAME 为容器短 ID, 也可被 Docker API 解析。
    """
    hostname = os.environ.get("HOSTNAME", "").strip()
    if not hostname:
        raise RuntimeError("HOSTNAME 未找到，无法定位容器")
    try:
        docker = await get_docker_client()
        return await docker.containers.get(hostname)
    except Exception as e:
        logger.error(f"获取自身容器失败: {e!s} (HOSTNAME={hostname})")
        raise


# `asyncio.create_task()` 的返回值只被事件循环弱引用，调用方不持有就可能在执行途中被 GC 掉。
# 重启自身这条路上任务被回收 = 接口回了 ok、容器却没重启，且没有任何信号。
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn_detached(coro, description: str) -> None:
    """起一个不等待结果的后台任务，并保证它不会被 GC，失败时留下日志"""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)

    def _on_done(finished: asyncio.Task) -> None:
        _BACKGROUND_TASKS.discard(finished)
        if not finished.cancelled() and finished.exception() is not None:
            logger.error(f"{description}失败: {finished.exception()!s}")

    task.add_done_callback(_on_done)


async def restart_self(timeout: int = 30) -> bool:
    """重启自身容器

    Args:
        timeout: 重启超时时间(秒)

    Returns:
        bool: 是否成功发送重启命令
    """
    if not OsEnv.RUN_IN_DOCKER:
        raise RuntimeError("当前应用未在 Docker 中运行")
    try:
        # 定位容器同样可能失败（HOSTNAME 缺失、docker.sock 不可达）。改前它在 try 之外，
        # 异常直接穿透到全局兜底处理器 —— 调用方拿到的是「服务器内部错误」而不是「重启系统失败」。
        container = await get_self_container()
    except Exception as e:
        logger.error(f"重启自身容器失败: {e!s}")
        return False
    # 创建任务但不等待完成：容器一旦停止，这个进程也就没了，等不到结果
    _spawn_detached(container.restart(timeout=timeout), "重启自身容器")
    return True


async def restart_container(container_id_or_name: str, timeout: int = 30) -> bool:
    """重启指定容器

    Args:
        container_id_or_name: 容器ID或名称
        timeout: 重启超时时间(秒)

    Returns:
        bool: 是否成功重启
    """
    try:
        docker = await get_docker_client()
        container = await docker.containers.get(container_id_or_name)
        await container.restart(timeout=timeout)
    except Exception as e:
        logger.error(f"重启容器失败: {e!s}")
        return False
    else:
        return True
