"""值守实例名单 `SSH_GATEWAY_DUTY_INSTANCES` 跟着实例走

名单里写的是 `适配器:实例`。实例标识会被重新认领，同名的新实例可能属于另一个租户、也可能是
另一台公开 Bot，所以名单不能留着已经不在的实例：

- 删实例、划转给租户：摘掉；
- 换实例对（含待认领并入）：目标归平台方时改写成目标实例，归租户时摘掉。

**摘掉最后一项不能留下空名单**：空名单的意思是「所有平台方实例」，摘空等于把值守放宽到
全部平台方实例。这时改写成占位 `!none`：不含冒号，永远匹配不上任何 `适配器:实例`。

写回系统配置失败只记 WARNING，不挡调用方（库已经改了）；判据本来就只认平台方实例。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from nekro_agent.core.config import SSH_GATEWAY_DUTY_INSTANCES_CLOSED, config, normalize_duty_instances, save_config
from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("ssh_gateway.duty_instances")

# 摘掉最后一项时留下的占位：不含冒号，永远匹配不上；平台方往名单里加实例时自动去掉
EMPTY_PLACEHOLDER = SSH_GATEWAY_DUTY_INSTANCES_CLOSED


def duty_list_view() -> Tuple[List[str], bool]:
    """给后台看的名单：`(真实条目, 是否已清空关闭)`；占位不原样给人看"""
    items = normalize_duty_instances(config.SSH_GATEWAY_DUTY_INSTANCES)
    closed = bool(items) and all(item == EMPTY_PLACEHOLDER for item in items)
    return [item for item in items if item != EMPTY_PLACEHOLDER], closed


def rewrite_duty_instances(source_ref: str, target_ref: Optional[str]) -> bool:
    """把名单里的 `source_ref` 改写成 `target_ref`（None 表示摘掉）；名单里没有它就什么都不做

    Returns:
        名单是否变了
    """
    entries: List[str] = [str(item).strip() for item in (config.SSH_GATEWAY_DUTY_INSTANCES or []) if str(item).strip()]
    if source_ref not in entries:
        return False
    rewritten: List[str] = []
    for item in entries:
        replacement = target_ref if item == source_ref else item
        if replacement and replacement not in rewritten:
            rewritten.append(replacement)
    if not rewritten:
        rewritten = [EMPTY_PLACEHOLDER]
    config.SSH_GATEWAY_DUTY_INSTANCES = rewritten
    action = f"改写成 {target_ref}" if target_ref else "摘掉"
    try:
        save_config()
    except Exception as exc:  # noqa: BLE001 - 内存里已经改了；写不回文件只记警告，重启前判据按内存走
        logger.warning(
            f"SSH_GATEWAY_DUTY_INSTANCES 里的 {source_ref} 已在内存里{action}，写回系统配置失败: {type(exc).__name__}；"
            "重启前请在后台核对这一项",
        )
        return True
    logger.info(f"SSH_GATEWAY_DUTY_INSTANCES：{source_ref} {action}")
    return True


__all__ = ["EMPTY_PLACEHOLDER", "duty_list_view", "rewrite_duty_instances"]
