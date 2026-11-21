import json
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from aiosmtplib import send
from jinja2 import Environment, FileSystemLoader
from nonebot import logger

from nekro_agent.adapters.email.base import EMAIL_PROVIDER_CONFIGS
from nekro_agent.adapters.email.config import EmailAccount, EmailConfig
from nekro_agent.core.config import config
from nekro_agent.core.core_utils import atomic_write_text
from nekro_agent.core.os_env import OsEnv

# 模板里的适配器名与 Bot ID 来自协议端握手，按 HTML 转义后再进正文
env = Environment(
    loader=FileSystemLoader("nekro_agent/services/mail"),
    auto_reload=False,
    enable_async=True,
    autoescape=True,
)

# 连接状态邮件的限频。协议端每次连上、断开都会触发一封；网络抖动，或有人拿着能握手的令牌反复连断，
# 都能把管理员邮箱刷满。同一个 Bot 在窗口内最多发 `STATUS_MAIL_PER_BOT_LIMIT` 封（一次「掉线又重连」
# 是两封，留出两次的余量），所有 Bot 合计不超过 `STATUS_MAIL_TOTAL_LIMIT` 封；超出的只记日志。
# 窗口内的最后一封在正文里写明此后一段时间不再单独发信，免得管理员把它当成最终状态
STATUS_MAIL_WINDOW_SECONDS = 1800.0
STATUS_MAIL_PER_BOT_LIMIT = 4
STATUS_MAIL_TOTAL_LIMIT = 20
# 计数落盘、按墙钟记：进程反复崩溃被拉起时最容易刷信（每次启动各 Bot 一重连就是一轮上线邮件），
# 只记在内存里的话每重启一次额度就满血。记录最多 `STATUS_MAIL_TOTAL_LIMIT` 条，每发一封原子写回一次
STATUS_MAIL_STATE_FILE_NAME = "status_mail_rate.json"
# SMTP 连上就握 TLS 的端口（SMTPS / submissions）
SMTP_IMPLICIT_TLS_PORT = 465
# 窗口内已发的 (适配器, Bot ID, 发送时刻)；None 表示本进程还没读过落盘的记录
_status_mail_log: Optional[List[Tuple[str, str, float]]] = None
# 被限频挡下时，WARNING 每个 Bot 每个窗口一条，其余记 debug（只是日志，不落盘）；
# 记着的 Bot 数有上限，按插入顺序淘汰最早的（dict 保持插入顺序）
_status_mail_suppression_warned: Dict[Tuple[str, str], float] = {}
_STATUS_MAIL_WARN_TRACK_LIMIT = 1024


def _load_email_adapter_config() -> EmailConfig:
    adapter_config_path = Path(OsEnv.DATA_DIR) / "configs" / "email" / "config.yaml"
    return EmailConfig.load_config(file_path=adapter_config_path, auto_register=False)


def _get_provider_config_for_account(account: EmailAccount) -> dict[str, str]:
    if account.EMAIL_ACCOUNT == "自定义":
        return {
            "imap_host": account.CUSTOM_IMAP_HOST,
            "imap_port": str(account.CUSTOM_IMAP_PORT),
            "smtp_host": account.CUSTOM_SMTP_HOST,
            "smtp_port": str(account.CUSTOM_SMTP_PORT),
            "smtp_ssl_port": str(account.CUSTOM_SMTP_SSL_PORT),
            "smtp_use_ssl": str(account.CUSTOM_SMTP_USE_SSL).lower(),
        }
    return EMAIL_PROVIDER_CONFIGS.get(account.EMAIL_ACCOUNT, {})


