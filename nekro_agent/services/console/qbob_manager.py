"""附带 qbob（QQ 官方机器人 → OneBot v11）的托管

NA 后端经 qbob 的**管理 REST API** 接入与运维 bot，数据面完全复用 `onebot_v11` 适配器。
设计见 `docs/qbob-integration.md`，下面几条是改动时不能破的约束：

1. **不 import qbob 的任何模块**，只经 HTTP 通信（AGPL 边界，设计 §4）。
2. **AppSecret 只在内存里流转**：不落 NA 数据库、不回传前端、不进日志（日志只记 AppID）。
   服务账号口令以 `encrypt_credential` 加密落在数据目录，同样不回传。
3. **出站端点只由后端推导**：主机名与令牌取自 `resolve_self_hostname()` / `resolve_access_token()`，
   与内置 NapCat 的托管条目同一来源；端口取 NA **进程实际监听**的端口（容器内恒为 8021），
   不是 `EXPOSE_PORT`（那是发布到宿主的端口，容器网络里连它连不上）。不接受调用方传入。
4. **先建后接线**：建 bot 时端点为空 → 读出 `self_id` → 绑到实例 → 再写端点。
   一步建好的话 bot 启动即连 NA，此时账号还没绑到实例，握手只认部署级令牌（403）
   或落成「未认领实例」（设计 §7.2）。
5. **只接管带本实例标记的 bot**：`notes` 里写着 `managed-by: nekro-agent deploy=<部署 ID> instance=<实例>`。
   运维在 qbob 管理台手工建的 bot、别的实例的 bot，一律不接管、不删除。部署 ID 随机生成后存在
   数据目录，不随 compose 形态 / `NEKRO_INSTANCE_NAME` 变化。
6. **锁内一律读库里的当前行**：路由在进锁前读出来的实例可能已经旧了（同一实例两次接入并发时，
   第二次拿着旧快照会以为还没接入，把第一个 bot 变成带本实例令牌的孤儿）。

## 凭据错了怎么发现

qbob 的 `self_id` 只由 AppID 决定、在 bot 启动第一步就生成，**与凭据对不对无关**；建 bot 时
同步做一次身份同步，凭据错只记日志，不报错。所以「读得到 self_id」证明不了接入成功。
能用的信号是 qbob 在第一次取 token 失败时写下的 `last_error`（`凭据错误 ×N`，见 qbob `core/bot.py`
的 `_on_credential_error`），以及 bot 被自动停用后从运行列表里消失。建 bot、换 Secret 之后都查一次。

## 会话

qbob 管理 API 只有 cookie 会话（`qqob_session`，存在 qbob 进程内存里，qbob 重启即失效），
没有 API 令牌。这里缓存会话，401 时**重登一次、重试一次**；登录本身失败后 5 分钟内不再自动登录——
qbob 按 (IP, 用户名) 与 IP 锁定失败登录，面板每 10 秒一次的轮询会把 NA 自己锁在外面。
平台方手工提供账号不受这个退避限制。

## 服务账号引导（设计 §6）

第一次需要时，用 qbob 首启写下的 admin 初始口令（`initial_admin_password.txt`，与 NA 同路径挂载）
登录，建一个专用账号 `nekro_agent` 并把口令加密存进 NA 数据目录的 `qbob_state/`；之后只用它。
admin 的口令不动，留给运维。初始口令文件不在了（运维改过 admin 口令）又没有服务账号时，
要平台方在总后台手工提供一个 qbob **admin** 账号（删 bot 需要 admin）。
"""

import asyncio
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import httpx
from tortoise.transactions import in_transaction

from nekro_agent.adapters.onebot_v11.core.channel import ADAPTER_KEY as ONEBOT_ADAPTER_KEY
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv, resolve_self_hostname
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.schemas.console import InstanceProvider, InstanceUpdateRequest
from nekro_agent.schemas.console_qbob import (
    QbobBotInfo,
    QbobBotOptionsRequest,
    QbobOperationError,
    QbobQrPollResponse,
    QbobQrStartResponse,
    QbobServiceStatus,
)
from nekro_agent.schemas.errors import ConflictError, DeploymentCapabilityError, ValidationError
from nekro_agent.services.console import instance_manager
from nekro_agent.services.console.container_manager import resolve_access_token
from nekro_agent.tools.credential_cipher import decrypt_credential, encrypt_credential

logger = get_sub_logger("console.qbob")

