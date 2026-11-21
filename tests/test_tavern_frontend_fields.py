"""前端第一份要用的后端字段

## 为什么单独一份

这一份里每条用例对应人设页上的一个判据：列表要不要显示「酒馆卡」标记、扩展页要不要
说明「这段保存了会不会发」、预览消息怎么分组、明细里有没有酒馆那一栏、模型组表单能不能
存预填方式。它们分属五个模块，但共同点是**只加字段、不改行为**——所以这里连"旧值仍在"
一起钉：加字段最容易踩的坑是顺手把原来那一栏改坏。

## 两种身份

平台方走全局视图，租户走自己实例；越权结论仍与「不存在」逐字相同。模型组那一组额外钉
租户能改自己的组、改不了平台方的组。

## 另外两处编辑期修正

文件末尾两组用例钉住扩展行编辑时的两个判据：没有扩展行时宏默认关，以及停用的清理规则
只查形状、不按启用规则校验正则。它们与前面的「只加字段」不同，是会改变保存结果的修正。
"""

from __future__ import annotations

import datetime
import json
from typing import Any, AsyncIterator, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError
from tortoise import Tortoise

from nekro_agent.core.config import ModelConfigGroup
from nekro_agent.core.exception_handlers import register_exception_handlers
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_mem_prompt_round import DBMemPromptRound
from nekro_agent.models.db_model_group import DBModelGroup
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_preset_card import DBPresetCard
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import memory_admin, tavern
from nekro_agent.routers import presets as presets_router
from nekro_agent.routers.console import model_groups as model_groups_router
from nekro_agent.routers.console.model_groups import (
    ModelGroupCreateRequest,
    ModelGroupPayload,
    create_console_model_group,
    update_console_model_group,
)
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.schemas.tavern import PresetCardUpdateRequest
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services import config_resolver as config_resolver_module
from nekro_agent.services import model_group_service
from nekro_agent.services.tavern import cleanup_runtime
from nekro_agent.services.tavern import preview as preview_service
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
T1_CHAT = "onebot_v11-t1a-group_front"
PLATFORM_CHAT = "onebot_v11-p1-group_front"


def _user(user_id: int, username: str, *, role: Role, tenant_id: Optional[int], tenant_role: str) -> DBUser:
    return DBUser(
        id=user_id,
        username=username,
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=role,
        tenant_id=tenant_id,
        tenant_role=tenant_role,
    )


def _platform_user() -> DBUser:
    return _user(1, "platform", role=Role.Super, tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER.value)


def _tenant_user() -> DBUser:
    return _user(21, "tenant-admin", role=Role.Admin, tenant_id=1, tenant_role=TenantRole.TENANT_ADMIN.value)


def _other_tenant_user() -> DBUser:
    return _user(31, "tenant2-admin", role=Role.Admin, tenant_id=2, tenant_role=TenantRole.TENANT_ADMIN.value)


async def _instance(instance_key: str, tenant_id: Optional[int]) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        display_name=instance_key,
        tenant_id=tenant_id,
        provider_account_id=f"bot_{instance_key}",
    )


async def _preset(name: str, owner_instance_key: str) -> DBPreset:
    return await DBPreset.create(
        name=name,
        title=name,
        avatar="",
        content=f"{name}的正文",
        description="",
        tags="",
        author="测试",
        owner_instance_key=owner_instance_key,
    )


