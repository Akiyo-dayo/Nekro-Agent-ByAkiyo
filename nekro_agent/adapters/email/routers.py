import hashlib
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from tortoise.transactions import in_transaction

from nekro_agent.adapters import load_adapter_config, loaded_adapters
from nekro_agent.adapters.email.base import CUSTOM_PROVIDER_KEY, EMAIL_PROVIDER_CONFIGS
from nekro_agent.adapters.email.clients.oauth import build_oauth_authorize_url, exchange_oauth_code
from nekro_agent.adapters.email.config import EmailAccount
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import PermissionDeniedError
from nekro_agent.services.user.role import Role
from nekro_agent.services.user.tenant_perm import require_platform_owner

logger = get_sub_logger("adapter.email.router")

# 适配器注册器
_email_adapter = None


def set_email_adapter(adapter):
    """设置邮箱适配器实例"""
    global _email_adapter
    _email_adapter = adapter


class _EmailConfigAdapter:
    def __init__(self):
        self.config = load_adapter_config("email")


def get_email_adapter():
    """获取邮箱适配器实例；未启用时回退到配置对象，保证账户配置页可用。"""
    adapter = _email_adapter or loaded_adapters.get("email")
    return adapter or _EmailConfigAdapter()


async def require_platform_mailbox_admin(_owner: DBUser = Depends(require_platform_owner())) -> DBUser:
    """本 router 的统一门槛：平台方 **且** 至少 `Role.Admin`

    与 `routers/email.py` 那四条读接口同一档：那边是 `@require_role(Role.Admin)` +
    按 `(owner_adapter_key, owner_instance_key)` 收敛到某个实例；这里因为要一次覆盖
    整个 router（`APIRouter(dependencies=...)` 用不了装饰器）才收成一个依赖，而且
    这一族只碰**平台方自己的** `RECEIVE_ACCOUNTS`，所以额外要求平台方身份。
    实例自有账户走 `/api/instance-email/*`，那是另一条路（`Role.Admin` + 实例作用域）。
    """
    if _owner.perm_level < Role.Admin:
        logger.debug(f"权限不足，邮箱适配器接口需要管理员: user={_owner.username}")
        raise PermissionDeniedError
    return _owner


# 注意：这里不应该设置prefix，因为在外层已经设置了
#
# ## 为什么整个 router 挂平台方门槛（而不是维持"完全不鉴权"）
#
# 这一族此前**一个鉴权都没有**：`adapters/__init__.py:load_adapters_api` 与
# `routers/__init__.py:152` 都是无依赖挂载，`mount_middlewares` 只有 CORS + 计时，
# 没有全局鉴权。实测匿名请求（不带 Authorization 头）可以：
#
# - `GET /accounts` 读出平台方配置的全部收件账号（地址、服务器、显示名）；
# - `POST /raw-email` 取任意邮件的**网页原文** —— 与 `GET /emails/{id}/raw-content`
#   返回的是同一份数据，后者收敛到实例归属、不锁这里等于那条收敛没有发生；
# - `POST/PUT/DELETE /accounts[/{index}]` 增删改收件账号，
#   `POST /accounts/{index}/test|pull` 让服务端带着平台方凭据发起出站连接；
# - `GET /attachments/status` 读出宿主机上的附件目录绝对路径与体量。
#
# 判据与 `routers/email.py` 同源：收件账号是 email 适配器的**适配器层**配置
# （`RECEIVE_ACCOUNTS`），那是跨租户共享的作用域，`config.py:_assert_config_key_accessible`
# 已经把同类作用域判成"租户身份一律不得触碰"，且用 403 而非 404 —— 能力边界而非资源归属。
#
# 只锁这一个适配器、不动 `adapters/__init__.py`：全部适配器共有的"自带 router 不鉴权"
# 是另一条审计线（其中 webhook/回调类端点本就必须匿名可达，不能一刀切），
# 但 email 这一族是**本轮收敛的那份数据的另一条路**，留着等于绕过。
router = APIRouter(tags=["Adapter:email"], dependencies=[Depends(require_platform_mailbox_admin)])


