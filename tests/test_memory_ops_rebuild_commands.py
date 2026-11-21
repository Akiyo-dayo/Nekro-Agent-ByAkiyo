"""运维命令：工作区重建与单频道域重建的交互与结果输出。

命令层只负责调用服务层函数并把进度/结果翻译成人话，所以这里全部用假的服务层替身，
不碰数据库与模型。
"""

from __future__ import annotations

from typing import Any, List

import pytest

from nekro_agent.services.command.built_in import ops
from nekro_agent.services.command.schemas import CommandResponse, CommandResponseStatus
from nekro_agent.services.memory import rebuild as rebuild_module
from nekro_agent.services.memory.rebuild import (
    MemoryRebuildChannelStatusView,
    MemoryRebuildResult,
    MemoryRebuildStatus,
    RebuildEstimate,
)

WORKSPACE_ID = 9404
CHAT_KEY = "onebot_v11-ops-rebuild-group_1"


async def _collect(command: Any, **kwargs: Any) -> List[CommandResponse]:
    return [response async for response in command.execute(context=None, **kwargs)]


def _status(
    *,
    status: str = "completed",
    channels: List[MemoryRebuildChannelStatusView] | None = None,
    failure_reason: str | None = None,
    failure_code: str | None = None,
    processed: int = 12,
) -> MemoryRebuildStatus:
    return MemoryRebuildStatus(
        workspace_id=WORKSPACE_ID,
        job_id="job-1",
        is_running=False,
        status=status,
        failure_reason=failure_reason,
        failure_code=failure_code,
        total_channels=2,
        completed_channels=1,
        total_messages_processed=processed,
        channels=channels or [],
    )


def _channel_view(chat_key: str, *, status: str, last_error: str | None = None) -> MemoryRebuildChannelStatusView:
    return MemoryRebuildChannelStatusView(
        chat_key=chat_key,
        status=status,
        upper_bound_message_db_id=10,
        initial_cursor_db_id=0,
        last_cursor_db_id=5,
        message_count_total=10,
        messages_processed=5,
        completed=status == "completed",
        progress_ratio=0.5,
        last_error=last_error,
    )


