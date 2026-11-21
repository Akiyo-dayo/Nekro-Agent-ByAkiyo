"""可覆盖配置项的标题与说明不能写成只针对频道的口吻

可覆盖的配置项会出现在全局、实例、频道三个视图里，标题都是同一份文案。写成「本频道启用…」
时，管理员在实例视图里看到的是一句「本频道」，会以为自己在改某个频道的覆盖。这里把措辞
钉住：标题里不出现「本频道」/ 「This Channel」，中文说明里也不出现「本频道」。
"""

from __future__ import annotations

from nekro_agent.core.config import CoreConfig


def _overridable_fields() -> dict[str, dict]:
    result: dict[str, dict] = {}
    for name, field in CoreConfig.model_fields.items():
        extra = field.json_schema_extra
        if isinstance(extra, dict) and extra.get("overridable"):
            result[name] = {"field": field, "extra": extra}
    return result


def test_overridable_titles_are_not_channel_specific() -> None:
    offenders = []
    for name, item in _overridable_fields().items():
        field, extra = item["field"], item["extra"]
        titles = [field.title or ""]
        i18n_title = extra.get("i18n_title")
        if isinstance(i18n_title, dict):
            titles.extend(str(value) for value in i18n_title.values())
        if any("本频道" in title or "this channel" in title.lower() for title in titles):
            offenders.append(name)
    assert offenders == []


def test_overridable_chinese_descriptions_do_not_say_this_channel() -> None:
    offenders = []
    for name, item in _overridable_fields().items():
        field, extra = item["field"], item["extra"]
        descriptions = [field.description or ""]
        i18n_description = extra.get("i18n_description")
        if isinstance(i18n_description, dict):
            descriptions.append(str(i18n_description.get("zh-CN", "")))
        if any("本频道" in text for text in descriptions):
            offenders.append(name)
    assert offenders == []


def test_switches_explain_the_three_levels() -> None:
    """改名后的三个开关在说明里写清「全局、实例、频道三层都能覆盖」"""
    fields = CoreConfig.model_fields
    for name in ("WORLDBOOK_ENABLED", "WORLDBOOK_MAX_CHARS", "MEMORY_ENABLE_FOR_CHAT"):
        extra = fields[name].json_schema_extra
        assert isinstance(extra, dict)
        assert "全局、实例、频道三层" in extra["i18n_description"]["zh-CN"], name
        assert "global, instance and channel levels" in extra["i18n_description"]["en-US"], name
    assert fields["WORLDBOOK_ENABLED"].title == "启用世界书"
    assert fields["MEMORY_ENABLE_FOR_CHAT"].title == "启用聊天记忆"