def _get_accounts(adapter) -> List[EmailAccount]:
    return list(getattr(adapter.config, "RECEIVE_ACCOUNTS", []) or [])


def _save_email_config(adapter) -> None:
    adapter.config.dump_config()


async def _assert_platform_address_available(adapter, username: str, *, ignore_username: str = "") -> None:
    """新建 / 修改平台方账户时查「账户表里有没有同一个地址」

    两处合起来必须全局唯一（同一个地址放在两处会让客户端池与附件目录分不清是谁的），
    冲突时回与实例自有接口同一句文案——不说明被谁占用。

    `ignore_username` 是「改自己」的情形：同一个地址改大小写或首尾空白不算冲突。
    """
    from nekro_agent.services.email_accounts import ADDRESS_UNAVAILABLE, assert_address_available

    try:
        await assert_address_available(
            username,
            ignore_platform_username=ignore_username,
            adapter=adapter,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=ADDRESS_UNAVAILABLE) from e


def _normalize_default_sender(accounts: List[EmailAccount], selected_index: int | None = None) -> None:
    default_indexes = [index for index, account in enumerate(accounts) if account.IS_DEFAULT_SENDER]
    if selected_index is not None and 0 <= selected_index < len(accounts) and accounts[selected_index].IS_DEFAULT_SENDER:
        default_indexes = [selected_index]
    if not default_indexes:
        return
    selected = default_indexes[-1]
    for index, account in enumerate(accounts):
        account.IS_DEFAULT_SENDER = index == selected


def build_account_display_label(account: EmailAccount) -> str:
    display_name = account.DISPLAY_NAME.strip()
    if display_name and account.USERNAME:
        return f"{display_name} ({account.USERNAME})"
    return display_name or account.USERNAME or account.EMAIL_ACCOUNT


def _sanitize_account(account: EmailAccount, index: int) -> dict:
    data = account.model_dump()
    has_password = bool(data.get("PASSWORD"))
    has_client_secret = bool(data.get("CLIENT_SECRET"))
    has_refresh_token = bool(data.get("REFRESH_TOKEN"))
    data["PASSWORD"] = ""
    data["CLIENT_SECRET"] = ""
    data["ACCESS_TOKEN"] = ""
    data["REFRESH_TOKEN"] = ""
    data["HAS_PASSWORD"] = has_password
    data["HAS_CLIENT_SECRET"] = has_client_secret
    data["OAUTH_CONNECTED"] = has_refresh_token
    data["DISPLAY_LABEL"] = build_account_display_label(account)
    data["index"] = index
    return data


def _validate_index(accounts: List[EmailAccount], index: int) -> None:
    if index < 0 or index >= len(accounts):
        raise HTTPException(status_code=404, detail="Email account not found")


@router.get("/status")
async def get_email_status():
    """获取邮箱适配器状态"""
    return {"status": "ok", "message": "Email adapter is running"}


@router.get("/accounts")
async def get_email_accounts(adapter=Depends(get_email_adapter)):
    """获取邮箱账户列表（基于适配器配置，移除敏感字段）"""
    if not getattr(adapter, "config", None):
        return {"error": "Adapter not initialized"}

    accounts = _get_accounts(adapter)
    return {"accounts": [_sanitize_account(account, index) for index, account in enumerate(accounts)]}


@router.post("/accounts")
async def create_email_account(request: dict, adapter=Depends(get_email_adapter)):
    """新增邮箱账户，数据写入 Email 适配器配置。"""
    try:
        account = EmailAccount.model_validate(request)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors()) from e

    await _assert_platform_address_available(adapter, account.USERNAME)
    accounts = _get_accounts(adapter)
    accounts.append(account)
    _normalize_default_sender(accounts, len(accounts) - 1)
    adapter.config.RECEIVE_ACCOUNTS = accounts
    _save_email_config(adapter)
    from nekro_agent.services.email_accounts import (
        clear_default_sender_cache,
        purge_foreign_cache_rows,
        sync_runtime_account,
    )

    # 新账户开始收信之前清掉这个地址下不属于平台方的残留行：同一个地址可能刚被某个实例
    # 释放（旧账户删了、缓存行还在），不清的话新信撞上同一个 UID 会被整行改写（写侧也兜住了），
    # 没撞上的行则会以平台方账户的名义继续占着位置
    await purge_foreign_cache_rows(account.USERNAME, owner_adapter_key="", owner_instance_key="")
    clear_default_sender_cache()
    await sync_runtime_account(adapter, account)
    return {"success": True, "index": len(accounts) - 1}


