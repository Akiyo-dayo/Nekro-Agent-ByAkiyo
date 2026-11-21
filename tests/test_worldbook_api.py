"""世界书管理接口的行为用例

直接调用路由函数（与记忆管理那条用例同一做法），跑在真表（SQLite 内存库）上，不起
Qdrant、不调模型。要钉住四件事：

1. 可见范围只来自身份：平台方的全量、租户的实例，筛选参数（作用域类型 / 人设 / 频道 /
   人 / 启用 / 关键词）都不能把范围外的书带出来；
2. 「不存在」与「不是你的」在按 ID 的每个写接口上逐字相同，且失败后库里一行都没变；
3. 建书规则、信任级别、条目校验、状态流转、导入导出、触发测试各自的判据；
4. 复核改动涉及的边界：导入表单校验、频道书缺频道、指定人设的触发测试、删书清熔断记录、
   触发文字上限、导入时编号缺失的明细。

触发测试的窗口用真实消息行（`chat_message`）造，因为「某个人的书生不生效」看的是消息里
的发送者，合成消息只有固定的发送者。
"""

from __future__ import annotations

import asyncio
import inspect
import io
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator, Optional

import pytest
from pydantic import ValidationError as PydanticValidationError
from starlette.datastructures import UploadFile
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_identity import DBIdentityAlias
from nekro_agent.models.db_instance_group import DBInstanceDomainBinding, DBInstanceGroup
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.routers import worldbook
from nekro_agent.schemas.errors import NotFoundError, PermissionDeniedError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.schemas.worldbook import (
    PERSON_NOTE_MAX_CHARS,
    BookCreateRequest,
    BookUpdateRequest,
    EntryActionRequest,
    EntryCreateRequest,
    EntryUpdateRequest,
    PersonNoteCreateRequest,
    TriggerTestRequest,
)
from nekro_agent.services.memory.instance_scope import AdminMemoryScope
from nekro_agent.services.user.role import Role
from nekro_agent.services.worldbook import purpose as wb_purpose
from nekro_agent.services.worldbook import render, safe_regex, st_format
from nekro_agent.services.worldbook import service as wb_service
from nekro_agent.services.worldbook.types import (
    MAX_CONTENT_CHARS,
    MAX_KEYS,
    MAX_NAME_CHARS,
    MAX_TRIGGER_TEXT_CHARS,
)
from tests.worldbook_st_fixtures import dump, full_entry, world_of

ADAPTER = "onebot_v11"

CHAT_T1A = f"{ADAPTER}-t1a-group_1"
CHAT_T1B = f"{ADAPTER}-t1b-group_2"
CHAT_T2A = f"{ADAPTER}-t2a-group_3"
CHAT_P1 = f"{ADAPTER}-p1-group_4"

# 人设书的绑定对象。全局人设的归属实例是空串，平台方不选实例建书时只能绑它。
PRESET_GLOBAL = "全局人设"
PRESET_T1A = "租户一甲人设"
PRESET_T1B = "租户一乙人设"
PRESET_T2A = "租户二人设"


def _platform_user() -> DBUser:
    return DBUser(
        id=1,
        username="platform",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Super,
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


def _tenant_user(tenant_id: int = 1, *, user_id: int = 21, name: str = "tenant-admin") -> DBUser:
    return DBUser(
        id=user_id,
        username=name,
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Admin,
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )


@dataclass
class World:
    platform: DBUser
    tenant: DBUser
    tenant2: DBUser
    group: DBInstanceGroup
    instances: dict[str, DBAdapterInstance]
    channels: dict[str, DBChatChannel]
    presets: dict[str, DBPreset]
    books: dict[str, DBWbBook]
    entries: dict[str, DBWbEntry]


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    # 共享组配置在别的用例里可能被改过；域成员必须只来自本用例建的实例组，否则
    # 「域书在哪个频道生效」会跟着上一个用例的残留走。
    monkeypatch.setattr(config, "MEMORY_INSTANCE_SHARING_GROUPS", {})

    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield await _seed_world()
    finally:
        await Tortoise.close_connections()


async def _instance(instance_key: str, *, tenant_id: Optional[int], display_name: str, bot_account: str) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        display_name=display_name,
        tenant_id=tenant_id,
        provider_account_id=bot_account,
    )


async def _channel(chat_key: str, *, instance_key: str, channel_id: str, name: str, preset_id: int) -> DBChatChannel:
    return await DBChatChannel.create(
        chat_key=chat_key,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        channel_id=channel_id,
        channel_name=name,
        channel_type="group",
        preset_id=preset_id,
        data="{}",
        workspace_id=None,
    )


async def _message(chat_key: str, *, instance_key: str, account: str, sender_id: str, content: str, message_id: str) -> DBChatMessage:
    return await DBChatMessage.create(
        sender_id=sender_id,
        sender_name=account,
        sender_nickname=account,
        is_tome=0,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        message_id=message_id,
        chat_key=chat_key,
        chat_type="group",
        platform_userid=account,
        content_text=content,
        content_data="{}",
        raw_cq_code="",
        ext_data="",
        send_timestamp=1_700_000,
    )


async def _preset(name: str, owner_instance_key: str) -> DBPreset:
    return await DBPreset.create(
        name=name,
        title=name,
        avatar="",
        content=f"{name}的设定",
        description="",
        tags="",
        author="test",
        owner_instance_key=owner_instance_key,
    )


async def _book(
    name: str,
    *,
    scope_kind: str,
    adapter_key: str,
    instance_key: str,
    preset_id: int = 0,
    chat_key: str = "",
    person_account: str = "",
    enabled: bool = True,
    scan_depth: int = 4,
    budget_tokens: int = 0,
    description: str = "",
    scan_names: bool = False,
) -> DBWbBook:
    return await DBWbBook.create(
        name=name,
        description=description,
        scope_kind=scope_kind,
        adapter_key=adapter_key,
        instance_key=instance_key,
        preset_id=preset_id,
        chat_key=chat_key,
        person_account=person_account,
        enabled=enabled,
        scan_depth=scan_depth,
        budget_tokens=budget_tokens,
        scan_names=scan_names,
    )


async def _entry(
    book: DBWbBook,
    *,
    uid: int,
    keys: tuple[str, ...] = (),
    content: str = "正文",
    constant: bool = False,
    order: int = 100,
    position: str = "before_history",
    enabled: bool = True,
    trusted: bool = False,
    status: str = "active",
    ext: Optional[dict[str, Any]] = None,
    scan_depth: Optional[int] = None,
    secondary_keys: tuple[str, ...] = (),
    selective_logic: str = "and_any",
    use_regex: bool = False,
    depth: int = 4,
    sticky: Optional[int] = None,
    cooldown: Optional[int] = None,
    delay: Optional[int] = None,
    comment: str = "",
    ignore_budget: bool = False,
) -> DBWbEntry:
    return await DBWbEntry.create(
        book=book,
        uid=uid,
        comment=comment,
        keys=list(keys),
        secondary_keys=list(secondary_keys),
        selective_logic=selective_logic,
        use_regex=use_regex,
        constant=constant,
        content=content,
        order=order,
        position=position,
        depth=depth,
        scan_depth=scan_depth,
        sticky=sticky,
        cooldown=cooldown,
        delay=delay,
        enabled=enabled,
        trusted=trusted,
        ignore_budget=ignore_budget,
        status=status,
        ext=ext or {},
    )


async def _seed_world() -> World:
    platform = _platform_user()
    tenant = _tenant_user()
    tenant2 = _tenant_user(2, user_id=22, name="tenant2-admin")

    group = await DBInstanceGroup.create(name="租户一记忆域", tenant_id=1)
    instances = {
        "t1a": await _instance("t1a", tenant_id=1, display_name="租户一甲", bot_account="bot_t1a"),
        "t1b": await _instance("t1b", tenant_id=1, display_name="租户一乙", bot_account="bot_t1b"),
        "t2a": await _instance("t2a", tenant_id=2, display_name="租户二", bot_account="bot_t2a"),
        "p1": await _instance("p1", tenant_id=None, display_name="平台实例", bot_account="bot_p1"),
    }
    # 同一租户的两个实例放进同一个记忆域：域书因此会同时在这两个实例的频道生效，
    # 而频道书只在它绑的那个频道生效——两条腿的差别只有靠这个域才看得出来。
    await DBInstanceDomainBinding.bind_to_groups(ADAPTER, "t1a", memory_group=group)
    await DBInstanceDomainBinding.bind_to_groups(ADAPTER, "t1b", memory_group=group)

    presets = {
        "global": await _preset(PRESET_GLOBAL, ""),
        "t1a": await _preset(PRESET_T1A, "t1a"),
        "t1b": await _preset(PRESET_T1B, "t1b"),
        "t2a": await _preset(PRESET_T2A, "t2a"),
    }

    channels = {
        "t1a": await _channel(CHAT_T1A, instance_key="t1a", channel_id="group_1", name="租户一甲群", preset_id=presets["t1a"].id),
        "t1b": await _channel(CHAT_T1B, instance_key="t1b", channel_id="group_2", name="租户一乙群", preset_id=presets["t1b"].id),
        "t2a": await _channel(CHAT_T2A, instance_key="t2a", channel_id="group_3", name="租户二群", preset_id=presets["t2a"].id),
        "p1": await _channel(CHAT_P1, instance_key="p1", channel_id="group_4", name="平台群", preset_id=presets["global"].id),
    }

    # 别名只落在一跳上：`u1alt` 是 `u1main` 的副号。按人筛选与 person 书都要靠它命中。
    # 域编码必须用实例组那一份（`t1a` 已经入组），写成 `i:适配器:实例` 的别名落在
    # 自成一域的编码下，展开时一条都查不到。
    await DBIdentityAlias.create(domain_id=f"g:{group.id}", platform_userid="u1alt", person_uid="u1main")

    # 触发测试的窗口：一条来自副号、一条来自 Bot、再来两条带关键词的副号发言。
    # 合成消息的发送者是固定的 `test` 且没有平台账号，person 书只能靠真实消息验，
    # 所以关键词要真的写进消息正文里。最早那条与最后那条各有专属关键词，用来验
    # 「只扫最后 N 条」是按时间算的。
    await _message(CHAT_T1A, instance_key="t1a", account="u1alt", sender_id="u1alt", content="今天天气不错", message_id="m-t1a-1")
    await _message(CHAT_T1A, instance_key="t1a", account="bot_t1a", sender_id="-1", content="我是机器人", message_id="m-t1a-2")
    await _message(CHAT_T1A, instance_key="t1a", account="u1alt", sender_id="u1alt", content="个人相关的话题", message_id="m-t1a-3")
    await _message(CHAT_T1A, instance_key="t1a", account="u1alt", sender_id="u1alt", content="最新的一条", message_id="m-t1a-4")

    books: dict[str, DBWbBook] = {
        "domain_t1a": await _book("租户一域书", scope_kind="domain", adapter_key=ADAPTER, instance_key="t1a"),
        "channel_t1a": await _book(
            "租户一频道书",
            scope_kind="channel",
            adapter_key=ADAPTER,
            instance_key="t1a",
            chat_key=CHAT_T1A,
        ),
        "preset_t1a": await _book(
            "租户一私有人设书",
            scope_kind="preset",
            adapter_key=ADAPTER,
            instance_key="t1a",
            preset_id=presets["t1a"].id,
        ),
        "person_t1a": await _book(
            "租户一个人书",
            scope_kind="person",
            adapter_key=ADAPTER,
            instance_key="t1a",
            person_account="u1main",
            scan_depth=50,
        ),
        "person_bot": await _book(
            "Bot 个人书",
            scope_kind="person",
            adapter_key=ADAPTER,
            instance_key="t1a",
            person_account="bot_t1a",
            scan_depth=50,
        ),
        "budget_t1a": await _book(
            "租户一预算书",
            scope_kind="domain",
            adapter_key=ADAPTER,
            instance_key="t1a",
            budget_tokens=1,
        ),
        "disabled_t1a": await _book(
            "租户一停用书",
            scope_kind="domain",
            adapter_key=ADAPTER,
            instance_key="t1a",
            enabled=False,
        ),
        "global_preset": await _book(
            "全局人设书",
            scope_kind="preset",
            adapter_key="",
            instance_key="",
            preset_id=presets["global"].id,
            description="覆盖全部实例的全局人设书",
        ),
        "domain_t1b": await _book("租户一乙域书", scope_kind="domain", adapter_key=ADAPTER, instance_key="t1b"),
        "domain_t2a": await _book("租户二域书", scope_kind="domain", adapter_key=ADAPTER, instance_key="t2a"),
        "domain_p1": await _book("平台实例书", scope_kind="domain", adapter_key=ADAPTER, instance_key="p1"),
    }

    entries: dict[str, DBWbEntry] = {
        "domain_key": await _entry(books["domain_t1a"], uid=0, keys=("龙",), content="域书的正文", order=100),
        "domain_constant": await _entry(
            books["domain_t1a"],
            uid=1,
            constant=True,
            content="常驻正文",
            order=50,
            position="after_persona",
        ),
        "channel_key": await _entry(books["channel_t1a"], uid=0, keys=("频道",), content="频道正文"),
        "preset_key": await _entry(books["preset_t1a"], uid=0, keys=("人设",), content="人设正文"),
        "person_key": await _entry(books["person_t1a"], uid=0, keys=("个人",), content="个人正文"),
        "person_bot_key": await _entry(books["person_bot"], uid=0, keys=("机器人",), content="机器人正文"),
        "budget_key": await _entry(books["budget_t1a"], uid=0, keys=("龙",), content="预算不够的正文", order=10),
        "disabled_key": await _entry(books["disabled_t1a"], uid=0, keys=("龙",), content="停用书的正文"),
        "global_key": await _entry(books["global_preset"], uid=0, keys=("全局",), content="全局正文"),
        "t1b_key": await _entry(books["domain_t1b"], uid=0, keys=("龙",), content="乙域正文"),
        "t2a_key": await _entry(books["domain_t2a"], uid=0, keys=("龙",), content="租户二正文"),
        "p1_key": await _entry(books["domain_p1"], uid=0, keys=("龙",), content="平台正文"),
        # 扫描深度只有 1 条，关键词只在最早那条消息里：窗口按时间升序理解「最后一条」时
        # 它扫不到，把倒序列表直接当窗口用时就会误命中。这条专门钉住那个顺序。
        "depth_one": await _entry(books["domain_t1a"], uid=2, keys=("今天天气不错",), content="只扫一条", scan_depth=1),
    }

    return World(
        platform=platform,
        tenant=tenant,
        tenant2=tenant2,
        group=group,
        instances=instances,
        channels=channels,
        presets=presets,
        books=books,
        entries=entries,
    )


