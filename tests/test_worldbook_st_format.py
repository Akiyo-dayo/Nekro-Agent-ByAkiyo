"""酒馆世界书格式的导入与导出

最要紧的一条验收是「导入再导出逐字段相等」：拿一份覆盖**全部** `newWorldInfoEntryDefinition`
字段的条目（含本插件用不上的字段、`null` 值、各种 `position` 与 `selectiveLogic`、非字符串
关键词、超长内容、越界数值），走一遍 `parse → st_entry_to_columns → columns_to_st_entry →
export_world`，结果必须与原对象键集合与值都相同。

这条用例挡住的是最不可接受的一类缺陷：一次「导入再导出」之后用户的书少了内容。它同时
覆盖了两个方向——`null` 不能被写成默认值，插件用不上的字段不能被丢掉。

另外几组用例各管一件事：文件的六种拒绝理由（各自独立，混在一起就分不清哪一条没生效）、
报告的计数、正则关键词只报正则。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nekro_agent.services.worldbook.st_format import (
    MAX_ENTRIES,
    MAX_FILE_BYTES,
    MAX_JSON_DEPTH,
    NOTE_KINDS,
    ST_ENTRY_DEFAULTS,
    ImportNote,
    WorldFileRejected,
    budget_notes,
    columns_to_st_entry,
    export_world,
    parse_world_file,
    st_entry_to_columns,
    summarize_notes,
    validate_entry_regexes,
)
from nekro_agent.services.worldbook.types import MAX_COMMENT_CHARS, MAX_CONTENT_CHARS, MAX_KEY_LENGTH, MAX_KEYS
from tests.worldbook_st_fixtures import dump, full_entry, round_trip, world_of


def legacy_round_trip(entry: dict[str, Any]) -> dict[str, Any]:
    """旧数据的往返：不经过文件解析（它现在会拒绝超限），直接映射再导出"""
    columns, _ = st_entry_to_columns(entry)
    return export_world({}, [(columns, entry)])["entries"][str(columns["uid"])]


def json_depth(data: Any) -> int:
    """独立算一遍 JSON 深度，用来把「33 层」这个前提钉住"""
    deepest = 0
    stack: list[tuple[Any, int]] = [(data, 1)]
    while stack:
        node, depth = stack.pop()
        deepest = max(deepest, depth)
        if isinstance(node, dict):
            stack.extend((value, depth + 1) for value in node.values())
        elif isinstance(node, list):
            stack.extend((value, depth + 1) for value in node)
    return deepest


def nested_payload(layers: int) -> dict[str, Any]:
    """造一份最大深度恰好是 `BASE_DEPTH + layers` 的世界书文件"""
    node: dict[str, Any] = {}
    for _ in range(layers):
        node = {"deep": node}
    return {"entries": {"1": {"content": "", "extra": node}}}


# `nested_payload(0)` 的深度：顶层对象 1 层、`entries` 2 层、条目的 `extra` 3 层。
BASE_DEPTH = json_depth(nested_payload(0))


class TestDefaults:
    def test_covers_every_definition_key(self) -> None:
        """逐个照抄酒馆 `newWorldInfoEntryDefinition` 的 42 个键，不多不少"""
        assert len(ST_ENTRY_DEFAULTS) == 42

    def test_uid_is_not_a_default(self) -> None:
        """`uid` 是每条自己的编号，不属于「新条目模板」"""
        assert "uid" not in ST_ENTRY_DEFAULTS

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("key", []),
            ("selective", True),
            ("selectiveLogic", 0),
            ("order", 100),
            ("position", 0),
            ("depth", 4),
            ("groupWeight", 100),
            ("probability", 100),
            ("scanDepth", None),
            ("caseSensitive", None),
            ("matchWholeWords", None),
            ("sticky", None),
            ("cooldown", None),
            ("delay", None),
            ("triggers", []),
        ],
    )
    def test_spot_check_values(self, key: str, value: Any) -> None:
        assert ST_ENTRY_DEFAULTS[key] == value

    def test_defaults_are_not_shared_mutable_state(self) -> None:
        """导出时以 `ST_ENTRY_DEFAULTS` 为底，必须是深拷贝，否则改一次就污染后面所有条目"""
        columns, _ = st_entry_to_columns({})
        first = columns_to_st_entry(columns, {})
        first["key"].append("污染")
        second = columns_to_st_entry(columns, {})
        assert second["key"] == []


class TestRoundTrip:
    def test_full_entry_round_trips_field_by_field(self) -> None:
        original = full_entry(1)
        assert round_trip(original) == original

    @pytest.mark.parametrize("position", [0, 1, 2, 3, 4, 5, 6])
    def test_every_supported_position_round_trips(self, position: int) -> None:
        """2 / 3 / 5 / 6 都映射到「历史之前」，导出时必须还原成原值"""
        original = full_entry(1, position=position)
        assert round_trip(original) == original

    def test_outlet_position_round_trips_without_loss(self) -> None:
        """`outlet` 在本插件里没有对应位置：列上降级成历史之前并停用，但导出要还原 7"""
        original = full_entry(1, position=7)
        assert round_trip(original) == original

    @pytest.mark.parametrize("logic", [0, 1, 2, 3])
    def test_every_selective_logic_round_trips(self, logic: int) -> None:
        original = full_entry(1, selectiveLogic=logic)
        assert round_trip(original) == original

    def test_null_values_stay_null(self) -> None:
        """`null` 不能被写成默认值：三态覆盖的「跟书」与「显式 false」是两件事"""
        original = full_entry(1, scanDepth=None, caseSensitive=None, matchWholeWords=None, sticky=None)
        restored = round_trip(original)
        assert restored["scanDepth"] is None
        assert restored["caseSensitive"] is None
        assert restored["matchWholeWords"] is None
        assert restored["sticky"] is None

    def test_unsupported_fields_are_preserved(self) -> None:
        """插件用不上的字段原样保留：只存映射列会让导出少一大截内容"""
        original = full_entry(1, **{"vectorized": True, "role": 2, "automationId": "a1", "triggers": ["gen"]})
        restored = round_trip(original)
        assert restored["vectorized"] is True
        assert restored["role"] == 2
        assert restored["automationId"] == "a1"
        assert restored["triggers"] == ["gen"]

    def test_legacy_truncated_content_round_trips(self) -> None:
        """旧数据：以前导入时正文被截断存列、原文在 `ext` 里；没被编辑过时导出仍是原长

        现在导入超长就整份拒绝，走不到截断；这条钉的是「库里已有的旧行导出不丢字」，所以
        绕过文件解析，直接拿原始条目映射。
        """
        original = full_entry(1, content="甲" * (MAX_CONTENT_CHARS + 50))
        assert legacy_round_trip(original)["content"] == original["content"]

    def test_clamped_numbers_round_trip(self) -> None:
        """越界数值被夹进区间存列，导出仍是原值"""
        original = full_entry(1, order=10**8, depth=500, sticky=500, scanDepth=99)
        restored = round_trip(original)
        assert restored["order"] == 10**8
        assert restored["depth"] == 500
        assert restored["sticky"] == 500
        assert restored["scanDepth"] == 99

    def test_mixed_key_types_round_trip(self) -> None:
        """非字符串关键词被丢弃存列，但原文件里的那一项要回来"""
        original = full_entry(1, key=["龙", 7, None, "凤凰"])
        restored = round_trip(original)
        assert restored["key"] == ["龙", 7, None, "凤凰"]

    def test_legacy_too_many_keys_round_trip(self) -> None:
        original = full_entry(1, key=[f"k{index}" for index in range(MAX_KEYS + 5)])
        assert legacy_round_trip(original)["key"] == original["key"]

    def test_legacy_overlong_key_round_trips(self) -> None:
        original = full_entry(1, key=["甲" * (MAX_KEY_LENGTH + 30)])
        assert legacy_round_trip(original)["key"] == original["key"]

    def test_long_but_legal_entry_round_trips_untouched(self) -> None:
        """大世界书里常见的长条目（几万字正文、几百个关键词、长标题）完整导入，一字不截"""
        original = full_entry(
            1,
            content="设定" * 30_000,
            comment="备注" * 1000,
            key=[f"关键词{index}" for index in range(300)],
        )
        parsed = parse_world_file(dump(world_of([original])))
        columns, notes = st_entry_to_columns(parsed.entries[0][1])
        assert columns["content"] == original["content"]
        assert columns["comment"] == original["comment"]
        assert columns["keys"] == original["key"]
        assert "truncated" not in [note.kind for note in notes]
        assert round_trip(original) == original

    def test_bad_logic_value_round_trips(self) -> None:
        original = full_entry(1, selectiveLogic=99)
        restored = round_trip(original)
        assert restored["selectiveLogic"] == 99

    def test_multiple_entries_round_trip(self) -> None:
        """一本书里多条一起走：键是 `uid` 的字符串形态"""
        entries = [full_entry(1), full_entry(2, position=4), full_entry(3, disable=True)]
        parsed = parse_world_file(dump(world_of(entries)))
        pairs = []
        for _, raw in parsed.entries:
            columns, _ = st_entry_to_columns(raw)
            pairs.append((columns, raw))
        exported = export_world(parsed.book_ext, pairs)
        for entry in entries:
            assert exported["entries"][str(entry["uid"])] == entry


class TestEditsAreExported:
    def test_changing_one_column_changes_only_that_field(self) -> None:
        original = full_entry(1)
        parsed = parse_world_file(dump(world_of([original])))
        _, raw = parsed.entries[0]
        columns, _ = st_entry_to_columns(raw)
        columns["content"] = "改过的正文"

        restored = columns_to_st_entry(columns, raw)
        assert restored["content"] == "改过的正文"
        differing = {key for key in restored if restored[key] != original[key]}
        assert differing == {"content"}

    def test_changing_position_writes_the_canonical_value(self) -> None:
        """改过位置之后按 NA 列写：历史之前 → 6（不再记得原来是 2 还是 3）"""
        original = full_entry(1, position=2)
        parsed = parse_world_file(dump(world_of([original])))
        _, raw = parsed.entries[0]
        columns, _ = st_entry_to_columns(raw)
        columns["position"] = "after_persona"
        assert columns_to_st_entry(columns, raw)["position"] == 1

    def test_disable_follows_enabled_column(self) -> None:
        original = full_entry(1, disable=False)
        parsed = parse_world_file(dump(world_of([original])))
        _, raw = parsed.entries[0]
        columns, _ = st_entry_to_columns(raw)
        columns["enabled"] = False
        assert columns_to_st_entry(columns, raw)["disable"] is True

    def test_uid_always_comes_from_the_column(self) -> None:
        """追加导入时 `uid` 会被重编号，导出必须以列为准"""
        original = full_entry(1)
        parsed = parse_world_file(dump(world_of([original])))
        _, raw = parsed.entries[0]
        columns, _ = st_entry_to_columns(raw)
        columns["uid"] = 77
        assert columns_to_st_entry(columns, raw)["uid"] == 77

    def test_na_keys_never_leak_into_the_export(self) -> None:
        """`ext.na_*` 是插件自有的附加信息，导出时必须剔掉"""
        original = full_entry(1)
        raw = dict(original, na_invalid_keys=[{"key": "/x/q", "reason": "旗标"}])
        columns, _ = st_entry_to_columns(raw)
        exported = columns_to_st_entry(columns, raw)
        assert not any(str(key).startswith("na_") for key in exported)

    def test_book_level_ext_is_preserved(self) -> None:
        """书级附加字段（`originalData` 之类）原样导出，`na_` 开头的剔掉"""
        book_ext = {"originalData": {"name": "卡"}, "token_budget": 900, "na_owner": "x"}
        exported = export_world(book_ext, [])
        assert exported["originalData"] == {"name": "卡"}
        assert exported["token_budget"] == 900
        assert "na_owner" not in exported
        assert exported["entries"] == {}


class TestColumnsMapping:
    def test_logic_mapping(self) -> None:
        expected = {0: "and_any", 1: "not_all", 2: "not_any", 3: "and_all"}
        for st_value, column in expected.items():
            columns, _ = st_entry_to_columns({"uid": 1, "selectiveLogic": st_value})
            assert columns["selective_logic"] == column

    def test_position_mapping(self) -> None:
        expected = {
            0: "after_persona",
            1: "after_persona",
            2: "before_history",
            3: "before_history",
            4: "at_depth",
            5: "before_history",
            6: "before_history",
            7: "before_history",
        }
        for st_value, column in expected.items():
            columns, _ = st_entry_to_columns({"uid": 1, "position": st_value})
            assert columns["position"] == column

    def test_outlet_is_disabled(self) -> None:
        columns, notes = st_entry_to_columns({"uid": 1, "position": 7})
        assert columns["enabled"] is False
        assert any(note.kind == "position_downgraded" for note in notes)

    def test_disable_becomes_enabled(self) -> None:
        columns, _ = st_entry_to_columns({"uid": 1, "disable": True})
        assert columns["enabled"] is False

    def test_uid_not_integer_is_none(self) -> None:
        """`uid` 读不出来时交给调用方编号，不擅自造一个"""
        columns, notes = st_entry_to_columns({"uid": "abc"})
        assert columns["uid"] is None
        assert any(note.kind == "bad_value" and note.field == "uid" for note in notes)

    def test_missing_fields_use_defaults(self) -> None:
        columns, notes = st_entry_to_columns({"uid": 1})
        assert columns["selective_logic"] == "and_any"
        assert columns["order"] == 100
        assert columns["depth"] == 4
        assert columns["content"] == ""
        assert notes == []

    def test_columns_have_no_forbidden_keys(self) -> None:
        """列字典里不带 `book` / `trusted` / `status` / `ext`：那几项由调用方决定"""
        columns, _ = st_entry_to_columns({"uid": 1})
        for forbidden in ("book", "trusted", "status", "ext"):
            assert forbidden not in columns

    def test_ignore_budget_is_read_as_a_plain_boolean(self) -> None:
        on, notes = st_entry_to_columns({"uid": 1, "ignoreBudget": True})
        off, _ = st_entry_to_columns({"uid": 1})
        assert (on["ignore_budget"], off["ignore_budget"]) == (True, False)
        assert notes == []

    def test_unreadable_ignore_budget_is_false_with_a_note(self) -> None:
        columns, notes = st_entry_to_columns({"uid": 1, "ignoreBudget": "yes"})
        assert columns["ignore_budget"] is False
        assert [(note.field, note.kind) for note in notes] == [("ignoreBudget", "bad_value")]

    def test_edited_ignore_budget_is_exported_as_ignore_budget(self) -> None:
        original = full_entry(1, ignoreBudget=False)
        columns, _ = st_entry_to_columns(original)
        columns["ignore_budget"] = True
        restored = columns_to_st_entry(columns, original)
        assert restored["ignoreBudget"] is True
        # 没改过时保留原值，且不重复存第二份
        untouched = columns_to_st_entry(columns, dict(original, ignoreBudget=True))
        assert untouched["ignoreBudget"] is True
        assert sum(1 for key in untouched if key == "ignoreBudget") == 1


class TestNotes:
    def test_truncation_note_is_only_a_fallback_for_old_rows(self) -> None:
        """映射函数本身对超限值仍截断并记一条：只给读库里旧数据兜底，导入路径先整份拒绝了"""
        _, notes = st_entry_to_columns({"uid": 1, "content": "甲" * (MAX_CONTENT_CHARS + 1)})
        assert [note.kind for note in notes] == ["truncated"]

    def test_budget_notes_flag_entries_longer_than_the_round_budget(self) -> None:
        """按预算对象的数法比：这里用一字一 token 的替身，便于手算"""
        budget = SimpleNamespace(budget=3000, model_group="stub-group", count=len)
        notes = budget_notes(
            [("1", {"content": "甲" * 3001}), ("2", {"content": "乙" * 3000}), ("3", {"content": ""})],
            budget=budget,
        )
        assert [(note.entry_key, note.kind) for note in notes] == [("1", "over_budget")]
        assert "WORLDBOOK_BUDGET_PERCENT" in notes[0].detail and "WORLDBOOK_BUDGET_CAP_TOKENS" in notes[0].detail
        assert "3001 token" in notes[0].detail and "3000 token" in notes[0].detail
        assert "stub-group" in notes[0].detail

    def test_budget_notes_are_silent_without_a_budget(self) -> None:
        """预算读不到（配置坏了）就不提示：提示不该让导入失败"""
        assert budget_notes([("1", {"content": "甲" * 10})], budget=None) == []

    def test_clamped_note(self) -> None:
        _, notes = st_entry_to_columns({"uid": 1, "order": 10**9})
        assert [note.kind for note in notes] == ["clamped"]

    def test_skipped_note_for_non_string_key(self) -> None:
        _, notes = st_entry_to_columns({"uid": 1, "key": ["龙", 5]})
        assert [note.kind for note in notes] == ["skipped"]

    def test_bad_value_note_for_unknown_logic(self) -> None:
        _, notes = st_entry_to_columns({"uid": 1, "selectiveLogic": 42})
        assert [note.kind for note in notes] == ["bad_value"]

    def test_summarize_counts_every_kind(self) -> None:
        notes = [
            ImportNote("1", "key", "skipped", ""),
            ImportNote("1", "key", "skipped", ""),
            ImportNote("1", "content", "truncated", ""),
            ImportNote("1", "order", "clamped", ""),
            ImportNote("1", "position", "position_downgraded", ""),
        ]
        summary = summarize_notes(notes)
        assert summary["skipped"] == 2
        assert summary["truncated"] == 1
        assert summary["clamped"] == 1
        assert summary["position_downgraded"] == 1
        assert summary["invalid_key"] == 0

    def test_summarize_always_has_every_key(self) -> None:
        """每个类别都有键（0 也在）：前端可以无条件读某个键"""
        summary = summarize_notes([])
        assert set(summary) == set(NOTE_KINDS)
        assert all(value == 0 for value in summary.values())

    def test_unknown_kind_is_ignored(self) -> None:
        """未知类别不新增键，否则前端的固定列表会悄悄变长"""
        summary = summarize_notes([ImportNote("1", "x", "future_kind", "")])
        assert set(summary) == set(NOTE_KINDS)


class TestRegexValidation:
    def test_plain_keys_are_not_reported(self) -> None:
        assert validate_entry_regexes({"keys": ["龙", "凤凰"], "secondary_keys": ["火"]}) == []

    def test_slash_form_is_validated(self) -> None:
        invalid = validate_entry_regexes({"keys": ["/龙/", "/x/q"], "secondary_keys": []})
        assert [item["key"] for item in invalid] == ["/x/q"]
        assert invalid[0]["reason"]

    def test_use_regex_validates_every_key(self) -> None:
        invalid = validate_entry_regexes({"keys": ["龙(", "正常"], "secondary_keys": [], "use_regex": True})
        assert [item["key"] for item in invalid] == ["龙("]

    def test_secondary_keys_are_validated_too(self) -> None:
        invalid = validate_entry_regexes({"keys": [], "secondary_keys": ["/a)\\1/"]})
        assert [item["key"] for item in invalid] == ["/a)\\1/"]

    def test_non_string_entries_are_ignored(self) -> None:
        """列里理论上只有字符串，手工改库可能塞进别的类型：跳过而不是抛错"""
        assert validate_entry_regexes({"keys": [7, None]}) == []


class TestFileRejection:
    def test_oversized_file(self) -> None:
        data = b"x" * (MAX_FILE_BYTES + 1)
        with pytest.raises(WorldFileRejected, match="MiB"):
            parse_world_file(data)

    def test_non_utf8(self) -> None:
        with pytest.raises(WorldFileRejected, match="UTF-8"):
            parse_world_file(b"\xff\xfe\x00")

    def test_broken_json(self) -> None:
        with pytest.raises(WorldFileRejected, match="JSON"):
            parse_world_file(b"{")

    def test_top_level_not_object(self) -> None:
        with pytest.raises(WorldFileRejected, match="顶层"):
            parse_world_file(dump([1, 2, 3]))

    def test_entries_not_object(self) -> None:
        with pytest.raises(WorldFileRejected, match="entries"):
            parse_world_file(dump({"entries": []}))

    def test_missing_entries(self) -> None:
        with pytest.raises(WorldFileRejected, match="entries"):
            parse_world_file(dump({"name": "书"}))

    def test_too_many_entries(self) -> None:
        entries = {str(index): {"uid": index} for index in range(MAX_ENTRIES + 1)}
        with pytest.raises(WorldFileRejected, match="条目数"):
            parse_world_file(dump({"entries": entries}))

    @pytest.mark.parametrize(
        ("overrides", "field_word"),
        [
            ({"content": "甲" * (MAX_CONTENT_CHARS + 1)}, "正文"),
            ({"comment": "甲" * (MAX_COMMENT_CHARS + 1)}, "标题"),
            ({"key": [f"k{index}" for index in range(MAX_KEYS + 1)]}, "主关键词"),
            ({"keysecondary": [f"k{index}" for index in range(MAX_KEYS + 1)]}, "次关键词"),
            ({"key": ["甲" * (MAX_KEY_LENGTH + 1)]}, "主关键词"),
        ],
    )
    def test_entry_over_a_safety_limit_rejects_the_whole_file(
        self,
        overrides: dict[str, Any],
        field_word: str,
    ) -> None:
        """超了安全上限整份拒绝，原因里写明是哪一条、哪个字段，而不是截断后照常导入"""
        entries = [full_entry(1), full_entry(7, **overrides)]
        with pytest.raises(WorldFileRejected) as rejected:
            parse_world_file(dump(world_of(entries)))
        message = str(rejected.value)
        assert "第 7 号条目" in message
        assert field_word in message
        assert "整份未导入" in message

    def test_entry_exactly_at_the_limits_is_accepted(self) -> None:
        entry = full_entry(
            1,
            content="甲" * MAX_CONTENT_CHARS,
            comment="乙" * MAX_COMMENT_CHARS,
            key=["丙" * MAX_KEY_LENGTH] + [f"k{index}" for index in range(MAX_KEYS - 1)],
        )
        parsed = parse_world_file(dump(world_of([entry])))
        assert len(parsed.entries) == 1

    def test_card_book_entry_over_a_limit_rejects_the_card(self) -> None:
        """卡内知识书走同一套安全上限：超了整张卡拒绝（回 400 的那种异常），不截断条目"""
        from nekro_agent.services.tavern.card_book import build_book_rows
        from nekro_agent.services.tavern.errors import CardFileRejected

        book = {"entries": [{"keys": ["龙"], "content": "正常"}, {"keys": ["凤"], "content": "甲" * (MAX_CONTENT_CHARS + 1)}]}
        with pytest.raises(CardFileRejected) as rejected:
            build_book_rows(book)
        assert "卡内知识书第 2 条" in str(rejected.value)
        _, rows, _ = build_book_rows({"entries": [{"keys": ["龙"], "content": "设定" * 30_000}]})
        assert rows[0][0]["content"] == "设定" * 30_000

    def test_limits_are_wide_enough_for_real_large_books(self) -> None:
        """几 MB 的世界书（几千条、每条上百个关键词、单条几万字）不能碰到任何一个安全上限"""
        assert MAX_FILE_BYTES >= 16 * 1024 * 1024
        assert MAX_ENTRIES >= 20_000
        assert MAX_KEYS >= 500
        assert MAX_KEY_LENGTH >= 1000
        assert MAX_CONTENT_CHARS >= 200_000
        assert MAX_COMMENT_CHARS >= 4000

    def test_depth_at_the_limit_is_accepted(self) -> None:
        """恰好 32 层可以过：上限是「挡住拖死进程的文件」，不是功能限制"""
        payload = nested_payload(MAX_JSON_DEPTH - BASE_DEPTH)
        assert json_depth(payload) == MAX_JSON_DEPTH
        assert parse_world_file(dump(payload)) is not None

    def test_depth_above_the_limit_is_rejected(self) -> None:
        payload = nested_payload(MAX_JSON_DEPTH + 1 - BASE_DEPTH)
        assert json_depth(payload) == MAX_JSON_DEPTH + 1
        with pytest.raises(WorldFileRejected, match="嵌套"):
            parse_world_file(dump(payload))

    def test_nesting_that_breaks_the_parser_is_rejected(self) -> None:
        """几十万层 `[` 在深度检查之前就会撑爆解析器，也要报「嵌套」而不是 500"""
        data = b'{"entries": {}, "x": ' + b"[" * 500_000 + b"]" * 500_000 + b"}"
        assert len(data) <= MAX_FILE_BYTES
        with pytest.raises(WorldFileRejected, match="嵌套"):
            parse_world_file(data)

    def test_bom_is_accepted(self) -> None:
        """酒馆在 Windows 上导出的文件偶尔带 BOM，为它报「不是 UTF-8」没意义"""
        payload = dump(world_of([full_entry(1)]))
        parsed = parse_world_file(b"\xef\xbb\xbf" + payload)
        assert len(parsed.entries) == 1

    def test_book_ext_keeps_everything_but_entries(self) -> None:
        parsed = parse_world_file(dump(world_of([full_entry(1)], originalData={"n": "卡"})))
        assert parsed.book_ext == {"originalData": {"n": "卡"}}
        assert len(parsed.entries) == 1


class TestParsedEntries:
    def test_entry_keys_are_kept_verbatim(self) -> None:
        """`entries` 的键原样保留：追加导入要拿它判断撞号"""
        payload = {"entries": {"7": {"uid": 7, "content": "a"}, "12": {"uid": 12, "content": "b"}}}
        parsed = parse_world_file(dump(payload))
        assert [key for key, _ in parsed.entries] == ["7", "12"]

    def test_entry_body_is_the_raw_object(self) -> None:
        raw = full_entry(1)
        parsed = parse_world_file(dump(world_of([raw])))
        assert parsed.entries[0][1] == raw