@router.put("/accounts/{index}")
async def update_email_account(index: int, request: dict, adapter=Depends(get_email_adapter)):
    """更新邮箱账户；未传 PASSWORD 时保留旧密码。"""
    accounts = _get_accounts(adapter)
    _validate_index(accounts, index)

    current = accounts[index].model_dump()
    payload = dict(request)
    if "PASSWORD" not in payload:
        payload["PASSWORD"] = current.get("PASSWORD", "")
    for secret_key in ("CLIENT_SECRET", "ACCESS_TOKEN", "REFRESH_TOKEN"):
        if secret_key not in payload:
            payload[secret_key] = current.get(secret_key, "")

    try:
        accounts[index] = EmailAccount.model_validate({**current, **payload})
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors()) from e

    await _assert_platform_address_available(
        adapter,
        accounts[index].USERNAME,
        ignore_username=str(current.get("USERNAME") or ""),
    )
    _normalize_default_sender(accounts, index)
    adapter.config.RECEIVE_ACCOUNTS = accounts
    _save_email_config(adapter)
    from nekro_agent.services.email_accounts import (
        clear_default_sender_cache,
        normalize_username,
        purge_foreign_cache_rows,
        sync_runtime_account,
    )

    if normalize_username(accounts[index].USERNAME) != normalize_username(str(current.get("USERNAME") or "")):
        # 地址换了：新地址下可能还留着别人的缓存行，理由与「新增」那一段相同
        await purge_foreign_cache_rows(accounts[index].USERNAME, owner_adapter_key="", owner_instance_key="")
    clear_default_sender_cache()
    await sync_runtime_account(adapter, accounts[index])
    return {"success": True}


@router.post("/accounts/{index}/test")
async def test_email_account(index: int, adapter=Depends(get_email_adapter)):
    """测试邮箱账户连接并记录最近一次测试结果。"""
    accounts = _get_accounts(adapter)
    _validate_index(accounts, index)
    account = accounts[index]

    success = False
    stage = "创建客户端"
    message = ""
    client = None
    try:
        create_client = getattr(adapter, "_create_email_client")
        client = create_client(account)
        client_name = client.__class__.__name__
        log_message = (
            f"测试邮箱账户连接: index={index}, username={account.USERNAME}, provider={account.EMAIL_ACCOUNT}, "
            f"auth_type={account.AUTH_TYPE}, transport_type={account.TRANSPORT_TYPE}, oauth_provider={account.OAUTH_PROVIDER}, "
            f"has_password={bool(account.PASSWORD)}, has_refresh_token={bool(account.REFRESH_TOKEN)}, "
            f"token_expires_at={account.TOKEN_EXPIRES_AT}, client={client_name}"
        )
        logger.info(log_message)

        stage = "连接服务器"
        await client.connect()
        logger.info(f"测试邮箱账户连接成功进入选择邮箱阶段: username={account.USERNAME}, client={client_name}")

        stage = "选择邮箱文件夹"
        mailbox = await client.select_mailbox()
        success = True
        message = f"连接成功，已选择邮箱文件夹: {mailbox}"
        logger.info(f"测试邮箱账户连接成功: username={account.USERNAME}, mailbox={mailbox}")
    except Exception as e:
        message = f"{stage}失败: {e!s}"
        logger.exception(
            f"测试邮箱账户连接失败: index={index}, username={account.USERNAME}, provider={account.EMAIL_ACCOUNT}, "
            f"auth_type={account.AUTH_TYPE}, transport_type={account.TRANSPORT_TYPE}, oauth_provider={account.OAUTH_PROVIDER}, "
            f"has_password={bool(account.PASSWORD)}, has_refresh_token={bool(account.REFRESH_TOKEN)}, "
            f"token_expires_at={account.TOKEN_EXPIRES_AT}, stage={stage}"
        )
    finally:
        if client:
            try:
                await client.close()
            except Exception:
                pass

    account.LAST_TEST_SUCCESS = success
    account.LAST_TEST_MESSAGE = message
    account.LAST_TEST_TIME = int(time.time())
    accounts[index] = account
    adapter.config.RECEIVE_ACCOUNTS = accounts
    _save_email_config(adapter)
    return {"success": success, "message": message, "tested_at": account.LAST_TEST_TIME}


