"""卡内知识书的双向映射、导入与往返

手工构造卡内条目，逐字段钉住 `convertCharacterBook` 的取值规则；再用真表验证
`create_card_book` 的作用域、信任级别与编号重排，以及「没编辑过就逐字还原」的往返。
"""

from __future__ import annotations

from typing import Any, AsyncIterator

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.services.tavern import card_book
from nekro_agent.services.worldbook import st_format


@pytest.fixture
async def db() -> AsyncIterator[None]:
    """每个用例一套 SQLite 内存库；按模型建表，不依赖迁移文件"""
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _preset(name: str = "人设") -> DBPreset:
    return await DBPreset.create(
        name=name,
        title=name,
        avatar="",
        content="正文",
        description="",
        tags="",
        author="作者",
        owner_instance_key="",
    )


def _full_entry() -> dict[str, Any]:
    """一条把所有映射字段都填上的卡内条目"""
    return {
        "id": 7,
        "keys": ["甲", "乙"],
        "secondary_keys": ["丙"],
        "comment": "标题",
        "content": "条目正文",
        "constant": True,
        "selective": True,
        "insertion_order": 42,
        "enabled": False,
        "position": "before_char",
        "use_regex": True,
        "priority": 9,
        "name": "NA 不用的名字",
        "extensions": {
            "position": 4,
            "exclude_recursion": True,
            "prevent_recursion": True,
            "delay_until_recursion": True,
            "display_index": 3,
            "probability": 55,
            "useProbability": False,
            "depth": 9,
            "selectiveLogic": 2,
            "outlet_name": "出口",
            "group": "组",
            "group_override": True,
            "group_weight": 12,
            "scan_depth": 5,
            "case_sensitive": True,
            "match_whole_words": False,
            "use_group_scoring": True,
            "automation_id": "自动",
            "role": 1,
            "vectorized": True,
            "sticky": 2,
            "cooldown": 3,
            "delay": 4,
            "match_persona_description": True,
            "match_character_description": True,
            "match_character_personality": True,
            "match_character_depth_prompt": True,
            "match_scenario": True,
            "match_creator_notes": True,
            "triggers": ["normal"],
            "ignore_budget": True,
        },
    }


class TestCardEntryToWorld:
    def test_every_field_follows_convert_character_book(self) -> None:
        world = card_book.card_entry_to_world_entry(_full_entry(), 0)
        assert world["uid"] == 7
        assert world["key"] == ["甲", "乙"]
        assert world["keysecondary"] == ["丙"]
        assert world["comment"] == "标题"
        assert world["content"] == "条目正文"
        assert world["constant"] is True
        assert world["selective"] is True
        assert world["order"] == 42
        assert world["position"] == 4
        assert world["disable"] is True
        assert world["addMemo"] is True
        assert world["displayIndex"] == 3
        assert world["probability"] == 55
        assert world["useProbability"] is False
        assert world["depth"] == 9
        assert world["selectiveLogic"] == 2
        assert world["outletName"] == "出口"
        assert world["group"] == "组"
        assert world["groupOverride"] is True
        assert world["groupWeight"] == 12
        assert world["scanDepth"] == 5
        assert world["caseSensitive"] is True
        assert world["matchWholeWords"] is False
        assert world["useGroupScoring"] is True
        assert world["automationId"] == "自动"
        assert world["role"] == 1
        assert world["vectorized"] is True
        assert world["sticky"] == 2
        assert world["cooldown"] == 3
        assert world["delay"] == 4
        assert world["excludeRecursion"] is True
        assert world["preventRecursion"] is True
        assert world["delayUntilRecursion"] is True
        assert world["matchPersonaDescription"] is True
        assert world["matchCharacterDescription"] is True
        assert world["matchCharacterPersonality"] is True
        assert world["matchCharacterDepthPrompt"] is True
        assert world["matchScenario"] is True
        assert world["matchCreatorNotes"] is True
        assert world["triggers"] == ["normal"]
        assert world["ignoreBudget"] is True
        assert world["extensions"]["position"] == 4
        # 没有 id 时用序号。
        assert card_book.card_entry_to_world_entry({"content": "无编号"}, 3)["uid"] == 3

    def test_position_falls_back_to_before_or_after_char(self) -> None:
        before = card_book.card_entry_to_world_entry({"position": "before_char"}, 0)
        after = card_book.card_entry_to_world_entry({"position": "after_char"}, 0)

        assert before["position"] == 0
        assert after["position"] == 1

    def test_top_level_case_sensitive_is_used_when_extension_missing(self) -> None:
        entry = {"case_sensitive": True, "extensions": {}}

        assert card_book.card_entry_to_world_entry(entry, 0)["caseSensitive"] is True

    def test_extension_case_sensitive_wins(self) -> None:
        entry = {"case_sensitive": True, "extensions": {"case_sensitive": False}}

        assert card_book.card_entry_to_world_entry(entry, 0)["caseSensitive"] is False

    def test_use_regex_is_stored_as_false_but_exported_back(self) -> None:
        entry = _full_entry()
        columns, ext = card_book.build_book_rows({"entries": [entry]})[1][0]

        assert columns["use_regex"] is False
        restored = card_book.card_entry_from_row(columns, ext)
        assert restored["use_regex"] is True

    def test_invalid_slash_keyword_is_reported(self) -> None:
        entry = {"id": 1, "keys": ["/(/"], "content": "正文"}

        _book_ext, rows, notes = card_book.build_book_rows({"entries": [entry]})
        columns, ext = rows[0]

        assert columns["use_regex"] is False
        assert ext["na_invalid_keys"]
        assert [note.kind for note in notes].count("invalid_key") == 1

    def test_budget_hint_is_off_by_default_and_keyed_by_uid_when_given(self) -> None:
        book = {"entries": [{"id": 3, "content": "长" * 10}, {"id": 4, "content": "短"}]}

        _ext, _rows, silent = card_book.build_book_rows(book)
        _ext, _rows, notes = card_book.build_book_rows(book, budget_chars=5)

        assert [note.kind for note in silent].count("over_budget") == 0
        assert [(note.entry_key, note.kind) for note in notes if note.kind == "over_budget"] == [("3", "over_budget")]

    def test_non_object_entry_is_skipped(self) -> None:
        _book_ext, rows, notes = card_book.build_book_rows({"entries": ["坏条目", {"content": "好"}]})

        assert len(rows) == 1
        assert "skipped" in [note.kind for note in notes]

    def test_scan_depth_is_clamped(self) -> None:
        assert card_book.book_scan_depth({"scan_depth": 0}) == 1
        assert card_book.book_scan_depth({"scan_depth": 99}) == 50
        assert card_book.book_scan_depth({"scan_depth": 12}) == 12
        assert card_book.book_scan_depth({"scan_depth": "12"}) == 4
        assert card_book.book_scan_depth({}) == 4


