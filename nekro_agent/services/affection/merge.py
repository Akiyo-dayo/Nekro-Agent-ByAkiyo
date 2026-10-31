"""账号合并的服务层：把两个平台账号认成同一个人，并且**能拆开**

## 合并是别名指向，不是把数据行并到一起

`DECISIONS.md`「账号合并」写死了这条：**必须用别名指向，不能物理合并数据行**——
合错了要能拆开，删别名即撤销；真把两个人的数据搅在一起就再也分不开。
因此本模块只做两件事：

1. 在 `identity_alias` 上建（删）一行 `(域, 副账号) → 人`；
2. 为"好感取高的"这条语义补一笔**补偿事件**，让人的那份好感涨到两边的较高者。

副账号那一行好感、以及它名下的全部流水，**一个字节都不动**。撤销时把补偿事件按原样扣回，
两边就各自回到自己的原值——这条由 `test_affection_scope_and_merge.py` 的
「合并 → 撤销 → 数据完好」钉死。

## 为什么"取高的"必须是一笔事件，而不是改一个数

想过三种写法，只有这一种同时满足"可撤销"与"不改副账号的行"：

- **读时取 max**：写侧不动，读时把人的分数取成全部账号的最大值。撤销免费，但这个人从此
  **跌不下去**——副账号那份旧分数变成一条永久地板，一个后来变得很讨厌的人也会一直显示"交心"。
- **直接把主账号的分数改成较高值**：撤销时不知道该改回多少（中间还有正常的涨跌），
  "原值完好"直接落空。
- **补一笔 `source=merge` 的事件**（本模块）：分数按普通增量走一遍
  `store.apply_delta`，因此有流水、有审计、能被后台看见；撤销时按
  `(source in (merge, unmerge), source_platform_userid=副账号)` 把这一对账目求和，
  取相反数再补一笔就精确回到"没合并过的样子"。中间正常挣到的分数留下，
  借来的那一份还回去。

## 人设维：一次合并要处理好几份好感

好感按 `(域, 人, 人设)` 隔离，所以副账号在**每一个说过话的人设下**都可能有一行。
合并时逐个人设比较、逐个人设补偿；人设维在这里一步都不能省，否则"人设甲的分数被并进人设乙"
就是把两个人格的关系搅在一起。

## 域是唯一的授权边界

本模块的入口都要求调用方**先把域解析出来**（`identity_scope.resolve_affection_domain`
或 `ScopeDomain` 的两个工厂），而不是接一个 `instance_key` 自己去查：

- 别名表的第一维就是域，`(域, 账号) → 人` 少了域就会泄进另一个租户
  （见 `models/db_identity.py`「人的标识必须与域绑定」）；
- 接口层因此必须先过 `tenant_perm.assert_instance_accessible`（越权 404）拿到实例，
  再由实例解析出域。**租户只能合并自己域内的账号**，这是本轮第二条高危风险面
  （第一条是把别人的实例拉进自己的组）。

配置了好感域覆盖（`affection_group_id`，前端不暴露）时记忆域与好感域是两个不同的域，
别名要在两个域里各建一次才会同时对记忆与好感生效。本模块只认调用方传进来的那个域，
不替它做这个决定。

## 不做自动推断

触发只有两条：后台手动选两个账号合并，或用户在 Bot 内自助申报、管理员批准。
把两个不同的人合并的代价远大于多点一下，所以本模块不提供任何"看起来像同一个人"的判定。
"""

from __future__ import annotations

from typing import Dict, List, NamedTuple

from tortoise.exceptions import IntegrityError

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_affection import DBAffection, DBAffectionEvent
from nekro_agent.models.db_identity import ALIAS_SOURCE_MANUAL, DBIdentityAlias
from nekro_agent.services.identity_scope import ScopeDomain

from .key import AffectionKey
from .stage import AffectionEventSource
from .store import apply_delta

logger = get_sub_logger("affection")

# 补偿事件的来源集合。撤销时按它求和，因此**多一个少一个都会让撤销算错**：
# 少了 `MERGE` 会重复扣、少了 `UNMERGE` 会把已经撤销过的再扣一次。
# 公开给 `identity_admin` 的入域并入用：它把「补偿没落地」的那批事件改记成别的来源，
# 就是靠把它们挪出这个集合来让撤销不再认它们（见 `merge_self_domain_into_group`）。
COMPENSATION_SOURCES = (AffectionEventSource.MERGE.value, AffectionEventSource.UNMERGE.value)


