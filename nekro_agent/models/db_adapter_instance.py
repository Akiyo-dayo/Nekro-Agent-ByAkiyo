"""适配器实例表

## 账号绑定的唯一性为什么只在代码里，没有数据库约束

一个实例 = 一个 Bot 账号，反向也必须成立：`provider_account_id` 是入站消息定位实例的唯一依据
（`get_by_bound_account`），一个账号绑两个实例时这条查询没有正确答案。唯一性由
`find_account_binding_conflict` 在创建/更新侧把关，启动期的
`unbind_duplicate_account_bindings` 负责收拾存量。

**这里本该再加一条数据库约束兜底，但当前工具链做不到，且已验证过**：

- 不能用 `unique_together`。未绑定账号存的是空串，而「实例已建好、还没扫码登录」是常态
  （线上库实测 6 行实例里 4 行是空串），普通唯一约束会把这些空串判成互相冲突。
  正确的形状是带 `WHERE provider_account_id <> ''` 谓词的**部分**唯一索引。
- 不能用 `Meta.indexes`。tortoise 的 `Index` 只生成普通索引，自带的 `PartialIndex` 只支持
  等值谓词；借 `INDEX_TYPE` 插槽拼 UNIQUE 在 SQLite 上能跑通，但 Postgres 的
  `BasePostgresSchemaGenerator._get_index_sql` 会把它强行塞进 `USING {index_type}`，
  aerich 实测生成的是 `CREATE INDEX ... USING UNIQUE (...) WHERE ...` —— 一条语法错误的 SQL。
- 本项目**禁止手写迁移文件**，因此也不能绕过 aerich 直接写这条索引。

要补上兜底，得先把 `provider_account_id` 改成可空、把存量空串回填为 NULL，再用
`unique_together`（NULL 之间不互相冲突）。那是一次跨 schema/前端类型与存量数据的改动，
不在本轮范围内。

没有这条兜底的残余风险是**一个很窄的竞争窗口**：总后台创建/改绑与协议端首次自行连入
（`resolve_instance_key` 的自动登记）恰好并发时仍可能写出两行同账号。后果不再是崩溃——
读侧会确定性取一行并告警，下次启动的存量修复会把多余绑定解掉。这条「确定性」只有在
**没有别的地方去写入站路由缓存**时才成立，见 `adapters/onebot_v11/core/instance.py` 里
`_account_to_instance` 的注释：出站解析写反向缓存会把它整个绕过。
"""

from typing import Optional

from tortoise import fields
from tortoise.models import Model

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_tenant import DBTenant

logger = get_sub_logger("adapter_instance")

# `AdapterInstanceService.soft_delete_instance` 留下的墓碑状态。
#
# 账号占用判定**排除墓碑**：墓碑行既不在总后台列表里（`list_instances` 过滤掉它），
# 也不该再收到任何入站消息，而软删并不清空 `provider_account_id`。让它继续占着账号会同时
# 造成两种伤：管理员改绑时被一个界面上根本不存在的实例挡住（文案还点名让他去解绑），
# 以及存量修复按"最早那行"把账号判给墓碑、反过来解绑那个正在收发消息的实例。
# 归档（archived）不同——它在列表里可见、可解绑，因此仍然算占用。
TOMBSTONE_STATUS = "deleted"

# 「这个实例已经不该收发消息了」的三种管理态。
#
# 三者都由总后台的动作写入，且都会**同时**把 `enabled` 置 False
# （`set_instance_enabled` / `archive_instance` / `soft_delete_instance`），
# 所以闸门只看 `enabled` 本来就够。状态一并纳入判据是为了兜住两列不一致的脏行：
# 升级前的存量行、以及只改了一列的手工修补，都可能留下 `enabled=True, status=disabled`
# 这种组合，而管理员在界面上看到的是「已停用」——以界面所示为准更符合他的意图。
#
# **刻意不含 `unclaimed`。** 协议端先于总后台登记连入是常态，此时它被自动登记为
# 未认领实例且 `enabled=True`（见 `adapters/onebot_v11/core/instance.py`）。
# 把未认领判成停用，新接入的 Bot 就会静默失灵，而这恰恰是自动登记要避免的那件事。
# 同理不含 `offline` / `error` / `login_required` / `connecting`：那些是运行态，
# 由掉线监测按真实连接刷新，不是管理员的停用意图。
NON_DISPATCHABLE_STATUSES = frozenset({"disabled", "archived", TOMBSTONE_STATUS})


