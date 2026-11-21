"""租户会话的资源可见性止血（邮箱读口子、摘要模型组、表情库）

## 威胁模型

邮箱账户与表情库都挂在**适配器层 / 插件全局**，没有归属列：任何租户实例的会话都能列出平台方
配的全部邮箱、读它们的邮件与附件，能改删别人收集的表情，摘要方法的 `model_group` 还能由模型
自己填成平台方 `private` 组。会话身份只认宿主签发的 `from_chat_key`，模型给的任何 `chat_key`
都不作数。

## 本文件钉住的四件事

1. `resolve_session_owner` 的判据（含 `chat_key` 参数不影响结果）；
2. 邮箱四个读口子按"不存在"回答，发信与平台方、单租户部署逐字不变；
3. 摘要的模型组可见性、`count` 夹值与通知频道的归属；
4. 表情库在租户实例的会话里整体暂停，且在执行到读库 / 算嵌入 / 查向量库之前就停。

"拒绝"与"不存在"同文：模型分不出"没有"和"不是你的"。
"""

from types import SimpleNamespace
from typing import Any, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.adapters.email.adapter import EmailAdapter, ParsedEmail
from nekro_agent.adapters.email.config import EmailAccount
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_email import DBEmail
from nekro_agent.models.db_model_group import DBModelGroup
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services.command.schemas import CommandResponseStatus
from nekro_agent.services.resource_scope import resolve_chat_owner, resolve_session_owner

ACME = 1
RIVAL = 2

KEY_A1 = "onebot_v11-inst1-group_10001"
KEY_A2 = "onebot_v11-inst1-group_10002"
KEY_B = "onebot_v11-rival1-group_20001"
KEY_P = "onebot_v11-plat1-group_30001"
KEY_GHOST = "onebot_v11-inst1-group_99999"

EMAIL_CHAT = "email-ops@example.com"


def ctx_of(chat_key: Optional[str]) -> AgentCtx:
    return AgentCtx(from_chat_key=chat_key, container_key="probe-container")


async def seed_deployment(tenants: bool) -> None:
    """三档实例各一个群频道；`tenants=False` 时三个实例全归平台方（单租户部署）"""
    layout = (
        ("inst1", ACME if tenants else None, [KEY_A1, KEY_A2]),
        ("rival1", RIVAL if tenants else None, [KEY_B]),
        ("plat1", None, [KEY_P]),
    )
    for instance_key, tenant_id, chat_keys in layout:
        await DBAdapterInstance.create(
            adapter_key="onebot_v11",
            instance_key=instance_key,
            display_name=instance_key,
            status="online",
            provider="remote_onebot",
            tenant_id=tenant_id,
            metadata_json="",
        )
        for chat_key in chat_keys:
            await DBChatChannel.create(
                adapter_key="onebot_v11",
                instance_key=instance_key,
                channel_id=chat_key.rsplit("-", maxsplit=1)[-1],
                chat_key=chat_key,
                channel_type="group",
                data="{}",
            )
    # 单账号适配器的频道：没有实例段，也就没有"本实例"可归属
    await DBChatChannel.create(
        adapter_key="email",
        instance_key="",
        channel_id=EMAIL_CHAT,
        chat_key=EMAIL_CHAT,
        channel_type="private",
        data="{}",
    )


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await seed_deployment(tenants=True)
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
async def single_tenant_db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await seed_deployment(tenants=False)
        yield
    finally:
        await Tortoise.close_connections()


# ---------------------------------------------------------------------------
# 1. 归属解析
# ---------------------------------------------------------------------------


