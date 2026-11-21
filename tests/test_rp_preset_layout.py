"""酒馆预设的排布：占位段、世界书落点、覆盖、后置段与深度段"""

from __future__ import annotations

from typing import Any, Mapping

from nekro_agent.services.tavern.rp_layout import build_layout
from nekro_agent.services.tavern.rp_preset import RpSegment, parse_preset
from tests.rp_preset_fixtures import custom, standard_preset, with_prompts, wrapped_preset


def _expand(text: str) -> str:
    return text.replace("{{char}}", "角色名").replace("{{user}}", "用户")


def _fields(**overrides: str) -> dict[str, str]:
    fields = {
        "system": "",
        "description": "DESC {{char}}",
        "personality": "PERSONALITY",
        "scenario": "SCENARIO",
        "mes_example": "EXAMPLES",
    }
    fields.update(overrides)
    return fields


def _segments(raw: Mapping[str, Any]) -> list[RpSegment]:
    segments, _rules, _notes = parse_preset(dict(raw))
    return segments


def _layout(raw: Mapping[str, Any], **kwargs: Any):
    params: dict[str, Any] = {
        "fields": _fields(),
        "worldbook_block": "",
        "post_history_slot": "",
        "expand": _expand,
    }
    params.update(kwargs)
    return build_layout(_segments(raw), **params)


class TestPlaceholders:
    def test_placeholders_are_filled_and_wrapper_tags_stay_adjacent(self) -> None:
        layout = _layout(wrapped_preset(), worldbook_block="WB_BLOCK")

        lines = layout.system_block.split("\n")
        description_index = lines.index("DESC 角色名")
        assert lines[description_index - 1] == "<characters>"
        assert lines[description_index + 1] == "</characters>"
        assert "PERSONALITY" in lines
        assert "SCENARIO" in lines
        assert "EXAMPLES" in lines
        assert "WRAPPED_MAIN" in lines

    def test_worldbook_after_wins_when_both_placeholders_are_enabled(self) -> None:
        layout = _layout(standard_preset(), worldbook_block="WB_BLOCK")

        assert layout.system_block.count("WB_BLOCK") == 1
        lines = layout.system_block.split("\n")
        # worldInfoAfter 在示例对话之前：块落在这里，而不是前面的 worldInfoBefore
        assert lines[lines.index("WB_BLOCK") + 1] == "EXAMPLES"

    def test_worldbook_before_when_only_before_is_enabled(self) -> None:
        raw = standard_preset()
        raw["prompt_order"][1]["order"] = [
            item for item in raw["prompt_order"][1]["order"] if item["identifier"] != "worldInfoAfter"
        ] + [{"identifier": "worldInfoAfter", "enabled": False}]
        layout = _layout(raw, worldbook_block="WB_BLOCK")

        lines = layout.system_block.split("\n")
        assert lines[lines.index("WB_BLOCK") + 1] == "DESC 角色名"

    def test_worldbook_attaches_after_last_persona_placeholder_when_none_remains(self) -> None:
        raw = with_prompts(
            [
                custom("main", "MAIN"),
                {
                    "identifier": "scenario",
                    "name": "Scenario",
                    "role": "system",
                    "content": "",
                    "marker": True,
                    "injection_position": 0,
                    "injection_depth": 0,
                    "injection_order": 100,
                },
                {
                    "identifier": "chatHistory",
                    "name": "Chat History",
                    "role": "system",
                    "content": "",
                    "marker": True,
                    "injection_position": 0,
                    "injection_depth": 0,
                    "injection_order": 100,
                },
            ],
            [
                {"identifier": "main", "enabled": True},
                {"identifier": "scenario", "enabled": True},
                {"identifier": "chatHistory", "enabled": True},
            ],
        )
        layout = _layout(raw, worldbook_block="WB_BLOCK")

        lines = layout.system_block.split("\n")
        assert lines[-1] == "WB_BLOCK"
        assert lines[-2] == "SCENARIO"

    def test_worldbook_goes_to_system_tail_without_persona_placeholders(self) -> None:
        raw = with_prompts(
            [
                custom("main", "MAIN"),
                {
                    "identifier": "chatHistory",
                    "name": "Chat History",
                    "role": "system",
                    "content": "",
                    "marker": True,
                    "injection_position": 0,
                    "injection_depth": 0,
                    "injection_order": 100,
                },
            ],
            [
                {"identifier": "main", "enabled": True},
                {"identifier": "chatHistory", "enabled": True},
            ],
        )
        layout = _layout(raw, worldbook_block="WB_BLOCK")

        assert layout.system_block == "MAIN\nWB_BLOCK"


