"""实例组（记忆域）与实例的域归属

## 域是什么

隔离键 `(记忆域, 人, 人设)` 里的第一维。**载体是实例组**：每个实例属于且仅属于一个域，
**默认每个实例自成一域**。互通就是把几个实例放进同一个组。

刻意**不做「每对实例的开关」**：那种形态有传递性悖论——A 通 B、B 通 C 之后，A 与 C 到底
通不通没有答案，而记忆与好感是有向读取的，两种回答都能各自举出反例。分组没有这个问题。

## 「不建行就等于自成一域」

`DBInstanceDomainBinding` **只在实例被显式放进某个组时才存在一行**。没有行 = 自成一域。
这条选择是有代价对比的：

- 「每个实例建一行」需要一次存量回填，还要在实例创建/删除的每条路径上维护那一行；
  漏了一处就是一个没有域的实例（读侧要么崩、要么被判成"和谁都不通"）。
- 「不建行」零迁移、零默认数据，且**零配置的部署与今天逐字相同**——这正是本轮最重要的
  那条回归保险（见 `tests/test_identity_scope_foundation.py`）。

代价是「域」不是一张能直接 `JOIN` 的表，取域必须走 `services/identity_scope.py`。
那是有意的：解析器是唯一的判据实现，消费方不许自己拼键。

## 记忆域与好感域

绑定行上有**两个**可空的组字段：

- `memory_group_id` —— 记忆域，也是好感域的默认值；
- `affection_group_id` —— **好感域覆盖**，NULL 表示跟随 `memory_group_id`。

两个字段都可空的原因是「将来不必改表」：想要「好感互通但记忆不互通」，
就只填 `affection_group_id`；想要反过来就只填 `memory_group_id`。
**前端不暴露 `affection_group_id`**（2026-08-12 拍板：默认就绑在一起配一个域），
它的存在只是为了让那天到来时不用动 schema。

## 跨租户串域是本模块最严重的风险面

域的配置入口如果不设防，租户管理员就能**把别的租户的实例拉进自己的组**，
于是读到对方的记忆与好感。因此：

1. 组本身有归属（`tenant_id`，NULL = 平台方）；
2. 成员的归属必须与组一致，由 :meth:`DBInstanceGroup.assert_instance_joinable` 把关，
   而这道校验长在**唯一的写入口** :meth:`DBInstanceDomainBinding.bind_to_groups` 上——
   摆成一个"记得调"的独立函数等于没有，任何一处直接 `create(...)` 都能绕开它；
3. 接口层另有一道 `tenant_perm.assert_instance_accessible`（越权 404）。

两扇门缺一不可，理由与 #51（模型组写侧归属校验）完全一样：接口层挡的是"够不着"，
模型层挡的是"从别的入口进来"。设置人设的可见性校验放在模型层也是同一条理由
（见 `DBChatChannel.set_preset`）。

**还有第三扇门，且它不在"加入"这个动作上**：归属校验只在加入的那一刻成立，
而实例的租户归属**事后还会变**（平台方划转），实例本身也会被彻底删除、
之后同名 slug 被重新认领。两种情况下旧的绑定行都会继续生效，于是不经过任何守卫就串了域。
因此这两条路径必须调 :meth:`DBInstanceDomainBinding.release_instances` 把绑定作废，
判据与用例见该方法的 docstring。
"""

from typing import List, Optional, Sequence, Tuple

from tortoise import fields
from tortoise.models import Model

from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("instance_group")

# 「不属于任何实例组」的取值。绑定行不存在、或组字段为 NULL 时，域退化成"自成一域"，
# 此时域的身份由实例自己的 `(adapter_key, instance_key)` 决定。
# 好感表用 `domain_group_id = 0` 表示这种情况，理由见 `db_affection.py` 的模块文档。
SELF_DOMAIN_GROUP_ID = 0


