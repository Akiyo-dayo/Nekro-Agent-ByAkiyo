"""酒馆卡的导入与导出往返

手工构造 V1 / V2 / V3 三张卡，JSON 与 PNG 两种来源各导入一次再导出 JSON，逐字段比对；
另钉住正文手改、取消系统提示词、头像重新编码与超大图、导入事务与「不碰频道 / 配置」、
以及 NA 原生人设的 V2 骨架。
"""

from __future__ import annotations

import base64
import json
from io import BytesIO
from typing import Any, AsyncIterator

import pytest
from PIL import Image
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_preset_card import DBPresetCard
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.schemas.tavern import CardImportOptions
from nekro_agent.services.tavern import card_png, importer
from nekro_agent.services.tavern.card_format import _convert_v1_to_v2
from nekro_agent.services.tavern.exporter import build_card_object, export_json, export_png

TEMPLATE = "{{system}}\n\n{{description}}\n\n{{personality}}\n\n{{scenario}}\n\n{{mes_example}}"


@pytest.fixture
async def db() -> AsyncIterator[None]:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


def _regex_script() -> dict[str, Any]:
    return {
        "id": "s1",
        "scriptName": "清理",
        "findRegex": "/foo/g",
        "replaceString": "bar",
        "trimStrings": [],
        "placement": [2],
        "disabled": False,
        "markdownOnly": False,
        "promptOnly": False,
        "runOnEdit": True,
        "substituteRegex": 0,
        "minDepth": None,
        "maxDepth": None,
    }


def _book_entry() -> dict[str, Any]:
    return {
        "id": 1,
        "keys": ["甲", "乙"],
        "secondary_keys": ["丙"],
        "comment": "条目",
        "content": "知识书正文",
        "constant": False,
        "selective": True,
        "insertion_order": 42,
        "enabled": True,
        "position": "before_char",
        "use_regex": True,
        "extensions": {"position": 4, "depth": 3, "case_sensitive": True},
    }


def _character_book() -> dict[str, Any]:
    return {"name": "卡内书", "scan_depth": 7, "entries": [_book_entry()], "extensions": {"unknown": 1}}


def _v2_data() -> dict[str, Any]:
    return {
        "name": "V2 角色",
        "description": "描述",
        "personality": "性格",
        "scenario": "场景",
        "first_mes": "开场白",
        "mes_example": "示例对话",
        "creator_notes": "备注",
        "system_prompt": "系统提示",
        "post_history_instructions": "后置指令",
        "alternate_greetings": ["备选一", "备选二"],
        "tags": ["标签一", "标签二"],
        "creator": "作者",
        "character_version": "1.2",
        "extensions": {
            "depth_prompt": {"prompt": "深度正文", "depth": 6, "role": "assistant"},
            "regex_scripts": [_regex_script()],
            "unknown_key": {"nested": 1},
        },
        "character_book": _character_book(),
        "source": ["来源"],
    }


def _v2_card() -> dict[str, Any]:
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": _v2_data(), "extra_top": {"keep": True}}


def _v3_data() -> dict[str, Any]:
    data = _v2_data()
    data.update(
        {
            "name": "V3 角色",
            "nickname": "昵称",
            "group_only_greetings": ["群聊开场"],
            "creation_date": 17000000,
            "modification_date": 17000100,
            "creator_notes_multilingual": {"zh": "中文备注", "en": "English"},
        },
    )
    return data


def _v3_card() -> dict[str, Any]:
    return {"spec": "chara_card_v3", "spec_version": "3.0", "data": _v3_data()}


def _v1_card() -> dict[str, Any]:
    return {
        "name": "V1 角色",
        "description": "V1 描述",
        "personality": "V1 性格",
        "scenario": "V1 场景",
        "first_mes": "V1 开场",
        "mes_example": "V1 示例",
        "creatorcomment": "V1 备注",
        "tags": "甲, 乙",
        "creator": "V1 作者",
        "depth_prompt_prompt": "V1 深度",
        "depth_prompt_depth": "6",
        "depth_prompt_role": "assistant",
        "extra_v1": "只在原文里",
    }


def _base_png(width: int = 2, height: int = 2) -> bytes:
    output = BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(output, format="PNG")
    return output.getvalue()


