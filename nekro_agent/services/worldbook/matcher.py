"""命中判定：扫描文本、归一化、关键词与四种次关键词逻辑

这一层是纯函数，输入是 :class:`~nekro_agent.services.worldbook.types.MatchEntry` 与
:class:`~nekro_agent.services.worldbook.types.WindowMessage`，输出是「因为什么命中」
（或没命中）。刻意不碰数据库、不取配置、不读时间：判据的每一种组合都要能被穷举用例
钉住，任何一次查库都会把它变成「只能靠集成测试碰运气」的逻辑。

## 扫描文本默认不含说话人名字

昵称由群友自己改，拿它触发条目等于让任何人改个名就能召唤这本书里的设定。所以默认只扫
正文。书级开关「扫描时带上说话人名字」（`scan_names`，对应酒馆的 `include_names`）打开
以后，每条消息写成「名字: 正文」：Bot 自己的消息用这一轮生效的人设名（关键词写成人设名的
条目，只要 Bot 最近一次发言还在扫描深度内就命中，这是这个开关的主要用途），群友用清洗过
的昵称。代价是上面那一条：群友昵称里带关键词也会触发，酒馆同样如此。这是管理员按书选的，
已有的书默认关；能触发的只是这个频道本来就生效的书里的条目，个人书的「在场」判断看的是
账号（`book_active_for_person`），不受名字影响。

## 为什么归一化不删空白

输出过滤那边删空白是对的（它要在「拆字」写法上还原出词表里的词）。这里必须保留：
英文关键词靠空格分词，删掉空白会让 `cat` 命中 `concatenate`，整词匹配也就失去意义。
所以两处共用的只是「哪些字符不可见」，空白的处理各按各的目的来。
"""

from __future__ import annotations

import functools
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from nekro_agent.services.memory.persons import human_accounts
from nekro_agent.services.output_filter.normalize import is_invisible_char

from .safe_regex import RegexRunner, parse_regex_key
from .types import MatchBook, MatchEntry, SelectiveLogic, WindowMessage

# 整词匹配的前后边界：ASCII 字母、数字与下划线都算「词内」。含下划线是刻意的——它在
# 英文标识符里与字母同权，把 `_` 排除会让 `foo_bar` 里的 `bar` 被当成独立的词。
_WORD_CHAR = "A-Za-z0-9_"


@dataclass(frozen=True)
class HitReason:
    """为什么命中

    Attributes:
        kind: `constant`（常驻）/ `key`（主关键词命中）/ `sticky`（定时效果保持）/
            `semantic`（语义相似度命中）。前两种由本模块给出，`sticky` 由定时效果那一层在
            保持期内替换出来，`semantic` 由运行时在关键词没命中时补上。
        key: 触发的主关键词原文；常驻、sticky 与语义命中为空串。
        secondary: 判定次关键词逻辑时命中的次关键词原文，按出现顺序。
        score: 语义命中的余弦相似度；其余原因为 `None`。
    """

    kind: str
    key: str = ""
    secondary: tuple[str, ...] = ()
    score: float | None = None


def _normalize(text: str, case_sensitive: bool) -> str:
    """归一化的实现本体，不带缓存；口径见 :func:`normalize_for_match`

    整段已经是 NFKC 形态时（中文与普通英文几乎都是）跳过逐字 NFKC：逐字处理只是为了让
    兼容字符的展开保持局部，对本来就不需要展开的文字它只是逐字原样拼回去，结果相同。
    不可见字符照样逐字去掉——它们本身就是 NFKC 形态，快速判断挡不住它们。
    """
    stripped = _strip_invisible(text)
    if unicodedata.is_normalized("NFKC", stripped):
        normalized = stripped
    else:
        normalized = "".join(_strip_invisible(unicodedata.normalize("NFKC", char)) for char in stripped)
    collapsed = " ".join(normalized.split())
    return collapsed if case_sensitive else collapsed.casefold()


@functools.lru_cache(maxsize=256)
def normalize_for_match(text: str, *, case_sensitive: bool) -> str:
    """按匹配用的口径归一化一段文字

    步骤与顺序都固定：

    - 先丢一次不可见字符；
    - 再**逐字**做 NFKC，做完再丢一次——NFKC 可能把兼容字符展开成带零宽字符的序列，
      也可能把两段拼成一个字符，逐字处理让「一个原文字符变成几个归一化字符」这件事
      保持局部，不会跨字符重排；
    - 连续空白压成一个空格，但不删——见模块文档；
    - casefold 而不是 lower：德语 `ß` 之类只有 casefold 才与 `ss` 等价。

    纯函数，缓存不改变结果。匹配热路径不走这个缓存，见 :func:`_normalized_scan_text`。
    """
    return _normalize(text, case_sensitive)


@functools.lru_cache(maxsize=64)
def _normalized_scan_text(text: str, case_sensitive: bool) -> str:
    """扫描文本专用的归一化缓存

    一轮里只有几段扫描文本（每种扫描深度一段），却要被成千上万个关键词各比一次。关键词
    与扫描文本共用一个缓存时，关键词一多（一本书几千条、每条上百个）就会把扫描文本挤出去，
    每个关键词都把同一段文本重新逐字归一化一遍，一轮匹配要几秒。分开以后扫描文本稳稳留在
    这里；关键词各不相同、反复命中缓存的机会很少，直接现算。
    """
    return _normalize(text, case_sensitive)


