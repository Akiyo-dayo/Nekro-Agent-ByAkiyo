"""提示词预览、试发与变更记录接口

直接调用路由函数，跑在真表上：频道、人设、书、模型组、用量账本都是真行；只有模型调用
与几条有副作用的协作者被替换。平台方与租户管理员各走一遍，另外钉住只读成员被装饰器挡住。

预览的承诺是「不调模型、不写库、不留进程内状态」，所以除了响应内容，还比较调用前后的
缓存、草稿、账本行数与几个副作用钩子的调用次数。试发的承诺是「只发一次、不发进频道、
不进聊天记录与记忆」，用打桩的模型与发送接口逐条验证。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, AsyncIterator, Optional

import pytest
from fastapi import HTTPException
from tortoise import Tortoise

from nekro_agent.adapters import loaded_adapters
from nekro_agent.core.config import ModelConfigGroup, config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_channel_quota import DBChannelQuotaDaily
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_config_audit import DBConfigChangeAudit
from nekro_agent.models.db_llm_usage import DBLLMUsageRecord
from nekro_agent.models.db_mem_prompt_round import DBMemPromptRound
from nekro_agent.models.db_model_group import DBModelGroup
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_preset_card import DBPresetCard
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.routers import tavern
from nekro_agent.schemas.errors import AppError, NotFoundError, ValidationError
from nekro_agent.schemas.tavern import TavernPreviewRequest, TavernTrialRequest
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.agent import round_prompt as round_prompt_module
from nekro_agent.services.agent.openai import OpenAIResponse
from nekro_agent.services.agent.templates.compiler import EXAMPLE_SESSION_TOKEN
from nekro_agent.services.memory import prompt_rounds, recall_cooldown
from nekro_agent.services.tavern import runtime as tavern_runtime
from nekro_agent.services.tavern import trial as trial_service
from nekro_agent.services.usage.context import current_usage_context
from nekro_agent.services.usage.ledger import flush_llm_usage, record_llm_usage
from nekro_agent.services.user.role import Role
from nekro_agent.services.worldbook import render as wb_render
from nekro_agent.services.worldbook import runtime as wb_runtime
from nekro_agent.services.worldbook import timed_state

ADAPTER = "onebot_v11"
P1 = "onebot_v11-p1-group_p"
T1 = "onebot_v11-t1a-group_t"
T1_EMPTY = "onebot_v11-t1a-group_empty"
T2 = "onebot_v11-t2a-group_t"
T3 = "onebot_v11-t3a-group_t"

PLATFORM_SECRET_BOOK = "平台方的秘密设定"
TENANT_BOOK_BODY = "租户自己的设定"


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


def _tenant2_user() -> DBUser:
    return _user(31, "tenant2-admin", role=Role.Admin, tenant_id=2, tenant_role=TenantRole.TENANT_ADMIN.value)


def _tenant3_user() -> DBUser:
    return _user(41, "tenant3-admin", role=Role.Admin, tenant_id=3, tenant_role=TenantRole.TENANT_ADMIN.value)


def _readonly_user() -> DBUser:
    return _user(22, "tenant-reader", role=Role.User, tenant_id=1, tenant_role=TenantRole.TENANT_USER.value)


class _FakeAdapter:
    """只实现 `build_round_prompt` 真正会调的四个方法"""

    chat_key_rules: list[str] = []
    config = SimpleNamespace(SESSION_ENABLE_AT=False)

    async def set_dialog_example(self) -> list[Any]:
        return []

    async def get_jinja_env(self) -> None:
        return None

    async def get_self_info(self, _chat_key: str) -> Any:
        return type("SelfInfo", (), {"platform_name": "qq", "user_id": "10001"})()

    async def render_runtime_prompt(self) -> str:
        return ""


def _group(chat_model: str = "stub-model", prefill_mode: str = "assistant") -> ModelConfigGroup:
    return ModelConfigGroup(
        GROUP_NAME=chat_model,
        CHAT_MODEL=chat_model,
        PREFILL_MODE=prefill_mode,  # type: ignore[arg-type]
        ENABLE_VISION=False,
    )


async def _instance(adapter_key: str, instance_key: str, tenant_id: Optional[int]) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=adapter_key,
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


async def _channel(
    chat_key: str,
    *,
    instance_key: str,
    channel_id: str,
    preset_id: Optional[int],
) -> DBChatChannel:
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


async def _message(chat_key: str, *, instance_key: str, message_id: str, content: str) -> DBChatMessage:
    return await DBChatMessage.create(
        sender_id="10001",
        sender_name="群友",
        sender_nickname="群友",
        is_tome=0,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        message_id=message_id,
        chat_key=chat_key,
        chat_type="group",
        platform_userid="10001",
        content_text=content,
        content_data="{}",
        raw_cq_code="",
        ext_data="",
        send_timestamp=1_800_000_000,
    )


async def _model_row(
    name: str,
    *,
    owner_tenant_id: Optional[int],
    model_type: str = "chat",
    visibility: str = "private",
) -> DBModelGroup:
    return await DBModelGroup.create(
        name=name,
        ownership="tenant" if owner_tenant_id is not None else "platform",
        owner_tenant_id=owner_tenant_id,
        visibility=visibility,
        group_name=name,
        chat_model="stub-model",
        model_type=model_type,
        prefill_mode="assistant",
    )


def _openai_response(text: str, *, model: str = "stub-model") -> OpenAIResponse:
    return OpenAIResponse(
        response_content=text,
        thought_chain="",
        messages=[],
        message_cnt=0,
        token_consumption=30,
        token_input=10,
        token_output=20,
        use_model=model,
        speed_tokens_per_second=0.0,
        first_token_cost_ms=0,
        generation_time_ms=0,
        stream_mode=False,
    )


class _PrefillRejected(Exception):
    status_code = 400


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)
        monkeypatch.setattr(config, "PERSONA_GREETING_ENABLED", True)
        monkeypatch.setattr(config, "WORLDBOOK_ENABLED", True)
        monkeypatch.setattr(config, "WORLDBOOK_MAX_ENTRIES_PER_ROUND", 20)
        monkeypatch.setattr(config, "PROMPT_ADVANCED_SLOTS_ENABLED", True)
        monkeypatch.setattr(config, "PROMPT_POST_HISTORY", "平台{{char}}")
        monkeypatch.setattr(config, "PROMPT_AUXILIARY", "平台辅助")
        monkeypatch.setattr(config, "PROMPT_PREFILL", "平台预填")
        monkeypatch.setattr(config, "USE_MODEL_GROUP", "shared")
        monkeypatch.setattr(config, "TAVERN_TRIAL_DAILY_LIMIT", 100)
        monkeypatch.setattr(config, "AI_REQUEST_STREAM_MODE", False)
        monkeypatch.setattr(
            config,
            "MODEL_GROUPS",
            {
                "shared": _group("shared-model"),
                "t1chat": _group("t1-model"),
                "t2chat": _group("t2-model"),
                "t1embed": _group("embed-model"),
            },
        )

        await _instance(ADAPTER, "p1", None)
        await _instance(ADAPTER, "t1a", 1)
        await _instance(ADAPTER, "t2a", 2)
        await _instance(ADAPTER, "t3a", 3)
        await _model_row("shared", owner_tenant_id=None, visibility="shared")
        await _model_row("t1chat", owner_tenant_id=1)
        await _model_row("t2chat", owner_tenant_id=2)
        await _model_row("t1embed", owner_tenant_id=1, model_type="embedding")

        presets = {
            "global": await _preset("全局人设", ""),
            "t1a": await _preset("租户一人设", "t1a"),
            "t2a": await _preset("租户二人设", "t2a"),
        }
        global_card = await DBPresetCard.create(
            preset=presets["global"],
            nickname="角色",
            greetings=["开场白正文"],
            group_greetings=[],
            post_history="{{original}}+人设",
            prefill="",
            macros_enabled=True,
        )
        global_book = await DBWbBook.create(
            name="全局人设书",
            scope_kind="preset",
            adapter_key="",
            instance_key="",
            preset_id=presets["global"].id,
        )
        await DBWbEntry.create(
            book=global_book,
            uid=0,
            keys=[],
            constant=True,
            content=PLATFORM_SECRET_BOOK,
            position="after_persona",
        )
        tenant_book = await DBWbBook.create(
            name="租户人设书",
            scope_kind="preset",
            adapter_key=ADAPTER,
            instance_key="t1a",
            preset_id=presets["global"].id,
        )
        await DBWbEntry.create(
            book=tenant_book,
            uid=0,
            keys=[],
            constant=True,
            content=TENANT_BOOK_BODY,
            position="after_persona",
        )

        channels = {
            "p1": await _channel(P1, instance_key="p1", channel_id="p", preset_id=presets["global"].id),
            "t1": await _channel(T1, instance_key="t1a", channel_id="t", preset_id=presets["global"].id),
            "t1empty": await _channel(T1_EMPTY, instance_key="t1a", channel_id="empty", preset_id=presets["global"].id),
            "t2": await _channel(T2, instance_key="t2a", channel_id="t2", preset_id=presets["global"].id),
            "t3": await _channel(T3, instance_key="t3a", channel_id="t3", preset_id=presets["global"].id),
        }
        await _message(T1, instance_key="t1a", message_id="m1", content="群友甲说话")
        await _message(T1, instance_key="t1a", message_id="m2", content="群友乙说话")

        monkeypatch.setitem(loaded_adapters, ADAPTER, _FakeAdapter())
        monkeypatch.setattr(round_prompt_module, "render_affection_prompt", _empty_affection)
        monkeypatch.setattr(round_prompt_module, "render_ops_prompt_section", lambda _ctx: "")

        yield {
            "platform": _platform_user(),
            "tenant": _tenant_user(),
            "tenant2": _tenant2_user(),
            "tenant3": _tenant3_user(),
            "readonly": _readonly_user(),
            "presets": presets,
            "global_card": global_card,
            "channels": channels,
            "books": {"global": global_book, "tenant": tenant_book},
            "counters": {"schedule": 0, "note_rendered": 0, "send": 0, "memory": 0, "plugin_inject": []},
        }
    finally:
        await flush_llm_usage()
        await Tortoise.close_connections()


async def _empty_affection(**_kwargs: Any) -> str:
    return ""


@pytest.fixture(autouse=True)
def _clean_process_state() -> AsyncIterator[None]:
    tavern._trial_timestamps.clear()  # noqa: SLF001
    prompt_rounds._rounds.clear()  # noqa: SLF001
    tavern_runtime._rounds.clear()  # noqa: SLF001
    wb_runtime._rounds.clear()  # noqa: SLF001
    timed_state.timed_state_store.clear()
    yield
    tavern._trial_timestamps.clear()  # noqa: SLF001
    prompt_rounds._rounds.clear()  # noqa: SLF001
    tavern_runtime._rounds.clear()  # noqa: SLF001
    wb_runtime._rounds.clear()  # noqa: SLF001
    timed_state.timed_state_store.clear()


def _texts(response: Any) -> str:
    return "\n".join(message.text for message in response.messages)


def _field(message: Any, name: str) -> Any:
    return getattr(message, name) if hasattr(message, name) else message[name]


def _install_model(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[Any],
    calls: Optional[list[list[Any]]] = None,
) -> list[list[Any]]:
    """按顺序返回响应或抛异常；每次调用先按当前归属记一行用量，模拟真实发送侧"""
    seen: list[list[Any]] = calls if calls is not None else []

    async def fake(**kwargs: Any) -> OpenAIResponse:
        seen.append(list(kwargs["messages"]))
        item = responses[len(seen) - 1]
        if isinstance(item, Exception):
            raise item
        text = str(item)
        record_llm_usage(
            model_name="stub-model",
            stream_mode=False,
            token_input=10,
            token_output=20,
            token_total=30,
            context=current_usage_context(),
        )
        return _openai_response(text)

    monkeypatch.setattr(trial_service, "gen_openai_chat_response", fake)
    return seen


class TestPreview:
    async def test_messages_have_system_history_and_fixed_token(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(tavern.secrets, "token_hex", lambda _n: "deadbeefdeadbeef")

        response = await tavern.preview_prompt(
            TavernPreviewRequest(chat_key=T1),
            _current_user=world["tenant"],
        )

        assert response.messages[0].role == "system"
        assert response.messages[-1].role == "user"
        assert "Recent Messages:" in response.messages[-1].text
        assert "[EXAMPLE ON" in _texts(response)
        assert "The examples above have ended" in _texts(response)
        text = _texts(response)
        assert "deadbeefdeadbeef" not in text
        assert EXAMPLE_SESSION_TOKEN in text
        assert response.meta["persona_chars"] > 0

    async def test_offline_bot_does_not_break_the_preview(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """协议端没连上时预览照常返回，Bot 自己的账号换成占位并在元数据里标出来

        实例掉线是常态；取 Bot 信息失败原来会让预览整页 500。
        """

        async def _offline(_chat_key: str) -> Any:
            raise RuntimeError("协议端没连上")

        monkeypatch.setattr(loaded_adapters[ADAPTER], "get_self_info", _offline, raising=False)

        response = await tavern.preview_prompt(
            TavernPreviewRequest(chat_key=T1),
            _current_user=world["tenant"],
        )

        assert response.meta["bot_offline"] is True
        assert round_prompt_module.OFFLINE_BOT_ID_PLACEHOLDER in response.messages[0].text

    async def test_preview_with_another_preset_uses_that_presets_books(self, world: dict[str, Any]) -> None:
        """换一个人设预览时，人设书按所选人设判，不再是「新人设 + 频道当前人设的书」"""
        other = world["presets"]["t1a"]
        book = await DBWbBook.create(
            name="租户一人设的书",
            scope_kind="preset",
            adapter_key=ADAPTER,
            instance_key="t1a",
            preset_id=other.id,
        )
        await DBWbEntry.create(
            book=book,
            uid=0,
            keys=[],
            constant=True,
            content="只属于租户一人设的设定",
            position="after_persona",
        )

        current = await tavern.preview_prompt(
            TavernPreviewRequest(chat_key=T1),
            _current_user=world["tenant"],
        )
        switched = await tavern.preview_prompt(
            TavernPreviewRequest(chat_key=T1, preset_id=other.id),
            _current_user=world["tenant"],
        )

        assert TENANT_BOOK_BODY in _texts(current)
        assert "只属于租户一人设的设定" not in _texts(current)
        assert "只属于租户一人设的设定" in _texts(switched)
        assert TENANT_BOOK_BODY not in _texts(switched)

    async def test_online_bot_is_reported_as_online(self, world: dict[str, Any]) -> None:
        response = await tavern.preview_prompt(
            TavernPreviewRequest(chat_key=T1),
            _current_user=world["tenant"],
        )

        # 来源是组装处记下的，条数与消息一一对应
        assert response.meta["message_sources"] == [message.source for message in response.messages]
        assert response.meta["message_sources"][0] == "system"
        assert response.meta["message_sources"][-1] == "history"
        assert response.meta["bot_offline"] is False
        assert round_prompt_module.OFFLINE_BOT_ID_PLACEHOLDER not in _texts(response)

    async def test_real_round_still_fails_when_the_bot_is_offline(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """只有预览 / 试发兜底；正式回合取不到 Bot 信息照旧抛出"""

        async def _offline(_chat_key: str) -> Any:
            raise RuntimeError("协议端没连上")

        monkeypatch.setattr(loaded_adapters[ADAPTER], "get_self_info", _offline, raising=False)
        channel = world["channels"]["t1"]

        with pytest.raises(RuntimeError, match="协议端没连上"):
            await round_prompt_module.build_round_prompt(
                chat_key=T1,
                db_chat_channel=channel,
                ctx=await tavern.AgentCtx.create_by_chat_key(T1),
                one_time_code="0123456789abcdef",
                trigger_message=None,
                dry_run=False,
            )

    async def test_sample_line_is_last_and_uses_real_format(self, world: dict[str, Any]) -> None:
        response = await tavern.preview_prompt(
            TavernPreviewRequest(chat_key=T1, sample_text="示例内容"),
            _current_user=world["tenant"],
        )

        last = response.messages[-1].text
        assert '"管理员（预览）" 说: 示例内容' in last
        assert last.index("示例内容") > last.index("群友乙说话")
        assert "(tome:true)[" in last
        assert ' id:] "管理员（预览）" 说: 示例内容' in last

    async def test_sample_line_survives_empty_channel(self, world: dict[str, Any]) -> None:
        response = await tavern.preview_prompt(
            TavernPreviewRequest(chat_key=T1_EMPTY, sample_text="空频道里的一句话"),
            _current_user=world["tenant"],
        )

        last = response.messages[-1].text
        assert '"管理员（预览）" 说: 空频道里的一句话' in last
        assert last.index("空频道里的一句话") > last.index("Recent Messages:")

    async def test_invisible_preset_is_404(self, world: dict[str, Any]) -> None:
        with pytest.raises(NotFoundError):
            await tavern.preview_prompt(
                TavernPreviewRequest(chat_key=T1, preset_id=world["presets"]["t2a"].id),
                _current_user=world["tenant"],
            )

    async def test_out_of_scope_channel_is_404(self, world: dict[str, Any]) -> None:
        with pytest.raises(NotFoundError):
            await tavern.preview_prompt(
                TavernPreviewRequest(chat_key=T2),
                _current_user=world["tenant"],
            )

    async def test_tenant_sees_platform_global_preset_book_body(self, world: dict[str, Any]) -> None:
        # 平台方给全局人设建的书对人设可见的租户只读开放：预览里正文照常显示
        response = await tavern.preview_prompt(
            TavernPreviewRequest(chat_key=T1),
            _current_user=world["tenant"],
        )

        text = _texts(response)
        assert PLATFORM_SECRET_BOOK in text
        assert wb_render.HIDDEN_BODY not in text
        assert TENANT_BOOK_BODY in text

    async def test_tenant_cannot_see_book_whose_preset_is_hidden_from_it(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 判据判成不可见的书（例如绑在别人实例私有人设上的平台书），预览里正文仍换成占位
        from nekro_agent.services.worldbook import service as wb_service

        async def _all_global_invisible(_user: Any, books: Any) -> set[int]:
            return {book.id for book in books if not book.adapter_key}

        monkeypatch.setattr(wb_service, "books_invisible_to", _all_global_invisible)
        response = await tavern.preview_prompt(
            TavernPreviewRequest(chat_key=T1),
            _current_user=world["tenant"],
        )

        text = _texts(response)
        assert PLATFORM_SECRET_BOOK not in text
        assert wb_render.HIDDEN_BODY in text
        assert TENANT_BOOK_BODY in text

    async def test_platform_sees_global_book_body(self, world: dict[str, Any]) -> None:
        response = await tavern.preview_prompt(
            TavernPreviewRequest(chat_key=T1),
            _current_user=world["platform"],
        )

        text = _texts(response)
        assert PLATFORM_SECRET_BOOK in text
        assert wb_render.HIDDEN_BODY not in text
        assert TENANT_BOOK_BODY in text

    async def test_tenant_masks_platform_slot_text_with_true_length(self, world: dict[str, Any]) -> None:
        response = await tavern.preview_prompt(
            TavernPreviewRequest(chat_key=T1),
            _current_user=world["tenant"],
        )

        expected_post = f"[含平台方设置的后置指令，共 {len('平台角色+人设')} 字]"
        expected_aux = f"[含平台方设置的辅助提示词，共 {len('平台辅助')} 字]"
        expected_prefill = f"[含平台方设置的预填，共 {len('平台预填')} 字]"
        assert response.meta["slots"]["post_history"]["text"] == expected_post
        assert response.meta["slots"]["auxiliary"]["text"] == expected_aux
        assert response.meta["slots"]["prefill"]["text"] == expected_prefill
        assert response.meta["slots_preview"]["post_history"]["text"] == expected_post
        assert response.prefill.text == expected_prefill
        text = _texts(response)
        assert "平台角色+人设" not in text
        assert "平台辅助" not in text
        assert "平台预填" not in text

    async def test_platform_sees_slot_originals_and_expansion(self, world: dict[str, Any]) -> None:
        response = await tavern.preview_prompt(
            TavernPreviewRequest(chat_key=T1),
            _current_user=world["platform"],
        )

        assert response.meta["slots"]["post_history"]["text"] == "平台角色+人设"
        assert response.meta["slots"]["auxiliary"]["text"] == "平台辅助"
        assert response.meta["slots"]["prefill"]["text"] == "平台预填"
        assert "平台角色+人设" in _texts(response)

    async def test_preview_leaves_no_state(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services.agent.templates import history as history_module
        from nekro_agent.services.chat.universal_chat_service import universal_chat_service
        from nekro_agent.services.ops_agent import context_guard

        stats = world["counters"]

        async def schedule(*_args: Any, **_kwargs: Any) -> None:
            stats["schedule"] += 1

        def note(*_args: Any, **_kwargs: Any) -> None:
            stats["note_rendered"] += 1

        async def send(*_args: Any, **_kwargs: Any) -> None:
            stats["send"] += 1

        async def memory(**_kwargs: Any) -> str:
            stats["memory"] += 1
            return "不应出现"

        async def plugins(*_args: Any, **kwargs: Any) -> Any:
            stats["plugin_inject"].append(kwargs.get("call_inject"))
            return type("Rendered", (), {"runtime_prompt": "", "system_prompt": "", "depth_blocks": []})()

        monkeypatch.setattr(round_prompt_module, "schedule_affection_evaluation", schedule)
        monkeypatch.setattr(context_guard, "note_rendered_history", note)
        monkeypatch.setattr(universal_chat_service, "send_agent_message", send)
        monkeypatch.setattr(history_module, "_inject_memory_context", memory)
        monkeypatch.setattr(round_prompt_module, "render_plugins_prompt", plugins)

        before = (
            len(prompt_rounds._rounds),  # noqa: SLF001
            len(wb_runtime._rounds),  # noqa: SLF001
            len(tavern_runtime._rounds),  # noqa: SLF001
            len(timed_state.timed_state_store._staged),  # noqa: SLF001
            len(recall_cooldown._cooldowns),  # noqa: SLF001
            await DBMemPromptRound.all().count(),
        )

        await tavern.preview_prompt(TavernPreviewRequest(chat_key=T1, sample_text="看看"), _current_user=world["tenant"])

        after = (
            len(prompt_rounds._rounds),  # noqa: SLF001
            len(wb_runtime._rounds),  # noqa: SLF001
            len(tavern_runtime._rounds),  # noqa: SLF001
            len(timed_state.timed_state_store._staged),  # noqa: SLF001
            len(recall_cooldown._cooldowns),  # noqa: SLF001
            await DBMemPromptRound.all().count(),
        )
        assert after == before
        assert stats["schedule"] == 0
        assert stats["note_rendered"] == 0
        assert stats["send"] == 0
        assert stats["memory"] == 0
        assert stats["plugin_inject"] == [False]

    async def test_preview_reports_greeting_never_sent(self, world: dict[str, Any]) -> None:
        response = await tavern.preview_prompt(
            TavernPreviewRequest(chat_key=T1),
            _current_user=world["tenant"],
        )

        assert response.greeting == {"will_send": False, "text": ""}

    async def test_slots_disabled_keep_preview_text_but_not_rendered(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "PROMPT_ADVANCED_SLOTS_ENABLED", False)

        tenant = await tavern.preview_prompt(TavernPreviewRequest(chat_key=T1), _current_user=world["tenant"])
        platform = await tavern.preview_prompt(TavernPreviewRequest(chat_key=T1), _current_user=world["platform"])

        expected = f"[含平台方设置的后置指令，共 {len('平台角色+人设')} 字]"
        assert tenant.meta["slots"]["enabled"] is False
        assert tenant.meta["slots"]["post_history"]["text"] == ""
        assert tenant.meta["slots_preview"]["post_history"]["text"] == expected
        assert platform.meta["slots"]["post_history"]["text"] == ""
        assert platform.meta["slots_preview"]["post_history"]["text"] == "平台角色+人设"
        assert "平台角色+人设" not in _texts(tenant)


class TestTrialGroupSelection:
    async def test_platform_may_use_any_visible_chat_group(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = _install_model(monkeypatch, ["```python\nprint(1)\n```"])

        response = await tavern.trial_prompt(
            TavernTrialRequest(chat_key=T1, text="你好", model_group="t2chat"),
            _current_user=world["platform"],
        )

        assert response.model_group == "t2chat"
        assert len(calls) == 1

    async def test_platform_without_group_uses_channel_effective_group(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_model(monkeypatch, ["ok"])

        response = await tavern.trial_prompt(
            TavernTrialRequest(chat_key=T1, text="你好"),
            _current_user=world["platform"],
        )

        assert response.model_group == "shared"

    async def test_tenant_cannot_use_platform_group(self, world: dict[str, Any]) -> None:
        with pytest.raises(AppError) as exc:
            await tavern.trial_prompt(
                TavernTrialRequest(chat_key=T1, text="你好", model_group="shared"),
                _current_user=world["tenant"],
            )
        assert exc.value.http_status == 404

    async def test_tenant_cannot_use_another_tenants_group(self, world: dict[str, Any]) -> None:
        with pytest.raises(AppError) as exc:
            await tavern.trial_prompt(
                TavernTrialRequest(chat_key=T1, text="你好", model_group="t2chat"),
                _current_user=world["tenant"],
            )
        assert exc.value.http_status == 404

    async def test_tenant_may_use_own_chat_group(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_model(monkeypatch, ["ok"])

        response = await tavern.trial_prompt(
            TavernTrialRequest(chat_key=T1, text="你好", model_group="t1chat"),
            _current_user=world["tenant"],
        )

        assert response.model_group == "t1chat"

    async def test_tenant_channel_on_platform_group_gets_explanation(
        self,
        world: dict[str, Any],
    ) -> None:
        with pytest.raises(ValidationError, match="频道当前用的是平台方的模型组"):
            await tavern.trial_prompt(
                TavernTrialRequest(chat_key=T1, text="你好"),
                _current_user=world["tenant"],
            )

    async def test_tenant_channel_on_own_group_works(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "USE_MODEL_GROUP", "t1chat")
        _install_model(monkeypatch, ["ok"])

        response = await tavern.trial_prompt(
            TavernTrialRequest(chat_key=T1, text="你好"),
            _current_user=world["tenant"],
        )

        assert response.model_group == "t1chat"

    async def test_tenant_without_any_group_gets_explanation(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "USE_MODEL_GROUP", "shared")

        with pytest.raises(ValidationError, match="你名下还没有模型组"):
            await tavern.trial_prompt(
                TavernTrialRequest(chat_key=T3, text="你好"),
                _current_user=world["tenant3"],
            )

    async def test_non_chat_group_is_rejected(self, world: dict[str, Any]) -> None:
        with pytest.raises(ValidationError, match="chat"):
            await tavern.trial_prompt(
                TavernTrialRequest(chat_key=T1, text="你好", model_group="t1embed"),
                _current_user=world["tenant"],
            )


class TestTrial:
    async def test_one_call_and_no_channel_or_memory_side_effects(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services.agent.templates import history as history_module
        from nekro_agent.services.chat.universal_chat_service import universal_chat_service

        calls = _install_model(monkeypatch, ["```python\nprint(1)\n```"])
        stats = world["counters"]

        async def send(*_args: Any, **_kwargs: Any) -> None:
            stats["send"] += 1

        async def memory(**_kwargs: Any) -> str:
            stats["memory"] += 1
            return ""

        monkeypatch.setattr(universal_chat_service, "send_agent_message", send)
        monkeypatch.setattr(history_module, "_inject_memory_context", memory)
        before_messages = await DBChatMessage.all().count()
        before_details = await DBMemPromptRound.all().count()

        response = await tavern.trial_prompt(
            TavernTrialRequest(chat_key=T1, text="你好", model_group="t1chat"),
            _current_user=world["tenant"],
        )

        assert len(calls) == 1
        assert "print(1)" in response.code
        assert response.code_missing is False
        assert response.usage.total_tokens == 30
        assert response.latency_ms >= 0
        assert stats["send"] == 0
        assert stats["memory"] == 0
        assert await DBChatMessage.all().count() == before_messages
        assert await DBMemPromptRound.all().count() == before_details

    async def test_ledger_row_has_scene_and_instance_but_no_chat_key(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_model(monkeypatch, ["ok"])

        before_quota = await DBChannelQuotaDaily.all().count()
        await tavern.trial_prompt(
            TavernTrialRequest(chat_key=T1, text="你好", model_group="t1chat"),
            _current_user=world["tenant"],
        )
        await flush_llm_usage()

        row = await DBLLMUsageRecord.filter(scene="prompt_trial").order_by("-id").first()
        assert row is not None
        assert (row.adapter_key, row.instance_key) == (ADAPTER, "t1a")
        assert row.tenant_id == 1
        assert row.chat_key == ""
        assert row.model_group_name == "t1chat"
        assert await DBLLMUsageRecord.filter(scene="prompt_trial", chat_key=T1).count() == 0
        assert await DBChannelQuotaDaily.all().count() == before_quota

    async def test_prefill_is_merged_and_state_is_sent(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = _install_model(monkeypatch, ["续写"])

        response = await tavern.trial_prompt(
            TavernTrialRequest(chat_key=T1, text="你好", model_group="t1chat"),
            _current_user=world["tenant"],
        )

        assert response.reply == "平台预填续写"
        assert response.prefill_state == "sent"
        assert _field(calls[0][-1], "role") == "assistant"
        assert _field(calls[0][-1], "content") == "平台预填"

    async def test_prefill_rejection_resends_without_prefill(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = _install_model(monkeypatch, [_PrefillRejected("bad request"), "```python\nprint(2)\n```"])

        response = await tavern.trial_prompt(
            TavernTrialRequest(chat_key=T1, text="你好", model_group="t1chat"),
            _current_user=world["tenant"],
        )

        assert len(calls) == 2
        assert _field(calls[0][-1], "role") == "assistant"
        assert all(str(_field(message, "content")) != "平台预填" for message in calls[1])
        assert len(calls[1]) == len(calls[0]) - 1
        assert response.prefill_state == "retried_without"
        assert "print(2)" in response.code

    async def test_refusal_is_classified_without_code(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_model(monkeypatch, ["Your request was rejected as a result of our safety system."])

        response = await tavern.trial_prompt(
            TavernTrialRequest(chat_key=T1, text="你好", model_group="t1chat"),
            _current_user=world["tenant"],
        )

        assert response.refusal
        assert response.code == ""
        assert response.code_missing is True

    async def test_failure_kind_and_redaction(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MODEL_GROUPS", {**config.MODEL_GROUPS, "t1chat": _group("secret-model")})
        config.MODEL_GROUPS["t1chat"].API_KEY = "sk-secret-value"
        _install_model(monkeypatch, [RuntimeError("boom sk-secret-value 401")])

        response = await tavern.trial_prompt(
            TavernTrialRequest(chat_key=T1, text="你好", model_group="t1chat"),
            _current_user=world["tenant"],
        )

        assert response.error_kind == "other"
        assert "sk-secret-value" not in response.error
        assert "boom" in response.error

    async def test_per_minute_limit_rejects_sixth(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = _install_model(monkeypatch, ["ok"] * 6)

        for index in range(5):
            await tavern.trial_prompt(
                TavernTrialRequest(chat_key=T1, text=f"你好{index}", model_group="t1chat"),
                _current_user=world["tenant"],
            )
        with pytest.raises(ValidationError, match="每分钟"):
            await tavern.trial_prompt(
                TavernTrialRequest(chat_key=T1, text="第六次", model_group="t1chat"),
                _current_user=world["tenant"],
            )

        assert len(calls) == 5

    async def test_daily_limit_counts_ledger_rows(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "TAVERN_TRIAL_DAILY_LIMIT", 2)
        for _ in range(2):
            await DBLLMUsageRecord.create(
                adapter_key=ADAPTER,
                instance_key="t1a",
                tenant_id=1,
                scene="prompt_trial",
                model_group_name="t1chat",
            )
        _install_model(monkeypatch, ["ok"])

        with pytest.raises(ValidationError, match="今天最多试发"):
            await tavern.trial_prompt(
                TavernTrialRequest(chat_key=T1, text="你好", model_group="t1chat"),
                _current_user=world["tenant"],
            )

    async def test_daily_limit_zero_means_unlimited(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "TAVERN_TRIAL_DAILY_LIMIT", 0)
        for _ in range(3):
            await DBLLMUsageRecord.create(
                adapter_key=ADAPTER,
                instance_key="t1a",
                tenant_id=1,
                scene="prompt_trial",
                model_group_name="t1chat",
            )
        _install_model(monkeypatch, ["ok"])

        response = await tavern.trial_prompt(
            TavernTrialRequest(chat_key=T1, text="你好", model_group="t1chat"),
            _current_user=world["tenant"],
        )

        assert response.error == ""

    async def test_empty_channel_sample_is_last_in_model_messages(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = _install_model(monkeypatch, ["ok"])

        await tavern.trial_prompt(
            TavernTrialRequest(chat_key=T1_EMPTY, text="空频道试发", model_group="t1chat"),
            _current_user=world["tenant"],
        )

        history = "\n".join(str(_field(message, "content")) for message in calls[0])
        assert '管理员（试发）" 说: 空频道试发' in history


class TestAudit:
    async def _row(self, *, instance_key: str, field: str) -> DBConfigChangeAudit:
        return await DBConfigChangeAudit.create(
            operator="someone",
            operator_is_platform=instance_key == "",
            layer="system" if instance_key == "" else "instance",
            adapter_key=ADAPTER if instance_key else "",
            instance_key=instance_key,
            chat_key="",
            preset_id=0,
            field=field,
            old_value="len=1 sha=aaa",
            new_value="len=2 sha=bbb",
        )

    async def test_platform_sees_all_rows(self, world: dict[str, Any]) -> None:
        await self._row(instance_key="", field="system.a")
        await self._row(instance_key="t1a", field="instance.a")
        await self._row(instance_key="t2a", field="instance.b")

        response = await tavern.list_tavern_audit(instance_key="", page=1, page_size=20, _current_user=world["platform"])

        assert response.total == 3
        assert {item.instance_key for item in response.items} == {"", "t1a", "t2a"}

    async def test_platform_can_filter_by_instance(self, world: dict[str, Any]) -> None:
        await self._row(instance_key="t1a", field="instance.a")
        await self._row(instance_key="t2a", field="instance.b")

        response = await tavern.list_tavern_audit(
            instance_key="t1a",
            page=1,
            page_size=20,
            _current_user=world["platform"],
        )

        assert response.total == 1
        assert response.items[0].instance_key == "t1a"

    async def test_tenant_sees_only_own_instance_rows(self, world: dict[str, Any]) -> None:
        await self._row(instance_key="", field="system.a")
        own = await self._row(instance_key="t1a", field="instance.a")
        await self._row(instance_key="t2a", field="instance.b")

        response = await tavern.list_tavern_audit(instance_key="", page=1, page_size=20, _current_user=world["tenant"])

        assert response.total == 1
        assert [item.instance_key for item in response.items] == ["t1a"]
        assert response.items[0].old_value == own.old_value

    async def test_tenant_sees_preset_layer_rows_of_own_instance(self, world: dict[str, Any]) -> None:
        # 人设只按实例标识归属，人设层的审计行没有适配器一列；租户照样要看得到自己人设上
        # 后置指令 / 预填的改动，看不到别人实例的。
        for instance_key, field in (("t1a", "preset.post_history"), ("t2a", "preset.prefill")):
            await DBConfigChangeAudit.create(
                operator="someone",
                operator_is_platform=False,
                layer="preset",
                adapter_key="",
                instance_key=instance_key,
                chat_key="",
                preset_id=7,
                field=field,
                old_value="",
                new_value="len=3 sha=ccc",
            )

        response = await tavern.list_tavern_audit(instance_key="", page=1, page_size=20, _current_user=world["tenant"])
        assert [(item.layer, item.instance_key, item.field) for item in response.items] == [
            ("preset", "t1a", "preset.post_history"),
        ]

        filtered = await tavern.list_tavern_audit(
            instance_key="t1a",
            page=1,
            page_size=20,
            _current_user=world["tenant"],
        )
        assert filtered.total == 1

    async def test_tenant_cannot_filter_another_instance(self, world: dict[str, Any]) -> None:
        with pytest.raises(NotFoundError):
            await tavern.list_tavern_audit(instance_key="t2a", _current_user=world["tenant"])

    async def test_pagination_is_descending(self, world: dict[str, Any]) -> None:
        for index in range(3):
            await self._row(instance_key="t1a", field=f"instance.{index}")

        response = await tavern.list_tavern_audit(
            instance_key="t1a",
            page=1,
            page_size=1,
            _current_user=world["tenant"],
        )

        assert response.total == 3
        assert len(response.items) == 1
        assert response.items[0].field == "instance.2"


class TestReadonlyMember:
    async def test_all_three_endpoints_are_forbidden(self, world: dict[str, Any]) -> None:
        calls = [
            tavern.preview_prompt(TavernPreviewRequest(chat_key=T1), _current_user=world["readonly"]),
            tavern.trial_prompt(
                TavernTrialRequest(chat_key=T1, text="你好", model_group="t1chat"),
                _current_user=world["readonly"],
            ),
            tavern.list_tavern_audit(instance_key="", page=1, page_size=20, _current_user=world["readonly"]),
        ]
        for call in calls:
            with pytest.raises(HTTPException) as exc:
                await call
            assert exc.value.status_code == 403
