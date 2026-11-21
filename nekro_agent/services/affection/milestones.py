"""好感里程碑：关系阶段跨过门槛时，宿主按模板写一条长期记忆

## 什么时候写

只看**模型写的分**（`stage.MODEL_SOURCES`：后台评估的 `llm` 与主模型自主调整的 `tool`）。人工调分、重置、
账号合并与撤销、入域并入、清印象 / 编辑印象都是运营动作，不是关系里发生的事；
把它们记成里程碑，后台时间线里就会出现「管理员改了分数，于是关系升温了」这种假叙事。

判据读的是好感行 `milestone_refs` 的第一个元素 `L`（新的在前；重置时整列清空，所以它就是
**自上一次重置以来**的最后一条里程碑），当前阶段为 `S`：

- 没有 `L`：这一次写入让阶段变了就写，「从」是写入之前的阶段；
- 有 `L` 且 `S` 落在 `L.to` 与它来的那一侧紧挨着的一个阶段之内：不写。
  这一格回差是为了挡住门槛附近来回跳（29 ↔ 30），同时又不至于像「只认新高」那样
  让和好永远记不下来；
- 有 `L`、`S` 在范围外、且距 `L` 已满冷却：写，「从」是 `L.to`；
- 有 `L`、`S` 在范围外但冷却没到：这次不写，冷却过后下一次模型写分会补上。

## 写什么

不另调模型，正文是第三人称的模板：

```
{日期}，{人设名}和「{昵称}」的关系从「{从}」变成了「{到}」。起因：{起因}
```

起因就是这一次写分的 `reason`，清洗后截断。另加一道宿主判据：起因里有连续若干字出现在
这一轮评估看过的消息原文里，就当成抄了原话，整句丢掉——里程碑会被注入提示词，
把用户原话抄进去等于给了一条把任意文本送进系统提示词的路。

`summary` 存**不带起因**的整句：删流水理由时直接拿它覆盖正文（见 :func:`strip_reason`），
因此它必须自己就是一段完整可读的文字，而不是「从 → 到」这种只能配着正文看的摘要。

## 怎么存

一行 `mem_paragraph`（`origin_kind = affection`、语义 / 情感、`is_frozen = true`），
「涉及的人」只写这一次说话的那个账号，向量照常嵌入。`is_frozen` 是必须的：好感行还在，
它的里程碑就不该在几个月后被自动清理按权重悄悄失活；管理员仍可以在记忆页解冻。

好感行上的 `milestone_refs` 留最近 5 条引用（新的在前），流水上的 `memory_paragraph_id`
反指段落。两处在同一个事务里写，段落与「涉及的人」在这个事务之前建好，事务失败就把刚建的
段落软删掉——否则会留下一段谁也不指向、又会被检索到的记忆。

## 怎么取

好感块里每人一行 `milestone:`，取 `milestone_refs` 第一个元素指向的、仍然有效的段落。
**只看第一个**：最近那条被删掉了就不显示，不往后找——「最近一条」是产品口径，
往后找会把一条更久远、当时已经翻篇的关系重新顶到最前面。

## 联动

- 好感「重置」：这一份好感下的里程碑全部软删、`milestone_refs` 清空；
- 「删一条流水的理由」：对应段落改写成 `summary`（不带起因）并重新嵌入；
- 孤儿人设清理：删流水之前先软删这一桶的里程碑；
- 频道重建：`reset_domain_channel_memory` 跳过里程碑（它们不是从消息沉淀出来的，
  重建回放不出来）；
- 自动记忆块在回表时排除里程碑，`memory_tools` 照旧搜得到。

## 失败不连坐

里程碑写在分数落库之后。任何异常只告警，绝不影响这一轮好感，也不往外抛。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, Mapping, Optional, Sequence, Tuple

from tortoise.timezone import now as db_now
from tortoise.transactions import in_transaction

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_affection import DBAffection, DBAffectionEvent
from nekro_agent.models.db_mem_paragraph import CognitiveType, DBMemParagraph, KnowledgeType, OriginKind
from nekro_agent.services.identity_scope import ScopeDomain, resolve_affection_domain
from nekro_agent.services.memory.feature_flags import (
    is_domain_scope_enabled,
    is_memory_enabled_for_channel,
    is_memory_system_enabled,
)
from nekro_agent.tools.untrusted_content import sanitize_inline_text

from .key import AffectionKey
from .stage import MODEL_SOURCES, AffectionEventSource, AffectionStage, all_stages, stage_label
from .store import (
    AffectionSnapshot,
    AffectionWriteOutcome,
    invalidate_cache,
    normalize_milestone_refs,
    person_account_ids,
)

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注，运行期不需要频道模型
    from nekro_agent.core.config import CoreConfig
    from nekro_agent.models.db_chat_channel import DBChatChannel
    from nekro_agent.services.identity_scope import IdentityScope

logger = get_sub_logger("affection")

# 好感行上留几条引用。5 条足够让后台看出「这段关系怎么走到今天的」，
# 再多也不会被提示词用到（提示词只取第一条）。
MILESTONE_REFS_KEEP = 5

# 起因最多留多少字。它是一句解释，不是一段叙事；太长会把整条记忆挤成理由的容器。
MILESTONE_REASON_MAX_CHARS = 60

# 注入好感块时正文的上限，与 `recently:` 同一量级，免得一条里程碑挤占整个上下文。
MILESTONE_PROMPT_MAX_CHARS = 160

# 起因与原文的连续重合字数。与消息撤回插件同一取值：短于这个长度，中文里的常用搭配
# （「一起」「今天」）会频繁误判，长于这个长度又挡不住只抄半句的情况。
ORIGINAL_OVERLAP_CHARS = 6

# 抄了原文时整句换上的宿主文字（与消息撤回同一种写法）。理由那句主模型调分与后台评估共用：
# 两边的理由都写进流水、回注成 `recently:`；印象只有后台评估会写，回注成好感块里的那一段。
# 判据（`quotes_original`）与替换文字都只放在这一处。
REASON_REDACTED = "（理由里带了原文，已丢弃）"
FEELING_REDACTED = "（印象里带了原文，已丢弃）"

# 昵称与人设名的上限。两者都进正文，都是不可信内容（昵称由用户自己设、人设名由管理员填）。
MILESTONE_NICKNAME_MAX_CHARS = 24
MILESTONE_PERSONA_MAX_CHARS = 32

# 每个键一把进程内的锁：同一个人可能在两个群里同时被评估，两次写入各自判「有没有跨门槛」，
# 不加锁就会写出两条一模一样的里程碑。NA 单进程运行，与「记一笔」同一做法。
_LOCKS_MAX_ENTRIES = 2000
_locks: Dict[AffectionKey, asyncio.Lock] = {}

# 告警节流：同一类失败每十分钟最多一条，避免一个坏频道把日志刷满。
_WARN_SUPPRESS_SECONDS = 600
_WARN_MAX_ENTRIES = 1024
_warned_at: Dict[str, float] = {}


def quotes_original(text: str, originals: Sequence[str], window: int = ORIGINAL_OVERLAP_CHARS) -> bool:
    """`text` 里有没有连续 `window` 个非空白字符出现在任一条原文里

    去掉全部空白再比：模型抄原话时经常会重新断行、加空格，按原样比对会漏。
    去空白后不足 `window` 字的文本返回 False——太短的片段无法区分「抄」与「巧合」。

    两边先过同一道清洗（剥零宽等不可见字符）：理由在落库前已经清洗过，原文若不清洗，
    原文里每个字之间插一个零宽字符，模型照抄后的理由就跟它对不上任何一个窗口。
    """
    squashed = "".join(_visible_text(text).split())
    if len(squashed) < window:
        return False
    bodies = ["".join(_visible_text(original).split()) for original in originals]
    for start in range(len(squashed) - window + 1):
        piece = squashed[start : start + window]
        if any(piece in body for body in bodies):
            return True
    return False


def _visible_text(text: str) -> str:
    """剥掉不可见字符、压平空白，不截断（上限取原长，清洗只会让它更短）"""
    return sanitize_inline_text(text or "", max(1, len(text or "")))


def milestone_band(last_to: AffectionStage, last_from: AffectionStage) -> frozenset[AffectionStage]:
    """`last_to` 与它朝 `last_from` 方向紧挨着的那个阶段

    这一格回差是判据的核心：只看 `last_to` 会挡住「和好」（掉下去再回来永远算新低），
    看得太宽又挡不住门槛附近的来回跳。朝来的那一侧多看一格，恰好覆盖「刚跨过来的那一步」，
    而反方向的下一步则一定在范围外。
    """
    order = all_stages()
    index_to = order.index(last_to)
    index_from = order.index(last_from)
    band = {last_to}
    if index_from > index_to and index_to + 1 < len(order):
        band.add(order[index_to + 1])
    elif index_from < index_to and index_to - 1 >= 0:
        band.add(order[index_to - 1])
    return frozenset(band)


def _ref_of(raw: Mapping[str, Any]) -> Optional[Tuple[AffectionStage, AffectionStage, datetime]]:
    """从一条引用里读出 `(from, to, at)`；任何一项不合法都返回 None

    引用是 JSON，可能是手工改库留下的、也可能是将来改过格式的旧值。判据宁可当作
    「没有上一条里程碑」重来一次，也不要拿半条脏数据去比阶段。
    """
    raw_from = raw.get("from")
    raw_to = raw.get("to")
    raw_at = raw.get("at")
    if not isinstance(raw_from, str) or not isinstance(raw_to, str) or not isinstance(raw_at, str):
        return None
    try:
        stage_from = AffectionStage(raw_from)
        stage_to = AffectionStage(raw_to)
    except ValueError:
        return None
    try:
        at = datetime.fromisoformat(raw_at)
    except ValueError:
        return None
    # 库里写的一律是带时区的值；朴素值只可能来自手工改库，按 UTC 解释比整条丢掉更有用。
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return stage_from, stage_to, at


def decide_milestone(
    *,
    refs: Sequence[Mapping[str, Any]],
    stage_before: AffectionStage,
    stage_now: AffectionStage,
    now: datetime,
    cooldown_hours: int,
    rebased: bool = False,
) -> Optional[Tuple[AffectionStage, AffectionStage]]:
    """判这一次写入要不要记里程碑，返回 `(从, 到)` 或 None

    Args:
        refs: 这一行现有的里程碑引用，新的在前
        stage_before: 这次写入之前的阶段（行锁内读到）
        stage_now: 这次写入之后的阶段（同样取自行锁内，不是判的时候再读一次）
        now: 判据的当前时刻
        cooldown_hours: 两条里程碑之间的最短间隔（小时）
        rebased: 上一条里程碑之后、这次写入之前有过运营动作改分（人工调分、账号合并补偿、
            入域并入）。那时阶段的变化不是关系里发生的事，不能在这次补记成里程碑：
            只看**这一次写入本身**有没有跨阶段，冷却仍按上一条里程碑算。

    Returns:
        要写时的 `(从, 到)`；不写返回 None。
    """
    last = _ref_of(refs[0]) if refs else None
    if last is None:
        if refs:
            _warn_once("bad_ref", "里程碑引用里有读不懂的 from / to / at，按「没有上一条」处理")
        if stage_before == stage_now:
            return None
        return stage_before, stage_now
    last_from, last_to, last_at = last
    if rebased:
        if stage_before == stage_now:
            return None
        if now - last_at < timedelta(hours=max(1, int(cooldown_hours))):
            return None
        return stage_before, stage_now
    if stage_now in milestone_band(last_to, last_from):
        return None
    if now - last_at < timedelta(hours=max(1, int(cooldown_hours))):
        return None
    return last_to, stage_now


def render_milestone_text(
    *,
    when: datetime,
    persona_name: str,
    nickname: str,
    stage_from: AffectionStage,
    stage_to: AffectionStage,
    reason: str,
) -> Tuple[str, str]:
    """生成里程碑正文与不带起因的摘要

    Returns:
        `(content, summary)`。`summary` 是不带起因的整句，删理由时直接拿它覆盖正文。
    """
    # 日期按部署时区写：记忆是给人看的，「11 月 10 日」比 UTC 时间戳有用得多。
    local = when.astimezone()
    date_text = f"{local.month} 月 {local.day} 日"
    clean_persona = sanitize_inline_text(persona_name, MILESTONE_PERSONA_MAX_CHARS) or "Bot"
    clean_nickname = sanitize_inline_text(nickname, MILESTONE_NICKNAME_MAX_CHARS) or "对方"
    summary = (
        f"{date_text}，{clean_persona}和「{clean_nickname}」的关系"
        f"从「{stage_label(stage_from)}」变成了「{stage_label(stage_to)}」。"
    )
    content = summary + (f"起因：{reason}" if reason else "")
    return content, summary


def clean_reason(reason: str, originals: Sequence[str]) -> str:
    """清洗起因；抄了原文或清洗后为空就返回空串（正文只留模板）"""
    cleaned = sanitize_inline_text(reason, MILESTONE_REASON_MAX_CHARS)
    if not cleaned:
        return ""
    if quotes_original(cleaned, originals):
        return ""
    return cleaned


async def latest_milestone_texts(
    keys: Sequence[AffectionKey],
    snapshots: Mapping[AffectionKey, AffectionSnapshot],
) -> Dict[AffectionKey, Tuple[int, str]]:
    """每个键最近一条仍然有效的里程碑正文，一次 `IN` 查询取完

    只看 `milestone_refs` 的第一个元素：最近那条被删掉了就不显示，不往后找。
    一轮对话通常只有几个人，逐个点查会把代价乘上人数，因此合并成一次查询。
    """
    first_ids: Dict[AffectionKey, int] = {}
    for key in keys:
        snapshot = snapshots.get(key)
        if snapshot is None or not snapshot.milestone_refs:
            continue
        paragraph_id = snapshot.milestone_refs[0].get("paragraph_id")
        # bool 是 int 的子类，脏值里出现 True 时不能当成段落 1。
        if isinstance(paragraph_id, bool) or not isinstance(paragraph_id, int):
            continue
        first_ids[key] = paragraph_id
    if not first_ids:
        return {}
    rows = {
        int(row.id): row
        for row in await DBMemParagraph.filter(id__in=sorted(set(first_ids.values())), is_inactive=False)
    }
    domains: Dict[Tuple[str, str], ScopeDomain] = {}
    texts: Dict[AffectionKey, Tuple[int, str]] = {}
    for key, paragraph_id in first_ids.items():
        row = rows.get(paragraph_id)
        if row is None:
            continue
        # 段落写它的那个实例已经不在这份好感的域里（组内实例被单独划给了别的租户）：
        # 段落跟着实例走了、归了新主人，正文可能已被改写，不能再当成这份关系的记忆注入。
        if not await _in_domain(row, key.domain, domains):
            continue
        texts[key] = (paragraph_id, row.content)
    return texts


async def paragraph_ids_in_domain(domain: ScopeDomain, ids: Sequence[int]) -> set[int]:
    """这些段落里，此刻仍落在 `domain` 这个好感域里的那些

    里程碑段落按写它的实例记（与别的记忆同一口径，实例划转时跟着实例走），好感行却挂在域上。
    组内的某个实例被单独划给别的租户、又没勾「一并清空」时，两者就分开了：段落归了新主人，
    好感行还留在原来的组上。这时原来那一侧再读、再删、再改写这些段落，就是在动别人的数据。
    凡是「从好感行这一侧去碰段落」的入口（好感块、重置连带、删理由改写、孤儿清理、
    后台详情与删除）都先过这一道。

    判据是「写它的实例此刻的好感域是不是这一个」，与好感块每轮解析域用的是同一个函数。
    """
    wanted = sorted({int(value) for value in ids})
    if not wanted:
        return set()
    domains: Dict[Tuple[str, str], ScopeDomain] = {}
    kept: set[int] = set()
    for row in await DBMemParagraph.filter(id__in=wanted):
        if await _in_domain(row, domain, domains):
            kept.add(int(row.id))
    return kept


async def _in_domain(row: DBMemParagraph, domain: ScopeDomain, cache: Dict[Tuple[str, str], ScopeDomain]) -> bool:
    pair = (row.adapter_key or "", row.instance_key or "")
    if pair not in cache:
        cache[pair] = await resolve_affection_domain(*pair)
    return cache[pair] == domain


async def maybe_record(
    *,
    key: AffectionKey,
    scope: "IdentityScope",
    channel: "DBChatChannel",
    outcome: AffectionWriteOutcome,
    source: AffectionEventSource,
    reason: str,
    nickname: str,
    persona_name: str,
    originals: Sequence[str],
    config: "CoreConfig",
    source_account: str,
) -> Optional[int]:
    """模型写分之后判一次里程碑，写了就返回段落 ID

    前提任一不满足就直接返回 None：不是模型写的分、里程碑开关关着、记忆总开关或域分区闸关着、
    这个频道没开聊天记忆、好感域被覆盖成了与记忆域不同的域（写进去会落在一个与这份好感的
    可见范围不一致的地方）。

    整段包在异常边界里：里程碑写在分数落库之后，它失败不该让这一轮好感跟着失败。
    """
    if source not in MODEL_SOURCES:
        return None
    if not config.AFFECTION_MILESTONE_ENABLED:
        return None
    if not is_memory_system_enabled() or not is_domain_scope_enabled():
        return None
    if scope.domain != scope.affection_domain:
        return None
    try:
        if not await is_memory_enabled_for_channel(channel):
            return None
    except Exception as e:
        _warn_once("channel_flag", f"读取频道聊天记忆开关失败，不写里程碑: error={e}")
        return None
    try:
        async with _key_lock(key):
            return await _record_locked(
                key=key,
                channel=channel,
                outcome=outcome,
                reason=reason,
                nickname=nickname,
                persona_name=persona_name,
                originals=originals,
                config=config,
                source_account=source_account,
            )
    except Exception as e:
        _warn_once("record", f"写里程碑失败（已忽略）: key={key}, error={type(e).__name__}: {e}")
        return None


async def soft_delete_paragraphs(ids: Sequence[int]) -> int:
    """软删若干段落；委托记忆管理的公开入口，口径只有一份"""
    from nekro_agent.services.memory import admin_service

    if not ids:
        return 0
    return await admin_service.soft_delete_paragraphs(list(ids))


async def clear_for_reset(key: AffectionKey) -> int:
    """好感「重置」的连带：这一份好感下的里程碑全部软删，引用清空

    持同一把键锁：重置与评估可能同时发生，不串行的话评估会把一条刚被软删的段落重新
    挂回 `milestone_refs`，于是重置之后第一次跃迁就不是「第一条」了。
    """
    async with _key_lock(key):
        # 已经随实例划走的段落不归这份好感管了，重置不能顺手软删别人的记忆
        ids = sorted(await paragraph_ids_in_domain(key.domain, await _person_milestone_ids(key)))
        deleted = await soft_delete_paragraphs(ids)
        await DBAffection.filter(**key.as_columns()).update(milestone_refs=[])
        invalidate_cache(key)
        return deleted


async def strip_reason(paragraph_id: int, *, domain: ScopeDomain) -> None:
    """把一条里程碑改写成不带起因的模板，并重新嵌入

    删流水理由时调用：起因抄自那条理由，理由删了正文却留着，等于换个地方原样保存了
    要删掉的内容。已经失活的段落只改正文、不碰向量——它不再被检索到，
    重新嵌入只是白花一次外部调用。

    `domain` 是那条流水所在好感行的域：段落已经随实例划走时不改（见 :func:`paragraph_ids_in_domain`）。
    """
    row = await DBMemParagraph.filter(id=paragraph_id).first()
    if row is None or row.origin_kind != OriginKind.AFFECTION:
        return
    if not await _in_domain(row, domain, {}):
        return
    if not row.summary or row.content == row.summary:
        return
    row.content = row.summary
    row.last_manual_action = "affection_redact"
    row.last_manual_action_at = db_now()
    await row.save(update_fields=["content", "last_manual_action", "last_manual_action_at", "update_time"])
    if row.is_inactive:
        return
    try:
        accounts = await _paragraph_accounts(row.id)
        await _embed_and_upsert(row, accounts)
    except Exception as e:
        _warn_once("strip_embed", f"改写里程碑后重新嵌入失败（已忽略）: paragraph_id={paragraph_id}, error={e}")


async def clear_for_orphan_bucket(columns: Dict[str, object]) -> int:
    """孤儿人设清理的连带：软删这一桶（同实例对、同人设）的里程碑

    必须在删流水**之前**调用：段落 ID 只记在流水的 `memory_paragraph_id` 上，
    流水删了就再也找不回那些段落，它们会留在记忆里被检索到。
    """
    rows = await DBAffectionEvent.filter(**columns, memory_paragraph_id__isnull=False).values_list(
        "memory_paragraph_id",
        flat=True,
    )
    group_id = int(columns.get("domain_group_id") or 0)
    domain = (
        ScopeDomain.of_group(group_id)
        if group_id
        else ScopeDomain.of_instance(str(columns.get("adapter_key") or ""), str(columns.get("instance_key") or ""))
    )
    ids = sorted(await paragraph_ids_in_domain(domain, [int(value) for value in rows if value is not None]))
    return await soft_delete_paragraphs(ids)


async def _record_locked(
    *,
    key: AffectionKey,
    channel: "DBChatChannel",
    outcome: AffectionWriteOutcome,
    reason: str,
    nickname: str,
    persona_name: str,
    originals: Sequence[str],
    config: "CoreConfig",
    source_account: str,
) -> Optional[int]:
    """锁内的一整套判据与写入，见模块文档

    判的是**这一次写入**：分数落库与拿到这把锁之间隔着几次 await（读频道开关、取人设名），
    管理员可能恰好在这中间重置、人工调分或删掉这条流水的理由。因此：

    - 阶段一律取行锁里记下的 `outcome.stage_before` / `stage_after`，不在这里再读一次分数；
    - 这条流水之后已经有运营动作改过分（重置、人工调分、合并补偿），这次的判断就过时了，不写；
    - 这条流水的理由已经被删掉了，起因不再带上。
    """
    event = await DBAffectionEvent.filter(id=outcome.event_id).first()
    if event is None:
        return None
    # 同一条流水只会生成一段：重试、重复投递都可能让同一个 outcome 再进来一次。
    if event.memory_paragraph_id is not None:
        return None
    if await _operator_changed_score(key, after_event_id=event.id):
        return None
    row = await DBAffection.filter(**key.as_columns()).first()
    if row is None:
        return None
    refs = normalize_milestone_refs(row.milestone_refs)
    last = _ref_of(refs[0]) if refs else None
    # 上一条里程碑之后、这次写入之前有过运营动作改分：那一下的阶段变化不是关系里发生的事，
    # 不能拿「现在」去跟上一条里程碑比，只看这一次写入自己有没有跨阶段（见 `decide_milestone`）。
    # 先后按流水 ID 定（引用里记着上一条里程碑的流水）；读不到时才退回按时间比
    last_event_id = refs[0].get("event_id") if refs else None
    if isinstance(last_event_id, bool) or not isinstance(last_event_id, int):
        last_event_id = None
    rebased = last is not None and await _operator_changed_score(
        key,
        after_event_id=last_event_id,
        before_event_id=event.id,
        since=last[2] if last_event_id is None else None,
    )
    now = db_now()
    decision = decide_milestone(
        refs=refs,
        stage_before=outcome.stage_before,
        stage_now=outcome.stage_after,
        now=now,
        cooldown_hours=int(config.AFFECTION_MILESTONE_COOLDOWN_HOURS),
        rebased=rebased,
    )
    if decision is None:
        return None

    stage_from, stage_to = decision
    # 正文里怎么称呼对方：已经记下的称呼（`set_call_name` / 后台编辑）优先，平台昵称兜底。
    # 平台昵称常常是「web_user_3」或一串表情，Bot 平时根本不这么叫他，读记忆时对不上人。
    content, summary = render_milestone_text(
        when=now,
        persona_name=persona_name,
        nickname=(row.call_name or "").strip() or nickname or row.nickname,
        stage_from=stage_from,
        stage_to=stage_to,
        reason="" if event.deleted_at is not None else clean_reason(reason, originals),
    )
    paragraph = await DBMemParagraph.create(
        workspace_id=0,
        instance_key=channel.instance_key,
        adapter_key=channel.adapter_key,
        preset_id=key.preset_id,
        origin_chat_key=channel.chat_key,
        origin_chat_type=_origin_chat_type(channel),
        memory_source="na",
        # 必须是语义记忆：Episode 聚合只收情景记忆，里程碑因此不会被并进任何 Episode。
        cognitive_type=CognitiveType.SEMANTIC,
        knowledge_type=KnowledgeType.EMOTION,
        content=content,
        summary=summary,
        event_time=now,
        half_life_seconds=int(config.MEMORY_SEMANTIC_HALF_LIFE_DAYS) * 24 * 3600,
        # 冻结：好感行还在，它的里程碑就不该在几个月后被自动清理按权重悄悄失活。
        # 暂停衰减也顺带保证它不会因为「很久没被检索到」而排到后面去。
        is_frozen=True,
        origin_kind=OriginKind.AFFECTION,
        origin_ref=f"affection_event:{outcome.event_id}",
    )
    ref = {
        "paragraph_id": paragraph.id,
        "event_id": outcome.event_id,
        "from": stage_from.value,
        "to": stage_to.value,
        "at": now.isoformat(),
    }
    try:
        # 「涉及的人」也放进兜底范围：它失败时段落已经建好、却没有任何流水指向它，
        # 重置与孤儿清理都按流水找段落，这样一段就永远清不到了
        accounts = await _write_persons(paragraph.id, source_account or key.person_uid)
        async with in_transaction():
            locked = await DBAffection.filter(**key.as_columns()).select_for_update().first()
            if locked is None:  # pragma: no cover - 行在锁内被删只可能是并发 purge
                raise RuntimeError(f"好感行在写里程碑时消失: {key}")
            locked.milestone_refs = _merge_refs(ref, locked.milestone_refs)
            await locked.save(update_fields=["milestone_refs", "update_time"])
            await DBAffectionEvent.filter(id=outcome.event_id).update(memory_paragraph_id=paragraph.id)
    except Exception as e:
        # 段落已经建好、引用却没落上：不软删就是一段谁也不指向、又会被检索到的记忆。
        await soft_delete_paragraphs([paragraph.id])
        _warn_once("commit", f"里程碑引用落库失败，已软删刚建的段落: key={key}, error={e}")
        return None

    invalidate_cache(key)
    try:
        await _embed_and_upsert(paragraph, accounts)
    except Exception as e:
        # 好感块按 ID 直取，不依赖向量；嵌入失败只是这条里程碑搜不到，段落本身照留。
        _warn_once("embed", f"里程碑嵌入失败（段落照留）: paragraph_id={paragraph.id}, error={e}")
    return paragraph.id


def _merge_refs(new_ref: Dict[str, Any], existing: object) -> list[Dict[str, Any]]:
    """新引用插到最前，按段落 ID 去重，截到保留条数"""
    merged: list[Dict[str, Any]] = [new_ref]
    seen = {new_ref["paragraph_id"]}
    for item in normalize_milestone_refs(existing):
        paragraph_id = item.get("paragraph_id")
        if isinstance(paragraph_id, bool) or not isinstance(paragraph_id, int) or paragraph_id in seen:
            continue
        seen.add(paragraph_id)
        merged.append(dict(item))
    return merged[:MILESTONE_REFS_KEEP]


def _origin_chat_type(channel: "DBChatChannel") -> str:
    """频道的类型列取值，复用记忆侧现成的判据

    记忆表的这一列只认 `group` / `private`，未知写空串（见 `models/db_mem_paragraph.py`）。
    `instance_scope` 里的判据多返回一个 `other`，那正是「未知」，因此在这里折成空串——
    自己再写一份 `getattr(chat_type, "value", ...)` 就是同一条判据的第二处实现。
    """
    from nekro_agent.services.memory.instance_scope import _resolve_chat_kind

    kind = _resolve_chat_kind(channel)
    return kind if kind in ("group", "private") else ""


async def _write_persons(paragraph_id: int, account: str) -> list[str]:
    """写「涉及的人」：只写这一次说话的那个账号，不展开别名

    与 L2 的约定一致。展开别名会让一条里程碑挂上一个人全部账号，
    而合并是可撤销的——展开过的名单撤销不回来。
    """
    from nekro_agent.services.memory.persons import replace_paragraph_persons

    return await replace_paragraph_persons(paragraph_id, [account])


async def _paragraph_accounts(paragraph_id: int) -> list[str]:
    from nekro_agent.services.memory.persons import load_person_accounts

    return (await load_person_accounts([paragraph_id])).get(paragraph_id, [])


async def _embed_and_upsert(paragraph: DBMemParagraph, accounts: Sequence[str]) -> None:
    """嵌入并写向量，用量记到这个实例对；调用方负责兜异常"""
    from nekro_agent.services.memory.embedding_service import embed_text
    from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager

    embedding = await embed_text(
        paragraph.content,
        requester=(paragraph.adapter_key, paragraph.instance_key),
    )
    await memory_qdrant_manager.upsert_paragraph(
        paragraph_id=paragraph.id,
        embedding=embedding,
        payload=paragraph.to_qdrant_payload(person_accounts=accounts),
    )
    paragraph.embedding_ref = str(paragraph.id)
    await paragraph.save(update_fields=["embedding_ref"])


async def _operator_changed_score(
    key: AffectionKey,
    *,
    after_event_id: Optional[int] = None,
    before_event_id: Optional[int] = None,
    since: Optional[datetime] = None,
) -> bool:
    """这个人有没有（在给定范围内）被运营动作改过分

    「运营动作」= 不是模型写的分、且真的动了分数（`delta != 0`）：人工调分、重置、账号合并
    与撤销的补偿、入域并入。只清 / 编辑印象、评估被拦后的清理都是 0 分，不算。
    按这个人全部账号的并集查，与流水、额度同一个范围；按流水 ID 定先后（自增、与提交顺序一致）。
    """
    query = DBAffectionEvent.filter(
        **key.scope_columns(),
        platform_userid__in=await person_account_ids(key),
    ).exclude(delta=0).exclude(source__in=[source.value for source in MODEL_SOURCES])
    if after_event_id is not None:
        query = query.filter(id__gt=after_event_id)
    if before_event_id is not None:
        query = query.filter(id__lt=before_event_id)
    if since is not None:
        query = query.filter(create_time__gt=since)
    return await query.exists()


async def _person_milestone_ids(key: AffectionKey) -> list[int]:
    from .store import person_milestone_paragraph_ids

    return await person_milestone_paragraph_ids(key)


@asynccontextmanager
async def _key_lock(key: AffectionKey) -> AsyncIterator[None]:
    """按好感键串行；键太多时清掉当前没被持有的锁"""
    lock = _locks.get(key)
    if lock is None:
        if len(_locks) >= _LOCKS_MAX_ENTRIES:
            for existing, candidate in list(_locks.items()):
                if not candidate.locked():
                    _locks.pop(existing, None)
        lock = asyncio.Lock()
        _locks[key] = lock
    async with lock:
        yield


def _warn_once(kind: str, message: str) -> None:
    """同一类失败每十分钟最多告警一次，其余降到 debug"""
    now = time.time()
    if now - _warned_at.get(kind, 0.0) < _WARN_SUPPRESS_SECONDS:
        logger.debug(message)
        return
    if len(_warned_at) >= _WARN_MAX_ENTRIES:
        _warned_at.clear()
    _warned_at[kind] = now
    logger.warning(message)


def reset_milestone_state() -> None:
    """清空进程内的锁与告警节流；只给测试用"""
    _locks.clear()
    _warned_at.clear()


__all__ = [
    "MILESTONE_NICKNAME_MAX_CHARS",
    "MILESTONE_PERSONA_MAX_CHARS",
    "MILESTONE_PROMPT_MAX_CHARS",
    "MILESTONE_REASON_MAX_CHARS",
    "MILESTONE_REFS_KEEP",
    "ORIGINAL_OVERLAP_CHARS",
    "clean_reason",
    "clear_for_orphan_bucket",
    "clear_for_reset",
    "decide_milestone",
    "latest_milestone_texts",
    "maybe_record",
    "milestone_band",
    "quotes_original",
    "render_milestone_text",
    "reset_milestone_state",
    "soft_delete_paragraphs",
    "strip_reason",
]
