"""世界书的命中判定

判据是「哪一条进提示词」的唯一依据，因此这里逐条钉死：主关键词、四种次关键词逻辑、
常驻、大小写与整词的两种默认来源、归一化的三类写法、扫描深度、扫描文本不含昵称、
`person` 书的在场判定（含别名与 Bot 排除）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from nekro_agent.services.worldbook.matcher import (
    HitReason,
    book_active_for_person,
    entry_hit,
    key_hit,
    normalize_for_match,
    scan_text,
)
from nekro_agent.services.worldbook.safe_regex import RegexRunner
from nekro_agent.services.worldbook.types import (
    TIMED_MAX,
    MatchBook,
    MatchEntry,
    Position,
    ScopeKind,
    SelectiveLogic,
    WindowMessage,
    book_to_match,
    entry_to_match,
)


def make_book(**extra: object) -> MatchBook:
    """一本域内书；只给用例关心的字段"""
    defaults: dict[str, object] = {
        "id": 1,
        "scope_kind": ScopeKind.DOMAIN,
        "person_account": "",
        "scan_depth": 4,
        "budget_chars": 0,
        "case_sensitive": False,
        "match_whole_words": False,
    }
    defaults.update(extra)
    return MatchBook(**defaults)  # type: ignore[arg-type]


def make_entry(**extra: object) -> MatchEntry:
    """一条条目；默认按「非常驻、无定时、非正则」的普通条目"""
    defaults: dict[str, object] = {
        "id": 10,
        "book_id": 1,
        "keys": ("龙",),
        "secondary_keys": (),
        "selective_logic": SelectiveLogic.AND_ANY,
        "use_regex": False,
        "constant": False,
        "content": "这是设定正文",
        "order": 100,
        "position": Position.BEFORE_HISTORY,
        "depth": 4,
        "scan_depth": 4,
        "case_sensitive": False,
        "match_whole_words": False,
        "sticky": 0,
        "cooldown": 0,
        "delay": 0,
        "trusted": False,
        "invalid_keys": frozenset(),
    }
    defaults.update(extra)
    return MatchEntry(**defaults)  # type: ignore[arg-type]


def window(*texts: str, start_id: int = 1) -> list[WindowMessage]:
    """按顺序造一批同一个人的消息；`id` 自增表示时间顺序"""
    return [
        WindowMessage(id=start_id + index, sender_id="s1", platform_userid="u1", content_text=text)
        for index, text in enumerate(texts)
    ]


def runner() -> RegexRunner:
    return RegexRunner()


class TestPrimaryKeys:
    def test_any_primary_key_hits(self) -> None:
        entry = make_entry(keys=("龙", "凤凰"))
        hit = entry_hit(entry, window("他看见一只凤凰飞过"), runner())
        assert hit is not None
        assert hit.kind == "key"
        assert hit.key == "凤凰"

    def test_first_hit_key_is_reported(self) -> None:
        """明细要说清「为什么这条进来了」，因此记第一个命中的关键词"""
        entry = make_entry(keys=("凤凰", "龙"))
        hit = entry_hit(entry, window("龙与凤凰"), runner())
        assert hit is not None
        assert hit.key == "凤凰"

    def test_no_key_hits(self) -> None:
        entry = make_entry(keys=("龙", "凤凰"))
        assert entry_hit(entry, window("今天天气不错"), runner()) is None

    def test_empty_keys_never_hit(self) -> None:
        """非常驻又没有主关键词：永不命中，而不是「匹配一切」"""
        assert entry_hit(make_entry(keys=()), window("任意内容"), runner()) is None

    def test_constant_entry_needs_no_key(self) -> None:
        hit = entry_hit(make_entry(keys=(), constant=True), window("任意内容"), runner())
        assert hit == HitReason(kind="constant")


class TestSecondaryLogic:
    """四种次关键词逻辑各一对正反例"""

    def _entry(self, logic: SelectiveLogic) -> MatchEntry:
        return make_entry(keys=("龙",), secondary_keys=("火", "冰"), selective_logic=logic)

    def test_and_any(self) -> None:
        entry = self._entry(SelectiveLogic.AND_ANY)
        assert entry_hit(entry, window("龙与火"), runner()) is not None
        assert entry_hit(entry, window("龙与水"), runner()) is None

    def test_and_all(self) -> None:
        entry = self._entry(SelectiveLogic.AND_ALL)
        assert entry_hit(entry, window("龙与火与冰"), runner()) is not None
        assert entry_hit(entry, window("龙与火"), runner()) is None

    def test_not_any(self) -> None:
        entry = self._entry(SelectiveLogic.NOT_ANY)
        assert entry_hit(entry, window("龙与水"), runner()) is not None
        assert entry_hit(entry, window("龙与冰"), runner()) is None

    def test_not_all(self) -> None:
        entry = self._entry(SelectiveLogic.NOT_ALL)
        assert entry_hit(entry, window("龙与火"), runner()) is not None
        assert entry_hit(entry, window("龙与火与冰"), runner()) is None

    def test_empty_secondary_always_passes(self) -> None:
        """只写主关键词是最常见的用法，次关键词这一层不该参与判定"""
        entry = make_entry(keys=("龙",), secondary_keys=(), selective_logic=SelectiveLogic.AND_ALL)
        hit = entry_hit(entry, window("龙"), runner())
        assert hit is not None
        assert hit.secondary == ()

    def test_secondary_hits_are_recorded(self) -> None:
        entry = self._entry(SelectiveLogic.AND_ANY)
        hit = entry_hit(entry, window("龙与火与冰"), runner())
        assert hit is not None
        assert hit.secondary == ("火", "冰")


class TestCaseSensitivity:
    def test_case_insensitive_by_default(self) -> None:
        entry = make_entry(keys=("Dragon",), case_sensitive=False)
        assert entry_hit(entry, window("a DRAGON appears"), runner()) is not None

    def test_case_sensitive_on_entry(self) -> None:
        entry = make_entry(keys=("Dragon",), case_sensitive=True)
        assert entry_hit(entry, window("a DRAGON appears"), runner()) is None
        assert entry_hit(entry, window("a Dragon appears"), runner()) is not None

    def test_book_default_is_filled_by_caller(self) -> None:
        """书的默认由 `entry_to_match` 补进条目，引擎只看条目的值"""
        book = make_book(case_sensitive=True)
        entry = make_entry(keys=("Dragon",), case_sensitive=book.case_sensitive)
        assert entry_hit(entry, window("DRAGON"), runner()) is None


class TestWholeWords:
    def test_ascii_key_needs_boundary(self) -> None:
        entry = make_entry(keys=("cat",), match_whole_words=True)
        assert entry_hit(entry, window("a cat sits"), runner()) is not None
        assert entry_hit(entry, window("concatenate"), runner()) is None
        assert entry_hit(entry, window("cat_9"), runner()) is None

    def test_ascii_key_without_whole_words_is_substring(self) -> None:
        entry = make_entry(keys=("cat",), match_whole_words=False)
        assert entry_hit(entry, window("concatenate"), runner()) is not None

    def test_chinese_key_is_always_substring(self) -> None:
        """中文没有词边界：开不开整词都按子串，否则整词对中文恒不命中"""
        entry = make_entry(keys=("龙",), match_whole_words=True)
        assert entry_hit(entry, window("那条龙的鳞片"), runner()) is not None

    def test_book_default_is_used_by_caller(self) -> None:
        book = make_book(match_whole_words=True)
        entry = make_entry(keys=("cat",), match_whole_words=book.match_whole_words)
        assert entry_hit(entry, window("concatenate"), runner()) is None


class TestNormalization:
    def test_fullwidth_forms_match(self) -> None:
        entry = make_entry(keys=("Dragon",))
        assert entry_hit(entry, window("ＤＲＡＧＯＮ 出现"), runner()) is not None

    def test_zero_width_between_chars_matches(self) -> None:
        """在字之间插零宽字符是最常见的绕过写法"""
        entry = make_entry(keys=("龙族",))
        assert entry_hit(entry, window("龙\u200b族"), runner()) is not None

    def test_whitespace_is_collapsed_not_removed(self) -> None:
        """连续空白压成一个空格：英文关键词要靠空格分词，删空白会让 cat 命中 concatenate"""
        assert normalize_for_match("  a   b  ", case_sensitive=False) == "a b"
        entry = make_entry(keys=("a b",))
        assert entry_hit(entry, window("x   a \t b   y"), runner()) is not None

    def test_empty_key_after_normalization_never_hits(self) -> None:
        """只由不可见字符组成的关键词归一化后为空，不能变成「匹配一切」"""
        entry = make_entry(keys=("\u200b",))
        assert entry_hit(entry, window("任意内容"), runner()) is None

    def test_casefold_before_compare(self) -> None:
        assert normalize_for_match("STRASSE", case_sensitive=False) == normalize_for_match(
            "straße", case_sensitive=False
        )


class TestScanWindow:
    def test_scan_text_uses_last_messages_in_time_order(self) -> None:
        messages = window("一", "二", "三", "四")
        assert scan_text(messages, 2) == "三\n四"

    def test_scan_text_sorts_by_id(self) -> None:
        """调用方常按 id 倒序取最近 N 条，窗口顺序不能直接信"""
        messages = list(reversed(window("一", "二", "三")))
        assert scan_text(messages, 2) == "二\n三"

    def test_scan_text_has_no_nickname(self) -> None:
        """昵称不参与扫描：类型里就没有这个字段"""
        message = WindowMessage(id=1, sender_id="s1", platform_userid="u1", content_text="正文")
        assert scan_text([message], 1) == "正文"
        assert not hasattr(message, "sender_nickname")

    def test_scan_text_zero_depth_is_empty(self) -> None:
        assert scan_text(window("一"), 0) == ""

    def test_entry_scan_depth_overrides_book(self) -> None:
        """条目的扫描深度覆盖书的：只扫一条时，更早的关键词看不见"""
        entry = make_entry(keys=("龙",), scan_depth=1)
        assert entry_hit(entry, window("龙", "无关"), runner()) is None
        assert entry_hit(entry, window("无关", "龙"), runner()) is not None

    def test_deeper_scan_sees_older_messages(self) -> None:
        entry = make_entry(keys=("龙",), scan_depth=3)
        assert entry_hit(entry, window("龙", "无关", "无关"), runner()) is not None

    def test_constant_entry_ignores_scan_window(self) -> None:
        entry = make_entry(keys=(), constant=True, scan_depth=1)
        assert entry_hit(entry, [], runner()) is not None


class TestDisabledKeys:
    def test_invalid_keys_are_skipped(self) -> None:
        """导入时判为无效的关键词留在 keys 里以便导出，但匹配时必须跳过"""
        entry = make_entry(keys=("龙",), invalid_keys=frozenset({"龙"}))
        assert key_hit("龙", entry, "龙的传说", runner()) is False
        assert entry_hit(entry, window("龙的传说"), runner()) is None

    def test_valid_sibling_key_still_works(self) -> None:
        entry = make_entry(keys=("龙", "凤凰"), invalid_keys=frozenset({"凤凰"}))
        hit = entry_hit(entry, window("凤凰与龙"), runner())
        assert hit is not None
        assert hit.key == "龙"


class TestConversion:
    """ORM 行到引擎输入的转换：空覆盖按书补齐、越界夹值、无效关键词取出来"""

    def _row(self, **extra: object) -> object:
        """造一个只有属性的假行：转换函数只读属性、不查库"""
        values: dict[str, object] = {
            "id": 3,
            "scope_kind": "person",
            "person_account": "u1",
            "scan_depth": 12,
            "budget_chars": 500,
            "case_sensitive": True,
            "match_whole_words": True,
        }
        values.update(extra)
        return SimpleNamespace(**values)

    def _entry_row(self, **extra: object) -> object:
        values: dict[str, object] = {
            "id": 9,
            "book_id": 3,
            "keys": ["龙", 7],
            "secondary_keys": ["火"],
            "selective_logic": "and_all",
            "use_regex": False,
            "constant": False,
            "content": "正文",
            "order": 100,
            "position": "at_depth",
            "depth": 2,
            "scan_depth": None,
            "case_sensitive": None,
            "match_whole_words": None,
            "sticky": None,
            "cooldown": 5,
            "delay": None,
            "trusted": True,
            "ext": {},
        }
        values.update(extra)
        return SimpleNamespace(**values)

    def test_book_conversion(self) -> None:
        book = book_to_match(self._row())
        assert book.scope_kind is ScopeKind.PERSON
        assert book.person_account == "u1"
        assert book.scan_depth == 12
        assert book.budget_chars == 500
        assert book.case_sensitive is True

    def test_book_scan_depth_is_clamped(self) -> None:
        assert book_to_match(self._row(scan_depth=0)).scan_depth == 1
        assert book_to_match(self._row(scan_depth=999)).scan_depth == 50

    def test_unknown_scope_kind_falls_back_to_domain(self) -> None:
        """坏数据不给异常：范围条件按 `scope_kind` 精确匹配，这样的书一条腿都选不中"""
        assert book_to_match(self._row(scope_kind="nope")).scope_kind is ScopeKind.DOMAIN

    def test_entry_inherits_book_defaults_when_overrides_are_null(self) -> None:
        book = book_to_match(self._row(scan_depth=12, case_sensitive=True, match_whole_words=True))
        entry = entry_to_match(self._entry_row(), book)
        assert entry.scan_depth == 12
        assert entry.case_sensitive is True
        assert entry.match_whole_words is True

    def test_entry_overrides_win(self) -> None:
        book = book_to_match(self._row())
        entry = entry_to_match(self._entry_row(scan_depth=3, case_sensitive=False, match_whole_words=False), book)
        assert entry.scan_depth == 3
        assert entry.case_sensitive is False
        assert entry.match_whole_words is False

    def test_entry_scan_depth_is_clamped_even_with_book(self) -> None:
        book = book_to_match(self._row(scan_depth=12))
        assert entry_to_match(self._entry_row(scan_depth=0), book).scan_depth == 1
        assert entry_to_match(self._entry_row(scan_depth=500), book).scan_depth == 50

    def test_non_string_keys_are_dropped(self) -> None:
        entry = entry_to_match(self._entry_row(keys=["龙", 7, None]), book_to_match(self._row()))
        assert entry.keys == ("龙",)

    def test_null_timed_values_become_zero(self) -> None:
        entry = entry_to_match(self._entry_row(), book_to_match(self._row()))
        assert entry.sticky == 0
        assert entry.cooldown == 5
        assert entry.delay == 0

    def test_timed_values_are_clamped(self) -> None:
        entry = entry_to_match(self._entry_row(sticky=999), book_to_match(self._row()))
        assert entry.sticky == TIMED_MAX

    def test_invalid_keys_come_from_ext(self) -> None:
        entry = entry_to_match(
            self._entry_row(ext={"na_invalid_keys": [{"key": "/x/q", "reason": "旗标"}, {"key": 5}]}),
            book_to_match(self._row()),
        )
        assert entry.invalid_keys == frozenset({"/x/q"})

    def test_broken_ext_does_not_raise(self) -> None:
        for ext in (None, "x", {"na_invalid_keys": "x"}, {"na_invalid_keys": [1, 2]}):
            entry = entry_to_match(self._entry_row(ext=ext), book_to_match(self._row()))
            assert entry.invalid_keys == frozenset()

    def test_unknown_enums_fall_back(self) -> None:
        entry = entry_to_match(
            self._entry_row(selective_logic="nope", position="nope"),
            book_to_match(self._row()),
        )
        assert entry.selective_logic is SelectiveLogic.AND_ANY
        assert entry.position is Position.BEFORE_HISTORY


@dataclass(frozen=True)
class _WindowCase:
    """`person` 书判定的一组输入"""

    texts: tuple[str, ...]
    accounts: tuple[str, ...]

    def messages(self) -> list[WindowMessage]:
        return [
            WindowMessage(id=index + 1, sender_id="s1", platform_userid=account, content_text=text)
            for index, (text, account) in enumerate(zip(self.texts, self.accounts))
        ]


class TestPersonBook:
    def _book(self, account: str = "u1", depth: int = 4) -> MatchBook:
        return make_book(scope_kind=ScopeKind.PERSON, person_account=account, scan_depth=depth)

    def test_other_scope_kinds_are_always_active(self) -> None:
        """非 person 书不受「人在不在场」约束"""
        book = make_book(scope_kind=ScopeKind.DOMAIN, person_account="u1")
        assert book_active_for_person(book, [], bot_account=None, alias_groups={}) is True

    def test_person_speaks_within_depth(self) -> None:
        case = _WindowCase(("你好", "在吗"), ("u1", "u1"))
        assert book_active_for_person(self._book(), case.messages(), bot_account=None, alias_groups={}) is True

    def test_person_only_spoke_too_early(self) -> None:
        """只在更早的消息里出现：书的扫描深度没覆盖到，不算在场"""
        messages = [
            WindowMessage(id=1, sender_id="s1", platform_userid="u1", content_text="我来了"),
            WindowMessage(id=2, sender_id="s1", platform_userid="u2", content_text="嗯"),
            WindowMessage(id=3, sender_id="s1", platform_userid="u2", content_text="好"),
        ]
        assert book_active_for_person(self._book(depth=2), messages, bot_account=None, alias_groups={}) is False
        assert book_active_for_person(self._book(depth=3), messages, bot_account=None, alias_groups={}) is True

    def test_alias_account_counts(self) -> None:
        """同一个人用小号发言也算在场：别名展开由调用方算好传进来"""
        case = _WindowCase(("我是小号",), ("u2",))
        book = self._book(account="u1")
        assert book_active_for_person(book, case.messages(), bot_account=None, alias_groups={}) is False
        assert (
            book_active_for_person(
                book, case.messages(), bot_account=None, alias_groups={"u2": frozenset({"u1", "u2"})}
            )
            is True
        )

    def test_named_account_is_the_alias_target(self) -> None:
        """书绑的是主账号，小号出现在窗口里时通过别名表展开到主账号"""
        case = _WindowCase(("发言",), ("u9",))
        book = self._book(account="u9")
        assert book_active_for_person(book, case.messages(), bot_account=None, alias_groups={}) is True

    def test_bot_account_is_excluded(self) -> None:
        case = _WindowCase(("我是 Bot",), ("bot1",))
        book = self._book(account="bot1")
        assert book_active_for_person(book, case.messages(), bot_account="bot1", alias_groups={}) is False

    def test_bot_sender_row_is_excluded(self) -> None:
        """`sender_id = "-1"` 的行不管账号是谁都不算人"""
        messages = [WindowMessage(id=1, sender_id="-1", platform_userid="u1", content_text="Bot 发言")]
        assert book_active_for_person(self._book(), messages, bot_account=None, alias_groups={}) is False

    def test_blank_account_rows_are_skipped(self) -> None:
        messages = [WindowMessage(id=1, sender_id="s1", platform_userid="   ", content_text="x")]
        assert book_active_for_person(self._book(), messages, bot_account=None, alias_groups={}) is False

    def test_book_without_account_is_never_active(self) -> None:
        case = _WindowCase(("你好",), ("u1",))
        book = make_book(scope_kind=ScopeKind.PERSON, person_account="")
        assert book_active_for_person(book, case.messages(), bot_account=None, alias_groups={}) is False


@pytest.mark.parametrize("depth", [1, 2, 3, 50])
def test_scan_text_depth_bounds(depth: int) -> None:
    """深度在 1 到 50 之间都只取末尾若干条，不抛错"""
    messages = window(*[f"第 {index} 条" for index in range(1, 6)])
    expected_count = min(depth, len(messages))
    assert len(scan_text(messages, depth).split("\n")) == expected_count


def test_replace_keeps_frozen_types() -> None:
    """冻结数据类可以被派生（引擎各处都这么做），派生结果仍是同一个类型"""
    entry = make_entry()
    derived = replace(entry, keys=("凤凰",))
    assert isinstance(derived, MatchEntry)
    assert derived.keys == ("凤凰",)


def test_runner_is_required_for_regex_keys() -> None:
    """正则关键词必须走 runner：直接按子串找不到它，因此不该「碰巧命中」"""
    entry = make_entry(keys=("/龙./",))
    assert entry_hit(entry, window("龙与凤凰"), runner()) is not None
    plain = make_entry(keys=("/龙./",), invalid_keys=frozenset({"/龙./"}))
    assert entry_hit(plain, window("龙与凤凰"), runner()) is None


def _reference_normalize(text: str, case_sensitive: bool) -> str:
    """归一化的原始写法（每个字都做 NFKC），用来对照加了快速路径之后结果一字不差"""
    import unicodedata

    from nekro_agent.services.output_filter.normalize import is_invisible_char

    def strip(value: str) -> str:
        return "".join(char for char in value if not is_invisible_char(char))

    normalized = "".join(strip(unicodedata.normalize("NFKC", char)) for char in strip(text))
    collapsed = " ".join(normalized.split())
    return collapsed if case_sensitive else collapsed.casefold()


class TestNormalizeFastPath:
    @pytest.mark.parametrize(
        "text",
        [
            "龙与凤凰",
            "Dragon  Slayer",
            "ＤＲＡＧＯＮ",  # 全角
            "ﬁre",  # 连字
            "龙\u200b凤",  # 零宽空格
            "e\u0301clair",  # 组合字符
            "\u2f00一",  # 康熙部首
            "Ⅻ号",  # 罗马数字兼容字符
            "STRASSE ß",
            "\ufeff前导 BOM",
            "",
        ],
    )
    @pytest.mark.parametrize("case_sensitive", [False, True])
    def test_fast_path_matches_the_per_char_reference(self, text: str, case_sensitive: bool) -> None:
        from nekro_agent.services.worldbook import matcher

        assert matcher._normalize(text, case_sensitive) == _reference_normalize(text, case_sensitive)
        assert normalize_for_match(text, case_sensitive=case_sensitive) == _reference_normalize(text, case_sensitive)

    def test_many_keywords_do_not_evict_the_scan_text(self) -> None:
        """一条条目上千个关键词也只归一化一次扫描文本：关键词不再与扫描文本共用缓存"""
        from nekro_agent.services.worldbook import matcher

        matcher._normalized_scan_text.cache_clear()
        entries = [
            make_entry(id=index, keys=tuple(f"不会命中的词{index}-{n}" for n in range(300)))
            for index in range(5)
        ]
        messages = window("今天天气不错", "一起去吃饭吧")
        for entry in entries:
            assert entry_hit(entry, messages, runner()) is None
        info = matcher._normalized_scan_text.cache_info()
        assert info.misses == 1
        assert info.hits >= 5 * 300 - 1
