"""总后台运维审计路由

## 为什么是平台方专属

这张表记的是宿主上每一次运维调用的去向：哪个实例、哪个频道、谁触发的、动了什么。
它不按租户分栏——一条记录可能同时提到平台方的实例与某个租户的会话。租户管理员
拿到它，等于拿到一份跨租户的操作清单。因此整条路由只挂 `require_platform_owner()`，
与频道额度的平台方那一组同档。

## 只读

审计一旦写入就不提供修改与删除入口：能改审计的后台等于没有审计。保留期由写入侧
按 365 天清理，见 `services/ops_agent/audit.py`。
"""

from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from nekro_agent.models.db_ops_agent_audit import DBOpsAgentAudit
from nekro_agent.models.db_user import DBUser
from nekro_agent.services.ops_agent.audit import list_ops_events
from nekro_agent.services.user.tenant_perm import require_platform_owner

router = APIRouter(tags=["Console"])


class OpsAuditItem(BaseModel):
    """一条运维审计记录（表的全部列，时间用 ISO 字符串）"""

    id: int = Field(description="记录号")
    created_at: str = Field(description="写入时间（ISO）")
    finished_at: Optional[str] = Field(default=None, description="关闭时间（ISO）；未关闭为空")
    source: str = Field(description="来源：agent / na_ops")
    operator: str = Field(description="授权上的运维人员，逗号连接")
    adapter_key: str = Field(description="来源适配器")
    instance_key: str = Field(description="来源实例")
    chat_key: str = Field(description="来源会话")
    round_id: str = Field(description="回合标识；/na_ops 为空串")
    tool: str = Field(description="工具名或 na_ops:<指令>")
    args_json: Dict[str, Any] = Field(description="脱敏限长后的调用参数")
    outcome: str = Field(description="pending / executed / refused / failed")
    refuse_reason: str = Field(description="拒绝原因码或失败类别")
    result_summary: str = Field(description="宿主写的结果摘要")
    error_type: str = Field(description="异常类名，不含原文")


class OpsAuditListResponse(BaseModel):
    """分页的审计记录"""

    total: int = Field(description="符合筛选条件的总条数")
    items: List[OpsAuditItem] = Field(description="当前页记录，按记录号倒序")


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _item(row: DBOpsAgentAudit) -> OpsAuditItem:
    return OpsAuditItem(
        id=int(row.id),
        created_at=_iso(row.created_at) or "",
        finished_at=_iso(row.finished_at),
        source=row.source,
        operator=row.operator,
        adapter_key=row.adapter_key,
        instance_key=row.instance_key,
        chat_key=row.chat_key,
        round_id=row.round_id,
        tool=row.tool,
        args_json=row.args_json or {},
        outcome=row.outcome,
        refuse_reason=row.refuse_reason,
        result_summary=row.result_summary,
        error_type=row.error_type,
    )


@router.get("/ops-audit", summary="运维审计记录")
async def list_ops_audit(
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，1 到 100")] = 20,
    outcome: Annotated[str, Query(description="按结果过滤")] = "",
    tool: Annotated[str, Query(description="按工具名过滤")] = "",
    source: Annotated[str, Query(description="按来源过滤")] = "",
    operator: Annotated[str, Query(description="按操作人包含匹配")] = "",
    _current_user: DBUser = Depends(require_platform_owner()),
) -> OpsAuditListResponse:
    """按记录号倒序分页读取运维审计"""
    total, rows = await list_ops_events(
        limit=page_size,
        offset=(page - 1) * page_size,
        outcome=outcome,
        tool=tool,
        source=source,
        operator=operator,
    )
    return OpsAuditListResponse(total=total, items=[_item(row) for row in rows])


__all__ = ["OpsAuditItem", "OpsAuditListResponse", "list_ops_audit", "router"]
