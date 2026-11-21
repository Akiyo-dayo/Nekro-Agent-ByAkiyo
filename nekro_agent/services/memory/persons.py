"""记忆「涉及的人」的共用逻辑

写入侧（沉淀时决定一段记忆涉及哪些账号）与读取侧（判断一段记忆该给谁看、当前私聊的
对象是谁）必须走同一套函数。两边一旦各写一份，账号归一与私聊对象的判断就会分叉，
「跟人走」的召回结果也会随之对不上。

本模块只放纯逻辑与小查询：不拼检索条件，也不碰向量库。
"""

from __future__ import annotations

import time
from collections.abc import Collection, Iterable, Sequence
from typing import TYPE_CHECKING, Any

from tortoise.expressions import Subquery
from tortoise.queryset import QuerySet
from tortoise.transactions import in_transaction

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import TOMBSTONE_STATUS, DBAdapterInstance
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_identity import DBIdentityAlias
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.models.db_mem_paragraph_person import DBMemParagraphPerson

if TYPE_CHECKING:  # pragma: no cover - 只在类型检查时需要，运行时延迟导入避免成环
    from nekro_agent.services.identity_scope import ScopeDomain

logger = get_sub_logger("memory.persons")

# Bot 自己的发言在消息表里以这个 `sender_id` 记录。它与 `DBChatMessage` 的约定一致，
# 集中定义在这里，避免沉淀侧与读取侧各写一个字面量。
BOT_SENDER_ID = "-1"
# 不当作「本机其他 Bot」的实例状态：墓碑、归档、未认领（与 `services/unclaimed_dispatch.UNCLAIMED_STATUS`
# 同值；不 import 那个模块，免得记忆层反过来依赖消息分发）
_NOT_A_KNOWN_BOT_STATUSES = frozenset({TOMBSTONE_STATUS, "archived", "unclaimed"})

# 查私聊对象时向前翻页的大小。绝大多数频道第一页就能命中；分页只是为了跳过没有账号的行。
_PRIVATE_COUNTERPART_PAGE_SIZE = 256

# 「最近 N 天在群里发过言的人」是模糊判据，允许进程内缓存 5 分钟；别名展开不允许缓存。
# 依据见设计稿的「授权判据每次现算、只有这份原始集合可以缓存」。
MEMBER_CACHE_TTL_SECONDS = 300

# 缓存条目上限；超过时丢最早的一半。绝大多数部署的频道数远小于这个量级。
_MEMBER_CACHE_MAX_ENTRIES = 4096

_MEMBER_CACHE: dict[tuple[str, int, str | None], tuple[float, frozenset[str]]] = {}


async def resolve_bot_account(adapter_key: str, instance_key: str) -> str | None:
    """本实例 Bot 自己的平台账号；查不到或未绑定时返回 None

    同一个号被别的程序发出消息时会以普通用户身份入库。把 Bot 自己算进「涉及的人」或
    「群成员」，会让它出现在本该只属于真人的召回范围里。
    """
    instance = await DBAdapterInstance.filter(
        adapter_key=adapter_key,
        instance_key=instance_key,
    ).first()
    if instance is None:
        return None
    account = (instance.provider_account_id or "").strip()
    return account or None


async def resolve_other_bot_accounts(adapter_key: str, instance_key: str) -> frozenset[str]:
    """同一适配器下本机**其他** NA 实例的 Bot 账号

    几个实例的 Bot 在同一个群里时，彼此的发言以普通账号入库。把别的 Bot 的人设台词当成
    「这个群友」的事实记下来、给它建人物事实，是验收里看到的毛病之一。只认本机登记过的
    实例账号；第三方 Bot 认不出来，由提示词交给模型按「角色扮演台词」判断。

    墓碑、归档与未认领的实例不算：未认领实例是协议端按 `self_id` 自动登记的，谁连上来都会有
    一行，算进来就能让任意账号被标成〔他 Bot〕、从「涉及的人」里消失。
    """
    rows = (
        await DBAdapterInstance.filter(adapter_key=adapter_key)
        .exclude(instance_key=instance_key)
        .exclude(status__in=sorted(_NOT_A_KNOWN_BOT_STATUSES))
        .values_list("provider_account_id", flat=True)
    )
    return frozenset(str(account).strip() for account in rows if account and str(account).strip())


def merge_other_bot_accounts(
    auto_accounts: Collection[str],
    configured_accounts: Iterable[object],
    *,
    bot_account: str | None,
) -> frozenset[str]:
    """把自动识别出的其他实例 Bot 账号与频道配置里的第三方 Bot 名单并起来

    配置项来自四层合并后的频道生效配置，可能是任意可迭代值；逐项转成字符串、去空白、
    去空项，并去掉本 Bot 自己的号——把本 Bot 标成〔他 Bot〕会让它自己的台词从记忆里消失。
    """
    accounts = {str(account).strip() for account in auto_accounts if str(account).strip()}
    for item in configured_accounts:
        account = str(item).strip()
        if account:
            accounts.add(account)
    if bot_account is not None:
        # 两个来源都要去：自动识别那一份里万一混进了本 Bot 的号，也不能把自己标成别人。
        accounts.discard(bot_account)
    return frozenset(accounts)


