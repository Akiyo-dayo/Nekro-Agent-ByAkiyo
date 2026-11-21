"""把 SSH 网关接进 NA 的运行期：开关跳变与凭据变更时的补配

总开关是后台可热改的，监督任务每 5 秒读一次。开关从关到开的那一拍，已经建好的工作区
不会自动获得网关：网络规则是按创建容器时的签名缓存的旧版本、容器里也没有网关钥匙。
这里订阅监督任务的跳变，把「能补的」补上：

- 重新 `prepare_workspace_network`，让缓存键变化触发重装，规则里多出网关端口；
- 对**已经有网关钥匙**的容器重写 `config` / `known_hosts`、收权限、登记；
- 没有钥匙的容器**不在运行中补发**：钥匙要和容器一起轮换，运行中塞进去等于给一个
  已经跑着任意代码的容器换锁芯。写日志说明要重建容器才能用 SSH。

从开到关时只清登记表：文件留着，下一次建容器时会删；监听由网关后端自己停；规则在
下一次准备网络时因为缓存键变化自然去掉。

## 凭据变更

凭据行被建、改、删、启停之后，在跑的工作区要立刻重写 `config`（不动私钥）并刷新
`CLAUDE.md`。删除的通知只带行 ID，行已经不在库里；这时退化为扫描在跑的工作区，
按库里现有行重建（删掉的那个别名自然消失）。这条路径只在总开关开着时走。

## 闸关时

两个回调都在第一行看开关：关着时一次库都不查、一个 Docker 客户端都不建。
"""

from __future__ import annotations

from typing import List

import aiodocker

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv
from nekro_agent.services.ssh_gateway import events, registry, supervisor
from nekro_agent.services.ssh_gateway.container_files import write_nk_ssh_files
from nekro_agent.services.ssh_gateway.image_support import image_supports_gateway

logger = get_sub_logger("ssh_gateway.provisioning")

_installed = False


def install() -> None:
    """把订阅接到监督任务与凭据变更通知上；重复调用无副作用

    启动流程在 `supervisor.start()` 之前调用。任何异常由调用方兜住：网关补配起不来
    最多是 SSH 用不了，不能拦住 NA 启动。
    """
    global _installed
    if _installed:
        return
    supervisor.on_enabled_change(_on_enabled_change)
    if not events.is_subscribed(_on_credential_changed):
        events.on_credential_changed(_on_credential_changed)
    _installed = True


def _open_docker() -> aiodocker.Docker:
    """单独一层，方便测试替换"""
    return aiodocker.Docker()


async def _listen_address_for_container(docker: aiodocker.Docker, container_name: str) -> str:
    """按容器实际所在的网络推出容器该连的 NA 地址

    与建容器时同一套落点：NA 在容器里用它在网桥上的地址，NA 在宿主上用本网桥网关。
    运行中的容器不重建就换不了网络，所以必须按 inspect 反查，不能按当前绑定重算分组。
    """
    from nekro_agent.services.workspace.network_guard import resolve_container_relay_addresses

    controller_address, host_address = await resolve_container_relay_addresses(docker, container_name)
    return controller_address if OsEnv.RUN_IN_DOCKER else host_address


async def _register_existing_container(
    docker: aiodocker.Docker,
    workspace: object,
    *,
    listen_address: str,
    network_mode: str,
    public_key: str,
) -> bool:
    """读容器地址与镜像版本，登记成 `ContainerPeer`；任何一步失败只记 WARNING"""
    from nekro_agent.services.ssh_gateway.registry import ContainerPeer
    from nekro_agent.services.workspace.client import CCSandboxClient
    from nekro_agent.services.workspace.network_guard import extract_network_ipv4

    workspace_id = int(getattr(workspace, "id"))
    container_name = str(getattr(workspace, "container_name", "") or "")
    try:
        container = await docker.containers.get(container_name)
        info = await container.show()
        if not (info.get("State") or {}).get("Running"):
            logger.debug(f"工作区 {workspace_id} 的容器没有在跑，暂不登记 SSH 网关")
            return False
        peer_address = extract_network_ipv4(info, network_mode)
        if not peer_address:
            logger.warning(f"工作区 {workspace_id} 的容器在 {network_mode} 上没有 IPv4 地址，暂不登记 SSH 网关")
            return False
        version = await CCSandboxClient(workspace).get_cc_version()  # type: ignore[arg-type]
        registry.register_peer(
            ContainerPeer(
                workspace_id=workspace_id,
                peer_address=peer_address,
                listen_address=listen_address,
                client_public_key=public_key,
                isolation_ok=True,
                image_ok=image_supports_gateway(version),
            ),
        )
        return True
    except Exception as exc:  # noqa: BLE001 - 登记失败只该让这个工作区用不了 SSH
        logger.warning(
            f"登记工作区 {workspace_id} 的 SSH 网关容器失败（这个工作区暂时用不了 SSH）: "
            f"{type(exc).__name__}: {exc}",
        )
        return False


async def _refresh_claude_md(workspace: object) -> None:
    """按刚写入的凭据行重写 `CLAUDE.md` 里的「可用的服务器」一节"""
    try:
        from nekro_agent.services.workspace.manager import WorkspaceService

        WorkspaceService.update_claude_md(workspace)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 - 提示词刷新失败不该影响凭据本身
        logger.warning(
            f"刷新工作区 {getattr(workspace, 'id', '?')} 的 CLAUDE.md 失败（已忽略）: {type(exc).__name__}: {exc}",
        )


