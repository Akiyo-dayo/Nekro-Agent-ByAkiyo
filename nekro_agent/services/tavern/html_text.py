"""发送前把展示用的 HTML 标签转成纯文字

酒馆卡的开场白与回复里常夹着给网页看的标签：`<font color=…>` 给字上色、`<br>` 换行、`<div>` 排版、
`<details>` 折叠，偶尔还有整段 `<style>`。QQ 只显示文字，这些标签原样发出去就是一串尖括号。
发送路径在「内置剥离变量标记 → 卡片清理规则」之后再做这一步，只对从酒馆卡导入的人设做——别的
人设的群里，有人就是在聊 HTML，不该替他改。

宁可少转，不可错删。模型写的普通文字里也有 `<`：`if a<b and c>d`、`1<b>2`、`<a:wave:123>` 这种表情
代码、反引号里的 `<div>` 示例。所以：

- 标签名只认一张固定的展示标签表（块级与行内），按 ASCII 匹配（`ſ`、开尔文符号 `K` 不会当成 `s`、
  `k`），名字后面必须紧跟空白、`/` 或 `>`；
- 属性按 HTML 的写法：`名字=值`（值带引号或不带），不带值的只认 `open`、`hidden` 这几个常见的，
  最多 32 个——`<b and c>` 这种就不是标签；
- 除了 `<br>`、`<hr>`、`<img>` 这类空元素，其余标签必须开闭成对（同名、按层配对）才转，落单的照原样留着；
- 块级标签换成换行，行内标签与 `img` 去掉、留字；成对的 `<style>…</style>`、`<script>…</script>` 连内容
  去掉，成对的 `<!-- … -->` 去掉；没闭合的一律不动（不从它删到末尾）；
- 认识的标签之外一律不动：`<pic>`、`<options>` 这类卡片自定义标签由卡片规则管，`<UpdateVariable>`
  由内置剥离管；
- 有东西被转掉时，再把 `&lt;` `&gt;` `&amp;` `&quot;` `&#39;` `&nbsp;` 这六个带分号的实体还原（浏览器里
  显示的就是还原后的字，所以 `&lt;b&gt;` 会变成 `<b>`）；别的实体、不带分号的（`&copy`）不动；行尾空白
  逐行去掉、多余空行收一收。

整段线性：标签正则里每段重复都封了顶、属性值里不许出现 `<` `>`；`<style>` / `<script>` / 注释的结尾用
`str.find` 或有界正则找，且每处只找一次；配对用每个名字一个栈。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_BLOCK_NAMES = frozenset(
    {
        "br", "p", "div", "li", "ul", "ol", "dl", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "tr",
        "table", "thead", "tbody", "tfoot", "section", "article", "header", "footer", "aside", "nav", "main",
        "blockquote", "details", "summary", "center", "pre", "figure", "figcaption", "fieldset", "legend",
    },
)  # fmt: skip
_INLINE_NAMES = frozenset(
    {
        "font", "span", "b", "strong", "i", "em", "u", "s", "strike", "del", "ins", "small", "big", "sub", "sup",
        "mark", "a", "abbr", "cite", "q", "tt", "kbd", "var", "code", "ruby", "rt", "rp", "label", "bdi", "bdo",
        "time", "wbr", "img", "td", "th", "caption", "col", "colgroup",
    },
)  # fmt: skip
# 没有结束标签的空元素：单独出现就转
_VOID_NAMES = frozenset({"br", "hr", "img", "wbr", "col"})
_BOOLEAN_ATTRS = "open|hidden|disabled|checked|selected|nowrap|controls|autoplay|loop|muted|reversed|async|defer"


def _names_pattern(names: frozenset[str]) -> str:
    # 长的在前，免得 `b` 抢在 `blockquote` 前面
    return "|".join(sorted(names, key=len, reverse=True))


_ATTR = (
    rf"\s{{1,16}}(?:(?:{_BOOLEAN_ATTRS})(?![-\w:.])"
    r"""|[A-Za-z_:][-\w:.]{0,63}\s{0,8}=\s{0,8}(?:"[^"<>]{0,1000}"|'[^'<>]{0,1000}'|[^\s"'=<>`]{1,200}))"""
)
_TAG = re.compile(
    rf"<(/?)({_names_pattern(_BLOCK_NAMES | _INLINE_NAMES)})(?=[\s/>])((?:{_ATTR}){{0,32}})\s{{0,16}}(/?)>",
    re.IGNORECASE | re.ASCII,
)
_DROP_OPEN = re.compile(rf"<(style|script)(?=[\s>])(?:{_ATTR}){{0,32}}\s{{0,16}}>", re.IGNORECASE | re.ASCII)
_DROP_CLOSE = {name: re.compile(rf"</{name}\s{{0,16}}>", re.IGNORECASE | re.ASCII) for name in ("style", "script")}
_ENTITIES = {"&lt;": "<", "&gt;": ">", "&amp;": "&", "&quot;": '"', "&#39;": "'", "&nbsp;": " "}
_ENTITY = re.compile(r"&(?:lt|gt|amp|quot|#39|nbsp);", re.ASCII)
_BLANK_LINES = re.compile(r"\n{3,}")


