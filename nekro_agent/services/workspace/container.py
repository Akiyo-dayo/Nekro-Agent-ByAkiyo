import asyncio
import contextlib
import os
import random
import re
import secrets
from pathlib import Path
from typing import Set

import aiodocker
import httpx

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import deployment_scope
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.services.workspace.cc_credential import METADATA_TOKEN_KEY
from nekro_agent.services.workspace.manager import WorkspaceService
from nekro_agent.services.workspace.network_guard import (
    prepare_workspace_network,
    resolve_container_relay_addresses,
    resolve_workspace_network_group,
)

logger = get_sub_logger("workspace_container")


class ImageNotFoundError(Exception):
    """本地不存在指定的沙盒镜像，需要先拉取。"""

    def __init__(self, image: str) -> None:
        self.image = image
        super().__init__(f"本地不存在镜像 {image!r}，请先拉取")

CONTAINER_WORKSPACE_PATH = "/workspace"  # 容器内工作区挂载路径

_resolved_nekro_network: str = ""  # 模块级缓存，避免每次创建容器都重复探测
_resolve_network_lock: asyncio.Lock = asyncio.Lock()  # 并发保护，确保只探测一次
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")  # Docker 容器 ID 格式：64 位十六进制


async def resolve_nekro_network(docker: aiodocker.Docker) -> str:
    """解析 NA 所在的 nekro_network 网络名。

    通过 Docker API inspect 当前 NA 容器自身，取其所连接的以 'nekro_network'
    结尾的网络名。结果缓存于模块变量，同一进程内只探测一次；并发调用由 asyncio.Lock 保护。
    """
    global _resolved_nekro_network

    # 快速路径：已缓存则直接返回（无需持锁）
    if _resolved_nekro_network:
        return _resolved_nekro_network

    async with _resolve_network_lock:
        # 持锁后再次检查，避免多个等待者重复探测
        if _resolved_nekro_network:
            return _resolved_nekro_network

        # 通过 Docker API inspect 自身容器，取实际所属的 nekro_network
        try:
            container_id = Path("/etc/hostname").read_text().strip()
            if not _CONTAINER_ID_RE.match(container_id):
                logger.warning(
                    f"[网络自动探测] /etc/hostname 内容 {container_id!r} 不是有效容器 ID"
                    "（非 64 位十六进制），跳过 Docker inspect"
                )
            else:
                self_container = await docker.containers.get(container_id)
                info = await self_container.show()
                networks: dict = info.get("NetworkSettings", {}).get("Networks", {})
                for network_name in networks:
                    if network_name.endswith("nekro_network"):
                        _resolved_nekro_network = network_name
                        logger.info(f"[网络自动探测] 已发现 nekro_network: {network_name!r}")
                        return _resolved_nekro_network
        except Exception as e:
            logger.warning(
                f"[网络自动探测] Docker API 探测失败，回退至默认网络名: {e}",
                exc_info=True,
            )

        # 最终兜底：无前缀的默认名
        _resolved_nekro_network = "nekro_network"
        logger.warning("[网络自动探测] 未找到匹配网络，使用默认值: nekro_network")
        return _resolved_nekro_network


def _get_host_timezone() -> str:
    """获取宿主机时区标识，用于注入容器。优先读 TZ 环境变量，再读 /etc/timezone，最终 fallback UTC。"""
    tz = os.environ.get("TZ", "").strip()
    if tz:
        return tz
    try:
        tz = Path("/etc/timezone").read_text().strip()
        if tz:
            return tz
    except Exception:
        pass
    return "UTC"


def _generate_container_name() -> str:
    """生成随机容器名 nekro-cc-{hex8}"""
    return f"nekro-cc-{secrets.token_hex(4)}"


async def _container_exists(docker: aiodocker.Docker, name: str) -> bool:
    """检查容器是否存在"""
    try:
        c = await docker.containers.get(name)
        return c is not None
    except Exception:
        return False