def _strip_invisible(text: str) -> str:
    """去掉不可见字符（零宽、方向控制、变体选择符），保留空白"""
    if text.isascii():
        # ASCII 里没有不可见字符（控制字符属于 `Cc`，不在 `Cf` 里），英文关键词不必逐字判
        return text
    return "".join(char for char in text if not _invisible(char))


# 逐字判「不可见」的缓存：判据本身要查 Unicode 类别与几个区间，而同一轮里要判的字绝大多数
# 是同一批常用字。按字缓存后，几十万个关键词逐字去不可见字符时每个字只是一次查表。
_invisible = functools.lru_cache(maxsize=8192)(is_invisible_char)


def scan_text(window: Sequence[WindowMessage], depth: int, *, include_names: bool = False) -> str:
    """取窗口里最后 `depth` 条消息的正文，按时间顺序用换行连起来

    `window` 按 `id` 升序排：调用方拿到的顺序不一定是时间顺序（倒序取最近 N 条是最
    常见的写法），而「最后 depth 条」必须按时间算。`id` 是自增主键，在同一个频道里
    就是时间顺序。

    `include_names` 为真时每条写成「名字: 正文」（与酒馆 `include_names` 的写法一致）；
    没有名字的消息（`speaker` 为空）照旧只有正文，不拼一个空名字和冒号。

    `depth <= 0` 返回空串：那是「一条都不扫」，不是「扫全部」。
    """
    if depth <= 0:
        return ""
    ordered = sorted(window, key=lambda message: message.id)
    recent = ordered[-depth:]
    if not include_names:
        return "\n".join(message.content_text for message in recent)
    return "\n".join(
        f"{message.speaker}: {message.content_text}" if message.speaker else message.content_text for message in recent
    )


def _is_ascii_word_char(char: str) -> bool:
    """字符是否 ASCII 字母或数字（整词匹配只对这类关键词生效）"""
    return char.isascii() and char.isalnum()


def _substring_hit(key: str, entry: MatchEntry, text: str) -> bool:
    """归一化后的子串命中；整词匹配只在关键词两端都是 ASCII 字母数字时启用

    中文关键词无论开不开整词都按子串：中文没有空格分词，硬做「前后不能是字母数字」
    只会让 `(?<![A-Za-z0-9_])` 这种边界对汉字失效或误判。
    """
    normalized_key = _normalize(key, entry.case_sensitive)
    if not normalized_key:
        return False
    normalized_text = _normalized_scan_text(text, entry.case_sensitive)
    if not entry.match_whole_words:
        return normalized_key in normalized_text
    if not (_is_ascii_word_char(normalized_key[0]) and _is_ascii_word_char(normalized_key[-1])):
        return normalized_key in normalized_text
    pattern = rf"(?<![{_WORD_CHAR}]){re.escape(normalized_key)}(?![{_WORD_CHAR}])"
    return re.search(pattern, normalized_text) is not None


def key_hit(key: str, entry: MatchEntry, raw_text: str, runner: RegexRunner) -> bool:
    """单个关键词是否命中

    正则关键词在**原始**扫描文本上跑：正则的 `\\s` / `\\b` / 字符类都依赖原文的空格与
    标点，先归一化会把它们改成另一条正则。大小写也只由 `i` 旗标与 `case_sensitive`
    决定，同样不经过归一化。

    导入时被判无效的关键词一律不命中：它们留在 `keys` 里只是为了让导出不丢内容，
    每轮再跑一遍既无意义又可能抛异常。
    """
    if key in entry.invalid_keys:
        return False
    spec = parse_regex_key(key, use_regex=entry.use_regex)
    if spec is not None:
        return runner.search(entry.id, key, spec, raw_text, case_sensitive=entry.case_sensitive)
    return _substring_hit(key, entry, raw_text)


def _secondary_satisfied(entry: MatchEntry, raw_text: str, runner: RegexRunner) -> tuple[bool, tuple[str, ...]]:
    """次关键词逻辑；返回（是否通过, 判定时命中的次关键词）

    次关键词为空视为通过：酒馆里「只写主关键词」是最常见的用法，那时次关键词这一层
    根本不该参与判定。
    """
    if not entry.secondary_keys:
        return True, ()
    hits = tuple(key for key in entry.secondary_keys if key_hit(key, entry, raw_text, runner))
    if entry.selective_logic is SelectiveLogic.AND_ANY:
        passed = bool(hits)
    elif entry.selective_logic is SelectiveLogic.AND_ALL:
        passed = len(hits) == len(entry.secondary_keys)
    elif entry.selective_logic is SelectiveLogic.NOT_ANY:
        passed = not hits
    else:  # NOT_ALL：至少一条没命中
        passed = len(hits) < len(entry.secondary_keys)
    return passed, hits


