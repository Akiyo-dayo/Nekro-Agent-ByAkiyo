"""未认领实例的入站闸门：没人认领的协议端，只有凭平台方的部署级令牌连上来才分发

## 为什么要拦

部署级 `ONEBOT_ACCESS_TOKEN` 为空时握手不校验，任何人带一个陌生账号号码连上来，第一条事件就会被
`adapters/onebot_v11/core/instance.py` 自动登记成未认领实例（`enabled=True`），消息、通知、命令照常进 collector、
落库、触发 Agent：匿名者能白用平台的模型额度、往会话里塞消息。上游同样如此（令牌为空时不校验、照常分发），
本模块是对上游的收紧。

## 规则

已认领的实例不受影响，判据只看 `unclaimed` 这一种状态。未认领实例的入站事件一律丢弃，除非这条事件所在连接的
握手（`adapters/onebot_v11/core/access_token.py` 放进 `ContextVar` 的那条记录）凭的是**当前**非空的部署级令牌，
且这枚令牌没有对租户可见过（`services/user/token_exposure.py`）。按指纹比，不看握手记录里的来源标签：
查库失败退回部署级令牌的握手（`deployment_fallback`）同样出示了部署级令牌；待绑实例的备选令牌不是部署级令牌。
也不要求握手记录里的实例就是这个未认领实例：它本来就是握手之后、第一条事件到来时才登记的。

## 放在哪

入站入口的最前面、自动登记之后，与「实例停用」同一层：`collector.collect_message`（通用兜底），以及 OneBot
的消息、自身消息、群文件上传与通知匹配器（挡在建频道行、向协议端问群成员信息之前）。**不放进**
`push_system_message` / `schedule_agent_task` / 出站：那几处也服务定时任务这类内部触发，没有握手上下文；
没过闸的未认领实例建不出频道行，自然不会有内部触发。

握手阶段不拒绝连接：连接照常建立、照常自动登记，管理员在总后台看得到、能认领。认领之后状态变了，
同一条连接的下一条事件就照常分发，不用重连。
"""

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv

logger = get_sub_logger("unclaimed_dispatch")

UNCLAIMED_STATUS = "unclaimed"
MIGRATION_FILE_NAME = "unclaimed_dispatch_migration.json"

# 同一个实例被挡下时，WARNING 最多每这么多秒一条，其余记 debug
_WARN_INTERVAL_SECONDS = 600.0
# 记着「上次告警时间」的实例数上限（按插入顺序淘汰最早的；dict 保持插入顺序）
_WARN_TRACK_LIMIT = 4096
_last_warned: Dict[Tuple[str, str], float] = {}


async def _instance_status(adapter_key: str, instance_key: str) -> Optional[str]:
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance

    row = await DBAdapterInstance.filter(adapter_key=adapter_key, instance_key=instance_key).order_by("id").first()
    return row.status.strip().lower() if row is not None else None


async def unclaimed_pause_reason() -> str:
    """未认领实例此刻凭什么都收不了消息的原因；空串表示凭部署级令牌连上来的可以收

    与连接无关的那一半判据（部署级令牌为空、曝光过、曝光记录不可用），总后台的待认领面板据此提示。
    """
    from nekro_agent.core.os_env import resolve_deployment_onebot_access_token
    from nekro_agent.services.user.super_users import exposed_fingerprints_with_deployment_check
    from nekro_agent.services.user.token_exposure import token_fingerprint

    deployment = resolve_deployment_onebot_access_token()
    if not deployment:
        return "部署级令牌为空"
    exposed = await exposed_fingerprints_with_deployment_check(deployment_in_use=True)
    if exposed is None:
        return "令牌曝光记录不可用"
    if token_fingerprint(deployment) in exposed:
        return "部署级令牌曾对租户可见"
    return ""


async def _unclaimed_block_reason() -> str:
    """当前事件所在连接为什么不能给未认领实例分发；空串表示可以

    便宜的判据在前：部署级令牌为空、没有握手记录、握手令牌不是部署级令牌，都不必去读曝光记录、扫租户实例。
    """
    from nekro_agent.adapters.onebot_v11.core.access_token import current_handshake
    from nekro_agent.core.os_env import resolve_deployment_onebot_access_token
    from nekro_agent.services.user.token_exposure import token_fingerprint

    deployment = resolve_deployment_onebot_access_token()
    if not deployment:
        return "部署级令牌为空"
    handshake = current_handshake()
    if handshake is None:
        return "没有握手记录"
    if handshake.token_fingerprint != token_fingerprint(deployment):
        return "连接凭的不是部署级令牌"
    return await unclaimed_pause_reason()


