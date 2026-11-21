"""SSH 服务器凭据的服务层

后台接口与后续的网关都只经这里读写凭据。这一层同时负责三件互相牵连的事：

- **可见性**：平台方看全部；租户只看自己实例对上的 `user` 行。看不见的行一律按
  "不存在"处理，响应与真的不存在逐字相同，否则 403 会把别人的凭据变可枚举。
- **字段语义**：三个凭据列只在提交了新值时才写；掩码原样提交回来表示保持不变。
  解不开的密文不能因为一次普通保存被抹掉——那可能还是能靠找回原密钥救回来的值。
- **审计**：增、改、删、查看明文、启停各写一行同步审计。`detail` 只写字段名，
  永远不写值。

聊天登记的行对租户额外收紧（凭据只给掩码、明文只有平台方能看、租户只能启停、改备注或删除，改不了连接信息与凭据）。
这条规则是刻意收的：聊天里登记的是用户个人的服务器，他未必知道 Bot 的运营方能看到。
"""

from __future__ import annotations

import datetime
from typing import Dict, Iterable, List, Optional, Set, Tuple

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_ws_ssh_audit import DBWsSshAudit
from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
from nekro_agent.schemas.errors import NotFoundError, PermissionDeniedError, ValidationError
from nekro_agent.schemas.i18n import I18nDict, i18n_text
from nekro_agent.schemas.ssh_gateway import (
    SshAuditView,
    SshCredentialCreate,
    SshCredentialSecretResponse,
    SshCredentialUpdate,
    SshCredentialView,
    SshHostKeyView,
)
from nekro_agent.schemas.tenant import CREDENTIAL_MASK
from nekro_agent.services.ssh_gateway import constants as C
from nekro_agent.services.ssh_gateway import events, row_state
from nekro_agent.services.ssh_gateway.destination import check_user_destination
from nekro_agent.services.ssh_gateway.secrets import (
    SshSecretEncryptError,
    decrypt_secret,
    encrypt_secret,
    secret_is_undecryptable,
)
from nekro_agent.services.ssh_gateway.validation import (
    host_key_fingerprint,
    parse_host_keys,
    validate_alias,
    validate_host,
    validate_owner_account,
    validate_port,
    validate_private_key,
    validate_username,
)
from nekro_agent.services.user.super_users import matches_super_users
from nekro_agent.services.user.tenant_perm import (
    accessible_instance_refs,
    get_tenant_scope,
    instance_refs_filter,
)
from nekro_agent.services.workspace.credential_policy import platform_credentials_allowed
from nekro_agent.services.workspace.tenant_scope import assert_workspace_accessible

logger = get_sub_logger("ssh_gateway.credentials")

_SECRET_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("password", "password_enc"),
    ("private_key", "private_key_enc"),
    ("passphrase", "passphrase_enc"),
)


class ChatRegisteredSecretForbiddenError(PermissionDeniedError):
    """租户对聊天登记的凭据调查看明文

    行本身他看得见，所以不按不存在处理；这条路径需要一句确切的文案告诉用户
    "明文只有平台方能看"，而不是通用的权限不足。
    """

    http_status = 403
    i18n_message: I18nDict = i18n_text(
        zh_CN="这条凭据的明文只有平台方能看",
        en_US="Only the platform owner can reveal the plaintext of this credential",
    )


class ChatRegisteredEditForbiddenError(PermissionDeniedError):
    """租户试图修改聊天登记的凭据的连接信息"""

    http_status = 403
    i18n_message: I18nDict = i18n_text(
        zh_CN="用户在聊天里登记的服务器，租户只能启用、停用、改备注或删除，不能改连接信息与凭据",
        en_US=(
            "For servers registered in chat, a tenant may only enable, disable, edit the note or delete them, "
            "not change the connection details or credentials"
        ),
    )


def _not_found(_credential_id: int) -> NotFoundError:
    """所有"看不到 / 不在这个工作区 / 真的没有"共用同一句，避免可枚举

    文案里刻意不带 ID：带上 ID 时"越权"与"真的不存在"会给出不同的字符串，
    调用方就能靠它区分"这一行存在但不是我的"和"这一行不存在"。
    """
    return NotFoundError(resource="服务器凭据")