class DBAdapterInstance(Model):
    """适配器实例模型，用于存储通用多实例配置。"""

    id = fields.IntField(pk=True, generated=True, description="ID")
    adapter_key = fields.CharField(max_length=128, index=True, description="适配器唯一标识")
    instance_key = fields.CharField(max_length=128, description="适配器实例唯一标识")
    display_name = fields.CharField(max_length=256, default="", description="实例显示名称")
    status = fields.CharField(max_length=32, default="pending", index=True, description="实例状态")
    enabled = fields.BooleanField(default=True, index=True, description="是否启用")
    is_default = fields.BooleanField(default=False, description="是否默认实例")
    provider = fields.CharField(max_length=128, default="", description="服务提供方")
    provider_account_id = fields.CharField(max_length=256, default="", description="服务提供方账号标识")
    # 实例的租户归属。NULL 表示归平台方（共用版实例、以及未启用多租户时的全部实例）。
    tenant_id = fields.IntField(null=True, index=True, description="所属租户ID；NULL 表示归平台方")
    metadata_json = fields.TextField(default="", description="实例元数据(JSON)")
    last_error = fields.TextField(default="", description="最近错误信息")
    last_active_at = fields.DatetimeField(null=True, description="最近活跃时间")
    next_renew_at = fields.DatetimeField(null=True, index=True, description="下次续期时间")
    renew_before_minutes = fields.IntField(default=60, description="提前续期分钟数")
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "adapter_instance"
        unique_together = (("adapter_key", "instance_key"),)

    @classmethod
    async def get_by_bound_account(
        cls,
        adapter_key: str,
        provider_account_id: str,
    ) -> Optional["DBAdapterInstance"]:
        """按「适配器 + 协议端账号」取回绑定该账号的实例行

        刻意不用 `get_or_none`：它在匹配到多行时抛 `MultipleObjectsReturned`，而账号唯一性是本次
        修复才补上的、且只在代码层（见模块文档），存量库里仍会残留一个账号绑两个实例的行。
        整条入站消息链路的第一步就是这次查询，抛异常等于该账号的消息全部收不到——确定性地取最早
        那行要好得多：它是绑定该账号最久的实例，也就是历史会话与记忆的归属方。同一判据被启动期的
        `unbind_duplicate_account_bindings` 复用，修复保留的正是这里会选中的那一行。

        空账号不是一个可查的绑定：`provider_account_id` 为空串表示「尚未绑定」，
        拿它去查会匹配到所有未绑定实例。软删墓碑同样不是可路由的归属方，见 `TOMBSTONE_STATUS`。
        """
        if not provider_account_id:
            return None
        rows = (
            await cls.filter(adapter_key=adapter_key, provider_account_id=provider_account_id)
            .exclude(status=TOMBSTONE_STATUS)
            .order_by("id")
            .limit(2)
        )
        if len(rows) > 1:
            logger.warning(
                f"账号 {provider_account_id} 同时被多个实例绑定，本次按最早的一行路由: "
                f"adapter_key={adapter_key}, 选中 instance_key={rows[0].instance_key}；"
                "重启后会由存量修复自动解绑多余的绑定",
            )
        return rows[0] if rows else None

    @classmethod
    async def find_account_binding_conflict(
        cls,
        adapter_key: str,
        provider_account_id: str,
        exclude_id: Optional[int] = None,
    ) -> Optional["DBAdapterInstance"]:
        """找出已经占用该账号的其他实例，没有则返回 None

        判据与 `get_by_bound_account` 逐字对应（同一适配器、账号非空、排除软删墓碑）——
        两者必须一致，否则「创建时挡下来的」与「入站时选中的」会是两批行。
        已归档、已停用的实例仍然算占用：读侧同样会选中它们，放行等于把入站路由的歧义放回来，
        而它们在总后台列表里可见、可解绑，管理员照着文案就能腾出账号。
        """
        if not provider_account_id:
            return None
        query = cls.filter(adapter_key=adapter_key, provider_account_id=provider_account_id).exclude(
            status=TOMBSTONE_STATUS,
        )
        if exclude_id is not None:
            query = query.exclude(id=exclude_id)
        return await query.order_by("id").first()

    @classmethod
    async def is_instance_active(cls, adapter_key: str, instance_key: str) -> bool:
        """判断某个实例当前是否允许收发消息

        这是消息入站 / Agent 触发 / 出站三处闸门共用的唯一判据，两个维度是**与**关系：

        1. **实例自身的开关** —— 总后台的停用 / 归档 / 软删（`enabled=False`，
           以及 `NON_DISPATCHABLE_STATUSES` 里的状态）；
        2. **所属租户的开关** —— 租户被停用（`suspended`）或归档（`archived`）时，
           其名下实例应当像频道被关闭一样静默停止收发，而不是继续消耗平台方的额度。

        本方法一度**只有第 2 条**（当时叫 `is_instance_tenant_active`），于是「停用实例」
        只是后台的一个标记：被停用的 Bot 照常收消息、照常跑 Agent、照常回复。
        两个维度合并成一个判据，就不会再出现「加了一个开关却没接上闸门」这种漏。

        以下情况一律返回 True，保证未启用多租户的部署行为与改造前完全一致：
        - `instance_key` 为空（历史数据里存在未带实例标识的频道）
        - 实例不存在（频道引用了一个已被彻底删除的实例属于数据异常，
          此时放行而不是静默停掉一个 Bot，与 `DBTenant.is_tenant_active` 的悬挂引用同一取舍）
        - 实例已启用且未绑定租户（`tenant_id` 为 NULL，即归平台方）

        **必须带 `adapter_key` 一起查。** `instance_key` 的唯一性是
        `unique_together = (adapter_key, instance_key)`，全局唯一从来没有被保证过：
        总后台的创建接口确实额外查了一次全表重名，但适配器自己的登记接口没有
        （`adapters/wechat_ilink_multi/routers.py` 只查 `(adapter_key, instance_key)`），
        因此两个适配器下同名的实例行是能同时存在的。只按 `instance_key` 查会随机命中其中一行，
        两个方向都错得很难看：停用 A 的实例把 B 的同名实例一起闷掉，或者停用 B 的实例
        因为查到了 A 的那行（仍启用）而完全不生效。定序也是同一条理由——
        与 `get_by_bound_account` / `get_channel` 的取舍一致，脏行下取最早那行而不是随机一行。

        **刻意不做缓存。** 停用 / 启用必须热生效、不重启进程，而这里是每条消息都会走的路径；
        任何 TTL 缓存都会把「热生效」变成「TTL 之后生效」。代价是一次主键之外的单行查询，
        实例表是一张几十行的小表，且改造前的租户闸门本来就在这个位置查一次。
        """
        if not instance_key:
            return True
        instance = await cls.filter(adapter_key=adapter_key, instance_key=instance_key).order_by("id").first()
        if instance is None:
            return True
        if not instance.enabled or instance.status.strip().lower() in NON_DISPATCHABLE_STATUSES:
            return False
        return await DBTenant.is_tenant_active(instance.tenant_id)

    @classmethod
    async def is_chat_key_active(cls, chat_key: str) -> bool:
        """按会话标识判断其所属实例当前是否允许收发消息

        出站与 Agent 调度两处闸门手上只有 `chat_key`，而从 `chat_key` 解析实例段的规则是
        适配器私有的（`onebot_v11` 有实例段、单账号适配器没有）。频道行上的
        `(adapter_key, instance_key)` 是跨适配器都成立的那一份答案，因此统一从这里取。

        频道行查不到时放行：那种 `chat_key` 本来就发不出去，下游会给出更准确的报错，
        在这里抢先判成「实例已停用」只会误导排查。

        `DBChatChannel` 反过来会把本模块拖进它的导入链（它导入适配器工具与配置解析器），
        所以在函数内导入。
        """
        from nekro_agent.models.db_chat_channel import DBChatChannel

        channel = await DBChatChannel.filter(chat_key=chat_key).order_by("id").first()
        if channel is None:
            return True
        return await cls.is_instance_active(channel.adapter_key, channel.instance_key)