class TestMainOverride:
    def test_card_system_replaces_main_and_original_is_substituted(self) -> None:
        layout = _layout(
            standard_preset(),
            fields=_fields(system="CARD {{original}} END"),
        )

        assert layout.system_block.split("\n")[0].startswith("CARD MAIN_BODY")
        assert layout.system_block.split("\n")[0].endswith("END")

    def test_forbid_overrides_keeps_the_preset_body(self) -> None:
        raw = standard_preset()
        raw["prompts"][0]["forbid_overrides"] = True
        layout = _layout(raw, fields=_fields(system="CARD {{original}} END"))

        assert layout.system_block.split("\n")[0].startswith("MAIN_BODY")
        assert "CARD" not in layout.system_block


class TestTailAndDepth:
    def test_last_assistant_becomes_prefill_and_not_tail(self) -> None:
        layout = _layout(wrapped_preset())

        assert layout.prefill_text == "PREFILL_MARKER"
        assert "PREFILL_MARKER" not in layout.tail_text
        assert "WRAPPED_JAIL" in layout.tail_text

    def test_jailbreak_is_replaced_by_the_slot(self) -> None:
        raw = wrapped_preset()
        layout = _layout(raw, post_history_slot="SLOT {{original}} END")

        assert "SLOT WRAPPED_JAIL END" in layout.tail_text
        assert "WRAPPED_JAIL" not in layout.tail_text.replace("SLOT WRAPPED_JAIL END", "")

    def test_forbid_overrides_appends_the_slot_instead(self) -> None:
        raw = wrapped_preset()
        for prompt in raw["prompts"]:
            if prompt["identifier"] == "jailbreak":
                prompt["forbid_overrides"] = True
        layout = _layout(raw, post_history_slot="SLOT_TEXT")

        assert layout.tail_text.endswith("SLOT_TEXT")
        assert "WRAPPED_JAIL" in layout.tail_text

    def test_slot_is_appended_when_no_jailbreak_is_enabled(self) -> None:
        raw = wrapped_preset()
        raw["prompt_order"][0]["order"] = [
            item for item in raw["prompt_order"][0]["order"] if item["identifier"] != "jailbreak"
        ]
        layout = _layout(raw, post_history_slot="SLOT_TEXT")

        assert layout.tail_text.endswith("SLOT_TEXT")

    def test_depth_blocks_are_sorted_by_injection_order(self) -> None:
        prompts = [
            custom("main", "MAIN"),
            {
                "identifier": "chatHistory",
                "name": "Chat History",
                "role": "system",
                "content": "",
                "marker": True,
                "injection_position": 0,
                "injection_depth": 0,
                "injection_order": 100,
            },
            {
                "identifier": "late",
                "name": "Late",
                "role": "system",
                "content": "LATE",
                "marker": False,
                "injection_position": 1,
                "injection_depth": 1,
                "injection_order": 90,
            },
            {
                "identifier": "early",
                "name": "Early",
                "role": "system",
                "content": "EARLY",
                "marker": False,
                "injection_position": 1,
                "injection_depth": 2,
                "injection_order": 10,
            },
        ]
        order = [{"identifier": prompt["identifier"], "enabled": True} for prompt in prompts]
        layout = _layout(with_prompts(prompts, order))

        assert layout.depth_blocks == ((2, "EARLY"), (1, "LATE"))
        assert [item["where"] for item in layout.used if str(item["where"]).startswith("depth:")] == [
            "depth:2",
            "depth:1",
        ]


class TestBlankAndDisabled:
    def test_disabled_segments_do_not_appear(self) -> None:
        layout = _layout(standard_preset())

        assert "ENHANCE_BODY" not in layout.system_block
        assert "STYLE_MARKER" not in layout.system_block
        assert "ORPHAN_BODY" not in layout.system_block

    def test_empty_segments_leave_no_blank_lines(self) -> None:
        raw = with_prompts(
            [
                custom("main", "   \n  "),
                custom("second", "\nSECOND\n"),
                custom("third", "THIRD"),
                {
                    "identifier": "chatHistory",
                    "name": "Chat History",
                    "role": "system",
                    "content": "",
                    "marker": True,
                    "injection_position": 0,
                    "injection_depth": 0,
                    "injection_order": 100,
                },
            ],
            [{"identifier": "main", "enabled": True}, {"identifier": "second", "enabled": True},
             {"identifier": "third", "enabled": True}, {"identifier": "chatHistory", "enabled": True}],
        )
        layout = _layout(raw)

        assert layout.system_block == "SECOND\nTHIRD"
        assert "\n\n" not in layout.system_block

    def test_used_lists_the_segments_that_produced_text(self) -> None:
        layout = _layout(standard_preset(), worldbook_block="WB_BLOCK")
        identifiers = [str(item["identifier"]) for item in layout.used]

        assert identifiers[0] == "main"
        assert "personaDescription" not in identifiers
        assert identifiers.count("worldInfoAfter") == 1
        by_id = {str(item["identifier"]): item for item in layout.used}
        assert by_id["worldInfoAfter"]["chars"] == len("WB_BLOCK")
