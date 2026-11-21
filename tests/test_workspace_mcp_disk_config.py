"""`.mcp.json` 的写盘形态与网页聊天条目

两份约束同时在这儿：
1. 字段名跟 Claude Code 的 schema（`type`，不是 `transport`；不带 NA 内部的 `id`）；
2. 网页聊天条目只写给**合格**的工作区（此刻归平台方，且从未出现过非平台方归属）：
   令牌按工作区现场派生，库里或手工条目里带的头一律被整体覆盖；不合格时整条不写。
   资格判定本身在 `services/mcp/web_chat_workspace_taint.py`，本文件只管写盘形态。
"""

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from nekro_agent.services.mcp import web_chat_auth as auth
from nekro_agent.services.workspace import manager
from nekro_agent.services.workspace.manager import _build_disk_mcp_config, _resolve_library_entry


def _web_chat_library(**overrides: Any) -> Dict[str, Dict[str, Any]]:
    entry: Dict[str, Any] = {
        "id": auth.WEB_CHAT_MCP_SERVER_ID,
        "name": auth.WEB_CHAT_MCP_SERVER_NAME,
        "type": "http",
        "auto_inject": False,
        "url": auth.WEB_CHAT_MCP_URL,
        "headers": {},
        "validation": {"status": "validated", "server_name": "nekro-web-chat"},
    }
    entry.update(overrides)
    return {entry["name"]: entry}


def _fetch_library() -> Dict[str, Dict[str, Any]]:
    return {
        "some-fetch": {
            "name": "some-fetch",
            "type": "http",
            "url": "https://example.com/mcp",
            "headers": {"X": "y"},
            "validation": {"status": "validated"},
        },
    }


def test_http_entry_uses_claude_code_schema() -> None:
    disk = _build_disk_mcp_config(
        {"mcp_servers_enabled": ["some-fetch"]}, library=_fetch_library(), allow_platform_secrets=True
    )

    entry = disk["mcpServers"]["some-fetch"]
    assert entry["type"] == "http"
    assert "transport" not in entry
    assert "id" not in entry
    assert entry["url"] == "https://example.com/mcp"


@pytest.mark.parametrize(
    "library",
    [
        _web_chat_library(),
        # 平台方手工把外接令牌写进条目：没有合格工作区时同样不写，令牌不能落进以后可能交给租户的工作区
        _web_chat_library(headers={"Authorization": "Bearer external-secret"}),
        # 改了名、只靠 URL 认出来的
        {"my-chat": {"name": "my-chat", "type": "http", "url": "http://example.com/api/mcp/web-chat/mcp", "validation": {"status": "validated"}}},
    ],
)
def test_web_chat_entry_is_skipped_without_an_eligible_workspace(library: Dict[str, Dict[str, Any]]) -> None:
    names = list(library)
    disk = _build_disk_mcp_config(
        {"mcp_servers_enabled": names}, library=library, allow_platform_secrets=True
    )

    assert disk["mcpServers"] == {}
    assert "nwc1." not in json.dumps(disk)


@pytest.mark.parametrize(
    "library",
    [
        _web_chat_library(),
        _web_chat_library(headers={"Authorization": "Bearer external-secret"}),
        {"my-chat": {"name": "my-chat", "type": "http", "url": "http://example.com/api/mcp/web-chat/mcp", "validation": {"status": "validated"}}},
    ],
)
def test_eligible_workspace_gets_a_fresh_token_and_overrides_manual_headers(
    library: Dict[str, Dict[str, Any]],
) -> None:
    names = list(library)
    disk = _build_disk_mcp_config(
        {"mcp_servers_enabled": names},
        library=library,
        web_chat_workspace_id=42,
        allow_platform_secrets=True,
    )

    entry = disk["mcpServers"][names[0]]
    assert entry["headers"] == auth.build_workspace_web_chat_mcp_headers(42)
    assert "external-secret" not in json.dumps(disk)


