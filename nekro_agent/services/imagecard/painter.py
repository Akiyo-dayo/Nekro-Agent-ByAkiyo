"""图片卡片的绘制原语与文本排版

本模块只关心"怎么把一段文本画到画布上"，不知道任何一张具体卡片长什么样。
两张卡片（心声、好感度排行）共用这里的换行、截断、字体链绘制与圆角矩形。

## 排版为什么要自己写

`ImageDraw.text` 的多行支持只按 `\\n` 拆分，既不会按宽度折行，也不认识中文——
中文可以在任意两个字之间断行，英文单词却不能从中间劈开。卡片上的昵称与心声正文
都是用户/模型产出的任意文本，没有一个按像素宽度工作的折行器，超长内容就会直接
画出画布之外（画布不会报错，只是内容凭空消失）。

## 坐标约定

对外的纵坐标一律是**文本框顶端**，不是基线。基线位置由字体的 ascent 决定，
不同字体不一样；调用方按顶端排版才能把行与行之间的间距算对。
"""

from __future__ import annotations

import re
from typing import List, Literal, Optional, Sequence, Tuple

from PIL import Image, ImageDraw

from .fonts import LayeredFont

RGBA = Tuple[int, int, int, int]
Align = Literal["left", "center", "right"]

ELLIPSIS = "…"

# 不允许出现在行首的标点：中文标点跑到下一行行首是最刺眼的排版错误之一
_NO_LINE_START = "，。、；：？！）】》」』%,.;:?!)]}>"

# 连续的 ASCII 单词（字母/数字/常见词内符号）视为一个不可拆分的整体
_ASCII_WORD = re.compile(r"[0-9A-Za-z@#$&_\-+='\"/\\.]+")