def has_display_html(text: str) -> bool:
    """文字里有没有这里可能转掉的东西（标签、`<style>` / `<script>` 开始标签、注释）；没有就不必查人设"""
    if "<" not in text:
        return False
    return "<!--" in text or _TAG.search(text) is not None or _DROP_OPEN.search(text) is not None


def _drop_blocks(text: str) -> tuple[str, bool]:
    """去掉成对的 `<style>…</style>`、`<script>…</script>` 与 `<!-- … -->`；返回 `(结果, 有没有去掉东西)`

    没闭合的开始标签或注释开头原样留着，从它后面接着找。每种结尾只找一次：一旦确认某种块在某个位置之后
    再也没有结尾，后面同类的开头一律当文字，不再重复往后扫。
    """
    pieces: list[str] = []
    position = 0
    changed = False
    no_comment_end_after = len(text) + 1
    no_close_after: dict[str, int] = {}
    # 下一个注释开头与下一个开始标签各自缓存，越过了才重找：位置单调往前，每段文字只被扫常数遍
    comment = text.find("<!--")
    block = _DROP_OPEN.search(text)
    while True:
        if 0 <= comment < position:
            comment = text.find("<!--", position)
        if block is not None and block.start() < position:
            block = _DROP_OPEN.search(text, position)
        candidates = [index for index in (comment, block.start() if block else -1) if index >= 0]
        if not candidates:
            break
        start = min(candidates)
        if start == comment:
            end = -1 if comment >= no_comment_end_after else text.find("-->", comment + 4)
            if end < 0:
                no_comment_end_after = min(no_comment_end_after, comment)
                pieces.append(text[position : comment + 4])
                position = comment + 4
                continue
            pieces.append(text[position:comment])
            position = end + 3
            changed = True
            continue
        assert block is not None
        name = block.group(1).lower()
        closing = None
        if block.end() < no_close_after.get(name, len(text) + 1):
            closing = _DROP_CLOSE[name].search(text, block.end())
        if closing is None:
            no_close_after[name] = min(no_close_after.get(name, len(text) + 1), block.end())
            pieces.append(text[position : block.end()])
            position = block.end()
            continue
        pieces.append(text[position : block.start()])
        position = closing.end()
        changed = True
    pieces.append(text[position:])
    return "".join(pieces), changed


@dataclass(frozen=True)
class _Tag:
    start: int
    end: int
    name: str
    closing: bool


def _removable_spans(text: str) -> list[tuple[int, int, str]]:
    """可以转掉的标签 `(起点, 终点, 换成什么)`：空元素单独就算，其余必须开闭成对"""
    tags = [
        _Tag(match.start(), match.end(), match.group(2).lower(), match.group(1) == "/") for match in _TAG.finditer(text)
    ]
    keep = [False] * len(tags)
    stacks: dict[str, list[int]] = {}
    for index, tag in enumerate(tags):
        if tag.name in _VOID_NAMES:
            keep[index] = True
            continue
        if not tag.closing:
            stacks.setdefault(tag.name, []).append(index)
            continue
        stack = stacks.get(tag.name)
        if stack:
            keep[stack.pop()] = True
            keep[index] = True
    return [(tag.start, tag.end, "\n" if tag.name in _BLOCK_NAMES else "") for tag, ok in zip(tags, keep) if ok]


def _tidy(text: str) -> str:
    text = _ENTITY.sub(lambda match: _ENTITIES[match.group(0)], text)
    # 逐行去行尾空白：`[ \t]+\n` 这种正则在一长串空白后面没有换行时是二次方的
    text = "\n".join(line.rstrip(" \t") for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", text).strip()


def display_html_to_text(text: str) -> str:
    """把展示用的标签转成纯文字；什么都没转时原样返回（同一个对象）"""
    if not has_display_html(text):
        return text
    stripped, changed = _drop_blocks(text)
    spans = _removable_spans(stripped)
    if not spans and not changed:
        return text
    pieces: list[str] = []
    position = 0
    for start, end, replacement in spans:
        pieces.append(stripped[position:start])
        pieces.append(replacement)
        position = end
    pieces.append(stripped[position:])
    return _tidy("".join(pieces))


__all__ = ["display_html_to_text", "has_display_html"]
