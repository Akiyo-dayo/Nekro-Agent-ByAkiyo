import json
from dataclasses import dataclass
from email.header import decode_header
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from nekro_agent.models.db_email import DBEmail
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.services import email_accounts as mail
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import assert_instance_accessible, is_platform_owner


class EmailItem(BaseModel):
    """邮件列表项"""

    id: int
    account_username: str
    subject: str
    sender: str
    date: Optional[str] = None
    body_preview: str = Field(description="邮件正文预览（前200字符）")
    has_attachments: bool
    create_time: str


class EmailDetail(BaseModel):
    """邮件详情"""

    id: int
    account_username: str
    email_uid: str
    message_id: str
    subject: str
    sender: str
    recipients: str
    date: Optional[str] = None
    body_text: str
    has_attachments: bool
    attachment_names: str
    attachments: List["EmailAttachmentItem"] = Field(default_factory=list)
    in_reply_to: str
    references: str
    create_time: str


class EmailAttachmentItem(BaseModel):
    """邮件附件项"""

    name: str
    url: str
    extension: str
    preview_type: str


class EmailRawContent(BaseModel):
    """邮件原始内容"""

    html_content: str = ""
    text_content: str = ""


class PaginationMeta(BaseModel):
    """分页元数据"""

    offset: int
    limit: int
    total: int
    has_more: bool


class EmailListResponse(BaseModel):
    """邮件列表响应"""

    items: List[EmailItem]
    pagination: PaginationMeta


router = APIRouter(prefix="/emails", tags=["Emails"])

# 这一族四条接口按**归属**过滤，不再是平台方专属。
#
# 改造前它们只能是平台方专属：收件账号存在 email 适配器的适配器层配置
# `RECEIVE_ACCOUNTS` 里，email 又是单实例适配器（`build_chat_key` 不带 instance_key），
# `DBEmail` 里没有任何一列能把某封邮件判给某个租户。现在 `DBEmail` 多了两列归属快照
# （`owner_adapter_key` / `owner_instance_key`，空 = 平台方），实例也可以有自己填的邮箱，
# 于是有了一条能收敛的租户维度：
#
# - 平台方不带 `instance_key`：只看平台方邮箱（`owner_*` 为空的行），与改造前逐字相同；
# - 平台方带 `instance_key`：看那个实例的自有邮箱；
# - 租户：必须带 `adapter_key` + `instance_key`，先过 `assert_instance_accessible`。
#
# 按 ID 访问一律**先取行再判归属**，「不存在」与「不是你的」抛同一个 `NotFoundError`：
# 两种回答如果不同，租户就能用「换一个 ID 试试」把平台方与别的实例的邮件枚举出来。
#
# 角色门槛仍是 `Role.Admin`：租户管理员正是这一档，降成 `Role.User` 会让只读成员读到
# 整个实例的收件箱（密码重置邮件、验证码、账单都在里面）。


@dataclass(frozen=True)
class MailboxScope:
    """一次请求落在哪个邮箱上

    `adapter_key` / `instance_key` 都是空串表示平台方邮箱；否则是某个实例的自有邮箱。
    """

    adapter_key: str = ""
    instance_key: str = ""

    @property
    def is_platform(self) -> bool:
        return not self.adapter_key and not self.instance_key


async def _resolve_mailbox_scope(user: DBUser, adapter_key: str, instance_key: str) -> MailboxScope:
    """按当前身份与查询参数解析出这次请求的邮箱归属

    平台方：两个参数都为空 → 平台方邮箱；带了实例 → 那个实例（不需要再过租户守卫，
    平台方本来就能看所有实例）。
    租户：两个参数缺一不可，且实例必须是自己租户的——越权与不存在同样抛 404。
    """
    normalized_adapter = (adapter_key or "").strip()
    normalized_instance = (instance_key or "").strip()

    if is_platform_owner(user):
        if not normalized_instance:
            return MailboxScope()
        if not normalized_adapter:
            raise ValidationError(reason="按实例查看邮箱时必须同时提供 adapter_key 与 instance_key")
        await assert_instance_accessible(user, normalized_instance, adapter_key=normalized_adapter)
        return MailboxScope(adapter_key=normalized_adapter, instance_key=normalized_instance)

    if not normalized_adapter or not normalized_instance:
        raise ValidationError(reason="必须提供 adapter_key 与 instance_key")
    instance = await assert_instance_accessible(user, normalized_instance, adapter_key=normalized_adapter)
    # `assert_instance_accessible` 已经按租户作用域查过；这里再核一遍，防它将来放宽
    if instance.tenant_id != user.tenant_id:
        raise NotFoundError(resource="实例")
    return MailboxScope(adapter_key=normalized_adapter, instance_key=normalized_instance)


