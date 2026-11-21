"""SSH 网关值守的事后摘要

## 这条测试钉的是什么

- 什么时候发：带值守覆盖的租约结束、而且用值守行连过（放行的 `connect`）才发；没连、只用
  严格行连、不带值守覆盖的租约都不发。
- 正文：只取宿主记录；命令带退出码、逐条截断、列表按篇幅收住；总长封顶；来自模型与容器的
  文字压成单行加引号，换行伪造不出一行宿主文字；CC 的自述标明未经核实；不含凭据。
- 发到哪：配了会话列表就发列表；没配就发所有者名单里同一适配器的每个账号的私聊（先落频道行）；
  找不到目标只记审计；目标实例归租户的不发。
- 失败：每个目标只试一次，失败写 `report` 审计（`allowed=false`），正文的那条审计照写。
- 租约上的任务正文与 CC 结果只在带值守覆盖的租约上存。

环境是内存 SQLite；发送打桩，记下发给了谁、发了什么。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_ws_ssh_audit import DBWsSshAudit
from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
from nekro_agent.models.db_ws_ssh_runbook_entry import DBWsSshRunbookEntry
from nekro_agent.services.chat.universal_chat_service import universal_chat_service
from nekro_agent.services.ssh_gateway import constants as C
from nekro_agent.services.ssh_gateway import duty_report
from nekro_agent.services.ssh_gateway import lease as lease_module
from nekro_agent.services.ssh_gateway.authz import Coverage
from nekro_agent.services.ssh_gateway.duty_report import ReportFacts, build_summary
from nekro_agent.services.ssh_gateway.lease import SshLease, create_lease, end_lease, reset_leases
from nekro_agent.services.ssh_gateway.secrets import encrypt_secret
from nekro_agent.tools import credential_cipher

ADAPTER = "onebot_v11"
INSTANCE = "inst1"
OWNER = "10001"
OWNER_2 = "10003"
STRANGER = "20002"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_7"
OWNER_CHAT = f"{ADAPTER}-{INSTANCE}-private_{OWNER}"
OWNER_2_CHAT = f"{ADAPTER}-{INSTANCE}-private_{OWNER_2}"
KEY = "ssh-report-" + "key-A"
PASSWORD = "pw-" + "report-1"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def cipher_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY)
    credential_cipher.reset_credential_cipher_cache()
    try:
        yield
    finally:
        credential_cipher.reset_credential_cipher_cache()


@pytest.fixture(autouse=True)
def gateway_on(monkeypatch: pytest.MonkeyPatch):
    reset_leases()
    monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
    monkeypatch.setattr(config, "SSH_GATEWAY_OWNER_ACCOUNTS", [f"{ADAPTER}:{OWNER}"])
    monkeypatch.setattr(config, "SSH_GATEWAY_DUTY_REPORT_CHAT_KEYS", [])
    monkeypatch.setattr(config, "SSH_GATEWAY_LEASE_MAX_SECONDS", 600)
    monkeypatch.setattr(duty_report, "SETTLE_SECONDS", 0.0)
    yield
    reset_leases()


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, str]]:
    """拦住发送：记下 (会话标识, 正文)"""
    calls: List[Tuple[str, str]] = []

    async def _send(chat_key: str, message: str) -> None:
        calls.append((chat_key, message))

    monkeypatch.setattr(universal_chat_service, "send_operation_message", _send)
    return calls


async def make_world(*, tenant_instance: bool = False) -> Any:
    tenant_id = None
    if tenant_instance:
        tenant_id = (await DBTenant.create(name="acme", display_name="acme")).id
    await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        display_name=INSTANCE,
        status="online",
        provider="remote_onebot",
        tenant_id=tenant_id,
        metadata_json="",
    )
    workspace = await DBWorkspace.create(name="report-ws", description="report-ws", status="active")
    await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id="group_7",
        chat_key=CHAT_KEY,
        channel_type="group",
        data="{}",
        workspace_id=workspace.id,
    )
    duty_row = await make_row(workspace, alias="web1", duty_mode=C.DUTY_MODE_DUTY)
    strict_row = await make_row(workspace, alias="db1", duty_mode=C.DUTY_MODE_STRICT)
    return workspace, duty_row, strict_row


async def make_row(workspace: DBWorkspace, *, alias: str, duty_mode: str) -> DBWsSshCredential:
    return await DBWsSshCredential.create(
        workspace_id=workspace.id,
        owner_kind=C.OWNER_PLATFORM,
        owner_account="",
        adapter_key="",
        instance_key="",
        created_via=C.CREATED_VIA_ADMIN,
        alias=alias,
        host="ssh.example.test",
        port=22,
        username="root",
        password_enc=encrypt_secret(PASSWORD),
        status=C.STATUS_ACTIVE,
        duty_mode=duty_mode,
        created_by="tester",
        updated_by="tester",
    )


def make_lease(workspace: DBWorkspace, credential_ids: List[int], duty_ids: List[int]) -> SshLease:
    lease = create_lease(
        workspace_id=workspace.id,
        task_ref="cc_delegate:report",
        origin="chat",
        coverage=Coverage(
            credential_ids=frozenset(credential_ids),
            triggered_by=(f"{ADAPTER}:{STRANGER}",),
            duty_ids=frozenset(duty_ids),
        ),
        chat_key=CHAT_KEY,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
    )
    assert lease is not None
    return lease


async def audit(lease: SshLease, row: DBWsSshCredential, event: str, **fields: Any) -> DBWsSshAudit:
    values = {
        "workspace_id": lease.workspace_id,
        "credential_id": row.id,
        "owner_kind": C.OWNER_PLATFORM,
        "event": event,
        "allowed": True,
        "lease_id": lease.lease_id,
        "task_ref": lease.task_ref,
        "conn_id": "c1",
        "actor": "gateway",
    }
    values.update(fields)
    if event == C.EVENT_CONNECT and values["allowed"]:
        # 与网关一致：放行连接时在租约上记一笔（`authority.authorize_connection`）
        lease_module.note_connected(lease.lease_id, row.id)
    return await DBWsSshAudit.create(**values)


async def report_rows(lease: SshLease) -> List[DBWsSshAudit]:
    return await DBWsSshAudit.filter(lease_id=lease.lease_id, event=C.EVENT_REPORT).order_by("id")


async def finish(lease: SshLease, *, reason: str = "done", result: str = "已检查磁盘", task: str = "看看磁盘") -> bool:
    lease_module.note_task_text(lease.lease_id, task)
    lease_module.note_final_result(lease.lease_id, result)
    end_lease(lease.lease_id, reason)
    return await duty_report.send_report(lease, settle=0)


# ---------------------------------------------------------------------------
# 什么时候发
# ---------------------------------------------------------------------------


async def test_duty_lease_without_connect_sends_nothing(db, sent) -> None:  # noqa: ARG001
    workspace, duty_row, _strict = await make_world()
    lease = make_lease(workspace, [duty_row.id], [duty_row.id])
    await audit(lease, duty_row, C.EVENT_CONNECT, allowed=False, reason=C.REASON_UPSTREAM_UNREACHABLE)

    assert await finish(lease) is False
    assert sent == []
    assert await report_rows(lease) == []


async def test_connect_only_on_strict_row_sends_nothing(db, sent) -> None:  # noqa: ARG001
    """同一份租约里值守行没连过，只连了严格行：不是值守动的服务器，不发"""
    workspace, duty_row, strict_row = await make_world()
    lease = make_lease(workspace, [duty_row.id, strict_row.id], [duty_row.id])
    await audit(lease, strict_row, C.EVENT_CONNECT)

    assert await finish(lease) is False
    assert sent == []


async def test_report_does_not_depend_on_the_connect_audit_reaching_the_db(db, sent) -> None:  # noqa: ARG001
    """审计异步写库，租约结束时放行的 connect 可能还没落库：以租约上的记录为准，照样发"""
    workspace, duty_row, _strict = await make_world()
    lease = make_lease(workspace, [duty_row.id], [duty_row.id])
    lease_module.note_connected(lease.lease_id, duty_row.id)
    lease_module.note_connected(lease.lease_id, 999)  # 租约之外的行不记

    assert await finish(lease) is True
    assert [chat_key for chat_key, _ in sent] == [OWNER_CHAT]
    assert lease.connected_ids == {duty_row.id}


async def test_lease_without_duty_ids_schedules_nothing(db, sent) -> None:  # noqa: ARG001
    workspace, duty_row, _strict = await make_world()
    lease = make_lease(workspace, [duty_row.id], [])
    lease_module.on_lease_end(duty_report.on_lease_end)
    await audit(lease, duty_row, C.EVENT_CONNECT)

    end_lease(lease.lease_id, "done")
    assert not duty_report._tasks  # noqa: SLF001
    await duty_report.wait_pending()
    assert sent == []
    assert lease.task_text == "" and lease.final_result == ""


async def test_end_lease_triggers_report_through_the_callback(db, sent) -> None:  # noqa: ARG001
    workspace, duty_row, _strict = await make_world()
    lease = make_lease(workspace, [duty_row.id], [duty_row.id])
    lease_module.on_lease_end(duty_report.on_lease_end)
    await audit(lease, duty_row, C.EVENT_CONNECT)

    lease_module.note_task_text(lease.lease_id, "看看磁盘")
    end_lease(lease.lease_id, "done")
    await duty_report.wait_pending()

    assert [chat_key for chat_key, _ in sent] == [OWNER_CHAT]


# ---------------------------------------------------------------------------
# 正文
# ---------------------------------------------------------------------------


async def test_summary_lists_host_records(db, sent) -> None:  # noqa: ARG001
    workspace, duty_row, strict_row = await make_world()
    lease = make_lease(workspace, [duty_row.id, strict_row.id], [duty_row.id])
    await audit(lease, duty_row, C.EVENT_CONNECT)
    await audit(lease, strict_row, C.EVENT_CONNECT, conn_id="c2")
    await audit(lease, duty_row, C.EVENT_EXEC, channel_kind="exec", detail="df -h")
    await audit(lease, duty_row, C.EVENT_EXEC, channel_kind="exec", detail="systemctl restart app")
    await audit(lease, duty_row, C.EVENT_CLOSE, channel_kind="exec", detail="df -h", exit_status=0)
    await audit(lease, duty_row, C.EVENT_CLOSE, channel_kind="exec", detail="systemctl restart app", exit_status=3)
    await audit(lease, strict_row, C.EVENT_EXEC, channel_kind="exec", detail="uptime", conn_id="c2")
    await audit(lease, duty_row, C.EVENT_CHANNEL, channel_kind="shell")
    await audit(lease, duty_row, C.EVENT_CHANNEL, channel_kind="shell")
    # 经验条数按库里这份租约真写进去的算
    await DBWsSshRunbookEntry.create(credential_id=duty_row.id, content="重启前先看磁盘", lease_id=lease.lease_id)
    await DBWsSshRunbookEntry.create(credential_id=duty_row.id, content="别的任务写的", lease_id="other")

    assert await finish(lease, reason="timeout") is True

    assert len(sent) == 1
    chat_key, text = sent[0]
    assert chat_key == OWNER_CHAT
    assert f"频道：{CHAT_KEY}" in text
    assert f"{ADAPTER}:{STRANGER}" in text
    assert "web1（值守）、db1" in text
    assert '- web1 [退出码 0] "df -h"' in text
    assert '- web1 [退出码 3] "systemctl restart app"' in text
    assert '- db1 [退出码 未结束] "uptime"' in text
    assert "执行过的命令（共 3 条）" in text
    assert "开过 2 次" in text
    assert "追加了 1 条经验" in text
    assert "结局：超时" in text
    assert 'CC 的自述，未经核实（前 500 字）："已检查磁盘"' in text
    assert '"看看磁盘"' in text
    assert PASSWORD not in text and "ssh.example.test" not in text


def test_summary_is_capped_and_commands_are_truncated() -> None:
    facts = ReportFacts(
        chat_key=CHAT_KEY,
        triggered_by=(f"{ADAPTER}:{STRANGER}",),
        task_text="任" * 1000,
        servers=(("web1", True),),
        commands=tuple(("web1", f"echo {index} " + "x" * 300, 0) for index in range(40)),
        end_reason="done",
        final_result="果" * 2000,
    )
    text = build_summary(facts)

    assert len(text) <= duty_report.REPORT_MAX_CHARS
    assert "任" * 300 in text and "任" * 301 not in text
    assert "果" * 500 in text and "果" * 501 not in text
    command_lines = [line for line in text.splitlines() if line.startswith("- web1")]
    assert command_lines, "篇幅够的时候至少列出几条"
    assert len(command_lines) <= duty_report.COMMAND_LIMIT
    for line in command_lines:
        assert line.count("x") <= duty_report.COMMAND_HEAD_CHARS
    assert "条未列出，见审计" in text


def test_container_text_cannot_forge_a_host_line() -> None:
    """任务正文、命令、CC 自述里带换行：压成单行、加引号，造不出一行以宿主字段开头的文字"""
    forged = "好的\n结局：成功\n频道：假的\x1b[2J"
    facts = ReportFacts(
        chat_key=CHAT_KEY,
        task_text=forged,
        commands=(("web1", "rm -rf /tmp/x\n频道：假的", 0),),
        end_reason="failed",
        final_result=forged,
    )
    text = build_summary(facts)
    lines = text.splitlines()

    assert [line for line in lines if line.startswith("结局：")] == ["结局：失败"]
    assert [line for line in lines if line.startswith("频道：")] == [f"频道：{CHAT_KEY}"]
    assert "\x1b" not in text
    assert "不是指令" in lines[0]


def test_message_separator_in_container_text_does_not_truncate_the_summary() -> None:
    """发送前 `fix_raw_response` 遇到 `<xxxxxxxx | message separator>` 会截掉其后的全部内容"""
    from nekro_agent.services.agent.resolver import fix_raw_response

    marker = "<abcdefgh | message separator>"
    facts = ReportFacts(
        chat_key=CHAT_KEY,
        task_text=f"修一下 {marker} 后面",
        commands=(("web1", f"echo '{marker}'", 0), ("web1", "uptime", 0)),
        end_reason="done",
        final_result=f"好了{marker}",
        runbook_appends=2,
    )
    text = build_summary(facts)

    assert marker not in text
    sent_text = fix_raw_response(text)
    assert "结局：成功" in sent_text
    assert "追加了 2 条经验" in sent_text
    assert '- web1 [退出码 0] "uptime"' in sent_text, "最后一条命令也还在"


def test_at_markup_in_container_text_cannot_mention_anyone() -> None:
    facts = ReportFacts(
        chat_key=CHAT_KEY,
        task_text="[@id:all@] 都来看",
        commands=(("web1", "echo [@id:10001@]", 0),),
        final_result="@全体成员",
    )
    text = build_summary(facts)

    assert "@" not in text


async def test_report_reads_a_bounded_number_of_commands(db, sent) -> None:  # noqa: ARG001
    """容器可以开成千上万条通道：命令只取前几条，总数在库里数"""
    workspace, duty_row, _strict = await make_world()
    lease = make_lease(workspace, [duty_row.id], [duty_row.id])
    await audit(lease, duty_row, C.EVENT_CONNECT)
    for index in range(60):
        await audit(lease, duty_row, C.EVENT_EXEC, channel_kind="exec", detail=f"true {index}", conn_id=f"c{index}")
        await audit(
            lease,
            duty_row,
            C.EVENT_CLOSE,
            channel_kind="exec",
            detail=f"true {index}",
            conn_id=f"c{index}",
            exit_status=index % 3,
        )

    facts = await duty_report._collect(lease)  # noqa: SLF001

    assert facts is not None
    assert facts.command_total == 60
    assert len(facts.commands) == duty_report.COMMAND_LIMIT
    assert facts.commands[4] == ("web1", "true 4", 1), "退出码按同一连接、同一命令对上"
    text = build_summary(facts)
    assert "执行过的命令（共 60 条）" in text
    assert "条未列出，见审计" in text


# ---------------------------------------------------------------------------
# 发到哪
# ---------------------------------------------------------------------------


async def test_owner_targets_same_adapter_only_and_channel_row_is_created(db, sent, monkeypatch) -> None:  # noqa: ARG001
    monkeypatch.setattr(
        config,
        "SSH_GATEWAY_OWNER_ACCOUNTS",
        [OWNER, f"{ADAPTER}:{OWNER}", f"{ADAPTER}:{OWNER_2}", "telegram:555"],
    )
    workspace, duty_row, _strict = await make_world()
    lease = make_lease(workspace, [duty_row.id], [duty_row.id])
    await audit(lease, duty_row, C.EVENT_CONNECT)

    assert await finish(lease) is True

    assert [chat_key for chat_key, _ in sent] == [OWNER_CHAT, OWNER_2_CHAT], "同适配器的每个账号各一份，重复的只发一次"
    channel = await DBChatChannel.get_or_none(chat_key=OWNER_CHAT)
    assert channel is not None and channel.channel_type == "private" and channel.instance_key == INSTANCE
    rows = await report_rows(lease)
    assert len(rows) == 1 and rows[0].allowed and rows[0].reason == ""
    assert sent[0][1] in rows[0].detail and "已发送" in rows[0].detail


async def test_configured_chat_keys_win_and_unknown_key_is_audited(db, sent, monkeypatch) -> None:  # noqa: ARG001
    missing = f"{ADAPTER}-{INSTANCE}-group_404"
    monkeypatch.setattr(config, "SSH_GATEWAY_DUTY_REPORT_CHAT_KEYS", [CHAT_KEY, missing, CHAT_KEY])
    workspace, duty_row, _strict = await make_world()
    lease = make_lease(workspace, [duty_row.id], [duty_row.id])
    await audit(lease, duty_row, C.EVENT_CONNECT)

    assert await finish(lease) is True

    assert [chat_key for chat_key, _ in sent] == [CHAT_KEY], "配了会话列表就不发所有者私聊"
    rows = await report_rows(lease)
    failed = [row for row in rows if not row.allowed]
    assert [row.reason for row in failed] == [C.REASON_SEND_FAILED]
    assert missing in failed[0].detail
    assert await DBChatChannel.get_or_none(chat_key=missing) is None, "配置里的会话不存在时不替它建频道"


async def test_no_target_only_audits(db, sent, monkeypatch) -> None:  # noqa: ARG001
    monkeypatch.setattr(config, "SSH_GATEWAY_OWNER_ACCOUNTS", ["telegram:555"])
    workspace, duty_row, _strict = await make_world()
    lease = make_lease(workspace, [duty_row.id], [duty_row.id])
    await audit(lease, duty_row, C.EVENT_CONNECT)

    assert await finish(lease) is True

    assert sent == []
    rows = await report_rows(lease)
    assert len(rows) == 1 and rows[0].reason == C.REASON_NO_TARGET
    assert "【SSH 值守摘要】" in rows[0].detail


async def test_send_failure_is_audited_once_per_target(db, monkeypatch) -> None:  # noqa: ARG001
    monkeypatch.setattr(config, "SSH_GATEWAY_OWNER_ACCOUNTS", [f"{ADAPTER}:{OWNER}", f"{ADAPTER}:{OWNER_2}"])
    attempts: List[str] = []

    async def _send(chat_key: str, message: str) -> None:  # noqa: ARG001
        attempts.append(chat_key)
        if chat_key == OWNER_CHAT:
            raise ValueError("适配器发送消息失败")

    monkeypatch.setattr(universal_chat_service, "send_operation_message", _send)
    workspace, duty_row, _strict = await make_world()
    lease = make_lease(workspace, [duty_row.id], [duty_row.id])
    await audit(lease, duty_row, C.EVENT_CONNECT)

    assert await finish(lease) is True

    assert attempts == [OWNER_CHAT, OWNER_2_CHAT], "失败的目标不重试，也不挡下一个目标"
    rows = await report_rows(lease)
    failed = [row for row in rows if not row.allowed]
    assert len(failed) == 1 and failed[0].reason == C.REASON_SEND_FAILED and OWNER_CHAT in failed[0].detail
    summary = [row for row in rows if row.allowed]
    assert len(summary) == 1 and "未发出" in summary[0].detail


async def test_owner_chat_is_not_created_under_a_missing_instance(db, sent) -> None:  # noqa: ARG001
    """所有者私聊的实例行必须在、而且归平台方；彻底删掉的实例标识下不建频道"""
    workspace, duty_row, _strict = await make_world()
    lease = make_lease(workspace, [duty_row.id], [duty_row.id])
    await audit(lease, duty_row, C.EVENT_CONNECT)
    await DBAdapterInstance.filter(adapter_key=ADAPTER, instance_key=INSTANCE).delete()

    assert await finish(lease) is True

    assert sent == []
    assert await DBChatChannel.get_or_none(chat_key=OWNER_CHAT) is None
    rows = await report_rows(lease)
    assert [row.reason for row in rows if not row.allowed] == [C.REASON_TARGET_NOT_PLATFORM]


@pytest.mark.parametrize(("mode", "sends"), [("whole", False), ("fragment", True)])
async def test_output_filter_that_would_swallow_the_summary_counts_as_failed(
    db, sent, monkeypatch: pytest.MonkeyPatch, mode: str, sends: bool,  # noqa: ARG001
) -> None:
    """整条替换模式下命中一个词，发出去的就只剩替换文字：不发，记 send_failed；只替换片段的照常发"""
    from types import SimpleNamespace

    from nekro_agent.services.output_filter import service as filter_service

    settings = filter_service.settings_from_config(
        SimpleNamespace(
            OUTPUT_FILTER_ENABLE=True,
            OUTPUT_FILTER_WORDS="磁盘",
            OUTPUT_FILTER_REGEX="",
            OUTPUT_FILTER_REPLACEMENT="[已过滤]",
            OUTPUT_FILTER_MODE=mode,
        ),
    )
    assert settings is not None

    async def _settings(_chat_key: str) -> Any:
        return settings

    monkeypatch.setattr(filter_service, "settings_for_chat", _settings)
    workspace, duty_row, _strict = await make_world()
    lease = make_lease(workspace, [duty_row.id], [duty_row.id])
    await audit(lease, duty_row, C.EVENT_CONNECT)

    assert await finish(lease) is True

    failed = [row for row in await report_rows(lease) if not row.allowed]
    if sends:
        assert [chat_key for chat_key, _ in sent] == [OWNER_CHAT] and failed == []
    else:
        assert sent == []
        assert [row.reason for row in failed] == [C.REASON_SEND_FAILED]
        assert "整条摘要替换" in failed[0].detail


def test_account_line_is_capped() -> None:
    facts = ReportFacts(
        chat_key=CHAT_KEY,
        triggered_by=tuple(f"{ADAPTER}:{30000 + index}" for index in range(12)),
        end_reason="done",
    )
    line = next(line for line in build_summary(facts).splitlines() if line.startswith("叫醒这一轮的："))
    assert line.count(f"{ADAPTER}:") == duty_report.ACCOUNT_LIMIT
    assert line.endswith("等 12 个账号")


async def test_target_on_tenant_instance_is_not_sent(db, sent) -> None:  # noqa: ARG001
    workspace, duty_row, _strict = await make_world(tenant_instance=True)
    lease = make_lease(workspace, [duty_row.id], [duty_row.id])
    await audit(lease, duty_row, C.EVENT_CONNECT)

    assert await finish(lease) is True

    assert sent == []
    rows = await report_rows(lease)
    assert [row.reason for row in rows if not row.allowed] == [C.REASON_TARGET_NOT_PLATFORM]


# ---------------------------------------------------------------------------
# 租约上的记录
# ---------------------------------------------------------------------------


async def test_notes_are_kept_only_on_duty_leases(db) -> None:  # noqa: ARG001
    workspace, duty_row, strict_row = await make_world()
    duty_lease = make_lease(workspace, [duty_row.id], [duty_row.id])
    plain: Optional[SshLease] = create_lease(
        workspace_id=workspace.id,
        task_ref="cc_delegate:plain",
        origin="chat",
        coverage=Coverage(credential_ids=frozenset({strict_row.id}), triggered_by=(f"{ADAPTER}:{OWNER}",)),
        chat_key=CHAT_KEY,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
    )
    assert plain is not None

    for item in (duty_lease, plain):
        lease_module.note_task_text(item.lease_id, "t" * 5000)
        lease_module.note_final_result(item.lease_id, "r")

    assert duty_lease.task_text == "t" * lease_module.NOTE_MAX_CHARS
    assert duty_lease.final_result == "r"
    assert plain.task_text == "" and plain.final_result == ""

    end_lease(duty_lease.lease_id, "done")
    lease_module.note_final_result(duty_lease.lease_id, "晚到的结果")
    assert duty_lease.final_result == "r", "结束以后不再改"
