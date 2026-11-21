"""节日邮件的正文生成：提示词、解析、输出过滤与逐实例的失败隔离

## 这里钉什么

1. **提示词里只有人设、节日名与说明**：不带聊天内容、不带收件人。正文会被发给名单上的每一个
   人，模型从聊天里读到的私事不该出现在给别人的祝福邮件里；带收件人也会让同一批信各不相同。
2. **模型输出不可靠**：不是 JSON、字段为空、超长，都要退化成一个能发的邮件，而不是让整批消失。
3. **输出过滤与出站同一套规则**：`fragment` 换片段，`whole` 命中一处就整批不发（整封换成替换语
   没有意义），引擎报错按 `whole` 处理。
4. **一个实例失败不拖累别的实例**：模型调用、过滤、入队各自 try，只跳过出事的那个实例。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config as core_config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.services.output_filter.service import FilterSettings
from plugins.builtin.festival_greeting import mail as festival_mail

ADAPTER = "onebot_v11"
INSTANCE_A = "inst1"
INSTANCE_B = "inst2"

FESTIVAL = "中秋"
DESC = "农历八月十五"


def recipient(address: str = "alice@example.test") -> Any:
    from nekro_agent.services.email_outbox import FestivalRecipient

    return FestivalRecipient(
        platform_userid="".join(("1234", "567")),
        to_address=address,
        nickname="小明",
        source="private",
    )


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        for instance_key, tenant_id in ((INSTANCE_A, 1), (INSTANCE_B, 2)):
            await DBAdapterInstance.create(
                adapter_key=ADAPTER,
                instance_key=instance_key,
                display_name=instance_key,
                status="online",
                provider="remote_onebot",
                tenant_id=tenant_id,
                metadata_json="",
            )
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> dict:
    """把模型调用、人设、模型组三处换成可控的替身；返回一个可读的调用记录"""
    seen: dict[str, Any] = {"calls": []}

    async def fake_preset(_adapter_key: str, _instance_key: str) -> Any:
        return SimpleNamespace(content="你是一个温柔的人。")

    group = SimpleNamespace(
        CHAT_MODEL="chat-model",
        BASE_URL="https://api.example.test/v1",
        API_KEY="sk-x",
        TOKEN_INPUT_RATE=1.0,
        TOKEN_COMPLETION_RATE=2.0,
        MODEL_PRICE_RATE=1.0,
    )
    effective = SimpleNamespace(USE_MODEL_GROUP="default", MODEL_GROUPS={"default": group})

    async def fake_response(**kwargs: Any) -> Any:
        seen["calls"].append(kwargs)
        text = seen.get("reply", json.dumps({"subject": "中秋快乐", "body": "祝你团圆。"}, ensure_ascii=False))
        return SimpleNamespace(response_content=text)

    monkeypatch.setattr("nekro_agent.services.preset_service.resolve_instance_preset", fake_preset)
    monkeypatch.setattr(festival_mail.config_resolver, "get_instance_effective_config", lambda *_a, **_k: effective)
    monkeypatch.setattr(festival_mail, "gen_openai_chat_response", fake_response)
    monkeypatch.setattr(festival_mail, "settings_for_instance", lambda *_a, **_k: None)
    return seen


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch) -> dict:
    """队列那一侧换成记录调用的替身：本文件只测「正文怎么来的」"""
    seen: dict[str, Any] = {"enqueued": []}

    async def ready(adapter_key: str, instance_key: str) -> Any:
        return SimpleNamespace(kind="platform", username="ops@example.test", ref="platform:abc")

    async def recipients(_adapter_key: str, _instance_key: str) -> list:
        return [recipient()]

    async def enqueue(**kwargs: Any) -> int:
        seen["enqueued"].append(kwargs)
        return len(kwargs["recipients"])

    monkeypatch.setattr(festival_mail.email_outbox, "instance_festival_mail_ready", ready)
    monkeypatch.setattr(festival_mail.email_outbox, "festival_recipients", recipients)
    monkeypatch.setattr(festival_mail.email_outbox, "enqueue_festival_batch", enqueue)
    return seen


class TestThePrompt:
    def test_it_carries_only_the_festival_name_and_description(self) -> None:
        prompt = festival_mail._build_prompt(FESTIVAL, DESC)  # noqa: SLF001

        assert FESTIVAL in prompt
        assert DESC in prompt
        assert "JSON" in prompt
        assert "不超过 300 字" in prompt
        assert "不要提到具体的人" in prompt

    def test_an_empty_description_gets_a_neutral_stand_in(self) -> None:
        prompt = festival_mail._build_prompt(FESTIVAL, "  ")  # noqa: SLF001

        assert "值得祝福的日子" in prompt

    def test_the_subject_limit_is_stated_once(self) -> None:
        prompt = festival_mail._build_prompt(FESTIVAL, DESC)  # noqa: SLF001

        assert f"不超过 {festival_mail.SUBJECT_PROMPT_LIMIT} 字" in prompt


class TestParsingTheModelOutput:
    def test_a_plain_json_object_is_used_as_is(self) -> None:
        subject, body = festival_mail._parse_generated(  # noqa: SLF001
            json.dumps({"subject": "中秋快乐", "body": "祝你团圆。"}, ensure_ascii=False),
            FESTIVAL,
        )

        assert (subject, body) == ("中秋快乐", "祝你团圆。")

    def test_a_fenced_json_block_is_unwrapped(self) -> None:
        text = "```json\n" + json.dumps({"subject": "标题", "body": "正文"}, ensure_ascii=False) + "\n```"

        subject, body = festival_mail._parse_generated(text, FESTIVAL)  # noqa: SLF001

        assert (subject, body) == ("标题", "正文")

    def test_plain_text_becomes_the_body_with_a_fallback_subject(self) -> None:
        subject, body = festival_mail._parse_generated("直接写了一段祝福", FESTIVAL)  # noqa: SLF001

        assert subject == f"{FESTIVAL}祝福"
        assert body == "直接写了一段祝福"

    def test_missing_fields_are_filled_separately(self) -> None:
        subject, body = festival_mail._parse_generated(json.dumps({"body": "只有正文"}), FESTIVAL)  # noqa: SLF001

        assert subject == f"{FESTIVAL}祝福"
        assert body == "只有正文"

    def test_over_long_fields_are_truncated(self) -> None:
        text = json.dumps({"subject": "标" * 100, "body": "文" * 1000}, ensure_ascii=False)

        subject, body = festival_mail._parse_generated(text, FESTIVAL)  # noqa: SLF001

        assert len(subject) == festival_mail.SUBJECT_MAX_CHARS
        assert len(body) == festival_mail.BODY_MAX_CHARS

    def test_an_empty_reply_is_still_a_sendable_mail(self) -> None:
        subject, body = festival_mail._parse_generated("", FESTIVAL)  # noqa: SLF001

        assert subject == f"{FESTIVAL}祝福"
        assert body == ""


class TestTheOutputFilter:
    def _settings(self, mode: str, replacement: str = "**Filter**") -> FilterSettings:
        from nekro_agent.services.output_filter.rules import compile_rules

        return FilterSettings(
            mode=mode,  # type: ignore[arg-type]
            replacement=replacement,
            rules=compile_rules("违禁词", ""),
        )

    def test_no_settings_means_no_filtering(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(festival_mail, "settings_for_instance", lambda *_a, **_k: None)

        assert festival_mail._apply_filter(ADAPTER, INSTANCE_A, "主题", "违禁词正文") == ("主题", "违禁词正文")  # noqa: SLF001

    def test_fragment_mode_replaces_the_hit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(festival_mail, "settings_for_instance", lambda *_a, **_k: self._settings("fragment"))

        result = festival_mail._apply_filter(ADAPTER, INSTANCE_A, "违禁词主题", "正文")  # noqa: SLF001

        assert result is not None
        subject, body = result
        assert "违禁词" not in subject
        assert "**Filter**" in subject
        assert body == "正文"

    def test_whole_mode_drops_the_whole_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """整封换成替换语没有意义：命中一处就整批不发"""
        monkeypatch.setattr(festival_mail, "settings_for_instance", lambda *_a, **_k: self._settings("whole"))

        assert festival_mail._apply_filter(ADAPTER, INSTANCE_A, "主题", "违禁词正文") is None  # noqa: SLF001

    def test_whole_mode_passes_a_clean_mail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(festival_mail, "settings_for_instance", lambda *_a, **_k: self._settings("whole"))

        assert festival_mail._apply_filter(ADAPTER, INSTANCE_A, "主题", "正文") == ("主题", "正文")  # noqa: SLF001

    def test_an_engine_error_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(festival_mail, "settings_for_instance", lambda *_a, **_k: self._settings("fragment"))

        def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("engine broke")

        monkeypatch.setattr(festival_mail, "filter_text", boom)

        assert festival_mail._apply_filter(ADAPTER, INSTANCE_A, "主题", "正文") is None  # noqa: SLF001


class TestEnqueueing:
    async def test_one_instance_gets_one_batch(self, db, model, outbox) -> None:
        await festival_mail.enqueue_festival_mail(FESTIVAL, DESC)

        assert len(outbox["enqueued"]) == 2
        first = outbox["enqueued"][0]
        assert first["adapter_key"] == ADAPTER
        assert first["instance_key"] == INSTANCE_A
        assert first["festival_name"] == FESTIVAL
        assert first["subject"] == "中秋快乐"
        assert first["body"] == "祝你团圆。"
        assert first["recipients"]

    async def test_the_model_is_called_once_per_instance(self, db, model, outbox) -> None:
        await festival_mail.enqueue_festival_mail(FESTIVAL, DESC)

        assert len(model["calls"]) == 2

    async def test_the_prompt_carries_the_preset_and_the_model_group(self, db, model, outbox) -> None:
        await festival_mail.enqueue_festival_mail(FESTIVAL, DESC)

        messages = model["calls"][0]["messages"]
        assert messages[0]["role"] == "system"
        assert "你是一个温柔的人。" in messages[0]["content"]
        assert messages[1]["role"] == "user"
        assert FESTIVAL in messages[1]["content"]

    async def test_the_global_switch_stops_everything(self, db, model, outbox, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(core_config, "EMAIL_FESTIVAL_MAIL_ENABLED", False)

        await festival_mail.enqueue_festival_mail(FESTIVAL, DESC)

        assert outbox["enqueued"] == []
        assert model["calls"] == []

    async def test_an_instance_without_a_sender_is_skipped(self, db, model, outbox, monkeypatch: pytest.MonkeyPatch) -> None:
        async def ready(adapter_key: str, instance_key: str) -> Optional[Any]:
            return None if instance_key == INSTANCE_A else SimpleNamespace(kind="platform", username="ops@example.test")

        monkeypatch.setattr(festival_mail.email_outbox, "instance_festival_mail_ready", ready)

        await festival_mail.enqueue_festival_mail(FESTIVAL, DESC)

        assert [entry["instance_key"] for entry in outbox["enqueued"]] == [INSTANCE_B]

    async def test_an_empty_recipient_list_is_skipped(self, db, model, outbox, monkeypatch: pytest.MonkeyPatch) -> None:
        async def recipients(adapter_key: str, instance_key: str) -> list:
            return [] if instance_key == INSTANCE_A else [recipient()]

        monkeypatch.setattr(festival_mail.email_outbox, "festival_recipients", recipients)

        await festival_mail.enqueue_festival_mail(FESTIVAL, DESC)

        assert [entry["instance_key"] for entry in outbox["enqueued"]] == [INSTANCE_B]

    async def test_a_failing_model_skips_only_that_instance(
        self,
        db,
        model,
        outbox,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """一个实例的模型调用失败不该把另一个实例的祝福一起拖没"""
        original = festival_mail.gen_openai_chat_response
        calls = {"n": 0}

        async def flaky(**kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("upstream 502")
            return await original(**kwargs)

        monkeypatch.setattr(festival_mail, "gen_openai_chat_response", flaky)

        await festival_mail.enqueue_festival_mail(FESTIVAL, DESC)

        assert [entry["instance_key"] for entry in outbox["enqueued"]] == [INSTANCE_B]

    async def test_a_missing_model_group_skips_that_instance(
        self,
        db,
        model,
        outbox,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        effective = SimpleNamespace(USE_MODEL_GROUP="ghost", MODEL_GROUPS={})
        monkeypatch.setattr(festival_mail.config_resolver, "get_instance_effective_config", lambda *_a, **_k: effective)

        await festival_mail.enqueue_festival_mail(FESTIVAL, DESC)

        assert outbox["enqueued"] == []

    async def test_a_whole_mode_hit_skips_the_instance(
        self,
        db,
        model,
        outbox,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services.output_filter.rules import compile_rules

        settings = FilterSettings(
            mode="whole",
            replacement="**Filter**",
            rules=compile_rules("中秋", ""),
        )
        monkeypatch.setattr(festival_mail, "settings_for_instance", lambda *_a, **_k: settings)

        await festival_mail.enqueue_festival_mail(FESTIVAL, DESC)

        assert outbox["enqueued"] == []

    async def test_a_tombstoned_instance_is_not_visited(
        self,
        db,
        model,
        outbox,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.models.db_adapter_instance import TOMBSTONE_STATUS

        await DBAdapterInstance.filter(instance_key=INSTANCE_B).update(status=TOMBSTONE_STATUS)

        await festival_mail.enqueue_festival_mail(FESTIVAL, DESC)

        assert [entry["instance_key"] for entry in outbox["enqueued"]] == [INSTANCE_A]