class TestWorldEntryToCard:
    def test_reverse_mapping_restores_card_shape(self) -> None:
        world = card_book.card_entry_to_world_entry(_full_entry(), 0)
        world["extensions"]["unknown_extension"] = {"keep": 1}

        entry = card_book.world_entry_to_card_entry(world, use_regex=True)

        assert entry["id"] == 7
        assert entry["keys"] == ["甲", "乙"]
        assert entry["secondary_keys"] == ["丙"]
        assert entry["comment"] == "标题"
        assert entry["content"] == "条目正文"
        assert entry["constant"] is True
        assert entry["selective"] is True
        assert entry["insertion_order"] == 42
        assert entry["enabled"] is False
        assert entry["position"] == "after_char"
        assert entry["use_regex"] is True
        assert entry["extensions"]["position"] == 4
        assert entry["extensions"]["display_index"] == 3
        assert entry["extensions"]["case_sensitive"] is True
        assert entry["extensions"]["unknown_extension"] == {"keep": 1}

    def test_position_other_than_zero_becomes_after_char(self) -> None:
        world = card_book.card_entry_to_world_entry({"id": 1, "position": "before_char"}, 0)
        world["position"] = 4

        assert card_book.world_entry_to_card_entry(world, use_regex=False)["position"] == "after_char"


class TestCreateCardBook:
    @pytest.mark.asyncio
    async def test_scope_is_preset_and_entries_are_untrusted(self, db: None) -> None:
        preset = await _preset()

        book, report = await card_book.create_card_book(
            ("", ""),
            preset.id,
            "卡内书",
            {"name": "卡内书", "scan_depth": 8, "entries": [{"id": 1, "keys": ["甲"], "content": "正文"}]},
            username="platform",
        )

        assert book.scope_kind == "preset"
        assert book.preset_id == preset.id
        assert book.name == "卡内书"
        assert book.scan_depth == 8
        assert report.created == 1
        entries = await DBWbEntry.filter(book_id=book.id)
        assert entries
        assert all(entry.trusted is False for entry in entries)
        assert book.ext["na_card_book"] == {"name": "卡内书", "scan_depth": 8}

    @pytest.mark.asyncio
    async def test_duplicate_uids_are_renumbered(self, db: None) -> None:
        preset = await _preset()
        entries = [
            {"id": 5, "content": "第一条"},
            {"id": 5, "content": "第二条"},
        ]

        book, report = await card_book.create_card_book(
            ("", ""),
            preset.id,
            "卡内书",
            {"entries": entries},
            username="tester",
        )

        rows = await DBWbEntry.filter(book_id=book.id).order_by("id")
        assert sorted(row.uid for row in rows) == [0, 5]
        assert report.notes.get("renumbered") == 1