class PresetTransfer(NamedTuple):
    """一个人设下的补偿结果

    Attributes:
        preset_id: 人设标识；0 表示无人设档
        delta: 本次补偿的增量（合并时 >= 0，撤销时 <= 0）
        score_after: 补偿之后人的分数
    """

    preset_id: int
    delta: int
    score_after: int


class MergeResult(NamedTuple):
    """一次合并/撤销的结果，供后台如实回显

    Attributes:
        domain_id: 操作发生的域
        person_uid: 人的标识（= 主账号）
        alias_platform_userid: 被指向的副账号
        transfers: 逐人设的补偿明细；空表示两边没有一份好感需要动
        rewritten_aliases: 因为"别名不成链"而被一并改写的其他副账号
    """

    domain_id: str
    person_uid: str
    alias_platform_userid: str
    transfers: List[PresetTransfer]
    rewritten_aliases: List[str]

    @property
    def total_delta(self) -> int:
        """全部人设上的补偿总量，仅供日志与后台提示"""
        return sum(transfer.delta for transfer in self.transfers)


async def merge_accounts(
    domain: ScopeDomain,
    *,
    person_uid: str,
    alias_platform_userid: str,
    operator: str = "",
    source: str = ALIAS_SOURCE_MANUAL,
    note: str = "",
) -> MergeResult:
    """把 `alias_platform_userid` 认成 `person_uid` 这个人的副账号

    Args:
        domain: 合并发生的域。**调用方必须先做越权校验**再解析出它，理由见模块文档。
            域只能来自 `store.affection_domain_of_instance` 或
            `identity_scope.resolve_affection_domain`——**不要自己
            `ScopeDomain.of_instance(适配器, 实例)`**：实例被放进实例组之后那是**另一个域**，
            别名会建在一个谁也不会去查的位置上，表现为"合并了但没反应"
        person_uid: 合并之后这个人的标识；必须是一个"自己不是别名"的账号
        alias_platform_userid: 被合并掉的那个账号（换号场景下是旧号）
        operator: 操作者用户名，落在别名行与补偿事件上
        source: 别名来源，`manual`（后台手动）或 `self_report`（用户申报 + 管理员批准）
        note: 备注

    Returns:
        合并结果，含逐人设的补偿明细

    Raises:
        ValueError: 两个账号相同、任一为空、目标账号自己已经是别名，
            或该副账号已经被合并到**另一个**人名下

    合并是幂等的：同一对账号重复合并不会再补一次分（第二次的差额是 0），
    也不会写出第二行别名。
    """
    person_uid = person_uid.strip()
    alias_platform_userid = alias_platform_userid.strip()
    if not person_uid or not alias_platform_userid:
        raise ValueError("合并账号需要两个非空的平台账号标识")
    if person_uid == alias_platform_userid:
        raise ValueError(f"不能把账号合并到它自己: {person_uid}")

    # 目标必须是"自己不是别名"的账号，否则建出来的就是一条链，而解析器只跟一跳
    # （`models/db_identity.py`「别名不成链」）。把链拆成"先撤销再合并"是操作者的事——
    # 这里替他猜一个目标，猜错的代价是两个不相干的人被并在一起。
    target_alias = (
        await DBIdentityAlias.filter(domain_id=domain.domain_id, platform_userid=person_uid).order_by("id").first()
    )
    if target_alias is not None:
        raise ValueError(
            f"账号 {person_uid} 自己已经是 {target_alias.person_uid} 的副号，"
            "请先撤销那条合并，或直接合并到最终的那个人",
        )

    existing = (
        await DBIdentityAlias.filter(domain_id=domain.domain_id, platform_userid=alias_platform_userid)
        .order_by("id")
        .first()
    )
    if existing is not None and existing.person_uid != person_uid:
        raise ValueError(
            f"账号 {alias_platform_userid} 已经被合并到 {existing.person_uid} 名下，"
            "请先撤销那条合并再重新合并",
        )

    # 动手之前先看一眼域对不对。放在补偿之后就永远看不出问题了：补偿会把人的那一行建出来，
    # "这个域里一份数据都没有"这个前提当场自己消失。
    await _warn_if_domain_looks_wrong(domain, person_uid=person_uid, alias_platform_userid=alias_platform_userid)

    # 副账号自己名下还挂着别的副号时，那些副号必须跟着改写指向新的人，否则就成了链。
    # 它们**不会**在撤销时自动跟回去（撤销只认被点名的那一个账号），因此如实报给操作者。
    rewritten = await _rewrite_aliases_pointing_to(
        domain,
        old_person_uid=alias_platform_userid,
        new_person_uid=person_uid,
    )

    if existing is None:
        try:
            await DBIdentityAlias.create(
                domain_id=domain.domain_id,
                platform_userid=alias_platform_userid,
                person_uid=person_uid,
                source=source,
                operator=operator[:128],
                note=note,
            )
        except IntegrityError:
            # 唯一约束裁决并发：输的一方想要的结果（这一行已经在了）恰好已经达成
            logger.debug(f"账号别名已被并发创建，沿用既有行: domain={domain.domain_id}, alias={alias_platform_userid}")

    transfers = await _apply_merge_compensation(
        domain,
        person_uid=person_uid,
        alias_platform_userid=alias_platform_userid,
        operator=operator,
    )
    result = MergeResult(
        domain_id=domain.domain_id,
        person_uid=person_uid,
        alias_platform_userid=alias_platform_userid,
        transfers=transfers,
        rewritten_aliases=rewritten,
    )
    logger.info(
        f"账号合并完成: domain={result.domain_id}, {alias_platform_userid} -> {person_uid}, "
        f"operator={operator or '-'}, 补偿 {len(transfers)} 个人设共 {result.total_delta} 点"
        + (f"，一并改写的副号: {rewritten}" if rewritten else ""),
    )
    return result


