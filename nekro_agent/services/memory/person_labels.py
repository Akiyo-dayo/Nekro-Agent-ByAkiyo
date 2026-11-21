"""记忆里「涉及的人」怎么显示：昵称、称呼与账号

记忆只记账号（`DBMemParagraphPerson.account`）。后台要给人看的是名字：

- **昵称**：这个账号在这段记忆的**来源频道**里最近一条消息的发送者昵称；来源频道里没说过话
  （或者段落没有来源频道）时，退到好感记录里存的「最近一次见到的昵称」——先取段落**自己人设**
  那条好感记录上的，没有再取这个人任一人设上最近更新的那条。好感记录是整个好感域共用的，
  这个昵称可能是在同一个域里别的群或私聊里看到的，所以标上 `name_source = "affection"`，
  界面在悬停提示里写明「来自好感记录」。
- **称呼**：好感里模型给这个人记的称呼（`DBAffection.call_name`），按段落**自己的实例**所在的
  好感域、段落**自己的人设**取。它是模型写的不可信内容，前端只当纯文本显示。

两样都按段落各自的来源取，不跨段落去猜：同一个账号在两个群里可以用两个昵称，两个人设下可以
有两种称呼。账号本身始终带着，界面上放在悬停提示里。

**可见范围不在这里判**：调用方传进来的段落已经是这个身份能看的。名字只从段落自己的来源频道、
段落自己实例所在的好感域里取：不会取别的租户的群里的发言，也不会跨好感域；但好感域之内的
后备昵称不限于这段记忆的来源频道（见上）。

批量：一页最多 100 段、每段最多 10 个人；消息两次查询、每个好感域两次查询，不逐行点查。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Optional

from tortoise.functions import Max

from nekro_agent.models.db_affection import DBAffection
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.services.identity_scope import ScopeDomain, resolve_affection_domain, resolve_person_uids
from nekro_agent.services.memory.persons import BOT_SENDER_ID

# 一条语句里 `IN (...)` 最多放多少个值。比驱动上限保守得多，单条语句的耗时也可控。
_IN_CHUNK = 500


# `PersonLabel.name_source` 的取值
NAME_FROM_CHAT = "chat"
NAME_FROM_AFFECTION = "affection"


@dataclass(frozen=True)
class PersonLabel:
    """一个账号在某一段记忆里的显示名

    `display_name` / `call_name` 为空串表示查不到；界面此时只显示账号。`name_source` 说明
    `display_name` 从哪来：`chat`（来源频道里的发言）、`affection`（好感记录，好感域共用，
    可能是在别的群或私聊里看到的）、空串（没查到）。
    """

    account: str
    display_name: str = ""
    call_name: str = ""
    name_source: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "account": self.account,
            "display_name": self.display_name,
            "call_name": self.call_name,
            "name_source": self.name_source,
        }


def _chunks(values: Sequence[str]) -> Iterable[list[str]]:
    for start in range(0, len(values), _IN_CHUNK):
        yield list(values[start : start + _IN_CHUNK])


async def _chat_nicknames(pairs: set[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """`(会话标识, 账号)` → 这个账号在这个会话里最近一条消息的昵称

    先按（会话, 账号）分组取最大消息 ID，再按 ID 取昵称：两次查询，不把整段聊天记录拉回来。
    Bot 自己的发言（`sender_id = -1`）不算。
    """
    if not pairs:
        return {}
    chats = sorted({chat for chat, _ in pairs})
    accounts = sorted({account for _, account in pairs})
    last_ids: dict[tuple[str, str], int] = {}
    for chat_chunk in _chunks(chats):
        for account_chunk in _chunks(accounts):
            rows = (
                await DBChatMessage.filter(chat_key__in=chat_chunk, platform_userid__in=account_chunk)
                .exclude(sender_id=BOT_SENDER_ID)
                .annotate(last_id=Max("id"))
                .group_by("chat_key", "platform_userid")
                .values("chat_key", "platform_userid", "last_id")
            )
            for row in rows:
                key = (str(row["chat_key"]), str(row["platform_userid"]))
                if key in pairs and row["last_id"] is not None:
                    last_ids[key] = int(row["last_id"])
    if not last_ids:
        return {}
    by_id = {message_id: key for key, message_id in last_ids.items()}
    nicknames: dict[tuple[str, str], str] = {}
    ids = sorted(by_id)
    for start in range(0, len(ids), _IN_CHUNK):
        rows = await DBChatMessage.filter(id__in=ids[start : start + _IN_CHUNK]).values("id", "sender_nickname")
        for row in rows:
            nickname = str(row["sender_nickname"] or "").strip()
            if nickname:
                nicknames[by_id[int(row["id"])]] = nickname
    return nicknames


@dataclass(frozen=True)
class _AffectionNames:
    """一个好感域里查到的名字

    `call_names` / `preset_nicknames`：`(人, 人设)` → 称呼 / 昵称；`latest_nicknames`：`人` →
    这个人任一人设上最近更新的那条好感记录的昵称（段落自己人设上没有昵称时的后备）。
    """

    call_names: dict[tuple[str, int], str]
    preset_nicknames: dict[tuple[str, int], str]
    latest_nicknames: dict[str, str]
    person_of: dict[str, str]

    def nickname(self, person: str, preset_id: int) -> str:
        """段落自己人设上记的昵称优先：它与这一段说的是同一个「这个人设眼里的他」"""
        return self.preset_nicknames.get((person, preset_id)) or self.latest_nicknames.get(person, "")


async def _affection_names(domain: ScopeDomain, accounts: set[str], preset_ids: set[int]) -> _AffectionNames:
    """一个好感域里这些账号的称呼与昵称

    好感记录按「人」存（合并过的账号归到主账号上），所以先把账号解析成人再查。
    称呼与「同人设的昵称」都按 `(人, 人设)` 存，取的时候用段落自己的人设对上——不能拿这一页
    所有段落人设的并集去比，否则一个人在两个人设下的昵称会互相顶替。
    """
    person_of = await resolve_person_uids(domain, sorted(accounts))
    people = sorted(set(person_of.values()))
    call_names: dict[tuple[str, int], str] = {}
    preset_nicknames: dict[tuple[str, int], str] = {}
    latest_nicknames: dict[str, str] = {}
    for chunk in _chunks(people):
        # 最近更新的在前：每个人第一次见到的非空昵称就是「最近一次见到的」
        rows = (
            await DBAffection.filter(**domain.as_columns(), platform_userid__in=chunk)
            .order_by("-update_time", "-id")
            .values("platform_userid", "preset_id", "nickname", "call_name")
        )
        for row in rows:
            person = str(row["platform_userid"])
            preset_id = int(row["preset_id"])
            call_name = str(row["call_name"] or "").strip()
            nickname = str(row["nickname"] or "").strip()
            if call_name and preset_id in preset_ids:
                call_names.setdefault((person, preset_id), call_name)
            if nickname and preset_id in preset_ids:
                preset_nicknames.setdefault((person, preset_id), nickname)
            if nickname:
                latest_nicknames.setdefault(person, nickname)
    return _AffectionNames(
        call_names=call_names,
        preset_nicknames=preset_nicknames,
        latest_nicknames=latest_nicknames,
        person_of=person_of,
    )


async def label_paragraph_persons(
    rows: Sequence[DBMemParagraph],
    accounts_by_paragraph: Mapping[int, Sequence[str]],
) -> dict[int, list[PersonLabel]]:
    """给每段记忆涉及的每个账号配上显示名，返回 `段落 ID → [PersonLabel]`（顺序同入参账号）

    `accounts_by_paragraph` 一般就是 `load_person_accounts` 的结果；没有出现在里面的段落
    对应空列表。
    """
    result: dict[int, list[PersonLabel]] = {}
    chat_pairs: set[tuple[str, str]] = set()
    # 好感域按段落自己的实例解析；适配器为空的存量行不知道属于哪个实例，不查好感
    domain_rows: dict[tuple[str, str], list[DBMemParagraph]] = {}
    for row in rows:
        accounts = list(accounts_by_paragraph.get(row.id, ()))
        if not accounts:
            continue
        if row.origin_chat_key:
            chat_pairs.update((row.origin_chat_key, account) for account in accounts)
        if row.adapter_key:
            domain_rows.setdefault((row.adapter_key, row.instance_key), []).append(row)

    chat_nicknames = await _chat_nicknames(chat_pairs)

    domain_of_pair: dict[tuple[str, str], ScopeDomain] = {}
    rows_of_domain: dict[ScopeDomain, list[DBMemParagraph]] = {}
    for pair, pair_rows in domain_rows.items():
        domain = await resolve_affection_domain(*pair)
        domain_of_pair[pair] = domain
        rows_of_domain.setdefault(domain, []).extend(pair_rows)
    names_of_domain: dict[ScopeDomain, _AffectionNames] = {}
    for domain, domain_paragraphs in rows_of_domain.items():
        accounts = {account for row in domain_paragraphs for account in accounts_by_paragraph.get(row.id, ())}
        presets = {row.preset_id for row in domain_paragraphs}
        names_of_domain[domain] = await _affection_names(domain, accounts, presets)

    for row in rows:
        names: Optional[_AffectionNames] = None
        row_domain = domain_of_pair.get((row.adapter_key, row.instance_key)) if row.adapter_key else None
        if row_domain is not None:
            names = names_of_domain.get(row_domain)
        labels: list[PersonLabel] = []
        for account in accounts_by_paragraph.get(row.id, ()):
            display_name = chat_nicknames.get((row.origin_chat_key or "", account), "")
            name_source = NAME_FROM_CHAT if display_name else ""
            call_name = ""
            if names is not None:
                person = names.person_of.get(account, account)
                call_name = names.call_names.get((person, row.preset_id), "")
                if not display_name:
                    display_name = names.nickname(person, row.preset_id)
                    name_source = NAME_FROM_AFFECTION if display_name else ""
            labels.append(
                PersonLabel(account=account, display_name=display_name, call_name=call_name, name_source=name_source),
            )
        result[row.id] = labels
    return result


__all__ = ["NAME_FROM_AFFECTION", "NAME_FROM_CHAT", "PersonLabel", "label_paragraph_persons"]
