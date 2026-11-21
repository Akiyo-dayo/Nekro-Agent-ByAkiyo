"""记忆的可见范围解析：**域** + **人设**。

一个工作区可以绑定多个频道（`WorkspaceService.bind_channel` 是一对多），
多实例化后仅按 workspace_id 隔离会让实例 A 的私密记忆被召回并注入实例 B 的提示词。
本模块负责把「当前频道」换算为「该频道允许检索的记忆范围」，供检索侧构造过滤条件。

## 隔离键

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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Literal, NamedTuple, Optional

from qdrant_client import models as qdrant_models
from tortoise.expressions import Q
from tortoise.queryset import QuerySet

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.services.memory.feature_flags import is_domain_scope_enabled
from nekro_agent.services.memory.scope_condition import (
    FALSE,
    TRUE,
    Cond,
    MemTable,
    ScopeField,
    and_,
    build_preset_condition,
    eq,
    gt,
    in_,
    is_null,
    member_pairs,
    or_,
    persons_all_in,
    persons_any,
    preset_visible,
    to_qdrant_filter,
    to_tortoise_q,
)

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查，运行时的导入在函数内部（避免与 identity_scope 成环）
    from nekro_agent.models.db_user import DBUser
    from nekro_agent.services.identity_scope import ChannelLike

logger = get_sub_logger("memory.instance_scope")

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

# 控制台直发 CC 的结论专用的人设档。**负数**，因为人设 ID 是数据库自增主键，
# 频道侧能解析出来的只会是 0 或正整数，负数没有任何频道命中得了。
#
# 频道侧的每一条读路径都按人设维过滤：闸关是 :meth:`MemoryScope.as_filter`，闸开是
# `PresetVisible` / 向量侧的 `build_preset_condition`，两者都只放行「当前人设」与
# :data:`NO_PRESET_ID`（0 = 不属于任何人设）。所以这一档对**所有群和私聊都不可见**，
# 闸开闸关一样；而 :meth:`MemoryScope.unrestricted()` 不按人设过滤，
# CC 握手与后台记忆页照旧看得到这些结论。
#
# 这是「不改任何召回条件」就能做到的隔离：频道侧的条件一个字都不用动。
CONSOLE_ONLY_PRESET_ID = -1

# 控制台直发 CC 时使用的伪频道标识（`semantic_writer.persist_cc_task_memory` 的
# `source_chat_key` 默认值）。它是「以控制台身份写回」的唯一凭据：写入侧见到它就强制
# 落进 :data:`CONSOLE_ONLY_PRESET_ID`，与 `(instance_key="")` 一起构成控制台专属的那样一档。
CONSOLE_USER_CHAT_KEY = "__user__"


@dataclass(frozen=True)
class ChannelVisibility:
    """当前频道算好的可见性。

    它是**算好的值**，不带任何查询能力：域成员对、本地集、频道类型、三个跨频道开关、
    成员集与对方账号集都在 :func:`resolve_memory_scope` 里一次算完，检索侧只做纯函数式
    的条件拼接。这样授权判据是每次现算的（不缓存），而拼条件的地方不需要再查库。

    Attributes:
        chat_key: 当前频道标识。
        chat_kind: ``group`` / ``private`` / ``other``。
        local_keys: 本地集：当前频道自己；群且绑了工作区时，再加上同工作区、同域的
            其他群的 `chat_key`。私聊永远只有自己。
        group_to_group: 群聊记忆跨群召回档位（``off`` / ``related`` / ``all``）。
        group_to_private: 群聊记忆进入私聊档位。
        private_to_group: 私聊记忆进入群聊档位。
        member_accounts: 成员集 `Members*(C)`，按别名展开后；只在群且相关档用到时才算。
        counterpart_accounts: 对方账号集 `Accounts*(X)`，按别名展开后；只在私聊时算，
            跟人走这条硬规则不看开关。
    """

    chat_key: str
    chat_kind: str
    local_keys: frozenset[str]
    group_to_group: str = "off"
    group_to_private: str = "off"
    private_to_group: str = "off"
    member_accounts: frozenset[str] = frozenset()
    counterpart_accounts: frozenset[str] = frozenset()


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
    # 新增的两维。两个字段的默认值都是 `None`，因此旧构造写法全部照常可用；
    # `domain_members is None` 就是「旧形状」的判据。
    domain_members: Optional[tuple[tuple[str, str], ...]] = None
    visibility: Optional[ChannelVisibility] = None

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
        2. **存量行与没有人设的频道。** `preset_id = 0` 这一档装的是两类行：改造前写入的
           存量（列默认值就是 0），以及**改造前**写入的控制台直发 CC 结论
           （`semantic_writer.persist_cc_task_memory` 当时的默认档是
           `(instance_key="", preset_id=0)`）。后者在新代码里改落
           :data:`CONSOLE_ONLY_PRESET_ID`，频道读不到是**有意的**——那是控制台里
           一个人对工作区下的指令与结论，不该被任何群或私聊当成自己的记忆召回；
           而 0 这一档仍然必须放行，否则那些改造前写进去的行会在升级那一刻集体失忆。
           同一件事的写入侧收口见 `semantic_writer.persist_cc_task_memory` 与
           `rebuild._resolve_log_scope` 对 `__user__` 日志的处理。
        3. **两半判据分叉。** 实例维已经把"不属于任何实例"办成了"对所有实例可见"
           （`LEGACY_INSTANCE_KEY`）。人设维不再跟着办同一件事：控制台直发有了专属的
           负数档，0 这一档只剩「存量」与「解析不出人设的频道写下的记忆」两种来源。

        **这不会放宽任何隔离**：0 这一档在改造前对每个频道就是可见的，条件里带上它
        等于"零配置逐字相同"。真人设之间仍然互不可见（`preset_id__in` 只多这一个值）。
        """
        query: dict[str, Any] = dict(build_instance_filter(self.allowed_instance_keys))
        if self.preset_id is None:
            return query
        if self.preset_id == NO_PRESET_ID:
            query["preset_id"] = NO_PRESET_ID
        else:
            query["preset_id__in"] = [self.preset_id, NO_PRESET_ID]
        return query

    @property
    def is_legacy_shape(self) -> bool:
        """是否是改造前的两维形状。`domain_members is None` 就是判据。"""
        return self.domain_members is None

    def condition(self, table: MemTable, *, workspace_id: int) -> Cond:
        """按设计稿的「工作区腿 + 域腿」拼出条件树。

        只在**新形状**上可用。旧形状的渲染必须逐字走改造前的代码（见 :meth:`apply` /
        :meth:`qdrant_must`），不能让「新代码碰巧算出同样的东西」冒充回滚保证。

        段落表的域腿带三个跨频道开关与「跟人走」；实体 / 关系 / episode 的域腿仍只有
        公共一行（它们不挂「涉及的人」，偏严但不串）。工作区腿在四张表上都保持改造前的
        语义：实体 / 关系全可见，段落 / episode 按本地集、``__user__`` 与 NULL 收紧。

        Args:
            table: 条件要落到哪张表；字段集合由 ``scope_condition.TABLE_FIELDS`` 约束。
            workspace_id: 当前频道绑定的工作区；没绑时传 0，此时只有域腿。

        Raises:
            ValueError: 旧形状、人设缺失或域成员为空。
        """
        if self.is_legacy_shape:
            raise ValueError("旧形状的 MemoryScope 没有条件树；请直接用 apply / qdrant_must")
        if self.preset_id is None:
            raise ValueError("新形状的 MemoryScope 必须有人设；为 None 说明解析入口漏传了")
        if not self.domain_members:
            # 域成员至少含自己；为空说明解析器算错了。授权边界上「算不出来」不能退化成
            # 「不过滤」，这里直接抛，由上层 fail-closed。
            raise ValueError("新形状的 MemoryScope 域成员为空")

        # `visibility is None` 按设计稿只给管理端：不按频道来源过滤，两条腿都保持 TRUE。
        # 解析入口不会产出这种形状，因此它不会成为「解析失败 → 静默放宽」的路径。
        domain_items: list[Cond] = [
            eq(ScopeField.WORKSPACE_ID, 0),
            member_pairs(self.domain_members),
            preset_visible(self.preset_id),
        ]
        if self.visibility is not None:
            if table is MemTable.PARAGRAPH:
                domain_items.append(_paragraph_domain_visibility(self.visibility))
            else:
                domain_items.append(in_(ScopeField.ORIGIN_CHAT_KEY, tuple(sorted(self.visibility.local_keys))))
        domain_leg = and_(*domain_items)
        if workspace_id < 1:
            return domain_leg

        if table in (MemTable.ENTITY, MemTable.RELATION) or self.visibility is None:
            # 存量实体 / 关系没有「来源频道」这一维的实际取值，保持改造前的
            # 「工作区内全可见」；域行则由上面的域腿按本地集收紧。
            workspace_visibility: Cond = TRUE
        else:
            workspace_visibility = or_(
                in_(ScopeField.ORIGIN_CHAT_KEY, tuple(sorted(self.visibility.local_keys))),
                eq(ScopeField.ORIGIN_CHAT_KEY, CONSOLE_USER_CHAT_KEY),
                is_null(ScopeField.ORIGIN_CHAT_KEY),
            )

        workspace_items: list[Cond] = [eq(ScopeField.WORKSPACE_ID, workspace_id)]
        if self.allowed_instance_keys is not None:
            workspace_items.append(in_(ScopeField.INSTANCE_KEY, tuple(self.allowed_instance_keys)))
        workspace_items.extend([preset_visible(self.preset_id), workspace_visibility])
        return or_(and_(*workspace_items), domain_leg)

    def pg_q(self, table: MemTable, *, workspace_id: int) -> Q:
        """数据库侧的 ``Q``。检索出口统一走它，避免各处自己拼范围条件。

        旧形状返回与改造前逐字相同的 ``Q(workspace_id=..., **as_filter())``；新形状走
        条件树。这样调用方不必判断形状，回滚路径也不会被新代码「顺手」改写。
        """
        if self.is_legacy_shape:
            return Q(workspace_id=workspace_id, **self.as_filter())
        return to_tortoise_q(self.condition(table, workspace_id=workspace_id), table)

    def apply(self, queryset: QuerySet[Any], table: MemTable, *, workspace_id: int) -> QuerySet[Any]:
        """把可见范围应用到一条已经带好非范围条件的查询上。

        旧形状逐字复刻今天各出口的写法（``filter(workspace_id=..., **as_filter())``），
        编译出的 SQL 与改造前相同；新形状改走条件树。

        条件化简成 ``FALSE`` 时返回一个必然为空的查询集。当前 Tortoise 版本的
        ``QuerySet`` 没有 ``none()``，因此用 ``filter(id__in=[])``；四张记忆表都有主键
        ``id``，这个条件在 SQLite 与 PostgreSQL 上都稳定返回空集。
        """
        if self.is_legacy_shape:
            return queryset.filter(workspace_id=workspace_id, **self.as_filter())
        cond = self.condition(table, workspace_id=workspace_id)
        if cond is FALSE:
            return queryset.filter(id__in=[])
        return queryset.filter(to_tortoise_q(cond, table))

    def qdrant_must(self, *, workspace_id: int) -> Optional[list[Any]]:
        """向量侧的范围条件列表。

        旧形状把今天 ``search()`` 的前三类条件原样搬过来（工作区、实例集合、人设），
        顺序与对象都不变；新形状把整棵树渲染成一个嵌套 ``Filter``。

        Returns:
            条件列表；新形状化简成 ``FALSE`` 时返回 ``None``，调用方据此直接返回空结果、
            不向 Qdrant 发请求。
        """
        if self.is_legacy_shape:
            must: list[Any] = [
                qdrant_models.FieldCondition(
                    key=ScopeField.WORKSPACE_ID.value,
                    match=qdrant_models.MatchValue(value=workspace_id),
                ),
            ]
            if self.allowed_instance_keys is not None:
                must.append(
                    qdrant_models.FieldCondition(
                        key=ScopeField.INSTANCE_KEY.value,
                        match=qdrant_models.MatchAny(any=list(self.allowed_instance_keys)),
                    ),
                )
            if self.preset_id is not None:
                must.append(build_preset_condition(self.preset_id))
            return must

        cond = self.condition(MemTable.PARAGRAPH, workspace_id=workspace_id)
        if cond is FALSE:
            return None
        return [to_qdrant_filter(cond)]

    def is_local_origin(self, chat_key: Optional[str]) -> bool:
        """某个段落来源频道是否属于当前频道的本地集。

        旧形状与「不按频道过滤」的新形状恒为 ``True``：前者不知道来源频道，后者是管理端
        的全量视角，都不该脱敏。其余情况只认算好的本地集，调用方不要再自己拼一份判据。
        """
        if self.is_legacy_shape:
            return True
        if self.visibility is None:
            return True
        if chat_key is None:
            return False
        return chat_key in self.visibility.local_keys


