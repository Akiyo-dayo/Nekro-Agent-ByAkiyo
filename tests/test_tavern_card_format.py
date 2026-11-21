"""卡片识别、V1 转换、字段规范化与不支持项报告

手工构造 V1 / V2 / V3 三张卡，覆盖全部字段、扩展里的未知键与 null；另钉住超限、
过深、坏编码与知识书条数这些拒绝边界，以及 `detect_unsupported` 的四类报告。
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from nekro_agent.services.tavern import card_format
from nekro_agent.services.tavern.card_format import (
    CardFileRejected,
    detect_unsupported,
    main_icon_data_uri,
    parse_card_file,
    pick_creator_notes,
)
from nekro_agent.services.tavern.limits import MAX_DATA_URI_IMAGE_BYTES


def _parse(card: Any) -> Any:
    return parse_card_file(json.dumps(card).encode("utf-8"))[0]


def _kinds(parsed: Any) -> list[str]:
    return [note.kind for note in parsed.notes]


class TestIdentify:
    def test_v1_is_converted_to_v2_shape(self) -> None:
        card = {
            "name": "V1 角色",
            "description": "描述",
            "personality": "性格",
            "scenario": "场景",
            "first_mes": "开场",
            "mes_example": "示例",
            "creatorcomment": "备注",
            "tags": "甲, 乙",
            "creator": "作者",
            "depth_prompt_prompt": "深度",
            "depth_prompt_depth": "6",
            "depth_prompt_role": "assistant",
            "extra_v1": "只在原文里",
        }

        parsed = _parse(card)

        assert parsed.spec == "v1"
        assert parsed.raw == card
        assert parsed.data["name"] == "V1 角色"
        assert parsed.data["description"] == "描述"
        assert parsed.data["personality"] == "性格"
        assert parsed.data["scenario"] == "场景"
        assert parsed.data["first_mes"] == "开场"
        assert parsed.data["mes_example"] == "示例"
        assert parsed.data["creator_notes"] == "备注"
        assert parsed.data["tags"] == ["甲", "乙"]
        assert parsed.data["creator"] == "作者"
        assert parsed.data["extensions"]["depth_prompt"] == {"prompt": "深度", "depth": 6, "role": "assistant"}
        assert parsed.data["system_prompt"] == ""
        assert "extra_v1" not in parsed.data

    def test_v1_missing_optional_fields_do_not_report_bad_values(self) -> None:
        parsed = _parse({"name": "只有名字", "description": "描述"})

        assert parsed.data["personality"] == ""
        assert parsed.data["tags"] == []
        assert not any(note.kind == "bad_value" for note in parsed.notes)

    def test_v2_keeps_unknown_keys_and_normalizes_fields(self) -> None:
        card = {
            "spec": "chara_card_v2",
            "spec_version": "2.0",
            "data": {
                "name": "V2",
                "nickname": "昵称",
                "description": "描述",
                "personality": None,
                "scenario": "场景",
                "first_mes": "开场",
                "mes_example": "示例",
                "creator_notes": "备注",
                "system_prompt": "系统",
                "post_history_instructions": "后置",
                "creator": "作者",
                "character_version": "1.2",
                "tags": ["标签", 5, None],
                "alternate_greetings": ["备用", 3],
                "group_only_greetings": ["群聊"],
                "source": ["来源", 4],
                "creator_notes_multilingual": {"zh": "中文备注", "en": "English", "bad": 3},
                "extensions": {
                    "unknown_key": {"nested": 1},
                    "depth_prompt": {"prompt": "深度", "depth": 200, "role": "system", "extra": True},
                    "regex_scripts": [{"scriptName": "一"}, {"scriptName": "二"}],
                },
                "character_book": {"name": "书", "entries": [{"content": "正文"}]},
                "unknown_top": {"a": 1},
            },
        }

        parsed = _parse(card)
        data = parsed.data

        assert parsed.spec == "v2"
        assert data["name"] == "V2"
        assert data["nickname"] == "昵称"
        assert data["personality"] == ""
        assert data["creator_notes"] == "备注"
        assert data["system_prompt"] == "系统"
        assert data["post_history_instructions"] == "后置"
        assert data["character_version"] == "1.2"
        assert data["tags"] == ["标签"]
        assert data["alternate_greetings"] == ["备用"]
        assert data["group_only_greetings"] == ["群聊"]
        assert data["source"] == ["来源"]
        assert data["creator_notes_multilingual"] == {"zh": "中文备注", "en": "English"}
        assert data["extensions"]["unknown_key"] == {"nested": 1}
        assert data["extensions"]["depth_prompt"] == {
            "prompt": "深度",
            "depth": 100,
            "role": "system",
            "extra": True,
        }
        assert len(data["extensions"]["regex_scripts"]) == 2
        assert data["character_book"]["entries"] == [{"content": "正文"}]
        assert data["unknown_top"] == {"a": 1}
        assert "dropped" in _kinds(parsed)
        assert "bad_value" in _kinds(parsed)

    def test_v3_reads_extra_fields(self) -> None:
        card = {
            "spec": "chara_card_v3",
            "spec_version": "3.0",
            "data": {
                "name": "V3",
                "nickname": "小名",
                "description": "描述",
                "personality": "性格",
                "scenario": "场景",
                "first_mes": "开场",
                "mes_example": "示例",
                "creator_notes": "备注",
                "system_prompt": "系统",
                "post_history_instructions": "后置",
                "creator": "作者",
                "character_version": "3.0",
                "group_only_greetings": ["群聊开场"],
                "source": ["来源"],
                "creator_notes_multilingual": {"zh": "中文"},
                "assets": [{"type": "icon", "name": "main", "uri": "data:image/png;base64,QUJD"}],
                "creation_date": 11,
                "modification_date": 12,
                "character_book": {"entries": []},
            },
        }

        parsed = _parse(card)

        assert parsed.spec == "v3"
        assert parsed.data["nickname"] == "小名"
        assert parsed.data["group_only_greetings"] == ["群聊开场"]
        assert parsed.data["source"] == ["来源"]
        assert parsed.data["creator_notes_multilingual"] == {"zh": "中文"}
        assert parsed.data["assets"][0]["type"] == "icon"
        assert parsed.data["creation_date"] == 11
        assert parsed.data["modification_date"] == 12
        assert "newer_spec" not in _kinds(parsed)

    def test_newer_spec_version_is_reported(self) -> None:
        parsed = _parse({"spec": "chara_card_v3", "spec_version": "3.1", "data": {"name": "新", "description": "d"}})

        assert "newer_spec" in _kinds(parsed)


class TestRejections:
    def test_zip_header_is_refused(self) -> None:
        with pytest.raises(CardFileRejected, match="CHARX"):
            parse_card_file(b"PK\x03\x04rest")

    @pytest.mark.parametrize(
        ("data", "name"),
        [
            (b"\xff\xd8\xff\xe0" + b"\x00" * 32, "JPG"),
            (b"GIF89a" + b"\x00" * 32, "GIF"),
            (b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32, "WebP"),
        ],
    )
    def test_pictures_that_cannot_hold_card_data_are_named(self, data: bytes, name: str) -> None:
        with pytest.raises(CardFileRejected) as caught:
            parse_card_file(data)

        message = str(caught.value)
        assert message.startswith(f"这是 {name} 图片")
        assert "存不下人设卡数据" in message
        assert "原始 PNG 或 JSON" in message

    def test_json_that_is_not_a_card(self) -> None:
        with pytest.raises(CardFileRejected, match="不是酒馆人设卡"):
            parse_card_file(b'{"hello": "world"}')

    def test_flat_object_with_only_a_name_is_not_a_v1_card(self) -> None:
        # V1 的识别要求除了 `name` 还有描述或开场白：只有一个 `name` 的随便什么对象
        # （例如别的程序导出的配置）不该被当成一张空卡导进来。
        with pytest.raises(CardFileRejected, match="不是酒馆人设卡"):
            parse_card_file(b'{"name": "x", "version": 1}')

    def test_top_level_array(self) -> None:
        with pytest.raises(CardFileRejected, match="顶层"):
            parse_card_file(b"[]")

    def test_data_is_not_an_object(self) -> None:
        with pytest.raises(CardFileRejected, match="data"):
            parse_card_file(b'{"spec": "chara_card_v2", "data": []}')

    def test_oversized_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(card_format, "MAX_CARD_JSON_BYTES", 10)
        with pytest.raises(CardFileRejected, match="MiB"):
            parse_card_file(json.dumps({"spec": "chara_card_v2", "data": {}}).encode("utf-8"))

    def test_thirty_three_levels_of_nesting(self) -> None:
        deep: Any = "x"
        for _ in range(33):
            deep = [deep]
        card = {"spec": "chara_card_v2", "data": {"name": "深", "description": "d", "deep": deep}}

        with pytest.raises(CardFileRejected, match="嵌套"):
            _parse(card)

    def test_not_utf8(self) -> None:
        with pytest.raises(CardFileRejected, match="UTF-8"):
            parse_card_file(b"\xff\xfe{}")


class TestNormalizationLimits:
    def test_long_text_is_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(card_format, "MAX_TEXT_FIELD_CHARS", 5)
        parsed = _parse({"spec": "chara_card_v2", "data": {"name": "A", "description": "abcdefgh"}})

        assert parsed.data["description"] == "abcde"
        assert any(note.kind == "truncated" and note.field == "description" for note in parsed.notes)

    def test_non_string_tags_are_dropped(self) -> None:
        parsed = _parse({"spec": "chara_card_v2", "data": {"name": "A", "description": "d", "tags": ["ok", 3, None]}})

        assert parsed.data["tags"] == ["ok"]
        assert any(note.kind == "dropped" and note.field == "tags" for note in parsed.notes)

    def test_depth_is_clamped_both_ways(self) -> None:
        high = _parse(
            {"spec": "chara_card_v2", "data": {"name": "A", "description": "d", "extensions": {"depth_prompt": {"depth": 200}}}},
        )
        low = _parse(
            {"spec": "chara_card_v2", "data": {"name": "A", "description": "d", "extensions": {"depth_prompt": {"depth": -9}}}},
        )

        assert high.data["extensions"]["depth_prompt"]["depth"] == 100
        assert low.data["extensions"]["depth_prompt"]["depth"] == 0
        assert any(note.kind == "bad_value" for note in high.notes)
        assert any(note.kind == "bad_value" for note in low.notes)

    def test_depth_role_is_cut_to_the_column_width(self) -> None:
        parsed = _parse(
            {
                "spec": "chara_card_v2",
                "data": {"name": "A", "description": "d", "extensions": {"depth_prompt": {"role": "r" * 20}}},
            },
        )

        assert parsed.data["extensions"]["depth_prompt"]["role"] == "r" * 16

    def test_book_over_two_thousand_entries_is_refused(self) -> None:
        card = {
            "spec": "chara_card_v2",
            "data": {"name": "A", "description": "d", "character_book": {"entries": [{} for _ in range(2001)]}},
        }

        with pytest.raises(CardFileRejected, match="2000"):
            _parse(card)


class TestUnsupported:
    def test_parse_result_carries_unsupported_notes(self) -> None:
        parsed = _parse(
            {"spec": "chara_card_v2", "data": {"name": "A", "description": "{{getvar::x}}"}},
        )

        assert any(note.kind == "unsupported" for note in parsed.notes)

    def test_four_categories_are_reported_once_each(self) -> None:
        data = {
            "name": "{{setvar::a::1}}",
            "description": "{{IF x}}y{{/if}}",
            "alternate_greetings": ["{{getvar::a}}"],
            "character_book": {"entries": [{"content": "@@depth 4"}, {"content": "plain"}]},
            "assets": [
                {"type": "icon", "name": "main", "uri": "ccdefault:"},
                {"type": "background", "name": "bg", "uri": "data:image/png;base64,QUJD"},
            ],
            "extensions": {"world": "外部世界"},
        }

        notes = detect_unsupported(data)

        assert len(notes) == 4
        assert {note.field for note in notes} == {"content", "character_book", "assets", "extensions.world"}
        assert all(note.kind == "unsupported" for note in notes)
        macro_note = next(note for note in notes if note.field == "content")
        assert "4" in macro_note.detail
        assert "变量宏或条件宏" in macro_note.detail

    def test_world_that_names_the_embedded_book_is_not_reported(self) -> None:
        # 酒馆导出带卡内书的卡时把 `extensions.world` 写成卡内书自己的名字。
        data = {"character_book": {"name": " 卡内书 ", "entries": []}, "extensions": {"world": "卡内书"}}

        assert [note.field for note in detect_unsupported(data)] == []

    def test_world_that_names_another_book_is_still_reported(self) -> None:
        data = {"character_book": {"name": "卡内书", "entries": []}, "extensions": {"world": "别的书"}}

        assert [note.field for note in detect_unsupported(data)] == ["extensions.world"]

    def test_ejs_blocks_are_counted(self) -> None:
        data = {
            "description": "a<% if (x) { %>b<% } %>",
            "alternate_greetings": ["<%= getvar('t') %>"],
            "character_book": {"entries": [{"content": "无模板"}]},
        }

        notes = [note for note in detect_unsupported(data) if note.field == "ejs"]

        assert len(notes) == 1
        assert "3 个 EJS 模板块" in notes[0].detail
        assert "在 2 处" in notes[0].detail
        assert "不执行" in notes[0].detail
        # 卡内书条目里的模板按安全子集求值，其它字段里的原样留着：两件事都要说清楚。
        assert "安全子集" in notes[0].detail
        assert "原样留在文字里" in notes[0].detail

    def test_ejs_block_count_is_linear_on_unclosed_tags(self) -> None:
        import time

        started = time.monotonic()
        assert card_format._count_ejs_blocks("<%" * 300_000) == 0  # noqa: SLF001
        assert card_format._count_ejs_blocks("a<%b%>c<%d%><%") == 2  # noqa: SLF001
        assert time.monotonic() - started < 2

    def test_tavern_helper_scripts_are_listed_with_hosts(self) -> None:
        data = {
            "extensions": {
                "tavern_helper": {
                    "scripts": [
                        {
                            "type": "script",
                            "name": "加载\x07器",
                            "content": "import 'https://cdn.example.test/a.js'; fetch('https://API.example.test/x');"
                            " fetch('https://cdn.example.test/b'); '<svg xmlns=\"http://www.w3.org/2000/svg\">'"
                            " 'https://stub.invalid/x'",
                        },
                        {"type": "folder", "name": "文件夹", "scripts": [{"name": "里面的", "content": "console.log(1)"}]},
                    ],
                },
                "TavernHelper_scripts": [{"name": "旧版", "content": "x"}],
            },
        }

        notes = [note for note in detect_unsupported(data) if note.field == "extensions.tavern_helper"]

        assert len(notes) == 1
        detail = notes[0].detail
        assert "3 个酒馆助手脚本" in detail
        assert "NA 不执行" in detail
        assert "「加载器」" in detail
        assert "联网 cdn.example.test、api.example.test" in detail
        assert "www.w3.org" not in detail
        assert ".invalid" not in detail
        assert "「里面的」14 字，交互程序，没有写死的联网地址" in detail
        assert "「旧版」1 字" in detail
        # 第一个脚本从网址 import 代码：归「远端代码或改世界书」；三个脚本按类别计数写在开头。
        assert "「加载器」" in detail and "远端代码或改世界书，联网" in detail
        assert "（交互程序 2 个、远端代码或改世界书 1 个）" in detail

    def test_tavern_helper_list_is_capped(self) -> None:
        hosts = " ".join(f"https://h{index}.example.test/" for index in range(card_format.MAX_LISTED_HOSTS + 2))
        scripts = [{"name": f"脚本{index}", "content": hosts} for index in range(card_format.MAX_LISTED_SCRIPTS + 1)]

        detail = detect_unsupported({"extensions": {"tavern_helper": {"scripts": scripts}}})[0].detail

        assert f"{card_format.MAX_LISTED_SCRIPTS + 1} 个酒馆助手脚本" in detail
        assert "另有 1 个未列出" in detail
        assert f"等 {card_format.MAX_LISTED_HOSTS + 2} 个" in detail


class TestDerivedValues:
    def test_creator_notes_prefers_chinese(self) -> None:
        data = {"creator_notes": "英文", "creator_notes_multilingual": {"zh": "中文", "en": "English"}}

        assert pick_creator_notes(data) == "中文"
        assert pick_creator_notes({"creator_notes": "英文"}) == "英文"

    def test_main_icon_accepts_only_the_main_data_uri(self) -> None:
        small = "data:image/png;base64," + base64.b64encode(b"png").decode("ascii")
        data = {"assets": [{"type": "icon", "name": "main", "uri": small}]}

        assert main_icon_data_uri(data) == small
        assert main_icon_data_uri({"assets": [{"type": "icon", "name": "main", "uri": "https://x.test/a.png"}]}) is None
        assert main_icon_data_uri({"assets": [{"type": "icon", "name": "main", "uri": "embeded://a.png"}]}) is None
        assert main_icon_data_uri({"assets": [{"type": "icon", "name": "main", "uri": "ccdefault:"}]}) is None
        assert main_icon_data_uri({"assets": [{"type": "icon", "name": "other", "uri": small}]}) is None

    def test_main_icon_over_limit_returns_none(self) -> None:
        big = "data:image/png;base64," + base64.b64encode(b"\0" * (MAX_DATA_URI_IMAGE_BYTES + 1)).decode("ascii")

        assert main_icon_data_uri({"assets": [{"type": "icon", "name": "main", "uri": big}]}) is None
