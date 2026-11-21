"""第 1 层出站过滤：通用发送与命令输出

打桩方式与 `test_universal_chat_recorded.py` 一致：实例闸门放行、适配器只记录收到的请求、
聊天记录服务只记录调用。过滤设置直接替换 `service.settings_for_chat`，不走数据库。
"""

from collections import Counter
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from nekro_agent.adapters.interface.base import BaseAdapter
from nekro_agent.adapters.interface.schemas.platform import (
    PlatformSendRequest,
    PlatformSendResponse,
    PlatformSendSegment,
    PlatformSendSegmentType,
)
from nekro_agent.core.config import CoreConfig
from nekro_agent.schemas.agent_message import AgentMessageSegment, AgentMessageSegmentType
from nekro_agent.services.chat import universal_chat_service as ucs_mod
from nekro_agent.services.chat.universal_chat_service import universal_chat_service
from nekro_agent.services.command.schemas import (
    CommandOutputSegment,
    CommandOutputSegmentType,
    CommandResponse,
    CommandResponseStatus,
)
from nekro_agent.services.output_filter import service, stats
from nekro_agent.services.output_filter.rules import compile_rules
from nekro_agent.services.output_filter.service import FilterSettings, is_outbound_filtered, settings_from_config


class _Recorder:
    def __init__(self) -> None:
        self.calls: List[dict[str, Any]] = []

    async def push_bot_message(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append({"args": args, "kwargs": kwargs})


class _CaptureAdapter:
    def __init__(self, *, recorded: bool = False) -> None:
        self.recorded = recorded
        self.requests: List[PlatformSendRequest] = []
        self.filtered_flags: List[bool] = []

    async def forward_message(self, request: PlatformSendRequest) -> PlatformSendResponse:
        self.requests.append(request)
        self.filtered_flags.append(is_outbound_filtered())
        return PlatformSendResponse(success=True, message_id="out1", recorded=self.recorded)


def _settings(
    *,
    mode: str = "fragment",
    replacement: str = "**Filter**",
    words: str = "bad",
    regex: str = "",
) -> FilterSettings:
    return FilterSettings(
        mode=mode,  # type: ignore[arg-type]
        replacement=replacement,
        rules=compile_rules(words, regex),
    )


@pytest.fixture(autouse=True)
def _active_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok(_chat_key: str) -> None:
        return None

    monkeypatch.setattr(ucs_mod.UniversalChatService, "_assert_instance_active", staticmethod(_ok))


@pytest.fixture(autouse=True)
def _fake_file_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    """图片段只用于证明 whole 模式会连它一起丢掉，不真的去读文件"""

    async def _fake(
        self: Any,
        agent_message: AgentMessageSegment,
        chat_key: str,
        ctx: Any,
        content: str,
        segment_type: PlatformSendSegmentType,
    ) -> List[PlatformSendSegment]:
        del self, agent_message, chat_key, ctx
        return [PlatformSendSegment(type=segment_type, content=content)]

    monkeypatch.setattr(ucs_mod.UniversalChatService, "_preprocess_file_segment", _fake)


@pytest.fixture(autouse=True)
def _recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    from nekro_agent.services import message_service as message_service_mod

    recorder = _Recorder()
    monkeypatch.setattr(message_service_mod, "message_service", recorder)
    return recorder


@pytest.fixture(autouse=True)
def _no_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(_chat_key: str, _hits: Counter[str]) -> None:
        return None

    monkeypatch.setattr(service, "record_hits_for_chat", _noop)


@pytest.fixture
def use_settings(monkeypatch: pytest.MonkeyPatch):
    def install(settings: Optional[FilterSettings]) -> None:
        async def _fake(_chat_key: str) -> Optional[FilterSettings]:
            return settings

        monkeypatch.setattr(service, "settings_for_chat", _fake)

    return install


async def _send(
    adapter: _CaptureAdapter,
    content: str,
    *,
    record: bool = True,
) -> None:
    await universal_chat_service.send_agent_message(
        chat_key="onebot_v11-inst1-group_12345",
        messages=content,
        adapter=adapter,  # type: ignore[arg-type]
        record=record,
    )


class TestNoFilter:
    async def test_filter_off_forwards_text_verbatim(self, use_settings, _recorder: _Recorder) -> None:
        use_settings(None)
        adapter = _CaptureAdapter()

        await _send(adapter, "原样发出的 bad 文字")

        assert adapter.requests[0].segments[0].content == "原样发出的 bad 文字"
        assert _recorder.calls[0]["args"][1][0].content == "原样发出的 bad 文字"

    async def test_filter_on_but_no_hit_forwards_text_verbatim(self, use_settings, _recorder: _Recorder) -> None:
        use_settings(_settings(words="bad"))
        adapter = _CaptureAdapter()

        await _send(adapter, "这句话很干净")

        assert adapter.requests[0].segments[0].content == "这句话很干净"
        assert _recorder.calls[0]["args"][1][0].content == "这句话很干净"


class TestFragmentMode:
    async def test_adapter_and_history_both_get_replaced_text(self, use_settings, _recorder: _Recorder) -> None:
        use_settings(_settings(words="bad"))
        adapter = _CaptureAdapter()

        await _send(adapter, "你好 bad 呀")

        assert adapter.requests[0].segments[0].content == "你好 **Filter** 呀"
        assert _recorder.calls[0]["args"][1][0].content == "你好 **Filter** 呀"

    async def test_custom_replacement_takes_effect(self, use_settings) -> None:
        use_settings(_settings(words="bad", replacement="[已屏蔽]"))
        adapter = _CaptureAdapter()

        await _send(adapter, "bad")

        assert adapter.requests[0].segments[0].content == "[已屏蔽]"

    def test_blank_replacement_falls_back_to_default(self) -> None:
        cfg = CoreConfig(
            OUTPUT_FILTER_ENABLE=True,
            OUTPUT_FILTER_WORDS="bad",
            OUTPUT_FILTER_REPLACEMENT="   ",
        )

        settings = settings_from_config(cfg)

        assert settings is not None
        assert settings.replacement == "**Filter**"


class TestWholeMode:
    async def test_only_one_text_segment_is_sent_and_recorded(self, use_settings, _recorder: _Recorder) -> None:
        use_settings(_settings(mode="whole", words="bad"))
        adapter = _CaptureAdapter()
        messages = [
            AgentMessageSegment(type=AgentMessageSegmentType.TEXT, content="bad"),
            AgentMessageSegment(type=AgentMessageSegmentType.IMAGE, content="shared/pic.png"),
        ]

        await universal_chat_service.send_agent_message(
            chat_key="onebot_v11-inst1-group_12345",
            messages=messages,
            adapter=adapter,  # type: ignore[arg-type]
            record=True,
        )

        assert adapter.requests[0].segments == [
            PlatformSendSegment(type=PlatformSendSegmentType.TEXT, content="**Filter**"),
        ]
        recorded_messages = _recorder.calls[0]["args"][1]
        assert len(recorded_messages) == 1
        assert recorded_messages[0].content == "**Filter**"


class TestOutboundContext:
    async def test_flag_is_true_during_forward_and_false_after(self, use_settings) -> None:
        use_settings(_settings())
        adapter = _CaptureAdapter()

        assert is_outbound_filtered() is False
        await _send(adapter, "hello")

        assert adapter.filtered_flags == [True]
        assert is_outbound_filtered() is False


class TestEngineFailure:
    async def test_engine_exception_falls_back_to_whole_replacement(self, use_settings, monkeypatch, _recorder) -> None:
        use_settings(_settings(mode="fragment", words="bad"))
        adapter = _CaptureAdapter()

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("engine down")

        monkeypatch.setattr(service, "filter_text", _boom)

        await _send(adapter, "bad")

        assert adapter.requests[0].segments == [
            PlatformSendSegment(type=PlatformSendSegmentType.TEXT, content="**Filter**"),
        ]
        assert _recorder.calls[0]["args"][1][0].content == "**Filter**"


class TestCommandOutputPath:
    async def test_platform_segments_branch_is_filtered(self, use_settings, monkeypatch) -> None:
        use_settings(_settings(words="bad"))
        adapter = _CaptureAdapter()

        import nekro_agent.services.command.output as command_output

        async def _segments(_response: CommandResponse, _chat_key: str) -> List[PlatformSendSegment]:
            return [PlatformSendSegment(type=PlatformSendSegmentType.TEXT, content="命令输出 bad")]

        async def _messages(_response: CommandResponse, _chat_key: str) -> Any:
            return None

        monkeypatch.setattr(command_output, "build_command_output_platform_segments", _segments)
        monkeypatch.setattr(command_output, "build_command_output_messages", _messages)

        adapter.config = SimpleNamespace(COMMAND_ENHANCED_OUTPUT=False)  # type: ignore[attr-defined]
        response = CommandResponse(
            status=CommandResponseStatus.SUCCESS,
            message="",
            output_segments=[CommandOutputSegment(type=CommandOutputSegmentType.TEXT, text="命令输出 bad")],
        )

        await BaseAdapter._send_command_response(adapter, "onebot_v11-inst1-group_12345", response)  # type: ignore[arg-type]

        assert len(adapter.requests) == 1
        assert adapter.requests[0].segments[0].content == "命令输出 **Filter**"
        assert adapter.filtered_flags == [True]


class TestHitStats:
    async def test_hits_are_persisted_without_message_text(self, use_settings, monkeypatch, tmp_path) -> None:
        use_settings(_settings(words="bad"))
        hits_path = tmp_path / "output_filter" / "hits.json"
        monkeypatch.setattr(stats, "HITS_PATH", hits_path)

        async def _record(chat_key: str, hits: Counter[str]) -> None:
            del chat_key
            await stats.record_hits_for_instance("onebot_v11", "inst1", hits)

        monkeypatch.setattr(service, "record_hits_for_chat", _record)
        adapter = _CaptureAdapter()

        await _send(adapter, "开场白 bad 结尾")

        snapshot = {item["instance_key"]: item for item in stats.hit_store.snapshot()}
        assert snapshot["inst1"]["adapter_key"] == "onebot_v11"
        rules = {rule["rule"]: rule for rule in snapshot["inst1"]["rules"]}
        assert rules["bad"]["kind"] == "word"
        assert rules["bad"]["count"] == 1

        raw = hits_path.read_text(encoding="utf-8")
        assert "bad" in raw
        assert "开场白" not in raw
        assert "结尾" not in raw
