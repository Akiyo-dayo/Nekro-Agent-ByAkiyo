"""CC 的失败信息在落进模型上下文、通讯日志与运行状态推送之前必须脱敏

## 背景

CC 报回的错误文本、SSE 错误事件、异常文本里有三种凭据要遮：

- **平台配置树**里的凭据（`collect_platform_secrets()`）；
- **CC 模型预设**里的 `auth_token` 与 `extra_env` 里的 Key——预设存在独立的
  `configs/cc-model-presets.json`，不在配置树上，`collect_platform_secrets()` 收不到；
- **认不出形态**的凭据（`Bearer …` 之类），按形态遮。

失败信息会进模型上下文（不可信正文，模型可能转述给群里）、工作区通讯日志与运行状态 SSE
（租户后台看得到）。成功结果不脱敏：按形态的规则会把代码里的 `token = "…"` 也打掉。

## 本文件钉什么

1. `CCModelPresetsStore.secret_values()` 收原文与 strip 后的值、跳过空串、只读不写、坏文件不抛；
2. 后台结果错误、委托任务失败、`_cc_delegate_task` 三个出口都遮住三把凭据与 `Bearer` 形态；
3. 对照组：成功结果里的 `token = "…"` 原样保留。
"""

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from nekro_agent.core.cc_model_presets import CCModelPresetsStore
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_workspace_comm_log import DBWorkspaceCommLog
from nekro_agent.services.plugin.task import AsyncTaskHandle, TaskCtl
from nekro_agent.services.workspace.client import CCSandboxError
from nekro_agent.tools.redaction import REDACTED_CREDENTIAL
from tests.test_pushed_external_content import (  # noqa: F401 - fixtures are used by name
    CHAT_KEY,
    cc,
    db,
    delegated,
    triggers,
)

# 三把假凭据：都 ≥ 16 字符、互不包含
PLATFORM_SECRET = "platform-secret-AAAA1111"
PRESET_AUTH_RAW = "preset-auth-token-BBBB2222\n"
PRESET_AUTH_STRIPPED = PRESET_AUTH_RAW.strip()
PRESET_EXTRA = "preset-extra-env-CCCC3333"
BEARER_SECRET = "abcdefghijklmnop"
ALL_SECRETS = (PLATFORM_SECRET, PRESET_AUTH_STRIPPED, PRESET_EXTRA, BEARER_SECRET)


def write_presets(path: Any, *, auth_token: str = PRESET_AUTH_RAW, extra_env: Dict[str, str] | None = None) -> None:
    path.write_text(
        json.dumps(
            {
                "presets": [
                    {
                        "id": 1,
                        "name": "p",
                        "auth_token": auth_token,
                        "extra_env": {"ANTHROPIC_API_KEY": PRESET_EXTRA, **(extra_env or {})},
                    },
                    {"id": 2, "name": "empty", "auth_token": "", "extra_env": {"API_KEY": ""}},
                ],
                "_next_id": 3,
            },
        ),
        encoding="utf-8",
    )


class TestPresetSecretValues:
    def test_both_the_raw_and_stripped_values_are_collected(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch):
        store = CCModelPresetsStore(path=tmp_path / "presets.json")
        write_presets(store.path)

        values = store.secret_values()

        assert PRESET_AUTH_RAW in values, "原文（带尾随换行）也要收"
        assert PRESET_AUTH_STRIPPED in values, "strip 后上线的那份也要收"
        assert PRESET_EXTRA in values
        assert "" not in values, "空串跳过"
        assert PLATFORM_SECRET not in values, "预设 store 不负责平台凭据"

    def test_a_broken_file_returns_an_empty_set_without_raising(self, tmp_path: Any):
        store = CCModelPresetsStore(path=tmp_path / "presets.json")
        store.path.write_text("{ this is not json", encoding="utf-8")

        assert store.secret_values() == frozenset()

    def test_a_missing_file_is_not_created(self, tmp_path: Any):
        missing = tmp_path / "does-not-exist.json"
        store = CCModelPresetsStore(path=missing)

        assert store.secret_values() == frozenset()
        assert not missing.exists(), "读脱敏集合不能有落盘副作用（不能调 ensure_default / list_all）"


