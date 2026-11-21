"""记忆可见范围的条件树：数据与三个渲染器。

## 为什么要有条件树

记忆的可见范围要同时落到三个地方：Tortoise 的 SQL 条件（PG / SQLite）、Qdrant 的
payload 过滤条件、以及测试里用来做参照的纯 Python 求值。三处各写一份「等价」的
条件，就是同一份授权判据的第二个实现；本项目已经反复在「第二处实现」上栽过
（只改一份、另一份静默放宽），而这里的后果是跨域 / 跨人设的记忆被召回。

因此可见范围先被拼成一棵**只含数据**的树，三个渲染器都从这棵树出发：

- :func:`to_tortoise_q` 给数据库侧；
- :func:`to_qdrant_filter` 给向量侧（只用于段落）；
- :func:`evaluate` 给测试当参照语义。

本模块不查库、不 import ORM 模型（只有人这一维的渲染需要一个模型，延迟导入），
因此可以在任何一侧安全地被调用。

世界书的书表（:attr:`MemTable.WB_BOOK`）也走这棵树，但它只经两个渲染器：
:func:`to_tortoise_q` 与管理端 / 运行时的纯 Python 求值。书不进向量库，因此
:func:`to_qdrant_filter` 与书表无关，也不会被它调用。

## Qdrant 的数组语义

对数组字段（`person_accounts`），本机 qdrant-client 1.15.1 本地模式实测：

- `MatchAny(M)` = 至少一个元素在 M；
- `MatchExcept(M)` = 至少一个元素不在 M（空数组与缺键都不匹配）。

所以 `MatchAny(M) ∧ ¬MatchExcept(M)` = 「非空且全在 M 里」，与 SQL 侧的两条子查询逐项
对应。数组上不能把「全部在 M 里」直接写成一条 `MatchExcept` 的取反：缺键的点会被
`must_not` 放行，而那恰好是本该判假的行。

## 唯一允许两侧写法不同的叶子

:class:`PresetVisible` 是唯一的例外。数据库侧的存量行 ``preset_id`` 有列默认值 0，
所以 ``preset_id in (当前人设, 0)`` 就覆盖了「不属于任何人设」这一档；而改造前写入的
Qdrant point **根本没有 preset_id 这个 key**，Qdrant 的 ``must`` 对缺字段的 point 判为
不匹配。向量侧因此必须多放行「字段为空」这一档（``IsEmpty``），否则升级那一刻全部存量
向量集体落选，表现为 Bot 集体失忆且没有任何报错。两者的等价性由 PG 存量行的默认值 0
保证，判据沿用改造前 ``_build_preset_condition`` 的理由。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable, Mapping, Union

from qdrant_client import models as qdrant_models
from tortoise.expressions import Q, Subquery

# 「不属于任何人设」在 payload 与数据库两侧的共同取值。本模块是 `instance_scope` 的下层，
# 不能反过来 import 它（会成环），因此这里保留一个字面量；`instance_scope.NO_PRESET_ID`
# 的值必须与它相同，由冻结副本用例从行为上钉住。
PRESET_NONE_PAYLOAD = 0


class ScopeField(StrEnum):
    """可见范围条件里允许出现的字段。

    值同时是数据库列名与 Qdrant payload 键名，**不要在别处再写一份字符串**：两边的键
    一旦分叉，同一次检索的两条腿就会给出不同的可见集合。
    """

    WORKSPACE_ID = "workspace_id"
    ADAPTER_KEY = "adapter_key"
    INSTANCE_KEY = "instance_key"
    ORIGIN_CHAT_KEY = "origin_chat_key"
    ORIGIN_CHAT_TYPE = "origin_chat_type"
    # 管理端的两维：人设是精确等于（与召回的 PresetVisible 不同），频道列在摘要类表上
    # 叫 `chat_key`（它们没有「来源频道」这个概念，频道就是主键的一半）。
    PRESET_ID = "preset_id"
    CHAT_KEY = "chat_key"
    # 世界书书表的两维：作用域类型与 person 书绑定的人。只登记给 `wb_book`，记忆表上
    # 用它们会被 `_assert_field` 挡住——这两列在记忆表里根本不存在。
    SCOPE_KIND = "scope_kind"
    PERSON_ACCOUNT = "person_account"


class MemTable(StrEnum):
    """条件树可以落在哪一张记忆表上。"""

    PARAGRAPH = "paragraph"
    ENTITY = "entity"
    RELATION = "relation"
    EPISODE = "episode"
    SUMMARY = "summary"
    SUMMARY_STATE = "summary_state"
    PROMPT_ROUND = "prompt_round"
    WB_BOOK = "wb_book"


# 每张表真正存在的范围列。实体与关系没有「来源频道类型」这一列（本轮新加的
# `origin_chat_key` 才是它们的来源维），在它们上面渲染 `ORIGIN_CHAT_TYPE` 是调用方的 bug。
# 摘要类表与提示词明细按「频道 + 人设」归属，没有工作区分区、来源频道类型与
# 「涉及的人」：它们的隔离键是 `(频道, 人设)`，实例对只是归属信息。管理端仍要按实例
# 范围过滤，所以 `ADAPTER_KEY` / `INSTANCE_KEY` 在。
TABLE_FIELDS: dict[MemTable, frozenset[ScopeField]] = {
    MemTable.PARAGRAPH: frozenset(
        {
            ScopeField.WORKSPACE_ID,
            ScopeField.ADAPTER_KEY,
            ScopeField.INSTANCE_KEY,
            ScopeField.ORIGIN_CHAT_KEY,
            ScopeField.ORIGIN_CHAT_TYPE,
            ScopeField.PRESET_ID,
        }
    ),
    MemTable.ENTITY: frozenset(
        {
            ScopeField.WORKSPACE_ID,
            ScopeField.ADAPTER_KEY,
            ScopeField.INSTANCE_KEY,
            ScopeField.ORIGIN_CHAT_KEY,
            ScopeField.PRESET_ID,
        }
    ),
    MemTable.RELATION: frozenset(
        {
            ScopeField.WORKSPACE_ID,
            ScopeField.ADAPTER_KEY,
            ScopeField.INSTANCE_KEY,
            ScopeField.ORIGIN_CHAT_KEY,
            ScopeField.PRESET_ID,
        }
    ),
    MemTable.EPISODE: frozenset(
        {
            ScopeField.WORKSPACE_ID,
            ScopeField.ADAPTER_KEY,
            ScopeField.INSTANCE_KEY,
            ScopeField.ORIGIN_CHAT_KEY,
            ScopeField.ORIGIN_CHAT_TYPE,
            ScopeField.PRESET_ID,
        }
    ),
    MemTable.SUMMARY: frozenset(
        {
            ScopeField.ADAPTER_KEY,
            ScopeField.INSTANCE_KEY,
            ScopeField.CHAT_KEY,
            ScopeField.PRESET_ID,
        }
    ),
    MemTable.SUMMARY_STATE: frozenset(
        {
            ScopeField.ADAPTER_KEY,
            ScopeField.INSTANCE_KEY,
            ScopeField.CHAT_KEY,
            ScopeField.PRESET_ID,
        }
    ),
    MemTable.PROMPT_ROUND: frozenset(
        {
            ScopeField.ADAPTER_KEY,
            ScopeField.INSTANCE_KEY,
            ScopeField.CHAT_KEY,
            ScopeField.PRESET_ID,
        }
    ),
    # 书表按「归属实例对 + 四种作用域」隔离，没有工作区，也没有「涉及的人」这种集合列。
    # 条目表不进这张表：条目只经书访问，范围永远算在书上。
    MemTable.WB_BOOK: frozenset(
        {
            ScopeField.ADAPTER_KEY,
            ScopeField.INSTANCE_KEY,
            ScopeField.PRESET_ID,
            ScopeField.CHAT_KEY,
            ScopeField.SCOPE_KIND,
            ScopeField.PERSON_ACCOUNT,
        }
    ),
}


@dataclass(frozen=True)
class Eq:
    """字段等于某个值。"""

    field: ScopeField
    value: Any


@dataclass(frozen=True)
class Gt:
    """字段严格大于某个值。

    管理端用它表达「工作区行」：`workspace_id > 0`。不能写成 `not_(eq(...))` —— 取反只
    接受 `PersonsOutside`，那条约束本身的理由见 :func:`not_`。NULL / 缺键一律判假，
    与 SQL 的三值逻辑一致。
    """

    field: ScopeField
    value: Any


@dataclass(frozen=True)
class In:
    """字段属于一个集合。"""

    field: ScopeField
    values: tuple[Any, ...]


@dataclass(frozen=True)
class IsNull:
    """字段为 NULL。

    Qdrant 侧的语义是「键存在且为 null」，与「缺这个键」不同；这条差异只在测试里
    用真实过滤验证，不在树上做抽象。
    """

    field: ScopeField


@dataclass(frozen=True)
class PresetVisible:
    """人设维：``preset_id in (p, 0)``。

    这是唯一允许数据库侧与向量侧写法不同的叶子，理由见模块文档。
    """

    preset_id: int


@dataclass(frozen=True)
class MemberPairs:
    """域成员对：``OR (adapter_key = a AND instance_key = i)``。"""

    pairs: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class PersonsAny:
    """涉及的人里至少一个在给定账号集合里。

    只允许出现在段落表上：这一维的数据挂在段落上，实体 / 关系 / episode 的可见性不按人。
    """

    accounts: tuple[str, ...]


@dataclass(frozen=True)
class PersonsOutside:
    """涉及的人里至少一个不在给定账号集合里。

    只经 :func:`persons_all_in` 产生，不提供单独构造函数：它唯一正确的用途是给
    「所有人都在集合里」做否定的一半，单独构造很容易把语义写反。只允许出现在段落表上。
    """

    accounts: tuple[str, ...]


@dataclass(frozen=True)
class And:
    """全部成立。"""

    items: tuple["Cond", ...]


@dataclass(frozen=True)
class Or:
    """任一成立。"""

    items: tuple["Cond", ...]


@dataclass(frozen=True)
class Not:
    """取反。"""

    item: "Cond"


@dataclass(frozen=True)
class _Constant:
    """条件树里的真 / 假单例。

    用单例而不是 ``True`` / ``False`` 是为了让「化简成假」在调用点上可以用
    ``cond is FALSE`` 判断，并且不会被 ``bool`` 与条件节点混在同一个位置。
    """

    value: bool


TRUE = _Constant(True)
FALSE = _Constant(False)

Cond = Union[
    Eq,
    Gt,
    In,
    IsNull,
    PresetVisible,
    MemberPairs,
    PersonsAny,
    PersonsOutside,
    And,
    Or,
    Not,
    _Constant,
]


def eq(field: ScopeField, value: Any) -> Cond:
    """构造「字段等于」叶子。"""
    return Eq(field=field, value=value)


def gt(field: ScopeField, value: Any) -> Cond:
    """构造「字段严格大于」叶子。"""
    return Gt(field=field, value=value)


def in_(field: ScopeField, values: Iterable[Any]) -> Cond:
    """构造「字段属于」，空集合直接化简成 :data:`FALSE`。

    值去重时保留原顺序：同一棵树必须永远渲染出同一个对象，集合的迭代顺序在不同进程里
    不稳定，会让「逐字相同」的用例随机失败。
    """
    unique: list[Any] = []
    for value in values:
        if value not in unique:
            unique.append(value)
    if not unique:
        return FALSE
    return In(field=field, values=tuple(unique))


def is_null(field: ScopeField) -> Cond:
    """构造「字段为 NULL」。"""
    return IsNull(field=field)


def preset_visible(preset_id: int) -> Cond:
    """构造人设维叶子。"""
    return PresetVisible(preset_id=preset_id)


def member_pairs(pairs: Iterable[tuple[str, str]]) -> Cond:
    """构造域成员对；空集合直接化简成 :data:`FALSE`。

    空成员对意味着「这个域里谁也不是」，绝不等于「不过滤」——授权边界上退化成一类
    未知状态是泄露的形状。
    """
    materialized = tuple(pairs)
    if not materialized:
        return FALSE
    return MemberPairs(pairs=materialized)


def _normalize_accounts(accounts: Iterable[str]) -> tuple[str, ...]:
    """账号去重排序：渲染结果不能随集合迭代顺序变化。"""
    return tuple(sorted({account for account in accounts if account}))


def persons_any(accounts: Iterable[str]) -> Cond:
    """构造「涉及的人里至少一个在集合里」；空集合直接化简成 :data:`FALSE`。

    空集合不是「不过滤」：没有人这一维的记忆不该被这条分支召回。
    """
    normalized = _normalize_accounts(accounts)
    if not normalized:
        return FALSE
    return PersonsAny(accounts=normalized)


def persons_all_in(accounts: Iterable[str]) -> Cond:
    """构造「至少有一个涉及的人，且所有人都在集合里」；空集合化简成 :data:`FALSE`。

    派生写法是 :func:`persons_any` 与「至少一个不在」取反的合取。之所以不直接写一条
    「所有人都在」，是因为「空集合」在集合论上恒真，而在授权边界上它必须恒假。
    """
    normalized = _normalize_accounts(accounts)
    if not normalized:
        return FALSE
    return and_(PersonsAny(accounts=normalized), not_(PersonsOutside(accounts=normalized)))


def and_(*items: Cond) -> Cond:
    """构造合取，并做常量化简。

    - 去掉 :data:`TRUE`；
    - 任一项是 :data:`FALSE` 时整体就是 :data:`FALSE`；
    - 空合取是 :data:`TRUE`；
    - 只剩一项时返回它自己。
    """
    simplified: list[Cond] = []
    for item in items:
        if item is TRUE:
            continue
        if item is FALSE:
            return FALSE
        simplified.append(item)
    if not simplified:
        return TRUE
    if len(simplified) == 1:
        return simplified[0]
    return And(items=tuple(simplified))


def or_(*items: Cond) -> Cond:
    """构造析取，并做常量化简。

    - 去掉 :data:`FALSE`；
    - 任一项是 :data:`TRUE` 时整体就是 :data:`TRUE`；
    - 空析取是 :data:`FALSE`；
    - 只剩一项时返回它自己。
    """
    simplified: list[Cond] = []
    for item in items:
        if item is FALSE:
            continue
        if item is TRUE:
            return TRUE
        simplified.append(item)
    if not simplified:
        return FALSE
    if len(simplified) == 1:
        return simplified[0]
    return Or(items=tuple(simplified))


def not_(item: Cond) -> Cond:
    """构造取反；``TRUE`` / ``FALSE`` 当场对折。

    除常量外**只接受** :class:`PersonsOutside`，其余一律抛 ``ValueError``。

    为什么收得这么紧：SQL 的 ``NOT`` 是三值逻辑，在可空字段上取反会把 NULL 行排除
    （``NOT (x = 1)`` 对 ``x IS NULL`` 结果是 NULL，WHERE 里等于不成立），而纯 Python
    的 ``not`` 与 Qdrant 的 ``must_not`` 都会把那种行当成真。三方从此不一致，且不一致的
    方向取决于字段是不是 NULL——这正是本项目反复栽过的「同一判据的第二处实现」。
    ``PersonsOutside`` 渲染出来是「主键 IN 子查询」，子查询的账号列非空，所以它永远
    不为 NULL，取反安全。
    """
    if item is TRUE:
        return FALSE
    if item is FALSE:
        return TRUE
    if not isinstance(item, PersonsOutside):
        raise ValueError("not_ 只接受 PersonsOutside：其余节点在 SQL 三值逻辑下与 Python / Qdrant 不等价")
    return Not(item=item)


def build_preset_condition(preset_id: int) -> qdrant_models.Filter:
    """人设维的向量侧过滤条件。

    判据必须与数据库侧的 ``PresetVisible`` 逐字对应，否则同一次检索的两条腿会给出不同
    的可见集合：向量召回一批 ID，SQL 侧再按条件取回行，松的那一侧白召回、紧的那一侧
    静默丢结果。数据库侧是 ``preset_id in (当前人设, 0)``，向量侧因此是三档：

    - 等于当前人设；
    - 等于 0（「不属于任何人设」：存量记忆、改造前写入的控制台直发 CC 结论）；
    - **payload 里没有 preset_id 这个 key**——改造前写入的 point 就是这样，而 Qdrant
      的 ``must`` 对缺字段的 point 判为不匹配。少了这一档，升级那一刻每一个存量向量
      都会落选，表现为 Bot 集体失忆，没有任何报错指向这一行。它与数据库侧的列默认值 0
      是同一件事：那边存量行读出来是 0，这边把「没有这个 key」读成同一个 0。

    控制台专用的负数档（新增的控制台直发 CC 结论落在那里）在**这里永远不匹配**：
    档位是负数、0 与「缺 key」都不是负数，频道侧也就永远召回不到这些行。
    这是有意的隔离，不是遗漏——它让那批结论只对不按人设过滤的调用方可见
    （CC 握手与后台记忆页）。
    """
    # `preset_id` 不属于 `ScopeField`：它是人设维，不是可见范围的通用列。
    should: list[Any] = [
        qdrant_models.FieldCondition(
            key="preset_id",
            match=qdrant_models.MatchValue(value=preset_id),
        ),
    ]
    if preset_id != PRESET_NONE_PAYLOAD:
        should.append(
            qdrant_models.FieldCondition(
                key="preset_id",
                match=qdrant_models.MatchValue(value=PRESET_NONE_PAYLOAD),
            ),
        )
    should.append(
        qdrant_models.IsEmptyCondition(
            is_empty=qdrant_models.PayloadField(key="preset_id"),
        ),
    )
    return qdrant_models.Filter(should=should)


def _assert_field(field: ScopeField, table: MemTable) -> None:
    """字段必须真的存在于这张表上，否则渲染出来的 SQL 会在运行期才炸。"""
    if field not in TABLE_FIELDS[table]:
        raise ValueError(f"字段 {field.value} 不在 {table.value} 表的可见范围列里")


def _assert_person_table(table: MemTable) -> None:
    """人这一维只挂在段落上；其它表出现它说明调用方拼错了树的形状。"""
    if table is not MemTable.PARAGRAPH:
        raise ValueError("涉及的人这一维只允许出现在段落表上")


def _ensure_real_condition(cond: Cond) -> None:
    """渲染器入口的常量检查：``FALSE`` 必须由调用方先判掉。"""
    if cond is FALSE:
        raise ValueError("条件恒为 FALSE：调用方应先判断 cond is FALSE 并直接返回空结果")


def to_tortoise_q(cond: Cond, table: MemTable) -> Q:
    """把条件树渲染成 Tortoise ``Q``。

    为什么需要它：数据库侧是四张表共用的检索出口，少写一处作用域就等于多一条无过滤的
    旁路。让所有出口从同一棵树渲染，可以把「漏写」压缩成「树算错」，而树是有穷举用例的。

    ``FALSE`` 在这里抛错而不是返回一个永远为假的 ``Q``：调用方（``MemoryScope.apply``）
    必须先判断 ``cond is FALSE``，然后返回一个必然为空的查询集。把「空集」表达成一个
    看起来普通、其实恒假的条件，会让下一处拼接忘记它、或者把它与别的条件做 ``|`` 时
    静默恢复可见性。
    """
    _ensure_real_condition(cond)
    if cond is TRUE:
        return Q()
    if isinstance(cond, Eq):
        _assert_field(cond.field, table)
        return Q(**{cond.field.value: cond.value})
    if isinstance(cond, Gt):
        _assert_field(cond.field, table)
        return Q(**{f"{cond.field.value}__gt": cond.value})
    if isinstance(cond, In):
        _assert_field(cond.field, table)
        return Q(**{f"{cond.field.value}__in": list(cond.values)})
    if isinstance(cond, IsNull):
        _assert_field(cond.field, table)
        return Q(**{f"{cond.field.value}__isnull": True})
    if isinstance(cond, PresetVisible):
        if cond.preset_id == PRESET_NONE_PAYLOAD:
            return Q(preset_id=PRESET_NONE_PAYLOAD)
        return Q(preset_id__in=[cond.preset_id, PRESET_NONE_PAYLOAD])
    if isinstance(cond, MemberPairs):
        if not cond.pairs:
            raise ValueError("MemberPairs 为空：构造时应已化简成 FALSE")
        rendered: Q | None = None
        for adapter_key, instance_key in cond.pairs:
            pair = Q(adapter_key=adapter_key, instance_key=instance_key)
            rendered = pair if rendered is None else rendered | pair
        if rendered is None:
            raise ValueError("MemberPairs 为空：构造时应已化简成 FALSE")
        return rendered
    if isinstance(cond, (PersonsAny, PersonsOutside)):
        _assert_person_table(table)
        if not cond.accounts:
            raise ValueError("涉及的人集合为空：构造时应已化简成 FALSE")
        # 延迟导入：只有人这一维的渲染需要一个模型，其它维度与测试求值都不该因此把
        # ORM 的整条导入链拖进来。
        from nekro_agent.models.db_mem_paragraph_person import DBMemParagraphPerson

        person_query = (
            Q(account__in=list(cond.accounts))
            if isinstance(cond, PersonsAny)
            else ~Q(account__in=list(cond.accounts))
        )
        # 用子查询而不是先取出段落 ID：段落一多就会超过驱动一条语句的参数上限。
        # 子查询只选 `paragraph_id`，账号列非空，所以 `IN (...) / NOT IN (...)`
        # 不会出现 SQL 三值逻辑的 NULL 行。
        return Q(
            id__in=Subquery(
                DBMemParagraphPerson.filter(person_query).values("paragraph_id"),
            ),
        )
    if isinstance(cond, And):
        if not cond.items:
            return Q()
        rendered_and = to_tortoise_q(cond.items[0], table)
        for item in cond.items[1:]:
            rendered_and = rendered_and & to_tortoise_q(item, table)
        return rendered_and
    if isinstance(cond, Or):
        if not cond.items:
            raise ValueError("Or 为空：构造时应已化简成 FALSE")
        rendered_or = to_tortoise_q(cond.items[0], table)
        for item in cond.items[1:]:
            rendered_or = rendered_or | to_tortoise_q(item, table)
        return rendered_or
    if isinstance(cond, Not):
        return ~to_tortoise_q(cond.item, table)
    raise TypeError(f"未知的条件节点: {type(cond)!r}")


def _render_qdrant_piece(cond: Cond) -> Any:
    """渲染一个条件节点，叶子返回裸条件，便于父节点直接放进 ``must`` / ``should``。

    ``to_qdrant_filter`` 的返回值必须是 ``Filter``；内部节点之间则不需要每个叶子再包一层
    ``Filter``。写法固定成「叶子裸放、组合节点是 Filter」，同一棵树就永远得到同一个对象。
    """
    _ensure_real_condition(cond)
    if cond is TRUE:
        return qdrant_models.Filter()
    if isinstance(cond, Eq):
        return qdrant_models.FieldCondition(
            key=cond.field.value,
            match=qdrant_models.MatchValue(value=cond.value),
        )
    if isinstance(cond, Gt):
        return qdrant_models.FieldCondition(
            key=cond.field.value,
            range=qdrant_models.Range(gt=cond.value),
        )
    if isinstance(cond, In):
        return qdrant_models.FieldCondition(
            key=cond.field.value,
            match=qdrant_models.MatchAny(any=list(cond.values)),
        )
    if isinstance(cond, IsNull):
        return qdrant_models.IsNullCondition(
            is_null=qdrant_models.PayloadField(key=cond.field.value),
        )
    if isinstance(cond, PresetVisible):
        return build_preset_condition(cond.preset_id)
    if isinstance(cond, MemberPairs):
        if not cond.pairs:
            raise ValueError("MemberPairs 为空：构造时应已化简成 FALSE")
        return qdrant_models.Filter(
            should=[
                qdrant_models.Filter(
                    must=[
                        qdrant_models.FieldCondition(
                            key=ScopeField.ADAPTER_KEY.value,
                            match=qdrant_models.MatchValue(value=adapter_key),
                        ),
                        qdrant_models.FieldCondition(
                            key=ScopeField.INSTANCE_KEY.value,
                            match=qdrant_models.MatchValue(value=instance_key),
                        ),
                    ]
                )
                for adapter_key, instance_key in cond.pairs
            ]
        )
    if isinstance(cond, PersonsAny):
        if not cond.accounts:
            raise ValueError("涉及的人集合为空：构造时应已化简成 FALSE")
        return qdrant_models.FieldCondition(
            key="person_accounts",
            match=qdrant_models.MatchAny(any=list(cond.accounts)),
        )
    if isinstance(cond, PersonsOutside):
        if not cond.accounts:
            raise ValueError("涉及的人集合为空：构造时应已化简成 FALSE")
        return qdrant_models.FieldCondition(
            key="person_accounts",
            match=qdrant_models.MatchExcept(**{"except": list(cond.accounts)}),
        )
    if isinstance(cond, And):
        return qdrant_models.Filter(must=[_render_qdrant_piece(item) for item in cond.items])
    if isinstance(cond, Or):
        return qdrant_models.Filter(should=[_render_qdrant_piece(item) for item in cond.items])
    if isinstance(cond, Not):
        return qdrant_models.Filter(must_not=[_render_qdrant_piece(cond.item)])
    raise TypeError(f"未知的条件节点: {type(cond)!r}")


def to_qdrant_filter(cond: Cond) -> qdrant_models.Filter:
    """把条件树渲染成 Qdrant 的 ``Filter``（只用于段落）。

    段落是唯一进 Qdrant 的记忆表；实体、关系、episode 只在数据库侧过滤。``FALSE`` 与
    Tortoise 侧一样抛错，由调用方提前返回空结果、不向 Qdrant 发请求。
    """
    rendered = _render_qdrant_piece(cond)
    if isinstance(rendered, qdrant_models.Filter):
        return rendered
    return qdrant_models.Filter(must=[rendered])


def evaluate(cond: Cond, row: Mapping[str, Any]) -> bool:
    """纯 Python 参照语义：对一行「列名到值」的字典求值。

    它只给测试当参照。行里的 ``None`` 表示 SQL 的 NULL；缺键也按 ``None`` 处理，因为
    PostgreSQL 行经 ORM 转成字典后不会缺列，缺键只会出现在测试手写的行里。
    """
    if cond is TRUE:
        return True
    if cond is FALSE:
        return False
    if isinstance(cond, Eq):
        return row.get(cond.field.value) == cond.value
    if isinstance(cond, Gt):
        value = row.get(cond.field.value)
        # SQL 的 `NULL > x` 不成立；参照语义同样把缺键与 None 判假。
        return value is not None and value > cond.value
    if isinstance(cond, In):
        value = row.get(cond.field.value)
        # SQL 的 `IN (NULL)` 永远不成立；参照语义也保持这一点，避免手写行里的 None
        # 被当成一个可命中的值。
        return value is not None and value in cond.values
    if isinstance(cond, IsNull):
        return row.get(cond.field.value) is None
    if isinstance(cond, PresetVisible):
        value = row.get("preset_id")
        if cond.preset_id == PRESET_NONE_PAYLOAD:
            return value == PRESET_NONE_PAYLOAD
        return value in {cond.preset_id, PRESET_NONE_PAYLOAD}
    if isinstance(cond, MemberPairs):
        pair = (row.get(ScopeField.ADAPTER_KEY.value), row.get(ScopeField.INSTANCE_KEY.value))
        return pair in cond.pairs
    if isinstance(cond, (PersonsAny, PersonsOutside)):
        # 缺键按空列表处理：存量点 / 手写行都可能没有这一维，空集合两条判据都为假。
        raw_accounts = row.get("person_accounts") or []
        accounts = {str(account) for account in raw_accounts}
        if isinstance(cond, PersonsAny):
            return bool(accounts & set(cond.accounts))
        return any(account not in cond.accounts for account in accounts)
    if isinstance(cond, And):
        return all(evaluate(item, row) for item in cond.items)
    if isinstance(cond, Or):
        return any(evaluate(item, row) for item in cond.items)
    if isinstance(cond, Not):
        return not evaluate(cond.item, row)
    raise TypeError(f"未知的条件节点: {type(cond)!r}")


__all__ = [
    "FALSE",
    "PRESET_NONE_PAYLOAD",
    "TABLE_FIELDS",
    "TRUE",
    "And",
    "Cond",
    "Eq",
    "Gt",
    "In",
    "IsNull",
    "MemTable",
    "MemberPairs",
    "Not",
    "Or",
    "PersonsAny",
    "PersonsOutside",
    "PresetVisible",
    "ScopeField",
    "and_",
    "build_preset_condition",
    "eq",
    "evaluate",
    "gt",
    "in_",
    "is_null",
    "member_pairs",
    "not_",
    "or_",
    "persons_all_in",
    "persons_any",
    "preset_visible",
    "to_qdrant_filter",
    "to_tortoise_q",
]