_ADMIN_PARTITIONS = frozenset({"all", "domain", "workspace"})

# 摘要类表与提示词明细按「频道 + 人设」归属，没有工作区分区，也没有「涉及的人」这一维。
_CHANNEL_SCOPED_TABLES = frozenset({MemTable.SUMMARY, MemTable.SUMMARY_STATE, MemTable.PROMPT_ROUND})


@dataclass(frozen=True)
class AdminFilter:
    """管理端的**筛选**维度（不是授权范围，授权范围在 :class:`AdminMemoryScope` 里）。

    状态、时间、关键词与排序刻意不在这里：它们不是「这一行属于谁」的判据，由各服务函数
    自己往查询上追加。把可见范围和普通筛选混成一个类型，下一次新增筛选维度时很容易把
    它一并塞进授权条件，而放宽的方向永远看不出来。

    Attributes:
        partition: `all` / `domain`（`workspace_id = 0`）/ `workspace`（`workspace_id > 0`）。
        chat_key: 频道。段落 / episode / 实体 / 关系按 `origin_chat_key`，摘要类表按 `chat_key`。
        preset_id: 人设精确等于；`None` 表示不筛。
        person_accounts: 已按别名展开的账号集合。`None` = 不按人筛，空集 = 恒空。
    """

    partition: Literal["all", "domain", "workspace"] = "all"
    chat_key: Optional[str] = None
    preset_id: Optional[int] = None
    person_accounts: Optional[frozenset[str]] = None


