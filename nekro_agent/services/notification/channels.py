"""通知渠道抽象与内置实现

## 为什么需要这一层

告警的产生方（实例健康监测、后续的配额/异常告警）只关心"发一条通知"，
不应该关心 SMTP 参数、Webhook 地址是否安全、某个渠道是否配置齐全。
本模块把这些差异收敛到 `NotificationChannel` 协议后面，调用方只用
`dispatch_notification(title, body, level)`。

## 告警要发给**被告警的那一方**

`INSTANCE_ALERT_WEBHOOK_URL` 曾经是一个单值全局配置，于是「客户 A 的 Bot 掉线」只会发到
平台方那一个收件端，定制版客户自己收不到——而他又没有任何自助运维入口，体验就是
「Bot 挂了，等平台方发现」。现在这一项标了 `overridable`，租户能在自己的实例作用域上
配自己的 Webhook，`dispatch_notification()` 按 `(adapter_key, instance_key)` 解析出该实例
上真正生效的那条地址（实例 > 适配器 > 全局，见 `config_resolver.get_instance_effective_config`）。

**邮件与运维频道两条通道刻意不跟随实例**：邮件收件人来自 Email 适配器的适配器层配置
（跨租户共享的作用域），运维 Bot 按 DECISIONS 是平台方专属、租户不能有自己的。
两者恒定走全局配置，因此平台方原本收到的每一条告警一条不少。

## 安全约束

`WebhookChannel` 的目标地址完全由用户在 WebUI 中填写，等价于一个 SSRF 原语：
若不校验，攻击者（或误操作的管理员）可以让服务端主动 POST 到内网元数据服务、
本机管理端口等。因此每次发送前都要校验，并禁用重定向跟随，避免 302 到内网绕过校验。

校验分两档，判据是**这条地址是谁填的**：

- 全局层那条只有平台方写得进（`system` 配置对租户是 403），沿用
  `assert_download_url_safe()` —— 它认 `ALLOW_PRIVATE_NETWORK_DOWNLOAD`，
  自建部署把告警发到内网的收件器是正当用法，行为逐字不变；
- **实例层**那条租户写得进，走 `assert_tenant_endpoint_url_safe()` ——
  与模型组的 `assert_outbound_urls_allowed` 同一条判据，**不认**那个放行开关。
  写入侧已经拦过一道（`routers/config.py:_assert_tenant_may_write`），这里是第二道：
  写入时公网、发送时经 DNS 变成内网（rebinding），或者放行开关在写入之后才被打开，
  都只有发送前这一次校验能挡住。

"这条地址写在哪一层"由 `alert_webhook_is_instance_owned()` 问解析器要，
**不是**拿最终值与全局值做字符串比较：两者取值相同时比较法会把一次真实的实例层覆盖判成
"来自全局"，于是租户填的地址反过来拿到宽松那一档；而平台方写在适配器层的内网地址
又会被误判成租户的、按严格档拒发。这类误判两个方向都没有任何运行时信号。

## 试发通知的扇出面

邮件与运维频道的收件端都取自全局配置，是**平台方的**。租户自己发起的试发因此走
`scoped_only`：只发他自己实例层上配的那条 Webhook。否则租户手上就多出一个
「把标题正文任意的消息投递到平台方邮箱与运维群、且带着 NA 自己的发信身份」的原语。
真实告警不带这个开关，平台方原本收到的每一条一条不少。
"""

from __future__ import annotations

import asyncio
import time
from email.message import EmailMessage
from typing import Dict, List, Literal, Optional, Protocol

import httpx

from nekro_agent.core.config import CoreConfig, config
from nekro_agent.core.logger import get_sub_logger

# 邮件渠道刻意不重新实现 SMTP：发信开关、发信账号、收件人列表都以邮件服务的
# 运行时结算结果为准（它会优先取 Email 适配器配置，再回退到旧版全局配置）。
from nekro_agent.services.mail.mail_service import _get_runtime_mail_settings, send_email
from nekro_agent.tools.common_util import (
    UnsafeDownloadUrlError,
    assert_download_url_safe,
    assert_tenant_endpoint_url_safe,
)