def _iso(value: Optional[datetime.datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _detail_of(field_names: Iterable[str]) -> str:
    """审计 detail：只保留字段名，去重并排序"""
    return ", ".join(sorted({name for name in field_names if name}))


def _mask(value: str) -> str:
    return CREDENTIAL_MASK if value else ""


class SshCredentialService:
    """凭据的读写、可见性与审计入口"""

    # ==== 读取 ====

    async def list_credentials(self, user: DBUser, workspace_id: int) -> List[SshCredentialView]:
        workspace = await assert_workspace_accessible(user, workspace_id)
        scope = get_tenant_scope(user)
        query = DBWsSshCredential.filter(workspace_id=workspace_id)
        if scope is not None:
            refs = await accessible_instance_refs(user)
            query = query.filter(instance_refs_filter(refs), owner_kind=C.OWNER_USER)
        rows = await query.order_by("id")
        return [self._to_view(row, scope, workspace.name) for row in rows]

    async def get_credential(self, user: DBUser, workspace_id: int, credential_id: int) -> SshCredentialView:
        workspace = await assert_workspace_accessible(user, workspace_id)
        row, scope = await self._load_visible(user, workspace_id, credential_id)
        return self._to_view(row, scope, workspace.name)

    async def list_all_credentials(self, user: DBUser) -> List[SshCredentialView]:
        """平台方的全局列表：全部工作区、全部归属"""
        if get_tenant_scope(user) is not None:
            raise PermissionDeniedError
        rows = await DBWsSshCredential.all().order_by("workspace_id", "id")
        names = {
            int(workspace_id): str(name)
            for workspace_id, name in await DBWorkspace.all().values_list("id", "name")
        }
        return [self._to_view(row, None, names.get(int(row.workspace_id), "")) for row in rows]

    async def reveal_secret(
        self,
        user: DBUser,
        workspace_id: int,
        credential_id: int,
    ) -> SshCredentialSecretResponse:
        await assert_workspace_accessible(user, workspace_id)
        row, scope = await self._load_visible(user, workspace_id, credential_id)
        is_chat_row_for_tenant = scope is not None and row.created_via == C.CREATED_VIA_CHAT
        if is_chat_row_for_tenant:
            await self._write_audit(
                row,
                event=C.EVENT_CRED_REVEAL,
                allowed=False,
                actor=user.username,
                reason=C.REASON_FORBIDDEN,
                detail=_detail_of(("password", "private_key", "passphrase")),
            )
            logger.warning(
                f"租户查看聊天登记的 SSH 凭据明文被拒：用户={user.username} 工作区={workspace_id} 行={credential_id}",
            )
            raise ChatRegisteredSecretForbiddenError

        values: Dict[str, str] = {}
        for field, column in _SECRET_FIELDS:
            stored = str(getattr(row, column) or "")
            plain, source = decrypt_secret(stored)
            if source == "undecryptable":
                await self._write_audit(
                    row,
                    event=C.EVENT_CRED_REVEAL,
                    allowed=False,
                    actor=user.username,
                    reason=C.REASON_UNDECRYPTABLE,
                    detail=_detail_of(("password", "private_key", "passphrase")),
                )
                raise ValidationError(reason="密钥解不开，请恢复原密钥或重新填写")
            values[field] = plain

        await self._write_audit(
            row,
            event=C.EVENT_CRED_REVEAL,
            allowed=True,
            actor=user.username,
            detail=_detail_of(("password", "private_key", "passphrase")),
        )
        return SshCredentialSecretResponse(**values)

    async def list_audit(
        self,
        user: DBUser,
        *,
        workspace_id: Optional[int] = None,
        credential_id: Optional[int] = None,
        task_ref: Optional[str] = None,
        limit: int = 100,
        before_id: Optional[int] = None,
        refs: Optional[Set[Tuple[str, str]]] = None,
    ) -> Tuple[List[SshAuditView], Optional[int]]:
        """审计列表；租户按快照里的实例归属过滤，平台方看全部

        审计行不跟着凭据删除，也不做工作区可见性推导：它按写入时的归属快照过滤，
        这样凭据或工作区删掉以后，历史记录仍然能被正确的人看到。

        `refs` 是调用方已经算好的租户可见实例对；`None` 表示平台方。路由传它进来是
        为了避免同一请求里把作用域查询走两遍。
        """
        scope = get_tenant_scope(user)
        query = DBWsSshAudit.all()
        if scope is not None:
            if refs is None:
                refs = await accessible_instance_refs(user)
            query = query.filter(instance_refs_filter(refs), owner_kind=C.OWNER_USER)
        if workspace_id is not None:
            query = query.filter(workspace_id=workspace_id)
        if credential_id is not None:
            query = query.filter(credential_id=credential_id)
        if task_ref:
            query = query.filter(task_ref=task_ref)
        if before_id is not None:
            query = query.filter(id__lt=before_id)
        rows = await query.order_by("-id").limit(limit)
        next_before_id = int(rows[-1].id) if len(rows) == limit else None
        return [self._audit_to_view(row) for row in rows], next_before_id

    # ==== 写入 ====

    async def create_credential(
        self,
        user: DBUser,
        workspace_id: int,
        body: SshCredentialCreate,
    ) -> SshCredentialView:
        workspace = await assert_workspace_accessible(user, workspace_id)
        scope = get_tenant_scope(user)
        owner_account = validate_owner_account(body.owner_account)
        adapter_key = body.adapter_key.strip()
        instance_key = body.instance_key.strip()

        # 先做与归属无关的字段校验：用户行的目的地检查要用校验后的 host，
        # 否则空串或带空白的地址会先被送进 DNS，报出一句与字段无关的错
        alias = validate_alias(body.alias)
        host = validate_host(body.host)
        port = validate_port(body.port)
        username = validate_username(body.username)
        password = body.password or ""
        private_key = validate_private_key(body.private_key or "")
        passphrase = body.passphrase or ""
        if not password and not private_key:
            raise ValidationError(reason="密码与私钥至少要填一样")
        host_keys = parse_host_keys("\n".join(body.host_keys)) if body.host_keys else []

        if body.owner_kind == C.OWNER_PLATFORM:
            if scope is not None:
                raise ValidationError(reason="租户不能登记平台方服务器")
            if owner_account or adapter_key or instance_key:
                raise ValidationError(reason="平台方服务器不能带凭据主人或归属实例")
            if not await platform_credentials_allowed(workspace):
                raise ValidationError(reason="这个工作区不归平台方（或曾经不归平台方），不能挂平台方服务器")
        elif body.owner_kind == C.OWNER_USER:
            if not owner_account or not adapter_key or not instance_key:
                raise ValidationError(reason="用户服务器必须写明凭据主人与归属实例")
            if len(adapter_key) > 64 or len(instance_key) > 64:
                raise ValidationError(reason="归属实例的适配器与实例标识最长 64 个字符")
            bound = await DBChatChannel.filter(
                workspace_id=workspace_id,
                adapter_key=adapter_key,
                instance_key=instance_key,
            ).exists()
            if not bound:
                raise ValidationError(reason="这个实例没有绑定在这个工作区上")
            if scope is not None:
                refs = await accessible_instance_refs(user)
                if (adapter_key, instance_key) not in refs:
                    logger.warning(
                        f"租户登记不属于自己的 SSH 凭据：用户={user.username} 工作区={workspace_id} "
                        f"实例={adapter_key}/{instance_key}",
                    )
                    raise ValidationError(reason="这个实例不属于你所在的租户")
            await self._assert_destination(host)
        else:
            raise ValidationError(reason="归属只能是 platform 或 user")

        if await DBWsSshCredential.filter(workspace_id=workspace_id, alias=alias).exists():
            raise ValidationError(reason="这个工作区里已经有同名的服务器别名")

        row = await self.create_credential_row(
            workspace_id=workspace_id,
            owner_kind=body.owner_kind,
            owner_account=owner_account if body.owner_kind == C.OWNER_USER else "",
            adapter_key=adapter_key if body.owner_kind == C.OWNER_USER else "",
            instance_key=instance_key if body.owner_kind == C.OWNER_USER else "",
            created_via=C.CREATED_VIA_ADMIN,
            created_by=user.username,
            alias=alias,
            host=host,
            port=port,
            username=username,
            password=password,
            private_key=private_key,
            passphrase=passphrase,
            host_keys=host_keys,
            note=body.note or "",
        )
        return self._to_view(row, scope, workspace.name)

    async def create_credential_row(
        self,
        *,
        workspace_id: int,
        owner_kind: str,
        alias: str,
        host: str,
        port: int,
        username: str,
        created_via: str,
        created_by: str,
        owner_account: str = "",
        adapter_key: str = "",
        instance_key: str = "",
        import_ref: str = "",
        password: str = "",
        private_key: str = "",
        passphrase: str = "",
        host_keys: Optional[List[str]] = None,
        note: str = "",
    ) -> DBWsSshCredential:
        """建一行已校验好的凭据，顺带写 `cred_create` 审计

        只为"后台建行"与"存量资源导入"共用同一份加密与审计而存在：这两个调用方
        都不需要 `DBUser` 对象（导入的 actor 是 `import`），但都必须走
        `encrypt_secret` 的拒绝语义，也都不该各写一遍字段名清单。归属检查、
        目的地检查与别名冲突仍由调用方负责——导入的是平台方行，本来就不查目的地。

        Returns:
            已落库的行；凭据列的密文已经写好。
        """
        resolved_host_keys = list(host_keys or [])
        row = await DBWsSshCredential.create(
            workspace_id=workspace_id,
            owner_kind=owner_kind,
            owner_account=owner_account,
            adapter_key=adapter_key,
            instance_key=instance_key,
            created_via=created_via,
            import_ref=import_ref,
            alias=alias,
            host=host,
            port=port,
            username=username,
            password_enc=self._encrypt_or_validation(password),
            private_key_enc=self._encrypt_or_validation(private_key),
            passphrase_enc=self._encrypt_or_validation(passphrase),
            host_keys=resolved_host_keys,
            status=C.STATUS_ACTIVE,
            status_reason="",
            note=note,
            created_by=created_by,
            updated_by=created_by,
            last_error="",
        )

        field_names = ["alias", "host", "port", "username"]
        if password:
            field_names.append("password")
        if private_key:
            field_names.append("private_key")
        if passphrase:
            field_names.append("passphrase")
        if resolved_host_keys:
            field_names.append("host_keys")
        if note:
            field_names.append("note")
        await self._write_audit(
            row,
            event=C.EVENT_CRED_CREATE,
            allowed=True,
            actor=created_by,
            detail=_detail_of(field_names),
        )
        # 建行也要通知：在跑的工作区要立刻多出这一段 `Host`，`CLAUDE.md` 也要跟着刷新。
        # 与改、删、启停同一条顺序：写库成功之后才通知。
        await events.notify_credential_changed(int(row.id))
        return row

    async def update_credential(
        self,
        user: DBUser,
        workspace_id: int,
        credential_id: int,
        body: SshCredentialUpdate,
    ) -> SshCredentialView:
        workspace = await assert_workspace_accessible(user, workspace_id)
        row, scope = await self._load_visible(user, workspace_id, credential_id)
        is_chat_row_for_tenant = scope is not None and row.created_via == C.CREATED_VIA_CHAT

        if is_chat_row_for_tenant and self._has_change_outside_status_note(row, body):
            logger.warning(
                f"租户修改聊天登记的 SSH 凭据连接信息被拒：用户={user.username} 工作区={workspace_id} 行={credential_id}",
            )
            raise ChatRegisteredEditForbiddenError

        changed: List[str] = []
        update_fields: List[str] = []

        if body.alias is not None and body.alias != row.alias:
            alias = validate_alias(body.alias)
            # 先查重再写：唯一约束兜底时报的是数据库错误（500），用户看不懂
            if await DBWsSshCredential.filter(workspace_id=workspace_id, alias=alias).exclude(id=row.id).exists():
                raise ValidationError(reason="这个工作区里已经有同名的服务器别名")
            row.alias = alias
            changed.append("alias")
            update_fields.append("alias")
        if body.host is not None:
            host = validate_host(body.host)
            if host != row.host:
                row.host = host
                changed.append("host")
                update_fields.append("host")
        if body.port is not None:
            port = validate_port(body.port)
            if port != row.port:
                row.port = port
                changed.append("port")
                update_fields.append("port")
        if body.username is not None:
            username = validate_username(body.username)
            if username != row.username:
                row.username = username
                changed.append("username")
                update_fields.append("username")
        if body.host_keys is not None:
            host_keys = parse_host_keys("\n".join(body.host_keys))
            if host_keys != list(row.host_keys or []):
                row.host_keys = host_keys
                changed.append("host_keys")
                update_fields.append("host_keys")
        if body.note is not None and body.note != row.note:
            row.note = body.note
            changed.append("note")
            update_fields.append("note")

        # 「换了凭据」只认真的提交了新值的密码或私钥：编辑表单会把没填过的凭据字段以空串
        # 提交，原本就空的列再写一次空串不算改动，更不能借此把登不上的行放回可用
        new_secret_fields: List[str] = []
        for field, column in _SECRET_FIELDS:
            submitted: Optional[str] = getattr(body, field)
            if submitted is None or submitted == CREDENTIAL_MASK:
                continue
            if submitted == "" and not str(getattr(row, column) or ""):
                continue
            if field == "private_key":
                submitted = validate_private_key(submitted)
            changed.append(field)
            update_fields.append(column)
            if submitted == "":
                setattr(row, column, "")
            else:
                setattr(row, column, self._encrypt_or_validation(submitted))
                new_secret_fields.append(field)

        if not str(row.password_enc or "") and not str(row.private_key_enc or ""):
            raise ValidationError(reason="密码与私钥至少要留一样")

        if row.owner_kind == C.OWNER_USER and "host" in changed:
            await self._assert_destination(row.host)

        secrets_changed = bool({"password", "private_key"} & set(new_secret_fields))
        requested_status = body.status
        if requested_status is not None and requested_status != row.status:
            if requested_status == C.STATUS_DISABLED:
                self._apply_disable(row)
                changed.append("status")
                update_fields.extend(["status", "status_reason"])
            elif requested_status == C.STATUS_ACTIVE:
                self._apply_enable(row, secrets_changed=secrets_changed)
                changed.append("status")
                update_fields.extend(["status", "status_reason"])
            else:
                raise ValidationError(reason="状态只能通过启用或停用切换")
        elif row.status == C.STATUS_AUTH_FAILED and secrets_changed:
            row.status = C.STATUS_ACTIVE
            row.status_reason = ""
            changed.append("status")
            update_fields.extend(["status", "status_reason"])
        elif row.status == C.STATUS_DISABLED and row.status_reason == C.STATUS_AUTH_FAILED and secrets_changed:
            # 停用前就登不上的行，凭据改过以后去掉这个记号；仍保持停用，由人决定何时启用
            row.status_reason = ""
            changed.append("status")
            update_fields.append("status_reason")

        row.updated_by = user.username
        update_fields.extend(["updated_by", "update_time"])
        await row.save(update_fields=list(dict.fromkeys(update_fields)))

        await self._write_audit(
            row,
            event=C.EVENT_CRED_UPDATE,
            allowed=True,
            actor=user.username,
            detail=_detail_of(changed),
        )
        # 写库成功之后再通知：网关据此作废复核缓存、断开相关连接、关上游
        await events.notify_credential_changed(int(row.id))
        return self._to_view(row, scope, workspace.name)

    async def delete_credential(self, user: DBUser, workspace_id: int, credential_id: int) -> None:
        await assert_workspace_accessible(user, workspace_id)
        row, _scope = await self._load_visible(user, workspace_id, credential_id)
        await self._write_audit(
            row,
            event=C.EVENT_CRED_DELETE,
            allowed=True,
            actor=user.username,
            detail=_detail_of(("alias", "host", "username")),
        )
        await DBWsSshCredential.filter(id=row.id).delete()
        # 行已经没了，通知里只带 id；网关据此作废缓存并断开相关连接
        await events.notify_credential_changed(int(row.id))

    async def set_status(
        self,
        user: DBUser,
        workspace_id: int,
        credential_id: int,
        *,
        enable: bool,
    ) -> SshCredentialView:
        workspace = await assert_workspace_accessible(user, workspace_id)
        row, scope = await self._load_visible(user, workspace_id, credential_id)
        if enable:
            self._apply_enable(row, secrets_changed=False)
        else:
            self._apply_disable(row)
        row.updated_by = user.username
        await row.save(update_fields=["status", "status_reason", "updated_by", "update_time"])
        await self._write_audit(
            row,
            event=C.EVENT_CRED_UPDATE,
            allowed=True,
            actor=user.username,
            detail=_detail_of(("status",)),
        )
        await events.notify_credential_changed(int(row.id))
        return self._to_view(row, scope, workspace.name)

    # ==== 内部 ====

    async def _load_visible(
        self,
        user: DBUser,
        workspace_id: int,
        credential_id: int,
    ) -> Tuple[DBWsSshCredential, Optional[int]]:
        row = await DBWsSshCredential.get_or_none(id=credential_id)
        if row is None or int(row.workspace_id) != int(workspace_id):
            raise _not_found(credential_id)
        scope = get_tenant_scope(user)
        if scope is not None:
            refs = await accessible_instance_refs(user)
            visible = row.owner_kind == C.OWNER_USER and (row.adapter_key, row.instance_key) in refs
            if not visible:
                logger.warning(
                    f"SSH 凭据越权访问：用户={user.username} 工作区={workspace_id} 行={credential_id}",
                )
                raise _not_found(credential_id)
        return row, scope

    async def _assert_destination(self, host: str) -> None:
        verdict = await check_user_destination(host)
        if verdict.ok:
            return
        if verdict.reason == C.REASON_TARGET_BLOCKED:
            # 不回显解析出的地址：否则后台的「新建」就成了一个探内网 DNS 的工具
            raise ValidationError(reason="这台服务器解析到了不允许的地址；用户服务器不能是内网地址")
        raise ValidationError(reason=verdict.reason)

    def _encrypt_or_validation(self, plain: str) -> str:
        try:
            return encrypt_secret(plain)
        except SshSecretEncryptError as exc:
            raise ValidationError(reason=str(exc)) from exc

    def _apply_enable(self, row: DBWsSshCredential, *, secrets_changed: bool) -> None:
        """启用：挂起的、以及登不上又没改过凭据的行都不能直接回到 active

        「登不上」要跟着停用走一遍：先停用再启用不能成为绕过「先更新凭据」的捷径，
        所以停用一条 `auth_failed` 的行时把原因记在 `status_reason` 上（见 `_apply_disable`）。
        后台「测试连接」刚用这组凭据登录成功过（10 分钟内、凭据与目标都没变）的，也可以
        直接启用：服务器那边修好了就不必为了启用再改一次凭据。
        """
        if row.status == C.STATUS_SUSPENDED:
            raise ValidationError(reason="工作区已不归平台方，平台方服务器只能挂到新的平台方工作区上")
        failed_before = row.status == C.STATUS_AUTH_FAILED or (
            row.status == C.STATUS_DISABLED and row.status_reason == C.STATUS_AUTH_FAILED
        )
        if failed_before and not secrets_changed and not row_state.test_passed_recently(row):
            raise ValidationError(reason="请先更新凭据，或先测试连接确认能登上")
        row.status = C.STATUS_ACTIVE
        row.status_reason = ""

    def _apply_disable(self, row: DBWsSshCredential) -> None:
        """停用；挂起的行不许停用，登不上的行停用时记住原因

        挂起是工作区归属变了的结果，是粘的：允许「停用」就等于允许随后「启用」把它带回 active。
        不再需要它，就删除。
        """
        if row.status == C.STATUS_SUSPENDED:
            raise ValidationError(reason="这台平台方服务器已挂起（工作区已不归平台方），不能启停；不再需要就删除")
        row.status_reason = C.STATUS_AUTH_FAILED if row.status == C.STATUS_AUTH_FAILED else ""
        row.status = C.STATUS_DISABLED

    def _has_change_outside_status_note(self, row: DBWsSshCredential, body: SshCredentialUpdate) -> bool:
        """聊天登记的行：只要提交的字段真会改变连接信息，就拒绝"""
        if body.alias is not None and body.alias != row.alias:
            return True
        if body.host is not None and body.host.strip() != row.host:
            return True
        if body.port is not None and body.port != row.port:
            return True
        if body.username is not None and body.username.strip() != row.username:
            return True
        if body.host_keys is not None:
            try:
                parsed = parse_host_keys("\n".join(body.host_keys))
            except ValidationError:
                return True
            if parsed != list(row.host_keys or []):
                return True
        for field, _column in _SECRET_FIELDS:
            submitted: Optional[str] = getattr(body, field)
            if submitted is not None and submitted != CREDENTIAL_MASK:
                return True
        return False

    def _to_view(
        self,
        row: DBWsSshCredential,
        scope: Optional[int],
        workspace_name: str,
    ) -> SshCredentialView:
        has_password = bool(row.password_enc)
        has_private_key = bool(row.private_key_enc)
        has_passphrase = bool(row.passphrase_enc)
        secret_state = (
            "undecryptable"
            if any(
                secret_is_undecryptable(str(getattr(row, column) or ""))
                for _field, column in _SECRET_FIELDS
            )
            else "ok"
        )
        host_keys: List[SshHostKeyView] = []
        for entry in row.host_keys or []:
            text = str(entry)
            try:
                fingerprint = host_key_fingerprint(text)
            except ValidationError:
                fingerprint = ""
            host_keys.append(SshHostKeyView(entry=text, fingerprint=fingerprint))

        can_manage = scope is None or row.created_via != C.CREATED_VIA_CHAT
        owner_whitelisted: Optional[bool] = None
        if row.owner_kind == C.OWNER_USER:
            owner_whitelisted = matches_super_users(
                row.adapter_key,
                row.owner_account,
                config.SSH_GATEWAY_USER_ACCOUNTS,
            )

        return SshCredentialView(
            id=int(row.id),
            workspace_id=int(row.workspace_id),
            workspace_name=workspace_name,
            owner_kind=row.owner_kind,
            owner_account=row.owner_account,
            adapter_key=row.adapter_key,
            instance_key=row.instance_key,
            created_via=row.created_via,
            import_ref=row.import_ref,
            alias=row.alias,
            host=row.host,
            port=int(row.port),
            username=row.username,
            password=_mask(row.password_enc),
            private_key=_mask(row.private_key_enc),
            passphrase=_mask(row.passphrase_enc),
            host_keys=host_keys,
            status=row.status,
            status_reason=row.status_reason,
            note=row.note,
            created_by=row.created_by,
            updated_by=row.updated_by,
            last_used_at=_iso(row.last_used_at),
            last_error=row.last_error,
            create_time=_iso(row.create_time) or "",
            update_time=_iso(row.update_time) or "",
            has_password=has_password,
            has_private_key=has_private_key,
            has_passphrase=has_passphrase,
            secret_state=secret_state,
            owner_whitelisted=owner_whitelisted,
            can_reveal=can_manage,
            can_edit_connection=can_manage,
        )

    async def _write_audit(
        self,
        row: Optional[DBWsSshCredential],
        *,
        event: str,
        allowed: bool,
        actor: str,
        reason: str = "",
        detail: str = "",
        workspace_id: Optional[int] = None,
        credential_id: Optional[int] = None,
    ) -> None:
        await DBWsSshAudit.create(
            workspace_id=int(row.workspace_id) if row is not None else int(workspace_id or 0),
            credential_id=int(row.id) if row is not None else credential_id,
            owner_kind=row.owner_kind if row is not None else "",
            owner_account=row.owner_account if row is not None else "",
            adapter_key=row.adapter_key if row is not None else "",
            instance_key=row.instance_key if row is not None else "",
            event=event,
            allowed=allowed,
            reason=reason,
            detail=detail,
            actor=actor,
        )

    def _audit_to_view(self, row: DBWsSshAudit) -> SshAuditView:
        return SshAuditView(
            id=int(row.id),
            create_time=_iso(row.create_time),
            workspace_id=int(row.workspace_id),
            credential_id=int(row.credential_id) if row.credential_id is not None else None,
            owner_kind=row.owner_kind,
            owner_account=row.owner_account,
            adapter_key=row.adapter_key,
            instance_key=row.instance_key,
            event=row.event,
            allowed=bool(row.allowed),
            reason=row.reason,
            task_ref=row.task_ref,
            lease_id=row.lease_id,
            conn_id=row.conn_id,
            triggered_by=list(row.triggered_by or []),
            channel_kind=row.channel_kind,
            detail=row.detail,
            input_head=row.input_head,
            exit_status=row.exit_status,
            bytes_up=row.bytes_up,
            bytes_down=row.bytes_down,
            duration_ms=row.duration_ms,
            host_key_fp=row.host_key_fp,
            actor=row.actor,
        )


ssh_credential_service = SshCredentialService()

__all__ = [
    "ChatRegisteredEditForbiddenError",
    "ChatRegisteredSecretForbiddenError",
    "SshCredentialService",
    "ssh_credential_service",
]
