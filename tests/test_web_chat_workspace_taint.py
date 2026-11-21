"""网页聊天 MCP 工作区令牌的「曾经不归平台方」标记

工作区归属是从绑定频道现场推出来的，只判此刻拦不住「先拿到令牌、再解绑」：平台方新建工作区
时令牌写进 `.mcp.json`，租户频道绑上来后令牌留在租户读得到的地方，租户再解绑，工作区重新
算成平台方。本文件钉住根治方案：出现过非平台方归属的工作区记进只增不减的标记，之后永远
不发也不认它的令牌；置位分写入处置位、看到就置位、启动回填三类，任何一类失效都要有对应用例变红。
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.core import auto_inject_mcp
from nekro_agent.core.logger import logger as root_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.routers.console.tenants import bind_tenant_instances
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.schemas.errors import OperationFailedError, PermissionDeniedError, UnauthorizedError
from nekro_agent.schemas.tenant import TenantInstanceBindRequest, TenantRole
from nekro_agent.services.chat_key_migration import migrate_instance_ownership
from nekro_agent.services.mcp import web_chat_auth as auth
from nekro_agent.services.mcp import web_chat_workspace_taint as taint
from nekro_agent.services.mcp.web_chat_auth import (
    WEB_CHAT_MCP_SERVER_NAME,
    build_web_chat_mcp_server_entry,
)
from nekro_agent.services.mcp.web_chat_mcp import na_app
from nekro_agent.services.user.role import Role
from nekro_agent.services.workspace import manager
from nekro_agent.services.workspace.tenant_scope import accessible_workspace_ids, assert_workspace_accessible

ADAPTER = "wechat_ilink_multi"
TENANT_ID = 5


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def workspace_root(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """工作区目录改道到临时目录，不碰真实 WORKSPACE_ROOT_DIR"""
    monkeypatch.setattr(
        manager.WorkspaceService,
        "get_workspace_dir",
        staticmethod(lambda workspace_id: tmp_path / str(workspace_id)),
    )
    return tmp_path


@pytest.fixture(autouse=True)
def isolated_auto_inject_path(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """内置 MCP 清单落到临时目录：启动回填会读写它"""
    monkeypatch.setattr(auto_inject_mcp, "_AUTO_INJECT_PATH", tmp_path / "configs" / "auto-inject-mcp.json")


@pytest.fixture(autouse=True)
def web_chat_library(monkeypatch: pytest.MonkeyPatch):
    """全局 MCP 库里只有一条内置网页聊天条目，写盘时才会走到令牌派生"""
    monkeypatch.setattr(
        manager,
        "_global_library_by_name",
        lambda: {WEB_CHAT_MCP_SERVER_NAME: build_web_chat_mcp_server_entry()},
    )


async def _seed_admin() -> DBUser:
    return await DBUser.create(
        id=1,
        username="admin",
        password="",
        adapter_key="",
        platform_userid="1",
        perm_level=Role.Admin,
        login_time=datetime.now(timezone.utc),
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


async def _make_tenant_user() -> DBUser:
    return await DBUser.create(
        id=2,
        username="tenant-admin",
        password="",
        adapter_key="",
        platform_userid="2",
        perm_level=Role.Admin,
        login_time=datetime.now(timezone.utc),
        tenant_id=TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )


async def _make_instance(instance_key: str, tenant_id: Optional[int] = None) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        display_name=instance_key,
        tenant_id=tenant_id,
    )


async def _make_workspace(
    workspace_id: int,
    *,
    enabled: bool = True,
    enabled_names: Optional[List[str]] = None,
) -> DBWorkspace:
    names = enabled_names if enabled_names is not None else ([WEB_CHAT_MCP_SERVER_NAME] if enabled else [])
    metadata = {"mcp_servers_enabled": names} if names else {}
    return await DBWorkspace.create(id=workspace_id, name=f"ws-{workspace_id}", metadata=metadata)


async def _make_channel(
    chat_key: str,
    instance_key: str,
    *,
    workspace_id: Optional[int] = None,
    channel_id: Optional[str] = None,
) -> DBChatChannel:
    return await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        channel_id=channel_id or chat_key.rsplit("-", 1)[-1],
        channel_name="频道",
        chat_key=chat_key,
        channel_type=ChatType.GROUP.value,
        workspace_id=workspace_id,
        is_active=True,
        observe_mode=False,
        data="{}",
    )


def _request(*, token: str = "", header: str = "") -> Any:
    from starlette.requests import Request

    headers = [(b"authorization", f"Bearer {header}".encode())] if header else []
    query = f"token={token}".encode() if token else b""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "query_string": query,
            "headers": headers,
        },
    )


async def _resolve(token: str) -> DBUser:
    return await na_app._resolve_mcp_user(_request(header=token))


def _workspace_token(workspace_id: int) -> str:
    return auth.build_workspace_web_chat_mcp_token(workspace_id)


def _mcp_path(workspace_id: int):
    return manager.WorkspaceService.get_workspace_dir(workspace_id) / "default" / ".mcp.json"


def _read_disk(workspace_id: int) -> Dict[str, Any]:
    return json.loads(_mcp_path(workspace_id).read_text(encoding="utf-8"))


async def _sync(workspace: DBWorkspace) -> None:
    await manager.WorkspaceService.sync_mcp_to_disk(workspace)


def _disk_token(workspace_id: int) -> str:
    headers = _read_disk(workspace_id)["mcpServers"][WEB_CHAT_MCP_SERVER_NAME]["headers"]
    return str(headers["Authorization"]).removeprefix("Bearer ")


def _marker_ids() -> List[int]:
    path = taint.taint_file()
    if not path.exists():
        return []
    return list(json.loads(path.read_text(encoding="utf-8"))["workspace_ids"])


async def _drain_scrub() -> None:
    for _ in range(10):
        tasks = list(taint._scrub_tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


# ── 1. 平台方正常使用 ────────────────────────────────────────────────────────


async def test_platform_workspace_writes_token_and_accepts_both_transports(db) -> None:  # noqa: ARG001
    await _seed_admin()
    workspace = await _make_workspace(100)

    await _sync(workspace)

    token = _disk_token(100)
    assert token == _workspace_token(100)
    assert (await _resolve(token)).username == "admin"
    assert (await na_app._resolve_mcp_user(_request(token=token))).username == "admin"


async def test_platform_and_legacy_channels_do_not_taint(db) -> None:  # noqa: ARG001
    """实例 tenant_id 为空、以及多账号化之前的空 instance_key 频道都算平台方"""
    await _seed_admin()
    await _make_instance("platform_inst")
    workspace = await _make_workspace(101)
    await _make_channel(f"{ADAPTER}-platform_inst-group_1", "platform_inst", workspace_id=101, channel_id="group_1")
    await _make_channel(f"{ADAPTER}--group_2", "", workspace_id=101, channel_id="group_2")

    await _sync(workspace)

    assert 101 not in _marker_ids()
    assert (await _resolve(_disk_token(101))).username == "admin"


# ── 2. 复核的 5 步：绑频道这一处置位本身管用 ────────────────────────────────


async def test_five_step_bind_then_unbind_does_not_revive_the_token(db) -> None:  # noqa: ARG001
    await _seed_admin()
    tenant = await DBTenant.create(id=TENANT_ID, name="tenant")
    await _make_instance("tenant_inst", tenant.id)
    workspace = await _make_workspace(200)
    channel = await _make_channel(f"{ADAPTER}-tenant_inst-group_9", "tenant_inst", channel_id="group_9")

    # 第 1、2 步：平台方工作区，写进令牌
    await _sync(workspace)
    leaked = _disk_token(200)
    assert (await _resolve(leaked)).username == "admin"

    # 第 3 步：租户频道绑上来。这里先看标记文件，不先做任何归属推导——
    # 要证明的是「绑频道」这个写入处置位本身管用，而不是后续某次推导顺手补上了
    await manager.WorkspaceService.bind_channel(workspace, channel.chat_key)
    assert 200 in _marker_ids()
    with pytest.raises(PermissionDeniedError):
        await _resolve(leaked)

    await _drain_scrub()
    assert WEB_CHAT_MCP_SERVER_NAME not in _read_disk(200)["mcpServers"]

    # 第 4、5 步：租户解绑自己的频道，工作区重新算成平台方，令牌仍然不认
    await manager.WorkspaceService.unbind_channel(workspace, channel.chat_key)
    with pytest.raises(PermissionDeniedError):
        await _resolve(leaked)
    await _sync(workspace)
    assert WEB_CHAT_MCP_SERVER_NAME not in _read_disk(200)["mcpServers"]

    # 模拟重启：内存集合清空、启动流程重扫并轮换密钥，新令牌仍然不认
    taint._memory_tainted.clear()
    await auth.init_web_chat_mcp_runtime_auth()
    with pytest.raises(PermissionDeniedError):
        await _resolve(_workspace_token(200))
    assert WEB_CHAT_MCP_SERVER_NAME not in _read_disk(200)["mcpServers"]


# ── 3. 多方共用 ──────────────────────────────────────────────────────────────


async def test_shared_workspace_is_tainted_and_token_is_403(db) -> None:  # noqa: ARG001
    await _seed_admin()
    tenant = await DBTenant.create(id=TENANT_ID, name="tenant")
    await _make_instance("platform_inst")
    await _make_instance("tenant_inst", tenant.id)
    workspace = await _make_workspace(300)
    await _make_channel(f"{ADAPTER}-platform_inst-g1", "platform_inst", workspace_id=300, channel_id="g1")
    await _make_channel(f"{ADAPTER}-tenant_inst-g2", "tenant_inst", workspace_id=300, channel_id="g2")

    with pytest.raises(PermissionDeniedError):
        await _resolve(_workspace_token(300))
    assert 300 in _marker_ids()

    await _sync(workspace)
    assert WEB_CHAT_MCP_SERVER_NAME not in _read_disk(300)["mcpServers"]


# ── 4. 实例改归属 ────────────────────────────────────────────────────────────


def _platform_owner() -> DBUser:
    return DBUser(
        username="platform",
        password="",
        adapter_key="",
        platform_userid="9",
        perm_level=Role.Super,
        login_time=datetime.now(timezone.utc),
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


async def test_instance_reassignment_taints_and_release_keeps_it_tainted(db) -> None:  # noqa: ARG001
    await _seed_admin()
    await DBTenant.create(id=TENANT_ID, name="tenant")
    await _make_instance("inst", None)
    workspace = await _make_workspace(400)
    await _make_channel(f"{ADAPTER}-inst-g", "inst", workspace_id=400)
    await _sync(workspace)
    token = _disk_token(400)
    assert (await _resolve(token)).username == "admin"

    owner = _platform_owner()
    await bind_tenant_instances(TENANT_ID, TenantInstanceBindRequest(instance_keys=["inst"]), owner)
    assert 400 in _marker_ids()
    with pytest.raises(PermissionDeniedError):
        await _resolve(token)

    await bind_tenant_instances(TENANT_ID, TenantInstanceBindRequest(instance_keys=[]), owner)
    assert 400 in _marker_ids()
    with pytest.raises(PermissionDeniedError):
        await _resolve(token)


async def test_instance_reassignment_is_refused_when_taint_write_fails(
    db,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await DBTenant.create(id=TENANT_ID, name="tenant")
    await _make_instance("inst2", None)
    await _make_workspace(401)
    await _make_channel(f"{ADAPTER}-inst2-g", "inst2", workspace_id=401)

    def broken_write(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(taint, "_write", broken_write)

    with pytest.raises(OperationFailedError):
        await bind_tenant_instances(
            TENANT_ID,
            TenantInstanceBindRequest(instance_keys=["inst2"]),
            _platform_owner(),
        )

    row = await DBAdapterInstance.get(instance_key="inst2")
    assert row.tenant_id is None
    assert 401 not in taint._memory_tainted


# ── 5. 频道迁移实例 ──────────────────────────────────────────────────────────


async def test_channel_migration_to_tenant_instance_taints(db) -> None:  # noqa: ARG001
    await _seed_admin()
    tenant = await DBTenant.create(id=TENANT_ID, name="tenant")
    await _make_instance("A")
    await _make_instance("B", tenant.id)
    workspace = await _make_workspace(500)
    await _make_channel(f"{ADAPTER}-A-group_1", "A", workspace_id=500, channel_id="group_1")
    await _sync(workspace)
    token = _disk_token(500)

    await migrate_instance_ownership(ADAPTER, "A", "B")

    assert 500 in _marker_ids()
    with pytest.raises(PermissionDeniedError):
        await _resolve(token)


async def test_channel_migration_between_platform_instances_does_not_taint(db) -> None:  # noqa: ARG001
    await _make_instance("A")
    await _make_instance("B")
    await _make_workspace(501)
    await _make_channel(f"{ADAPTER}-A-group_1", "A", workspace_id=501, channel_id="group_1")

    await migrate_instance_ownership(ADAPTER, "A", "B")

    assert 501 not in _marker_ids()


# ── 6. 历史频道认领 ──────────────────────────────────────────────────────────


async def test_claiming_legacy_channel_for_tenant_instance_taints(db) -> None:  # noqa: ARG001
    tenant = await DBTenant.create(id=TENANT_ID, name="tenant")
    await _make_instance("tenant_inst", tenant.id)
    await _make_workspace(600)
    await _make_channel(f"{ADAPTER}--group_1", "", workspace_id=600, channel_id="group_1")

    claimed = await DBChatChannel._claim_unclaimed_channel(
        ADAPTER,
        "group_1",
        f"{ADAPTER}-tenant_inst-group_1",
        "tenant_inst",
    )

    assert claimed is not None
    assert 600 in _marker_ids()


async def test_claiming_legacy_channel_for_platform_instance_does_not_taint(db) -> None:  # noqa: ARG001
    await _make_instance("platform_inst")
    await _make_workspace(601)
    await _make_channel(f"{ADAPTER}--group_1", "", workspace_id=601, channel_id="group_1")

    claimed = await DBChatChannel._claim_unclaimed_channel(
        ADAPTER,
        "group_1",
        f"{ADAPTER}-platform_inst-group_1",
        "platform_inst",
    )

    assert claimed is not None
    assert 601 not in _marker_ids()


async def test_claim_survives_taint_write_failure_and_tokens_stay_closed(
    db,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_admin()
    tenant = await DBTenant.create(id=TENANT_ID, name="tenant")
    await _make_instance("tenant_inst", tenant.id)
    await _make_workspace(602)
    await _make_channel(f"{ADAPTER}--group_1", "", workspace_id=602, channel_id="group_1")
    platform_workspace = await _make_workspace(603)
    platform_token = _workspace_token(603)

    original_write = taint._write
    state = {"fail": True}

    def flaky_write(workspace_ids: Any, *, seeded: bool) -> None:
        if state["fail"]:
            raise OSError("disk full")
        original_write(workspace_ids, seeded=seeded)

    monkeypatch.setattr(taint, "_write", flaky_write)

    claimed = await DBChatChannel._claim_unclaimed_channel(
        ADAPTER,
        "group_1",
        f"{ADAPTER}-tenant_inst-group_1",
        "tenant_inst",
    )

    assert claimed is not None
    assert taint._rebuild_pending is True
    with pytest.raises(PermissionDeniedError):
        await _resolve(platform_token)

    state["fail"] = False
    await taint.scan_workspace_taint()

    assert taint._rebuild_pending is False
    assert 602 in _marker_ids()
    assert (await _resolve(platform_token)).username == "admin"
    assert (await _resolve(_workspace_token(platform_workspace.id))).username == "admin"


# ── 7. 看到就置位 ────────────────────────────────────────────────────────────


async def test_owner_derivation_taints_a_directly_wired_channel(db) -> None:  # noqa: ARG001
    tenant = await DBTenant.create(id=TENANT_ID, name="tenant")
    await _make_instance("tenant_inst", tenant.id)
    user = await _make_tenant_user()
    await _make_workspace(700)
    channel = await _make_channel(f"{ADAPTER}-tenant_inst-g", "tenant_inst", channel_id="g")

    # 先把记录建好，让「看到就置位」不是靠首次扫描兜住的
    await taint.scan_workspace_taint()
    assert 700 not in _marker_ids()

    # 绕过所有写入处，直接把租户频道挂到工作区上
    await DBChatChannel.filter(id=channel.id).update(workspace_id=700)

    assert (await assert_workspace_accessible(user, 700)).id == 700
    assert 700 in _marker_ids()


async def test_batch_owner_derivation_taints_directly_wired_channels(db) -> None:  # noqa: ARG001
    tenant = await DBTenant.create(id=TENANT_ID, name="tenant")
    await _make_instance("tenant_inst", tenant.id)
    user = await _make_tenant_user()
    await _make_workspace(701)
    channel = await _make_channel(f"{ADAPTER}-tenant_inst-g", "tenant_inst", channel_id="g")
    await taint.scan_workspace_taint()

    await DBChatChannel.filter(id=channel.id).update(workspace_id=701)
    ids = await accessible_workspace_ids(user)

    assert ids is not None and 701 in ids
    assert 701 in _marker_ids()


# ── 8. CC 委托兜底 ──────────────────────────────────────────────────────────


async def test_cc_delegate_taints_tenant_workspace_and_clears_platform_one(db) -> None:  # noqa: ARG001
    tenant = await DBTenant.create(id=TENANT_ID, name="tenant")
    await _make_instance("tenant_inst", tenant.id)
    tenant_workspace = await _make_workspace(800)
    await _make_channel(f"{ADAPTER}-tenant_inst-g", "tenant_inst", workspace_id=800, channel_id="g")

    assert await taint.taint_workspace_if_non_platform(tenant_workspace, reason="CC 委托") is True
    assert 800 in _marker_ids()

    platform_workspace = await _make_workspace(801)
    assert await taint.taint_workspace_if_non_platform(platform_workspace, reason="CC 委托") is False
    assert 801 not in _marker_ids()


async def test_cc_delegate_treats_unreadable_ownership_as_non_platform(
    db,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nekro_agent.services.workspace import network_guard

    workspace = await _make_workspace(802)

    async def broken(_workspace: DBWorkspace) -> str:
        raise RuntimeError("数据库不可用")

    monkeypatch.setattr(network_guard, "resolve_workspace_owner_group_strict", broken)

    assert await taint.taint_workspace_if_non_platform(workspace, reason="CC 委托") is True
    assert 802 in _marker_ids()


# ── 9. 记录失效与写盘失败：一律不合格 ───────────────────────────────────────


async def test_broken_record_is_moved_aside_and_rebuilt(db, workspace_root) -> None:  # noqa: ARG001
    await _seed_admin()
    workspace = await _make_workspace(900)
    await taint.scan_workspace_taint()
    taint.taint_file().write_text("not json", encoding="utf-8")

    assert await taint.is_workspace_web_chat_token_eligible(workspace) is True

    broken = list(workspace_root.glob("web_chat_mcp_tainted_workspaces.json.broken-*"))
    assert len(broken) == 1
    assert taint.taint_file().exists()
    state = json.loads(taint.taint_file().read_text(encoding="utf-8"))
    assert state["seeded"] is True


async def test_scan_failure_keeps_every_workspace_token_closed(
    db,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_admin()
    await _make_workspace(901)
    await taint.scan_workspace_taint()
    taint.taint_file().write_text("not json", encoding="utf-8")

    async def broken_scan() -> None:
        raise RuntimeError("数据库不可用")

    monkeypatch.setattr(taint, "scan_workspace_taint", broken_scan)

    with pytest.raises(PermissionDeniedError):
        await _resolve(_workspace_token(901))


async def test_bind_channel_is_refused_when_taint_write_fails(
    db,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await DBTenant.create(id=TENANT_ID, name="tenant")
    await _make_instance("tenant_inst", tenant.id)
    workspace = await _make_workspace(902)
    channel = await _make_channel(f"{ADAPTER}-tenant_inst-g", "tenant_inst", channel_id="g")

    def broken_write(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(taint, "_write", broken_write)

    with pytest.raises(OperationFailedError):
        await manager.WorkspaceService.bind_channel(workspace, channel.chat_key)

    await channel.refresh_from_db()
    assert channel.workspace_id is None
    assert 902 not in taint._memory_tainted


# ── 10. 启动回填 ────────────────────────────────────────────────────────────


async def test_startup_backfill_seeds_record_and_only_writes_eligible_workspaces(
    db,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await DBTenant.create(id=TENANT_ID, name="tenant")
    await _make_instance("tenant_inst", tenant.id)
    tenant_workspace = await _make_workspace(1001)
    await _make_channel(f"{ADAPTER}-tenant_inst-g", "tenant_inst", workspace_id=1001, channel_id="g")
    platform_workspace = await _make_workspace(1002)

    # 把「启动回填」与「重写引用」分开观察：这里只让 init 做扫描与轮换，文件形态随后手动同步
    async def noop_refresh(_server_name: str) -> None:
        return None

    monkeypatch.setattr(
        manager.WorkspaceService,
        "refresh_global_mcp_server_references",
        staticmethod(noop_refresh),
    )
    captured: List[str] = []
    sink_id = root_logger.add(lambda message: captured.append(str(message)), level="WARNING", format="{message}")
    try:
        await auth.init_web_chat_mcp_runtime_auth()
    finally:
        root_logger.remove(sink_id)

    state = json.loads(taint.taint_file().read_text(encoding="utf-8"))
    assert state["seeded"] is True
    assert 1001 in state["workspace_ids"]
    assert 1002 not in state["workspace_ids"]
    assert any("历史无从追溯" in line for line in captured)

    await _sync(tenant_workspace)
    await _sync(platform_workspace)
    assert WEB_CHAT_MCP_SERVER_NAME not in _read_disk(1001)["mcpServers"]
    assert _disk_token(1002) == _workspace_token(1002)


# ── 11. 入口不回落 JWT ──────────────────────────────────────────────────────


async def test_entry_does_not_fall_back_to_jwt_for_a_workspace_token(
    db,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = DBUser(
        username="owner",
        password="",
        adapter_key="",
        platform_userid="9",
        perm_level=Role.Super,
        login_time=datetime.now(timezone.utc),
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )

    async def fake_current_user(_request: Any) -> DBUser:
        return owner

    async def fake_active(user: DBUser) -> DBUser:
        return user

    monkeypatch.setattr(na_app, "get_current_user", fake_current_user)
    monkeypatch.setattr(na_app, "get_current_active_user", fake_active)

    with pytest.raises(UnauthorizedError):
        await na_app._resolve_mcp_user(_request(token="nwc1.伪造", header="some-jwt"))


async def test_valid_mac_for_a_missing_workspace_is_401(db) -> None:  # noqa: ARG001
    await _seed_admin()

    with pytest.raises(UnauthorizedError):
        await _resolve(_workspace_token(9999))


# ── 12. 写盘判资格 ──────────────────────────────────────────────────────────


async def test_tainted_platform_workspace_has_no_disk_entry(db) -> None:  # noqa: ARG001
    await _seed_admin()
    workspace = await _make_workspace(1200)
    taint.taint_workspaces([1200], reason="用例")

    await _sync(workspace)

    assert WEB_CHAT_MCP_SERVER_NAME not in _read_disk(1200)["mcpServers"]


async def test_manual_entry_is_recognized_and_its_headers_are_overwritten(
    db,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manual_library = {
        "my-chat": {
            "name": "my-chat",
            "type": "http",
            "url": "http://example.com/api/mcp/web-chat/mcp",
            "headers": {"Authorization": "Bearer external-secret"},
            "validation": {"status": "validated"},
        },
    }
    monkeypatch.setattr(manager, "_global_library_by_name", lambda: manual_library)
    workspace = await _make_workspace(1201, enabled_names=["my-chat"])

    await _sync(workspace)

    written = _read_disk(1201)
    entry = written["mcpServers"]["my-chat"]
    assert entry["headers"] == auth.build_workspace_web_chat_mcp_headers(1201)
    assert "external-secret" not in json.dumps(written)


# ── 13. 读标记文件出错：不挪文件、按不可用处理 ──────────────────────────────


async def test_read_error_keeps_the_marker_file_in_place(
    db,  # noqa: ARG001
    workspace_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_admin()
    tenant = await DBTenant.create(id=TENANT_ID, name="tenant")
    await _make_instance("tenant_inst", tenant.id)
    await _make_workspace(1300)
    tenant_channel = await _make_channel(f"{ADAPTER}-tenant_inst-g", "tenant_inst", workspace_id=1300)
    await taint.scan_workspace_taint()
    await _drain_scrub()
    assert 1300 in _marker_ids()

    platform_workspace = await _make_workspace(1301)
    platform_token = _workspace_token(platform_workspace.id)
    assert (await _resolve(platform_token)).username == "admin"

    marker = taint.taint_file()
    original_read_bytes = Path.read_bytes

    def permission_denied(self: Path) -> bytes:
        if self == marker:
            raise PermissionError("标记文件暂时读不到")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", permission_denied)

    # 读文件本身出错：这次判定按记录不可用处理
    with pytest.raises(PermissionDeniedError):
        await _resolve(platform_token)
    assert await taint.ensure_taint_record() is False

    # 文件既没被挪走，也没被改写：挪走会丢掉「曾经不归平台方」的历史
    assert marker.exists()
    assert not list(workspace_root.glob(f"{marker.name}.broken-*"))
    assert 1300 in json.loads(marker.read_text(encoding="utf-8"))["workspace_ids"]

    # 恢复读取。W 此刻已经没有任何频道，按当前归属算平台方；清空内存集合模拟重启，
    # 令牌仍然被文件里的历史标记挡住
    monkeypatch.setattr(Path, "read_bytes", original_read_bytes)
    await DBChatChannel.filter(id=tenant_channel.id).delete()
    taint._memory_tainted.clear()

    assert (await _resolve(platform_token)).username == "admin"
    with pytest.raises(PermissionDeniedError):
        await _resolve(_workspace_token(1300))


# ── 14. 插件取用工作区：按取用方频道置位 ────────────────────────────────────


async def test_channel_workspace_access_taints_tenant_channel_only(
    db,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await DBTenant.create(id=TENANT_ID, name="tenant")
    await _make_instance("tenant_inst", tenant.id)
    await _make_instance("platform_inst")
    await _make_workspace(1400)
    await _make_workspace(1401)
    tenant_channel = await _make_channel(f"{ADAPTER}-tenant_inst-g1", "tenant_inst", channel_id="g1")
    platform_channel = await _make_channel(f"{ADAPTER}-platform_inst-g2", "platform_inst", channel_id="g2")

    # 先建档：此刻两个工作区都还没绑频道，扫描不会标记它们；之后直接改库挂上频道，
    # 绕过 bind_channel，证明是「取用工作区」这条路径置位的
    await taint.scan_workspace_taint()
    assert 1400 not in _marker_ids()
    assert 1401 not in _marker_ids()
    await DBChatChannel.filter(id=tenant_channel.id).update(workspace_id=1400)
    await DBChatChannel.filter(id=platform_channel.id).update(workspace_id=1401)

    tenant_workspace = await AgentCtx(from_chat_key=tenant_channel.chat_key).get_bound_workspace()
    assert tenant_workspace is not None
    assert tenant_workspace.id == 1400
    assert 1400 in _marker_ids()

    platform_workspace = await AgentCtx(from_chat_key=platform_channel.chat_key).get_bound_workspace()
    assert platform_workspace is not None
    assert platform_workspace.id == 1401
    assert 1401 not in _marker_ids()

    # 读不到实例归属时只记警告：取用工作区照常返回，不抛
    async def broken_owner_tenant(_adapter_key: str, _instance_key: str) -> Optional[int]:
        raise RuntimeError("实例归属查不到")

    monkeypatch.setattr(taint, "instance_owner_tenant", broken_owner_tenant)
    again = await AgentCtx(from_chat_key=tenant_channel.chat_key).get_bound_workspace()
    assert again is not None
    assert again.id == 1400


# ── 15. 绑频道：改库后按整个工作区复核 ──────────────────────────────────────


async def test_bind_channel_rechecks_ownership_after_db_write(
    db,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await DBTenant.create(id=TENANT_ID, name="tenant")
    await _make_instance("tenant_inst", tenant.id)
    workspace = await _make_workspace(1500)
    channel = await _make_channel(f"{ADAPTER}-tenant_inst-g", "tenant_inst", channel_id="g")
    await taint.scan_workspace_taint()
    assert 1500 not in _marker_ids()

    original = taint.instance_owner_tenant
    calls = {"count": 0}

    async def raced_owner_tenant(adapter_key: str, instance_key: str) -> Optional[int]:
        calls["count"] += 1
        # 第一次：查的时候实例还归平台方；之后（改库后复核）查到真实归属
        if calls["count"] == 1:
            return None
        return await original(adapter_key, instance_key)

    monkeypatch.setattr(taint, "instance_owner_tenant", raced_owner_tenant)

    await manager.WorkspaceService.bind_channel(workspace, channel.chat_key)

    await channel.refresh_from_db()
    assert channel.workspace_id == 1500
    assert 1500 in _marker_ids()


# ── 16. 实例划转：改库后补一遍窗口里新绑的频道 ──────────────────────────────


async def test_instance_reassignment_rechecks_after_db_write(
    db,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await DBTenant.create(id=TENANT_ID, name="tenant")
    await _make_instance("inst")
    await _make_workspace(1600)
    await _make_channel(f"{ADAPTER}-inst-g1", "inst", channel_id="g1")
    racer = await _make_channel(f"{ADAPTER}-inst-g2", "inst", channel_id="g2")
    await taint.scan_workspace_taint()
    assert 1600 not in _marker_ids()

    original = taint.taint_workspaces_bound_to_instance_keys
    calls = {"count": 0}

    async def raced(
        instance_keys: Any,
        *,
        reason: str,
        refuse_on_failure: bool = False,
    ) -> Any:
        result = await original(instance_keys, reason=reason, refuse_on_failure=refuse_on_failure)
        calls["count"] += 1
        if calls["count"] == 1:
            # 模拟竞态：改库前的置位查完之后，并发的绑定恰好落在窗口里
            await DBChatChannel.filter(id=racer.id).update(workspace_id=1600)
        return result

    monkeypatch.setattr(taint, "taint_workspaces_bound_to_instance_keys", raced)

    await bind_tenant_instances(
        TENANT_ID,
        TenantInstanceBindRequest(instance_keys=["inst"]),
        _platform_owner(),
    )

    assert (await DBAdapterInstance.get(instance_key="inst")).tenant_id == TENANT_ID
    assert 1600 in _marker_ids()


# ── 17. 频道迁移：改库后补一遍、冲突合并、置位失败拒绝 ──────────────────────


async def test_channel_migration_rechecks_after_db_write(
    db,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await DBTenant.create(id=TENANT_ID, name="tenant")
    await _make_instance("A")
    await _make_instance("B", tenant.id)
    await _make_workspace(1700)
    await _make_channel(f"{ADAPTER}-A-g1", "A", channel_id="g1")
    racer = await _make_channel(f"{ADAPTER}-A-g2", "A", channel_id="g2")
    await taint.scan_workspace_taint()
    assert 1700 not in _marker_ids()

    original = taint.taint_workspaces_bound_to_instance_keys
    calls = {"count": 0}

    async def raced(
        instance_keys: Any,
        *,
        reason: str,
        refuse_on_failure: bool = False,
    ) -> Any:
        result = await original(instance_keys, reason=reason, refuse_on_failure=refuse_on_failure)
        calls["count"] += 1
        if calls["count"] == 1:
            # 模拟竞态：开始置位之后、真正搬频道之前，另一个频道挂上了工作区
            await DBChatChannel.filter(id=racer.id).update(workspace_id=1700)
        return result

    monkeypatch.setattr(taint, "taint_workspaces_bound_to_instance_keys", raced)

    await migrate_instance_ownership(ADAPTER, "A", "B")

    assert 1700 in _marker_ids()


async def test_channel_migration_conflict_absorbs_workspace_into_target_row(db) -> None:  # noqa: ARG001
    await _seed_admin()
    tenant = await DBTenant.create(id=TENANT_ID, name="tenant")
    await _make_instance("A")
    await _make_instance("B", tenant.id)
    await _make_workspace(1800)
    source = await _make_channel(f"{ADAPTER}-A-group_1", "A", workspace_id=1800, channel_id="group_1")
    target = await _make_channel(f"{ADAPTER}-B-group_1", "B", channel_id="group_1")

    await migrate_instance_ownership(ADAPTER, "A", "B")

    # 冲突时目标行幸存，工作区绑定只补空缺
    survivor = await DBChatChannel.get(id=target.id)
    assert survivor.instance_key == "B"
    assert survivor.workspace_id == 1800
    assert not await DBChatChannel.filter(id=source.id).exists()
    assert 1800 in _marker_ids()
    with pytest.raises(PermissionDeniedError):
        await _resolve(_workspace_token(1800))


async def test_channel_migration_is_refused_when_taint_write_fails(
    db,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await DBTenant.create(id=TENANT_ID, name="tenant")
    await _make_instance("A")
    await _make_instance("B", tenant.id)
    await _make_workspace(1900)
    channel = await _make_channel(f"{ADAPTER}-A-group_1", "A", workspace_id=1900, channel_id="group_1")

    def broken_write(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(taint, "_write", broken_write)

    with pytest.raises(OperationFailedError):
        await migrate_instance_ownership(ADAPTER, "A", "B")

    await channel.refresh_from_db()
    assert channel.instance_key == "A"
    assert channel.workspace_id == 1900
