"""CC 工作区不下发平台方凭据

## 哪几样算「平台方凭据」

容器里能读到的东西里，下面几样是**平台方**的、而不是这个工作区自己的：

1. **模型 Key**：`NEKRO_CC_CREDENTIAL_MODE=direct` 时把 CC 模型预设的真实 `auth_token`
   写进工作区的 `settings.json` 与 CC 用户级设置（CC home 里的 `settings.json`，容器里
   `~/.claude/settings.json`；home 在哪见 `WorkspaceService.claude_home`；早先在
   `.claude/settings.json`），cc-sandbox 每次拉起 `claude`
   进程时再把它读进进程环境。于是 CC 的 Bash 一句 `echo` 就能拿到，会话还能用
   `download_file_from_cc` 把文件取回 NA 侧。
2. **工作区资源**：SSH 私钥、root 口令、数据库密码、访问令牌。它们由平台方建立、由平台方
   绑定，但每次向 CC 发请求都会被注入 CC 进程的环境，非敏感字段还会写进 `CLAUDE.md`。
3. **全局 MCP 库条目的 `env` / `headers`**：条目只有平台方能改，但租户管理员能把库里的
   条目加进自己的工作区——连凭据一起写进 `.mcp.json`，而且工作区 MCP 列表接口原样把
   它们返回给租户管理员。
4. **CC 模型预设的 `extra_env`**：原样写进 CC 用户级设置，任何模式都写。
   运维把别的凭据塞进 `extra_env` 时，那份凭据也躺在容器可读的文件里。

## 判据：这个工作区能不能拿平台方凭据

`platform_credentials_allowed()`：

- 单租户部署（没有任何实例挂在租户名下）→ 能，行为与改造前一致，一个标记文件都不读；
- 否则复用网页聊天 MCP 令牌的**同一套**严格判据
  （`is_workspace_web_chat_token_eligible`）：标记记录可用 ∧ 这个工作区从来没被标记过
  ∧ 此刻严格推导是平台方分组。三者缺一不可，任何异常按不能拿处理。

带上「曾经不归平台方」这一条，是因为租户用过的工作区目录里可能留着他放的东西（例如写进
`CLAUDE.md` 的指令、会在下次运行时把环境变量发出去的脚本）。工作区解绑回平台方名下以后
再把真 Key 放进来，等于把 Key 交给那些东西。两处用同一个判据，不分叉。

## 只删文件不够：Key 在进程环境里

模型 Key 必须在整个 LLM 调用期间待在 `claude` 进程的环境里，而 CC 的 Bash 工具就是它的
子进程，`echo $ANTHROPIC_AUTH_TOKEN` 一样拿得到。删掉 `settings.json` 只是把门关上、
窗户还开着——所以工作区一旦变成不归平台方，**正在跑的容器要停掉**，下次启动才是干净的
新容器。停之前先把三份配置文件按新判据重写一遍（纵深防御），停之后再清掉「下发过」标记。

## `relay` 模式下模型 Key 本来就不进容器

`relay`（默认）时容器里只有一把只对本工作区有效的中继令牌，真 Key 留在 NA 进程侧，
所以第 1 条与模式有关。但第 2 ~ 4 条**与模式无关**：资源、MCP 凭据、`extra_env` 在
`relay` 下照旧会进容器，一样按本模块的判据拦。
"""

from __future__ import annotations

import asyncio
from typing import Dict, Iterable, List, Set

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_workspace import DBWorkspace

logger = get_sub_logger("workspace.credential_policy")

# 判据不过时不进容器的那句报错：调用方原样回给用户，不自己拼文案
WITHHELD_MESSAGE = "本部署的 CC 凭据模式是 direct，租户工作区不提供平台方模型，请联系平台方改用 relay"

# 工作区 metadata 上的三个标记
METADATA_SECRET_DELIVERED_KEY = "cc_platform_secret_delivered"
METADATA_DIRECT_WITHHELD_KEY = "cc_direct_withheld"
# 启动复核已经看过这个工作区的存量（不能拿的已撤销、能拿的已按配置补记「下发过」）：之后所有写
# 凭据的路径都按判据走、写出去就记标记，再停一次容器只是白白打断任务，再推断一遍也没有必要
METADATA_STARTUP_SWEPT_KEY = "cc_platform_secret_swept"

