"""记忆实体模型

统一实体节点，承担规范名、别名、类型归并。
实体类型包括：Person（人物）、Concept（概念）、Project（项目）、Artifact（产物）、Event（事件）
"""

from enum import Enum
from typing import Any

from tortoise import fields
from tortoise.models import Model


class EntityType(str, Enum):
    """实体类型枚举"""

    PERSON = "person"  # 人物（用户、群成员等）
    CONCEPT = "concept"  # 概念（技术术语、抽象概念等）
    PROJECT = "project"  # 项目（代码仓库、产品等）
    ARTIFACT = "artifact"  # 产物（文件、文档、代码片段等）
    EVENT = "event"  # 事件（会议、讨论、里程碑等）


class MemorySource(str, Enum):
    """记忆来源枚举"""

    NA = "na"  # NekroAgent 聊天对话
    CC = "cc"  # ClaudeCode 任务执行


class DBMemEntity(Model):
    """记忆实体模型

    表达统一实体节点，用于实体识别、归一化和关系构建。
    """

    id = fields.IntField(pk=True, generated=True, description="主键 ID")
    workspace_id = fields.IntField(index=True, description="工作区 ID（隔离边界）")
    instance_key = fields.CharField(
        max_length=64,
        default="",
        index=True,
        description="来源实例标识（同一适配器下的账号实例，单实例部署留空）",
    )

    # 人设维：这段记忆是**在哪个人格下**产生的。判据与好感度同源
    # （`identity_scope.resolve_preset_id` → `DBChatChannel.get_effective_preset_id`），
    # 0 = 无人设档（回落到内置默认人设，库里没有那一行）。
    # 按人设隔离**不产生副本**：一段对话在人设甲下发生就写一次、标成甲，
    # 不会因为存在人设乙而复制。
    preset_id = fields.IntField(default=0, index=True, description="人设标识；0 表示无人设档")

    # 实体核心信息
    entity_type = fields.CharEnumField(
        EntityType,
        max_length=16,
        index=True,
        description="实体类型",
    )
    name = fields.CharField(max_length=256, index=True, description="实体名称（原始）")
    canonical_name = fields.CharField(
        max_length=256,
        index=True,
        description="规范化名称（用于去重和归一化）",
    )
    aliases = fields.JSONField(
        default=list,
        description="别名列表（JSON 数组）",
    )

    # 统计信息
    appearance_count = fields.IntField(default=1, description="出现次数")
    source_hint = fields.CharEnumField(
        MemorySource,
        max_length=8,
        default=MemorySource.NA,
        description="主要来源提示（na/cc）",
    )

    # 状态
    is_inactive = fields.BooleanField(default=False, index=True, description="是否已失活")

    # 预留字段：跨平台身份映射（暂不实现）
    platform_identities = fields.JSONField(
        default=list,
        description="跨平台身份映射（预留字段）",
    )

    # 时间戳
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "mem_entity"
        indexes = [
            ("workspace_id", "instance_key"),  # 复合索引：工作区+实例
            ("workspace_id", "instance_key", "preset_id"),  # 复合索引：工作区+实例+人设（检索热路径）
            ("workspace_id", "canonical_name"),  # 复合索引：工作区+规范名
            ("workspace_id", "entity_type"),  # 复合索引：工作区+类型
        ]

    def __str__(self) -> str:
        return f"Entity({self.entity_type.value}:{self.canonical_name})"

    def add_alias(self, alias: str) -> bool:
        """添加别名（如果不存在）"""
        aliases: list[str] = self.aliases if isinstance(self.aliases, list) else []
        normalized_alias = alias.strip().lower()
        if normalized_alias and normalized_alias not in [a.lower() for a in aliases]:
            aliases.append(alias.strip())
            self.aliases = aliases
            return True
        return False

    def matches_name(self, query: str) -> bool:
        """检查是否匹配给定名称（包括别名）"""
        query_lower = query.strip().lower()
        if self.canonical_name.lower() == query_lower:
            return True
        if self.name.lower() == query_lower:
            return True
        aliases: list[str] = self.aliases if isinstance(self.aliases, list) else []
        return any(a.lower() == query_lower for a in aliases)

    @classmethod
    async def find_or_create(
        cls,
        workspace_id: int,
        entity_type: EntityType,
        name: str,
        source: MemorySource = MemorySource.NA,
        instance_key: str = "",
        preset_id: int = 0,
    ) -> tuple["DBMemEntity", bool]:
        """查找或创建实体

        去重维度包含 instance_key：不同实例下的同名实体是彼此独立的节点，
        否则实体会成为跨实例共享的图谱入口，绕开检索侧的实例过滤。

        **`preset_id` 出于同一条理由进入去重维度**，而且这里少了它不是"泄漏"而是"丢数据"：
        实体行只带一个 `preset_id`，若人设甲下建的行被人设乙复用，这行仍旧标着甲，
        于是乙的检索按人设过滤时把它挡在外面——而乙这一轮新建的关系又指着这个 ID，
        关系召回那一支拿不到实体名，整条关系静默消失。分开建行是唯一自洽的形态。
        这**不违反**"按人设隔离不产生副本"：那条说的是一段对话不会因为存在别的人设而被复制，
        而实体是去重节点，同一维度上 `instance_key` 早就是这么做的。

        Returns:
            (entity, created): 实体对象和是否新创建
        """
        canonical = name.strip().lower()

        # 先尝试查找已存在的实体
        existing = await cls.filter(
            workspace_id=workspace_id,
            instance_key=instance_key,
            preset_id=preset_id,
            entity_type=entity_type,
            canonical_name=canonical,
            is_inactive=False,
        ).first()

        if existing:
            existing.appearance_count += 1
            await existing.save(update_fields=["appearance_count", "update_time"])
            return existing, False

        # 创建新实体
        entity = await cls.create(
            workspace_id=workspace_id,
            instance_key=instance_key,
            preset_id=preset_id,
            entity_type=entity_type,
            name=name.strip(),
            canonical_name=canonical,
            source_hint=source,
        )
        return entity, True

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "instance_key": self.instance_key,
            "preset_id": self.preset_id,
            "entity_type": self.entity_type.value,
            "name": self.name,
            "canonical_name": self.canonical_name,
            "aliases": self.aliases,
            "appearance_count": self.appearance_count,
            "source_hint": self.source_hint.value,
            "is_inactive": self.is_inactive,
        }