SERVICE_USERNAME = "nekro_agent"
SESSION_COOKIE = "qqob_session"
INITIAL_PASSWORD_FILE = "initial_admin_password.txt"
STATE_DIR_NAME = "qbob_state"
SERVICE_ACCOUNT_FILE = "service_account.json"
DEPLOYMENT_ID_FILE = "deployment_id"
METADATA_APPID = instance_manager.METADATA_QBOB_APPID
REQUEST_TIMEOUT_SECONDS = 15.0
# 建 bot / 改 bot / 重载在 qbob 里同步取 token 与平台身份（每个 token 地址 15 秒、/users/@me 30 秒），
# 到 QQ 的网络只丢包时最坏要一分多钟；短于它会报「连不上」而 qbob 那边其实已经改了
SLOW_REQUEST_TIMEOUT_SECONDS = 90.0
# 登录失败后的退避窗口
LOGIN_BACKOFF_SECONDS = 300.0
# 扫码绑定任务在 qbob 里保留 10 分钟，这边同一口径
QR_TASK_TTL_SECONDS = 600.0
# qbob 在 POST /api/bots 里同步拉起 bot，正常一次就读得到 self_id
SELF_ID_POLL_ATTEMPTS = 10
SELF_ID_POLL_INTERVAL_SECONDS = 0.5
# qbob `core/bot.py` 写进 last_error 的两种「bot 已不可用」：凭据无效（_on_credential_error）、平台封禁（_on_fatal_code）
CREDENTIAL_ERROR_MARKERS = ("凭据错误", "平台判决")
# 给租户管理员看的服务级错误：不带内部地址，也不提平台方才有的设置入口
TENANT_SERVICE_MESSAGE = "qbob 服务暂不可用，请联系服务提供方"
# 租户视角下 qbob 明确拒绝 / 状态冲突时的统一说法：原文可能点名平台方才能做的操作，
# 或带出服务账号名，只能进日志
TENANT_OPERATION_MESSAGE = "qbob 侧状态异常，请联系平台方处理"


class QbobError(Exception):
    """调用 qbob 失败；`message` 是给人看的说明，不含任何凭据"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class QbobUnreachableError(QbobError):
    """qbob 管理面连不上（或处理超时）"""


class QbobSetupRequiredError(QbobError):
    """拿不到可用的服务账号，需要平台方手工提供"""


class QbobApiError(QbobError):
    """qbob 返回了非 2xx"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# 部署配置、NA 侧状态文件
# ---------------------------------------------------------------------------


def admin_url() -> str:
    return OsEnv.QBOB_ADMIN_URL.strip().rstrip("/")


def is_configured() -> bool:
    return bool(admin_url())


def qbob_data_dir() -> Path:
    configured = OsEnv.QBOB_DATA_DIR.strip()
    return Path(configured) if configured else Path(OsEnv.DATA_DIR) / "qbob_data"


def state_dir() -> Path:
    """NA 自己关于 qbob 的状态（服务账号、部署 ID）

    不放在 `qbob/` 下：那是 install.sh 拷进来的 Dockerfile / compose 附加文件所在目录，
    也是镜像的构建上下文——放进去会被发给构建器，运维清理「安装文件」时也会被一并删掉。
    """
    return Path(OsEnv.DATA_DIR) / STATE_DIR_NAME


def service_account_path() -> Path:
    return state_dir() / SERVICE_ACCOUNT_FILE


