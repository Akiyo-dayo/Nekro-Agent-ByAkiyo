"""实例自有邮箱的后台接口

## 这一族服务的是「某个实例的邮箱」

参数一律带 `adapter_key` + `instance_key`，**平台方也必须带**：这组接口回答的问题是
「这个实例能收发什么」，不带实例就没有主语。作用域统一在 `_resolve_scope` 里收口：
租户只能碰自己租户下的实例（不经 `assert_instance_accessible` 的路径一条都没有），
越权与不存在抛同一个 `NotFoundError`。

## 凭据只写不读

列表与详情**永远不返回** `password`，只回 `HAS_PASSWORD`：授权码是「能登进那个邮箱」的东西，
后台页面只需要知道「填过没有」。修改时传空串或不传表示不改——否则编辑显示名这种动作
会把密码一起清掉。

## 发信记录里的地址

平台方账户发的那些行对租户只显示「平台方提供：<显示名>」：租户本来就没有平台方账户的地址，
发信记录不能变成第二条泄漏路径。发信记录里的账户引用本来就是哈希 / 表 ID，不含地址。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from tortoise.transactions import in_transaction

from nekro_agent.adapters.email.base import CUSTOM_PROVIDER_KEY, list_provider_presets
from nekro_agent.adapters.email.config import EmailAccount
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_email import DBEmail
from nekro_agent.models.db_email_account import DBEmailAccount
from nekro_agent.models.db_email_send_log import DBEmailSendLog
from nekro_agent.models.db_email_user_pref import DBEmailUserPref
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import NotFoundError, PermissionDeniedError, ValidationError
from nekro_agent.services import email_accounts as mail
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import assert_instance_accessible, is_platform_owner

logger = get_sub_logger("instance_email")

router = APIRouter(prefix="/instance-email", tags=["InstanceEmail"])


# ===== 请求 / 响应 =====


class InstanceEmailAccountPayload(BaseModel):
    """新建 / 修改实例自有账户的请求体

    `password` 只在新建时必填；修改时不传或传空串表示不改（编辑显示名不该把授权码清掉）。
    选了预设（`provider` 不是 `自定义`）时主机端口取自预设，`settings` 里的那几项被忽略。
    """

    username: str = Field(description="邮箱地址；入库前去首尾空白转小写")
    password: str = Field(default="", description="授权码 / 密码；修改时留空表示不改")
    display_name: str = Field(default="", description="显示名；空则显示地址")
    provider: str = Field(default="自定义", description="服务商预设键；自定义表示自填服务器")
    settings: Dict[str, Any] = Field(
        default_factory=dict,
        description="自定义服务器：imap_host / imap_port / smtp_host / smtp_port / smtp_ssl_port / smtp_use_ssl",
    )
    enabled: bool = Field(default=True, description="启用账户")
    receive_enabled: bool = Field(default=True, description="启用收信（本版实例自有账户暂不收信）")
    send_enabled: bool = Field(default=False, description="启用发信")
    is_default_sender: bool = Field(default=False, description="设为本实例的默认发件人")
    notify_chat_key: str = Field(default="", description="新邮件通知推到哪个频道；必须属于本实例")


class InstanceEmailAccountInfo(BaseModel):
    """实例自有账户的对外形态：凭据只写不读"""

    id: int
    adapter_key: str
    instance_key: str
    username: str
    display_name: str
    provider: str
    settings: Dict[str, Any]
    HAS_PASSWORD: bool
    enabled: bool
    receive_enabled: bool
    send_enabled: bool
    is_default_sender: bool
    notify_chat_key: str
    attachment_dir: str


class PlatformAccountInfo(BaseModel):
    """平台方账户在某个实例眼里的形态：只有显示名、引用与是否默认"""

    display_label: str
    account_ref: str
    is_default_sender: bool


class SendLogInfo(BaseModel):
    """发信记录的一行；平台方账户发的那几行不给地址"""

    id: int
    account_kind: str
    display_label: str
    purpose: str
    to_address: str
    subject: str
    reason: str
    ok: bool
    error: str
    create_time: str


class UserPrefInfo(BaseModel):
    """用户邮件偏好的一行；地址遮掩显示"""

    id: int
    platform_userid: str
    email: str = Field(description="遮掩后的地址（a***@qq.com）")
    festival_subscribed: bool
    unsubscribed: bool
    unsubscribed_at: Optional[str] = None
    set_in_chat_key: str


class OutboxBatchInfo(BaseModel):
    """节日邮件队列的一批"""

    batch_key: str
    festival_name: str
    date: str
    counts: Dict[str, int] = Field(description="各状态的计数：queued / paused / sent / failed / skipped")
    first_queued_at: Optional[str] = None


class OutboxBatchAction(BaseModel):
    """暂停 / 恢复 / 作废一批的请求体"""

    adapter_key: str = Field(description="实例所属适配器")
    instance_key: str = Field(description="实例标识")
    batch_key: str = Field(description="批次键")


@dataclass(frozen=True)
class _Scope:
    """这一组接口的主语：一个实例对"""

    adapter_key: str
    instance_key: str
    instance: DBAdapterInstance
    is_platform_instance: bool


# ===== 作用域 =====


async def _resolve_scope(user: DBUser, adapter_key: str, instance_key: str) -> _Scope:
    """校验并解析出「哪个实例」

    租户必须带两个参数（缺一个就是 400，不是 404：那是请求本身不成立）；平台方也必须带，
    因为这组接口全部是「某个实例的邮箱」。可见性走 `assert_instance_accessible`，
    越权与不存在同一个 404。
    """
    normalized_adapter = (adapter_key or "").strip()
    normalized_instance = (instance_key or "").strip()
    if not normalized_adapter or not normalized_instance:
        raise ValidationError(reason="必须提供 adapter_key 与 instance_key")
    instance = await assert_instance_accessible(user, normalized_instance, adapter_key=normalized_adapter)
    # 「是不是平台方实例」只看实例本身归谁，不看是谁在看：平台方管理员打开一个租户实例时，
    # 这个实例能用哪些平台方邮箱仍要按共享设置算，否则页面上显示的与会话里实际能用的对不上
    return _Scope(
        adapter_key=normalized_adapter,
        instance_key=normalized_instance,
        instance=instance,
        is_platform_instance=instance.tenant_id is None,
    )


async def _load_visible_account(scope: _Scope, account_id: int) -> DBEmailAccount:
    """按 ID 取账户并判归属；别的实例的账户与不存在同一个 404"""
    row = await DBEmailAccount.get_or_none(id=account_id)
    if row is None:
        raise NotFoundError(resource="邮箱账户")
    if row.owner_adapter_key != scope.adapter_key or row.owner_instance_key != scope.instance_key:
        raise NotFoundError(resource="邮箱账户")
    return row


# ===== 账户的读写 =====


def _account_info(row: DBEmailAccount) -> InstanceEmailAccountInfo:
    return InstanceEmailAccountInfo(
        id=row.id,
        adapter_key=row.owner_adapter_key,
        instance_key=row.owner_instance_key,
        username=row.username,
        display_name=row.display_name,
        provider=row.provider,
        settings=dict(row.settings or {}),
        HAS_PASSWORD=bool(row.password),
        enabled=bool(row.enabled),
        receive_enabled=bool(row.receive_enabled),
        send_enabled=bool(row.send_enabled),
        is_default_sender=bool(row.is_default_sender),
        notify_chat_key=row.notify_chat_key,
        attachment_dir=row.attachment_dir,
    )


def _validated_username(value: str) -> str:
    """账户地址：先过地址格式（一个地址、没有空白与 `, ; < >`），再规范成小写

    地址不只是登录名：它是缓存表的键、连接池的键，转回平台方时还会成为附件目录名。
    不校验格式的话，`../x` 这类写法会一路带进这些地方（目录名那一处另有判据兜底，
    但不该靠兜底）。
    """
    if not str(value or "").strip():
        raise ValidationError(reason="请填写邮箱地址")
    try:
        return mail.normalize_username(mail.validate_mail_address(value))
    except ValueError as e:
        raise ValidationError(reason=str(e)) from e


def _normalize_provider(provider: str) -> str:
    """服务商键只认预设表里有的（含「自定义」）

    不认识的键一律拒绝，而不是按自定义处理：自定义主机要过公网校验，而校验只在键等于
    「自定义」时才做——放行一个不认识的键，等于让它带着没校验过的主机进到运行时。
    """
    from nekro_agent.adapters.email.base import EMAIL_PROVIDER_CONFIGS

    key = (provider or "").strip() or CUSTOM_PROVIDER_KEY
    if key not in EMAIL_PROVIDER_CONFIGS:
        raise ValidationError(reason="不认识的邮箱服务商")
    return key


async def _validate_custom_host(provider: str, settings: Dict[str, Any]) -> None:
    """自定义服务商的主机与端口过公网校验；选了预设的不校验

    预设的主机是平台方写死的常量，不是租户填的，没有「拿 NA 的网络位置打内网」这回事。
    三个端口（IMAP、SMTP、SMTP SSL）都要在白名单里：发信客户端优先走 SSL 端口、失败时
    回退到另一个，漏校一个就等于放开一个任意端口。
    """
    if provider != CUSTOM_PROVIDER_KEY:
        return
    checks = (("imap_host", "imap_port", True), ("smtp_host", "smtp_port", True), ("smtp_host", "smtp_ssl_port", False))
    for host_key, port_key, required in checks:
        host = str(settings.get(host_key, "") or "").strip()
        port = settings.get(port_key)
        if not host:
            raise ValidationError(reason=mail.CUSTOM_HOST_UNAVAILABLE)
        if not port:
            if required:
                raise ValidationError(reason=mail.CUSTOM_HOST_UNAVAILABLE)
            continue
        try:
            await mail.assert_custom_mail_host_allowed(host, int(port))
        except (TypeError, ValueError) as e:
            raise ValidationError(reason=mail.CUSTOM_HOST_UNAVAILABLE) from e


async def _validate_notify_chat_key(scope: _Scope, chat_key: str) -> str:
    """通知频道必须属于这个实例（保存时判一次；推送时再判一次）"""
    target = (chat_key or "").strip()
    if not target:
        return ""
    from nekro_agent.models.db_chat_channel import DBChatChannel

    channel = await DBChatChannel.filter(chat_key=target).order_by("id").first()
    if channel is None or channel.adapter_key != scope.adapter_key or (channel.instance_key or "") != scope.instance_key:
        raise ValidationError(reason="通知频道必须属于这个实例")
    return target


def _address_hash(username: str) -> str:
    """日志里的地址只留哈希：日志会被带走、被搜到，地址本身没有写进去的必要"""
    import hashlib

    return hashlib.sha256(mail.normalize_username(username).encode("utf-8")).hexdigest()[:16]


def _require_instance_accounts_enabled() -> None:
    """总闸关着时，所有**写 / 用**的动作一律拒绝；列表类照常可看（管理页要显示存量）

    判据只有服务层那一处（`instance_accounts_enabled`），这里只是把它的结果翻成给页面看的
    `ValidationError`。关掉是平台方的决定，拒绝动作时不必再解释为什么。
    """
    if not mail.instance_accounts_enabled():
        raise ValidationError(reason="平台方已关闭实例自有邮箱")


def _get_email_adapter():
    """取 email 适配器（未启用时退回配置对象：账户表本身还能维护）"""
    from nekro_agent.adapters.email.routers import get_email_adapter

    return get_email_adapter()


async def _sync_row(scope: _Scope, row: DBEmailAccount) -> None:
    """写库之后让运行时客户端跟上（同时把「这个地址是谁的」写进适配器）

    `owner` 必须一起传：只重连客户端、不记归属的话，收信时这个地址会被当成平台方账户，
    于是去调 `collect_message` 建一个没有实例段的 `email-<地址>` 频道——正是本份要堵的路。
    """
    mail.clear_default_sender_cache()
    try:
        await mail.sync_runtime_account(
            _get_email_adapter(),
            mail.to_runtime_account(row),
            owner=mail.instance_owner_of(row),
        )
    except Exception as e:  # noqa: BLE001 - 客户端连不上不该让「账户已保存」变成 500
        logger.warning(f"实例自有邮箱的运行时客户端同步失败: id={row.id} | {type(e).__name__}")


CONNECTION_TEST_TIMEOUT_SECONDS = 20

# 测试连接失败时回给页面的话只有这几句：原始异常里有对端的 banner、端口开没开、解析到哪个地址，
# 租户能拿「测试连接」当端口扫描器用。细节只进服务端日志
TEST_FAILED_HOST = "服务器地址不能使用"
TEST_FAILED_LOGIN = "登录失败：请检查邮箱地址与授权码"
TEST_FAILED_CONNECT = "连接失败：请检查服务器地址、端口与服务商设置"


def _connection_test_failure(error: BaseException) -> str:
    """把测试连接的异常归成一句笼统的话（不带对端回的任何内容）

    归类只在服务层那一处（`classify_mail_failure`）。「服务器地址不能使用」只给**还没连**就被拒的情形；
    连上之后对端核对没过与连不上是同一句，否则租户能拿测试连接判断 NA 连没连上某个内网端口。
    """
    kind = mail.classify_mail_failure(error)
    if kind == mail.MAIL_FAILURE_HOST:
        return TEST_FAILED_HOST
    if kind == mail.MAIL_FAILURE_LOGIN:
        return TEST_FAILED_LOGIN
    return TEST_FAILED_CONNECT


def _test_client(account: EmailAccount) -> Any:
    """测试连接用的客户端：与收发信同一个类、同一个构造方式（连上之后的对端核对也在里面）"""
    from nekro_agent.adapters.email.clients import ImapSmtpPasswordClient

    return ImapSmtpPasswordClient(account, CONNECTION_TEST_TIMEOUT_SECONDS)


async def _test_smtp_connection(account: EmailAccount) -> str:
    """测 SMTP：连上、核对对端、登录、断开（不发信）

    自定义服务器先按主机与端口校验一次（失败给一句明确的话）；权威判据是客户端在连上之后、
    登录之前核对的实际对端 IP——校验与连接各解析一次，中间的 DNS 重绑只有它挡得住。
    """
    if account.EMAIL_ACCOUNT == CUSTOM_PROVIDER_KEY:
        _host, port, _use_ssl = _smtp_endpoint_of(account)
        await mail.assert_custom_mail_host_allowed(account.CUSTOM_SMTP_HOST, port)
    endpoint = await _test_client(account).check_smtp_login(CONNECTION_TEST_TIMEOUT_SECONDS)
    return f"SMTP 连接成功（{endpoint}）"


def _smtp_endpoint_of(account: EmailAccount) -> tuple[str, int, bool]:
    """运行时账户首选的 SMTP 主机、端口与是否 SSL（与发信客户端同一口径）"""
    if account.CUSTOM_SMTP_USE_SSL:
        return account.CUSTOM_SMTP_HOST, int(account.CUSTOM_SMTP_SSL_PORT), True
    return account.CUSTOM_SMTP_HOST, int(account.CUSTOM_SMTP_PORT), False


async def _test_imap_connection(account: EmailAccount) -> str:
    """测 IMAP：连上、核对对端、登录、`select INBOX` 一次

    `select INBOX` 必须做：有些服务商登录成功但信箱不可读（权限没开、容量满），只测登录会把
    「收不到信」判成「连接正常」。
    """
    host = account.CUSTOM_IMAP_HOST
    port = int(account.CUSTOM_IMAP_PORT)
    if not host:
        raise ValueError("缺少 IMAP 服务器地址")
    if account.EMAIL_ACCOUNT == CUSTOM_PROVIDER_KEY:
        await mail.assert_custom_mail_host_allowed(host, port)

    client = _test_client(account)
    try:
        await client.connect()
        await client.select_mailbox("INBOX")
    finally:
        try:
            await client.close()
        except Exception as e:  # noqa: BLE001 - 关不掉不影响「测过了」这个结论
            logger.debug(f"测试 IMAP 之后关闭客户端失败: {type(e).__name__}")
    return f"IMAP 连接成功（{host}:{port}）"


# ===== 端点 =====


@router.get("/providers", summary="服务商预设列表")
@require_role(Role.Admin)
async def list_providers(_current_user: DBUser = Depends(get_current_active_user)) -> List[Dict[str, Any]]:
    """服务商预设：主机、端口、要不要 SSL、页面上照写的那句说明"""
    return list_provider_presets()


@router.get("/accounts", summary="实例自有邮箱账户列表")
@require_role(Role.Admin)
async def list_instance_email_accounts(
    adapter_key: str = Query(..., description="实例所属适配器"),
    instance_key: str = Query(..., description="实例标识"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> List[InstanceEmailAccountInfo]:
    """这个实例自己填的账户；凭据只写不读"""
    scope = await _resolve_scope(_current_user, adapter_key, instance_key)
    rows = await mail.list_instance_accounts(scope.adapter_key, scope.instance_key)
    return [_account_info(row) for row in rows]


@router.post("/accounts", summary="新建实例自有邮箱账户")
@require_role(Role.Admin)
async def create_instance_email_account(
    payload: InstanceEmailAccountPayload,
    adapter_key: str = Query(..., description="实例所属适配器"),
    instance_key: str = Query(..., description="实例标识"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> InstanceEmailAccountInfo:
    """新建：先查上限、再查地址唯一、再校验自定义主机，最后写表并同步运行时

    顺序是「先便宜后贵」：上限与地址都是单条查询，主机校验要解析 DNS；任何一步不过都不该
    留下半行数据。附件目录名要等拿到表 ID 才能算（`_i<id>`），所以先建行再回写。
    """
    scope = await _resolve_scope(_current_user, adapter_key, instance_key)
    _require_instance_accounts_enabled()
    username = _validated_username(payload.username)

    try:
        await mail.assert_instance_account_quota(scope.adapter_key, scope.instance_key)
    except ValueError as e:
        raise ValidationError(reason=str(e)) from e
    try:
        await mail.assert_address_available(username)
    except ValueError as e:
        raise ValidationError(reason=mail.ADDRESS_UNAVAILABLE) from e
    provider = _normalize_provider(payload.provider)
    await _validate_custom_host(provider, payload.settings)
    notify_chat_key = await _validate_notify_chat_key(scope, payload.notify_chat_key)

    if not payload.password:
        raise ValidationError(reason="请填写授权码或密码")

    # 新账户开始轮询之前先清掉这个地址下别人的残留行：唯一键是 (地址, UID)，同一个地址
    # 换过主人时旧行还留着（旧账户删了、新账户建起来），新信的 UID 撞上会被整行改写，
    # 没撞上的那些行仍会以新主人的名义占着位置。附件目录按地址式的那种一并清掉。
    await mail.purge_foreign_cache_rows(
        username,
        owner_adapter_key=scope.adapter_key,
        owner_instance_key=scope.instance_key,
    )

    row = await DBEmailAccount.create(
        owner_adapter_key=scope.adapter_key,
        owner_instance_key=scope.instance_key,
        username=username,
        display_name=payload.display_name.strip(),
        provider=provider,
        settings=dict(payload.settings or {}),
        password=payload.password,
        enabled=payload.enabled,
        receive_enabled=payload.receive_enabled,
        send_enabled=payload.send_enabled,
        is_default_sender=False,
        notify_chat_key=notify_chat_key,
        attachment_dir="",
        created_by=_current_user.username,
        updated_by=_current_user.username,
    )
    # 附件目录用表 ID：ID 不复用，地址换了主人也看不到旧主人的文件
    row.attachment_dir = mail.new_attachment_dir(row.id)
    if payload.is_default_sender:
        row.is_default_sender = True
    await row.save()
    if row.is_default_sender:
        await mail.clear_other_default_senders(scope.adapter_key, scope.instance_key, keep_id=row.id)

    await _sync_row(scope, row)
    return _account_info(row)


@router.put("/accounts/{account_id}", summary="修改实例自有邮箱账户")
@require_role(Role.Admin)
async def update_instance_email_account(
    account_id: int,
    payload: InstanceEmailAccountPayload,
    adapter_key: str = Query(..., description="实例所属适配器"),
    instance_key: str = Query(..., description="实例标识"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> InstanceEmailAccountInfo:
    """修改：`password` 不传或传空串表示不改——**但服务商或主机变了就必须重新填**；地址不能改

    - **地址不能改**：地址是缓存行、连接池、附件目录（由平台方转来的那种）的键。改名要同时
      换掉这几样，任何一样漏了都会出事（别的租户建同名地址时误删目录、平台方加回旧地址时
      附件落进这个实例的目录、旧地址的客户端还在轮询）。换地址请删掉这个账户再新建。
    - **服务商或主机变了要重填授权码**：否则租户把平台方转来的账户（授权码是平台方当初填的）
      主机改成自己的服务器，再点「测试」，就能截到那份授权码。
    """
    scope = await _resolve_scope(_current_user, adapter_key, instance_key)
    _require_instance_accounts_enabled()
    row = await _load_visible_account(scope, account_id)

    username = _validated_username(payload.username)
    if username != mail.normalize_username(row.username):
        raise ValidationError(reason="邮箱地址不能修改；要换地址请删除这个账户后重新添加")
    provider = _normalize_provider(payload.provider)
    await _validate_custom_host(provider, payload.settings)
    notify_chat_key = await _validate_notify_chat_key(scope, payload.notify_chat_key)
    if not payload.password and _endpoint_changed(row, provider, payload.settings):
        raise ValidationError(reason="改了服务商或服务器地址，请重新填写授权码")

    row.display_name = payload.display_name.strip()
    row.provider = provider
    row.settings = dict(payload.settings or {})
    if payload.password:
        # 空串 = 不改：编辑显示名不该把授权码清掉
        row.password = payload.password
    row.enabled = payload.enabled
    row.receive_enabled = payload.receive_enabled
    row.send_enabled = payload.send_enabled
    row.notify_chat_key = notify_chat_key
    row.updated_by = _current_user.username

    if payload.is_default_sender:
        row.is_default_sender = True
    else:
        row.is_default_sender = False
    await row.save()
    if row.is_default_sender:
        await mail.clear_other_default_senders(scope.adapter_key, scope.instance_key, keep_id=row.id)

    await _sync_row(scope, row)
    return _account_info(row)


def _endpoint_changed(row: DBEmailAccount, provider: str, settings: Dict[str, Any]) -> bool:
    """服务商或任何一个主机变了没有（端口在同一台主机上换不出新的接收方，不算）"""
    if provider != (row.provider or CUSTOM_PROVIDER_KEY):
        return True
    if provider != CUSTOM_PROVIDER_KEY:
        return False
    old = dict(row.settings or {})
    for key in ("imap_host", "smtp_host"):
        if str(settings.get(key, "") or "").strip().lower() != str(old.get(key, "") or "").strip().lower():
            return True
    return False


@router.delete("/accounts/{account_id}", summary="删除实例自有邮箱账户")
@require_role(Role.Admin)
async def delete_instance_email_account(
    account_id: int,
    adapter_key: str = Query(..., description="实例所属适配器"),
    instance_key: str = Query(..., description="实例标识"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """删账户、断开运行时客户端，并清掉这个账户的缓存行与附件目录

    三样一起删的理由：账户行没了之后，按地址与快照都查不出这些缓存行属于谁（它们会变成
    孤儿，只能等每日清理），附件目录更没有任何一处能再推导出它的名字。
    附件：`_i<数字>` 形态（新建账户时的命名，表 ID 不复用）整个目录删；地址形态的是「由平台方
    账户转来」的那一种，只删这个账户名下被删掉的那几封（`<目录>/<UID>/`），目录空了再删目录——
    整个删的话，删错一次就再也拿不回来。判据与路径见 `email_accounts.plan_attachment_cleanup`，
    要在删缓存行之前算好。

    顺序：**先断开运行时客户端**，再算附件路径、删行、删文件。客户端还连着时会接着收信、下附件：
    先算路径再断开的话，这中间新到的那封的 `<UID>/` 不在清单里，删完还留在磁盘上。
    """
    scope = await _resolve_scope(_current_user, adapter_key, instance_key)
    _require_instance_accounts_enabled()
    row = await _load_visible_account(scope, account_id)
    runtime = mail.to_runtime_account(row)
    username = mail.normalize_username(row.username)
    try:
        await mail.remove_runtime_account(_get_email_adapter(), runtime)
    except Exception as e:  # noqa: BLE001 - 客户端关不掉不该让删除失败
        logger.warning(f"实例自有邮箱的运行时客户端断开失败: {type(e).__name__}")
    attachment_cleanup = await mail.plan_attachment_cleanup([row])
    await row.delete()
    try:
        await DBEmail.filter(
            account_username=username,
            owner_adapter_key=scope.adapter_key,
            owner_instance_key=scope.instance_key,
        ).delete()
    except Exception as e:  # noqa: BLE001 - 缓存清不掉不该让「账户已删除」变成 500
        logger.warning(f"删除实例自有邮箱的缓存行失败: id={account_id} | {type(e).__name__}")
    mail.run_attachment_cleanup(attachment_cleanup)
    mail.clear_default_sender_cache()
    return {"success": True, "id": account_id}


@router.post("/accounts/{account_id}/test", summary="测试实例自有邮箱的连接")
@require_role(Role.Admin)
async def test_instance_email_account(
    account_id: int,
    adapter_key: str = Query(..., description="实例所属适配器"),
    instance_key: str = Query(..., description="实例标识"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """测试连接：SMTP 与 IMAP 各测一次，两项结果分别写回 `settings`

    页面要回答的是「这个账户能不能收、能不能发」，只测一半会让「能发不能收」显示成正常。
    SMTP 建连接、登录、断开；IMAP 再 `select INBOX` 一次（登录成功但信箱不可读也算失败）。
    失败原因只回类型与短消息，避免把宿主路径带出来。
    """
    import time

    scope = await _resolve_scope(_current_user, adapter_key, instance_key)
    _require_instance_accounts_enabled()
    row = await _load_visible_account(scope, account_id)
    runtime = mail.to_runtime_account(row)

    smtp_success = False
    smtp_message = ""
    try:
        smtp_message = await _test_smtp_connection(runtime)
        smtp_success = True
    except Exception as e:  # noqa: BLE001 - 失败要回给页面，不能变成 500；细节只进日志
        logger.warning(f"实例自有邮箱 SMTP 测试失败: id={account_id} | {type(e).__name__}: {e}")
        smtp_message = _connection_test_failure(e)

    imap_success = False
    imap_message = ""
    try:
        imap_message = await _test_imap_connection(runtime)
        imap_success = True
    except Exception as e:  # noqa: BLE001 - 同上
        logger.warning(f"实例自有邮箱 IMAP 测试失败: id={account_id} | {type(e).__name__}: {e}")
        imap_message = _connection_test_failure(e)

    tested_at = int(time.time())
    settings = dict(row.settings or {})
    settings.update(
        {
            # 旧的两个键保留：页面与既有调用方还在读它，语义是「整体可用」
            "last_test_success": smtp_success and imap_success,
            "last_test_message": smtp_message if smtp_success else f"{smtp_message}；{imap_message}",
            "last_test_time": tested_at,
            "last_test_smtp": smtp_success,
            "last_test_imap": imap_success,
        },
    )
    if imap_success:
        settings.pop("last_receive_error", None)
    else:
        settings["last_receive_error"] = imap_message
    row.settings = settings
    await row.save(update_fields=["settings", "update_time"])
    return {
        "success": smtp_success and imap_success,
        "smtp": {"success": smtp_success, "message": smtp_message},
        "imap": {"success": imap_success, "message": imap_message},
        "message": smtp_message if imap_success else f"{smtp_message}；{imap_message}",
        "tested_at": tested_at,
    }


def _count_new_messages(result: Any) -> int:
    """把 `manual_pull_account` 的结果数成一个「处理了几封」

    它回的是诊断 dict（`processed_count` 是真正走完处理的封数）。容错地接住别的形状：
    手动收信失败时宁可报 0 也不要 500——错误详情在服务端日志里，页面只需要一个计数。
    """
    if isinstance(result, dict):
        for key in ("processed_count", "found_count"):
            try:
                return max(0, int(result.get(key) or 0))
            except (TypeError, ValueError):
                continue
    return 0


@router.post("/accounts/{account_id}/fetch", summary="手动收一次这个账户的信")
@require_role(Role.Admin)
async def fetch_instance_email_account(
    account_id: int,
    adapter_key: str = Query(..., description="实例所属适配器"),
    instance_key: str = Query(..., description="实例标识"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """手动收信：照平台方账户 `POST /accounts/{index}/pull` 的做法，走实例自有那条处理路径

    收上来的信只写缓存、下载附件、按需推通知——**不建频道、不触发 Agent 的聊天路**，
    所以返回值只是计数，不回邮件内容（正文已经进缓存表，页面按 ID 去读）。
    账户停用、收信关着、总闸关着都拒绝：这三个开关任意一个关着时，用户点「收一次」也不该绕过它。
    """
    scope = await _resolve_scope(_current_user, adapter_key, instance_key)
    _require_instance_accounts_enabled()
    row = await _load_visible_account(scope, account_id)
    if not row.enabled:
        raise ValidationError(reason="账户已停用")
    if not row.receive_enabled:
        raise ValidationError(reason="这个账户没有开收信")

    adapter = _get_email_adapter()
    runtime = mail.to_runtime_account(row)
    if row.provider == CUSTOM_PROVIDER_KEY:
        try:
            await mail.assert_custom_mail_host_allowed(runtime.CUSTOM_IMAP_HOST, int(runtime.CUSTOM_IMAP_PORT))
        except (TypeError, ValueError) as e:
            raise ValidationError(reason=mail.CUSTOM_HOST_UNAVAILABLE) from e

    # 归属必须先进适配器：收信处理按它决定走哪条路（没有它就会被当成平台方账户建频道）
    owner_map = getattr(adapter, "instance_account_owner", None)
    if isinstance(owner_map, dict):
        owner_map[runtime.USERNAME] = mail.instance_owner_of(row)
    pull = getattr(adapter, "manual_pull_account", None)
    if pull is None:
        raise ValidationError(reason="邮箱适配器还没初始化，稍后再试")

    try:
        result = await pull(runtime)
    except Exception as e:  # noqa: BLE001 - 收信失败要回给页面，不能变成 500
        # 异常类型名也不回：`ConnectionRefusedError` 与 `TimeoutError` 分得开，就能拿「收一次」探测端口
        logger.warning(f"手动收信失败: id={account_id} | {type(e).__name__}: {e}")
        raise ValidationError(reason=f"收信失败: {mail.public_mail_failure(e)}") from e

    count = _count_new_messages(result)
    logger.info(f"手动收信完成: 账户={row.username}, 实例={scope.adapter_key}/{scope.instance_key}, 处理 {count} 封")
    return {"success": True, "fetched": count}


@router.post("/accounts/{account_id}/to-platform", summary="把这个实例自有邮箱转回平台方")
@require_role(Role.Admin)
async def transfer_instance_account_to_platform(
    account_id: int,
    adapter_key: str = Query(..., description="实例所属适配器"),
    instance_key: str = Query(..., description="实例标识"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """反向动作：把表里的一个账户搬回平台方的 `RECEIVE_ACCOUNTS`

    **平台方专属**：这个动作改变的是平台方自己的配置（而且搬回去之后这个账户对所有实例
    都按共享设置生效），租户不该能替平台方做这个决定，所以租户身份一律 403。

    步骤与「转为实例自有」对称：先写配置（追加一项，`SEND_SHARE` 用默认），再把这个地址下
    全部缓存行的快照改回空，再删表行，最后改挂运行时客户端。
    附件目录是 `_i<id>` 形态的改名成地址目录（与平台方账户的命名一致）；目标目录已存在时
    **拒绝并报错**——两个目录合并会有同名文件互相覆盖的风险，那种事不该由一次点击决定。
    是地址形态的（本来就是从平台方转来的）不动。
    """
    scope = await _resolve_scope(_current_user, adapter_key, instance_key)
    if not is_platform_owner(_current_user):
        logger.warning(f"租户尝试把实例自有邮箱转回平台方，拒绝: user={_current_user.username}")
        raise PermissionDeniedError
    _require_instance_accounts_enabled()
    row = await _load_visible_account(scope, account_id)

    from nekro_agent.adapters.email.config import EmailAccount

    platform_adapter = _get_email_adapter()
    accounts = list(getattr(platform_adapter.config, "RECEIVE_ACCOUNTS", []) or [])
    if any(mail.normalize_username(account.USERNAME) == mail.normalize_username(row.username) for account in accounts):
        raise ValidationError(reason=mail.ADDRESS_UNAVAILABLE)

    # 运行时账户已经把预设与自定义都摊平到 `CUSTOM_*`、认证方式固定 `password` + `imap_smtp`：
    # 搬回平台方之后它必须仍按同一套参数工作，不能退回去读 `row.provider` 猜主机
    runtime = mail.to_runtime_account(row)
    payload = runtime.model_dump()
    # 搬回去之后它不再属于那个实例：共享设置回到默认（不点名、不全开），由平台方重新决定
    payload["SEND_SHARE"] = "none"
    payload["SEND_SHARE_INSTANCES"] = []
    account = EmailAccount(**payload)
    # 服务器是租户填的（`model_dump()` 不带运行时标记）：记下当时的服务器，平台方改掉它之前，
    # 客户端照旧只连校验过的地址、拒内网——域名的 DNS 可能还在租户手里
    if runtime.is_tenant_endpoint():
        account.TENANT_SUPPLIED_HOSTS = account.tenant_hosts_marker()

    old_dir = str(row.attachment_dir or "").strip()
    new_dir = ""
    renamed_from = ""
    if mail.is_instance_attachment_dir(old_dir):
        target = mail.attachment_dir_path(mail.normalize_username(row.username))
        source = mail.attachment_dir_path(old_dir)
        if target is not None and target.exists():
            raise ValidationError(reason="目标附件目录已存在，请先处理它")
        if source is not None and source.is_dir() and target is not None:
            try:
                source.rename(target)
                renamed_from, new_dir = old_dir, target.name
            except Exception as e:  # noqa: BLE001 - 改名失败要回给页面，配置还没动
                logger.warning(f"附件目录改名失败: {old_dir} -> {target.name} | {type(e).__name__}")
                raise ValidationError(reason="附件目录改名失败，未做任何改动") from e

    # 缓存行快照、删表行、写回配置放进**一个事务**，配置写回放在最后（与「转为实例自有」同一个写法）：
    # 写不进去就整块回滚，不留「配置里有它、表里也有它」的两份，也不留「配置里有它、缓存行还记着实例」
    original_accounts = list(accounts)
    try:
        async with in_transaction():
            await DBEmail.filter(
                account_username=mail.normalize_username(row.username),
                owner_adapter_key=scope.adapter_key,
                owner_instance_key=scope.instance_key,
            ).update(owner_adapter_key="", owner_instance_key="")
            await row.delete()
            accounts.append(account)
            platform_adapter.config.RECEIVE_ACCOUNTS = accounts
            platform_adapter.config.dump_config()
    except Exception as e:
        # 事务回滚的是数据库；内存里的配置与目录改名得自己改回去
        platform_adapter.config.RECEIVE_ACCOUNTS = original_accounts
        if renamed_from and new_dir:
            source = mail.attachment_dir_path(new_dir)
            target = mail.attachment_dir_path(renamed_from)
            if source is not None and target is not None and source.is_dir():
                try:
                    source.rename(target)
                except Exception as restore_error:  # noqa: BLE001 - 尽力而为
                    logger.warning(f"回滚附件目录改名失败: {type(restore_error).__name__}")
        raise ValidationError(reason=f"转回平台方失败，配置与账户都没有改动: {type(e).__name__}") from e

    mail.clear_default_sender_cache()

    try:
        await mail.sync_runtime_account(platform_adapter, account)
    except Exception as e:  # noqa: BLE001 - 客户端连不上不该让「已转回平台方」变成 500
        logger.warning(f"转回平台方之后的运行时客户端同步失败: {type(e).__name__}")

    logger.info(
        f"实例自有邮箱已转回平台方: 操作人={_current_user.username}, "
        f"地址哈希={_address_hash(row.username)}, "
        f"原实例={scope.adapter_key}/{scope.instance_key}, 附件目录={renamed_from or old_dir or '(空)'}",
    )
    return {"success": True, "id": account_id, "attachment_dir": new_dir or old_dir}


@router.get("/platform-accounts", summary="这个实例能用的平台方邮箱")
@require_role(Role.Admin)
async def list_platform_accounts_for_instance(
    adapter_key: str = Query(..., description="实例所属适配器"),
    instance_key: str = Query(..., description="实例标识"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> List[PlatformAccountInfo]:
    """平台方邮箱：只给显示名、引用与「对本实例是不是默认发件人」

    地址、服务商、服务器与凭据一律不给：租户没有那条授权；平台方管理员要改这些去
    `/api/adapters/email/accounts`（那一族是平台方门槛）。
    """
    scope = await _resolve_scope(_current_user, adapter_key, instance_key)
    accounts = await mail.instance_accounts(
        scope.adapter_key,
        scope.instance_key,
        is_platform_instance=scope.is_platform_instance,
    )
    tenant_viewer = not is_platform_owner(_current_user)
    return [
        PlatformAccountInfo(
            display_label=mail.visible_label_for(view, tenant_viewer=tenant_viewer),
            account_ref=view.ref,
            is_default_sender=view.is_default_sender,
        )
        for view in accounts.sendable
        if view.kind == mail.KIND_PLATFORM
    ]


@router.get("/send-logs", summary="发信记录")
@require_role(Role.Admin)
async def list_send_logs(
    adapter_key: str = Query(..., description="实例所属适配器"),
    instance_key: str = Query(..., description="实例标识"),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=200),
    _current_user: DBUser = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """发信记录：租户只看 `tenant_id` 快照等于自己的；平台方看这个实例的全部

    平台方账户发的那几行对租户只显示「平台方提供：<显示名>」——记录里的 `account_ref`
    本来就是哈希或表 id，这里再按账户类型换掉显示名，就不存在第二条泄漏地址的路。
    """
    scope = await _resolve_scope(_current_user, adapter_key, instance_key)
    tenant_viewer = not is_platform_owner(_current_user)

    query = DBEmailSendLog.filter(adapter_key=scope.adapter_key, instance_key=scope.instance_key)
    if tenant_viewer:
        query = query.filter(tenant_id=scope.instance.tenant_id)
    total = await query.count()
    rows = await query.order_by("-id").offset(offset).limit(limit)

    # 平台方账户的显示名要按当前配置翻：记录里只有哈希，没有名字
    platform_labels: Dict[str, str] = {}
    for account in mail.platform_accounts():
        view = mail.build_account_view(
            kind=mail.KIND_PLATFORM,
            username=account.USERNAME,
            display_name=account.DISPLAY_NAME,
            send_enabled=bool(account.SEND_ENABLED),
            readable=True,
        )
        # 旧版引用（从 JWT 密钥派生）写下的那些行同样翻得出名字
        for ref in (mail.account_send_log_ref(view), *mail.legacy_send_log_refs(view)):
            platform_labels[ref] = (account.DISPLAY_NAME or "").strip()

    items: List[SendLogInfo] = []
    for row in rows:
        if row.account_kind == mail.KIND_PLATFORM:
            name = platform_labels.get(row.account_ref, "")
            if not name or "@" in name:
                name = "邮箱"
            label = f"平台方提供：{name}" if tenant_viewer else (name if name != "邮箱" else "平台方邮箱")
        else:
            label = "本实例邮箱"
        items.append(
            SendLogInfo(
                id=row.id,
                account_kind=row.account_kind,
                display_label=label,
                purpose=row.purpose,
                to_address=row.to_address,
                subject=row.subject,
                reason=row.reason,
                ok=bool(row.ok),
                # 平台方邮箱的原始 SMTP 报错里会带平台方的地址与服务器：租户只看到「失败」，
                # 与插件那边对租户的掩码同一口径。实例自有邮箱的行现在写进去的就是归类后的一句，
                # 旧版本写下的原始报错（可能带对端地址、分得出连接被拒与超时）读出来时再收一次
                error=(
                    ""
                    if tenant_viewer and row.account_kind == mail.KIND_PLATFORM
                    else mail.public_stored_send_error(row.error) if tenant_viewer else row.error
                ),
                create_time=row.create_time.isoformat(),
            ),
        )
    return {"items": [item.model_dump() for item in items], "total": total}


@router.get("/user-prefs", summary="用户邮件偏好")
@require_role(Role.Admin)
async def list_user_prefs(
    adapter_key: str = Query(..., description="实例所属适配器"),
    instance_key: str = Query(..., description="实例标识"),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=200),
    _current_user: DBUser = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """用户邮件偏好：地址遮掩显示（`a***@qq.com`）"""
    scope = await _resolve_scope(_current_user, adapter_key, instance_key)
    query = DBEmailUserPref.filter(adapter_key=scope.adapter_key, instance_key=scope.instance_key)
    total = await query.count()
    rows = await query.order_by("-id").offset(offset).limit(limit)
    items = [
        UserPrefInfo(
            id=row.id,
            platform_userid=row.platform_userid,
            email=mail.mask_email_address(row.email),
            festival_subscribed=bool(row.festival_subscribed),
            unsubscribed=bool(row.unsubscribed),
            unsubscribed_at=row.unsubscribed_at.isoformat() if row.unsubscribed_at else None,
            set_in_chat_key=row.set_in_chat_key,
        )
        for row in rows
    ]
    return {"items": [item.model_dump() for item in items], "total": total}


@router.delete("/user-prefs/{pref_id}", summary="删除用户邮件偏好")
@require_role(Role.Admin)
async def delete_user_pref(
    pref_id: int,
    adapter_key: str = Query(..., description="实例所属适配器"),
    instance_key: str = Query(..., description="实例标识"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """删除偏好：只清地址与订阅，退订标记留着

    退订标记留着是因为删掉一行偏好不能让这个人重新变成「默认收件人」——那等于用一次删除
    偷偷把他订阅回来。实例划转时「一并清空」才连退订标记一起清（那时换的是新主人）。
    """
    scope = await _resolve_scope(_current_user, adapter_key, instance_key)
    row = await DBEmailUserPref.get_or_none(id=pref_id)
    if row is None or row.adapter_key != scope.adapter_key or row.instance_key != scope.instance_key:
        raise NotFoundError(resource="用户邮件偏好")
    row.email = ""
    row.festival_subscribed = False
    row.set_in_chat_key = ""
    await row.save(update_fields=["email", "festival_subscribed", "set_in_chat_key", "update_time"])
    return {"success": True, "id": pref_id, "unsubscribed": bool(row.unsubscribed)}


@router.get("/outbox/batches", summary="节日邮件队列的各批")
@require_role(Role.Admin)
async def list_outbox_batches(
    adapter_key: str = Query(..., description="实例所属适配器"),
    instance_key: str = Query(..., description="实例标识"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> List[OutboxBatchInfo]:
    """这个实例的各批节日邮件：批次键、节日名、日期、各状态计数、最早入队时间

    租户与平台方都能看：队列里有收件地址与发信理由，但这两样本来就出现在同一组接口的
    发信记录与用户偏好里，不构成新的泄漏面；能不能暂停 / 作废由下面三个动作各自把关。
    """
    from nekro_agent.services import email_outbox

    scope = await _resolve_scope(_current_user, adapter_key, instance_key)
    batches = await email_outbox.list_batches(scope.adapter_key, scope.instance_key)
    return [
        OutboxBatchInfo(
            batch_key=str(entry["batch_key"]),
            festival_name=str(entry["festival_name"]),
            date=str(entry["date"]),
            counts={key: int(value) for key, value in dict(entry["counts"]).items()},
            first_queued_at=entry["first_queued_at"].isoformat() if entry["first_queued_at"] else None,
        )
        for entry in batches
    ]


async def _resolve_batch_scope(user: DBUser, adapter_key: str, instance_key: str, batch_key: str) -> _Scope:
    """解析作用域并确认这一批属于这个实例；不属于与不存在同一个 404

    按 `batch_key` 单独查一次存在性：批次键里带实例对的哈希，但哈希不是判据（它可以被伪造），
    判据是「这个实例名下真的有这一批」。
    """
    from nekro_agent.models.db_email_outbox import DBEmailOutbox

    scope = await _resolve_scope(user, adapter_key, instance_key)
    exists = await DBEmailOutbox.filter(
        adapter_key=scope.adapter_key,
        instance_key=scope.instance_key,
        batch_key=batch_key,
    ).exists()
    if not exists:
        raise NotFoundError(resource="节日邮件批次")
    return scope


def _require_platform_owner(user: DBUser) -> None:
    """暂停 / 恢复 / 作废是平台方专属：租户只能看"""
    if not is_platform_owner(user):
        raise HTTPException(status_code=403, detail="只有平台方能操作节日邮件队列")


@router.post("/outbox/batches/pause", summary="暂停一批节日邮件")
@require_role(Role.Admin)
async def pause_outbox_batch(
    payload: OutboxBatchAction,
    _current_user: DBUser = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """暂停：这一批 `queued` 的改成 `paused`（已发出的不受影响）"""
    from nekro_agent.services import email_outbox

    _require_platform_owner(_current_user)
    scope = await _resolve_batch_scope(_current_user, payload.adapter_key, payload.instance_key, payload.batch_key)
    changed = await email_outbox.pause_batch(scope.adapter_key, scope.instance_key, payload.batch_key)
    return {"success": True, "changed": changed}


@router.post("/outbox/batches/resume", summary="恢复一批节日邮件")
@require_role(Role.Admin)
async def resume_outbox_batch(
    payload: OutboxBatchAction,
    _current_user: DBUser = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """恢复：这一批 `paused` 的改回 `queued`，工人下一轮接着发"""
    from nekro_agent.services import email_outbox

    _require_platform_owner(_current_user)
    scope = await _resolve_batch_scope(_current_user, payload.adapter_key, payload.instance_key, payload.batch_key)
    changed = await email_outbox.resume_batch(scope.adapter_key, scope.instance_key, payload.batch_key)
    return {"success": True, "changed": changed}


@router.post("/outbox/batches/void", summary="作废一批节日邮件")
@require_role(Role.Admin)
async def void_outbox_batch(
    payload: OutboxBatchAction,
    _current_user: DBUser = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """作废：这一批 `queued` / `paused` 的改成 `skipped` 并清空正文"""
    from nekro_agent.services import email_outbox

    _require_platform_owner(_current_user)
    scope = await _resolve_batch_scope(_current_user, payload.adapter_key, payload.instance_key, payload.batch_key)
    changed = await email_outbox.void_batch(scope.adapter_key, scope.instance_key, payload.batch_key)
    return {"success": True, "changed": changed}


__all__ = [
    "InstanceEmailAccountInfo",
    "InstanceEmailAccountPayload",
    "OutboxBatchAction",
    "OutboxBatchInfo",
    "PlatformAccountInfo",
    "SendLogInfo",
    "UserPrefInfo",
    "router",
]