def _note_blocked(adapter_key: str, instance_key: str, reason: str) -> None:
    key = (adapter_key, instance_key)
    now = time.monotonic()
    if now - _last_warned.get(key, float("-inf")) >= _WARN_INTERVAL_SECONDS:
        _last_warned.pop(key, None)
        _last_warned[key] = now
        # 换号刷行时每个号都是一个新实例：按最早记下的淘汰，字典不随之无限变大
        while len(_last_warned) > _WARN_TRACK_LIMIT:
            _last_warned.pop(next(iter(_last_warned)))
        logger.warning(
            f"未认领实例 {instance_key} 的入站事件暂不处理（{reason}）：在总后台认领该实例，或设置 ONEBOT_ACCESS_TOKEN"
            "（本实例此后 10 分钟内的同类丢弃只记 debug）",
        )
    else:
        logger.debug(f"未认领实例 {instance_key} 的入站事件已丢弃（{reason}）")


async def is_inbound_dispatch_allowed(adapter_key: str, instance_key: str) -> bool:
    """入站闸门：实例停用 / 租户停用照旧拒绝；未认领实例另须凭当前未曝光的部署级令牌握手。任何异常按拒绝处理"""
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance

    if not await DBAdapterInstance.is_instance_active(adapter_key, instance_key):
        return False
    if not instance_key or await _instance_status(adapter_key, instance_key) != UNCLAIMED_STATUS:
        return True
    try:
        reason = await _unclaimed_block_reason()
    except Exception:  # noqa: BLE001 - 判不出来就不分发
        logger.warning(f"判定未认领实例 {instance_key} 能否分发失败，按不分发处理", exc_info=True)
        reason = "判定出错"
    if reason:
        _note_blocked(adapter_key, instance_key, reason)
        return False
    return True


def _migration_file() -> Path:
    return Path(OsEnv.DATA_DIR) / "system" / MIGRATION_FILE_NAME


async def auto_claim_unclaimed_with_history() -> List[str]:
    """升级后首次启动：有聊天记录的存量未认领实例就地认领一次，返回被认领的实例标识

    升级前未认领实例照常分发，管理员可能一直没去认领，而它的会话早就在用了；不认领的话升级后它会当场失声。
    没有聊天记录的不动。标记文件在跑完之后写；写不进去下次启动重跑——已认领的不会再碰，
    只有升级后新冒出、又已经攒下聊天记录的未认领实例会被多认领一次（日志里写明）。
    """
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance
    from nekro_agent.models.db_chat_message import DBChatMessage
    from nekro_agent.services.adapter_instance_service import adapter_instance_service
    from nekro_agent.services.console.instance_manager import STATUS_CONNECTING

    path = _migration_file()
    if path.exists():
        return []

    claimed: List[str] = []
    failed: List[str] = []
    for row in await DBAdapterInstance.filter(status=UNCLAIMED_STATUS).order_by("id"):
        try:
            if not await DBChatMessage.filter(adapter_key=row.adapter_key, instance_key=row.instance_key).exists():
                continue
            row.display_name = row.instance_key
            row.enabled = True
            await adapter_instance_service.set_status(
                row,
                STATUS_CONNECTING,
                message="升级时自动认领：存量未认领实例有聊天记录，消息照常处理；请在总后台确认或归档",
                extra_fields=["display_name", "enabled"],
            )
        except Exception:  # noqa: BLE001 - 一行失败不拖垮其余行；标记不写，下次启动重跑剩下的
            logger.error(f"升级时自动认领未认领实例 {row.instance_key} 失败", exc_info=True)
            failed.append(row.instance_key)
            continue
        # 当场记一条：中途失败、下次重跑时，已经认领的这几个不会再出现在名单里
        logger.warning(f"升级时自动认领了有聊天记录的未认领实例 {row.instance_key}")
        claimed.append(row.instance_key)

    if failed:
        logger.error(f"有未认领实例没能自动认领（{', '.join(failed)}），迁移标记不写，下次启动重跑")
    else:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.tmp")
            tmp.write_text(json.dumps({"done": True, "claimed": claimed}, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            logger.error(
                "未认领实例的升级迁移标记写不进去，下次启动会重跑：已认领的不会再碰，新出现且已有聊天记录的未认领实例会被认领",
                exc_info=True,
            )

    if claimed:
        logger.warning(
            f"升级时自动认领了这些有聊天记录的未认领实例（{', '.join(claimed)}）：它们的消息照常处理。"
            "请在总后台确认它们是你的协议端，不是就归档；并建议设置 ONEBOT_ACCESS_TOKEN",
        )
    return claimed
