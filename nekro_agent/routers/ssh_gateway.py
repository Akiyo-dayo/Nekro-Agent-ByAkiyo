"""SSH 服务器凭据的后台路由

## 两条前缀

工作区子资源那几条挂在 `/workspaces/{workspace_id}/ssh-credentials` 下，与工作区其他
子资源同一个前缀；平台方的全局列表与审计挂在 `/ssh-gateway` 下。父路由只做汇总，
这样契约测试看到的仍是完整的路径。

## 角色之外还有一层

`Role.Admin` 挡的是只读成员；真正决定"能看见哪一行"的是服务层的可见性矩阵。
每条吃 `workspace_id` 的端点都先过 `assert_workspace_accessible`，越权与不存在
返回同一句话。查看明文单独一条路径，响应头带 `Cache-Control: no-store`，避免明文
停在浏览器缓存、开发者工具或截图里。
"""

import asyncio
from typing import List, Optional, Set, Tuple

from fastapi import APIRouter, Depends, Query, Response

from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.schemas.ssh_gateway import (
    ImportReport,
    SshActionOkResponse,
    SshAuditListResponse,
    SshBoundInstance,
    SshCredentialCreate,
    SshCredentialListResponse,
    SshCredentialSecretResponse,
    SshCredentialTestResponse,
    SshCredentialUpdate,
    SshCredentialView,
    SshGatewayStatusResponse,
    SshHostKeyAcceptRequest,
    SshHostKeyView,
)
from nekro_agent.services.ssh_gateway import audit_queue, row_state
from nekro_agent.services.ssh_gateway import constants as C
from nekro_agent.services.ssh_gateway.authority import TargetSpec
from nekro_agent.services.ssh_gateway.credentials import (
    ChatRegisteredEditForbiddenError,
    ssh_credential_service,
)
from nekro_agent.services.ssh_gateway.destination import check_user_destination
from nekro_agent.services.ssh_gateway.import_resources import import_ssh_resources
from nekro_agent.services.ssh_gateway.secrets import decrypt_secret
from nekro_agent.services.ssh_gateway.supervisor import supervisor
from nekro_agent.services.ssh_gateway.validation import host_key_fingerprint
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import accessible_instance_refs, get_tenant_scope
from nekro_agent.services.workspace.tenant_scope import assert_workspace_accessible

router = APIRouter(tags=["SSH Gateway"])
workspace_router = APIRouter(prefix="/workspaces")
gateway_router = APIRouter(prefix="/ssh-gateway")


async def _visible_instance_refs(user: DBUser) -> Optional[Set[Tuple[str, str]]]:
    """一次算出租户可见的实例对；平台方返回 None 表示不受限

    路由先算一次并交给服务层，避免同一请求里把 `get_tenant_scope` 与实例查询走两遍。
    这个函数本身也是契约测试认识的"走到作用域守卫"的入口。
    """
    if get_tenant_scope(user) is None:
        return None
    return await accessible_instance_refs(user)


