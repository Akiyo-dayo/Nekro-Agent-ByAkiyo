"""Web Chat MCP 专用鉴权与内置配置。

与上游的关键差别：内置条目**不存令牌**。上游用一枚全局令牌写进库条目，再抄进每个引用它的
工作区 `.mcp.json`；本分支的工作区可以归租户，全局令牌落进租户工作区就是提权。所以这里
`headers` 恒为空，工作区令牌按工作区现场派生（`nwc1.<工作区 id>.<HMAC>`），只写给合格的工作区：
合格 = 此刻归平台方，且从来没有出现过非平台方归属（`services/mcp/web_chat_workspace_taint.py`）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv

logger = get_sub_logger("web_chat_mcp.auth")

WEB_CHAT_MCP_SERVER_ID = "nekro-web-chat"
WEB_CHAT_MCP_SERVER_NAME = "NekroWebChatTest"
WEB_CHAT_MCP_URL = os.getenv(
    "NEKRO_WEB_CHAT_MCP_URL",
    "http://host.docker.internal:8021/api/mcp/web-chat/mcp",
)

_EXTERNAL_TOKEN_PATH = Path(OsEnv.DATA_DIR) / "configs" / "web-chat-mcp-external-token.json"
_MIN_EXTERNAL_TOKEN_LENGTH = 16


def generate_external_web_chat_mcp_token() -> tuple[str, Dict[str, Any]]:
    """生成并启用用户外接固定 token，仅调用方可拿到一次明文。"""
    token = secrets.token_urlsafe(48)
    status = save_external_web_chat_mcp_token(enabled=True, token=token)
    return token, status


def save_external_web_chat_mcp_token(*, enabled: bool, token: str | None = None) -> Dict[str, Any]:
    """保存用户外接 token 状态。token 为空时只切换启用状态，不覆盖既有密钥。"""
    state = _read_external_token_state()
    normalized_token = token.strip() if token is not None else ""
    if normalized_token:
        _validate_external_token(normalized_token)
        state["token_hash"] = _hash_token(normalized_token)
        state["token_preview"] = _preview_token(normalized_token)
        state["updated_at"] = _utc_now()

    if enabled and not state.get("token_hash"):
        raise ValueError("启用外接密钥前需要先设置密钥")

    state["enabled"] = enabled
    if not state.get("updated_at"):
        state["updated_at"] = _utc_now()
    _write_external_token_state(state)
    return get_external_web_chat_mcp_status()


def clear_external_web_chat_mcp_token() -> Dict[str, Any]:
    """关闭并清除用户外接 token。"""
    state: Dict[str, Any] = {
        "enabled": False,
        "token_hash": None,
        "token_preview": None,
        "updated_at": _utc_now(),
    }
    _write_external_token_state(state)
    return get_external_web_chat_mcp_status()


def get_external_web_chat_mcp_status() -> Dict[str, Any]:
    state = _read_external_token_state()
    return {
        "enabled": bool(state.get("enabled")) and bool(state.get("token_hash")),
        "configured": bool(state.get("token_hash")),
        "token_preview": state.get("token_preview") if state.get("token_hash") else None,
        "updated_at": state.get("updated_at") if state.get("token_hash") or state.get("updated_at") else None,
    }


def is_valid_external_web_chat_mcp_token(token: str) -> bool:
    state = _read_external_token_state()
    if not state.get("enabled") or not state.get("token_hash"):
        return False
    return hmac.compare_digest(_hash_token(token), str(state["token_hash"]))


# ── 工作区令牌 ────────────────────────────────────────────────────────────────
# 按工作区派生，只写给合格的工作区（此刻归平台方且从未出现过非平台方归属，见
# `web_chat_workspace_taint.py`）；被标记的工作区不发也不认，标记不会自动消失。
# 进程级密钥，每次重启换一份：旧令牌自动失效，与上游「重启后失效」的语义一致。
_INTERNAL_SECRET: str = secrets.token_urlsafe(32)
WORKSPACE_TOKEN_PREFIX = "nwc1"
_TOKEN_MAC_PREFIX = "web-chat-mcp:"


def rotate_web_chat_mcp_secret() -> str:
    """轮换进程级密钥，此前签发的所有工作区令牌立即失效"""
    global _INTERNAL_SECRET
    _INTERNAL_SECRET = secrets.token_urlsafe(32)
    return _INTERNAL_SECRET


def build_workspace_web_chat_mcp_token(workspace_id: int) -> str:
    """为某个工作区派生令牌 `nwc1.<工作区 id>.<HMAC-SHA256 十六进制>`

    令牌里只有工作区 id 与一个 MAC，不含任何秘密；拿到它的人只能操作它被签发的那一个工作区。
    """
    mac = hmac.new(
        _INTERNAL_SECRET.encode("utf-8"),
        f"{_TOKEN_MAC_PREFIX}{int(workspace_id)}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{WORKSPACE_TOKEN_PREFIX}.{int(workspace_id)}.{mac}"


def parse_workspace_web_chat_mcp_token(token: str) -> Optional[int]:
    """校验工作区令牌并取回工作区 id；不是本进程签发的合法令牌一律返回 None

    MAC 比较走 `hmac.compare_digest`，不按前缀长度提前返回：调用方拿到的是「401 / 不是 401」，
    逐字节的耗时差不该成为一句旁路。
    """
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != WORKSPACE_TOKEN_PREFIX:
        return None
    raw_id = parts[1]
    if not raw_id.isdigit():
        return None
    workspace_id = int(raw_id)
    if workspace_id <= 0:
        return None
    return workspace_id if hmac.compare_digest(token, build_workspace_web_chat_mcp_token(workspace_id)) else None


def build_workspace_web_chat_mcp_headers(workspace_id: int) -> Dict[str, str]:
    """写进某个工作区 `.mcp.json` 的鉴权头"""
    return {"Authorization": f"Bearer {build_workspace_web_chat_mcp_token(workspace_id)}"}


def is_web_chat_mcp_token(token: str) -> bool:
    """令牌是不是「本服务签发的那一种」——工作区令牌或已启用的外接令牌"""
    if parse_workspace_web_chat_mcp_token(token) is not None:
        return True
    return is_valid_external_web_chat_mcp_token(token)


def refresh_web_chat_mcp_runtime_auth() -> List[str]:
    """清掉库里内置条目残留的令牌，返回需要重写 `.mcp.json` 的条目名

    与上游的差别：这里不做「注入令牌」，只做「归一 + 清空」，库条目本身永远不带令牌；
    工作区 `.mcp.json` 里的令牌由写盘时按工作区现场派生（`services/workspace/manager.py`）。
    """
    from nekro_agent.core.auto_inject_mcp import get_auto_inject_mcp_servers, set_auto_inject_mcp_servers

    referenced_names: List[str] = [WEB_CHAT_MCP_SERVER_NAME]
    normalized: List[Dict[str, Any]] = []
    for raw in get_auto_inject_mcp_servers():
        if not isinstance(raw, dict):
            continue
        if is_web_chat_mcp_entry(raw):
            entry = normalize_web_chat_mcp_entry(raw)
            name = entry.get("name")
            if isinstance(name, str) and name and name not in referenced_names:
                referenced_names.append(name)
            normalized.append(entry)
        else:
            normalized.append(raw)
    set_auto_inject_mcp_servers(normalized)
    return referenced_names


async def init_web_chat_mcp_runtime_auth() -> None:
    """启动时先回填工作区标记，再轮换密钥，并重写引用了内置条目的工作区 `.mcp.json`

    回填扫描单独 try：失败只记 error，不拖住密钥轮换。记录不可用期间工作区令牌一律不生效，
    下一次判定会经 `ensure_taint_record` 重扫重建。
    整个流程失败只记警告：网页聊天 MCP 起不来不该拖住整个进程启动。
    """
    try:
        from nekro_agent.services.mcp.web_chat_workspace_taint import scan_workspace_taint

        await scan_workspace_taint()
    except Exception as e:  # noqa: BLE001
        logger.error(
            f"启动时回填网页聊天 MCP 的工作区标记失败，记录可用之前所有工作区令牌都不生效，"
            f"下一次判定会重扫: {e}",
        )
    try:
        from nekro_agent.services.workspace.manager import WorkspaceService

        rotate_web_chat_mcp_secret()
        for name in refresh_web_chat_mcp_runtime_auth():
            await WorkspaceService.refresh_global_mcp_server_references(name)
        logger.info("Web Chat MCP 运行期密钥已刷新")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"刷新 Web Chat MCP 运行期密钥失败（非致命）: {e}")


def _read_external_token_state() -> Dict[str, Any]:
    if not _EXTERNAL_TOKEN_PATH.exists():
        return _empty_external_token_state()
    try:
        data = json.loads(_EXTERNAL_TOKEN_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"读取 Web Chat MCP 外接密钥状态失败，将按未配置处理: {e}")
        return _empty_external_token_state()
    if not isinstance(data, dict):
        return _empty_external_token_state()
    return {
        "enabled": bool(data.get("enabled")),
        "token_hash": data.get("token_hash") if isinstance(data.get("token_hash"), str) else None,
        "token_preview": data.get("token_preview") if isinstance(data.get("token_preview"), str) else None,
        "updated_at": data.get("updated_at") if isinstance(data.get("updated_at"), str) else None,
    }


def _write_external_token_state(state: Dict[str, Any]) -> None:
    _EXTERNAL_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _EXTERNAL_TOKEN_PATH.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        _EXTERNAL_TOKEN_PATH.chmod(0o600)
    except OSError as e:
        logger.debug(f"设置 Web Chat MCP 外接密钥文件权限失败: {e}")


def _empty_external_token_state() -> Dict[str, Any]:
    return {
        "enabled": False,
        "token_hash": None,
        "token_preview": None,
        "updated_at": None,
    }


def _validate_external_token(token: str) -> None:
    if len(token) < _MIN_EXTERNAL_TOKEN_LENGTH:
        raise ValueError(f"外接密钥长度不能少于 {_MIN_EXTERNAL_TOKEN_LENGTH} 个字符")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _preview_token(token: str) -> str:
    if len(token) <= 12:
        return f"{token[:2]}...{token[-2:]}"
    return f"{token[:6]}...{token[-6:]}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_web_chat_mcp_key(value: str) -> str:
    return "".join(char for char in value.lower() if char.isalnum())


def is_web_chat_mcp_entry(raw: Dict[str, Any]) -> bool:
    """判断一条 MCP 条目是不是内置的网页聊天条目

    认三种形态：带内置 `id`、名字归一后与内置名相同、URL 指向本服务的网页聊天端点。
    用户手改过名字或从模板加进来的历史变体都能认出来，交给 `normalize_web_chat_mcp_entry` 归一。
    """
    name_key = _normalize_web_chat_mcp_key(str(raw.get("name") or raw.get("id") or ""))
    url = str(raw.get("url") or "").rstrip("/")
    return (
        raw.get("id") == WEB_CHAT_MCP_SERVER_ID
        or name_key == _normalize_web_chat_mcp_key(WEB_CHAT_MCP_SERVER_NAME)
        or url.endswith("/api/mcp/web-chat/mcp")
    )


def build_web_chat_mcp_server_entry(*, auto_inject: bool = False) -> Dict[str, Any]:
    """全局 MCP 库里的内置条目

    **不带任何令牌**：`headers` 恒为空。库条目会被工作区列表接口与 `/mcp/registry` 带出去，
    令牌只按工作区现场派生、只写进合格工作区的 `.mcp.json`。
    """
    return {
        "id": WEB_CHAT_MCP_SERVER_ID,
        "name": WEB_CHAT_MCP_SERVER_NAME,
        "type": "http",
        "auto_inject": auto_inject,
        "url": WEB_CHAT_MCP_URL,
        "headers": {},
        "validation": {
            "status": "validated",
            "server_name": "nekro-web-chat",
        },
    }


def normalize_web_chat_mcp_entry(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把网页聊天条目的历史变体归一，并清掉其中可能残留的令牌

    旧版本会把运行期令牌写进库条目；这里无条件覆盖成空 `headers`，
    升级后第一次读清单就把它抹掉，不留到下一个读接口里。
    """
    entry = dict(raw)
    if not is_web_chat_mcp_entry(entry):
        return entry
    entry["id"] = WEB_CHAT_MCP_SERVER_ID
    entry["type"] = "http"
    entry.setdefault("url", WEB_CHAT_MCP_URL)
    entry["headers"] = {}
    return entry
