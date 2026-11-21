"""Web Chat MCP 的工具面与挂载点

工具本身不做权限判定（判定在入口），所以这里只钉住「注册了哪些工具」「哪些参数会被拒」
以及「应用确实挂在 `/api/mcp/web-chat`」——工具签名写错或挂载路径漏了，入口鉴权再严也没用。
"""

from pathlib import Path
from typing import Any

import pytest

from nekro_agent.services.mcp import web_chat_auth as auth
from nekro_agent.services.mcp.web_chat_mcp.service import ChatMessageItem, WebChatMcpService, WebChatMcpSettings

EXPECTED_TOOLS = {
    "check_web_chat_status",
    "list_web_chat_sessions",
    "create_web_chat_session",
    "rename_web_chat_session",
    "delete_web_chat_session",
    "send_web_chat_message",
    "send_web_chat_file",
    "get_web_chat_messages",
    "get_web_chat_channel_detail",
    "wait_for_web_chat_reply",
    "send_and_wait_web_chat_reply",
}


async def test_expected_tools_are_registered() -> None:
    from nekro_agent.services.mcp.web_chat_mcp.server import mcp

    tools = await mcp.list_tools()

    assert {tool.name for tool in tools} == EXPECTED_TOOLS


def test_transport_security_allows_docker_gateway_host() -> None:
    """沙盒里访问的就是 `host.docker.internal`，不放行等于内置条目永远连不上"""
    from nekro_agent.services.mcp.web_chat_mcp.server import mcp

    security = mcp.settings.transport_security

    assert security is not None
    assert security.enable_dns_rebinding_protection is True
    assert "127.0.0.1:*" in security.allowed_hosts
    assert "172.17.0.1:*" in security.allowed_hosts
    assert "host.docker.internal:*" in security.allowed_hosts


def test_app_is_mounted_under_api_mcp_web_chat() -> None:
    from fastapi import FastAPI

    from nekro_agent.routers import mount_api_routes

    app = FastAPI()
    mount_api_routes(app)

    mounted = {getattr(route, "path", "") for route in app.routes}
    assert "/api/mcp/web-chat" in mounted


async def test_delete_session_requires_confirmation() -> None:
    result = await WebChatMcpService().delete_session("web-session_test", confirm=False)

    assert result["ok"] is False
    assert result["status"] == "confirmation_required"


async def test_file_upload_requires_allowlist(tmp_path: Any) -> None:
    upload_file = tmp_path / "input.txt"
    upload_file.write_text("hello", encoding="utf-8")

    result = await WebChatMcpService(WebChatMcpSettings(file_allowlist=[])).send_file(
        "web-session_test",
        file_path=str(upload_file),
    )

    assert result["ok"] is False
    assert result["status"] == "file_not_allowed"


async def test_file_upload_rejects_paths_outside_allowlist(tmp_path: Any) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("hello", encoding="utf-8")

    result = await WebChatMcpService(WebChatMcpSettings(file_allowlist=[allowed])).send_file(
        "web-session_test",
        file_path=str(outside),
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "FileNotAllowed"


async def test_send_file_to_missing_path_reports_file_error(tmp_path: Any) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()

    result = await WebChatMcpService(WebChatMcpSettings(file_allowlist=[allowed])).send_file(
        "web-session_test",
        file_path=str(allowed / "missing.bin"),
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "FileNotFound"


async def test_file_upload_outside_allowlist_does_not_reveal_existence(tmp_path: Any) -> None:
    """白名单外的路径，存在与不存在必须拿到同一个结果，否则等于一个宿主机文件探针"""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    existing = tmp_path / "existing.txt"
    existing.write_text("hello", encoding="utf-8")
    missing = tmp_path / "missing.txt"
    service = WebChatMcpService(WebChatMcpSettings(file_allowlist=[allowed]))

    existing_result = await service.send_file("web-session_test", file_path=str(existing))
    missing_result = await service.send_file("web-session_test", file_path=str(missing))

    assert existing_result == missing_result
    assert existing_result["error"]["type"] == "FileNotAllowed"


async def test_file_allowlist_is_checked_before_existence(tmp_path: Any) -> None:
    result = await WebChatMcpService(WebChatMcpSettings(file_allowlist=[])).send_file(
        "web-session_test",
        file_path=str(tmp_path / "missing.txt"),
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "FileAllowlistRequired"


async def test_nul_byte_path_is_rejected_as_not_allowed(tmp_path: Any) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    service = WebChatMcpService(WebChatMcpSettings(file_allowlist=[allowed]))

    result = await service.send_file("web-session_test", file_path=str(allowed / "a\x00b.txt"))

    assert result["ok"] is False
    assert result["error"]["type"] == "FileNotAllowed"


async def test_symlink_inside_allowlist_pointing_outside_is_rejected(tmp_path: Any) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("hello", encoding="utf-8")
    link = allowed / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("本机不允许创建符号链接")

    result = await WebChatMcpService(WebChatMcpSettings(file_allowlist=[allowed])).send_file(
        "web-session_test",
        file_path=str(link),
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "FileNotAllowed"


async def test_subdirectory_inside_allowlist_is_not_a_file(tmp_path: Any) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "subdir").mkdir()

    result = await WebChatMcpService(WebChatMcpSettings(file_allowlist=[allowed])).send_file(
        "web-session_test",
        file_path=str(allowed / "subdir"),
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "FileNotFound"


def test_validate_file_path_returns_resolved_path_for_allowed_file(tmp_path: Any) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    target = allowed / "ok.txt"
    target.write_text("hello", encoding="utf-8")
    service = WebChatMcpService(WebChatMcpSettings(file_allowlist=[allowed]))

    result = service._validate_file_path(str(target))

    assert isinstance(result, Path)
    assert result == target.resolve()


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"sender_id": "-1", "sender_name": "Bot", "platform_userid": "0x1"}, "agent"),
        ({"sender_id": "5", "sender_name": "网页用户", "platform_userid": "admin_1"}, "user"),
        ({"sender_id": "-1", "sender_name": "SYSTEM", "platform_userid": "0"}, "system"),
    ],
)
def test_chat_message_item_role(kwargs: dict, expected: str) -> None:
    item = ChatMessageItem(
        id=1,
        content="x",
        chat_key="web-session_test",
        create_time="2026-08-05 00:00:00",
        sender_nickname=kwargs["sender_name"],
        **kwargs,
    )

    assert item.role == expected


def test_settings_read_allowlist_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("NEKRO_WEB_CHAT_MCP_FILE_ALLOWLIST", str(tmp_path / "a"))

    settings = WebChatMcpSettings.from_env()

    assert settings.file_allowlist == [(tmp_path / "a").resolve()]


def test_internal_secret_is_not_derivable_from_a_token() -> None:
    """令牌里只有工作区 id 与 MAC：拿到一枚令牌推不出密钥，也推不出别的工作区的令牌"""
    token = auth.build_workspace_web_chat_mcp_token(42)
    mac = token.rsplit(".", 1)[1]

    assert mac != str(42)
    assert auth.build_workspace_web_chat_mcp_token(43) != token