async def _channel(chat_key: str, *, instance_key: str, channel_id: str, preset_id: Optional[int]) -> DBChatChannel:
    return await DBChatChannel.create(
        chat_key=chat_key,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        channel_id=channel_id,
        channel_name=chat_key,
        channel_type="group",
        preset_id=preset_id,
        data="{}",
        workspace_id=None,
    )


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        # 实例层的闸状态用例要能改到「这个实例」的生效值：没有实例覆盖目录时解析器
        # 直接跳过实例层，两个实例的闸状态就永远一样，用例会变成同义反复。
        monkeypatch.setattr(config_resolver_module.config_resolver, "_has_instance_layer", lambda _adapter_key: True)

        await _instance("p1", None)
        await _instance("t1a", 1)
        await _instance("t2a", 2)

        presets = {
            "global": await _preset("全局人设", ""),
            "t1a": await _preset("租户一人设", "t1a"),
            "t2a": await _preset("租户二人设", "t2a"),
        }
        # 一个人设有卡片行、另一个没有：列表里的 `has_card` 才有两种取值可比
        cards = {
            "global": await DBPresetCard.create(
                preset=presets["global"],
                card_spec="v2",
                macros_enabled=True,
                cleanup_rules=[
                    {"name": "状态栏", "find": "/<status>/g", "replace": "", "trim": [], "enabled": True},
                ],
            ),
            "t1a": await DBPresetCard.create(preset=presets["t1a"], card_spec="", macros_enabled=True),
        }

        channels = {
            "t1": await _channel(T1_CHAT, instance_key="t1a", channel_id="front", preset_id=presets["t1a"].id),
            "p1": await _channel(PLATFORM_CHAT, instance_key="p1", channel_id="front", preset_id=presets["global"].id),
        }
        yield {
            "platform": _platform_user(),
            "tenant": _tenant_user(),
            "other_tenant": _other_tenant_user(),
            "presets": presets,
            "cards": cards,
            "channels": channels,
        }
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def _clean_cleanup_registry() -> AsyncIterator[None]:
    """停用登记是进程内状态：上一条用例留下的序号会让这一条凭空多出一个停用规则"""
    cleanup_runtime._disabled.clear()  # noqa: SLF001
    yield
    cleanup_runtime._disabled.clear()  # noqa: SLF001


async def _detail_of(preset_id: int, *, instance_key: str, user: DBUser) -> Any:
    return await presets_router.get_preset_detail(
        preset_id,
        instance_key=instance_key,
        _current_user=user,
    )