@dataclass(frozen=True)
class AdminMemoryScope:
    """管理端的可见范围：`None` = 平台方全量，否则只看这些实例对。

    与 :class:`MemoryScope` 分开而不是复用它：召回的 `MemoryScope` 是**频道**视角
    （本地集、跨频道开关、人设可见性都参与），管理端是**实例**视角，两者混在一个类型里
    会让「管理端顺手把召回条件放宽」变成一次参数改动的副作用。
    """

    pairs: Optional[tuple[tuple[str, str], ...]]

    @classmethod
    def platform_all(cls) -> "AdminMemoryScope":
        """平台方的全量视图：不加实例条件。"""
        return cls(pairs=None)

    @classmethod
    def for_pairs(cls, pairs: Iterable[tuple[str, str]]) -> "AdminMemoryScope":
        """只看这些实例对；去重排序保证同一组条件渲染结果稳定。

        空集**仍是空元组**，不退化成 `None`：`None` 是平台方全量，把「一个实例都没有」
        当成全量正是授权边界上最危险的退化。
        """
        return cls(pairs=tuple(sorted(set(pairs))))

    def condition(self, table: MemTable, f: AdminFilter = AdminFilter()) -> Cond:
        """把可见范围与筛选条件拼成一棵条件树。

        顺序固定（实例对 → 分区 → 频道 → 人设 → 人），便于用例逐项比对，也让同一次
        请求的 SQL 与 Qdrant 过滤条件逐字稳定。
        """
        if f.partition not in _ADMIN_PARTITIONS:
            raise ValueError(f"未知的分区筛选: {f.partition!r}")

        items: list[Cond] = []
        if self.pairs is not None:
            items.append(member_pairs(self.pairs))
        if table is MemTable.WB_BOOK:
            # 书表没有工作区分区，也没有「涉及的人」这种集合列；它的「人」是书自己绑定的
            # 那一个人（精确等于），不是一条记忆涉及的一群人。
            if f.partition != "all":
                raise ValueError("世界书书表没有工作区分区，不能按 domain / workspace 筛选")
            if f.chat_key:
                items.append(eq(ScopeField.CHAT_KEY, f.chat_key))
            if f.preset_id is not None:
                items.append(eq(ScopeField.PRESET_ID, f.preset_id))
            if f.person_accounts is not None:
                items.append(in_(ScopeField.PERSON_ACCOUNT, sorted(f.person_accounts)))
            return and_(*items)
        if f.partition != "all":
            if table in _CHANNEL_SCOPED_TABLES:
                raise ValueError("频道类表没有工作区分区，不能按 domain / workspace 筛选")
            items.append(eq(ScopeField.WORKSPACE_ID, 0) if f.partition == "domain" else gt(ScopeField.WORKSPACE_ID, 0))
        if f.chat_key:
            if table in _CHANNEL_SCOPED_TABLES:
                items.append(eq(ScopeField.CHAT_KEY, f.chat_key))
            else:
                items.append(eq(ScopeField.ORIGIN_CHAT_KEY, f.chat_key))
        if f.preset_id is not None:
            items.append(eq(ScopeField.PRESET_ID, f.preset_id))
        if f.person_accounts is not None:
            if table is not MemTable.PARAGRAPH:
                raise ValueError("涉及的人这一维只允许出现在段落表上")
            items.append(persons_any(f.person_accounts))
        return and_(*items)

    def apply(self, queryset: QuerySet[Any], table: MemTable, f: AdminFilter = AdminFilter()) -> QuerySet[Any]:
        """把范围条件应用到一条已经带好非范围条件的查询上。

        `FALSE` 用 `filter(id__in=[])` 表达：当前 Tortoise 版本没有 `none()`，而四张记忆表
        都有主键 `id`，这个条件在 SQLite 与 PostgreSQL 上都稳定返回空集。
        """
        cond = self.condition(table, f)
        if cond is TRUE:
            return queryset
        if cond is FALSE:
            return queryset.filter(id__in=[])
        return queryset.filter(to_tortoise_q(cond, table))

    def contains_pair(self, adapter_key: str, instance_key: str) -> bool:
        """某个实例对是否在本范围内；平台方全量恒真。"""
        if self.pairs is None:
            return True
        return (adapter_key, instance_key) in self.pairs