logger = get_sub_logger("notification")

NotificationLevel = Literal["info", "warning", "error"]

WEBHOOK_TIMEOUT_SECONDS: float = 10.0


class NotificationChannel(Protocol):
    """通知渠道协议

    Attributes:
        name: 渠道标识，用于日志与 API 回显
    """

    name: str

    async def send(self, title: str, body: str, level: str) -> bool:
        """投递一条通知

        实现方不得向上抛出异常，投递失败以返回 False 表示。

        Args:
            title: 通知标题
            body: 通知正文（纯文本）
            level: 通知级别，取值见 `NotificationLevel`

        Returns:
            bool: 是否成功投递
        """
        ...


class EmailChannel:
    """邮件通知渠道

    复用 `services/mail/mail_service.py` 的 SMTP 能力，不自行建立连接。
    返回 True 表示已成功交由邮件服务投递；SMTP 层的失败由邮件服务内部记录日志，
    这里无法区分，故不作为失败上报。
    """

    name: str = "email"

    async def send(self, title: str, body: str, level: str) -> bool:
        settings = _get_runtime_mail_settings()
        if not settings["enabled"]:
            logger.debug("邮件通知未启用，跳过发送")
            return False

        username = str(settings["username"] or "").strip()
        if not username:
            logger.warning("邮件通知未配置发信账号，跳过发送")
            return False

        raw_targets = settings["targets"] or []
        targets: List[str] = [str(item).strip() for item in raw_targets if str(item).strip()]

        message = EmailMessage()
        message["From"] = username
        message["To"] = ", ".join(targets) if targets else username
        message["Subject"] = f"[Nekro Agent][{level.upper()}] {title}"
        message.set_content(body, subtype="plain")

        await send_email(message)
        return True


class WebhookChannel:
    """Webhook 通知渠道，POST 一个 JSON 载荷到用户配置的地址

    Attributes:
        url: 目标地址
        tenant_writable: 这条地址是否可能由租户填写（来自实例/适配器/频道覆盖层）。
            为真时用不认 `ALLOW_PRIVATE_NETWORK_DOWNLOAD` 的严格判据，理由见模块 docstring。
    """

    name: str = "webhook"

    def __init__(self, url: str, *, tenant_writable: bool = False) -> None:
        self.url: str = url
        self.tenant_writable: bool = tenant_writable

    async def send(self, title: str, body: str, level: str) -> bool:
        guard = assert_tenant_endpoint_url_safe if self.tenant_writable else assert_download_url_safe
        try:
            await guard(self.url)
        except UnsafeDownloadUrlError as e:
            logger.warning(f"Webhook 通知目标地址未通过安全校验，已拒绝发送: {e}")
            return False

        payload: Dict[str, object] = {
            "source": "nekro-agent",
            "title": title,
            "body": body,
            "level": level,
            "timestamp": int(time.time() * 1000),
        }

        try:
            # follow_redirects=False：跟随重定向会让上面的公网 IP 校验形同虚设
            async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS, follow_redirects=False) as client:
                response = await client.post(self.url, json=payload)
        except httpx.HTTPError as e:
            logger.warning(f"Webhook 通知发送失败: {e}")
            return False

        if response.status_code >= 400:
            logger.warning(f"Webhook 通知返回异常状态码: {response.status_code}")
            return False
        return True


