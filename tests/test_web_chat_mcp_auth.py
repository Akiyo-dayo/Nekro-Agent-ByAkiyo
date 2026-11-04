"""Web Chat MCP 的令牌：按工作区派生、进程内轮换、外接固定密钥

上游用的是「一枚全局内置令牌」：它写进库条目，再抄进每个引用该条目的工作区 `.mcp.json`。
本分支的工作区可以归租户，那枚令牌落进租户工作区就是提权。这里钉住替代方案：
令牌只与一个工作区对应，重启即换，库里不存。
"""

import json
from pathlib import Path

import pytest

from nekro_agent.services.mcp import web_chat_auth as auth


@pytest.fixture(autouse=True)
def token_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """外接密钥落盘到临时目录，别碰真实的 data/configs"""
    path = tmp_path / "web-chat-mcp-external-token.json"
    monkeypatch.setattr(auth, "_EXTERNAL_TOKEN_PATH", path)
    return path


def test_workspace_token_is_derived_from_workspace_id() -> None:
    token = auth.build_workspace_web_chat_mcp_token(7)

    assert token.startswith("nwc1.7.")
    assert auth.parse_workspace_web_chat_mcp_token(token) == 7


def test_different_workspaces_get_different_tokens() -> None:
    assert auth.build_workspace_web_chat_mcp_token(1) != auth.build_workspace_web_chat_mcp_token(2)


def test_token_of_one_workspace_does_not_parse_as_another() -> None:
    """把 id 改掉、MAC 不动，必须验不过——否则改一个数字就能换工作区"""
    token = auth.build_workspace_web_chat_mcp_token(3)
    forged = token.replace("nwc1.3.", "nwc1.4.", 1)

    assert auth.parse_workspace_web_chat_mcp_token(forged) is None


@pytest.mark.parametrize(
    "token",
    [
        "",
        "nwc1",
        "nwc1.5",
        "nwc1.5.deadbeef",
        "nwc2.5." + "0" * 64,
        "nwc1.abc." + "0" * 64,
        "nwc1.0." + "0" * 64,
        "nwc1.-1." + "0" * 64,
        "nwc1.5." + "0" * 64 + "00",
        "Bearer nwc1.5." + "0" * 64,
    ],
)
def test_malformed_tokens_do_not_parse(token: str) -> None:
    assert auth.parse_workspace_web_chat_mcp_token(token) is None


def test_rotating_secret_invalidates_issued_tokens() -> None:
    """重启换密钥：旧 `.mcp.json` 里那张令牌下一次调用就该失效"""
    old = auth.build_workspace_web_chat_mcp_token(9)
    assert auth.parse_workspace_web_chat_mcp_token(old) == 9

    auth.rotate_web_chat_mcp_secret()

    assert auth.parse_workspace_web_chat_mcp_token(old) is None
    assert auth.parse_workspace_web_chat_mcp_token(auth.build_workspace_web_chat_mcp_token(9)) == 9


def test_workspace_headers_carry_bearer_token() -> None:
    headers = auth.build_workspace_web_chat_mcp_headers(11)

    assert headers == {"Authorization": f"Bearer {auth.build_workspace_web_chat_mcp_token(11)}"}


def test_builtin_entry_has_no_token() -> None:
    """库条目会被 `/mcp/registry`（租户可读）与工作区列表接口带出去"""
    entry = auth.build_web_chat_mcp_server_entry(auto_inject=True)

    assert entry["id"] == auth.WEB_CHAT_MCP_SERVER_ID
    assert entry["name"] == auth.WEB_CHAT_MCP_SERVER_NAME
    assert entry["type"] == "http"
    assert entry["url"] == auth.WEB_CHAT_MCP_URL
    assert entry["headers"] == {}
    assert entry["auto_inject"] is True


def test_normalize_strips_residual_token_from_legacy_entry() -> None:
    """旧版本把运行期令牌写进库条目；升级后第一次读清单就该抹掉"""
    legacy = {
        "id": auth.WEB_CHAT_MCP_SERVER_ID,
        "name": auth.WEB_CHAT_MCP_SERVER_NAME,
        "type": "http",
        "url": auth.WEB_CHAT_MCP_URL,
        "headers": {"Authorization": "Bearer leaked-from-old-version"},
    }

    normalized = auth.normalize_web_chat_mcp_entry(legacy)

    assert normalized["headers"] == {}
    assert normalized["type"] == "http"


def test_normalize_leaves_other_entries_untouched() -> None:
    other = {"name": "my-own-mcp", "type": "stdio", "command": "npx", "headers": {"X": "y"}}

    assert auth.normalize_web_chat_mcp_entry(other) == other