def _apply_scope(query, scope: MailboxScope):
    """把归属条件加到查询上（平台方邮箱 = 两列快照都为空）"""
    if scope.is_platform:
        return query.filter(owner_adapter_key="", owner_instance_key="")
    return query.filter(owner_adapter_key=scope.adapter_key, owner_instance_key=scope.instance_key)


async def _load_visible_email(email_id: int, scope: MailboxScope) -> DBEmail:
    """按 ID 取一封邮件并判归属；看不到与不存在同一个 404"""
    email = await DBEmail.get_or_none(id=email_id)
    if email is None:
        raise NotFoundError(resource="邮件")
    if (email.owner_adapter_key or "") != scope.adapter_key or (email.owner_instance_key or "") != scope.instance_key:
        raise NotFoundError(resource="邮件")
    return email


def _decode_mime_words(value: str) -> str:
    if not value:
        return ""

    decoded_parts: list[str] = []
    for part, encoding in decode_header(value):
        if isinstance(part, bytes):
            try:
                decoded_parts.append(part.decode(encoding or "utf-8", errors="ignore"))
            except LookupError:
                decoded_parts.append(part.decode("utf-8", errors="ignore"))
        else:
            decoded_parts.append(part)
    return "".join(decoded_parts).strip()


def _format_email_address(raw_value: str) -> str:
    if not raw_value:
        return ""

    name, addr = parseaddr(raw_value)
    decoded_name = _decode_mime_words(name).strip().strip('"')
    normalized_addr = addr.strip()

    if not normalized_addr:
        return decoded_name or raw_value.strip()

    local_part = normalized_addr.split("@", 1)[0].strip().lower()
    normalized_name = decoded_name.lower()

    if not decoded_name or normalized_name in {normalized_addr.lower(), local_part}:
        return normalized_addr
    return f"{decoded_name} <{normalized_addr}>"


def _normalize_recipients(raw_recipients: str) -> str:
    if not raw_recipients:
        return "[]"

    try:
        loaded = json.loads(raw_recipients)
        raw_items = [str(item) for item in loaded] if isinstance(loaded, list) else [str(loaded)]
    except json.JSONDecodeError:
        raw_items = [raw_recipients]

    normalized: list[str] = []
    for item in raw_items:
        parsed_items = getaddresses([item])
        if parsed_items:
            for name, addr in parsed_items:
                formatted = _format_email_address(f"{name} <{addr}>")
                if formatted:
                    normalized.append(formatted)
            continue

        formatted = _format_email_address(item)
        if formatted:
            normalized.append(formatted)

    return json.dumps(normalized, ensure_ascii=False)


def _guess_preview_type(extension: str) -> str:
    normalized = extension.lower()
    if normalized in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}:
        return "image"
    if normalized in {".md", ".markdown"}:
        return "markdown"
    if normalized in {".txt", ".log", ".csv", ".json", ".yaml", ".yml"}:
        return "text"
    if normalized in {".pdf"}:
        return "pdf"
    return "download"


def _build_attachment_items(
    email_id: int,
    account_username: str,
    email_uid: str,
    raw_attachment_names: str,
    directory: str,
) -> List[EmailAttachmentItem]:
    """这封邮件的附件列表

    `url` 一律指向**按邮件 ID** 的那一条（`/api/emails/{email_id}/attachments/{filename}`）：
    旧的按地址那条要继续给平台方邮箱用，但实例自有账户的附件目录不是地址（`_i<id>` 或由
    平台方转来的原地址），按地址拼路径对它们根本不成立。前端改用新的这一条。
    """
    attachment_names = parse_json_names(raw_attachment_names)
    if not attachment_names:
        return []

    items: list[EmailAttachmentItem] = []
    for name in attachment_names:
        file_path = mail.attachment_file_path(directory, email_uid, name)
        if file_path is None or not file_path.exists() or not file_path.is_file():
            continue

        extension = file_path.suffix.lower()
        preview_type = _guess_preview_type(extension)
        url = f"/api/emails/{email_id}/attachments/{file_path.name}"
        items.append(
            EmailAttachmentItem(
                name=file_path.name,
                url=url,
                extension=extension,
                preview_type=preview_type,
            )
        )
    return items