async def resolve_admin_scope(
    user: DBUser,
    *,
    instance_key: str = "",
    adapter_key: str = "",
    domain: Literal["self", "group"] = "self",
    group_id: Optional[int] = None,
) -> AdminMemoryScope:
    """把当前身份与请求参数换算成管理端的可见范围。

    平台方不选实例时看全量；选了实例时可以再按域展开。租户必须带实例（与好感页一致），
    越权由 `assert_instance_accessible` 按 404 处理。按域展开时逐个与
    `accessible_instance_refs` 取交集：实例组的写入侧已经校验同租户，这里仍 fail-closed，
    因为「组成员同租户」是一条会被事后划转打破的不变式。

    这里的 import 全部放在函数里：`tenant_perm` 一路会拉进 `user.deps` 与路由层，
    `identity_scope` 又在模块级 import 本模块，放顶层会成环。
    """
    from nekro_agent.models.db_instance_group import DBInstanceDomainBinding
    from nekro_agent.schemas.errors import ValidationError
    from nekro_agent.services.identity_scope import resolve_domain_members
    from nekro_agent.services.user.tenant_perm import (
        accessible_instance_refs,
        assert_instance_accessible,
        get_tenant_scope,
    )

    requested_key = instance_key.strip()
    tenant_scope = get_tenant_scope(user)

    if tenant_scope is None:
        if not requested_key:
            if group_id is not None:
                return AdminMemoryScope.for_pairs(await DBInstanceDomainBinding.members_of_memory_group(group_id))
            return AdminMemoryScope.platform_all()
        instance = await assert_instance_accessible(user, requested_key, adapter_key=adapter_key)
        if domain == "group":
            return AdminMemoryScope.for_pairs(
                await resolve_domain_members(instance.adapter_key, instance.instance_key),
            )
        return AdminMemoryScope.for_pairs([(instance.adapter_key, instance.instance_key)])

    if not requested_key:
        raise ValidationError(reason="租户身份必须指定实例作用域")
    if group_id is not None:
        raise ValidationError(reason="租户身份不能按实例组筛选")
    instance = await assert_instance_accessible(user, requested_key, adapter_key=adapter_key)
    own_pair = (instance.adapter_key, instance.instance_key)
    if domain != "group":
        return AdminMemoryScope.for_pairs([own_pair])
    members = await resolve_domain_members(instance.adapter_key, instance.instance_key)
    allowed = await accessible_instance_refs(user)
    return AdminMemoryScope.for_pairs(pair for pair in members if pair in allowed)