def entry_hit(
    entry: MatchEntry,
    window: Sequence[WindowMessage],
    runner: RegexRunner,
) -> HitReason | None:
    """这条条目在这一轮窗口里是否命中，命中时给出原因

    顺序是「常驻 → 主关键词 → 次关键词」：常驻条目连扫描文本都不用取，先判它能省掉
    后面每一步。主关键词逐个试，第一个命中的记进原因里——明细页要说清「为什么这条
    进来了」，只给一个布尔值说不清。
    """
    if entry.constant:
        return HitReason(kind="constant")

    if not entry.keys:
        # 非常驻又没有主关键词：永远不命中。不把空关键词当成「匹配一切」，那会让一条
        # 忘填关键词的条目变成常驻条目。
        return None

    raw_text = scan_text(window, entry.scan_depth, include_names=entry.scan_names)
    hit_key = ""
    for key in entry.keys:
        if key_hit(key, entry, raw_text, runner):
            hit_key = key
            break
    if not hit_key:
        return None

    passed, secondary = _secondary_satisfied(entry, raw_text, runner)
    if not passed:
        return None
    return HitReason(kind="key", key=hit_key, secondary=secondary)


def present_person_accounts(
    book: MatchBook,
    window: Sequence[WindowMessage],
    *,
    bot_account: str | None,
    alias_groups: Mapping[str, Iterable[str]],
) -> tuple[str, ...]:
    """这本个人书的主人在它自己的扫描窗口里实际发言用的账号，最近一次发言在前

    取法与 :func:`book_active_for_person` 完全同一套：按 `message.id` 排序、只看书自己的
    `scan_depth`、账号判据走 :func:`nekro_agent.services.memory.persons.human_accounts`
    （Bot 不算人）。差在输出：这里返回**具体是哪个账号**发的言，而不是「在不在场」。

    一个人可能用主号与副号轮流说话，因此可能返回多个账号；每个账号只出现一次，顺序按它
    最近一次发言的位置从新到旧。非 `person` 书、或没有 `person_account` 的个人书返回空元组。
    """
    if book.scope_kind != "person" or not book.person_account:
        return ()
    ordered = sorted(window, key=lambda message: message.id)
    recent = ordered[-book.scan_depth :]
    accounts = human_accounts(
        [_MessageView(message.sender_id, message.platform_userid) for message in recent],
        bot_account=bot_account,
    )
    # 只留「主人自己」的那些账号：窗口里可能有别人，不能把别人的账号标到这本书上。
    present = [
        account
        for account in accounts
        if account == book.person_account or book.person_account in alias_groups.get(account, ())
    ]
    if not present:
        return ()
    last_seen: dict[str, int] = {}
    for index, message in enumerate(recent):
        account = (message.platform_userid or "").strip()
        if account in present:
            last_seen[account] = index
    # 排序键只取「最近一次发言的位置」；`dict.fromkeys` 先去重，再按位置从新到旧。
    return tuple(sorted(dict.fromkeys(present), key=lambda account: last_seen[account], reverse=True))


def book_active_for_person(
    book: MatchBook,
    window: Sequence[WindowMessage],
    *,
    bot_account: str | None,
    alias_groups: Mapping[str, Iterable[str]],
) -> bool:
    """`person` 书绑的那个人是否在书自己的扫描深度里发过言

    只看书自己的 `scan_depth`，不是窗口长度：一本深扫的书与一本浅扫的书绑的是不同的人，
    用同一个窗口长度会让「窗口取大了」顺手把一本浅扫的书也放行。

    账号判据直接调 :func:`nekro_agent.services.memory.persons.human_accounts`，不复制：
    复制一份就会出现「沉淀时排除 Bot、世界书里没排除」这种只在一个方向上错的差异。
    `human_accounts` 只读 `sender_id` / `platform_userid`，`WindowMessage` 两个字段都有。

    `alias_groups` 是「账号 → 它所属的人的全部账号」的展开结果，调用方一次算好传进来
    （每本书都现查别名表会把这一层变成 N 次查询）。拼错、缺项都按「只有它自己」处理：
    少放行一本书好过多放行一本书。

    在场判据与 :func:`present_person_accounts` 是同一套（后者返回具体账号）；这里只回答
    「在不在」，两者对同一组输入的结论必须逐例一致。
    """
    if book.scope_kind != "person":
        # 其它类型的书不受「人在不在场」约束；返回 True 而不是抛错，是因为调用方会统一
        # 对所有书调这个函数，按类型提前返回只是它自己的优化。
        return True
    return bool(
        present_person_accounts(
            book,
            window,
            bot_account=bot_account,
            alias_groups=alias_groups,
        ),
    )


@dataclass(frozen=True)
class _MessageView:
    """`human_accounts` 需要的两列

    `human_accounts` 的签名收的是消息行，而这一层只该看到 :class:`WindowMessage`。
    用一个只带两个属性的视图转接，比让引擎去依赖 ORM 行更安全：窗口永远拿不到超出
    「谁发的」之外的信息。
    """

    sender_id: str
    platform_userid: str
