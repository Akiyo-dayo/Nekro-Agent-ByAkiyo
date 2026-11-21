"""租户状态的写入

总后台的 `PATCH /console/tenants/{tenant_id}` 与运维通道共用这一处：两处各自改
`tenant.status` 再 `save()` 的话，将来给状态变更补的副作用（审计、通知、缓存失效）
只会落在其中一条路径上。「停用立即生效」的判据在 `DBAdapterInstance.is_instance_active`，
它每次现查数据库，所以这里不需要额外清缓存。

日志只在状态真的变化时写一条：后台保存表单时经常把未改动的状态一并提交，
每次都记一条会把「谁停了哪个租户」埋在噪声里。
"""

from __future__ import annotations

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_tenant import DBTenant

logger = get_sub_logger("console.tenant")


async def set_tenant_status(tenant: DBTenant, status: str) -> DBTenant:
    """把租户状态改成 `status` 并保存

    调用方负责校验取值与租户存在；这里只落库。保存整行而不是只更新 `status`：
    总后台的更新接口会在调用前后改其它可变字段，分两次写会改变它原本的原子性。
    """
    if status != tenant.status:
        logger.warning(f"租户状态变更: name={tenant.name}, {tenant.status} -> {status}")
        tenant.status = status
    await tenant.save()
    return tenant


__all__ = ["set_tenant_status"]
