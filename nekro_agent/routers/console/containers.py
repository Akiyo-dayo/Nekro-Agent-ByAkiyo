"""总后台：NapCat 协议端容器管理路由

挂载于 `/api/console` 之下。

容器创建与删除等价于宿主机 root 权限（NA 挂载了 `/var/run/docker.sock`），因此这一层
只做参数收敛与权限校验，镜像白名单、容器名模板、归属标签校验、端口回环绑定与挂载路径
生成全部落在 `services/console/container_manager.py`，不给路由层留下绕过空间。

## 两档权限，判据是「宿主资源分配」还是「我自己那台 Bot 的日常运维」

| 档 | 端点 | 要求 |
|---|---|---|
| 宿主资源分配 | `create` / `start` / `stop` / `delete` / `network-config` 读写 | `Role.Super` |
| 协议端自助运维 | `container` 读 / `restart` / `qrcode` / `quick-login` | `Role.Admin` + 归属校验 |

第二档的边界：租户**最多能「重启自己的 NapCat」**，
且必须由后端校验该容器确实属于该租户。此前整族 11 个端点全是 `Role.Super`，于是
QQ 掉线 / 需要重新扫码 —— 最高频的那类运营事件 —— 对定制版客户是一个红点加零个按钮。

留在第一档的四类各有理由，都不是"顺手"：

- `create` 会分配宿主端口、拉镜像、起一个挂着本部署数据目录的容器；
- `delete` 删的是宿主上的容器；
- `start` / `stop` 能让一台 Bot 长期停着，而"重启"覆盖了自助运维要的全部形态
  （Docker 的 restart 对已停止的容器就是启动它），因此租户侧不需要它们；
- `network-config` 的响应里带着托管连接的 **OneBot 访问令牌**，写侧还能把协议端的
  出站地址改到别处 —— 那是接入拓扑，不是运维动作。

⚠️ **未启用多租户的部署上，这一档的口径确实放宽了一格**：那里每个账号都走"平台方"分支，
于是第二档变成"任何 `Role.Admin` 账号"。二维归属校验在这种部署上恒真（一个租户都没有），
挡不住它 —— 挡住它的只有 `Role.Admin` 这道门槛本身。扫码那一条尤其值得知道：
它给出的是一张**能把某个 QQ 登进这台容器**的二维码。这与 `skills` / `workspaces` 那几族
降到 `Role.Admin` 时的口径一致（见 `tests/test_route_permission_contract.py` 的
`TENANT_SCOPED` 桶），不是本模块独有的取舍；单部署里把非运维人员提到 `Role.Admin`
之前要知道这件事。

## 归属校验落在两处，缺一不可

1. **实例这一维**：`_resolve_accessible_instance()` 走 `assert_instance_accessible`，
   租户填别人的 `instance_key` 一律 404（不是 403：403 会告诉他这个标识确实存在）。
   同一个 `instance_key` 上可以并存多行（唯一性只到 `(adapter_key, instance_key)`），
   所以它还要把"被授权的那一行"与"将要被操作的那一行"对上号，理由见该函数。
2. **容器这一维**：容器名是宿主全局的，`instance_key` 对得上不代表那个容器是本部署建的。
   写路径由 `container_manager._owned_container` 兜（restart 走它），读路径由
   `get_container_info` 兜（不归本部署所有时按 `absent` 回答）。判据是同一份
   `_assert_owned`，路由层不另写一套。
"""

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.console import (
    ActionOkResponse,
    ContainerInfo,
    LoginQrCodeResponse,
    NetworkConfig,
)
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.services.console import container_manager
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import assert_instance_accessible, get_tenant_scope

logger = get_sub_logger("console.container")

router = APIRouter(prefix="/instances", tags=["Console Container"])