class TestResolveSessionOwner:
    async def test_a_platform_channel_is_the_platform(self, db):
        owner = await resolve_session_owner(ctx_of(KEY_P))
        assert owner.platform is True
        assert owner.resolved is True
        assert owner.tenant_id is None
        assert (owner.adapter_key, owner.instance_key) == ("onebot_v11", "plat1")

    async def test_a_tenant_channel_carries_its_tenant(self, db):
        owner = await resolve_session_owner(ctx_of(KEY_A1))
        assert owner.platform is False
        assert owner.resolved is True
        assert owner.tenant_id == ACME
        assert (owner.adapter_key, owner.instance_key) == ("onebot_v11", "inst1")

    async def test_a_channel_without_instance_key_is_the_platform(self, db):
        owner = await resolve_chat_owner(EMAIL_CHAT)
        assert owner.platform is True
        assert owner.resolved is True
        assert owner.tenant_id is None
        assert owner.instance_key == ""

    async def test_a_missing_chat_key_fails_closed_in_a_multi_tenant_deployment(self, db):
        for raw in (None, "", 42, KEY_GHOST):
            owner = await resolve_chat_owner(raw)
            assert owner.platform is False, raw
            assert owner.resolved is False
            assert owner.tenant_id is None
            assert (owner.adapter_key, owner.instance_key) == ("", "")

    async def test_a_missing_chat_key_is_the_platform_in_a_single_tenant_deployment(self, single_tenant_db):
        for raw in (None, "", 42, KEY_GHOST):
            owner = await resolve_chat_owner(raw)
            assert owner.platform is True, raw
            assert owner.resolved is False

    async def test_only_from_chat_key_is_trusted(self, db):
        """模型能把 `chat_key` 传成平台方频道，但归属只看宿主签发的 `from_chat_key`"""
        probe = SimpleNamespace(from_chat_key=KEY_A1, chat_key=KEY_P)
        owner = await resolve_session_owner(probe)
        assert owner.platform is False
        assert owner.tenant_id == ACME

    async def test_a_context_without_from_chat_key_fails_closed(self, db):
        owner = await resolve_session_owner(SimpleNamespace(chat_key=KEY_P))
        assert owner.platform is False
        assert owner.resolved is False


# ---------------------------------------------------------------------------
# 2. 邮箱
# ---------------------------------------------------------------------------

ACCOUNT_NOTICE = EmailAccount(
    DISPLAY_NAME="通知",
    USERNAME="notice@example.com",
    SEND_ENABLED=True,
    IS_DEFAULT_SENDER=True,
)
ACCOUNT_ADDRESSY = EmailAccount(
    DISPLAY_NAME="a@b.example",
    USERNAME="ops@example.com",
    SEND_ENABLED=True,
)
ACCOUNT_SEND_OFF = EmailAccount(
    DISPLAY_NAME="内部",
    USERNAME="noreply@example.com",
    SEND_ENABLED=False,
)

SHARED_KEYS = {"display_label", "account_ref", "send_enabled", "readable", "is_default_sender"}


class MailClientSpy:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send_message(self, message: Any) -> None:
        self.sent.append(message)


class MailAdapterSpy:
    """假适配器：只具备 `email_utils` 与发信出口用到的那些成员"""

    def __init__(self) -> None:
        self.config = SimpleNamespace(
            RECEIVE_ACCOUNTS=[ACCOUNT_NOTICE, ACCOUNT_ADDRESSY, ACCOUNT_SEND_OFF],
        )
        self.client = MailClientSpy()
        self.email_clients: dict[str, Any] = {ACCOUNT_NOTICE.USERNAME: self.client}
        self.imap_connections: dict[str, Any] = {}
        self.imap_locks: dict[str, Any] = {}
        self.forwarded: list[Any] = []

    async def all_accounts(self) -> list[EmailAccount]:
        """发信出口按地址找回运行时账户走这里（平台方账户 ∪ 表里的实例自有账户）"""
        return list(self.config.RECEIVE_ACCOUNTS)

    def _create_email_client(self, account: EmailAccount) -> Any:
        return self.client

    async def forward_message(self, request: Any) -> Any:
        self.forwarded.append(request)
        return SimpleNamespace(success=True, error_message="")


@pytest.fixture
def mail(monkeypatch: pytest.MonkeyPatch) -> MailAdapterSpy:
    from nekro_agent.services import email_accounts
    from plugins.builtin import email_utils

    adapter = MailAdapterSpy()
    monkeypatch.setattr(email_utils.adapter_utils, "get_adapter", lambda _key: adapter)
    # 账户清单现在从服务层取（`session_accounts`），平台方那一份也要换成本用例的假配置
    monkeypatch.setattr(email_accounts, "_load_platform_config", lambda _adapter=None: adapter.config)
    email_accounts.clear_default_sender_cache()
    return adapter