def _book_ids(response: Any) -> set[int]:
    return {int(item.id) for item in response.items}


def _snapshot(row: Any) -> dict[str, Any]:
    """把一行读成可比较的字典

    世界书两张表没有 `to_dict`，这里按元数据里的字段名逐个取值：外键读成它的主键，
    时间读成 ISO 串，于是「失败后库里一行都没变」可以逐字段比对，而不是只看几个显眼列。
    """
    data: dict[str, Any] = {}
    for name in row._meta.db_fields:
        # 外键列在行上叫 `<字段>_id`，直接取字段名拿到的是关联对象，比较起来会连它的
        # 更新时间一起带进来。
        attribute = f"{name}_id" if hasattr(row, f"{name}_id") else name
        value = getattr(row, attribute, None)
        data[name] = value.isoformat() if isinstance(value, datetime) else value
    return data


async def _expect_not_found(awaitable: Any, expected: str) -> None:
    with pytest.raises(NotFoundError) as exc:
        await awaitable
    assert str(exc.value) == expected


async def _import_new(
    user: DBUser,
    data: bytes,
    *,
    instance_key: str = "t1a",
    adapter_key: str = ADAPTER,
    name: str = "导入书",
    description: str = "",
    scope_kind: str = "domain",
    preset_id: int = 0,
    chat_key: str = "",
    person_account: str = "",
    enabled: bool = True,
    scan_depth: Optional[int] = None,
    budget_tokens: int = 0,
    inject_mode: str = "both",
    active_scene: str = "both",
    case_sensitive: bool = False,
    match_whole_words: bool = False,
    rewrite_name_vars: bool = True,
    scan_names: bool = True,
) -> Any:
    """调导入建书接口；表单字段全部显式传

    直接调用路由函数时，`Form(...)` 默认值拿到的是 `FieldInfo` 对象而不是真正的默认值，
    漏传任何一个都会让请求对象里出现一个不是字符串的东西。这个帮手把整套字段补齐，
    用例只写自己关心的那几个。
    """
    return await worldbook.import_new_book(
        file=UploadFile(file=io.BytesIO(data), filename="w.json"),
        name=name,
        description=description,
        scope_kind=scope_kind,
        preset_id=preset_id,
        chat_key=chat_key,
        person_account=person_account,
        enabled=enabled,
        scan_depth=scan_depth,
        budget_tokens=budget_tokens,
        inject_mode=inject_mode,
        active_scene=active_scene,
        case_sensitive=case_sensitive,
        match_whole_words=match_whole_words,
        rewrite_name_vars=rewrite_name_vars,
        scan_names=scan_names,
        instance_key=instance_key,
        adapter_key=adapter_key,
        _current_user=user,
    )


def _upload(data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename="w.json")


class TestPlatformScope:
    async def test_without_instance_sees_every_book_including_the_global_preset_one(self, world: World) -> None:
        response = await worldbook.list_books(_current_user=world.platform, page_size=100)

        assert _book_ids(response) == {book.id for book in world.books.values()}
        global_book = next(item for item in response.items if item.id == world.books["global_preset"].id)
        assert global_book.adapter_key == ""
        assert global_book.instance_key == ""
        assert global_book.preset_id == world.presets["global"].id

    async def test_selected_instance_only_returns_its_own_books(self, world: World) -> None:
        response = await worldbook.list_books(
            _current_user=world.platform,
            instance_key="t1a",
            page_size=100,
        )

        assert _book_ids(response) == {
            world.books[key].id
            for key in ("domain_t1a", "channel_t1a", "preset_t1a", "person_t1a", "person_bot", "budget_t1a", "disabled_t1a")
        }

    async def test_scope_kind_preset_and_channel_filters(self, world: World) -> None:
        channels = await worldbook.list_books(
            _current_user=world.platform,
            scope_kind="channel",
            page_size=100,
        )
        assert _book_ids(channels) == {world.books["channel_t1a"].id}

        presets = await worldbook.list_books(
            _current_user=world.platform,
            preset_id=world.presets["global"].id,
            page_size=100,
        )
        assert _book_ids(presets) == {world.books["global_preset"].id}

        by_channel = await worldbook.list_books(
            _current_user=world.platform,
            chat_key=CHAT_T1A,
            page_size=100,
        )
        assert _book_ids(by_channel) == {world.books["channel_t1a"].id}

    async def test_person_filter_expands_the_alias(self, world: World) -> None:
        # 书绑的是主号，筛的是副号：不展开别名就找不到这本刚建的书。
        response = await worldbook.list_books(
            _current_user=world.platform,
            instance_key="t1a",
            person="u1alt",
            page_size=100,
        )
        assert _book_ids(response) == {world.books["person_t1a"].id}

    async def test_enabled_and_keyword_filters(self, world: World) -> None:
        disabled = await worldbook.list_books(
            _current_user=world.platform,
            enabled=False,
            page_size=100,
        )
        assert _book_ids(disabled) == {world.books["disabled_t1a"].id}

        by_description = await worldbook.list_books(
            _current_user=world.platform,
            q="覆盖全部实例",
            page_size=100,
        )
        assert _book_ids(by_description) == {world.books["global_preset"].id}

        by_name = await worldbook.list_books(
            _current_user=world.platform,
            q="租户一频道",
            page_size=100,
        )
        assert _book_ids(by_name) == {world.books["channel_t1a"].id}

    async def test_pagination_and_sorting(self, world: World) -> None:
        first = await worldbook.list_books(
            _current_user=world.platform,
            sort="name",
            order="asc",
            page=1,
            page_size=3,
        )
        assert first.total == len(world.books)
        assert len(first.items) == 3
        assert [item.name for item in first.items] == sorted(book.name for book in world.books.values())[:3]

        second = await worldbook.list_books(
            _current_user=world.platform,
            sort="name",
            order="asc",
            page=2,
            page_size=3,
        )
        assert [item.name for item in second.items] == sorted(book.name for book in world.books.values())[3:6]

    async def test_book_detail_carries_entry_counts_and_labels(self, world: World) -> None:
        detail = await worldbook.get_book(world.books["domain_t1a"].id, _current_user=world.platform)
        assert detail.entry_count == 3
        assert detail.quarantined_count == 0
        assert detail.scope_kind == "domain"

        preset_detail = await worldbook.get_book(world.books["preset_t1a"].id, _current_user=world.platform)
        assert preset_detail.preset_name == PRESET_T1A
        assert preset_detail.binding_missing is False

        channel_detail = await worldbook.get_book(world.books["channel_t1a"].id, _current_user=world.platform)
        assert channel_detail.channel_name == "租户一甲群"
        assert channel_detail.binding_missing is False


class TestTenantScope:
    async def test_tenant_must_select_an_instance(self, world: World) -> None:
        with pytest.raises(ValidationError):
            await worldbook.list_books(_current_user=world.tenant)

    async def test_another_tenants_instance_is_not_found(self, world: World) -> None:
        with pytest.raises(NotFoundError):
            await worldbook.list_books(_current_user=world.tenant, instance_key="t2a")

    async def test_tenant_sees_its_own_books_and_the_platform_global_preset_book_read_only(self, world: World) -> None:
        """自己实例的书，加上平台方给全局人设建的书（只读）；别的租户、别的实例的书照旧看不到"""
        response = await worldbook.list_books(
            _current_user=world.tenant,
            instance_key="t1a",
            page_size=100,
        )
        assert _book_ids(response) == {
            world.books[key].id
            for key in (
                "domain_t1a",
                "channel_t1a",
                "preset_t1a",
                "person_t1a",
                "person_bot",
                "budget_t1a",
                "disabled_t1a",
                "global_preset",
            )
        }
        read_only = {item.id for item in response.items if item.read_only}
        assert read_only == {world.books["global_preset"].id}

    async def test_foreign_and_missing_books_are_indistinguishable(self, world: World) -> None:
        with pytest.raises(NotFoundError) as missing:
            await worldbook.get_book(999_999, _current_user=world.tenant)
        expected = str(missing.value)

        book_snapshots = {book_id: _snapshot(book) for book_id, book in world.books.items()}
        entry_snapshots = {entry_id: _snapshot(entry) for entry_id, entry in world.entries.items()}

        for key in ("domain_t2a", "domain_p1"):
            book_id = world.books[key].id
            await _expect_not_found(worldbook.get_book(book_id, _current_user=world.tenant), expected)
            await _expect_not_found(
                worldbook.update_book(book_id, BookUpdateRequest(name="越权改名"), _current_user=world.tenant),
                expected,
            )
            await _expect_not_found(worldbook.delete_book(book_id, _current_user=world.tenant), expected)
            await _expect_not_found(worldbook.list_entries(book_id, _current_user=world.tenant), expected)
            await _expect_not_found(
                worldbook.import_into_book(book_id, file=_upload(dump(world_of([full_entry(0)]))), _current_user=world.tenant),
                expected,
            )

        for key, book_id in world.books.items():
            refreshed = await DBWbBook.get(id=book_id.id)
            assert _snapshot(refreshed) == book_snapshots[key]
        for key, entry_id in world.entries.items():
            refreshed_entry = await DBWbEntry.get(id=entry_id.id)
            assert _snapshot(refreshed_entry) == entry_snapshots[key]

    async def test_foreign_and_missing_entries_are_indistinguishable(self, world: World) -> None:
        with pytest.raises(NotFoundError) as missing:
            await worldbook.update_entry(999_999, EntryUpdateRequest(comment="越权"), _current_user=world.tenant)
        expected = str(missing.value)

        entry_snapshots = {entry_id: _snapshot(entry) for entry_id, entry in world.entries.items()}

        # 平台方全局人设书里的条目对租户只读可见，写操作回 403，见 `TestPlatformGlobalBookReadOnly`
        for key in ("t2a_key", "p1_key"):
            entry_id = world.entries[key].id
            await _expect_not_found(
                worldbook.update_entry(entry_id, EntryUpdateRequest(comment="越权"), _current_user=world.tenant),
                expected,
            )
            await _expect_not_found(
                worldbook.entry_action(entry_id, EntryActionRequest(action="quarantine"), _current_user=world.tenant),
                expected,
            )
            await _expect_not_found(worldbook.delete_entry(entry_id, _current_user=world.tenant), expected)

        for key, entry_id in world.entries.items():
            refreshed = await DBWbEntry.get(id=entry_id.id)
            assert _snapshot(refreshed) == entry_snapshots[key]

    async def test_quarantine_list_is_scoped_too(self, world: World) -> None:
        await worldbook.entry_action(
            world.entries["t2a_key"].id,
            EntryActionRequest(action="quarantine"),
            _current_user=world.platform,
        )
        await worldbook.entry_action(
            world.entries["domain_key"].id,
            EntryActionRequest(action="quarantine"),
            _current_user=world.platform,
        )

        platform = await worldbook.list_quarantine(_current_user=world.platform, page_size=100)
        assert {item.entry_id for item in platform.items} == {
            world.entries["t2a_key"].id,
            world.entries["domain_key"].id,
        }

        tenant = await worldbook.list_quarantine(
            _current_user=world.tenant,
            instance_key="t1a",
            page_size=100,
        )
        assert {item.entry_id for item in tenant.items} == {world.entries["domain_key"].id}


class TestScopeOptionPresets:
    """筛选项里的人设是「能拿来建书的人设」，与有没有记忆无关（夹具里一条记忆都没有）"""

    async def test_tenant_sees_only_its_own_instance_presets(self, world: World) -> None:
        options = await worldbook.get_scope_options(instance_key="t1a", _current_user=world.tenant)
        assert [item.preset_id for item in options.presets] == [world.presets["t1a"].id]

    async def test_platform_with_an_instance_sees_global_and_that_instance(self, world: World) -> None:
        options = await worldbook.get_scope_options(instance_key="t1a", _current_user=world.platform)
        assert {item.preset_id for item in options.presets} == {
            world.presets["global"].id,
            world.presets["t1a"].id,
        }

    async def test_platform_without_an_instance_sees_every_preset(self, world: World) -> None:
        options = await worldbook.get_scope_options(_current_user=world.platform)
        assert {item.preset_id for item in options.presets} == {preset.id for preset in world.presets.values()}
        names = {item.preset_id: item.name for item in options.presets}
        assert names[world.presets["global"].id]


