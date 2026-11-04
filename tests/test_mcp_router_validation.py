"""全局 MCP 库保存时，validation 由服务端说了算（`ca32bb0`）

前端提交的 `validation` 一律丢掉：它可能是上一份配置的结果，也可能是手工拼的。
配置指纹没变才沿用旧状态，变了就重置为 unvalidated——否则改完 `url` 还能顶着一份
「已验证」，`.mcp.json` 会写入一条从没真正握手成功过的服务。
"""

import pytest

from nekro_agent.routers.mcp import _apply_server_owned_validation, _validation_config_fingerprint


def test_preserves_validation_when_connection_config_unchanged() -> None:
    """`auto_inject` 变了不算配置变了：它不影响连不连得上"""
    entry = {
        "name": "fetch",
        "type": "http",
        "url": "https://example.test/mcp",
        "headers": {"Authorization": "Bearer client"},
        "auto_inject": False,
        "validation": {"status": "validated", "server_name": "client-spoof"},
    }
    prev = {
        "name": "fetch",
        "type": "http",
        "url": "https://example.test/mcp",
        "headers": {"Authorization": "Bearer client"},
        "auto_inject": True,
        "validation": {"status": "validated", "server_name": "fetch", "tools_count": 3},
    }

    _apply_server_owned_validation(entry, prev)

    assert entry["validation"] == {"status": "validated", "server_name": "fetch", "tools_count": 3}


def test_resets_validation_when_url_changes() -> None:
    entry = {
        "name": "fetch",
        "type": "http",
        "url": "https://new.example.test/mcp",
        "headers": {"Authorization": "Bearer client"},
        "validation": {"status": "validated", "server_name": "client-spoof"},
    }
    prev = {
        "name": "fetch",
        "type": "http",
        "url": "https://old.example.test/mcp",
        "headers": {"Authorization": "Bearer client"},
        "validation": {"status": "validated", "server_name": "fetch", "tools_count": 3},
    }

    _apply_server_owned_validation(entry, prev)

    assert entry["validation"] == {"status": "unvalidated"}


def test_resets_validation_when_stdio_command_changes() -> None:
    entry = {
        "name": "local-tools",
        "type": "stdio",
        "command": "uvx",
        "args": ["new-package"],
        "env": {},
        "validation": {"status": "validated", "server_name": "client-spoof"},
    }
    prev = {
        "name": "local-tools",
        "type": "stdio",
        "command": "npx",
        "args": ["old-package"],
        "env": {},
        "validation": {"status": "validated", "server_name": "old-package"},
    }

    _apply_server_owned_validation(entry, prev)

    assert entry["validation"] == {"status": "unvalidated"}


def test_client_submitted_validation_is_dropped_for_new_entry() -> None:
    """新条目没有 prev：客户端说「已验证」不算数"""
    entry = {
        "name": "brand-new",
        "type": "http",
        "url": "https://example.test/mcp",
        "validation": {"status": "validated", "tools_count": 99},
    }

    _apply_server_owned_validation(entry, {})

    assert "validation" not in entry


@pytest.mark.parametrize("field", ["url", "headers", "env", "args", "command", "type"])
def test_every_connection_field_participates_in_the_fingerprint(field: str) -> None:
    entry = {"type": "http", "url": "https://a/mcp", "headers": {}}
    prev = dict(entry)
    prev[field] = "changed"

    assert _validation_config_fingerprint(entry) != _validation_config_fingerprint(prev)


@pytest.mark.parametrize("field", ["auto_inject", "name", "id", "enabled"])
def test_display_and_library_fields_do_not_participate(field: str) -> None:
    """改名不该把已验证状态清掉——那会让 `.mcp.json` 里那条静默消失"""
    entry = {"type": "http", "url": "https://a/mcp", "headers": {}, field: "value"}
    prev = {"type": "http", "url": "https://a/mcp", "headers": {}, field: "other"}

    assert _validation_config_fingerprint(entry) == _validation_config_fingerprint(prev)