async def _provision_running_workspace(
    docker: aiodocker.Docker,
    workspace: object,
    *,
    listen_address: str,
    network_mode: str,
    public_key: str,
) -> None:
    """重写一个在跑工作区的网关文件并登记

    运行中只改配置，不传私钥：钥匙要和容器一起轮换。
    """
    try:
        await write_nk_ssh_files(
            workspace,
            listen_address=listen_address,
            private_key=None,
        )
        await _register_existing_container(
            docker,
            workspace,
            listen_address=listen_address,
            network_mode=network_mode,
            public_key=public_key,
        )
        await _refresh_claude_md(workspace)
    except Exception as exc:  # noqa: BLE001 - 单个工作区失败不能挡住别的工作区
        logger.warning(
            f"补配工作区 {getattr(workspace, 'id', '?')} 的 SSH 网关失败（这个工作区暂时用不了 SSH）: "
            f"{type(exc).__name__}: {exc}",
        )


async def _on_enabled_change(enabled: bool) -> None:
    """总开关跳变：开 -> 逐个补规则与容器文件；关 -> 清空登记表"""
    if not enabled:
        # 网关后端自己停监听（监督任务已经停过它）；这里只负责让「这是哪个容器」的事实消失。
        # 文件与规则都留着：文件在下次建容器时删（总开关关着的那条创建路径），规则下次准备网络时自然去掉。
        registry.reset_registry()
        logger.info("SSH 网关已关闭：登记表已清空，容器里的配置文件留到下次重建时删除")
        return

    from nekro_agent.models.db_workspace import DBWorkspace
    from nekro_agent.services.workspace.network_guard import prepare_workspace_network, resolve_workspace_network_group

    try:
        workspaces = await DBWorkspace.filter(status="active")
    except Exception as exc:  # noqa: BLE001 - 读不到工作区列表时只补不了，不影响网关本身
        logger.warning(f"SSH 网关开启后枚举在跑的工作区失败（本次不补配）: {type(exc).__name__}: {exc}")
        return

    docker = _open_docker()
    try:
        for workspace in workspaces:
            workspace_id = int(workspace.id)
            container_name = str(workspace.container_name or "")
            if not container_name:
                continue
            public_key = str((workspace.metadata or {}).get("ssh_gateway_client_key") or "").strip()
            if not public_key:
                logger.warning(
                    f"工作区 {workspace_id} 的容器是在网关开启前建的，重建后才能用 SSH（不在运行中补发钥匙）",
                )
                continue
            try:
                group = await resolve_workspace_network_group(workspace)
                plan = await prepare_workspace_network(docker, group)
                if not plan.gateway_listen_address:
                    logger.warning(
                        f"工作区 {workspace_id} 的网络隔离没有生效，SSH 网关不向它提供服务（暂时用不了 SSH）",
                    )
                    continue
                await _provision_running_workspace(
                    docker,
                    workspace,
                    listen_address=plan.gateway_listen_address,
                    network_mode=plan.network_mode,
                    public_key=public_key,
                )
            except Exception as exc:  # noqa: BLE001 - 单个工作区失败不能挡住别的工作区
                logger.warning(
                    f"SSH 网关开启后补配工作区 {workspace_id} 失败（这个工作区暂时用不了 SSH）: "
                    f"{type(exc).__name__}: {exc}",
                )
    finally:
        try:
            await docker.close()
        except Exception:  # noqa: BLE001
            pass


async def _running_workspaces_for_credential(credential_id: int) -> List[object]:
    """通知里只有行 ID：行还在就只取那个工作区，行已删就退化为扫在跑的工作区"""
    from nekro_agent.models.db_workspace import DBWorkspace
    from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential

    row = await DBWsSshCredential.get_or_none(id=int(credential_id))
    if row is not None:
        workspace = await DBWorkspace.get_or_none(id=int(row.workspace_id))
        return [workspace] if workspace is not None else []

    # 删除之后行已经不在了。删除只可能让配置少一段，按库里现有行整份重建即可；
    # 只挑「在跑、且建容器时拿到了网关钥匙」的工作区，避免为无关工作区白跑 Docker。
    result: List[object] = []
    for workspace in await DBWorkspace.filter(status="active"):
        if not str(workspace.container_name or ""):
            continue
        if not str((workspace.metadata or {}).get("ssh_gateway_client_key") or "").strip():
            continue
        result.append(workspace)
    return result


async def _on_credential_changed(credential_id: int) -> None:
    """凭据建 / 改 / 删 / 启停：在跑的工作区重写配置并刷新 CLAUDE.md"""
    if not config.SSH_GATEWAY_ENABLED:
        return

    try:
        workspaces = await _running_workspaces_for_credential(int(credential_id))
    except Exception as exc:  # noqa: BLE001 - 通知失败不该影响已经落库的凭据
        logger.warning(f"SSH 凭据变更后查工作区失败（本次不重写容器配置）: {type(exc).__name__}: {exc}")
        return

    docker = None
    try:
        for workspace in workspaces:
            workspace_id = int(workspace.id)
            container_name = str(workspace.container_name or "")
            if not container_name or str(workspace.status) != "active":
                continue
            try:
                if docker is None:
                    docker = _open_docker()
                listen_address = await _listen_address_for_container(docker, container_name)
                if not listen_address:
                    logger.warning(f"读不出工作区 {workspace_id} 的网关地址，暂不重写 SSH 配置")
                    continue
                await write_nk_ssh_files(workspace, listen_address=listen_address, private_key=None)
                await _refresh_claude_md(workspace)
            except Exception as exc:  # noqa: BLE001 - 单个工作区失败不能挡住别的
                logger.warning(
                    f"SSH 凭据变更后重写工作区 {workspace_id} 的配置失败（这个工作区的 SSH 暂时不可用）: "
                    f"{type(exc).__name__}: {exc}",
                )
    finally:
        if docker is not None:
            try:
                await docker.close()
            except Exception:  # noqa: BLE001
                pass


__all__ = ["install"]