class TestCreateRules:
    async def test_domain_and_person_books_need_an_instance_for_the_platform(self, world: World) -> None:
        for scope_kind in ("domain", "person"):
            with pytest.raises(ValidationError) as exc:
                await worldbook.create_book(
                    BookCreateRequest(name="没有实例的书", scope_kind=scope_kind, person_account="u1main"),
                    _current_user=world.platform,
                )
            assert "请先选择实例" in str(exc.value)

    async def test_tenant_cannot_bind_a_global_preset(self, world: World) -> None:
        # 全局共享人设对实例只读，判据在 `get_writable_preset` 里，按 403 处理。
        with pytest.raises(PermissionDeniedError):
            await worldbook.create_book(
                BookCreateRequest(name="全局人设书", scope_kind="preset", preset_id=world.presets["global"].id),
                instance_key="t1a",
                _current_user=world.tenant,
            )

    async def test_tenant_cannot_bind_another_instances_preset(self, world: World) -> None:
        await _expect_not_found(
            worldbook.create_book(
                BookCreateRequest(name="别人的人设书", scope_kind="preset", preset_id=world.presets["t2a"].id),
                instance_key="t1a",
                _current_user=world.tenant,
            ),
            str(NotFoundError(resource="人设")),
        )

    async def test_tenant_can_bind_its_own_preset(self, world: World) -> None:
        detail = await worldbook.create_book(
            BookCreateRequest(name="自己的人设书", scope_kind="preset", preset_id=world.presets["t1a"].id),
            instance_key="t1a",
            _current_user=world.tenant,
        )
        assert detail.adapter_key == ADAPTER
        assert detail.instance_key == "t1a"
        assert detail.preset_id == world.presets["t1a"].id
        assert detail.preset_name == PRESET_T1A

    async def test_platform_global_preset_book_has_no_owner_instance(self, world: World) -> None:
        detail = await worldbook.create_book(
            BookCreateRequest(name="平台全局书", scope_kind="preset", preset_id=world.presets["global"].id),
            _current_user=world.platform,
        )
        assert detail.adapter_key == ""
        assert detail.instance_key == ""
        assert detail.preset_id == world.presets["global"].id

    async def test_platform_cannot_bind_an_instance_preset_without_selecting_the_instance(self, world: World) -> None:
        with pytest.raises(ValidationError) as exc:
            await worldbook.create_book(
                BookCreateRequest(name="私有人设书", scope_kind="preset", preset_id=world.presets["t1a"].id),
                _current_user=world.platform,
            )
        assert "请先选择实例" in str(exc.value)

    async def test_channel_book_must_use_a_visible_channel(self, world: World) -> None:
        await _expect_not_found(
            worldbook.create_book(
                BookCreateRequest(name="别人的频道书", scope_kind="channel", chat_key=CHAT_T2A),
                instance_key="t1a",
                _current_user=world.tenant,
            ),
            str(NotFoundError(resource="世界书")),
        )

    async def test_channel_book_must_match_the_selected_instance(self, world: World) -> None:
        # 频道属于同一租户的另一个实例：频道本身可见，但归属对不上所选实例，按 404 处理。
        await _expect_not_found(
            worldbook.create_book(
                BookCreateRequest(name="对不上实例的频道书", scope_kind="channel", chat_key=CHAT_T1B),
                instance_key="t1a",
                _current_user=world.tenant,
            ),
            str(NotFoundError(resource="世界书")),
        )

    async def test_platform_can_create_a_channel_book_without_selecting_an_instance(self, world: World) -> None:
        detail = await worldbook.create_book(
            BookCreateRequest(name="平台按频道建的书", scope_kind="channel", chat_key=CHAT_T2A),
            _current_user=world.platform,
        )
        assert detail.adapter_key == ADAPTER
        assert detail.instance_key == "t2a"
        assert detail.chat_key == CHAT_T2A

    async def test_scan_depth_defaults_follow_the_scope_kind(self, world: World) -> None:
        person = await worldbook.create_book(
            BookCreateRequest(name="个人书", scope_kind="person", person_account="u1main"),
            instance_key="t1a",
            _current_user=world.tenant,
        )
        assert person.scan_depth == 10

        domain = await worldbook.create_book(
            BookCreateRequest(name="域书", scope_kind="domain"),
            instance_key="t1a",
            _current_user=world.tenant,
        )
        assert domain.scan_depth == 4

        explicit = await worldbook.create_book(
            BookCreateRequest(name="指定深度的域书", scope_kind="domain", scan_depth=17),
            instance_key="t1a",
            _current_user=world.tenant,
        )
        assert explicit.scan_depth == 17

    async def test_scope_and_binding_are_not_editable(self, world: World) -> None:
        # 作用域与绑定对象不在可改字段里：改作用域等于换一本书。
        assert "scope_kind" not in BookUpdateRequest.model_fields
        assert "preset_id" not in BookUpdateRequest.model_fields
        assert "chat_key" not in BookUpdateRequest.model_fields
        assert "person_account" not in BookUpdateRequest.model_fields

        book = world.books["channel_t1a"]
        detail = await worldbook.update_book(
            book.id,
            BookUpdateRequest(name="改过名的频道书", budget_tokens=1200),
            _current_user=world.platform,
        )
        assert detail.name == "改过名的频道书"
        assert detail.budget_tokens == 1200
        assert detail.scope_kind == "channel"
        assert detail.chat_key == CHAT_T1A
        assert detail.instance_key == "t1a"

    async def test_scope_fields_of_unused_kinds_are_cleared(self, world: World) -> None:
        detail = await worldbook.create_book(
            BookCreateRequest(
                name="带脏绑定的域书",
                scope_kind="domain",
                preset_id=world.presets["t1a"].id,
                chat_key=CHAT_T1A,
                person_account="u1main",
            ),
            instance_key="t1a",
            _current_user=world.tenant,
        )
        assert detail.preset_id == 0
        assert detail.chat_key == ""
        assert detail.person_account == ""


class TestTrustLevel:
    async def test_platform_entry_is_trusted(self, world: World) -> None:
        item = await worldbook.create_entry(
            world.books["domain_t1a"].id,
            EntryCreateRequest(keys=["平台"], content="平台写的"),
            _current_user=world.platform,
        )
        assert item.trusted is True

    async def test_tenant_entry_is_untrusted_even_if_requested(self, world: World) -> None:
        item = await worldbook.create_entry(
            world.books["domain_t1a"].id,
            EntryCreateRequest(keys=["租户"], content="租户写的", trusted=True),
            _current_user=world.tenant,
        )
        assert item.trusted is False

    async def test_tenant_edit_downgrades_a_platform_entry(self, world: World) -> None:
        entry = await _entry(world.books["domain_t1a"], uid=90, keys=("甲",), content="平台条目", trusted=True)

        item = await worldbook.update_entry(
            entry.id,
            EntryUpdateRequest(content="租户改过的", trusted=True),
            _current_user=world.tenant,
        )
        assert item.content == "租户改过的"
        assert item.trusted is False

    async def test_platform_edit_keeps_or_sets_the_flag(self, world: World) -> None:
        entry = await _entry(world.books["domain_t1a"], uid=91, keys=("乙",), content="租户条目", trusted=False)

        kept = await worldbook.update_entry(
            entry.id,
            EntryUpdateRequest(comment="只改标题"),
            _current_user=world.platform,
        )
        assert kept.trusted is False

        raised = await worldbook.update_entry(
            entry.id,
            EntryUpdateRequest(trusted=True),
            _current_user=world.platform,
        )
        assert raised.trusted is True


