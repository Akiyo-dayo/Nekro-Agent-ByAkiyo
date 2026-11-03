"""隔离键的**后台侧**：域配置、账号合并、换人设影响面、人设孤儿数据

## 与 `services/identity_scope.py` 的分工

`identity_scope` 是**读侧**：每条消息都要走，负责把 `(频道, 账号)` 解析成
`(记忆域, 人, 人设)`。本模块是**写侧与展示侧**：运营在后台配域、合并账号、换人设、
清孤儿数据时用的那一套。两者的关系是"同一套判据的两侧"，因此本模块：

- **域的编码一律取 `ScopeDomain`**，不自己拼 `domain_id`、不自己写那三列过滤条件；
- **域的写入一律走 `DBInstanceDomainBinding.bind_to_groups`**，不自己 `create/update`
  （归属校验长在那个方法上，绕开它就是跨租户串域）；
- **判"这个实例归谁"一律走 `tenant_perm`**，不自己比 `tenant_id`。

## 账号合并为什么**一个数据行都不动**

账号合并的设计约束是：必须用别名指向，不能物理合并数据行；合错了要能拆开，删别名即撤销。
于是本模块的合并动作只做一件事：往 `identity_alias` 写一行 `(域, 副账号) → 主账号`。

那"好感取高的"这条语义落在哪里？——落在**方向**上，而不是落在一次写入上。
读侧解析到的人是主账号，因此生效的就是主账号那一份；所以合并前要把**分高的那一侧
选成主账号**。:func:`preview_merge` 因此除了两边的数值，还给出 `recommended_person`，
前端默认按它填，操作者可以改但会看到一条警告。

反过来做（把 max 写进主账号那行）看着更"直白"，但它有两个不可接受的后果：

1. 那是一次**物理合并**——撤销之后主账号那行留着一个来路不明的高分，
   而"删别名即撤销"承诺的是回到合并前的状态；
2. 好感的写入口是 `services/affection/store.py`，在这里再写一份"建行 + 派生阶段 + 记流水"
   就是第二处实现，两处的键形态迟早分叉，而隔离键是授权边界。

## 链式别名由合并动作维护，不由解析器兜

`identity_scope.resolve_person_uids` 只跟一跳，发现链只告警不追。维护"不成链"是本模块的
职责：把 A 并进 C 时，若已有 B→A，必须把 B→C 一并改写（:func:`merge_accounts` 在同一个
事务里做这件事），并且当 C 自己是别名时先跟一跳落到它的终点。

## 换人设的影响面为什么只数好感

「本次将影响 N 人」这句话必须能落到一个**查得出来的数**上。好感表上有 `preset_id` 这一列，
数得出来；`mem_*` 四张表**没有**人设列也没有账号列（见 `models/db_mem_paragraph.py`），
所以"记忆条数"只能给到**域**这一层。宁可把这件事在响应里说清楚
（`memory_scope_note`），也不要凑一个看起来精确、其实是别的东西的数字。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_affection import DBAffection, DBAffectionEvent
from nekro_agent.models.db_identity import DBIdentityAlias, DBIdentityPerson
from nekro_agent.models.db_instance_group import (
    SELF_DOMAIN_GROUP_ID,
    DBInstanceDomainBinding,
    DBInstanceGroup,
)
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.schemas.errors import ConcurrentWriteConflictError
from nekro_agent.services.affection.key import AffectionKey
from nekro_agent.services.affection.merge import COMPENSATION_SOURCES
from nekro_agent.services.affection.stage import AffectionEventSource, stage_label, stage_of
from nekro_agent.services.affection.store import apply_delta, invalidate_cache
from nekro_agent.services.identity_scope import (
    PRESET_NONE,
    ScopeDomain,
    resolve_affection_domain,
    resolve_domain_instance_keys,
    resolve_memory_domain,
)
from nekro_agent.tools.untrusted_content import sanitize_inline_text

logger = get_sub_logger("identity_admin")

# 组名与备注的长度上限。两者都是管理员填写的展示字段，落库前按不可信内容清洗。
DOMAIN_NAME_MAX_LENGTH = 128
NOTE_MAX_LENGTH = 500

# 「无人设档」在后台的展示名。0 不是一个人设行，它是"回落到内置默认人设"那一档。
PRESET_NONE_LABEL = "未绑定人设（内置默认）"

# 合并预览里逐人设明细的条数上限。一个人在几十个人设下都有记录是异常形态，
# 展示前几条足以判断，不要把一个越权探测变成一次全表扫描。
MAX_PRESET_BREAKDOWN = 20


@dataclass(frozen=True)
class DomainMember:
    """域里的一个成员实例"""

    adapter_key: str
    instance_key: str
    display_name: str = ""


@dataclass(frozen=True)
class DomainSummary:
    """一个域（实例组）在后台的展示形态"""

    group_id: int
    name: str
    note: str
    tenant_id: Optional[int]
    members: List[DomainMember]
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None


@dataclass(frozen=True)
class InstanceDomainInfo:
    """一个实例当前的域归属

    `group_id` 为 `None` 表示**自成一域**——库里没有绑定行，与今天的行为逐字相同。
    """

    adapter_key: str
    instance_key: str
    display_name: str
    tenant_id: Optional[int]
    group_id: Optional[int]
    group_name: str
    domain_id: str


@dataclass(frozen=True)
class DomainJoinPreview:
    """实例入域并入存量好感的预览

    与 `MergePreview` 同一形态：`blockers` 非空时前端置灰执行，真正的判据在执行侧
    （`merge_self_domain_into_group` 由 `set_instance_domain` 在绑定写入的同一事务里调）。
    """

    adapter_key: str
    instance_key: str
    source_domain_id: str
    target_domain_id: str
    target_domain_label: str
    affection_rows: int
    affection_people: int
    event_rows: int
    # 源行按目标域别名归一成「最终的人」之后，与目标域已有行、或与另一条同样归一过来的源行
    # 撞上「同（人, 人设）」、将按「取高」折叠的行数
    fold_pairs: int
    # 其 uid 被目标域别名改写成最终的人的源行数（折叠与否都算）
    remapped_rows: int
    alias_rows: int
    alias_conflicts: List[str]
    # 搬过去时被跟到终点改写的源别名，形如「A（P → Q）」
    alias_rewrites: List[str]
    person_rows: int
    blockers: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def can_migrate(self) -> bool:
        return not self.blockers


@dataclass(frozen=True)
class DomainJoinMigration:
    """一次「自成一域 → 组域」存量并入的实做结果，供后台如实回显"""

    affection_rows: int
    folded_rows: int
    score_delta: int
    event_rows: int
    remapped_rows: int
    alias_rows: int
    alias_conflicts: List[str]
    alias_rewrites: List[str]
    person_rows: int
    person_conflicts: List[str]
    # 随迁别名落地后补给主号的总分（走 `apply_delta`，改了目标域的行，路由据此判断要不要失效快照缓存）
    alias_compensated: int = 0


@dataclass(frozen=True)
class PresetAffectionRow:
    """某个账号在某个人设下的那一行好感"""

    preset_id: int
    preset_name: str
    preset_exists: bool
    score: int
    stage: str
    stage_label: str
    interaction_count: int
    last_interact_at: Optional[datetime]


@dataclass(frozen=True)
class AccountFacts:
    """合并预览里"一侧"的全部事实"""

    platform_userid: str
    nickname: str
    # 该账号在本域内已经是别名时，指向的人；否则为空串
    alias_person_uid: str
    # 有多少个账号已经指向本账号（= 本账号已经是几个号的主账号）
    incoming_alias_count: int
    best_score: int
    affection_rows: int
    event_rows: int
    interaction_total: int
    last_interact_at: Optional[datetime]
    presets: List[PresetAffectionRow] = field(default_factory=list)


@dataclass(frozen=True)
class MergeTransferPreview:
    """合并会在某个人设下补给主账号多少分

    「好感取高的」由 `services/affection/merge.py` 用一笔 `source=merge` 的补偿事件实现，
    所以这里能逐人设算出**将采用哪个值**：`score_after = max(两边)`。
    """

    preset_id: int
    preset_name: str
    preset_exists: bool
    alias_score: int
    person_score: int
    delta: int
    score_after: int


@dataclass(frozen=True)
class MergePreview:
    """合并预览：两边各自是什么、合完之后每个人设采用哪个值"""

    domain_id: str
    domain_label: str
    alias: AccountFacts
    person: AccountFacts
    # 逐人设的补偿明细；只列副账号**有行**的那些人设（主账号在别的人设下的分数与本次无关）
    transfers: List[MergeTransferPreview]
    total_delta: int
    # 本域的记忆条数。记忆不按账号分行，因此这是**域级**数字，不是"某个账号的记忆"
    domain_memory_paragraphs: int
    memory_scope_note: str
    reversible_note: str
    warnings: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)

    @property
    def can_merge(self) -> bool:
        return not self.blockers


@dataclass(frozen=True)
class AliasInfo:
    """一条已生效的合并（别名）"""

    id: int
    domain_id: str
    platform_userid: str
    person_uid: str
    source: str
    operator: str
    note: str
    create_time: Optional[datetime]


@dataclass(frozen=True)
class PresetSwitchImpact:
    """换人设的影响面

    `affected_people` 是**当前**人设下本域里有好感记录的人数——换过去之后这些人
    在这个频道里看起来会"当场归零"（实为切到另一份）。
    `returning_people` 是**目标**人设下已经有记录的人数——那些是"换回来就回来"的证据。
    """

    scope_label: str
    domain_id: str
    current_preset_id: int
    current_preset_name: str
    target_preset_id: int
    target_preset_name: str
    is_noop: bool
    affected_people: int
    returning_people: int
    affected_channels: int
    domain_memory_paragraphs: int
    memory_scope_note: str


@dataclass(frozen=True)
class OrphanPresetBucket:
    """挂在一个**已删除**人设下的孤儿数据"""

    preset_id: int
    domain_id: str
    domain_label: str
    adapter_key: str
    instance_key: str
    domain_group_id: int
    affection_rows: int
    event_rows: int
    people: int
    last_update: Optional[datetime]


def clean_name(value: str) -> str:
    """组名/显示名的落库清洗"""
    return sanitize_inline_text(value, DOMAIN_NAME_MAX_LENGTH)


def clean_note(value: str) -> str:
    """备注的落库清洗"""
    return sanitize_inline_text(value, NOTE_MAX_LENGTH)


def domain_label(domain: ScopeDomain, group_name: str = "") -> str:
    """域的人类可读标签

    好感度详情页要标"这份好感属于：域 X / 人设 甲"，这里给出那个 X。
    自成一域时用实例标识本身——那正是它的身份，不是"没有域"。
    """
    if domain.is_group:
        return f"{group_name}（组 #{domain.group_id}）" if group_name else f"实例组 #{domain.group_id}"
    if domain.instance_key:
        return f"{domain.instance_key}（自成一域）"
    return f"{domain.adapter_key}（单实例适配器，自成一域）"


async def group_names(group_ids: Sequence[int]) -> Dict[int, str]:
    """批量取组名，取不到的组不在返回值里（组被删了但绑定还在时是正常形态）"""
    wanted = sorted({group_id for group_id in group_ids if group_id > SELF_DOMAIN_GROUP_ID})
    if not wanted:
        return {}
    rows = await DBInstanceGroup.filter(id__in=wanted).values_list("id", "name")
    return {int(group_id): str(name) for group_id, name in rows}


async def preset_names(preset_ids: Sequence[int]) -> Dict[int, str]:
    """批量取人设名

    返回值里**只有真实存在的人设**。查不到的那些正是"孤儿"——调用方据此判断
    `preset_exists`，不要在这里替它编一个名字。
    """
    wanted = sorted({preset_id for preset_id in preset_ids if preset_id > PRESET_NONE})
    if not wanted:
        return {}
    rows = await DBPreset.filter(id__in=wanted).values_list("id", "title", "name")
    return {int(preset_id): str(title or name) for preset_id, title, name in rows}


def preset_display_name(preset_id: int, names: Dict[int, str]) -> str:
    """人设的展示名；`0` 是"无人设档"，查不到的是孤儿"""
    if preset_id <= PRESET_NONE:
        return PRESET_NONE_LABEL
    return names.get(preset_id, f"已删除的人设 #{preset_id}")


# ---------------------------------------------------------------------------
# 域（实例组）
# ---------------------------------------------------------------------------


async def list_domains(tenant_scope: Optional[int]) -> List[DomainSummary]:
    """列出该作用域可见的域

    Args:
        tenant_scope: `None` 表示平台方（看全部）；否则只看该租户名下的组。

    组的归属就是 `DBInstanceGroup.tenant_id`，与成员实例的归属由
    `assert_instance_joinable` 保证一致，因此按组过滤一次就够，不必再按成员反查。
    """
    query = DBInstanceGroup.all() if tenant_scope is None else DBInstanceGroup.filter(tenant_id=tenant_scope)
    groups = await query.order_by("id")
    if not groups:
        return []

    members_by_group: Dict[int, List[Tuple[str, str]]] = {}
    for group in groups:
        members_by_group[group.id] = await DBInstanceDomainBinding.members_of_memory_group(group.id)

    wanted_keys = sorted({instance_key for members in members_by_group.values() for _adapter, instance_key in members})
    display_names: Dict[Tuple[str, str], str] = {}
    if wanted_keys:
        rows = await DBAdapterInstance.filter(instance_key__in=wanted_keys).values_list(
            "adapter_key",
            "instance_key",
            "display_name",
        )
        display_names = {(str(adapter), str(instance)): str(name) for adapter, instance, name in rows}

    return [
        DomainSummary(
            group_id=group.id,
            name=group.name,
            note=group.note,
            tenant_id=group.tenant_id,
            members=[
                DomainMember(
                    adapter_key=adapter_key,
                    instance_key=instance_key,
                    display_name=display_names.get((adapter_key, instance_key), ""),
                )
                for adapter_key, instance_key in members_by_group[group.id]
            ],
            create_time=group.create_time,
            update_time=group.update_time,
        )
        for group in groups
    ]


async def list_instance_domains(tenant_scope: Optional[int]) -> List[InstanceDomainInfo]:
    """列出该作用域可见的实例、以及它们当前各自在哪个域

    没有绑定行的实例回 `group_id=None`（自成一域）。**不给它们建行**——
    "没有行"就是零配置的标准形态，见 `models/db_instance_group.py` 的模块文档。
    """
    query = DBAdapterInstance.all() if tenant_scope is None else DBAdapterInstance.filter(tenant_id=tenant_scope)
    instances = await query.order_by("id")
    if not instances:
        return []

    bindings = {
        (str(row.adapter_key), str(row.instance_key)): row
        for row in await DBInstanceDomainBinding.filter(
            instance_key__in=[instance.instance_key for instance in instances],
        )
    }
    names = await group_names([row.memory_group_id or 0 for row in bindings.values()])

    items: List[InstanceDomainInfo] = []
    for instance in instances:
        binding = bindings.get((instance.adapter_key, instance.instance_key))
        group_id = binding.memory_group_id if binding is not None else None
        group_id = group_id if group_id and group_id > SELF_DOMAIN_GROUP_ID else None
        domain = (
            ScopeDomain.of_group(group_id)
            if group_id is not None
            else ScopeDomain.of_instance(instance.adapter_key, instance.instance_key)
        )
        items.append(
            InstanceDomainInfo(
                adapter_key=instance.adapter_key,
                instance_key=instance.instance_key,
                display_name=instance.display_name,
                tenant_id=instance.tenant_id,
                group_id=group_id,
                group_name=names.get(group_id, "") if group_id is not None else "",
                domain_id=domain.domain_id,
            ),
        )
    return items


async def release_group_members(group_id: int, *, reason: str) -> int:
    """删组之前把成员踢回自成一域，返回受影响的实例数

    `resolve_memory_domain` **刻意**不查 `instance_group`：绑定行指向一个已被删除的组时
    仍然按那个组 ID 成立，否则整组的好感与记忆会在删组的那一刻"消失"。
    代价是删组动作必须自己把绑定行清干净——不清就留下一批指向不存在的组的实例，
    它们互相之间仍然互通，而后台再也看不到这个域。
    """
    members = {
        *await DBInstanceDomainBinding.members_of_memory_group(group_id),
        *await DBInstanceDomainBinding.members_of_affection_group(group_id),
    }
    if not members:
        return 0
    released = await DBInstanceDomainBinding.release_instances(
        [instance_key for _adapter_key, instance_key in members],
        reason=reason,
    )
    logger.info(f"删除实例组前已释放成员绑定: group_id={group_id}, released={released}")
    return released


# ---------------------------------------------------------------------------
# 实例入域并入存量好感
# ---------------------------------------------------------------------------
#
# 「加入域」改变的是好感键的域维：自成一域的行（`domain_group_id=0` + 本行
# `(adapter_key, instance_key)`）与组域的行（`domain_group_id=组` + 两列空串）是
# 两组完全不同的键，只写绑定行等于让旧数据在运行时当场归零（`db_affection.py`
# 的「已知代价」注释点名的就是这个坑）。
#
# 因此这里提供「并入」选项：**只搬该实例自成一域时期的存量**。组域里的行是
# 全体成员的共享历史，随实例搬走会把别家成员的数据带走——所以
# 「组 → 组」「组 → 自成一域」两个方向都没有并入可言。
#
# 折叠规则与账号合并（`services/affection/merge.py`）的「取高的」同源但不相同：
# 账号合并是**别名指向、一个数据行都不动**（可撤销）；入域并入是**物理改键**，
# 同（人, 人设）撞上时只能留一行——两边是同一个人对同一个人设的两段历史，
# 分数取高（差额记一笔 `domain_merge` 补偿事件，可审计），叙事取分数更高一侧的，
# 互动次数相加、最近互动取晚。这不是可逆操作：退出域不会把行搬回来，
# 因此接口层把「并入」做成显式选项而不是默认行为。

# 入域确认对话框里原样显示的那句话，与 MERGE_REVERSIBLE_NOTE 同一取舍：
# 这是给运营看的，措辞里不留源码路径与标记语言。
DOMAIN_JOIN_MIGRATION_NOTE = (
    "并入的是该实例「自成一域」时期留下的好感与流水：与目标域已有记录重合的按「取高」折叠"
    "（差额记一笔入域补偿事件，可审计），账号合并关系与人档案一并随迁；目标域已有的同名账号指向不被改写。"
    "目标域里已被合并到别人名下的账号，其好感会按目标域的合并关系归到最终的那个人名下。"
    "离开域时不做反向并入——组域里的数据是全体成员的共享历史，随实例搬走会把别家成员的数据带走。"
)

# 源别名迁不过去的原因。文案要能直接念给运营听，所以写账号间的关系，不写实现术语。
_CONFLICT_SAME_ACCOUNT = "目标域里该账号已有合并指向"
_CONFLICT_POINTED_AT = "目标域里已有账号合并到它名下，迁入会与目标域既有的合并关系接成链"
_CONFLICT_SELF_LOOP = "按目标域的合并关系跟到终点后会指回它自己"
_CONFLICT_TARGET_CHAIN = "它要指向的人在目标域里处于一条已有的合并链中间"


@dataclass(frozen=True)
class _AliasMovePlan:
    """源域别名迁入目标域的方案：预览与执行吃同一份

    保证（`TestMovedAliasesNeverFormChains` 钉住）：按 `moves` 落地之后，目标域里不存在
    「指向的人自己又是别名」的行——**由本方案新增或改写的行**不会造出链，目标域既有的行一行不动。
    """

    # (源别名行, 迁入后指向的人)；指向与行内原值不同的就是被改写的
    moves: List[Tuple[DBIdentityAlias, str]]
    # (副账号, 原指向, 改写后指向)
    rewrites: List[Tuple[str, str, str]]
    # (副账号, 原因)，按源别名行 ID 顺序
    conflicts: List[Tuple[str, str]]

    @property
    def conflict_accounts(self) -> List[str]:
        return [account for account, _reason in self.conflicts]

    @property
    def rewrite_labels(self) -> List[str]:
        return [f"{account}（{old} → {new}）" for account, old, new in self.rewrites]


def _plan_alias_moves(
    source_aliases: Sequence[DBIdentityAlias],
    target_alias_map: Dict[str, str],
) -> _AliasMovePlan:
    """决定每一条源别名是迁、改写后迁、还是留下

    **取舍（实例入域时并入存量好感）**：

    - 源别名 `A→P` 撞上目标域 `P→Q`：**改写为 `A→Q`**，不留下。理由：A 是 P 的旧号（源域运营认定），
      P 是 Q 的副号（目标域运营认定），二者说的是同一条换号链上的前后两环；留下 A 的话，
      A 在目标域里会变成一个独立的人，它自己名下那份在源域里休眠的旧好感随之复活，
      比跟到终点的改动更意外。改写在别名备注里留痕、在预览与执行响应里逐个点名。
    - 目标域已有别名**指向**迁入账号 A（`X→A`），或跟到终点后指回 A 自己：不改目标域既有的任何行，
      源别名留在原地并点名——那是目标域运营已经认定的事实，一次入域不能悄悄改写它。
    - 源别名与目标域已有行是同一个账号：沿用既有口径，以目标域为准。

    `running` 随迁移推进，因此同一批里先迁的行也参与后面的判断，不依赖源别名的先后顺序。
    解析只跟一跳（`identity_scope.resolve_person_uids`），这里同样只跟一跳。
    """
    running = dict(target_alias_map)
    pointed_at = set(running.values())
    moves: List[Tuple[DBIdentityAlias, str]] = []
    rewrites: List[Tuple[str, str, str]] = []
    conflicts: List[Tuple[str, str]] = []

    for alias in source_aliases:
        account = alias.platform_userid
        if account in running:
            conflicts.append((account, _CONFLICT_SAME_ACCOUNT))
            continue
        final = running.get(alias.person_uid, alias.person_uid)
        if final == account:
            conflicts.append((account, _CONFLICT_SELF_LOOP))
            continue
        if account in pointed_at:
            conflicts.append((account, _CONFLICT_POINTED_AT))
            continue
        if final in running:
            # 目标域自己已经有链（脏数据）：跟一跳落不到终点，再迁一条只会让链更长
            conflicts.append((account, _CONFLICT_TARGET_CHAIN))
            continue
        running[account] = final
        pointed_at.add(final)
        moves.append((alias, final))
        if final != alias.person_uid:
            rewrites.append((account, alias.person_uid, final))
    return _AliasMovePlan(moves=moves, rewrites=rewrites, conflicts=conflicts)


# 折叠时 `twin.save()` 允许写的列：叙事与计数。**不含 score / stage**（理由见 `merge_self_domain_into_group`）
_FOLD_NARRATIVE_FIELDS = [
    "state_text",
    "state_generated_at",
    "last_event_text",
    "interaction_count",
    "last_interact_at",
    "nickname",
    "last_chat_key",
]


@dataclass(frozen=True)
class _RowPlan:
    """一行源好感归一之后的去处"""

    row: DBAffection
    # 归一后的人：运行时在目标域里读这一份用的 uid
    target_uid: str
    # 归一后的 (人, 人设) 已被目标域的行、或排在前面的另一条源行占着 → 折叠进去
    folds: bool

    @property
    def remapped(self) -> bool:
        return self.target_uid != self.row.platform_userid


def _normalised_uid(uid: str, secondary_accounts: Set[str], target_alias_map: Dict[str, str]) -> str:
    """源域里一个 uid 搬进目标域后读侧用的「人」

    源域里本来就是副号的账号不归一（理由见 `_plan_rows`）；其余按目标域别名只跟一跳，
    与 `identity_scope.resolve_person_uids` 在运行时的解析一致。
    """
    if uid in secondary_accounts:
        return uid
    return target_alias_map.get(uid, uid)


def _plan_rows(
    source_rows: Sequence[DBAffection],
    *,
    target_pairs: Set[Tuple[str, int]],
    secondary_accounts: Set[str],
    target_alias_map: Dict[str, str],
) -> List[_RowPlan]:
    """把源行的 uid 按目标域别名映射到最终的人，并判定谁折叠、谁直接搬

    **只归一源域里的「人」，不归一源域里本来就是副号的账号**：副号（`A→P`）名下那一行在源域
    读不到（休眠，撤销合并时原样回来），搬过去也该保持休眠——把它折进主号是一次不可撤销的
    物理合并，违背「删别名即撤销」。

    `source_rows` 须已按 ID 排序：同一个 (人, 人设) 上有多条源行归一过来时，
    排在前面的占位、后面的折叠进去（取高对顺序不敏感，叙事字段按分数选，所以结果与顺序无关）。
    """
    seen = set(target_pairs)
    plans: List[_RowPlan] = []
    for row in source_rows:
        target_uid = _normalised_uid(row.platform_userid, secondary_accounts, target_alias_map)
        key = (target_uid, row.preset_id)
        plans.append(_RowPlan(row=row, target_uid=target_uid, folds=key in seen))
        seen.add(key)
    return plans


async def _alias_map_of(domain: ScopeDomain) -> Dict[str, str]:
    """域里全部别名：账号 → 人（脏数据下同一账号取最早那行，与解析器一致）"""
    result: Dict[str, str] = {}
    for row in await DBIdentityAlias.filter(domain_id=domain.domain_id).order_by("id"):
        result.setdefault(str(row.platform_userid), str(row.person_uid))
    return result


def _plan_persons(
    source_persons: Sequence[DBIdentityPerson],
    target_person_uids: Set[str],
) -> Tuple[List[DBIdentityPerson], List[str]]:
    """人档案的去向：目标域已有同 `person_uid` 的（或排在前面的源档案已占了位的）撞车，不随迁

    预览与执行**共用这一个判据**——预览数「随迁多少份」必须等于执行真搬的份数，
    不能一边数全部、一边在执行时静默跳过撞车的。
    `source_persons` 须已按 ID 排序；返回 `(随迁的档案, 撞车的 person_uid)`。
    """
    taken = set(target_person_uids)
    moves: List[DBIdentityPerson] = []
    conflicts: List[str] = []
    for person in source_persons:
        if person.person_uid in taken:
            conflicts.append(person.person_uid)
            continue
        taken.add(person.person_uid)
        moves.append(person)
    return moves, conflicts


async def _person_uids_of(domain: ScopeDomain) -> Set[str]:
    return {
        str(uid) for uid in await DBIdentityPerson.filter(domain_id=domain.domain_id).values_list("person_uid", flat=True)
    }


def _rewrite_note(existing: str, old: str, new: str) -> str:
    """别名被改写时在备注里留的痕"""
    trace = f"入域并入时由指向 {old} 改写为指向 {new}（目标域里 {old} 是 {new} 的副号）"
    return f"{existing}；{trace}" if existing else trace


async def preview_domain_join(
    *,
    adapter_key: str,
    instance_key: str,
    group: DBInstanceGroup,
) -> DomainJoinPreview:
    """组装"入域并入会影响什么"的那段预览

    源域**逐字**是该实例的自成一域键 `(adapter_key, instance_key, 0)`，与当前绑定无关：
    留在那儿的行就是这个实例自己的历史，绑没绑组都改变不了一个事实——它们不会再被读到。
    当前已在组域是 blocker 而不是静默空预览：那说明操作者大概率想搬的是组域里的共享数据，
    而那恰恰是不搬的那一类（理由见本节题注）。
    """
    source = ScopeDomain.of_instance(adapter_key, instance_key)
    target = ScopeDomain.of_group(group.id)

    blockers: List[str] = []
    current = await resolve_memory_domain(adapter_key, instance_key)
    if current.is_group:
        blockers.append(
            f"该实例当前在域「{current.domain_id}」中：存量好感属于组域的共享数据，"
            "不随实例搬走。如需并入本组，请先退回自成一域，再加入本组时勾选并入。",
        )

    source_rows = list(await DBAffection.filter(**source.as_columns()).order_by("id"))
    target_pairs = {
        (str(platform_userid), int(preset_id))
        for platform_userid, preset_id in await DBAffection.filter(**target.as_columns()).values_list(
            "platform_userid",
            "preset_id",
        )
    }

    source_aliases = list(await DBIdentityAlias.filter(domain_id=source.domain_id).order_by("id"))
    target_alias_map = await _alias_map_of(target)
    # 与执行用同一组函数、同一组输入：预览说的数就是执行要做的事（见 TestPreviewReflectsNormalisation）
    alias_plan = _plan_alias_moves(source_aliases, target_alias_map)
    row_plans = _plan_rows(
        source_rows,
        target_pairs=target_pairs,
        secondary_accounts={row.platform_userid for row in source_aliases},
        target_alias_map=target_alias_map,
    )
    fold_pairs = sum(1 for plan in row_plans if plan.folds)
    remapped_rows = sum(1 for plan in row_plans if plan.remapped)
    people = len({plan.target_uid for plan in row_plans})
    alias_conflicts = alias_plan.conflict_accounts

    source_persons = list(await DBIdentityPerson.filter(domain_id=source.domain_id).order_by("id"))
    person_moves, person_conflicts = _plan_persons(source_persons, await _person_uids_of(target))
    event_rows = await DBAffectionEvent.filter(**source.as_columns()).count()

    notes: List[str] = [DOMAIN_JOIN_MIGRATION_NOTE]
    if not source_rows and not source_aliases and not source_persons:
        notes.append("该实例在自成一域期间没有留下任何好感数据，并入与否结果相同。")
    else:
        if source_rows:
            notes.append(
                f"将并入 {people} 人的 {len(source_rows)} 条好感记录与 {event_rows} 条事件流水"
                + (f"；其中 {fold_pairs} 条与目标域已有记录重合，将按「取高」折叠。" if fold_pairs else "。"),
            )
            if remapped_rows:
                notes.append(
                    f"其中 {remapped_rows} 条属于目标域里已被合并到别人名下的账号，"
                    "将归到目标域认定的最终那个人名下（运行时读的是那一份）。",
                )
        if alias_plan.moves:
            notes.append(f"{len(alias_plan.moves)} 条账号合并关系随迁。")
            held_accounts = sorted(
                {uid for uid, _preset in target_pairs} & {alias.platform_userid for alias, _final in alias_plan.moves},
            )
            if held_accounts:
                notes.append(
                    f"其中 {len(held_accounts)} 个副号（{'；'.join(held_accounts)}）在目标域里本来就有自己的好感记录："
                    "别名落地后它们读到的是主号那一份，因此会把主号补到不低于它们原有的分数"
                    "（记账号合并补偿事件，撤销该合并即扣回）。",
                )
        if alias_plan.rewrites:
            notes.append(
                f"其中 {len(alias_plan.rewrites)} 条（{'；'.join(alias_plan.rewrite_labels)}）"
                "指向的账号在目标域里已是别人的副号，迁入时改写为指向最终的那个人（备注里留痕）。",
            )
        if person_moves:
            notes.append(f"{len(person_moves)} 份人档案随迁。")
        if person_conflicts:
            notes.append(
                f"{len(person_conflicts)} 份人档案（{'；'.join(person_conflicts)}）与目标域已有档案撞车，"
                "不随迁，以目标域为准。",
            )
        for account, reason in alias_plan.conflicts:
            notes.append(f"账号 {account} 的合并关系不随迁：{reason}，以目标域为准——请并入后在目标域里人工核对。")

    return DomainJoinPreview(
        adapter_key=adapter_key,
        instance_key=instance_key,
        source_domain_id=source.domain_id,
        target_domain_id=target.domain_id,
        target_domain_label=domain_label(target, group.name),
        affection_rows=len(source_rows),
        affection_people=people,
        event_rows=event_rows,
        fold_pairs=fold_pairs,
        remapped_rows=remapped_rows,
        alias_rows=len(alias_plan.moves),
        alias_conflicts=alias_conflicts,
        alias_rewrites=alias_plan.rewrite_labels,
        person_rows=len(person_moves),
        blockers=blockers,
        notes=notes,
    )


async def _compensate_held_alias_rows(
    target: ScopeDomain,
    moves: Sequence[Tuple[DBIdentityAlias, str]],
    held_scores: Dict[Tuple[str, int], int],
    target_rows: Dict[Tuple[str, int], DBAffection],
    *,
    operator: str,
) -> int:
    """随迁别名 `A→P` 落地后，把 P 在各人设下补到不低于 A 在目标域里本来就有的好感，返回补的总分

    只看 `held_scores`（别名落地**之前**目标域里就有的 A 的行，以及**折叠之前**的原分）：源域里 A 休眠的那一行
    当初已经按账号合并补偿过 P，这里不重算——否则 P 之后合法下跌的分数会被翻出来再补一遍。必须用快照而不是
    `target_rows` 里的当前分：源域那行 A 会折进目标域的 A 行并把它抬高（目标 A=50、源 A=95 → 95），读折叠后的分
    就等于把休眠的 95 又补了一遍。补偿走 `apply_delta`，记 `merge` 事件、
    来源账号是 A，与 `merge_accounts` 同一口径：撤销 A 时按流水扣回，落地多少扣多少。

    取舍（实例入域时并入存量好感）：不选「把这类别名列入冲突、留在源域」。那样每个在目标域
    有过发言的副号都要运营事后手工合并，且合并时走的恰好就是这段补偿逻辑；这里直接复用同一口径，
    结果等价于「运营入域后立刻合并了一次」，取高的语义不变（P ≥ A），不丢信息、也不多给。
    主号一侧读 `target_rows`：它已被行锁锁住且内存值与库一致（折叠与搬行都回写了），读它即主号当前分数。
    """
    total = 0
    for alias, final in moves:
        account = alias.platform_userid
        for preset_id in sorted(preset for uid, preset in held_scores if uid == account):
            alias_score = held_scores[(account, preset_id)]
            person_row = target_rows.get((final, preset_id))
            gap = alias_score - (person_row.score if person_row is not None else 0)
            if gap <= 0:
                continue
            snapshot = await apply_delta(
                AffectionKey.of_domain(target, person_uid=final, preset_id=preset_id),
                delta=gap,
                reason=f"入域并入随迁账号 {account} 的合并关系：补到不低于其在本域已有的好感（{alias_score}）",
                source=AffectionEventSource.MERGE,
                operator=operator,
                bump_interaction=False,
                update_event_text=False,
                source_platform_userid=account,
            )
            if person_row is not None:
                person_row.score = snapshot.score
                person_row.stage = snapshot.stage.value
            else:
                # 行是 apply_delta 现建的：内存里没有它的对象，用一个只带分数的占位让同一人名下的下一个副号读得到
                target_rows[(final, preset_id)] = DBAffection(
                    **target.as_columns(),
                    platform_userid=final,
                    preset_id=preset_id,
                    score=snapshot.score,
                    stage=snapshot.stage.value,
                )
            total += gap
    return total


# 一次锁行读取的账号数上限：IN 列表过长会撞各数据库的绑定变量上限
_LOCK_BATCH = 500


async def _lock_target_rows(
    target_cols: Dict[str, Any],
    uids: Set[str],
) -> Dict[Tuple[str, int], DBAffection]:
    """带行锁读出目标域里这些「人」名下的好感行，键是 `(人, 人设)`

    只锁会被折叠判定用到的行（候选 uid 名下），不锁整个组域——组域是全体成员共享的，
    整表锁会让组里所有实例的好感写入排队。加锁顺序固定为 `(人, 人设)` 升序（分批也按 uid 升序），
    两次并发并入对重叠的行总是同序加锁，不会互相死锁。SQLite 没有行锁，Tortoise 在该方言下忽略它。
    """
    rows: Dict[Tuple[str, int], DBAffection] = {}
    ordered = sorted(uids)
    for start in range(0, len(ordered), _LOCK_BATCH):
        chunk = ordered[start : start + _LOCK_BATCH]
        locked = (
            await DBAffection.filter(**target_cols, platform_userid__in=chunk)
            .order_by("platform_userid", "preset_id")
            .select_for_update()
        )
        for row in locked:
            rows[(row.platform_userid, row.preset_id)] = row
    return rows


# 同组并入互斥用的 Postgres 咨询锁的 classid；objid 取 group_id。数值任意，只要全库唯一用于这一件事
_DOMAIN_JOIN_LOCK_CLASS = 0x4E414A31


async def _serialise_joins_into_group(connection: Any, group_id: int) -> None:
    """在当前事务里取「并入这个组」的互斥锁，事务结束（提交或回滚）自动释放

    行锁只能锁已存在的行；目标域里还没有这个人时无行可锁：两个实例同时并入同一个新人，各自读到「没有」、
    各自把源行改键成目标键，后到的撞唯一约束；源行顺序相反的两个实例各占一个目标键再互相等对方，死锁。
    所以事务开头按 `group_id` 串行化同组并入——并入是低频的管理动作，
    排队的代价可以忽略；不同组互不影响。选咨询锁而不是锁组行：组行在测试里并不总是存在，且锁它会挡住
    同一事务里对组的其他写。SQLite 是单写者，写事务本来就串行，无需（也没有）这把锁。
    运行时写入（评估落分）不取这把锁，仍可能在锁读与搬行之间把目标行建出来——那一条由
    `merge_self_domain_into_group` 把唯一约束冲突翻成 409。
    """
    if connection.capabilities.dialect != "postgres":
        return
    await connection.execute_query("SELECT pg_advisory_xact_lock($1, $2)", [_DOMAIN_JOIN_LOCK_CLASS, int(group_id)])


def _is_write_conflict(error: BaseException) -> bool:
    """唯一约束冲突，或 Postgres 的死锁 / 序列化失败：都是「与并发写入撞车、整笔回滚后重试即可」"""
    if isinstance(error, IntegrityError):
        return True
    # asyncpg 的异常类不在模块顶层导入（SQLite 部署不需要它），按类名认
    return type(error).__name__ in {"DeadlockDetectedError", "SerializationError"}


async def merge_self_domain_into_group(
    *,
    adapter_key: str,
    instance_key: str,
    group: DBInstanceGroup,
    operator: str = "",
    defer_cache_invalidation: bool = False,
) -> DomainJoinMigration:
    """并入存量好感（见 `_merge_self_domain_into_group`），并把并发写入撞车翻成清晰的 409

    撞车的几种形态（唯一约束冲突、死锁）都发生在事务里，抛出时事务已整笔回滚——没有半成品。
    原样抛出会让路由变成 500；翻成 `ConcurrentWriteConflictError`，管理员看到的是「请稍后重试」。
    同组并入之间的撞车靠事务开头的互斥锁根本不发生，这里兜的是运行时写入这类不取锁的写入者（A5）。
    """
    try:
        return await _merge_self_domain_into_group(
            adapter_key=adapter_key,
            instance_key=instance_key,
            group=group,
            operator=operator,
            defer_cache_invalidation=defer_cache_invalidation,
        )
    except Exception as error:
        if not _is_write_conflict(error):
            raise
        logger.warning(
            f"实例入域并入与并发写入撞车，整笔已回滚: instance={adapter_key}/{instance_key}, "
            f"group={group.id}, error={error!r}",
        )
        raise ConcurrentWriteConflictError(operation="并入存量好感") from error


async def _merge_self_domain_into_group(
    *,
    adapter_key: str,
    instance_key: str,
    group: DBInstanceGroup,
    operator: str = "",
    defer_cache_invalidation: bool = False,
) -> DomainJoinMigration:
    """把实例「自成一域」时期的存量好感并入组域，返回实做结果

    `defer_cache_invalidation`：调用方自己开着外层事务时传 True，并在**外层事务提交之后**
    调 `invalidate_cache()`（路由 `set_instance_domain` 就是这样）。本函数里的失效点在内层
    `in_transaction` 退出之后，但外层没提交时那仍在提交之前：并发读者在失效与提交之间读到
    旧值又缓存起来，并入的好感就在 TTL 内读不到。独立调用（默认 False）
    没有外层事务，本函数的 `in_transaction` 退出即提交，失效在其后。

    由 `set_instance_domain` 在**绑定行写入的同一事务里**调用（本函数内部也用
    `in_transaction` 收一次口：调用方在事务里就并入同一事务，独立调用时自己保证原子——
    「绑了域但存量只并了一半」这个半成品状态必须整体回滚）。源域逐字是
    `(adapter_key, instance_key, 0)` 三列——调用前不必先解析当前绑定，**但要并入时
    必须是自成一域**（接口层已校验；这里若再判一次会让"组 → 组顺便带走旧存量"
    这条被明确拒绝的路径看起来像是被支持的）。

    撞上行（目标域已有同「人, 人设」）时折叠：目标行分数取高——差额走
    `store.apply_delta` 记一笔 `domain_merge` 补偿事件，好感的写入口因此仍是
    唯一的（建行 / 派生阶段 / 记流水 / 缓存失效都不在这里再造一份）。

    **身份按目标域别名归一**（#67 复核补上）：源行的 uid 是源域里的「人」，而运行时读侧在目标域里
    先把账号解析成人再读行。目标域已有 `P→Q` 时源域 P 名下的行必须落到 Q 上（与 Q 的行取高折叠，
    流水跟着改挂），否则搬过去读不到。源别名同理要按目标域归一，且**搬完之后目标域里绝不成链**
    （解析只跟一跳）：方案见 `_plan_alias_moves`，行的去向见 `_plan_rows`。
    预览（`preview_domain_join`）用同一组函数，数字与本函数的实做结果逐项对拍过。
    """
    source = ScopeDomain.of_instance(adapter_key, instance_key)
    target = ScopeDomain.of_group(group.id)
    target_cols = target.as_columns()

    moved = folded = score_delta = event_rows = remapped = 0
    alias_conflicts: List[str] = []
    person_conflicts: List[str] = []
    alias_rows = person_rows = 0

    async with in_transaction() as connection:
        # 同组并入串行：目标域里没有这个人时无行可锁，行锁挡不住并发新建目标行与死锁
        await _serialise_joins_into_group(connection, group.id)
        # 先把别名的去向定下来（读目标域**迁入前**的别名）：行的归一与别名的改写依据同一份
        # 目标域事实，之后才动任何数据。归一的语义见 `_plan_rows` / `_plan_alias_moves`。
        target_alias_map = await _alias_map_of(target)
        source_aliases = list(await DBIdentityAlias.filter(domain_id=source.domain_id).order_by("id"))
        alias_plan = _plan_alias_moves(source_aliases, target_alias_map)

        source_rows = list(await DBAffection.filter(**source.as_columns()).order_by("id"))
        secondary = {row.platform_userid for row in source_aliases}
        moved_accounts = {alias.platform_userid for alias, _final in alias_plan.moves}
        # 目标行带行锁读：同组其他实例此刻若正在写这些行（评估落分、另一个实例并入），
        # 无锁读到的是旧分，差额按旧分算、再叠加到别人刚提交的新分上，结果既不是「先并入再写」
        # 也不是「先写再并入」。锁到事务结束，读到的就是折叠时的真值。
        # 锁的范围：源行归一后的人，加上随迁副号自己与它们指向的人（别名落地后的补偿要读这几行，见下）。
        target_rows = await _lock_target_rows(
            target_cols,
            {_normalised_uid(row.platform_userid, secondary, target_alias_map) for row in source_rows}
            | moved_accounts
            | {final for _alias, final in alias_plan.moves},
        )
        # 随迁副号在目标域里**本来就有**的好感行（落地别名之前就在）：别名一落地，副号读到的就是主号那一行，
        # 这些行自己的分数随之休眠——主号分数更低时副号的好感会当场掉分，见别名落地后的补偿
        # **快照原分**：之后折叠会把源域那行副号折进这些行并抬高它们，补偿只能按折叠之前的分算
        held_scores = {key: row.score for key, row in target_rows.items() if key[0] in moved_accounts}
        row_plans = _plan_rows(
            source_rows,
            target_pairs=set(target_rows),
            secondary_accounts=secondary,
            target_alias_map=target_alias_map,
        )

        # 发生过折叠的 (目标人, 人设)：落在它们上面的账号合并补偿事件（不论来自源域还是目标域自己）
        # 都不再可撤销，见事件搬完之后的说明。
        collided_keys = {(plan.target_uid, plan.row.preset_id) for plan in row_plans if plan.folds}

        for plan in row_plans:
            row = plan.row
            if plan.remapped:
                remapped += 1
            twin = target_rows.get((plan.target_uid, row.preset_id))
            if twin is None:
                row.adapter_key = ""
                row.instance_key = ""
                row.domain_group_id = group.id
                row.platform_userid = plan.target_uid
                await row.save()
                # 后面归一到同一个 (人, 人设) 的源行要能找到它，折叠进来
                target_rows[(plan.target_uid, row.preset_id)] = row
                moved += 1
                continue

            # 折叠：两段历史都是真实发生过的。分数取高（差额记补偿事件）；
            # 叙事字段取分数更高一侧——分数与状态描述必须来自同一段关系，
            # 各取一半会把"冷淡的 80 分"这种拼合物写进提示词。
            gap = row.score - twin.score
            if gap > 0:
                twin.state_text = row.state_text
                twin.state_generated_at = row.state_generated_at
                twin.last_event_text = row.last_event_text
            twin.interaction_count += row.interaction_count
            if row.last_interact_at is not None and (
                twin.last_interact_at is None or row.last_interact_at > twin.last_interact_at
            ):
                twin.last_interact_at = row.last_interact_at
                if row.nickname:
                    twin.nickname = row.nickname
                if row.last_chat_key:
                    twin.last_chat_key = row.last_chat_key
            # **只写叙事与计数字段，绝不写 score / stage**：分数的唯一写入口是 `apply_delta`
            # （它在库里行锁内读改写），而 `twin` 是循环外读出来的内存对象——前一条源行折叠时
            # `apply_delta` 已把库里的分数涨上去，这里若整行 save，下一条折叠到同一 twin 的源行
            # 会把旧分数写回，分数被打回去而叙事已经换成了低分一侧。
            await twin.save(update_fields=_FOLD_NARRATIVE_FIELDS)

            if gap > 0:
                key = AffectionKey.of_domain(target, person_uid=plan.target_uid, preset_id=row.preset_id)
                snapshot = await apply_delta(
                    key,
                    delta=gap,
                    reason=f"实例入域并入存量好感：与自成一域时期的记录取高（{row.score}）",
                    source=AffectionEventSource.DOMAIN_MERGE,
                    operator=operator,
                    # 与账号合并的补偿事件同一组理由：不是一次互动、也不是这段关系里
                    # 刚发生的事（写进 last_event_text 会被下一轮提示词念给用户听）。
                    bump_interaction=False,
                    update_event_text=False,
                )
                # 用 apply_delta 的结果回填内存对象：后面折叠进同一 twin 的源行要拿**当前**分数算差额，
                # 并据此决定叙事跟谁（取高与顺序无关，靠的就是这一步）。
                twin.score = snapshot.score
                twin.stage = snapshot.stage.value
                score_delta += gap
            await row.delete()
            folded += 1

        # 事件流水是审计不是唯一键，整批搬域（流水没有可撞的行）。归一过的人（P→Q）名下的流水
        # 跟着改挂到 Q，否则时间线上 Q 的好感从哪来的对不上；其余原样换域。
        secondary_accounts = secondary
        event_uids = {
            str(uid)
            for uid in await DBAffectionEvent.filter(**source.as_columns()).distinct().values_list(
                "platform_userid",
                flat=True,
            )
        }
        for uid in sorted(event_uids):
            event_target_uid = _normalised_uid(uid, secondary_accounts, target_alias_map)
            if event_target_uid != uid:
                # 空串 = 「触发账号与 platform_userid 相同」。改挂到 Q 之后空串会变成「是 Q 自己干的」，
                # 原来是 P 的来源信息就丢了：先把空串落成原 uid（P 现在是 Q 的副号，正是
                # 「副号干的」该有的样子）。只动空串的事件——补偿事件的来源账号本来就非空，
                # 撤销求和又只认补偿来源，这一步与下面补偿事件的撤销语义互不相干。
                await DBAffectionEvent.filter(
                    **source.as_columns(),
                    platform_userid=uid,
                    source_platform_userid="",
                ).update(source_platform_userid=uid)
                event_rows += int(
                    await DBAffectionEvent.filter(**source.as_columns(), platform_userid=uid).update(
                        **target_cols,
                        platform_userid=event_target_uid,
                    )
                    or 0,
                )
        event_rows += int(await DBAffectionEvent.filter(**source.as_columns()).update(**target_cols) or 0)

        # 账号合并补偿事件的可撤销性：`A→P` 的 `merge` 补偿
        # 记在 P 的行上（`source_platform_userid=A`），`unmerge_account` 按这个标记求和后从人那一行扣回。
        # 一个 (人, 人设) 发生折叠之后，分数是「取高」的结果，行上任何一笔补偿是否还在分数里都无法逐笔对账：
        # 源域补偿被更高的目标分吃掉（Q=90、源 P=20+50 → 撤销 A 把 Q 扣成 40），目标域自己的补偿被更高的源分
        # 吃掉（目标 P=30+30 借 A、源 P=80 → 撤销后 50），或者两边都没吃但分数已与补偿脱钩。
        # 所以统一做法：事件搬完之后，落在发生过折叠的 (目标人, 人设) 上的补偿事件——源域搬来的与目标域
        # 本来就有的一并——整体改记为 `domain_merge`。条数、增量、来源账号都原样保留（审计不丢），只是不再属于
        # 撤销求和的来源集合。代价：这部分借分并入后不可单独撤销（宁可少扣，不能把别人挣的分扣掉）。
        # 没折叠的 (人, 人设)（整行搬过去、分数原样落地）补偿保持可撤销。
        for target_uid, preset_id in sorted(collided_keys):
            await DBAffectionEvent.filter(
                **target_cols,
                platform_userid=target_uid,
                preset_id=preset_id,
                source__in=COMPENSATION_SOURCES,
            ).update(source=AffectionEventSource.DOMAIN_MERGE.value)

        # 别名按方案落地：改写的指向最终的人并在备注里留痕，迁不过去的留在源域并点名
        # （目标域既有的事实不该被一次入域动作悄悄改写）。
        for alias, final in alias_plan.moves:
            if final != alias.person_uid:
                alias.note = _rewrite_note(alias.note, alias.person_uid, final)
                alias.person_uid = final
            alias.domain_id = target.domain_id
            await alias.save()
            alias_rows += 1
        alias_conflicts = alias_plan.conflict_accounts

        # 随迁的 `A→P`：目标域里 A 本来有活跃的好感行时，别名落地后 A 读到的是 P 的行。P 低于 A 就是
        # 一次悄悄的降分（目标 A=90、源 P 补到 70，并入后 A 读到 70）——与账号合并同一个口径：
        # 把 P 补到不低于 A，补偿记 `merge` 事件、来源账号是 A，撤销 A 即扣回（落地的补偿逐笔可对账）。
        # 放在折叠与「折叠后补偿改记」之后：P 的分数此时已定，这一笔是全新的、整笔落了地的补偿。
        alias_compensated = await _compensate_held_alias_rows(
            target,
            alias_plan.moves,
            held_scores,
            target_rows,
            operator=operator,
        )

        # 与预览同一个判据（`_plan_persons`）
        source_persons = list(await DBIdentityPerson.filter(domain_id=source.domain_id).order_by("id"))
        person_moves, person_conflicts = _plan_persons(source_persons, await _person_uids_of(target))
        for person in person_moves:
            person.domain_id = target.domain_id
            await person.save()
            person_rows += 1

    # 折叠改的是目标域的行但走的不是 apply_delta 那一路（gap <= 0 时连事件都没有），
    # 快照缓存里可能还躺着折叠前的旧值。搬进来的行也要失效：组域的其他成员实例早就在读这个域，
    # 目标键上缓存着的「从未打过交道」空快照（`load_snapshots` 会缓存空值）会让并入的好感在 TTL 内读不到——
    # 归一改键搬来的行（P→Q）撞上的正是 Q 的键。
    if (moved or folded or alias_compensated) and not defer_cache_invalidation:
        invalidate_cache()

    if moved or folded or event_rows or alias_rows:
        logger.info(
            f"实例入域并入存量好感: {source.domain_id} -> {target.domain_id}, "
            f"搬 {moved} 行 / 折叠 {folded} 行（补 {score_delta} 分，其中按目标域别名归一 {remapped} 行）/ "
            f"随迁副号在目标域已有好感，补偿主号 {alias_compensated} 分 / "
            f"流水 {event_rows} 行 / 别名 {alias_rows} 条（改写 {len(alias_plan.rewrites)} 条）/ "
            f"档案 {person_rows} 份, operator={operator or '-'}",
        )
    if alias_conflicts or person_conflicts:
        logger.warning(
            f"实例入域并入时目标域已有同名指向或会接成链，以目标域为准未随迁: {target.domain_id}, "
            f"aliases={alias_plan.conflicts}, persons={person_conflicts}, operator={operator or '-'}",
        )

    return DomainJoinMigration(
        affection_rows=moved,
        folded_rows=folded,
        score_delta=score_delta,
        event_rows=int(event_rows or 0),
        remapped_rows=remapped,
        alias_rows=alias_rows,
        alias_conflicts=alias_conflicts,
        alias_rewrites=alias_plan.rewrite_labels,
        person_rows=person_rows,
        person_conflicts=person_conflicts,
        alias_compensated=alias_compensated,
    )


# ---------------------------------------------------------------------------
# 账号合并
# ---------------------------------------------------------------------------


async def _account_facts(domain: ScopeDomain, platform_userid: str) -> AccountFacts:
    """把一个账号在某个域里的全部可见事实汇总出来"""
    columns = domain.as_columns()
    rows = await DBAffection.filter(**columns, platform_userid=platform_userid).order_by("-score", "id")
    names = await preset_names([row.preset_id for row in rows])

    presets: List[PresetAffectionRow] = []
    for row in rows[:MAX_PRESET_BREAKDOWN]:
        stage = stage_of(row.score)
        presets.append(
            PresetAffectionRow(
                preset_id=row.preset_id,
                preset_name=preset_display_name(row.preset_id, names),
                preset_exists=row.preset_id <= PRESET_NONE or row.preset_id in names,
                score=row.score,
                stage=stage.value,
                stage_label=stage_label(stage),
                interaction_count=row.interaction_count,
                last_interact_at=row.last_interact_at,
            ),
        )

    alias_row = await DBIdentityAlias.filter(
        domain_id=domain.domain_id,
        platform_userid=platform_userid,
    ).order_by("id").first()

    nickname = ""
    last_interact_at: Optional[datetime] = None
    for row in rows:
        if not nickname and row.nickname:
            nickname = row.nickname
        if row.last_interact_at is not None and (last_interact_at is None or row.last_interact_at > last_interact_at):
            last_interact_at = row.last_interact_at

    return AccountFacts(
        platform_userid=platform_userid,
        nickname=nickname,
        alias_person_uid=alias_row.person_uid if alias_row is not None else "",
        incoming_alias_count=await DBIdentityAlias.filter(
            domain_id=domain.domain_id,
            person_uid=platform_userid,
        ).count(),
        best_score=max((row.score for row in rows), default=0),
        affection_rows=len(rows),
        event_rows=await DBAffectionEvent.filter(**columns, platform_userid=platform_userid).count(),
        interaction_total=sum(row.interaction_count for row in rows),
        last_interact_at=last_interact_at,
        presets=presets,
    )


async def domain_memory_paragraph_count(domain: ScopeDomain, adapter_key: str, instance_key: str) -> int:
    """本域内的记忆条数（**域级**，不是某个账号的）

    过滤集合取自 `resolve_domain_instance_keys`——那是记忆检索侧用的同一份判据，
    在这里再拼一份就是第二处实现。返回 `None` 表示"不施加实例过滤"，
    此时数的是全部记忆（未启用实例隔离的部署本来就是这个语义）。
    """
    allowed = await resolve_domain_instance_keys(adapter_key, instance_key)
    query = DBMemParagraph.filter(is_inactive=False)
    if allowed is not None:
        query = query.filter(instance_key__in=allowed)
    return await query.count()


MEMORY_SCOPE_NOTE = (
    "记忆按（域, 人, 人设）隔离，但 mem_* 表上没有账号列与人设列，"
    "因此这里给出的是「本域」的记忆条数，不是某一个账号或某一个人设名下的条数。"
)

# 换人设二次确认里"旧数据保留、换回来就回来"那句话。
# 频道那一处与实例默认那一处**共用这一份**：同一件事在两个弹窗里说成两种措辞，
# 运营会以为是两种不同的后果。
PRESET_SWITCH_REVERSIBLE_NOTE = (
    "旧数据不会被删除：好感与记忆按（域, 人, 人设）分开存放，换人设只是切到另一份。"
    "把人设换回去，原来那份原样回来。"
)


# 合并预览面板上原样显示的那句话。**这是给运营看的，不是给读代码的人看的**：
# MUI 的 Alert 不渲染 markdown，反引号与源码路径在界面上就是字面的反引号与一串
# 谁也点不开的文件名——而这一页租户管理员也看得到。措辞里因此不留任何标记语言与实现路径。
MERGE_REVERSIBLE_NOTE = (
    "撤销 = 删掉那一行别名，并把合并时补进去的那一份好感原样扣回。"
    "副账号自己那一行好感与全部流水一个字节都不动，撤销后原样回来。"
)


async def preview_merge(
    domain: ScopeDomain,
    *,
    adapter_key: str,
    instance_key: str,
    alias_userid: str,
    person_userid: str,
) -> MergePreview:
    """合并预览：两边各自的好感与流水、以及**每个人设将采用哪个值**

    Args:
        domain: 合并发生的域。别名按域隔离，跨域套用就是串域。
        adapter_key: 解析该域时用的适配器标识（记忆条数要用）
        instance_key: 同上
        alias_userid: 副账号，合并后指向主账号
        person_userid: 主账号，`person_uid` 就是它

    Returns:
        `MergePreview`。`blockers` 非空时前端置灰执行按钮——但那只是提示，
        真正的判据在 `services/affection/merge.py:merge_accounts` 里，它会再拒一次。

    **执行不在本模块。** 合并的权威实现（别名 + 逐人设补偿事件 + 别名不成链的改写）
    是 `services/affection/merge.py`；这里只负责"点之前先看清楚"。
    本函数因此复刻它的两条判据（拒绝条件、取高的差额）——复刻是有代价的，
    所以 `tests/test_identity_console_api.py` 有一组用例**拿预览与真实合并结果对拍**，
    两边一旦分叉就红。
    """
    alias_facts = await _account_facts(domain, alias_userid)
    person_facts = await _account_facts(domain, person_userid)

    warnings: List[str] = []
    blockers: List[str] = []

    if not alias_userid or not person_userid:
        blockers.append("两侧账号都必须填写。")
    elif alias_userid == person_userid:
        blockers.append("两侧是同一个账号，无法合并。")
    if person_facts.alias_person_uid:
        # `merge_accounts` 对这种情况直接抛：替操作者猜一个终点，猜错的代价是
        # 两个不相干的人被并在一起。因此这里也是 blocker 而不是 warning。
        blockers.append(
            f"主账号 {person_userid} 自己已经是 {person_facts.alias_person_uid} 的副号，"
            "请先撤销那条合并，或直接合并到最终的那个人。",
        )
    if alias_facts.alias_person_uid and alias_facts.alias_person_uid != person_userid:
        blockers.append(
            f"账号 {alias_userid} 已经被合并到 {alias_facts.alias_person_uid} 名下，"
            "请先撤销那条合并再重新合并。",
        )
    if alias_facts.incoming_alias_count:
        warnings.append(
            f"已有 {alias_facts.incoming_alias_count} 个账号指向 {alias_userid}，"
            "执行时会把它们一并改指向主账号；撤销本次合并不会把它们改回去。",
        )

    person_scores = {row.preset_id: row.score for row in person_facts.presets}
    names = await preset_names([row.preset_id for row in (*alias_facts.presets, *person_facts.presets)])
    transfers: List[MergeTransferPreview] = []
    for row in sorted(alias_facts.presets, key=lambda item: item.preset_id):
        person_score = person_scores.get(row.preset_id, 0)
        # 与 `_apply_merge_compensation` 逐字同一条规则：只看副账号有行的人设，
        # 差额 <= 0 的一笔都不写（重复合并、副账号分数更低这两种情况因此零副作用）
        delta = max(0, row.score - person_score)
        transfers.append(
            MergeTransferPreview(
                preset_id=row.preset_id,
                preset_name=preset_display_name(row.preset_id, names),
                preset_exists=row.preset_id <= PRESET_NONE or row.preset_id in names,
                alias_score=row.score,
                person_score=person_score,
                delta=delta,
                score_after=person_score + delta,
            ),
        )

    return MergePreview(
        domain_id=domain.domain_id,
        domain_label=domain_label(domain, (await group_names([domain.group_id])).get(domain.group_id, "")),
        alias=alias_facts,
        person=person_facts,
        transfers=transfers,
        total_delta=sum(transfer.delta for transfer in transfers),
        domain_memory_paragraphs=await domain_memory_paragraph_count(domain, adapter_key, instance_key),
        memory_scope_note=MEMORY_SCOPE_NOTE,
        reversible_note=MERGE_REVERSIBLE_NOTE,
        warnings=warnings,
        blockers=blockers,
    )


class IdentityAdminRejected(ValueError):
    """后台侧的操作被拒（调用方应转成 400）"""


async def list_aliases(domain: ScopeDomain) -> List[AliasInfo]:
    """列出该域内已生效的合并"""
    rows = await DBIdentityAlias.filter(domain_id=domain.domain_id).order_by("id")
    return [
        AliasInfo(
            id=row.id,
            domain_id=row.domain_id,
            platform_userid=row.platform_userid,
            person_uid=row.person_uid,
            source=row.source,
            operator=row.operator,
            note=row.note,
            create_time=row.create_time,
        )
        for row in rows
    ]


async def alias_in_domain(domain: ScopeDomain, alias_id: int) -> Optional[AliasInfo]:
    """按 `(域, 行 ID)` **双条件**定位一行别名，域内没有则返回 `None`

    撤销合并的作用域判据落在这里。别名表是全局一张，只按 ID 定位等于让任何一个能进后台
    的人撤销别的域（别的租户）的合并——接口层的 `assert_instance_accessible` 挡的是
    "你够不着那个实例"，这一条挡的是"你拿着自己域的入场券去动别人域的行"。

    **撤销动作本身不在这里**：删别名 + 把补进去的那一份好感原样扣回，
    权威实现是 `services/affection/merge.py:unmerge_account`。本函数只回答"这一行是不是你的"。
    """
    row = await DBIdentityAlias.filter(domain_id=domain.domain_id, id=alias_id).first()
    if row is None:
        return None
    return AliasInfo(
        id=row.id,
        domain_id=row.domain_id,
        platform_userid=row.platform_userid,
        person_uid=row.person_uid,
        source=row.source,
        operator=row.operator,
        note=row.note,
        create_time=row.create_time,
    )


# ---------------------------------------------------------------------------
# 换人设的影响面
# ---------------------------------------------------------------------------


async def _people_under(domain: ScopeDomain, preset_id: int) -> int:
    """本域在某个人设下有好感记录的人数"""
    return await DBAffection.filter(**domain.as_columns(), preset_id=preset_id).count()


async def preview_preset_switch(
    *,
    adapter_key: str,
    instance_key: str,
    scope_label: str,
    current_preset_id: int,
    target_preset_id: int,
    affected_channels: int,
) -> PresetSwitchImpact:
    """组装"换人设会影响谁"的那段说明

    影响面按**好感域**算：好感与记忆共用一个域，而只有好感表上有 `preset_id` 这一列，
    数得出人数。记忆那一侧只能给到域级条数，理由见模块文档。
    """
    domain = await resolve_affection_domain(adapter_key, instance_key)
    names = await preset_names([current_preset_id, target_preset_id])
    return PresetSwitchImpact(
        scope_label=scope_label,
        domain_id=domain.domain_id,
        current_preset_id=current_preset_id,
        current_preset_name=preset_display_name(current_preset_id, names),
        target_preset_id=target_preset_id,
        target_preset_name=preset_display_name(target_preset_id, names),
        is_noop=current_preset_id == target_preset_id,
        affected_people=await _people_under(domain, current_preset_id),
        returning_people=await _people_under(domain, target_preset_id),
        affected_channels=affected_channels,
        domain_memory_paragraphs=await domain_memory_paragraph_count(domain, adapter_key, instance_key),
        memory_scope_note=MEMORY_SCOPE_NOTE,
    )


# ---------------------------------------------------------------------------
# 人设孤儿数据
# ---------------------------------------------------------------------------


async def _live_preset_ids(preset_ids: Sequence[int]) -> Set[int]:
    wanted = sorted({preset_id for preset_id in preset_ids if preset_id > PRESET_NONE})
    if not wanted:
        return set()
    return {int(preset_id) for preset_id in await DBPreset.filter(id__in=wanted).values_list("id", flat=True)}


async def list_orphan_buckets(instances: Optional[Sequence[Tuple[str, str]]]) -> List[OrphanPresetBucket]:
    """列出挂在**已删除人设**下的好感数据，按 (域, 人设) 分桶

    Args:
        instances: `None` 表示不施加实例过滤（平台方全局视图）；否则只看这些实例
            （`(adapter_key, instance_key)`）的行。成对过滤是因为另一个适配器下可以有
            归别的租户的同名实例，只按标识会把它的孤儿数据列进来、进而被这里清掉。
            注意好感行在属于实例组时 `instance_key` 是空串，域的身份落在 `domain_group_id`
            上——因此按实例过滤时必须**同时**放行"本作用域相关的组"的行，见下面的实现。

    Returns:
        每一桶一条。**不做级联删**：人设可能是误删，这些数据原地保留，
        由 :func:`cleanup_orphan_preset` 在管理员点过一次之后才动。
    """
    query = DBAffection.filter(preset_id__gt=PRESET_NONE)
    group_ids: Set[int] = set()
    if instances is not None:
        from tortoise.expressions import Q

        from nekro_agent.services.user.tenant_perm import instance_refs_filter

        wanted = instance_refs_filter(instances)
        # 作用域内的实例可能自成一域（行落在 instance_key 上），也可能在某个组里
        # （行落在 domain_group_id 上、instance_key 为空串）。两种形态都要收进来，
        # 只收前者会让"进组之后孤儿数据在后台整体消失"。
        for row in await DBInstanceDomainBinding.filter(wanted):
            for candidate in (row.memory_group_id, row.affection_group_id):
                if candidate and candidate > SELF_DOMAIN_GROUP_ID:
                    group_ids.add(int(candidate))

        condition = wanted
        if group_ids:
            condition = condition | Q(domain_group_id__in=sorted(group_ids))
        query = query.filter(condition)

    rows = await query.order_by("id")
    if not rows:
        return []

    live = await _live_preset_ids([row.preset_id for row in rows])
    orphan_rows = [row for row in rows if row.preset_id not in live]
    if not orphan_rows:
        return []

    buckets: Dict[Tuple[str, str, int, int], List[DBAffection]] = {}
    for row in orphan_rows:
        buckets.setdefault((row.adapter_key, row.instance_key, row.domain_group_id, row.preset_id), []).append(row)

    names = await group_names([group_id for _a, _i, group_id, _p in buckets])
    items: List[OrphanPresetBucket] = []
    for (adapter_key, instance_key, domain_group_id, preset_id), rows_in_bucket in sorted(buckets.items()):
        domain = (
            ScopeDomain.of_group(domain_group_id)
            if domain_group_id > SELF_DOMAIN_GROUP_ID
            else ScopeDomain.of_instance(adapter_key, instance_key)
        )
        last_update = max((row.update_time for row in rows_in_bucket if row.update_time is not None), default=None)
        items.append(
            OrphanPresetBucket(
                preset_id=preset_id,
                domain_id=domain.domain_id,
                domain_label=domain_label(domain, names.get(domain_group_id, "")),
                adapter_key=adapter_key,
                instance_key=instance_key,
                domain_group_id=domain_group_id,
                affection_rows=len(rows_in_bucket),
                event_rows=await DBAffectionEvent.filter(
                    adapter_key=adapter_key,
                    instance_key=instance_key,
                    domain_group_id=domain_group_id,
                    preset_id=preset_id,
                ).count(),
                people=len({row.platform_userid for row in rows_in_bucket}),
                last_update=last_update,
            ),
        )
    return items


async def cleanup_orphan_preset(bucket: OrphanPresetBucket) -> Dict[str, int]:
    """手动清理一桶孤儿数据，返回各表删掉的行数

    **只删指定的那一桶**，而且执行前再确认一次那个人设确实不存在——预览与执行之间
    人设可能被重新建出来（云端人设导入会带回同一个 ID），那时候删的就不是孤儿了。
    """
    if await DBPreset.filter(id=bucket.preset_id).exists():
        raise IdentityAdminRejected(f"人设 {bucket.preset_id} 现在是存在的，它名下的数据不是孤儿，拒绝清理")

    columns = {
        "adapter_key": bucket.adapter_key,
        "instance_key": bucket.instance_key,
        "domain_group_id": bucket.domain_group_id,
        "preset_id": bucket.preset_id,
    }
    async with in_transaction():
        events = await DBAffectionEvent.filter(**columns).delete()
        records = await DBAffection.filter(**columns).delete()
    logger.warning(
        f"孤儿好感数据已清理: domain={bucket.domain_id}, preset_id={bucket.preset_id}, "
        f"affection={records}, events={events}",
    )
    return {"affection": int(records or 0), "affection_event": int(events or 0)}


__all__ = [
    "DOMAIN_JOIN_MIGRATION_NOTE",
    "MEMORY_SCOPE_NOTE",
    "MERGE_REVERSIBLE_NOTE",
    "PRESET_NONE_LABEL",
    "PRESET_SWITCH_REVERSIBLE_NOTE",
    "AccountFacts",
    "AliasInfo",
    "DomainJoinMigration",
    "DomainJoinPreview",
    "DomainMember",
    "DomainSummary",
    "IdentityAdminRejected",
    "InstanceDomainInfo",
    "MergePreview",
    "MergeTransferPreview",
    "OrphanPresetBucket",
    "PresetAffectionRow",
    "PresetSwitchImpact",
    "alias_in_domain",
    "clean_name",
    "clean_note",
    "cleanup_orphan_preset",
    "domain_label",
    "domain_memory_paragraph_count",
    "group_names",
    "list_aliases",
    "list_domains",
    "list_instance_domains",
    "list_orphan_buckets",
    "merge_self_domain_into_group",
    "preset_display_name",
    "preset_names",
    "preview_domain_join",
    "preview_merge",
    "preview_preset_switch",
    "release_group_members",
]
