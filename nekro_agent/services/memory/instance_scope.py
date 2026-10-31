"""记忆的可见范围解析：**域** + **人设**。

一个工作区可以绑定多个频道（`WorkspaceService.bind_channel` 是一对多），
多实例化后仅按 workspace_id 隔离会让实例 A 的私密记忆被召回并注入实例 B 的提示词。
本模块负责把「当前频道」换算为「该频道允许检索的记忆范围」，供检索侧构造过滤条件。

## 隔离键（`DECISIONS.md`「好感度与记忆的隔离维度」）

记忆的隔离键是 `(记忆域, 人设)`——好感那条键上的「人」这一维在记忆上不成立：
记忆是对话的产物，不挂在某个人头上。

- **域**：载体是实例组。`identity_scope.resolve_domain_instance_keys` 把它换算成
  「允许命中的 `instance_key` 集合」——`mem_*` 四张表上只有 `instance_key` 这一列，
  过滤条件只能表达到这一层（丢掉适配器维的代价见那个函数的 docstring）。
  **没有配置实例组时它逐字等于本模块的 `resolve_allowed_instance_keys`**，
  也就是与改造前完全一致。
- **人设**：频道当前生效的人设行 ID（`identity_scope.resolve_preset_id`），
  0 = 无人设档。写入侧把它标在记忆行上，检索侧按它过滤。
  「换了个人设就不会记得以前的恩怨情仇」。

## 为什么两维要合成一个 :class:`MemoryScope` 而不是两个平行参数

`allowed_instance_keys` 这个参数要穿过 7 个函数、8 个查询点。本项目在 #45 上栽过一次：
「四个消费点只测了一个，8 条变异存活」。再并排加一个 `preset_id`，
「某个调用点少传了一个」的面积**直接翻倍**，而少传的后果是静默的——
过滤条件消失、检索照常返回结果、没有任何报错。

所以两维合成一个不可分的值，并且在**对外的五个检索入口**上写成
**必填的关键字参数**（`retrieve_memories` / `retrieve_memories_batch` /
`search_workspace_memories` / `get_workspace_memory_detail` /
`trace_workspace_memory_origin`）。想看整个工作区的调用方必须显式写
`MemoryScope.unrestricted()`——那是一个能被 grep 出来、能在 review 里看见的东西，
而 `None` 默认值不是。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple, Optional

from nekro_agent.core.config import config

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查，运行时的导入在函数内部（避免与 identity_scope 成环）
    from nekro_agent.services.identity_scope import ChannelLike

# 存量数据（改造前写入的记忆）的 instance_key 为空字符串。
# 任何非空实例在检索时都必须额外允许这个空值，否则升级后旧记忆会被过滤掉、直接失忆。
LEGACY_INSTANCE_KEY = ""

# 「不属于任何人设」的记忆。与 `identity_scope.PRESET_NONE` 同值——那边不能反过来 import
# 本模块之外的东西（`identity_scope` 已经 import 本模块，成环），因此这里是第二个字面量，
# 由 `tests/test_memory_preset_scope.py::test_no_preset_constant_matches_the_resolver` 钉住。
#
# **这个取值在检索时对所有人设可见**，判据与 `LEGACY_INSTANCE_KEY` 逐字同源，理由见
# :meth:`MemoryScope.as_filter`。
NO_PRESET_ID = 0


def resolve_allowed_instance_keys(instance_key: str) -> list[str] | None:
    """返回该实例可检索的实例范围（**只看全局配置项，不看实例组**）。

    Args:
        instance_key: 当前实例标识（实例创建时分配的稳定 slug），单实例适配器为空串。

    Returns:
        允许检索的 instance_key 列表；返回 None 表示不施加任何实例过滤。

    规则：
    - 空 instance_key（单实例部署）返回 None，行为与改造前完全一致，零影响。
    - 命中 `MEMORY_INSTANCE_SHARING_GROUPS` 中的共享组时，返回该组全部成员。
    - 未命中任何共享组时，只返回自身。
    - 以上两种非空情况都会补上 `LEGACY_INSTANCE_KEY`（空串），
      使升级前写入的存量记忆仍可被召回。

    ⚠️ **产品链路不要再直接调它**：域的权威解析器是
    `identity_scope.resolve_domain_instance_keys`（本函数是它的一部分——
    它先取本函数的结果，再并上实例组带来的成员）。本函数保留是因为已经配了
    `MEMORY_INSTANCE_SHARING_GROUPS` 的部署不能在升级那一刻静默失去共享。
    """
    normalized = instance_key.strip()
    if not normalized:
        return None

    allowed: list[str] = []
    for members in config.MEMORY_INSTANCE_SHARING_GROUPS.values():
        if normalized not in {member.strip() for member in members}:
            continue
        for member in members:
            normalized_member = member.strip()
            if normalized_member and normalized_member not in allowed:
                allowed.append(normalized_member)

    if normalized not in allowed:
        allowed.append(normalized)
    allowed.append(LEGACY_INSTANCE_KEY)
    return allowed


def build_instance_filter(allowed_instance_keys: list[str] | None) -> dict[str, list[str]]:
    """把允许集合换算成 Tortoise 的过滤条件，`None` 时返回空 dict（不附加任何条件）。

    检索器、查询服务这些地方都要拼同一个条件。写成两份的风险不是"多几行"，而是**只改一份**
    ——本项目已经在这上面栽过：自动注入那侧按实例过滤了，`memory_tools` 插件按 ID 直取的
    出口没过滤，实例 B 的 Agent 因此能把实例 A 的私密记忆念出来。只留一处实现。
    """
    if allowed_instance_keys is None:
        return {}
    return {"instance_key__in": allowed_instance_keys}


class MemoryScope(NamedTuple):
    """一次记忆读写的可见范围：域（换算成实例集合）+ 人设。

    Attributes:
        allowed_instance_keys: 允许命中的 `instance_key` 列表；`None` = 不按实例过滤。
        preset_id: 当前人设标识；`None` = 不按人设过滤。**它不是"允许集合"**——
            :meth:`as_filter` 会额外放行 :data:`NO_PRESET_ID`（不属于任何人设的那一档），
            理由见那个方法的 docstring。

    两个维度各自的 `None` 都表示「这一维不设限」，它们**不是**一起开关的：
    单实例部署（`instance_key` 为空）天然没有实例维，但仍然可能有人设维。
    """

    allowed_instance_keys: Optional[list[str]] = None
    preset_id: Optional[int] = None

    @classmethod
    def unrestricted(cls) -> "MemoryScope":
        """整个工作区可见（两维都不过滤）。

        只给"本来就该看到工作区全量记忆"的管理端调用方用。写成一个具名构造而不是让
        参数默认成 `None`：**默认值不会出现在调用点上**，于是"忘了传"和"故意要全量"
        长得一模一样。这个名字在 grep 里、在 review 里都看得见。
        """
        return cls(allowed_instance_keys=None, preset_id=None)

    @property
    def filters_by_preset(self) -> bool:
        """本范围是否施加人设过滤"""
        return self.preset_id is not None

    def as_filter(self) -> dict[str, Any]:
        """Tortoise 查询过滤条件（`mem_paragraph` / `mem_entity` / `mem_relation` / `mem_episode` 共用）

        四张表都带 `instance_key` 与 `preset_id` 两列，因此同一份条件对四张表都成立。
        实例维仍旧走 `build_instance_filter`——不要在这里内联一份"等价"的写法。

        ## 人设维为什么是 `in (当前人设, 0)` 而不是等值

        `preset_id = 0` 是「**不属于任何人设**」这一档，不是「某个叫 0 的人设」。
        它与 `instance_key = ""` 是同一个东西的两半，检索时必须**对所有人设可见**，
        否则会同时炸掉三件事：

        1. **升级即失忆。** 存量行的 `preset_id` 是列默认值 0。而「零配置的频道解析出来
           也是 0」这个前提**不成立**：`preset_service.init_default_preset()` 在每次启动时
           保证 `AI_CHAT_DEFAULT_PRESET_ID` 指向一行真实人设，于是
           `get_effective_preset_id` 的回退链（频道 → 实例默认 → **系统默认** → 内置）
           对一个什么都没绑的频道也会返回正整数。测试部署上 65/65 个频道 `preset_id IS NULL`、
           而 `AI_CHAT_DEFAULT_PRESET_ID: 2` —— 等值条件会让这些频道一条存量记忆都读不到。
        2. **控制台直发 CC 的记忆变成写了没人能读。** `semantic_writer.persist_cc_task_memory`
           的默认档就是 `(instance_key="", preset_id=0)`（控制台上没有频道，既没有实例
           也没有人设），`rebuild._resolve_log_scope` 对 `__user__` 日志同理。
           等值条件下这些行**在全新部署上**也永远召回不到——与存量数据无关，
           「上线时全新启动」挡不住它。
        3. **两半判据分叉。** 实例维已经把"不属于任何实例"办成了"对所有实例可见"
           （`LEGACY_INSTANCE_KEY`，`persist_cc_task_memory` 的 docstring 逐字写着
           "该记忆对所有实例可见"）。人设维办成"对所有人设不可见"，同一个 CC 结论
           就会卡在两条规则中间。

        **这不会放宽任何隔离**：0 这一档在改造前对每个频道就是可见的，条件里带上它
        等于"零配置逐字相同"。真人设之间仍然互不可见（`preset_id__in` 只多这一个值），
        而 0 这一档本身在实践中只可能由上面那两条"没有人设"的路径产生——
        真人格的记忆一律落在正整数上。
        """
        query: dict[str, Any] = dict(build_instance_filter(self.allowed_instance_keys))
        if self.preset_id is None:
            return query
        if self.preset_id == NO_PRESET_ID:
            query["preset_id"] = NO_PRESET_ID
        else:
            query["preset_id__in"] = [self.preset_id, NO_PRESET_ID]
        return query


async def resolve_memory_scope(channel: "ChannelLike", *, preset_id: Optional[int] = None) -> MemoryScope:
    """把一个频道换算成它的记忆可见范围。

    Args:
        channel: 当前频道。`adapter_key` / `instance_key` 是域的权威来源，
            **不要解析 `chat_key` 字符串**（实例段的规则是适配器私有的）。
        preset_id: 已经取好的人设标识；`None` 时本函数自己去解析。
            `get_effective_preset_id` 最多三次主键点查，热路径上取过就传进来。

    Returns:
        该频道的 :class:`MemoryScope`。实例维零配置时逐字等于
        `resolve_allowed_instance_keys(instance_key)`。

        ⚠️ **人设维不要指望它是 0。** `get_effective_preset_id` 走的是完整回退链
        （频道 → 实例默认 → 系统默认 → 内置），而 `preset_service.init_default_preset()`
        每次启动都保证系统默认那一层指向一行真实人设。因此**一个什么都没绑的频道也会
        解析出正整数**，`PRESET_NONE` 在跑过一次启动的部署里几乎不可达。
        "零配置 = 改造前逐字相同"是靠 :meth:`MemoryScope.as_filter` 把 `preset_id = 0`
        这一档始终放行来兑现的，不是靠这里返回 0。

    写入侧与读取侧**必须用同一个函数**取这两维：写进去的标签和查询用的条件一旦分叉，
    写进去的行自己查不到（`IdentityScope.affection_columns` 的 docstring 是同一条理由）。
    """
    from nekro_agent.services.identity_scope import resolve_domain_instance_keys, resolve_preset_id

    allowed = await resolve_domain_instance_keys(channel.adapter_key, channel.instance_key)
    resolved_preset_id = preset_id if preset_id is not None else await resolve_preset_id(channel)
    return MemoryScope(allowed_instance_keys=allowed, preset_id=resolved_preset_id)


async def resolve_memory_scope_by_chat_key(chat_key: str) -> Optional[MemoryScope]:
    """按会话标识解析记忆可见范围；频道行不存在时返回 `None`。

    只给手上确实只有 `chat_key` 的调用方用，比传频道对象多一次查询。
    **定位不到频道时返回 `None` 而不是退化成 `unrestricted()`**：这里是授权边界，
    "算不出来就当作全局"正是要堵的那个泄露形态。
    """
    from nekro_agent.models.db_chat_channel import DBChatChannel

    channel = await DBChatChannel.filter(chat_key=chat_key).order_by("id").first()
    if channel is None:
        return None
    return await resolve_memory_scope(channel)
