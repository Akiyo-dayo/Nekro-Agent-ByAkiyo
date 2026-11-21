"""邮箱账户的归属、可见性与发信记录

## 两处账户合成一份

平台方账户仍在 email 适配器的配置 `RECEIVE_ACCOUNTS` 里：OAuth、运行状态邮件、适配器账户页
那一整条链都按它的既有形态工作，搬进表里等于同时重写那一整条链。实例自己填的账户在
`email_account` 表里（归属 `(adapter_key, instance_key)`）。本模块把两处合成**运行时的一份**
（`all_runtime_accounts()`，给发信客户端用）与**对某个会话的一份**（`session_accounts()`，
给插件与后台接口用）：前者是连接池的视图，后者带「这次会话能读什么、能发什么」。

## 会话能读什么、能发什么

- **租户实例**：能读的只有本实例自己的账户（`enabled`）；能发的除了自己的（还要求
  `send_enabled`），再加上**共享给这个实例**的平台方账户。平台方账户永远进不了租户的
  「可读」：它的收件箱里有平台方的信，而租户没有那条授权。
- **平台方实例**：能读能发的都是「本实例自有 + 平台方账户」，平台方账户的共享设置对它无效——
  共享设置管的是「哪些租户实例能用平台方的东西」，平台方实例本来就是平台方自己的。
- **解析不出归属**：多租户部署里 fail-closed（两个列表都空），单租户部署按平台方对待，
  与改造前的行为逐字一致。

## 平台方账户对租户：只给显示名与不透明引用

租户会话（以及租户管理员的后台页面）看平台方账户时拿不到地址、服务商、服务器与凭据，
只拿一个 `ref`（`platform:` + 地址 sha256 前 10 位）与「平台方提供：<显示名>」。用不透明引用
而不是地址，是因为 `send_email` 的 `from_account` 是这个字符串：它必须**可被模型填写**，
又不能因此把地址带出去。哈希只取前 10 位、不加盐，本来就只用来区分账户，不承担保密。

## 越权为什么按「不存在」

越权与真的不存在如果给出不同的文案，租户就能用「换一个账户名试试」把平台方或别的实例的
账户清单枚举出来。所以 `resolve_readable` / `resolve_sendable` 对「不在这次会话的集合里」
一律抛与插件改造前逐字相同的那两句（`ACCOUNT_NOT_FOUND` / `NO_SENDER`），日志另记 WARNING。

## 地址为什么全局唯一

同一个地址同时出现在两处会让附件目录、客户端池的键（都是地址）与 IMAP 轮询都分不清是谁的：
两处各轮询一次就是重复入库，附件也可能写进同一个目录。所以保存前两边都查，
冲突时统一回 `ADDRESS_UNAVAILABLE`——**不说明被谁占用**，否则这就成了「查某个地址在不在
平台方手里」的探针。

## 这里的边界

本模块是核心零件，供插件（`email_utils`、`festival_greeting`）与路由调用，**不 import 任何
`plugins.*`**：核心被插件拖住就再也拆不开了。取会话身份也只经 `services/resource_scope`，
不读 `ctx` 上的频道字段——沙盒方法的参数里也有叫 `chat_key` 的东西，在服务层读属性等于给
模型传入的值留了一条能长得一模一样的路。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple

from tortoise.expressions import Q
from tortoise.timezone import now as db_now

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_email_account import DBEmailAccount
from nekro_agent.models.db_email_send_log import DBEmailSendLog
from nekro_agent.tools.fs_links import is_link, unlink_link

if TYPE_CHECKING:  # pragma: no cover - 只在类型检查时用，避免运行时互相拖入
    from tortoise.queryset import QuerySet

    from nekro_agent.adapters.email.config import EmailAccount

logger = get_sub_logger("email_accounts")

# ===== 账户类型 =====
KIND_PLATFORM = "platform"
KIND_INSTANCE = "instance"

# ===== 发信用途 =====
PURPOSE_SEND_EMAIL = "send_email"
PURPOSE_FESTIVAL = "festival"

# ===== 面向模型 / 用户的文案 =====
# 与 `get_email_content` 现有那句逐字相同：越权与真的不存在必须不可区分
ACCOUNT_NOT_FOUND = "账户 {account} 未连接或不存在"
# 与 `send_email` 现有那句逐字相同
NO_SENDER = "没有找到可用的发件人账户"
# 地址冲突的统一文案：不说被谁占用
ADDRESS_UNAVAILABLE = "这个邮箱地址不能使用"
# 自定义主机不允许时的文案（内网、回环、解析失败、端口不在白名单）
CUSTOM_HOST_UNAVAILABLE = "这个邮箱服务器地址不能使用"
# 定不出本轮发言人时给模型的提示：请用户单独再说一次
SPEAKER_REQUIRED = "这一轮不止一个人在说话，请你单独再说一次"
# 会话解析不出实例段时给模型的提示（没有「这个会话」就没有可存的偏好）
PREF_NOT_AVAILABLE = "这个会话不能设置邮件偏好"
# 地址格式不合格时的文案
INVALID_MAIL_ADDRESS = "邮箱地址格式不对"

# 发信记录保留 365 天，进程内每 24 小时最多清一次（与运维审计同一做法）
SEND_LOG_KEEP_DAYS = 365
SEND_LOG_PURGE_INTERVAL_SECONDS = 86400.0

# 自定义服务器只许这几个端口：IMAP 993 / 143，SMTP 465 / 587 / 25
CUSTOM_MAIL_PORTS = frozenset({993, 143, 465, 587, 25})

# 平台方账户的 `ref` 前缀与哈希长度；实例自有的 `ref` 用表 ID
PLATFORM_REF_PREFIX = "platform:"
PLATFORM_REF_HASH_CHARS = 10

# 默认发件人的进程内缓存：同一个实例对、同一个平台方 / 租户身份，5 秒内不重复查库。
# TTL 只影响「换了默认发件人之后多久生效」，5 秒足够短。
_DEFAULT_SENDER_CACHE_TTL_SECONDS = 5.0
_default_sender_cache: Dict[Tuple[str, str, bool], Tuple[float, Optional["AccountView"]]] = {}

# 发信记录清理用的单调时钟与写库用的墙钟；测试直接替换模块属性
_clock: Callable[[], float] = time.monotonic
_wall_clock: Callable[[], datetime] = db_now
_last_purge_at: Optional[float] = None


@dataclass(frozen=True)
class AccountView:
    """一次会话看到的一个账户

    Attributes:
        kind: `platform` / `instance`
        ref: 不透明引用。平台方 `platform:` + 地址 sha256 前 10 个十六进制；实例自有 `instance:<表 id>`。
            模型把它填进 `from_account` / `account` 这类参数，所以它必须稳定且不含地址
        display_label: 给人看的名字。平台方对租户会话是「平台方提供：<显示名>」；其余是显示名，空则地址
        username: 地址（小写）。只给有权看到地址的调用方用；插件给租户回显前必须换成 `display_label`
        send_enabled: 这个账户本身是否开着发信
        readable: 这次会话能不能读它的收件箱
        is_default_sender: 对这次会话而言是不是默认发件人
        owner_adapter_key: 归属适配器；平台方为空串
        owner_instance_key: 归属实例；平台方为空串
        instance_account_id: 实例自有账户的表 id；平台方为 None
        provider: 服务商预设键。平台方账户上也照填（平台方会话要看它），但插件给租户会话
            回显平台方账户时不许带出去
    """

    kind: str
    ref: str
    display_label: str
    username: str
    send_enabled: bool
    readable: bool
    is_default_sender: bool
    owner_adapter_key: str = ""
    owner_instance_key: str = ""
    instance_account_id: Optional[int] = None
    provider: str = ""


@dataclass(frozen=True)
class SessionSpeaker:
    """这一轮唯一的那个人类发言人，以及他说话的那个会话

    用户邮件偏好的三个方法都**没有用户 ID 参数**：身份只能从宿主记下的「本轮发言人」
    （`current_round_speaker`）取，会话只能是 `resolve_session_owner` 解析出来的那一个。
    模型填的任何用户 ID 都不认——否则群友能让 Bot 把别人的祝福改投到自己的邮箱、
    或者替别人退订。

    Attributes:
        adapter_key: 会话所属适配器（来自频道行，不是模型传入的参数）
        instance_key: 会话所属实例；为空表示频道没有实例段，此时偏好无处可存
        chat_key: `resolve_session_owner` 用的那个频道标识（`from_chat_key`），不是模型给的
        platform_userid: 本轮唯一的人类触发者账号
        is_private: 这个会话是不是私聊（看频道行的 `channel_type`）；私聊才回显完整地址
    """

    adapter_key: str
    instance_key: str
    chat_key: str
    platform_userid: str
    is_private: bool


@dataclass(frozen=True)
class SessionAccounts:
    """一次会话（或一个实例）能读、能发的账户

    Attributes:
        resolved: 归属解析出来了没有
        adapter_key: 实例对里的适配器标识；解析不出时为空串
        instance_key: 实例对里的实例标识；解析不出或频道没有实例段时为空串
        is_platform_instance: 是否按平台方实例对待（含单租户部署里解析不出的情况）
        readable: 能读的账户（能看内容）
        sendable: 能发的账户（可能只可消耗、看不到配置，例如共享给租户的平台方账户）
    """

    resolved: bool
    adapter_key: str
    instance_key: str
    is_platform_instance: bool
    readable: List[AccountView] = field(default_factory=list)
    sendable: List[AccountView] = field(default_factory=list)


# ===== 基础的规范化与引用 =====


def instance_accounts_enabled() -> bool:
    """实例自有邮箱的总闸（平台方专属配置，默认开）

    判据只写在这一处：收信轮询、运行时账户表、会话账户集合、后台接口都问它，不各自去读
    配置项。关掉时「没有实例自有账户」是**读与发**两头都不给——只关收信不关发信会留下
    「账户列表里没有它、但模型能拿它发信」这种说不清的中间态。
    """
    from nekro_agent.core.config import config as core_config

    try:
        return bool(getattr(core_config, "EMAIL_INSTANCE_ACCOUNTS_ENABLED", True))
    except Exception:  # pragma: no cover - 配置对象异常时按「开着」不改变既有行为
        return True


@dataclass(frozen=True)
class InstanceMailOwner:
    """适配器里「这个地址是谁的」那一份记录

    收信时先按地址查它决定走哪条路：平台方账户不在表里（`None`），实例自有账户在表里。
    只放收信要用到的几项，不放授权码——适配器的连接池里已经有客户端了，这份记录只回答归属。

    Attributes:
        account_id: `email_account` 表 id（附件目录、`notify_unavailable` 都按它回写）
        adapter_key: 归属适配器的标识
        instance_key: 归属实例的标识
        attachment_dir: 附件根目录名（新建的是 `_i<id>`，由平台方转来的沿用原地址）
        notify_chat_key: 新邮件通知要推到的频道；空表示不推
    """

    account_id: int
    adapter_key: str
    instance_key: str
    attachment_dir: str
    notify_chat_key: str = ""


def instance_owner_of(row: DBEmailAccount) -> InstanceMailOwner:
    """把一个账户行翻成适配器要记的那份归属"""
    return InstanceMailOwner(
        account_id=int(row.id),
        adapter_key=row.owner_adapter_key or "",
        instance_key=row.owner_instance_key or "",
        attachment_dir=str(row.attachment_dir or "").strip(),
        notify_chat_key=str(row.notify_chat_key or "").strip(),
    )


def normalize_username(username: Any) -> str:
    """地址的规范形式：去首尾空白、转小写

    比较与入库都走这一处：邮件服务器的用户名不区分大小写，两处写法不一致会让「这个地址
    能不能用」的判定与运行时客户端池的键（也是地址）对不上。
    """
    return str(username or "").strip().lower()


# 账户引用的 HMAC 用途串与密钥文件名。密钥**不从 `JWT_SECRET_KEY` 派生**：那一把要求能随时轮换、没配时
# 每进程随机，而引用存进了节日邮件队列与发信记录、也被模型记在上下文里——密钥一换，排队的平台方节日邮件
# 会被整批判成「发件账户已不可用」。所以单独一把、放数据目录、不轮换（见 `tools/local_secret.py`）
_ACCOUNT_REF_KEY_PURPOSE = b"nekro-agent/email-account-ref/v1"
ACCOUNT_REF_KEY_FILE = "email_account_ref.key"
_account_ref_key: Optional[bytes] = None


def reset_account_ref_key_cache() -> None:
    """丢掉进程内缓存的引用密钥（测试改了数据目录之后调）"""
    global _account_ref_key  # noqa: PLW0603
    _account_ref_key = None


def _account_ref_hmac_key() -> bytes:
    """引用摘要用的 HMAC 密钥：数据目录里那把专用密钥文件派生；文件读不了也建不了时退回 `JWT_SECRET_KEY`

    退回的那一档会在密钥轮换 / 重启后变（没配 `JWT_SECRET_KEY` 时每进程随机），所以打 ERROR；
    不拒绝工作——引用变了的后果是排队的平台方节日邮件被跳过，不是数据丢失。
    """
    global _account_ref_key  # noqa: PLW0603
    if _account_ref_key is not None:
        return _account_ref_key
    from nekro_agent.tools.local_secret import load_or_create_local_secret

    material = load_or_create_local_secret(ACCOUNT_REF_KEY_FILE, purpose="邮箱账户引用")
    if material is None:
        from nekro_agent.core.os_env import OsEnv

        logger.error(
            "邮箱账户引用的密钥文件读不了也建不了，本进程退回用 JWT_SECRET_KEY 派生："
            "重启或轮换 JWT 密钥后，排队中的平台方节日邮件会被判成「发件账户已不可用」。请让数据目录可写",
        )
        material = str(OsEnv.JWT_SECRET_KEY)
    _account_ref_key = hmac.new(material.encode("utf-8"), _ACCOUNT_REF_KEY_PURPOSE, hashlib.sha256).digest()
    return _account_ref_key


def _keyed_address_digest(username: str) -> str:
    """地址的带密钥摘要（十六进制）

    不加盐的 sha256 截几位，对 QQ 号这种取值空间很小的地址几秒钟就能穷举回去——租户看到
    `platform:xxxx` 就等于看到了地址。改成用服务端密钥做 HMAC：没有密钥就算不出来。
    （用平台方邮箱发出去的信，`From:` 头里本来就能看到地址；这里防的是不发信也能读出来。）
    密钥是本部署专用、不轮换的那一把（`_account_ref_hmac_key`），所以引用跨重启稳定。
    """
    return hmac.new(_account_ref_hmac_key(), normalize_username(username).encode("utf-8"), hashlib.sha256).hexdigest()


def platform_account_ref(username: str) -> str:
    """平台方账户的不透明引用：`platform:` + 地址带密钥摘要的前 10 个十六进制

    稳定（同一个地址、同一把服务端密钥永远同一个值）且推不回地址。用途是让模型能点名一个账户
    而不必知道它的地址。
    """
    return f"{PLATFORM_REF_PREFIX}{_keyed_address_digest(username)[:PLATFORM_REF_HASH_CHARS]}"


def instance_account_ref(account_id: int) -> str:
    """实例自有账户的引用：`instance:<表 id>`"""
    return f"{KIND_INSTANCE}:{int(account_id)}"


def legacy_send_log_refs(account: "AccountView") -> Tuple[str, ...]:
    """平台方账户在旧版本里的发信记录 / 队列引用（密钥从 `JWT_SECRET_KEY` 派生的那一版）

    升级之前已经排进节日邮件队列的平台方邮件、写下的发信记录，存的是这一版；比对与显示名翻译时
    同时认它，免得升级后整批被判成「发件账户已不可用」。只有 `JWT_SECRET_KEY` 是配好的固定值时
    算得出同一个值（没配时每进程随机，那时旧引用本来就在下一次重启后失效）。实例自有账户的引用是表 id，
    没有旧版。
    """
    if account.kind != KIND_PLATFORM:
        return ()
    from nekro_agent.core.os_env import OsEnv

    key = hmac.new(str(OsEnv.JWT_SECRET_KEY).encode("utf-8"), _ACCOUNT_REF_KEY_PURPOSE, hashlib.sha256).digest()
    return (hmac.new(key, normalize_username(account.username).encode("utf-8"), hashlib.sha256).hexdigest()[:16],)


def send_log_ref_matches(account: "AccountView", stored_ref: str) -> bool:
    """库里存的引用是不是这个账户的（认当前版与旧版）"""
    return stored_ref == account_send_log_ref(account) or stored_ref in legacy_send_log_refs(account)


def account_send_log_ref(account: "AccountView | DBEmailAccount") -> str:
    """发信记录里的账户引用：平台方存地址带密钥摘要的前 16 位（推不回地址），实例自有存表 id"""
    if isinstance(account, DBEmailAccount):
        return str(int(account.id))
    if account.kind == KIND_PLATFORM:
        return _keyed_address_digest(account.username)[:16]
    if account.instance_account_id is not None:
        return str(int(account.instance_account_id))
    return str(account.ref)


def display_label_of(*, display_name: str, username: str, provider: str = "") -> str:
    """显示名：填了就用，没填用地址，再没有用服务商名"""
    return (display_name or "").strip() or normalize_username(username) or provider


def build_account_view(
    *,
    kind: str,
    username: str,
    display_name: str,
    send_enabled: bool,
    readable: bool,
    provider: str = "",
    owner_adapter_key: str = "",
    owner_instance_key: str = "",
    instance_account_id: Optional[int] = None,
) -> AccountView:
    """按运行时账户的属性造一个 `AccountView`（ref 按类型算，不吃外部传入的值）"""
    if kind == KIND_PLATFORM:
        ref = platform_account_ref(username)
    else:
        ref = instance_account_ref(instance_account_id or 0)
    return AccountView(
        kind=kind,
        ref=ref,
        display_label=display_label_of(display_name=display_name, username=username, provider=provider),
        username=normalize_username(username),
        send_enabled=bool(send_enabled),
        readable=bool(readable),
        is_default_sender=False,
        owner_adapter_key=owner_adapter_key if kind == KIND_INSTANCE else "",
        owner_instance_key=owner_instance_key if kind == KIND_INSTANCE else "",
        instance_account_id=instance_account_id if kind == KIND_INSTANCE else None,
        provider=(provider or ""),
    )


def visible_label_for(view: AccountView, *, tenant_viewer: bool) -> str:
    """对外显示的名字

    租户看到的平台方账户只给「平台方提供：<显示名>」。显示名本身可能被管理员填成地址
    （见过直接填邮箱地址的），所以含 `@` 时整个换掉：宁可少给一个名字，也不能漏出地址。
    """
    if not tenant_viewer or view.kind != KIND_PLATFORM:
        return view.display_label
    name = (view.display_label or "").strip()
    if not name or "@" in name:
        name = "邮箱"
    return f"平台方提供：{name}"


def mask_email_address(address: str) -> str:
    """地址遮掩成 `a***@qq.com` 的形式

    后台的用户邮件偏好页与插件回显共用这一处：只露出本地部分的首字符与域名。
    认不出 `@` 的原样返回（它不是地址，遮掩只会让人更看不懂）。
    """
    text = str(address or "").strip()
    if "@" not in text:
        return text
    local, _, domain = text.partition("@")
    if not local:
        return f"***@{domain}"
    return f"{local[0]}***@{domain}"


def platform_account_shared_with(account: "EmailAccount", adapter_key: str, instance_key: str) -> bool:
    """这个平台方账户有没有共享给某个租户实例发信

    三种取值：`all_instances`（默认，所有实例）、`listed`（只给 `SEND_SHARE_INSTANCES` 里
    点名的实例）、`none`（谁都不给）。**只管租户实例**：平台方实例不受它限制，
    调用方别把它用在平台方实例上（见 `instance_accounts` 的分支）。

    认不出的取值按 `none` 处理：配置里手改出一个拼错的字符串时，宁可少给一个授权。
    """
    share = str(getattr(account, "SEND_SHARE", "all_instances") or "").strip()
    if not share:
        # 旧配置文件里没有这一项时 pydantic 会给默认值，走到这里说明是构造出来的对象
        share = "all_instances"
    if share == "all_instances":
        return True
    if share != "listed":
        return False
    target = f"{adapter_key}:{instance_key}"
    entries = getattr(account, "SEND_SHARE_INSTANCES", None) or []
    return target in {str(item).strip() for item in entries if str(item).strip()}


def rewrite_platform_share_instances(source_ref: str, target_ref: Optional[str]) -> int:
    """改写平台方邮箱 `SEND_SHARE_INSTANCES` 里点名某个实例的那一项，返回改动的账户数

    `target_ref` 为 None 是「摘掉」（彻底删除实例、划转），否则是「改名」（实例归属迁移）。
    彻底删除、划转、迁移三处都走这里，判据与写回只有一份。

    email 适配器没加载时也要改：配置对象从 `load_adapter_config` 取（有缓存，与适配器用的是同一个
    对象），改完 `dump_config()` 写回文件。只在适配器加载时才改的话，配置文件里会留着一个已经
    不存在的实例名，同名实例重建（实例标识可以被重新认领）后就自动继承那份发信授权。
    """
    config = _load_platform_config(None)
    changed = 0
    # 走 `platform_accounts`：它返回的是浅拷贝的列表，元素还是同一批账户对象，
    # 改它们的 `SEND_SHARE_INSTANCES` 照样落到配置上；不在这里第二次读 `RECEIVE_ACCOUNTS`
    for account in platform_accounts(config):
        entries = [str(item).strip() for item in (getattr(account, "SEND_SHARE_INSTANCES", None) or [])]
        if source_ref not in entries:
            continue
        rewritten: List[str] = []
        for item in entries:
            replacement = target_ref if item == source_ref else item
            if replacement and replacement not in rewritten:
                rewritten.append(replacement)
        account.SEND_SHARE_INSTANCES = rewritten
        changed += 1
    if changed:
        clear_default_sender_cache()
        try:
            config.dump_config()
        except Exception as e:  # noqa: BLE001 - 内存里的值已经改了，写不回文件只记警告
            logger.warning(f"保存平台方邮箱的共享名单失败: {type(e).__name__}")
    return changed


# ===== 平台方账户的来源 =====


def _load_platform_config(adapter: Any = None) -> Any:
    """取 email 适配器的配置对象（平台方账户就在它上面）

    优先用传进来的适配器的配置；没有适配器（未启用、测试里没起）时退回
    `load_adapter_config("email")`：账户配置页在适配器没跑起来时也是可用的，
    服务层不该因为「适配器没启动」就看不见平台方账户。
    """
    config = getattr(adapter, "config", None) if adapter is not None else None
    if config is not None:
        return config
    from nekro_agent.adapters import load_adapter_config, loaded_adapters

    loaded = loaded_adapters.get("email")
    loaded_config = getattr(loaded, "config", None) if loaded is not None else None
    if loaded_config is not None:
        return loaded_config
    return load_adapter_config("email")


def platform_accounts(adapter: Any = None) -> List["EmailAccount"]:
    """平台方账户的运行时列表（`RECEIVE_ACCOUNTS` 的浅拷贝：调用方不该借它改配置）"""
    config = _load_platform_config(adapter)
    return list(getattr(config, "RECEIVE_ACCOUNTS", []) or [])


def _platform_view(account: "EmailAccount", *, readable: bool) -> AccountView:
    return build_account_view(
        kind=KIND_PLATFORM,
        username=account.USERNAME,
        display_name=account.DISPLAY_NAME,
        provider=account.EMAIL_ACCOUNT,
        send_enabled=bool(account.SEND_ENABLED),
        readable=readable,
    )


def _instance_view(row: DBEmailAccount, *, readable: bool = True) -> AccountView:
    # 实例自有账户一律读得到（会话读它的缓存行）；收信本身在后续轮次接上
    return build_account_view(
        kind=KIND_INSTANCE,
        username=row.username,
        display_name=row.display_name,
        provider=row.provider,
        send_enabled=bool(row.send_enabled),
        readable=readable,
        owner_adapter_key=row.owner_adapter_key,
        owner_instance_key=row.owner_instance_key,
        instance_account_id=int(row.id),
    )


def _send_eligible(account: "EmailAccount") -> bool:
    """平台方账户能不能用来发信：`ENABLED` 且 `SEND_ENABLED`"""
    return bool(account.ENABLED and account.SEND_ENABLED)


def _with_default(views: List[AccountView], default_ref: Optional[str]) -> List[AccountView]:
    """按 `default_ref` 给一组视图标上 `is_default_sender`"""
    if not default_ref:
        return list(views)
    return [
        AccountView(**{**view.__dict__, "is_default_sender": view.ref == default_ref})
        for view in views
    ]


def _pick_default_ref(
    instance_sendable: Optional[Sequence[AccountView]],
    platform_sendable: Sequence[AccountView],
    platform_accounts_list: Sequence["EmailAccount"],
) -> Optional[str]:
    """算出默认发件人的 `ref`

    规则（会话与节日邮件共用）：
    1. 本实例有发信开着的自有账户 → 用它：先看 `is_default_sender` 标记的，其次按表 id；
    2. 没有 → 平台方里共享给它的账户：先看 `IS_DEFAULT_SENDER` 标记的，其次按
       `RECEIVE_ACCOUNTS` 里的顺序——与改造前 `send_email` 的选法逐字相同。
    """
    if instance_sendable:
        for view in instance_sendable:
            if view.is_default_sender:
                return view.ref
        return instance_sendable[0].ref
    if platform_sendable:
        flagged = [account for account in platform_accounts_list if account.ENABLED and account.SEND_ENABLED and account.IS_DEFAULT_SENDER]
        if flagged:
            return platform_account_ref(flagged[0].USERNAME)
        return platform_sendable[0].ref
    return None


# ===== 会话 / 实例的账户集合 =====


async def _instance_rows(adapter_key: str, instance_key: str) -> List[DBEmailAccount]:
    if not adapter_key or not instance_key:
        return []
    return list(
        await DBEmailAccount.filter(
            owner_adapter_key=adapter_key,
            owner_instance_key=instance_key,
        ).order_by("id"),
    )


async def instance_accounts(
    adapter_key: str,
    instance_key: str,
    *,
    is_platform_instance: bool,
    adapter: Any = None,
) -> SessionAccounts:
    """按实例对算一份账户集合（不经过会话身份）

    平台方管理员在后台选一个实例看它的邮箱时走这里；会话走 `session_accounts`，
    两者对同一个实例对必须得到同一份结果，所以规则只写在这一处。

    - 实例自有：`enabled` 的进可读；其中 `send_enabled` 的进可发
    - 平台方账户：平台方实例可读；`ENABLED` + `SEND_ENABLED` 且（平台方实例 **或**
      共享给这个实例）的进可发
    """
    rows = await _instance_rows(adapter_key, instance_key)
    # 总闸关掉时表里的行照旧存在（后台列表还要看得到），但这次会话**没有**实例自有账户：
    # 读与发都不给，否则「关掉了」只是不轮询，模型还能拿它发信
    usable_rows = [row for row in rows if row.enabled and instance_accounts_enabled()]
    instance_readable = [_instance_view(row) for row in usable_rows]
    instance_sendable = [_instance_view(row) for row in usable_rows if row.send_enabled]
    # 表上的 `is_default_sender` 是行上的事实，只有它参与默认发件人的选择
    instance_sendable_with_flag = [
        AccountView(**{**view.__dict__, "is_default_sender": bool(row.is_default_sender)})
        for view, row in zip(instance_sendable, [row for row in usable_rows if row.send_enabled])
    ]

    platform_list = platform_accounts(adapter)
    platform_enabled = [account for account in platform_list if account.ENABLED]
    shared_list = [
        account
        for account in platform_enabled
        if is_platform_instance or platform_account_shared_with(account, adapter_key, instance_key)
    ]
    platform_sendable = [
        _platform_view(account, readable=is_platform_instance) for account in shared_list if _send_eligible(account)
    ]

    readable = list(instance_readable)
    if is_platform_instance:
        readable.extend(_platform_view(account, readable=True) for account in platform_enabled)

    default_ref = _pick_default_ref(instance_sendable_with_flag, platform_sendable, shared_list)
    sendable = _with_default([*instance_sendable, *platform_sendable], default_ref)
    # 可读那一份里的同一个账户也标上默认：平台方实例的读侧要能看出谁是默认发件人
    readable = _with_default(readable, default_ref)

    return SessionAccounts(
        resolved=True,
        adapter_key=adapter_key,
        instance_key=instance_key,
        is_platform_instance=is_platform_instance,
        readable=readable,
        sendable=sendable,
    )


async def _platform_only_accounts() -> SessionAccounts:
    """只有平台方账户的那一份（单租户部署解析不出归属、频道没有实例段时用）"""
    platform_list = platform_accounts()
    enabled = [account for account in platform_list if account.ENABLED]
    sendable_views = [_platform_view(account, readable=True) for account in enabled if _send_eligible(account)]
    default_ref = _pick_default_ref(None, sendable_views, enabled)
    return SessionAccounts(
        resolved=False,
        adapter_key="",
        instance_key="",
        is_platform_instance=True,
        readable=_with_default([_platform_view(account, readable=True) for account in enabled], default_ref),
        sendable=_with_default(sendable_views, default_ref),
    )


async def session_accounts(ctx: Any) -> SessionAccounts:
    """这次会话能读 / 能发哪些账户

    身份只经 `resolve_session_owner`：`ctx.from_chat_key` 是 RPC 调用许可绑定的那一个，
    模型改不了。
    """
    from nekro_agent.services.resource_scope import resolve_session_owner

    owner = await resolve_session_owner(ctx)
    return await _accounts_for_owner(owner)


async def chat_accounts(chat_key: str) -> SessionAccounts:
    """按频道标识算一份账户集合（给没有 `ctx` 的调用方：命令、后台任务）"""
    from nekro_agent.services.resource_scope import resolve_chat_owner

    owner = await resolve_chat_owner(chat_key)
    return await _accounts_for_owner(owner)


async def _accounts_for_owner(owner: Any) -> SessionAccounts:
    """把一次归属解析的结果翻成账户集合（`session_accounts` / `chat_accounts` 共用）"""
    from nekro_agent.services.plugin.scope_guard import deployment_has_tenants

    if not owner.resolved:
        # 多租户部署里解析不出一律 fail-closed：没有租户边界可越时按平台方，行为与改造前一致
        if await deployment_has_tenants():
            return SessionAccounts(
                resolved=False,
                adapter_key="",
                instance_key="",
                is_platform_instance=False,
                readable=[],
                sendable=[],
            )
        accounts = await _platform_only_accounts()
        return SessionAccounts(
            resolved=False,
            adapter_key="",
            instance_key="",
            is_platform_instance=True,
            readable=accounts.readable,
            sendable=accounts.sendable,
        )

    if not owner.instance_key:
        # 频道行没有实例段（email 频道、历史数据）：按平台方，但它没有「本实例」的自有账户
        accounts = await _platform_only_accounts()
        return SessionAccounts(
            resolved=True,
            adapter_key=owner.adapter_key,
            instance_key="",
            is_platform_instance=True,
            readable=accounts.readable,
            sendable=accounts.sendable,
        )

    return await instance_accounts(
        owner.adapter_key,
        owner.instance_key,
        is_platform_instance=bool(owner.platform),
    )


# ===== 会话身份：本轮发言人 =====


async def session_speaker(ctx: Any) -> SessionSpeaker:
    """这次会话里「正在说话的这个人」以及他所在的实例

    身份有两个来源，缺一不可：

    1. 会话与实例：`resolve_session_owner`（只认宿主签发的 `from_chat_key`）；
    2. 人：`current_round_speaker`（宿主在消息到达时记下的发送者，见
       `services/agent/round_trigger_accounts.py`）。

    任一取不到就抛错，由插件原样把文案回给模型——不猜、不退回「模型填的那个用户 ID」。

    Raises:
        ValueError: 解析不出会话（`PREF_NOT_AVAILABLE`），或本轮定不出唯一发言人
            （`SPEAKER_REQUIRED`）
    """
    from nekro_agent.models.db_chat_channel import DBChatChannel
    from nekro_agent.services.agent.round_trigger_accounts import current_round_speaker
    from nekro_agent.services.resource_scope import resolve_session_owner

    owner = await resolve_session_owner(ctx)
    if not owner.resolved or not owner.adapter_key or not owner.instance_key:
        # 没有实例段（email 频道、历史数据）就没有「这个实例的偏好」可存，按不支持处理
        raise ValueError(PREF_NOT_AVAILABLE)

    speaker = current_round_speaker(owner.chat_key)
    if not speaker:
        raise ValueError(SPEAKER_REQUIRED)

    channel = await DBChatChannel.filter(chat_key=owner.chat_key).order_by("id").first()
    channel_type = str(getattr(channel, "channel_type", "") or "").strip().lower()
    return SessionSpeaker(
        adapter_key=owner.adapter_key,
        instance_key=owner.instance_key,
        chat_key=owner.chat_key,
        platform_userid=speaker,
        is_private=channel_type == "private",
    )


# ===== 地址校验与默认节日地址 =====

# `, ; < >` 防一次填进多个收件人或邮件头注入；`/ \` 防地址被拿去拼目录名时带出路径
# （RFC 允许本地部分里有 `/`，但这里接的几类服务商都不收这种地址）
_MAIL_ADDRESS_FORBIDDEN = frozenset(",;<>/\\")


def validate_mail_address(value: str) -> str:
    """校验并规范化一个收件地址；不合格抛 `ValueError`

    这是**用户自己填**的收件地址（不是账户配置），它只决定节日邮件发到哪里。校验只做
    「一个地址、看起来像地址」这一档：一个地址、去首尾空白后不超过 254 字、没有空白与
    `, ; < >`（防止一次填进多个收件人或邮件头注入）、恰好一个 `@`、`@` 后面有点号。
    真正的可达性由 SMTP 去试——多写几条正则只会把合法的地址拒掉。

    Returns:
        去首尾空白后的地址（**不**转小写：本地部分按 RFC 是大小写敏感的，虽然现实中很少见）

    Raises:
        ValueError: 不合格（文案 `INVALID_MAIL_ADDRESS`）
    """
    text = str(value or "").strip()
    if not text or len(text) > 254:
        raise ValueError(INVALID_MAIL_ADDRESS)
    if any(ch.isspace() or not ch.isprintable() or ch in _MAIL_ADDRESS_FORBIDDEN for ch in text):
        raise ValueError(INVALID_MAIL_ADDRESS)
    if text.count("@") != 1:
        raise ValueError(INVALID_MAIL_ADDRESS)
    local, _, domain = text.partition("@")
    if not local or not domain or "." not in domain:
        raise ValueError(INVALID_MAIL_ADDRESS)
    return text


def default_festival_address(adapter_key: str, platform_userid: str) -> str:
    """没有留地址时的默认节日收件地址；给不出时返回空串

    只有 `onebot_v11` 的账号能拼：它的平台账号就是 QQ 号，`<QQ号>@qq.com` 是同一个人的邮箱。
    账号必须是 5 ~ 11 位纯数字才拼——短了长了都不是 QQ 号，拼出来只会是一封退信。
    其他适配器（`qqbot_openclaw` 只有 openid 等）没有这种确定性，一律返回空串，由调用方跳过。
    """
    if adapter_key != "onebot_v11":
        return ""
    account = str(platform_userid or "").strip()
    if not account.isascii() or not account.isdigit():
        return ""
    if not (5 <= len(account) <= 11):
        return ""
    return f"{account}@qq.com"


# ===== 发信的唯一出口 =====


def _smtp_endpoint(account: "EmailAccount") -> Tuple[str, int, bool]:
    """运行时账户的 SMTP 主机、端口与是否 SSL

    与发信客户端（`ImapSmtpPasswordClient.send_message`）同一口径：优先走 SSL 端口，
    否则走普通端口 + STARTTLS。`to_runtime_account` 已经把预设与自定义都摊平成 `CUSTOM_*`。
    """
    if bool(account.CUSTOM_SMTP_USE_SSL):
        return account.CUSTOM_SMTP_HOST, int(account.CUSTOM_SMTP_SSL_PORT), True
    return account.CUSTOM_SMTP_HOST, int(account.CUSTOM_SMTP_PORT), False


# ===== 收发信失败的对外文案 =====
#
# 原始异常里有对端回的 banner、端口开没开（拒绝 / 超时分得出来）、核对时连上的是哪个地址。租户自填的服务器
# 经 DNS 重绑可以指向内网，这些差别回到租户眼前，「测试连接」「发一封信」就成了内网端口扫描器。
# 所以对外只有这几句，细节只进服务端日志。
MAIL_FAILURE_HOST = "host"
MAIL_FAILURE_LOGIN = "login"
MAIL_FAILURE_REJECTED = "rejected"
MAIL_FAILURE_CONNECT = "connect"
MAIL_FAILURE_TEXTS: Dict[str, str] = {
    MAIL_FAILURE_HOST: CUSTOM_HOST_UNAVAILABLE,
    MAIL_FAILURE_LOGIN: "登录失败：请检查邮箱地址与授权码",
    MAIL_FAILURE_REJECTED: "邮件服务器拒收了这封信",
    MAIL_FAILURE_CONNECT: "连不上邮件服务器，请稍后再试",
}
# 本模块自己抛的业务文案：不含对端的任何内容，原样给
_OWN_FAILURE_TEXTS = frozenset({NO_SENDER, CUSTOM_HOST_UNAVAILABLE, INVALID_MAIL_ADDRESS, ADDRESS_UNAVAILABLE})


def classify_mail_failure(error: BaseException) -> str:
    """把一次收发信失败归成四类之一（`MAIL_FAILURE_*`）

    - `host`：还没连就被拒了（主机名不合法、解析失败、指向不允许的地址）；
    - `login`：连上了、登录被拒；
    - `rejected`：登录了、服务器不收这封信（收件人 / 发件人 / 正文被拒）；
    - `connect`：其余一切——连不上、超时、TLS 失败、连上后对端核对没过（`MailPeerRejectedError`）。
      「对端核对没过」必须和「连不上」同一类：分开的话就等于告诉租户 NA 连上了那个内网端口。
    """
    import imaplib
    import smtplib

    from nekro_agent.tools.common_util import UnsafeDownloadUrlError

    if isinstance(error, UnsafeDownloadUrlError) or (isinstance(error, ValueError) and str(error) == CUSTOM_HOST_UNAVAILABLE):
        return MAIL_FAILURE_HOST
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return MAIL_FAILURE_LOGIN
    if isinstance(error, imaplib.IMAP4.abort):
        return MAIL_FAILURE_CONNECT
    if isinstance(error, imaplib.IMAP4.error) and "AUTHENTICATIONFAILED" in str(error).upper():
        return MAIL_FAILURE_LOGIN
    if isinstance(error, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError)):
        return MAIL_FAILURE_REJECTED
    return MAIL_FAILURE_CONNECT


def public_mail_failure(error: BaseException) -> str:
    """给租户 / 模型看的一句失败原因：本模块自己的业务文案原样给，其余按 `classify_mail_failure` 归成一句"""
    if isinstance(error, ValueError) and str(error) in _OWN_FAILURE_TEXTS:
        return str(error)
    return MAIL_FAILURE_TEXTS[classify_mail_failure(error)]


# 读出来时认不出的旧报错（旧版本把原始 SMTP 异常写进了实例自有邮箱的发信记录）统一换成这一句
STORED_SEND_ERROR_FALLBACK = "发送失败（详细原因只记在服务端日志里）"


def public_stored_send_error(stored: str) -> str:
    """发信记录里存着的失败原因，给租户看之前再收一次

    现在写进去的只会是 `MAIL_FAILURE_TEXTS` 里的几句或本模块的业务文案，原样给；其余（旧版本写下的
    原始异常文本，可能带对端地址、分得出连接被拒与超时）一律换成一句不带细节的话。
    """
    text = str(stored or "").strip()
    if not text:
        return ""
    if text in _OWN_FAILURE_TEXTS or text in MAIL_FAILURE_TEXTS.values():
        return text
    return STORED_SEND_ERROR_FALLBACK


async def send_via_account(view: AccountView, *, to_address: str, subject: str, content: str) -> None:
    """会话发信与节日邮件工人共用的唯一一条 SMTP 出口

    按地址从 `all_runtime_accounts()` 找回运行时账户（那是平台方账户 ∪ 表里启用的实例自有
    账户），取客户端池里按地址的那个客户端，没有就照 `send_email` 原来的做法临时建一个。
    纯文本 `MIMEText`（utf-8）：邮件正文是模型写的，没有 HTML。

    实例自有账户且服务商是自定义时，**连接前**再对 SMTP 主机与实际要连的端口做一次公网校验：
    保存时解析到公网、发信时解析到内网（DNS 重绑）就是靠这一步挡住的。不过就按「没有这个
    账户」回答，不把主机与原因带出去。

    **不写发信记录**：记录由调用方按自己的口径写（会话发信每次都写；队列只在最终结果时写），
    这里写一份会让两边各记一次。

    Raises:
        ValueError: 找不到运行时账户，或自定义主机这一轮不合格（文案 `NO_SENDER`）
    """
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    from nekro_agent.adapters.email.base import CUSTOM_PROVIDER_KEY
    from nekro_agent.adapters.utils import adapter_utils

    target = normalize_username(view.username)
    adapter = adapter_utils.get_adapter("email")
    runtime: Optional["EmailAccount"] = None
    for account in await all_runtime_accounts(adapter):
        if normalize_username(account.USERNAME) == target:
            runtime = account
            break
    if runtime is None:
        logger.warning(f"发信时找不到运行时账户，按没有可用发件人处理: account={view.ref}")
        raise ValueError(NO_SENDER)

    if view.kind == KIND_INSTANCE and runtime.EMAIL_ACCOUNT == CUSTOM_PROVIDER_KEY:
        host, port, _use_ssl = _smtp_endpoint(runtime)
        try:
            await assert_custom_mail_host_allowed(host, port)
        except ValueError as e:
            logger.warning(f"实例自有邮箱的自定义 SMTP 主机在发信前校验不过，按不可用处理: account={view.ref}")
            raise ValueError(NO_SENDER) from e

    # 信封与改造前 `send_email` 逐字相同（`MIMEMultipart` 包一段纯文本）：几种客户端
    # （密码、OAuth、Gmail API）的 `send_message` 都是按这个形状写、按这个形状测过的
    message = MIMEMultipart()
    message["From"] = runtime.USERNAME
    message["To"] = to_address
    message["Subject"] = subject
    message.attach(MIMEText(content, "plain", "utf-8"))

    clients = getattr(adapter, "email_clients", None) if adapter is not None else None
    client = clients.get(runtime.USERNAME) if isinstance(clients, dict) else None
    if client is None:
        create = getattr(adapter, "_create_email_client", None)
        if create is None:
            logger.warning(f"邮箱适配器不可用，无法发信: account={view.ref}")
            raise ValueError(NO_SENDER)
        client = create(runtime)
    await client.send_message(message)


# ===== 解析：账户名或引用 -> 视图 =====


def _matches(view: AccountView, account: str) -> bool:
    """`account` 可以是地址、`ref`，也接受显示名（模型常直接抄显示名）"""
    needle = str(account or "").strip()
    if not needle:
        return False
    if needle == view.ref:
        return True
    if normalize_username(needle) == view.username:
        return True
    return needle == view.display_label


def resolve_readable(accounts: SessionAccounts, account: str) -> AccountView:
    """从可读集合里挑一个账户；不在里面按「不存在」报错

    Raises:
        ValueError: 不在这次会话的可读集合里（文案与真的查不到逐字相同）
    """
    for view in accounts.readable:
        if _matches(view, account):
            return view
    logger.warning(f"账户不在可读集合里，按不存在处理: account={account!r}")
    raise ValueError(ACCOUNT_NOT_FOUND.format(account=account))


def resolve_sendable(accounts: SessionAccounts, from_account: Optional[str]) -> AccountView:
    """挑一个发件账户：点名就用点名的，没点名用默认的

    点名的可以是一个地址、一个 `ref` 或显示名；不能只给地址是因为租户会话拿不到平台方
    账户的地址——`ref` 正是为这条路径准备的。

    Raises:
        ValueError: 点名的账户不在可发集合里，或没有可用的发件人（文案与改造前逐字相同）
    """
    if from_account is None or not str(from_account).strip():
        for view in accounts.sendable:
            if view.is_default_sender:
                return view
        if accounts.sendable:
            return accounts.sendable[0]
        logger.warning("没有可用的发件人账户，按不存在处理")
        raise ValueError(NO_SENDER)
    for view in accounts.sendable:
        if _matches(view, str(from_account)):
            return view
    logger.warning(f"发件账户不在可发集合里，按不存在处理: from_account={from_account!r}")
    raise ValueError(NO_SENDER)


async def default_sender_for_instance(
    adapter_key: str,
    instance_key: str,
    *,
    is_platform_instance: bool,
    adapter: Any = None,
) -> Optional[AccountView]:
    """这个实例的默认发件人；一个能用的都没有时返回 None

    会话里「不填发件人」与节日邮件共用这一个选法（规则见 `_pick_default_ref`）。
    结果在进程内缓存 5 秒：同一轮里每个沙盒方法都查一次库没有意义，而 5 秒足够短，
    换了默认发件人之后下一次调用就能看到。
    """
    if not adapter_key or not instance_key:
        # 没有实例对时只能看平台方（频道没有实例段的老路径）
        accounts = await _platform_only_accounts()
        for view in accounts.sendable:
            if view.is_default_sender:
                return view
        return accounts.sendable[0] if accounts.sendable else None

    cache_key = (adapter_key, instance_key, bool(is_platform_instance))
    cached = _default_sender_cache.get(cache_key)
    if cached is not None and _clock() - cached[0] < _DEFAULT_SENDER_CACHE_TTL_SECONDS:
        return cached[1]

    accounts = await instance_accounts(
        adapter_key,
        instance_key,
        is_platform_instance=is_platform_instance,
        adapter=adapter,
    )
    picked: Optional[AccountView] = None
    for view in accounts.sendable:
        if view.is_default_sender:
            picked = view
            break
    if picked is None and accounts.sendable:
        picked = accounts.sendable[0]
    _default_sender_cache[cache_key] = (_clock(), picked)
    return picked


def clear_default_sender_cache() -> None:
    """清掉默认发件人的进程内缓存（改完账户之后必须调，否则最多 5 秒内还按旧值）"""
    _default_sender_cache.clear()


# ===== 地址唯一、自定义主机、每实例上限 =====


async def assert_address_available(
    username: str,
    *,
    exclude_instance_account_id: Optional[int] = None,
    ignore_platform_username: Optional[str] = None,
    adapter: Any = None,
) -> None:
    """地址必须在「适配器配置 + 账户表」两处合起来全局唯一

    Args:
        username: 待检查的地址（比较前统一去空白转小写）
        exclude_instance_account_id: 改一个实例自有账户时排除它自己
        ignore_platform_username: 改一个平台方账户时放行它原来的地址（同一个地址改了大小写
            或首尾空白不算冲突）
        adapter: 平台方配置所在的适配器；不传时从配置层取

    Raises:
        ValueError: 这个地址在另一处已经存在（文案不说明被谁占用）
    """
    target = normalize_username(username)
    ignored = normalize_username(ignore_platform_username) if ignore_platform_username else ""

    for account in platform_accounts(adapter):
        existing = normalize_username(account.USERNAME)
        if not existing or existing == ignored:
            continue
        if existing == target:
            logger.warning(f"邮箱地址与平台方账户冲突，拒绝使用: {target}")
            raise ValueError(ADDRESS_UNAVAILABLE)

    query = DBEmailAccount.filter(username=target)
    if exclude_instance_account_id is not None:
        query = query.exclude(id=int(exclude_instance_account_id))
    if await query.exists():
        logger.warning(f"邮箱地址与实例自有账户冲突，拒绝使用: {target}")
        raise ValueError(ADDRESS_UNAVAILABLE)


async def assert_custom_mail_host_allowed(host: str, port: int) -> None:
    """自定义邮件服务器的主机与端口校验

    租户能填任意主机等于拿到「从 NA 服务器发起出站连接」的能力：填一个内网地址就能拿 NA
    的网络位置连内网。所以主机走与租户接口地址**同一套**解析与网段判断
    （`assert_tenant_endpoint_url_safe` 那一份，本模块只做「主机名 + 端口」的包装，不另写
    网段表），端口只许常用那几个。

    保存时校验一次，建立连接前再校验一次。真正连接时客户端还会自己解析一次、只连校验过的地址
    （`ImapSmtpPasswordClient._checked_addresses`），这里是给页面一句明确的话、并在连不上之前就停下。
    透明代理的 Fake-IP 段对租户也拒绝（见 `common_util._FAKE_IP_NETWORKS`）。

    Raises:
        ValueError: 主机解析失败、指向非公网地址，或端口不在白名单里
    """
    from nekro_agent.tools.common_util import UnsafeDownloadUrlError, assert_tenant_host_safe

    port_value = int(port)
    if port_value not in CUSTOM_MAIL_PORTS:
        logger.warning(f"自定义邮箱服务器端口不在白名单里: {port_value}")
        raise ValueError(CUSTOM_HOST_UNAVAILABLE)
    host_text = str(host or "").strip()
    if not host_text:
        raise ValueError(CUSTOM_HOST_UNAVAILABLE)
    try:
        await assert_tenant_host_safe(host_text, port_value, subject="邮箱服务器", exclude_fake_ip=True)
    except UnsafeDownloadUrlError as e:
        logger.warning(f"自定义邮箱服务器地址被拒绝: host={host_text}, port={port_value}, reason={e}")
        raise ValueError(CUSTOM_HOST_UNAVAILABLE) from e


def instance_account_quota() -> int:
    """每个实例最多几个自有账户（平台方专属配置；读不到时按默认 3）"""
    from nekro_agent.core.config import config as core_config

    try:
        return max(0, int(getattr(core_config, "EMAIL_MAX_ACCOUNTS_PER_INSTANCE", 3)))
    except (TypeError, ValueError):  # pragma: no cover - 配置被手改坏时不该让整条路径 500
        return 3


async def assert_instance_account_quota(adapter_key: str, instance_key: str) -> None:
    """这个实例的自有账户还没到上限

    Raises:
        ValueError: 已经到上限（`EMAIL_MAX_ACCOUNTS_PER_INSTANCE`，0 表示一个都不许建）
    """
    quota = instance_account_quota()
    used = await DBEmailAccount.filter(
        owner_adapter_key=adapter_key,
        owner_instance_key=instance_key,
    ).count()
    if used >= quota:
        raise ValueError(f"每个实例最多 {quota} 个自有邮箱账户")


# ===== 运行时账户 =====


def to_runtime_account(row: DBEmailAccount) -> "EmailAccount":
    """把表里的行转成运行时用的 `EmailAccount`

    - 选了预设的账户：主机端口按预设填（预设改了就跟上），账户自己的 `settings` 覆盖不了它们；
    - `自定义`：主机端口取自 `settings`；
    - `USE_PROXY` 恒为假：实例自有账户不许走平台方的邮箱连接代理（那是平台方自己的出站通道）；
    - `RECEIVE_ENABLED` = 行上的 `receive_enabled` 且行是启用的且总闸开着：三者缺一，这个账户
      就不该进收信连接池。总闸关掉时即便行上开着收信也不连——那是平台方收回整项能力的开关。

    填出来的是**同一个** `EmailAccount`（认证方式固定 `password` + `imap_smtp`：实例只填地址与
    授权码；OAuth 与官方 API 是平台方账户的能力）。
    """
    from nekro_agent.adapters.email.base import CUSTOM_PROVIDER_KEY, EMAIL_PROVIDER_CONFIGS
    from nekro_agent.adapters.email.config import EmailAccount

    settings: Dict[str, Any] = dict(row.settings or {})
    provider_key = row.provider or CUSTOM_PROVIDER_KEY
    preset = EMAIL_PROVIDER_CONFIGS.get(provider_key)
    is_custom = provider_key == CUSTOM_PROVIDER_KEY

    if preset is None:
        # 预设表里没有这个键（接口层不收不认识的键，只会出现在预设被删掉之后）：
        # **不能**退回去读 `settings` 里的主机——那些主机从没按自定义校验过。主机留空，连接直接失败
        logger.warning(f"实例自有邮箱的服务商预设已不存在，按不可用处理: id={row.id}, provider={provider_key!r}")
        imap_host = smtp_host = ""
        imap_port, smtp_port, smtp_ssl_port, smtp_use_ssl = 993, 587, 465, True
    elif is_custom:
        imap_host = str(settings.get("imap_host", "") or "")
        smtp_host = str(settings.get("smtp_host", "") or "")
        imap_port = int(settings.get("imap_port") or 993)
        smtp_port = int(settings.get("smtp_port") or 587)
        smtp_ssl_port = int(settings.get("smtp_ssl_port") or 465)
        smtp_use_ssl = bool(settings.get("smtp_use_ssl", True))
    else:
        imap_host = str(preset.get("imap_host", ""))
        smtp_host = str(preset.get("smtp_host", ""))
        imap_port = int(preset.get("imap_port") or 993)
        smtp_port = int(preset.get("smtp_port") or 587)
        smtp_ssl_port = int(preset.get("smtp_ssl_port") or 465)
        smtp_use_ssl = str(preset.get("smtp_use_ssl", "")).lower() == "true"

    account = EmailAccount(
        DISPLAY_NAME=row.display_name or "",
        EMAIL_ACCOUNT=provider_key,
        CUSTOM_IMAP_HOST=imap_host,
        CUSTOM_IMAP_PORT=imap_port,
        CUSTOM_SMTP_HOST=smtp_host,
        CUSTOM_SMTP_PORT=smtp_port,
        CUSTOM_SMTP_SSL_PORT=smtp_ssl_port,
        CUSTOM_SMTP_USE_SSL=smtp_use_ssl,
        ENABLED=bool(row.enabled),
        RECEIVE_ENABLED=bool(row.receive_enabled and row.enabled and instance_accounts_enabled()),
        USE_PROXY=False,
        USERNAME=row.username,
        PASSWORD=row.password or "",
        AUTH_TYPE="password",
        TRANSPORT_TYPE="imap_smtp",
        SEND_ENABLED=bool(row.send_enabled),
        IS_DEFAULT_SENDER=bool(row.is_default_sender),
        LAST_TEST_SUCCESS=settings.get("last_test_success"),
        LAST_TEST_MESSAGE=str(settings.get("last_test_message", "") or ""),
        LAST_TEST_TIME=int(settings.get("last_test_time") or 0),
    )
    # 自定义服务器是租户填的：客户端在连上之后、登录之前核对实际对端 IP（见 EmailAccount 上的说明）。
    # 预设已被删掉的那种（主机留空、本来就连不上）也一并打上，不留一个不核对的口子
    account._tenant_endpoint = bool(is_custom or preset is None)
    return account


def _owner_map(adapter: Any) -> Optional[Dict[str, InstanceMailOwner]]:
    """取适配器上那份「地址 → 归属」的表；不是适配器（配置回退对象）时返回 None"""
    mapping = getattr(adapter, "instance_account_owner", None)
    return mapping if isinstance(mapping, dict) else None


async def refresh_runtime_owners(adapter_key: str, instance_key: str, *, adapter: Any = None) -> int:
    """把这个实例对下各账户在适配器「地址 → 归属」表里的那一项改成表里当前的归属，返回改了几项

    实例归属迁移只改数据库：适配器内存里那张表还记着旧实例，重启之前收到的新信，缓存快照就会
    记成旧实例——旧实例归另一个租户时，那个租户能按旧实例读到新实例的信。迁移的事务提交之后调它。
    只改表里已有的项（在池里的、或登记过的），不重连：客户端按地址做键，地址没变。
    """
    if adapter is None:
        from nekro_agent.adapters import loaded_adapters

        adapter = loaded_adapters.get("email")
    owners = _owner_map(adapter)
    if owners is None:
        return 0
    changed = 0
    for row in await DBEmailAccount.filter(owner_adapter_key=adapter_key, owner_instance_key=instance_key):
        if row.username in owners:
            owners[row.username] = instance_owner_of(row)
            changed += 1
    return changed


async def sync_runtime_account(adapter: Any, account: "EmailAccount", *, owner: Optional[InstanceMailOwner] = None) -> None:
    """账户写库之后让运行时客户端跟上（新增 / 修改都走这里）

    两件事：

    1. **重连客户端**，与适配器账户接口原来那一份相同；
    2. **记下这个地址归谁**：`owner` 非空（实例自有账户）就写进适配器的
       `instance_account_owner`，收信时按它决定走哪条路；平台方账户不在这张表里，所以要显式
       清掉这个地址（同一个地址从实例自有转回平台方时，残留的一项会让它的信继续按实例路径处理）。

    `account_chat_mapping` 只放平台方账户：那张表是「地址 → `email-<地址>` 频道」的映射，
    实例自有账户没有那个频道（建出来没有实例段，会被判成平台方）。
    """
    # 归属要在**重连之前**登记：适配器的重连按「地址在不在这张表里」决定走哪条路——
    # 新建的实例自有账户如果此刻还不在表里，就会按平台方账户去连，跳过总闸与
    # 「连接前再校验自定义主机」（保存时解析到公网、一秒后连接时改指内网，正是那一步要挡的）
    owners = _owner_map(adapter)
    if owners is not None:
        if owner is not None:
            owners[account.USERNAME] = owner
        else:
            owners.pop(account.USERNAME, None)

    reconnect_success = True
    reconnect = getattr(adapter, "_reconnect_email_client", None)
    if reconnect:
        reconnect_success = bool(await reconnect(account))

    mapping = getattr(adapter, "account_chat_mapping", None)
    if not isinstance(mapping, dict):
        return
    if owner is None and account.ENABLED and account.RECEIVE_ENABLED and reconnect_success:
        mapping[account.USERNAME] = account.USERNAME
    else:
        mapping.pop(account.USERNAME, None)


async def remove_runtime_account(adapter: Any, account: "EmailAccount") -> None:
    """账户删掉 / 停用之后断开运行时客户端（逻辑与适配器账户接口原来那一份相同）"""
    owners = _owner_map(adapter)
    if owners is not None:
        owners.pop(account.USERNAME, None)
    remove = getattr(adapter, "_remove_email_client", None)
    if remove:
        await remove(account.USERNAME)


async def instance_runtime_accounts(adapter: Any = None) -> List[Tuple["EmailAccount", InstanceMailOwner]]:
    """实例自有账户的「运行时账户 + 归属」列表（总闸关掉时为空）

    收信连接池按它建（同时填适配器上那张「地址 → 归属」的表），发信客户端池也用它拼。
    与 `all_runtime_accounts` 分开是因为**收信侧必须知道每个地址归谁**：只拿到一个
    `EmailAccount` 分不出「这封信该走平台方那条建频道的路，还是只写缓存 + 推通知」。
    """
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance

    if not instance_accounts_enabled():
        return []
    accounts: List[Tuple["EmailAccount", InstanceMailOwner]] = []
    for row in await DBEmailAccount.filter(enabled=True).order_by("id"):
        try:
            active = await DBAdapterInstance.is_instance_active(row.owner_adapter_key, row.owner_instance_key)
        except Exception as e:  # noqa: BLE001 - 判据查不动时按不可用处理（fail-closed）
            logger.warning(
                f"判断实例是否可用失败，跳过它的自有邮箱: {row.owner_adapter_key}/{row.owner_instance_key} | {e}",
            )
            continue
        if not active:
            continue
        accounts.append((to_runtime_account(row), instance_owner_of(row)))
    return accounts


async def all_runtime_accounts(adapter: Any = None) -> List["EmailAccount"]:
    """平台方账户 ∪ 表里启用且归属实例当前可用的账户

    这是发信客户端池的视图（`EmailAdapter.all_accounts` 就是它），收信连接池也按它的
    实例自有那一部分建（收信时按地址查 `instance_account_owner` 决定走哪条路）。

    总闸关掉时**不含**实例自有账户：发信那边也不能用，与 `instance_accounts` 同一口径。
    """
    runtime: List["EmailAccount"] = list(platform_accounts(adapter))
    runtime.extend(account for account, _owner in await instance_runtime_accounts(adapter))
    return runtime


# ===== 附件根目录 =====


def email_attachment_root() -> Path:
    """附件总目录（`uploads/email_attachment/`）

    下载、删除、改名、写入四处都从这里取：路径口径只写一次，才不会出现「写入到 A、
    读取时按 B 拼」这种只有一处能对上的偏差。测试把它指到临时目录。
    """
    from nekro_agent.core.os_env import OsEnv

    return Path(OsEnv.DATA_DIR) / "uploads" / "email_attachment"


def safe_attachment_dir_name(directory: str) -> str:
    """把附件目录名收成一个单段名字；不合法返回空串

    目录名来自表里的 `attachment_dir` 或地址（后者可能是调用方给的）：先 `Path(...).name`，
    再确认结果是非空、不是 `.` / `..`、且原名里没有分隔符。`../<别的账户>` 这种写法
    `Path(...).name` 只得 `别的账户`，看起来干净，但它本来就不该出现在目录名里——直接拒掉。
    """
    text = str(directory or "").strip()
    if not text or "/" in text or "\\" in text:
        return ""
    name = Path(text).name
    if not name or name in {".", ".."}:
        return ""
    return name


def attachment_dir_path(directory: str) -> Optional[Path]:
    """附件总目录下某个账户目录的绝对路径；目录名不合法时返回 None

    父目录必须正好是附件总目录：只判「落在总目录里」放得过 `a/../b` 这种写法。
    """
    name = safe_attachment_dir_name(directory)
    if not name:
        return None
    root = email_attachment_root().resolve()
    candidate = (root / name).resolve()
    if candidate.parent != root:
        return None
    return candidate


def attachment_file_path(directory: str, email_uid: str, filename: str) -> Optional[Path]:
    """`email_attachment/<目录>/<UID>/<文件名>` 的绝对路径；任何一段不合法都返回 None

    逐层判父子关系，而不是只判「落在总目录里」：`../<别的账户>/<UID>` 这种写法仍然在总目录
    里，只判总目录会让它走进别人的目录。所以：

    1. 账户目录的父目录必须是附件总目录；
    2. 邮件目录的父目录必须是账户目录（UID 只收单段名字）；
    3. 文件的父目录必须是邮件目录（文件名只收单段名字，不能是 `.` / `..`）。

    调用方拿到的路径可能指向一个不存在的文件——那是「附件没下载成功」，由调用方决定怎么答。
    """
    dir_name = safe_attachment_dir_name(directory)
    uid = str(email_uid or "").strip()
    name = str(filename or "").strip()
    if not dir_name or not uid or not name:
        return None
    if not is_mail_uid(uid):
        return None
    if "/" in name or "\\" in name or name in {".", ".."}:
        return None

    root = email_attachment_root().resolve()
    account_dir = (root / dir_name).resolve()
    if account_dir.parent != root:
        return None
    mail_dir = (account_dir / uid).resolve()
    if mail_dir.parent != account_dir:
        return None
    file_path = (mail_dir / name).resolve()
    if file_path.parent != mail_dir:
        return None
    return file_path


_INSTANCE_ATTACHMENT_DIR_PATTERN = re.compile(r"_i[0-9]+")
# IMAP 的 UID 是正整数（写入侧 `_email_id_text` 也只收纯数字）；`\d` 会放过全角等 Unicode 数字
_MAIL_UID_PATTERN = re.compile(r"[0-9]{1,20}")


def is_instance_attachment_dir(name: str) -> bool:
    """附件目录名是不是实例自有账户新建时的那种（`_i<数字>`，表 ID 不复用）

    判据只写这一份：删账户、划转删箱、彻底删除实例、转回平台方四处都按它分两种删法。
    只收 ASCII 数字：`\\d` 会放过全角数字之类的 Unicode 数字。
    """
    return bool(_INSTANCE_ATTACHMENT_DIR_PATTERN.fullmatch(str(name or "").strip()))


def is_mail_uid(uid: str) -> bool:
    """附件路径里的 UID 段只收 1 ~ 20 位 ASCII 数字（白名单，读、写、删同一个判据）"""
    return bool(_MAIL_UID_PATTERN.fullmatch(str(uid or "")))


@dataclass(frozen=True)
class AttachmentCleanup:
    """删一批实例自有账户时附件该怎么删（先算好，删完缓存行再执行）

    - `whole_dirs`：这个账户自己的 `_i<id>` 目录，整个删；
    - `mail_dirs`：由平台方「转为实例自有」的账户沿用的地址目录里，**这个账户被删掉的那几封**
      （`<目录>/<UID>/`）；目录本身删完这几封之后空了才删（`prune_dirs`）。

    路径一律是**没解析过**的：删之前要用它判「是不是链接」，解析过的路径已经是链接指向的目标了。
    """

    whole_dirs: Tuple[Path, ...] = ()
    mail_dirs: Tuple[Path, ...] = ()
    prune_dirs: Tuple[Path, ...] = ()


def _unresolved_account_dir(dir_name: str) -> Optional[Path]:
    """`附件总目录/<目录名>`（不解析）；目录名不合法、或（不是链接时）解析后不在总目录正下方时为 None

    本身是链接的照样返回：调用方只删链接本身，不进入它，链接指到哪里都无所谓。
    """
    name = safe_attachment_dir_name(dir_name)
    if not name:
        return None
    candidate = email_attachment_root() / name
    if is_link(candidate):
        return candidate
    return candidate if attachment_dir_path(name) is not None else None


async def plan_attachment_cleanup(account_rows: Sequence[DBEmailAccount]) -> AttachmentCleanup:
    """算出删这些账户时要删的附件路径（只读；要在删缓存行**之前**调，删完就查不到 UID 了）

    地址目录为什么逐封删而不是整个删：整个删的判据只能是「目录名长得像这个地址」，而删错一个
    地址目录就是平台方自己收件箱的附件全没了、没法恢复。逐封删只碰这个账户名下缓存行对应的
    `<UID>/`：行的归属快照就是这个账户，目录里别的东西一样不动。

    两种形态都要对得上这个账户自己，对不上的跳过并记警告：

    - 整目录删：目录名必须**正好是** `_i<这个账户的 id>`（表 ID 不复用）。只认「长得像 `_i<数字>`」
      的话，一行被改过 `attachment_dir` 的账户能让删除落到别的账户的 `_i` 目录上；
    - 地址目录：目录名必须是这个账户自己的地址（地址不能改，由平台方转来时目录名就是当时的地址），
      且含 `@`。
    """
    whole: List[Path] = []
    mails: List[Path] = []
    prune: List[Path] = []
    for row in account_rows:
        dir_name = str(row.attachment_dir or "").strip()
        if not dir_name:
            continue
        directory = _unresolved_account_dir(dir_name)
        if directory is None:
            logger.warning(f"附件目录名不合法，跳过不删: 账户 id={row.id}")
            continue
        if is_instance_attachment_dir(dir_name):
            if dir_name != new_attachment_dir(row.id):
                logger.warning(f"附件目录 {dir_name} 不是这个账户自己的 _i<id>，跳过不删: 账户 id={row.id}")
                continue
            whole.append(directory)
            continue
        username = normalize_username(row.username)
        if "@" not in dir_name or dir_name != safe_attachment_dir_name(username):
            logger.warning(f"附件目录名既不是 _i<数字> 也不是账户自己的地址，跳过不删: 账户 id={row.id}")
            continue
        if is_link(directory):
            # 地址目录本身是链接：只删链接，不进入它（里面的东西不知道是谁的）
            whole.append(directory)
            continue
        from nekro_agent.models.db_email import DBEmail

        uids = await DBEmail.filter(
            account_username=username,
            owner_adapter_key=row.owner_adapter_key or "",
            owner_instance_key=row.owner_instance_key or "",
        ).values_list("email_uid", flat=True)
        for uid in sorted({str(value or "").strip() for value in uids}):
            if is_mail_uid(uid):
                mails.append(directory / uid)
        prune.append(directory)
    return AttachmentCleanup(whole_dirs=tuple(whole), mail_dirs=tuple(mails), prune_dirs=tuple(prune))


def run_attachment_cleanup(cleanup: AttachmentCleanup) -> int:
    """按 `plan_attachment_cleanup` 的结果删文件，返回删掉的目录数；删不掉的只记警告

    每个路径删之前用**没解析过**的路径判一次是不是链接：是就只删链接本身，不进入它。
    """
    removed = 0
    for directory in (*cleanup.whole_dirs, *cleanup.mail_dirs):
        try:
            if is_link(directory):
                unlink_link(directory)
                removed += 1
            elif directory.is_dir():
                shutil.rmtree(directory)
                removed += 1
        except Exception as e:  # noqa: BLE001 - 删不掉只记警告，账户行已经删了
            logger.warning(f"删除邮箱附件失败: {directory.name} | {type(e).__name__}")
    for directory in cleanup.prune_dirs:
        try:
            if not is_link(directory) and directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
                removed += 1
        except Exception as e:  # noqa: BLE001 - 同上
            logger.warning(f"删除空的邮箱附件目录失败: {directory.name} | {type(e).__name__}")
    if removed:
        logger.info(
            f"已删除邮箱附件: 整目录 {len(cleanup.whole_dirs)} 个、地址目录里的邮件 {len(cleanup.mail_dirs)} 封",
        )
    return removed


async def attachment_dir_for_message(
    account_username: str,
    *,
    owner_adapter_key: str,
    owner_instance_key: str,
) -> str:
    """这封缓存行对应的账户附件目录名

    - 平台方账户（快照为空）：目录名就是地址（写入侧一直如此）；
    - 实例自有账户：查账户表拿 `attachment_dir`（`_i<id>` 或由平台方转来的原地址）。
      查不到行时退回 `_i<id>` 形态是不可能的（没有 id），所以退回空串——调用方按 404 处理，
      这比拿地址去猜一个可能属于平台方账户的目录安全。
    """
    if not owner_adapter_key and not owner_instance_key:
        return safe_attachment_dir_name(account_username)
    row = await DBEmailAccount.filter(
        username=normalize_username(account_username),
        owner_adapter_key=owner_adapter_key or "",
        owner_instance_key=owner_instance_key or "",
    ).order_by("id").first()
    if row is None:
        return ""
    return safe_attachment_dir_name(row.attachment_dir)


# ===== 缓存行的归属条件 =====

_PLATFORM_SNAPSHOT = {"owner_adapter_key": "", "owner_instance_key": ""}


def owner_snapshot(view: "AccountView") -> Dict[str, str]:
    """这个账户的行该匹配哪一组归属快照

    读侧一律按「行的快照 = 账户当前归属」过滤：地址换了主人之后，旧主人的缓存行不会以
    新主人的名义被读到。平台方账户的两列都是空串，与存量口径一致。
    """
    if view.kind == KIND_PLATFORM:
        return dict(_PLATFORM_SNAPSHOT)
    return {
        "owner_adapter_key": view.owner_adapter_key,
        "owner_instance_key": view.owner_instance_key,
    }


async def cache_rows_for(view: "AccountView") -> QuerySet:
    """这个账户能读的缓存行（按归属快照过滤的查询，调用方自己接排序与分页）

    插件与路由共用这一处：两边各拼一份条件时，只要有一边漏了一列，租户就能读到别人的缓存。
    """
    from nekro_agent.models.db_email import DBEmail

    return DBEmail.filter(account_username=normalize_username(view.username), **owner_snapshot(view))


async def purge_foreign_cache_rows(
    username: str,
    *,
    owner_adapter_key: str,
    owner_instance_key: str,
) -> int:
    """删掉这个地址下**不属于**给定归属的全部缓存行，返回删除行数

    为什么要在新账户开始轮询之前做：唯一键是 `(account_username, email_uid)`，同一个地址
    换了主人（旧账户删了、新账户建起来）之后，旧主人留下的行还在表里。新信 UID 撞上时
    `update_or_create` 会改写那一行（写侧也做了整行覆盖），但「没撞上」的那些行仍会以新主人
    的地址留着——它们的归属快照是旧主人，读侧虽然看不到，却会一直占着 UID 与磁盘。

    归属是实例自有时顺带删掉地址形态的附件目录（`email_attachment/<地址>/`）：那种目录只属于
    平台方账户，实例自有账户的附件在 `_i<id>` / 由平台方转来的原地址目录里，删它反而会删掉
    平台方自己收件箱的附件——所以只在**归属是实例自有**时删，且目录名按 `Path(...).name` 收。
    """
    from nekro_agent.models.db_email import DBEmail

    target = normalize_username(username)
    if not target:
        return 0
    # 条件是「快照**不等于**给定归属」，必须显式写成 `NOT (a = ? AND b = ?)`：
    # `exclude(a=..., b=...)` 在 ORM 里是 `NOT (a = ? OR b = ?)`，两列里任意一列不同就算
    # 「不属于」的话，会把同一实例下另一个地址的行也清掉（更糟的是反过来漏删）
    keep = Q(owner_adapter_key=owner_adapter_key or "") & Q(owner_instance_key=owner_instance_key or "")
    try:
        stale_ids = await DBEmail.filter(account_username=target).exclude(keep).values_list("id", flat=True)
        deleted = int(await DBEmail.filter(id__in=list(stale_ids)).delete()) if stale_ids else 0
    except Exception as e:  # noqa: BLE001 - 清理失败不该挡住新账户开始收信
        logger.warning(f"清理地址 {target} 的残留缓存行失败: {type(e).__name__}")
        return 0

    if owner_adapter_key or owner_instance_key:
        directory = attachment_dir_path(target)
        if directory is not None and directory.is_dir():
            try:
                shutil.rmtree(directory)
                logger.info(f"已删除地址形态的附件目录（地址换了主人）: {directory.name}")
            except Exception as e:  # noqa: BLE001 - 删不掉只记警告，缓存行已经清干净了
                logger.warning(f"删除地址形态的附件目录失败: {directory.name} | {type(e).__name__}")
    return deleted


async def purge_orphan_cache_rows() -> int:
    """删掉归属快照对不上任何现存账户的缓存行，返回删除行数

    两种「对不上」：

    1. 快照非空：`(地址, 适配器, 实例)` 在 `email_account` 里没有这一行（账户被删了、或者
       实例被彻底删了）；
    2. 快照为空：地址也不在平台方账户的 `RECEIVE_ACCOUNTS` 里（平台方账户被删了）。

    每天随发信记录的清理跑一次（同一个频率，见 `_purge_old_send_logs`）：这些行读侧本来
    就看不到，留着只占 UID 与磁盘，但删它们必须**不**影响正被使用的缓存——所以只删
    「确实没有账户」的那些。
    """
    from nekro_agent.models.db_email import DBEmail

    platform_names = {normalize_username(account.USERNAME) for account in platform_accounts()}
    live_pairs = {
        (
            normalize_username(row.username),
            row.owner_adapter_key or "",
            row.owner_instance_key or "",
        )
        for row in await DBEmailAccount.all()
    }
    # 只取 ID 与三列归属：缓存行带正文（每封最多 5000 字），整表读进内存再逐行删，
    # 在大邮箱上既吃内存又慢——而这一步挂在发信记录清理上，会落在某次发信调用的路径里
    orphan_ids: List[int] = []
    rows = await DBEmail.all().values_list("id", "account_username", "owner_adapter_key", "owner_instance_key")
    for row_id, account_username, owner_adapter_key, owner_instance_key in rows:
        adapter_key = owner_adapter_key or ""
        instance_key = owner_instance_key or ""
        if not adapter_key and not instance_key:
            alive = normalize_username(account_username) in platform_names
        else:
            alive = (normalize_username(account_username), adapter_key, instance_key) in live_pairs
        if not alive:
            orphan_ids.append(int(row_id))

    removed = 0
    for start in range(0, len(orphan_ids), _ORPHAN_DELETE_CHUNK):
        chunk = orphan_ids[start : start + _ORPHAN_DELETE_CHUNK]
        try:
            removed += int(await DBEmail.filter(id__in=chunk).delete())
        except Exception as e:  # noqa: BLE001 - 一批删不掉不该让整轮清理停下
            logger.warning(f"删除孤儿缓存行失败: count={len(chunk)} | {type(e).__name__}")
    return removed


# 孤儿缓存行按 ID 分批删，一批的个数（SQL 的 IN 列表不宜过长）
_ORPHAN_DELETE_CHUNK = 500


# ===== 发信记录 =====


def _clip(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def reset_send_log_state() -> None:
    """清掉「上次清理时刻」（进程内状态，供测试与关停使用）"""
    global _last_purge_at
    _last_purge_at = None


async def _purge_old_send_logs() -> None:
    """距上次清理超过一天就删掉 365 天前的记录、顺带清孤儿缓存行；进程里第一次写入也算超过

    两件事挂在同一个「每 24 小时最多一次」的闸上：它们都是维护动作、都不该每次发信都跑一遍，
    而合在一起就只有一个时刻状态要维护（`_last_purge_at`），不会出现「发信记录清了、
    缓存行还留着」的中间态。
    """
    global _last_purge_at
    moment = _clock()
    if _last_purge_at is not None and moment - _last_purge_at < SEND_LOG_PURGE_INTERVAL_SECONDS:
        return
    # 先记时刻再删：删除失败时不必让下一次写操作再撞一遍同一个失败
    _last_purge_at = moment
    cutoff = _wall_clock() - timedelta(days=SEND_LOG_KEEP_DAYS)
    try:
        await DBEmailSendLog.filter(create_time__lt=cutoff).delete()
    except Exception as e:  # noqa: BLE001 - 清理失败不能反过来影响业务调用
        logger.warning(f"清理过期发信记录失败: {type(e).__name__}")
    try:
        removed = await purge_orphan_cache_rows()
        if removed:
            logger.info(f"已清理 {removed} 条没有对应账户的邮件缓存行")
    except Exception as e:  # noqa: BLE001 - 同上：维护动作失败不外抛
        logger.warning(f"清理孤儿邮件缓存行失败: {type(e).__name__}")


async def record_send(
    *,
    account: AccountView,
    adapter_key: str,
    instance_key: str,
    chat_key: str,
    tenant_id: Optional[int],
    purpose: str,
    to_address: str,
    subject: str,
    reason: str,
    ok: bool,
    error: str = "",
) -> None:
    """写一行发信记录；写成功后顺手清一次过期行

    字段一律先截断再入库（主题 256、理由 500、失败原因 256）：它们的长度由模型与上游决定，
    一行撑爆一张表只需要一次调用。写库失败只记 WARNING，不外抛——发信本身已经发生了，
    记录写不进去不该让调用方以为信没发出去。
    """
    try:
        await DBEmailSendLog.create(
            account_kind=account.kind,
            account_ref=account_send_log_ref(account),
            adapter_key=_clip(adapter_key, 64),
            instance_key=_clip(instance_key, 64),
            chat_key=_clip(chat_key, 256),
            tenant_id=tenant_id,
            purpose=_clip(purpose, 16),
            to_address=_clip(to_address, 512),
            subject=_clip(subject, 256),
            reason=_clip(reason, 500),
            ok=bool(ok),
            error=_clip(error, 256),
        )
    except Exception as e:  # noqa: BLE001 - 记录写不进去不能把已经发出的信变成失败
        logger.warning(f"写发信记录失败: account={account.ref}, to={to_address} | {type(e).__name__}")
        return
    await _purge_old_send_logs()


# ===== 账户行上的小工具（后台接口与生命周期共用） =====


async def list_instance_accounts(adapter_key: str, instance_key: str) -> List[DBEmailAccount]:
    """按实例对列出实例自有账户（不做可用性判断，后台要看得见停用的那些）"""
    return await _instance_rows(adapter_key, instance_key)


async def clear_other_default_senders(adapter_key: str, instance_key: str, *, keep_id: int) -> int:
    """同一实例对里只留一个默认发件人，返回被取消标记的行数"""
    updated = await DBEmailAccount.filter(
        owner_adapter_key=adapter_key,
        owner_instance_key=instance_key,
        is_default_sender=True,
    ).exclude(id=int(keep_id)).update(is_default_sender=False)
    if updated:
        clear_default_sender_cache()
    return int(updated)


def new_attachment_dir(account_id: int) -> str:
    """新建实例自有账户的附件目录名：`_i<id>`

    表 ID 不复用，所以地址换了主人之后新主人永远看不到旧主人的文件。
    """
    return f"_i{int(account_id)}"


async def attachment_root_for(view: AccountView) -> str:
    """这个账户的附件根目录名（相对 `uploads/email_attachment/`）

    - 平台方账户：地址（改造前的路径，文件不搬）；
    - 实例自有账户：表里的 `attachment_dir`（新建的是 `_i<id>`；由平台方账户转过来的沿用
      原来的地址目录）。为空按不存在处理——拿地址去拼会让它读到平台方同名目录里的文件。

    Raises:
        ValueError: 实例自有账户的行没了或没有目录名（文案 `ACCOUNT_NOT_FOUND`）
    """
    if view.kind == KIND_PLATFORM:
        return view.username
    row: Optional[DBEmailAccount] = None
    if view.instance_account_id is not None:
        row = await DBEmailAccount.get_or_none(id=int(view.instance_account_id))
    if row is None:
        logger.warning(f"附件根目录：实例自有账户的行不存在，按不存在处理: account={view.ref}")
        raise ValueError(ACCOUNT_NOT_FOUND.format(account=view.ref))
    directory = str(row.attachment_dir or "").strip()
    if not directory:
        logger.warning(f"附件根目录：实例自有账户没有目录名，按不存在处理: account={view.ref}")
        raise ValueError(ACCOUNT_NOT_FOUND.format(account=view.ref))
    return directory


__all__ = [
    "ACCOUNT_NOT_FOUND",
    "ACCOUNT_REF_KEY_FILE",
    "ADDRESS_UNAVAILABLE",
    "CUSTOM_HOST_UNAVAILABLE",
    "CUSTOM_MAIL_PORTS",
    "INVALID_MAIL_ADDRESS",
    "KIND_INSTANCE",
    "KIND_PLATFORM",
    "MAIL_FAILURE_CONNECT",
    "MAIL_FAILURE_HOST",
    "MAIL_FAILURE_LOGIN",
    "MAIL_FAILURE_REJECTED",
    "MAIL_FAILURE_TEXTS",
    "NO_SENDER",
    "STORED_SEND_ERROR_FALLBACK",
    "PREF_NOT_AVAILABLE",
    "PURPOSE_FESTIVAL",
    "PURPOSE_SEND_EMAIL",
    "SEND_LOG_KEEP_DAYS",
    "SPEAKER_REQUIRED",
    "AccountView",
    "AttachmentCleanup",
    "InstanceMailOwner",
    "SessionAccounts",
    "SessionSpeaker",
    "all_runtime_accounts",
    "account_send_log_ref",
    "assert_address_available",
    "assert_custom_mail_host_allowed",
    "assert_instance_account_quota",
    "attachment_dir_for_message",
    "attachment_dir_path",
    "attachment_file_path",
    "attachment_root_for",
    "build_account_view",
    "cache_rows_for",
    "chat_accounts",
    "classify_mail_failure",
    "clear_default_sender_cache",
    "clear_other_default_senders",
    "default_festival_address",
    "default_sender_for_instance",
    "display_label_of",
    "email_attachment_root",
    "instance_account_quota",
    "instance_account_ref",
    "instance_accounts",
    "instance_accounts_enabled",
    "instance_owner_of",
    "instance_runtime_accounts",
    "is_instance_attachment_dir",
    "is_mail_uid",
    "legacy_send_log_refs",
    "list_instance_accounts",
    "mask_email_address",
    "new_attachment_dir",
    "normalize_username",
    "owner_snapshot",
    "platform_account_ref",
    "platform_account_shared_with",
    "plan_attachment_cleanup",
    "platform_accounts",
    "public_mail_failure",
    "public_stored_send_error",
    "purge_foreign_cache_rows",
    "purge_orphan_cache_rows",
    "record_send",
    "refresh_runtime_owners",
    "remove_runtime_account",
    "reset_account_ref_key_cache",
    "reset_send_log_state",
    "resolve_readable",
    "resolve_sendable",
    "run_attachment_cleanup",
    "safe_attachment_dir_name",
    "send_log_ref_matches",
    "send_via_account",
    "session_accounts",
    "session_speaker",
    "sync_runtime_account",
    "to_runtime_account",
    "validate_mail_address",
    "visible_label_for",
]