class OpsBotChannel:
    """运维频道通知渠道

    把告警直接发到运维 Bot 所在的频道，作为邮件 / Webhook 之外的第三条通道。

    这条通道**只出站不入站**：它不接收任何指令，因此不扩大运维通道的攻击面。
    目标频道取自全局配置（`OPS_BOT_INSTANCE_KEY` / `OPS_BOT_CHANNEL_ID`），
    与指令受理用的是同一份判定源，不会出现"指令认 A 群、告警发 B 群"。

    发送前先落一行频道记录：出站链路要求频道已存在，而运维群很可能从没人说过话
    （它就是个收告警的群）。不建这一行的话，配置完成后告警会一直静默失败，
    而"告警通道自己不响"恰恰是最不该发生的一种故障。
    """

    name: str = "ops_bot"

    def __init__(self, adapter_key: str, instance_key: str, channel_id: str, chat_key: str) -> None:
        self.adapter_key: str = adapter_key
        self.instance_key: str = instance_key
        self.channel_id: str = channel_id
        self.chat_key: str = chat_key

    async def send(self, title: str, body: str, level: str) -> bool:
        from nekro_agent.models.db_chat_channel import DBChatChannel
        from nekro_agent.schemas.chat_message import ChatType
        from nekro_agent.services.chat.universal_chat_service import universal_chat_service

        try:
            await DBChatChannel.get_or_create(
                adapter_key=self.adapter_key,
                channel_id=self.channel_id,
                channel_type=ChatType.PRIVATE if self.channel_id.startswith("private_") else ChatType.GROUP,
                chat_key=self.chat_key,
                instance_key=self.instance_key,
            )
            await universal_chat_service.send_operation_message(
                chat_key=self.chat_key,
                message=f"[{level.upper()}] {title}\n{body}",
            )
        except Exception as e:
            # 运维频道多半就是"用来发现故障"的那条通道，它自己挂掉时
            # 绝不能连累邮件/Webhook，因此这里必须吞掉异常只返回 False
            logger.warning(f"运维频道通知发送失败: {e}")
            return False
        return True


def resolve_alert_config(adapter_key: str = "", instance_key: str = "") -> CoreConfig:
    """取某个实例上生效的告警配置；未指定实例时就是全局配置

    实例标识为空是**平台方视角的"全局"**（`/health/test-notification` 不带实例时走这里），
    不是"随便挑一个实例"。租户身份没有这一档，路由层已经把它挡在外面。
    """
    if not adapter_key or not instance_key:
        return config

    from nekro_agent.services.config_resolver import config_resolver

    try:
        return config_resolver.get_instance_effective_config(adapter_key, instance_key)
    except Exception as e:
        # 解析失败不能让告警整条哑掉：回落全局配置至少还能发到平台方
        logger.warning(f"解析实例 {adapter_key}/{instance_key} 的告警配置失败，回落全局配置: {e}")
        return config


def alert_webhook_is_instance_owned(adapter_key: str = "", instance_key: str = "") -> bool:
    """这条实例上生效的 Webhook 地址，是不是写在**实例层**上的

    是 ⇒ 它归这个实例的主人所有（租户写得进），因此
    (1) 出网校验用不认 `ALLOW_PRIVATE_NETWORK_DOWNLOAD` 的严格那一档，
    (2) 租户自己发起的试发只允许打到它。

    判据取自 `config_resolver.instance_field_source()`，**不**拿最终值与全局值做比较：
    两者相等时比较法会把一次真实的实例层覆盖判成"来自全局"，于是租户填的地址反而拿到
    平台方那一档的宽松出网判据 —— 一条只在取值撞车时才出现、且完全没有信号的漏判。
    适配器层同理不算：那是跨租户共享的作用域，只有平台方写得进。
    """
    if not adapter_key or not instance_key:
        return False

    from nekro_agent.services.config_resolver import ConfigLayer, config_resolver

    try:
        source = config_resolver.instance_field_source(adapter_key, instance_key, "INSTANCE_ALERT_WEBHOOK_URL")
    except Exception as e:
        # 判不出来源时按"不是实例层"处理：严格档只是更严，而放宽是一次安全判据的降级
        logger.warning(f"解析实例 {adapter_key}/{instance_key} 的告警 Webhook 来源失败，按全局层处理: {e}")
        return False
    return source is ConfigLayer.INSTANCE