def _png_with_text(chara: dict[str, Any] | None = None, ccv3: dict[str, Any] | None = None, *, width: int = 2) -> bytes:
    """在 PNG 的 IEND 前插入指定的 tEXt 块；只写给了的那几块"""
    png = _base_png(width=width)
    chunks = card_png._walk_chunks(png)
    output = bytearray(card_png.PNG_SIGNATURE)
    for chunk_type, chunk_data in chunks:
        if chunk_type == b"IEND":
            for keyword, card in (("chara", chara), ("ccv3", ccv3)):
                if card is None:
                    continue
                encoded = base64.b64encode(json.dumps(card).encode("utf-8"))
                output += card_png._pack_chunk(b"tEXt", keyword.encode("latin-1") + b"\0" + encoded)
        output += card_png._pack_chunk(chunk_type, chunk_data)
    return bytes(output)


def _card_bytes(card: dict[str, Any], source: str) -> bytes:
    if source == "json":
        return json.dumps(card).encode("utf-8")
    if card.get("spec") == "chara_card_v3":
        return _png_with_text(ccv3=card)
    return _png_with_text(chara=card)


async def _import(card_bytes: bytes, **options: Any) -> tuple[DBPreset, DBPresetCard]:
    result = await importer.import_card(
        card_bytes,
        CardImportOptions(**options),
        owner_instance_key="",
        book_owner=("", ""),
        username="admin",
        template=TEMPLATE,
    )
    preset = await DBPreset.get(id=result.preset_id)
    card = await DBPresetCard.get(preset_id=preset.id)
    return preset, card


async def _import_with_result(card_bytes: bytes, **options: Any) -> tuple[DBPreset, DBPresetCard, Any]:
    result = await importer.import_card(
        card_bytes,
        CardImportOptions(**options),
        owner_instance_key="",
        book_owner=("", ""),
        username="admin",
        template=TEMPLATE,
    )
    preset = await DBPreset.get(id=result.preset_id)
    card = await DBPresetCard.get(preset_id=preset.id)
    return preset, card, result


async def _books_for(preset: DBPreset) -> list[tuple[DBWbBook, list[DBWbEntry]]]:
    books = await DBWbBook.filter(scope_kind="preset", preset_id=preset.id).order_by("id")
    return [(book, await DBWbEntry.filter(book_id=book.id)) for book in books]


async def _exported_payload(preset: DBPreset, card: DBPresetCard) -> dict[str, Any]:
    payload, _filename = export_json(preset, card, await _books_for(preset))
    return json.loads(payload)


class TestRoundTrip:
    @pytest.mark.parametrize("source", ["json", "png"])
    @pytest.mark.parametrize("build", [_v2_card, _v3_card, _v1_card], ids=["v2", "v3", "v1"])
    async def test_card_survives_import_and_export(self, db: None, build: Any, source: str) -> None:
        original = build()
        preset, card = await _import(_card_bytes(original, source))
        exported = await _exported_payload(preset, card)

        if original.get("spec") == "chara_card_v3":
            data = exported["data"]
            assert exported["spec"] == "chara_card_v3"
            assert data["modification_date"] != original["data"]["modification_date"]
            data = {key: value for key, value in data.items() if key != "modification_date"}
            expected = {key: value for key, value in original["data"].items() if key != "modification_date"}
            assert data == expected
        elif original.get("spec") == "chara_card_v2":
            assert exported == original
        else:
            assert exported["spec"] == "chara_card_v2"
            assert exported["data"] == _convert_v1_to_v2(original)
            for top_key, data_key in (
                ("name", "name"),
                ("description", "description"),
                ("personality", "personality"),
                ("scenario", "scenario"),
                ("first_mes", "first_mes"),
                ("mes_example", "mes_example"),
                ("creatorcomment", "creator_notes"),
                ("tags", "tags"),
            ):
                assert exported[top_key] == exported["data"][data_key]

    async def test_png_export_has_both_blocks_and_v3_defaults(self, db: None) -> None:
        preset, card = await _import(json.dumps(_v2_card()).encode("utf-8"))

        payload, filename = export_png(preset, card, await _books_for(preset))

        assert filename.endswith(".png")
        info = card_png.read_png(payload)
        assert "chara" in info.texts and "ccv3" in info.texts
        assert json.loads(card_png.card_text_from_png(info))["spec"] == "chara_card_v3"
        ccv3 = json.loads(base64.b64decode(info.texts["ccv3"]))
        chara = json.loads(base64.b64decode(info.texts["chara"]))
        assert ccv3["data"]["group_only_greetings"] == []
        assert ccv3["data"]["character_book"]["entries"][0]["use_regex"] is True
        assert ccv3["data"]["name"] == chara["data"]["name"]
        assert ccv3["data"]["description"] == chara["data"]["description"]
        assert chara["spec"] == "chara_card_v2"
        for key in ("name", "description", "personality", "scenario", "first_mes", "mes_example", "creatorcomment", "tags"):
            assert key in chara
        assert not any(str(key).startswith("na_") for key in ccv3)
        assert not any(str(key).startswith("na_") for key in ccv3["data"])

    async def test_avatar_is_reencoded_without_text_chunks(self, db: None) -> None:
        preset, _card = await _import(_png_with_text(chara=_v2_card()))

        assert preset.avatar.startswith("data:image/")
        image_bytes = base64.b64decode(preset.avatar.split(",", 1)[1])
        assert card_png.read_png(image_bytes).texts == {}

    async def test_oversized_image_uses_default_avatar(self, db: None) -> None:
        preset, _card = await _import(_png_with_text(chara=_v2_card(), width=5000))

        assert preset.avatar == ""


