"""图片卡片的字体解析与字形回退

## 为什么不能只 `ImageFont.truetype("某个字体")` 了事

卡片上要渲染的几乎全是用户可控内容：群昵称里有 emoji、日文假名、生僻字、颜文字符号。
一旦某个字符在当前字体里没有对应字形，FreeType 会画出 `.notdef`——也就是俗称的"豆腐块"。
豆腐块不会报错、不会降级，只会安安静静地把一张卡片变成一排空心方框，是这类功能最典型的
线上事故形态。

因此本模块做两件事：

1. **按平台探测一条字体链**：中文主字体 → 彩色 emoji 字体 → 符号兜底字体。
   Docker 镜像（`python:3.11-slim-bookworm`）自身不带任何 CJK 字体，必须由 dockerfile
   安装 `fonts-noto-cjk`；候选路径表里同时列出 Noto / 文泉驿 / 微软雅黑 / 苹方，
   让开发机、服务器、容器三种环境都能自己找到可用字体。
2. **按字形覆盖切分文本**：用 fontTools 读出每个字体的 cmap，把一段文本切成若干"这一段
   由哪个字体画"的片段。三条链都覆盖不到的字符**直接丢弃**而不是硬画——
   宁可少一个字符，也绝不出现豆腐块。

fontTools 不是新增依赖：它是 matplotlib 的传递依赖，本仓库已经装着。

## 字号与缓存

`load_font()` 按 (角色, 字号) 缓存 `LayeredFont`。字体文件的 cmap 解析只在进程内做一次，
一个 cmap 表几万个码位，解析一次约几十毫秒，缓存后可以忽略。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from fontTools.ttLib import TTCollection, TTFont
from PIL import ImageFont

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv

logger = get_sub_logger("imagecard")

# 自带彩色位图/矢量色表的字体，绘制时要开 embedded_color 才是彩色而不是黑块
_COLOR_FONT_TABLES = ("CBDT", "sbix", "COLR")

# 用户自备字体的存放目录：放进去的 .ttf/.otf/.ttc 优先级最高，
# 用于内网机器没有系统中文字体、或运营方想统一卡片字形的场景。
CUSTOM_FONT_DIR = Path(OsEnv.DATA_DIR) / "fonts"


class FontRole(StrEnum):
    """字体角色

    只区分常规与加粗两种：卡片设计里没有斜体，伪斜体在中文上又很难看。
    """

    REGULAR = "regular"
    BOLD = "bold"


@dataclass(frozen=True)
class FontSpec:
    """一个具体的字体面

    `.ttc` 是字体集合，同一个文件里装着多个字体面，必须用 `index` 指定取哪一个。
    """

    path: str
    index: int = 0


# 中文主字体候选，按优先级排列。
# Linux 侧的路径对应 Debian/Ubuntu 的 fonts-noto-cjk 与 fonts-wqy-* 包。
_CJK_CANDIDATES: Dict[FontRole, Tuple[FontSpec, ...]] = {
    FontRole.REGULAR: (
        FontSpec("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        FontSpec("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
        FontSpec("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        FontSpec("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
        FontSpec("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        FontSpec("C:/Windows/Fonts/msyh.ttc"),
        FontSpec("C:/Windows/Fonts/simhei.ttf"),
        FontSpec("C:/Windows/Fonts/simsun.ttc"),
        FontSpec("/System/Library/Fonts/PingFang.ttc"),
        FontSpec("/System/Library/Fonts/STHeiti Medium.ttc"),
    ),
    FontRole.BOLD: (
        FontSpec("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        FontSpec("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf"),
        FontSpec("/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc"),
        FontSpec("C:/Windows/Fonts/msyhbd.ttc"),
        FontSpec("C:/Windows/Fonts/simhei.ttf"),
        FontSpec("/System/Library/Fonts/PingFang.ttc", index=4),
    ),
}

# emoji 与符号兜底，按"能上色的优先、画得出来的兜底"排列。彩色 emoji 字体在不同平台
# 差异很大（Windows 是矢量 COLR，任意字号可用；Linux 的 Noto Color Emoji 是只有 109px
# 一档的 CBDT 位图，卡片字号下必然加载失败），因此这条链上的每一个候选都要保留：
# 彩色那层加载失败时，后面的单色符号字体仍然能把 ❤ ★ 画出来，只有整条链都覆盖不到的
# 字符才走"丢弃"。
_FALLBACK_CANDIDATES: Tuple[FontSpec, ...] = (
    FontSpec("C:/Windows/Fonts/seguiemj.ttf"),
    FontSpec("/System/Library/Fonts/Apple Color Emoji.ttc"),
    FontSpec("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
    FontSpec("/usr/share/fonts/truetype/ancient-scripts/Symbola_hint.ttf"),
    FontSpec("/usr/share/fonts/truetype/symbola/Symbola.ttf"),
    FontSpec("C:/Windows/Fonts/seguisym.ttf"),
)

# 空白字符即便字体里没有 cmap 条目也必须保留：它们不产生字形，
# 丢掉只会让"张 三"变成"张三"，而不会产生豆腐块。
_ALWAYS_KEEP = frozenset({0x20, 0x09})


@dataclass(frozen=True)
class TextRun:
    """一段由同一个字体绘制的连续文本"""

    text: str
    font: ImageFont.FreeTypeFont
    embedded_color: bool


class LayeredFont:
    """某一字号下的字体链

    对外只暴露三件事：把文本切成 `TextRun`、量宽度、给出行高。
    调用方不需要知道底下到底装了几层字体。
    """

    def __init__(self, layers: Sequence[Tuple[ImageFont.FreeTypeFont, frozenset[int], bool]], size: int) -> None:
        if not layers:
            raise RuntimeError("字体链为空")
        self._layers = list(layers)
        self.size = size

    @property
    def primary(self) -> ImageFont.FreeTypeFont:
        """主字体，用于取行高等与具体字符无关的度量"""
        return self._layers[0][0]

    def _layer_for(self, char: str) -> Optional[Tuple[ImageFont.FreeTypeFont, bool]]:
        code = ord(char)
        if code in _ALWAYS_KEEP:
            font, _, color = self._layers[0]
            return font, color
        for font, coverage, color in self._layers:
            if code in coverage:
                return font, color
        return None

    def split(self, text: str) -> List[TextRun]:
        """把文本切成若干"由同一个字体绘制"的片段

        任何一层都覆盖不到的字符会被丢弃，因此返回的片段拼起来可能短于入参。
        """
        runs: List[TextRun] = []
        for char in text:
            layer = self._layer_for(char)
            if layer is None:
                continue
            font, color = layer
            if runs and runs[-1].font is font:
                runs[-1] = TextRun(runs[-1].text + char, font, color)
            else:
                runs.append(TextRun(char, font, color))
        return runs

    def renderable(self, text: str) -> str:
        """丢掉无字形字符后的文本"""
        return "".join(run.text for run in self.split(text))

    def measure(self, text: str, letter_spacing: float = 0.0) -> float:
        """文本的水平推进宽度"""
        width = 0.0
        count = 0
        for run in self.split(text):
            width += run.font.getlength(run.text)
            count += len(run.text)
        if count > 1 and letter_spacing:
            width += letter_spacing * (count - 1)
        return width

    def line_height(self, multiplier: float = 1.0) -> int:
        """按主字体的升降部推算的行高"""
        ascent, descent = self.primary.getmetrics()
        return int(round((ascent + descent) * multiplier))


@lru_cache(maxsize=32)
def _coverage_of(spec: FontSpec) -> frozenset[int]:
    """读出字体 cmap 覆盖的全部码位

    解析失败时返回空集合而不是抛错：这一层拿不到覆盖信息，就当它什么都画不了，
    字体链会自动落到下一层。
    """
    try:
        if spec.path.lower().endswith(".ttc"):
            with TTCollection(spec.path, lazy=True) as collection:
                font = collection.fonts[spec.index]
                return frozenset(_merge_cmap(font))
        with TTFont(spec.path, fontNumber=spec.index, lazy=True) as font:
            return frozenset(_merge_cmap(font))
    except Exception as exc:  # noqa: BLE001 - 字体文件损坏/格式不支持都只应降级
        logger.warning(f"解析字体覆盖范围失败，已跳过该字体: {spec.path} ({exc})")
        return frozenset()


def _merge_cmap(font: TTFont) -> set[int]:
    codes: set[int] = set()
    for table in font["cmap"].tables:
        codes.update(table.cmap.keys())
    return codes


@lru_cache(maxsize=32)
def _is_color_font(spec: FontSpec) -> bool:
    try:
        if spec.path.lower().endswith(".ttc"):
            with TTCollection(spec.path, lazy=True) as collection:
                return any(tag in collection.fonts[spec.index] for tag in _COLOR_FONT_TABLES)
        with TTFont(spec.path, fontNumber=spec.index, lazy=True) as font:
            return any(tag in font for tag in _COLOR_FONT_TABLES)
    except Exception:  # noqa: BLE001
        return False


def _custom_specs() -> Tuple[FontSpec, ...]:
    """用户放进 data/fonts 的字体，按文件名排序保证顺序稳定"""
    if not CUSTOM_FONT_DIR.is_dir():
        return ()
    found = sorted(
        path for path in CUSTOM_FONT_DIR.iterdir() if path.suffix.lower() in {".ttf", ".otf", ".ttc"}
    )
    return tuple(FontSpec(str(path)) for path in found)


def _first_available(candidates: Sequence[FontSpec]) -> Optional[FontSpec]:
    for spec in candidates:
        if Path(spec.path).is_file() and _coverage_of(spec):
            return spec
    return None


def _try_load(spec: FontSpec, size: int) -> Optional[ImageFont.FreeTypeFont]:
    """按指定字号加载字体面

    Noto Color Emoji 这类只带固定尺寸位图的字体在任意字号下都可能拒绝加载，
    这时返回 None 让调用方跳过该层，而不是让整张卡片失败。
    """
    try:
        return ImageFont.truetype(spec.path, size, index=spec.index)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"字体 {spec.path}#{spec.index} 不支持字号 {size}，跳过: {exc}")
        return None


@lru_cache(maxsize=128)
def load_font(role: FontRole, size: int) -> LayeredFont:
    """取某个角色、某个字号的字体链

    Raises:
        RuntimeError: 一个可用的中文字体都找不到。调用方应当据此整体降级为文本消息。
    """
    specs: List[FontSpec] = []
    specs.extend(_custom_specs())

    primary = _first_available(_CJK_CANDIDATES[role])
    if primary is None and role is FontRole.BOLD:
        # 没有独立粗体时退回常规字重：字重不对只是不够好看，缺字体则是完全画不出来
        primary = _first_available(_CJK_CANDIDATES[FontRole.REGULAR])
    if primary is not None:
        specs.append(primary)

    # 兜底层不是"二选一"，而是整条链都要挂上去。Linux 的 Noto Color Emoji 是只有 109px
    # 一档的 CBDT 位图，卡片上的任何字号都会加载失败；如果这里只取第一个可用候选，
    # 排在它后面的单色符号字体就永远轮不上，❤ ★ 这类本来画得出来的字符也会被当成
    # "无字形"直接丢掉。挂满之后，加载失败的那一层会在下面自动被跳过。
    specs.extend(spec for spec in _FALLBACK_CANDIDATES if Path(spec.path).is_file() and _coverage_of(spec))

    layers: List[Tuple[ImageFont.FreeTypeFont, frozenset[int], bool]] = []
    for spec in specs:
        font = _try_load(spec, size)
        if font is None:
            continue
        layers.append((font, _coverage_of(spec), _is_color_font(spec)))

    if not layers:
        raise RuntimeError(
            "未找到任何可用的中文字体，无法渲染图片卡片。"
            f"请安装 fonts-noto-cjk，或把字体文件放入 {CUSTOM_FONT_DIR}",
        )
    return LayeredFont(layers, size)


def fonts_available() -> bool:
    """当前环境是否具备渲染中文卡片的字体

    插件在启用前用它做一次自检，缺字体时直接走文本降级，
    而不是等到每次要发卡片时再抛异常。
    """
    try:
        load_font(FontRole.REGULAR, 16)
    except RuntimeError:
        return False
    return True


def describe_fonts() -> str:
    """当前解析到的字体，用于日志与后台自检展示"""
    parts: List[str] = []
    for role in (FontRole.REGULAR, FontRole.BOLD):
        try:
            font = load_font(role, 16)
        except RuntimeError:
            parts.append(f"{role.value}=<无>")
            continue
        parts.append(f"{role.value}={font.primary.path}")
    parts.append(f"platform={sys.platform}")
    return ", ".join(parts)
