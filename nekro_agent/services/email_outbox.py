"""节日祝福邮件的名单、入队、发信工人与队列管理

## 为什么名单与队列在核心里

节日祝福插件负责「什么时候发、正文写什么」，**名单**与**怎么发**是归属与配额的事：
收件人从频道表与用户偏好表算出来，发件人要过实例、插件开关与平台方共享三道判据，发信还要按
发件邮箱匀速。这些规则如果写在插件里，将来第二个插件要做同样的推送就得再抄一遍，抄漏一条
就是一次越权或一次垃圾邮件投诉。所以核心给零件，插件只填「节日名、说明、正文」。

本模块**不 import 任何 `plugins.*`**：两个插件键是字符串常量，与插件自己的 `plugin.key`
由用例比对（漂移就变红），而不是靠 import 绑在一起。

## 匀速与重试

同一批几十上百封一次全发出去会被收件方当成垃圾邮件，所以按**发件邮箱**匀速：进程内记每个
发件账户上一次发出的时刻，两封之间至少隔 `EMAIL_OUTBOX_INTERVAL_SECONDS`；不同发件账户各走
各的，一轮里每个发件账户最多发一封。失败按 1 分钟、5 分钟重试两次，第三次落 `failed`。

进程重启后照样从 `queued` 的行接着发：状态全在表里，只有「每个发件账户上一次发出的时刻」是
进程内的，重启后从头算——重启后多发一封不会造成实质影响，而为了它去写一张表不值得。

## 已知残留

发出成功、写库前进程崩了，重启后这一封会再发一次。收件人可能收到两封一样的祝福邮件，
比「可能一封都没发」好，接受。

## 发出前的判据为什么每次重判

入队到发出之间可能过去很久（队列积压、暂停后恢复、进程重启），这段时间里用户可能退订、
平台方可能取消共享、实例可能被停用、总闸可能被关掉。所以每一封发出前都把判据重跑一遍，
任何一条不过就记 `skipped` 并清空正文——不换发件人、不补发、不悄悄丢掉（后台看得到计数）。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from tortoise.timezone import now as db_now

from nekro_agent.core.config import config as core_config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_email_outbox import DBEmailOutbox
from nekro_agent.models.db_email_send_log import DBEmailSendLog
from nekro_agent.models.db_email_user_pref import DBEmailUserPref
from nekro_agent.services import email_accounts as mail
from nekro_agent.services.plugin.scope import plugin_effective_on_instance, plugin_effective_with_instance

if TYPE_CHECKING:  # pragma: no cover - 只在类型检查时用
    from nekro_agent.services.email_accounts import AccountView

logger = get_sub_logger("email_outbox")

# 两个插件键：与 `plugins/builtin/festival_greeting/plugin.py`、`plugins/builtin/email_utils.py`
# 的 `plugin.key` 必须一致（用例比对）。放在核心是为了让本模块不 import 插件
FESTIVAL_PLUGIN_KEY = "KroMiose.festival_greeting"
EMAIL_UTILS_PLUGIN_KEY = "liugu.email_utils"

# 队列行状态。`paused` 只是模型说明里的第五个取值，列定义不动（仍是 16 字）
STATUS_QUEUED = "queued"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_PAUSED = "paused"

# 失败重试的等待秒数：第 1、2 次失败分别等 1 分钟、5 分钟，第 3 次落 failed
RETRY_DELAYS_SECONDS = (60, 300)
MAX_ATTEMPTS = 3

# 已结束的行保留 7 天；进程内每 24 小时最多清一次
OUTBOX_KEEP_DAYS = 7
OUTBOX_PURGE_INTERVAL_SECONDS = 86400.0

# 工人每秒醒一次（配置管的是「同一个发件邮箱两封之间至少隔多久」，不是醒来频率）
WORKER_TICK_SECONDS = 1.0
# 启动后先等一会儿：迁移、插件加载、适配器连接都在抢磁盘与网络
WORKER_STARTUP_DELAY_SECONDS = 5.0

# 昵称清洗后的长度上限
NICKNAME_MAX_CHARS = 24
NICKNAME_FALLBACK = "朋友"

# 进度记在进程内：`(account_kind, account_ref) -> 上一次发出的单调时刻`
_last_sent_at: Dict[tuple, float] = {}
_last_purge_at: Optional[float] = None

# 供测试注入的时钟：匀速间隔与清理节流都用它
_clock: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class FestivalRecipient:
    """节日邮件的一个收件人

    Attributes:
        platform_userid: 平台账号（去重与查偏好行用它）
        to_address: 收件地址
        nickname: 已清洗的称呼：去控制字符与换行、首尾空白、最多 24 字；取不到用「朋友」
        source: `private`（在本实例开过私聊）/ `subscribed`（在群里订阅过）；两类都是时记 private
    """

    platform_userid: str
    to_address: str
    nickname: str
    source: str


# ===== 小工具 =====


def _clip(text: str, limit: int) -> str:
    return (text or "")[:limit]


def clean_nickname(raw: str) -> str:
    """昵称清洗：去控制字符与换行、首尾空白，最多 24 字；空则用「朋友」

    昵称是外部内容（用户自己填的、协议端上报的），它会进邮件正文：换行能把一句话拆成两行，
    控制字符能改写终端与日志，过长会把标题挤掉。这里只做「能不能安全地当一行称呼用」。
    """
    text = "".join(ch for ch in str(raw or "") if ch.isprintable() and ch not in "\r\n")
    text = " ".join(text.split())
    text = text.strip()[:NICKNAME_MAX_CHARS]
    return text or NICKNAME_FALLBACK


def festival_batch_key(festival_name: str, date_str: str, adapter_key: str, instance_key: str) -> str:
    """批次键：`<节日名前 32 字>:<日期>:<实例对哈希前 12 位>`，总长 ≤ 64

    后台按它暂停 / 作废一批；实例对用哈希是为了「同一个节日名 + 同一个日期在不同实例下是两批」
    且不必把实例标识原样拼进去（节日名是平台方配置的，可能带冒号）。
    """
    digest = hashlib.sha256(f"{adapter_key}:{instance_key}".encode("utf-8")).hexdigest()[:12]
    return f"{str(festival_name or '')[:32]}:{str(date_str or '')}:{digest}"[:64]


def _local_day_start(moment: Optional[datetime] = None) -> datetime:
    """本地时区当天零点

    「今天已经给这个地址发过」按**本地**日切：节日是本地日历上的事，用 UTC 切会在东八区
    把「今天早上发的」算成昨天，于是同一天发两封。
    """
    current = moment if moment is not None else db_now()
    local = current.astimezone() if current.tzinfo is not None else current
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


async def _sent_today(adapter_key: str, instance_key: str, to_address: str, *, now: Optional[datetime] = None) -> bool:
    """今天这个实例有没有给这个地址成功发过节日邮件（入队与发出前共用同一条判据）"""
    target = mail.normalize_username(to_address)
    if not target:
        return False
    rows = await DBEmailSendLog.filter(
        adapter_key=adapter_key,
        instance_key=instance_key,
        purpose=mail.PURPOSE_FESTIVAL,
        ok=True,
        create_time__gte=_local_day_start(now),
    ).values_list("to_address", flat=True)
    return any(mail.normalize_username(address) == target for address in rows)


def reset_outbox_state() -> None:
    """清空进程内状态（测试用）：发信进度与清理节流"""
    global _last_purge_at
    _last_sent_at.clear()
    _last_purge_at = None


def set_clock(clock: Optional[Callable[[], float]]) -> None:
    """替换匀速间隔用的时钟（测试用）；传 None 恢复默认"""
    global _clock
    _clock = clock or time.monotonic


# ===== 名单 =====


async def _private_channels(adapter_key: str, instance_key: str) -> List[DBChatChannel]:
    """这个实例里开着的私聊频道

    停用的频道不算：平台方 / 租户停掉了这个会话，就是不想让 Bot 再往那里说话，
    「开过私聊」不该因此变成「还愿意收节日邮件」。
    """
    return list(
        await DBChatChannel.filter(
            adapter_key=adapter_key,
            instance_key=instance_key,
            channel_type="private",
            is_active=True,
        ).order_by("id"),
    )


async def _counterpart_of_channel(channel: DBChatChannel) -> str:
    """私聊频道的对方账号；取不到返回空串

    `onebot_v11` 的 `private_<QQ>` 是确定的，用运维策略里那一个解析函数（同一处口径）；
    其他适配器的对方标识可能由调用方填写，只能取这个频道里最近一条非 Bot 消息的发送者。
    """
    from nekro_agent.services.ops_agent.policy import private_counterpart_from_channel

    if channel.adapter_key == "onebot_v11":
        return private_counterpart_from_channel(channel.adapter_key, channel.channel_id)
    message = (
        await DBChatMessage.filter(
            chat_key=channel.chat_key,
            adapter_key=channel.adapter_key,
            instance_key=channel.instance_key,
        )
        .exclude(sender_id="-1")
        .order_by("-send_timestamp", "-id")
        .first()
    )
    return str(message.platform_userid or "").strip() if message is not None else ""


async def _nickname_of(adapter_key: str, instance_key: str, platform_userid: str) -> str:
    """这个人在本实例最近一条消息里的显示昵称（取不到由 `clean_nickname` 兜底）"""
    if not platform_userid:
        return ""
    message = (
        await DBChatMessage.filter(
            adapter_key=adapter_key,
            instance_key=instance_key,
            platform_userid=platform_userid,
        )
        .order_by("-send_timestamp", "-id")
        .first()
    )
    return str(message.sender_nickname or "") if message is not None else ""


async def festival_recipients(adapter_key: str, instance_key: str) -> List[FestivalRecipient]:
    """这个实例这个节日的收件人名单

    并集（按账号）：
    - **开过私聊**：这个实例里 `channel_type` 为私聊、`is_active` 为真的频道；对方账号取得到，
      且 `festival_greeting` 与 `email_utils` 在**这个私聊频道**上都生效；
    - **群里订阅过**：偏好行 `festival_subscribed` 为真。

    再减掉偏好行 `unsubscribed` 为真的；地址优先用偏好行里的，没有就用默认地址（只有
    `onebot_v11` 的 QQ 号能拼出来）；按地址（小写）去重。
    """
    prefs: Dict[str, DBEmailUserPref] = {}
    for row in await DBEmailUserPref.filter(adapter_key=adapter_key, instance_key=instance_key):
        prefs[row.platform_userid] = row

    chosen: Dict[str, str] = {}  # platform_userid -> source
    for channel in await _private_channels(adapter_key, instance_key):
        counterpart = await _counterpart_of_channel(channel)
        if not counterpart:
            continue
        # 两个插件都必须在这个私聊频道上生效：只开了节日祝福、没开邮箱工具时发不了信；
        # 只开了邮箱工具、没开节日祝福时不该收到节日邮件
        if not plugin_effective_with_instance(FESTIVAL_PLUGIN_KEY, instance_key, channel.chat_key):
            continue
        if not plugin_effective_with_instance(EMAIL_UTILS_PLUGIN_KEY, instance_key, channel.chat_key):
            continue
        chosen[counterpart] = "private"

    for userid, pref in prefs.items():
        if not pref.festival_subscribed or not userid:
            continue
        # 两类都是时记 private：理由文案要写「在本实例开过私聊」这一条更准确的那个
        chosen.setdefault(userid, "subscribed")

    recipients: List[FestivalRecipient] = []
    seen_addresses: set[str] = set()
    for userid, source in chosen.items():
        pref = prefs.get(userid)
        if pref is not None and pref.unsubscribed:
            continue
        address = str(pref.email or "").strip() if pref is not None else ""
        if not address:
            address = mail.default_festival_address(adapter_key, userid)
        if not address:
            continue
        key = mail.normalize_username(address)
        if not key or key in seen_addresses:
            continue
        seen_addresses.add(key)
        recipients.append(
            FestivalRecipient(
                platform_userid=userid,
                to_address=address,
                nickname=clean_nickname(await _nickname_of(adapter_key, instance_key, userid)),
                source=source,
            ),
        )
    return recipients


async def instance_festival_mail_ready(adapter_key: str, instance_key: str) -> Optional["AccountView"]:
    """这个实例现在能不能发节日邮件；能就返回发件人，否则 None

    四道判据（缺一条就整批跳过，不半批发）：
    1. 实例当前可用（停用 / 归档 / 租户停用都不发）；
    2. 两个插件在实例上都生效（只开了节日祝福、没开邮箱工具时发不了信）；
    3. `EMAIL_FESTIVAL_MAIL_ENABLED` 开着；
    4. 这个实例有能用的默认发件人。
    """
    if not adapter_key or not instance_key:
        return None
    if not await DBAdapterInstance.is_instance_active(adapter_key, instance_key):
        return None
    if not plugin_effective_on_instance(FESTIVAL_PLUGIN_KEY, adapter_key, instance_key):
        return None
    if not plugin_effective_on_instance(EMAIL_UTILS_PLUGIN_KEY, adapter_key, instance_key):
        return None
    if not bool(core_config.EMAIL_FESTIVAL_MAIL_ENABLED):
        return None
    instance = (
        await DBAdapterInstance.filter(adapter_key=adapter_key, instance_key=instance_key).order_by("id").first()
    )
    if instance is None:
        return None
    return await mail.default_sender_for_instance(
        adapter_key,
        instance_key,
        is_platform_instance=instance.tenant_id is None,
    )


# ===== 入队 =====


def _reason_for(source: str, festival_name: str) -> str:
    where = "收件人在本实例开过私聊" if source == "private" else "收件人在群里订阅过"
    return f"节日祝福：{festival_name}；{where}"


async def enqueue_festival_batch(
    *,
    adapter_key: str,
    instance_key: str,
    festival_name: str,
    date_str: str,
    sender: "AccountView",
    recipients: List[FestivalRecipient],
    subject: str,
    body: str,
) -> int:
    """把一批收件人写进队列，返回入队的行数

    去重两条：
    - 同一个 `batch_key` 下已有同一地址的行（任何状态）不再入队——重复调用入队（插件被触发
      两次、进程重启后又跑了一次）不该让同一个人收到两封；
    - 今天这个实例已经成功给这个地址发过节日邮件，也不再入队（与发出前的判据同一条）。
    """
    if not recipients:
        return 0
    batch_key = festival_batch_key(festival_name, date_str, adapter_key, instance_key)
    existing = {
        mail.normalize_username(address)
        for address in await DBEmailOutbox.filter(batch_key=batch_key).values_list("to_address", flat=True)
    }
    now = db_now()
    queued = 0
    for recipient in recipients:
        key = mail.normalize_username(recipient.to_address)
        if not key or key in existing:
            continue
        if await _sent_today(adapter_key, instance_key, recipient.to_address):
            continue
        await DBEmailOutbox.create(
            batch_key=batch_key,
            adapter_key=adapter_key,
            instance_key=instance_key,
            platform_userid=recipient.platform_userid,
            account_kind=sender.kind,
            account_ref=mail.account_send_log_ref(sender),
            to_address=recipient.to_address,
            subject=_clip(subject, 256),
            body=f"{recipient.nickname}：\n\n{body}",
            reason=_reason_for(recipient.source, festival_name),
            status=STATUS_QUEUED,
            attempts=0,
            next_at=now,
        )
        existing.add(key)
        queued += 1
    return queued


# ===== 发信工人 =====


async def _skip(row: DBEmailOutbox, reason: str) -> None:
    """把一行记成跳过并清空正文（不写发信记录：这一封没有发出去）"""
    logger.debug(f"节日邮件跳过: id={row.id}, to={row.to_address} | {reason}")
    row.status = STATUS_SKIPPED
    row.body = ""
    row.next_at = None
    await row.save(update_fields=["status", "body", "next_at", "update_time"])


async def _still_sendable(row: DBEmailOutbox) -> tuple:
    """发出前的判据；返回 `(能不能发, 不能发的原因, 发件账户视图)`

    任何一条不过都记 `skipped`、**不换发件人**：换一个发件人发出去等于绕过了平台方
    「这个邮箱不给这个实例用」的决定。
    """
    if not bool(core_config.EMAIL_FESTIVAL_MAIL_ENABLED):
        return False, "总闸关着", None
    if not await DBAdapterInstance.is_instance_active(row.adapter_key, row.instance_key):
        return False, "实例当前不可用", None
    if not plugin_effective_on_instance(FESTIVAL_PLUGIN_KEY, row.adapter_key, row.instance_key):
        return False, "节日祝福插件在这个实例上不生效", None
    if not plugin_effective_on_instance(EMAIL_UTILS_PLUGIN_KEY, row.adapter_key, row.instance_key):
        return False, "邮箱工具插件在这个实例上不生效", None

    pref = await DBEmailUserPref.get_or_none(
        adapter_key=row.adapter_key,
        instance_key=row.instance_key,
        platform_userid=row.platform_userid,
    )
    if pref is not None and pref.unsubscribed:
        return False, "收件人退订了", None
    if await _sent_today(row.adapter_key, row.instance_key, row.to_address):
        return False, "今天已经给这个地址发过", None

    instance = (
        await DBAdapterInstance.filter(adapter_key=row.adapter_key, instance_key=row.instance_key).order_by("id").first()
    )
    if instance is None:
        return False, "实例行不存在", None
    accounts = await mail.instance_accounts(
        row.adapter_key,
        row.instance_key,
        is_platform_instance=instance.tenant_id is None,
    )
    sender: Optional["AccountView"] = None
    for view in accounts.sendable:
        # 认旧版引用：升级前排进队列的平台方邮件存的是从 JWT 密钥派生的那一版
        if view.kind == row.account_kind and mail.send_log_ref_matches(view, row.account_ref):
            sender = view
            break
    if sender is None:
        return False, "发件账户已不可用（共享取消、账户删除或关了发信）", None
    return True, "", sender


def _interval_seconds() -> float:
    try:
        value = int(core_config.EMAIL_OUTBOX_INTERVAL_SECONDS)
    except (TypeError, ValueError):
        value = 5
    return float(max(1, min(600, value)))


async def _pick_due_rows(now: datetime) -> List[DBEmailOutbox]:
    """到点的 `queued` 行，按 id 升序；同一个发件账户只取第一封

    `next_at` 为空表示立即可发（模型上的说明就是这么写的），要和「已经到点」一起取：
    只按 `next_at <= now` 查，空值那几行会永远留在队列里。
    """
    from tortoise.expressions import Q

    rows = await DBEmailOutbox.filter(
        Q(next_at__isnull=True) | Q(next_at__lte=now),
        status=STATUS_QUEUED,
    ).order_by("id")
    picked: List[DBEmailOutbox] = []
    seen: set[tuple] = set()
    for row in rows:
        key = (row.account_kind, row.account_ref)
        if key in seen:
            continue
        seen.add(key)
        picked.append(row)
    return picked


async def _send_one(row: DBEmailOutbox, sender: "AccountView", now: datetime) -> None:
    """发一封；成功落 sent，失败按次数重试或落 failed"""
    try:
        await mail.send_via_account(
            sender,
            to_address=row.to_address,
            subject=row.subject,
            content=row.body,
        )
    except Exception as e:  # noqa: BLE001 - 失败要落状态，不能把工人打掉
        attempts = int(row.attempts or 0) + 1
        error_text = f"{type(e).__name__}: {e}"
        row.attempts = attempts
        if attempts >= MAX_ATTEMPTS:
            row.status = STATUS_FAILED
            row.body = ""
            row.next_at = None
            logger.warning(f"节日邮件最终失败: id={row.id}, attempts={attempts} | {error_text}")
            await row.save(update_fields=["attempts", "status", "body", "next_at", "update_time"])
            await mail.record_send(
                account=sender,
                adapter_key=row.adapter_key,
                instance_key=row.instance_key,
                chat_key="",
                tenant_id=await _tenant_of(row.adapter_key, row.instance_key),
                purpose=mail.PURPOSE_FESTIVAL,
                to_address=row.to_address,
                subject=row.subject,
                reason=row.reason,
                ok=False,
                # 实例自有邮箱的记录租户看得到：只存归类后的一句（同 `email_utils.send_email`），原文在上面的日志里
                error=mail.public_mail_failure(e) if sender.kind == mail.KIND_INSTANCE else error_text,
            )
            return
        delay = RETRY_DELAYS_SECONDS[min(attempts, len(RETRY_DELAYS_SECONDS)) - 1]
        row.next_at = now + timedelta(seconds=delay)
        logger.warning(f"节日邮件发送失败，{delay} 秒后重试: id={row.id}, attempts={attempts} | {error_text}")
        await row.save(update_fields=["attempts", "next_at", "update_time"])
        return

    row.status = STATUS_SENT
    row.body = ""
    row.next_at = None
    await row.save(update_fields=["status", "body", "next_at", "update_time"])
    await mail.record_send(
        account=sender,
        adapter_key=row.adapter_key,
        instance_key=row.instance_key,
        chat_key="",
        tenant_id=await _tenant_of(row.adapter_key, row.instance_key),
        purpose=mail.PURPOSE_FESTIVAL,
        to_address=row.to_address,
        subject=row.subject,
        reason=row.reason,
        ok=True,
    )


async def _tenant_of(adapter_key: str, instance_key: str) -> Optional[int]:
    """实例当前的租户（发信记录的 `tenant_id` 是快照，取发信那一刻的值）"""
    instance = await DBAdapterInstance.filter(adapter_key=adapter_key, instance_key=instance_key).order_by("id").first()
    return instance.tenant_id if instance is not None else None


async def _purge_finished(*, now: Optional[datetime] = None) -> None:
    """删掉 7 天前就结束的行；进程内每 24 小时最多一次"""
    global _last_purge_at
    moment = _clock()
    if _last_purge_at is not None and moment - _last_purge_at < OUTBOX_PURGE_INTERVAL_SECONDS:
        return
    # 先记时刻再删：删除失败时不必让下一次处理再撞一遍同一个失败
    _last_purge_at = moment
    cutoff = (now if now is not None else db_now()) - timedelta(days=OUTBOX_KEEP_DAYS)
    try:
        await DBEmailOutbox.filter(
            status__in=[STATUS_SENT, STATUS_FAILED, STATUS_SKIPPED],
            update_time__lt=cutoff,
        ).delete()
    except Exception as e:  # noqa: BLE001 - 清理失败不能反过来影响发信
        logger.warning(f"清理过期节日邮件队列行失败: {type(e).__name__}")


async def process_due_outbox(*, now: Optional[datetime] = None) -> int:
    """处理一轮到期的行，返回处理数（发出去、跳过、失败都算处理过）

    「匀速」按发件账户算：同一个发件邮箱两封之间至少隔 `EMAIL_OUTBOX_INTERVAL_SECONDS`，
    不同发件邮箱各走各的；一轮里每个发件邮箱最多发一封。
    """
    moment = now if now is not None else db_now()
    interval = _interval_seconds()
    handled = 0
    for row in await _pick_due_rows(moment):
        key = (row.account_kind, row.account_ref)
        last = _last_sent_at.get(key)
        if last is not None and _clock() - last < interval:
            continue
        sendable, reason, sender = await _still_sendable(row)
        if not sendable or sender is None:
            await _skip(row, reason)
            handled += 1
            continue
        await _send_one(row, sender, moment)
        _last_sent_at[key] = _clock()
        handled += 1
    if handled:
        await _purge_finished(now=now)
    return handled


# ===== 常驻任务 =====

_worker_task: Optional["asyncio.Task[None]"] = None
_worker_running = False


def start_outbox_worker() -> None:
    """起常驻发信工人（应用启动时调一次；重复调用是空操作）"""
    global _worker_task, _worker_running
    if _worker_running:
        return
    _worker_running = True
    _worker_task = asyncio.create_task(_worker_loop())


async def stop_outbox_worker() -> None:
    """停掉常驻发信工人（应用关闭时调）"""
    global _worker_task, _worker_running
    _worker_running = False
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None


async def _worker_loop() -> None:
    # 先等一会儿：启动时迁移、插件加载、适配器连接都在抢磁盘与网络
    await asyncio.sleep(WORKER_STARTUP_DELAY_SECONDS)
    while _worker_running:
        try:
            await process_due_outbox()
        except Exception as exc:  # noqa: BLE001 - 一轮失败不能让循环退出，明天也不会自己好
            logger.warning(f"节日邮件发信工人这一轮出错（下一秒再试）: {type(exc).__name__}: {exc}")
        await asyncio.sleep(WORKER_TICK_SECONDS)


# ===== 队列管理（后台接口用） =====


async def _batch_rows(adapter_key: str, instance_key: str, batch_key: str) -> List[DBEmailOutbox]:
    return list(
        await DBEmailOutbox.filter(
            adapter_key=adapter_key,
            instance_key=instance_key,
            batch_key=batch_key,
        ),
    )


async def pause_batch(adapter_key: str, instance_key: str, batch_key: str) -> int:
    """暂停一批：`queued` 改成 `paused`，返回改了几行（`update_time` 见 `void_batch`）"""
    return int(
        await DBEmailOutbox.filter(
            adapter_key=adapter_key,
            instance_key=instance_key,
            batch_key=batch_key,
            status=STATUS_QUEUED,
        ).update(status=STATUS_PAUSED, update_time=db_now()),
    )


async def resume_batch(adapter_key: str, instance_key: str, batch_key: str) -> int:
    """恢复一批：`paused` 改回 `queued`，返回改了几行（`update_time` 见 `void_batch`）"""
    return int(
        await DBEmailOutbox.filter(
            adapter_key=adapter_key,
            instance_key=instance_key,
            batch_key=batch_key,
            status=STATUS_PAUSED,
        ).update(status=STATUS_QUEUED, update_time=db_now()),
    )


async def void_batch(adapter_key: str, instance_key: str, batch_key: str) -> int:
    """作废一批：`queued` / `paused` 改成 `skipped` 并清空正文，返回改了几行

    `update_time` 要一起写上：查询集的 `update()` 不触发 `auto_now`，不写的话这一行会带着
    入队时刻的时间戳，清理时按「结束时刻」算就会提前被删掉。
    """
    return int(
        await DBEmailOutbox.filter(
            adapter_key=adapter_key,
            instance_key=instance_key,
            batch_key=batch_key,
            status__in=[STATUS_QUEUED, STATUS_PAUSED],
        ).update(status=STATUS_SKIPPED, body="", next_at=None, update_time=db_now()),
    )


def _split_batch_key(batch_key: str) -> tuple:
    """把批次键拆回 `(节日名, 日期)`；拆不出来时整串当节日名"""
    parts = str(batch_key or "").split(":")
    if len(parts) >= 3:
        return ":".join(parts[:-2]), parts[-2]
    return str(batch_key or ""), ""


async def list_batches(adapter_key: str, instance_key: str) -> List[Dict[str, Any]]:
    """这个实例的各批：批次键、节日名、日期、各状态计数、最早入队时间

    计数在 Python 里数而不是用 `group by`：一个实例的队列是几十到几百行，一次取回来
    比按状态分组再拼更简单，也不会因为多一条状态就少一行。
    """
    rows = await DBEmailOutbox.filter(adapter_key=adapter_key, instance_key=instance_key).order_by("id")
    batches: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        entry = batches.get(row.batch_key)
        if entry is None:
            name, date_str = _split_batch_key(row.batch_key)
            entry = {
                "batch_key": row.batch_key,
                "festival_name": name,
                "date": date_str,
                "counts": {status: 0 for status in (STATUS_QUEUED, STATUS_PAUSED, STATUS_SENT, STATUS_FAILED, STATUS_SKIPPED)},
                "first_queued_at": row.create_time,
            }
            batches[row.batch_key] = entry
        counts = entry["counts"]
        counts[row.status] = int(counts.get(row.status, 0)) + 1
        if entry["first_queued_at"] is None or row.create_time < entry["first_queued_at"]:
            entry["first_queued_at"] = row.create_time
    return list(batches.values())


__all__ = [
    "EMAIL_UTILS_PLUGIN_KEY",
    "FESTIVAL_PLUGIN_KEY",
    "MAX_ATTEMPTS",
    "NICKNAME_FALLBACK",
    "NICKNAME_MAX_CHARS",
    "OUTBOX_KEEP_DAYS",
    "RETRY_DELAYS_SECONDS",
    "STATUS_FAILED",
    "STATUS_PAUSED",
    "STATUS_QUEUED",
    "STATUS_SENT",
    "STATUS_SKIPPED",
    "FestivalRecipient",
    "clean_nickname",
    "enqueue_festival_batch",
    "festival_batch_key",
    "festival_recipients",
    "instance_festival_mail_ready",
    "list_batches",
    "pause_batch",
    "process_due_outbox",
    "reset_outbox_state",
    "resume_batch",
    "set_clock",
    "start_outbox_worker",
    "stop_outbox_worker",
    "void_batch",
]
