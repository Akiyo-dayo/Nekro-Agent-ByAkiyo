"""工作区变成非平台方归属时，挂起它上面的平台方服务器凭据

## 为什么不删只挂起

平台方服务器只能挂在「能拿平台方凭据」的工作区上。工作区一旦变成非平台方归属，它里面
可能已经留下租户放的指令或脚本（与工作区资源、模型 Key 同一条理由）。挂起是**粘的**：
平台方把工作区要回来也不会自动解除，得在新的平台方工作区上重新建——和「曾经不归平台方」
标记本身一样，错误的恢复方向是放行，而这里宁可不放行。

## 为什么总开关关着也挂起

挂起只缩小权限：它不影响今天任何一条已有路径（网关关着时这些行本来也用不上），却保证
下次开闸时不会因为「那段时间闸关着、归属变了没人处理」而把凭据交给出事后的工作区。
所以这里没有 `SSH_GATEWAY_ENABLED` 判断。

## 三条入口

- 归属变化的那一刻：`web_chat_workspace_taint` 的「曾经不归平台方」标记新置位后，
  调度一批挂起（与平台方凭据撤销同一条路径）；
- 启动兜底：`suspend_platform_rows_on_startup()` 把挂着平台方行、当下又不过判据的
  工作区补挂一遍，覆盖标记在别的路径上变了而调度没跑到的情形；
- 复核：网关每次连接、每个通道都会再判一次（后续网关任务），挂起只是把状态提前写明。
"""

from __future__ import annotations

import asyncio
from typing import Iterable, List, Set

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
from nekro_agent.services.ssh_gateway import constants as C
from nekro_agent.services.ssh_gateway import events
from nekro_agent.services.ssh_gateway.credentials import ssh_credential_service

logger = get_sub_logger("ssh_gateway.suspend")

# 后台挂起任务，持有强引用以免被事件循环回收
_suspend_tasks: Set[asyncio.Task[None]] = set()


def _normalize_ids(workspace_ids: Iterable[int]) -> List[int]:
    """只保留正整数工作区 id；乱值直接丢掉，不让一次调度把整批挂起拖垮"""
    normalized: List[int] = []
    for raw in workspace_ids:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            normalized.append(value)
    return sorted(set(normalized))


async def suspend_platform_rows(workspace_ids: Iterable[int], *, reason: str) -> int:
    """把这些工作区上的平台方行改成 `suspended` 并逐行记审计，返回改了几行

    已经是 `suspended` 的行不再改、也不再记审计：启动兜底与归属变化两条路径会重复
    覆盖同一批工作区，重复的审计只会让「什么时候真的挂起过」变得难读。

    `status_reason` 固定写 :data:`constants.REASON_WORKSPACE_NOT_PLATFORM`，它是取值集合
    里的理由码；`reason` 只进审计 `detail` 与日志，是给人看的那句话。
    """
    ids = _normalize_ids(workspace_ids)
    if not ids:
        return 0

    rows = await DBWsSshCredential.filter(
        workspace_id__in=ids,
        owner_kind=C.OWNER_PLATFORM,
    ).exclude(status=C.STATUS_SUSPENDED)
    changed = 0
    for row in rows:
        row.status = C.STATUS_SUSPENDED
        row.status_reason = C.REASON_WORKSPACE_NOT_PLATFORM
        # 审计写的是"这一行被挂起"这件事实；detail 里放触发原因，不碰任何凭据值
        await ssh_credential_service._write_audit(  # noqa: SLF001 - 与凭据服务共用同一份审计写法
            row,
            event=C.EVENT_SUSPEND,
            allowed=True,
            actor="system",
            detail=reason,
        )
        await row.save(update_fields=["status", "status_reason", "update_time"])
        await events.notify_credential_changed(int(row.id))
        changed += 1

    if changed:
        logger.warning(f"已挂起 {changed} 行平台方 SSH 服务器凭据（工作区 {ids}）：{reason}")
    return changed