def _get_runtime_mail_settings() -> dict:
    email_config = _load_email_adapter_config()

    enabled = bool(email_config.STATUS_MAIL_ENABLED or config.MAIL_ENABLED)
    sender_account = next(
        (
            account
            for account in email_config.RECEIVE_ACCOUNTS
            if account.USERNAME == email_config.STATUS_MAIL_SENDER_ACCOUNT and account.SEND_ENABLED
        ),
        None,
    )
    username = sender_account.USERNAME if sender_account else config.MAIL_USERNAME
    password = sender_account.PASSWORD if sender_account else config.MAIL_PASSWORD
    targets = (
        [item.EMAIL for item in email_config.STATUS_MAIL_TARGETS if item.EMAIL.strip()]
        if email_config.STATUS_MAIL_TARGETS
        else config.MAIL_TARGET
    )
    tenant_host = False
    validate_certs = True
    if sender_account:
        provider_config = _get_provider_config_for_account(sender_account)
        hostname = str(provider_config.get("smtp_host") or "")
        smtp_port = int(provider_config.get("smtp_port", 587))
        smtp_ssl_port = int(provider_config.get("smtp_ssl_port", smtp_port))
        use_tls = str(provider_config.get("smtp_use_ssl", False)).lower() == "true"
        port = smtp_ssl_port if use_tls else smtp_port
        starttls = not use_tls
        # 从实例转回平台方、主机没被平台方改过的账户：SMTP 主机的 DNS 可能还在租户手里，而这里经 aiosmtplib
        # 直接按主机名连，没有「只连校验过的地址」那一层。状态邮件是平台方的运维通知，不值得为它再写一套直连，
        # 遇到这种发件邮箱就不发（见 `send_email`）
        tenant_host = sender_account.is_tenant_host(hostname)
        # 与 IMAP / SMTP 客户端同一判据：租户的账户、预设服务商一律校验；只有平台方自己的自定义服务器
        # 打开了 `SKIP_TLS_VERIFY` 才不校验
        validate_certs = sender_account.tls_verify_required(hostname)
    else:
        hostname = config.MAIL_HOSTNAME
        port = int(config.MAIL_PORT)
        # 已弃用的核心配置 `MAIL_*`：465 是连上就握 TLS 的端口（原来一律当明文连、再看 `MAIL_STARTTLS`，
        # 在 465 上本来就连不通）；其余端口按 `MAIL_STARTTLS`
        use_tls = port == SMTP_IMPLICIT_TLS_PORT
        starttls = not use_tls and bool(config.MAIL_STARTTLS)

    return {
        "tenant_host": tenant_host,
        "enabled": enabled,
        "username": username,
        "password": password,
        "targets": targets,
        "hostname": hostname,
        "port": port,
        "use_tls": use_tls,
        "starttls": starttls,
        "validate_certs": validate_certs,
        # 既不是 465、也不 STARTTLS，又要登录：账号和授权码会明文过网（`send_email` 遇到就不发）。
        # 不登录的明文中继（例如本机的邮件服务，账号口令都空）不在此列，那时没有秘密可漏
        "plaintext_login": not use_tls and not starttls and bool(username or password),
    }

async def send_email(message: EmailMessage) -> None:
    """
    发送邮件

    Args:
        message: 邮件消息对象

    这是设置邮件内容的方法
    message.set_content("这是邮件的正文内容", subtype='plain')

    这是HTML格式的邮件内容
    message.add_alternative("<html><body><h1>这是HTML格式的邮件内容</h1></body></html>", subtype='html')

    添加附件
    with open('path/to/file', 'rb') as f:
        message.add_attachment(f.read(), maintype='application', subtype='octet-stream', filename='file.txt')
    
    """
    settings = _get_runtime_mail_settings()
    if settings.get("tenant_host"):
        logger.warning(
            "状态邮件没有发出：选作发件人的邮箱用的是租户填的 SMTP 服务器（从实例转回平台方后还没改过）。"
            "请在 Email 适配器页换一个发件邮箱，或把它的服务器改成平台方自己的",
        )
        return
    if settings.get("plaintext_login"):
        logger.error(
            "状态邮件没有发出：旧的「邮件通知」配置（MAIL_*）既没开 TLS 加密、端口也不是 465，"
            "登录时账号和授权码会明文发出去。请打开「邮件通知启用 TLS 加密」，或在 Email 适配器页选一个发件邮箱",
        )
        return
    try:
        await send(
            message,
            hostname=settings["hostname"],
            port=settings["port"],
            username=settings["username"],
            password=settings["password"],
            use_tls=settings["use_tls"],
            start_tls=settings["starttls"],
            validate_certs=settings["validate_certs"],
        )
        logger.info("邮件发送成功")
    except Exception as e:
        logger.error(f"邮件发送失败: {e!s}")


def _status_mail_state_file() -> Path:
    return Path(OsEnv.DATA_DIR) / "system" / STATUS_MAIL_STATE_FILE_NAME


def _load_status_mail_log() -> List[Tuple[str, str, float]]:
    """读出落盘的发信记录；没有文件、读不出、格式不对时从空记录起步，下一次发信写回一份完整的"""
    path = _status_mail_state_file()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw.get("sent") if isinstance(raw, dict) else None
        records: List[Tuple[str, str, float]] = []
        for item in items if isinstance(items, list) else []:
            if (
                isinstance(item, list)
                and len(item) == 3
                and isinstance(item[0], str)
                and isinstance(item[1], str)
                and isinstance(item[2], (int, float))
            ):
                records.append((item[0], item[1], float(item[2])))
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001 - 坏文件（含深层嵌套、超大整数）不能让之后每封状态邮件都抛异常
        logger.warning(f"连接状态邮件的限频记录读不出来，从空记录起步: {path}")
        return []
    return records


