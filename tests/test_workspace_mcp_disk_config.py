"""`.mcp.json` 的写盘形态与网页聊天条目

两份约束同时在这儿：
1. 字段名跟 Claude Code 的 schema（`type`，不是 `transport`；不带 NA 内部的 `id`）；
2. 网页聊天条目**这一版一律不写**，不论工作区此刻归谁、条目是内置的还是平台方手工配的。
   工作区归属随频道绑定 / 解绑来回变：平台方新建工作区时写进去的令牌，租户频道绑上来后留在租户
   读得到的地方，租户再解绑，工作区重新算成平台方，那张令牌就能以平台方身份调用（插件兼容会话复核）。
"""

import json
from pathlib import Path
from typing import Any, Dict

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
    disk = _build_disk_mcp_config({"mcp_servers_enabled": ["some-fetch"]}, library=_fetch_library())

    entry = disk["mcpServers"]["some-fetch"]
    assert entry["type"] == "http"
    assert "transport" not in entry
    assert "id" not in entry
    assert entry["url"] == "https://example.com/mcp"


@pytest.mark.parametrize(
    "library",
    [
        _web_chat_library(),
        # 平台方手工把外接令牌写进条目：同样不写，令牌不能落进以后可能交给租户的工作区
        _web_chat_library(headers={"Authorization": "Bearer external-secret"}),
        # 改了名、只靠 URL 认出来的
        {"my-chat": {"name": "my-chat", "type": "http", "url": "http://example.com/api/mcp/web-chat/mcp", "validation": {"status": "validated"}}},
    ],
)
def test_web_chat_entry_is_never_written(library: Dict[str, Dict[str, Any]]) -> None:
    names = list(library)
    disk = _build_disk_mcp_config({"mcp_servers_enabled": names}, library=library)

    assert disk["mcpServers"] == {}
    assert "nwc1." not in json.dumps(disk)


def test_renamed_reference_to_the_builtin_entry_is_still_skipped() -> None:
    """工作区记的是旧名、库里内置条目改过名：解析得到的仍是网页聊天条目，照样不写"""
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
    disk = _build_disk_mcp_config({"mcp_servers_enabled": [auth.WEB_CHAT_MCP_SERVER_NAME]}, library=library)

    assert disk["mcpServers"] == {}


def test_other_servers_are_written_next_to_a_skipped_web_chat_entry() -> None:
    library = {**_fetch_library(), **_web_chat_library()}

    disk = _build_disk_mcp_config(
        {"mcp_servers_enabled": ["some-fetch", auth.WEB_CHAT_MCP_SERVER_NAME]},
        library=library,
    )

    assert list(disk["mcpServers"]) == ["some-fetch"]


@pytest.mark.parametrize("owner_group", ["", "t5", "shared"])
async def test_written_file_has_no_web_chat_entry_whoever_owns_the_workspace(
    owner_group: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """插件兼容会话复核的 5 步里，第 1 步平台方工作区写盘就会把令牌写进去；现在不论归属都不写"""
    from nekro_agent.services.workspace import network_guard

    async def owner(_workspace: Any) -> str:
        return owner_group

    monkeypatch.setattr(network_guard, "resolve_workspace_network_group", owner)
    monkeypatch.setattr(network_guard, "resolve_workspace_owner_group_strict", owner)
    monkeypatch.setattr(manager, "_global_library_by_name", lambda: {**_fetch_library(), **_web_chat_library(auto_inject=True)})
    monkeypatch.setattr(manager.WorkspaceService, "get_workspace_dir", staticmethod(lambda _workspace_id: tmp_path))

    class _Workspace:
        id = 7
        metadata = {"mcp_servers_enabled": ["some-fetch", auth.WEB_CHAT_MCP_SERVER_NAME]}

    await manager.WorkspaceService._write_mcp_disk_locked(_Workspace())  # type: ignore[arg-type]

    written = (tmp_path / "default" / ".mcp.json").read_text(encoding="utf-8")
    assert "nwc1." not in written
    assert auth.WEB_CHAT_MCP_SERVER_NAME not in written
    assert "some-fetch" in json.loads(written)["mcpServers"]


def test_unvalidated_entry_is_not_written() -> None:
    library = _fetch_library()
    library["some-fetch"]["validation"] = {"status": "unvalidated"}

    disk = _build_disk_mcp_config({"mcp_servers_enabled": ["some-fetch"]}, library=library)

    assert disk["mcpServers"] == {}


def test_resolve_library_entry_returns_none_for_unknown_name() -> None:
    assert _resolve_library_entry("nope", {}) is None


def test_legacy_metadata_falls_back_to_mcp_config_keys() -> None:
    metadata = {"mcp_config": {"mcpServers": {"some-fetch": {}}}}

    disk = _build_disk_mcp_config(metadata, library=_fetch_library())

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

    disk = _build_disk_mcp_config({"mcp_servers_enabled": ["custom"]}, library=library)

    assert field in disk["mcpServers"]["custom"]