class TestEntryValidation:
    async def test_dangerous_regex_is_rejected_with_the_key(self, world: World) -> None:
        with pytest.raises(ValidationError) as exc:
            await worldbook.create_entry(
                world.books["domain_t1a"].id,
                EntryCreateRequest(keys=["/(a+)+$/"], content="坏正则"),
                _current_user=world.platform,
            )
        assert "/(a+)+$/" in str(exc.value)
        assert "重复里不能再套重复" in str(exc.value)

    async def test_unknown_flag_is_rejected_with_the_key(self, world: World) -> None:
        with pytest.raises(ValidationError) as exc:
            await worldbook.create_entry(
                world.books["domain_t1a"].id,
                EntryCreateRequest(keys=["/abc/q"], content="坏旗标"),
                _current_user=world.platform,
            )
        assert "/abc/q" in str(exc.value)

    async def test_secondary_keys_are_validated_too(self, world: World) -> None:
        with pytest.raises(ValidationError) as exc:
            await worldbook.create_entry(
                world.books["domain_t1a"].id,
                EntryCreateRequest(keys=["好词"], secondary_keys=["/(a+)+$/"], content="次关键词坏了"),
                _current_user=world.platform,
            )
        assert "/(a+)+$/" in str(exc.value)

    async def test_too_many_keys_and_blank_content_are_422(self, world: World) -> None:
        with pytest.raises(PydanticValidationError):
            EntryCreateRequest(keys=[f"k{index}" for index in range(MAX_KEYS + 1)], content="关键词太多")
        # 上限内的几百个关键词照常可存（大世界书导入后在后台编辑同一条不能被卡住）
        assert len(EntryCreateRequest(keys=[f"k{index}" for index in range(300)], content="正文").keys) == 300

        with pytest.raises(PydanticValidationError):
            EntryCreateRequest(keys=["词"], content="   ")

    async def test_edit_clears_the_imported_invalid_key_marks(self, world: World) -> None:
        entry = await _entry(
            world.books["domain_t1a"],
            uid=92,
            keys=("/(a+)+$/",),
            content="导入时坏掉的条目",
            ext={"uid": 92, "na_invalid_keys": [{"key": "/(a+)+$/", "reason": "重复里不能再套重复"}]},
        )

        before = await worldbook.list_entries(world.books["domain_t1a"].id, _current_user=world.platform)
        item = next(row for row in before.items if row.id == entry.id)
        assert [invalid.key for invalid in item.invalid_keys] == ["/(a+)+$/"]

        updated = await worldbook.update_entry(
            entry.id,
            EntryUpdateRequest(keys=["改好的词"]),
            _current_user=world.platform,
        )
        assert updated.invalid_keys == []
        refreshed = await DBWbEntry.get(id=entry.id)
        assert "na_invalid_keys" not in (refreshed.ext or {})
        # 原始条目的其他字段仍然保留：只删附加信息，不动导入的原样内容。
        assert refreshed.ext.get("uid") == 92

    async def test_edit_calls_forget_entry(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        entry = await _entry(world.books["domain_t1a"], uid=93, keys=("旧词",), content="改之前的正文")
        forgotten: list[int] = []
        monkeypatch.setattr(safe_regex, "forget_entry", forgotten.append)

        await worldbook.update_entry(
            entry.id,
            EntryUpdateRequest(keys=["新词"]),
            _current_user=world.platform,
        )
        assert forgotten == [entry.id]

    async def test_explicit_null_clears_the_nullable_overrides(self, world: World) -> None:
        entry = await _entry(
            world.books["domain_t1a"],
            uid=95,
            keys=("覆盖",),
            content="单独设过扫描深度与定时效果",
            scan_depth=7,
            sticky=3,
            cooldown=2,
            delay=5,
        )
        await DBWbEntry.filter(id=entry.id).update(case_sensitive=True, match_whole_words=False)

        # 请求体走 JSON 解析：显式的 null 进 `model_fields_set`，与路由收到的一样。
        req = EntryUpdateRequest.model_validate_json(
            '{"scan_depth": null, "case_sensitive": null, "match_whole_words": null,'
            ' "sticky": null, "cooldown": null, "delay": null}',
        )
        await worldbook.update_entry(entry.id, req, _current_user=world.platform)

        refreshed = await DBWbEntry.get(id=entry.id)
        assert refreshed.scan_depth is None
        assert refreshed.case_sensitive is None
        assert refreshed.match_whole_words is None
        assert (refreshed.sticky, refreshed.cooldown, refreshed.delay) == (None, None, None)

    async def test_omitted_or_null_non_nullable_fields_are_left_alone(self, world: World) -> None:
        entry = await _entry(
            world.books["domain_t1a"],
            uid=96,
            keys=("不动",),
            content="原来的正文",
            scan_depth=7,
            sticky=3,
        )

        # 只改标题：没传的可空列保持原值；不可空的列传 null 也当没传。
        req = EntryUpdateRequest.model_validate_json('{"comment": "新标题", "content": null, "order": null}')
        await worldbook.update_entry(entry.id, req, _current_user=world.platform)

        refreshed = await DBWbEntry.get(id=entry.id)
        assert refreshed.comment == "新标题"
        assert refreshed.content == "原来的正文"
        assert refreshed.order == 100
        assert (refreshed.scan_depth, refreshed.sticky) == (7, 3)

    async def test_delete_entry_returns_one(self, world: World) -> None:
        entry = await _entry(world.books["domain_t1a"], uid=94, keys=("要删的",), content="待删")

        response = await worldbook.delete_entry(entry.id, _current_user=world.platform)
        assert response.deleted_entries == 1
        assert not await DBWbEntry.filter(id=entry.id).exists()


class TestEntryStatus:
    async def test_quarantine_restore_enable_and_disable(self, world: World) -> None:
        entry = world.entries["domain_key"]

        quarantined = await worldbook.entry_action(
            entry.id,
            EntryActionRequest(action="quarantine"),
            _current_user=world.tenant,
        )
        assert quarantined.status == "quarantined"
        assert quarantined.quarantine_reason == "manual"
        assert quarantined.quarantined_at is not None

        quarantine = await worldbook.list_quarantine(
            _current_user=world.tenant,
            instance_key="t1a",
            page_size=100,
        )
        row = next(item for item in quarantine.items if item.entry_id == entry.id)
        assert row.book_name == "租户一域书"
        assert row.quarantine_reason == "manual"

        # 已经是目标状态时不报错：两个管理员同时点「隔离」，后一个不该收到看不懂的错误。
        again = await worldbook.entry_action(
            entry.id,
            EntryActionRequest(action="quarantine"),
            _current_user=world.tenant,
        )
        assert again.status == "quarantined"

        await DBWbEntry.filter(id=entry.id).update(blocked_round_hits=3)
        restored = await worldbook.entry_action(
            entry.id,
            EntryActionRequest(action="restore"),
            _current_user=world.tenant,
        )
        assert restored.status == "active"
        assert restored.quarantined_at is None
        assert restored.quarantine_reason == ""
        assert restored.blocked_round_hits == 0

        after = await worldbook.list_quarantine(
            _current_user=world.tenant,
            instance_key="t1a",
            page_size=100,
        )
        assert entry.id not in {item.entry_id for item in after.items}

        disabled = await worldbook.entry_action(
            entry.id,
            EntryActionRequest(action="disable"),
            _current_user=world.tenant,
        )
        assert disabled.enabled is False

        enabled = await worldbook.entry_action(
            entry.id,
            EntryActionRequest(action="enable"),
            _current_user=world.tenant,
        )
        assert enabled.enabled is True

    async def test_editing_a_quarantined_entry_keeps_it_quarantined(self, world: World) -> None:
        entry = await _entry(world.books["domain_t1a"], uid=95, keys=("词",), content="隔离中的正文")
        await worldbook.entry_action(
            entry.id,
            EntryActionRequest(action="quarantine"),
            _current_user=world.platform,
        )

        updated = await worldbook.update_entry(
            entry.id,
            EntryUpdateRequest(content="隔离中改的正文"),
            _current_user=world.platform,
        )
        assert updated.content == "隔离中改的正文"
        assert updated.status == "quarantined"
        assert updated.quarantine_reason == "manual"

    async def test_delete_book_removes_its_entries_and_reports_the_count(self, world: World) -> None:
        book = await _book("待删的书", scope_kind="domain", adapter_key=ADAPTER, instance_key="t1a")
        entries = [
            await _entry(book, uid=index, keys=(f"词{index}",), content=f"正文{index}")
            for index in range(3)
        ]

        response = await worldbook.delete_book(book.id, _current_user=world.tenant)
        assert response.deleted_entries == 3
        assert not await DBWbBook.filter(id=book.id).exists()
        assert not await DBWbEntry.filter(id__in=[row.id for row in entries]).exists()

    async def test_entry_list_is_ordered_by_order_then_id(self, world: World) -> None:
        book = await _book("排序书", scope_kind="domain", adapter_key=ADAPTER, instance_key="t1a")
        low = await _entry(book, uid=0, keys=("低",), content="低", order=10)
        high = await _entry(book, uid=1, keys=("高",), content="高", order=90)
        same_a = await _entry(book, uid=2, keys=("同甲",), content="同甲", order=50)
        same_b = await _entry(book, uid=3, keys=("同乙",), content="同乙", order=50)

        response = await worldbook.list_entries(book.id, _current_user=world.platform)
        assert [item.id for item in response.items] == [high.id, same_a.id, same_b.id, low.id]

    async def test_entry_list_is_paged_and_searched_on_the_server(self, world: World) -> None:
        book = await _book("分页书", scope_kind="domain", adapter_key=ADAPTER, instance_key="t1a")
        rows = [
            await _entry(book, uid=index, keys=(f"词{index}",), content=f"正文{index}", order=100 - index)
            for index in range(7)
        ]

        first = await worldbook.list_entries(book.id, page=1, page_size=3, _current_user=world.platform)
        assert (first.total, first.page, first.page_size) == (7, 1, 3)
        assert [item.id for item in first.items] == [row.id for row in rows[:3]]
        last = await worldbook.list_entries(book.id, page=3, page_size=3, _current_user=world.platform)
        assert [item.id for item in last.items] == [rows[6].id]
        beyond = await worldbook.list_entries(book.id, page=9, page_size=3, _current_user=world.platform)
        assert beyond.items == [] and beyond.total == 7
        # 页大小越界会被夹住
        clamped = await worldbook.list_entries(book.id, page=0, page_size=10_000, _current_user=world.platform)
        assert (clamped.page, clamped.page_size) == (1, worldbook.ENTRY_PAGE_SIZE_MAX)
        huge = await worldbook.list_entries(book.id, page=10**30, page_size=3, _current_user=world.platform)
        assert huge.page == worldbook.ENTRY_PAGE_MAX and huge.items == [] and huge.total == 7

        by_key = await worldbook.list_entries(book.id, q="词5", _current_user=world.platform)
        assert [item.id for item in by_key.items] == [rows[5].id] and by_key.total == 1
        by_content = await worldbook.list_entries(book.id, q="正文", page_size=2, _current_user=world.platform)
        assert by_content.total == 7 and len(by_content.items) == 2
        # 搜索词里的空字符：PostgreSQL 会直接报错，这里先按参数错误拒掉
        with pytest.raises(ValidationError):
            await worldbook.list_entries(book.id, q="a" + chr(0), _current_user=world.platform)

    async def test_entry_list_marks_unanalysed_entries_pending(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        book = await _book("大模板书", scope_kind="domain", adapter_key=ADAPTER, instance_key="t1a")
        dense = "<%=1%>" * 2000
        for index in range(3):
            await _entry(book, uid=index, keys=(f"词{index}",), content=dense, order=100 - index)
        wb_purpose.clear_cache()
        monkeypatch.setattr(wb_purpose, "LIST_ANALYSIS_CHARS", len(dense))

        response = await worldbook.list_entries(book.id, _current_user=world.platform)
        assert [item.analysis_pending for item in response.items] == [False, True, True]
        # 下一次请求接着算
        response = await worldbook.list_entries(book.id, _current_user=world.platform)
        assert [item.analysis_pending for item in response.items] == [False, False, True]
        wb_purpose.clear_cache()


class TestImportExport:
    async def test_full_entry_survives_a_round_trip(self, world: World) -> None:
        original = world_of(
            [full_entry(0), full_entry(1, comment="第二条", key=["凤"], content="第二条正文")],
            originalData={"name": "导出用的书", "token_budget": 500},
        )

        report = await _import_new(world.platform, dump(original), name="往返书")
        assert report.created == 2
        assert report.notes["renumbered"] == 0

        response = await worldbook.export_book(report.book_id, _current_user=world.platform)
        assert response.headers["content-disposition"] == f'attachment; filename="worldbook-{report.book_id}.json"'
        assert json.loads(response.body) == original

    async def test_append_import_renumbers_collisions_and_keeps_the_original_uid(self, world: World) -> None:
        book = await _book("追加书", scope_kind="domain", adapter_key=ADAPTER, instance_key="t1a")
        await _entry(book, uid=0, keys=("甲",), content="已有零号")
        await _entry(book, uid=1, keys=("乙",), content="已有一号")

        # 两条不撞号的（10、11）加一条撞号的（0）：只有撞号的那条被重编。
        incoming = world_of([full_entry(0), full_entry(10, comment="不撞号甲"), full_entry(11, comment="不撞号乙")])
        report = await worldbook.import_into_book(book.id, file=_upload(dump(incoming)), _current_user=world.platform)

        assert report.created == 3
        assert report.notes["renumbered"] == 1
        renumbered = [item for item in report.details if item.kind == "renumbered"]
        assert len(renumbered) == 1
        assert "与书内已有编号冲突" in renumbered[0].detail

        rows = list(await DBWbEntry.filter(book_id=book.id).order_by("id"))
        by_comment = {row.comment: row for row in rows}
        # 撞号的条目拿到了新号，但原始条目整份留在 ext 里，导出时能还原回原号。
        assert by_comment["标题"].uid == 2
        assert by_comment["标题"].ext["uid"] == 0
        assert by_comment["不撞号甲"].uid == 10
        assert by_comment["不撞号乙"].uid == 11

    async def test_name_vars_are_rewritten_by_default_and_the_export_keeps_the_original(self, world: World) -> None:
        original = world_of([full_entry(0, content="你好，{{getvar::数据库用户名}}。")])

        report = await _import_new(world.platform, dump(original), name="换名的书")

        assert report.notes["name_var"] == 1
        rows = list(await DBWbEntry.filter(book_id=report.book_id))
        assert rows[0].content == "你好，{{user}}。"

        # 导出以原始条目为底：正文还原成导入前的写法，导入时的改写不写回文件。
        response = await worldbook.export_book(report.book_id, _current_user=world.platform)
        assert json.loads(response.body)["entries"]["0"]["content"] == "你好，{{getvar::数据库用户名}}。"

    async def test_name_vars_can_be_kept_as_written_on_new_and_append_imports(self, world: World) -> None:
        content = "你好，{{getvar::数据库用户名}}。"

        report = await _import_new(
            world.platform,
            dump(world_of([full_entry(0, content=content)])),
            name="不改名的书",
            rewrite_name_vars=False,
        )
        assert report.notes["name_var"] == 0
        rows = list(await DBWbEntry.filter(book_id=report.book_id))
        assert rows[0].content == content

        book = await _book("追加不改名", scope_kind="domain", adapter_key=ADAPTER, instance_key="t1a")
        appended = await worldbook.import_into_book(
            book.id,
            file=_upload(dump(world_of([full_entry(1, content=content)]))),
            rewrite_name_vars=False,
            _current_user=world.platform,
        )
        assert appended.notes["name_var"] == 0
        row = await DBWbEntry.get(book_id=book.id, uid=1)
        assert row.content == content

    async def test_dangerous_regex_is_imported_and_marked(self, world: World) -> None:
        incoming = world_of(
            [full_entry(0, key=["/(a+)+$/", "好词"], keysecondary=["/(b|bb)+/"], comment="坏正则条目")],
        )

        report = await _import_new(world.platform, dump(incoming), name="坏正则书")
        assert report.created == 1
        # 导入报告逐个列出无效关键词，字段名用酒馆的（主 / 次关键词各一条），普通关键词不进报告。
        assert report.notes["invalid_key"] == 2
        invalid_notes = [item for item in report.details if item.kind == "invalid_key"]
        assert [(item.entry_key, item.field) for item in invalid_notes] == [("0", "key"), ("0", "keysecondary")]
        assert invalid_notes[0].detail.startswith("/(a+)+$/：")
        assert "重复里不能再套重复" in invalid_notes[0].detail
        assert invalid_notes[1].detail.startswith("/(b|bb)+/：")

        entries = await worldbook.list_entries(report.book_id, _current_user=world.platform)
        item = entries.items[0]
        assert [invalid.key for invalid in item.invalid_keys] == ["/(a+)+$/", "/(b|bb)+/"]
        assert "重复里不能再套重复" in item.invalid_keys[0].reason
        # 导出不丢：坏关键词仍留在 keys 里，原样写回文件。
        response = await worldbook.export_book(report.book_id, _current_user=world.platform)
        exported = json.loads(response.body)
        assert exported["entries"]["0"]["key"] == ["/(a+)+$/", "好词"]
        assert exported["entries"]["0"]["keysecondary"] == ["/(b|bb)+/"]

    async def test_upload_rejections_are_validation_errors(self, world: World) -> None:
        with pytest.raises(ValidationError) as too_big:
            await _import_new(world.platform, b"x" * (st_format.IMPORT_MAX_BYTES + 1), name="超大文件")
        assert "32 MiB" in str(too_big.value)
        assert "整份未导入" in str(too_big.value)

        with pytest.raises(ValidationError):
            await _import_new(world.platform, b"not json", name="不是 JSON")

        with pytest.raises(ValidationError) as bad_entries:
            await _import_new(world.platform, dump({"entries": []}), name="条目不是对象")
        assert "entries 必须是一个对象" in str(bad_entries.value)

    async def test_entry_over_a_safety_limit_rejects_the_import_and_writes_nothing(self, world: World) -> None:
        """超了安全上限整份拒绝：报 400 并写明是哪一条，库里一本书、一条条目都不多"""
        books_before = await DBWbBook.all().count()
        entries_before = await DBWbEntry.all().count()
        incoming = world_of([full_entry(0), full_entry(3, content="甲" * (MAX_CONTENT_CHARS + 1))])
        with pytest.raises(ValidationError) as rejected:
            await _import_new(world.platform, dump(incoming), name="超限书")
        assert "第 3 号条目" in str(rejected.value)
        assert "整份未导入" in str(rejected.value)
        assert await DBWbBook.all().count() == books_before
        assert await DBWbEntry.all().count() == entries_before

    async def test_large_book_is_imported_in_full_with_a_budget_hint(
        self,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """几百条、每条上百个关键词、长正文：一字不截地进库；比每轮预算还长的条目只给提示"""
        # 百分比 100、上限 1000 token：这条五千字的正文放不下，其余短正文放得下
        monkeypatch.setattr(config, "WORLDBOOK_BUDGET_PERCENT", 100)
        monkeypatch.setattr(config, "WORLDBOOK_BUDGET_CAP_TOKENS", 1000)
        long_body = "设定" * 2500
        entries = [
            full_entry(index, comment=f"条目{index}", content=long_body if index == 7 else f"正文{index}",
                       key=[f"词{index}-{n}" for n in range(120)])
            for index in range(600)
        ]
        report = await _import_new(world.platform, dump(world_of(entries)), name="大书")

        assert report.created == 600
        assert report.notes["truncated"] == 0
        assert report.notes["over_budget"] == 1
        hint = [item for item in report.details if item.kind == "over_budget"]
        assert [(item.entry_key, item.field) for item in hint] == [("7", "content")]
        assert "每轮世界书预算 1000 token" in hint[0].detail

        row = await DBWbEntry.get(book_id=report.book_id, uid=7)
        assert row.content == long_body
        assert len(row.keys) == 120
        assert await DBWbEntry.filter(book_id=report.book_id).count() == 600

    async def test_trust_follows_the_importer(self, world: World) -> None:
        incoming = dump(world_of([full_entry(0)]))

        by_tenant = await _import_new(world.tenant, incoming, name="租户导入书", instance_key="t1a")
        by_platform = await _import_new(world.platform, incoming, name="平台导入书", instance_key="t1a")

        tenant_rows = await DBWbEntry.filter(book_id=by_tenant.book_id).all()
        platform_rows = await DBWbEntry.filter(book_id=by_platform.book_id).all()
        assert [row.trusted for row in tenant_rows] == [False]
        assert [row.trusted for row in platform_rows] == [True]

    async def test_tenant_cannot_import_into_another_tenants_book(self, world: World) -> None:
        await _expect_not_found(
            worldbook.import_into_book(
                world.books["domain_t2a"].id,
                file=_upload(dump(world_of([full_entry(0)]))),
                _current_user=world.tenant,
            ),
            str(NotFoundError(resource="世界书")),
        )
        assert await DBWbEntry.filter(book_id=world.books["domain_t2a"].id).count() == 1

    async def test_import_failure_rolls_the_whole_book_back(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        original = st_format.st_entry_to_columns

        def explode(raw: Any, **kwargs: Any) -> Any:
            calls.append("called")
            if len(calls) > 1:
                raise RuntimeError("导入中途出错")
            return original(raw, **kwargs)

        monkeypatch.setattr(st_format, "st_entry_to_columns", explode)

        before = await DBWbBook.all().count()
        with pytest.raises(RuntimeError):
            await _import_new(world.platform, dump(world_of([full_entry(0), full_entry(1)])), name="中途失败的书")

        assert await DBWbBook.all().count() == before
        assert not await DBWbBook.filter(name="中途失败的书").exists()


_ROUTING_FILLER = "这是一段与设定无关的说明文字，用来凑够长度。" * 60


class TestTemplateRoutingApi:
    """按用途路由与模板：触发测试、书详情、导入报告（样本现写，不取自真实卡片）"""

    async def test_trigger_test_lists_routed_out_hits_with_reasons(self, world: World) -> None:
        book = world.books["domain_t1a"]
        update = await _entry(book, uid=901, comment="[mvu_update]变量规则", content="写 <UpdateVariable>", constant=True)
        trap = await _entry(
            book,
            uid=902,
            comment="菜单存档",
            content=f"<% if (window.__x) {{ %>{_ROUTING_FILLER}<% }} %>",
            keys=("菜单",),
        )
        rendered = await _entry(
            book,
            uid=903,
            comment="角色",
            content="<% if (getvar('x') === undefined) { %>初次见面<% } else { %>老朋友<% } %>",
            constant=True,
        )

        response = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_T1A, text="看看菜单", preset_id=world.presets["t1a"].id),
            _current_user=world.platform,
        )

        hit_ids = {item.entry_id for item in response.hits}
        assert {update.id, trap.id, rendered.id} <= hit_ids
        reasons = {item.entry_id: item.reason for item in response.dropped}
        assert reasons[update.id] == "state_only"
        assert reasons[trap.id] == "template_unsupported"
        assert rendered.id in response.selected
        assert "初次见面" in response.rendered.before_history
        assert "<%" not in response.rendered.before_history
        assert _ROUTING_FILLER[:20] not in response.rendered.before_history

    async def test_entry_list_carries_purpose_and_trap(self, world: World) -> None:
        book = world.books["domain_t1a"]
        await _entry(book, uid=911, comment="[initvar]初值", content="{}")
        await _entry(book, uid=912, comment="排序", content=f"<% if (false) {{ %>{_ROUTING_FILLER}<% }} %>")
        await _entry(book, uid=913, comment="插图", content="<% for (const n of names) { %>x<% } %>")

        response = await worldbook.list_entries(book.id, _current_user=world.platform)

        by_uid = {item.uid: item for item in response.items}
        assert by_uid[911].purpose == "initvar"
        assert by_uid[912].purpose == "normal" and by_uid[912].suspected_trap
        assert "永远不成立" in by_uid[912].trap_reason
        assert by_uid[913].purpose == "unsupported_template" and by_uid[913].template_note
        # 普通条目没有标记。
        plain = [item for item in response.items if item.uid not in (911, 912, 913)]
        assert all(item.purpose == "normal" and not item.suspected_trap for item in plain)

    async def test_import_report_counts_template_notes_and_keeps_entries_enabled(self, world: World) -> None:
        data = json.dumps(
            {
                "entries": {
                    "0": {"uid": 0, "comment": "[mvu_update]规则", "content": "x", "key": []},
                    "1": {"uid": 1, "comment": "陷阱", "content": f"<% if (localStorage.k) {{ %>{_ROUTING_FILLER}<% }} %>", "key": ["海边"]},
                    "2": {"uid": 2, "comment": "普通", "content": "纯文字", "key": []},
                },
            },
            ensure_ascii=False,
        ).encode()

        report = await _import_new(world.platform, data)

        assert report.notes["state_only"] == 1
        assert report.notes["unsupported_template"] == 1
        assert report.notes["suspected_trap"] == 1
        kinds = {(item.entry_key, item.kind) for item in report.details}
        assert ("1", "suspected_trap") in kinds
        # 只是提示：三条都照样导入、都还是启用状态。
        rows = await DBWbEntry.filter(book_id=report.book_id).order_by("uid")
        assert [row.enabled for row in rows] == [True, True, True]


class TestTriggerTest:
    async def test_domain_and_channel_books_apply_on_their_own_channel(self, world: World) -> None:
        response = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_T1A, text="龙", preset_id=world.presets["t1a"].id),
            _current_user=world.platform,
        )

        assert {item.id for item in response.books} == {
            world.books[key].id
            for key in ("domain_t1a", "domain_t1b", "channel_t1a", "preset_t1a", "budget_t1a")
        }
        assert {item.entry_id for item in response.hits} == {
            world.entries[key].id for key in ("domain_key", "t1b_key", "domain_constant", "budget_key")
        }
        # 域书里的关键词条目因为「龙」命中，原因里带着那个词。
        key_hit = next(item for item in response.hits if item.entry_id == world.entries["domain_key"].id)
        assert key_hit.reason_kind == "key"
        assert key_hit.matched_key == "龙"
        constant_hit = next(item for item in response.hits if item.entry_id == world.entries["domain_constant"].id)
        assert constant_hit.reason_kind == "constant"
        # 书预算只有 1 token，这条 7 字正文放不下：整条跳过并记下原因与 token 数。
        assert [(item.entry_id, item.reason) for item in response.dropped] == [
            (world.entries["budget_key"].id, "book_tokens"),
        ]
        assert world.entries["budget_key"].id not in response.selected
        assert response.timed_ignored is True
        # 渲染文字来自与运行时同一个 `render_block`，令牌是固定占位串。
        assert "域书的正文" in response.rendered.before_history
        assert "SESSION_TOKEN" in response.rendered.before_history
        assert response.rendered.at_depth == {}

    async def test_keyword_and_preset_books_only_apply_to_their_own_trigger(self, world: World) -> None:
        channel_only = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_T1A, text="频道", preset_id=world.presets["t1a"].id),
            _current_user=world.platform,
        )
        assert {item.entry_id for item in channel_only.hits} == {
            world.entries["channel_key"].id,
            world.entries["domain_constant"].id,
        }

        preset_only = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_T1A, text="人设", preset_id=world.presets["t1a"].id),
            _current_user=world.platform,
        )
        assert {item.entry_id for item in preset_only.hits} == {
            world.entries["preset_key"].id,
            world.entries["domain_constant"].id,
        }

    async def test_person_book_uses_the_alias_and_ignores_the_bot(self, world: World) -> None:
        # 合成消息没有平台账号，person 书在那种窗口里永远不成立；这里用真实的最近消息，
        # 让「谁在场」这件事真的有答案。
        response = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_T1A, text="", preset_id=world.presets["t1a"].id),
            _current_user=world.platform,
        )
        book_ids = {item.id for item in response.books}
        # 窗口里的发言来自副号 `u1alt`，别名展开后命中绑主号的那本书。
        assert world.books["person_t1a"].id in book_ids
        # Bot 的发言不算「人在场」：绑 Bot 账号的那本不生效。
        assert world.books["person_bot"].id not in book_ids
        assert {item.entry_id for item in response.hits} == {
            world.entries["person_key"].id,
            world.entries["domain_constant"].id,
        }

    async def test_global_preset_book_applies_to_any_instance_using_that_preset(self, world: World) -> None:
        response = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_P1, text="全局", preset_id=world.presets["global"].id),
            _current_user=world.platform,
        )
        assert world.books["global_preset"].id in {item.id for item in response.books}
        assert {item.entry_id for item in response.hits} == {world.entries["global_key"].id}

    async def test_another_tenants_channel_is_not_visible(self, world: World) -> None:
        await _expect_not_found(
            worldbook.test_trigger(
                TriggerTestRequest(chat_key=CHAT_T2A, text="龙", preset_id=world.presets["t2a"].id),
                _current_user=world.tenant,
            ),
            str(NotFoundError(resource="世界书")),
        )

    async def test_other_tenants_domain_books_do_not_leak_into_this_channel(self, world: World) -> None:
        response = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_T2A, text="龙", preset_id=world.presets["t2a"].id),
            _current_user=world.tenant2,
        )
        assert {item.id for item in response.books} == {world.books["domain_t2a"].id}
        assert {item.entry_id for item in response.hits} == {world.entries["t2a_key"].id}

    async def test_empty_window_leaves_only_the_constant_entries(self, world: World) -> None:
        response = await worldbook.test_trigger(
            TriggerTestRequest(
                chat_key=CHAT_T1A,
                text="",
                use_recent=False,
                preset_id=world.presets["t1a"].id,
            ),
            _current_user=world.platform,
        )
        assert response.window_size == 0
        assert {item.entry_id for item in response.hits} == {world.entries["domain_constant"].id}
        assert response.groups.after_persona == [world.entries["domain_constant"].id]

    async def test_recent_messages_are_read_in_time_order(self, world: World) -> None:
        # 不带 `text` 时窗口取这个频道最近的消息，`window_size` 就是真实消息条数。
        response = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_T1A, text="", preset_id=world.presets["t1a"].id),
            _current_user=world.platform,
        )
        assert response.window_size == 4
        assert {item.entry_id for item in response.hits} == {
            world.entries["domain_constant"].id,
            world.entries["person_key"].id,
        }
        # 扫描深度 1 条的那条关键词只在最早那条消息里：最后一条不是它，所以不命中。
        # 反过来，合成消息排在窗口最前时它必须命中——两种写法都要钉住，否则「最后 N 条」
        # 被实现成「最早 N 条」时，这两条断言里总有一条会绿。
        assert world.entries["depth_one"].id not in {item.entry_id for item in response.hits}

        synthetic = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_T1A, text="今天天气不错", preset_id=world.presets["t1a"].id),
            _current_user=world.platform,
        )
        assert world.entries["depth_one"].id in {item.entry_id for item in synthetic.hits}

    async def test_rendered_groups_follow_the_injection_positions(self, world: World) -> None:
        await _entry(
            world.books["domain_t1a"],
            uid=20,
            constant=True,
            content="深度块正文",
            position="at_depth",
            depth=2,
        )
        response = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_T1A, text="", use_recent=False, preset_id=world.presets["t1a"].id),
            _current_user=world.platform,
        )

        # `after_persona` 是常驻条目所在的组：它在系统提示词里，包法与聊天记录里的不同。
        assert "常驻正文" in response.rendered.after_persona
        assert response.rendered.after_persona.startswith("## World Info")
        assert "深度块正文" in response.rendered.at_depth["2"]
        # 没有条目的位置是空串，前端可以无条件渲染三段。
        assert response.rendered.before_history == ""

    async def test_gate_off_still_reports_results_and_says_so(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        # 闸关时结果照算：管理员常常是先看效果再决定要不要打开，清空结果会让他以为书配错了。
        monkeypatch.setattr(config, "WORLDBOOK_ENABLED", False)
        world.channels["t1a"].invalidate_effective_config()

        response = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_T1A, text="龙", preset_id=world.presets["t1a"].id),
            _current_user=world.platform,
        )

        assert response.gate_enabled is False
        assert world.entries["domain_key"].id in {item.entry_id for item in response.hits}

    async def test_gate_on_is_reported(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "WORLDBOOK_ENABLED", True)
        world.channels["t1a"].invalidate_effective_config()

        response = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_T1A, text="龙", preset_id=world.presets["t1a"].id),
            _current_user=world.platform,
        )

        assert response.gate_enabled is True

    async def test_budget_comes_from_the_effective_channel_config(
        self,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 生效配置的 token 上限压到 1：正文都放不下，于是全部进 dropped。
        # 用「读不到配置就用全局默认值」的实现时这里会仍然选中它们。
        monkeypatch.setattr(config, "WORLDBOOK_BUDGET_CAP_TOKENS", 1)
        world.channels["t1a"].invalidate_effective_config()

        response = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_T1A, text="龙", preset_id=world.presets["t1a"].id),
            _current_user=world.platform,
        )

        assert response.selected == []
        assert {item.reason for item in response.dropped} == {"global_tokens"}
        assert response.budget is not None and response.budget.budget == 1 and response.budget.cap_tokens == 1

    async def test_platform_global_book_bodies_are_shown_to_tenants(self, world: World) -> None:
        # 平台方给全局人设建的书对租户只读开放：触发测试里正文照常显示，不再换成占位。
        response = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_T1A, text="全局", preset_id=world.presets["global"].id),
            _current_user=world.tenant,
        )

        assert world.books["global_preset"].id in {item.id for item in response.books}
        assert "全局正文" in response.rendered.before_history
        assert "[这条设定来自平台方，正文不显示]" not in response.rendered.before_history

    async def test_invisible_bodies_are_hidden_in_every_position(
        self,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 三个位置各自渲染：只替换了一个位置的话，另外两段会把看不到的书的正文带给租户。
        # 正常数据里租户能命中、却看不到的书已经很难造出来（全局人设书改成只读可见了），
        # 这里直接让判据把全局人设书判成不可见，钉住遮挡这一层本身。
        await _entry(world.books["global_preset"], uid=1, constant=True, content="全局人设后正文", position="after_persona")
        await _entry(
            world.books["global_preset"],
            uid=2,
            constant=True,
            content="全局深度正文",
            position="at_depth",
            depth=1,
        )
        hidden_id = world.books["global_preset"].id

        async def _fake_invisible(_user: DBUser, books: Any) -> set[int]:
            return {book.id for book in books if book.id == hidden_id}

        monkeypatch.setattr(wb_service, "books_invisible_to", _fake_invisible)

        response = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_T1A, text="", use_recent=False, preset_id=world.presets["global"].id),
            _current_user=world.tenant,
        )

        hidden = "[这条设定来自平台方，正文不显示]"
        assert "全局人设后正文" not in response.rendered.after_persona
        assert hidden in response.rendered.after_persona
        assert "全局深度正文" not in response.rendered.at_depth["1"]
        assert hidden in response.rendered.at_depth["1"]
        # 租户自己的书照常显示正文：只替换看不到的那几本。
        assert "常驻正文" in response.rendered.after_persona

    async def test_host_group_in_the_system_prompt_is_not_wrapped_like_history(self, world: World) -> None:
        # `after_persona` 在系统提示词里：宿主组直接接标题。按聊天记录的包法渲染时，测试页
        # 看到的分隔符会与正式提示词不同。
        await _entry(
            world.books["domain_t1a"],
            uid=21,
            constant=True,
            content="宿主常驻正文",
            position="after_persona",
            trusted=True,
        )

        response = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_T1A, text="", use_recent=False, preset_id=world.presets["t1a"].id),
            _current_user=world.platform,
        )

        assert response.rendered.after_persona.startswith(render.HOST_HEADER)
        assert "WorldInfo source:host" not in response.rendered.after_persona
        assert "宿主常驻正文" in response.rendered.after_persona

    async def test_token_budget_is_the_channels_not_the_global_one(
        self,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 全局仍是默认值，只有这个频道的生效配置把 token 上限压到 1：读全局配置的实现会照样选中它们。
        original = DBChatChannel.get_effective_config

        async def effective(self: DBChatChannel) -> Any:
            return (await original(self)).model_copy(update={"WORLDBOOK_BUDGET_CAP_TOKENS": 1})

        monkeypatch.setattr(DBChatChannel, "get_effective_config", effective)

        response = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_T1A, text="龙", preset_id=world.presets["t1a"].id),
            _current_user=world.platform,
        )

        assert response.selected == []
        assert {item.reason for item in response.dropped} == {"global_tokens"}

    async def test_entry_limit_comes_from_the_global_config(
        self,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "WORLDBOOK_MAX_ENTRIES_PER_ROUND", 1)
        world.channels["t1a"].invalidate_effective_config()

        response = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_T1A, text="龙", preset_id=world.presets["t1a"].id),
            _current_user=world.platform,
        )

        assert len(response.selected) == 1
        assert "max_entries" in {item.reason for item in response.dropped}