# 后台重写配置文件 / 停容器的任务，持有强引用以免被回收
_revoke_tasks: Set[asyncio.Task[None]] = set()


class PlatformCredentialWithheldError(Exception):
    """这个工作区不能拿平台方凭据，而这次操作又必须拿到（`direct` 模式下的 CC 调用）"""


def _metadata_flag(workspace: DBWorkspace, key: str) -> bool:
    """metadata 上的布尔标记，缺失或非真一律按假"""
    return bool((workspace.metadata or {}).get(key))


async def platform_credentials_allowed(workspace: DBWorkspace) -> bool:
    """这个工作区能不能拿平台方凭据（模型 Key、资源、MCP 凭据、`extra_env`）

    单租户部署直接放行、一个标记文件都不读（没有租户就没有这条边界，行为与改造前一致）。
    多租户部署复用网页聊天 MCP 令牌的严格判据：标记记录可用、从来没被标记过、此刻仍归平台方。

    **整个函数不抛异常**：判据出错一律按不能拿处理并记 WARNING。调用方里有写盘与发请求的
    路径，那里抛出去的表现是「配置写不出来」或「CC 调用 500」，而不是「凭据没下发」——
    一个把失效方向搞反的报错比拒绝本身更危险。
    """
    try:
        from nekro_agent.services.plugin.scope_guard import deployment_has_tenants

        if not await deployment_has_tenants():
            return True

        from nekro_agent.services.mcp.web_chat_workspace_taint import is_workspace_web_chat_token_eligible

        return await is_workspace_web_chat_token_eligible(workspace)
    except Exception as e:  # noqa: BLE001 - 判据出错一律按不能拿处理
        logger.warning(f"判定工作区 {workspace.id} 能否拿平台方凭据失败，按不能拿处理: {e}")
        return False


async def mark_platform_secret_delivered(workspace: DBWorkspace) -> None:
    """记下「这个工作区拿到过平台方凭据」

    归属变化时靠这个标记判断「要不要停掉正在跑的容器」：停容器挡的是进程环境里已经存在的
    那一份，而只有发过凭据的工作区才有那一份。幂等：已经为真就不再落库。
    """
    if _metadata_flag(workspace, METADATA_SECRET_DELIVERED_KEY):
        return
    # 按键写：它是受保护的键（`GUARDED_METADATA_KEYS`），别处拿旧对象整份写回 metadata 也抹不掉它
    from nekro_agent.models.db_workspace import patch_workspace_metadata

    await patch_workspace_metadata(workspace, set_keys={METADATA_SECRET_DELIVERED_KEY: True})


async def prepare_cc_dispatch(workspace: DBWorkspace) -> Dict[str, str]:
    """向 CC 发消息之前的统一入口，返回这次要注入 CC 进程的环境变量

    三档：

    - 判据不过而 `direct` 模式：抛 :class:`PlatformCredentialWithheldError`，调用方
      原样把 :data:`WITHHELD_MESSAGE` 回给用户，**不去调容器**（否则用户看到的是一句
      `claude` CLI 的鉴权失败，看不出真因）；
    - 判据不过而 `relay` 模式：返回空字典（模型侧本来就不发真 Key，这里拦的是资源注入）；
    - 判据通过：按旧行为解出资源环境变量并返回。

    判据不过、而标记说这个工作区**曾经**拿到过平台方凭据时，先走一次撤销：那条路径会重写
    配置文件、必要时停掉容器。覆盖「归属在别的路径上变了、撤销没跑到」这一种兜底。
    """
    allowed = await platform_credentials_allowed(workspace)
    if not allowed:
        if _metadata_flag(workspace, METADATA_SECRET_DELIVERED_KEY):
            await revoke_platform_secrets(workspace, reason="CC 调用前复核")

        from nekro_agent.services.workspace.cc_credential import credential_mode

        if credential_mode() == "direct":
            raise PlatformCredentialWithheldError(WITHHELD_MESSAGE)
        return {}

    from nekro_agent.services.resources import workspace_resource_service

    env = await workspace_resource_service.resolve_workspace_resources_to_env(
        workspace.id,
        allow_platform_secrets=True,
    )
    if env:
        await mark_platform_secret_delivered(workspace)
    return env


