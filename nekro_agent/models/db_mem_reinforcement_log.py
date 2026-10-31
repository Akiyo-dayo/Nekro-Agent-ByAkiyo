"""记忆强化日志模型

记录强化行为，为后续质量分析提供审计轨迹。
"""

from datetime import timedelta
from enum import Enum
from typing import Any

from tortoise import fields
from tortoise.functions import Count
from tortoise.models import Model
from tortoise.timezone import now as db_now


class TargetType(str, Enum):
    """强化目标类型"""

    PARAGRAPH = "paragraph"
    RELATION = "relation"
    ENTITY = "entity"


class TriggerSource(str, Enum):
    """触发来源"""

    NA_RETRIEVAL = "na_retrieval"  # NA 检索命中
    CC_RETRIEVAL = "cc_retrieval"  # CC 检索命中
    MANUAL = "manual"  # 手动强化
    CONSOLIDATION = "consolidation"  # 沉淀过程中关联
    PLUGIN = "plugin"  # 插件触发


class DBMemReinforcementLog(Model):
    """记忆强化日志模型

    记录每次强化行为，用于：
    1. 分析哪些记忆经常被命中
    2. 判断哪些记忆只是噪声
    3. 追踪强化是 NA 触发多还是 CC 触发多
    """

    id = fields.IntField(pk=True, generated=True, description="主键 ID")
    workspace_id = fields.IntField(index=True, description="工作区 ID")

    # === 目标信息 ===
    target_type = fields.CharEnumField(
        TargetType,
        max_length=16,
        description="目标类型",
    )
    target_id = fields.IntField(index=True, description="目标 ID")

    # === 触发信息 ===
    trigger_source = fields.CharEnumField(
        TriggerSource,
        max_length=16,
        description="触发来源",
    )
    trigger_ref = fields.CharField(
        max_length=256,
        null=True,
        description="触发引用（查询文本/任务ID等）",
    )

    # === 强化详情 ===
    weight_before = fields.FloatField(null=True, description="强化前权重")
    weight_after = fields.FloatField(null=True, description="强化后权重")
    boost_amount = fields.FloatField(default=0.3, description="强化增量")

    # === 时间戳 ===
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")

    class Meta:  # type: ignore
        table = "mem_reinforcement_log"
        indexes = [
            ("workspace_id", "target_type", "target_id"),
            ("workspace_id", "trigger_source"),
            ("workspace_id", "create_time"),
        ]

    def __str__(self) -> str:
        return f"Reinforcement({self.target_type.value}:{self.target_id} by {self.trigger_source.value})"

    @classmethod
    async def log_reinforcement(
        cls,
        workspace_id: int,
        target_type: TargetType,
        target_id: int,
        trigger_source: TriggerSource,
        trigger_ref: str | None = None,
        weight_before: float | None = None,
        weight_after: float | None = None,
        boost_amount: float = 0.3,
    ) -> "DBMemReinforcementLog":
        """记录一次强化行为"""
        return await cls.create(
            workspace_id=workspace_id,
            target_type=target_type,
            target_id=target_id,
            trigger_source=trigger_source,
            trigger_ref=trigger_ref,
            weight_before=weight_before,
            weight_after=weight_after,
            boost_amount=boost_amount,
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "target_type": self.target_type.value,
            "target_id": self.target_id,
            "trigger_source": self.trigger_source.value,
            "trigger_ref": self.trigger_ref,
            "weight_before": self.weight_before,
            "weight_after": self.weight_after,
            "boost_amount": self.boost_amount,
            "create_time": self.create_time.isoformat() if self.create_time else None,
        }

    @classmethod
    async def get_hot_memories(
        cls,
        workspace_id: int,
        target_type: TargetType,
        limit: int = 20,
        days: int = 7,
    ) -> list[dict[str, Any]]:
        """获取最近被频繁强化的记忆

        窗口起点取 `tortoise.timezone.now()` 而不是 `datetime.now()`：`create_time` 由
        `auto_now_add` 按 ORM 配置的时区写成**带时区**的值，拿一个朴素本地时间去比，
        窗口边界的含义就取决于底层驱动怎么解释这个没有时区的值——asyncpg 按进程本地时区
        补全，SQLite 则退化成字符串比较，同一份代码在不同后端上算出的窗口并不一致。

        Returns:
            按强化次数排序的记忆统计列表
        """
        cutoff = db_now() - timedelta(days=days)

        results = (
            await cls.filter(
                workspace_id=workspace_id,
                target_type=target_type,
                create_time__gte=cutoff,
            )
            .group_by("target_id")
            .annotate(hit_count=Count("id"))
            .order_by("-hit_count")
            .limit(limit)
            .values("target_id", "hit_count")
        )

        return list(results)
