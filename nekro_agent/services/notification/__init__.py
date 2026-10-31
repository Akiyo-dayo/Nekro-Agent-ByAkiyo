"""通知服务

对外统一从本包导出通知渠道抽象与调度入口，调用方无需感知具体渠道实现。
"""

from .channels import (
    WEBHOOK_TIMEOUT_SECONDS,
    EmailChannel,
    NotificationChannel,
    NotificationLevel,
    WebhookChannel,
    alert_webhook_is_instance_owned,
    build_enabled_channels,
    dispatch_notification,
    resolve_alert_config,
)

__all__ = [
    "WEBHOOK_TIMEOUT_SECONDS",
    "EmailChannel",
    "NotificationChannel",
    "NotificationLevel",
    "WebhookChannel",
    "alert_webhook_is_instance_owned",
    "build_enabled_channels",
    "dispatch_notification",
    "resolve_alert_config",
]