async def _resolve_accessible_instance(user: DBUser, instance_key: str) -> DBAdapterInstance:
    """定位实例并校验归属

    顺序不能反：先 `resolve_instance()` 让平台方一侧的行为逐字不变（标识非法 400、
    实例不存在或已删除 404），再补一道归属校验。反过来的话，平台方拿一个非法标识
    会从 400 变成 404 —— 那是一次没必要的行为改动。

    对租户来说两条判据的结论一致：不属于自己的实例与不存在的实例都是 404。

    ## 两条查询必须落在**同一行**上

    `instance_key` 的唯一性只到 `unique_together = (adapter_key, instance_key)`，全局唯一
    从来没有被保证过（见 `models/db_adapter_instance.py` 的模块 docstring）。而这两条查询
    的判据并不相同：`resolve_instance()` 排除墓碑（`status="deleted"`），
    `assert_instance_accessible()` 不排除。于是同一个 `instance_key` 上并存两行时，
    「被授权的那一行」与「将要被操作的那一行」可以是不同的两行 ——

        租户名下 onebot_v11/dup（墓碑，id 小）+ 平台方的 minecraft/dup（在跑）
        ⇒ 归属校验命中租户那行放行，`resolve_instance` 命中平台方那行去重启

    容器名只由 `instance_key` 派生（`nekro_napcat_{key}`），归属标签又是本部署的，
    所以 `_owned_container` 这第二道也拦不住 —— 它拦的是**跨部署**，这里是同部署内跨租户。
    对上行号是唯一能把两条判据钉在同一行上的办法；不一致时按 404（与越权同一种回答，
    不泄露"这个标识上还有别的行"）。

    这一道**只对租户身份生效**：平台方一侧不存在需要防的边界（它对哪一行都有权），
    而对它也判一次会把"标识重名"变成一个新的 404 —— 那是一次没必要的行为改动。
    与 `assert_managed_user_accessible` 的目标等级判据只对租户生效是同一条理由。
    """
    instance = await container_manager.resolve_instance(instance_key)
    accessible = await assert_instance_accessible(user, instance_key)
    if get_tenant_scope(user) is not None and accessible.id != instance.id:
        logger.warning(
            f"实例标识 {instance_key!r} 在库里对应多行，归属校验命中 id={accessible.id}"
            f"（adapter={accessible.adapter_key}, tenant={accessible.tenant_id}）"
            f"而容器操作会落到 id={instance.id}（adapter={instance.adapter_key}, "
            f"tenant={instance.tenant_id}），拒绝本次操作",
        )
        raise NotFoundError(resource=f"实例 '{instance_key}'")
    return instance


class ContainerCreateRequest(BaseModel):
    """创建协议端容器

    刻意只暴露镜像标签：仓库名恒为白名单中的 `mlikiowa/napcat-docker`，
    容器名、挂载路径与端口绑定均由后端生成，不接受任何调用方输入。
    """

    image_tag: str = Field(default=container_manager.DEFAULT_IMAGE_TAG, description="NapCat 镜像标签")
    auto_pull: bool = Field(default=True, description="本地缺少镜像时自动拉取")