async def _find_free_port() -> int:
    """在高位端口段随机选取一个未被 DB 使用的端口"""
    start = config.CC_SANDBOX_PORT_RANGE_START
    end = config.CC_SANDBOX_PORT_RANGE_END
    used_ports_raw = await DBWorkspace.filter(host_port__isnull=False).values_list("host_port", flat=True)
    used_from_db: Set[int] = {int(p) for p in used_ports_raw if p is not None}

    for _ in range(100):
        port = random.randint(start, end)
        if port not in used_from_db:
            return port
    raise RuntimeError("无法在端口段内找到空闲端口")


class SandboxContainerManager:
    @staticmethod
    async def _ensure_internal_api_token(workspace: DBWorkspace) -> str:
        """确保工作区存在沙盒内部 API 鉴权 token。"""
        metadata = dict(workspace.metadata or {})
        token = metadata.get("sandbox_api_token")
        if isinstance(token, str) and token.strip():
            return token

        token = secrets.token_urlsafe(32)
        metadata["sandbox_api_token"] = token
        workspace.metadata = metadata
        await workspace.save(update_fields=["metadata", "update_time"])
        return token

    @staticmethod
    async def _resolve_preset(workspace: DBWorkspace):
        """从工作区 metadata 读取 CC 模型预设，fallback 到默认预设"""
        from nekro_agent.core.cc_model_presets import cc_presets_store

        preset_id = (workspace.metadata or {}).get("cc_model_preset_id")
        if preset_id:
            preset = cc_presets_store.get_by_id(int(preset_id))
            if preset:
                return preset
        return cc_presets_store.get_default()

    @staticmethod
    async def check_image_exists(image: str) -> bool:
        """检查本地是否已有指定镜像。"""
        docker = aiodocker.Docker()
        try:
            await docker.images.get(image)
            return True
        except aiodocker.exceptions.DockerError:
            return False
        finally:
            await docker.close()

    @staticmethod
    async def create_and_start(workspace: DBWorkspace) -> DBWorkspace:
        """初始化目录 + 创建并启动容器 + 等待健康检查"""
        image_name = workspace.sandbox_image or config.CC_SANDBOX_IMAGE
        image_tag = workspace.sandbox_version or config.CC_SANDBOX_IMAGE_TAG
        image = f"{image_name}:{image_tag}"

        # 提前检查镜像是否存在，给出友好错误而非 500
        if not await SandboxContainerManager.check_image_exists(image):
            raise ImageNotFoundError(image)

        cc_preset = await SandboxContainerManager._resolve_preset(workspace)
        api_token = await SandboxContainerManager._ensure_internal_api_token(workspace)

        docker = aiodocker.Docker()
        try:
            # Docker 网络配置：默认把工作区放到专用网络上并在宿主侧切断内网访问。
            # 隔离未生效时（best_effort/off）退回改造前的形态——直接挂在 NA 所在的业务网络上。
            #
            # 网络按归属分组：同一网桥内的容器互访是二层交换、不进 iptables，跨租户隔离只能靠
            # 「不同租户不同网络」来做（详见 network_guard 模块 docstring）。分组在**创建容器时**
            # 定下，之后改频道绑定不会让运行中的容器换网，要重建容器才会重新分组。
            #
            # 必须排在写配置文件之前：容器里的 `ANTHROPIC_BASE_URL` 指向 NA 在这张网络上的地址，
            # 网络定下来才知道该写哪个地址。
            network_plan = await prepare_workspace_network(
                docker,
                await resolve_workspace_network_group(workspace),
            )

            # 初始化工作区目录。容器是全新拉起的，中继令牌在这里轮换：
            # 上一代容器若曾把令牌泄漏出去，有效期到此为止。
            await WorkspaceService.init_workspace_dir(
                workspace,
                cc_preset=cc_preset,
                rotate_credential=True,
                controller_address=network_plan.controller_address,
                host_address=network_plan.host_address,
            )

            # 生成唯一容器名（查重）
            container_name = _generate_container_name()
            for _ in range(10):
                if not await _container_exists(docker, container_name):
                    break
                container_name = _generate_container_name()
            else:
                raise RuntimeError("无法生成唯一容器名")

            host_port = await _find_free_port()
            ws_host_dir = str(WorkspaceService.get_workspace_dir(workspace.id).resolve())
            claude_home_host_dir = str((WorkspaceService.get_workspace_dir(workspace.id) / ".claude_home").resolve())

            host_tz = _get_host_timezone()
            binds = [
                f"{ws_host_dir}:{CONTAINER_WORKSPACE_PATH}:rw",
                f"{claude_home_host_dir}:/home/appuser/.claude:rw",
            ]
            if Path("/etc/localtime").exists():
                binds.append("/etc/localtime:/etc/localtime:ro")

            container_config: dict = {
                "Image": image,
                # 归属标签：让 docker-guard 硬化部署能认出这是本部署的容器，从而放行对它的
                # exec（MCP stdio 校验、CC 终端）/logs/inspect 等按 ID 指向的操作。缺了它，
                # 代理重启后无法从快表认出这个长命容器，会把 NA 对自己工作区的 exec 也判成越权。
                # 值取 deployment_scope()（与沙盒网络名/iptables 标记同一套派生，见 core/os_env.py），
                # 便于将来按部署收敛跨部署归属；guard 当前只校验 `nekro.` 前缀。
                "Labels": {"nekro.owner.scope": deployment_scope()},
                "HostConfig": {
                    "Binds": binds,
                    "PortBindings": {
                        f"{config.CC_SANDBOX_INTERNAL_PORT}/tcp": [
                            {"HostIp": "127.0.0.1", "HostPort": str(host_port)}
                        ]
                    },
                    # 隔离没生效时（含 NA 跑在宿主上的开发部署）容器挂在默认网络上，
                    # 回连 NA 的中继端点只能经宿主网关，这个别名是它的落点；
                    # 隔离生效时中继地址用的是 NA 在专用网桥上的 IP，这条别名用不到，
                    # 而且发往宿主的包会被守卫的 INPUT 默认拒绝挡掉。
                    "ExtraHosts": ["host.docker.internal:host-gateway"],
                    "RestartPolicy": {"Name": "no"},
                },
                "Env": [
                    f"WORKSPACE_ROOT={CONTAINER_WORKSPACE_PATH}",
                    f"SETTINGS_PATH={CONTAINER_WORKSPACE_PATH}/settings.json",
                    f"RUNTIME_POLICY={workspace.runtime_policy}",
                    "SKIP_PERMISSIONS=true",
                    f"INTERNAL_API_TOKEN={api_token}",
                    f"PORT={config.CC_SANDBOX_INTERNAL_PORT}",
                    "HOST=0.0.0.0",
                    f"TZ={host_tz}",
                ],
                "ExposedPorts": {f"{config.CC_SANDBOX_INTERNAL_PORT}/tcp": {}},
            }

            if network_plan.network_mode:
                container_config["HostConfig"]["NetworkMode"] = network_plan.network_mode

            container = await docker.containers.create_or_replace(
                name=container_name,
                config=container_config,
            )
            await container.start()

            # 通过 inspect 获取 container_id
            info = await container.show()
            container_id: str = info["Id"][:12]

            # 更新 DB
            workspace.container_name = container_name
            workspace.container_id = container_id
            workspace.host_port = host_port
            workspace.status = "active"
            workspace.last_error = None  # type: ignore[assignment]
            await workspace.save(
                update_fields=[
                    "container_name",
                    "container_id",
                    "host_port",
                    "status",
                    "last_error",
                    "update_time",
                ]
            )

            logger.info(f"容器已启动: {container_name} (port={host_port})")
        finally:
            await docker.close()

        # 等待健康检查（在 docker.close() 后，避免泄漏连接）
        timeout = config.CC_SANDBOX_STARTUP_TIMEOUT
        healthy = await SandboxContainerManager._wait_healthy(workspace.api_endpoint, timeout)
        if not healthy:
            workspace.status = "failed"
            workspace.last_error = f"容器启动超时（{timeout}s）"  # type: ignore[assignment]
            await workspace.save(update_fields=["status", "last_error", "update_time"])
            raise RuntimeError(f"cc-sandbox 容器健康检查超时: {workspace.container_name}")

        return workspace

    @staticmethod
    async def stop(workspace: DBWorkspace) -> None:
        """停止容器"""
        if not workspace.container_name:
            return
        docker = aiodocker.Docker()
        try:
            try:
                container = await docker.containers.get(workspace.container_name)
                await container.stop(t=10)
            except Exception as e:
                logger.warning(f"停止容器失败（忽略）: {workspace.container_name}: {e}")
        finally:
            await docker.close()

        # 容器停了就没有合法的中继调用方了，令牌当场作废：中继端点挂在 NA 的主端口上，
        # 不随容器一起消失，留着它等于「工作区停着、额度还能被烧」。
        # 唯一的启动入口 `sandbox/start` 走 create_and_start，会重新签发一把。
        metadata = dict(workspace.metadata or {})
        fields = ["status", "update_time"]
        if metadata.pop(METADATA_TOKEN_KEY, None) is not None:
            workspace.metadata = metadata
            fields.append("metadata")

        workspace.status = "stopped"
        await workspace.save(update_fields=fields)

    @staticmethod
    async def remove(workspace: DBWorkspace) -> None:
        """删除容器（工作区被删除时用）

        与 `stop()` 的差别是它不回写数据库：调用方紧接着就要把工作区那一行删掉。
        只 `stop` 是不够的 —— 工作区行一没，容器名/端口/磁盘就再没有任何入口能碰到，
        只能到宿主机上 `docker rm` 手工收拾（与 §3 #41 的悬空绑定同一种形态）。

        Docker 不可用或容器早已不在都只记录告警：删除工作区不该因为容器层的状态而失败。
        **连 `aiodocker.Docker()` 的构造都在 try 里** —— 宿主上没有 Docker 时它自己就抛，
        而这条路径改前只在 `status == "active"` 时才碰 Docker，把它挪出 try 等于
        给没有 Docker 的部署新增一条"工作区删不掉"的 500。
        """
        if not workspace.container_name:
            return
        docker = None
        try:
            docker = aiodocker.Docker()
            container = await docker.containers.get(workspace.container_name)
            with contextlib.suppress(Exception):
                await container.stop(t=10)
            await container.delete(force=True)
            logger.info(f"已删除工作区容器: {workspace.container_name}")
        except Exception as e:
            logger.warning(f"删除工作区容器失败（忽略）: {workspace.container_name}: {e}")
        finally:
            if docker is not None:
                with contextlib.suppress(Exception):
                    await docker.close()

    @staticmethod
    async def restart(workspace: DBWorkspace) -> None:
        """重启容器（进程级恢复），重启前同步配置文件"""
        if not workspace.container_name:
            raise RuntimeError("容器未运行")

        # 重启前重新写入配置文件，确保数据库与磁盘一致。
        # 中继令牌一并轮换：重启会杀掉容器里所有在跑的 `claude` 进程，本来就没有在途请求可打断。
        cc_preset = await SandboxContainerManager._resolve_preset(workspace)

        docker = aiodocker.Docker()
        try:
            # 中继地址按容器**实际所在**的网络重新反查一次，而不是沿用 metadata 里那份。
            # 存着的地址会过期：NA 容器被重建后在这张网桥上的 IP 可能变，改造前建的容器
            # 更是压根没有这份地址。重启是运维遇到「CC 连不上模型」时的第一反应，
            # 这条路径修不回来的话就没有别的路径能修了（只剩重建容器）。
            controller_address, host_address = await resolve_container_relay_addresses(
                docker,
                workspace.container_name,
            )
            await WorkspaceService.init_workspace_dir(
                workspace,
                cc_preset=cc_preset,
                rotate_credential=True,
                controller_address=controller_address,
                host_address=host_address,
            )

            container = await docker.containers.get(workspace.container_name)
            await container.restart(t=10)
        finally:
            await docker.close()

        # 等待健康
        timeout = config.CC_SANDBOX_STARTUP_TIMEOUT
        healthy = await SandboxContainerManager._wait_healthy(workspace.api_endpoint, timeout)
        if not healthy:
            workspace.status = "failed"
            workspace.last_error = "容器重启后健康检查超时"  # type: ignore[assignment]
            await workspace.save(update_fields=["status", "last_error", "update_time"])

    @staticmethod
    async def rebuild(workspace: DBWorkspace) -> DBWorkspace:
        """彻底重建容器（不依赖 cc-sandbox API，纯 Docker 操作）"""
        docker = aiodocker.Docker()
        try:
            if workspace.container_name:
                try:
                    container = await docker.containers.get(workspace.container_name)
                    try:
                        await container.stop(t=10)
                    except Exception:
                        pass
                    await container.delete(force=True)
                    logger.info(f"已删除旧容器: {workspace.container_name}")
                except Exception as e:
                    logger.warning(f"删除旧容器失败（继续重建）: {e}")
        finally:
            await docker.close()

        # 清空容器信息
        workspace.container_name = None  # type: ignore[assignment]
        workspace.container_id = None  # type: ignore[assignment]
        workspace.host_port = None  # type: ignore[assignment]
        workspace.status = "stopped"
        await workspace.save(
            update_fields=["container_name", "container_id", "host_port", "status", "update_time"]
        )

        return await SandboxContainerManager.create_and_start(workspace)

    @staticmethod
    async def get_logs(workspace: DBWorkspace, tail: int = 100) -> str:
        """获取容器日志"""
        if not workspace.container_name:
            return ""
        docker = aiodocker.Docker()
        try:
            container = await docker.containers.get(workspace.container_name)
            logs = await container.log(stdout=True, stderr=True, tail=tail)
            return "".join(logs)
        except Exception as e:
            logger.warning(f"获取容器日志失败: {e}")
            return f"[获取日志失败: {e}]"
        finally:
            await docker.close()

    @staticmethod
    async def _wait_healthy(endpoint: str, timeout: int = 60) -> bool:
        """轮询 GET /health 直到返回 healthy 或超时"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.get(f"{endpoint}/health")
                    if resp.status_code == 200 and resp.json().get("status") == "healthy":
                        return True
            except Exception:
                pass
            await asyncio.sleep(2)
        return False

    @staticmethod
    async def recover_on_startup() -> None:
        """NA 启动时：inspect 验证所有 active workspace，不在则降级 stopped"""
        active_workspaces = await DBWorkspace.filter(status="active").all()
        if not active_workspaces:
            return
        docker = aiodocker.Docker()
        try:
            for workspace in active_workspaces:
                if not workspace.container_name:
                    workspace.status = "stopped"
                    await workspace.save(update_fields=["status", "update_time"])
                    continue
                try:
                    container = await docker.containers.get(workspace.container_name)
                    info = await container.show()
                    state = info.get("State", {})
                    if state.get("Running"):
                        logger.debug(f"workspace {workspace.id} 容器运行中: {workspace.container_name}")
                    else:
                        workspace.status = "stopped"
                        await workspace.save(update_fields=["status", "update_time"])
                        logger.info(f"workspace {workspace.id} 容器已停止，降级为 stopped")
                except Exception:
                    workspace.status = "stopped"
                    await workspace.save(update_fields=["status", "update_time"])
                    logger.info(f"workspace {workspace.id} 容器不存在，降级为 stopped")
        finally:
            await docker.close()