def test_renamed_reference_to_the_builtin_entry_is_still_recognized() -> None:
    """工作区记的是旧名、库里内置条目改过名：解析得到的仍是网页聊天条目，按资格决定写不写"""
    library = {
        "Local Web Chat MCP": {
            "id": auth.WEB_CHAT_MCP_SERVER_ID,
            "name": "Local Web Chat MCP",
            "type": "http",
            "url": auth.WEB_CHAT_MCP_URL,
            "headers": {},
            "validation": {"status": "validated"},
        },
    }

    assert _resolve_library_entry(auth.WEB_CHAT_MCP_SERVER_NAME, library) is library["Local Web Chat MCP"]
    metadata = {"mcp_servers_enabled": [auth.WEB_CHAT_MCP_SERVER_NAME]}

    assert _build_disk_mcp_config(metadata, library=library, allow_platform_secrets=True)["mcpServers"] == {}
    entry = _build_disk_mcp_config(
        metadata, library=library, web_chat_workspace_id=42, allow_platform_secrets=True
    )["mcpServers"][
        auth.WEB_CHAT_MCP_SERVER_NAME
    ]
    assert entry["headers"] == auth.build_workspace_web_chat_mcp_headers(42)


def test_other_servers_are_written_next_to_a_skipped_web_chat_entry() -> None:
    library = {**_fetch_library(), **_web_chat_library()}

    disk = _build_disk_mcp_config(
        {"mcp_servers_enabled": ["some-fetch", auth.WEB_CHAT_MCP_SERVER_NAME]},
        library=library,
        allow_platform_secrets=True,
    )

    assert list(disk["mcpServers"]) == ["some-fetch"]