async def admin_row_visible(user: DBUser, adapter_key: str, instance_key: str) -> bool:
    """按 ID 取到的单行是否属于当前身份。

    平台方恒真。租户要求 `adapter_key` 非空且实例对成对命中自己名下的实例：存量行的
    `adapter_key` 是空串，它们只属于工作区记忆页；把空串当成通配会让租户读到别人的历史行。
    `instance_key` 可以是空串——单实例适配器的实例段本来就是空串，是否归这个租户仍由
    成对比较决定，空串在这里不是通配。

    与 :func:`resolve_admin_scope` 同一理由局部导入。
    """
    from nekro_agent.services.user.tenant_perm import accessible_instance_refs, get_tenant_scope

    if get_tenant_scope(user) is None:
        return True
    if not adapter_key:
        return False
    return (adapter_key, instance_key) in await accessible_instance_refs(user)


def worldbook_condition(
    domain_pairs: Iterable[tuple[str, str]],
    *,
    chat_key: str,
    preset_id: int,
    present_accounts: Iterable[str],
) -> Cond:
    """运行时该注入哪些世界书的范围条件。

    与记忆的召回条件分开写，而不是复用 :class:`MemoryScope`：记忆的可见性是一个纯
    「域 + 人设」的二维问题，而世界书按书自己的作用域类型分四条腿，其中两条腿
    （`channel` / `person`）根本不看人设、另两条腿（`preset`）允许出域。把它塞进
    `MemoryScope` 会让两边的条件互相牵制，下一次改动记忆召回就会顺手改到世界书。

    四条腿（其余类型的书一律不注入，这是 fail-closed 的默认）：

    - `domain`：归属实例对在域里；
    - `channel`：书的 `chat_key` 是当前频道，且归属实例对在域里；
    - `preset`：书的人设是当前人设，且（归属实例对在域里，或归属实例对为空）；
    - `person`：书绑的人出现在最近窗口里，且归属实例对在域里。

    「归属实例对为空」只放行 `preset` 一条腿：那是平台方给全局人设建的书，它本来就该
    对所有实例生效。若把它放进 `domain` 或 `channel` 腿，一本空归属的租户书就能进任何
    频道——这正是统一不变式要挡的形状。

    `person` 这条腿在这里只按「最近窗口里出现过的人」粗筛，每本书自己的扫描深度（可能
    与窗口深度不同）由匹配引擎的 `matcher.book_active_for_person` 再判一次。两处判据
    不是重复：这里决定要不要把书捞出来，那里决定捞出来的书对这一轮是否真的成立。

    三个退化输入一律折成 :data:`FALSE` 而不是「不过滤」：没有频道标识、人设 ID 非正、
    在场账号为空集。它们在授权边界上的含义都是「一条也不该注入」。
    """
    pairs = member_pairs(domain_pairs)
    accounts = tuple(sorted({account.strip() for account in present_accounts if account and account.strip()}))

    domain_leg = and_(eq(ScopeField.SCOPE_KIND, "domain"), pairs)
    channel_leg = (
        and_(eq(ScopeField.SCOPE_KIND, "channel"), eq(ScopeField.CHAT_KEY, chat_key), pairs)
        if chat_key
        else FALSE
    )
    preset_leg = (
        and_(
            eq(ScopeField.SCOPE_KIND, "preset"),
            eq(ScopeField.PRESET_ID, preset_id),
            or_(pairs, and_(eq(ScopeField.ADAPTER_KEY, ""), eq(ScopeField.INSTANCE_KEY, ""))),
        )
        if preset_id > 0
        else FALSE
    )
    person_leg = (
        and_(
            eq(ScopeField.SCOPE_KIND, "person"),
            pairs,
            in_(ScopeField.PERSON_ACCOUNT, accounts),
        )
        if accounts
        else FALSE
    )
    return or_(domain_leg, channel_leg, preset_leg, person_leg)


