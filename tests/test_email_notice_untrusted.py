"""邮件适配器的新邮件通知不以 SYSTEM 身份推外部字段

## 背景

`_notify_new_email` 曾经把发件人名、发件地址、主题、日期与附件名原样拼进
`push_system_message(trigger_agent=True)`：任何能给这个邮箱发信的人都能在发件人名、主题、
附件名里写指令，以「系统说的话」进上下文并立刻触发 Agent。

现在改走 `push_untrusted_message`：宿主写的引导语进可信摘要，这些外部字段进不可信块
（落库身份不是 SYSTEM，渲染时用当轮令牌包裹），触发 Agent 的行为不变。

## 本文件钉什么

1. 外部字段只在不可信块里，可信摘要里没有；
2. 可信摘要里有收件账户、UID 与取正文的方法名；
3. 通知照常触发 Agent；
4. 开关关掉时既不落库也不触发。
"""

from types import SimpleNamespace

import pytest

from nekro_agent.adapters.email.adapter import EmailAdapter, ParsedEmail
from nekro_agent.adapters.email.config import EmailConfig
from tests.test_pushed_external_content import (  # noqa: F401 - fixtures are used by name
    CHAT_KEY,
    INJECTION,
    RENDER_CONFIG,
    TOKEN,
    assert_one_untrusted_row,
    db,
    stored_rows,
    triggers,
)

NOTIFY_CHAT_KEY = CHAT_KEY
ACCOUNT = "mailbox@example.com"
CHAT_ID = "email-mailbox@example.com"
EMAIL_ID = b"42"


def make_parsed() -> ParsedEmail:
    """必填字段给占位值；发件人名、主题、附件名各放一段带换行的敌意文本"""
    return ParsedEmail(
        subject=INJECTION,
        sender_name=INJECTION,
        sender_addr="mallory@example.com",
        date_str="2026-01-01 00:00:00",
        html_content="",
        text_content="",
        attachments=[INJECTION],
        raw_bytes=b"",
    )


def make_adapter(enabled: bool = True, chat_key: str = NOTIFY_CHAT_KEY) -> EmailAdapter:
    """绕过构造函数：只塞 `_notify_new_email` 与 `_email_id_text` 会读到的配置

    `config` 是只读属性，实际读的是 `_config`；`_Configs` 一起给上，`get_config` 才会直接返回它。
    """
    adapter = object.__new__(EmailAdapter)
    adapter._Configs = EmailConfig
    adapter._config = SimpleNamespace(  # type: ignore[assignment]
        EMAIL_NOTIFICATIONS_ENABLED=enabled,
        EMAIL_NOTIFICATIONS_CHAT_KEY=chat_key,
    )
    return adapter


class TestEmailNotice:
    async def test_the_sender_subject_and_attachment_are_untrusted(self, db, triggers):  # noqa: F811
        await make_adapter()._notify_new_email(ACCOUNT, CHAT_ID, EMAIL_ID, make_parsed())

        row = await assert_one_untrusted_row()
        assert INJECTION not in row.content_text
        assert '"mailbox@example.com"' in row.content_text, "收件账户压成单行回显"
        assert '"42"' in row.content_text, "UID 压成单行回显"
        assert "get_email_content" in row.content_text
        assert triggers.scheduled == [NOTIFY_CHAT_KEY]

    async def test_the_attachment_count_is_on_the_opening_tag(self, db, triggers):  # noqa: F811
        await make_adapter()._notify_new_email(ACCOUNT, CHAT_ID, EMAIL_ID, make_parsed())

        row = await assert_one_untrusted_row()
        prompt = row.parse_chat_history_prompt(TOKEN, RENDER_CONFIG)  # type: ignore[arg-type]
        opening = f"<{TOKEN} | EmailNotice attachments:1 trust:untrusted>"
        assert opening in prompt
        assert prompt.index(opening) < prompt.index("附件: 有 1 个附件")

    @pytest.mark.parametrize(
        ("enabled", "chat_key"),
        [(False, NOTIFY_CHAT_KEY), (True, "")],
        ids=["disabled", "no-channel"],
    )
    async def test_switched_off_stores_and_triggers_nothing(self, db, triggers, enabled: bool, chat_key: str):  # noqa: F811
        await make_adapter(enabled=enabled, chat_key=chat_key)._notify_new_email(ACCOUNT, CHAT_ID, EMAIL_ID, make_parsed())

        assert await stored_rows() == []
        assert triggers.scheduled == [] and triggers.hooked == []
