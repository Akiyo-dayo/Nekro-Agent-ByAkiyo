"""`email_utils` 各方法的归属判据：账户只从这次会话的集合里取

## 这里钉什么

改造前这些方法直接遍历适配器的 `RECEIVE_ACCOUNTS`：租户群里的会话能列出平台方配的全部
邮箱、读它们的邮件与附件、用它们发信。现在账户只有一个来源（`session_accounts`），
本文件逐条钉住那四个口子在**不该给的时候逐字像「没有」**：

1. `get_email_accounts`：租户只看得到能发信的显示名，看不到地址与服务商；
2. `send_email`：发件人不在可发集合里与「一个能用的都没有」同文；发信理由必填且落记录；
3. `summarize_recent_emails` / `get_email_content`：不可读的账户与不存在的账户同文；
4. `send_email_attachment`：同上，且在碰文件、调适配器之前就挡住。

`mail` 那个替身只实现 `email_utils` 与发信出口真正用到的那几个成员：多给一个成员就会
让「方法偷偷用了别的东西」不被发现。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from tortoise import Tortoise
from tortoise.timezone import now as db_now

from nekro_agent.adapters.email.config import EmailAccount
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_email import DBEmail
from nekro_agent.models.db_email_account import DBEmailAccount
from nekro_agent.models.db_email_send_log import DBEmailSendLog
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services import email_accounts as mail

ADAPTER = "onebot_v11"
OWN_INSTANCE = "inst1"
FOREIGN_INSTANCE = "rival1"
PLATFORM_INSTANCE = "plat1"

ACME = 1
RIVAL = 2

OWN_CHAT = f"{ADAPTER}-{OWN_INSTANCE}-group_10001"
FOREIGN_CHAT = f"{ADAPTER}-{FOREIGN_INSTANCE}-group_20001"
PLATFORM_CHAT = f"{ADAPTER}-{PLATFORM_INSTANCE}-group_30001"

PLATFORM_ADDRESS = "ops@example.test"
OWN_ADDRESS = "own@example.test"
FOREIGN_ADDRESS = "rival@example.test"


def ctx_of(chat_key: str) -> AgentCtx:
    return AgentCtx(from_chat_key=chat_key, container_key="probe-container")


PLATFORM_ACCOUNT = EmailAccount(
    DISPLAY_NAME="通知",
    USERNAME=PLATFORM_ADDRESS,
    SEND_ENABLED=True,
    IS_DEFAULT_SENDER=True,
)


class ClientSpy:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send_message(self, message: Any) -> None:
        self.sent.append(message)


class AdapterSpy:
    """假适配器：只具备 `email_utils` 与发信出口用到的那几个成员"""

    def __init__(self) -> None:
        self.config = SimpleNamespace(RECEIVE_ACCOUNTS=[PLATFORM_ACCOUNT])
        self.client = ClientSpy()
        self.email_clients: dict[str, Any] = {PLATFORM_ADDRESS: self.client}
        self.imap_connections: dict[str, Any] = {}
        self.imap_locks: dict[str, Any] = {}
        self.forwarded: list[Any] = []

    async def all_accounts(self) -> list[EmailAccount]:
        return list(self.config.RECEIVE_ACCOUNTS)

    def _create_email_client(self, _account: EmailAccount) -> Any:
        return self.client

    async def forward_message(self, request: Any) -> Any:
        self.forwarded.append(request)
        return SimpleNamespace(success=True, error_message="")


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    mail.clear_default_sender_cache()
    try:
        for instance_key, tenant_id, chat_key in (
            (OWN_INSTANCE, ACME, OWN_CHAT),
            (FOREIGN_INSTANCE, RIVAL, FOREIGN_CHAT),
            (PLATFORM_INSTANCE, None, PLATFORM_CHAT),
        ):
            await DBAdapterInstance.create(
                adapter_key=ADAPTER,
                instance_key=instance_key,
                display_name=instance_key,
                status="online",
                provider="remote_onebot",
                tenant_id=tenant_id,
                metadata_json="",
            )
            await DBChatChannel.create(
                adapter_key=ADAPTER,
                instance_key=instance_key,
                channel_id=chat_key.rsplit("-", maxsplit=1)[-1],
                chat_key=chat_key,
                channel_type="group",
                data="{}",
                is_active=True,
            )
        yield
    finally:
        mail.clear_default_sender_cache()
        await Tortoise.close_connections()


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> AdapterSpy:
    from plugins.builtin import email_utils

    spy = AdapterSpy()
    monkeypatch.setattr(email_utils.adapter_utils, "get_adapter", lambda _key: spy)
    monkeypatch.setattr(mail, "_load_platform_config", lambda _adapter=None: spy.config)
    mail.clear_default_sender_cache()
    return spy


async def add_instance_account(instance_key: str, username: str, *, send_enabled: bool = True) -> DBEmailAccount:
    return await DBEmailAccount.create(
        owner_adapter_key=ADAPTER,
        owner_instance_key=instance_key,
        username=mail.normalize_username(username),
        display_name="本实例",
        provider="自定义",
        settings={},
        password="secret",
        enabled=True,
        receive_enabled=True,
        send_enabled=send_enabled,
        is_default_sender=False,
        attachment_dir=f"_i{instance_key}",
    )


class TestTheAccountListFollowsTheSession:
    async def test_a_tenant_sees_only_labels_for_platform_accounts(self, db, adapter) -> None:
        from plugins.builtin import email_utils

        items = await email_utils.get_email_accounts(ctx_of(OWN_CHAT))

        assert len(items) == 1
        assert set(items[0]) == {"display_label", "account_ref", "send_enabled", "readable", "is_default_sender"}
        assert items[0]["display_label"] == "平台方提供：通知"
        assert items[0]["readable"] is False

    async def test_a_tenant_sees_its_own_accounts_in_full(self, db, adapter) -> None:
        from plugins.builtin import email_utils

        await add_instance_account(OWN_INSTANCE, OWN_ADDRESS)

        items = await email_utils.get_email_accounts(ctx_of(OWN_CHAT))

        own = [item for item in items if item.get("email_address") == OWN_ADDRESS]
        assert len(own) == 1
        assert own[0]["provider"] == "自定义"
        assert own[0]["readable"] is True
        assert own[0]["display_label"] == f"本实例 ({OWN_ADDRESS})"

    async def test_another_instances_account_never_appears(self, db, adapter) -> None:
        from plugins.builtin import email_utils

        await add_instance_account(FOREIGN_INSTANCE, FOREIGN_ADDRESS)

        items = await email_utils.get_email_accounts(ctx_of(OWN_CHAT))

        assert all(item.get("email_address") != FOREIGN_ADDRESS for item in items)

    async def test_a_send_disabled_account_is_not_in_the_list(self, db, adapter) -> None:
        """可读 ∪ 可发：停发信的账户既不进可读（实例自有但停发？仍可读）也不该凭空出现

        实例自有账户停发信时仍然可读，因此这里改的是平台方账户的 `SEND_ENABLED`。
        """
        from plugins.builtin import email_utils

        adapter.config.RECEIVE_ACCOUNTS = [
            EmailAccount(DISPLAY_NAME="内部", USERNAME=PLATFORM_ADDRESS, SEND_ENABLED=False),
        ]
        mail.clear_default_sender_cache()

        items = await email_utils.get_email_accounts(ctx_of(OWN_CHAT))

        assert items == []


class TestSendingFollowsTheSession:
    async def test_the_reason_is_required_and_nothing_is_sent(self, db, adapter) -> None:
        from plugins.builtin import email_utils

        result = await email_utils.send_email(ctx_of(OWN_CHAT), "to@example.test", "主题", "正文", "   ")

        assert result["success"] is False
        assert result["message"] == "请写明发信理由"
        assert adapter.client.sent == []
        assert await DBEmailSendLog.all().count() == 0

    async def test_an_over_long_reason_is_refused(self, db, adapter) -> None:
        from plugins.builtin import email_utils

        result = await email_utils.send_email(ctx_of(OWN_CHAT), "to@example.test", "主题", "正文", "x" * 201)

        assert result["success"] is False
        assert result["message"] == "发信理由请控制在 200 字以内"
        assert adapter.client.sent == []

    async def test_a_platform_sender_works_for_a_tenant_and_is_recorded(self, db, adapter) -> None:
        from plugins.builtin import email_utils

        result = await email_utils.send_email(ctx_of(OWN_CHAT), "to@example.test", "主题", "正文", "对方要求回信")

        assert result["success"] is True
        assert len(adapter.client.sent) == 1
        log = await DBEmailSendLog.get()
        assert log.purpose == mail.PURPOSE_SEND_EMAIL
        assert log.adapter_key == ADAPTER
        assert log.instance_key == OWN_INSTANCE
        assert log.chat_key == OWN_CHAT
        assert log.tenant_id == ACME
        assert log.ok is True
        assert log.reason == "对方要求回信"

    async def test_the_returned_sender_label_hides_the_platform_address(self, db, adapter) -> None:
        """返回值会落成 SYSTEM 消息：租户会话里不能借它把平台方地址读出来"""
        from plugins.builtin import email_utils

        result = await email_utils.send_email(ctx_of(OWN_CHAT), "to@example.test", "主题", "正文", "对方要求回信")

        assert PLATFORM_ADDRESS not in str(result)
        assert result["from"] == "平台方提供：通知"

    async def test_a_platform_session_still_gets_the_address_back(self, db, adapter) -> None:
        """平台方实例的会话与改造前相同：`from` 回的是发件地址"""
        from plugins.builtin import email_utils

        result = await email_utils.send_email(ctx_of(PLATFORM_CHAT), "to@example.test", "主题", "正文", "对方要求回信")

        assert result["success"] is True
        assert result["from"] == PLATFORM_ADDRESS

    async def test_an_unknown_sender_and_no_sender_read_the_same(self, db, adapter) -> None:
        from plugins.builtin import email_utils

        named = await email_utils.send_email(
            ctx_of(OWN_CHAT),
            "to@example.test",
            "主题",
            "正文",
            "对方要求回信",
            from_account=FOREIGN_ADDRESS,
        )
        adapter.config.RECEIVE_ACCOUNTS = []
        mail.clear_default_sender_cache()
        empty = await email_utils.send_email(ctx_of(OWN_CHAT), "to@example.test", "主题", "正文", "对方要求回信")

        assert named["success"] is False
        assert empty["success"] is False
        # 前缀与改造前逐字相同；点名别人的邮箱与一个都没有，两句话分不出来
        assert named["message"] == empty["message"] == f"邮件发送失败: {mail.NO_SENDER}"

    async def test_a_failure_still_writes_a_record(self, db, adapter) -> None:
        from plugins.builtin import email_utils

        async def boom(_message: Any) -> None:
            raise RuntimeError("smtp said no")

        adapter.client.send_message = boom  # type: ignore[method-assign]

        result = await email_utils.send_email(ctx_of(OWN_CHAT), "to@example.test", "主题", "正文", "对方要求回信")

        assert result["success"] is False
        log = await DBEmailSendLog.get()
        assert log.ok is False
        assert "smtp said no" in log.error

    async def test_a_tenant_does_not_get_the_platform_smtp_error_text(self, db, adapter) -> None:
        """SMTP 报错里可能带平台方的服务器与地址：租户会话只给一句能转告的话"""
        from plugins.builtin import email_utils

        async def boom(_message: Any) -> None:
            raise RuntimeError(f"535 auth failed for {PLATFORM_ADDRESS}")

        adapter.client.send_message = boom  # type: ignore[method-assign]

        result = await email_utils.send_email(ctx_of(OWN_CHAT), "to@example.test", "主题", "正文", "对方要求回信")

        assert result["message"] == "邮件发送失败，请稍后再试或联系平台方"
        assert PLATFORM_ADDRESS not in result["message"]

    async def test_an_own_mailbox_failure_is_one_general_sentence(self, db, adapter, monkeypatch: pytest.MonkeyPatch) -> None:
        """实例自有邮箱的服务器可能是租户填的：拒绝 / 超时 / 对端核对没过回同一句，记录里也只有这一句"""
        from plugins.builtin import email_utils

        await add_instance_account(OWN_INSTANCE, OWN_ADDRESS)
        internal = ".".join(("10", "0", "0", "5"))
        outcomes = []
        for error in (ConnectionRefusedError(f"{internal}:25 refused"), TimeoutError("timed out")):

            async def boom(*_args: Any, _error: BaseException = error, **_kwargs: Any) -> None:
                raise _error

            monkeypatch.setattr(mail, "send_via_account", boom)
            result = await email_utils.send_email(
                ctx_of(OWN_CHAT), "to@example.test", "主题", "正文", "对方要求回信", from_account=OWN_ADDRESS,
            )
            outcomes.append(result["message"])

        expected = f"邮件发送失败: {mail.MAIL_FAILURE_TEXTS[mail.MAIL_FAILURE_CONNECT]}"
        assert outcomes == [expected, expected]
        errors = [log.error for log in await DBEmailSendLog.all()]
        assert errors == [mail.MAIL_FAILURE_TEXTS[mail.MAIL_FAILURE_CONNECT]] * 2
        assert all(internal not in text for text in errors)


class TestReadingFollowsTheSession:
    async def test_a_cached_platform_mail_is_unreadable_for_a_tenant(self, db, adapter) -> None:
        from plugins.builtin import email_utils

        await DBEmail.create(
            account_username=PLATFORM_ADDRESS,
            email_uid="7",
            subject="平台方邮件",
            sender="eve@example.test",
            body_text="只给平台方看",
            attachment_names="[]",
        )

        with pytest.raises(Exception) as error:
            await email_utils.get_email_content(ctx_of(OWN_CHAT), PLATFORM_ADDRESS, "7")

        assert str(error.value) == f"获取邮件内容失败: 账户 {PLATFORM_ADDRESS} 未连接或不存在"

    async def test_the_platform_session_reads_the_same_cached_mail(self, db, adapter) -> None:
        from plugins.builtin import email_utils

        await DBEmail.create(
            account_username=PLATFORM_ADDRESS,
            email_uid="7",
            subject="平台方邮件",
            sender="eve@example.test",
            body_text="只给平台方看",
            attachment_names="[]",
        )

        result = await email_utils.get_email_content(ctx_of(PLATFORM_CHAT), PLATFORM_ADDRESS, "7")

        assert result.kind == "Email"
        assert "平台方邮件" in result.body

    async def test_a_cache_row_of_a_previous_owner_is_not_readable(self, db, adapter) -> None:
        """地址换了主人：旧主人收的信不能以新主人的名义读到"""
        from plugins.builtin import email_utils

        await add_instance_account(OWN_INSTANCE, OWN_ADDRESS)
        await DBEmail.create(
            account_username=OWN_ADDRESS,
            email_uid="7",
            subject="旧主人的信",
            sender="eve@example.test",
            body_text="不该被新主人看到",
            attachment_names="[]",
            owner_adapter_key=ADAPTER,
            owner_instance_key=FOREIGN_INSTANCE,
        )

        with pytest.raises(Exception) as error:
            await email_utils.get_email_content(ctx_of(OWN_CHAT), OWN_ADDRESS, "7")

        assert str(error.value) == f"获取邮件内容失败: 账户 {OWN_ADDRESS} 未连接或不存在"

    async def test_a_server_error_while_fetching_reads_in_general_terms(self, db, adapter) -> None:
        """取信时的原始异常（可能带对端地址、分得出连接被拒与超时）只进日志，模型拿到归类后的一句"""
        from plugins.builtin import email_utils

        await add_instance_account(OWN_INSTANCE, OWN_ADDRESS)
        internal = ".".join(("10", "0", "0", "5"))

        class _Refusing:
            async def get_raw_email_content(self, _email_id: str) -> Any:
                raise ConnectionRefusedError(f"{internal}:993 refused")

        adapter.email_clients[OWN_ADDRESS] = _Refusing()

        with pytest.raises(Exception) as error:
            await email_utils.get_email_content(ctx_of(OWN_CHAT), OWN_ADDRESS, "7")

        assert str(error.value) == f"获取邮件内容失败: {mail.MAIL_FAILURE_TEXTS[mail.MAIL_FAILURE_CONNECT]}"
        assert internal not in str(error.value)

    async def test_an_unconnected_account_keeps_its_own_wording(self, db, adapter) -> None:
        """本函数自己的业务文案原样给（这几句不带对端信息）"""
        from plugins.builtin import email_utils

        await add_instance_account(OWN_INSTANCE, OWN_ADDRESS)

        with pytest.raises(Exception) as error:
            await email_utils.get_email_content(ctx_of(OWN_CHAT), OWN_ADDRESS, "7")

        assert str(error.value) == f"获取邮件内容失败: 账户 {OWN_ADDRESS} 未连接或不存在"

    async def test_the_summary_falls_back_to_the_same_answer(self, db, adapter, monkeypatch: pytest.MonkeyPatch) -> None:
        from plugins.builtin import email_utils

        async def forbidden(**_kwargs: Any) -> Any:
            raise AssertionError("没有可读账户时不该走到模型调用")

        monkeypatch.setattr(email_utils, "gen_openai_chat_response", forbidden)

        text = await email_utils.summarize_recent_emails(ctx_of(OWN_CHAT))

        assert "没有找到符合条件的邮箱账户" in text

    async def test_the_summary_reads_the_cached_row_of_a_readable_account(
        self,
        db,
        adapter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from plugins.builtin import email_utils

        await add_instance_account(OWN_INSTANCE, OWN_ADDRESS)
        await DBEmail.create(
            account_username=OWN_ADDRESS,
            email_uid="7",
            subject="本实例的信",
            sender="eve@example.test",
            date=db_now(),
            body_text="正文",
            attachment_names="[]",
            owner_adapter_key=ADAPTER,
            owner_instance_key=OWN_INSTANCE,
        )

        seen: dict[str, Any] = {}

        async def fake_model(**kwargs: Any) -> Any:
            seen["messages"] = kwargs["messages"]
            return SimpleNamespace(response_content="摘要")

        monkeypatch.setattr(email_utils, "gen_openai_chat_response", fake_model)

        result = await email_utils.summarize_recent_emails(ctx_of(OWN_CHAT))

        assert result.kind == "EmailSummary"
        assert "本实例的信" in str(seen["messages"])


class TestTheAttachmentFollowsTheSession:
    async def test_a_tenant_gets_the_missing_account_answer_without_touching_files(self, db, adapter) -> None:
        from plugins.builtin import email_utils

        result = await email_utils.send_email_attachment(ctx_of(OWN_CHAT), PLATFORM_ADDRESS, "7", "a.txt", OWN_CHAT)

        assert result["success"] is False
        assert result["message"] == f"发送邮件附件失败: 账户 {PLATFORM_ADDRESS} 未连接或不存在"
        assert adapter.forwarded == []

    async def test_an_own_account_reads_from_its_own_attachment_dir(
        self,
        db,
        adapter,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """附件根目录来自账户行，不是模型填的那个账户名"""
        from plugins.builtin import email_utils

        row = await add_instance_account(OWN_INSTANCE, OWN_ADDRESS)
        base = tmp_path / "email_attachment"
        monkeypatch.setattr(email_utils, "ATTACHMENT_BASE_DIR", base)
        monkeypatch.setattr(email_utils, "USER_UPLOAD_DIR", str(tmp_path / "uploads"))

        async def fake_send_file(chat_key: str, file_path: str, _ctx: Any, **_kwargs: Any) -> bool:
            adapter.forwarded.append((chat_key, file_path))
            return True

        monkeypatch.setattr(email_utils.plugin, "send_file", fake_send_file)
        # 这封信要在这个账户的缓存里、附件名要在它的附件清单里（转发前先核对）
        await DBEmail.create(
            account_username=OWN_ADDRESS,
            email_uid="7",
            subject="主题",
            sender="noreply@sender.example",
            recipients="[]",
            body_text="正文",
            has_attachments=True,
            attachment_names='["a.txt"]',
            owner_adapter_key=ADAPTER,
            owner_instance_key=OWN_INSTANCE,
        )
        source = base / str(row.attachment_dir) / "7" / "a.txt"
        source.parent.mkdir(parents=True)
        source.write_text("hello", encoding="utf-8")

        result = await email_utils.send_email_attachment(ctx_of(OWN_CHAT), OWN_ADDRESS, "7", "a.txt", OWN_CHAT)

        assert result["success"] is True
        assert len(adapter.forwarded) == 1
        # 读的是账户行里的目录（`_i<id>`），不是按地址拼的那个
        copied = tmp_path / "uploads" / "onebot_v11-inst1-group_10001" / "a.txt"
        assert copied.read_text(encoding="utf-8") == "hello"

    async def test_a_file_only_in_the_address_named_dir_is_not_found(
        self,
        db,
        adapter,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """按地址拼目录会让实例自有账户读到平台方同名目录里的文件：必须读不到"""
        from plugins.builtin import email_utils

        await add_instance_account(OWN_INSTANCE, OWN_ADDRESS)
        base = tmp_path / "email_attachment"
        monkeypatch.setattr(email_utils, "ATTACHMENT_BASE_DIR", base)
        monkeypatch.setattr(email_utils, "USER_UPLOAD_DIR", str(tmp_path / "uploads"))
        # 只有「按地址命名」的那份文件（改造前的口径），账户行里的目录是空的
        stray = base / OWN_ADDRESS / "7" / "a.txt"
        stray.parent.mkdir(parents=True)
        stray.write_text("别人的文件", encoding="utf-8")

        result = await email_utils.send_email_attachment(ctx_of(OWN_CHAT), OWN_ADDRESS, "7", "a.txt", OWN_CHAT)

        assert result["success"] is False
        assert adapter.forwarded == []

    async def test_a_mail_that_is_not_in_the_cache_is_not_forwarded(
        self,
        db,
        adapter,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """文件在磁盘上、但缓存里没有这封信（或附件名不在清单里）：不转发——不让模型拿任意编号探测目录"""
        from plugins.builtin import email_utils

        row = await add_instance_account(OWN_INSTANCE, OWN_ADDRESS)
        base = tmp_path / "email_attachment"
        monkeypatch.setattr(email_utils, "ATTACHMENT_BASE_DIR", base)
        monkeypatch.setattr(email_utils, "USER_UPLOAD_DIR", str(tmp_path / "uploads"))

        async def fake_send_file(chat_key: str, file_path: str, _ctx: Any, **_kwargs: Any) -> bool:
            adapter.forwarded.append((chat_key, file_path))
            return True

        monkeypatch.setattr(email_utils.plugin, "send_file", fake_send_file)
        source = base / str(row.attachment_dir) / "9" / "a.txt"
        source.parent.mkdir(parents=True)
        source.write_text("hello", encoding="utf-8")

        missing_mail = await email_utils.send_email_attachment(ctx_of(OWN_CHAT), OWN_ADDRESS, "9", "a.txt", OWN_CHAT)
        await DBEmail.create(
            account_username=OWN_ADDRESS, email_uid="9", subject="s", sender="x@example.test", recipients="[]",
            body_text="", has_attachments=True, attachment_names='["other.txt"]',
            owner_adapter_key=ADAPTER, owner_instance_key=OWN_INSTANCE,
        )
        unlisted_name = await email_utils.send_email_attachment(ctx_of(OWN_CHAT), OWN_ADDRESS, "9", "a.txt", OWN_CHAT)

        assert missing_mail["success"] is False and unlisted_name["success"] is False
        assert adapter.forwarded == []

    @pytest.mark.parametrize(
        ("email_id", "filename"),
        [
            (f"../{PLATFORM_ADDRESS}/7", "a.txt"),
            ("..", "a.txt"),
            ("7", ".."),
            (f"7/../../{PLATFORM_ADDRESS}/7", "a.txt"),
            (f"..\\{PLATFORM_ADDRESS}\\7", "a.txt"),
        ],
    )
    async def test_an_own_account_cannot_walk_into_another_accounts_dir(
        self,
        db,
        adapter,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        email_id: str,
        filename: str,
    ) -> None:
        """`email_id` 与文件名都是模型填的：`../<平台方地址>/<UID>` 仍落在附件总目录里，却走进了别人的目录"""
        from plugins.builtin import email_utils

        await add_instance_account(OWN_INSTANCE, OWN_ADDRESS)
        base = tmp_path / "email_attachment"
        monkeypatch.setattr(email_utils, "ATTACHMENT_BASE_DIR", base)
        monkeypatch.setattr(email_utils, "USER_UPLOAD_DIR", str(tmp_path / "uploads"))

        async def fake_send_file(chat_key: str, file_path: str, _ctx: Any, **_kwargs: Any) -> bool:
            adapter.forwarded.append((chat_key, file_path))
            return True

        monkeypatch.setattr(email_utils.plugin, "send_file", fake_send_file)
        secret = base / PLATFORM_ADDRESS / "7" / "a.txt"
        secret.parent.mkdir(parents=True)
        secret.write_text("平台方邮箱的附件", encoding="utf-8")
        # 故意造一行「编号、附件名都对得上」的缓存，让请求越过缓存核对、走到路径判据那一层：
        # 缓存行的编号来自收信服务器，不能只靠「缓存里有没有」挡穿越
        await DBEmail.create(
            account_username=OWN_ADDRESS,
            email_uid=email_id,
            subject="主题",
            sender="noreply@sender.example",
            recipients="[]",
            body_text="正文",
            has_attachments=True,
            attachment_names=f'["{filename}"]'.replace("\\", "\\\\"),
            owner_adapter_key=ADAPTER,
            owner_instance_key=OWN_INSTANCE,
        )

        result = await email_utils.send_email_attachment(ctx_of(OWN_CHAT), OWN_ADDRESS, email_id, filename, OWN_CHAT)

        assert result["success"] is False
        assert adapter.forwarded == []
        assert not (tmp_path / "uploads").exists() or not any((tmp_path / "uploads").rglob("a.txt"))