class TestEditedContent:
    async def test_edited_content_exports_as_description(self, db: None) -> None:
        preset, card = await _import(json.dumps(_v2_card()).encode("utf-8"))
        preset.content = "手改后的正文"
        await preset.save()

        exported = await _exported_payload(preset, card)

        data = exported["data"]
        assert data["description"] == "手改后的正文"
        for key in ("personality", "scenario", "mes_example", "system_prompt"):
            assert data[key] == ""

        reimported, _reimported_card = await _import(json.dumps(exported).encode("utf-8"))
        assert reimported.content == "手改后的正文"

    async def test_cancelled_system_prompt_is_not_in_content_but_stays_in_raw(self, db: None) -> None:
        preset, card = await _import(json.dumps(_v2_card()).encode("utf-8"), include_system_prompt=False)

        assert "系统提示" not in preset.content
        assert card.card_raw["data"]["system_prompt"] == "系统提示"
        exported = await _exported_payload(preset, card)
        assert exported["data"]["system_prompt"] == ""

    async def test_no_channel_or_config_is_touched(self, db: None) -> None:
        before_config = config.model_dump()
        await _import(json.dumps(_v2_card()).encode("utf-8"))

        assert await DBChatChannel.all().count() == 0
        assert config.model_dump() == before_config


class TestRuleOptions:
    def _card(self, script: dict[str, Any]) -> dict[str, Any]:
        card = _v2_card()
        card["data"]["extensions"]["regex_scripts"] = [script]
        return card

    async def test_option_can_disable_a_default_enabled_rule(self, db: None) -> None:
        _preset, card = await _import(json.dumps(self._card(_regex_script())).encode("utf-8"), rule_enabled={0: False})

        assert card.cleanup_rules[0]["enabled"] is False
        assert card.cleanup_rules[0]["disabled_reason"] == "manual"

    async def test_option_can_enable_a_rule_disabled_in_card(self, db: None) -> None:
        script = dict(_regex_script(), disabled=True)
        _preset, card = await _import(json.dumps(self._card(script)).encode("utf-8"), rule_enabled={0: True})

        assert card.cleanup_rules[0]["enabled"] is True
        assert card.cleanup_rules[0]["disabled_reason"] == ""

    async def test_option_cannot_override_a_semantic_disable(self, db: None) -> None:
        script = dict(_regex_script(), placement=[1])
        _preset, card, result = await _import_with_result(
            json.dumps(self._card(script)).encode("utf-8"),
            rule_enabled={0: True},
        )

        assert card.cleanup_rules[0]["enabled"] is False
        assert card.cleanup_rules[0]["disabled_reason"] == "not_ai_output"
        assert any("不能由导入选项覆盖" in note for note in result.notes)

    async def test_enabling_an_html_rule_over_the_save_limit_is_refused(self, db: None) -> None:
        """替换文字超过扩展页保存上限的 HTML 规则不在导入时打开

        放行的话规则表里会有一条启用着、却过不了保存校验的规则，之后改表里任何一条都 400。
        """
        script = dict(_regex_script(), replaceString="<div>" + "状" * 4500 + "</div>")
        _preset, card, result = await _import_with_result(
            json.dumps(self._card(script)).encode("utf-8"),
            rule_enabled={0: True},
        )

        assert card.cleanup_rules[0]["enabled"] is False
        assert card.cleanup_rules[0]["disabled_reason"] == "html_output"
        assert any("没有打开" in note and "替换文字" in note for note in result.notes)

    async def test_enabling_an_unsafe_rule_disabled_in_card_is_refused(self, db: None) -> None:
        """卡里关着的规则导入时不解析正则；打开前要补上这道检查"""
        script = dict(_regex_script(), disabled=True, findRegex="/(a+)+b/g")
        _preset, card, result = await _import_with_result(
            json.dumps(self._card(script)).encode("utf-8"),
            rule_enabled={0: True},
        )

        assert card.cleanup_rules[0]["enabled"] is False
        assert card.cleanup_rules[0]["disabled_reason"] == "disabled_in_card"
        assert any("没有打开" in note for note in result.notes)


