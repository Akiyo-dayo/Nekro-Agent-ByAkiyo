"""共享到云端、从云端下载与从云端同步的酒馆卡兼容

所有云端调用都打桩：上传时抓 `PresetCreate` / `PresetUpdate` 请求体，下载与同步时
直接返回构造好的 `PresetDetail`。用例跑在 SQLite 内存库上，钉住信封的打包与拆包、
共享时的可见知识书、下载还原的事务回退，以及同步只动扩展行不动人设书。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_config_audit import DBConfigChangeAudit
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_preset_card import DBPresetCard
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.routers import presets as presets_router
from nekro_agent.routers.cloud import presets_market
from nekro_agent.schemas.tavern import CardImportOptions
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.tavern import cloud as cloud_service
from nekro_agent.services.tavern import exporter, importer
from nekro_agent.services.tavern.assemble import assemble_content, content_sha256
from nekro_agent.services.tavern.cloud import (
    ENVELOPE_KEY,
    build_envelope,
    cloud_content_field,
    parse_envelope,
)
from nekro_agent.services.tavern.limits import MAX_BOOK_ENTRIES
from nekro_agent.services.user.role import Role
from nekro_agent.systems.cloud.schemas.preset import PresetCreate, PresetUpdate

ADAPTER = "onebot_v11"
REMOTE_ID = "rm1"


class _CloudObject:
    """上游云端响应或请求体的替身；字段形状与真实 schema 一致，只是不走网络"""

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


def _platform_user() -> DBUser:
    return DBUser(
        id=1,
        username="plat",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Super,
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


def _tenant_user(username: str = "tadmin", tenant_id: int = 1) -> DBUser:
    return DBUser(
        id=tenant_id + 10,
        username=username,
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Admin,
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )


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


def _book_entry(content: str = "知识书正文") -> dict[str, Any]:
    return {
        "id": 1,
        "keys": ["甲", "乙"],
        "secondary_keys": ["丙"],
        "comment": "条目",
        "content": content,
        "constant": False,
        "selective": True,
        "insertion_order": 42,
        "enabled": True,
        "position": "before_char",
        "use_regex": True,
        "extensions": {"position": 4, "depth": 3, "case_sensitive": True},
    }


def _v3_card(name: str = "云端角色") -> dict[str, Any]:
    return {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {
            "name": name,
            "nickname": "昵称",
            "description": "描述",
            "personality": "性格",
            "scenario": "场景",
            "first_mes": "开场白",
            "mes_example": "示例对话",
            "creator_notes": "备注",
            "system_prompt": "系统提示",
            "post_history_instructions": "后置指令",
            "alternate_greetings": ["备选一"],
            "group_only_greetings": ["群聊开场"],
            "tags": ["标签一", "标签二"],
            "creator": "作者",
            "character_version": "1.2",
            "extensions": {
                "depth_prompt": {"prompt": "深度正文", "depth": 6, "role": "assistant"},
                "regex_scripts": [_regex_script()],
            },
            "character_book": {"name": "卡内书", "scan_depth": 7, "entries": [_book_entry()]},
        },
    }


def _json_field_value(value: Any) -> Any:
    """把测试里的字符串塞进 JSON 列：合法 JSON 原样给，不合法先包成 JSON 字符串"""
    if isinstance(value, str):
        try:
            json.loads(value)
        except ValueError:
            return json.dumps(value, ensure_ascii=False)
    return value


def _expected_stored_ext(raw: str) -> Any:
    """云端扩展文本落进 JSON 列后读回来的值"""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


async def _preset(
    *,
    owner: str = "",
    name: str = "人设",
    content: str = "正文",
    ext_data: Any = None,
    remote_id: str | None = None,
    on_shared: bool = False,
) -> DBPreset:
    preset = await DBPreset.create(
        name=name,
        title=f"{name}标题",
        avatar="data:image/png;base64,AAAA",
        content=content,
        description="描述",
        tags="甲,乙",
        author="作者",
        ext_data=None,
        owner_instance_key=owner,
        remote_id=remote_id,
        on_shared=on_shared,
    )
    if ext_data is not None:
        # 与下载回退同一做法：先建行再赋 JSON 列，避免构造器把字符串先解码一次。
        preset.ext_data = _json_field_value(ext_data)
        await preset.save()
    return preset


async def _import_v3(*, owner: str = "", book_owner: tuple[str, str] = ("", ""), username: str = "plat") -> tuple[DBPreset, DBPresetCard]:
    result = await importer.import_card(
        json.dumps(_v3_card()).encode("utf-8"),
        CardImportOptions(),
        owner_instance_key=owner,
        book_owner=book_owner,
        username=username,
        template=config.TAVERN_CARD_TEMPLATE,
    )
    preset = await DBPreset.get(id=result.preset_id)
    card = await DBPresetCard.get(preset_id=preset.id)
    # 共享接口要求头像非空；JSON 卡没有内嵌主图标时导入出来是空串。
    preset.avatar = "data:image/png;base64,AAAA"
    await preset.save()
    return preset, card


async def _books_for(preset: DBPreset) -> list[tuple[DBWbBook, list[DBWbEntry]]]:
    books = await DBWbBook.filter(scope_kind="preset", preset_id=preset.id).order_by("id")
    return [(book, await DBWbEntry.filter(book_id=book.id)) for book in books]


def _strip_modification_date(value: Any) -> Any:
    """往返比较时去掉每次导出都会变的修改时间"""
    if isinstance(value, dict):
        return {key: _strip_modification_date(item) for key, item in value.items() if key != "modification_date"}
    if isinstance(value, list):
        return [_strip_modification_date(item) for item in value]
    return value


def _capture_create(monkeypatch: pytest.MonkeyPatch) -> list[PresetCreate]:
    captured: list[PresetCreate] = []

    async def fake_create(preset_data: PresetCreate) -> Any:
        captured.append(preset_data)
        return _CloudObject(success=True, message="", data=_CloudObject(id=REMOTE_ID))

    monkeypatch.setattr(presets_router, "cloud_create_preset", fake_create)
    monkeypatch.setattr(presets_router, "generate_instance_id", lambda: "inst1")
    return captured


def _capture_update(monkeypatch: pytest.MonkeyPatch) -> list[PresetUpdate]:
    captured: list[PresetUpdate] = []

    async def fake_update(_remote_id: str, preset_data: PresetUpdate) -> Any:
        captured.append(preset_data)
        return _CloudObject(success=True, message="")

    monkeypatch.setattr(presets_router, "cloud_update_preset", fake_update)
    monkeypatch.setattr(presets_router, "generate_instance_id", lambda: "inst1")
    return captured


def _detail(*, ext_data: Any, content: str = "云端正文", **overrides: Any) -> _CloudObject:
    fields: dict[str, Any] = {
        "id": REMOTE_ID,
        "name": "云端人设",
        "title": "云端标题",
        "avatar": "data:image/png;base64,AAAA",
        "content": content,
        "description": "云端描述",
        "tags": "云,端",
        "author": "云端作者",
        "is_owner": False,
        "ext_data": ext_data,
        "created_at": None,
        "updated_at": None,
        "favorite_count": 0,
        "isFavorited": None,
    }
    fields.update(overrides)
    return _CloudObject(**fields)


def _serve_detail(monkeypatch: pytest.MonkeyPatch, detail: _CloudObject) -> None:
    async def fake_get(_remote_id: str) -> Any:
        return _CloudObject(success=True, error=None, message="", data=detail)

    monkeypatch.setattr(presets_market, "get_preset", fake_get)


def _serve_sync_detail(monkeypatch: pytest.MonkeyPatch, detail: _CloudObject) -> None:
    async def fake_get(_remote_id: str) -> Any:
        return _CloudObject(success=True, error=None, message="", data=detail)

    monkeypatch.setattr(presets_router, "get_preset", fake_get)


@pytest.fixture
async def world() -> AsyncIterator[dict[str, Any]]:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key="t1a",
            display_name="租户一",
            tenant_id=1,
            provider_account_id="bot_t1a",
        )
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key="t2a",
            display_name="租户二",
            tenant_id=2,
            provider_account_id="bot_t2a",
        )
        yield {
            "platform": _platform_user(),
            "tenant": _tenant_user(),
            "tenant2": _tenant_user("tadmin2", tenant_id=2),
        }
    finally:
        await Tortoise.close_connections()


class TestEnvelope:
    def test_roundtrip(self) -> None:
        card = _v3_card()
        raw = build_envelope(
            card,
            content="完整正文",
            prefill="预填",
            macros_enabled=False,
            greeting_pick="random",
            prev_ext_data={"old": 1},
        )

        envelope, reason = parse_envelope(raw)

        assert reason == ""
        assert envelope is not None
        assert envelope.card == card
        assert envelope.content == "完整正文"
        assert envelope.prefill == "预填"
        assert envelope.macros_enabled is False
        assert envelope.greeting_pick == "random"
        assert envelope.prev_ext_data == {"old": 1}

    @pytest.mark.parametrize("raw", [None, "", {}, [], {"legacy": 1}, "[]"])
    def test_not_an_envelope_has_no_reason(self, raw: Any) -> None:
        assert parse_envelope(raw) == (None, "")

    def test_invalid_greeting_pick_falls_back_to_first(self) -> None:
        raw = build_envelope(
            _v3_card(),
            content="正文",
            prefill="",
            macros_enabled=True,
            greeting_pick="whatever",
            prev_ext_data=None,
        )

        envelope, reason = parse_envelope(raw)

        assert reason == ""
        assert envelope is not None
        assert envelope.greeting_pick == "first"

    @pytest.mark.parametrize(
        ("payload", "reason_part"),
        [
            pytest.param({"v": 2, "card": {}, "content": ""}, "版本", id="bad-version"),
            pytest.param({"v": 1, "card": [], "content": ""}, "卡片", id="card-not-object"),
            pytest.param({"v": 1, "card": {}, "content": 1}, "正文", id="content-not-string"),
            pytest.param({"v": 1, "card": {}, "content": "", "na": []}, "NA", id="na-not-object"),
            pytest.param({"v": 1, "card": {}, "content": "", "na": {"prefill": 1}}, "预填", id="prefill-not-string"),
            pytest.param(
                {"v": 1, "card": {}, "content": "", "na": {"macros_enabled": "yes"}},
                "宏开关",
                id="macros-not-bool",
            ),
        ],
    )
    def test_bad_envelope_returns_reason(self, payload: dict[str, Any], reason_part: str) -> None:
        envelope, reason = parse_envelope(json.dumps({ENVELOPE_KEY: payload}, ensure_ascii=False))

        assert envelope is None
        assert reason_part in reason

    def test_oversized_string_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cloud_service, "MAX_CARD_JSON_BYTES", 16)

        envelope, reason = parse_envelope("x" * 17)

        assert envelope is None
        assert "大小上限" in reason

    def test_too_deep_returns_reason(self) -> None:
        deep: Any = {}
        for _ in range(40):
            deep = {"next": deep}
        payload = {ENVELOPE_KEY: {"v": 1, "card": {"deep": deep}, "content": ""}}

        envelope, reason = parse_envelope(payload)

        assert envelope is None
        assert "嵌套" in reason

    @pytest.mark.parametrize("empty", [None, "", {}, []])
    def test_empty_prev_ext_data_is_not_written(self, empty: Any) -> None:
        raw = build_envelope(
            _v3_card(),
            content="正文",
            prefill="",
            macros_enabled=True,
            greeting_pick="first",
            prev_ext_data=empty,
        )

        payload = json.loads(raw)[ENVELOPE_KEY]
        assert "prev_ext_data" not in payload

    def test_non_empty_prev_ext_data_is_written(self) -> None:
        raw = build_envelope(
            _v3_card(),
            content="正文",
            prefill="",
            macros_enabled=True,
            greeting_pick="first",
            prev_ext_data={"old": 1},
        )

        assert json.loads(raw)[ENVELOPE_KEY]["prev_ext_data"] == {"old": 1}


class TestCloudContentField:
    def test_5000_chars_is_unchanged(self) -> None:
        content = "甲" * 5000
        assert cloud_content_field(content) == content

    def test_5001_chars_is_truncated_to_4999_plus_ellipsis(self) -> None:
        content = "甲" * 5001
        field = cloud_content_field(content)

        assert len(field) == 5000
        assert field == "甲" * 4999 + "…"


class TestSharePreset:
    async def test_without_card_row_uploads_the_old_request(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = _capture_create(monkeypatch)
        # 没有扩展行且扩展位为空：请求体应与改造前逐字相同，`ext_data` 落成空串。
        preset = await _preset(name="普通人设", content="短正文", ext_data=None)

        await presets_router.share_preset(
            preset.id,
            is_sfw=True,
            instance_key="",
            _current_user=world["platform"],
        )

        expected = PresetCreate(
            name=preset.name,
            title=preset.title,
            avatar=preset.avatar,
            content="短正文",
            description=preset.description,
            tags=preset.tags,
            author=preset.author,
            ext_data="",
            is_sfw=True,
            instance_id="inst1",
        )
        assert captured[0].model_dump() == expected.model_dump()

    async def test_with_card_row_packs_full_content_and_na(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = _capture_create(monkeypatch)
        preset, card = await _import_v3()
        preset.content = "长正文" * 2000
        await preset.save()
        card.prefill = "预填字"
        card.macros_enabled = False
        card.greeting_pick = "random"
        await card.save()

        await presets_router.share_preset(
            preset.id,
            is_sfw=True,
            instance_key="",
            _current_user=world["platform"],
        )

        request = captured[0]
        assert request.content == preset.content[:4999] + "…"
        assert request.content != preset.content
        payload = json.loads(request.ext_data)[ENVELOPE_KEY]
        assert payload["v"] == 1
        assert payload["content"] == preset.content
        assert payload["na"] == {"prefill": "预填字", "macros_enabled": False, "greeting_pick": "random"}
        assert payload["card"]["spec"] == "chara_card_v3"
        assert payload["card"]["data"]["character_book"]["entries"]
        assert payload["card"]["data"]["extensions"]["regex_scripts"]

    async def test_tenant_envelope_only_contains_visible_books(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = _capture_create(monkeypatch)
        preset, _card = await _import_v3(owner="t1a", book_owner=(ADAPTER, "t1a"), username="tadmin")
        rival = await DBWbBook.create(
            name="别人的书",
            scope_kind="preset",
            adapter_key=ADAPTER,
            instance_key="r1x",
            preset_id=preset.id,
        )
        await DBWbEntry.create(book=rival, uid=0, keys=["乙"], content="别人看得到")

        await presets_router.share_preset(
            preset.id,
            is_sfw=True,
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        payload = json.loads(captured[0].ext_data)[ENVELOPE_KEY]
        entries = payload["card"]["data"]["character_book"]["entries"]
        assert [entry["content"] for entry in entries] == ["知识书正文"]


class TestSyncToCloud:
    async def test_without_card_row_uploads_the_old_request(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = _capture_update(monkeypatch)
        preset = await _preset(
            name="普通人设",
            content="短正文",
            ext_data=None,
            remote_id=REMOTE_ID,
            on_shared=True,
        )

        await presets_router.sync_to_cloud(
            preset.id,
            is_sfw=True,
            instance_key="",
            _current_user=world["platform"],
        )

        expected = PresetUpdate(
            name=preset.name,
            title=preset.title,
            avatar=preset.avatar,
            content="短正文",
            description=preset.description,
            tags=preset.tags,
            author=preset.author,
            ext_data="",
            is_sfw=True,
            instance_id="inst1",
        )
        assert captured[0].model_dump() == expected.model_dump()

    async def test_with_card_row_packs_envelope(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = _capture_update(monkeypatch)
        preset, card = await _import_v3()
        preset.content = "长正文" * 2000
        preset.remote_id = REMOTE_ID
        preset.on_shared = True
        await preset.save()
        card.prefill = "预填字"
        card.greeting_pick = "random"
        await card.save()

        await presets_router.sync_to_cloud(
            preset.id,
            is_sfw=True,
            instance_key="",
            _current_user=world["platform"],
        )

        request = captured[0]
        assert request.content == preset.content[:4999] + "…"
        payload = json.loads(request.ext_data)[ENVELOPE_KEY]
        assert payload["content"] == preset.content
        assert payload["na"]["prefill"] == "预填字"
        assert payload["na"]["greeting_pick"] == "random"

    async def test_tenant_envelope_only_contains_visible_books(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = _capture_update(monkeypatch)
        preset, _card = await _import_v3(owner="t1a", book_owner=(ADAPTER, "t1a"), username="tadmin")
        preset.remote_id = REMOTE_ID
        preset.on_shared = True
        await preset.save()
        rival = await DBWbBook.create(
            name="别人的书",
            scope_kind="preset",
            adapter_key=ADAPTER,
            instance_key="r1x",
            preset_id=preset.id,
        )
        await DBWbEntry.create(book=rival, uid=0, keys=["乙"], content="别人看得到")

        await presets_router.sync_to_cloud(
            preset.id,
            is_sfw=True,
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        payload = json.loads(captured[0].ext_data)[ENVELOPE_KEY]
        entries = payload["card"]["data"]["character_book"]["entries"]
        assert [entry["content"] for entry in entries] == ["知识书正文"]


class TestDownloadPreset:
    async def test_envelope_restores_card_book_and_scope(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        content = "完整正文" * 1200
        card = _v3_card()
        envelope = build_envelope(
            card,
            content=content,
            prefill="预填",
            macros_enabled=False,
            greeting_pick="random",
            prev_ext_data={"old": 1},
        )
        _serve_detail(monkeypatch, _detail(ext_data=envelope, content=cloud_content_field(content)))

        response = await presets_market.download_preset(
            REMOTE_ID,
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        assert response.ok is True
        assert response.note == ""
        preset = await DBPreset.get(remote_id=REMOTE_ID)
        assert preset.owner_instance_key == "t1a"
        assert preset.on_shared is False
        assert preset.content == content
        assert preset.ext_data == {"old": 1}
        assert preset.avatar == "data:image/png;base64,AAAA"

        stored_card = await DBPresetCard.get(preset_id=preset.id)
        assert stored_card.card_spec == "v3"
        assert stored_card.prefill == "预填"
        assert stored_card.macros_enabled is False
        assert stored_card.greeting_pick == "random"
        assert stored_card.post_history == "后置指令"
        assert stored_card.depth_prompt == "深度正文"
        assert stored_card.cleanup_rules[0]["enabled"] is True
        assembled = assemble_content(
            config.TAVERN_CARD_TEMPLATE,
            stored_card.source_fields if isinstance(stored_card.source_fields, dict) else {},
        )
        assert stored_card.assembled_sha256 == content_sha256(assembled)
        assert stored_card.assembled_sha256 != content_sha256(preset.content)

        book = await DBWbBook.get(preset_id=preset.id)
        assert (book.adapter_key, book.instance_key) == (ADAPTER, "t1a")
        entries = await DBWbEntry.filter(book_id=book.id)
        assert entries
        assert all(entry.trusted is False for entry in entries)
        assert entries[0].content == "知识书正文"

    @pytest.mark.parametrize(
        "ext_data",
        [
            pytest.param("{not json", id="not-json"),
            pytest.param(json.dumps({ENVELOPE_KEY: {"v": 2, "card": {}, "content": ""}}), id="bad-version"),
            pytest.param("book-overflow", id="book-overflow"),
        ],
    )
    async def test_bad_envelope_falls_back_to_plain_preset(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        ext_data: str,
    ) -> None:
        # 只有这一种的信封本身解析得出（卡片过不了上限），正文应取信封里的那份
        envelope_parses = ext_data == "book-overflow"
        if ext_data == "book-overflow":
            card = _v3_card()
            data = card["data"]
            assert isinstance(data, dict)
            data["character_book"] = {
                "name": "太大的书",
                "entries": [{"keys": ["甲"], "content": "条目"} for _ in range(MAX_BOOK_ENTRIES + 1)],
            }
            ext_data = build_envelope(
                card,
                content="正文",
                prefill="",
                macros_enabled=True,
                greeting_pick="first",
                prev_ext_data=None,
            )
        _serve_detail(monkeypatch, _detail(ext_data=ext_data, content="云端正文"))

        response = await presets_market.download_preset(
            REMOTE_ID,
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        assert response.ok is True
        assert "无法还原" in response.note
        preset = await DBPreset.get(remote_id=REMOTE_ID)
        # 信封本身解析得出（只是卡片过不了上限）时用信封里的完整正文；云端那一栏在共享时
        # 被截到 5000 字以内，用它会丢掉长正文的尾部。信封都解析不出来时才只能用云端那栏。
        assert preset.content == ("正文" if envelope_parses else "云端正文")
        assert preset.ext_data == _expected_stored_ext(ext_data)
        assert await DBPresetCard.filter(preset_id=preset.id).count() == 0
        assert await DBWbBook.filter(preset_id=preset.id).count() == 0

    async def test_book_failure_rolls_back_and_falls_back(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        envelope = build_envelope(
            _v3_card(),
            content="信封正文",
            prefill="",
            macros_enabled=True,
            greeting_pick="first",
            prev_ext_data=None,
        )
        _serve_detail(monkeypatch, _detail(ext_data=envelope, content="云端正文"))

        async def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("建书失败")

        monkeypatch.setattr(importer.card_book_service, "create_card_book", boom)

        response = await presets_market.download_preset(
            REMOTE_ID,
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        assert "无法还原" in response.note
        preset = await DBPreset.get(remote_id=REMOTE_ID)
        assert preset.content == "信封正文"
        assert await DBPresetCard.filter(preset_id=preset.id).count() == 0
        assert await DBWbBook.filter(preset_id=preset.id).count() == 0

    async def test_card_failure_keeps_the_full_content_of_a_long_preset(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """长正文共享后云端那一栏是截断的；卡片还原失败也不能拿它建人设"""
        content = "完整正文" * 1500
        card = _v3_card()
        data = card["data"]
        assert isinstance(data, dict)
        data["character_book"] = {
            "name": "太大的书",
            "entries": [{"keys": ["甲"], "content": "条目"} for _ in range(MAX_BOOK_ENTRIES + 1)],
        }
        envelope = build_envelope(
            card,
            content=content,
            prefill="",
            macros_enabled=True,
            greeting_pick="first",
            prev_ext_data=None,
        )
        _serve_detail(monkeypatch, _detail(ext_data=envelope, content=cloud_content_field(content)))

        response = await presets_market.download_preset(
            REMOTE_ID,
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        assert "无法还原" in response.note
        preset = await DBPreset.get(remote_id=REMOTE_ID)
        assert preset.content == content

    async def test_non_envelope_is_downloaded_as_before(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ext_data = json.dumps({"legacy": 1})
        _serve_detail(monkeypatch, _detail(ext_data=ext_data, content="云端正文"))

        response = await presets_market.download_preset(
            REMOTE_ID,
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        assert response.note == ""
        preset = await DBPreset.get(remote_id=REMOTE_ID)
        assert preset.content == "云端正文"
        assert preset.ext_data == {"legacy": 1}
        assert await DBPresetCard.filter(preset_id=preset.id).count() == 0
        assert await DBWbBook.filter(preset_id=preset.id).count() == 0


class TestSyncPreset:
    async def _local_with_card(self, owner: str) -> tuple[DBPreset, DBPresetCard, DBWbBook]:
        preset = await _preset(
            owner=owner,
            name="旧名字",
            content="旧正文",
            ext_data={"old": 1},
            remote_id=REMOTE_ID,
            on_shared=True,
        )
        card = await DBPresetCard.create(
            preset=preset,
            card_spec="v2",
            source_fields={"system": "", "description": "旧描述"},
            assembled_sha256="",
            prefill="预填",
            post_history="旧后置",
            macros_enabled=True,
            greeting_pick="first",
            cleanup_rules=[],
        )
        book = await DBWbBook.create(
            name="本地书",
            scope_kind="preset",
            adapter_key=ADAPTER,
            instance_key="t1a",
            preset_id=preset.id,
        )
        await DBWbEntry.create(book=book, uid=0, keys=["甲"], content="本地知识")
        return preset, card, book

    async def test_envelope_updates_card_and_content_but_not_book(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        preset, _card, book = await self._local_with_card("t1a")
        content = "新正文" * 1200
        envelope = build_envelope(
            _v3_card(),
            content=content,
            prefill="预填",
            macros_enabled=False,
            greeting_pick="random",
            prev_ext_data=None,
        )
        detail = _detail(
            ext_data=envelope,
            content=cloud_content_field(content),
            name="新名字",
            title="新标题",
            avatar="data:image/png;base64,BBBB",
            description="新描述",
            tags="新,标签",
            author="新作者",
        )
        _serve_sync_detail(monkeypatch, detail)

        response = await presets_router.sync_preset(
            preset.id,
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        assert response.ok is True
        assert response.note == "知识书没有随同步更新"
        await preset.refresh_from_db()
        assert preset.name == "新名字"
        assert preset.title == "新标题"
        assert preset.avatar == "data:image/png;base64,BBBB"
        assert preset.description == "新描述"
        assert preset.tags == "新,标签"
        assert preset.author == "新作者"
        assert preset.content == content
        assert preset.ext_data == {"old": 1}

        card = await DBPresetCard.get(preset_id=preset.id)
        assert card.card_spec == "v3"
        assert card.prefill == "预填"
        assert card.post_history == "后置指令"
        assert card.macros_enabled is False
        assert card.greeting_pick == "random"
        assert card.depth_prompt == "深度正文"
        assert card.cleanup_rules[0]["enabled"] is True
        assert card.assembled_sha256 == content_sha256(
            assemble_content(
                config.TAVERN_CARD_TEMPLATE,
                card.source_fields if isinstance(card.source_fields, dict) else {},
            ),
        )

        entries = await DBWbEntry.filter(book_id=book.id)
        assert [(entry.content, entry.trusted) for entry in entries] == [("本地知识", False)]

        audits = await DBConfigChangeAudit.all()
        assert len(audits) == 1
        assert audits[0].field == "preset.post_history"
        assert audits[0].operator == "tadmin"
        assert audits[0].operator_is_platform is False
        assert "旧后置" not in audits[0].old_value
        assert "后置指令" not in audits[0].new_value

    async def test_invalid_envelope_updates_only_the_preset(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        preset, card, book = await self._local_with_card("t1a")
        ext_data = json.dumps({ENVELOPE_KEY: {"v": 2, "card": {}, "content": ""}})
        _serve_sync_detail(monkeypatch, _detail(ext_data=ext_data, content="云端新正文"))

        response = await presets_router.sync_preset(
            preset.id,
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        assert response.note == "附带的酒馆卡数据无法还原"
        await preset.refresh_from_db()
        assert preset.content == "云端新正文"
        card = await DBPresetCard.get(preset_id=preset.id)
        assert card.prefill == "预填"
        assert card.post_history == "旧后置"
        assert await DBWbEntry.filter(book_id=book.id).count() == 1
        assert await DBConfigChangeAudit.all().count() == 0

    async def test_card_failure_keeps_the_full_content_from_the_envelope(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """信封解析得出、卡片还原失败：扩展行不动，正文用信封里的完整正文而不是云端截断那栏"""
        preset, _card, _book = await self._local_with_card("t1a")
        content = "新长正文" * 1500
        card = _v3_card()
        data = card["data"]
        assert isinstance(data, dict)
        data["character_book"] = {
            "name": "太大的书",
            "entries": [{"keys": ["甲"], "content": "条目"} for _ in range(MAX_BOOK_ENTRIES + 1)],
        }
        envelope = build_envelope(
            card,
            content=content,
            prefill="",
            macros_enabled=True,
            greeting_pick="first",
            prev_ext_data=None,
        )
        _serve_sync_detail(monkeypatch, _detail(ext_data=envelope, content=cloud_content_field(content)))

        response = await presets_router.sync_preset(
            preset.id,
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        assert response.note == "附带的酒馆卡数据无法还原"
        await preset.refresh_from_db()
        assert preset.content == content
        stored = await DBPresetCard.get(preset_id=preset.id)
        assert stored.post_history == "旧后置"

    async def test_missing_card_row_is_created(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        preset = await _preset(owner="t1a", remote_id=REMOTE_ID, on_shared=True)
        envelope = build_envelope(
            _v3_card(),
            content="同步正文",
            prefill="同步预填",
            macros_enabled=True,
            greeting_pick="first",
            prev_ext_data=None,
        )
        _serve_sync_detail(monkeypatch, _detail(ext_data=envelope, content="云端正文"))

        response = await presets_router.sync_preset(
            preset.id,
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        assert response.note == "知识书没有随同步更新"
        card = await DBPresetCard.get(preset_id=preset.id)
        assert card.card_spec == "v3"
        assert card.prefill == "同步预填"
        await preset.refresh_from_db()
        assert preset.content == "同步正文"


class TestShareDownloadRoundTrip:
    async def test_exported_card_survives_share_and_download(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = _capture_create(monkeypatch)
        preset, card = await _import_v3(owner="t1a", book_owner=(ADAPTER, "t1a"), username="tadmin")
        before_payload, _filename = exporter.export_json(preset, card, await _books_for(preset))
        before = json.loads(before_payload)

        await presets_router.share_preset(
            preset.id,
            is_sfw=True,
            instance_key="t1a",
            _current_user=world["tenant"],
        )
        request = captured[0]
        detail = _detail(
            ext_data=request.ext_data,
            content=request.content,
            name=request.name,
            title=request.title,
            avatar=request.avatar,
            description=request.description,
            tags=request.tags,
            author=request.author,
        )
        _serve_detail(monkeypatch, detail)

        response = await presets_market.download_preset(
            REMOTE_ID,
            instance_key="t2a",
            _current_user=world["tenant2"],
        )

        assert response.note == ""
        downloaded = await DBPreset.get(remote_id=REMOTE_ID, owner_instance_key="t2a")
        downloaded_card = await DBPresetCard.get(preset_id=downloaded.id)
        after_payload, _filename = exporter.export_json(
            downloaded,
            downloaded_card,
            await _books_for(downloaded),
        )
        after = json.loads(after_payload)

        assert _strip_modification_date(after) == _strip_modification_date(before)
        assert downloaded.content == preset.content