async def revoke_platform_secrets(
    workspace: DBWorkspace,
    *,
    reason: str,
    force_stop: bool = False,
) -> None:
    """把这个工作区上的平台方凭据撤下来：重写配置文件，必要时停容器，清掉标记

    Args:
        workspace: 工作区记录
        reason: 写进 WARNING 的原因，供排查用
        force_stop: 标记为假时也照样停容器。启动复核用它兜存量——升级前下发的凭据没有标记，
            而容器可能正跑着

    每一步单独 try：撤销是清理动作，一处失败不该把其余步骤（尤其是停容器）一起丢掉。
    """
    stopped = False

    # 1. 三份配置文件按当前判据重写：这一步之后磁盘上已经没有凭据了
    preset = None
    try:
        from nekro_agent.services.workspace.container import SandboxContainerManager

        preset = await SandboxContainerManager._resolve_preset(workspace)
    except Exception as e:  # noqa: BLE001 - 拿不到预设只影响 CC 用户级设置的模型档位
        logger.warning(f"撤销工作区 {workspace.id} 的平台方凭据时读取 CC 模型预设失败（{reason}）: {e}")

    from nekro_agent.services.workspace.manager import WorkspaceService

    try:
        await WorkspaceService.update_workspace_settings(workspace, preset)
    except Exception as e:  # noqa: BLE001 - 单个文件重写失败继续
        logger.warning(f"撤销工作区 {workspace.id} 的平台方凭据时重写 settings.json 失败（{reason}）: {e}")
    try:
        await WorkspaceService.sync_mcp_to_disk(workspace)
    except Exception as e:  # noqa: BLE001 - 单个文件重写失败继续
        logger.warning(f"撤销工作区 {workspace.id} 的平台方凭据时重写 .mcp.json 失败（{reason}）: {e}")
    try:
        from nekro_agent.services.resources import workspace_resource_service

        await workspace_resource_service.refresh_workspace_cache(workspace.id)
    except Exception as e:  # noqa: BLE001 - 单个文件重写失败继续
        logger.warning(f"撤销工作区 {workspace.id} 的平台方凭据时刷新资源缓存失败（{reason}）: {e}")

    # 2. 停容器：只改文件不算数，Key 已经在进程环境里了
    if (_metadata_flag(workspace, METADATA_SECRET_DELIVERED_KEY) or force_stop) and workspace.container_name:
        if workspace.status != "stopped":
            try:
                from nekro_agent.services.workspace.container import SandboxContainerManager

                await SandboxContainerManager.stop(workspace)
                stopped = True
            except Exception as e:  # noqa: BLE001 - 停容器失败也要把标记清掉，下次启动再复核
                logger.warning(f"撤销工作区 {workspace.id} 的平台方凭据时停容器失败（{reason}）: {e}")

    # 3. 清标记：按键删（受保护的键，见 `GUARDED_METADATA_KEYS`），不碰 `stop()` 刚改过的别的键
    try:
        from nekro_agent.models.db_workspace import patch_workspace_metadata

        if await DBWorkspace.exists(id=workspace.id):
            await patch_workspace_metadata(workspace, drop_keys=[METADATA_SECRET_DELIVERED_KEY])
    except Exception as e:  # noqa: BLE001 - 标记清不掉不影响这次的撤销动作本身
        logger.warning(f"撤销工作区 {workspace.id} 的平台方凭据时清除标记失败（{reason}）: {e}")

    if stopped:
        logger.warning(f"已撤销工作区 {workspace.id} 的平台方凭据（{reason}），已停掉容器，下次启动是干净的新容器")
    else:
        logger.info(f"已按判据重写工作区 {workspace.id} 的配置文件（{reason}），未停容器")


