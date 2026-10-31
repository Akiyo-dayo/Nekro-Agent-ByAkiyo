"""心声卡片的排版与绘制

一张柔和的心理便签卡：微渐变底 + 左上角淡玫瑰色光晕，图标是一枚渐变爱心，
标题「心声」下挂一行小字 INNER VOICE，右侧是定宽心电示意、末端脉搏光点与拟态心率，
正文落在带左侧强调条的引用面板里。

## 高度是算出来的，不是定死的

正文由模型生成，长度不可控。卡片先按固定宽度把正文折行，数出行数，再据此推出整张图的
高度——先定高度再往里塞文字，一定会出现要么下方大片留白、要么正文溢出画布的结果。

## 正文是不可信内容

正文来自 LLM，`prepare_body_text` 会先经 `tools/untrusted_content.sanitize_block_text`
剥掉控制字符并封顶长度。这段文本**不会回注到后续提示词**（卡片以图片形式发出，
聊天历史里只留下一个 `<Image:...>` 标记），因此不需要再包会话令牌边界标签；
清洗仍然要做，因为控制字符同样会破坏图片排版。

## 情绪只改色相，不改结构

calm 与 agitated 共用同一套排版，只切换强调色（图标渐变、心电波形、心率数字、
强调条、光晕）。结构随情绪跳动会让同一机器人的卡片看起来不像同一个东西。
"""

from __future__ import annotations

import io
import math
import random
from dataclasses import dataclass
from typing import List, Tuple

from PIL import Image, ImageDraw

from nekro_agent.tools.untrusted_content import sanitize_block_text

from .fonts import FontRole, load_font
from .painter import RGBA, corner_glow, draw_text, rounded_rect, wrap_text

# 正文落到卡片上之前的长度上限。模型偶尔会不受控地长篇大论，
# 心声这个体裁本身就该是一两句话，超出的部分对读者没有价值。
MAX_BODY_LENGTH = 200

# 正文最多占几行；超出以省略号收尾
MAX_BODY_LINES = 8

# 逻辑宽度（未乘缩放）。参考稿的观感就是 520 左右
CARD_WIDTH = 520

# 拟态情绪装饰，不是生理测量。固定时间窗使 R-R 间距与标注心率一致。
ECG_WINDOW_SECONDS = 3.0

# 内部渲染超采样倍率：PIL 的线/多边形/圆角描边没有抗锯齿，按 2 倍于输出的
# 尺寸渲染再 LANCZOS 缩回，矢量边缘才有平滑过渡。最终输出尺寸不变。
RENDER_SUPERSAMPLE = 2

RGB = Tuple[int, int, int]


def sample_heart_rate(emotion: str = "calm") -> int:
    return random.randint(100, 120) if emotion == "agitated" else random.randint(60, 100)


def sinus_points(
    heart_rate: int, left: float, right: float, center_y: float, amplitude: float,
) -> List[Tuple[float, float]]:
    """规律 P–QRS–T 示意；R 波间隔为 60 / bpm 秒，不引入随机心律不齐。"""
    if isinstance(heart_rate, bool) or not isinstance(heart_rate, int) or not 60 <= heart_rate <= 120:
        raise ValueError("拟态心率必须是 60–120 的整数")
    if right <= left:
        return []
    period = 60.0 / heart_rate
    # 各波相对于 R 峰的位置和宽度以秒计，快心率主要缩短舒张间隔。
    waves = ((-0.18, 0.025, 0.15), (-0.035, 0.009, -0.18),
             (0.0, 0.010, 1.0), (0.035, 0.012, -0.28), (0.23, 0.045, 0.27))
    peaks = [0.34 + k * period for k in range(-1, math.ceil(ECG_WINDOW_SECONDS / period) + 1)]
    samples = max(2, math.ceil((right - left) * 2))
    points = []
    for i in range(samples + 1):
        t = ECG_WINDOW_SECONDS * i / samples
        value = sum(height * math.exp(-0.5 * ((t - peak - offset) / width) ** 2)
                    for peak in peaks for offset, width, height in waves)
        points.append((left + (right - left) * i / samples, center_y - amplitude * value))
    return points


