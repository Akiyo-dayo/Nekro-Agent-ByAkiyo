"""好感度排行榜卡片：取数、排版与绘制

## 展示分与内部分是两套刻度

内部 `DBAffection.score` 是 `[-100, 200]` 的整数（见 `services/affection/stage.py`），
它服务于阶段判定与排序，不适合直接摆在卡片上：`200` 这个上限对读者没有任何含义，
而一列整数看上去也像是随手编的。

卡片上展示的是 **内部分数除以 2 后保留一位小数**：

- 上限 `200` → `100.0`，读者一眼就知道这是"满分一百"的刻度；
- 下限 `-100` → `-50.0`，负分照常展示，不做截断也不取绝对值；
- 奇数分自然落在 `.5` 上，一位小数因此是真实精度而不是硬凑的 `.0`。

这只是一层展示换算，`display_score` 是纯函数，**不写回数据库、不改变任何内部语义**。
需要改刻度时只动这一个函数。

## 谁会出现在榜上

好感度按 **(域, 人, 人设)** 隔离（见 `services/identity_scope.py`），本身不分频道；
而排行榜是"这个群里的人"的榜，所以成员集合来自该频道近期的发言记录，
再与好感度记录取交集。没有好感度记录的人不上榜（他们的分数是"尚未建立关系"，
而不是 0 分），但会计入副标题的活跃人数。

## 排序范围锁在「当前域 + 当前人设」

榜单的取数范围**恒等于发起会话所在的那一份好感**，一维都不能少：

- **少了域** = 把另一个租户/另一个实例的好感度排进这个群的榜；
- **少了人设** = 把另一个人格下的关系暴露出来。运营在群 A 用人设甲、群 B 用人设乙时，
  两边本来就是两份好感（"换了个人设就不会记得以前的恩怨情仇"），跨人设排名等于
  当着甲的面念出乙那边的亲疏。

这两条都是**信息泄露**，而不是"显示得不太准"——它们不会报错、不会降级，
在生产上几乎不可能被发现，所以范围只从 `services/identity_scope.py` 解析，
过滤条件直接用好感度自己的键类型（`services/affection/key.py` 的
:meth:`AffectionKey.scope_columns`），**本模块不自己拼键**。

## 榜上是「人」不是「账号」

上榜单位是 :class:`RankEntry.person_uid`——合并过的账号（换了 QQ 号）解析成同一个人，
因此**只出现一次**，副标题里的活跃人数也按人算。同理，发起指令的人用副号发的指令
也能高亮到自己那一行：高亮比对的是解析后的人，不是原始账号。

## 截断规则

按展示分降序取前 `MAX_ENTRIES` 名，同分时按 `person_uid` 升序保证顺序稳定
（否则同一份数据两次渲染的行序可能不同）。列数最多 `MAX_COLUMNS` 列，
按列优先填充：第一列从上到下是第 1 名到第 N 名，第二列接着往下。
人少时不会硬凑满 6 列——`MIN_ROWS_PER_COLUMN` 保证每列至少有这么多行，
否则 5 个人会被摊成 5 列各 1 行，看起来像坏掉了。
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass
from math import ceil
from typing import Dict, List, Optional, Protocol, Sequence, Tuple

from PIL import Image, ImageChops, ImageDraw

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_affection import DBAffection
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.services.affection.key import AffectionKey
from nekro_agent.services.identity_scope import (
    ChannelLike,
    IdentityScope,
    resolve_identity_scopes,
)
from nekro_agent.tools.untrusted_content import sanitize_inline_text

from .fonts import FontRole, load_font
from .painter import RGBA, corner_glow, draw_text, ellipsize, rounded_rect

logger = get_sub_logger("imagecard")

# 榜上最多展示多少人。6 列 × 34 行的密度已经接近可读性的下限，再多就只是把图撑大
MAX_ENTRIES = 200
MAX_COLUMNS = 6
MIN_ROWS_PER_COLUMN = 8

# 昵称清洗后的保留长度。真正决定显示长度的是像素宽度，这里只是先砍掉
# 那些几百字的昵称，免得后面对每个字符反复量宽度
MAX_NICKNAME_LENGTH = 32

# 昵称后缀取平台 ID 的末尾几位，用于区分重名
USERID_SUFFIX_LENGTH = 4

# 统计群成员时最多回溯多少条消息。活跃窗口内的消息量在大群里可以非常大，
# 不设上限会让一次指令拖垮数据库
DEFAULT_SCAN_LIMIT = 20000

# Bot 自身发言在消息表里以 sender_id "-1" 记录，不该出现在榜上
BOT_SENDER_ID = "-1"


class RankChannel(ChannelLike, Protocol):
    """榜单取数需要频道提供的最小接口

    在 `identity_scope.ChannelLike`（域与人设的权威来源）之上多一个 `chat_key`——
    成员集合来自这个频道的发言记录。用 Protocol 而不是直接依赖 `DBChatChannel`，
    理由与 `ChannelLike` 逐字相同：后者会把插件收集器与适配器注册表整条导入链拖进来。
    """

    chat_key: str


@dataclass(frozen=True)
class RankEntry:
    """参与排名的一个人（内部刻度）

    单位是**人**不是账号：`person_uid` 是 `identity_scope` 解析出来的人标识
    （= 该人主账号的平台用户 ID），合并过的几个账号在这里已经塌成一个。
    """

    person_uid: str
    nickname: str
    score: int


@dataclass(frozen=True)
class RankCollection:
    """一次榜单取数的结果

    Attributes:
        entries: 有好感记录的人，顺序无意义（排序由 :func:`build_rank_view` 负责）
        total_active: 活跃**人数**（合并过的账号只算一个人），可以大于榜上人数
        scope: 本次取数用的隔离键。域与人设就是这张榜的排序范围，
            `affection_person_uid` 是发起人解析后的那个人（用于高亮）
    """

    entries: Tuple[RankEntry, ...]
    total_active: int
    scope: IdentityScope

    @property
    def highlight_person_uid(self) -> str:
        """要高亮的那个人；发起人未知（空串）时为空串，表示不高亮"""
        return self.scope.affection_person_uid


@dataclass(frozen=True)
class RankRow:
    """榜上一行的展示内容"""

    display_name: str
    suffix: str
    score_text: str
    highlighted: bool


@dataclass(frozen=True)
class RankView:
    """一张排行榜卡片的全部展示数据

    纯数据结构，不含任何像素信息，因此分列/截断/格式化都能直接用表驱动用例断言。
    """

    rows: Tuple[RankRow, ...]
    columns: int
    rows_per_column: int
    total_active: int
    total_ranked: int
    truncated: bool


@dataclass(frozen=True)
class RankTheme:
    """排行榜配色"""

    card_bg: RGBA = (255, 255, 255, 255)
    card_border: RGBA = (214, 234, 217, 255)
    glow: Tuple[int, int, int] = (76, 175, 108)
    icon_bg: RGBA = (60, 173, 106, 255)
    icon_glyph: RGBA = (255, 255, 255, 255)
    title: RGBA = (26, 94, 55, 255)
    subtitle: RGBA = (150, 160, 154, 255)
    pill_border: RGBA = (144, 205, 166, 255)
    pill_text: RGBA = (72, 152, 103, 255)
    name: RGBA = (46, 56, 50, 255)
    suffix: RGBA = (166, 177, 170, 255)
    score: RGBA = (46, 56, 50, 255)
    separator: RGBA = (238, 244, 239, 255)
    highlight_bg: RGBA = (231, 246, 235, 255)
    highlight_border: RGBA = (152, 210, 172, 255)


def display_score(score: int) -> float:
    """内部分数换算成展示刻度

    详见模块文档：除以 2，使上限 200 对应满分 100.0。
    """
    return score / 2


def format_score(score: int) -> str:
    """展示分的文本形式，固定一位小数，负号照常保留"""
    return f"{display_score(score):.1f}"


def plan_columns(count: int) -> Tuple[int, int]:
    """决定列数与每列行数

    Returns:
        `(列数, 每列行数)`；`count` 为 0 时返回 `(0, 0)`
    """
    if count <= 0:
        return 0, 0
    columns = min(MAX_COLUMNS, max(1, ceil(count / MIN_ROWS_PER_COLUMN)))
    return columns, ceil(count / columns)


def build_rank_view(
    entries: Sequence[RankEntry],
    *,
    total_active: int,
    highlight_person_uid: str = "",
    max_entries: int = MAX_ENTRIES,
) -> RankView:
    """把好感度记录整理成可渲染的榜单

    Args:
        entries: 候选成员，顺序无所谓，本函数负责排序
        total_active: 活跃人数，只用于副标题展示，可以大于榜上人数
        highlight_person_uid: 需要高亮的**人**（不是账号），通常是发起查询的那个人。
            发起人用副号发指令时这里已经是他主账号的标识，因此仍然高亮到他自己那一行。
        max_entries: 榜单容量上限

    Returns:
        已排序、已截断、已格式化的榜单视图
    """
    ordered = sorted(entries, key=lambda item: (-item.score, item.person_uid))
    kept = ordered[:max_entries] if max_entries > 0 else []
    columns, rows_per_column = plan_columns(len(kept))

    rows = tuple(
        RankRow(
            display_name=_display_name(entry),
            suffix=entry.person_uid[-USERID_SUFFIX_LENGTH:],
            score_text=format_score(entry.score),
            highlighted=bool(highlight_person_uid) and entry.person_uid == highlight_person_uid,
        )
        for entry in kept
    )

    return RankView(
        rows=rows,
        columns=columns,
        rows_per_column=rows_per_column,
        total_active=total_active,
        total_ranked=len(ordered),
        truncated=len(ordered) > len(kept),
    )


def _display_name(entry: RankEntry) -> str:
    """昵称是用户可控内容，清洗后为空时退回人的标识"""
    cleaned = sanitize_inline_text(entry.nickname, MAX_NICKNAME_LENGTH)
    return cleaned or entry.person_uid


async def collect_rank_entries(
    *,
    channel: RankChannel,
    active_days: int,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
    highlight_userid: str = "",
    now: Optional[float] = None,
) -> RankCollection:
    """取出某个频道近期活跃成员的好感度记录

    成员集合来自频道最近的发言（这是"这个群里有谁"唯一的答案来源，好感度本身不分频道），
    好感度则来自**当前会话所在的域 + 当前人设**那一份——范围少一维就是信息泄露，
    判据见模块文档。

    Args:
        channel: 当前频道。域与人设从它解析（`adapter_key` / `instance_key` /
            `get_effective_preset_id`），成员集合从它的 `chat_key` 取。
        active_days: 活跃窗口天数
        scan_limit: 统计成员时最多回溯多少条消息
        highlight_userid: 发起查询的那个**账号**；本函数负责把它解析成"人"
        now: 当前时间戳，仅供测试注入

    Returns:
        :class:`RankCollection`。上榜单位是人：合并过的账号只出现一次，
        活跃人数也按人算。
    """
    cutoff = int((now if now is not None else time.time()) - active_days * 86400)

    recent = (
        await DBChatMessage.filter(
            chat_key=channel.chat_key,
            send_timestamp__gte=cutoff,
        )
        .exclude(sender_id=BOT_SENDER_ID)
        .order_by("-send_timestamp")
        .limit(scan_limit)
        .values("platform_userid", "sender_nickname")
    )

    # 同一个账号会出现很多次，第一次出现的就是最新的昵称
    latest_nickname: Dict[str, str] = {}
    for row in recent:
        userid = str(row["platform_userid"] or "")
        if not userid or userid == BOT_SENDER_ID:
            continue
        latest_nickname.setdefault(userid, str(row["sender_nickname"] or ""))

    # 发起人**恒在**这一批里（空串也不例外，批量解析对每个入参都有一项）：一次 `IN` 同时得到
    # 「域 + 人设」这个排序范围与「发起人是哪个人」，空群也因此有确定的范围可报，
    # 不必为了拿范围再解析一次。
    scopes = await resolve_identity_scopes(channel, [*latest_nickname, highlight_userid])
    scope = scopes[highlight_userid]

    # 账号 → 人：合并过的号在这里塌成同一个人，榜上只出现一次。
    # `latest_nickname` 按发言时间倒序插入，因此 `setdefault` 留下的是这个人最近用过的昵称。
    person_nickname: Dict[str, str] = {}
    for account, nickname in latest_nickname.items():
        person_nickname.setdefault(scopes[account].affection_person_uid, nickname)

    if not person_nickname:
        return RankCollection(entries=(), total_active=0, scope=scope)

    # 范围条件走好感度自己的键类型（`AffectionKey.scope_columns` = 域三列 + 人设，不含人），
    # 与写侧建行、事件流水用的是同一份：本模块因此不存在"第二处键实现"，
    # 将来键上加一维也会自动跟着收紧。
    #
    # 这里直接查表而不走 `store` 的快照接口：快照是**按人**的点查（还带 TTL 缓存），
    # 一张两百人的榜要么变成两百次点查，要么读到一批新旧不一的分数。
    records = await DBAffection.filter(
        **AffectionKey.of_scope(scope).scope_columns(),
        platform_userid__in=list(person_nickname),
    ).values("platform_userid", "nickname", "score")

    entries = tuple(
        RankEntry(
            person_uid=str(record["platform_userid"]),
            # 消息表里的昵称比好感度记录里的更新，优先用它
            nickname=person_nickname.get(str(record["platform_userid"]), "") or str(record["nickname"] or ""),
            score=int(record["score"]),
        )
        for record in records
    )
    return RankCollection(entries=entries, total_active=len(person_nickname), scope=scope)


def render_rank_card(
    view: RankView,
    *,
    title: str = "好感度",
    subtitle: str,
    badge: str = "好感查看",
    scale: int = 2,
    theme: Optional[RankTheme] = None,
) -> bytes:
    """渲染好感度排行榜卡片

    Args:
        view: 已整理好的榜单视图
        subtitle: 头部小字，例如 "群 123456 · 近30天活跃 227 人"

    Returns:
        PNG 字节流

    Raises:
        ValueError: 榜单为空
        RuntimeError: 环境缺少可用中文字体
    """
    if not view.rows:
        raise ValueError("好感度榜单没有任何可展示的记录")

    palette = theme or RankTheme()
    metrics = _RankMetrics(scale)

    columns_width = view.columns * metrics.column_width + max(0, view.columns - 1) * metrics.column_gap
    # 人少时列区很窄，但头部的图标 + 标题 + 药丸按钮是有固有宽度的：
    # 只按列区定宽会让标题直接压在按钮上（三个人的群就会撞车）
    width = max(metrics.pad * 2 + columns_width, _header_min_width(metrics, title, subtitle, badge))
    column_origin = max(metrics.pad, (width - columns_width) // 2)
    body_top = metrics.pad + metrics.header_height
    height = body_top + view.rows_per_column * metrics.row_height + metrics.pad

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    rounded_rect(
        draw,
        (0, 0, width - 1, height - 1),
        metrics.card_radius,
        fill=palette.card_bg,
        outline=palette.card_border,
        width=metrics.hairline,
    )

    glow = corner_glow((width, height), palette.glow, reach=0.55, max_alpha=26)
    # 光晕要被卡片的圆角裁掉，否则会在圆角外面露出四方形的直角
    corner_mask = Image.new("L", (width, height), 0)
    rounded_rect(ImageDraw.Draw(corner_mask), (0, 0, width - 1, height - 1), metrics.card_radius, fill=(255,))
    image.paste(glow, (0, 0), ImageChops.multiply(glow.getchannel("A"), corner_mask))

    _draw_header(draw, width, metrics, palette, title=title, subtitle=subtitle, badge=badge)
    _draw_columns(draw, view, metrics, palette, body_top=body_top, height=height, column_origin=column_origin)

    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


class _RankMetrics:
    """排行榜卡片的度量"""

    def __init__(self, scale: int) -> None:
        if scale < 1:
            raise ValueError(f"缩放倍率必须不小于 1: {scale}")
        self.scale = scale
        self.pad = 26 * scale
        self.card_radius = 20 * scale
        self.hairline = max(1, scale)

        self.header_height = 84 * scale
        self.icon_size = 46 * scale
        self.icon_radius = 13 * scale
        self.title_size = 27 * scale
        self.subtitle_size = 13 * scale
        self.header_gap = 14 * scale
        self.badge_size = 13 * scale
        self.badge_pad_x = 14 * scale
        self.badge_pad_y = 8 * scale

        self.column_width = 168 * scale
        self.column_gap = 14 * scale
        self.row_height = 26 * scale
        self.name_size = 13 * scale
        self.suffix_size = 10 * scale
        self.score_size = 13 * scale
        self.name_suffix_gap = 5 * scale
        self.score_gap = 8 * scale
        self.row_pad_x = 6 * scale
        self.subtitle_max_width = 330 * scale


def _header_min_width(metrics: _RankMetrics, title: str, subtitle: str, badge: str) -> int:
    """头部内容不被挤压所需的最小卡片宽度

    副标题可以很长（群号 + 活跃人数），完全按它定宽会让人少的卡片变得又宽又空，
    所以副标题只保证一个上限内的宽度，超出的部分交给省略号。
    """
    font_title = load_font(FontRole.BOLD, metrics.title_size)
    font_subtitle = load_font(FontRole.REGULAR, metrics.subtitle_size)
    font_badge = load_font(FontRole.REGULAR, metrics.badge_size)

    text_width = max(
        font_title.measure(title),
        min(font_subtitle.measure(subtitle), metrics.subtitle_max_width),
    )
    badge_width = (font_badge.measure(badge) + metrics.badge_pad_x * 2) if badge else 0
    return int(
        ceil(
            metrics.pad * 2
            + metrics.icon_size
            + metrics.header_gap
            + text_width
            + metrics.header_gap
            + badge_width,
        ),
    )


def _draw_header(
    draw: ImageDraw.ImageDraw,
    width: int,
    metrics: _RankMetrics,
    palette: RankTheme,
    *,
    title: str,
    subtitle: str,
    badge: str,
) -> None:
    icon_box = (
        float(metrics.pad),
        float(metrics.pad),
        float(metrics.pad + metrics.icon_size),
        float(metrics.pad + metrics.icon_size),
    )
    rounded_rect(draw, icon_box, metrics.icon_radius, fill=palette.icon_bg)
    _draw_heart(draw, icon_box, palette.icon_glyph)

    font_title = load_font(FontRole.BOLD, metrics.title_size)
    font_subtitle = load_font(FontRole.REGULAR, metrics.subtitle_size)

    text_x = metrics.pad + metrics.icon_size + metrics.header_gap
    title_top = metrics.pad - metrics.scale
    draw_text(draw, (text_x, title_top), title, font_title, palette.title)

    subtitle_top = title_top + metrics.title_size * 1.45
    badge_left = _badge_left(width, metrics, badge)
    subtitle_budget = badge_left - text_x - metrics.header_gap
    draw_text(
        draw,
        (text_x, subtitle_top),
        ellipsize(subtitle, font_subtitle, subtitle_budget),
        font_subtitle,
        palette.subtitle,
    )

    _draw_badge(draw, width, metrics, palette, badge)


def _badge_left(width: int, metrics: _RankMetrics, badge: str) -> float:
    font_badge = load_font(FontRole.REGULAR, metrics.badge_size)
    return width - metrics.pad - font_badge.measure(badge) - metrics.badge_pad_x * 2


def _draw_badge(
    draw: ImageDraw.ImageDraw,
    width: int,
    metrics: _RankMetrics,
    palette: RankTheme,
    badge: str,
) -> None:
    if not badge:
        return
    font_badge = load_font(FontRole.REGULAR, metrics.badge_size)
    text_width = font_badge.measure(badge)
    ascent, descent = font_badge.primary.getmetrics()
    box_height = ascent + descent + metrics.badge_pad_y * 2
    left = width - metrics.pad - text_width - metrics.badge_pad_x * 2
    top = metrics.pad + (metrics.icon_size - box_height) / 2

    rounded_rect(
        draw,
        (left, top, left + text_width + metrics.badge_pad_x * 2, top + box_height),
        box_height / 2,
        outline=palette.pill_border,
        width=metrics.hairline,
    )
    draw_text(draw, (left + metrics.badge_pad_x, top + metrics.badge_pad_y), badge, font_badge, palette.pill_text)


def _draw_columns(
    draw: ImageDraw.ImageDraw,
    view: RankView,
    metrics: _RankMetrics,
    palette: RankTheme,
    *,
    body_top: int,
    height: int,
    column_origin: int,
) -> None:
    font_name = load_font(FontRole.REGULAR, metrics.name_size)
    font_suffix = load_font(FontRole.REGULAR, metrics.suffix_size)
    font_score = load_font(FontRole.REGULAR, metrics.score_size)

    name_ascent, name_descent = font_name.primary.getmetrics()
    text_offset = (metrics.row_height - (name_ascent + name_descent)) / 2

    for index, row in enumerate(view.rows):
        column = index // view.rows_per_column
        line = index % view.rows_per_column
        col_left = column_origin + column * (metrics.column_width + metrics.column_gap)
        col_right = col_left + metrics.column_width
        row_top = body_top + line * metrics.row_height

        if row.highlighted:
            rounded_rect(
                draw,
                (col_left, row_top + metrics.scale, col_right, row_top + metrics.row_height - metrics.scale),
                6 * metrics.scale,
                fill=palette.highlight_bg,
                outline=palette.highlight_border,
                width=metrics.hairline,
            )

        text_top = row_top + text_offset
        score_width = font_score.measure(row.score_text)
        draw_text(
            draw,
            (col_right - metrics.row_pad_x, text_top),
            row.score_text,
            font_score,
            palette.score,
            align="right",
        )

        name_left = col_left + metrics.row_pad_x
        suffix_width = font_suffix.measure(row.suffix)
        name_budget = (
            col_right - metrics.row_pad_x - score_width - metrics.score_gap - suffix_width - metrics.name_suffix_gap - name_left
        )
        name = font_name.renderable(row.display_name) or f"用户{row.suffix}"
        name = ellipsize(name, font_name, name_budget)
        name_width = draw_text(draw, (name_left, text_top), name, font_name, palette.name)

        # 后缀跟着昵称走而不是固定列位：昵称长短不一，固定列位会在短昵称后留一大段空白
        suffix_ascent, suffix_descent = font_suffix.primary.getmetrics()
        suffix_top = text_top + (name_ascent + name_descent) - (suffix_ascent + suffix_descent) - metrics.scale
        draw_text(
            draw,
            (name_left + name_width + metrics.name_suffix_gap, suffix_top),
            row.suffix,
            font_suffix,
            palette.suffix,
        )

    for column in range(1, view.columns):
        x = column_origin + column * (metrics.column_width + metrics.column_gap) - metrics.column_gap / 2
        draw.line(
            [(x, body_top), (x, height - metrics.pad)],
            fill=palette.separator,
            width=metrics.hairline,
        )


def _draw_heart(draw: ImageDraw.ImageDraw, box: Tuple[float, float, float, float], color: RGBA) -> None:
    """图标里的白色爱心：两个圆加一个下尖的多边形"""
    left, top, right, bottom = box
    size = right - left
    cx = (left + right) / 2
    cy = (top + bottom) / 2
    half_w = size * 0.24
    lobe = half_w / 2
    lobe_cy = cy - size * 0.05

    draw.ellipse((cx - half_w, lobe_cy - lobe, cx, lobe_cy + lobe), fill=color)
    draw.ellipse((cx, lobe_cy - lobe, cx + half_w, lobe_cy + lobe), fill=color)
    points: List[Tuple[float, float]] = [
        (cx - half_w, lobe_cy),
        (cx + half_w, lobe_cy),
        (cx, cy + size * 0.23),
    ]
    draw.polygon(points, fill=color)