class DBInstanceGroup(Model):
    """实例组（记忆域）

    组名**不要求唯一**：跨租户强制唯一会让两个客户抢同一个"家族组"这种名字，
    而组的身份从来是 `id`，名字只用于后台展示。
    """

    id = fields.IntField(pk=True, generated=True, description="ID")

    name = fields.CharField(max_length=128, index=True, description="组名称（仅用于展示，不要求唯一）")
    note = fields.TextField(default="", description="备注")
    # 组的归属。NULL 表示归平台方（共用版、以及未启用多租户时的全部组）。
    # 成员实例的归属必须与本字段一致，见 `assert_instance_joinable`。
    tenant_id = fields.IntField(null=True, index=True, description="所属租户ID；NULL 表示归平台方")

    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "instance_group"

    @classmethod
    async def assert_instance_joinable(cls, group: "DBInstanceGroup", adapter_key: str, instance_key: str) -> None:
        """校验某个实例可以被放进本组，不可以就抛 `ValueError`

        判据只有一条：**实例的租户归属必须与组的租户归属逐字相等**（都为 NULL 也算相等）。
        这是"跨租户串域"这条风险面的运行时兜底——接口层的 `assert_instance_accessible`
        挡的是"这个租户够不着那个实例"，本方法挡的是"从别的入口（脚本、命令、以后新加的接口）
        进来的同一个动作"。

        **实例查不到时拒绝（fail-closed）**，与 `DBAdapterInstance.is_instance_active` 的
        fail-open 取舍**刻意相反**：那里放行的代价是"一个 Bot 继续收发消息"，这里放行的代价是
        "一份归属不明的数据被并进某个租户的域"，前者可恢复、后者是泄露。

        Args:
            group: 目标实例组
            adapter_key: 待加入实例的适配器标识
            instance_key: 待加入实例的实例标识

        Raises:
            ValueError: 实例不存在，或其租户归属与组不一致
        """
        from nekro_agent.models.db_adapter_instance import DBAdapterInstance

        instance = (
            await DBAdapterInstance.filter(adapter_key=adapter_key, instance_key=instance_key).order_by("id").first()
        )
        if instance is None:
            raise ValueError(f"实例不存在，拒绝加入实例组: adapter_key={adapter_key}, instance_key={instance_key}")
        if instance.tenant_id != group.tenant_id:
            raise ValueError(
                f"实例与实例组的归属不一致，拒绝加入: adapter_key={adapter_key}, "
                f"instance_key={instance_key}, instance_tenant={instance.tenant_id}, group_tenant={group.tenant_id}",
            )