@dataclass(frozen=True)
class InnerVoiceTheme:
    """心声卡片配色

    与情绪无关的部分保持中性（底、正文、标题），与情绪相关的部分集中在
    accent 一组里——换情绪就是换这一组色，卡片骨架不动。
    """

    card_top: RGB = (255, 255, 255)
    card_bottom: RGB = (250, 247, 251)
    card_border: RGBA = (234, 229, 236, 255)
    title: RGBA = (43, 45, 56, 255)
    subtitle: RGBA = (168, 162, 175, 255)
    body_bg: RGBA = (247, 244, 249, 255)
    body_text: RGBA = (74, 72, 88, 255)
    rate_unit: RGBA = (154, 150, 164, 255)
    # 强调色一组：图标渐变两端、装饰光晕、心电波形与心率数字、正文强调条
    accent: RGBA = (213, 112, 126, 255)
    icon_top: RGB = (233, 158, 169)
    icon_bottom: RGB = (213, 112, 126)
    glow: RGB = (232, 176, 186)
    icon_glyph: RGBA = (255, 255, 255, 255)

    @staticmethod
    def for_emotion(emotion: str) -> "InnerVoiceTheme":
        """激动时整体色相转向更暖的珊瑚红，其余结构与平静一致"""
        if emotion == "agitated":
            return InnerVoiceTheme(
                accent=(222, 88, 96, 255),
                icon_top=(240, 143, 133),
                icon_bottom=(222, 88, 96),
                glow=(242, 165, 155),
                body_bg=(250, 243, 244, 255),
            )
        return InnerVoiceTheme()


@dataclass(frozen=True)
class InnerVoiceLayout:
    """一张心声卡片的排版结果

    把"算尺寸"和"画像素"分开：尺寸计算是纯函数，可以直接用表驱动用例断言，
    不需要为了验证换行是否正确去比对图片像素。
    """

    lines: Tuple[str, ...]
    width: int
    height: int
    body_top: int
    body_height: int


def prepare_body_text(raw: str) -> str:
    """清洗模型产出的心声正文

    Returns:
        清洗后的正文；全部是空白或控制字符时返回空串，调用方据此放弃发送
    """
    return sanitize_block_text(raw, MAX_BODY_LENGTH)


def layout_inner_voice(body: str, *, scale: int = 2) -> InnerVoiceLayout:
    """计算心声卡片的排版

    Raises:
        ValueError: 正文为空。空卡片没有任何意义，宁可不发
    """
    if not body.strip():
        raise ValueError("心声正文为空")

    metrics = _Metrics(scale)
    font_body = load_font(FontRole.REGULAR, metrics.body_size)

    # 无字形的字符会在绘制时被丢掉，整段都画不出来时卡片会是一片空白——
    # 折行阶段看不出这一点（它照样能数出"一行"），必须在这里先拦掉
    if not font_body.renderable(body).strip():
        raise ValueError("心声正文无可渲染字符")

    text_width = metrics.width - metrics.pad * 2 - metrics.body_text_left - metrics.body_text_right
    lines = wrap_text(body, font_body, text_width, max_lines=MAX_BODY_LINES)
    if not lines:
        raise ValueError("心声正文无可渲染字符")

    body_inner = int(round(len(lines) * metrics.body_line_height))
    body_height = body_inner + metrics.body_pad_y * 2
    body_top = metrics.pad + metrics.icon_size + metrics.head_gap
    height = body_top + body_height + metrics.pad

    return InnerVoiceLayout(
        lines=tuple(lines),
        width=metrics.width,
        height=height,
        body_top=body_top,
        body_height=body_height,
    )


