"""网页聊天 MCP 条目对租户不可见、也加不进工作区（设计 §3.2）

租户看得见却永远用不了，比看不见更糟：他会去点、去测、去报「连不上」。所以读取侧直接摘掉，
加入侧按「全局库中不存在」拒绝——不额外泄露「有这么一条，但你不能用」。
"""

from datetime import datetime, timezone
from typing import List, Optional

import pytest

from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import workspaces as ws_router
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.mcp.schemas import McpServerConfig
from nekro_agent.services.mcp.web_chat_auth import (
    WEB_CHAT_MCP_SERVER_ID,
    WEB_CHAT_MCP_SERVER_NAME,
    WEB_CHAT_MCP_URL,
)
from nekro_agent.services.user.role import Role

TENANT_ID = 3


def _user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    return DBUser(
        username=f"user-{tenant_role.value}-{tenant_id}",
        password="",
        adapter_key="",
        platform_userid="0",
        perm_level=perm_level,
        login_time=datetime.now(timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )


def tenant_admin() -> DBUser:
    return _user(tenant_id=TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def platform_owner() -> DBUser:
    return _user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


def _web_chat_config() -> McpServerConfig:
    return McpServerConfig(
        id=WEB_CHAT_MCP_SERVER_ID,
        name=WEB_CHAT_MCP_SERVER_NAME,
        type="http",
        url=WEB_CHAT_MCP_URL,
    )


def _other_config() -> McpServerConfig:
    return McpServerConfig(name="fetch", type="stdio", command="uvx", args=["mcp-server-fetch"])


def test_tenant_does_not_see_web_chat_entry() -> None:
    servers = [_web_chat_config(), _other_config()]

    visible = ws_router._hide_web_chat_mcp(servers, tenant_admin())

    assert [server.name for server in visible] == ["fetch"]


def test_platform_owner_sees_web_chat_entry() -> None:
    servers = [_web_chat_config(), _other_config()]

    visible = ws_router._hide_web_chat_mcp(servers, platform_owner())

    assert {server.name for server in visible} == {WEB_CHAT_MCP_SERVER_NAME, "fetch"}


def test_renamed_web_chat_entry_is_still_hidden() -> None:
    """用户把内置条目改了名，认的仍是 `id` 与 URL"""
    renamed = McpServerConfig(
        id=WEB_CHAT_MCP_SERVER_ID,
        name="MyLocalWebChat",
        type="http",
        url=WEB_CHAT_MCP_URL,
    )

    assert ws_router._hide_web_chat_mcp([renamed], tenant_admin()) == []


def test_entry_recognized_by_url_only_is_hidden() -> None:
    """老条目可能丢了 id，只剩 URL"""
    url_only = McpServerConfig(name="whatever", type="http", url=WEB_CHAT_MCP_URL)

    assert ws_router._hide_web_chat_mcp([url_only], tenant_admin()) == []


async def test_tenant_cannot_add_web_chat_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    enabled: List[str] = []

    async def fake_accessible(_user: DBUser, _workspace_id: int) -> object:
        return object()

    async def fake_available(_ws: object) -> List[McpServerConfig]:
        return [_web_chat_config(), _other_config()]

    async def fake_enabled_list(_ws: object) -> List[McpServerConfig]:
        return []

    async def fake_set(_ws: object, name: str, _enabled: bool) -> None:
        enabled.append(name)

    monkeypatch.setattr(ws_router, "assert_workspace_accessible", fake_accessible)
    monkeypatch.setattr(ws_router.WorkspaceService, "list_available_mcp_servers", fake_available)
    monkeypatch.setattr(ws_router.WorkspaceService, "list_mcp_servers", fake_enabled_list)
    monkeypatch.setattr(ws_router.WorkspaceService, "set_mcp_server_enabled", fake_set)

    with pytest.raises(ValidationError):
        await ws_router.add_mcp_server_to_workspace(1, WEB_CHAT_MCP_SERVER_NAME, _current_user=tenant_admin())

    assert enabled == []


async def test_tenant_can_add_other_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    enabled: List[str] = []

    async def fake_accessible(_user: DBUser, _workspace_id: int) -> object:
        return object()

    async def fake_available(_ws: object) -> List[McpServerConfig]:
        return [_web_chat_config(), _other_config()]

    async def fake_enabled_list(_ws: object) -> List[McpServerConfig]:
        return []

    async def fake_set(_ws: object, name: str, _enabled: bool) -> None:
        enabled.append(name)

    monkeypatch.setattr(ws_router, "assert_workspace_accessible", fake_accessible)
    monkeypatch.setattr(ws_router.WorkspaceService, "list_available_mcp_servers", fake_available)
    monkeypatch.setattr(ws_router.WorkspaceService, "list_mcp_servers", fake_enabled_list)
    monkeypatch.setattr(ws_router.WorkspaceService, "set_mcp_server_enabled", fake_set)

    await ws_router.add_mcp_server_to_workspace(1, "fetch", _current_user=tenant_admin())

    assert enabled == ["fetch"]


async def test_platform_owner_can_add_web_chat_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    enabled: List[str] = []

    async def fake_accessible(_user: DBUser, _workspace_id: int) -> object:
        return object()

    async def fake_available(_ws: object) -> List[McpServerConfig]:
        return [_web_chat_config()]

    async def fake_enabled_list(_ws: object) -> List[McpServerConfig]:
        return []

    async def fake_set(_ws: object, name: str, _enabled: bool) -> None:
        enabled.append(name)

    monkeypatch.setattr(ws_router, "assert_workspace_accessible", fake_accessible)
    monkeypatch.setattr(ws_router.WorkspaceService, "list_available_mcp_servers", fake_available)
    monkeypatch.setattr(ws_router.WorkspaceService, "list_mcp_servers", fake_enabled_list)
    monkeypatch.setattr(ws_router.WorkspaceService, "set_mcp_server_enabled", fake_set)

    await ws_router.add_mcp_server_to_workspace(1, WEB_CHAT_MCP_SERVER_NAME, _current_user=platform_owner())

    assert enabled == [WEB_CHAT_MCP_SERVER_NAME]


def test_registry_does_not_list_the_web_chat_template() -> None:
    """这一版不往工作区 `.mcp.json` 写网页聊天条目，模板加进库里也用不上，所以不列"""
    from nekro_agent.services.mcp.registry import get_registry

    assert all(item.id != WEB_CHAT_MCP_SERVER_ID for item in get_registry())
    assert all(item.url != WEB_CHAT_MCP_URL for item in get_registry())


def test_new_library_is_not_seeded_with_the_web_chat_entry(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from nekro_agent.core import auto_inject_mcp

    monkeypatch.setattr(auto_inject_mcp, "_AUTO_INJECT_PATH", tmp_path / "configs" / "auto-inject-mcp.json")

    assert auto_inject_mcp.get_auto_inject_mcp_servers() == []


def test_registry_returns_copies() -> None:
    """调用方按请求改字段时，不能改到共享对象上（下一个请求就会继承）"""
    from nekro_agent.services.mcp.registry import get_registry

    first = get_registry()
    first[0].name = "mutated-by-caller"

    assert get_registry()[0].name != "mutated-by-caller"


def test_registry_item_models_accept_headers() -> None:
    from nekro_agent.services.mcp.schemas import McpRegistryItem

    item = McpRegistryItem(id="x", name="X", description="", type="http", url="https://a/mcp", headers={"A": "b"})

    assert item.headers == {"A": "b"}


@pytest.mark.parametrize(("user_factory", "expected"), [(tenant_admin, True), (platform_owner, False)])
async def test_ad_hoc_test_restricts_outbound_by_identity(
    user_factory: object,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工作区临时测试是租户唯一够得着的 MCP 校验入口：租户强制公网，平台方豁免（同模型组 base_url）"""
    from nekro_agent.services.mcp import validator
    from nekro_agent.services.mcp.validator import McpValidationResult

    seen: List[bool] = []

    async def fake_accessible(_user: DBUser, _workspace_id: int) -> object:
        return object()

    async def fake_validate(_ws: object, _server: McpServerConfig, *, restrict_to_public_network: bool) -> McpValidationResult:
        seen.append(restrict_to_public_network)
        return McpValidationResult(ok=False, error="stub", error_kind="url_not_allowed")

    monkeypatch.setattr(ws_router, "assert_workspace_accessible", fake_accessible)
    monkeypatch.setattr(validator, "validate_server", fake_validate)

    await ws_router.test_mcp_server_ad_hoc(1, _other_config(), _current_user=user_factory())  # type: ignore[operator]

    assert seen == [expected]
