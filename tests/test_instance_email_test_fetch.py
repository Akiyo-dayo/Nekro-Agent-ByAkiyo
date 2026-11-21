"""实例自有邮箱的「测连接」与「手动收一次」

## 为什么这两个端点要单独钉

两个都是**带凭据发起出站连接**的动作，而且都由租户触发。它们各自的坑不一样：

- `test`：只测 SMTP 会把「能发不能收」显示成正常——登录成功但信箱不可读（权限没开、容量满）
  是真实故障，必须 `select INBOX` 一次才算测过收信。自定义服务器要在**连接之前**再校验一次
  主机端口：保存时解析到公网、测试时解析到内网（DNS 重绑）就是这一步挡住的；
- `fetch`：手动收信必须走**实例自有那条处理路径**（不建频道、只写带归属快照的缓存），
  否则点一次「收一次」就等于给这个地址开了一条把信推进平台方会话的路。而且账户停用、
  收信关着、总闸关着时都要拒绝——三个开关任意一个关着时，点按钮也不该绕过它。

两处都用打桩替换真实的网络与客户端：这里测的是「谁被调用了、参数对不对、结果怎么落库」。
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.adapters.email.config import EmailAccount
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_email_account import DBEmailAccount
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import instance_email as routes
from nekro_agent.routers.instance_email import (
    fetch_instance_email_account,
)
from nekro_agent.routers.instance_email import (
    test_instance_email_account as run_connection_test,
)
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services import email_accounts as mail
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
OWN_INSTANCE = "inst1"
FOREIGN_INSTANCE = "rival1"
TENANT_ID = 1
ADDRESS = "own@example.test"


def platform_owner() -> DBUser:
    return DBUser(
        username="owner",
        password="",
        adapter_key="",
        platform_userid="0",
        perm_level=Role.Super,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


def tenant_admin() -> DBUser:
    return DBUser(
        username="acme-admin",
        password="",
        adapter_key="",
        platform_userid="0",
        perm_level=Role.Admin,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite::memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    mail.clear_default_sender_cache()
    for instance_key, tenant_id in ((OWN_INSTANCE, TENANT_ID), (FOREIGN_INSTANCE, 2)):
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
            channel_id=f"group_{instance_key}",
            chat_key=f"{ADAPTER}-{instance_key}-group_{instance_key}",
            channel_type="group",
            data="{}",
        )
    try:
        yield
    finally:
        mail.clear_default_sender_cache()
        await Tortoise.close_connections()


@pytest.fixture
def fake_adapter(monkeypatch: pytest.MonkeyPatch):
    """把「取适配器」换成一只记调用的小对象"""

    class FakeAdapter:
        def __init__(self) -> None:
            self.instance_account_owner: Dict[str, Any] = {}
            self.pulled: List[EmailAccount] = []
            self.result: Any = {"processed_count": 0, "found_count": 0}

        async def manual_pull_account(self, account: EmailAccount, **_kwargs: Any) -> Any:
            self.pulled.append(account)
            return self.result

    adapter = FakeAdapter()
    monkeypatch.setattr(routes, "_get_email_adapter", lambda: adapter)
    monkeypatch.setattr(mail, "sync_runtime_account", _noop)
    monkeypatch.setattr(mail, "remove_runtime_account", _noop)
    return adapter


async def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


async def add_account(
    *,
    instance_key: str = OWN_INSTANCE,
    provider: str = "QQ邮箱",
    settings: Optional[Dict[str, Any]] = None,
    enabled: bool = True,
    receive_enabled: bool = True,
) -> DBEmailAccount:
    return await DBEmailAccount.create(
        owner_adapter_key=ADAPTER,
        owner_instance_key=instance_key,
        username=ADDRESS,
        display_name="",
        provider=provider,
        settings=dict(settings or {}),
        password="authorization-code",
        enabled=enabled,
        receive_enabled=receive_enabled,
        send_enabled=False,
        attachment_dir="",
    )


class TestConnectionTest:
    async def test_both_protocols_are_tested_and_recorded(
        self, db, fake_adapter, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        row = await add_account()
        smtp_calls: List[EmailAccount] = []
        imap_calls: List[EmailAccount] = []

        async def fake_smtp(account: EmailAccount) -> str:
            smtp_calls.append(account)
            return "SMTP 连接成功"

        async def fake_imap(account: EmailAccount) -> str:
            imap_calls.append(account)
            return "IMAP 连接成功"

        monkeypatch.setattr(routes, "_test_smtp_connection", fake_smtp)
        monkeypatch.setattr(routes, "_test_imap_connection", fake_imap)

        result = await run_connection_test(
            row.id,
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
            _current_user=platform_owner(),
        )

        assert len(smtp_calls) == 1 and len(imap_calls) == 1
        assert result["success"] is True
        assert result["smtp"]["success"] is True and result["imap"]["success"] is True
        settings = (await DBEmailAccount.get(id=row.id)).settings
        assert settings["last_test_smtp"] is True
        assert settings["last_test_imap"] is True
        assert "last_receive_error" not in settings

    async def test_a_receiving_failure_is_not_reported_as_success(
        self, db, fake_adapter, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """能发不能收 = 不可用：整体 success 必须是假，且失败原因落在收信那一项上"""
        row = await add_account()

        async def fake_smtp(_account: EmailAccount) -> str:
            return "SMTP 连接成功"

        async def fake_imap(_account: EmailAccount) -> str:
            raise RuntimeError("mailbox unavailable")

        monkeypatch.setattr(routes, "_test_smtp_connection", fake_smtp)
        monkeypatch.setattr(routes, "_test_imap_connection", fake_imap)

        result = await run_connection_test(
            row.id,
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
            _current_user=platform_owner(),
        )

        assert result["success"] is False
        assert result["smtp"]["success"] is True and result["imap"]["success"] is False
        settings = (await DBEmailAccount.get(id=row.id)).settings
        assert settings["last_test_smtp"] is True
        assert settings["last_test_imap"] is False
        # 回给页面（以及写进账户行、后台会显示）的是一句笼统的话：原始异常里有对端的 banner、
        # 端口状态与解析结果，租户能拿「测试连接」当端口扫描器用
        assert settings["last_receive_error"] == routes.TEST_FAILED_CONNECT
        assert "mailbox unavailable" not in str(result)

    async def test_the_gate_blocks_it(self, db, fake_adapter, monkeypatch: pytest.MonkeyPatch) -> None:
        row = await add_account()
        monkeypatch.setattr(mail, "instance_accounts_enabled", lambda: False)

        with pytest.raises(ValidationError):
            await run_connection_test(
                row.id,
                adapter_key=ADAPTER,
                instance_key=OWN_INSTANCE,
                _current_user=platform_owner(),
            )

    async def test_a_tenant_cannot_test_a_foreign_account(self, db, fake_adapter) -> None:
        from nekro_agent.schemas.errors import NotFoundError

        row = await add_account(instance_key=FOREIGN_INSTANCE)

        with pytest.raises(NotFoundError):
            await run_connection_test(
                row.id,
                adapter_key=ADAPTER,
                instance_key=FOREIGN_INSTANCE,
                _current_user=tenant_admin(),
            )


class TestTheGate:
    """总闸关掉时的三件事：会话里没有、新建被拒、列表照常可看"""

    async def test_the_session_has_no_instance_accounts(
        self, db, fake_adapter, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await add_account()
        enabled = await mail.instance_accounts(
            ADAPTER, OWN_INSTANCE, is_platform_instance=False,
        )
        assert [view.username for view in enabled.readable] == [ADDRESS]

        monkeypatch.setattr(mail, "instance_accounts_enabled", lambda: False)
        gated = await mail.instance_accounts(
            ADAPTER, OWN_INSTANCE, is_platform_instance=False,
        )
        assert gated.readable == [] and gated.sendable == []

    async def test_the_gate_reads_the_platform_setting(self, db, fake_adapter, monkeypatch: pytest.MonkeyPatch) -> None:
        """判据读的是平台方那一项配置本身（别的用例替换了判据函数，这一条钉住函数与配置的接线）"""
        from nekro_agent.core.config import config as core_config

        await add_account()
        monkeypatch.setattr(core_config, "EMAIL_INSTANCE_ACCOUNTS_ENABLED", False)

        assert mail.instance_accounts_enabled() is False
        gated = await mail.instance_accounts(ADAPTER, OWN_INSTANCE, is_platform_instance=False)
        assert gated.readable == [] and gated.sendable == []

        monkeypatch.setattr(core_config, "EMAIL_INSTANCE_ACCOUNTS_ENABLED", True)
        assert mail.instance_accounts_enabled() is True

    async def test_create_is_refused(self, db, fake_adapter, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.routers.instance_email import (
            InstanceEmailAccountPayload,
            create_instance_email_account,
        )

        monkeypatch.setattr(mail, "instance_accounts_enabled", lambda: False)
        payload = InstanceEmailAccountPayload(
            username="another@example.test",
            password="authorization-code",
            display_name="",
            provider="QQ邮箱",
            settings={},
            enabled=True,
            receive_enabled=True,
            send_enabled=False,
            is_default_sender=False,
            notify_chat_key="",
        )

        with pytest.raises(ValidationError):
            await create_instance_email_account(
                payload,
                adapter_key=ADAPTER,
                instance_key=OWN_INSTANCE,
                _current_user=platform_owner(),
            )
        assert await DBEmailAccount.all().count() == 0

    async def test_the_list_still_works(self, db, fake_adapter, monkeypatch: pytest.MonkeyPatch) -> None:
        """管理页要显示存量：列表不跟着关，只把动作挡住"""
        from nekro_agent.routers.instance_email import list_instance_email_accounts

        row = await add_account()
        monkeypatch.setattr(mail, "instance_accounts_enabled", lambda: False)

        rows = await list_instance_email_accounts(
            ADAPTER,
            OWN_INSTANCE,
            _current_user=platform_owner(),
        )

        assert [item.id for item in rows] == [row.id]


class TestManualFetch:
    async def test_the_instance_path_is_used(self, db, fake_adapter) -> None:
        """收信走 `instance_account_owner` 登记过的账户：没有登记就会被当成平台方账户"""
        row = await add_account()
        fake_adapter.result = {"processed_count": 3, "found_count": 3}

        result = await fetch_instance_email_account(
            row.id,
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
            _current_user=platform_owner(),
        )

        assert result == {"success": True, "fetched": 3}
        assert [account.USERNAME for account in fake_adapter.pulled] == [ADDRESS]
        owner = fake_adapter.instance_account_owner[ADDRESS]
        assert (owner.adapter_key, owner.instance_key) == (ADAPTER, OWN_INSTANCE)
        assert owner.account_id == row.id

    async def test_nothing_is_returned_about_the_content(self, db, fake_adapter) -> None:
        row = await add_account()
        fake_adapter.result = {"processed_count": 1, "subject": "不该回给页面的主题"}

        result = await fetch_instance_email_account(
            row.id,
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
            _current_user=platform_owner(),
        )

        assert set(result) == {"success", "fetched"}

    @pytest.mark.parametrize("reason", ["disabled", "receive-off", "gate"])
    async def test_it_refuses_when_the_account_cannot_receive(
        self, db, fake_adapter, monkeypatch: pytest.MonkeyPatch, reason: str,
    ) -> None:
        row = await add_account(
            enabled=reason != "disabled",
            receive_enabled=reason != "receive-off",
        )
        if reason == "gate":
            monkeypatch.setattr(mail, "instance_accounts_enabled", lambda: False)

        with pytest.raises(ValidationError):
            await fetch_instance_email_account(
                row.id,
                adapter_key=ADAPTER,
                instance_key=OWN_INSTANCE,
                _current_user=platform_owner(),
            )
        assert fake_adapter.pulled == []

    async def test_a_custom_host_is_rechecked_before_the_pull(
        self, db, fake_adapter, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        row = await add_account(
            provider="自定义",
            settings={
                "imap_host": "imap.example.test",
                "imap_port": 993,
                "smtp_host": "smtp.example.test",
                "smtp_port": 465,
                "smtp_ssl_port": 465,
            },
        )
        checked: List[tuple[str, int]] = []

        async def refuse(host: str, port: int) -> None:
            checked.append((host, port))
            raise ValueError(mail.CUSTOM_HOST_UNAVAILABLE)

        monkeypatch.setattr(mail, "assert_custom_mail_host_allowed", refuse)

        with pytest.raises(ValidationError):
            await fetch_instance_email_account(
                row.id,
                adapter_key=ADAPTER,
                instance_key=OWN_INSTANCE,
                _current_user=platform_owner(),
            )
        assert checked == [("imap.example.test", 993)]
        assert fake_adapter.pulled == []

    async def test_a_failure_is_a_validation_error_not_a_500(self, db, fake_adapter) -> None:
        row = await add_account()

        async def boom(_account: EmailAccount, **_kwargs: Any) -> Any:
            raise RuntimeError("imap down")

        fake_adapter.manual_pull_account = boom  # type: ignore[method-assign]

        with pytest.raises(ValidationError):
            await fetch_instance_email_account(
                row.id,
                adapter_key=ADAPTER,
                instance_key=OWN_INSTANCE,
                _current_user=platform_owner(),
            )

    async def test_refused_and_timed_out_read_the_same(self, db, fake_adapter) -> None:
        """异常类型名也不回：`ConnectionRefusedError` 与 `TimeoutError` 分得开，就能拿「收一次」探测端口"""
        row = await add_account()
        messages = []
        for error in (ConnectionRefusedError("refused by the peer"), TimeoutError("timed out")):

            async def boom(_account: EmailAccount, _error: BaseException = error, **_kwargs: Any) -> Any:
                raise _error

            fake_adapter.manual_pull_account = boom  # type: ignore[method-assign]
            with pytest.raises(ValidationError) as excinfo:
                await fetch_instance_email_account(
                    row.id,
                    adapter_key=ADAPTER,
                    instance_key=OWN_INSTANCE,
                    _current_user=platform_owner(),
                )
            messages.append(str(excinfo.value.params["reason"]))

        assert messages[0] == messages[1] == f"收信失败: {mail.MAIL_FAILURE_TEXTS[mail.MAIL_FAILURE_CONNECT]}"
