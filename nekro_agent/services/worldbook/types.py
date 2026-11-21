"""世界书引擎的输入类型与上限常量

引擎（匹配、正则、定时、预算、导入导出）只认这里的冻结数据类，不认 ORM 行：这样
「一条条目怎么判命中」可以在没有任何数据库、没有事件循环的情况下被穷举用例覆盖，
而 ORM 到这些数据类的转换只有 `book_to_match` / `entry_to_match` 一处。若让引擎直接
读模型，任何一次查询都会把「判据」和「取数」缠在一起，判据就再也测不完整了。

上限常量集中在这里而不是散落在校验代码里：条目保存、导入、导出三处都要按同一组上限
夹值，写三遍就会出现「保存时允许 300 字、导入时允许 200 字」这种只有一条路径能复现的
不一致。

各常量的取舍：

上限分两类。`MAX_KEYS`、`MAX_KEY_LENGTH`、`MAX_CONTENT_CHARS`、`MAX_COMMENT_CHARS` 是**安全
上限**，不是功能限制：取得足够宽，真实的酒馆世界书（几 MB、几千条、每条上百个关键词、单条
几万字）碰不到。导入时任何一条超了就整份拒绝并说明是哪一条、哪个字段，**不截断**——截掉
半条设定比导入失败更糟，管理员往往要到出了怪回复才发现少了字。每轮注入的预算另有配置项
`WORLDBOOK_MAX_CHARS`，比它还长的条目照样导入，只在导入报告里提示。

- `MAX_KEYS` / `MAX_KEY_LENGTH`：主 / 次关键词各自的条数与单条长度。关键词是每轮逐条跑的，
  上限挡的是「一条条目塞几万个关键词」这种把每轮拖慢的输入；匹配耗时按 3000 条 × 每条 100 个
  关键词量过，见 `matcher` 模块。
- `MAX_CONTENT_CHARS`：单条正文上限，挡的是畸形文件，不是正常的长设定。
- `MAX_COMMENT_CHARS`：条目标题（备注）上限，同上。
- `MAX_NAME_CHARS`：书名上限。书名由管理员在表单里填（导入时默认取文件名），不是从文件里
  读的，导入不会因为它截断任何内容。
- `MAX_TRIGGER_TEXT_CHARS`：触发测试里手填的测试文字上限。它要被每条关键词扫一遍，与条目正文
  不是一回事，单独设一个小的。
- `SCAN_DEPTH_MIN` / `SCAN_DEPTH_MAX`：扫描深度 1 到 50。0 条等于永不命中（没有意义），
  超过 50 条则让每轮都要读半屏历史。
- `DEPTH_MIN` / `DEPTH_MAX`：深度注入的位置，0 表示最后一条之后。
- `TIMED_MAX`：sticky / cooldown / delay 的条数上限（按消息条数计）。
- `ORDER_MIN` / `ORDER_MAX`：排序权重的合法区间，宽到够用，又能挡住「用 10 位数当权重」
  这类把排序变成事故的输入。
- `DEFAULT_MAX_CHARS` / `DEFAULT_MAX_ENTRIES_PER_ROUND`：每轮注入的默认预算，只作为
  `runtime` 读配置时的兜底（配置对象上取不到这一项时用它们）。真正的预算来自配置项
  （`WORLDBOOK_MAX_CHARS` / `WORLDBOOK_MAX_ENTRIES_PER_ROUND`），触发测试读的是这个频道
  的生效配置——测试页必须与正式那一轮用同一份预算，否则管理员看到的「被预算截掉」会
  在改配置之后与实际情况不符。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

MAX_KEYS = 1000
MAX_KEY_LENGTH = 2000
MAX_CONTENT_CHARS = 1_000_000
MAX_COMMENT_CHARS = 10_000
MAX_NAME_CHARS = 128
MAX_TRIGGER_TEXT_CHARS = 8000
SCAN_DEPTH_MIN = 1
SCAN_DEPTH_MAX = 50
DEPTH_MIN = 0
DEPTH_MAX = 100
TIMED_MAX = 200
ORDER_MIN = -100_000
ORDER_MAX = 100_000

# 建书时的默认扫描深度。`person` 书建得深一些：判断「这个人在不在」看的是他有没有
# 发言，群里一轮对话往往几十条才轮到他一次，4 条几乎永远判不出来。
DEFAULT_BOOK_SCAN_DEPTH = 4
DEFAULT_PERSON_BOOK_SCAN_DEPTH = 10

# 每轮注入的默认预算，只用作 `runtime` 读配置时的兜底（配置对象上取不到这一项时）。
# 真正的预算来自 `WORLDBOOK_MAX_CHARS` / `WORLDBOOK_MAX_ENTRIES_PER_ROUND` 两项配置。
DEFAULT_MAX_CHARS = 3000
DEFAULT_MAX_ENTRIES_PER_ROUND = 32


class ScopeKind(StrEnum):
    """一本书绑定的作用域类型"""

    DOMAIN = "domain"
    PRESET = "preset"
    CHANNEL = "channel"
    PERSON = "person"


class SelectiveLogic(StrEnum):
    """次关键词的四种逻辑（取值与酒馆的 `selectiveLogic` 一一对应）"""

    AND_ANY = "and_any"
    AND_ALL = "and_all"
    NOT_ANY = "not_any"
    NOT_ALL = "not_all"


class Position(StrEnum):
    """条目注入的位置"""

    AFTER_PERSONA = "after_persona"
    BEFORE_HISTORY = "before_history"
    AT_DEPTH = "at_depth"


class EntryStatus(StrEnum):
    """条目的状态"""

    ACTIVE = "active"
    QUARANTINED = "quarantined"


class QuarantineReason(StrEnum):
    """条目被隔离的原因"""

    MANUAL = "manual"
    AUTO_BLOCKED = "auto_blocked"


def _clamp(value: int, low: int, high: int) -> int:
    """把整数夹进闭区间；导入与保存都走它，边界只有一个来源"""
    return max(low, min(high, value))


def _as_int(value: Any, default: int) -> int:
    """尽力把值读成整数，读不出来就用默认值

    只用于「从库里读出来的行」这条路径：列类型本身保证了是整数，这里兜的是历史上
    被手工改库或导入写坏的行。读坏了按默认值走，而不是抛异常让整轮提示词失败。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class MatchBook:
    """匹配引擎看到的书

    `scan_depth` / `case_sensitive` / `match_whole_words` 是书的默认值，条目的空覆盖
    由 `entry_to_match` 在这里补齐，引擎里不再出现「条目没设就回头看书的」这种分支。
    """

    id: int
    scope_kind: ScopeKind
    person_account: str
    scan_depth: int
    budget_chars: int
    case_sensitive: bool
    match_whole_words: bool


