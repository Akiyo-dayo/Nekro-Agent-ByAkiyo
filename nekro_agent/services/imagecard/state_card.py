"""角色状态卡片（状态图）

只吃一个已经整理好的数据类 :class:`StateCardData`：去掉隐藏字段、私密字段与别人的档案是调用方（角色状态
插件的 `view`）的事，这里拿不到状态树，只管排版。卡片里的任何字符串都当纯文本画：不解析标记、不展开宏、
不取任何外部资源。

版式（`tavern-html-render-design.md` §3.3）：页头是标题（人设名）与几项页头字段（时间、地点一类），下面
按组从上往下排，每组一个小标题、若干行「名字：值」，嵌套的往里缩。不做标签页。行数与单值长度都有上限，
超了的由调用方在页脚写一句。
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from math import ceil
from typing import Tuple

from PIL import Image, ImageChops, ImageDraw

from .fonts import FontRole, LayeredFont, load_font
from .painter import RGBA, corner_glow, draw_text, ellipsize, rounded_rect, wrap_text

CARD_WIDTH = 560
MAX_ROWS = 40
MAX_HEADLINE = 4
MAX_VALUE_CHARS = 120
MAX_LABEL_CHARS = 32
MAX_VALUE_LINES = 3
MAX_DEPTH = 2
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u2028-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")


@dataclass(frozen=True)
class StateCardRow:
    """一行：名字与值（都是已经过滤好的纯文本）；`depth` 是缩进层级（0 ~ 2）"""

    label: str
    value: str = ""
    depth: int = 0


@dataclass(frozen=True)
class StateCardSection:
    title: str
    rows: Tuple[StateCardRow, ...] = ()


@dataclass(frozen=True)
class StateCardData:
    """一张状态图的全部内容；只有字符串与它们的组合，没有状态树"""

    title: str
    subtitle: str = ""
    headline: Tuple[StateCardRow, ...] = ()
    sections: Tuple[StateCardSection, ...] = ()
    footer: str = ""
    theme: str = "default"


@dataclass(frozen=True)
class StateCardTheme:
    card_bg: RGBA
    card_border: RGBA
    glow: Tuple[int, int, int]
    accent: RGBA
    title: RGBA
    subtitle: RGBA
    chip_bg: RGBA
    chip_text: RGBA
    section: RGBA
    label: RGBA
    value: RGBA
    footer: RGBA
    separator: RGBA = field(default=(236, 238, 242, 255))


STATE_CARD_THEMES: dict[str, StateCardTheme] = {
    "default": StateCardTheme(
        card_bg=(255, 255, 255, 255),
        card_border=(222, 226, 240, 255),
        glow=(120, 132, 220),
        accent=(104, 116, 214, 255),
        title=(40, 46, 92, 255),
        subtitle=(150, 154, 172, 255),
        chip_bg=(240, 242, 252, 255),
        chip_text=(70, 80, 160, 255),
        section=(60, 66, 120, 255),
        label=(130, 134, 156, 255),
        value=(44, 48, 62, 255),
        footer=(160, 164, 180, 255),
    ),
    "night": StateCardTheme(
        card_bg=(30, 33, 46, 255),
        card_border=(58, 63, 86, 255),
        glow=(110, 120, 220),
        accent=(140, 152, 255, 255),
        title=(232, 234, 250, 255),
        subtitle=(150, 156, 186, 255),
        chip_bg=(48, 53, 76, 255),
        chip_text=(200, 206, 255, 255),
        section=(200, 206, 255, 255),
        label=(140, 146, 176, 255),
        value=(226, 228, 240, 255),
        footer=(120, 126, 156, 255),
        separator=(52, 57, 78, 255),
    ),
    "sakura": StateCardTheme(
        card_bg=(255, 252, 253, 255),
        card_border=(244, 214, 224, 255),
        glow=(236, 140, 170),
        accent=(222, 110, 150, 255),
        title=(120, 46, 76, 255),
        subtitle=(176, 150, 160, 255),
        chip_bg=(252, 236, 242, 255),
        chip_text=(176, 74, 112, 255),
        section=(150, 60, 96, 255),
        label=(176, 140, 152, 255),
        value=(70, 46, 56, 255),
        footer=(186, 160, 170, 255),
    ),
    "forest": StateCardTheme(
        card_bg=(255, 255, 255, 255),
        card_border=(214, 234, 217, 255),
        glow=(76, 175, 108),
        accent=(60, 160, 100, 255),
        title=(26, 94, 55, 255),
        subtitle=(150, 160, 154, 255),
        chip_bg=(234, 246, 237, 255),
        chip_text=(52, 130, 84, 255),
        section=(34, 106, 64, 255),
        label=(140, 156, 146, 255),
        value=(46, 56, 50, 255),
        footer=(160, 170, 164, 255),
    ),
    "ink": StateCardTheme(
        card_bg=(250, 248, 243, 255),
        card_border=(222, 216, 204, 255),
        glow=(150, 140, 120),
        accent=(90, 84, 72, 255),
        title=(40, 36, 30, 255),
        subtitle=(150, 144, 132, 255),
        chip_bg=(240, 236, 226, 255),
        chip_text=(80, 72, 60, 255),
        section=(60, 54, 44, 255),
        label=(140, 132, 118, 255),
        value=(44, 40, 34, 255),
        footer=(160, 152, 138, 255),
    ),
}


def plain(text: str, limit: int) -> str:
    """当纯文本画：去控制字符与零宽字符，换行换成空格，超长截断"""
    cleaned = _CONTROL.sub("", str(text)).replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()
    return cleaned if len(cleaned) <= limit else cleaned[: max(0, limit - 1)] + "…"


def card_text(data: StateCardData, limit: int) -> str:
    """文字版（命令的文字输出、缺字体时的退路）；超过 `limit` 字时截断并说明"""
    lines = [f"【{plain(data.title, 40)}】"]
    if data.headline:
        lines.append("  ".join(f"{plain(row.label, MAX_LABEL_CHARS)}：{plain(row.value, 60)}" for row in data.headline))
    for section in data.sections:
        lines.append(f"■ {plain(section.title, MAX_LABEL_CHARS)}")
        for row in section.rows:
            pad = "  " * (min(row.depth, MAX_DEPTH) + 1)
            label = plain(row.label, MAX_LABEL_CHARS)
            value = plain(row.value, MAX_VALUE_CHARS)
            lines.append(f"{pad}{label}：{value}" if label and value else f"{pad}{label or value}")
    if data.footer:
        lines.append(f"（{plain(data.footer, 80)}）")
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)].rstrip() + "\n…（太长了，后面没写）"


class _Metrics:
    def __init__(self, scale: int) -> None:
        if scale < 1:
            raise ValueError(f"缩放倍率必须不小于 1: {scale}")
        self.scale = scale
        self.width = CARD_WIDTH * scale
        self.pad = 24 * scale
        self.radius = 18 * scale
        self.hairline = max(1, scale)
        self.title_size = 22 * scale
        self.subtitle_size = 12 * scale
        self.chip_size = 13 * scale
        self.chip_pad_x = 10 * scale
        self.chip_pad_y = 5 * scale
        self.chip_gap = 8 * scale
        self.section_size = 15 * scale
        self.section_gap = 14 * scale
        self.text_size = 13 * scale
        self.line_height = 21 * scale
        self.row_gap = 4 * scale
        self.indent = 16 * scale
        self.label_width = 120 * scale
        self.footer_size = 11 * scale
        self.accent_width = 4 * scale


@dataclass
class _Fonts:
    title: LayeredFont
    subtitle: LayeredFont
    chip: LayeredFont
    section: LayeredFont
    label: LayeredFont
    value: LayeredFont
    footer: LayeredFont


def _fonts(metrics: _Metrics) -> _Fonts:
    return _Fonts(
        title=load_font(FontRole.BOLD, metrics.title_size),
        subtitle=load_font(FontRole.REGULAR, metrics.subtitle_size),
        chip=load_font(FontRole.REGULAR, metrics.chip_size),
        section=load_font(FontRole.BOLD, metrics.section_size),
        label=load_font(FontRole.REGULAR, metrics.text_size),
        value=load_font(FontRole.REGULAR, metrics.text_size),
        footer=load_font(FontRole.REGULAR, metrics.footer_size),
    )


@dataclass
class _Item:
    kind: str  # chip / section / row / footer
    top: float
    height: float
    text: str = ""
    label: str = ""
    lines: Tuple[str, ...] = ()
    left: float = 0.0
    width: float = 0.0


def _layout(data: StateCardData, metrics: _Metrics, fonts: _Fonts) -> tuple[list[_Item], int]:
    """先排好每一项的位置，再一次画；返回 (各项, 总高度)"""
    items: list[_Item] = []
    inner_width = metrics.width - metrics.pad * 2
    y = metrics.pad + metrics.title_size * 1.4
    if data.subtitle:
        y += metrics.subtitle_size * 1.6

    # 页头字段：一排小块，放不下就换行
    if data.headline:
        y += metrics.chip_gap
        x = float(metrics.pad)
        chip_height = metrics.chip_size + metrics.chip_pad_y * 2
        for row in data.headline[:MAX_HEADLINE]:
            text = f"{plain(row.label, MAX_LABEL_CHARS)} · {plain(row.value, 40)}"
            text = ellipsize(text, fonts.chip, inner_width - metrics.chip_pad_x * 2)
            width = fonts.chip.measure(text) + metrics.chip_pad_x * 2
            if x + width > metrics.pad + inner_width and x > metrics.pad:
                x = float(metrics.pad)
                y += chip_height + metrics.chip_gap
            items.append(_Item("chip", y, chip_height, text=text, left=x, width=width))
            x += width + metrics.chip_gap
        y += chip_height

    rows_left = MAX_ROWS
    for section in data.sections:
        if rows_left <= 0:
            break
        y += metrics.section_gap
        title = ellipsize(plain(section.title, MAX_LABEL_CHARS), fonts.section, inner_width - metrics.indent)
        items.append(_Item("section", y, metrics.section_size * 1.5, text=title))
        y += metrics.section_size * 1.5 + metrics.row_gap
        for row in section.rows:
            if rows_left <= 0:
                break
            rows_left -= 1
            depth = min(max(row.depth, 0), MAX_DEPTH)
            left = metrics.pad + metrics.indent * (depth + 1)
            label = plain(row.label, MAX_LABEL_CHARS)
            value = plain(row.value, MAX_VALUE_CHARS)
            if label and value:
                label_text = ellipsize(label, fonts.label, metrics.label_width)
                value_left = left + metrics.label_width + metrics.chip_gap
                lines = tuple(
                    wrap_text(value, fonts.value, metrics.width - metrics.pad - value_left, max_lines=MAX_VALUE_LINES)
                )
            else:
                label_text = ""
                value_left = left
                lines = tuple(
                    wrap_text(
                        label or value, fonts.value, metrics.width - metrics.pad - left, max_lines=MAX_VALUE_LINES
                    )
                )
            height = metrics.line_height * max(1, len(lines))
            items.append(_Item("row", y, height, label=label_text, lines=lines or ("",), left=left, width=value_left))
            y += height + metrics.row_gap

    if data.footer:
        y += metrics.section_gap
        text = ellipsize(plain(data.footer, 80), fonts.footer, inner_width)
        items.append(_Item("footer", y, metrics.footer_size * 1.5, text=text))
        y += metrics.footer_size * 1.5
    return items, int(ceil(y + metrics.pad))


def render_state_card(data: StateCardData, *, scale: int = 2) -> bytes:
    """画一张状态图，返回 PNG 字节流

    Raises:
        TypeError: 传进来的不是 `StateCardData`（渲染器只吃过滤好的数据，不吃状态树）
        RuntimeError: 环境缺少可用中文字体
    """
    if not isinstance(data, StateCardData):
        raise TypeError("状态图只收 StateCardData")
    palette = STATE_CARD_THEMES.get(data.theme) or STATE_CARD_THEMES["default"]
    metrics = _Metrics(scale)
    fonts = _fonts(metrics)
    items, height = _layout(data, metrics, fonts)
    width = metrics.width

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    rounded_rect(
        draw,
        (0, 0, width - 1, height - 1),
        metrics.radius,
        fill=palette.card_bg,
        outline=palette.card_border,
        width=metrics.hairline,
    )
    glow = corner_glow((width, height), palette.glow, reach=0.5, max_alpha=22)
    mask = Image.new("L", (width, height), 0)
    rounded_rect(ImageDraw.Draw(mask), (0, 0, width - 1, height - 1), metrics.radius, fill=(255,))
    image.paste(glow, (0, 0), ImageChops.multiply(glow.getchannel("A"), mask))

    inner_width = width - metrics.pad * 2
    draw_text(
        draw,
        (metrics.pad, metrics.pad),
        ellipsize(plain(data.title, 40), fonts.title, inner_width),
        fonts.title,
        palette.title,
    )
    if data.subtitle:
        draw_text(
            draw,
            (metrics.pad, metrics.pad + metrics.title_size * 1.4),
            ellipsize(plain(data.subtitle, 60), fonts.subtitle, inner_width),
            fonts.subtitle,
            palette.subtitle,
        )

    for item in items:
        if item.kind == "chip":
            rounded_rect(
                draw,
                (item.left, item.top, item.left + item.width, item.top + item.height),
                item.height / 2,
                fill=palette.chip_bg,
            )
            draw_text(
                draw,
                (item.left + metrics.chip_pad_x, item.top + metrics.chip_pad_y),
                item.text,
                fonts.chip,
                palette.chip_text,
            )
        elif item.kind == "section":
            draw.rectangle(
                (
                    metrics.pad,
                    item.top + metrics.scale * 3,
                    metrics.pad + metrics.accent_width,
                    item.top + metrics.section_size * 1.2,
                ),
                fill=palette.accent,
            )
            draw_text(draw, (metrics.pad + metrics.indent, item.top), item.text, fonts.section, palette.section)
        elif item.kind == "row":
            if item.label:
                draw_text(draw, (item.left, item.top), item.label, fonts.label, palette.label)
            for index, line in enumerate(item.lines):
                draw_text(draw, (item.width, item.top + index * metrics.line_height), line, fonts.value, palette.value)
        elif item.kind == "footer":
            draw_text(draw, (metrics.pad, item.top), item.text, fonts.footer, palette.footer)

    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


__all__ = [
    "MAX_HEADLINE",
    "MAX_ROWS",
    "MAX_VALUE_CHARS",
    "STATE_CARD_THEMES",
    "StateCardData",
    "StateCardRow",
    "StateCardSection",
    "card_text",
    "plain",
    "render_state_card",
]
