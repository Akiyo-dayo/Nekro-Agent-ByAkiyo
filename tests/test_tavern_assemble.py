"""拼装模板：空字段整段丢、占位替换、其余宏原样

模板只认五个卡片字段占位；其余 `{{…}}` 是运行时宏，拼装阶段必须原样留下。默认
模板与配置项默认值必须是同一份，否则导入时用的与后台显示的是两套。
"""

from __future__ import annotations

import hashlib

from nekro_agent.core.config import CoreConfig
from nekro_agent.services.tavern.assemble import (
    CARD_FIELDS,
    DEFAULT_CARD_TEMPLATE,
    assemble_content,
    content_sha256,
)


def _fields(**overrides: str) -> dict[str, str]:
    base = {name: "" for name in CARD_FIELDS}
    base.update(overrides)
    return base


class TestDefaultTemplate:
    def test_has_all_five_card_fields(self) -> None:
        for name in CARD_FIELDS:
            assert "{{" + name + "}}" in DEFAULT_CARD_TEMPLATE

    def test_config_default_is_the_same_template(self) -> None:
        assert CoreConfig.model_fields["TAVERN_CARD_TEMPLATE"].default == DEFAULT_CARD_TEMPLATE

    def test_config_is_a_textarea_and_not_overridable(self) -> None:
        extra = CoreConfig.model_fields["TAVERN_CARD_TEMPLATE"].json_schema_extra
        assert isinstance(extra, dict)
        assert extra["is_textarea"] is True
        assert extra["overridable"] is False


class TestAssemble:
    def test_all_fields_present(self) -> None:
        text = assemble_content(
            DEFAULT_CARD_TEMPLATE,
            _fields(system="系统", description="描述", personality="性格", scenario="场景", mes_example="示例"),
        )

        assert text.startswith("系统\n\n描述")
        assert "{{char}}'s personality: 性格" in text
        assert "Scenario: 场景" in text
        assert "Example dialogue:\n示例" in text

    def test_all_empty_gives_empty_text(self) -> None:
        assert assemble_content(DEFAULT_CARD_TEMPLATE, _fields()) == ""

    def test_only_one_field_keeps_only_its_paragraph(self) -> None:
        text = assemble_content(DEFAULT_CARD_TEMPLATE, _fields(description="只有描述"))

        assert text == "只有描述"

    def test_placeholder_case_and_whitespace(self) -> None:
        text = assemble_content("A\n\n{{ System }}\n\nB", _fields(system="S"))

        assert text == "A\n\nS\n\nB"

    def test_segment_with_two_fields_survives_when_one_is_non_empty(self) -> None:
        text = assemble_content("{{system}}/{{description}}", _fields(description="描述"))

        assert text == "/描述"

    def test_other_macros_are_left_alone(self) -> None:
        text = assemble_content("{{char}} 与 {{user}}", _fields())

        assert text == "{{char}} 与 {{user}}"

    def test_placeholder_values_are_not_stripped(self) -> None:
        text = assemble_content("X\n\n{{description}}\n\nY", _fields(description="  保留空白  "))

        assert text == "X\n\n  保留空白  \n\nY"

    def test_consecutive_blank_lines_are_one_separator(self) -> None:
        text = assemble_content("a\n\n\n \t\n{{description}}\n\n\n\nb", _fields(description="d"))

        assert text == "a\n\nd\n\nb"


class TestContentHash:
    def test_stable_and_matches_sha256(self) -> None:
        assert content_sha256("正文") == content_sha256("正文")
        assert content_sha256("正文") == hashlib.sha256("正文".encode("utf-8")).hexdigest()

    def test_changes_with_text(self) -> None:
        assert content_sha256("a") != content_sha256("b")
