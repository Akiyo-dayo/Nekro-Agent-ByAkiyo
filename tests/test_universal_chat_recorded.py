"""通用发送按 `PlatformSendResponse.recorded` 去重

适配器自己已经写进聊天历史时（网页适配器的 `forward_message`），通用发送再
`push_bot_message` 一次就是同一句话在历史里出现两遍；配额用尽的两条通知同理。
"""

from typing import Any, Dict, List

import pytest

from nekro_agent.adapters.interface.schemas.platform import (
    PlatformSendRequest,
    PlatformSendResponse,
    PlatformSendSegment,
    PlatformSendSegmentType,
)
from nekro_agent.services.chat.universal_chat_service import universal_chat_service


class _Recorder:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def push_bot_message(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append({"args": args, "kwargs": kwargs})


class _Adapter:
    def __init__(self, *, recorded: bool) -> None:
        self.recorded = recorded

    async def forward_message(self, request: PlatformSendRequest) -> PlatformSendResponse:
        del request
        return PlatformSendResponse(success=True, message_id="webout_test", recorded=self.recorded)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    from nekro_agent.services import message_service as message_service_mod

    rec = _Recorder()
    monkeypatch.setattr(message_service_mod, "message_service", rec)
    return rec


@pytest.fixture(autouse=True)
def _active_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """`send_agent_message` 的出口闸门：本用例只测记录去重，实例一律当作在跑"""
    from nekro_agent.services.chat import universal_chat_service as ucs_mod

    async def _ok(_chat_key: str) -> None:
        return None

    monkeypatch.setattr(ucs_mod.UniversalChatService, "_assert_instance_active", staticmethod(_ok))


async def test_recorded_response_skips_chat_history_write(recorder: _Recorder) -> None:
    await universal_chat_service.send_agent_message(
        chat_key="web-session_test",
        messages="hello",
        adapter=_Adapter(recorded=True),  # type: ignore[arg-type]
        record=True,
    )

    assert recorder.calls == []


async def test_unrecorded_response_still_writes_chat_history(recorder: _Recorder) -> None:
    await universal_chat_service.send_agent_message(
        chat_key="web-session_test",
        messages="hello",
        adapter=_Adapter(recorded=False),  # type: ignore[arg-type]
        record=True,
    )

    assert len(recorder.calls) == 1
    assert recorder.calls[0]["args"][0] == "web-session_test"
    assert recorder.calls[0]["kwargs"]["normalize_at_markup"] is False


async def test_record_false_writes_nothing_regardless(recorder: _Recorder) -> None:
    await universal_chat_service.send_agent_message(
        chat_key="web-session_test",
        messages="hello",
        adapter=_Adapter(recorded=False),  # type: ignore[arg-type]
        record=False,
    )

    assert recorder.calls == []


async def test_platform_send_response_defaults_to_not_recorded() -> None:
    assert PlatformSendResponse(success=True).recorded is False


async def test_segments_are_still_forwarded_to_adapter() -> None:
    """`recorded` 只影响"要不要再写一遍历史"，不影响发送本身"""
    seen: List[PlatformSendRequest] = []

    class _CaptureAdapter:
        async def forward_message(self, request: PlatformSendRequest) -> PlatformSendResponse:
            seen.append(request)
            return PlatformSendResponse(success=True, message_id="m1", recorded=True)

    await universal_chat_service.send_agent_message(
        chat_key="web-session_test",
        messages="hello",
        adapter=_CaptureAdapter(),  # type: ignore[arg-type]
        record=True,
    )

    assert len(seen) == 1
    assert seen[0].segments == [PlatformSendSegment(type=PlatformSendSegmentType.TEXT, content="hello")]