async def unmerge_account(
    domain: ScopeDomain,
    *,
    alias_platform_userid: str,
    operator: str = "",
) -> MergeResult:
    """撤销一次合并：删掉别名，并把当初补进去的那一份好感原样扣回

    Args:
        domain: 合并所在的域
        alias_platform_userid: 要拆出来的那个账号
        operator: 操作者用户名

    Returns:
        撤销结果；`transfers` 里的 `delta` 是负数（或 0）

    Raises:
        ValueError: 该账号在本域里没有被合并过

    撤销之后副账号立刻回到自己那份好感与流水（它们从来没被改过），主账号回到
    "没有借那一份"的分数。**合并期间挣到的分数留在主账号名下**——那是这个人自己挣的，
    与合并无关；这也是"删别名即撤销"能成立的边界：撤销的是合并这个动作，不是时间。
    """
    alias_platform_userid = alias_platform_userid.strip()
    alias = (
        await DBIdentityAlias.filter(domain_id=domain.domain_id, platform_userid=alias_platform_userid)
        .order_by("id")
        .first()
    )
    if alias is None:
        raise ValueError(f"账号 {alias_platform_userid} 在域 {domain.domain_id} 里没有被合并过")

    person_uid = alias.person_uid
    # 先删别名再扣分：反过来的话，扣分与删别名之间的那一瞬间读到的是"人还合着、分数已经掉了"，
    # 而这个顺序下最坏是"已经拆开、分数还没扣"，与撤销的方向一致。
    await alias.delete()
    transfers = await _apply_unmerge_compensation(
        domain,
        person_uid=person_uid,
        alias_platform_userid=alias_platform_userid,
        operator=operator,
    )
    result = MergeResult(
        domain_id=domain.domain_id,
        person_uid=person_uid,
        alias_platform_userid=alias_platform_userid,
        transfers=transfers,
        rewritten_aliases=[],
    )
    logger.info(
        f"账号合并已撤销: domain={result.domain_id}, {alias_platform_userid} 脱离 {person_uid}, "
        f"operator={operator or '-'}, 扣回 {len(transfers)} 个人设共 {result.total_delta} 点",
    )
    return result