@router.post("/accounts/{index}/to-instance")
async def transfer_email_account_to_instance(index: int, request: dict, adapter=Depends(get_email_adapter)):
    """把平台方的一个账户「转为实例自有」：从 `RECEIVE_ACCOUNTS` 搬进账户表

    存量里有不少账户本来就是替某个租户配的（那家租户自己的客服邮箱、通知邮箱）。
    这个动作把它们交给归属实例：该实例可以看到、可以改、由它自己的开关控制，而平台方
    不再替它收发。

    与「转回平台方」对称，几个刻意的取舍：

    - **一个事务**：建账户行与把这个地址下全部缓存行的快照改成目标实例要么都成、要么都不成。
      只成一半会留下「行归实例、缓存归平台方」的中间态，而那正好是读侧看不到、写侧会撞 UID 的形态；
    - **不经过残留清理**：缓存行本来就是在这个地址上收的信，搬过去时它们的归属要跟着改，
      而不是被删掉。清理由「新建账户」那条路负责，那条路的语义是「这个地址换了新主人」；
    - **附件不搬**：目录名沿用原地址（`attachment_dir = 原地址`），文件原地不动。搬文件要
      遍历整个目录、可能很大，而目录名本来就是按地址起的，搬过去之后语义仍然成立；
    - **只支持授权码方式**：OAuth / 官方 API 的凭据是平台方那一整套（刷新令牌、代理、应用凭据），
      搬进表里之后实例既没有能力维护它、也不该拿到它，直接拒绝并说明原因；
    - **按地址认账户，下标只是定位**：请求必须带 `expected_username`（页面上看到的那个地址），与下标处的
      账户对不上就 409。账户列表是数组，另一个平台方管理员在列表加载之后、点确认之前加了或删了一个账户，
      同一个下标就指向了别的邮箱——这一步会把它连同授权码与整个缓存收件箱交给租户实例。写回配置时也按
      地址在**当时的**列表里重新找，不用一开始那份快照的下标（中间有好几次等待）。
    """
    from nekro_agent.services import email_accounts as mail

    accounts = _get_accounts(adapter)
    _validate_index(accounts, index)
    account = accounts[index]

    expected = mail.normalize_username(str(request.get("expected_username", "") or ""))
    if not expected:
        raise HTTPException(status_code=400, detail="必须提供 expected_username（要转的那个邮箱地址）")
    if expected != mail.normalize_username(account.USERNAME):
        raise HTTPException(status_code=409, detail="账户列表已经变了，请刷新后再试")

    target_adapter = str(request.get("adapter_key", "") or "").strip()
    target_instance = str(request.get("instance_key", "") or "").strip()
    if not target_adapter or not target_instance:
        raise HTTPException(status_code=400, detail="必须提供 adapter_key 与 instance_key")

    if account.AUTH_TYPE != "password" or account.TRANSPORT_TYPE != "imap_smtp":
        raise HTTPException(status_code=400, detail="OAuth / API 方式的邮箱不能转为实例自有")

    from nekro_agent.models.db_adapter_instance import TOMBSTONE_STATUS, DBAdapterInstance
    from nekro_agent.models.db_email import DBEmail
    from nekro_agent.models.db_email_account import DBEmailAccount

    instance = (
        await DBAdapterInstance.filter(adapter_key=target_adapter, instance_key=target_instance)
        .exclude(status=TOMBSTONE_STATUS)
        .order_by("id")
        .first()
    )
    if instance is None:
        raise HTTPException(status_code=404, detail="实例不存在")
    try:
        await mail.assert_instance_account_quota(target_adapter, target_instance)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    username = mail.normalize_username(account.USERNAME)
    provider = account.EMAIL_ACCOUNT if account.EMAIL_ACCOUNT in EMAIL_PROVIDER_CONFIGS else CUSTOM_PROVIDER_KEY
    settings: Dict[str, Any] = {}
    if provider == CUSTOM_PROVIDER_KEY:
        # 自定义服务商的主机端口只能存在账户自己的 `settings` 里（预设表里没有它）
        settings = {
            "imap_host": account.CUSTOM_IMAP_HOST,
            "imap_port": int(account.CUSTOM_IMAP_PORT),
            "smtp_host": account.CUSTOM_SMTP_HOST,
            "smtp_port": int(account.CUSTOM_SMTP_PORT),
            "smtp_ssl_port": int(account.CUSTOM_SMTP_SSL_PORT),
            "smtp_use_ssl": bool(account.CUSTOM_SMTP_USE_SSL),
        }

    created_id: Optional[int] = None
    try:
        async with in_transaction():
            # 上面有好几次等待：按地址在当时的列表里重新找，这个账户已经不在（被删、被改了地址）就 409
            current = _get_accounts(adapter)
            position = next(
                (i for i, item in enumerate(current) if mail.normalize_username(item.USERNAME) == username),
                None,
            )
            if position is None:
                raise _AccountListChangedError
            row = await DBEmailAccount.create(
                owner_adapter_key=target_adapter,
                owner_instance_key=target_instance,
                username=username,
                display_name=account.DISPLAY_NAME or "",
                provider=provider,
                settings=settings,
                password=account.PASSWORD or "",
                enabled=bool(account.ENABLED),
                receive_enabled=bool(account.RECEIVE_ENABLED),
                send_enabled=bool(account.SEND_ENABLED),
                is_default_sender=bool(account.IS_DEFAULT_SENDER),
                notify_chat_key="",
                # 附件目录沿用原地址：文件不搬，语义也仍然成立（这个目录收的就是这个地址的信）
                attachment_dir=username,
                created_by="platform",
                updated_by="platform",
            )
            created_id = int(row.id)
            # 只搬**平台方名下**的缓存行（快照为空的那些）：同一个地址下若还留着别的实例的旧行（以前归某个
            # 实例、删账户时缓存没删掉），把它们一并改到目标实例就等于把别人的信交给这个实例
            await DBEmail.filter(account_username=username, owner_adapter_key="", owner_instance_key="").update(
                owner_adapter_key=target_adapter,
                owner_instance_key=target_instance,
            )
            # 配置的写回放在事务里：写不进去就整块回滚，不留「表里有行、配置里还有它」的两份
            current.pop(position)
            adapter.config.RECEIVE_ACCOUNTS = current
            _save_email_config(adapter)
            owner = mail.instance_owner_of(row)
    except _AccountListChangedError as e:
        raise HTTPException(status_code=409, detail="账户列表已经变了，请刷新后再试") from e
    except Exception as e:
        logger.warning(f"转为实例自有失败，已回滚: index={index} | {type(e).__name__}")
        if created_id is not None:
            # 事务回滚是数据库侧的事；配置对象是内存里的，必须自己复原
            await _restore_platform_account(adapter, account, index)
        raise HTTPException(status_code=400, detail="转换失败，配置与账户都没有改动") from e

    mail.clear_default_sender_cache()
    try:
        await mail.remove_runtime_account(adapter, account)
        await mail.sync_runtime_account(adapter, mail.to_runtime_account(row), owner=owner)
    except Exception as e:  # noqa: BLE001 - 客户端改挂失败不该让「已转成」变成 500
        logger.warning(f"转为实例自有之后的运行时客户端改挂失败: {type(e).__name__}")

    logger.info(
        f"平台方邮箱已转为实例自有: 操作人={_current_actor()}, 地址哈希={_address_hash(username)}, "
        f"目标实例={target_adapter}/{target_instance}",
    )
    return {"success": True, "id": created_id, "adapter_key": target_adapter, "instance_key": target_instance}