def schedule_platform_row_suspend(workspace_ids: Iterable[int], *, reason: str) -> None:
    """把挂起放进后台任务（调用点可能正持有别的锁或处在同步上下文里）

    照 `credential_policy.schedule_platform_secret_revoke` 的写法：没有运行中的事件循环
    就记 debug 跳过。归属变化那条路径下一次启动的兜底会补上，不会永远漏掉。
    """
    ids = _normalize_ids(workspace_ids)
    if not ids:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("没有运行中的事件循环，跳过平台方 SSH 服务器凭据的挂起")
        return
    task = loop.create_task(_suspend_workspaces(ids, reason=reason))
    _suspend_tasks.add(task)
    task.add_done_callback(_suspend_tasks.discard)


async def _suspend_workspaces(workspace_ids: Iterable[int], *, reason: str) -> None:
    """逐个工作区挂起；单个失败不拖垮其余，也绝不把异常抛给调度方"""
    for workspace_id in workspace_ids:
        try:
            await suspend_platform_rows([workspace_id], reason=reason)
        except Exception as e:  # noqa: BLE001 - 后台任务，失败只能记日志
            logger.warning(f"挂起工作区 {workspace_id} 的平台方 SSH 服务器凭据失败（{reason}）: {e}")


async def wait_pending_suspends() -> None:
    """等所有在跑 / 已排队的挂起任务收尾；测试用它来同步后台任务"""
    while _suspend_tasks:
        await asyncio.gather(*list(_suspend_tasks), return_exceptions=True)


async def _definitely_not_platform(workspace: DBWorkspace) -> bool:
    """这个工作区是否**确定**不再归平台方

    `platform_credentials_allowed` 在判不出来时（标记记录读不出、重建扫描失败、库异常）
    也返回「不能拿」——那是授权路径该有的取向。挂起却是粘的，只能删掉重建、重新填凭据，
    不能因为一次读文件失败就把所有平台方行永久挂起。所以这里只认确定的结论：
    记录可用且这个工作区被标记过，或者此刻的归属明确不是平台方。判不出来不挂起
    （网关的每次授权与复核仍然按「不能拿」拒，安全上不受影响）。
    """
    from nekro_agent.services.plugin.scope_guard import deployment_has_tenants

    if not await deployment_has_tenants():
        return False

    from nekro_agent.services.mcp.web_chat_workspace_taint import (
        ensure_taint_record,
        load_tainted_workspace_ids,
    )
    from nekro_agent.services.workspace.network_guard import (
        PLATFORM_GROUP,
        resolve_workspace_owner_group_strict,
    )

    if not await ensure_taint_record():
        raise RuntimeError("工作区标记记录不可用")
    tainted = load_tainted_workspace_ids()
    if tainted is None:
        raise RuntimeError("工作区标记记录不可用")
    if int(workspace.id) in tainted:
        return True
    return await resolve_workspace_owner_group_strict(workspace) != PLATFORM_GROUP


async def suspend_platform_rows_on_startup() -> int:
    """启动兜底：把当下**确定**不过判据、却还挂着活动平台方行的工作区补挂一遍

    只查「有平台方行且状态不是 suspended」的工作区，逐个重新判（不信任任何缓存）。
    判不出来的（标记记录读不出等）不挂起，只记 WARNING，见 `_definitely_not_platform`。
    每个工作区单独 try，一次复核失败不拦住启动。

    Returns:
        被改成 `suspended` 的行数。
    """
    workspace_ids = [
        int(workspace_id)
        for workspace_id in await DBWsSshCredential.filter(owner_kind=C.OWNER_PLATFORM)
        .exclude(status=C.STATUS_SUSPENDED)
        .values_list("workspace_id", flat=True)
        if workspace_id is not None
    ]
    total = 0
    for workspace_id in _normalize_ids(workspace_ids):
        try:
            workspace = await DBWorkspace.get_or_none(id=workspace_id)
            if workspace is None:
                continue
            if not await _definitely_not_platform(workspace):
                continue
            total += await suspend_platform_rows([workspace_id], reason="启动复核")
        except Exception as e:  # noqa: BLE001 - 单个工作区失败不拖垮其余；判不出来就不挂起
            logger.warning(f"启动复核工作区 {workspace_id} 的平台方 SSH 服务器凭据判不出来，这次不挂起: {e}")
    if total:
        logger.info(f"启动复核：挂起 {total} 行平台方 SSH 服务器凭据")
    return total


__all__ = [
    "schedule_platform_row_suspend",
    "suspend_platform_rows",
    "suspend_platform_rows_on_startup",
    "wait_pending_suspends",
]
