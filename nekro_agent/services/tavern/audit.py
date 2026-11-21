"""配置变更审计

高级槽位的文字可能是管理员自己写的破限文字，审计表只记「谁在什么时候改了哪一项、
长度多少、摘要是什么」，不记原文：改动能追溯到人，表里又不必再存一份敏感文字。
布尔项写 `true` / `false` / `inherit`（没人覆盖）；文字项写 `len=N sha=<前 12 位>`。

这里把「比较新旧摘要 → 写一行 → 记一行 WARNING」收成一处，人设扩展页、云端同步与
通用配置写入口都走它：三处各写一份摘要规则，迟早有一处漏掉「摘要一样就不写」。
"""

from __future__ import annotations

import hashlib
from typing import Any

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_config_audit import DBConfigChangeAudit
from nekro_agent.models.db_user import DBUser
from nekro_agent.services.user.tenant_perm import get_tenant_scope

logger = get_sub_logger("tavern.audit")


def summarize_text(value: str) -> str:
    """文字项的摘要：空串写空串，其余只写长度与 sha256 前 12 位

    空串保留空串而不是 `len=0 sha=...`：审计里要能一眼区分「这一层本来就没有文字」
    与「文字被改成了一段新的」。
    """
    if not value:
        return ""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"len={len(value)} sha={digest}"


def summarize_value(value: Any) -> str:
    """任意取值的审计摘要

    `None` 表示这一层没有覆盖（继承上层），与布尔项的 `false` 必须分开：
    「关掉覆盖」和「覆盖成关」在审计里是两件事。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "inherit"
    if isinstance(value, str):
        return summarize_text(value)
    return str(value)[:128]


async def record_change(
    *,
    user: DBUser,
    layer: str,
    field: str,
    old: Any,
    new: Any,
    adapter_key: str = "",
    instance_key: str = "",
    chat_key: str = "",
    preset_id: int = 0,
) -> bool:
    """记一行配置变更审计；摘要相同（等于没改）时不写并返回 `False`

    WARNING 里也只用摘要，不带原文：日志会被运维 Bot 推送、被 `/logs` 接口读出去，
    它和审计表一样不该成为那截破限文字的另一个副本。

    审计写库失败只记 WARNING、返回 `False`：配置已经写成功了，一次库抖动不该让管理员
    看到「保存失败」而实际已经生效——那会诱使他再写一次。
    """
    old_summary = summarize_value(old)
    new_summary = summarize_value(new)
    if old_summary == new_summary:
        return False

    try:
        await DBConfigChangeAudit.create(
            operator=user.username,
            operator_is_platform=get_tenant_scope(user) is None,
            layer=layer,
            adapter_key=adapter_key,
            instance_key=instance_key,
            chat_key=chat_key,
            preset_id=preset_id,
            field=field,
            old_value=old_summary,
            new_value=new_summary,
        )
    except Exception as e:
        logger.warning(
            f"配置变更审计写入失败（配置本身已生效）: operator={user.username}, layer={layer}, "
            f"field={field}, adapter={adapter_key or '空'}, instance={instance_key or '空'}, "
            f"chat={chat_key or '空'}, preset={preset_id or 0}, 错误: {e}",
        )
        return False

    logger.warning(
        f"配置变更: operator={user.username}, layer={layer}, field={field}, "
        f"adapter={adapter_key or '空'}, instance={instance_key or '空'}, "
        f"chat={chat_key or '空'}, preset={preset_id or 0}, "
        f"old={old_summary or '空'} -> new={new_summary or '空'}",
    )
    return True


__all__ = ["record_change", "summarize_text", "summarize_value"]