def _enabled_mcp_names(workspace: DBWorkspace) -> List[str]:
    """这个工作区启用的 MCP 条目名（含老字段的回落），与写盘时同一套口径"""
    metadata = workspace.metadata or {}
    names: List[str] = list(metadata.get("mcp_servers_enabled") or [])
    if not names:
        legacy = (metadata.get("mcp_config") or {}).get("mcpServers") or {}
        names = list(legacy.keys())
    return [str(name) for name in names]


def _workspace_has_credentialed_mcp(workspace: DBWorkspace) -> bool:
    """这个工作区启用的 MCP 条目里有没有「带凭据」的

    判据本体在 `manager.mcp_entry_has_credentials`，这里只是把「它启用了哪几条」搬到库里的
    原始条目上；任何一步读不出来按「有」处理（宁可多停一次容器，不要把带凭据的漏过去）。
    """
    names = _enabled_mcp_names(workspace)
    if not names:
        return False
    try:
        from nekro_agent.services.workspace.manager import (
            _global_library_by_name,
            _resolve_library_entry,
            mcp_entry_has_credentials,
        )

        library = _global_library_by_name()
        for name in names:
            raw = _resolve_library_entry(name, library)
            if raw is not None and mcp_entry_has_credentials(raw):
                return True
        return False
    except Exception as e:  # noqa: BLE001 - 读不出来按「有」处理
        logger.warning(f"读取工作区 {workspace.id} 的 MCP 条目失败，按带凭据处理: {e}")
        return True


async def _workspace_has_enabled_resource_binding(workspace_id: int) -> bool:
    """这个工作区有没有启用的资源绑定；查不出来按「有」处理（同上的保守方向）"""
    from nekro_agent.models.db_workspace_resource_binding import DBWorkspaceResourceBinding

    try:
        return await DBWorkspaceResourceBinding.filter(workspace_id=workspace_id, enabled=True).exists()
    except Exception as e:  # noqa: BLE001 - 查不出来按「有」处理
        logger.warning(f"查询工作区 {workspace_id} 的资源绑定失败，按有绑定处理: {e}")
        return True


async def _stock_may_hold_platform_secrets(workspace: DBWorkspace, *, direct: bool) -> bool:
    """按当前配置推断：改造前的写盘会不会已经把平台方凭据交给了这个工作区

    改造前四样凭据不看归属照写、也不记标记，所以「当前配置里有」就当「已经下发过」。
    **只看配置、不看值**，更不解密：

    - 资源：有启用的绑定就算（不去解出字段）；
    - MCP：启用的全局库条目里有 `env` / `headers` 非空的就算（判据同写盘）；
    - `extra_env`：预设里有键名像凭据的项就算（`CREDENTIAL_OVERRIDE_ENV_KEYS` 两项除外）；
    - 模型 Key：预设有 `auth_token`，且 `direct` 模式、或这个工作区还没有中继落点
      （改造前建的容器、热更新沿用直连的那一种，磁盘与进程里都是真 Key）。

    从没起过容器、目录也不存在的工作区什么都没拿到过。读不出来的一律按「有」：
    推断错了的代价只是这个工作区以后被划给租户时多停一次容器。
    """
    from nekro_agent.services.workspace.manager import WorkspaceService

    if not workspace.container_name and not WorkspaceService.get_workspace_dir(workspace.id).exists():
        return False
    if await _workspace_has_enabled_resource_binding(workspace.id):
        return True
    if _workspace_has_credentialed_mcp(workspace):
        return True

    try:
        from nekro_agent.services.workspace.container import SandboxContainerManager

        preset = await SandboxContainerManager._resolve_preset(workspace)
    except Exception as e:  # noqa: BLE001 - 读不出预设按「有」处理
        logger.warning(f"启动复核读取工作区 {workspace.id} 的 CC 模型预设失败，按下发过凭据处理: {e}")
        return True
    if preset is None:
        return False

    from nekro_agent.core.cc_model_presets import is_secret_env_key
    from nekro_agent.services.workspace.cc_credential import CREDENTIAL_OVERRIDE_ENV_KEYS, METADATA_BASE_URL_KEY

    if preset.auth_token and (direct or not (workspace.metadata or {}).get(METADATA_BASE_URL_KEY)):
        return True
    return any(is_secret_env_key(key) for key in preset.extra_env if key not in CREDENTIAL_OVERRIDE_ENV_KEYS)