class TestPresetSummaryCardMark:
    """人设列表与详情带扩展行标记"""

    async def test_list_marks_only_presets_with_a_card_row(self, world: dict[str, Any]) -> None:
        response = await presets_router.get_preset_list(
            page=1,
            page_size=50,
            instance_key="",
            _current_user=world["platform"],
        )
        by_name = {item.name: item for item in response.items}

        assert by_name["全局人设"].has_card is True
        assert by_name["全局人设"].card_spec == "v2"
        # NA 原生人设建的扩展行 `card_spec` 是空串：有行、但没有卡片版本
        assert by_name["租户一人设"].has_card is True
        assert by_name["租户一人设"].card_spec == ""
        assert by_name["租户二人设"].has_card is False
        assert by_name["租户二人设"].card_spec == ""

    async def test_list_reads_the_card_rows_in_one_query(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """列表不能逐条查扩展行：一次批量查询把整页的标记带回来"""
        calls: list[list[int]] = []
        original = presets_router._card_marks  # noqa: SLF001

        async def counting(preset_ids: list[int]) -> Any:
            calls.append(list(preset_ids))
            return await original(preset_ids)

        monkeypatch.setattr(presets_router, "_card_marks", counting)
        response = await presets_router.get_preset_list(
            page=1,
            page_size=50,
            instance_key="",
            _current_user=world["platform"],
        )

        assert response.total == 3
        assert len(calls) == 1
        assert sorted(calls[0]) == sorted(preset.id for preset in world["presets"].values())

    async def test_detail_and_by_remote_carry_the_mark(self, world: dict[str, Any]) -> None:
        global_preset = world["presets"]["global"]
        global_preset.remote_id = "remote-front-fields"
        await global_preset.save()

        detail = await _detail_of(global_preset.id, instance_key="", user=world["platform"])
        by_remote = await presets_router.get_preset_detail_by_remote_id(
            "remote-front-fields",
            instance_key="",
            _current_user=world["platform"],
        )

        for response in (detail, by_remote):
            assert response.has_card is True
            assert response.card_spec == "v2"
            # 加字段不能把正文弄丢
            assert response.content == global_preset.content

    async def test_instance_default_preset_keeps_the_defaults(self, world: dict[str, Any]) -> None:
        """实例默认人设那一处不查扩展行，`has_card` 留默认值 False

        它不是列表也不是详情，界面上不显示标记；为它多查一次库不值得，但也不能回报
        「有卡片」——那会让调用方以为可以按卡片导出。这里把「有扩展行的人设」挂成实例
        默认，才能确认那一路真的没去查扩展行。
        """
        instance = await DBAdapterInstance.get(instance_key="t1a")
        instance.metadata_json = json.dumps({"default_preset_id": world["presets"]["t1a"].id})
        await instance.save()

        response = await presets_router.get_instance_default_preset(
            "t1a",
            _current_user=world["tenant"],
        )

        assert response.preset is not None
        assert response.preset.id == world["presets"]["t1a"].id
        assert response.preset.has_card is False
        assert response.preset.card_spec == ""

    async def test_list_item_keeps_the_old_fields(self, world: dict[str, Any]) -> None:
        response = await presets_router.get_preset_list(
            page=1,
            page_size=50,
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        item = next(row for row in response.items if row.name == "租户一人设")
        dumped = item.model_dump()
        for key in (
            "id",
            "name",
            "title",
            "avatar",
            "description",
            "tags",
            "author",
            "owner_instance_key",
            "is_global",
            "is_remote",
            "create_time",
            "update_time",
            "remote_id",
            "on_shared",
            "has_card",
            "card_spec",
        ):
            assert key in dumped
        # 全局人设对租户可见，且标记照常带出来
        assert any(row.name == "全局人设" and row.has_card for row in response.items)


class TestPresetCardGates:
    """扩展行响应带两个闸与停用规则序号"""

    async def test_gates_follow_the_instance_scope(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config_resolver_module.system_config, "WORLDBOOK_ENABLED", False)
        monkeypatch.setattr(config_resolver_module.system_config, "PROMPT_ADVANCED_SLOTS_ENABLED", False)

        effective = config_resolver_module.system_config.model_copy(
            update={"WORLDBOOK_ENABLED": True, "PROMPT_ADVANCED_SLOTS_ENABLED": True},
        )
        monkeypatch.setattr(
            config_resolver_module.config_resolver,
            "get_instance_effective_config",
            lambda _adapter_key, _instance_key: effective,
        )

        instance_view = await tavern.get_preset_card(
            world["presets"]["t1a"].id,
            instance_key="t1a",
            adapter_key=ADAPTER,
            _current_user=world["tenant"],
        )
        global_view = await tavern.get_preset_card(
            world["presets"]["global"].id,
            instance_key="",
            _current_user=world["platform"],
        )

        assert instance_view.gates.worldbook_enabled is True
        assert instance_view.gates.advanced_slots_enabled is True
        # 全局视图读全局配置：同一个请求换个作用域，闸状态跟着换
        assert global_view.gates.worldbook_enabled is False
        assert global_view.gates.advanced_slots_enabled is False

    async def test_get_without_row_still_reports_the_gates(self, world: dict[str, Any]) -> None:
        response = await tavern.get_preset_card(
            world["presets"]["t2a"].id,
            instance_key="t2a",
            adapter_key=ADAPTER,
            _current_user=world["other_tenant"],
        )

        assert response.exists is False
        assert response.slow_disabled_rules == []

    async def test_slow_disabled_rules_are_scoped_to_the_preset(self, world: dict[str, Any]) -> None:
        preset = world["presets"]["t1a"]
        other = world["presets"]["global"]
        cleanup_runtime._disabled[(preset.id, 3)] = "慢规则"  # noqa: SLF001
        cleanup_runtime._disabled[(preset.id, 1)] = "更慢的规则"  # noqa: SLF001
        cleanup_runtime._disabled[(other.id, 2)] = "别人家的规则"  # noqa: SLF001

        response = await tavern.get_preset_card(
            preset.id,
            instance_key="t1a",
            adapter_key=ADAPTER,
            _current_user=world["tenant"],
        )

        # 升序，且不含别人设的序号
        assert response.slow_disabled_rules == [1, 3]

    async def test_put_clears_the_disabled_registry_for_changed_rules(self, world: dict[str, Any]) -> None:
        """保存规则后 `forget_preset` 清掉登记，PUT 响应里必须是空的

        不清的话，管理员刚把一条太慢的规则改好，扩展页仍会显示「已停用」——而它其实
        已经重新参与执行了。
        """
        preset = world["presets"]["t1a"]
        cleanup_runtime._disabled[(preset.id, 0)] = "旧规则"  # noqa: SLF001

        response = await tavern.update_preset_card(
            preset.id,
            PresetCardUpdateRequest(
                cleanup_rules=[
                    {"name": "改好的规则", "find": "/a/g", "replace": "b", "trim": [], "enabled": True},
                ],
            ),
            instance_key="t1a",
            adapter_key=ADAPTER,
            _current_user=world["tenant"],
        )

        assert response.slow_disabled_rules == []
        assert cleanup_runtime.disabled_cleanup_rules() == {}
        assert isinstance(response.gates.worldbook_enabled, bool)

    async def test_put_without_touching_rules_keeps_the_registry(self, world: dict[str, Any]) -> None:
        """对照组：只说改昵称时不该顺手把停用登记清掉"""
        preset = world["presets"]["t1a"]
        cleanup_runtime._disabled[(preset.id, 0)] = "旧规则"  # noqa: SLF001

        response = await tavern.update_preset_card(
            preset.id,
            PresetCardUpdateRequest(nickname="新昵称"),
            instance_key="t1a",
            adapter_key=ADAPTER,
            _current_user=world["tenant"],
        )

        assert response.slow_disabled_rules == [0]

    async def test_tenant_cannot_read_another_instances_card(self, world: dict[str, Any]) -> None:
        with pytest.raises(Exception) as excinfo:
            await tavern.get_preset_card(
                world["presets"]["t2a"].id,
                instance_key="t1a",
                adapter_key=ADAPTER,
                _current_user=world["tenant"],
            )
        assert getattr(excinfo.value, "http_status", None) == 404


class TestPreviewMessageSources:
    """预览消息逐条标来源"""

    @pytest.mark.parametrize(
        ("count", "expected"),
        [
            (1, ["system"]),
            (2, ["system", "history"]),
            (5, ["system", "example", "example", "example", "history"]),
        ],
    )
    def test_message_sources_by_count(self, count: int, expected: list[str]) -> None:
        assert preview_service.message_sources(count) == expected

    def test_zero_messages_do_not_index_out_of_range(self) -> None:
        assert preview_service.message_sources(0) == []

    async def test_preview_response_marks_each_message(self) -> None:
        from types import SimpleNamespace

        messages = [
            SimpleNamespace(role=role, content=[{"type": "text", "text": f"第{index}段"}])
            for index, role in enumerate(("system", "user", "user", "user"))
        ]
        prompt = SimpleNamespace(messages=messages, meta={})

        response = preview_service.build_preview_response(prompt, "")

        assert [message.source for message in response.messages] == [
            "system",
            "example",
            "example",
            "history",
        ]
        # 标来源不能动文本
        assert response.messages[0].text == "第0段"

    async def test_recorded_sources_win_over_position(self) -> None:
        """组装处记下的来源优先：哪怕形状不是「1 系统 + N 示例 + 1 历史」也照记录标"""
        from types import SimpleNamespace

        messages = [
            SimpleNamespace(role=role, content=[{"type": "text", "text": "段"}])
            for role in ("system", "user", "system")
        ]
        recorded = ["system", "history", "history"]
        prompt = SimpleNamespace(messages=messages, meta={"message_sources": recorded})

        response = preview_service.build_preview_response(prompt, "")

        assert [message.source for message in response.messages] == recorded

    @pytest.mark.parametrize(
        "recorded",
        [
            pytest.param(["system", "history"], id="count-mismatch"),
            pytest.param(["system", "plugin", "history"], id="unknown-value"),
            pytest.param("system", id="not-a-list"),
        ],
    )
    async def test_unusable_record_falls_back_to_position(self, recorded: object) -> None:
        from types import SimpleNamespace

        messages = [
            SimpleNamespace(role=role, content=[{"type": "text", "text": "段"}])
            for role in ("system", "user", "user")
        ]
        prompt = SimpleNamespace(messages=messages, meta={"message_sources": recorded})

        response = preview_service.build_preview_response(prompt, "")

        assert [message.source for message in response.messages] == ["system", "example", "history"]


class TestPromptRoundTavernItems:
    """提示词明细带酒馆那一栏"""

    async def _round(self, *, instance_key: str) -> DBMemPromptRound:
        now = datetime.datetime.now(datetime.timezone.utc)
        return await DBMemPromptRound.create(
            round_id="front-fields-round",
            chat_key=T1_CHAT,
            adapter_key=ADAPTER,
            instance_key=instance_key,
            preset_id=7,
            workspace_id=0,
            outcome="ok",
            render_count=1,
            memory_state="empty",
            memory_items=[],
            memory_block_chars=0,
            cooldown_skipped=0,
            summary_state="none",
            summary_chars=0,
            affection_shown=False,
            affection_chars=0,
            affection_people=0,
            worldbook_items=[],
            tavern_items={"gate": "gated", "post_history": {"chars": 12, "state": "sent"}},
            started_at=now,
            finished_at=now,
        )

    async def test_list_item_carries_tavern_items(self, world: dict[str, Any]) -> None:
        await self._round(instance_key="t1a")

        response = await memory_admin.list_prompt_rounds(
            chat_key=T1_CHAT,
            instance_key="t1a",
            adapter_key=ADAPTER,
            _current_user=world["tenant"],
        )

        assert len(response.items) == 1
        assert response.items[0].tavern_items["gate"] == "gated"

    async def test_detail_response_really_returns_tavern_items(self, world: dict[str, Any]) -> None:
        """详情服务层早就在给这个键，被响应模型丢掉了：这条证明它真的出现在响应里"""
        row = await self._round(instance_key="t1a")

        response = await memory_admin.get_prompt_round(
            round_pk=row.id,
            _current_user=world["tenant"],
        )

        assert response.tavern_items["post_history"]["chars"] == 12
        assert response.tavern_items["gate"] == "gated"

    async def test_empty_tavern_items_are_an_empty_object(self, world: dict[str, Any]) -> None:
        """历史行上的空值可能不是对象：JSON 列读回 `[]` 时不能原样塞进 `dict` 字段

        列的默认值是 `{}`，但这一列在早期行上可能是空的数组；响应模型收 `dict`，
        原样透传会让整条明细 500。
        """
        row = await self._round(instance_key="t1a")
        row.tavern_items = []
        await row.save(update_fields=["tavern_items"])

        response = await memory_admin.get_prompt_round(
            round_pk=row.id,
            _current_user=world["tenant"],
        )

        assert response.tavern_items == {}


@pytest.fixture
async def model_group_db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """一张空的模型组表 + 不落盘的运行时视图

    `refresh_runtime_view()` 默认会把视图 dump 回配置文件，测试里那会真的写开发机上的
    yaml；`MODEL_GROUPS` 也要清空，否则上一份用例留下的组名会跟着算进可见列表。
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    monkeypatch.setattr(model_group_service, "save_config", lambda: None)
    monkeypatch.setattr(model_group_service.config, "MODEL_GROUPS", {})
    try:
        yield
    finally:
        await Tortoise.close_connections()


class TestModelGroupPrefillMode:
    """模型组接口收预填方式"""

    def test_payload_field_matches_the_config_field(self) -> None:
        """小写字段靠 `field.upper()` 合并：名字对不上时静默丢掉，不报错"""
        assert "prefill_mode" in ModelGroupPayload.model_fields
        assert "PREFILL_MODE" in ModelConfigGroup.model_fields

    async def test_create_with_prefill_mode(self, model_group_db: None) -> None:
        body = ModelGroupCreateRequest(name="front_fields_create", chat_model="stub-model", prefill_mode="assistant")
        info = await create_console_model_group(body=body, _current_user=_platform_user())

        row = await DBModelGroup.get(name="front_fields_create")
        assert row.prefill_mode == "assistant"
        assert info.config.PREFILL_MODE == "assistant"

    async def test_update_with_prefill_mode(self, model_group_db: None) -> None:
        await DBModelGroup.create(name="front_fields_update", chat_model="stub-model", prefill_mode="off")

        info = await update_console_model_group(
            name="front_fields_update",
            body=ModelGroupPayload(prefill_mode="deepseek"),
            _current_user=_platform_user(),
        )

        row = await DBModelGroup.get(name="front_fields_update")
        assert row.prefill_mode == "deepseek"
        assert info.config.PREFILL_MODE == "deepseek"

    async def test_omitting_prefill_mode_keeps_the_stored_value(self, model_group_db: None) -> None:
        """不传就是「不改」：编辑别的字段不能把预填方式打回默认"""
        await DBModelGroup.create(name="front_fields_keep", chat_model="stub-model", prefill_mode="deepseek")

        info = await update_console_model_group(
            name="front_fields_keep",
            body=ModelGroupPayload(group_name="改个显示名"),
            _current_user=_platform_user(),
        )

        assert (await DBModelGroup.get(name="front_fields_keep")).prefill_mode == "deepseek"
        assert info.config.PREFILL_MODE == "deepseek"

    @pytest.mark.parametrize("value", ["bogus", "", "off ", "ON", 1])
    def test_invalid_value_is_rejected_by_the_model(self, value: Any) -> None:
        with pytest.raises(PydanticValidationError):
            ModelGroupPayload(prefill_mode=value)

    @pytest.mark.parametrize("value", ["off", "assistant", "deepseek"])
    def test_the_three_known_values_are_accepted(self, value: str) -> None:
        """正向对照：只断言「非法被拒」的话，一个把所有值都拒掉的实现也能通过"""
        assert ModelGroupPayload(prefill_mode=value).prefill_mode == value

    async def test_tenant_can_update_own_group(self, model_group_db: None) -> None:
        await DBModelGroup.create(
            name="front_fields_tenant",
            ownership="tenant",
            owner_tenant_id=1,
            visibility="private",
            chat_model="stub-model",
            prefill_mode="off",
        )

        info = await update_console_model_group(
            name="front_fields_tenant",
            body=ModelGroupPayload(prefill_mode="assistant"),
            _current_user=_tenant_user(),
        )

        assert (await DBModelGroup.get(name="front_fields_tenant")).prefill_mode == "assistant"
        assert info.config.PREFILL_MODE == "assistant"

    async def test_tenant_cannot_update_platform_group(self, model_group_db: None) -> None:
        await DBModelGroup.create(
            name="front_fields_platform",
            ownership="platform",
            visibility="shared",
            chat_model="stub-model",
            prefill_mode="off",
        )

        with pytest.raises(Exception) as excinfo:
            await update_console_model_group(
                name="front_fields_platform",
                body=ModelGroupPayload(prefill_mode="deepseek"),
                _current_user=_tenant_user(),
            )

        assert getattr(excinfo.value, "http_status", None) == 403
        assert (await DBModelGroup.get(name="front_fields_platform")).prefill_mode == "off"


class TestModelGroupPrefillModeOverHttp:
    """非法值在请求体校验阶段就被拒，走不到端点

    状态码是 **400 而不是 FastAPI 默认的 422**：本仓 `core/exception_handlers.py` 把
    `RequestValidationError` 统一翻成 `ValidationError`，后者是 `http_status = 400`，
    并且把英文的字段路径拼进中文消息里给管理员看。用例按**真实部署**的结论断言，
    同时校验消息里点了字段名——只断言"非 200"的话，一条 404 也能过。

    这里刻意用**最小的 app + 真的那组路由**，且不带数据库夹具：请求体不合法时端点根本
    不会执行，所以不必为了这条判据把内存库连到另一个事件循环上。
    """

    @pytest.fixture
    def client(self) -> TestClient:
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(model_groups_router.router, prefix="/api/console")
        app.dependency_overrides[get_current_active_user] = _platform_user
        return TestClient(app, raise_server_exceptions=False)

    def test_invalid_prefill_mode_never_reaches_the_endpoint(self, client: TestClient) -> None:
        response = client.post(
            "/api/console/model-groups",
            json={"name": "front_fields_http", "chat_model": "stub-model", "prefill_mode": "bogus"},
        )

        assert response.status_code == 400, response.text
        assert response.json()["error"] == "ValidationError"
        assert "prefill_mode" in response.json()["message"]

    def test_the_route_is_really_mounted(self, client: TestClient) -> None:
        """对照组：同一个 app 上 400 来自请求体而不是「路径不存在」

        只断言 400 的话，一个路径写错的用例同样能过（404 也是 4xx）。这里把两条并列摆着。
        """
        bad_path = client.post("/api/console/model-groups/nope/nope", json={})
        bad_body = client.post("/api/console/model-groups", json={"chat_model": 5, "name": ["不是字符串"]})

        assert bad_path.status_code == 404
        assert bad_body.status_code == 400


class TestPresetCardDefaults:
    """没有扩展行时的默认值与 PUT 新建行一致"""

    async def test_get_without_row_reports_macros_off(self, world: dict[str, Any]) -> None:
        """原生人设没有扩展行时宏默认关

        模型默认值是「导入的卡默认开」，前端会把 GET 的值当表单初值；这里若回默认值，
        管理员第一次保存扩展就会把宏打开，正文从逐字不变变成被展开。
        """
        response = await tavern.get_preset_card(
            world["presets"]["t2a"].id,
            instance_key="t2a",
            adapter_key=ADAPTER,
            _current_user=world["other_tenant"],
        )

        assert response.exists is False
        assert response.macros_enabled is False

    async def test_put_greeting_keeps_macros_off_for_a_new_row(self, world: dict[str, Any]) -> None:
        """只保存一个开场白时，新建的扩展行不会因为默认值把宏打开"""
        preset = world["presets"]["t2a"]

        await tavern.update_preset_card(
            preset.id,
            PresetCardUpdateRequest(greetings=["你好"]),
            instance_key="t2a",
            adapter_key=ADAPTER,
            _current_user=world["other_tenant"],
        )

        card = await DBPresetCard.get(preset_id=preset.id)
        assert card.greetings == ["你好"]
        assert card.macros_enabled is False


class TestDisabledCleanupRules:
    """停用的清理规则只查形状，不按启用规则校验正则"""

    UNSAFE_FIND = "(a+)+"

    async def _seed_rules(self, world: dict[str, Any]) -> DBPresetCard:
        """按导入的产物造两条规则：一条因 unsafe_regex 停用，一条正常启用"""
        card = world["cards"]["t1a"]
        card.cleanup_rules = [
            {
                "name": "不安全",
                "find": self.UNSAFE_FIND,
                "replace": "",
                "trim": [],
                "enabled": False,
                "disabled_reason": "unsafe_regex",
                "original": {"scriptName": "不安全", "findRegex": self.UNSAFE_FIND},
            },
            {
                "name": "正常",
                "find": "/x/g",
                "replace": "y",
                "trim": [],
                "enabled": True,
                "disabled_reason": "",
                "original": {"scriptName": "正常", "findRegex": "/x/g"},
            },
        ]
        await card.save()
        return card

    async def test_saving_the_table_keeps_the_disabled_rule_and_its_original(
        self,
        world: dict[str, Any],
    ) -> None:
        """改启用那条的替换文字能存进去，停用那条原样保留（底稿也在）

        前端会把整张表传回来，包括过不了安全检查的那条；旧实现逐条按启用规则校验，
        这张表从此存不进去。
        """
        await self._seed_rules(world)
        preset = world["presets"]["t1a"]

        response = await tavern.update_preset_card(
            preset.id,
            PresetCardUpdateRequest(
                cleanup_rules=[
                    {
                        "name": "不安全",
                        "find": self.UNSAFE_FIND,
                        "replace": "",
                        "trim": [],
                        "enabled": False,
                        "disabled_reason": "unsafe_regex",
                    },
                    {
                        "name": "正常",
                        "find": "/x/g",
                        "replace": "z",
                        "trim": [],
                        "enabled": True,
                        "disabled_reason": "",
                    },
                ],
            ),
            instance_key="t1a",
            adapter_key=ADAPTER,
            _current_user=world["tenant"],
        )

        assert [rule["name"] for rule in response.cleanup_rules] == ["不安全", "正常"]
        assert response.cleanup_rules[0]["enabled"] is False
        assert response.cleanup_rules[1]["replace"] == "z"
        # 响应不带底稿，但库里必须还留着：导出还原靠它
        assert "original" not in response.cleanup_rules[0]
        stored = await DBPresetCard.get(preset_id=preset.id)
        assert stored.cleanup_rules[0]["find"] == self.UNSAFE_FIND
        assert stored.cleanup_rules[0]["original"] == {
            "scriptName": "不安全",
            "findRegex": self.UNSAFE_FIND,
        }

    async def test_enabling_the_unsafe_rule_is_rejected_with_its_index(
        self,
        world: dict[str, Any],
    ) -> None:
        """管理员把不安全的那条打开再保存：照旧 400，原因指向第 1 条"""
        await self._seed_rules(world)
        preset = world["presets"]["t1a"]

        with pytest.raises(ValidationError) as excinfo:
            await tavern.update_preset_card(
                preset.id,
                PresetCardUpdateRequest(
                    cleanup_rules=[
                        {
                            "name": "不安全",
                            "find": self.UNSAFE_FIND,
                            "replace": "",
                            "trim": [],
                            "enabled": True,
                            "disabled_reason": "",
                        },
                    ],
                ),
                instance_key="t1a",
                adapter_key=ADAPTER,
                _current_user=world["tenant"],
            )

        assert excinfo.value.http_status == 400
        assert "第 1 条" in str(excinfo.value)

    async def test_imported_oversized_disabled_rules_can_be_saved_back(
        self,
        world: dict[str, Any],
    ) -> None:
        """导入时原样存下的大规则（正则过长、HTML 替换上万字、去除串很多）回传照样能存

        这些规则正是因为超出「要运行的规则」的上限才被停用的；停用规则若沿用那组上限，
        改表里任何一条都会因为它们 400。
        """
        await self._seed_rules(world)
        preset = world["presets"]["t1a"]
        long_find = "/" + "a" * 300 + "/g"
        long_replace = "<div>" + "状" * 5000 + "</div>"
        many_trims = [f"t{index}" for index in range(30)]

        response = await tavern.update_preset_card(
            preset.id,
            PresetCardUpdateRequest(
                cleanup_rules=[
                    {
                        "name": "正则过长",
                        "find": long_find,
                        "replace": "",
                        "trim": many_trims,
                        "enabled": False,
                        "disabled_reason": "unsafe_regex",
                    },
                    {
                        "name": "状态栏",
                        "find": "/x/g",
                        "replace": long_replace,
                        "trim": [],
                        "enabled": False,
                        "disabled_reason": "html_output",
                    },
                ],
            ),
            instance_key="t1a",
            adapter_key=ADAPTER,
            _current_user=world["tenant"],
        )

        assert response.cleanup_rules[0]["find"] == long_find
        assert response.cleanup_rules[0]["trim"] == many_trims
        assert response.cleanup_rules[1]["replace"] == long_replace

    async def test_enabling_an_oversized_html_rule_is_still_checked(
        self,
        world: dict[str, Any],
    ) -> None:
        """同一条大替换文字的规则一旦打开，就要按运行规则的上限检查"""
        await self._seed_rules(world)
        preset = world["presets"]["t1a"]

        with pytest.raises(ValidationError) as excinfo:
            await tavern.update_preset_card(
                preset.id,
                PresetCardUpdateRequest(
                    cleanup_rules=[
                        {
                            "name": "状态栏",
                            "find": "/x/g",
                            "replace": "<div>" + "状" * 5000 + "</div>",
                            "trim": [],
                            "enabled": True,
                            "disabled_reason": "",
                        },
                    ],
                ),
                instance_key="t1a",
                adapter_key=ADAPTER,
                _current_user=world["tenant"],
            )

        assert excinfo.value.http_status == 400
        assert "第 1 条" in str(excinfo.value)

    async def test_disabled_rule_with_an_overlong_find_is_rejected(
        self,
        world: dict[str, Any],
    ) -> None:
        """停用不等于什么都能存：查找串超过存储上限仍要 400"""
        await self._seed_rules(world)
        preset = world["presets"]["t1a"]

        with pytest.raises(ValidationError) as excinfo:
            await tavern.update_preset_card(
                preset.id,
                PresetCardUpdateRequest(
                    cleanup_rules=[
                        {
                            "name": "太长的查找",
                            "find": "a" * 64001,
                            "replace": "",
                            "trim": [],
                            "enabled": False,
                            "disabled_reason": "",
                        },
                    ],
                ),
                instance_key="t1a",
                adapter_key=ADAPTER,
                _current_user=world["tenant"],
            )

        assert excinfo.value.http_status == 400
        assert "第 1 条" in str(excinfo.value)
