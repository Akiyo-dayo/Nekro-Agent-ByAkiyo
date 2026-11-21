"""酒馆对话补全预设的解析：顺序、开关、上限与报告"""

from __future__ import annotations

import copy

import pytest

from nekro_agent.services.tavern.rp_preset import (
    MAX_SEGMENTS,
    RpPresetRejected,
    parse_preset,
)
from tests.rp_preset_fixtures import custom, standard_preset, with_prompts


class TestOrderAndSwitches:
    def test_uses_the_longest_order_and_keeps_switches(self) -> None:
        segments, _rules, _notes = parse_preset(standard_preset())

        identifiers = [segment.identifier for segment in segments]
        assert identifiers[:12] == [
            "main",
            "worldInfoBefore",
            "personaDescription",
            "charDescription",
            "charPersonality",
            "scenario",
            "enhanceDefinitions",
            "nsfw",
            "worldInfoAfter",
            "dialogueExamples",
            "chatHistory",
            "jailbreak",
        ]
        assert identifiers[-1] == "orphanNote"
        by_id = {segment.identifier: segment for segment in segments}
        assert by_id["main"].enabled is True
        assert by_id["enhanceDefinitions"].enabled is False
        assert by_id["nsfw"].enabled is False
        # prompts 里有、顺序表里没有：排最后且关闭
        assert by_id["orphanNote"].enabled is False

    def test_placeholder_content_is_not_saved(self) -> None:
        raw = standard_preset()
        for prompt in raw["prompts"]:
            if prompt["identifier"] == "chatHistory":
                prompt["content"] = "SHOULD_NOT_BE_SAVED"
        segments, _rules, _notes = parse_preset(raw)

        chat_history = next(segment for segment in segments if segment.identifier == "chatHistory")
        assert chat_history.marker is True
        assert chat_history.content == ""

    def test_role_and_depth_are_normalized(self) -> None:
        prompts = [
            custom("main", "MAIN"),
            {
                "identifier": "chatHistory",
                "name": "Chat History",
                "role": 0,
                "content": "",
                "marker": True,
                "injection_position": 0,
                "injection_depth": 0,
                "injection_order": 100,
            },
            {
                "identifier": "depth",
                "name": "Depth",
                "role": 2,
                "content": "DEPTH",
                "marker": False,
                "injection_position": 1,
                "injection_depth": 3,
                "injection_order": 20,
            },
        ]
        order = [
            {"identifier": "main", "enabled": True},
            {"identifier": "chatHistory", "enabled": True},
            {"identifier": "depth", "enabled": True},
        ]
        segments, _rules, _notes = parse_preset(with_prompts(prompts, order))

        by_id = {segment.identifier: segment for segment in segments}
        assert by_id["depth"].role == "assistant"
        assert by_id["depth"].position == "in_chat"
        assert by_id["depth"].depth == 3
        assert by_id["main"].position == "relative"