class TestPersonNotes:
    """记一笔：建书 / 追加、信任级别、范围与长度

    这一组用例只调路由函数，跑在真表上：它要钉住的是「写进了哪本书」与「谁能写」，
    而这两件事都靠查询条件，不靠匹配引擎。
    """

    async def test_first_note_creates_a_person_book(self, world: World) -> None:
        response = await worldbook.create_person_note(
            PersonNoteCreateRequest(instance_key="t1a", person_account="newbie", content="怕打雷"),
            _current_user=world.platform,
        )

        assert response.created_book is True
        book = await DBWbBook.get(id=response.book_id)
        assert book.scope_kind == "person"
        assert (book.adapter_key, book.instance_key) == (ADAPTER, "t1a")
        assert book.person_account == "newbie"
        assert book.name == "关于 newbie"
        entry = await DBWbEntry.get(id=response.entry_id)
        assert (entry.constant, entry.position, entry.order) == (True, "before_history", 100)
        assert entry.content == "怕打雷"
        assert entry.comment == "怕打雷"

    async def test_second_note_appends_to_the_same_book(self, world: World) -> None:
        first = await worldbook.create_person_note(
            PersonNoteCreateRequest(instance_key="t1a", person_account="newbie", content="怕打雷"),
            _current_user=world.platform,
        )
        second = await worldbook.create_person_note(
            PersonNoteCreateRequest(instance_key="t1a", person_account="newbie", content="养了一只猫"),
            _current_user=world.platform,
        )

        assert second.book_id == first.book_id
        assert second.created_book is False
        assert second.entry_id != first.entry_id
        # 编号接在书内最大值之后，不会与已有条目撞唯一索引。
        assert (await DBWbEntry.get(id=second.entry_id)).uid == 1

    async def test_book_name_uses_the_nickname_from_this_instance(self, world: World) -> None:
        # 昵称取这个实例下的最近一条消息；同名的另一条消息落在别的实例里，不该被取到。
        await _message(
            CHAT_T1A,
            instance_key="t1a",
            account="u-nick",
            sender_id="u-nick",
            content="你好",
            message_id="m-nick",
        )
        await DBChatMessage.filter(message_id="m-nick").update(sender_nickname="小明", sender_name="小明本名")
        # 更晚的一条落在同域的另一个实例里、昵称不同：不按实例限定时会取到它。
        await _message(
            CHAT_T1B,
            instance_key="t1b",
            account="u-nick",
            sender_id="u-nick",
            content="别处的发言",
            message_id="m-nick-elsewhere",
        )
        await DBChatMessage.filter(message_id="m-nick-elsewhere").update(sender_nickname="别处的名字")

        response = await worldbook.create_person_note(
            PersonNoteCreateRequest(instance_key="t1a", person_account="u-nick", content="记一句"),
            _current_user=world.platform,
        )

        assert (await DBWbBook.get(id=response.book_id)).name == "关于 小明"

    async def test_long_nickname_is_truncated_to_the_book_name_limit(self, world: World) -> None:
        # 昵称来自群名片，长度不受控。不截断的话这个人的「记一笔」会一直 422——而问题
        # 出在他改了个长名字，管理员无从下手。
        await _message(
            CHAT_T1A,
            instance_key="t1a",
            account="u-long",
            sender_id="u-long",
            content="你好",
            message_id="m-long",
        )
        # 昵称列本身限长，但「关于 」前缀会让书名超出一个字：正好钉住截断这一步。
        await DBChatMessage.filter(message_id="m-long").update(sender_nickname="名" * MAX_NAME_CHARS)

        response = await worldbook.create_person_note(
            PersonNoteCreateRequest(instance_key="t1a", person_account="u-long", content="记一句"),
            _current_user=world.platform,
        )

        assert len((await DBWbBook.get(id=response.book_id)).name) == MAX_NAME_CHARS

    async def test_tenant_note_is_untrusted_and_platform_note_is_trusted(self, world: World) -> None:
        tenant_note = await worldbook.create_person_note(
            PersonNoteCreateRequest(instance_key="t1a", person_account="u1main", content="租户写的"),
            _current_user=world.tenant,
        )
        platform_note = await worldbook.create_person_note(
            PersonNoteCreateRequest(instance_key="p1", person_account="u1main", content="平台方写的"),
            _current_user=world.platform,
        )

        assert (await DBWbEntry.get(id=tenant_note.entry_id)).trusted is False
        assert (await DBWbEntry.get(id=platform_note.entry_id)).trusted is True

    async def test_without_instance_is_a_validation_error(self, world: World) -> None:
        # 平台方不选实例时范围是全量，但个人书没有空归属形态：写下去就是一本永不生效的书。
        with pytest.raises(ValidationError) as exc:
            await worldbook.create_person_note(
                PersonNoteCreateRequest(instance_key="", person_account="u1", content="没有实例"),
                _current_user=world.platform,
            )
        assert "请先选择实例" in str(exc.value)
        assert not await DBWbBook.filter(person_account="u1").exists()

    async def test_another_tenants_instance_is_not_found(self, world: World) -> None:
        await _expect_not_found(
            worldbook.create_person_note(
                PersonNoteCreateRequest(instance_key="t2a", person_account="u1", content="越权的"),
                _current_user=world.tenant,
            ),
            str(NotFoundError(resource="实例 't2a'")),
        )
        assert not await DBWbBook.filter(person_account="u1").exists()

    async def test_writes_only_into_this_instances_book(self, world: World) -> None:
        # 同一租户的另一个实例也给这个人建了书：平台方的全量范围两本都看得见，
        # 但这一次记一笔只能落进请求选中的那个实例的那一本。
        other = await _book(
            "另一个实例的个人书",
            scope_kind="person",
            adapter_key=ADAPTER,
            instance_key="t1b",
            person_account="shared-person",
        )
        await _entry(other, uid=0, constant=True, content="另一个群里的备注")

        response = await worldbook.create_person_note(
            PersonNoteCreateRequest(instance_key="t1a", person_account="shared-person", content="这个群里的备注"),
            _current_user=world.platform,
        )

        assert response.book_id != other.id
        assert (await DBWbBook.get(id=response.book_id)).instance_key == "t1a"
        # 另一本一个字都没变。
        assert await DBWbEntry.filter(book_id=other.id).count() == 1

    async def test_blank_content_is_rejected_by_the_model(self) -> None:
        with pytest.raises(PydanticValidationError):
            PersonNoteCreateRequest(instance_key="t1a", person_account="u1", content="   ")

    async def test_content_over_the_limit_is_rejected_by_the_model(self) -> None:
        with pytest.raises(PydanticValidationError):
            PersonNoteCreateRequest(
                instance_key="t1a",
                person_account="u1",
                content="字" * (PERSON_NOTE_MAX_CHARS + 1),
            )

    async def test_blank_person_is_rejected_by_the_model(self) -> None:
        with pytest.raises(PydanticValidationError):
            PersonNoteCreateRequest(instance_key="t1a", person_account="  ", content="内容")

    async def test_surrounding_whitespace_does_not_count_towards_the_limit(self) -> None:
        # 上限按去空白后的字数算：500 字正文后面跟两个空格是合法的，原样收下会把一个
        # 「正好写满」的备注判成超长。
        request = PersonNoteCreateRequest(
            instance_key="t1a",
            person_account=" u1 ",
            content="  " + "字" * PERSON_NOTE_MAX_CHARS + "  ",
        )

        assert request.person_account == "u1"
        assert len(request.content) == PERSON_NOTE_MAX_CHARS

    async def test_list_returns_every_entry_including_disabled_and_quarantined(self, world: World) -> None:
        book = await _book(
            "关于 u-lister",
            scope_kind="person",
            adapter_key=ADAPTER,
            instance_key="t1a",
            person_account="u-lister",
        )
        active = await _entry(book, uid=0, constant=True, content="正常的一条")
        disabled = await _entry(book, uid=1, constant=True, content="停用的一条", enabled=False)
        quarantined = await _entry(book, uid=2, constant=True, content="隔离的一条", status="quarantined")

        response = await worldbook.list_person_notes(
            instance_key="t1a",
            person_account="u-lister",
            _current_user=world.platform,
        )

        assert {item.entry_id for item in response.items} == {active.id, disabled.id, quarantined.id}
        by_id = {item.entry_id: item for item in response.items}
        assert by_id[disabled.id].enabled is False
        assert by_id[quarantined.id].status == "quarantined"
        assert by_id[active.id].book_name == "关于 u-lister"

    async def test_list_does_not_leak_another_instances_notes(self, world: World) -> None:
        other = await _book(
            "别的实例的个人书",
            scope_kind="person",
            adapter_key=ADAPTER,
            instance_key="t2a",
            person_account="u-leak",
        )
        await _entry(other, uid=0, constant=True, content="不该读到的一条")

        response = await worldbook.list_person_notes(
            instance_key="t1a",
            person_account="u-leak",
            _current_user=world.tenant,
        )

        assert response.items == []

    async def test_list_without_person_is_a_validation_error(self, world: World) -> None:
        with pytest.raises(ValidationError) as exc:
            await worldbook.list_person_notes(instance_key="t1a", _current_user=world.platform)
        assert "必须指定一个人" in str(exc.value)

    async def test_list_without_instance_is_a_validation_error(self, world: World) -> None:
        with pytest.raises(ValidationError) as exc:
            await worldbook.list_person_notes(person_account="u1main", _current_user=world.platform)
        assert "请先选择实例" in str(exc.value)

    async def test_list_is_ordered_by_weight_then_creation(self, world: World) -> None:
        # 记一笔写出来的都是同一个权重，但条目在世界书页里可以改权重：列表与注入同一个次序。
        book = await _book(
            "关于 u-order",
            scope_kind="person",
            adapter_key=ADAPTER,
            instance_key="t1a",
            person_account="u-order",
        )
        low = await _entry(book, uid=0, constant=True, content="低权重", order=10)
        high = await _entry(book, uid=1, constant=True, content="高权重", order=200)
        later = await _entry(book, uid=2, constant=True, content="同权重后建", order=10)

        response = await worldbook.list_person_notes(
            instance_key="t1a",
            person_account="u-order",
            _current_user=world.platform,
        )

        assert [item.entry_id for item in response.items] == [high.id, low.id, later.id]

    async def test_failed_entry_leaves_no_empty_book(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        # 建书与写条目同一个事务：条目写失败时不该留下一本空书让管理员手工去删。
        async def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("写条目失败")

        monkeypatch.setattr(wb_service, "create_entry", boom)

        with pytest.raises(RuntimeError):
            await worldbook.create_person_note(
                PersonNoteCreateRequest(instance_key="t1a", person_account="u-rollback", content="会失败"),
                _current_user=world.platform,
            )

        assert not await DBWbBook.filter(person_account="u-rollback").exists()

    async def test_concurrent_first_notes_share_one_book(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        # 两位管理员同时给同一个人记第一笔：串行之后只有一本个人书，两条条目的编号不撞。
        original_find = wb_service.find_person_books

        async def slow_find(*args: Any, **kwargs: Any) -> Any:
            books = await original_find(*args, **kwargs)
            # 撑开「找完书、还没建书」这段窗口，让另一个请求也走到找书这一步。
            await asyncio.sleep(0.05)
            return books

        monkeypatch.setattr(wb_service, "find_person_books", slow_find)

        first, second = await asyncio.wait_for(
            asyncio.gather(
                worldbook.create_person_note(
                    PersonNoteCreateRequest(instance_key="t1a", person_account="u-race", content="第一笔"),
                    _current_user=world.platform,
                ),
                worldbook.create_person_note(
                    PersonNoteCreateRequest(instance_key="t1a", person_account="u-race", content="第二笔"),
                    _current_user=world.tenant,
                ),
            ),
            timeout=30,
        )

        assert first.book_id == second.book_id
        assert [first.created_book, second.created_book].count(True) == 1
        assert await DBWbBook.filter(person_account="u-race").count() == 1
        uids = sorted(await DBWbEntry.filter(book_id=first.book_id).values_list("uid", flat=True))
        assert uids == [0, 1]

    async def test_find_person_books_keeps_only_this_instances_person_books(self, world: World) -> None:
        # 范围比一个实例宽时（这里直接给两个实例对），服务层仍只留归属正是这个实例、类型正是
        # 个人书的书；坏数据里带着人的域书也不算。
        mine = await _book(
            "t1a 的个人书",
            scope_kind="person",
            adapter_key=ADAPTER,
            instance_key="t1a",
            person_account="u-find",
        )
        await _book(
            "t1b 的个人书",
            scope_kind="person",
            adapter_key=ADAPTER,
            instance_key="t1b",
            person_account="u-find",
        )
        await _book("带人的域书", scope_kind="domain", adapter_key=ADAPTER, instance_key="t1a", person_account="u-find")
        scope = AdminMemoryScope.for_pairs([(ADAPTER, "t1a"), (ADAPTER, "t1b")])

        books = await wb_service.find_person_books(scope, (ADAPTER, "t1a"), "u-find")

        assert [book.id for book in books] == [mine.id]


class TestReviewChanges:
    async def test_import_form_errors_are_validation_errors(self, world: World) -> None:
        data = dump(world_of([full_entry(0)]))

        with pytest.raises(ValidationError) as blank_name:
            await _import_new(world.platform, data, name="   ", instance_key="t1a")
        assert "书名" in str(blank_name.value)

        with pytest.raises(ValidationError) as bad_kind:
            await _import_new(world.platform, data, name="坏作用域", scope_kind="nope", instance_key="t1a")
        assert "scope_kind" in str(bad_kind.value)

        # 失败时不该留下半本书。
        assert not await DBWbBook.filter(name="坏作用域").exists()

    async def test_channel_book_without_a_channel_is_a_validation_error(self, world: World) -> None:
        with pytest.raises(ValidationError) as exc:
            await worldbook.create_book(
                BookCreateRequest(name="没选频道的书", scope_kind="channel", chat_key=""),
                instance_key="t1a",
                _current_user=world.platform,
            )
        assert "频道书必须指定频道" in str(exc.value)

    async def test_trigger_with_another_instances_preset_is_not_found(self, world: World) -> None:
        await _expect_not_found(
            worldbook.test_trigger(
                TriggerTestRequest(chat_key=CHAT_T1A, text="龙", preset_id=world.presets["t2a"].id),
                _current_user=world.tenant,
            ),
            str(NotFoundError(resource="人设")),
        )

    async def test_trigger_with_the_global_preset_sees_the_global_book(self, world: World) -> None:
        response = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_T1A, text="全局", preset_id=world.presets["global"].id),
            _current_user=world.tenant,
        )
        assert world.books["global_preset"].id in {item.id for item in response.books}
        assert world.entries["global_key"].id in {item.entry_id for item in response.hits}
        # 频道自己的私有人设书换成人设后就不该再生效。
        assert world.books["preset_t1a"].id not in {item.id for item in response.books}

    async def test_trigger_with_preset_zero_disables_every_preset_book(self, world: World) -> None:
        response = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_T1A, text="人设", preset_id=0),
            _current_user=world.tenant,
        )
        book_ids = {item.id for item in response.books}
        assert world.books["preset_t1a"].id not in book_ids
        assert world.books["global_preset"].id not in book_ids
        assert world.entries["preset_key"].id not in {item.entry_id for item in response.hits}

    async def test_deleting_a_book_forgets_the_regex_state_of_its_entries(
        self,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        book = await _book("有熔断记录的书", scope_kind="domain", adapter_key=ADAPTER, instance_key="t1a")
        entries = [
            await _entry(book, uid=index, keys=(f"词{index}",), content=f"正文{index}")
            for index in range(3)
        ]
        forgotten: list[int] = []
        monkeypatch.setattr(safe_regex, "forget_entry", forgotten.append)

        response = await worldbook.delete_book(book.id, _current_user=world.tenant)

        assert response.deleted_entries == 3
        assert set(forgotten) == {row.id for row in entries}

    async def test_trigger_text_over_the_limit_is_rejected_by_the_model(self) -> None:
        with pytest.raises(PydanticValidationError):
            TriggerTestRequest(chat_key="x", text="a" * (MAX_TRIGGER_TEXT_CHARS + 1))
        # 测试文字的上限是它自己的，比条目正文的安全上限小得多
        assert MAX_TRIGGER_TEXT_CHARS < MAX_CONTENT_CHARS

    async def test_missing_uid_is_reported_as_renumbered(self, world: World) -> None:
        entry = full_entry(5)
        data = world_of([entry])
        del data["entries"]["5"]["uid"]

        report = await _import_new(world.platform, dump(data), name="缺编号的书")

        assert report.created == 1
        assert report.notes["renumbered"] == 1
        detail = next(item for item in report.details if item.kind == "renumbered")
        assert detail.detail.startswith("原编号缺失")
        rows = await DBWbEntry.filter(book_id=report.book_id).all()
        assert [row.uid for row in rows] == [0]


class TestGuardEdges:
    """变异检查时漏抓的几处判据，各补一条只钉它的用例"""

    async def _duplicate_channel_row(self) -> DBChatChannel:
        # 同一个 chat_key 在另一个租户的实例下又有一行（唯一约束是适配器、实例、频道 ID
        # 三元组，库里允许这样）。守卫必须对每一行都判归属，不能只看第一行。
        return await _channel(CHAT_T1A, instance_key="t2a", channel_id="group_1", name="撞标识的群", preset_id=0)

    async def test_channel_with_a_foreign_duplicate_row_is_not_visible(self, world: World) -> None:
        await self._duplicate_channel_row()
        await _expect_not_found(
            worldbook.test_trigger(
                TriggerTestRequest(chat_key=CHAT_T1A, text="龙", preset_id=world.presets["t1a"].id),
                _current_user=world.tenant,
            ),
            str(NotFoundError(resource="世界书")),
        )

    async def test_channel_book_on_a_key_spanning_two_instances_is_rejected(self, world: World) -> None:
        await self._duplicate_channel_row()
        with pytest.raises(ValidationError) as exc:
            await worldbook.create_book(
                BookCreateRequest(name="归属不明的频道书", scope_kind="channel", chat_key=CHAT_T1A),
                _current_user=world.platform,
            )
        assert "多个实例" in str(exc.value)
        assert not await DBWbBook.filter(name="归属不明的频道书").exists()

    async def test_trigger_respects_delay_by_window_size(self, world: World) -> None:
        book = world.books["domain_t1a"]
        late = await _entry(book, uid=10, keys=("龙",), content="消息够多才出场", delay=2)
        early = await _entry(book, uid=11, keys=("龙",), content="一条就够", delay=1)

        response = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_T1A, text="龙", preset_id=world.presets["t1a"].id),
            _current_user=world.platform,
        )

        hit_ids = {item.entry_id for item in response.hits}
        # 合成文字的窗口只有 1 条：delay 2 不满足，不算命中，也不进 dropped。
        assert response.window_size == 1
        assert late.id not in hit_ids
        assert late.id not in {item.entry_id for item in response.dropped}
        assert early.id in hit_ids

    async def test_person_book_honours_its_own_scan_depth(self, world: World) -> None:
        # 副号在窗口里说过话（在场账号里有它），但最后一条换成了别人：扫描深度只有 1 条
        # 的个人书必须按「最后 1 条里有没有他」判，而不是按整个窗口。
        shallow = await _book(
            "只看最后一条的个人书",
            scope_kind="person",
            adapter_key=ADAPTER,
            instance_key="t1a",
            person_account="u1main",
            scan_depth=1,
        )
        await _entry(shallow, uid=0, constant=True, content="浅个人书")
        await _message(CHAT_T1A, instance_key="t1a", account="u3", sender_id="u3", content="插一句", message_id="m-t1a-5")

        response = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_T1A, text="", preset_id=world.presets["t1a"].id),
            _current_user=world.platform,
        )

        book_ids = {item.id for item in response.books}
        assert world.books["person_t1a"].id in book_ids
        assert shallow.id not in book_ids

    async def test_turning_regex_on_validates_existing_keys_as_regex(self, world: World) -> None:
        created = await worldbook.create_entry(
            world.books["domain_t1a"].id,
            EntryCreateRequest(keys=["(a+)+"], content="普通关键词里带括号"),
            _current_user=world.platform,
        )
        # 作为普通关键词它是合法的；同一次请求把 use_regex 打开，就得按正则口径重新校验。
        with pytest.raises(ValidationError) as exc:
            await worldbook.update_entry(
                created.id,
                EntryUpdateRequest(use_regex=True),
                _current_user=world.platform,
            )
        assert "(a+)+" in str(exc.value)
        row = await DBWbEntry.get(id=created.id)
        assert row.use_regex is False

    async def test_append_import_failure_rolls_back(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        book = world.books["domain_t1a"]
        before = await DBWbEntry.filter(book_id=book.id).count()
        calls: list[int] = []
        original = st_format.st_entry_to_columns

        def explode(raw: Any, **kwargs: Any) -> Any:
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError("追加导入中途出错")
            return original(raw, **kwargs)

        monkeypatch.setattr(st_format, "st_entry_to_columns", explode)

        with pytest.raises(RuntimeError):
            await worldbook.import_into_book(
                book.id,
                file=_upload(dump(world_of([full_entry(20), full_entry(21)]))),
                _current_user=world.platform,
            )

        assert len(calls) == 2
        assert await DBWbEntry.filter(book_id=book.id).count() == before

    async def test_missing_bindings_are_flagged(self, world: World) -> None:
        orphan_channel = await _book(
            "频道已删的书",
            scope_kind="channel",
            adapter_key=ADAPTER,
            instance_key="t1a",
            chat_key=f"{ADAPTER}-t1a-group_9",
        )
        await DBPreset.filter(id=world.presets["t1a"].id).delete()

        preset_book = await worldbook.get_book(world.books["preset_t1a"].id, _current_user=world.platform)
        assert preset_book.binding_missing is True
        assert preset_book.preset_name == ""

        channel_book = await worldbook.get_book(orphan_channel.id, _current_user=world.platform)
        assert channel_book.binding_missing is True

        live_channel_book = await worldbook.get_book(world.books["channel_t1a"].id, _current_user=world.platform)
        assert live_channel_book.binding_missing is False
        assert live_channel_book.channel_name == "租户一甲群"


class TestPlatformGlobalBookReadOnly:
    """平台方给全局人设建的书：人设对租户的实例可见时，租户只读可见"""

    async def test_tenant_can_read_the_book_and_its_entries(self, world: World) -> None:
        book_id = world.books["global_preset"].id
        detail = await worldbook.get_book(book_id, _current_user=world.tenant)
        assert detail.read_only is True
        assert detail.name == "全局人设书"

        entries = await worldbook.list_entries(book_id, _current_user=world.tenant)
        assert [(item.id, item.content) for item in entries.items] == [(world.entries["global_key"].id, "全局正文")]

        # 平台方看同一本书不是只读
        platform_detail = await worldbook.get_book(book_id, _current_user=world.platform)
        assert platform_detail.read_only is False
        platform_list = await worldbook.list_books(_current_user=world.platform, page_size=100)
        assert not any(item.read_only for item in platform_list.items)

    async def test_every_write_is_refused_and_nothing_changes(self, world: World) -> None:
        book = world.books["global_preset"]
        entry = world.entries["global_key"]
        book_before = _snapshot(await DBWbBook.get(id=book.id))
        entries_before = {row.id: _snapshot(row) for row in await DBWbEntry.filter(book_id=book.id)}

        refused = [
            worldbook.update_book(book.id, BookUpdateRequest(name="租户改名"), _current_user=world.tenant),
            worldbook.delete_book(book.id, _current_user=world.tenant),
            worldbook.export_book(book.id, _current_user=world.tenant),
            worldbook.import_into_book(book.id, file=_upload(dump(world_of([full_entry(5)]))), _current_user=world.tenant),
            worldbook.create_entry(book.id, EntryCreateRequest(keys=["新词"], content="租户加的"), _current_user=world.tenant),
            worldbook.update_entry(entry.id, EntryUpdateRequest(content="租户改的"), _current_user=world.tenant),
            worldbook.delete_entry(entry.id, _current_user=world.tenant),
            worldbook.entry_action(entry.id, EntryActionRequest(action="disable"), _current_user=world.tenant),
            worldbook.entry_action(entry.id, EntryActionRequest(action="quarantine"), _current_user=world.tenant),
        ]
        for call in refused:
            with pytest.raises(PermissionDeniedError) as denied:
                await call
            assert "只读" in str(denied.value.detail)

        assert _snapshot(await DBWbBook.get(id=book.id)) == book_before
        assert {row.id: _snapshot(row) for row in await DBWbEntry.filter(book_id=book.id)} == entries_before

    async def test_book_on_a_preset_the_tenant_cannot_see_stays_invisible(self, world: World) -> None:
        """归属为空、但绑的是别的租户实例的私有人设：照旧看不到，写也是 404 而不是 403"""
        hidden = await _book(
            "别人人设上的平台书",
            scope_kind="preset",
            adapter_key="",
            instance_key="",
            preset_id=world.presets["t2a"].id,
        )
        hidden_entry = await _entry(hidden, uid=0, keys=("秘密",), content="看不到的正文")

        listed = await worldbook.list_books(_current_user=world.tenant, instance_key="t1a", page_size=100)
        assert hidden.id not in _book_ids(listed)
        with pytest.raises(NotFoundError):
            await worldbook.get_book(hidden.id, _current_user=world.tenant)
        with pytest.raises(NotFoundError):
            await worldbook.list_entries(hidden.id, _current_user=world.tenant)
        with pytest.raises(NotFoundError):
            await worldbook.update_book(hidden.id, BookUpdateRequest(name="改名"), _current_user=world.tenant)
        with pytest.raises(NotFoundError):
            await worldbook.delete_entry(hidden_entry.id, _current_user=world.tenant)
        assert await wb_service.books_invisible_to(world.tenant, [hidden]) == {hidden.id}
        assert await wb_service.books_invisible_to(world.tenant, [world.books["global_preset"]]) == set()

        # 人设对他的实例不可见，触发测试里也照旧不能指定
        with pytest.raises(NotFoundError):
            await worldbook.test_trigger(
                TriggerTestRequest(chat_key=CHAT_T1A, text="秘密", preset_id=world.presets["t2a"].id),
                _current_user=world.tenant,
            )

    async def test_global_preset_moved_to_another_instance_is_no_longer_readable(self, world: World) -> None:
        """可见性跟着人设现在的归属走：人设改成别人实例私有后，这本书对租户一就不可见了"""
        await DBPreset.filter(id=world.presets["global"].id).update(owner_instance_key="t2a")
        with pytest.raises(NotFoundError):
            await worldbook.get_book(world.books["global_preset"].id, _current_user=world.tenant)
        listed = await worldbook.list_books(_current_user=world.tenant, instance_key="t1a", page_size=100)
        assert world.books["global_preset"].id not in _book_ids(listed)

    async def test_other_tenants_books_stay_invisible(self, world: World) -> None:
        listed = await worldbook.list_books(_current_user=world.tenant2, instance_key="t2a", page_size=100)
        ids = _book_ids(listed)
        # 租户二看得到自己的书和平台方的全局人设书（只读），看不到租户一的书
        assert world.books["domain_t2a"].id in ids
        assert world.books["global_preset"].id in ids
        assert not ids & {world.books[key].id for key in ("domain_t1a", "preset_t1a", "domain_t1b", "domain_p1")}
        with pytest.raises(NotFoundError):
            await worldbook.get_book(world.books["domain_t1a"].id, _current_user=world.tenant2)

    async def test_channel_and_person_filters_do_not_pull_in_the_global_book(self, world: World) -> None:
        by_channel = await worldbook.list_books(
            _current_user=world.tenant,
            instance_key="t1a",
            chat_key=CHAT_T1A,
            page_size=100,
        )
        assert world.books["global_preset"].id not in _book_ids(by_channel)
        by_preset = await worldbook.list_books(
            _current_user=world.tenant,
            instance_key="t1a",
            preset_id=world.presets["global"].id,
            page_size=100,
        )
        assert _book_ids(by_preset) == {world.books["global_preset"].id}


class TestImportAnalysisIsolation:
    """导入的分析在事务之外、导入专用的执行器里跑：不占数据库连接，同一个租户一次一个"""

    async def test_analysis_runs_outside_the_transaction_and_blocks_no_one(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import threading

        from nekro_agent.schemas.errors import WorldbookAnalysisBusyError

        started = threading.Event()
        release = threading.Event()
        real = wb_purpose.template_import_notes

        def slow(columns_list: Any, **kwargs: Any) -> Any:
            started.set()
            release.wait(5)
            return real(columns_list, **kwargs)

        monkeypatch.setattr(wb_purpose, "template_import_notes", slow)
        task = asyncio.ensure_future(_import_new(world.platform, dump(world_of([full_entry(0)])), name="慢导入"))
        try:
            for _ in range(200):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()
            # 分析期间，另一个租户读库、平台方别的查询都照常（分析没开事务，不占连接）
            other = await asyncio.wait_for(
                worldbook.list_entries(world.books["global_preset"].id, _current_user=world.tenant),
                timeout=3,
            )
            assert other.total >= 1
            assert await asyncio.wait_for(DBWbBook.filter(name="慢导入").count(), timeout=3) == 0
            # 同一个租户（这里是平台方）再来一个导入：直接 409，不排队
            with pytest.raises(WorldbookAnalysisBusyError):
                await _import_new(world.platform, dump(world_of([full_entry(1)])), name="第二个")
        finally:
            release.set()
        report = await asyncio.wait_for(task, timeout=5)
        assert report.created == 1
        # 跑完名额就还回来了
        again = await _import_new(world.platform, dump(world_of([full_entry(2)])), name="第三个")
        assert again.created == 1

    async def test_import_does_not_count_tokens_through_the_default_pool(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nekro_agent.services.worldbook import tokens as wb_tokens

        async def forbidden(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("导入的 token 预数应在导入执行器里同步做")

        monkeypatch.setattr(wb_tokens, "base_counts", forbidden)
        monkeypatch.setattr(wb_tokens, "prefetch", forbidden)
        report = await _import_new(world.platform, dump(world_of([full_entry(0)])), name="不走默认线程池")
        assert report.created == 1

    async def test_analysis_cap_marks_the_rest_not_analysed(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wb_purpose, "IMPORT_ANALYSIS_CHARS", 1)
        entries = [full_entry(index, content="<% if (true) { %>有<% } %>") for index in range(3)]
        report = await _import_new(world.platform, dump(world_of(entries)), name="分析上限")
        assert report.created == 3
        assert report.notes["template_not_analyzed"] == 2


class TestIgnoreBudgetAndScanNames:
    """「忽略预算」与「扫描时带上说话人名字」：存取、导入导出、触发测试三处都要对上"""

    async def test_book_and_entry_flags_are_stored_and_returned(self, world: World) -> None:
        created = await worldbook.create_book(
            BookCreateRequest(name="带名字的书", scope_kind="domain", scan_names=True),
            instance_key="t1a",
            _current_user=world.platform,
        )
        assert created.scan_names is True
        assert (await DBWbBook.get(id=created.id)).scan_names is True

        listed = await worldbook.list_books(_current_user=world.platform, q="带名字的书", page_size=100)
        assert next(item for item in listed.items if item.id == created.id).scan_names is True

        updated = await worldbook.update_book(
            created.id,
            BookUpdateRequest(scan_names=False),
            _current_user=world.platform,
        )
        assert updated.scan_names is False
        assert (await DBWbBook.get(id=created.id)).scan_names is False

        entry = await worldbook.create_entry(
            created.id,
            EntryCreateRequest(keys=["必进"], content="必进的正文", ignore_budget=True),
            _current_user=world.platform,
        )
        assert entry.ignore_budget is True
        assert (await DBWbEntry.get(id=entry.id)).ignore_budget is True

        edited = await worldbook.update_entry(
            entry.id,
            EntryUpdateRequest(ignore_budget=False),
            _current_user=world.platform,
        )
        assert edited.ignore_budget is False
        assert (await DBWbEntry.get(id=entry.id)).ignore_budget is False

        entries = await worldbook.list_entries(created.id, _current_user=world.platform)
        assert next(item for item in entries.items if item.id == entry.id).ignore_budget is False

    async def test_creating_an_entry_without_the_flag_means_false(self, world: World) -> None:
        item = await worldbook.create_entry(
            world.books["domain_t1a"].id,
            EntryCreateRequest(keys=["普通"], content="普通正文"),
            _current_user=world.platform,
        )
        assert item.ignore_budget is False

    async def test_import_reads_ignore_budget_and_defaults_scan_names_on(self, world: World) -> None:
        # 直接调路由函数时 `Form(...)` 的默认值拿到的是 `FieldInfo`，所以这里显式传参；
        # 接口本身的默认值（新导入默认开）单独断言，免得「默认值改成 False」悄悄漏过去。
        scan_names_default = inspect.signature(worldbook.import_new_book).parameters["scan_names"].default
        assert getattr(scan_names_default, "default", scan_names_default) is True

        incoming = world_of(
            [
                full_entry(0, comment="必进的", ignoreBudget=True),
                full_entry(1, comment="普通的", key=["凤"]),
            ],
        )

        report = await _import_new(world.platform, dump(incoming), name="忽略预算书")

        rows = {row.comment: row for row in await DBWbEntry.filter(book_id=report.book_id)}
        assert rows["必进的"].ignore_budget is True
        assert rows["普通的"].ignore_budget is False
        # 表单没传 scan_names 时新导入的书默认带说话人名字
        assert (await DBWbBook.get(id=report.book_id)).scan_names is True

        explicit = await _import_new(world.platform, dump(incoming), name="不带名字的书", scan_names=False)
        assert (await DBWbBook.get(id=explicit.book_id)).scan_names is False

    async def test_append_import_keeps_the_books_scan_names(self, world: World) -> None:
        book = await _book(
            "不带名字的追加书",
            scope_kind="domain",
            adapter_key=ADAPTER,
            instance_key="t1a",
            scan_names=False,
        )

        report = await worldbook.import_into_book(
            book.id,
            file=_upload(dump(world_of([full_entry(0)]))),
            _current_user=world.platform,
        )

        assert report.created == 1
        assert (await DBWbBook.get(id=book.id)).scan_names is False

    async def test_export_writes_ignore_budget_and_survives_a_reimport(self, world: World) -> None:
        incoming = world_of([full_entry(0, comment="必进的", ignoreBudget=True)])
        report = await _import_new(world.platform, dump(incoming), name="导出忽略预算")

        response = await worldbook.export_book(report.book_id, _current_user=world.platform)
        exported = json.loads(response.body)
        assert exported["entries"]["0"]["ignoreBudget"] is True
        # 没映射的字段不重复留在条目里：`ignoreBudget` 只出现一次，且就是映射后的那个值
        assert sum(1 for key in exported["entries"]["0"] if key == "ignoreBudget") == 1

        again = await _import_new(world.platform, response.body, name="再导入的书")
        row = await DBWbEntry.get(book_id=again.book_id, uid=0)
        assert row.ignore_budget is True

    async def test_trigger_test_marks_entries_that_ignored_the_budget(self, world: World) -> None:
        book = await _book(
            "触发测试的忽略预算书",
            scope_kind="domain",
            adapter_key=ADAPTER,
            instance_key="t1a",
            budget_tokens=1,
        )
        exempt = await _entry(book, uid=0, constant=True, content="必进的正文", trusted=True, ignore_budget=True)
        tenant_entry = await _entry(
            book,
            uid=1,
            constant=True,
            content="租户写的正文",
            trusted=False,
            ignore_budget=True,
        )

        response = await worldbook.test_trigger(
            TriggerTestRequest(chat_key=CHAT_T1A, text="", use_recent=False, preset_id=world.presets["t1a"].id),
            _current_user=world.platform,
        )

        hits = {item.entry_id: item for item in response.hits}
        assert exempt.id in hits and hits[exempt.id].ignored_budget is True
        # 租户条目上的标记不起作用：书预算只有 1 token，它照旧被挡掉
        assert tenant_entry.id in hits
        assert hits[tenant_entry.id].ignored_budget is False
        assert tenant_entry.id not in response.selected
        assert tenant_entry.id in {item.entry_id for item in response.dropped}


class TestImportBusyIsCheckedFirst:
    async def test_busy_tenant_is_refused_before_the_upload_is_read(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nekro_agent.schemas.errors import WorldbookAnalysisBusyError

        async def forbidden(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("忙着的租户不该先把上传读完、解析完")

        monkeypatch.setattr(worldbook, "_read_upload", forbidden)
        key = wb_purpose.tenant_slot_key(None)
        wb_purpose._import_running.add(key)
        try:
            with pytest.raises(WorldbookAnalysisBusyError):
                await _import_new(world.platform, dump(world_of([full_entry(0)])), name="忙")
            with pytest.raises(WorldbookAnalysisBusyError):
                await worldbook.import_into_book(
                    world.books["domain_t1a"].id,
                    file=_upload(dump(world_of([full_entry(0)]))),
                    _current_user=world.platform,
                )
        finally:
            wb_purpose._import_running.discard(key)


class TestBookInjectModeAndScene:
    async def test_create_stores_and_returns_both_columns(self, world: World) -> None:
        detail = await worldbook.create_book(
            BookCreateRequest(
                name="只能查的私聊书",
                scope_kind="domain",
                inject_mode="lookup",
                active_scene="private",
            ),
            instance_key="t1a",
            _current_user=world.tenant,
        )

        assert detail.inject_mode == "lookup"
        assert detail.active_scene == "private"
        row = await DBWbBook.get(id=detail.id)
        assert row.inject_mode == "lookup"
        assert row.active_scene == "private"

    async def test_create_defaults_to_both(self, world: World) -> None:
        detail = await worldbook.create_book(
            BookCreateRequest(name="默认书", scope_kind="domain"),
            instance_key="t1a",
            _current_user=world.tenant,
        )

        assert detail.inject_mode == "both"
        assert detail.active_scene == "both"

    async def test_update_changes_both_and_keeps_omitted_values(self, world: World) -> None:
        book = await _book("要改的书", scope_kind="domain", adapter_key=ADAPTER, instance_key="t1a")

        changed = await worldbook.update_book(
            book.id,
            BookUpdateRequest(inject_mode="lookup", active_scene="group"),
            _current_user=world.platform,
        )
        assert changed.inject_mode == "lookup"
        assert changed.active_scene == "group"

        # 只改书名：没传的两列保持原值，不该被写回默认的 `both`。
        renamed = await worldbook.update_book(
            book.id,
            BookUpdateRequest(name="改过名的书"),
            _current_user=world.platform,
        )
        assert renamed.name == "改过名的书"
        assert renamed.inject_mode == "lookup"
        assert renamed.active_scene == "group"
        row = await DBWbBook.get(id=book.id)
        assert row.inject_mode == "lookup"
        assert row.active_scene == "group"

    async def test_invalid_values_are_rejected(self, world: World) -> None:
        with pytest.raises(PydanticValidationError):
            BookCreateRequest(name="坏注入方式", scope_kind="domain", inject_mode="nope")
        with pytest.raises(PydanticValidationError):
            BookUpdateRequest(active_scene="nope")

        with pytest.raises(ValidationError) as exc:
            await _import_new(
                world.platform,
                dump(world_of([full_entry(0)])),
                name="坏注入方式导入",
                inject_mode="nope",
            )
        assert "inject_mode" in str(exc.value)

    async def test_import_form_carries_both_columns(self, world: World) -> None:
        report = await _import_new(
            world.platform,
            dump(world_of([full_entry(0)])),
            name="带模式的导入书",
            inject_mode="lookup",
            active_scene="private",
        )

        row = await DBWbBook.get(id=report.book_id)
        assert row.inject_mode == "lookup"
        assert row.active_scene == "private"

    async def test_import_form_defaults_to_both(self, world: World) -> None:
        report = await _import_new(
            world.platform,
            dump(world_of([full_entry(0)])),
            name="默认模式的导入书",
        )

        row = await DBWbBook.get(id=report.book_id)
        assert row.inject_mode == "both"
        assert row.active_scene == "both"