def render_inner_voice_card(
    body: str,
    *,
    title: str = "心声",
    scale: int = 2,
    theme: InnerVoiceTheme | None = None,
    emotion: str = "calm",
    heart_rate: int | None = None,
) -> bytes:
    """渲染心声卡片

    Args:
        body: 已经过 `prepare_body_text` 清洗的正文
        title: 标题文字，绘制时会拉开字距
        scale: 输出倍率。聊天客户端会把图片缩小显示，2 倍下文字边缘才够干净
        theme: 自定义配色；默认按 `emotion` 取色
        emotion: calm 或 agitated，遗漏或未知值按平静处理
        heart_rate: 测试/预览可指定 60–120 的整数；默认按情绪档随机抽取

    Returns:
        PNG 字节流

    Raises:
        ValueError: 正文为空
        RuntimeError: 环境缺少可用中文字体
    """
    palette = theme or InnerVoiceTheme.for_emotion(emotion)
    # 排版（折行与画布尺寸）按请求的 scale 推导，输出尺寸与 layout_inner_voice
    # 的结果严格一致——这是对外契约，调用方可以先用 layout 占位再等图。
    # 像素绘制在 RENDER_SUPERSAMPLE 倍画布上进行：PIL 的线、多边形、圆角描边
    # 都不带抗锯齿，超采样渲染再 LANCZOS 缩回，矢量边缘才有平滑过渡。
    layout = layout_inner_voice(body, scale=scale)
    ss = RENDER_SUPERSAMPLE
    render_scale = scale * ss
    metrics = _Metrics(render_scale)
    canvas_width = layout.width * ss
    canvas_height = layout.height * ss
    body_top = layout.body_top * ss
    body_height = layout.body_height * ss

    image = _card_base(canvas_width, canvas_height, metrics.card_radius, palette,
                       border_width=max(1, render_scale))
    draw = ImageDraw.Draw(image)

    icon_box = (
        float(metrics.pad),
        float(metrics.pad),
        float(metrics.pad + metrics.icon_size),
        float(metrics.pad + metrics.icon_size),
    )
    _draw_icon(image, draw, icon_box, metrics.icon_radius, palette)
    icon_center_y = metrics.pad + metrics.icon_size / 2

    # 标题块：「心 声」+ 一行小字 INNER VOICE，整体与图标垂直居中对齐
    font_title = load_font(FontRole.BOLD, metrics.title_size)
    font_caps = load_font(FontRole.REGULAR, metrics.caps_size)
    title_x = metrics.pad + metrics.icon_size + metrics.title_gap
    title_ascent, title_descent = font_title.primary.getmetrics()
    caps_ascent, caps_descent = font_caps.primary.getmetrics()
    block_height = (title_ascent + title_descent) + metrics.caps_gap + (caps_ascent + caps_descent)
    block_top = icon_center_y - block_height / 2
    draw_text(
        draw,
        (title_x, block_top),
        title,
        font_title,
        palette.title,
        letter_spacing=metrics.title_spacing,
    )
    draw_text(
        draw,
        (title_x, block_top + title_ascent + title_descent + metrics.caps_gap),
        "INNER VOICE",
        font_caps,
        palette.subtitle,
        letter_spacing=metrics.caps_spacing,
    )

    # 右侧一组：定宽心电示意 + 末端脉搏光点 +「粗体数字 + 小字单位」，基线对齐
    bpm = sample_heart_rate(emotion) if heart_rate is None else heart_rate
    font_num = load_font(FontRole.BOLD, metrics.rate_num_size)
    font_unit = load_font(FontRole.REGULAR, metrics.rate_unit_size)
    num_text = str(bpm)
    num_width = font_num.measure(num_text)
    unit_width = font_unit.measure("bpm")
    cluster_right = canvas_width - metrics.pad

    num_ascent, num_descent = font_num.primary.getmetrics()
    unit_ascent, _ = font_unit.primary.getmetrics()
    rate_baseline = icon_center_y + (num_ascent - num_descent) / 2
    num_left = cluster_right - num_width - metrics.rate_unit_gap - unit_width
    draw_text(draw, (num_left, rate_baseline - num_ascent), num_text, font_num, palette.accent)
    draw_text(draw, (num_left + num_width + metrics.rate_unit_gap, rate_baseline - unit_ascent),
              "bpm", font_unit, palette.rate_unit)

    wave_right = num_left - metrics.rate_wave_gap
    wave_left = wave_right - metrics.wave_width
    if wave_left > title_x:
        points = sinus_points(bpm, wave_left, wave_right, icon_center_y, metrics.wave_amplitude)
        if points:
            draw.line(points, fill=palette.accent, width=max(1, 2 * render_scale), joint="curve")
            # 末端的脉搏光点：让波形读起来是"正在跳动"而不是一段静态装饰
            dot_x, dot_y = points[-1]
            dot_r = metrics.pulse_dot_radius
            draw.ellipse((dot_x - dot_r, dot_y - dot_r, dot_x + dot_r, dot_y + dot_r),
                         fill=palette.accent)

    # 正文面板：浅底圆角 + 左侧强调条，引用块的观感
    body_box = (
        float(metrics.pad),
        float(body_top),
        float(canvas_width - metrics.pad),
        float(body_top + body_height),
    )
    rounded_rect(draw, body_box, metrics.body_radius, fill=palette.body_bg)
    bar_box = (
        float(metrics.pad + metrics.body_bar_inset),
        float(body_top + metrics.body_bar_inset_y),
        float(metrics.pad + metrics.body_bar_inset + metrics.body_bar_width),
        float(body_top + body_height - metrics.body_bar_inset_y),
    )
    rounded_rect(draw, bar_box, metrics.body_bar_width / 2, fill=palette.accent)

    font_body = load_font(FontRole.REGULAR, metrics.body_size)
    text_x = metrics.pad + metrics.body_text_left
    text_top = body_top + metrics.body_pad_y
    for index, line in enumerate(layout.lines):
        draw_text(
            draw,
            (text_x, text_top + index * metrics.body_line_height),
            line,
            font_body,
            palette.body_text,
        )

    if RENDER_SUPERSAMPLE > 1:
        image = image.resize(
            (layout.width, layout.height),
            Image.Resampling.LANCZOS,
        )

    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _card_base(
    width: int,
    height: int,
    radius: int,
    palette: InnerVoiceTheme,
    *,
    border_width: int = 1,
) -> Image.Image:
    """微渐变底 + 左上角淡色光晕，裁出圆角卡片轮廓"""
    card = _vertical_gradient((width, height), palette.card_top, palette.card_bottom)
    card = card.convert("RGBA")
    card.alpha_composite(corner_glow((width, height), palette.glow, reach=0.6, max_alpha=36))

    mask = Image.new("L", (width, height), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    image.paste(card, (0, 0), mask)
    rounded_rect(
        ImageDraw.Draw(image),
        (0, 0, width - 1, height - 1),
        radius,
        outline=palette.card_border,
        width=border_width,
    )
    return image


def _vertical_gradient(size: Tuple[int, int], top: RGB, bottom: RGB) -> Image.Image:
    """竖直双色渐变。先在 1×256 的窄条上插值再放大，避免逐像素 Python 循环。"""
    seed = Image.new("RGB", (1, 256))
    pixels = seed.load()
    if pixels is None:  # pragma: no cover
        return Image.new("RGB", size, top)
    for i in range(256):
        t = i / 255
        pixels[0, i] = tuple(int(round(top[c] + (bottom[c] - top[c]) * t)) for c in range(3))
    return seed.resize(size, Image.Resampling.BICUBIC)


def _draw_icon(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    icon_box: Tuple[float, float, float, float],
    radius: int,
    palette: InnerVoiceTheme,
) -> None:
    """渐变圆角方块里的白色爱心

    渐变先画在独立图层上再按圆角蒙版贴入，心形用两个半圆加一个倒三角拼出，
    尺寸全部按图标框推导，调图标大小不需要同步改魔数。
    """
    left, top, right, bottom = icon_box
    size = int(round(right - left))
    gradient = _vertical_gradient((size, size), palette.icon_top, palette.icon_bottom).convert("RGBA")

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    image.paste(gradient, (int(left), int(top)), mask)

    cx = (left + right) / 2
    cy = (top + bottom) / 2
    w = size * 0.52
    r = w / 4
    circle_top = cy - size * 0.17
    for sign in (-1, 1):
        draw.ellipse(
            (cx + sign * r - r, circle_top - r, cx + sign * r + r, circle_top + r),
            fill=palette.icon_glyph,
        )
    draw.polygon(
        [
            (cx - r * 1.92, circle_top + r * 0.28),
            (cx + r * 1.92, circle_top + r * 0.28),
            (cx, circle_top + w * 0.78),
        ],
        fill=palette.icon_glyph,
    )


class _Metrics:
    """把逻辑尺寸乘上缩放倍率后的一整套度量"""

    def __init__(self, scale: int) -> None:
        if scale < 1:
            raise ValueError(f"缩放倍率必须不小于 1: {scale}")
        self.scale = scale
        self.width = CARD_WIDTH * scale
        self.pad = 24 * scale
        self.icon_size = 40 * scale
        self.icon_radius = 12 * scale
        self.card_radius = 18 * scale
        self.title_size = 20 * scale
        self.title_gap = 13 * scale
        self.title_spacing = 6.0 * scale
        self.caps_size = int(8.5 * scale)
        self.caps_gap = 3 * scale
        self.caps_spacing = 2.4 * scale
        self.head_gap = 18 * scale
        self.body_radius = 14 * scale
        self.body_pad_y = 15 * scale
        self.body_size = 15 * scale
        # 强调条：距面板左缘 10、上下各收 12；正文从条右再让 12
        self.body_bar_inset = 10 * scale
        self.body_bar_inset_y = 12 * scale
        self.body_bar_width = 4 * scale
        self.body_text_left = (10 + 4 + 12) * scale
        self.body_text_right = 16 * scale
        # 心率读数：数字 17pt 粗体强调、单位 9pt 灰色，读数与波形之间留 12pt 呼吸位
        self.rate_num_size = 17 * scale
        self.rate_unit_size = 9 * scale
        self.rate_unit_gap = 3 * scale
        self.rate_wave_gap = 12 * scale
        # 心电示意的定宽区间、振幅与末端光点（逻辑值乘倍率）
        self.wave_width = 92 * scale
        self.wave_amplitude = 10 * scale
        self.pulse_dot_radius = 2.2 * scale

    @property
    def body_line_height(self) -> float:
        return self.body_size * 1.75
