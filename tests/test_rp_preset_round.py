"""预设接入组装层：没选时逐字不变，选中时替换人设节、后置段与深度段"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nekro_agent.core.config import config
from nekro_agent.services.agent import round_prompt as round_prompt_module
from nekro_agent.services.tavern import runtime as tavern_runtime
from nekro_agent.services.tavern.rp_preset import parse_preset, segment_as_dict
from nekro_agent.services.tavern.runtime import TavernRound
from nekro_agent.services.tavern.slots import AdvancedSlots, SlotText
from tests.rp_preset_fixtures import wrapped_preset
from tests.test_tavern_golden import (
    CHAT_KEY,
    CODE,
    _baseline,
    _cleanup,
    _install_fakes,
)


def _segments() -> list[dict[str, Any]]:
    segments, _rules, _notes = parse_preset(wrapped_preset())
    return [segment_as_dict(segment) for segment in segments]


def _round(*, enabled: bool = True, slots_enabled: bool = True) -> TavernRound:
    empty = SlotText(text="")
    slots = AdvancedSlots(
        enabled=slots_enabled,
        post_history=SlotText(text="HOST_POST {{original}}"),
        auxiliary=empty,
        prefill=empty,
    )
    return TavernRound(
        enabled=enabled,
        card=SimpleNamespace(
            source_fields={
                "system": "",
                "description": "CARD_DESCRIPTION",
                "personality": "CARD_PERSONALITY",
                "scenario": "CARD_SCENARIO",
                "mes_example": "CARD_EXAMPLES",
            },
        ),
        macro=None,
        persona_content="默认人设正文",
        depth_prompt="",
        depth=0,
        auxiliary="",
        post_history="HOST_POST {{original}}" if slots_enabled else "",
        prefill=None,
        slots=slots,
        rp_preset=SimpleNamespace(id=7, name="测试预设", segments=_segments())
        if enabled and slots_enabled
        else None,
    )


def _install_round(monkeypatch: pytest.MonkeyPatch, round_: TavernRound) -> None:
    async def fake_resolve(code: str, *_args: Any, **_kwargs: Any) -> TavernRound:
        tavern_runtime._store_round(code, round_)  # noqa: SLF001
        return round_

    monkeypatch.setattr(tavern_runtime, "resolve_tavern_round", fake_resolve)


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    tavern_runtime._rounds.clear()  # noqa: SLF001
    yield
    tavern_runtime._rounds.clear()  # noqa: SLF001


async def test_without_a_preset_the_round_is_byte_identical_to_the_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)
    channel, ctx = _install_fakes(monkeypatch)
    baseline = await _baseline()
    _cleanup()

    # 基准用的是频道当前人设；这里取同一份正文，避免在用例里写死一个人设名。
    no_preset_round = _round(enabled=False, slots_enabled=False)
    no_preset_round.persona_content = (await channel.get_preset()).content
    _install_round(monkeypatch, no_preset_round)
    built = await round_prompt_module.build_round_prompt(
        chat_key=CHAT_KEY,
        db_chat_channel=channel,  # type: ignore[arg-type]
        ctx=ctx,  # type: ignore[arg-type]
        one_time_code=CODE,
        trigger_message=SimpleNamespace(sender_nickname="某人"),
    )

    assert [message.to_dict() for message in built.messages] == baseline
    assert "CARD_DESCRIPTION" not in str(built.messages[0].to_dict()["content"])


async def test_active_preset_replaces_the_persona_section_and_appends_the_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)
    channel, ctx = _install_fakes(monkeypatch)
    _install_round(monkeypatch, _round())

    async def no_greeting(**_kwargs: Any) -> dict[str, Any]:
        return {"will_send": False, "text": ""}

    monkeypatch.setattr(round_prompt_module.tavern_greeting, "preview_greeting", no_greeting)

    built = await round_prompt_module.build_round_prompt(
        chat_key=CHAT_KEY,
        db_chat_channel=channel,  # type: ignore[arg-type]
        ctx=ctx,  # type: ignore[arg-type]
        one_time_code=CODE,
        trigger_message=None,
        dry_run=True,
    )

    system_text = str(built.messages[0].to_dict()["content"])
    history_text = str(built.messages[-1].to_dict()["content"])

    # 人设节被整块替换，卡字段、预设段落与世界书都在块里；旧人设正文不再出现。
    assert "CARD_DESCRIPTION" in system_text
    assert "CARD_PERSONALITY" in system_text
    assert "<characters>" in system_text
    assert "人设后世界书" in system_text
    assert "默认人设正文" not in system_text

    # 聊天记录之后：jailbreak 被槽位覆盖，深度段插进历史，预填只报告不发送。
    assert "HOST_POST" in history_text
    assert "WRAPPED_JAIL" in history_text  # 槽位里的 {{original}} 换回了预设原文
    assert "DEPTH_MARKER" in history_text
    assert history_text.index("DEPTH_MARKER") < history_text.index("[m2]")
    all_text = "\n".join(str(message.to_dict()["content"]) for message in built.messages)
    assert "PREFILL_MARKER" not in all_text

    assert built.meta["rp_preset"]["id"] == 7
    assert built.meta["rp_preset"]["name"] == "测试预设"
    assert built.meta["rp_preset"]["prefill_text"] == "PREFILL_MARKER"
    assert built.meta["rp_preset"]["prefill_sent"] is False
    used = {item["identifier"] for item in built.meta["rp_preset"]["used"]}
    assert {"main", "charDescription", "depthInstruction"} <= used
    assert "rp_preset_inactive_reason" not in built.meta


@pytest.mark.parametrize(
    ("enabled", "slots_enabled", "reason"),
    [
        (False, True, "tavern_off"),
        (True, False, "slots_off"),
    ],
)
async def test_inactive_override_reports_the_reason(
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    slots_enabled: bool,
    reason: str,
) -> None:
    monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)
    channel, ctx = _install_fakes(monkeypatch)
    _install_round(monkeypatch, _round(enabled=enabled, slots_enabled=slots_enabled))
    override = SimpleNamespace(id=7, name="测试预设", segments=_segments())

    async def no_greeting(**_kwargs: Any) -> dict[str, Any]:
        return {"will_send": False, "text": ""}

    monkeypatch.setattr(round_prompt_module.tavern_greeting, "preview_greeting", no_greeting)

    built = await round_prompt_module.build_round_prompt(
        chat_key=CHAT_KEY,
        db_chat_channel=channel,  # type: ignore[arg-type]
        ctx=ctx,  # type: ignore[arg-type]
        one_time_code=CODE,
        trigger_message=None,
        dry_run=True,
        rp_preset_override=override,
    )

    assert built.meta["rp_preset_inactive_reason"] == reason
    assert "rp_preset" not in built.meta
    assert "CARD_DESCRIPTION" not in str(built.messages[0].to_dict()["content"])