class TestEmailAccounts:
    async def test_a_tenant_session_only_sees_sendable_labels(self, db, mail):
        from plugins.builtin import email_utils

        accounts = await email_utils.get_email_accounts(ctx_of(KEY_A1))

        assert len(accounts) == 2, "只给能发信的两个，停发信的不列"
        for account in accounts:
            assert set(account) == SHARED_KEYS
        labels = [account["display_label"] for account in accounts]
        assert labels == ["平台方提供：通知", "平台方提供：邮箱"], "显示名是地址时换成'邮箱'"
        assert [account["is_default_sender"] for account in accounts] == [True, False]
        assert all(account["readable"] is False for account in accounts)
        assert not any("@" in str(value) for account in accounts for value in account.values())

    async def test_the_platform_session_keeps_the_full_list(self, db, mail):
        from nekro_agent.services import email_accounts
        from plugins.builtin import email_utils

        accounts = await email_utils.get_email_accounts(ctx_of(KEY_P))

        assert [account["email_address"] for account in accounts] == [
            ACCOUNT_NOTICE.USERNAME,
            ACCOUNT_ADDRESSY.USERNAME,
            ACCOUNT_SEND_OFF.USERNAME,
        ]
        assert accounts[0] == {
            "display_name": "通知",
            "display_label": "通知 (notice@example.com)",
            "email_address": "notice@example.com",
            "provider": ACCOUNT_NOTICE.EMAIL_ACCOUNT,
            "send_enabled": True,
            "is_default_sender": True,
            "readable": True,
            "account_ref": email_accounts.platform_account_ref(ACCOUNT_NOTICE.USERNAME),
        }


class TestEmailContent:
    async def test_a_tenant_session_cannot_read_a_cached_platform_email(self, db, mail):
        from plugins.builtin import email_utils

        await DBEmail.create(
            account_username=ACCOUNT_NOTICE.USERNAME,
            email_uid="7",
            subject="平台方邮件",
            sender="someone@example.com",
            body_text="正文",
            attachment_names="[]",
        )

        with pytest.raises(Exception) as tenant_error:
            await email_utils.get_email_content(ctx_of(KEY_A1), ACCOUNT_NOTICE.USERNAME, "7")
        assert str(tenant_error.value) == f"获取邮件内容失败: 账户 {ACCOUNT_NOTICE.USERNAME} 未连接或不存在"

        cached = await email_utils.get_email_content(ctx_of(KEY_P), ACCOUNT_NOTICE.USERNAME, "7")
        assert cached.kind == "Email"
        assert "平台方邮件" in cached.body

    async def test_the_tenant_error_is_verbatim_the_missing_account_error(self, db, mail):
        from plugins.builtin import email_utils

        with pytest.raises(Exception) as tenant_error:
            await email_utils.get_email_content(ctx_of(KEY_A1), "nobody@example.com", "7")
        with pytest.raises(Exception) as platform_error:
            await email_utils.get_email_content(ctx_of(KEY_P), "nobody@example.com", "7")
        assert str(tenant_error.value) == str(platform_error.value)
        assert str(tenant_error.value) == "获取邮件内容失败: 账户 nobody@example.com 未连接或不存在"


class TestEmailAttachment:
    async def test_a_tenant_session_gets_the_missing_account_answer_without_touching_files(self, db, mail):
        from plugins.builtin import email_utils

        result = await email_utils.send_email_attachment(ctx_of(KEY_A1), ACCOUNT_NOTICE.USERNAME, "7", "a.txt", KEY_A1)

        assert result == {
            "success": False,
            "message": f"发送邮件附件失败: 账户 {ACCOUNT_NOTICE.USERNAME} 未连接或不存在",
            "filename": "a.txt",
        }
        assert mail.forwarded == []


