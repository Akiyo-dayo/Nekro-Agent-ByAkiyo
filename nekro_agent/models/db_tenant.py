"""租户表

租户是"定制版客户"在数据库里的落点：一个租户名下有若干成员账号（`user.tenant_id`）
与若干实例（`adapter_instance.tenant_id`）。

## 未启用多租户时的行为

本表为空是常态。此时所有 `user.tenant_id` 与 `adapter_instance.tenant_id` 都是 NULL，
一切判定都会走到"归平台方、不受限"的分支，行为与引入多租户之前完全一致。
"""

from typing import Optional

from tortoise import fields
from tortoise.models import Model

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.schemas.tenant import TenantStatus

logger = get_sub_logger("tenant")


class DBTenant(Model):
    """租户（定制版客户）"""

    id = fields.IntField(pk=True, generated=True, description="租户ID")
    name = fields.CharField(max_length=64, unique=True, description="租户标识（唯一，创建后不可改）")
    display_name = fields.CharField(max_length=128, default="", description="显示名称")
    status = fields.CharField(max_length=32, default=TenantStatus.ACTIVE.value, index=True, description="租户状态")
    contact = fields.CharField(max_length=256, default="", description="联系方式")
    note = fields.TextField(default="", description="备注")

    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "tenant"

    @property
    def is_active(self) -> bool:
        """租户是否处于可用状态

        仅 `active` 允许登录与收发消息；`suspended`（停用）与 `archived`（归档）都不允许。
        """
        return self.status == TenantStatus.ACTIVE.value

    @classmethod
    async def is_tenant_active(cls, tenant_id: Optional[int]) -> bool:
        """判断某个租户是否允许其成员登录、其实例收发消息

        `tenant_id` 为 None 表示该资源归平台方，不受租户闸门约束，恒为 True。

        找不到租户行时同样返回 True：租户只归档不物理删除，出现悬挂引用属于数据异常，
        此时按"归平台方"放行而不是静默停掉一个 Bot，避免一次数据不一致直接造成业务中断。
        """
        if tenant_id is None:
            return True
        tenant = await cls.get_or_none(id=tenant_id)
        if tenant is None:
            logger.warning(f"引用了不存在的租户，按归平台方放行: tenant_id={tenant_id}")
            return True
        return tenant.is_active
