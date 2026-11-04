"""Bot 连接状态邮件：未认领实例不发，其余限频

修复前：OneBot 协议端每次连上、断开，连接钩子都先发一封状态邮件，再去同步实例状态。钩子在入站闸门之前执行，
部署级令牌为空时匿名者带一个陌生账号反复连断（每次都被自动登记成待认领实例），就能刷管理员邮箱；
已认领的账号网络抖动时同样一断一封。模板里的 Bot ID 来自握手请求头，原样进 HTML 正文。

现在：钩子先同步实例状态（陌生账号在这一步被登记），再按登记后的实例决定发不发——只给已认领、在用
（未停用 / 归档 / 软删）的 OneBot 实例发；发信本身按 Bot 与总量限频，窗口内最后一封写明之后暂不单独发信；
限频计数落盘在 `data/system` 下、按墙钟记，进程反复重启也接着用；模板按 HTML 转义。
"""

import asyncio
import inspect
import json
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple

import pytest
from tortoise import Tortoise

import nekro_agent
from nekro_agent.adapters.onebot_v11.core import endpoint_status
from nekro_agent.adapters.onebot_v11.core import instance as instance_mod
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.services.mail import mail_service

ONEBOT = "onebot_v11"
ONEBOT_NAME = "OneBot V11"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(mail_service, "time", fake, raising=False)
    return fake


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from nekro_agent.core.os_env import OsEnv

    path = tmp_path / "data"
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(path))
    return path


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch, clock: _Clock, data_dir: Path) -> List[EmailMessage]:  # noqa: ARG001
    """邮件开着、发信打桩成只记账；限频记录落在临时数据目录，每条用例从空起步"""
    messages: List[EmailMessage] = []

    async def _send(message: EmailMessage) -> None:
        messages.append(message)

    monkeypatch.setattr(mail_service, "send_email", _send)
    monkeypatch.setattr(
        mail_service,
        "_get_runtime_mail_settings",
        lambda: {"enabled": True, "username": "bot@example.com", "targets": ["admin@example.com"]},
    )
    # raising=False：在修复前的代码上这几项不存在，用例照样跑到断言、按行为失败
    monkeypatch.setattr(mail_service, "_status_mail_log", None, raising=False)
    monkeypatch.setattr(mail_service, "_status_mail_suppression_warned", {}, raising=False)
    return messages