@pytest.fixture
def fake_secrets(tmp_path: Any, monkeypatch: pytest.MonkeyPatch):
    """平台配置树 + CC 预设这三把假凭据"""
    from nekro_agent.services.agent import upstream_redaction

    write_presets(tmp_path / "presets.json")
    monkeypatch.setattr("nekro_agent.core.cc_model_presets.cc_presets_store.path", tmp_path / "presets.json")
    monkeypatch.setattr(upstream_redaction, "collect_platform_secrets", lambda: {PLATFORM_SECRET})
    return SimpleNamespace()


def error_text() -> str:
    return (
        "upstream 401\n"
        f"platform key: {PLATFORM_SECRET}\n"
        # 前缀故意不写成 `token:` / `api_key=` 之类：那会被按形态的规则顺带遮掉，测不到按值这一条
        f"preset value {PRESET_AUTH_STRIPPED}\n"
        f"env value {PRESET_EXTRA}\n"
        f"Authorization: Bearer {BEARER_SECRET}"
    )


def assert_no_secrets(text: str) -> None:
    for secret in ALL_SECRETS:
        assert secret not in text, (secret, text)


class TestPendingResultRedaction:
    async def test_an_error_result_is_redacted_in_the_row_and_the_comm_logs(
        self,
        db,  # noqa: F811
        triggers,  # noqa: F811
        cc,  # noqa: F811
        fake_secrets: Any,
    ):
        from nekro_agent.models.db_chat_channel import DBChatChannel

        await DBChatChannel.filter(chat_key=CHAT_KEY).update(workspace_id=7)
        workspace = SimpleNamespace(id=7, name="ws")
        item = {
            "source_chat_key": CHAT_KEY,
            "result": error_text(),
            "id": "result-1",
            "is_error": True,
            "error_code": f"E42 {PRESET_EXTRA}",
        }
        await cc._deliver_pending_result(workspace, item, source="watcher")  # noqa: SLF001

        [row] = await DBChatMessage.filter(chat_key=CHAT_KEY)
        assert_no_secrets(row.content_data)
        assert_no_secrets(row.content_text)
        assert REDACTED_CREDENTIAL in row.content_data

        logs = list(await DBWorkspaceCommLog.filter(workspace_id=7))
        assert logs
        for log in logs:
            assert_no_secrets(log.content)

    async def test_a_successful_result_keeps_the_code_token_assignment(
        self,
        db,  # noqa: F811
        triggers,  # noqa: F811
        cc,  # noqa: F811
        fake_secrets: Any,
    ):
        from nekro_agent.models.db_chat_channel import DBChatChannel

        await DBChatChannel.filter(chat_key=CHAT_KEY).update(workspace_id=7)
        workspace = SimpleNamespace(id=7, name="ws")
        item = {
            "source_chat_key": CHAT_KEY,
            "result": '示例代码:\ntoken = "abcd1234efgh"',
            "id": "result-2",
            "is_error": False,
            "error_code": "",
        }
        await cc._deliver_pending_result(workspace, item, source="watcher")  # noqa: SLF001

        [row] = await DBChatMessage.filter(chat_key=CHAT_KEY)
        body = json.loads(row.content_data)[0]["text"]
        assert 'token = "abcd1234efgh"' in body, "成功结果不套按形态的规则"


class TestDelegatedTaskFailureRedaction:
    async def test_a_failed_task_message_is_redacted_in_the_pushed_row(
        self,
        db,  # noqa: F811
        triggers,  # noqa: F811
        delegated,  # noqa: F811
        fake_secrets: Any,
    ):
        await delegated("", TaskCtl.fail(f"CC Sandbox 执行失败: {error_text()}"))

        [row] = await DBChatMessage.filter(chat_key=CHAT_KEY)
        assert_no_secrets(row.content_data)
        assert REDACTED_CREDENTIAL in row.content_data