class TestTransaction:
    async def test_book_failure_rolls_back_preset_and_card(self, db: None, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("建书失败")

        monkeypatch.setattr(importer.card_book_service, "create_card_book", _boom)

        with pytest.raises(RuntimeError):
            await _import(json.dumps(_v2_card()).encode("utf-8"))

        assert await DBPreset.all().count() == 0
        assert await DBPresetCard.all().count() == 0
        assert await DBWbBook.all().count() == 0
        assert await DBWbEntry.all().count() == 0


class TestNativePreset:
    async def test_native_preset_exports_v2_skeleton(self, db: None) -> None:
        preset = await DBPreset.create(
            name="原生人设",
            title="原生人设",
            avatar="",
            content="原生正文",
            description="原生备注",
            tags="甲,乙",
            author="作者",
            owner_instance_key="",
        )

        payload, _filename = export_json(preset, None, [])

        exported = json.loads(payload)
        assert exported["spec"] == "chara_card_v2"
        assert exported["spec_version"] == "2.0"
        data = exported["data"]
        assert data["name"] == "原生人设"
        assert data["description"] == "原生正文"
        assert data["creator_notes"] == "原生备注"
        assert data["tags"] == ["甲", "乙"]
        assert data["creator"] == "作者"
        assert data["extensions"] == {}
        assert "character_book" not in data

    async def test_native_preset_with_book_adds_character_book(self, db: None) -> None:
        preset = await DBPreset.create(
            name="原生人设",
            title="原生人设",
            avatar="",
            content="原生正文",
            description="",
            tags="",
            author="",
            owner_instance_key="",
        )
        book = await DBWbBook.create(name="人设书", scope_kind="preset", preset_id=preset.id)
        await DBWbEntry.create(book=book, uid=0, keys=["甲"], content="条目正文")

        exported = json.loads(export_json(preset, None, [(book, await DBWbEntry.filter(book_id=book.id))])[0])

        assert exported["data"]["character_book"]["entries"][0]["content"] == "条目正文"
        assert exported["spec"] == "chara_card_v2"


class TestBookDetails:
    async def test_untouched_book_and_rules_are_restored(self, db: None) -> None:
        preset, card = await _import(json.dumps(_v2_card()).encode("utf-8"))

        exported = await _exported_payload(preset, card)

        assert exported["data"]["character_book"]["name"] == "卡内书"
        assert exported["data"]["character_book"]["scan_depth"] == 7
        assert exported["data"]["character_book"]["extensions"] == {"unknown": 1}
        assert exported["data"]["character_book"]["entries"][0] == _book_entry()
        assert exported["data"]["extensions"]["regex_scripts"] == [_regex_script()]

    async def test_new_rule_without_original_uses_tavern_defaults(self, db: None) -> None:
        preset = await DBPreset.create(
            name="原生人设",
            title="原生人设",
            avatar="",
            content="正文",
            description="",
            tags="",
            author="",
            owner_instance_key="",
        )
        card = await DBPresetCard.create(
            preset=preset,
            card_spec="",
            source_fields={},
            assembled_sha256="",
            cleanup_rules=[
                {
                    "name": "新规则",
                    "find": "/x/g",
                    "replace": "y",
                    "trim": [],
                    "enabled": True,
                    "disabled_reason": "",
                    "original": None,
                },
            ],
        )

        exported = build_card_object(preset, card, [], spec="v2", now=None)  # type: ignore[arg-type]

        script = exported["data"]["extensions"]["regex_scripts"][0]
        assert script["scriptName"] == "新规则"
        assert script["placement"] == [2]
        assert script["disabled"] is False
        assert script["runOnEdit"] is True
        assert script["substituteRegex"] == 0
