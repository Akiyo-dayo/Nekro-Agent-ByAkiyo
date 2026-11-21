"""网页聊天 MCP 工作区令牌的「曾经不归平台方」标记

网页聊天 MCP 的调用方以平台方 `admin` 身份行事，而工作区令牌（`nwc1.<工作区 id>.<HMAC>`）
是写进工作区 `.mcp.json` 的。工作区表没有归属列，归属只能从绑定频道现场推出来
（`DBChatChannel.workspace_id -> (adapter_key, instance_key) -> DBAdapterInstance.tenant_id`）。
推出来的归属只描述此刻：平台方新建的工作区没有频道，按平台方处理，令牌写进 `.mcp.json`；
租户频道绑上来之后工作区归了租户，令牌却已经留在租户读得到的地方；租户再解绑自己的频道，
工作区重新算成平台方。此时按「当前归属」判定会放行一张早已泄露的令牌。实例划给租户 /
从租户收回、频道迁移实例，都是同一种来回。

所以这里记的是**历史**：工作区只要出现过非平台方归属（租户独占或与平台方共用），就把它的 id
记进这个只增不减的集合。被标记的工作区不发也不认网页聊天令牌，`.mcp.json` 也不写这一条，
而且**永远不清除**——平台方还要用它就另建一个工作区。

## 什么时候记

- 写入处置位：归属从平台方变成非平台方的那些写操作，在改库**之前**先置位
  （绑定租户频道、租户实例归属变更、频道迁移实例、历史频道认领）；
  管理员发起的路径置位失败就拒绝这次操作；
- 看到就置位：任何一次推导出非平台方归属时顺手置位（严格版与批量版归属推导、CC 委托入口），
  兜住漏掉的写入路径；
- 启动回填：启动时把此刻不归平台方的工作区全部置位（`scan_workspace_taint`）。

## 建档与重建

文件里的 `seeded` 为真表示建档完成（那一刻库里的归属都扫过一遍）。文件不存在、刚挪开坏文件
或还没建档时，判定按「全部不合格」处理，下一次判定会经 `ensure_taint_record` 重扫重建。

库里的频道绑定只保留现状，没有「曾经绑过谁」的历史，所以首次建档补不回此前发生过的事：
曾经绑过租户频道、现在又回到平台方名下的工作区不会被标记。每次新建（或丢失后重建）记录且库里
有租户时打一条告警，提示这类工作区不要启用网页聊天条目。

## 失败方向：一律按不合格

- 内容坏了：把坏文件挪开留档，然后按「待重建」处理；待重建期间所有工作区都不合格；
- 读文件本身出错（被占用、没权限）：不挪文件，这一次按记录不可用处理（判定不合格、扫描不覆盖文件），
  等下一次读得到；
- 自动路径（归属推导、历史频道认领、CC 委托）写不进去：操作照常完成，记 error、置为待重建，
  这一笔留在内存里，下一次写成功时一起落盘；
- 管理员发起的归属变更（绑租户频道、实例归属变更、频道迁移）写不进去：抛
  `OperationFailedError`，调用方还没有改库，这次操作就此拒绝。否则归属已经变了、标记却没记下，
  全量重建也补不回它（那时它可能已经不在租户名下了）。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, FrozenSet, Iterable, Mapping, Optional, Set

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv
from nekro_agent.schemas.errors import OperationFailedError
from nekro_agent.services.workspace.network_guard import PLATFORM_GROUP

if TYPE_CHECKING:
    from nekro_agent.models.db_workspace import DBWorkspace

logger = get_sub_logger("web_chat_mcp.taint")

_TAINT_FILE_NAME = "web_chat_mcp_tainted_workspaces.json"
_FORMAT_VERSION = 1

# 本进程里置过位的全部工作区 id，不论写盘成没成
_memory_tainted: Set[int] = set()
# 自动路径写盘失败过：重建成功之前一律按不合格处理
_rebuild_pending = False
_rebuild_lock: Optional[asyncio.Lock] = None
# 后台重写 `.mcp.json` 的任务，持有强引用以免被回收
_scrub_tasks: Set[asyncio.Task[None]] = set()


@dataclass(frozen=True)
class _TaintState:
    ids: FrozenSet[int]
    seeded: bool


def taint_file() -> Path:
    """标记文件的路径；调用时现算，测试可以改道"""
    return Path(OsEnv.DATA_DIR) / "system" / _TAINT_FILE_NAME


def _read_state() -> Optional[_TaintState]:
    """文件不存在返回 None；内容坏了（解码、解析、结构不对）时把它挪开留档，同样返回 None

    读文件本身出错（`OSError`：被占用、没权限、句柄耗尽）**照抛，不挪文件**：那多半是暂时的，
    文件内容很可能完好。挪走就等于丢掉历史——重建只补得回此刻不归平台方的工作区，
    早先置过位、现在又回到平台方名下的那些会重新合格。调用方把这种异常当成「记录此刻不可用」。
    """
    path = taint_file()
    if not path.exists():
        return None
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
        raw_ids = data["workspace_ids"]
        seeded = data.get("seeded", False)
        if not isinstance(raw_ids, list) or not all(
            isinstance(item, int) and not isinstance(item, bool) for item in raw_ids
        ):
            raise TypeError("workspace_ids 必须是整数列表")
        if not isinstance(seeded, bool):
            raise TypeError("seeded 必须是布尔值")
    except Exception:  # noqa: BLE001 - 读不出来就挪开重建，见模块文档
        aside = path.with_name(f"{path.name}.broken-{time.strftime('%Y%m%d%H%M%S')}")
        try:
            os.replace(path, aside)
        except OSError:
            logger.error(
                f"网页聊天 MCP 的工作区标记记录读不出来，也挪不开（{path}），"
                "重建成功之前所有工作区令牌一律不生效",
                exc_info=True,
            )
        else:
            logger.error(
                f"网页聊天 MCP 的工作区标记记录读不出来，已挪到 {aside}，马上重建；"
                "重建成功之前所有工作区令牌一律不生效",
                exc_info=True,
            )
        return None
    return _TaintState(ids=frozenset(raw_ids), seeded=seeded)


def load_tainted_workspace_ids() -> Optional[FrozenSet[int]]:
    """判定用：读出曾经不归平台方的工作区 id；记录不存在、读不出来、待重建都返回 None

    `None` 表示记录不可用，调用方按「全部不合格」处理。
    """
    if _rebuild_pending:
        return None
    try:
        state = _read_state()
    except OSError:
        logger.error("读取网页聊天 MCP 的工作区标记记录失败，这次判定按记录不可用处理", exc_info=True)
        return None
    if state is None or not state.seeded:
        return None
    return state.ids | _memory_tainted


def _write(workspace_ids: Iterable[int], *, seeded: bool) -> None:
    """先写同目录临时文件再整体替换：写到一半被打断时，旧文件仍然完整"""
    path = taint_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(
        json.dumps(
            {"version": _FORMAT_VERSION, "seeded": seeded, "workspace_ids": sorted(workspace_ids)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _normalize_ids(workspace_ids: Iterable[int]) -> Set[int]:
    """规整成正整数集合：0、负数都不是合法的工作区 id"""
    wanted: Set[int] = set()
    for raw in workspace_ids:
        value = int(raw)
        if value > 0:
            wanted.add(value)
    return wanted


def _schedule_platform_secret_revoke(workspace_ids: Iterable[int], reason: str) -> None:
    """新置位的这些工作区，顺手把下发过的平台方凭据撤下来

    归属变化时**只删文件不够**：模型 Key 已经在 `claude` 进程的环境里了，所以
    `credential_policy` 那条路径还要停掉正在跑的容器。绑定租户频道、划转、启动扫描三处
    都会经过 `taint_workspaces`，挂在这里就覆盖了全部归属变化路径。

    同一条路径上还要挂起平台方服务器凭据（工作区变成非平台方归属的同一套处理）：
    工作区已经不归平台方，上面那些「能登进平台方服务器」的行不该继续可用。
    挂起只缩小权限，网关关着也做。

    **绝不抛异常**：这里正在给工作区置位，撤销只是一条后续动作——为它打断置位的话，
    归属已经变了、标记却没记下，全量重建也补不回来。导入失败同样只记 warning。
    """
    ids = list(workspace_ids)
    try:
        from nekro_agent.services.workspace.credential_policy import schedule_platform_secret_revoke

        schedule_platform_secret_revoke(ids, reason=reason)
    except Exception:  # noqa: BLE001 - 撤销失败不能打断置位
        logger.warning(f"调度平台方凭据撤销失败（{reason}）", exc_info=True)
    try:
        from nekro_agent.services.ssh_gateway.suspend import schedule_platform_row_suspend

        schedule_platform_row_suspend(ids, reason=reason)
    except Exception:  # noqa: BLE001 - 挂起失败不能打断置位
        logger.warning(f"调度平台方 SSH 服务器凭据挂起失败（{reason}）", exc_info=True)


def taint_workspaces(
    workspace_ids: Iterable[int],
    *,
    reason: str,
    refuse_on_failure: bool = False,
) -> Set[int]:
    """把这些工作区记成「曾经不归平台方」，返回本次新置位的 id

    读改写之间没有 await，所以在事件循环里是原子的。记不下来时：

    - `refuse_on_failure=True`（管理员发起的归属变更）：抛 `OperationFailedError`，调用方还没改库，
      这次操作就此拒绝；
    - 否则（自动路径）不抛：记 error、置为待重建（重建成功之前所有工作区令牌都不生效），
      这一笔留在内存里等下一次写成功；清理 `.mcp.json` 的任务照常调度，清理本身只是纵深防御。
    """
    global _rebuild_pending

    wanted = _normalize_ids(workspace_ids)
    if not wanted:
        return set()

    added: Set[int] = set()
    try:
        state = _read_state()
        current = state.ids if state else frozenset()
        target = current | wanted | _memory_tainted
        added = wanted - current - _memory_tainted
        if target != current:
            _write(target, seeded=state.seeded if state else False)
    except Exception as e:
        if refuse_on_failure:
            logger.error(f"记不下网页聊天 MCP 的工作区标记（{reason}），这次操作已拒绝", exc_info=True)
            raise OperationFailedError(
                operation="写入网页聊天 MCP 工作区标记",
                detail="曾经不归平台方的工作区记不下来，这次操作没有执行，请检查数据目录能否写入",
            ) from e
        _memory_tainted.update(wanted)
        _rebuild_pending = True
        logger.error(
            f"记不下网页聊天 MCP 的工作区标记（{reason}）：已置为待重建，重建成功之前所有工作区令牌一律不生效；"
            "这一笔留在内存里等下一次写成功，进程在那之前重启就丢了",
            exc_info=True,
        )
        if added:
            logger.warning(f"新标记 {len(added)} 个网页聊天 MCP 工作区（{reason}）：{sorted(added)}")
            _schedule_scrub(added)
            _schedule_platform_secret_revoke(added, reason)
        return added

    _memory_tainted.update(wanted)
    if added:
        logger.warning(f"新标记 {len(added)} 个网页聊天 MCP 工作区（{reason}）：{sorted(added)}")
        _schedule_scrub(added)
        _schedule_platform_secret_revoke(added, reason)
    return added


def note_workspace_owner_group(workspace_id: int, group: str, *, reason: str) -> None:
    """归属推导时顺手置位；平台方分组与内存里已置位的不再处理

    绝不抛异常：置位是兜底，不能反过来把一次归属推导打断。
    """
    if group == PLATFORM_GROUP or workspace_id in _memory_tainted:
        return
    try:
        taint_workspaces([workspace_id], reason=reason)
    except Exception:  # noqa: BLE001 - 兜底置位不打断归属推导
        logger.warning(f"按归属推导置位工作区 {workspace_id} 的网页聊天 MCP 标记失败（{reason}）", exc_info=True)


def note_workspace_owner_groups(groups: Mapping[int, str], *, reason: str) -> None:
    """批量版归属推导的顺手置位；只处理非平台方且内存里还没有的 id

    与单个版本一样绝不抛异常。
    """
    wanted = sorted(
        workspace_id
        for workspace_id, group in groups.items()
        if group != PLATFORM_GROUP and workspace_id not in _memory_tainted
    )
    if not wanted:
        return
    try:
        taint_workspaces(wanted, reason=reason)
    except Exception:  # noqa: BLE001 - 兜底置位不打断归属推导
        logger.warning(
            f"按归属推导批量置位网页聊天 MCP 标记失败（{reason}）：{wanted}",
            exc_info=True,
        )


async def instance_owner_tenant(adapter_key: str, instance_key: str) -> Optional[int]:
    """按 (适配器, 实例) 查出实例归属的租户 id

    空实例、查不到实例行都按平台方处理（返回 None），判据与严格版归属推导一致。
    查询本身出错照抛。
    """
    if not instance_key:
        return None

    from nekro_agent.models.db_adapter_instance import DBAdapterInstance

    row = await DBAdapterInstance.filter(adapter_key=adapter_key, instance_key=instance_key).first()
    return row.tenant_id if row is not None else None


async def taint_workspaces_bound_to_instance_keys(
    instance_keys: Iterable[str],
    *,
    reason: str,
    refuse_on_failure: bool = False,
) -> Set[int]:
    """把绑在这些实例名下频道上的工作区全部置位

    查询异常照抛：管理员路径要靠它拒绝操作。
    """
    keys = sorted({key for key in instance_keys if key})
    if not keys:
        return set()

    from nekro_agent.models.db_chat_channel import DBChatChannel

    workspace_ids = await DBChatChannel.filter(
        instance_key__in=keys,
        workspace_id__not_isnull=True,
    ).values_list("workspace_id", flat=True)
    return taint_workspaces(
        [int(workspace_id) for workspace_id in workspace_ids if workspace_id is not None],
        reason=reason,
        refuse_on_failure=refuse_on_failure,
    )


async def note_channel_workspace_access(
    workspace: "DBWorkspace",
    *,
    adapter_key: str,
    instance_key: str,
    reason: str,
) -> None:
    """某个频道取用了它绑定的工作区：频道的实例归租户，就把工作区置位

    插件工具（CC 委托、收发工作区文件、知识库、记忆）都经 `AgentCtx.get_bound_workspace` 取工作区，
    这条路径上没有别的归属推导，所以在这里补一次。只看取用方这一个频道（一次查询），不推导整个
    工作区的归属：平台方频道取用不构成威胁。绝不抛异常，读不到实例归属只记警告——
    真正执行 CC 任务的 `_cc_delegate_task` 另有一次「读不到按非平台方」的置位。
    """
    try:
        if await instance_owner_tenant(adapter_key, instance_key) is not None:
            taint_workspaces([workspace.id], reason=reason)
    except Exception:  # noqa: BLE001 - 兜底置位不打断工具调用
        logger.warning(f"按频道取用置位工作区 {workspace.id} 的网页聊天 MCP 标记失败（{reason}）", exc_info=True)


async def taint_workspace_if_non_platform(workspace: "DBWorkspace", *, reason: str) -> bool:
    """按此刻的归属置位这个工作区，返回它是否不归平台方

    读不到归属时按非平台方处理：授权判定不能把「读不到」当成平台方。置位这一步本身再出异常
    也吞掉，返回值不变。
    """
    from nekro_agent.services.workspace.network_guard import resolve_workspace_owner_group_strict

    try:
        group = await resolve_workspace_owner_group_strict(workspace)
    except Exception as e:  # noqa: BLE001 - 读不到归属按非平台方处理
        logger.warning(f"读取工作区 {workspace.id} 的归属失败，按非平台方处理（{reason}）: {e}")
        try:
            taint_workspaces([workspace.id], reason=f"{reason}（读不到归属，按非平台方处理）")
        except Exception:  # noqa: BLE001 - 置位失败不改变判定结果
            logger.warning(f"置位工作区 {workspace.id} 的网页聊天 MCP 标记失败（{reason}）", exc_info=True)
        return True
    return group != PLATFORM_GROUP


async def scan_workspace_taint() -> None:
    """全量扫描：把此刻不归平台方的工作区全部置位，并完成建档

    启动时调用一次；判定前发现记录不可用时经 `ensure_taint_record` 再调。写不进去时抛异常。
    """
    global _rebuild_pending

    from nekro_agent.models.db_tenant import DBTenant
    from nekro_agent.models.db_workspace import DBWorkspace
    from nekro_agent.services.workspace.tenant_scope import resolve_workspace_owner_groups_strict

    state = _read_state()
    record_missing = state is None or not state.seeded
    workspace_ids = sorted(await DBWorkspace.all().values_list("id", flat=True))
    groups = await resolve_workspace_owner_groups_strict(workspace_ids)
    found = {workspace_id for workspace_id, group in groups.items() if group != PLATFORM_GROUP}

    current = state.ids if state else frozenset()
    added = found - current - _memory_tainted
    _write(current | found | _memory_tainted, seeded=True)
    _memory_tainted.update(found)
    _rebuild_pending = False
    if added:
        logger.warning(f"新标记 {len(added)} 个网页聊天 MCP 工作区（启动回填）：{sorted(added)}")
        _schedule_scrub(added)

    if record_missing and await DBTenant.all().exists():
        logger.warning(
            "网页聊天 MCP 的工作区标记记录是新建（或丢失后重建）的，之前的历史无从追溯："
            "曾经绑过租户频道、现在又回到平台方名下的工作区不会被标记。"
            "这类工作区不要启用网页聊天条目，需要时另建工作区",
        )


async def ensure_taint_record() -> bool:
    """判定前调用：记录不存在、读不出来、没建完档或待重建时，马上重扫重建；返回记录此刻是否可用"""
    global _rebuild_lock

    if load_tainted_workspace_ids() is not None:
        return True
    if _rebuild_lock is None:
        _rebuild_lock = asyncio.Lock()
    async with _rebuild_lock:
        if load_tainted_workspace_ids() is not None:
            return True
        try:
            await scan_workspace_taint()
        except Exception:  # noqa: BLE001 - 重建失败就继续按不合格，下次判定再试
            logger.error("重建网页聊天 MCP 的工作区标记记录失败，所有工作区令牌继续不生效", exc_info=True)
            return False
    return load_tainted_workspace_ids() is not None


async def is_workspace_web_chat_token_eligible(workspace: "DBWorkspace") -> bool:
    """这个工作区能不能发 / 认网页聊天令牌

    三个条件缺一不可：标记记录可用、它从来没被标记过、此刻仍归平台方。任何异常都按不合格处理。
    """
    try:
        if not await ensure_taint_record():
            return False
        tainted = load_tainted_workspace_ids()
        if tainted is None or workspace.id in tainted:
            return False

        from nekro_agent.services.workspace.network_guard import resolve_workspace_owner_group_strict

        return await resolve_workspace_owner_group_strict(workspace) == PLATFORM_GROUP
    except Exception as e:  # noqa: BLE001 - 判定出错一律按不合格
        logger.warning(f"判定工作区 {workspace.id} 的网页聊天 MCP 令牌资格失败，按不合格处理: {e}")
        return False


def _schedule_scrub(workspace_ids: Iterable[int]) -> None:
    """把重写 `.mcp.json` 的清理放到后台任务

    不能在持有 MCP 锁的代码里同步 await 清理：`asyncio.Lock` 不可重入。
    清理只是纵深防御——令牌是否有效不依赖它，入口按标记判。
    """
    ids = sorted(_normalize_ids(workspace_ids))
    if not ids:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("没有运行中的事件循环，跳过网页聊天 MCP 条目的 .mcp.json 清理")
        return
    task = loop.create_task(_scrub(ids))
    _scrub_tasks.add(task)
    task.add_done_callback(_scrub_tasks.discard)


async def _scrub(workspace_ids: Iterable[int]) -> None:
    """逐个重写被标记工作区的 `.mcp.json`：重写后里面就没有网页聊天条目了"""
    from nekro_agent.models.db_workspace import DBWorkspace
    from nekro_agent.services.workspace.manager import WorkspaceService

    for workspace_id in workspace_ids:
        try:
            workspace = await DBWorkspace.get_or_none(id=workspace_id)
            if workspace is None:
                continue
            # 只看「工作区目录初始化过没有」，不按路径判 `.mcp.json` 存不存在：那个判断会跟着
            # 容器放的链接走（链接悬空就被当成没有、跳过清理）。重写本身逐级不跟随链接
            default_dir = WorkspaceService.get_workspace_dir(workspace_id) / "default"
            if not os.path.lexists(default_dir):
                continue
            await WorkspaceService.sync_mcp_to_disk(workspace)
        except Exception as e:  # noqa: BLE001 - 单个工作区清理失败不拖垮其余
            logger.warning(f"清理工作区 {workspace_id} 的网页聊天 MCP 条目失败: {e}")