@dataclass(frozen=True)
class MatchEntry:
    """匹配引擎看到的条目

    Attributes:
        scan_depth: 已按书补齐并夹在 1 到 50 之间的扫描深度。
        case_sensitive: 已按书补齐。
        match_whole_words: 已按书补齐。
        sticky / cooldown / delay: 0 表示不用（列上的 NULL 与 0 在这里是同一件事）。
        invalid_keys: 导入时被判为无效正则的关键词原文。它们仍留在 `keys` 里以便导出
            不丢，但匹配时一律跳过，否则一条坏正则会每轮都抛错。
    """

    id: int
    book_id: int
    keys: tuple[str, ...]
    secondary_keys: tuple[str, ...]
    selective_logic: SelectiveLogic
    use_regex: bool
    constant: bool
    content: str
    order: int
    position: Position
    depth: int
    scan_depth: int
    case_sensitive: bool
    match_whole_words: bool
    sticky: int
    cooldown: int
    delay: int
    trusted: bool
    invalid_keys: frozenset[str]


@dataclass(frozen=True)
class WindowMessage:
    """扫描窗口里的一条消息

    刻意不带昵称：昵称由群友自己改，拿它触发条目等于让任何人改个名就能召唤设定。
    只带引擎真正要用的四列，免得「窗口」慢慢变成一个能读到整行消息的后门。
    """

    id: int
    sender_id: str
    platform_userid: str
    content_text: str