class DBInstanceDomainBinding(Model):
    """实例的域归属（只有被显式放进某个组的实例才有一行）

    没有行 = 自成一域 = 今天的行为。两个组字段都为 NULL 的行在语义上等价于没有行，
    读侧因此不需要区分"没有行"与"有行但都是 NULL"（:func:`identity_scope.resolve_memory_domain`
    两者走同一分支）。
    """

    id = fields.IntField(pk=True, generated=True, description="ID")

    adapter_key = fields.CharField(max_length=128, index=True, description="适配器标识")
    # 单实例适配器留空串。空串是一个**合法**的实例标识（见 DBChatChannel.instance_key），
    # 所以它也可以被放进组里；零配置的部署不会有这行，因此行为与今天逐字相同。
    instance_key = fields.CharField(max_length=128, default="", index=True, description="适配器实例标识")

    memory_group_id = fields.IntField(null=True, index=True, description="记忆域所属实例组 ID；NULL 表示自成一域")
    # 好感域覆盖。NULL = 跟随 `memory_group_id`。**前端不暴露**，见模块文档。
    affection_group_id = fields.IntField(
        null=True,
        index=True,
        description="好感域所属实例组 ID；NULL 表示跟随记忆域",
    )

    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "instance_domain_binding"
        # 「每个实例属于且仅属于一个域」的数据库兜底。少了它，两条并发的加组请求能写出两行，
        # 而读侧 `order_by("id").first()` 会确定性地取其中一行——不崩，但另一行永远不生效，
        # 表现为"改了域不生效"，是最难查的一类。
        unique_together = (("adapter_key", "instance_key"),)

    @classmethod
    async def bind_to_groups(
        cls,
        adapter_key: str,
        instance_key: str,
        *,
        memory_group: Optional["DBInstanceGroup"] = None,
        affection_group: Optional["DBInstanceGroup"] = None,
    ) -> Optional["DBInstanceDomainBinding"]:
        """**唯一**的域归属写入口：把实例放进（或移出）指定的组

        存在的理由与 `DBChatChannel.set_preset` 完全一样：归属校验必须长在写入口上，
        而不是一个"记得调"的独立函数。`assert_instance_joinable` 单独摆着时，
        后续任何一处 `DBInstanceDomainBinding.create(...)` / `.update(...)` 都能绕开它，
        而绕开的代价是一个租户的实例被放进另一个租户的域——本模块最严重的那条风险面。
        它同时补上了那个函数单独用时**够不着的一半**：`affection_group_id` 也要校验，
        只校验记忆组等于把好感域这一侧敞开。

        Args:
            adapter_key: 实例的适配器标识
            instance_key: 实例标识；单实例适配器为空串
            memory_group: 记忆域所属组；`None` 表示自成一域
            affection_group: 好感域覆盖组；`None` 表示跟随记忆域

        Returns:
            写入后的绑定行；两个组都为 `None`（= 完全回到自成一域）时删除该行并返回 `None`
            ——"没有行"是零配置的标准形态，留一行全 NULL 只会让后台多出一条看不懂的记录。

        Raises:
            ValueError: 任一目标组与实例的租户归属不一致，或实例不存在
        """
        for group in (memory_group, affection_group):
            if group is not None:
                await DBInstanceGroup.assert_instance_joinable(group, adapter_key, instance_key)

        memory_group_id = memory_group.id if memory_group is not None else None
        affection_group_id = affection_group.id if affection_group is not None else None
        existing = await cls.get_binding(adapter_key, instance_key)

        if memory_group_id is None and affection_group_id is None:
            if existing is not None:
                await existing.delete()
            return None

        if existing is None:
            return await cls.create(
                adapter_key=adapter_key,
                instance_key=instance_key,
                memory_group_id=memory_group_id,
                affection_group_id=affection_group_id,
            )
        existing.memory_group_id = memory_group_id
        existing.affection_group_id = affection_group_id
        await existing.save()
        return existing

    @classmethod
    async def release_instances(cls, instance_keys: Sequence[str], *, reason: str) -> int:
        """把一批实例踢回"自成一域"，返回被删掉的绑定行数

        **实例的租户归属一旦改变、或实例被彻底删除，它此前的域归属就必须作废。**
        `assert_instance_joinable` 只在"加入的那一刻"校验归属，之后没有任何东西复查；
        于是有两条不经过任何守卫的串域路径：

        1. 平台方把实例在租户之间划转（`routers/console/tenants.py` 的实例绑定覆盖）——
           实例换了主人，却还留在原租户的组里，当场读到原租户的记忆与好感；
        2. 实例被 `purge_instance` 彻底删除后绑定行残留，之后**同名**的新实例
           （`instance_key` 是可以被重新认领的 slug）直接继承旧域，连带旧租户的好感行。

        读侧不复查是刻意的（那要在每条消息上多查两张表，见 `services/identity_scope.py`
        的缓存取舍），因此这两处必须在**写侧**把绑定行清掉。清掉的方向是安全的那一侧：
        回到自成一域 = 与今天逐字相同，最坏结果是运营要重配一次域，而不是一次泄露。

        Args:
            instance_keys: 待释放的实例标识（与调用方的既有语义一致，不带适配器维——
                `instance_key` 是全局唯一的 slug，见 `console/container_manager.py` 的校验）
            reason: 记进日志的原因，域配置凭空消失时要能查到是谁清的

        Returns:
            实际删除的绑定行数
        """
        wanted = [key for key in instance_keys if key]
        if not wanted:
            return 0
        deleted = await cls.filter(instance_key__in=wanted).delete()
        if deleted:
            logger.warning(f"实例域归属已释放（回到自成一域）: instances={wanted}, reason={reason}")
        return deleted

    @classmethod
    async def get_binding(cls, adapter_key: str, instance_key: str) -> Optional["DBInstanceDomainBinding"]:
        """取回某个实例的域归属行，没有则返回 None

        与 `DBAdapterInstance.get_by_bound_account` / `DBChatChannel.get_by_identity` 同一取舍：
        用 `filter(...).order_by("id").first()` 而不是 `get_or_none`，脏行下确定性地取最早那行，
        而不是让整条热路径抛 `MultipleObjectsReturned`。
        """
        return await cls.filter(adapter_key=adapter_key, instance_key=instance_key).order_by("id").first()

    @classmethod
    async def members_of_memory_group(cls, group_id: int) -> List[Tuple[str, str]]:
        """列出记忆域包含的全部实例，按 `(adapter_key, instance_key)` 返回，顺序稳定（按 id）"""
        rows = await cls.filter(memory_group_id=group_id).order_by("id").values_list("adapter_key", "instance_key")
        return [(str(adapter_key), str(instance_key)) for adapter_key, instance_key in rows]

    @classmethod
    async def members_of_affection_group(cls, group_id: int) -> List[Tuple[str, str]]:
        """列出好感域包含的全部实例

        好感域的成员 = 「覆盖字段指向本组的」∪「覆盖字段为空且记忆域指向本组的」。
        两半缺一不可：只算前者会把默认跟随的成员漏掉，只算后者会把显式覆盖的成员漏掉。
        """
        rows = await cls.filter(affection_group_id=group_id).order_by("id").values_list("adapter_key", "instance_key")
        followers = (
            await cls.filter(memory_group_id=group_id, affection_group_id__isnull=True)
            .order_by("id")
            .values_list("adapter_key", "instance_key")
        )
        seen: List[Tuple[str, str]] = []
        for adapter_key, instance_key in [*rows, *followers]:
            item = (str(adapter_key), str(instance_key))
            if item not in seen:
                seen.append(item)
        return seen
