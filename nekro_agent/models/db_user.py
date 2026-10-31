from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from tortoise import fields
from tortoise.models import Model

from nekro_agent.schemas.tenant import TenantRole


class DBUser(Model):
    """数据库用户模型"""

    id = fields.IntField(pk=True, generated=True, description="用户ID")
    username = fields.CharField(max_length=128, description="用户名")
    password = fields.CharField(max_length=128, description="密码")

    adapter_key = fields.CharField(max_length=64, description="适配器ID")
    platform_userid = fields.CharField(max_length=256, description="平台用户ID")

    perm_level = fields.IntField(description="权限等级")
    login_time = fields.DatetimeField(description="上次登录时间")

    # 口令世代：自助改密与管理员重置口令成功时 +1。令牌里带着签发那一刻的世代
    # （`services/user/auth.py:build_pwd_gen_claim` 的 `pwd_gen`），get_current_user
    # 比对不一致即拒绝——这就是「改密吊销已签发令牌」的机制（PROJECT_STATE §3 #54）。
    # 与上游那条已铺进测试库的同名列（迁移 23_20260829235235）语义相同，列由迁移 25 补齐。
    password_generation = fields.IntField(default=0, description="口令世代：改密/重置时 +1，令牌内世代与其不一致即视为已吊销")

    # 多租户归属。NULL 表示该账号属于平台方（含现有 admin 与全部聊天平台用户），
    # 未启用多租户的部署里这两列恒为 (NULL, "platform_owner")，判定一律落到"不受限"分支。
    # 注意 tenant_role 与 perm_level 是两套体系：前者决定看得到哪些资源，
    # 后者决定能不能做宿主级操作。租户成员的 perm_level 上限被钉死在 Role.Admin，
    # 见 `services/user/tenant_perm.py` 的 tenant_perm_level()。
    tenant_id = fields.IntField(null=True, index=True, description="所属租户ID；NULL 表示归平台方")
    tenant_role = fields.CharField(
        max_length=32,
        default=TenantRole.PLATFORM_OWNER.value,
        index=True,
        description="租户体系下的角色",
    )

    ban_until = fields.DatetimeField(null=True, description="封禁截止时间")
    prevent_trigger_until = fields.DatetimeField(null=True, description="禁止触发截止时间")
    ext_data = fields.JSONField(default=dict, description="扩展数据")

    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    @property
    def is_active(self) -> bool:
        """检查用户是否处于活跃状态（未被封禁）"""
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        if self.ban_until is None:
            return True
        # 确保 ban_until 也是带时区的，如果是 UTC 时间则转换为上海时间
        ban_until = self.ban_until
        if ban_until.tzinfo is None:
            ban_until = ban_until.replace(tzinfo=ZoneInfo("UTC"))
        ban_until = ban_until.astimezone(ZoneInfo("Asia/Shanghai"))
        return now > ban_until  # 检查是否在封禁期

    @property
    def is_prevent_trigger(self) -> bool:
        """检查用户是否被禁止触发（临时或永久）"""
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        if self.prevent_trigger_until is None:
            return False
        # 确保 prevent_trigger_until 也是带时区的，如果是 UTC 时间则转换为上海时间
        prevent_until = self.prevent_trigger_until
        if prevent_until.tzinfo is None:
            prevent_until = prevent_until.replace(tzinfo=ZoneInfo("UTC"))
        prevent_until = prevent_until.astimezone(ZoneInfo("Asia/Shanghai"))
        return now < prevent_until  # 检查是否在禁止触发期间（现在时间小于结束时间）

    @property
    def unique_id(self) -> str:
        """获取用户唯一标识"""
        return f"{self.adapter_key}:{self.platform_userid}"

    class Meta:  # type: ignore
        table = "user"

    @classmethod
    async def get_by_union_id(cls, adapter_key: str, platform_userid: str) -> Optional["DBUser"]:
        """根据适配器ID和平台用户ID获取用户

        联合键本应唯一（迁移 24 加了 `WHERE adapter_key <> ''` 的部分唯一索引），
        但约束缺席的存量库里仍可能躺着重复行——这里处在每条入站消息的必经之路上，
        抛 `MultipleObjectsReturned` 等于当事人的消息全部处理不了。
        与 `DBAdapterInstance.get_by_bound_account` 同一条规矩：确定性取最早那行并告警。
        """
        rows = await cls.filter(adapter_key=adapter_key, platform_userid=platform_userid).order_by("id").limit(2)
        if len(rows) > 1:
            from nekro_agent.core.logger import get_sub_logger

            get_sub_logger("user").warning(
                f"联合键 ({adapter_key}, {platform_userid}) 存在 {len(rows)}+ 行重复用户数据，"
                f"取最早一行 id={rows[0].id}；请执行去重（见迁移 24 的处理口径）",
            )
        return rows[0] if rows else None
