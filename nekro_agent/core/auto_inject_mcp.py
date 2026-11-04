"""Manage the list of MCP servers to auto-inject when creating a new workspace."""

import json
from pathlib import Path
from typing import Any, Dict, List

from nekro_agent.core.logger import logger
from nekro_agent.core.os_env import OsEnv

_AUTO_INJECT_PATH = Path(OsEnv.DATA_DIR) / "configs" / "auto-inject-mcp.json"

# 默认为空列表 — 不预设任何 MCP 服务。上游在这里种一条内置网页聊天条目；本分支这一版不往工作区
# `.mcp.json` 写网页聊天条目（见 `web_chat_mcp/na_app.py` 模块说明），种下来也用不上，所以不种
_DEFAULT_MCP_SERVERS: List[Dict[str, Any]] = []


def _ensure_file() -> None:
    _AUTO_INJECT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not _AUTO_INJECT_PATH.exists():
        _AUTO_INJECT_PATH.write_text(
            json.dumps({"servers": _DEFAULT_MCP_SERVERS}, indent=2),
            encoding="utf-8",
        )


def get_auto_inject_mcp_servers() -> List[Dict[str, Any]]:
    """Return the list of MCP server configs marked for auto-injection."""
    _ensure_file()
    try:
        data = json.loads(_AUTO_INJECT_PATH.read_text(encoding="utf-8"))
        servers = list(data.get("servers", []))
        return _normalize_builtin_web_chat_mcp(servers)
    except Exception as e:
        logger.warning(f"读取 auto-inject-mcp.json 失败: {e}")
        return []


def set_auto_inject_mcp_servers(servers: List[Dict[str, Any]]) -> None:
    """Overwrite the auto-inject MCP server list."""
    _ensure_file()
    normalized = _normalize_builtin_web_chat_mcp(servers, persist=False)
    _AUTO_INJECT_PATH.write_text(
        json.dumps({"servers": normalized}, indent=2),
        encoding="utf-8",
    )


def update_auto_inject_validation(name: str, validation_state: Dict[str, Any]) -> bool:
    """更新自动注入清单中某个 server 的 validation 字段。

    返回 True 表示找到并更新成功，False 表示该 name 不在清单内。
    """
    servers = get_auto_inject_mcp_servers()
    found = False
    for entry in servers:
        if (entry or {}).get("name") == name:
            entry["validation"] = validation_state
            found = True
            break
    if found:
        set_auto_inject_mcp_servers(servers)
    return found


def _normalize_builtin_web_chat_mcp(servers: List[Dict[str, Any]], *, persist: bool = True) -> List[Dict[str, Any]]:
    """把内置网页聊天条目的历史变体归一，并清掉其中可能残留的令牌

    本分支不种这一条（见 `_DEFAULT_MCP_SERVERS` 上方），但平台方手工加的、或从上游迁来的库里
    可能已经有：这里把它们归一成一条，并抹掉其中带的令牌。

    与上游的差别：上游在这里注入运行期令牌，本分支的 `headers` 恒为空，而且这一版不往工作区写这一条。
    """
    from nekro_agent.services.mcp.web_chat_auth import (
        is_web_chat_mcp_entry,
        normalize_web_chat_mcp_entry,
    )

    normalized: List[Dict[str, Any]] = []
    web_chat_entries: List[Dict[str, Any]] = []
    for raw in servers:
        if not isinstance(raw, dict):
            continue
        if is_web_chat_mcp_entry(raw):
            web_chat_entries.append(raw)
            continue
        normalized.append(raw)

    if web_chat_entries:
        # 多个变体只留一条：优先带内置 id 的，其次带 validation 的，最后一条兜底
        selected = (
            next((entry for entry in web_chat_entries if entry.get("id")), None)
            or next((entry for entry in web_chat_entries if isinstance(entry.get("validation"), dict)), None)
            or web_chat_entries[-1]
        )
        auto_inject = any(bool(entry.get("auto_inject", entry.get("enabled", False))) for entry in web_chat_entries)
        canonical = normalize_web_chat_mcp_entry({**selected, "auto_inject": auto_inject})
        normalized.append(canonical)

    if persist and normalized != [entry for entry in servers if isinstance(entry, dict)]:
        _AUTO_INJECT_PATH.write_text(
            json.dumps({"servers": normalized}, indent=2),
            encoding="utf-8",
        )
    return normalized