async def list_person_accounts(domain: ScopeDomain, person_uid: str) -> List[str]:
    """列出这个人在该域内的全部账号（主账号在前）

    后台展示与"这一份好感属于谁"的说明用得到；热路径上的同一件事在
    `store.person_account_ids`（那边入参是隔离键，含人设维）。
    """
    accounts = [person_uid]
    rows = (
        await DBIdentityAlias.filter(domain_id=domain.domain_id, person_uid=person_uid)
        .order_by("id")
        .values_list("platform_userid", flat=True)
    )
    for row in rows:
        account = str(row)
        if account and account not in accounts:
            accounts.append(account)
    return accounts


async def _warn_if_domain_looks_wrong(domain: ScopeDomain, *, person_uid: str, alias_platform_userid: str) -> None:
    """两个账号在这个域里都毫无痕迹时告警

    合并**用错域**是这套接口唯一一种会静默失败的用法：调用方自己拼了
    `ScopeDomain.of_instance(...)`，而那个实例其实在一个实例组里，于是别名建在了一个
    谁也不会去查的位置上——界面显示"合并成功"，Bot 那边毫无变化。这条查询只在
    "一份数据都没匹配上"时才有话说，因此不会给正常合并添噪声。

    只告警不拒绝：一个从没说过话的新号被提前合并进来是**合法**的（换号的人可能还没发言），
    把它判成错误会挡掉一个真实用法。

    判据只查好感行、不查流水：流水只由 `store._write` 写，而它必然先把好感行建出来
    （`_ensure_record`），因此"有流水没有行"不存在。多查一张表只是多一个会分叉的判据。
    """
    touched = await DBAffection.filter(
        **domain.as_columns(),
        platform_userid__in=[person_uid, alias_platform_userid],
    ).exists()
    if not touched:
        logger.warning(
            f"账号合并的两个账号在域 {domain.domain_id} 里都没有任何数据: "
            f"{alias_platform_userid} -> {person_uid}；如果这个实例属于某个实例组，"
            "域应当由 `store.affection_domain_of_instance` 解析，而不是按实例自己拼",
        )


async def _rewrite_aliases_pointing_to(domain: ScopeDomain, *, old_person_uid: str, new_person_uid: str) -> List[str]:
    """把指向 `old_person_uid` 的别名改成指向 `new_person_uid`，返回被改写的账号

    维护"别名不成链"这条不变式是合并服务的职责（`models/db_identity.py`）：
    解析器只跟一跳，留下一条链等于让那些副号**当场失去自己的好感**——它们指向的账号
    已经不是一个人了，解析结果是一个谁也不会再写入的位置。

    被改写的这些副号有两处**已知的不完整**，因此本函数会 WARNING 把它们逐个报出来：
    它们不会随着"撤销 A"自动回到 A 名下；它们当初并给 A 的那笔补偿也记在 A 那一行上，
    单独撤销其中一个只会扣回 0（宁可少扣，不能把别人挣的分扣掉）。
    三方合并本来就该由操作者逐个确认，这里不替他猜。
    """
    rows = await DBIdentityAlias.filter(domain_id=domain.domain_id, person_uid=old_person_uid).order_by("id")
    if not rows:
        return []
    rewritten: List[str] = []
    for row in rows:
        row.person_uid = new_person_uid
        await row.save()
        rewritten.append(row.platform_userid)
    logger.warning(
        f"账号合并顺带改写了指向 {old_person_uid} 的别名: domain={domain.domain_id}, "
        f"accounts={rewritten} -> {new_person_uid}；撤销 {old_person_uid} 时它们不会自动跟回去",
    )
    return rewritten