class TestMemoryRebuildCommand:
    async def test_requires_confirmation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fail_start(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("没有 -y 不应该启动任务")

        monkeypatch.setattr(rebuild_module, "start_workspace_memory_rebuild", fail_start)

        responses = await _collect(ops.MemoryRebuildCommand(), workspace_id=WORKSPACE_ID, args_str="")

        assert responses[-1].status == CommandResponseStatus.ERROR
        assert "-y" in responses[-1].message

    async def test_rejects_invalid_workspace_id(self) -> None:
        responses = await _collect(ops.MemoryRebuildCommand(), workspace_id=0, args_str="-y")

        assert responses[-1].status == CommandResponseStatus.ERROR
        assert "workspace_id" in responses[-1].message

    async def test_reports_start_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail_start(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("锁被占用")

        monkeypatch.setattr(rebuild_module, "start_workspace_memory_rebuild", fail_start)

        responses = await _collect(ops.MemoryRebuildCommand(), workspace_id=WORKSPACE_ID, args_str="-y")

        assert responses[-1].status == CommandResponseStatus.ERROR
        assert "锁被占用" in responses[-1].message

    async def test_polls_and_reports_failed_channels(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fake_start(_workspace_id: int, *, requested_by: str | None = None) -> Any:
            assert requested_by == "ops"
            return type("Started", (), {"job_id": "job-1", "reused": False, "status": "running"})()

        statuses = [
            _status(status="running"),
            _status(
                status="completed",
                channels=[
                    _channel_view("onebot_v11-ops-rebuild-group_1", status="completed"),
                    _channel_view("onebot_v11-ops-rebuild-group_2", status="failed", last_error="模型超时"),
                ],
            ),
        ]
        calls = {"count": 0}

        async def fake_status(workspace_id: int) -> MemoryRebuildStatus:
            assert workspace_id == WORKSPACE_ID
            index = min(calls["count"], len(statuses) - 1)
            calls["count"] += 1
            return statuses[index]

        monkeypatch.setattr(rebuild_module, "start_workspace_memory_rebuild", fake_start)
        monkeypatch.setattr(rebuild_module, "get_workspace_memory_rebuild_status", fake_status)
        monkeypatch.setattr(ops, "REBUILD_COMMAND_POLL_INTERVAL_SECONDS", 0.0)

        responses = await _collect(ops.MemoryRebuildCommand(), workspace_id=WORKSPACE_ID, args_str="-y")

        assert calls["count"] == 2
        assert responses[0].status == CommandResponseStatus.PROCESSING
        assert "job-1" in responses[0].message
        assert "创建新任务" in responses[0].message
        assert responses[-1].status == CommandResponseStatus.SUCCESS
        assert "已处理消息: 12 条" in responses[-1].message
        assert "成功频道: 1 / 2 个" in responses[-1].message
        assert "失败频道: 1 个" in responses[-1].message
        assert "onebot_v11-ops-rebuild-group_2: 模型超时" in responses[-1].message

    async def test_reports_reused_job(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_start(_workspace_id: int, *, requested_by: str | None = None) -> Any:
            del requested_by
            return type("Started", (), {"job_id": "job-existing", "reused": True, "status": "running"})()

        async def fake_status(_workspace_id: int) -> MemoryRebuildStatus:
            return _status(status="completed")

        monkeypatch.setattr(rebuild_module, "start_workspace_memory_rebuild", fake_start)
        monkeypatch.setattr(rebuild_module, "get_workspace_memory_rebuild_status", fake_status)
        monkeypatch.setattr(ops, "REBUILD_COMMAND_POLL_INTERVAL_SECONDS", 0.0)

        responses = await _collect(ops.MemoryRebuildCommand(), workspace_id=WORKSPACE_ID, args_str="-y")

        assert "job-existing" in responses[0].message
        assert "复用现有任务" in responses[0].message

    async def test_failed_job_reports_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_start(*_args: Any, **_kwargs: Any) -> Any:
            return type("Started", (), {"job_id": "job-1", "reused": False, "status": "running"})()

        async def fake_status(_workspace_id: int) -> MemoryRebuildStatus:
            return _status(status="failed", failure_reason="快照写入失败")

        monkeypatch.setattr(rebuild_module, "start_workspace_memory_rebuild", fake_start)
        monkeypatch.setattr(rebuild_module, "get_workspace_memory_rebuild_status", fake_status)
        monkeypatch.setattr(ops, "REBUILD_COMMAND_POLL_INTERVAL_SECONDS", 0.0)

        responses = await _collect(ops.MemoryRebuildCommand(), workspace_id=WORKSPACE_ID, args_str="-y")

        assert responses[-1].status == CommandResponseStatus.ERROR
        assert "快照写入失败" in responses[-1].message

    async def test_stops_waiting_after_the_time_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_start(*_args: Any, **_kwargs: Any) -> Any:
            return type("Started", (), {"job_id": "job-1", "reused": False, "status": "running"})()

        async def fake_status(_workspace_id: int) -> MemoryRebuildStatus:
            return _status(status="running")

        monkeypatch.setattr(rebuild_module, "start_workspace_memory_rebuild", fake_start)
        monkeypatch.setattr(rebuild_module, "get_workspace_memory_rebuild_status", fake_status)
        monkeypatch.setattr(ops, "REBUILD_COMMAND_POLL_INTERVAL_SECONDS", 0.0)
        monkeypatch.setattr(ops, "REBUILD_COMMAND_MAX_WAIT_SECONDS", 0.0)

        responses = await _collect(ops.MemoryRebuildCommand(), workspace_id=WORKSPACE_ID, args_str="-y")

        assert responses[-1].status == CommandResponseStatus.PROCESSING
        assert "任务仍在后台运行" in responses[-1].message


class TestMemoryRebuildChannelCommand:
    async def test_gate_off_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("nekro_agent.services.memory.feature_flags.is_domain_scope_enabled", lambda: False)

        responses = await _collect(ops.MemoryRebuildChannelCommand(), chat_key=CHAT_KEY, args_str="")

        assert responses[-1].status == CommandResponseStatus.ERROR
        assert "记忆域分区未开启" in responses[-1].message

    async def test_requires_chat_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("nekro_agent.services.memory.feature_flags.is_domain_scope_enabled", lambda: True)

        responses = await _collect(ops.MemoryRebuildChannelCommand(), chat_key="  ", args_str="")

        assert responses[-1].status == CommandResponseStatus.ERROR
        assert "chat_key" in responses[-1].message

    async def test_estimate_only_without_confirmation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("nekro_agent.services.memory.feature_flags.is_domain_scope_enabled", lambda: True)
        calls = {"rebuild": 0}

        async def fake_estimate(chat_key: str) -> RebuildEstimate:
            assert chat_key == CHAT_KEY
            return RebuildEstimate(
                chat_key=chat_key,
                message_count=37,
                estimated_calls=19,
                cutoff="2026-01-01T00:00:00+00:00",
            )

        async def fake_rebuild(_chat_key: str) -> MemoryRebuildResult:
            calls["rebuild"] += 1
            return MemoryRebuildResult()

        monkeypatch.setattr(rebuild_module, "estimate_domain_channel_rebuild", fake_estimate)
        monkeypatch.setattr(rebuild_module, "rebuild_domain_channel", fake_rebuild)

        responses = await _collect(ops.MemoryRebuildChannelCommand(), chat_key=CHAT_KEY, args_str="")

        assert calls["rebuild"] == 0
        assert responses[-1].status == CommandResponseStatus.SUCCESS
        assert "需回放消息: 37 条" in responses[-1].message
        assert "约需沉淀模型调用: 19 次" in responses[-1].message
        assert "确认执行请加 -y" in responses[-1].message

    async def test_executes_with_confirmation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("nekro_agent.services.memory.feature_flags.is_domain_scope_enabled", lambda: True)

        async def fake_estimate(chat_key: str) -> RebuildEstimate:
            return RebuildEstimate(chat_key=chat_key, message_count=3, estimated_calls=2, cutoff=None)

        async def fake_rebuild(chat_key: str) -> MemoryRebuildResult:
            assert chat_key == CHAT_KEY
            return MemoryRebuildResult(
                paragraphs_deleted=4,
                channels_total=1,
                channels_completed=1,
                messages_processed=3,
                episodic_paragraphs_created=2,
            )

        monkeypatch.setattr(rebuild_module, "estimate_domain_channel_rebuild", fake_estimate)
        monkeypatch.setattr(rebuild_module, "rebuild_domain_channel", fake_rebuild)

        responses = await _collect(ops.MemoryRebuildChannelCommand(), chat_key=CHAT_KEY, args_str="-y")

        assert responses[0].status == CommandResponseStatus.PROCESSING
        assert "开始按频道重建域分区记忆" in responses[0].message
        assert responses[-1].status == CommandResponseStatus.SUCCESS
        assert "删除旧段落: 4 条" in responses[-1].message
        assert "已回放消息: 3 条" in responses[-1].message
        assert "新写段落: 2 条" in responses[-1].message
        assert "结果: 完成" in responses[-1].message

    async def test_reports_partial_channel_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("nekro_agent.services.memory.feature_flags.is_domain_scope_enabled", lambda: True)

        async def fake_estimate(chat_key: str) -> RebuildEstimate:
            return RebuildEstimate(chat_key=chat_key, message_count=3, estimated_calls=2, cutoff=None)

        async def fake_rebuild(chat_key: str) -> MemoryRebuildResult:
            return MemoryRebuildResult(
                channels_total=1,
                channels_failed=1,
                failed_channels=[chat_key],
                last_error="写向量失败",
            )

        monkeypatch.setattr(rebuild_module, "estimate_domain_channel_rebuild", fake_estimate)
        monkeypatch.setattr(rebuild_module, "rebuild_domain_channel", fake_rebuild)

        responses = await _collect(ops.MemoryRebuildChannelCommand(), chat_key=CHAT_KEY, args_str="-y")

        assert responses[-1].status == CommandResponseStatus.ERROR
        assert "结果: 失败" in responses[-1].message
        assert f"{CHAT_KEY}: 写向量失败" in responses[-1].message

    async def test_estimate_failure_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("nekro_agent.services.memory.feature_flags.is_domain_scope_enabled", lambda: True)

        async def fail_estimate(_chat_key: str) -> RebuildEstimate:
            raise RuntimeError("频道不存在")

        monkeypatch.setattr(rebuild_module, "estimate_domain_channel_rebuild", fail_estimate)

        responses = await _collect(ops.MemoryRebuildChannelCommand(), chat_key=CHAT_KEY, args_str="")

        assert responses[-1].status == CommandResponseStatus.ERROR
        assert "频道不存在" in responses[-1].message

    async def test_rebuild_failure_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("nekro_agent.services.memory.feature_flags.is_domain_scope_enabled", lambda: True)

        async def fake_estimate(chat_key: str) -> RebuildEstimate:
            return RebuildEstimate(chat_key=chat_key, message_count=1, estimated_calls=1, cutoff=None)

        async def fail_rebuild(_chat_key: str) -> MemoryRebuildResult:
            raise RuntimeError("重建锁被占用")

        monkeypatch.setattr(rebuild_module, "estimate_domain_channel_rebuild", fake_estimate)
        monkeypatch.setattr(rebuild_module, "rebuild_domain_channel", fail_rebuild)

        responses = await _collect(ops.MemoryRebuildChannelCommand(), chat_key=CHAT_KEY, args_str="-y")

        assert responses[-1].status == CommandResponseStatus.ERROR
        assert "重建锁被占用" in responses[-1].message