def wrap_text(
    text: str,
    font: LayeredFont,
    max_width: float,
    *,
    max_lines: Optional[int] = None,
) -> List[str]:
    """按像素宽度折行

    中文逐字断行，ASCII 单词整体断行；显式换行符始终生效。
    超出 `max_lines` 的内容会在最后一行末尾以省略号收尾，而不是被静默丢掉——
    读者需要知道"这里还有没显示完的内容"。

    Args:
        text: 待排版文本，已假定经过控制字符清洗
        font: 字体链
        max_width: 单行最大像素宽度
        max_lines: 最大行数；None 表示不限制

    Returns:
        每个元素是一行，可能为空列表（入参为空时）
    """
    if max_width <= 0:
        return []

    lines: List[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        lines.extend(_wrap_paragraph(paragraph, font, max_width))

    if max_lines is not None and len(lines) > max_lines:
        kept = lines[:max_lines]
        if kept:
            kept[-1] = ellipsize(kept[-1] + ELLIPSIS, font, max_width)
        lines = kept
    return lines


def _wrap_paragraph(paragraph: str, font: LayeredFont, max_width: float) -> List[str]:
    lines: List[str] = []
    current = ""
    for token in _tokenize(paragraph):
        # 续行不以空格开头：折行位置本来就是一个视觉停顿，再加一个空格只会让左边缘参差不齐
        if token.isspace() and not current and lines:
            continue
        if font.measure(current + token) <= max_width:
            current += token
            continue

        pending = token
        if current and token[0] in _NO_LINE_START:
            # 行首禁则：宁可让上一行略微超宽，也不让标点跑到下一行行首。
            # 这一步是有意突破 max_width 的，所以不能再对结果做一次硬切
            lines.append(current + token[0])
            current = ""
            pending = token[1:]
        elif current:
            lines.append(current)
            current = ""
            pending = token.lstrip(" ")

        if not pending:
            continue
        # 走到这里说明单个 token 自己就超宽（例如一长串没有空格的字母），只能逐字硬切
        pieces = _hard_split(pending, font, max_width)
        lines.extend(pieces[:-1])
        current = pieces[-1]

    if current:
        lines.append(current)
    return lines


def _tokenize(paragraph: str) -> List[str]:
    tokens: List[str] = []
    index = 0
    while index < len(paragraph):
        match = _ASCII_WORD.match(paragraph, index)
        if match:
            tokens.append(match.group())
            index = match.end()
            continue
        tokens.append(paragraph[index])
        index += 1
    return tokens


def _hard_split(line: str, font: LayeredFont, max_width: float) -> List[str]:
    if font.measure(line) <= max_width:
        return [line]
    pieces: List[str] = []
    current = ""
    for char in line:
        if current and font.measure(current + char) > max_width:
            pieces.append(current)
            current = char
        else:
            current += char
    if current:
        pieces.append(current)
    return pieces


def ellipsize(text: str, font: LayeredFont, max_width: float) -> str:
    """把文本压到指定宽度内，超出部分以省略号收尾

    昵称是用户可控内容，一个几百字的昵称必须在这里被截断，
    否则排行榜的一列会被它一个人撑满。
    """
    if max_width <= 0:
        return ""
    if font.measure(text) <= max_width:
        return text

    ellipsis_width = font.measure(ELLIPSIS)
    if ellipsis_width > max_width:
        return ""

    budget = max_width - ellipsis_width
    kept = ""
    for char in text:
        if font.measure(kept + char) > budget:
            break
        kept += char
    return (kept + ELLIPSIS) if kept else ELLIPSIS


def draw_text(
    draw: ImageDraw.ImageDraw,
    xy: Tuple[float, float],
    text: str,
    font: LayeredFont,
    fill: RGBA,
    *,
    align: Align = "left",
    letter_spacing: float = 0.0,
) -> float:
    """在文本框顶端坐标处绘制单行文本

    逐段切换字体绘制，所有段落对齐到同一条基线——emoji 字体与中文字体的
    ascent 通常不同，不对齐基线就会出现 emoji 上下乱跳。

    Args:
        xy: `align` 为 left 时是左上角，right 时是右上角，center 时是顶边中点
        letter_spacing: 字间距，用于"心 声"这类需要拉开字距的标题

    Returns:
        实际绘制宽度
    """
    runs = font.split(text)
    if not runs:
        return 0.0

    width = font.measure(text, letter_spacing)
    x, top = xy
    if align == "right":
        x -= width
    elif align == "center":
        x -= width / 2

    ascent, _ = font.primary.getmetrics()
    baseline = top + ascent

    for run in runs:
        if letter_spacing:
            for char in run.text:
                draw.text(
                    (x, baseline),
                    char,
                    font=run.font,
                    fill=fill,
                    anchor="ls",
                    embedded_color=run.embedded_color,
                )
                x += run.font.getlength(char) + letter_spacing
        else:
            draw.text(
                (x, baseline),
                run.text,
                font=run.font,
                fill=fill,
                anchor="ls",
                embedded_color=run.embedded_color,
            )
            x += run.font.getlength(run.text)
    return width


def draw_paragraph(
    draw: ImageDraw.ImageDraw,
    xy: Tuple[float, float],
    lines: Sequence[str],
    font: LayeredFont,
    fill: RGBA,
    *,
    line_height: float,
) -> float:
    """按固定行高绘制多行文本，返回占用的总高度"""
    x, top = xy
    for index, line in enumerate(lines):
        draw_text(draw, (x, top + index * line_height), line, font, fill)
    return line_height * len(lines)


def rounded_rect(
    draw: ImageDraw.ImageDraw,
    box: Tuple[float, float, float, float],
    radius: float,
    *,
    fill: Optional[RGBA] = None,
    outline: Optional[RGBA] = None,
    width: int = 1,
) -> None:
    """圆角矩形

    半径超过短边一半时会被收敛，避免 Pillow 画出自交的怪异形状。
    """
    left, top, right, bottom = box
    max_radius = max(0.0, min(right - left, bottom - top) / 2)
    draw.rounded_rectangle(
        (left, top, right, bottom),
        radius=min(radius, max_radius),
        fill=fill,
        outline=outline,
        width=width,
    )


def corner_glow(
    size: Tuple[int, int],
    color: Tuple[int, int, int],
    *,
    reach: float,
    max_alpha: int,
) -> Image.Image:
    """左上角向外衰减的淡色光晕

    逐像素算 alpha 在卡片尺寸下要跑上百万次 Python 循环，实测能到秒级；
    这里先在一张极小的图上算好衰减，再放大——视觉上完全一样，代价可以忽略。

    Args:
        reach: 光晕覆盖的对角比例，1.0 表示铺满整张图
        max_alpha: 左上角处的最大不透明度
    """
    width, height = size
    seed_size = 48
    seed = Image.new("L", (seed_size, seed_size))
    pixels = seed.load()
    if pixels is None:  # pragma: no cover - Pillow 在正常情况下不会返回 None
        return Image.new("RGBA", size, (*color, 0))

    span = max(reach, 1e-6) * (seed_size - 1)
    for y in range(seed_size):
        for x in range(seed_size):
            distance = (x * x + y * y) ** 0.5
            ratio = max(0.0, 1.0 - distance / span)
            pixels[x, y] = int(max_alpha * ratio * ratio)

    mask = seed.resize((width, height), Image.Resampling.BICUBIC)
    glow = Image.new("RGBA", size, (*color, 0))
    glow.putalpha(mask)
    return glow