def restart_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟进程重启：内存里的一切清空，落盘的记录还在"""
    monkeypatch.setattr(mail_service, "_status_mail_log", None, raising=False)
    monkeypatch.setattr(mail_service, "_status_mail_suppression_warned", {}, raising=False)
    monkeypatch.setattr(instance_mod, "_account_to_instance", {})
    monkeypatch.setattr(instance_mod, "_instance_to_account", {})


def body_of(message: EmailMessage) -> str:
    return message.get_content()


class TestRateLimit:
    async def test_one_bot_flapping_is_capped(self, sent: List[EmailMessage]) -> None:
        for index in range(mail_service.STATUS_MAIL_PER_BOT_LIMIT + 3):
            await mail_service.send_bot_status_email(ONEBOT_NAME, "20001", index % 2 == 0)

        assert len(sent) == mail_service.STATUS_MAIL_PER_BOT_LIMIT

    async def test_the_last_mail_in_the_window_says_so(self, sent: List[EmailMessage]) -> None:
        for _ in range(mail_service.STATUS_MAIL_PER_BOT_LIMIT):
            await mail_service.send_bot_status_email(ONEBOT_NAME, "20001", False)

        assert all("不再单独发信" not in body_of(message) for message in sent[:-1])
        assert "不再单独发信" in body_of(sent[-1])

    async def test_window_reopens(self, sent: List[EmailMessage], clock: _Clock) -> None:
        for _ in range(mail_service.STATUS_MAIL_PER_BOT_LIMIT + 1):
            await mail_service.send_bot_status_email(ONEBOT_NAME, "20001", True)
        assert len(sent) == mail_service.STATUS_MAIL_PER_BOT_LIMIT

        clock.now += mail_service.STATUS_MAIL_WINDOW_SECONDS + 1
        await mail_service.send_bot_status_email(ONEBOT_NAME, "20001", True)

        assert len(sent) == mail_service.STATUS_MAIL_PER_BOT_LIMIT + 1

    async def test_many_bots_are_capped_in_total(self, sent: List[EmailMessage]) -> None:
        for index in range(mail_service.STATUS_MAIL_TOTAL_LIMIT + 5):
            await mail_service.send_bot_status_email(ONEBOT_NAME, str(30000 + index), True)

        assert len(sent) == mail_service.STATUS_MAIL_TOTAL_LIMIT
        assert "所有 Bot" in body_of(sent[-1])

    async def test_control_another_bot_has_its_own_allowance(self, sent: List[EmailMessage]) -> None:
        for _ in range(mail_service.STATUS_MAIL_PER_BOT_LIMIT + 1):
            await mail_service.send_bot_status_email(ONEBOT_NAME, "20001", True)
        await mail_service.send_bot_status_email(ONEBOT_NAME, "20002", True)

        assert len(sent) == mail_service.STATUS_MAIL_PER_BOT_LIMIT + 1

    async def test_mail_switched_off_uses_no_allowance(self, sent: List[EmailMessage], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mail_service, "_get_runtime_mail_settings", lambda: {"enabled": False})
        for _ in range(mail_service.STATUS_MAIL_PER_BOT_LIMIT + 1):
            await mail_service.send_bot_status_email(ONEBOT_NAME, "20001", True)
        assert sent == []

        monkeypatch.setattr(
            mail_service,
            "_get_runtime_mail_settings",
            lambda: {"enabled": True, "username": "bot@example.com", "targets": []},
        )
        for _ in range(mail_service.STATUS_MAIL_PER_BOT_LIMIT):
            await mail_service.send_bot_status_email(ONEBOT_NAME, "20001", True)
        assert len(sent) == mail_service.STATUS_MAIL_PER_BOT_LIMIT

    async def test_suppression_warns_once_per_window(self, sent: List[EmailMessage], monkeypatch: pytest.MonkeyPatch) -> None:
        warnings: List[str] = []

        class _Recorder:
            def __getattr__(self, _name: str) -> Any:
                return lambda *_a, **_k: None

            def warning(self, message: str, *_a: Any, **_k: Any) -> None:
                warnings.append(message)

        monkeypatch.setattr(mail_service, "logger", _Recorder())
        for _ in range(mail_service.STATUS_MAIL_PER_BOT_LIMIT + 5):
            await mail_service.send_bot_status_email(ONEBOT_NAME, "20001", True)

        assert len(warnings) == 1
        assert "20001" in warnings[0]

    async def test_suppression_bookkeeping_is_bounded(self, sent: List[EmailMessage], monkeypatch: pytest.MonkeyPatch) -> None:
        """记着「告警过」的 Bot 数有上限：被挡下的 Bot 再多，这张表也不会无限变大"""
        monkeypatch.setattr(mail_service, "_STATUS_MAIL_WARN_TRACK_LIMIT", 5)
        for index in range(mail_service.STATUS_MAIL_TOTAL_LIMIT + 20):
            await mail_service.send_bot_status_email(ONEBOT_NAME, str(50000 + index), True)

        assert len(mail_service._status_mail_suppression_warned) == 5  # noqa: SLF001

    async def test_expired_records_are_dropped(self, sent: List[EmailMessage], clock: _Clock) -> None:
        for index in range(3):
            await mail_service.send_bot_status_email(ONEBOT_NAME, str(60000 + index), True)
        clock.now += mail_service.STATUS_MAIL_WINDOW_SECONDS + 1
        await mail_service.send_bot_status_email(ONEBOT_NAME, "60009", True)

        assert [item[1] for item in mail_service._status_mail_log] == ["60009"]  # noqa: SLF001


class TestTemplate:
    async def test_bot_id_is_html_escaped(self, sent: List[EmailMessage]) -> None:
        await mail_service.send_bot_status_email(ONEBOT_NAME, '<img src=x onerror="alert(1)">', True)

        body = body_of(sent[0])
        assert "<img" not in body
        assert "&lt;img" in body

    @pytest.mark.parametrize(("is_online", "headline"), [(True, "上线"), (False, "下线")])
    async def test_control_both_states_still_render(self, sent: List[EmailMessage], is_online: bool, headline: str) -> None:
        await mail_service.send_bot_status_email(ONEBOT_NAME, "20001", is_online)

        body = body_of(sent[0])
        assert headline in body and "20001" in body and ONEBOT_NAME in body


# ---------------------------------------------------------------------------
# 连接钩子：按登记之后的实例决定发不发
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def add_instance(instance_key: str, *, status: str, account: str, enabled: bool = True) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=ONEBOT,
        instance_key=instance_key,
        display_name=instance_key,
        status=status,
        enabled=enabled,
        provider="remote_onebot",
        provider_account_id=account,
        metadata_json=json.dumps({"access_token": f"own-{instance_key}"}),
    )


@pytest.fixture
def hook(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, str, bool]]:
    """记下钩子发出的状态邮件；协议端自检与内置 NapCat 登录归属推导打桩，账号缓存每条用例从空起步"""
    mails: List[Tuple[str, str, bool]] = []

    async def _mail(adapter: str, bot_id: str, is_online: bool) -> None:
        mails.append((adapter, bot_id, is_online))

    async def _no_probe(_bot: Any) -> None:
        return None

    async def _no_builtin_claim(_self_id: str) -> Optional[DBAdapterInstance]:
        return None

    monkeypatch.setattr(nekro_agent, "send_bot_status_email", _mail)
    monkeypatch.setattr(endpoint_status, "on_onebot_connect", _no_probe)
    monkeypatch.setattr(instance_mod, "_claim_builtin_napcat_login", _no_builtin_claim)
    monkeypatch.setattr(instance_mod, "_account_to_instance", {})
    monkeypatch.setattr(instance_mod, "_instance_to_account", {})
    return mails


def fake_bot(self_id: str, adapter_name: str = ONEBOT_NAME) -> Any:
    return SimpleNamespace(adapter=SimpleNamespace(get_name=lambda: adapter_name), self_id=self_id)


async def connect(self_id: str, adapter_name: str = ONEBOT_NAME) -> None:
    await nekro_agent._on_bot_status_change(fake_bot(self_id, adapter_name), is_online=True)  # noqa: SLF001


async def disconnect(self_id: str, adapter_name: str = ONEBOT_NAME) -> None:
    await nekro_agent._on_bot_status_change(fake_bot(self_id, adapter_name), is_online=False)  # noqa: SLF001


class TestConnectionHook:
    async def test_anonymous_account_flapping_sends_nothing(self, db, hook: List[Tuple[str, str, bool]]) -> None:
        for _ in range(3):
            await connect("30001")
            await disconnect("30001")

        assert hook == []
        row = await DBAdapterInstance.get_by_bound_account(ONEBOT, "30001")
        assert row is not None and row.status == "unclaimed"

    async def test_existing_unclaimed_instance_sends_nothing(self, db, hook: List[Tuple[str, str, bool]]) -> None:
        await add_instance("anon", status="unclaimed", account="30002")

        await connect("30002")
        await disconnect("30002")

        assert hook == []

    @pytest.mark.parametrize(
        ("status", "enabled"),
        [
            ("archived", False),
            ("disabled", False),
            ("online", False),
            # 两列不一致的脏行：与收发闸门一样，以界面上显示的「已归档 / 已停用」为准
            ("archived", True),
            ("disabled", True),
        ],
    )
    async def test_instance_out_of_service_sends_nothing(
        self, db, hook: List[Tuple[str, str, bool]], status: str, enabled: bool,
    ) -> None:
        """归档是管理员对不认识的待认领实例的处理；停用同理：之后再连上来不发信"""
        await add_instance("parked", status=status, account="30003", enabled=enabled)

        await connect("30003")
        await disconnect("30003")

        assert hook == []

    async def test_instance_of_a_suspended_tenant_sends_nothing(self, db, hook: List[Tuple[str, str, bool]]) -> None:
        from nekro_agent.models.db_tenant import DBTenant

        await DBTenant.create(id=9, name="acme", display_name="acme", status="suspended")
        row = await add_instance("tenant_inst", status="online", account="40003")
        row.tenant_id = 9
        await row.save(update_fields=["tenant_id"])

        await connect("40003")
        await disconnect("40003")

        assert hook == []

    async def test_claimed_instance_is_mailed(self, db, hook: List[Tuple[str, str, bool]]) -> None:
        await add_instance("platform", status="offline", account="20001")

        await connect("20001")
        await disconnect("20001")

        assert hook == [(ONEBOT_NAME, "20001", True), (ONEBOT_NAME, "20001", False)]

    async def test_account_attributed_during_status_sync_is_mailed(
        self, db, hook: List[Tuple[str, str, bool]], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """刚扫码登录的内置 NapCat：账号在同步实例状态那一步才绑上，邮件要按绑定之后的实例判"""
        napcat = await add_instance("napcat1", status="connecting", account="")

        async def _claim(self_id: str) -> Optional[DBAdapterInstance]:
            napcat.provider_account_id = self_id
            await napcat.save(update_fields=["provider_account_id"])
            return napcat

        monkeypatch.setattr(instance_mod, "_claim_builtin_napcat_login", _claim)

        await connect("20009")

        assert hook == [(ONEBOT_NAME, "20009", True)]

    async def test_mail_body_carries_no_token_or_raw_error(
        self, db, hook: List[Tuple[str, str, bool]], sent: List[EmailMessage], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """正文只有适配器名、Bot ID 与固定文案：实例令牌、最近一次报错都不进邮件"""
        row = await add_instance("platform", status="offline", account="20001")
        row.last_error = "Traceback: raw-error-detail"
        await row.save(update_fields=["last_error"])
        monkeypatch.setattr(nekro_agent, "send_bot_status_email", mail_service.send_bot_status_email)

        await connect("20001")
        await disconnect("20001")

        assert len(sent) == 2
        for message in sent:
            body = body_of(message)
            assert "own-platform" not in body and "raw-error-detail" not in body
            assert "20001" in body

    async def test_many_instances_dropping_at_once_stay_under_the_total_cap(
        self, db, hook: List[Tuple[str, str, bool]], sent: List[EmailMessage], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """NA 自己断网一类的场景：所有协议端几乎同时断开，邮件总数不超过总量上限"""
        accounts = [str(21000 + index) for index in range(mail_service.STATUS_MAIL_TOTAL_LIMIT + 5)]
        for account in accounts:
            await add_instance(f"inst{account}", status="online", account=account)
        monkeypatch.setattr(nekro_agent, "send_bot_status_email", mail_service.send_bot_status_email)

        await asyncio.gather(*(disconnect(account) for account in accounts))

        assert len(sent) == mail_service.STATUS_MAIL_TOTAL_LIMIT

    async def test_lookup_failure_sends_nothing(
        self, db, hook: List[Tuple[str, str, bool]], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """查实例抛错：不发，也不让异常冲出钩子"""
        await add_instance("platform", status="offline", account="20001")

        async def _boom(*_args: Any, **_kwargs: Any) -> Optional[DBAdapterInstance]:
            raise RuntimeError("db down")

        monkeypatch.setattr(DBAdapterInstance, "get_by_bound_account", _boom)

        await connect("20001")

        assert hook == []

    async def test_control_other_adapters_are_mailed(self, hook: List[Tuple[str, str, bool]]) -> None:
        await connect("bot-1", adapter_name="Telegram")

        assert hook == [("Telegram", "bot-1", True)]

    def test_driver_hooks_go_through_it(self) -> None:
        """NoneBot 的两个钩子只转调这一个函数，状态邮件只在它里面发"""
        source = inspect.getsource(nekro_agent)
        assert "await _on_bot_status_change(bot, is_online=True)" in source
        assert "await _on_bot_status_change(bot, is_online=False)" in source
        assert source.count("await send_bot_status_email(") == 1
        assert "await send_bot_status_email(" in inspect.getsource(nekro_agent._on_bot_status_change)  # noqa: SLF001


# ---------------------------------------------------------------------------
# 进程反复重启：计数接着用
# ---------------------------------------------------------------------------


class TestRestarts:
    """崩溃拉起的循环里，每次启动各 Bot 重连一次、崩溃前断开一次；额度不随重启回满"""

    RESTARTS = 6

    async def test_crash_loop_through_the_connection_hook_stays_under_the_per_bot_cap(
        self,
        db,
        hook: List[Tuple[str, str, bool]],
        sent: List[EmailMessage],
        clock: _Clock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        accounts = ["20001", "20002", "20003"]
        for account in accounts:
            await add_instance(f"inst{account}", status="online", account=account)
        monkeypatch.setattr(nekro_agent, "send_bot_status_email", mail_service.send_bot_status_email)

        for _ in range(self.RESTARTS):
            restart_process(monkeypatch)
            for account in accounts:
                await connect(account)
            clock.now += 20
            for account in accounts:
                await disconnect(account)
            clock.now += 10

        assert len(sent) == mail_service.STATUS_MAIL_PER_BOT_LIMIT * len(accounts)

    async def test_crash_loop_with_many_bots_stays_under_the_total_cap(
        self, sent: List[EmailMessage], clock: _Clock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bots = [str(30000 + index) for index in range(mail_service.STATUS_MAIL_TOTAL_LIMIT + 5)]

        for _ in range(self.RESTARTS):
            restart_process(monkeypatch)
            for bot_id in bots:
                await mail_service.send_bot_status_email(ONEBOT_NAME, bot_id, True)
            clock.now += 30

        assert len(sent) == mail_service.STATUS_MAIL_TOTAL_LIMIT

    async def test_the_record_lands_in_data_system(self, sent: List[EmailMessage], data_dir: Path) -> None:
        await mail_service.send_bot_status_email(ONEBOT_NAME, "20001", True)

        stored = json.loads((data_dir / "system" / mail_service.STATUS_MAIL_STATE_FILE_NAME).read_text(encoding="utf-8"))
        assert [item[:2] for item in stored["sent"]] == [[ONEBOT_NAME, "20001"]]

    async def test_window_reopens_across_a_restart(
        self, sent: List[EmailMessage], clock: _Clock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for _ in range(mail_service.STATUS_MAIL_PER_BOT_LIMIT):
            await mail_service.send_bot_status_email(ONEBOT_NAME, "20001", True)

        restart_process(monkeypatch)
        await mail_service.send_bot_status_email(ONEBOT_NAME, "20001", True)
        assert len(sent) == mail_service.STATUS_MAIL_PER_BOT_LIMIT

        clock.now += mail_service.STATUS_MAIL_WINDOW_SECONDS + 1
        restart_process(monkeypatch)
        await mail_service.send_bot_status_email(ONEBOT_NAME, "20001", True)
        assert len(sent) == mail_service.STATUS_MAIL_PER_BOT_LIMIT + 1

    async def test_corrupt_record_starts_fresh_and_is_rewritten(
        self, sent: List[EmailMessage], data_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = data_dir / "system" / mail_service.STATUS_MAIL_STATE_FILE_NAME
        state.parent.mkdir(parents=True)
        state.write_text("{not json", encoding="utf-8")

        restart_process(monkeypatch)
        await mail_service.send_bot_status_email(ONEBOT_NAME, "20001", True)

        assert len(sent) == 1
        assert len(json.loads(state.read_text(encoding="utf-8"))["sent"]) == 1

    async def test_records_far_in_the_future_do_not_hold_the_allowance_forever(
        self, sent: List[EmailMessage], clock: _Clock, data_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """时钟往回拨过一大段、或文件被改过：远在未来的记录丢掉，不让额度一直被占着"""
        state = data_dir / "system" / mail_service.STATUS_MAIL_STATE_FILE_NAME
        state.parent.mkdir(parents=True)
        future = clock.now + 10 * mail_service.STATUS_MAIL_WINDOW_SECONDS
        state.write_text(json.dumps({"sent": [[ONEBOT_NAME, "20001", future]] * 30}), encoding="utf-8")

        restart_process(monkeypatch)
        await mail_service.send_bot_status_email(ONEBOT_NAME, "20001", True)

        assert len(sent) == 1

    @pytest.mark.parametrize("kind", ["deeply_nested", "huge_integer"])
    async def test_pathological_record_starts_fresh_instead_of_raising(
        self, sent: List[EmailMessage], data_dir: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
    ) -> None:
        """深层嵌套（json 解析抛 RecursionError）、超大整数时间戳（float() 抛 OverflowError）"""
        content = (
            "[" * 100_000 + "]" * 100_000
            if kind == "deeply_nested"
            else '{"sent": [["OneBot V11", "20001", 1' + "0" * 400 + "]]}"
        )
        state = data_dir / "system" / mail_service.STATUS_MAIL_STATE_FILE_NAME
        state.parent.mkdir(parents=True)
        state.write_text(content, encoding="utf-8")

        restart_process(monkeypatch)
        await mail_service.send_bot_status_email(ONEBOT_NAME, "20001", True)
        await mail_service.send_bot_status_email(ONEBOT_NAME, "20002", True)

        assert len(sent) == 2
        assert len(json.loads(state.read_text(encoding="utf-8"))["sent"]) == 2

    async def test_bot_id_with_a_lone_surrogate_does_not_poison_the_record(
        self, sent: List[EmailMessage], data_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """记录写盘按 ASCII 转义、正文里换成替换字符：孤立代理字符不会让这一封、以及之后每一封重写整张表时都编码失败"""
        await mail_service.send_bot_status_email(ONEBOT_NAME, "bad\ud800id", True)
        await mail_service.send_bot_status_email(ONEBOT_NAME, "20002", True)

        state = data_dir / "system" / mail_service.STATUS_MAIL_STATE_FILE_NAME
        assert len(sent) == 2
        assert "bad?id" in body_of(sent[0])
        assert len(json.loads(state.read_text(encoding="utf-8"))["sent"]) == 2
        restart_process(monkeypatch)
        await mail_service.send_bot_status_email(ONEBOT_NAME, "20003", True)
        assert len(json.loads(state.read_text(encoding="utf-8"))["sent"]) == 3

    async def test_unwritable_record_still_limits_within_the_process(
        self, sent: List[EmailMessage], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _fail(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("read-only")

        monkeypatch.setattr(mail_service, "atomic_write_text", _fail)
        for _ in range(mail_service.STATUS_MAIL_PER_BOT_LIMIT + 2):
            await mail_service.send_bot_status_email(ONEBOT_NAME, "20001", True)

        assert len(sent) == mail_service.STATUS_MAIL_PER_BOT_LIMIT