@workspace_router.get(
    "/{workspace_id}/ssh-credentials",
    summary="列出工作区的 SSH 服务器凭据",
    response_model=SshCredentialListResponse,
)
@require_role(Role.Admin)
async def list_ssh_credentials(
    workspace_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> SshCredentialListResponse:
    await assert_workspace_accessible(_current_user, workspace_id)
    items = await ssh_credential_service.list_credentials(_current_user, workspace_id)
    # 工作区详情接口不带频道信息：S3 提醒与「归属实例」下拉从这里拿，租户只看到自己的实例
    from nekro_agent.models.db_chat_channel import DBChatChannel

    refs = await _visible_instance_refs(_current_user)
    channels = await DBChatChannel.filter(workspace_id=workspace_id).values_list("adapter_key", "instance_key")
    visible = [
        (str(adapter or ""), str(instance or ""))
        for adapter, instance in channels
        if refs is None or (str(adapter or ""), str(instance or "")) in refs
    ]
    bound_instances = [
        SshBoundInstance(adapter_key=adapter, instance_key=instance)
        for adapter, instance in sorted(set(visible))
        if adapter and instance
    ]
    return SshCredentialListResponse(
        items=items,
        total=len(items),
        bound_channel_count=len(visible),
        bound_instances=bound_instances,
    )


@workspace_router.post(
    "/{workspace_id}/ssh-credentials",
    summary="新建 SSH 服务器凭据",
    response_model=SshCredentialView,
)
@require_role(Role.Admin)
async def create_ssh_credential(
    workspace_id: int,
    body: SshCredentialCreate,
    _current_user: DBUser = Depends(get_current_active_user),
) -> SshCredentialView:
    await assert_workspace_accessible(_current_user, workspace_id)
    return await ssh_credential_service.create_credential(_current_user, workspace_id, body)


@workspace_router.get(
    "/{workspace_id}/ssh-credentials/{credential_id}",
    summary="查看 SSH 服务器凭据详情",
    response_model=SshCredentialView,
)
@require_role(Role.Admin)
async def get_ssh_credential(
    workspace_id: int,
    credential_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> SshCredentialView:
    await assert_workspace_accessible(_current_user, workspace_id)
    return await ssh_credential_service.get_credential(_current_user, workspace_id, credential_id)


@workspace_router.put(
    "/{workspace_id}/ssh-credentials/{credential_id}",
    summary="修改 SSH 服务器凭据",
    response_model=SshCredentialView,
)
@require_role(Role.Admin)
async def update_ssh_credential(
    workspace_id: int,
    credential_id: int,
    body: SshCredentialUpdate,
    _current_user: DBUser = Depends(get_current_active_user),
) -> SshCredentialView:
    await assert_workspace_accessible(_current_user, workspace_id)
    return await ssh_credential_service.update_credential(_current_user, workspace_id, credential_id, body)


@workspace_router.delete(
    "/{workspace_id}/ssh-credentials/{credential_id}",
    summary="删除 SSH 服务器凭据",
    response_model=SshActionOkResponse,
)
@require_role(Role.Admin)
async def delete_ssh_credential(
    workspace_id: int,
    credential_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> SshActionOkResponse:
    await assert_workspace_accessible(_current_user, workspace_id)
    await ssh_credential_service.delete_credential(_current_user, workspace_id, credential_id)
    return SshActionOkResponse(message="已删除")


@workspace_router.post(
    "/{workspace_id}/ssh-credentials/{credential_id}/disable",
    summary="停用 SSH 服务器凭据",
    response_model=SshCredentialView,
)
@require_role(Role.Admin)
async def disable_ssh_credential(
    workspace_id: int,
    credential_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> SshCredentialView:
    await assert_workspace_accessible(_current_user, workspace_id)
    return await ssh_credential_service.set_status(
        _current_user,
        workspace_id,
        credential_id,
        enable=False,
    )


@workspace_router.post(
    "/{workspace_id}/ssh-credentials/{credential_id}/enable",
    summary="启用 SSH 服务器凭据",
    response_model=SshCredentialView,
)
@require_role(Role.Admin)
async def enable_ssh_credential(
    workspace_id: int,
    credential_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> SshCredentialView:
    await assert_workspace_accessible(_current_user, workspace_id)
    return await ssh_credential_service.set_status(
        _current_user,
        workspace_id,
        credential_id,
        enable=True,
    )


@workspace_router.get(
    "/{workspace_id}/ssh-credentials/{credential_id}/secret",
    summary="查看 SSH 服务器凭据明文",
    response_model=SshCredentialSecretResponse,
)
@require_role(Role.Admin)
async def get_ssh_credential_secret(
    workspace_id: int,
    credential_id: int,
    response: Response,
    _current_user: DBUser = Depends(get_current_active_user),
) -> SshCredentialSecretResponse:
    await assert_workspace_accessible(_current_user, workspace_id)
    secret = await ssh_credential_service.reveal_secret(_current_user, workspace_id, credential_id)
    response.headers["Cache-Control"] = "no-store"
    return secret


def _host_key_views(entries: List[str]) -> List[SshHostKeyView]:
    views: List[SshHostKeyView] = []
    for item in entries:
        text = str(item or "")
        try:
            fingerprint = host_key_fingerprint(text)
        except ValidationError:
            fingerprint = ""
        views.append(SshHostKeyView(entry=text, fingerprint=fingerprint))
    return views


def _audit_cred_test(
    row: object,
    *,
    actor: str,
    ok: bool,
    reason: str,
    fingerprint: str,
) -> None:
    audit_queue.enqueue(
        workspace_id=int(getattr(row, "workspace_id", 0) or 0),
        credential_id=int(getattr(row, "id", 0) or 0),
        owner_kind=str(getattr(row, "owner_kind", "") or ""),
        owner_account=str(getattr(row, "owner_account", "") or ""),
        adapter_key=str(getattr(row, "adapter_key", "") or ""),
        instance_key=str(getattr(row, "instance_key", "") or ""),
        event=C.EVENT_CRED_TEST,
        allowed=bool(ok),
        reason=str(reason or ""),
        task_ref="",
        lease_id="",
        conn_id="",
        triggered_by=[],
        channel_kind="",
        detail="test",
        input_head="",
        exit_status=None,
        bytes_up=None,
        bytes_down=None,
        duration_ms=None,
        host_key_fp=str(fingerprint or ""),
        actor=str(actor or ""),
    )


def _test_response(
    row: object,
    *,
    ok: bool,
    reason: str,
    message: str,
    fingerprint: str = "",
    pinned: bool = False,
    presented_entry: str = "",
    presented_fingerprint: str = "",
) -> SshCredentialTestResponse:
    return SshCredentialTestResponse(
        ok=bool(ok),
        reason=str(reason or ""),
        message=str(message or ""),
        fingerprint=str(fingerprint or ""),
        pinned=bool(pinned),
        presented_entry=str(presented_entry or ""),
        presented_fingerprint=str(presented_fingerprint or ""),
        host_keys=_host_key_views([str(item) for item in (getattr(row, "host_keys", None) or [])]),
    )


@workspace_router.post(
    "/{workspace_id}/ssh-credentials/{credential_id}/test",
    summary="测试 SSH 服务器凭据",
    response_model=SshCredentialTestResponse,
)
@require_role(Role.Admin)
async def test_ssh_credential(
    workspace_id: int,
    credential_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> SshCredentialTestResponse:
    """登录一次、只认证、不开会话

    这条路径不看网关总开关：先把凭据备好、测试通过、再开闸是正常用法。聊天登记
    的行也允许测试，因为这里不返回任何凭据明文。用户行按当前解析结果过目的地检查，
    与真正授权时同一套判据。
    """
    await assert_workspace_accessible(_current_user, workspace_id)
    row, _scope = await ssh_credential_service._load_visible(_current_user, workspace_id, credential_id)

    password, password_source = decrypt_secret(str(row.password_enc or ""))
    private_key, key_source = decrypt_secret(str(row.private_key_enc or ""))
    passphrase, passphrase_source = decrypt_secret(str(row.passphrase_enc or ""))
    if "undecryptable" in (password_source, key_source, passphrase_source):
        _audit_cred_test(row, actor=_current_user.username, ok=False, reason=C.REASON_UNDECRYPTABLE, fingerprint="")
        return _test_response(
            row,
            ok=False,
            reason=C.REASON_UNDECRYPTABLE,
            message="密钥解不开，请恢复原密钥或重新填写",
        )

    connect_address = str(row.host or "")
    if str(row.owner_kind or "") == C.OWNER_USER:
        verdict = await check_user_destination(str(row.host or ""))
        if not verdict.ok or not verdict.addresses:
            reason = verdict.reason or C.REASON_TARGET_BLOCKED
            _audit_cred_test(row, actor=_current_user.username, ok=False, reason=reason, fingerprint="")
            return _test_response(row, ok=False, reason=reason, message="这台服务器的地址不允许连接")
        connect_address = str(verdict.addresses[0])

    from nekro_agent.services.ssh_gateway.upstream import (  # noqa: PLC0415 - 总开关关着时也不必导入 asyncssh
        UpstreamError,
        open_upstream,
        probe_host_key,
    )

    try:
        presented_entry = await probe_host_key(connect_address, int(row.port))
    except UpstreamError as exc:
        _audit_cred_test(row, actor=_current_user.username, ok=False, reason=exc.reason, fingerprint="")
        return _test_response(row, ok=False, reason=exc.reason, message=exc.message)
    presented_fingerprint = host_key_fingerprint(presented_entry)
    row_state.remember_pending_host_key(int(row.id), presented_entry, presented_fingerprint)

    target = TargetSpec(
        credential_id=int(row.id),
        workspace_id=int(row.workspace_id),
        alias=str(row.alias or ""),
        owner_kind=str(row.owner_kind or ""),
        host=str(row.host or ""),
        connect_address=connect_address,
        port=int(row.port),
        username=str(row.username or ""),
        password=password,
        private_key=private_key,
        passphrase=passphrase,
        # 测试自己拿到了这一把，先钉住它，避免 open_upstream 用 gateway 身份抢先记；
        # 成功后由下面按当前操作者补一条 host_key 审计，后台才知道是谁确认的。
        host_keys=[str(item) for item in (row.host_keys or [])] or [presented_entry],
    )
    try:
        conn = await open_upstream(target)
    except UpstreamError as exc:
        _audit_cred_test(
            row,
            actor=_current_user.username,
            ok=False,
            reason=exc.reason,
            fingerprint=presented_fingerprint if exc.reason == C.REASON_HOST_KEY_MISMATCH else "",
        )
        return _test_response(
            row,
            ok=False,
            reason=exc.reason,
            message=exc.message,
            fingerprint=presented_fingerprint if exc.reason == C.REASON_HOST_KEY_MISMATCH else "",
            presented_entry=presented_entry if exc.reason == C.REASON_HOST_KEY_MISMATCH else "",
            presented_fingerprint=presented_fingerprint if exc.reason == C.REASON_HOST_KEY_MISMATCH else "",
        )

    try:
        server_key = conn.get_server_host_key()
        if server_key is not None:
            exported = server_key.export_public_key("openssh")
            presented_entry = exported.decode("ascii", "replace").strip() if isinstance(exported, bytes) else str(exported).strip()
            presented_fingerprint = host_key_fingerprint(presented_entry)
    finally:
        conn.close()
        try:
            await asyncio.wait_for(conn.wait_closed(), timeout=5.0)
        except Exception:  # noqa: BLE001 - 测试已经成功，关连接慢不该报失败
            pass

    pinned = False
    if not row.host_keys:
        pinned = await row_state.pin_host_key(int(row.id), presented_entry, actor=_current_user.username)
    row_state.remember_pending_host_key(int(row.id), presented_entry, presented_fingerprint)
    fresh = await DBWsSshCredential.get_or_none(id=int(row.id))
    if fresh is not None:
        row = fresh
    # 这组凭据刚登录成功：登不上的行在 10 分钟内可以不改凭据直接启用
    row_state.note_test_passed(row)
    _audit_cred_test(
        row,
        actor=_current_user.username,
        ok=True,
        reason="",
        fingerprint=presented_fingerprint,
    )
    return _test_response(
        row,
        ok=True,
        reason="",
        message="连接成功",
        fingerprint=presented_fingerprint,
        pinned=pinned,
    )


@workspace_router.post(
    "/{workspace_id}/ssh-credentials/{credential_id}/host-keys/accept",
    summary="接受测试里看到的新主机指纹",
    response_model=SshActionOkResponse,
)
@require_role(Role.Admin)
async def accept_ssh_host_key(
    workspace_id: int,
    credential_id: int,
    body: SshHostKeyAcceptRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> SshActionOkResponse:
    """只有 10 分钟内"测试连接"看到的那一把能被接受

    请求体里的指纹与钥匙必须同时匹配，否则用户可能点到了一个中途已经换掉的确认框。
    聊天登记的行对租户仍然只能启停、改备注或删除；换主机钥匙等于改连接信息。
    """
    await assert_workspace_accessible(_current_user, workspace_id)
    row, scope = await ssh_credential_service._load_visible(_current_user, workspace_id, credential_id)
    if scope is not None and row.created_via == C.CREATED_VIA_CHAT:
        raise ChatRegisteredEditForbiddenError
    pending = row_state.get_pending_host_key(int(row.id))
    if pending is None:
        raise ValidationError(reason="请先测试连接，再接受测试里看到的那把")
    pending_entry, pending_fingerprint = pending
    if str(body.entry or "").strip() != pending_entry or str(body.fingerprint or "").strip() != pending_fingerprint:
        raise ValidationError(reason="请先测试连接，再接受测试里看到的那把")
    if host_key_fingerprint(pending_entry) != pending_fingerprint:
        raise ValidationError(reason="指纹和主机钥匙对不上，请重新测试")
    await row_state.append_host_key(int(row.id), pending_entry, actor=_current_user.username)
    row_state.clear_pending_host_key(int(row.id))
    return SshActionOkResponse(message="已接受新的主机指纹")


@gateway_router.get(
    "/credentials",
    summary="列出全部工作区的 SSH 服务器凭据",
    response_model=SshCredentialListResponse,
)
@require_role(Role.Super)
async def list_all_ssh_credentials(
    _current_user: DBUser = Depends(get_current_active_user),
) -> SshCredentialListResponse:
    items: List[SshCredentialView] = await ssh_credential_service.list_all_credentials(_current_user)
    return SshCredentialListResponse(items=items, total=len(items))


@gateway_router.get(
    "/audit",
    summary="查看 SSH 审计记录",
    response_model=SshAuditListResponse,
)
@require_role(Role.Admin)
async def list_ssh_audit(
    workspace_id: Optional[int] = Query(default=None),
    credential_id: Optional[int] = Query(default=None),
    task_ref: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    before_id: Optional[int] = Query(default=None),
    _current_user: DBUser = Depends(get_current_active_user),
) -> SshAuditListResponse:
    refs = await _visible_instance_refs(_current_user)
    items, next_before_id = await ssh_credential_service.list_audit(
        _current_user,
        workspace_id=workspace_id,
        credential_id=credential_id,
        task_ref=task_ref,
        limit=limit,
        before_id=before_id,
        refs=refs,
    )
    return SshAuditListResponse(items=items, next_before_id=next_before_id)


@gateway_router.post(
    "/import-resources",
    summary="导入存量工作区资源里的 SSH 模板",
    response_model=ImportReport,
)
@require_role(Role.Super)
async def import_ssh_gateway_resources(
    _current_user: DBUser = Depends(get_current_active_user),
) -> ImportReport:
    """手动触发存量导入，仅平台方可用

    与启动导入不同，这条路径**不看总开关**：先把凭据备好、再开网关是正常用法。
    幂等由 `import_ref` 保证，重复调用只会把已有条目计进 `already`。
    """
    return await import_ssh_resources(actor=_current_user.username)


@gateway_router.get(
    "/status",
    summary="查看 SSH 网关状态",
    response_model=SshGatewayStatusResponse,
)
@require_role(Role.Super)
async def get_ssh_gateway_status(
    _current_user: DBUser = Depends(get_current_active_user),
) -> SshGatewayStatusResponse:
    """平台方专属：状态里只有运行数字，不含凭据或目标地址"""
    return SshGatewayStatusResponse(**supervisor.status())


router.include_router(workspace_router)
router.include_router(gateway_router)

__all__ = ["router"]
