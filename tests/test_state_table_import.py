"""导入酒馆卡时生成角色状态模板

要钉住的事：

1. 检查页：插件在导入目标上开着时，按卡内书里启用的 `[initvar]` 条目在内存里推一遍，报字段数、自动标了
   私密的字段路径（用平台层面的词表）与隐藏字段数；不落库；插件没开时只说一句；没有 `[initvar]` 时说没有；
2. 导入：插件开着时生成模板（来源 `initvar`），没开时不生成；已经有模板的不动；
3. 钩子没登记时报告里没有这一段；钩子出错、超时都不影响导入，报告写 `failed`。
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_state_template import DBStateTemplate
from nekro_agent.schemas.tavern import CardImportOptions, TargetGates
from nekro_agent.services.tavern import importer, state_hook
from plugins.builtin.state_table import import_hook, initvar, session, store, updater
from plugins.builtin.state_table.schema import Template, TemplateError

INITVAR = """
当前时间: 早上
秘密: 没有
_显示: 不给模型看
主角信息:
  穿着: 便服
  隐私: 无
"""


def _card(*entries: dict[str, Any]) -> bytes:
    book = {"name": "卡内书", "entries": list(entries)} if entries else None
    data: dict[str, Any] = {
        "name": "阿青",
        "description": "描述",
        "personality": "",
        "scenario": "",
        "first_mes": "",
        "mes_example": "",
        "extensions": {},
    }
    if book is not None:
        data["character_book"] = book
    return json.dumps({"spec": "chara_card_v2", "spec_version": "2.0", "data": data}, ensure_ascii=False).encode()


def _entry(comment: str, content: str, *, enabled: bool = True, order: int = 100, index: int = 0) -> dict[str, Any]:
    return {
        "id": index,
        "keys": [],
        "content": content,
        "extensions": {},
        "enabled": enabled,
        "insertion_order": order,
        "comment": comment,
        "constant": False,
        "selective": False,
    }


CARD = _card(
    _entry("[initvar]初始变量", INITVAR, index=1),
    _entry("[initvar]停用的", "多余: 1\n", enabled=False, index=2),
    _entry("普通条目", "一些设定", index=3),
)


@pytest.fixture
async def db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    monkeypatch.setattr(updater, "platform_words", lambda: ("秘密", "隐私"))
    monkeypatch.setattr(state_hook, "_hooks", {})
    state_hook.register_state_template_hook("state", import_hook.inspect_columns, import_hook.persist_template)
    try:
        yield
    finally:
        await Tortoise.close_connections()


def _active(monkeypatch: pytest.MonkeyPatch, active: bool) -> list[str]:
    seen: list[str] = []

    def check(_plugin_key: str, instance_key: str = "", _chat_key: str = "") -> bool:
        seen.append(instance_key)
        return active

    monkeypatch.setattr(import_hook, "is_plugin_active", check)
    return seen


async def _inspect(data: bytes = CARD, owner: str = "st1") -> Any:
    return await importer.inspect_card(
        data, template=config.TAVERN_CARD_TEMPLATE, target=TargetGates(), owner_instance_key=owner
    )


async def _import(data: bytes = CARD, owner: str = "st1") -> Any:
    return await importer.import_card(
        data,
        CardImportOptions(),
        owner_instance_key=owner,
        book_owner=("onebot_v11", owner),
        username="管理员",
        template=config.TAVERN_CARD_TEMPLATE,
    )


class TestInspect:
    async def test_preview_lists_private_paths_without_writing(self, db: None, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _active(monkeypatch, True)

        report = (await _inspect()).state_template

        assert report is not None and report.status == "preview"
        assert report.entries == 1  # 停用的那条不算
        assert sorted(report.private_paths) == ["主角信息/隐私", "秘密"]
        assert report.hidden == 1 and report.fields >= 5
        assert await DBStateTemplate.all().count() == 0
        assert seen == ["st1"]

    async def test_inactive_plugin_says_one_line(self, db: None, monkeypatch: pytest.MonkeyPatch) -> None:
        _active(monkeypatch, False)

        report = (await _inspect()).state_template

        assert report is not None and report.status == "inactive" and report.private_paths == []
        assert "启用后" in report.note

    async def test_without_initvar_or_book(self, db: None, monkeypatch: pytest.MonkeyPatch) -> None:
        _active(monkeypatch, True)

        no_initvar = (await _inspect(_card(_entry("普通条目", "设定")))).state_template
        no_book = (await _inspect(_card())).state_template

        assert no_initvar is not None and no_initvar.status == "no_initvar"
        assert no_book is None

    async def test_unparsable_initvar_is_failed(self, db: None, monkeypatch: pytest.MonkeyPatch) -> None:
        _active(monkeypatch, True)

        report = (await _inspect(_card(_entry("[initvar]坏的", "a: [1, 2\n  b: {")))).state_template

        assert report is not None and report.status == "failed" and report.entries == 1


class TestImport:
    async def test_active_plugin_generates_the_template(self, db: None, monkeypatch: pytest.MonkeyPatch) -> None:
        _active(monkeypatch, True)

        result = await _import()

        report = result.state_template
        assert report is not None and report.status == "generated"
        row = await DBStateTemplate.get(preset_id=result.preset_id)
        template = Template.from_json(row.field_defs)
        assert row.source == "initvar" and template.is_private(("秘密",))
        assert sorted(report.private_paths) == ["主角信息/隐私", "秘密"]
        assert "多余" not in row.initial

    async def test_inactive_plugin_generates_nothing(self, db: None, monkeypatch: pytest.MonkeyPatch) -> None:
        _active(monkeypatch, False)

        result = await _import()

        assert result.state_template is not None and result.state_template.status == "inactive"
        assert await DBStateTemplate.all().count() == 0

    async def test_existing_template_is_left_alone(self, db: None, monkeypatch: pytest.MonkeyPatch) -> None:
        _active(monkeypatch, True)
        result = await _import()
        row = await DBStateTemplate.get(preset_id=result.preset_id)

        report = await import_hook.persist_template(result.preset_id, "st1")

        assert report is not None and report.status == "exists"
        assert (await DBStateTemplate.get(preset_id=result.preset_id)).version == row.version

    async def test_no_hook_means_no_section(self, db: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(state_hook, "_hooks", {})

        assert (await _inspect()).state_template is None
        assert (await _import()).state_template is None
        assert await DBPreset.all().count() == 1

    async def test_hook_failure_and_timeout_do_not_stop_the_import(
        self, db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _active(monkeypatch, True)

        async def broken(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("坏了")

        monkeypatch.setattr(import_hook, "derive_from_book", broken)
        failed = await _import()

        async def slow(*_args: Any, **_kwargs: Any) -> Any:
            await asyncio.sleep(5)

        monkeypatch.setattr(import_hook, "derive_from_book", slow)
        monkeypatch.setattr(state_hook, "HOOK_TIMEOUT_SECONDS", 0.05)
        timed_out = await _import()

        assert failed.state_template is not None and failed.state_template.status == "failed"
        assert timed_out.state_template is not None and timed_out.state_template.note == "超时"
        assert await DBPreset.all().count() == 2 and await DBStateTemplate.all().count() == 0

    async def test_parsing_runs_off_the_event_loop(self, db: None, monkeypatch: pytest.MonkeyPatch) -> None:
        _active(monkeypatch, True)
        threads: list[tuple[str, int]] = []

        def spy(module: Any, name: str) -> None:
            real = module.parse_initvar_text

            def wrapped(text: str) -> Any:
                threads.append((name, threading.get_ident()))
                return real(text)

            monkeypatch.setattr(module, "parse_initvar_text", wrapped)

        spy(import_hook, "inspect")
        spy(session, "persist")
        loop_thread = threading.get_ident()

        assert (await _inspect()).state_template.status == "preview"  # type: ignore[union-attr]
        assert (await _import()).state_template.status == "generated"  # type: ignore[union-attr]

        assert {name for name, _ in threads} == {"inspect", "persist"}
        assert all(ident != loop_thread for _, ident in threads)

    async def test_failure_logs_carry_only_the_category(self, db: None, monkeypatch: pytest.MonkeyPatch) -> None:
        _active(monkeypatch, True)
        lines: list[str] = []
        monkeypatch.setattr(session, "logger", SimpleNamespace(warning=lines.append, info=lines.append))

        def bad_parse(_text: str) -> Any:
            raise initvar.InitvarError("初值里「秘密档案」写错了") from ValueError("秘密档案")

        def bad_derive(*_args: Any, **_kwargs: Any) -> Any:
            raise initvar.InitvarError("推导出的模板存不下: 秘密档案：重复定义") from TemplateError(
                "秘密档案：重复定义"
            )

        monkeypatch.setattr(session, "derive_template", bad_derive)
        derived = await _import()
        monkeypatch.setattr(session, "parse_initvar_text", bad_parse)
        await import_hook.persist_template(derived.preset_id, "st1")

        assert derived.state_template is not None and derived.state_template.status == "failed"
        assert any("TemplateError" in line for line in lines) and any("ValueError" in line for line in lines)
        assert all("秘密档案" not in line for line in lines)

    async def test_saved_template_matches_runtime_rules(self, db: None, monkeypatch: pytest.MonkeyPatch) -> None:
        _active(monkeypatch, True)

        result = await _import()

        row, template = await store.load_template(result.preset_id)
        assert row is not None and template is not None
        assert template.is_hidden(("_显示",)) and row.initial["当前时间"] == "早上"