class TestSendEmailStaysOpen:
    async def test_a_tenant_session_can_still_send_from_the_default_sender(self, db, mail):
        from plugins.builtin import email_utils

        result = await email_utils.send_email(ctx_of(KEY_A1), "to@example.com", "主题", "正文", "对方要求回信")

        assert result["success"] is True
        assert len(mail.client.sent) == 1
        assert mail.client.sent[0]["From"] == ACCOUNT_NOTICE.USERNAME


class TestSummary:
    async def test_a_tenant_session_finds_no_accounts_and_calls_no_model(self, db, mail, monkeypatch: pytest.MonkeyPatch):
        from plugins.builtin import email_utils

        async def forbidden(**_kwargs: Any) -> Any:
            raise AssertionError("租户会话不该走到模型调用")

        monkeypatch.setattr(email_utils, "gen_openai_chat_response", forbidden)

        tenant_result = await email_utils.summarize_recent_emails(ctx_of(KEY_A1))
        platform_result = await email_utils.summarize_recent_emails(
            ctx_of(KEY_P),
            account_filter="nobody@example.com",
        )

        assert tenant_result == platform_result
        assert "没有找到符合条件的邮箱账户" in str(tenant_result)

    async def test_the_count_is_clamped_for_every_session(self, db):
        from plugins.builtin import email_utils

        limit = max(1, int(email_utils.config.MAX_EMAILS_FOR_SUMMARY))
        clamp = email_utils._clamp_summary_count  # noqa: SLF001
        assert clamp(None) == limit
        assert clamp(0) == 1
        assert clamp(-3) == 1
        assert clamp("x") == limit
        assert clamp(limit + 100) == limit
        assert clamp(3) == 3

    async def test_the_model_group_of_a_tenant_session_is_visibility_checked(self, db, mail):
        from plugins.builtin import email_utils

        await DBModelGroup.create(
            name="platform_private",
            ownership="platform",
            owner_tenant_id=None,
            visibility="private",
            group_name="platform_private",
            chat_model="gpt-4o",
            base_url="https://api.example/v1",
            api_key="sk-private",
            model_type="chat",
        )
        await DBModelGroup.create(
            name="platform_shared",
            ownership="platform",
            owner_tenant_id=None,
            visibility="shared",
            group_name="platform_shared",
            chat_model="gpt-4o",
            base_url="https://api.example/v1",
            api_key="sk-shared",
            model_type="chat",
        )
        await DBModelGroup.create(
            name="rival_own",
            ownership="tenant",
            owner_tenant_id=RIVAL,
            visibility="private",
            group_name="rival_own",
            chat_model="gpt-4o",
            base_url="https://api.example/v1",
            api_key="sk-rival",
            model_type="chat",
        )

        for name in ("platform_private", "rival_own"):
            result = str(await email_utils.summarize_recent_emails(ctx_of(KEY_A1), model_group=name))
            assert f"模型组 '{name}' 未找到，请检查配置" in result, name

        shared_result = str(await email_utils.summarize_recent_emails(ctx_of(KEY_A1), model_group="platform_shared"))
        assert "没有找到符合条件的邮箱账户" in shared_result
        assert "未找到" not in shared_result

        platform_result = str(
            await email_utils.summarize_recent_emails(
                ctx_of(KEY_P),
                model_group="platform_private",
                account_filter="nobody@example.com",
            ),
        )
        assert "没有找到符合条件的邮箱账户" in platform_result
        assert "模型组" not in platform_result


# ---------------------------------------------------------------------------
# 3. 新邮件通知的频道归属
# ---------------------------------------------------------------------------


def parsed_mail() -> ParsedEmail:
    return ParsedEmail(
        subject="主题",
        sender_name="发件人",
        sender_addr="sender@example.com",
        date_str="",
        html_content="",
        text_content="",
        attachments=[],
        raw_bytes=b"",
    )