def apply_worldbook_condition(queryset: QuerySet[Any], cond: Cond) -> QuerySet[Any]:
    """把 :func:`worldbook_condition` 算出的树应用到书上。

    常量处理与 :meth:`AdminMemoryScope.apply` 一致：`FALSE` 用 `filter(id__in=[])` 表达，
    书表有主键 `id`，两个后端上都稳定返回空集。
    """
    if cond is TRUE:
        return queryset
    if cond is FALSE:
        return queryset.filter(id__in=[])
    return queryset.filter(to_tortoise_q(cond, MemTable.WB_BOOK))


def _paragraph_domain_visibility(visibility: ChannelVisibility) -> Cond:
    """段落域腿的可见性：公共一行 + 当前频道类型允许的跨频道分支。

    「关」档不加分支；「相关」档的成员集 / 对方账号集为空时，`persons_*` 直接化简成
    :data:`FALSE`，整条分支随之消失，不会退化成「不过滤」。私聊的「跟人走」是一条
    不看开关的硬规则：只要段落涉及的人里有当前私聊对象，就召回到这个私聊里。
    """
    parts: list[Cond] = [
        in_(ScopeField.ORIGIN_CHAT_KEY, tuple(sorted(visibility.local_keys))),
    ]

    if visibility.chat_kind == "group":
        if visibility.group_to_group == "all":
            parts.append(eq(ScopeField.ORIGIN_CHAT_TYPE, "group"))
        elif visibility.group_to_group == "related":
            parts.append(
                and_(
                    eq(ScopeField.ORIGIN_CHAT_TYPE, "group"),
                    persons_all_in(visibility.member_accounts),
                ),
            )
        if visibility.private_to_group == "all":
            parts.append(eq(ScopeField.ORIGIN_CHAT_TYPE, "private"))
        elif visibility.private_to_group == "related":
            parts.append(
                and_(
                    eq(ScopeField.ORIGIN_CHAT_TYPE, "private"),
                    persons_all_in(visibility.member_accounts),
                ),
            )
    elif visibility.chat_kind == "private":
        # 硬规则：私聊记忆跟人走，不看三个开关。对方账号集算不出来时这一条化成假，
        # 私聊只剩自己的本地集。
        parts.append(
            and_(
                eq(ScopeField.ORIGIN_CHAT_TYPE, "private"),
                persons_any(visibility.counterpart_accounts),
            ),
        )
        if visibility.group_to_private == "all":
            parts.append(eq(ScopeField.ORIGIN_CHAT_TYPE, "group"))
        elif visibility.group_to_private == "related":
            parts.append(
                and_(
                    eq(ScopeField.ORIGIN_CHAT_TYPE, "group"),
                    persons_any(visibility.counterpart_accounts),
                ),
            )

    return or_(*parts)


