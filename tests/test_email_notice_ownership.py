"""通知只推到「这个账户自己那一侧的频道」

## 这条要防的是什么

适配器层的通知频道原来是一个配置键。配错了不会报错，只会把邮件的发件人、主题、附件名
推进别人的群；而实例自有邮箱加进来之后，这个口子多了一条更要命的路径：实例的信按平台方
那个键推出去，等于租户的私人邮件进了平台方的会话。

两条路径的判据不同，所以分两组钉：

- **平台方账户**：频道要能解析出归属，且归属是平台方；`email_utils` 要在那个频道生效；
- **实例自有账户**：频道要能解析出归属，且 `(adapter_key, instance_key)` 与账户的归属实例对
  逐字相同；`email_utils` 要在那个频道生效。不满足时**跳过 + WARNING + 在账户行上留标记**
  （`settings.notify_unavailable`），配错的管理员才看得到。

同时钉住共享的通知体：两条路用的是同一个 `_build_email_notice`，实例那条不能说
「已同步到聊天」（它没有频道）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from nekro_agent.adapters.email.adapter import EmailAdapter, ParsedEmail
from nekro_agent.core.config import config as core_config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_email_account import DBEmailAccount
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services import email_accounts as mail
from nekro_agent.services.message_service import message_service
from tests.test_pushed_external_content import (  # noqa: F401 - fixtures are used by name
    INJECTION,
    db,
)

PLUGIN_KEY = "liugu.email_utils"
ADAPTER = "onebot_v11"
OWN_INSTANCE = "inst1"
OTHER_INSTANCE = "rival1"
OWN_CHAT = f"{ADAPTER}-{OWN_INSTANCE}-group_1"
OTHER_CHAT = f"{ADAPTER}-{OTHER_INSTANCE}-group_1"
PLATFORM_CHAT = f"{ADAPTER}-plat1-group_1"
PLATFORM_NOTIFY_CHAT = f"{ADAPTER}-plat1-group_2"
ADDRESS = "own@example.test"
UID = b"9"


async def make_instances() -> None:
    # `db` 已经建好 `onebot_v11/inst1` 与它自己的一个频道：这里只补别的实例与频道
    for instance_key, tenant_id in ((OWN_INSTANCE, 1), (OTHER_INSTANCE, 2), ("plat1", None)):
        await DBAdapterInstance.get_or_create(
            adapter_key=ADAPTER,
            instance_key=instance_key,
            defaults={
                "display_name": instance_key,
                "status": "online",
                "provider": "remote_onebot",
                "tenant_id": tenant_id,
                "metadata_json": "",
            },
        )
        # `db` 建的那一行默认是平台方实例（没有租户）；测试要的是「属于某个租户的实例」
        if tenant_id is not None:
            await DBAdapterInstance.filter(adapter_key=ADAPTER, instance_key=instance_key).update(tenant_id=tenant_id)
    for chat_key, instance_key in (
        (OWN_CHAT, OWN_INSTANCE),
        (OTHER_CHAT, OTHER_INSTANCE),
        (PLATFORM_CHAT, "plat1"),
        (PLATFORM_NOTIFY_CHAT, "plat1"),
    ):
        await DBChatChannel.get_or_create(
            adapter_key=ADAPTER,
            instance_key=instance_key,
            channel_id=chat_key.rsplit("-", 1)[-1],
            channel_type=ChatType.GROUP,
            chat_key=chat_key,
        )


@pytest.fixture
async def instances(db, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    await make_instances()
    monkeypatch.setattr(core_config, "PLUGIN_ENABLED", [*core_config.PLUGIN_ENABLED, PLUGIN_KEY])
    yield


def make_parsed() -> ParsedEmail:
    return ParsedEmail(
        subject="季度报表",
        sender_name="Sender",
        sender_addr="sender@example.test",
        date_str="2026-01-01 00:00:00",
        html_content="",
        text_content="",
        attachments=[],
        raw_bytes=b"",
    )


def make_adapter(*, enabled: bool = False, chat_key: str = "") -> EmailAdapter:
    """只给通知这条路要读的配置：平台方的开关与频道"""
    adapter = object.__new__(EmailAdapter)
    adapter._Configs = SimpleNamespace
    adapter._config = SimpleNamespace(  # type: ignore[assignment]
        EMAIL_NOTIFICATIONS_ENABLED=enabled,
        EMAIL_NOTIFICATIONS_CHAT_KEY=chat_key,
    )
    return adapter


async def add_account(*, instance_key: str = OWN_INSTANCE, notify_chat_key: str = OWN_CHAT) -> DBEmailAccount:
    return await DBEmailAccount.create(
        owner_adapter_key=ADAPTER,
        owner_instance_key=instance_key,
        username=ADDRESS,
        display_name="",
        provider="QQ邮箱",
        settings={},
        password="authorization-code",
        enabled=True,
        receive_enabled=True,
        send_enabled=False,
        notify_chat_key=notify_chat_key,
        attachment_dir="",
    )


def captured() -> List[Dict[str, Any]]:
    """把推送记成 dict；`triggers` 的假 schedule 已经挡掉了真正的调度"""
    return []


class TestInstanceNoticeOwnership:
    async def test_the_owning_instance_channel_gets_it(self, instances, monkeypatch: pytest.MonkeyPatch):
        row = await add_account()
        pushed: List[Dict[str, Any]] = []

        async def record(**kwargs: Any) -> None:
            pushed.append(kwargs)

        monkeypatch.setattr(message_service, "push_untrusted_message", record)

        notified = await make_adapter()._notify_new_email(  # noqa: SLF001
            ADDRESS, "", UID, make_parsed(), owner=mail.instance_owner_of(row),
        )

        assert notified is True
        assert [item["chat_key"] for item in pushed] == [OWN_CHAT]
        assert pushed[0]["trigger_agent"] is True
        result = pushed[0]["result"]
        assert result.kind == "EmailNotice"
        assert "get_email_content" in result.summary
        assert "已同步到聊天" not in result.summary, "实例自有账户没有 email 频道"
        assert INJECTION not in result.summary
        refreshed = await DBEmailAccount.get(id=row.id)
        assert refreshed.settings.get("notify_unavailable") is not True

    @pytest.mark.parametrize(
        ("reason", "notify_chat_key", "instance_key"),
        [
            ("另一实例的频道", OTHER_CHAT, OWN_INSTANCE),
            ("本实例的账户配了平台方频道", PLATFORM_CHAT, OWN_INSTANCE),
            ("频道不存在", f"{ADAPTER}-{OWN_INSTANCE}-group_404", OWN_INSTANCE),
        ],
    )
    async def test_a_foreign_channel_is_skipped_with_a_marker(
        self,
        instances,
        monkeypatch: pytest.MonkeyPatch,
        reason: str,
        notify_chat_key: str,
        instance_key: str,
    ):
        row = await add_account(instance_key=instance_key, notify_chat_key=notify_chat_key)
        pushed: List[Dict[str, Any]] = []

        async def record(**kwargs: Any) -> None:
            pushed.append(kwargs)

        monkeypatch.setattr(message_service, "push_untrusted_message", record)

        notified = await make_adapter()._notify_new_email(  # noqa: SLF001
            ADDRESS, "", UID, make_parsed(), owner=mail.instance_owner_of(row),
        )

        assert notified is False, reason
        assert pushed == []
        refreshed = await DBEmailAccount.get(id=row.id)
        assert refreshed.settings.get("notify_unavailable") is True

    async def test_the_plugin_must_be_effective_in_the_channel(
        self, instances, monkeypatch: pytest.MonkeyPatch,
    ):
        row = await add_account()
        pushed: List[Dict[str, Any]] = []

        async def record(**kwargs: Any) -> None:
            pushed.append(kwargs)

        async def off(*_args: Any, **_kwargs: Any) -> bool:
            return False

        monkeypatch.setattr(message_service, "push_untrusted_message", record)
        monkeypatch.setattr(EmailAdapter, "_email_utils_effective", staticmethod(off))

        notified = await make_adapter()._notify_new_email(  # noqa: SLF001
            ADDRESS, "", UID, make_parsed(), owner=mail.instance_owner_of(row),
        )

        assert notified is False
        assert pushed == []
        refreshed = await DBEmailAccount.get(id=row.id)
        assert refreshed.settings.get("notify_unavailable") is True

    async def test_an_account_without_a_channel_pushes_nothing(
        self, instances, monkeypatch: pytest.MonkeyPatch,
    ):
        row = await add_account(notify_chat_key="")
        pushed: List[Dict[str, Any]] = []

        async def record(**kwargs: Any) -> None:
            pushed.append(kwargs)

        monkeypatch.setattr(message_service, "push_untrusted_message", record)

        notified = await make_adapter()._notify_new_email(  # noqa: SLF001
            ADDRESS, "", UID, make_parsed(), owner=mail.instance_owner_of(row),
        )

        assert notified is False
        assert pushed == []

    async def test_the_marker_clears_once_the_channel_works(
        self, instances, monkeypatch: pytest.MonkeyPatch,
    ):
        row = await add_account(notify_chat_key=OTHER_CHAT)

        await make_adapter()._notify_new_email(ADDRESS, "", UID, make_parsed(), owner=mail.instance_owner_of(row))  # noqa: SLF001
        assert (await DBEmailAccount.get(id=row.id)).settings.get("notify_unavailable") is True

        await DBEmailAccount.filter(id=row.id).update(notify_chat_key=OWN_CHAT)
        row = await DBEmailAccount.get(id=row.id)
        pushed: List[Dict[str, Any]] = []

        async def record(**kwargs: Any) -> None:
            pushed.append(kwargs)

        monkeypatch.setattr(message_service, "push_untrusted_message", record)
        notified = await make_adapter()._notify_new_email(ADDRESS, "", UID, make_parsed(), owner=mail.instance_owner_of(row))  # noqa: SLF001

        assert notified is True
        assert [item["chat_key"] for item in pushed] == [OWN_CHAT]
        assert (await DBEmailAccount.get(id=row.id)).settings.get("notify_unavailable") is not True


class TestPlatformNoticeOwnership:
    async def test_a_tenant_channel_is_skipped(self, instances, monkeypatch: pytest.MonkeyPatch):
        pushed: List[Dict[str, Any]] = []

        async def record(**kwargs: Any) -> None:
            pushed.append(kwargs)

        monkeypatch.setattr(message_service, "push_untrusted_message", record)
        adapter = make_adapter(enabled=True, chat_key=OWN_CHAT)

        notified = await adapter._notify_new_email(ADDRESS, "email-channel", UID, make_parsed())  # noqa: SLF001

        assert notified is False
        assert pushed == []

    async def test_the_platform_channel_gets_it(self, instances, monkeypatch: pytest.MonkeyPatch):
        pushed: List[Dict[str, Any]] = []

        async def record(**kwargs: Any) -> None:
            pushed.append(kwargs)

        monkeypatch.setattr(message_service, "push_untrusted_message", record)
        adapter = make_adapter(enabled=True, chat_key=PLATFORM_NOTIFY_CHAT)

        notified = await adapter._notify_new_email(ADDRESS, "email-channel", UID, make_parsed())  # noqa: SLF001

        assert notified is True
        assert [item["chat_key"] for item in pushed] == [PLATFORM_NOTIFY_CHAT]
        assert pushed[0]["result"].summary.find("已同步到聊天") > 0

    async def test_the_plugin_must_be_effective_for_platform_too(
        self, instances, monkeypatch: pytest.MonkeyPatch,
    ):
        pushed: List[Dict[str, Any]] = []

        async def record(**kwargs: Any) -> None:
            pushed.append(kwargs)

        async def off(*_args: Any, **_kwargs: Any) -> bool:
            return False

        monkeypatch.setattr(message_service, "push_untrusted_message", record)
        monkeypatch.setattr(EmailAdapter, "_email_utils_effective", staticmethod(off))
        adapter = make_adapter(enabled=True, chat_key=PLATFORM_NOTIFY_CHAT)

        notified = await adapter._notify_new_email(ADDRESS, "email-channel", UID, make_parsed())  # noqa: SLF001

        assert notified is False
        assert pushed == []