@pytest.fixture
def notified(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    from nekro_agent.adapters.email import adapter as email_adapter_module

    pushed: list[Any] = []

    async def fake_push(**kwargs: Any) -> None:
        pushed.append(kwargs)

    monkeypatch.setattr(email_adapter_module.message_service, "push_untrusted_message", fake_push)
    return pushed


def fake_email_self(chat_key: str) -> SimpleNamespace:
    """只够跑 `_notify_new_email` 平台方那一支的假适配器

    这组用例钉的是「通知频道必须归平台方」；`email_utils` 在频道上是否生效另有用例，
    这里按生效处理。通知正文用真的构造函数，免得两边的形状各写一份。
    """
    import types

    fake = SimpleNamespace(
        config=SimpleNamespace(
            EMAIL_NOTIFICATIONS_ENABLED=True,
            EMAIL_NOTIFICATIONS_CHAT_KEY=chat_key,
        ),
        _email_id_text=lambda _raw: "1",
        instance_account_owner={},
    )

    async def email_utils_effective(_self: Any, _chat_key: str) -> bool:
        return True

    fake._email_utils_effective = types.MethodType(email_utils_effective, fake)
    fake._build_email_notice = types.MethodType(EmailAdapter._build_email_notice, fake)  # noqa: SLF001
    return fake


class TestNotificationChannel:
    async def test_a_platform_channel_still_gets_the_notice(self, db, notified):
        pushed = await EmailAdapter._notify_new_email(  # noqa: SLF001
            fake_email_self(KEY_P),
            "ops@example.com",
            "chat",
            b"1",
            parsed_mail(),
        )
        assert pushed is True
        assert len(notified) == 1
        assert notified[0]["chat_key"] == KEY_P

    async def test_a_tenant_channel_is_skipped(self, db, notified):
        pushed = await EmailAdapter._notify_new_email(  # noqa: SLF001
            fake_email_self(KEY_A1),
            "ops@example.com",
            "chat",
            b"1",
            parsed_mail(),
        )
        assert pushed is False
        assert notified == []

    async def test_a_missing_channel_is_skipped_in_a_multi_tenant_deployment(self, db, notified):
        pushed = await EmailAdapter._notify_new_email(  # noqa: SLF001
            fake_email_self(KEY_GHOST),
            "ops@example.com",
            "chat",
            b"1",
            parsed_mail(),
        )
        assert pushed is False
        assert notified == []

    async def test_a_missing_channel_still_gets_the_notice_in_a_single_tenant_deployment(self, single_tenant_db, notified):
        pushed = await EmailAdapter._notify_new_email(  # noqa: SLF001
            fake_email_self(KEY_GHOST),
            "ops@example.com",
            "chat",
            b"1",
            parsed_mail(),
        )
        assert pushed is True
        assert len(notified) == 1


# ---------------------------------------------------------------------------
# 4. 表情库
# ---------------------------------------------------------------------------

EMOTION_ID = "abcd1234"
NOT_FOUND = f"Error: Emotion with ID '{EMOTION_ID}' not found"
NO_RESULTS = "No emotions found for query: '开心'. Try another search term or collect more emotions."
EMOTION_UNAVAILABLE = "表情库在当前频道不可用"


@pytest.fixture
def emotion(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """把表情文件目录改道到临时目录，并清掉作用域解析缓存

    模块不再持有 `store_dir` 这类可变单例（那正是本轮删掉的东西），文件目录只在服务层有
    一份常量，所以这里改的是服务层的那一份。缓存按 `one_time_code` 存，进程内会跨用例留下
    上一轮的解析结果，进去前先清一次。
    """
    from nekro_agent.services import emotion_library
    from plugins.builtin import emotion as emotion_module

    monkeypatch.setattr(emotion_library, "EMOTION_FILES_DIR", tmp_path / "emotions")
    emotion_library.clear_scope_cache()
    yield emotion_module
    emotion_library.clear_scope_cache()


@pytest.fixture
def no_downstream(monkeypatch: pytest.MonkeyPatch, emotion):
    """算嵌入、查向量库两个下游换成一碰就失败的替身"""

    async def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("被挡下的调用不该走到下游")

    monkeypatch.setattr(emotion, "generate_embedding", forbidden)
    monkeypatch.setattr(emotion, "get_qdrant_client", forbidden)


class TestEmotionIsScopedPerTenant:
    """本类原本断言「租户会话里表情库整体暂停」，现在改成新口径

    那个止血口径的由来是：改造前整库是插件存储里的一行、没有归属列，一个租户群的会话就能
    改删别人收集的表情，所以只能对租户整体关掉。表情库现在有套组、收集库与归属列，租户
    **能**用自己的收集库，只是看不到也改不动平台方与别的实例的套组。于是本类的重点移到
    仍然必须成立的那一半：**解析不出归属时一律 fail-closed**（多租户部署里频道行不存在，
    或频道行没有实例段），注入为空、方法按不存在、命令不响应。

    没有删掉原来的用例：它们钉的是同一条判据的另一头，即「宁可什么都不给」。
    """

    async def test_the_prompt_injection_is_empty(self, db, emotion, no_downstream):
        assert await emotion.emotion_prompt_inject(ctx_of(KEY_GHOST)) == ""

    async def test_collect_reports_the_library_unavailable(self, db, emotion, no_downstream):
        """解析不出归属的频道没有收集库：按不可用拒绝，且不走到读图与嵌入"""
        with pytest.raises(ValueError) as error:
            await emotion.collect_emotion(ctx_of(KEY_GHOST), "/app/shared/a.png", "描述", [], True)
        assert str(error.value) == "Error: 表情库在当前频道不可用"

    async def test_the_three_id_methods_answer_not_found(self, db, emotion, no_downstream):
        with pytest.raises(ValueError) as update_error:
            await emotion.update_emotion(ctx_of(KEY_GHOST), EMOTION_ID, "描述", [])
        with pytest.raises(ValueError) as remove_error:
            await emotion.remove_emotion(ctx_of(KEY_GHOST), EMOTION_ID)
        with pytest.raises(ValueError) as path_error:
            await emotion.get_emotion_path(ctx_of(KEY_GHOST), EMOTION_ID)
        assert str(update_error.value) == NOT_FOUND
        assert str(remove_error.value) == NOT_FOUND
        assert str(path_error.value) == NOT_FOUND

    async def test_search_answers_with_the_no_results_message(self, db, emotion, no_downstream):
        from nekro_agent.services.agent.creator import OpenAIChatMessage

        result = await emotion.search_emotion(ctx_of(KEY_GHOST), "开心")

        assert result == OpenAIChatMessage.from_text("user", NO_RESULTS).to_dict()

    async def test_the_five_commands_are_silent_when_the_channel_is_unresolvable(self, db, emotion, no_downstream):
        """`KEY_GHOST` 在多租户部署里是一条不存在的频道：归属判不出来，五条命令都不响应"""
        context = SimpleNamespace(chat_key=KEY_GHOST)

        assert (await emotion.emo_search_cmd(context, "开心")).message == EMOTION_UNAVAILABLE
        assert (await emotion.emo_stats_cmd(context)).message == EMOTION_UNAVAILABLE
        assert (await emotion.emo_list_cmd(context)).message == EMOTION_UNAVAILABLE
        # 迁移与重建是全集合维护，不看频道：它们不该被归属挡住，只看 `-y`
        assert (await emotion.emo_migrate_cmd(context)).status == CommandResponseStatus.SUCCESS
        reindex = emotion.emo_reindex_cmd(context, "")
        first = await reindex.__anext__()
        assert first.message == "请输入 -y 确认重建表情包索引"

    async def test_the_platform_session_reads_its_own_collection(self, db, emotion, monkeypatch: pytest.MonkeyPatch):
        """平台方实例的频道有收集库，注入正常渲染「还没有」那一句"""
        prompt = await emotion.emotion_prompt_inject(ctx_of(KEY_P))
        assert prompt.startswith("Recent Emotions: No emotions added yet.")

    async def test_a_single_tenant_deployment_also_has_a_collection(self, single_tenant_db, emotion):
        """单租户部署里 `plat1` 仍是平台方，但频道行带实例段，因此同样有一个收集库"""
        prompt = await emotion.emotion_prompt_inject(ctx_of(KEY_P))
        assert prompt.startswith("Recent Emotions: No emotions added yet.")