def _save_status_mail_log(records: List[Tuple[str, str, float]]) -> None:
    """写不进去只记一条 WARNING：这一封照发，计数留在内存里（`data/system` 本来就要求可写）

    `json.dumps` 保持默认的 `ensure_ascii`：Bot ID 里的孤立代理字符转义成 `\\uXXXX`，写盘不会编码失败。
    """
    path = _status_mail_state_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({"sent": [list(item) for item in records]}))
    except Exception:  # noqa: BLE001 - 同上：写盘出任何错都不能拖垮发信
        logger.warning(f"连接状态邮件的限频记录写不进去，本次计数只留在内存里，进程重启后不算: {path}")


def _reserve_status_mail(adapter: str, bot_id: str) -> Optional[str]:
    """按限频占一个发信名额；占不到返回 None，占到了返回要写进正文的提示（不是窗口内最后一封时为空串）

    判断、记账、写盘之间没有 await：并发的连接事件不会一起越过上限。
    """
    global _status_mail_log

    now = time.time()
    if _status_mail_log is None:
        _status_mail_log = _load_status_mail_log()
    # 窗口外的丢掉；时间戳远在未来的（时钟往回拨过、文件被改过）也丢掉，免得额度被永远占着
    _status_mail_log = [
        item for item in _status_mail_log if now - STATUS_MAIL_WINDOW_SECONDS < item[2] <= now + STATUS_MAIL_WINDOW_SECONDS
    ]
    for key, warned_at in list(_status_mail_suppression_warned.items()):
        if warned_at <= now - STATUS_MAIL_WINDOW_SECONDS:
            del _status_mail_suppression_warned[key]

    bot_count = sum(1 for item in _status_mail_log if item[0] == adapter and item[1] == bot_id)
    if bot_count >= STATUS_MAIL_PER_BOT_LIMIT or len(_status_mail_log) >= STATUS_MAIL_TOTAL_LIMIT:
        return None
    _status_mail_log.append((adapter, bot_id, now))
    _save_status_mail_log(_status_mail_log)

    window_minutes = int(STATUS_MAIL_WINDOW_SECONDS // 60)
    if len(_status_mail_log) >= STATUS_MAIL_TOTAL_LIMIT:
        return f"连接状态邮件 {window_minutes} 分钟内已发满 {STATUS_MAIL_TOTAL_LIMIT} 封，之后一段时间所有 Bot 的上下线都不再单独发信，请到总后台查看实时状态。"
    if bot_count + 1 >= STATUS_MAIL_PER_BOT_LIMIT:
        return f"这个 Bot {window_minutes} 分钟内已发满 {STATUS_MAIL_PER_BOT_LIMIT} 封状态邮件，之后一段时间它的上下线不再单独发信，请到总后台查看实时状态。"
    return ""


def _note_status_mail_suppressed(adapter: str, bot_id: str, is_online: bool) -> None:
    key = (adapter, bot_id)
    state = "上线" if is_online else "下线"
    if key in _status_mail_suppression_warned:
        logger.debug(f"Bot {bot_id}（{adapter}）{state}，连接状态邮件已达限频上限，本次不发")
        return
    _status_mail_suppression_warned[key] = time.time()
    while len(_status_mail_suppression_warned) > _STATUS_MAIL_WARN_TRACK_LIMIT:
        _status_mail_suppression_warned.pop(next(iter(_status_mail_suppression_warned)))
    logger.warning(
        f"Bot {bot_id}（{adapter}）{state}，连接状态邮件已达限频上限，本次不发"
        f"（{int(STATUS_MAIL_WINDOW_SECONDS // 60)} 分钟内的同类情况只记 debug）",
    )


async def send_bot_status_email(adapter: str, bot_id: str, is_online: bool) -> None:
    """
    发送机器人状态邮件通知（限频见 `STATUS_MAIL_WINDOW_SECONDS`）

    Args:
        adapter: 适配器名称
        bot_id: 机器人ID
        is_online: 是否在线
    """
    settings = _get_runtime_mail_settings()
    if not settings["enabled"]:
        return

    quota_notice = _reserve_status_mail(adapter, bot_id)
    if quota_notice is None:
        _note_status_mail_suppressed(adapter, bot_id, is_online)
        return

    message = EmailMessage()
    message["From"] = settings["username"]
    message["To"] = ", ".join(settings["targets"]) if settings["targets"] else settings["username"]

    # 加载 Jinja2 模板
    template = env.get_template("connection.jinja2")

    # 渲染模板。协议端上报的 ID 里若有孤立代理字符，`set_content` 编码正文时会抛错：显示用的文字先换成替换字符
    html_content = await template.render_async(
        is_online=is_online,
        adapter=adapter.encode("utf-8", "replace").decode("utf-8"),
        bot_id=bot_id.encode("utf-8", "replace").decode("utf-8"),
        quota_notice=quota_notice,
    )

    # 设置 HTML 内容
    message.set_content(html_content, subtype="html")

    await send_email(message)