def book_to_match(book: Any) -> MatchBook:
    """把一行书转成引擎输入（只读属性，不查库）"""
    scan_depth = _as_int(getattr(book, "scan_depth", DEFAULT_BOOK_SCAN_DEPTH), DEFAULT_BOOK_SCAN_DEPTH)
    try:
        scope_kind = ScopeKind(str(getattr(book, "scope_kind", ScopeKind.DOMAIN.value)))
    except ValueError:
        # 库里出现未知作用域类型是坏数据。这里退回 `domain` 只为让类型成立：范围条件本身
        # 是按 `scope_kind` 精确匹配四条腿的，这样的书一条腿都选不中，因此怎么读都不会让
        # 它凭空生效——「让类型成立」与「放宽授权」在这里是两件不相关的事。
        scope_kind = ScopeKind.DOMAIN
    return MatchBook(
        id=_as_int(getattr(book, "id", 0), 0),
        scope_kind=scope_kind,
        person_account=str(getattr(book, "person_account", "") or ""),
        scan_depth=_clamp(scan_depth, SCAN_DEPTH_MIN, SCAN_DEPTH_MAX),
        budget_chars=max(0, _as_int(getattr(book, "budget_chars", 0), 0)),
        case_sensitive=bool(getattr(book, "case_sensitive", False)),
        match_whole_words=bool(getattr(book, "match_whole_words", False)),
    )


def _invalid_keys(entry: Any) -> frozenset[str]:
    """从 `ext.na_invalid_keys` 里取无效关键词的原文

    结构是 `[{"key": ..., "reason": ...}, ...]`。任何一项形状不对就整项跳过：这是导入
    留下的附加信息，读坏了最坏是让一个坏正则多跑一次（会被 `safe_regex` 挡住），不该
    让整轮提示词抛异常。
    """
    ext = getattr(entry, "ext", None)
    if not isinstance(ext, dict):
        return frozenset()
    raw = ext.get("na_invalid_keys")
    if not isinstance(raw, list):
        return frozenset()
    keys: set[str] = set()
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            keys.add(item["key"])
    return frozenset(keys)


def _str_tuple(value: Any) -> tuple[str, ...]:
    """JSON 列读成字符串数组；非字符串元素直接丢掉（导入时已经记过报告）"""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _timed(value: Any) -> int:
    """sticky / cooldown / delay：NULL 与越界都折成 0 到上限之间"""
    if value is None:
        return 0
    return _clamp(_as_int(value, 0), 0, TIMED_MAX)


def _override(value: Any, fallback: bool) -> bool:
    """条目的三态覆盖：NULL 表示跟书，其它值按布尔读"""
    if value is None:
        return fallback
    return bool(value)


def entry_to_match(entry: Any, book: MatchBook) -> MatchEntry:
    """把一行条目转成引擎输入；空覆盖按书补齐

    扫描深度夹在 1 到 50：库里的值可能来自导入的酒馆文件（那边是任意整数），而窗口
    长度直接决定每轮读多少历史。夹在读取处而不是写入处，是因为手工改库与老数据都会
    绕过写入侧的校验。
    """
    raw_scan_depth = getattr(entry, "scan_depth", None)
    scan_depth = book.scan_depth if raw_scan_depth is None else _as_int(raw_scan_depth, book.scan_depth)
    try:
        selective_logic = SelectiveLogic(str(getattr(entry, "selective_logic", SelectiveLogic.AND_ANY.value)))
    except ValueError:
        selective_logic = SelectiveLogic.AND_ANY
    try:
        position = Position(str(getattr(entry, "position", Position.BEFORE_HISTORY.value)))
    except ValueError:
        position = Position.BEFORE_HISTORY
    return MatchEntry(
        id=_as_int(getattr(entry, "id", 0), 0),
        book_id=_as_int(getattr(entry, "book_id", book.id), book.id),
        keys=_str_tuple(getattr(entry, "keys", None)),
        secondary_keys=_str_tuple(getattr(entry, "secondary_keys", None)),
        selective_logic=selective_logic,
        use_regex=bool(getattr(entry, "use_regex", False)),
        constant=bool(getattr(entry, "constant", False)),
        content=str(getattr(entry, "content", "") or ""),
        order=_clamp(_as_int(getattr(entry, "order", 100), 100), ORDER_MIN, ORDER_MAX),
        position=position,
        depth=_clamp(_as_int(getattr(entry, "depth", 4), 4), DEPTH_MIN, DEPTH_MAX),
        scan_depth=_clamp(scan_depth, SCAN_DEPTH_MIN, SCAN_DEPTH_MAX),
        case_sensitive=_override(getattr(entry, "case_sensitive", None), book.case_sensitive),
        match_whole_words=_override(getattr(entry, "match_whole_words", None), book.match_whole_words),
        sticky=_timed(getattr(entry, "sticky", None)),
        cooldown=_timed(getattr(entry, "cooldown", None)),
        delay=_timed(getattr(entry, "delay", None)),
        trusted=bool(getattr(entry, "trusted", False)),
        invalid_keys=_invalid_keys(entry),
    )