class _AccountListChangedError(Exception):
    """要转的账户在写回时已经不在当时的列表里了（被删、被改了地址）"""


async def _restore_platform_account(adapter, account: EmailAccount, index: int) -> None:
    """把内存里的配置改回「这一个账户还在 `RECEIVE_ACCOUNTS` 里」

    事务只回滚数据库；`adapter.config` 是配置对象，得自己复原，否则运行时会以为这个账户
    已经搬走了（发信找不到它，而表里也没有）。
    """
    try:
        accounts = _get_accounts(adapter)
        known = [item.USERNAME for item in accounts]
        if account.USERNAME not in known:
            accounts.insert(min(index, len(accounts)), account)
            adapter.config.RECEIVE_ACCOUNTS = accounts
            _save_email_config(adapter)
    except Exception as e:  # noqa: BLE001 - 尽力而为，失败已在上层记过 WARNING
        logger.warning(f"回滚平台方邮箱配置失败: {type(e).__name__}")


def _current_actor() -> str:
    """日志里的操作人；这一族整组挂平台方门槛，取不到具体用户时记 platform"""
    return "platform"


def _address_hash(username: str) -> str:
    """日志里的地址只留哈希：日志会被带走、被搜到，地址本身没有写进去的必要"""
    from nekro_agent.services.email_accounts import normalize_username

    digest = hashlib.sha256(normalize_username(username).encode("utf-8")).hexdigest()
    return digest[:16]


