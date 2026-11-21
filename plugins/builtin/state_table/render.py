"""状态块：带字段说明的 YAML，按 token 预算整组截断

纯函数，不查库。输入里的状态树都按不可信内容看：这里只负责「写成什么样」，包裹与清洗由核心做
（插件注入返回 `PromptInjection(untrusted=True)`）。

- 隐藏字段（`_` 开头或模板标了 `hidden`）不写；私密字段只在私聊里写，群聊里只记省略了几处。
- 好感系统开着时，映射到好感的字段不从状态里写，个人那一节另写一行「关系：<阶段>」（只读镜像）。
- 个人档案只写「在场的人」（调用方按发言时间算好、从新到旧排好），每人一节，用昵称，不写平台账号。
- 超出预算时整组截掉：先截优先级低的组（顶层数组默认最低），再按发言从旧到新截个人节，最后才截
  频道主干；截掉的组名记下来给明细用。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from nekro_agent.tools.untrusted_content import sanitize_inline_text

from .schema import Template

INDENT = "  "
MAX_COMMENT_CHARS = 80
MAX_NICKNAME_CHARS = 32
PEOPLE_HEADING = "在场的人"
# 个人节在截断次序里的位置：比默认优先级（2）的组先截，比顶层数组（1）后截。
PERSON_PRIORITY = 1.5


@dataclass(frozen=True)
class Person:
    """一个在场的人：昵称、个人档案（`{个人范围的顶层键: 子树}`）、好感阶段（好感系统关着时为空）"""

    uid: str
    nickname: str
    profile: Mapping[str, Any] = field(default_factory=dict)
    relation: str = ""


@dataclass(frozen=True)
class BlockInput:
    template: Template
    channel: Mapping[str, Any]
    people: Sequence[Person]
    is_private: bool
    affection_enabled: bool
    elapsed_line: str
    budget_tokens: int
    count_tokens: Callable[[str], int]
    private_words: Sequence[str] = ()


@dataclass
class Block:
    text: str = ""
    tokens: int = 0
    trimmed: list[str] = field(default_factory=list)
    people: int = 0
    private_omitted: int = 0

    def meta(self) -> dict[str, Any]:
        """给提示词明细的那几个数（不含任何值）"""
        return {
            "tokens": self.tokens,
            "chars": len(self.text),
            "trimmed": list(self.trimmed),
            "people": self.people,
            "private_omitted": self.private_omitted,
        }


@dataclass
class _Group:
    name: str
    priority: float
    order: int
    lines: list[str]


class _Emitter:
    """把一棵子树写成 YAML 行；每一层（对象的键、列表的元素、二元组包着的值）都判隐藏、好感与私密"""

    def __init__(
        self,
        template: Template,
        *,
        is_private: bool,
        affection_enabled: bool,
        private_words: Sequence[str] = (),
    ) -> None:
        self.template = template
        self.is_private = is_private
        self.affection_enabled = affection_enabled
        self.private_words = tuple(private_words)
        self.private_omitted = 0

    def _skip(self, path: tuple[str, ...]) -> bool:
        if self.template.is_hidden(path):
            return True
        if self.affection_enabled and any(item is not None and item.affection for item in self.template.chain(path)):
            return True
        if not self.is_private and self.template.is_private(path, self.private_words):
            self.private_omitted += 1
            return True
        return False

    def _comment(self, path: tuple[str, ...], pair_note: str) -> str:
        item = self.template.match(path)
        text = item.description if item is not None and item.description else pair_note
        text = sanitize_inline_text(str(text), MAX_COMMENT_CHARS) if text else ""
        return f"  # {text}" if text else ""

    def emit(self, key: str, value: Any, path: tuple[str, ...], depth: int, out: list[str]) -> None:
        if self._skip(path):
            return
        pad = INDENT * depth
        label = _scalar(key)
        note = ""
        if self.template.is_pair_at(path, value):
            value, note = value[0], value[1]
        if isinstance(value, (Mapping, list)):
            body = self._body(value, path, depth + 1)
            if body:
                out.append(f"{pad}{label}:{self._comment(path, note)}")
                out.extend(body)
            return
        out.append(f"{pad}{label}: {_scalar(value)}{self._comment(path, note)}")

    def _body(self, value: Any, path: tuple[str, ...], depth: int) -> list[str]:
        lines: list[str] = []
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                self.emit(str(child_key), child, (*path, str(child_key)), depth, lines)
            return lines
        pad = INDENT * depth
        for index, element in enumerate(value):
            element_path = (*path, str(index))
            if self._skip(element_path):
                continue
            if self.template.is_pair_at(element_path, element):
                element = element[0]
            if isinstance(element, (Mapping, list)):
                inner = self._body(element, element_path, depth + 1)
                if inner:
                    lines.append(f"{pad}-")
                    lines.extend(inner)
            else:
                lines.append(f"{pad}- {_scalar(element)}")
        return lines


def _scalar(value: Any) -> str:
    """单值写成一行：普通文字原样，带换行、冒号、井号这类会改变 YAML 读法的加引号"""
    if isinstance(value, str):
        risky = (
            not value
            or value != value.strip()
            or any(mark in value for mark in ("\n", ": ", " #", '"', "'"))
            or value[0] in "-?[]{}!&*|>%@`,"
        )
        return json.dumps(value, ensure_ascii=False) if risky else value
    if isinstance(value, (Mapping, list)):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)


def _channel_groups(inp: BlockInput, emitter: _Emitter) -> list[_Group]:
    roots = set(inp.template.person_roots())
    priorities = {item.path[0]: item.priority for item in inp.template.top_level()}
    groups: list[_Group] = []
    for order, (key, value) in enumerate(inp.channel.items()):
        if key in roots:
            continue
        lines: list[str] = []
        emitter.emit(str(key), value, (str(key),), 0, lines)
        if lines:
            groups.append(_Group(str(key), float(priorities.get(key, 2)), order, lines))
    return groups


def _person_groups(inp: BlockInput, emitter: _Emitter) -> list[_Group]:
    groups: list[_Group] = []
    total = len(inp.people)
    for index, person in enumerate(inp.people):
        name = sanitize_inline_text(person.nickname, MAX_NICKNAME_CHARS) or f"第{index + 1}位"
        lines = [f"{INDENT}- {_scalar(name)}:"]
        if inp.affection_enabled and person.relation:
            lines.append(f"{INDENT * 3}关系: {_scalar(person.relation)}")
        for key, value in person.profile.items():
            emitter.emit(str(key), value, (str(key),), 3, lines)
        if len(lines) > 1:
            # 越早说话的越先截：次序号越小越靠前截
            groups.append(_Group(f"{PEOPLE_HEADING}:{index + 1}", PERSON_PRIORITY, total - index, lines))
    return groups


def _assemble(channel_groups: Iterable[_Group], person_groups: Sequence[_Group], elapsed_line: str) -> str:
    lines: list[str] = []
    for group in sorted(channel_groups, key=lambda item: item.order):
        lines.extend(group.lines)
    if person_groups:
        lines.append(f"{PEOPLE_HEADING}:")
        for group in sorted(person_groups, key=lambda item: -item.order):
            lines.extend(group.lines)
    if elapsed_line:
        lines.append(elapsed_line)
    return "\n".join(lines)


def render_state_block(inp: BlockInput) -> Block:
    """渲染状态块；预算为 0 或者什么都写不出来时返回空块

    截断不逐组重算整块：每组只数一次 token、累加估出要截几组，整块只精确数一次；估少了（拼接处的
    token 有出入）再在剩下的组上二分，最多再数 log2(组数) 次。
    """
    if inp.budget_tokens <= 0:
        return Block()
    emitter = _Emitter(
        inp.template,
        is_private=inp.is_private,
        affection_enabled=inp.affection_enabled,
        private_words=inp.private_words,
    )
    channel_groups = _channel_groups(inp, emitter)
    person_groups = _person_groups(inp, emitter)
    if not channel_groups and not person_groups:
        return Block(private_omitted=emitter.private_omitted)

    # 截断次序：(优先级, 次序) 小的先截；个人节的次序号越小表示说话越早。
    cut_order = sorted(
        [*channel_groups, *person_groups],
        key=lambda group: (group.priority, group.order if group.name.startswith(PEOPLE_HEADING) else -group.order),
    )

    def text_after_cutting(count: int) -> str:
        cut = {id(group) for group in cut_order[:count]}
        return _assemble(
            [group for group in channel_groups if id(group) not in cut],
            [group for group in person_groups if id(group) not in cut],
            inp.elapsed_line,
        )

    fixed = inp.count_tokens(f"{PEOPLE_HEADING}:\n{inp.elapsed_line}") if inp.elapsed_line or person_groups else 0
    sizes = [inp.count_tokens("\n".join(group.lines)) + 1 for group in cut_order]
    estimate = fixed + sum(sizes)
    cut = 0
    while cut < len(cut_order) and estimate > inp.budget_tokens:
        estimate -= sizes[cut]
        cut += 1
    text = text_after_cutting(cut)
    tokens = inp.count_tokens(text)
    if tokens > inp.budget_tokens:
        # 估少了：在 (cut, 全截] 里二分出放得下的最少截法
        low, high = cut + 1, len(cut_order)
        best: Optional[tuple[int, str, int]] = None
        while low <= high:
            middle = (low + high) // 2
            candidate = text_after_cutting(middle)
            counted = inp.count_tokens(candidate) if candidate else 0
            if counted <= inp.budget_tokens:
                best, high = (middle, candidate, counted), middle - 1
            else:
                low = middle + 1
        if best is None:
            return Block(trimmed=[group.name for group in cut_order], private_omitted=emitter.private_omitted)
        cut, text, tokens = best
    trimmed = [group.name for group in cut_order[:cut]]
    cut_ids = {id(group) for group in cut_order[:cut]}
    kept_people = [group for group in person_groups if id(group) not in cut_ids]
    if cut >= len(cut_order) or not text:
        return Block(trimmed=trimmed, private_omitted=emitter.private_omitted)
    return Block(
        text=text,
        tokens=tokens,
        trimmed=trimmed,
        people=len(kept_people),
        private_omitted=emitter.private_omitted,
    )


def elapsed_line(seconds: Optional[float]) -> str:
    """S4：距上一条消息过了多久（真实时间）；不知道时为空"""
    if seconds is None or seconds < 0:
        return ""
    minutes = int(seconds // 60)
    if minutes < 1:
        return "距上一条消息：不到 1 分钟"
    if minutes < 60:
        return f"距上一条消息：{minutes} 分钟"
    hours = minutes // 60
    if hours < 48:
        return f"距上一条消息：{hours} 小时"
    return f"距上一条消息：{hours // 24} 天"


__all__ = ["PEOPLE_HEADING", "Block", "BlockInput", "Person", "elapsed_line", "render_state_block"]