@router.get("/{instance_key}/container", summary="获取协议端容器信息")
@require_role(Role.Admin)
async def get_container(
    instance_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ContainerInfo:
    """获取实例的协议端容器信息（租户只能读自己名下的实例）"""
    instance = await _resolve_accessible_instance(_current_user, instance_key)
    info = await container_manager.get_container_info(instance)
    if info is None:
        raise ValidationError(reason=f"实例 '{instance_key}' 不是本机托管的 NapCat 容器，没有容器信息")
    return info


@router.post("/{instance_key}/container", summary="创建协议端容器")
@require_role(Role.Super)
async def create_container(
    instance_key: str,
    body: Optional[ContainerCreateRequest] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    """创建并启动实例的 NapCat 容器

    请求体可省略，此时按白名单仓库的默认标签创建。
    """
    instance = await container_manager.resolve_instance(instance_key)
    options = body or ContainerCreateRequest()
    info = await container_manager.create_container(instance, image_tag=options.image_tag, auto_pull=options.auto_pull)
    return ActionOkResponse(message=f"容器 {info.name} 已创建并启动")


@router.post("/{instance_key}/container/start", summary="启动协议端容器")
@require_role(Role.Super)
async def start_container(
    instance_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    """启动实例的 NapCat 容器"""
    instance = await container_manager.resolve_instance(instance_key)
    await container_manager.start_container(instance)
    return ActionOkResponse(message="容器已启动")


@router.post("/{instance_key}/container/stop", summary="停止协议端容器")
@require_role(Role.Super)
async def stop_container(
    instance_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    """停止实例的 NapCat 容器"""
    instance = await container_manager.resolve_instance(instance_key)
    await container_manager.stop_container(instance)
    return ActionOkResponse(message="容器已停止")


@router.post("/{instance_key}/container/restart", summary="重启协议端容器")
@require_role(Role.Admin)
async def restart_container(
    instance_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    """重启实例的 NapCat 容器（重启前同步网络配置）

    租户的自助运维出口：QQ 掉线时先重启协议端，是这类事件最常见也最有效的第一步。
    容器这一维的归属由 `container_manager._owned_container` 校验，跨部署同名容器 403。
    """
    instance = await _resolve_accessible_instance(_current_user, instance_key)
    await container_manager.restart_container(instance)
    return ActionOkResponse(message="容器已重启")


@router.delete("/{instance_key}/container", summary="删除协议端容器")
@require_role(Role.Super)
async def delete_container(
    instance_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    """删除实例的 NapCat 容器，保留登录态与配置数据"""
    instance = await container_manager.resolve_instance(instance_key)
    await container_manager.delete_container(instance)
    return ActionOkResponse(message="容器已删除，登录态数据已保留")


@router.get("/{instance_key}/container/qrcode", summary="获取扫码登录二维码")
@require_role(Role.Admin)
async def get_login_qrcode(
    instance_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> LoginQrCodeResponse:
    """获取扫码登录信息；取不到时返回 available=false 与说明文本，不视为错误

    重新扫码与重启并列，是掉线自愈的另一半：登录态过期时重启解决不了问题。
    """
    instance = await _resolve_accessible_instance(_current_user, instance_key)
    return await container_manager.get_login_qrcode(instance)


@router.post("/{instance_key}/container/quick-login", summary="快速登录")
@require_role(Role.Admin)
async def quick_login(
    instance_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    """复用容器内已缓存的登录态直接登录"""
    instance = await _resolve_accessible_instance(_current_user, instance_key)
    message = await container_manager.quick_login(instance)
    return ActionOkResponse(message=message)


@router.get("/{instance_key}/network-config", summary="获取协议端网络配置")
@require_role(Role.Super)
async def get_network_config(
    instance_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> NetworkConfig:
    """读取实例的协议端网络连接列表

    返回 NapCat 五种网络类型下的全部连接，`managed_by_nekro` 标出由 NA 托管的那条。
    配置里缺少托管连接时会补上一条按当前部署推导的结果，前端据此展示接入信息。

    **响应里带着托管连接的 OneBot 访问令牌**，因此这一条与写侧同档，都是平台方专属。
    """
    instance = await container_manager.resolve_instance(instance_key)
    return await container_manager.read_network_config(instance)


@router.put("/{instance_key}/network-config", summary="更新协议端网络配置")
@require_role(Role.Super)
async def update_network_config(
    instance_key: str,
    body: NetworkConfig,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    """写入实例的协议端网络连接列表，重启容器后生效

    托管连接的地址与令牌由后端生成，请求体里的对应字段会被忽略；其余连接经校验
    （同类型内名称唯一、端口合法且不冲突、出站地址必须是公网 http/https/ws/wss）后原样写入。
    """
    instance = await container_manager.resolve_instance(instance_key)
    await container_manager.write_network_config(instance, body)
    return ActionOkResponse(message="网络配置已更新，重启容器后生效")