@router.post("/accounts/{index}/pull")
async def pull_email_account(index: int, request: Optional[dict] = None, adapter=Depends(get_email_adapter)):
    """手动拉取指定账户的未读收件箱，并返回诊断统计。"""
    accounts = _get_accounts(adapter)
    _validate_index(accounts, index)
    account = accounts[index]

    manual_pull = getattr(adapter, "manual_pull_account", None)
    if not manual_pull:
        raise HTTPException(status_code=503, detail="Email adapter runtime is not initialized")

    payload = request or {}
    unseen_only = payload.get("unseen_only", True)
    if not isinstance(unseen_only, bool):
        raise HTTPException(status_code=400, detail="unseen_only must be a boolean")

    limit_value = payload.get("limit")
    limit: int | None = None
    if limit_value is not None:
        try:
            limit = int(limit_value)
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail="limit must be an integer") from e
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")

    try:
        result = await manual_pull(account, unseen_only=unseen_only, limit=limit)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"account_index": index, **result}


@router.delete("/accounts/{index}")
async def delete_email_account(index: int, adapter=Depends(get_email_adapter)):
    """删除邮箱账户，数据写回 Email 适配器配置。"""
    accounts = _get_accounts(adapter)
    _validate_index(accounts, index)
    removed_account = accounts.pop(index)
    _normalize_default_sender(accounts)
    adapter.config.RECEIVE_ACCOUNTS = accounts
    _save_email_config(adapter)
    from nekro_agent.services.email_accounts import clear_default_sender_cache, remove_runtime_account

    clear_default_sender_cache()
    await remove_runtime_account(adapter, removed_account)
    return {"success": True}


@router.post("/accounts/{index}/oauth/authorize-url")
async def create_oauth_authorize_url(index: int, request: dict, adapter=Depends(get_email_adapter)):
    """生成 Gmail/Outlook 官方 OAuth 授权跳转链接。"""
    accounts = _get_accounts(adapter)
    _validate_index(accounts, index)
    account = accounts[index]
    if account.EMAIL_ACCOUNT not in {"Gmail", "Outlook"}:
        raise HTTPException(status_code=400, detail="Only Gmail and Outlook support OAuth authorization")
    redirect_uri = str(request.get("redirect_uri") or "")
    if not redirect_uri:
        raise HTTPException(status_code=400, detail="Missing redirect_uri")
    state = str(request.get("state") or f"email:{index}")
    try:
        authorize_url = build_oauth_authorize_url(account, redirect_uri, state)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"authorize_url": authorize_url, "state": state}