class TestDelegateTaskErrorRedaction:
    async def test_the_sandbox_error_is_redacted_in_the_ctl_the_comm_log_and_the_runtime_event(
        self,
        db,  # noqa: F811
        cc,  # noqa: F811
        fake_secrets: Any,
        monkeypatch: pytest.MonkeyPatch,
    ):
        workspace = await DBWorkspace.create(name="redaction-ws", status="active")

        class _FailingClient:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            async def stream_message(self, *_args: Any, **_kwargs: Any):
                raise CCSandboxError(error_text())
                yield ""  # 让它是异步生成器：异常在 __anext__ 时抛出，与真实客户端一致

        async def _env(*_args: Any, **_kwargs: Any) -> Dict[str, str]:
            return {}

        events: List[Any] = []

        async def _publish(event: Any) -> None:
            events.append(event)

        async def _quiet(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(cc, "CCSandboxClient", _FailingClient)
        monkeypatch.setattr(cc.workspace_resource_service, "resolve_workspace_resources_to_env", _env)
        monkeypatch.setattr(cc, "publish_system_event", _publish)
        monkeypatch.setattr(cc, "_broadcast_cc_status", _quiet)
        monkeypatch.setattr(cc.comm_broadcast, "publish", _quiet)

        handle = AsyncTaskHandle(task_id=CHAT_KEY, chat_key=CHAT_KEY, plugin=SimpleNamespace())
        ctls = [ctl async for ctl in cc._cc_delegate_task(handle, workspace.id, "做点事")]  # noqa: SLF001

        assert ctls and ctls[-1].signal.value == "fail"
        assert_no_secrets(ctls[-1].message)

        logs = list(await DBWorkspaceCommLog.filter(workspace_id=workspace.id))
        assert logs
        for log in logs:
            assert_no_secrets(log.content)

        summaries = [event.error_summary for event in events if getattr(event, "error_summary", None)]
        assert summaries, "失败时应当推送带 error_summary 的运行状态事件"
        for summary in summaries:
            assert_no_secrets(summary)


class TestDirectSendRedaction:
    """后台「直接向 CC 发送指令」（`routers/workspaces.py` 的 `user_send_to_cc`）与插件委托路径用同一个脱敏出口"""

    def test_the_shared_helper_redacts_and_accepts_non_strings(self, fake_secrets: Any):
        from nekro_agent.services.agent.upstream_redaction import redact_cc_failure_text

        assert redact_cc_failure_text(None) == ""
        assert redact_cc_failure_text(42) == "42"
        redacted = redact_cc_failure_text(RuntimeError(error_text()))
        assert_no_secrets(redacted)
        assert REDACTED_CREDENTIAL in redacted

    def test_both_error_branches_of_the_direct_send_path_go_through_the_helper(self):
        """直发跑在接口拉起的后台任务里、要连真实的 SSE 流才走得到报错分支：在源码层钉住出口——
        写通讯日志的两个 `except` 里，异常对象只能出现在 `redact_cc_failure_text(e)` 里"""
        source = (Path(__file__).resolve().parent.parent / "nekro_agent" / "routers" / "workspaces.py").read_text(encoding="utf-8")
        func = next(
            node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.AsyncFunctionDef) and node.name == "user_send_to_cc"
        )
        handlers = [
            handler
            for handler in ast.walk(func)
            if isinstance(handler, ast.ExceptHandler)
            and handler.name
            and any("DBWorkspaceCommLog.create" in ast.unparse(stmt) for stmt in handler.body)
        ]
        assert len(handlers) == 2, [ast.unparse(h.type) for h in handlers if h.type is not None]
        for handler in handlers:
            uses = [n for stmt in handler.body for n in ast.walk(stmt) if isinstance(n, ast.Name) and n.id == handler.name]
            redacted = [
                arg
                for stmt in handler.body
                for call in ast.walk(stmt)
                if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "redact_cc_failure_text"
                for arg in call.args
                if isinstance(arg, ast.Name) and arg.id == handler.name
            ]
            assert uses and len(uses) == len(redacted), ast.unparse(handler)