def build_enabled_channels(
    alert_config: Optional[CoreConfig] = None,
    *,
    webhook_is_instance_owned: bool = False,
    scoped_only: bool = False,
) -> List[NotificationChannel]:
    """按当前配置构建启用的通知渠道列表

    邮件渠道恒定在列表中，其是否真正发送由邮件服务的运行时开关决定；
    Webhook 渠道仅在配置了地址时加入；运维频道渠道需要运维 Bot 已启用且配置完整。

    Args:
        alert_config: 该实例上生效的配置（见 `resolve_alert_config`）。传 None 表示全局配置。
            **只有 Webhook 这一条通道跟随它**，邮件与运维频道恒定读全局，理由见模块 docstring。
        webhook_is_instance_owned: 这条 Webhook 地址是否写在实例层上（见
            `alert_webhook_is_instance_owned`）。为真时按严格判据校验出网目标。
        scoped_only: 只保留**属于这个实例的主人**的通道。租户自己发起的试发走这一档：
            邮件收件人与运维频道都是平台方的，让租户拿任意标题正文去敲它们等于凭空造出
            一个进平台方收件端的投递口（见 `routers/console/health.py:send_test_notification`）。
            真实告警不传这个开关，平台方原本收到的每一条一条不少。

    Returns:
        List[NotificationChannel]: 渠道实例列表
    """
    scoped = alert_config or config
    channels: List[NotificationChannel] = [] if scoped_only else [EmailChannel()]

    webhook_url = scoped.INSTANCE_ALERT_WEBHOOK_URL.strip()
    # `scoped_only` 下只认实例层那条：解析结果回落到全局/适配器层时，那条地址是平台方的
    if webhook_url and (webhook_is_instance_owned or not scoped_only):
        channels.append(WebhookChannel(webhook_url, tenant_writable=webhook_is_instance_owned))

    if scoped_only:
        return channels

    from nekro_agent.services.ops_bot.policy import OpsBotPolicy

    ops_policy = OpsBotPolicy.from_config(config)
    if config.OPS_BOT_ALERT_ENABLED and ops_policy.enabled and ops_policy.is_configured:
        channels.append(
            OpsBotChannel(
                adapter_key=ops_policy.adapter_key,
                instance_key=ops_policy.instance_key,
                channel_id=ops_policy.channel_id,
                chat_key=ops_policy.ops_chat_key,
            ),
        )
    return channels


async def dispatch_notification(
    title: str,
    body: str,
    level: NotificationLevel = "info",
    *,
    adapter_key: str = "",
    instance_key: str = "",
    scoped_only: bool = False,
) -> Dict[str, bool]:
    """向所有启用的渠道并发投递一条通知

    单个渠道失败或抛异常不影响其他渠道。

    Args:
        title: 通知标题
        body: 通知正文（纯文本）
        level: 通知级别
        adapter_key: 告警所属实例的适配器标识；与 `instance_key` 同时给出时按该实例解析告警配置
        instance_key: 告警所属实例标识
        scoped_only: 只投递归该实例主人所有的通道，语义见 `build_enabled_channels`

    Returns:
        Dict[str, bool]: 渠道名 -> 是否投递成功
    """
    channels = build_enabled_channels(
        resolve_alert_config(adapter_key, instance_key),
        webhook_is_instance_owned=alert_webhook_is_instance_owned(adapter_key, instance_key),
        scoped_only=scoped_only,
    )
    results = await asyncio.gather(
        *(channel.send(title, body, level) for channel in channels),
        return_exceptions=True,
    )

    outcome: Dict[str, bool] = {}
    for channel, result in zip(channels, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning(f"通知渠道 {channel.name} 投递异常: {result}")
            outcome[channel.name] = False
            continue
        outcome[channel.name] = result
    return outcome