async def _apply_merge_compensation(
    domain: ScopeDomain,
    *,
    person_uid: str,
    alias_platform_userid: str,
    operator: str,
) -> List[PresetTransfer]:
    """好感取高的：逐人设把人的分数补到不低于副账号那一份

    只看副账号**有行**的那些人设：人在别的人设下的分数与这次合并无关。
    差额 <= 0 的人设一笔都不写——重复合并、以及"副账号分数更低"这两种最常见的情况因此
    完全没有副作用，也让合并天然幂等。
    """
    alias_rows = await DBAffection.filter(**domain.as_columns(), platform_userid=alias_platform_userid).order_by("id")
    if not alias_rows:
        return []

    person_scores = await _scores_by_preset(domain, person_uid)
    transfers: List[PresetTransfer] = []
    for alias_row in alias_rows:
        gap = alias_row.score - person_scores.get(alias_row.preset_id, 0)
        if gap <= 0:
            continue
        key = AffectionKey.of_domain(domain, person_uid=person_uid, preset_id=alias_row.preset_id)
        snapshot = await apply_delta(
            key,
            delta=gap,
            reason=f"并入账号 {alias_platform_userid} 的更高好感（{alias_row.score}）",
            source=AffectionEventSource.MERGE,
            operator=operator,
            # 三个 False 各有各的理由：
            # - 补偿不是一次互动，不该让"聊了多少轮"凭空+1；
            # - 也不是这段关系里刚发生的事，写进 `last_event_text` 会被下一轮提示词
            #   当成"最近发生了什么"念给用户听（"我并入了你另一个号的好感"）；
            # - 来源账号记成副号，撤销时正是靠它把这一笔认出来。
            bump_interaction=False,
            update_event_text=False,
            source_platform_userid=alias_platform_userid,
        )
        transfers.append(PresetTransfer(preset_id=alias_row.preset_id, delta=gap, score_after=snapshot.score))
    return transfers


async def _apply_unmerge_compensation(
    domain: ScopeDomain,
    *,
    person_uid: str,
    alias_platform_userid: str,
    operator: str,
) -> List[PresetTransfer]:
    """把这个副账号当初补进来的那一份原样扣回

    账目从流水里现算：`(source ∈ {merge, unmerge}, source_platform_userid = 副账号)`
    这一组事件的 `delta` 之和就是"目前还借着多少"。于是重复撤销是安全的（第二次求和为 0），
    合并 → 撤销 → 再合并 → 再撤销也不会算错。

    扣回之后分数可能低于合并前——中间发生过下跌时，被跌掉的那一部分本来就已经花在
    "借来的分数"上了。这是补偿账本的确定性结果，不做额外的"不许低于原值"修正：
    那需要把合并前的原值再存一份，而任何一份额外状态都会成为下一次不一致的来源。
    """
    outstanding = await _outstanding_compensation(
        domain,
        person_uid=person_uid,
        alias_platform_userid=alias_platform_userid,
    )
    transfers: List[PresetTransfer] = []
    for preset_id, borrowed in sorted(outstanding.items()):
        if borrowed == 0:
            continue
        key = AffectionKey.of_domain(domain, person_uid=person_uid, preset_id=preset_id)
        snapshot = await apply_delta(
            key,
            delta=-borrowed,
            reason=f"撤销与账号 {alias_platform_userid} 的合并，扣回并入的 {borrowed} 点",
            source=AffectionEventSource.UNMERGE,
            operator=operator,
            bump_interaction=False,
            update_event_text=False,
            source_platform_userid=alias_platform_userid,
        )
        transfers.append(PresetTransfer(preset_id=preset_id, delta=-borrowed, score_after=snapshot.score))
    return transfers


async def _outstanding_compensation(
    domain: ScopeDomain,
    *,
    person_uid: str,
    alias_platform_userid: str,
) -> Dict[int, int]:
    """逐人设算出"还借着这个副账号多少点"

    在库里按 `source_platform_userid` 过滤而不是把行捞回来筛：这一列上有索引，
    而一个长期用户的流水可以有几万条。
    """
    rows = await DBAffectionEvent.filter(
        **domain.as_columns(),
        platform_userid=person_uid,
        source_platform_userid=alias_platform_userid,
        source__in=COMPENSATION_SOURCES,
    ).values_list("preset_id", "delta")
    borrowed: Dict[int, int] = {}
    for preset_id, delta in rows:
        borrowed[int(preset_id)] = borrowed.get(int(preset_id), 0) + int(delta)
    return borrowed


async def _scores_by_preset(domain: ScopeDomain, platform_userid: str) -> Dict[int, int]:
    """某个账号在本域各人设下的当前分数"""
    rows = await DBAffection.filter(**domain.as_columns(), platform_userid=platform_userid).values_list(
        "preset_id",
        "score",
    )
    return {int(preset_id): int(score) for preset_id, score in rows}


__all__ = [
    "COMPENSATION_SOURCES",
    "MergeResult",
    "PresetTransfer",
    "list_person_accounts",
    "merge_accounts",
    "unmerge_account",
]