class TestRoundTrip:
    def test_untouched_entry_is_restored_verbatim(self) -> None:
        entry = _full_entry()
        _book_ext, rows, _notes = card_book.build_book_rows({"entries": [entry]})
        columns, ext = rows[0]

        assert card_book.card_entry_from_row(columns, ext) == entry

    def test_only_content_changes_when_edited(self) -> None:
        entry = _full_entry()
        _book_ext, rows, _notes = card_book.build_book_rows({"entries": [entry]})
        columns, ext = rows[0]
        edited = dict(columns, content="改过的正文")

        restored = card_book.card_entry_from_row(edited, ext)

        assert restored["content"] == "改过的正文"
        assert {key for key in restored if restored[key] != entry.get(key)} == {"content"}

    def test_entry_without_extensions_stays_without_them(self) -> None:
        entry = {"id": 1, "keys": ["甲"], "content": "正文"}
        _book_ext, rows, _notes = card_book.build_book_rows({"entries": [entry]})
        columns, ext = rows[0]

        assert card_book.card_entry_from_row(columns, ext) == entry

    def test_entry_without_id_is_not_given_one(self) -> None:
        entry = {"keys": ["甲"], "content": "正文", "extensions": {"display_index": 3, "depth": 9}}
        _book_ext, rows, _notes = card_book.build_book_rows({"entries": [entry]})
        columns, ext = rows[0]

        assert card_book.card_entry_from_row(columns, ext) == entry

    def test_edited_depth_adds_only_that_extension(self) -> None:
        entry = {"id": 1, "keys": ["甲"], "content": "正文"}
        _book_ext, rows, _notes = card_book.build_book_rows({"entries": [entry]})
        columns, ext = rows[0]
        edited = dict(columns, depth=9)

        restored = card_book.card_entry_from_row(edited, ext)

        assert restored["extensions"] == {"depth": 9}

    def test_edited_position_syncs_extension(self) -> None:
        _book_ext, rows, _notes = card_book.build_book_rows({"entries": [_full_entry()]})
        columns, ext = rows[0]
        edited = dict(columns, position="before_history")

        restored = card_book.card_entry_from_row(edited, ext)

        # 卡内顶层 position 只有 before_char / after_char，具体位置由 extensions 表达。
        assert restored["extensions"]["position"] == 6

    def test_edited_depth_syncs_extension(self) -> None:
        _book_ext, rows, _notes = card_book.build_book_rows({"entries": [_full_entry()]})
        columns, ext = rows[0]
        edited = dict(columns, depth=9)

        restored = card_book.card_entry_from_row(edited, ext)

        assert restored["extensions"]["depth"] == 9

    def test_new_row_without_original_generates_card_entry(self) -> None:
        columns, _notes = st_format.st_entry_to_columns(
            {
                **st_format.ST_ENTRY_DEFAULTS,
                "uid": 2,
                "key": ["新"],
                "keysecondary": ["次"],
                "content": "新条目",
                "order": 30,
                "position": 0,
            },
        )

        entry = card_book.card_entry_from_row(columns, {})

        assert entry["id"] == 2
        assert entry["keys"] == ["新"]
        assert entry["secondary_keys"] == ["次"]
        assert entry["content"] == "新条目"
        assert entry["insertion_order"] == 30
        assert entry["position"] == "before_char"
        assert entry["use_regex"] is False
        assert entry["extensions"]["position"] == 0

    @pytest.mark.asyncio
    async def test_build_character_book_merges_books_in_order(self, db: None) -> None:
        preset = await _preset()
        first = await DBWbBook.create(
            name="第一本",
            scope_kind="preset",
            preset_id=preset.id,
            ext={"na_card_book": {"name": "原书", "scan_depth": 6, "extra": True}},
        )
        second = await DBWbBook.create(
            name="第二本",
            scope_kind="preset",
            preset_id=preset.id,
            ext={},
        )
        await DBWbEntry.create(book=first, uid=2, content="第一本第二条", keys=["乙"])
        await DBWbEntry.create(book=first, uid=1, content="第一本第一条", keys=["甲"])
        await DBWbEntry.create(book=second, uid=0, content="第二本", keys=["丙"])

        book = card_book.build_character_book(
            [
                (first, await DBWbEntry.filter(book_id=first.id)),
                (second, await DBWbEntry.filter(book_id=second.id)),
            ],
        )

        assert book is not None
        assert book["name"] == "原书"
        assert book["scan_depth"] == 6
        assert book["extra"] is True
        assert [entry["content"] for entry in book["entries"]] == ["第一本第一条", "第一本第二条", "第二本"]
        assert card_book.build_character_book([]) is None
