"""一轮里的状态：装载、初始化，以及给世界书模板的变量视图

注入与变量提供者在同一轮里都要状态，按本轮令牌共用**同一次装载**（进行中的也共用：变量来源的工厂
限时 0.5 秒，第一轮推导模板可能更久，超时被取消也不会打断装载，状态块那边接着等同一个结果）。
预览（`dry_run`）只读不写、也不进缓存：不建模板、不建频道状态、不清开场白的初值。

- 模板：有就用；没有就从这个人设的书里**启用的** `[initvar]` 条目推导（多条按 order 从大到小合并，
  先出现的键优先），存成 `source=initvar`。模板存**未展开宏的原文**，私密词只用平台层面（插件全局配置）
  的词表推导——全局人设的模板各租户共用，不能带上某一个实例的词表或宏结果。都没有就这个频道什么都不做。
- 频道状态：第一次用到时按初值建，字符串值这时才按这个频道展开宏；开场白记下的 `<initvar>`（同一个
  人设）整体替换初值，读完就清掉。
- 私密判定：模板标的、模板推导时的词表、平台当前的词表、这个实例的词表，任何一个命中就算（实例层只能加严）。
- 人：私聊里是对方；群聊里是最近 `STATE_PRESENT_WINDOW_MINUTES` 分钟内说过话的非 Bot 账号（由宿主按
  发言时间算，触发这一轮的人排最前），最多 :data:`MAX_PEOPLE` 个。个人档案与好感各一次批量查询。
- 变量视图（`getvar('stat_data…')`）：频道状态加上「这一轮的主体」（群聊是触发的人，私聊是对方）的个人
  档案；非私聊里私密路径一律去掉（列表元素、二元组里包着的对象也往里走）。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_affection import DBAffection
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_state_channel import DBStateChannel
from nekro_agent.models.db_state_person import DBStatePerson
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services.affection.key import AffectionKey
from nekro_agent.services.affection.prompt import BOT_SENDER_IDS
from nekro_agent.services.affection.stage import stage_label, stage_of
from nekro_agent.services.identity_scope import resolve_identity_scopes, resolve_preset_id
from nekro_agent.services.tavern.greeting import GREETING_INITVAR_KEY
from nekro_agent.services.worldbook import service as wb_service
from nekro_agent.services.worldbook import template_lite
from nekro_agent.services.worldbook.purpose import EntryPurpose, title_purpose

from . import store
from . import tree as state_tree
from .initvar import InitvarError, derive_template, expand_values, parse_initvar_text, strip_meta
from .render import Person
from .schema import Template

logger = get_sub_logger("state_table.session")

MAX_PEOPLE = 8
MAX_ROUNDS = 512


@dataclass
class RoundState:
    """一轮里装载好的状态"""

    preset_id: int
    template: Template
    template_version: int
    channel_row: Optional[DBStateChannel]
    channel: dict[str, Any]
    is_private: bool
    affection_enabled: bool
    subject_uid: str
    people: list[Person] = field(default_factory=list)
    person_rows: dict[str, DBStatePerson] = field(default_factory=dict)
    person_keys: dict[str, dict[str, Any]] = field(default_factory=dict)
    person_initial: dict[str, Any] = field(default_factory=dict)
    elapsed_seconds: Optional[float] = None
    private_words: tuple[str, ...] = ()

    def subject_profile(self) -> dict[str, Any]:
        row = self.person_rows.get(self.subject_uid)
        if row is not None and isinstance(row.data, dict):
            return dict(row.data)
        return state_tree.copy_tree(self.person_initial)

    def variable_view(self) -> dict[str, Any]:
        """`getvar('stat_data…')` 读到的那棵树：频道状态 + 主体的个人档案；非私聊去掉私密路径"""
        view = state_tree.copy_tree(self.channel)
        if self.subject_uid:
            view.update(state_tree.copy_tree(self.subject_profile()))
        return view if self.is_private else strip_private(self.template, view, self.private_words)


@dataclass
class _Setup:
    """`ensure_channel_row` 与 `_build` 共用的一次初始化结果"""

    preset_id: int
    template: Template
    template_version: int
    channel_row: Optional[DBStateChannel]
    channel_tree: dict[str, Any]
    person_initial: dict[str, Any]


def strip_private(template: Template, tree: dict[str, Any], extra_words: Sequence[str] = ()) -> dict[str, Any]:
    """原地去掉私密路径（及其下面的一切），返回同一棵树

    字典、列表元素（路径段是下标）、二元组 `[值, "说明"]` 包着的值（路径不变）都往里走。列表里整个元素是
    私密的，换成 `null` 占位，不挪动后面元素的下标（更新调用按下标写）。
    """
    words = tuple(extra_words)
    _strip(template, tree, (), words)
    return tree


def _strip(template: Template, node: Any, path: tuple[str, ...], words: tuple[str, ...]) -> None:
    if isinstance(node, dict):
        for key in list(node):
            child_path = (*path, str(key))
            if template.is_private(child_path, words):
                del node[key]
            else:
                _strip(template, node[key], child_path, words)
    elif isinstance(node, list):
        if template.is_pair_at(path, node):
            _strip(template, node[0], path, words)
            return
        for index, child in enumerate(node):
            child_path = (*path, str(index))
            if template.is_private(child_path, words):
                node[index] = None
            else:
                _strip(template, child, child_path, words)


class _Rounds:
    """按本轮令牌共用一次装载（进行中的任务也共用）"""

    def __init__(self) -> None:
        self._items: OrderedDict[str, asyncio.Task[Optional[RoundState]]] = OrderedDict()

    def get(self, code: str) -> Optional[RoundState]:
        task = self._items.get(code) if code else None
        if task is None or not task.done() or task.cancelled() or task.exception() is not None:
            return None
        return task.result()

    async def load(self, code: str, factory: Callable[[], Awaitable[Optional[RoundState]]]) -> Optional[RoundState]:
        if not code:
            # 没有本轮令牌就不缓存：空串当键会让不相干的几轮共用一份
            return await factory()
        task = self._items.get(code)
        if task is not None and (task.get_loop() is not asyncio.get_running_loop() or task.cancelled()):
            self._items.pop(code, None)
            task = None
        if task is None:
            task = asyncio.ensure_future(factory())
            task.add_done_callback(lambda done: self._forget_failed(code, done))
            self._items[code] = task
            while len(self._items) > MAX_ROUNDS:
                self._items.popitem(last=False)
        # 调用方（变量来源的工厂）超时被取消时不打断装载：同一轮的另一个调用方接着等这个结果。
        return await asyncio.shield(task)

    def _forget_failed(self, code: str, task: "asyncio.Task[Optional[RoundState]]") -> None:
        if (task.cancelled() or task.exception() is not None) and self._items.get(code) is task:
            self._items.pop(code, None)

    def pop(self, code: str) -> None:
        if code:
            self._items.pop(code, None)

    def clear(self) -> None:
        self._items.clear()


rounds = _Rounds()


async def initvar_entries(preset_id: int) -> list[tuple[int, str, str]]:
    """这个人设的书里启用的 `[initvar]` 条目：`(条目 ID, 标题, 正文)`，按 order 从大到小"""
    books = await DBWbBook.filter(scope_kind="preset", preset_id=preset_id, enabled=True).values_list("id", flat=True)
    if not books:
        return []
    entries = await wb_service.active_entries_for_books(list(books))
    found = [
        (int(entry.order or 0), int(entry.id), str(entry.comment or ""), str(entry.content or ""))
        for entry in entries
        if title_purpose(str(entry.comment or ""), str(entry.content or "")) is EntryPurpose.INITVAR
    ]
    found.sort(key=lambda item: (-item[0], item[1]))
    return [(entry_id, comment, content) for _order, entry_id, comment, content in found]


async def derive_from_book(
    preset_id: int,
    *,
    private_words: Sequence[str],
    entry_id: Optional[int] = None,
) -> Optional[tuple[Template, dict[str, Any], Optional[int]]]:
    """从书里的 `[initvar]` 推一份模板（原文，不展开宏）；一条都解析不了时返回 None

    `private_words` 只该是平台层面（插件全局配置）的词表：模板跨租户共用。`entry_id` 给了就只用那一条
    （它得是这个人设的书里启用的 `[initvar]` 条目），避开残留条目的污染。解析与推导放进线程；失败只记类别，
    不记原因原文（原因里可能带卡片作者起的字段名）。
    """
    entries = await initvar_entries(preset_id)
    if entry_id is not None:
        entries = [entry for entry in entries if entry[0] == entry_id]
    if not entries:
        return None
    return await asyncio.to_thread(_derive, preset_id, entries, tuple(private_words))


def _derive(
    preset_id: int, entries: Sequence[tuple[int, str, str]], private_words: tuple[str, ...]
) -> Optional[tuple[Template, dict[str, Any], Optional[int]]]:
    merged: dict[str, Any] = {}
    first_id: Optional[int] = None
    for entry_id, _comment, content in entries:
        try:
            tree = parse_initvar_text(content)
        except InitvarError as error:
            logger.warning(f"[initvar] 条目解析不了，跳过: entry_id={entry_id}, 类别={_category(error)}")
            continue
        for key, value in tree.items():
            merged.setdefault(key, value)
        first_id = entry_id if first_id is None else first_id
    if not merged:
        return None
    try:
        template, cleaned = derive_template(merged, private_words=private_words)
    except InitvarError as error:
        logger.warning(f"[initvar] 推导模板失败: preset_id={preset_id}, 类别={_category(error)}")
        return None
    return template, cleaned, first_id


def _category(error: BaseException) -> str:
    """失败的类别：底下真正的异常类型名（模板不合法、YAML 写错、嵌套太深之类），不带原因原文"""
    return type(error.__cause__ or error).__name__


def _greeting_initvar(channel: DBChatChannel, preset_id: int) -> Optional[str]:
    try:
        data = channel._load_data()  # noqa: SLF001 - 与频道 data 的其它读写共用同一份解析
    except Exception:  # noqa: BLE001
        return None
    stored = data.get(GREETING_INITVAR_KEY)
    if isinstance(stored, dict) and stored.get("preset_id") == preset_id and isinstance(stored.get("text"), str):
        return stored["text"]
    return None


async def _clear_greeting_initvar(channel: DBChatChannel) -> None:
    try:
        await channel.refresh_from_db(fields=["data"])
        data = channel._load_data()  # noqa: SLF001
        if data.pop(GREETING_INITVAR_KEY, None) is None:
            return
        channel.data = json.dumps(data, ensure_ascii=False)
        await channel.save(update_fields=["data", "update_time"])
    except Exception as error:  # noqa: BLE001 - 清不掉只会让下次初始化再读一遍
        logger.warning(f"清开场白初值失败: chat_key={channel.chat_key}, error={type(error).__name__}")


async def present_people(
    channel: DBChatChannel, window_minutes: int, first: str = "", *, scope: str = "window"
) -> list[tuple[str, str]]:
    """「在场的人」：非 Bot 账号 `(账号, 昵称)`，从新到旧，`first` 排最前，最多 :data:`MAX_PEOPLE` 个

    `scope` 为 `window` 时是最近 `window_minutes` 分钟内说过话的人；为 `round` 时是这个频道上一条 Bot
    消息之后说过话的人（都只算本次会话开始之后的消息）。
    """
    floor = int(channel.conversation_start_time.timestamp()) if channel.conversation_start_time else 0
    query = DBChatMessage.filter(chat_key=channel.chat_key, send_timestamp__gte=floor)
    if scope == "round":
        last_bot = await (
            query.filter(sender_id__in=list(BOT_SENDER_IDS)).order_by("-id").limit(1).values_list("id", flat=True)
        )
        if last_bot:
            query = query.filter(id__gt=int(last_bot[0]))
    else:
        cutoff = int(time.time()) - max(1, window_minutes) * 60
        query = query.filter(send_timestamp__gte=max(cutoff, floor))
    rows = await (
        query.exclude(sender_id__in=list(BOT_SENDER_IDS))
        .order_by("-send_timestamp", "-id")
        .limit(200)
        .values("platform_userid", "sender_id", "sender_nickname")
    )
    seen: dict[str, str] = {}
    for row in rows:
        uid = str(row.get("platform_userid") or row.get("sender_id") or "")
        if uid and uid not in seen:
            seen[uid] = str(row.get("sender_nickname") or "")
    ordered = list(seen.items())
    if first:
        ordered = [(first, seen.get(first, ""))] + [item for item in ordered if item[0] != first]
    return ordered[:MAX_PEOPLE]


async def _elapsed(channel: DBChatChannel) -> Optional[float]:
    rows = (
        await DBChatMessage.filter(chat_key=channel.chat_key)
        .order_by("-send_timestamp")
        .limit(2)
        .values_list(
            "send_timestamp",
            flat=True,
        )
    )
    stamps = list(rows)
    if len(stamps) < 2:
        return None
    return float(stamps[0]) - float(stamps[1])


async def load_round_state(
    *,
    one_time_code: str,
    channel: DBChatChannel,
    preset_id: Optional[int],
    config: Any,
    trigger_uid: str,
    platform_words: Sequence[str],
    extra_words: Sequence[str] = (),
    expand: Optional[Callable[[str], str]] = None,
    dry_run: bool = False,
    person_scope: str = "window",
) -> Optional[RoundState]:
    """装载这一轮的状态（按令牌共用；预览不缓存）；这个频道没有模板也没有 `[initvar]` 时返回 None

    Args:
        platform_words: 平台层面的私密词表（推导模板只用它）。
        extra_words: 这个实例的私密词表（运行时额外加严）。
        expand: 这个频道的宏展开器（建频道状态时展开初值的字符串值）。
        person_scope: 群聊里「在场的人」怎么算（`window` / `round`，见 :func:`present_people`）。
    """

    async def build() -> Optional[RoundState]:
        return await _build(
            channel=channel,
            preset_id=preset_id,
            config=config,
            trigger_uid=trigger_uid,
            platform_words=tuple(platform_words),
            extra_words=tuple(extra_words),
            expand=expand,
            dry_run=dry_run,
            person_scope=person_scope,
        )

    if dry_run or not one_time_code:
        return await build()
    return await rounds.load(one_time_code, build)


async def _ensure_channel_row(
    channel: DBChatChannel,
    *,
    platform_words: tuple[str, ...],
    expand: Optional[Callable[[str], str]],
    dry_run: bool,
    preset_id: Optional[int] = None,
) -> Optional[_Setup]:
    """取模板（没有就从书里推导并保存）并确保这个频道有一行状态；没有模板时返回 None

    `dry_run` 只读：不保存推导出的模板、不建频道行、不清开场白初值。建行撞唯一键时由
    :func:`store.create_channel` 回读已有的那一行。
    """
    resolved_preset = preset_id if preset_id is not None else await resolve_preset_id(channel)
    template_row, template = await store.load_template(resolved_preset)
    initial: dict[str, Any] = dict(template_row.initial or {}) if template_row is not None else {}
    template_version = int(template_row.version) if template_row is not None else 0
    if template is None:
        derived = await derive_from_book(resolved_preset, private_words=platform_words)
        if derived is None:
            return None
        template, initial, source_entry = derived
        if not dry_run:
            saved = await store.save_template(
                resolved_preset, template, initial, source="initvar", source_entry_id=source_entry
            )
            if saved is not None:
                template_version = int(saved.version)

    channel_row = await store.get_channel(channel.chat_key, resolved_preset)
    if channel_row is None:
        greeting_text = _greeting_initvar(channel, resolved_preset)
        if greeting_text is not None:
            try:
                initial = strip_meta(parse_initvar_text(greeting_text))
            except InitvarError as error:
                logger.warning(f"开场白初值解析不了，用书里的: chat_key={channel.chat_key}, 类别={_category(error)}")
        channel_part, _person_part = store.split_initial(template, expand_values(initial, expand))
        if not dry_run:
            channel_row = await store.create_channel(
                chat_key=channel.chat_key,
                preset_id=resolved_preset,
                adapter_key=str(channel.adapter_key or ""),
                instance_key=str(channel.instance_key or ""),
                template_version=template_version,
                data=channel_part,
            )
            if greeting_text is not None:
                await _clear_greeting_initvar(channel)
        channel_tree = dict(channel_row.data) if channel_row is not None else channel_part
    else:
        channel_tree = dict(channel_row.data or {})
    _channel_part, person_initial = store.split_initial(template, initial)
    return _Setup(
        preset_id=resolved_preset,
        template=template,
        template_version=template_version,
        channel_row=channel_row,
        channel_tree=channel_tree,
        person_initial=person_initial,
    )


async def ensure_channel_row(
    channel: DBChatChannel,
    *,
    platform_words: tuple[str, ...],
    expand: Optional[Callable[[str], str]] = None,
) -> str:
    """确保这个频道在当前人设下有一行状态；返回 `ready`（已有或刚建好）或 `no_template`"""
    setup = await _ensure_channel_row(channel, platform_words=platform_words, expand=expand, dry_run=False)
    return "ready" if setup is not None else "no_template"


async def _build(
    *,
    channel: DBChatChannel,
    preset_id: Optional[int],
    config: Any,
    trigger_uid: str,
    platform_words: tuple[str, ...],
    extra_words: tuple[str, ...],
    expand: Optional[Callable[[str], str]],
    dry_run: bool,
    person_scope: str = "window",
) -> Optional[RoundState]:
    setup = await _ensure_channel_row(
        channel,
        preset_id=preset_id,
        platform_words=platform_words,
        expand=expand,
        dry_run=dry_run,
    )
    if setup is None:
        return None
    resolved_preset = setup.preset_id
    template = setup.template
    template_version = setup.template_version
    channel_row = setup.channel_row
    channel_tree = setup.channel_tree
    person_initial = setup.person_initial
    runtime_words = tuple(dict.fromkeys((*platform_words, *extra_words)))

    is_private = getattr(channel, "chat_type", None) == ChatType.PRIVATE
    affection_enabled = bool(getattr(config, "AFFECTION_ENABLE_SYSTEM", False))
    window = int(getattr(config, "STATE_PRESENT_WINDOW_MINUTES", 30) or 30)
    subject_uid = trigger_uid
    if is_private:
        people_ids = [(trigger_uid, "")] if trigger_uid else []
    else:
        people_ids = await present_people(channel, window, first=trigger_uid, scope=person_scope)

    person_keys: dict[str, dict[str, Any]] = {}
    if people_ids and (template.person_roots() or affection_enabled):
        scopes = await resolve_identity_scopes(channel, [uid for uid, _ in people_ids], preset_id=resolved_preset)
        person_keys = {uid: dict(AffectionKey.of_scope(scope).as_columns()) for uid, scope in scopes.items()}
    person_rows = await store.load_people(person_keys) if template.person_roots() else {}

    relations: dict[str, str] = {}
    if affection_enabled and person_keys:
        for uid, record in (await store.rows_by_person(DBAffection, person_keys)).items():
            relations[uid] = stage_label(stage_of(int(record.score)))

    people: list[Person] = []
    for uid, nickname in people_ids:
        row = person_rows.get(uid)
        if row is not None:
            profile = dict(row.data or {})
        elif uid == subject_uid and template.person_roots():
            profile = state_tree.copy_tree(person_initial)
        else:
            profile = {}
        people.append(Person(uid=uid, nickname=nickname, profile=profile, relation=relations.get(uid, "")))

    return RoundState(
        preset_id=resolved_preset,
        template=template,
        template_version=template_version,
        channel_row=channel_row,
        channel=channel_tree,
        is_private=is_private,
        affection_enabled=affection_enabled,
        subject_uid=subject_uid,
        people=people,
        person_rows=person_rows,
        person_keys=person_keys,
        person_initial=person_initial,
        elapsed_seconds=await _elapsed(channel),
        private_words=runtime_words,
    )


def provider_of(state: RoundState) -> template_lite.VariableProvider:
    """变量视图 → 模板求值用的提供者（`getLocalVar` 读不到：MVU 默认不写聊天变量）"""
    return template_lite.MappingVariables(cache={"stat_data": state.variable_view()}, local={})


def as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


__all__ = [
    "MAX_PEOPLE",
    "RoundState",
    "derive_from_book",
    "ensure_channel_row",
    "initvar_entries",
    "load_round_state",
    "present_people",
    "provider_of",
    "rounds",
    "strip_private",
]