@pytest.mark.parametrize("eligible", [True, False])
async def test_written_file_follows_the_eligibility_verdict(
    eligible: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """写盘只看资格判定：合格才写这一条并带令牌，不合格整条不写"""
    from nekro_agent.services.mcp import web_chat_workspace_taint
    from nekro_agent.services.workspace import credential_policy

    async def fake_eligible(_workspace: Any) -> bool:
        return eligible

    async def fake_allowed(_workspace: Any) -> bool:
        return True

    marked: List[int] = []

    async def fake_mark(workspace: Any) -> None:
        marked.append(workspace.id)

    monkeypatch.setattr(web_chat_workspace_taint, "is_workspace_web_chat_token_eligible", fake_eligible)
    monkeypatch.setattr(credential_policy, "platform_credentials_allowed", fake_allowed)
    # `some-fetch` 带 `headers`，写出去会记「下发过平台方凭据」；这里的桩没有库，换成记账
    monkeypatch.setattr(credential_policy, "mark_platform_secret_delivered", fake_mark)
    monkeypatch.setattr(manager, "_global_library_by_name", lambda: {**_fetch_library(), **_web_chat_library(auto_inject=True)})
    monkeypatch.setattr(manager.WorkspaceService, "get_workspace_dir", staticmethod(lambda _workspace_id: tmp_path))

    class _Workspace:
        id = 7
        metadata = {"mcp_servers_enabled": ["some-fetch", auth.WEB_CHAT_MCP_SERVER_NAME]}

    await manager.WorkspaceService._write_mcp_disk_locked(_Workspace())  # type: ignore[arg-type]

    written = json.loads((tmp_path / "default" / ".mcp.json").read_text(encoding="utf-8"))
    assert "some-fetch" in written["mcpServers"]
    # 标记只因全局库里带凭据的条目（`some-fetch`）而记；网页聊天那一条的令牌不算，合格与否结果一样
    assert marked == [7]
    if eligible:
        entry = written["mcpServers"][auth.WEB_CHAT_MCP_SERVER_NAME]
        assert entry["headers"] == auth.build_workspace_web_chat_mcp_headers(7)
    else:
        assert auth.WEB_CHAT_MCP_SERVER_NAME not in written["mcpServers"]
        assert "nwc1." not in json.dumps(written)


async def test_the_web_chat_token_alone_is_not_a_platform_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """网页聊天条目的令牌只对本工作区有效、有自己的清理路径：只写出它时不记「下发过平台方凭据」"""
    from nekro_agent.services.mcp import web_chat_workspace_taint
    from nekro_agent.services.workspace import credential_policy

    async def fake_yes(_workspace: Any) -> bool:
        return True

    marked: List[int] = []

    async def fake_mark(workspace: Any) -> None:
        marked.append(workspace.id)

    monkeypatch.setattr(web_chat_workspace_taint, "is_workspace_web_chat_token_eligible", fake_yes)
    monkeypatch.setattr(credential_policy, "platform_credentials_allowed", fake_yes)
    monkeypatch.setattr(credential_policy, "mark_platform_secret_delivered", fake_mark)
    monkeypatch.setattr(manager, "_global_library_by_name", lambda: _web_chat_library(auto_inject=True))
    monkeypatch.setattr(manager.WorkspaceService, "get_workspace_dir", staticmethod(lambda _workspace_id: tmp_path))

    class _Workspace:
        id = 7
        metadata = {"mcp_servers_enabled": [auth.WEB_CHAT_MCP_SERVER_NAME]}

    await manager.WorkspaceService._write_mcp_disk_locked(_Workspace())  # type: ignore[arg-type]

    written = json.loads((tmp_path / "default" / ".mcp.json").read_text(encoding="utf-8"))
    assert written["mcpServers"][auth.WEB_CHAT_MCP_SERVER_NAME]["headers"] == auth.build_workspace_web_chat_mcp_headers(7)
    assert marked == []


def test_unvalidated_entry_is_not_written() -> None:
    library = _fetch_library()
    library["some-fetch"]["validation"] = {"status": "unvalidated"}

    disk = _build_disk_mcp_config(
        {"mcp_servers_enabled": ["some-fetch"]}, library=library, allow_platform_secrets=True
    )

    assert disk["mcpServers"] == {}


def test_resolve_library_entry_returns_none_for_unknown_name() -> None:
    assert _resolve_library_entry("nope", {}) is None


def test_legacy_metadata_falls_back_to_mcp_config_keys() -> None:
    metadata = {"mcp_config": {"mcpServers": {"some-fetch": {}}}}

    disk = _build_disk_mcp_config(metadata, library=_fetch_library(), allow_platform_secrets=True)

    assert "some-fetch" in disk["mcpServers"]


@pytest.mark.parametrize("field", ["command", "args", "env", "url", "headers"])
def test_entry_fields_survive_the_conversion(field: str) -> None:
    library = {
        "custom": {
            "name": "custom",
            "type": "stdio" if field in {"command", "args", "env"} else "http",
            "command": "npx",
            "args": ["-y", "pkg"],
            "env": {"A": "1"},
            "url": "https://example.com/mcp",
            "headers": {"X": "y"},
            "validation": {"status": "validated"},
        },
    }

    disk = _build_disk_mcp_config(
        {"mcp_servers_enabled": ["custom"]}, library=library, allow_platform_secrets=True
    )

    assert field in disk["mcpServers"]["custom"]


def test_credentialed_entries_are_skipped_when_the_workspace_may_not_have_platform_secrets() -> None:
    """不能拿平台方凭据的工作区：`env` / `headers` 非空的条目整条不写，不带凭据的照写

    判据只看"有没有 `env` / `headers`"，不看里面装的是什么——库条目只有平台方能改，
    逐项猜"哪些像凭据"必然会漏。
    """
    library = {
        "with-env": {
            "name": "with-env",
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "pkg"],
            "env": {"GITHUB_TOKEN": "ghp-secret"},
            "validation": {"status": "validated"},
        },
        "with-headers": {
            "name": "with-headers",
            "type": "http",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer secret"},
            "validation": {"status": "validated"},
        },
        "plain": {
            "name": "plain",
            "type": "http",
            "url": "https://example.com/plain",
            "validation": {"status": "validated"},
        },
    }
    metadata = {"mcp_servers_enabled": ["with-env", "with-headers", "plain"]}

    allowed = _build_disk_mcp_config(metadata, library=library, allow_platform_secrets=True)
    assert sorted(allowed["mcpServers"]) == ["plain", "with-env", "with-headers"]

    withheld = _build_disk_mcp_config(metadata, library=library, allow_platform_secrets=False)
    assert list(withheld["mcpServers"]) == ["plain"]
    assert "ghp-secret" not in json.dumps(withheld)
    assert "Bearer secret" not in json.dumps(withheld)