def human_accounts(
    messages: Sequence[DBChatMessage],
    *,
    bot_account: str | None,
    other_bots: Collection[str] = (),
) -> list[str]:
    """按首次出现顺序返回这一批里的非 Bot 账号，已去重

    排除四类行：Bot 自己的发言、没有账号或账号是纯空白的行、账号等于本实例 Bot 自己的行、
    本机其他实例的 Bot 账号（`other_bots`）。返回的是账号本身（不是别名解析后的「人」）。
    """
    seen: set[str] = set()
    accounts: list[str] = []
    for message in messages:
        if message.sender_id == BOT_SENDER_ID:
            continue
        account = (message.platform_userid or "").strip()
        if not account:
            continue
        if bot_account is not None and account == bot_account:
            continue
        if account in other_bots:
            continue
        if account in seen:
            continue
        seen.add(account)
        accounts.append(account)
    return accounts


def assign_participant_codes(accounts: Sequence[str]) -> dict[str, str]:
    """按入参顺序给出参与者代号，返回「代号 → 账号」"""
    return {f"P{index}": account for index, account in enumerate(accounts, 1)}


async def resolve_private_counterpart(
    chat_key: str,
    *,
    adapter_key: str,
    instance_key: str,
    batch: Sequence[DBChatMessage] | None = None,
) -> str | None:
    """取私聊对象的账号

    **写入侧（沉淀私聊时）与读取侧（判断当前私聊的对象是谁）必须都用这个函数。**
    两边口径一分叉，「跟人走」的可见范围就会与实际私聊对象对不上：写入时记成了甲，
    读取时却按乙展开账号，甲的记忆永远召不回来，乙的隐私反而可能被带出。

    有 `batch` 时先看这一批：恰好一个非 Bot 账号就取它；多于一个说明这批消息混进了
    第二个人（异常形态，通常是迁移或并发写入造成的），不猜，打 WARNING 后返回 None；
    一个都没有才回落到查库。查库取这个频道里 id 最大、且满足非 Bot 条件的消息账号。
    """
    bot_account = await resolve_bot_account(adapter_key, instance_key)

    if batch is not None:
        accounts = human_accounts(batch, bot_account=bot_account)
        if len(accounts) == 1:
            return accounts[0]
        if len(accounts) > 1:
            logger.warning(
                f"私聊批次里出现多个非 Bot 账号，无法确定私聊对象: chat_key={chat_key}, count={len(accounts)}",
            )
            return None

    query = DBChatMessage.filter(chat_key=chat_key).exclude(sender_id=BOT_SENDER_ID)
    if bot_account is not None:
        query = query.exclude(platform_userid=bot_account)

    cursor: int | None = None
    while True:
        page_query = query
        if cursor is not None:
            page_query = page_query.filter(id__lt=cursor)
        rows: list[DBChatMessage] = await page_query.order_by("-id").limit(_PRIVATE_COUNTERPART_PAGE_SIZE)
        if not rows:
            return None
        for row in rows:
            account = (row.platform_userid or "").strip()
            if account:
                return account
        cursor = rows[-1].id


async def load_person_accounts(paragraph_ids: Sequence[int]) -> dict[int, list[str]]:
    """一次查出这些段落各自涉及的账号

    每个入参 ID 都有一项，没有人的段落对应空列表；账号列表已排序，输出确定。
    """
    unique_ids = list(dict.fromkeys(int(paragraph_id) for paragraph_id in paragraph_ids))
    result: dict[int, list[str]] = {paragraph_id: [] for paragraph_id in unique_ids}
    if not unique_ids:
        return result

    rows = await DBMemParagraphPerson.filter(paragraph_id__in=unique_ids).all()
    for row in rows:
        result.setdefault(row.paragraph_id, []).append(row.account)
    for accounts in result.values():
        accounts.sort()
    return result


def persons_of_paragraphs(**paragraph_filter: Any) -> QuerySet[DBMemParagraphPerson]:
    """「满足这些条件的段落」的全部关联行，用子查询表达

    删除实例 / 清空工作区时要先清关联行。先把段落 ID 全取出来再 `IN (...)`，段落一多
    就超过数据库驱动一条语句的参数上限（asyncpg 是 32767 个），删除直接失败；
    子查询让数据库自己在服务端连表，条数再多也是一条语句。
    """
    return DBMemParagraphPerson.filter(
        paragraph_id__in=Subquery(DBMemParagraph.filter(**paragraph_filter).values("id")),
    )


async def build_paragraph_payload(paragraph: DBMemParagraph) -> dict[str, Any]:
    """构造段落写入向量库时的 payload，涉及的人取库里的真值

    所有写段落 payload 的地方都应经过这里（或用 `to_qdrant_payload(person_accounts=...)`
    显式传入同一份账号），否则编辑一条有人的段落时会把 `person_accounts` 覆盖成空数组。
    """
    accounts = (await load_person_accounts([paragraph.id]))[paragraph.id]
    return paragraph.to_qdrant_payload(person_accounts=accounts)