class TestRejections:
    def test_missing_prompts(self) -> None:
        raw = standard_preset()
        raw.pop("prompts")
        with pytest.raises(RpPresetRejected, match="prompts"):
            parse_preset(raw)

    def test_missing_prompt_order(self) -> None:
        raw = standard_preset()
        raw["prompt_order"] = []
        with pytest.raises(RpPresetRejected, match="prompt_order"):
            parse_preset(raw)

    def test_missing_chat_history(self) -> None:
        raw = standard_preset()
        raw["prompts"] = [prompt for prompt in raw["prompts"] if prompt["identifier"] != "chatHistory"]
        raw["prompt_order"][1]["order"] = [
            item for item in raw["prompt_order"][1]["order"] if item["identifier"] != "chatHistory"
        ]
        with pytest.raises(RpPresetRejected, match="chatHistory"):
            parse_preset(raw)

    def test_too_many_segments_names_the_limit(self) -> None:
        raw = standard_preset()
        prompts = [custom(f"extra{index}", "X") for index in range(MAX_SEGMENTS + 1)]
        prompts.append(
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
        )
        raw["prompts"] = prompts
        raw["prompt_order"] = [
            {
                "character_id": 1,
                "order": [{"identifier": prompt["identifier"], "enabled": True} for prompt in prompts],
            },
        ]
        with pytest.raises(RpPresetRejected, match=str(MAX_SEGMENTS)):
            parse_preset(raw)

    def test_one_segment_too_long(self) -> None:
        raw = standard_preset()
        raw["prompts"][0]["content"] = "X" * 100_001
        with pytest.raises(RpPresetRejected, match="100000"):
            parse_preset(raw)

    def test_enabled_total_too_long(self) -> None:
        raw = standard_preset()
        raw["prompts"][0]["content"] = "X" * 100_000
        raw["prompts"][-1]["content"] = "Y" * 100_000
        # 再补两段启用的长正文，越过 300000
        raw["prompts"].extend(custom(f"extra{index}", "Z" * 100_000) for index in range(2))
        raw["prompt_order"][1]["order"].extend(
            {"identifier": f"extra{index}", "enabled": True} for index in range(2)
        )
        with pytest.raises(RpPresetRejected, match="300000"):
            parse_preset(raw)

    def test_file_too_large(self) -> None:
        raw = standard_preset()
        raw["prompts"][0]["content"] = "X" * (2 * 1024 * 1024)
        with pytest.raises(RpPresetRejected, match="2 MiB"):
            parse_preset(raw)


class TestReport:
    def _notes(self) -> dict[str, str]:
        _segments, _rules, notes = parse_preset(standard_preset())
        return {str(note["kind"]): str(note["text"]) for note in notes}

    def test_counts_and_split(self) -> None:
        notes = self._notes()
        assert notes["summary"] == "共 13 段，启用 10 段"
        # 聊天记录之前：main、两处世界书、人设类四段、示例段
        assert notes["pre_split"].startswith("聊天记录之前 8 段、")
        assert notes["post_split"].startswith("聊天记录之后 1 段、")
        assert notes["in_chat"] == "按深度插入 0 段"

    def test_unknown_macros_are_listed_and_known_ones_are_not(self) -> None:
        notes = self._notes()
        assert "getvar" in notes["unsupported_macros"]
        assert "char" not in notes["unsupported_macros"]
        assert "user" not in notes["unsupported_macros"]
        assert "//" not in notes["unsupported_macros"]

    def test_sampling_and_regex_are_reported(self) -> None:
        notes = self._notes()
        assert "temperature=0.7" in notes["sampling"]
        assert "不导入，模型组说了算" in notes["sampling"]
        assert "1 条正则" in notes["regex"]
        assert "这一版不生效" in notes["regex"]

    def test_regex_is_converted_to_cleanup_rule(self) -> None:
        _segments, rules, _notes = parse_preset(standard_preset())
        assert len(rules) == 1
        rule = rules[0]
        assert rule["name"] == "清理占位"
        assert rule["find"] == "/PLACEHOLDER/g"
        assert rule["enabled"] is True

    def test_pre_roles_and_tail_assistant_notes(self) -> None:
        raw = standard_preset()
        raw["prompts"][1]["role"] = "user"
        raw["prompts"].append(
            {
                "identifier": "tailAssistant",
                "name": "Tail",
                "role": "assistant",
                "content": "TAIL",
                "marker": False,
                "injection_position": 0,
                "injection_depth": 0,
                "injection_order": 100,
            },
        )
        raw["prompt_order"][1]["order"].append({"identifier": "tailAssistant", "enabled": True})
        _segments, _rules, notes = parse_preset(copy.deepcopy(raw))
        by_kind = {str(note["kind"]): str(note["text"]) for note in notes}
        assert "这一版按系统文本合并" in by_kind["pre_roles"]
        assert "最后一段 assistant 视为预填" in by_kind["tail_assistant"]