def schedule_platform_secret_revoke(workspace_ids: Iterable[int], *, reason: str) -> None:
    """把撤销放到后台任务里（归属变化的那几条路径都在事件循环里）

    不能在持有 MCP 锁的代码里同步 await 撤销：那条路径要重写 `.mcp.json`，而
    `asyncio.Lock` 不可重入。没有运行中的事件循环时跳过并记 debug —— 那时也没法撤销。
    """
    ids: List[int] = []
    for raw in workspace_ids:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            ids.append(value)
    if not ids:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("没有运行中的事件循环，跳过平台方凭据的撤销")
        return
    task = loop.create_task(_revoke_workspaces(sorted(set(ids)), reason=reason))
    _revoke_tasks.add(task)
    task.add_done_callback(_revoke_tasks.discard)


async def _revoke_workspaces(workspace_ids: Iterable[int], *, reason: str) -> None:
    """逐个撤销；只处理标记为「拿到过平台方凭据」的工作区

    没有标记的那些本来就没下发过凭据，停它们的容器只是白白打断任务。
    """
    for workspace_id in workspace_ids:
        try:
            workspace = await DBWorkspace.get_or_none(id=workspace_id)
            if workspace is None:
                continue
            if not _metadata_flag(workspace, METADATA_SECRET_DELIVERED_KEY):
                continue
            await revoke_platform_secrets(workspace, reason=reason)
        except Exception as e:  # noqa: BLE001 - 单个工作区失败不拖垮其余
            logger.warning(f"撤销工作区 {workspace_id} 的平台方凭据失败（{reason}）: {e}")


async def wait_pending_revokes() -> None:
    """等所有在跑 / 已排队的撤销任务收尾；测试用它来同步后台任务"""
    while _revoke_tasks:
        await asyncio.gather(*list(_revoke_tasks), return_exceptions=True)