@router.post("/accounts/{index}/oauth/callback")
async def handle_oauth_callback(index: int, request: dict, adapter=Depends(get_email_adapter)):
    """处理 Gmail/Outlook OAuth 回调 code，并把 token 写回账户配置。"""
    accounts = _get_accounts(adapter)
    _validate_index(accounts, index)
    account = accounts[index]
    if account.EMAIL_ACCOUNT not in {"Gmail", "Outlook"}:
        raise HTTPException(status_code=400, detail="Only Gmail and Outlook support OAuth authorization")
    code = str(request.get("code") or "")
    redirect_uri = str(request.get("redirect_uri") or "")
    if not code:
        raise HTTPException(status_code=400, detail="Missing code")
    if not redirect_uri:
        raise HTTPException(status_code=400, detail="Missing redirect_uri")
    proxy_url = getattr(adapter.config, "OAUTH_PROXY", "") if account.USE_PROXY else ""
    try:
        await exchange_oauth_code(account, code, redirect_uri, proxy_url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    accounts[index] = account
    adapter.config.RECEIVE_ACCOUNTS = accounts
    _save_email_config(adapter)
    from nekro_agent.services.email_accounts import sync_runtime_account

    await sync_runtime_account(adapter, account)
    return {"success": True}


@router.get("/polling-status")
async def get_polling_status(adapter=Depends(get_email_adapter)):
    """获取邮箱适配器轮询状态"""
    return adapter.get_polling_status()


@router.get("/attachments/status")
async def get_attachments_status(_adapter=Depends(get_email_adapter)):
    """获取附件下载状态"""
    try:
        # 仅统计实际的邮件附件目录，避免将非邮件上传目录计入统计
        save_path = Path(OsEnv.DATA_DIR) / "uploads" / "email_attachment"

        # 统计附件数量和总大小
        attachment_count = 0
        total_size = 0
        email_count = 0
        account_count = 0

        if save_path.exists():
            # 遍历所有邮箱账户目录
            for account_dir in save_path.iterdir():
                if not account_dir.is_dir():
                    continue
                account_count += 1

                # 遍历该账户下的所有邮件目录（按 email_uid）
                for email_uid_dir in account_dir.iterdir():
                    if not email_uid_dir.is_dir():
                        continue

                    # 统计该邮件目录下的附件
                    has_attachments = False
                    for file_path in email_uid_dir.rglob("*"):
                        if file_path.is_file():
                            try:
                                total_size += file_path.stat().st_size
                                attachment_count += 1
                                has_attachments = True
                            except OSError:
                                # 文件可能已被删除或无法访问
                                continue

                    if has_attachments:
                        email_count += 1

        return {
            "save_path": str(save_path),
            "account_count": account_count,
            "email_count": email_count,
            "attachment_count": attachment_count,
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get attachments status: {e!s}") from e


@router.post("/raw-email")
async def get_raw_email_content(request: dict, adapter=Depends(get_email_adapter)):
    """获取邮件原始内容"""

    def _validate_request(req: dict) -> None:
        if not req.get("account_username") or not req.get("email_id"):
            raise HTTPException(status_code=400, detail="Missing account_username or email_id")

    try:
        _validate_request(request)
        account_username = request.get("account_username")
        email_id = request.get("email_id")

        # 调用适配器方法获取原始邮件内容
        return await adapter.get_raw_email_content(account_username, email_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get raw email content: {e!s}") from e


# 同时支持PUT方法，避免405错误
@router.put("/raw-email")
async def get_raw_email_content_put(request: dict, adapter=Depends(get_email_adapter)):
    """获取邮件原始内容 (PUT方法)"""
    return await get_raw_email_content(request, adapter)