@pytest.mark.parametrize(
    "raw",
    [
        {"id": auth.WEB_CHAT_MCP_SERVER_ID},
        {"name": auth.WEB_CHAT_MCP_SERVER_NAME},
        {"name": "nekro_web_chat_test"},
        {"url": auth.WEB_CHAT_MCP_URL + "/"},
    ],
)
def test_entry_recognition_accepts_known_variants(raw: dict) -> None:
    assert auth.is_web_chat_mcp_entry(raw) is True


@pytest.mark.parametrize("raw", [{"name": "other"}, {"url": "http://example.com/mcp"}, {}])
def test_entry_recognition_rejects_unrelated(raw: dict) -> None:
    assert auth.is_web_chat_mcp_entry(raw) is False


def test_external_token_lifecycle_stores_only_hash(token_file: Path) -> None:
    token = "manual-external-token-123456"

    assert auth.get_external_web_chat_mcp_status()["configured"] is False
    assert auth.is_valid_external_web_chat_mcp_token(token) is False

    status = auth.save_external_web_chat_mcp_token(enabled=True, token=token)

    assert status["enabled"] is True
    assert status["configured"] is True
    assert auth.is_valid_external_web_chat_mcp_token(token) is True
    assert token not in token_file.read_text(encoding="utf-8")
    assert json.loads(token_file.read_text(encoding="utf-8"))["token_hash"]

    status = auth.save_external_web_chat_mcp_token(enabled=False)

    assert status["enabled"] is False
    assert status["configured"] is True
    assert auth.is_valid_external_web_chat_mcp_token(token) is False

    status = auth.clear_external_web_chat_mcp_token()

    assert status["configured"] is False
    assert auth.is_valid_external_web_chat_mcp_token(token) is False


def test_external_token_rejects_short_secret() -> None:
    with pytest.raises(ValueError, match="长度"):
        auth.save_external_web_chat_mcp_token(enabled=True, token="short")


def test_enabling_without_secret_is_refused() -> None:
    """「先开开关再生密钥」会让入口短暂接受任何令牌，必须拒"""
    with pytest.raises(ValueError, match="先设置密钥"):
        auth.save_external_web_chat_mcp_token(enabled=True)


def test_generated_external_token_is_returned_once(token_file: Path) -> None:
    token, status = auth.generate_external_web_chat_mcp_token()

    assert status["enabled"] is True
    assert auth.is_valid_external_web_chat_mcp_token(token) is True
    assert token not in token_file.read_text(encoding="utf-8")


def test_garbage_token_file_falls_back_to_unconfigured(token_file: Path) -> None:
    """密钥文件坏了要按「未配置」处理，不能让 MCP 入口 500"""
    token_file.write_text("not json", encoding="utf-8")

    assert auth.get_external_web_chat_mcp_status()["configured"] is False
    assert auth.is_valid_external_web_chat_mcp_token("anything") is False


def test_refresh_clears_library_entries_and_reports_names(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from nekro_agent.core import auto_inject_mcp

    monkeypatch.setattr(auto_inject_mcp, "_AUTO_INJECT_PATH", tmp_path / "auto-inject-mcp.json")
    auto_inject_mcp.set_auto_inject_mcp_servers(
        [
            {
                "id": auth.WEB_CHAT_MCP_SERVER_ID,
                "name": "Local Web Chat MCP",
                "type": "http",
                "auto_inject": False,
                "url": auth.WEB_CHAT_MCP_URL,
                "headers": {"Authorization": "Bearer stale"},
            },
            {"name": "other-mcp", "type": "stdio", "command": "npx"},
        ],
    )

    names = auth.refresh_web_chat_mcp_runtime_auth()

    servers = auto_inject_mcp.get_auto_inject_mcp_servers()
    assert names == [auth.WEB_CHAT_MCP_SERVER_NAME, "Local Web Chat MCP"]
    web_chat = next(server for server in servers if server.get("id") == auth.WEB_CHAT_MCP_SERVER_ID)
    assert web_chat["headers"] == {}
    assert any(server["name"] == "other-mcp" for server in servers)


def test_token_kind_predicate_accepts_both_kinds(token_file: Path) -> None:
    external = "manual-external-token-123456"
    auth.save_external_web_chat_mcp_token(enabled=True, token=external)

    assert auth.is_web_chat_mcp_token(auth.build_workspace_web_chat_mcp_token(1)) is True
    assert auth.is_web_chat_mcp_token(external) is True
    assert auth.is_web_chat_mcp_token("not-a-token-at-all") is False