async def enforce_platform_secret_policy_on_startup() -> None:
    """启动复核：存量工作区按判据处理一遍，每个工作区一辈子只做一次

    改造前的写盘不看归属、也不记「下发过」标记，所以升级后的存量分两种处理：

    - **此刻不能拿平台方凭据**（只在有租户的部署里出现）：存量里可能已经有在 `direct` 下跑着的
      租户工作区，所以对它们 `force_stop`——先重写配置文件，再停容器，下次启动就是干净的；
    - **此刻能拿**：按当前配置推断改造前会不会已经下发过（见
      :func:`_stock_may_hold_platform_secrets`，只看配置、不解密），会就补记「下发过」。
      不补的话，它以后第一次被划给租户时撤销会因为没有标记直接跳过：不停容器，
      CC 用户级设置等文件里的旧凭据留到下次写设置。单租户部署也补：
      之后建了租户、把它划过去，走的是同一条撤销路径。

    **只做一次**：两种处理共用 `METADATA_STARTUP_SWEPT_KEY`（「启动复核已经看过这个工作区的
    存量」）。看过之后写配置、注入资源的每一条路径都按判据走、写出凭据就记标记，不再需要推断；
    没有这个标记的话，`relay` 下仍挂着资源绑定或带凭据 MCP 条目的租户工作区会在每次重启时
    被停一次容器，能拿凭据的工作区每次启动都要重新推断一遍。之后再变成不归平台方的工作区由
    `taint_workspaces` 那条路径处理，不靠这里。

    **例外**：早先旧位置的 `.claude/settings.json` 每次启动都删一遍，不看这个标记。

    整个函数不抛异常，每个工作区单独 try——一次复核失败不该拦住启动。
    """
    try:
        from nekro_agent.services.plugin.scope_guard import deployment_has_tenants

        has_tenants = await deployment_has_tenants()
    except Exception as e:  # noqa: BLE001 - 判不出来就什么都不做，下次启动再来
        logger.warning(f"启动复核平台方凭据时判定部署形态失败，跳过: {e}")
        return

    from nekro_agent.services.workspace.cc_credential import credential_mode

    direct = credential_mode() == "direct"
    if has_tenants and direct:
        logger.warning(
            "CC 凭据模式是 direct：租户工作区的 CC 不可用（不会下发平台方模型 Key）。"
            "要让租户工作区可用，改为 relay（删掉 NEKRO_CC_CREDENTIAL_MODE 或设为 relay，"
            "并确认容器能回连 NA 的中继地址，必要时设 NEKRO_CC_RELAY_BASE_URL）"
        )

    try:
        workspaces = await DBWorkspace.all()
    except Exception as e:  # noqa: BLE001 - 读不到工作区就只留下上面那条告警
        logger.error(f"启动复核平台方凭据时读取工作区列表失败: {e}")
        return

    from nekro_agent.services.workspace.manager import WorkspaceService

    for workspace in workspaces:
        # 早先写在 `{ws}/.claude/settings.json` 的 CC 设置：CC 从来不读，里面却可能留着
        # 以前写出去的凭据。每次启动都清一遍（只是一次 stat），不受「复核过一次」标记限制——
        # 那些已经复核过、此后再也不会写设置的存量工作区，正是只靠这里才删得掉
        try:
            if WorkspaceService.remove_legacy_claude_settings(WorkspaceService.get_workspace_dir(workspace.id)):
                logger.info(f"启动复核：已删除工作区 {workspace.id} 旧位置的 .claude/settings.json")
        except Exception as e:  # noqa: BLE001 - 删不掉不拦启动，下次启动再来
            logger.warning(f"启动复核删除工作区 {workspace.id} 旧位置的 .claude/settings.json 失败: {e}")
        try:
            if _metadata_flag(workspace, METADATA_STARTUP_SWEPT_KEY):
                continue
            # 单租户部署不调判据：没有租户就都能拿，也不必为此读标记文件
            if not has_tenants or await platform_credentials_allowed(workspace):
                if not _metadata_flag(workspace, METADATA_SECRET_DELIVERED_KEY) and await _stock_may_hold_platform_secrets(
                    workspace, direct=direct
                ):
                    await mark_platform_secret_delivered(workspace)
                    logger.info(f"启动复核：工作区 {workspace.id} 的存量配置会下发平台方凭据，已补记标记")
            else:
                force_stop = (
                    direct
                    or await _workspace_has_enabled_resource_binding(workspace.id)
                    or _workspace_has_credentialed_mcp(workspace)
                )
                await revoke_platform_secrets(workspace, reason="启动复核", force_stop=force_stop)
            # 补记标记、`revoke_platform_secrets` 与 `stop()` 都会改 metadata：重新取一次行再记
            row = await DBWorkspace.get_or_none(id=workspace.id)
            if row is not None:
                metadata = dict(row.metadata or {})
                metadata[METADATA_STARTUP_SWEPT_KEY] = True
                row.metadata = metadata
                await row.save(update_fields=["metadata", "update_time"])
        except Exception as e:  # noqa: BLE001 - 单个工作区失败不拖垮其余
            logger.warning(f"启动复核工作区 {workspace.id} 的平台方凭据失败: {e}")


__all__ = [
    "METADATA_DIRECT_WITHHELD_KEY",
    "METADATA_SECRET_DELIVERED_KEY",
    "METADATA_STARTUP_SWEPT_KEY",
    "WITHHELD_MESSAGE",
    "PlatformCredentialWithheldError",
    "enforce_platform_secret_policy_on_startup",
    "mark_platform_secret_delivered",
    "platform_credentials_allowed",
    "prepare_cc_dispatch",
    "revoke_platform_secrets",
    "schedule_platform_secret_revoke",
    "wait_pending_revokes",
]