def _write_private(path: Path, text: str) -> None:
    """0600 写盘；先写临时文件再替换，崩溃不会留下半个文件"""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _read_deployment_id() -> str:
    try:
        return (state_dir() / DEPLOYMENT_ID_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def deployment_id() -> str:
    """本部署的稳定标识：第一次用到时随机生成并落盘，之后不变

    **异步入口必须先走 `ensure_deployment_id()`**：库里已有接入时不能在这里静默换一个新 ID，
    否则所有 bot 的归属标记都会对不上（见那个函数的说明）。
    """
    existing = _read_deployment_id()
    if existing:
        return existing
    generated = secrets.token_hex(8)
    _write_private(state_dir() / DEPLOYMENT_ID_FILE, generated)
    return generated


async def _has_attached_qbob() -> bool:
    """库里是否还有带 `qbob_appid` 的内置 qbob 实例（不看墓碑：接入记录在就说明用过部署 ID）"""
    rows = await DBAdapterInstance.filter(provider=InstanceProvider.BUILTIN_QBOB.value).values_list(
        "metadata_json",
        flat=True,
    )
    for raw in rows:
        try:
            metadata = json.loads(raw or "{}")
        except (ValueError, TypeError):
            continue
        if isinstance(metadata, dict) and str(metadata.get(METADATA_APPID) or ""):
            return True
    return False


async def ensure_deployment_id() -> str:
    """读部署 ID；文件丢了而库里已有接入时拒绝静默重建

    部署 ID 嵌在 qbob 每个 bot 的 `notes` 里。文件一丢就换新 ID，NA 会把自己建的 bot 全当成
    「别人的」：状态页报归属标记不符，重载、开关、重新接线、解绑全部被拒。所以这里宁可报错，
    让人从备份恢复，也不悄悄制造一堆认不回来的 bot。
    """
    if _read_deployment_id():
        return deployment_id()
    if await _has_attached_qbob():
        raise QbobSetupRequiredError(
            "部署 ID 文件 qbob_state/deployment_id 丢失，NA 认不出自己建的 bot；"
            "请从备份恢复这个文件，或在 qbob 管理台把这些 bot 的备注改成新标记",
        )
    return deployment_id()


def read_initial_admin_password() -> str:
    """qbob 首启写下的 admin 口令（格式 `admin / <口令>`）；文件不在或读不了返回空串"""
    path = qbob_data_dir() / INITIAL_PASSWORD_FILE
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    username, sep, password = text.partition(" / ")
    return password.strip() if sep and username.strip() == "admin" else ""


def load_service_account() -> Optional[Tuple[str, str]]:
    try:
        data = json.loads(service_account_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    username = str(data.get("username") or "")
    password = decrypt_credential(str(data.get("password") or ""))
    return (username, password) if username and password else None


def save_service_account(username: str, password: str) -> None:
    _write_private(
        service_account_path(),
        json.dumps({"username": username, "password": encrypt_credential(password)}, ensure_ascii=False),
    )


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------


def _detail_of(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict) and body.get("detail"):
        return str(body["detail"])[:200]
    return response.text[:200]


class QbobClient:
    """qbob 管理 REST API 客户端（带会话缓存、登录退避与服务账号引导）"""

    def __init__(self, base_url: str, *, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._transport = transport
        self._session_token = ""
        self._session_lock = asyncio.Lock()
        self._login_blocked_until = 0.0

    def _http(self, timeout: float) -> httpx.AsyncClient:
        # trust_env=False：本机系统代理会把容器网络内的流量也劫走
        return httpx.AsyncClient(base_url=self.base_url, transport=self._transport, timeout=timeout, trust_env=False)

    async def _send(
        self,
        method: str,
        path: str,
        token: str,
        *,
        json_body: Any = None,
        params: Optional[Mapping[str, str]] = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> httpx.Response:
        headers = {"Cookie": f"{SESSION_COOKIE}={token}"} if token else {}
        try:
            async with self._http(timeout) as http:
                return await http.request(method, path, json=json_body, params=params, headers=headers)
        except httpx.TimeoutException as e:
            logger.warning(f"qbob 请求超时: {method} {path} ({self.base_url})")
            raise QbobUnreachableError("qbob 处理超时：操作可能已经生效，请稍后刷新状态再决定是否重试") from e
        except httpx.HTTPError as e:
            # 地址只进日志：错误信息会被带给租户管理员
            logger.warning(f"连不上 qbob 管理面 {self.base_url}: {type(e).__name__}")
            raise QbobUnreachableError(f"连不上 qbob 管理面（{type(e).__name__}）") from e

    async def _login(self, username: str, password: str) -> Tuple[str, str]:
        """登录，返回 (会话, 角色)"""
        response = await self._send("POST", "/api/login", "", json_body={"username": username, "password": password})
        if response.status_code != 200:
            raise QbobApiError(response.status_code, f"qbob 登录失败（账号 {username}）：{_detail_of(response)}")
        token = response.cookies.get(SESSION_COOKIE) or ""
        if not token:
            raise QbobApiError(response.status_code, "qbob 登录成功但没有返回会话")
        try:
            role = str(response.json().get("role") or "")
        except (ValueError, AttributeError):
            role = ""
        return token, role

    def _block_login(self, reason: str) -> QbobSetupRequiredError:
        self._login_blocked_until = time.monotonic() + LOGIN_BACKOFF_SECONDS
        return QbobSetupRequiredError(
            f"{reason}。{int(LOGIN_BACKOFF_SECONDS // 60)} 分钟内不再自动登录（避免被 qbob 锁定）；"
            "请在总后台的 qbob 服务设置里手工提供一个 qbob 的 admin 账号。",
        )

    async def _bootstrap_service_account(self) -> Tuple[str, str]:
        """用 admin 初始口令建（或重置）服务账号并落盘"""
        admin_password = read_initial_admin_password()
        if not admin_password:
            raise QbobSetupRequiredError(
                "NA 还没有 qbob 服务账号，而 qbob 的 admin 初始口令文件已不存在（可能已改过口令）。"
                "请在总后台的 qbob 服务设置里手工提供一个 qbob 的 admin 账号。",
            )
        try:
            admin_token, _ = await self._login("admin", admin_password)
        except QbobApiError as e:
            raise self._block_login(f"用 qbob 初始口令登录失败：{e.message}") from e
        password = secrets.token_urlsafe(32)
        body = {"username": SERVICE_USERNAME, "password": password, "role": "admin"}
        response = await self._send("POST", "/api/users", admin_token, json_body=body)
        if response.status_code == 409:
            response = await self._send(
                "PUT",
                f"/api/users/{SERVICE_USERNAME}",
                admin_token,
                json_body={"password": password, "role": "admin"},
            )
        if response.status_code != 200:
            raise QbobSetupRequiredError(f"在 qbob 里建立服务账号失败：{_detail_of(response)}")
        save_service_account(SERVICE_USERNAME, password)
        logger.info(f"已在 qbob 里建立 NA 服务账号 {SERVICE_USERNAME}")
        try:
            await self._send("POST", "/api/logout", admin_token)
        except QbobError:
            pass
        return SERVICE_USERNAME, password

    async def _ensure_session(self, stale: str = "") -> str:
        async with self._session_lock:
            if self._session_token and self._session_token != stale:
                return self._session_token
            self._session_token = ""
            remaining = self._login_blocked_until - time.monotonic()
            if remaining > 0:
                raise QbobSetupRequiredError(
                    f"上次登录 qbob 失败，{int(remaining)} 秒内不再自动重试；"
                    "请在总后台的 qbob 服务设置里手工提供一个 qbob 的 admin 账号。",
                )
            account = load_service_account()
            if account is not None:
                try:
                    self._session_token, _ = await self._login(*account)
                    return self._session_token
                except QbobApiError as e:
                    if e.status_code != 401:
                        raise
                    # 服务账号在 qbob 里没了（库被重置、被人删掉、口令被改）：有初始口令就重建一次，没有就交给人
                    logger.warning(f"qbob 服务账号登录被拒，尝试重新建立: {e.message}")
                    if not read_initial_admin_password():
                        raise self._block_login(f"qbob 服务账号 {account[0]} 登录被拒") from e
            username, password = await self._bootstrap_service_account()
            self._session_token, _ = await self._login(username, password)
            return self._session_token

    async def set_service_credential(self, username: str, password: str) -> None:
        """平台方手工提供账号：先验证能登录且是 admin，再落盘；不受登录退避限制"""
        async with self._session_lock:
            token, role = await self._login(username, password)
            if role != "admin":
                raise QbobError(f"qbob 账号 {username} 的角色是 {role or '未知'}，需要 admin（删除 bot 需要 admin 权限）")
            save_service_account(username, password)
            self._session_token = token
            self._login_blocked_until = 0.0

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[Mapping[str, str]] = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> Any:
        token = await self._ensure_session()
        response = await self._send(method, path, token, json_body=json_body, params=params, timeout=timeout)
        if response.status_code == 401:
            token = await self._ensure_session(stale=token)
            response = await self._send(method, path, token, json_body=json_body, params=params, timeout=timeout)
        if response.status_code >= 400:
            raise QbobApiError(response.status_code, _detail_of(response))
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None


_client: Optional[QbobClient] = None


def get_client() -> QbobClient:
    global _client
    url = admin_url()
    if not url:
        raise DeploymentCapabilityError(operation="管理 qbob", reason="本部署没有附带 qbob（未设置 NEKRO_QBOB_ADMIN_URL）")
    if _client is None or _client.base_url != url:
        _client = QbobClient(url)
    return _client


def _as_app_error(error: Exception, operation: str, *, platform_view: bool = True) -> QbobOperationError:
    """把内部错误转成给前端的业务错误

    租户视角下 qbob 明确拒绝 / 状态冲突的原文不能出去：它可能点名「解绑」这种只有平台方
    能做的操作，或带出服务账号名。原文只进 WARNING 日志（这些错误里没有 Secret / 口令）。
    """
    reason = str(error)
    if not platform_view and isinstance(
        error,
        (QbobUnreachableError, QbobSetupRequiredError, QbobApiError, ValidationError, ConflictError),
    ):
        logger.warning(f"{operation}失败（租户视角只给通用说明）: {reason}")
        reason = (
            TENANT_OPERATION_MESSAGE
            if isinstance(error, (QbobApiError, ValidationError, ConflictError))
            else TENANT_SERVICE_MESSAGE
        )
    return QbobOperationError(operation=operation, reason=reason)


# ---------------------------------------------------------------------------
# 服务级
# ---------------------------------------------------------------------------


async def service_status() -> QbobServiceStatus:
    if not is_configured():
        return QbobServiceStatus(
            qbob_ref=OsEnv.QBOB_REF,
            message="本部署没有附带 qbob（未设置 NEKRO_QBOB_ADMIN_URL）",
        )
    try:
        data = await get_client().request("GET", "/api/status")
    except QbobUnreachableError as e:
        return QbobServiceStatus(configured=True, qbob_ref=OsEnv.QBOB_REF, message=e.message)
    except QbobError as e:
        return QbobServiceStatus(configured=True, reachable=True, qbob_ref=OsEnv.QBOB_REF, message=e.message)
    data = data if isinstance(data, dict) else {}
    bots = data.get("bots")
    return QbobServiceStatus(
        configured=True,
        reachable=True,
        service_account_ready=True,
        version=str(data.get("version") or ""),
        qbob_ref=OsEnv.QBOB_REF,
        bot_count=len(bots) if isinstance(bots, list) else 0,
    )


async def set_service_credential(username: str, password: str) -> None:
    try:
        await get_client().set_service_credential(username.strip(), password)
    except QbobError as e:
        raise _as_app_error(e, "设置 qbob 服务账号") from e
    logger.info(f"平台方手工设置了 qbob 服务账号 {username.strip()}")


# ---------------------------------------------------------------------------
# 实例级
# ---------------------------------------------------------------------------


@dataclass
class _QrTask:
    instance_key: str
    created: float
    # 接入中：qbob 在 ready / exists 时已经删掉了它那边的任务，这期间再问只会得到 404
    attaching: bool = False
    attached: bool = False
    # 扫码已出结果但接入失败时记下来：qbob 那边的任务这时已经没了，后续轮询要能看到真正的原因
    failed: bool = False
    conflict: bool = False
    message: str = ""
    bot: Optional[QbobBotInfo] = None

    def outcome(self) -> Optional[QbobQrPollResponse]:
        # 终态留到下一次轮询才删：慢接入时发起请求的那个浏览器可能已经超时，
        # 结果必须还能被下一次轮询取到
        if self.attached:
            return QbobQrPollResponse(status="attached", message=self.message, bot=self.bot)
        if self.conflict:
            return QbobQrPollResponse(status="conflict", message=self.message)
        if self.failed:
            return QbobQrPollResponse(status="failed", message=self.message)
        return None


_instance_locks: Dict[str, asyncio.Lock] = {}
_qr_tasks: Dict[str, _QrTask] = {}


def _lock_for(instance: DBAdapterInstance) -> asyncio.Lock:
    return _instance_locks.setdefault(instance.instance_key, asyncio.Lock())


def managed_note(instance: DBAdapterInstance) -> str:
    """写进 qbob bot `notes` 的归属标记"""
    return f"managed-by: nekro-agent deploy={deployment_id()} instance={instance.instance_key}"


def onebot_listen_port() -> int:
    """NA 进程实际监听的端口（容器网络里直连 NA 要用它）；与 CC 中继同一个判据"""
    from nekro_agent.services.workspace import cc_credential

    return cc_credential.relay_listen_port()


def expected_endpoint(instance: DBAdapterInstance) -> Dict[str, str]:
    """NA 期望 qbob 为这个 bot 配的唯一一条反向 WS 端点"""
    return {
        "url": f"ws://{resolve_self_hostname()}:{onebot_listen_port()}/onebot/v11/ws",
        "access_token": resolve_access_token(instance),
    }


def attached_appid(instance: DBAdapterInstance) -> str:
    return str(instance_manager.load_metadata(instance).get(METADATA_APPID) or "")


def require_qbob_instance(instance: DBAdapterInstance) -> None:
    if instance_manager.map_provider(instance.provider) is not InstanceProvider.BUILTIN_QBOB:
        raise ValidationError(reason=f"实例 '{instance.instance_key}' 不是内置 qbob 实例")
    if instance.adapter_key != ONEBOT_ADAPTER_KEY:
        raise ValidationError(reason=f"内置 qbob 实例必须挂在 {ONEBOT_ADAPTER_KEY} 适配器下")


async def _fresh(instance: DBAdapterInstance) -> DBAdapterInstance:
    row = await DBAdapterInstance.get_or_none(id=instance.id)
    if row is None:
        raise ValidationError(reason=f"实例 '{instance.instance_key}' 已不存在")
    return row


def _sync_back(target: DBAdapterInstance, source: DBAdapterInstance) -> None:
    """把锁内读写过的库中当前值带回调用方那份对象"""
    target.provider_account_id = source.provider_account_id
    target.status = source.status
    target.metadata_json = source.metadata_json


async def _write_metadata(instance: DBAdapterInstance, patch: Dict[str, Any]) -> None:
    """在行锁下按库里当前值合并写回（同 `container_manager._write_container_metadata` 的理由）"""
    async with in_transaction() as connection:
        row = await DBAdapterInstance.filter(id=instance.id).using_db(connection).select_for_update().first()
        if row is None:
            return
        metadata = instance_manager.load_metadata(row)
        for key, value in patch.items():
            if value is None:
                metadata.pop(key, None)
            else:
                metadata[key] = value
        row.metadata_json = instance_manager.dump_metadata(metadata)
        await row.save(update_fields=["metadata_json", "update_time"], using_db=connection)
    instance.metadata_json = row.metadata_json


async def _find_bot(appid: str) -> Optional[Dict[str, Any]]:
    """按 AppID 精确找 bot（qbob 的 q 参数是模糊匹配）"""
    bots = await get_client().request("GET", "/api/bots", params={"q": appid})
    for bot in bots if isinstance(bots, list) else []:
        if isinstance(bot, dict) and str(bot.get("appid")) == appid:
            return bot
    return None


async def _status_of(appid: str) -> Dict[str, Any]:
    """运行快照；qbob 只列在运行的 bot，被停用（含凭据无效自动停用）的拿到空字典"""
    data = await get_client().request("GET", "/api/status", params={"appids": appid})
    bots = data.get("bots") if isinstance(data, dict) else None
    for snap in bots if isinstance(bots, list) else []:
        if isinstance(snap, dict) and str(snap.get("appid")) == appid:
            return snap
    return {}


async def _wait_self_id(appid: str) -> int:
    for attempt in range(SELF_ID_POLL_ATTEMPTS):
        bot = await _find_bot(appid)
        self_id = int((bot or {}).get("self_id") or 0)
        if self_id > 0:
            return self_id
        if attempt + 1 < SELF_ID_POLL_ATTEMPTS:
            await asyncio.sleep(SELF_ID_POLL_INTERVAL_SECONDS)
    return 0


async def _credential_problem(appid: str) -> str:
    """建 bot / 换 Secret 之后，qbob 是否已经判定这个 bot 不可用；可用返回空串"""
    snap = await _status_of(appid)
    if not snap:
        return "qbob 没能让这个 bot 运行起来（多半是 AppID / AppSecret 无效已被自动停用），请核对凭据后重新提交"
    last_error = str(snap.get("last_error") or "")
    if any(marker in last_error for marker in CREDENTIAL_ERROR_MARKERS):
        return f"QQ 开放平台拒绝了这个 bot：{last_error}。请核对 AppID / AppSecret 后重新提交"
    return ""


def _validate_credentials(appid: str, secret: Optional[str]) -> None:
    if not (appid.isascii() and appid.isdigit()):
        raise ValidationError(reason="AppID 应为纯数字")
    if secret is not None and (not secret.strip() or "•" in secret):
        raise ValidationError(reason="AppSecret 不能为空")


async def _attach(instance: DBAdapterInstance, appid: str, secret: Optional[str]) -> QbobBotInfo:
    """接入的唯一实现。`secret=None` 表示 bot 已在 qbob 里（且必须带本实例标记）"""
    require_qbob_instance(instance)
    appid = appid.strip()
    _validate_credentials(appid, secret)
    # 归属标记要用部署 ID：文件丢了而库里有接入时这里就会拒绝，不能再生成一个新的
    await ensure_deployment_id()
    note = managed_note(instance)
    async with _lock_for(instance):
        row = await _fresh(instance)
        current = attached_appid(row)
        if current and current != appid:
            raise ConflictError(resource=f"本实例已接入的 bot {current}（请先解绑）")

        created = False
        bot = await _find_bot(appid)
        if bot is not None and str(bot.get("notes") or "") != note:
            raise ConflictError(resource=f"qbob 里的 bot {appid}（不是本实例托管的，请到 qbob 管理台处理）")
        if bot is None:
            if secret is None:
                raise ValidationError(reason=f"qbob 里没有 bot {appid}，需要提供 AppSecret")
            await get_client().request(
                "POST",
                "/api/bots",
                json_body={
                    "appid": appid,
                    "secret": secret.strip(),
                    "name": row.display_name or row.instance_key,
                    "notes": note,
                    # 端点留空：先拿 self_id 绑好账号，再接线（模块文档第 4 条）
                    "onebot_endpoints": [],
                    # 群默认可收，开关交给 NA 的频道管理
                    "group_list_mode": "black",
                    "report_self_message": True,
                    "enabled": True,
                },
                timeout=SLOW_REQUEST_TIMEOUT_SECONDS,
            )
            created = True
            logger.info(f"已在 qbob 里为实例 {row.instance_key} 建立 bot {appid}")
        else:
            # 本实例自己的 bot：重新提交了凭据（多半是 AppSecret 换过）就写进去；凭据无效时 qbob 会把它
            # 自动停用，而 qbob 的重载不会拉起停用的 bot，所以一并启用
            patch: Dict[str, Any] = {"enabled": True}
            if secret is not None:
                patch["secret"] = secret.strip()
            if secret is not None or not bot.get("enabled"):
                await get_client().request("PUT", f"/api/bots/{appid}", json_body=patch, timeout=SLOW_REQUEST_TIMEOUT_SECONDS)

        try:
            self_id = await _wait_self_id(appid)
            if self_id <= 0:
                raise QbobError("qbob 没能启动这个 bot（请到 qbob 管理台查看错误）")
            problem = await _credential_problem(appid)
            if problem:
                raise QbobError(problem)
            await _bind_account(row, str(self_id))
        except Exception:
            if created:
                await _delete_bot_quietly(appid)
            raise

        await _write_metadata(row, {METADATA_APPID: appid})
        await get_client().request(
            "PUT",
            f"/api/bots/{appid}",
            json_body={"onebot_endpoints": [expected_endpoint(row)]},
            timeout=SLOW_REQUEST_TIMEOUT_SECONDS,
        )
        _sync_back(instance, row)
        logger.info(f"实例 {row.instance_key} 已接入 qbob bot {appid}（self_id={self_id}）")
    return await get_bot_info(instance)


async def _bind_account(row: DBAdapterInstance, account_id: str) -> None:
    """改实例绑定的账号；走 `update_instance` 那一条现成路径（查重、清路由缓存、对齐状态）

    `update_instance` 会整行 `save()`，所以先重读库里的当前行（掉线监测可能刚写过 `metadata_json`）。
    """
    fresh = await _fresh(row)
    if fresh.provider_account_id != account_id:
        fresh = await instance_manager.update_instance(fresh, InstanceUpdateRequest(provider_account_id=account_id))
    _sync_back(row, fresh)


async def _delete_bot_quietly(appid: str) -> None:
    try:
        await get_client().request("DELETE", f"/api/bots/{appid}")
        logger.info(f"接入失败，已回滚删除刚建的 qbob bot {appid}")
    except QbobError as e:
        logger.warning(f"接入失败后回滚删除 qbob bot {appid} 也失败了，需要到 qbob 管理台手工删除: {e.message}")


async def attach_with_credentials(instance: DBAdapterInstance, appid: str, secret: str) -> QbobBotInfo:
    try:
        return await _attach(instance, appid, secret)
    except QbobError as e:
        raise _as_app_error(e, "接入 qbob bot") from e


def _remember_qr_task(task_id: str, instance: DBAdapterInstance) -> None:
    now = time.monotonic()
    for stale in [key for key, task in _qr_tasks.items() if now - task.created > QR_TASK_TTL_SECONDS]:
        _qr_tasks.pop(stale, None)
    _qr_tasks[task_id] = _QrTask(instance_key=instance.instance_key, created=now)


async def start_qrconnect(instance: DBAdapterInstance) -> QbobQrStartResponse:
    require_qbob_instance(instance)
    try:
        data = await get_client().request("POST", "/api/qrconnect/start")
    except QbobError as e:
        raise _as_app_error(e, "生成 qbob 扫码接入二维码") from e
    data = data if isinstance(data, dict) else {}
    task_id = str(data.get("task_id") or "")
    if not task_id:
        raise QbobOperationError(operation="生成 qbob 扫码接入二维码", reason="qbob 没有返回任务号")
    _remember_qr_task(task_id, instance)
    return QbobQrStartResponse(
        task_id=task_id,
        connect_url=str(data.get("connect_url") or ""),
        qr_svg=str(data.get("qr_svg") or ""),
    )


async def poll_qrconnect(instance: DBAdapterInstance, task_id: str) -> QbobQrPollResponse:
    require_qbob_instance(instance)
    task = _qr_tasks.get(task_id)
    if task is None or task.instance_key != instance.instance_key:
        # 别的实例发起的任务、或已过期被清掉的任务：不替它轮询，免得把 A 实例扫到的 bot 接到 B 上
        return QbobQrPollResponse(status="expired", message="扫码任务不存在或已过期，请重新生成二维码")
    if task.attaching:
        # qbob 那边在 ready / exists 时就把任务删了；接入还没结束时再去问只会得到 404，
        # 从而把「正在接入」误报成「已过期」
        return QbobQrPollResponse(status="pending")
    settled = task.outcome()
    if settled is not None:
        # 终态被取走后才删：慢接入时第一次轮询的浏览器可能已经超时，结果要留给下一次
        _qr_tasks.pop(task_id, None)
        return settled
    try:
        data = await get_client().request("GET", f"/api/qrconnect/poll/{task_id}")
    except QbobApiError as e:
        if e.status_code == 404:
            if task.attaching:
                return QbobQrPollResponse(status="pending")
            _qr_tasks.pop(task_id, None)
            return QbobQrPollResponse(status="expired", message="扫码任务已过期，请重新生成二维码")
        raise _as_app_error(e, "查询 qbob 扫码结果") from e
    except QbobError as e:
        raise _as_app_error(e, "查询 qbob 扫码结果") from e

    data = data if isinstance(data, dict) else {}
    status = str(data.get("status") or "")
    appid = str(data.get("appid") or "")
    if status == "expired":
        _qr_tasks.pop(task_id, None)
        return QbobQrPollResponse(status="expired", message="二维码已过期，请重新生成")
    if status not in {"ready", "exists"} or not appid:
        return QbobQrPollResponse(status="pending")

    # ready 带着 secret：只在这里用一次，不回传、不记日志。qbob 已经清掉了它那边的任务，
    # 所以先置「接入中」，其余轮询一律 pending；结果留在本地任务上，下一次轮询取走后才删
    secret = str(data.get("secret") or "") if status == "ready" else None
    task.attaching = True
    try:
        info = await _attach(instance, appid, secret)
    except ConflictError as e:
        task.conflict, task.message = True, str(e)
    except ValidationError as e:
        task.failed, task.message = True, str(e)
    except QbobError as e:
        task.failed, task.message = True, e.message
    else:
        task.attached, task.message, task.bot = True, f"已接入 bot {appid}", info
    finally:
        task.attaching = False
    return task.outcome() or QbobQrPollResponse(status="pending")


async def get_bot_info(instance: DBAdapterInstance, *, platform_view: bool = True) -> QbobBotInfo:
    """读状态；取不到时给出说明而不是报错（面板要的是状态，不是 500）

    `platform_view=False`（租户管理员）时，服务级问题只给通用说明：不带内部地址，
    也不提只有平台方才有的设置入口与操作。
    """
    if not is_configured():
        return QbobBotInfo(message="本部署没有附带 qbob")
    appid = attached_appid(instance)
    if not appid:
        return QbobBotInfo(configured=True, message="尚未接入 bot")
    try:
        # 必须在任何 `managed_note()` 之前：文件丢了而库里有接入时，比对备注会顺手生成新 ID，
        # 那正是要避免的事。状态页这里只把原因写进 message，不抛错。
        await ensure_deployment_id()
        bot = await _find_bot(appid)
        if bot is None:
            return QbobBotInfo(
                configured=True,
                attached=True,
                appid=appid,
                message=(
                    "qbob 里找不到这个 bot（可能在 qbob 管理台被删除了），可以解绑后重新接入"
                    if platform_view
                    else "qbob 里找不到这个 bot，请联系服务提供方"
                ),
            )
        snap = await _status_of(appid)
    except QbobError as e:
        return QbobBotInfo(configured=True, attached=True, appid=appid, message=e.message if platform_view else TENANT_SERVICE_MESSAGE)

    expected = expected_endpoint(instance)
    endpoints = bot.get("onebot_endpoints") if isinstance(bot.get("onebot_endpoints"), list) else []
    endpoint_ok = endpoints == [expected]
    links: List[Dict[str, Any]] = snap.get("links") if isinstance(snap.get("links"), list) else []
    onebot_connected = any(isinstance(link, dict) and link.get("url") == expected["url"] and link.get("connected") for link in links)
    self_id = int(bot.get("self_id") or snap.get("self_id") or 0)
    message = ""
    if str(bot.get("notes") or "") != managed_note(instance):
        message = "qbob 里这个 bot 的归属标记不是本实例，NA 不会改动它"
    elif self_id and str(instance.provider_account_id or "") != str(self_id):
        # 通用「改绑 / 解绑账号」曾经能把账号从 bot 的 self_id 改掉；这里只提示，不抛错
        message = "实例账号与 qbob 里 bot 的 QQ 号不一致，请解绑后重新接入"
    elif not endpoint_ok:
        message = (
            "反向 WS 端点与 NA 期望的不一致（可能在 qbob 管理台被改过），可以「重新接线」"
            if platform_view
            else "反向 WS 端点异常，请联系服务提供方"
        )
    return QbobBotInfo(
        configured=True,
        attached=True,
        found=True,
        appid=appid,
        name=str(bot.get("name") or ""),
        self_id=str(self_id) if self_id else "",
        running=bool(bot.get("running")),
        enabled=bool(bot.get("enabled")),
        qq_connected=bool(snap.get("qq_connected")),
        onebot_connected=onebot_connected,
        endpoint_ok=endpoint_ok,
        markdown_enabled=bool(bot.get("markdown_enabled")),
        report_self_message=bool(bot.get("report_self_message")),
        last_error=str(snap.get("last_error") or ""),
        message=message,
    )


async def _require_owned_bot(row: DBAdapterInstance) -> str:
    """本实例已接入、且 qbob 里那个 bot 带着本实例标记，返回 AppID"""
    require_qbob_instance(row)
    await ensure_deployment_id()
    appid = attached_appid(row)
    if not appid:
        raise ValidationError(reason="本实例尚未接入 qbob bot")
    bot = await _find_bot(appid)
    if bot is None:
        raise ValidationError(reason=f"qbob 里找不到 bot {appid}，请解绑后重新接入")
    if str(bot.get("notes") or "") != managed_note(row):
        raise ConflictError(resource=f"qbob 里的 bot {appid}（归属标记不是本实例）")
    self_id = int(bot.get("self_id") or 0)
    if self_id and str(row.provider_account_id or "") != str(self_id):
        # 账号被通用入口改掉 / 清空后，bot 重连会落到未认领实例；重载与开关先拒绝
        # ConflictError 的文案是「{resource}已存在」，所以按「主体（说明）」的写法
        raise ConflictError(resource=f"与实例账号不一致的 qbob bot {appid}（QQ 号 {self_id}，请解绑后重新接入）")
    return appid


async def update_bot_options(
    instance: DBAdapterInstance, body: QbobBotOptionsRequest, *, platform_view: bool = True
) -> QbobBotInfo:
    patch = {key: value for key, value in body.model_dump().items() if value is not None}
    if not patch:
        return await get_bot_info(instance, platform_view=platform_view)
    try:
        async with _lock_for(instance):
            row = await _fresh(instance)
            appid = await _require_owned_bot(row)
            await get_client().request("PUT", f"/api/bots/{appid}", json_body=patch, timeout=SLOW_REQUEST_TIMEOUT_SECONDS)
            _sync_back(instance, row)
    except QbobError as e:
        raise _as_app_error(e, "修改 qbob bot 设置", platform_view=platform_view) from e
    except (ValidationError, ConflictError) as e:
        if platform_view:
            raise
        raise _as_app_error(e, "修改 qbob bot 设置", platform_view=False) from e
    logger.info(f"实例 {instance.instance_key} 修改了 qbob bot 设置: {sorted(patch)}")
    return await get_bot_info(instance, platform_view=platform_view)


async def reload_bot(instance: DBAdapterInstance, *, platform_view: bool = True) -> None:
    try:
        async with _lock_for(instance):
            row = await _fresh(instance)
            appid = await _require_owned_bot(row)
            await get_client().request("POST", f"/api/bots/{appid}/reload", timeout=SLOW_REQUEST_TIMEOUT_SECONDS)
    except QbobError as e:
        raise _as_app_error(e, "重载 qbob bot", platform_view=platform_view) from e
    except (ValidationError, ConflictError) as e:
        if platform_view:
            raise
        raise _as_app_error(e, "重载 qbob bot", platform_view=False) from e


async def rewire(instance: DBAdapterInstance) -> QbobBotInfo:
    """把 bot 的反向 WS 端点写回 NA 期望的那一条（被人改过、或 NA 主机名 / 令牌变了之后）"""
    try:
        async with _lock_for(instance):
            row = await _fresh(instance)
            appid = await _require_owned_bot(row)
            await get_client().request(
                "PUT",
                f"/api/bots/{appid}",
                json_body={"onebot_endpoints": [expected_endpoint(row)]},
                timeout=SLOW_REQUEST_TIMEOUT_SECONDS,
            )
            _sync_back(instance, row)
    except QbobError as e:
        raise _as_app_error(e, "重新接线 qbob bot") from e
    logger.info(f"实例 {instance.instance_key} 已重新接线 qbob bot")
    return await get_bot_info(instance)


async def detach(instance: DBAdapterInstance) -> None:
    """从 qbob 删除本实例托管的 bot，清掉 NA 侧的接入记录与账号绑定；实例本身保留

    qbob 里那个 bot 还在、却不带本实例标记时**拒绝**：它多半仍配着本实例令牌的端点，
    只清 NA 侧的话实例回到「等待绑账号」，它那枚令牌会被握手放行，bot 下次重连就被登记成未认领实例。
    """
    require_qbob_instance(instance)
    try:
        await ensure_deployment_id()
        async with _lock_for(instance):
            row = await _fresh(instance)
            appid = attached_appid(row)
            if appid:
                bot = await _find_bot(appid)
                if bot is not None and str(bot.get("notes") or "") != managed_note(row):
                    raise ConflictError(resource=f"qbob 里的 bot {appid}（归属标记不是本实例，请先到 qbob 管理台处理它）")
                if bot is not None:
                    await get_client().request("DELETE", f"/api/bots/{appid}")
                    logger.info(f"实例 {row.instance_key} 已从 qbob 删除 bot {appid}")
                await _write_metadata(row, {METADATA_APPID: None})
            if row.provider_account_id:
                await _bind_account(row, "")
            _sync_back(instance, row)
    except QbobError as e:
        raise _as_app_error(e, "解绑 qbob bot") from e
