"""命令与状态图共用的「可以给人看的状态」

数据范围只看两样：频道类型与发命令的人（不看权限——高级用户、超级用户、WebUI 执行，在群里拿到的都一样）。

- **群里**：频道状态去掉隐藏与私密字段；个人档案只有发命令的人自己的那一份，同样去掉私密字段；自动发的图
  不带任何人的档案。别人的档案、别人的好感阶段一律不出现。
- **私聊**：频道状态去掉隐藏字段；对方（就是发命令的人）的档案完整给出，私密字段也在。
- **好感映射字段**：好感系统开着时不从状态里取（它是 L6 的只读镜像）；发命令的人自己那一节另加一行「关系」。

状态图与命令文字都只从这里拿数据：这里产出 :class:`StateCardData`（只有字符串），渲染器拿不到状态树。
这里只读不写：还没有频道状态的频道返回 None，不替它初始化。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional

from nekro_agent.models.db_affection import DBAffection
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services.affection.key import AffectionKey
from nekro_agent.services.affection.stage import stage_label, stage_of
from nekro_agent.services.identity_scope import resolve_identity_scopes, resolve_preset_id
from nekro_agent.services.imagecard.state_card import (
    MAX_HEADLINE,
    MAX_ROWS,
    MAX_VALUE_CHARS,
    StateCardData,
    StateCardRow,
    StateCardSection,
    plain,
)

from . import store
from .schema import Template

HEADLINE_WORDS = ("时间", "日期", "地点", "位置", "天气", "time", "date", "place", "location", "weather")
MAX_LIST_ITEMS = 8
MAX_DEPTH = 2
OVERVIEW_TITLE = "概况"


@dataclass(frozen=True)
class Profile:
    """一个人的档案（只会是发命令的人自己的）：节标题、档案子树、好感阶段（好感系统关着时为空）"""

    title: str
    tree: Mapping[str, Any]
    relation: str = ""


def _scalar_text(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if value is None:
        return "—"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")
    return plain(str(value), MAX_VALUE_CHARS)


class _Walker:
    """把一棵子树写成卡片的行；每一层都判隐藏、好感映射与私密（与状态块同一套判据）"""

    def __init__(
        self, template: Template, *, is_private: bool, affection_enabled: bool, private_words: Sequence[str]
    ) -> None:
        self.template = template
        self.is_private = is_private
        self.affection_enabled = affection_enabled
        self.private_words = tuple(private_words)

    def skip(self, path: tuple[str, ...]) -> bool:
        if self.template.is_hidden(path):
            return True
        if self.affection_enabled and any(item is not None and item.affection for item in self.template.chain(path)):
            return True
        return not self.is_private and self.template.is_private(path, self.private_words)

    def unwrap(self, path: tuple[str, ...], value: Any) -> Any:
        return value[0] if self.template.is_pair_at(path, value) else value

    def rows(self, value: Any, path: tuple[str, ...], depth: int) -> list[StateCardRow]:
        out: list[StateCardRow] = []
        if isinstance(value, Mapping):
            for key, child in value.items():
                child_path = (*path, str(key))
                if self.skip(child_path):
                    continue
                self._entry(str(key), self.unwrap(child_path, child), child_path, depth, out)
            return out
        if isinstance(value, list):
            shown = 0
            for index, element in enumerate(value):
                element_path = (*path, str(index))
                if self.skip(element_path):
                    continue
                if shown >= MAX_LIST_ITEMS:
                    rest = sum(1 for later in range(index, len(value)) if not self.skip((*path, str(later))))
                    out.append(StateCardRow("", f"…还有 {rest} 条", depth))
                    break
                shown += 1
                element = self.unwrap(element_path, element)
                if isinstance(element, (Mapping, list)):
                    inner = self.rows(element, element_path, depth + 1) if depth < MAX_DEPTH else []
                    out.append(StateCardRow(f"第 {index + 1} 项", "" if inner else "…", depth))
                    out.extend(inner)
                else:
                    out.append(StateCardRow("", _scalar_text(element), depth))
        return out

    def _entry(self, key: str, value: Any, path: tuple[str, ...], depth: int, out: list[StateCardRow]) -> None:
        label = plain(key, 32)
        if not isinstance(value, (Mapping, list)):
            out.append(StateCardRow(label, _scalar_text(value), depth))
            return
        if depth >= MAX_DEPTH:
            # 太深的折成一行，只写看得见的那几项的个数（被隐藏、私密的不算进去）
            keys = list(value) if isinstance(value, Mapping) else [str(index) for index in range(len(value))]
            count = sum(1 for key in keys if not self.skip((*path, str(key))))
            out.append(StateCardRow(label, f"（{count} 项）", depth))
            return
        inner = self.rows(value, path, depth + 1)
        if inner:
            out.append(StateCardRow(label, "", depth))
            out.extend(inner)


def _is_headline(template: Template, key: str, value: Any) -> bool:
    if isinstance(value, (Mapping, list)):
        return False
    marked = [item for item in template.top_level() if item.headline]
    if marked:
        return any(item.path == (key,) for item in marked)
    lowered = key.lower()
    return any(word in lowered for word in HEADLINE_WORDS)


def card_from_tree(
    template: Template,
    channel_tree: Mapping[str, Any],
    *,
    title: str,
    subtitle: str,
    is_private: bool,
    affection_enabled: bool,
    private_words: Sequence[str],
    profile: Optional[Profile] = None,
) -> StateCardData:
    """把频道状态（和发命令的人自己的档案）整理成卡片数据；纯函数"""
    walker = _Walker(template, is_private=is_private, affection_enabled=affection_enabled, private_words=private_words)
    roots = set(template.person_roots())
    headline: list[StateCardRow] = []
    overview: list[StateCardRow] = []
    # (优先级, 次序, 组)：超出行数上限时先丢优先级低的（同优先级先丢靠后的）
    sections: list[tuple[float, int, StateCardSection]] = []
    for order, (key, raw) in enumerate(channel_tree.items()):
        key = str(key)
        path = (key,)
        if key in roots or walker.skip(path):
            continue
        value = walker.unwrap(path, raw)
        if len(headline) < MAX_HEADLINE and _is_headline(template, key, value):
            headline.append(StateCardRow(plain(key, 32), _scalar_text(value)))
            continue
        if not isinstance(value, (Mapping, list)):
            overview.append(StateCardRow(plain(key, 32), _scalar_text(value)))
            continue
        rows = walker.rows(value, path, 0)
        if rows:
            item = template.match(path)
            priority = float(item.priority) if item is not None else 2.0
            sections.append((priority, order, StateCardSection(plain(key, 32), tuple(rows))))
    if overview:
        sections.insert(0, (3.0, -1, StateCardSection(OVERVIEW_TITLE, tuple(overview))))
    if profile is not None:
        profile_rows: list[StateCardRow] = []
        if profile.relation:
            profile_rows.append(StateCardRow("关系", plain(profile.relation, 32)))
        profile_rows.extend(walker.rows(dict(profile.tree), (), 0))
        if profile_rows:
            sections.append(
                (9.0, len(channel_tree) + 1, StateCardSection(plain(profile.title, 32), tuple(profile_rows)))
            )

    dropped = 0
    while sections and sum(len(item[2].rows) for item in sections) > MAX_ROWS and len(sections) > 1:
        lowest = min(sections, key=lambda item: (item[0], -item[1]))
        sections.remove(lowest)
        dropped += 1
    footer = f"还有 {dropped} 组放不下，没画" if dropped else ""
    if not headline and not sections:
        footer = "现在没有可以显示的内容"
    return StateCardData(
        title=plain(title, 40) or "角色状态",
        subtitle=plain(subtitle, 60),
        headline=tuple(headline),
        sections=tuple(section for _priority, _order, section in sections),
        footer=footer,
        theme=template.theme or "default",
    )


async def build_card(channel: DBChatChannel, *, requester_uid: str, include_requester: bool) -> Optional[StateCardData]:
    """这个频道现在（当前人设）的状态卡片数据；还没有状态时返回 None（不初始化）

    `include_requester` 为真时带上 `requester_uid` 自己的档案（群里去掉私密字段，私聊完整）；
    自动发的图传假，不带任何人的档案。
    """
    preset_id = await resolve_preset_id(channel)
    row = await store.get_channel(channel.chat_key, preset_id)
    if row is None:
        return None
    return await build_card_for_row(channel, row, requester_uid=requester_uid, include_requester=include_requester)


async def build_card_for_row(
    channel: DBChatChannel, row: Any, *, requester_uid: str, include_requester: bool
) -> Optional[StateCardData]:
    """某一行频道状态（它自己的人设）的卡片数据；后台预览按行画，换过人设的旧行也能看"""
    from .updater import runtime_words

    preset_id = int(row.preset_id)
    _template_row, template = await store.load_template(preset_id)
    if template is None:
        return None
    is_private = getattr(channel, "chat_type", None) == ChatType.PRIVATE
    config = await channel.get_effective_config()
    affection_enabled = bool(getattr(config, "AFFECTION_ENABLE_SYSTEM", False))

    profile: Optional[Profile] = None
    if include_requester and requester_uid and (template.person_roots() or affection_enabled):
        scopes = await resolve_identity_scopes(channel, [requester_uid], preset_id=preset_id)
        keys = {uid: dict(AffectionKey.of_scope(scope).as_columns()) for uid, scope in scopes.items()}
        if requester_uid in keys:
            mine = {requester_uid: keys[requester_uid]}
            person = (await store.load_people(mine)).get(requester_uid) if template.person_roots() else None
            relation = ""
            if affection_enabled:
                record = (await store.rows_by_person(DBAffection, mine)).get(requester_uid)
                relation = stage_label(stage_of(int(record.score))) if record is not None else ""
            tree = dict(person.data or {}) if person is not None else {}
            if tree or relation:
                profile = Profile(title="档案" if is_private else "你的档案", tree=tree, relation=relation)

    preset = await DBPreset.get_or_none(id=preset_id)
    return card_from_tree(
        template,
        dict(row.data or {}),
        title=str(preset.name) if preset is not None else "角色状态",
        subtitle="私聊 · 角色状态" if is_private else "角色状态",
        is_private=is_private,
        affection_enabled=affection_enabled,
        private_words=runtime_words(str(channel.instance_key or "")),
        profile=profile,
    )


__all__ = ["HEADLINE_WORDS", "MAX_LIST_ITEMS", "Profile", "build_card", "build_card_for_row", "card_from_tree"]
