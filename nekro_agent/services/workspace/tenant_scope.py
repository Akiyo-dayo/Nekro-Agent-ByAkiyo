"""CC 工作区的租户作用域

## 归属是推出来的，不是存出来的

工作区表本身没有租户列，归属只能从「它服务哪些频道」推：
`DBChatChannel.workspace_id -> instance_key -> DBAdapterInstance.tenant_id`。

这条链已经被网络分组用着（`services/workspace/network_guard.py` 的
`resolve_workspace_network_group`），本模块**直接复用它**而不是另推一套。
两处一旦分叉，就会出现「API 认为这是 A 租户的工作区，网络却把它挂在平台组的网桥上」
这种彼此矛盾的状态，而且没有任何运行时信号。

不把归属写成 `metadata["owner_tenant_id"]` 也是同一个理由：那是第二套归属来源，
网络守卫读不到，必然分叉。

## 三种归属各自的语义

`workspace_group_key` 的三种取值直接决定访问语义：

- `t{id}`（全部绑定频道同属一个租户）—— 该租户可见可管，其他租户一律 404。
- `PLATFORM_GROUP`（没有任何频道绑定，或绑定的频道都属于平台方 / 未归属实例）——
  **平台方专属**。新建而尚未绑定频道的工作区落在这里：此时没有任何归属信号，
  按「谁建的归谁」记账就是上面说的第二套来源。配套决策是把「创建工作区」提到
  `Role.Super` —— 否则租户建完立刻看不见自己刚建的东西，那是个必然的 bug。
- `SHARED_GROUP`（归属不唯一）—— **对所有租户 404，只有平台方能碰**。
  工作目录、会话历史、记忆库、知识库、产物文件都是**整个工作区一份**；
  它同时服务了两个归属的频道，就意味着两边的数据混在同一个目录里。
  给任何一侧哪怕只读，都是把另一侧的数据交出去，所以这里不是「降级为只读」而是拒绝。
  平台方仍然完整可见可操作，拆解（解绑到只剩一侧）本来也只该由平台方来做。

## 未启用多租户时

`get_tenant_scope()` 对平台方恒为 `None`，本模块的每个判定都在第一步就返回，
一次归属推导都不做，行为与改造前逐字等价。
"""

from typing import Dict, List, Optional, Sequence

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.schemas.errors import NotFoundError
from nekro_agent.services.user.tenant_perm import accessible_instance_keys, get_tenant_scope
from nekro_agent.services.workspace.network_guard import (
    PLATFORM_GROUP,
    resolve_workspace_network_group,
    workspace_group_key,
)

logger = get_sub_logger("workspace.tenant_scope")


def tenant_group_key(tenant_id: int) -> str:
    """某个租户独占时的分组键

    走 `workspace_group_key` 生成而不是自己拼 `f"t{id}"`：键的格式只能有一个定义处，
    否则网络分组换了写法而这里没跟上，所有租户会静默变成「谁的工作区都看不到」。
    """
    return workspace_group_key([tenant_id])


async def resolve_workspace_owner_groups(workspace_ids: Sequence[int]) -> Dict[int, str]:
    """批量推导一批工作区各自的归属分组键

    与 `resolve_workspace_network_group` 是同一套判定，只是把「一次两查」摊平成
    「一批两查」：列表接口一页可以有 100 个工作区，逐个推导就是 200 次查询。
    `tests/test_workspace_tenant_scope.py` 钉住了两者在全部边界上给出相同结论。

    读不到归属时（查询失败）按平台方分组处理，与单个版本一致 —— 对租户而言这是拒绝，
    不会把工作区错误地判给某个租户。

    Args:
        workspace_ids: 工作区 ID

    Returns:
        `{workspace_id: 分组键}`，入参里的每个 ID 都有一项
    """
    ids = sorted({int(workspace_id) for workspace_id in workspace_ids})
    groups: Dict[int, str] = dict.fromkeys(ids, PLATFORM_GROUP)
    if not ids:
        return groups

    try:
        rows = await DBChatChannel.filter(workspace_id__in=ids).values("workspace_id", "instance_key")
    except Exception as e:
        logger.warning(f"批量读取工作区的频道绑定失败，全部按平台方分组处理: {e}")
        return groups

    keys_by_workspace: Dict[int, List[str]] = {}
    for row in rows:
        keys_by_workspace.setdefault(int(row["workspace_id"]), []).append(str(row["instance_key"] or ""))
    if not keys_by_workspace:
        return groups

    named = sorted({key for keys in keys_by_workspace.values() for key in keys if key})
    tenant_by_key: Dict[str, Optional[int]] = {}
    if named:
        try:
            instance_rows = await DBAdapterInstance.filter(instance_key__in=named).values("instance_key", "tenant_id")
        except Exception as e:
            logger.warning(f"批量读取工作区所属实例的租户归属失败，全部按平台方分组处理: {e}")
            return groups
        tenant_by_key = {str(row["instance_key"]): row["tenant_id"] for row in instance_rows}

    # 空 instance_key 是多账号化之前尚未被认领的历史频道，查不到实例行的同理，两者都按平台方计入
    for workspace_id, keys in keys_by_workspace.items():
        groups[workspace_id] = workspace_group_key([tenant_by_key.get(key) if key else None for key in keys])
    return groups


async def accessible_workspace_ids(user: DBUser) -> Optional[List[int]]:
    """列出该身份可见的工作区 ID

    Returns:
        `None` 表示平台方、不受限（与 `accessible_instance_keys` 的空列表语义不同：
        租户一个工作区都看不到时返回的是空列表，那确实是「什么都看不到」）。
    """
    scope = get_tenant_scope(user)
    if scope is None:
        return None

    instance_keys = await accessible_instance_keys(user)
    if not instance_keys:
        return []
    candidates = {
        int(workspace_id)
        for workspace_id in await DBChatChannel.filter(
            instance_key__in=instance_keys,
            workspace_id__isnull=False,
        ).values_list("workspace_id", flat=True)
        if workspace_id is not None
    }
    if not candidates:
        return []

    own_group = tenant_group_key(scope)
    groups = await resolve_workspace_owner_groups(candidates)
    return sorted(workspace_id for workspace_id in candidates if groups.get(workspace_id) == own_group)


async def assert_workspace_accessible(user: DBUser, workspace_id: int) -> DBWorkspace:
    """校验当前身份能否访问某个工作区，并返回它

    越权抛 **404 而不是 403**：403 等于告诉租户「这个工作区确实存在」，
    把平台方与其他租户的工作区变成可枚举的。错误文案也与「不存在」逐字相同。
    """
    scope = get_tenant_scope(user)
    workspace = await DBWorkspace.get_or_none(id=workspace_id)
    if workspace is None:
        raise NotFoundError(resource=f"工作区 {workspace_id}")
    if scope is None:
        return workspace
    if await resolve_workspace_network_group(workspace) != tenant_group_key(scope):
        raise NotFoundError(resource=f"工作区 {workspace_id}")
    return workspace