async def replace_paragraph_persons(paragraph_id: int, accounts: Sequence[str]) -> list[str]:
    """整体替换一个段落涉及的账号，返回最终写入的列表

    先按入参去空白、去重、排序，再在一个事务里删掉旧行并批量插入。整体替换而不是增量
    合并：模型每次给的是这一条记忆的完整名单，增量合并会让已经不该出现的账号留在表里。
    """
    normalized = sorted({account.strip() for account in accounts if account and account.strip()})

    async with in_transaction():
        await DBMemParagraphPerson.filter(paragraph_id=paragraph_id).delete()
        if normalized:
            await DBMemParagraphPerson.bulk_create(
                [
                    DBMemParagraphPerson(paragraph_id=paragraph_id, account=account)
                    for account in normalized
                ],
            )
    return normalized


def reset_member_cache() -> None:
    """清空群成员缓存，仅供测试使用。

    缓存的是「最近 N 天在这个群里发过言的**原始账号集合**」。这份判据本来就是模糊的
    （30 天窗口、发言时间会变），多缓存 5 分钟最坏只是让刚发言的人晚一点被算成成员。
    而别名的展开**绝不能**缓存：合并 / 撤销合并必须在下一次检索就生效，缓存它会让撤销
    合并后的一段时间里记忆继续挂在错误的人名下。
    """
    _MEMBER_CACHE.clear()


def _drop_oldest_half() -> None:
    for key in list(_MEMBER_CACHE)[: max(1, len(_MEMBER_CACHE) // 2)]:
        _MEMBER_CACHE.pop(key, None)


async def active_member_accounts(
    chat_key: str,
    *,
    days: int,
    bot_account: str | None,
) -> frozenset[str]:
    """最近 ``days`` 天在这个频道发过言的非 Bot 账号

    口径与 :func:`human_accounts` 相同：排除 Bot 自己的发言（``sender_id == -1``）、
    空账号、以及本实例 Bot 自己的号。按 `DBChatMessage.send_timestamp`（秒）往回推。

    这份**原始账号集合**允许进程内缓存 ``MEMBER_CACHE_TTL_SECONDS`` 秒。理由：它是一条
    模糊判据，且别名展开不在这里做；真正需要「合并后下一次检索立刻生效」的是人这一维，
    由 :func:`expand_accounts` 每次现查别名表来保证。缓存条目超限时丢最早的一半。
    """
    cache_key = (chat_key, int(days), bot_account)
    cached = _MEMBER_CACHE.get(cache_key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < MEMBER_CACHE_TTL_SECONDS:
        return cached[1]

    cutoff = int(time.time()) - int(days) * 86400
    rows = await (
        DBChatMessage.filter(chat_key=chat_key, send_timestamp__gte=cutoff)
        .exclude(sender_id=BOT_SENDER_ID)
        .distinct()
        .values_list("platform_userid", flat=True)
    )
    accounts: set[str] = set()
    for raw in rows:
        account = str(raw or "").strip()
        if not account:
            continue
        if bot_account is not None and account == bot_account:
            continue
        accounts.add(account)

    frozen = frozenset(accounts)
    if len(_MEMBER_CACHE) >= _MEMBER_CACHE_MAX_ENTRIES:
        _drop_oldest_half()
    _MEMBER_CACHE[cache_key] = (now, frozen)
    return frozen


async def expand_accounts(domain: "ScopeDomain", accounts: Iterable[str]) -> frozenset[str]:
    """把账号展开成「这些账号 + 它们所属的人 + 这些人的全部别名账号」

    **不缓存。** 合并与撤销合并必须在下一次检索就生效：缓存展开结果会让撤销之后的一段
    时间里记忆继续挂在错误的人名下，而这条错误归属没有任何报错能指向它。
    """
    from nekro_agent.services.identity_scope import resolve_person_uids

    originals = {account.strip() for account in accounts if account and account.strip()}
    if not originals:
        return frozenset()

    person_uids = frozenset((await resolve_person_uids(domain, sorted(originals))).values())
    if not person_uids:
        return frozenset(originals)
    alias_rows = await DBIdentityAlias.filter(
        domain_id=domain.domain_id,
        person_uid__in=list(person_uids),
    ).values_list("platform_userid", flat=True)
    aliases = {str(account) for account in alias_rows if account}
    return frozenset(originals | person_uids | aliases)


__all__ = [
    "BOT_SENDER_ID",
    "MEMBER_CACHE_TTL_SECONDS",
    "active_member_accounts",
    "assign_participant_codes",
    "build_paragraph_payload",
    "expand_accounts",
    "human_accounts",
    "load_person_accounts",
    "merge_other_bot_accounts",
    "persons_of_paragraphs",
    "replace_paragraph_persons",
    "reset_member_cache",
    "resolve_bot_account",
    "resolve_other_bot_accounts",
    "resolve_private_counterpart",
]