def parse_json_names(raw_value: str) -> List[str]:
    if not raw_value:
        return []
    try:
        loaded = json.loads(raw_value)
        if isinstance(loaded, list):
            return [str(item) for item in loaded if str(item).strip()]
    except json.JSONDecodeError:
        pass
    return [item.strip() for item in raw_value.split(",") if item.strip()]


@router.get("", summary="获取邮件列表")
@require_role(Role.Admin)
async def get_emails(
    offset: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(25, ge=1, le=100, description="每页数量"),
    account: Optional[str] = Query(None, description="筛选账户"),
    adapter_key: str = Query("", description="实例所属适配器；不传表示平台方邮箱"),
    instance_key: str = Query("", description="实例标识；不传表示平台方邮箱"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> EmailListResponse:
    """获取邮件列表，支持分页和账户筛选

    归属由 `adapter_key` / `instance_key` 决定：都不传是平台方邮箱（与改造前相同），
    传了就是那个实例的自有邮箱。租户两个都要传。
    """
    scope = await _resolve_mailbox_scope(_current_user, adapter_key, instance_key)
    query = _apply_scope(DBEmail.all(), scope)

    if account:
        query = query.filter(account_username=account)

    total = await query.count()
    emails = await query.offset(offset).limit(limit).order_by("-create_time")

    items = [
        EmailItem(
            id=email.id,
            account_username=email.account_username,
            subject=email.subject,
            sender=_format_email_address(email.sender),
            date=email.date.isoformat() if email.date else None,
            body_preview=email.body_text[:200] if email.body_text else "",
            has_attachments=email.has_attachments,
            create_time=email.create_time.isoformat(),
        )
        for email in emails
    ]

    return EmailListResponse(
        items=items,
        pagination=PaginationMeta(
            offset=offset,
            limit=limit,
            total=total,
            has_more=offset + limit < total,
        ),
    )


@router.get("/{email_id}", summary="获取邮件详情")
@require_role(Role.Admin)
async def get_email_detail(
    email_id: int,
    adapter_key: str = Query("", description="实例所属适配器；不传表示平台方邮箱"),
    instance_key: str = Query("", description="实例标识；不传表示平台方邮箱"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> EmailDetail:
    """获取指定邮件的详细信息；别的归属的邮件与不存在同一个 404"""
    scope = await _resolve_mailbox_scope(_current_user, adapter_key, instance_key)
    email = await _load_visible_email(email_id, scope)
    directory = await mail.attachment_dir_for_message(
        email.account_username,
        owner_adapter_key=email.owner_adapter_key or "",
        owner_instance_key=email.owner_instance_key or "",
    )

    return EmailDetail(
        id=email.id,
        account_username=email.account_username,
        email_uid=email.email_uid,
        message_id=email.message_id,
        subject=email.subject,
        sender=_format_email_address(email.sender),
        recipients=_normalize_recipients(email.recipients),
        date=email.date.isoformat() if email.date else None,
        body_text=email.body_text,
        has_attachments=email.has_attachments,
        attachment_names=email.attachment_names,
        attachments=_build_attachment_items(
            email.id,
            email.account_username,
            email.email_uid,
            email.attachment_names,
            directory,
        ),
        in_reply_to=email.in_reply_to,
        references=email.references,
        create_time=email.create_time.isoformat(),
    )


@router.get("/{email_id}/raw-content", summary="获取邮件网页正文")
@require_role(Role.Admin)
async def get_email_raw_content(
    email_id: int,
    adapter_key: str = Query("", description="实例所属适配器；不传表示平台方邮箱"),
    instance_key: str = Query("", description="实例标识；不传表示平台方邮箱"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> EmailRawContent:
    scope = await _resolve_mailbox_scope(_current_user, adapter_key, instance_key)
    email = await _load_visible_email(email_id, scope)

    from nekro_agent.adapters.email.routers import get_email_adapter

    adapter = get_email_adapter()
    raw_content = await adapter.get_raw_email_content(email.account_username, email.email_uid)
    return EmailRawContent(
        html_content=str(raw_content.get("html_content") or ""),
        text_content=str(raw_content.get("text_content") or ""),
    )


def _attachment_file_response(filepath: Path):
    """把附件按扩展名给出合适的 media type（两条下载接口共用）"""
    suffix = filepath.suffix.lower().lstrip(".")
    media_map = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp",
        "svg": "image/svg+xml",
        "txt": "text/plain; charset=utf-8",
        "md": "text/markdown; charset=utf-8",
        "json": "application/json",
        "pdf": "application/pdf",
    }
    media_type = media_map.get(suffix, "application/octet-stream")
    headers = {"Cache-Control": "private, max-age=3600"}
    if media_type == "application/octet-stream":
        headers["Content-Disposition"] = f'attachment; filename="{filepath.name}"'
    return FileResponse(filepath, media_type=media_type, headers=headers, filename=filepath.name)


@router.get("/{email_id}/attachments/{filename}", summary="获取邮件附件（按邮件 ID）")
@require_role(Role.Admin)
async def get_email_attachment_by_id(
    email_id: int,
    filename: str,
    adapter_key: str = Query("", description="实例所属适配器；不传表示平台方邮箱"),
    instance_key: str = Query("", description="实例标识；不传表示平台方邮箱"),
    _current_user: DBUser = Depends(get_current_active_user),
):
    """按邮件 ID 取附件

    前端改用这一条：账户的附件目录不一定是地址（实例自有账户是 `_i<id>`，由平台方转来的
    是原地址），所以「地址 + UID + 文件名」那三个路径段拼不出正确的位置，只能先从缓存行
    找到这封信的账户、再按账户的附件目录拼。

    顺序是**先取行、再判可见、最后才碰文件系统**：路径里的文件名是调用方给的，
    先拼路径再判归属等于把「文件在不在」这件事泄露给任何登录身份。不可见、不存在、
    文件名穿越全部同一个 404——三种回答如果不同，换一个文件名就能把别人的附件探出来。
    """
    scope = await _resolve_mailbox_scope(_current_user, adapter_key, instance_key)
    email = await _load_visible_email(email_id, scope)
    directory = await mail.attachment_dir_for_message(
        email.account_username,
        owner_adapter_key=email.owner_adapter_key or "",
        owner_instance_key=email.owner_instance_key or "",
    )
    filepath = mail.attachment_file_path(directory, email.email_uid, filename)

    if filepath is None or not filepath.exists() or not filepath.is_file():
        raise NotFoundError(resource="附件")
    return _attachment_file_response(filepath)


@router.get("/{account_username}/{email_uid}/attachments/{filename}", summary="获取邮件附件（按地址，仅平台方邮箱）")
@require_role(Role.Admin)
async def get_email_attachment(
    account_username: str,
    email_uid: str,
    filename: str,
    adapter_key: str = Query("", description="实例所属适配器；不传表示平台方邮箱"),
    instance_key: str = Query("", description="实例标识；不传表示平台方邮箱"),
    _current_user: DBUser = Depends(get_current_active_user),
):
    """按地址 + UID 取附件：**只给平台方账户用**，前端已改用上面按邮件 ID 的那一条

    留着它是因为存量书签与外部脚本还在用；实例自有账户的目录不是地址，它们走
    `/emails/{email_id}/attachments/{filename}`。

    路径里的地址是调用方给的，所以**先按（地址, UID）查缓存行并判归属**再碰文件系统：
    否则换个附件名就能把任意地址目录下的文件读出来。看不到的行与不存在的行同一个 404。
    拼路径逐层判父子关系（账户目录 → 邮件目录 → 文件），只判「落在总目录里」会放过
    `../<别的账户>/<UID>`。
    """
    scope = await _resolve_mailbox_scope(_current_user, adapter_key, instance_key)
    row = await _apply_scope(
        DBEmail.filter(account_username=account_username, email_uid=email_uid),
        scope,
    ).first()
    if row is None or not scope.is_platform:
        raise NotFoundError(resource="附件")

    filepath = mail.attachment_file_path(account_username, email_uid, filename)
    if filepath is None or not filepath.exists() or not filepath.is_file():
        raise NotFoundError(resource="附件")
    return _attachment_file_response(filepath)