_RECALL_MODES = frozenset({"off", "related", "all"})


def _recall_mode(value: object, field_name: str, chat_key: str) -> str:
    """把生效配置的取值收窄成三档；非法值按「关」处理并告警。

    不抛错：配置写坏时记忆检索应该退到最保守的一档继续工作，而不是让整条注入链路失败。
    """
    normalized = str(value) if isinstance(value, str) else ""
    if normalized in _RECALL_MODES:
        return normalized
    logger.warning(f"跨频道召回开关取值非法，按关闭处理: chat_key={chat_key}, field={field_name}, value={value!r}")
    return "off"


async def _read_recall_settings(channel: object) -> tuple[str, str, str]:
    """读当前频道的三个召回开关生效值。

    开关取**当前频道**（召回发生的地方）的生效配置，不取记忆来源频道的：组域内各实例
    同属一个租户，按「在哪儿召回由哪儿的设置决定」最好解释，也只需读一次配置。
    """
    getter = getattr(channel, "get_effective_config", None)
    if not callable(getter):
        raise ValueError("频道对象没有 get_effective_config，无法读取跨频道召回开关")
    effective = await getter()
    chat_key = getattr(channel, "chat_key", "") or ""
    return (
        _recall_mode(getattr(effective, "MEMORY_RECALL_GROUP_TO_GROUP", "off"), "MEMORY_RECALL_GROUP_TO_GROUP", chat_key),
        _recall_mode(getattr(effective, "MEMORY_RECALL_GROUP_TO_PRIVATE", "off"), "MEMORY_RECALL_GROUP_TO_PRIVATE", chat_key),
        _recall_mode(getattr(effective, "MEMORY_RECALL_PRIVATE_TO_GROUP", "off"), "MEMORY_RECALL_PRIVATE_TO_GROUP", chat_key),
    )


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

    **闸开时**额外算域成员对与频道可见性。`chat_key` / 频道类型 / `workspace_id` 拿不到
    就抛 ``ValueError``：授权边界上「算不出来」不能退化成「不过滤」。注入与
    `memory_tools` 的工作区要求属于后续版本，本函数只负责把范围算对。
    """
    from nekro_agent.services.identity_scope import (
        resolve_domain_instance_keys,
        resolve_domain_members,
        resolve_memory_domain,
        resolve_preset_id,
    )
    from nekro_agent.services.memory.persons import (
        active_member_accounts,
        expand_accounts,
        resolve_bot_account,
        resolve_private_counterpart,
    )

    allowed = await resolve_domain_instance_keys(channel.adapter_key, channel.instance_key)
    resolved_preset_id = preset_id if preset_id is not None else await resolve_preset_id(channel)

    # 负数人设档（:data:`CONSOLE_ONLY_PRESET_ID`）是控制台专用的，任何频道都不能命中它：
    # 一个频道如果解析出负数，说明人设那一维的数据或回退链坏了，退化成"什么都不放行"
    # 会让这个频道永久失忆，而沿用它则会让它读到控制台直发的结论——两条都不对。
    # 所以在这里拦下并回落到 :data:`NO_PRESET_ID`，与"没有配置人设"同一档。
    # 这一步必须在闸门分支**之前**：闸关闸开走的是同一份人设解析。
    if resolved_preset_id < 0:
        logger.warning(
            f"频道解析出负数人设标识，已回落到无人设档: chat_key={getattr(channel, 'chat_key', '')}, "
            f"preset_id={resolved_preset_id}",
        )
        resolved_preset_id = NO_PRESET_ID

    if not is_domain_scope_enabled():
        return MemoryScope(allowed_instance_keys=allowed, preset_id=resolved_preset_id)

    chat_key = getattr(channel, "chat_key", None)
    if not isinstance(chat_key, str) or not chat_key:
        raise ValueError("频道对象缺少 chat_key，无法确定记忆可见性")
    chat_kind = _resolve_chat_kind(channel)

    domain_members = await resolve_domain_members(channel.adapter_key, channel.instance_key)
    group_to_group, group_to_private, private_to_group = await _read_recall_settings(channel)
    domain = await resolve_memory_domain(channel.adapter_key, channel.instance_key)

    # 成员集只在群、且某条「相关」档真的会用到它时才算：默认全关的部署一次成员查询都不做。
    member_accounts: frozenset[str] = frozenset()
    if chat_kind == "group" and "related" in (group_to_group, private_to_group):
        raw_accounts = await active_member_accounts(
            chat_key,
            days=config.MEMORY_MEMBER_ACTIVE_DAYS,
            bot_account=await resolve_bot_account(channel.adapter_key, channel.instance_key),
        )
        member_accounts = await expand_accounts(domain, raw_accounts)

    # 对方账号集不看开关：私聊的「跟人走」是硬规则，群→私聊的相关档也要用它。
    counterpart_accounts: frozenset[str] = frozenset()
    if chat_kind == "private":
        counterpart = await resolve_private_counterpart(
            chat_key,
            adapter_key=channel.adapter_key,
            instance_key=channel.instance_key,
        )
        if counterpart:
            counterpart_accounts = await expand_accounts(domain, [counterpart])

    visibility = await _resolve_channel_visibility(
        channel,
        domain_members,
        chat_key=chat_key,
        chat_kind=chat_kind,
        group_to_group=group_to_group,
        group_to_private=group_to_private,
        private_to_group=private_to_group,
        member_accounts=member_accounts,
        counterpart_accounts=counterpart_accounts,
    )
    return MemoryScope(
        allowed_instance_keys=allowed,
        preset_id=resolved_preset_id,
        domain_members=domain_members,
        visibility=visibility,
    )


def _resolve_chat_kind(channel: object) -> str:
    """把频道的类型映射成 ``group`` / ``private`` / ``other``。

    读 ``chat_type``（`DBChatChannel` 上的属性，返回 `ChatType`）或 ``channel_type``
    （列里的字符串），**不解析 `chat_key` 字符串**：实例段与频道段的格式是适配器私有的。
    """
    raw: object = None
    has_type_attribute = False
    for attribute in ("chat_type", "channel_type"):
        if not hasattr(channel, attribute):
            continue
        has_type_attribute = True
        try:
            raw = getattr(channel, attribute)
        except (TypeError, ValueError):
            # `DBChatChannel.chat_type` 在列值为 NULL / 空串 / 非法值时，
            # `ChatType(...)` 会抛 TypeError 或 ValueError。那表示「类型未知」，
            # 不是「拿不到属性」，按 other 处理即可。
            raw = None
        if raw is not None:
            break
    if not has_type_attribute:
        raise ValueError("频道对象既没有 chat_type 也没有 channel_type，无法确定可见性")
    value = getattr(raw, "value", raw)
    if value == "group":
        return "group"
    if value == "private":
        return "private"
    return "other"


async def _resolve_channel_visibility(
    channel: object,
    domain_members: tuple[tuple[str, str], ...],
    *,
    chat_key: str,
    chat_kind: str,
    group_to_group: str,
    group_to_private: str,
    private_to_group: str,
    member_accounts: frozenset[str],
    counterpart_accounts: frozenset[str],
) -> ChannelVisibility:
    """算当前频道的本地集与三个开关的生效值。

    本地集 = 自己；群且绑了工作区时，再加上同工作区、同域的其他**群**。私聊永远只有
    自己：私聊的聊天记录里带对方的账号，把它并进同工作区的群集合等于让同租户的另一个
    群看到别人的私聊。
    """
    from nekro_agent.models.db_chat_channel import DBChatChannel

    if not hasattr(channel, "workspace_id"):
        raise ValueError("频道对象缺少 workspace_id，无法确定记忆可见性")

    local_keys: set[str] = {chat_key}
    workspace_id = getattr(channel, "workspace_id")
    if chat_kind == "group" and workspace_id is not None:
        member_set = set(domain_members)
        rows = await DBChatChannel.filter(workspace_id=workspace_id, channel_type="group")
        for row in rows:
            if (row.adapter_key, row.instance_key) in member_set:
                local_keys.add(row.chat_key)

    return ChannelVisibility(
        chat_key=chat_key,
        chat_kind=chat_kind,
        local_keys=frozenset(local_keys),
        group_to_group=group_to_group,
        group_to_private=group_to_private,
        private_to_group=private_to_group,
        member_accounts=member_accounts,
        counterpart_accounts=counterpart_accounts,
    )


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
