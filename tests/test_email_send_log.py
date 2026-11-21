"""发信记录：截断、不可写时不外抛、过期清理

## 为什么这些要在服务层钉一遍

发信记录是**已经发生的事**的留痕。三种错误都不会让用户看见报错：

1. **写失败外抛** → 信已经发出去了，调用方却收到异常，于是重试一次 = 发两封；
2. **不截断** → 主题与失败原因的长度由模型与上游决定，一行撑爆一张表只需要一次调用；
3. **不清理** → 表无限增长，而它只是排查用的 365 天窗口。

清理用模块里的两个时钟（`_clock` / `_wall_clock`），用例直接替换它们，
所以不必真的等一天。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from tortoise import Tortoise
from tortoise.timezone import now as db_now

from nekro_agent.models.db_email_send_log import DBEmailSendLog
from nekro_agent.services import email_accounts as mail

ADAPTER = "onebot_v11"
INSTANCE = "inst1"
TENANT_ID = 1


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    mail.reset_send_log_state()
    try:
        yield
    finally:
        mail.reset_send_log_state()
        await Tortoise.close_connections()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    """可控的两只钟：单调钟管「多久清理一次」，墙钟管「删到哪一天为止」

    墙钟必须与写库用的 `db_now` 同一个时区口径，否则裁剪点会差几小时——不用
    `datetime.now()` 就是因为它在 UTC 部署里会偏。
    """

    class Clock:
        def __init__(self) -> None:
            self.monotonic = 1000.0
            self.wall = db_now()

        def advance_days(self, days: float) -> None:
            self.monotonic += days * 86400
            self.wall = self.wall + timedelta(days=days)

    instance = Clock()
    monkeypatch.setattr(mail, "_clock", lambda: instance.monotonic)
    monkeypatch.setattr(mail, "_wall_clock", lambda: instance.wall)
    return instance


def account_view(kind: str = mail.KIND_INSTANCE, *, account_id: int = 1, username: str = "own@example.test") -> Any:
    return mail.build_account_view(
        kind=kind,
        username=username,
        display_name="",
        send_enabled=True,
        readable=True,
        instance_account_id=account_id if kind == mail.KIND_INSTANCE else None,
    )


async def record(account: Any = None, **overrides: Any) -> None:
    payload: dict[str, Any] = {
        "account": account or account_view(),
        "adapter_key": ADAPTER,
        "instance_key": INSTANCE,
        "chat_key": f"{ADAPTER}-{INSTANCE}-group_1",
        "tenant_id": TENANT_ID,
        "purpose": mail.PURPOSE_SEND_EMAIL,
        "to_address": "someone@example.test",
        "subject": "主题",
        "reason": "",
        "ok": True,
    }
    payload.update(overrides)
    await mail.record_send(**payload)


class TestLongValuesAreClipped:
    """超长字段先截断再入库"""

    async def test_subject_is_clipped_to_the_column_width(self, db, clock) -> None:
        await record(subject="x" * 400)

        row = await DBEmailSendLog.get(adapter_key=ADAPTER)
        assert len(row.subject) == 256

    async def test_reason_is_clipped_to_the_column_width(self, db, clock) -> None:
        await record(reason="y" * 800)

        row = await DBEmailSendLog.get(adapter_key=ADAPTER)
        assert len(row.reason) == 500

    async def test_error_is_clipped_to_the_column_width(self, db, clock) -> None:
        await record(ok=False, error="z" * 400)

        row = await DBEmailSendLog.get(adapter_key=ADAPTER)
        assert len(row.error) == 256

    async def test_to_address_is_clipped(self, db, clock) -> None:
        await record(to_address="a" * 600)

        row = await DBEmailSendLog.get(adapter_key=ADAPTER)
        assert len(row.to_address) == 512


class TestAccountReferenceCarriesNoAddress:
    """记录里的账户引用是哈希或表 id，不含地址

    这一列在后台会被租户看到（`/instance-email/send-logs`），所以它自己就不能带地址。
    """

    async def test_platform_account_ref_is_a_keyed_hash(self, db, clock, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """带服务端密钥的摘要：不加盐的 sha256 对 QQ 号地址几秒就能穷举回去"""
        import hashlib

        from nekro_agent.core.os_env import OsEnv

        monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "deploy-a"))
        mail.reset_account_ref_key_cache()
        try:
            await record(account=account_view(mail.KIND_PLATFORM, username="ops@example.test"))

            row = await DBEmailSendLog.get(adapter_key=ADAPTER)
            plain = hashlib.sha256(mail.normalize_username("ops@example.test").encode("utf-8")).hexdigest()[:16]
            assert row.account_kind == mail.KIND_PLATFORM
            assert "ops" not in row.account_ref
            assert "@" not in row.account_ref
            assert len(row.account_ref) == 16
            assert row.account_ref != plain, "不能是不加盐的 sha256（可以离线穷举）"

            # 另一套部署（另一把密钥）算出来的不一样：没有密钥算不出来
            monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "deploy-b"))
            mail.reset_account_ref_key_cache()
            assert mail.account_send_log_ref(account_view(mail.KIND_PLATFORM, username="ops@example.test")) != row.account_ref
            assert mail.platform_account_ref("ops@example.test").startswith("platform:")
        finally:
            mail.reset_account_ref_key_cache()

    async def test_instance_account_ref_is_the_table_id(self, db, clock) -> None:
        await record(account=account_view(mail.KIND_INSTANCE, account_id=17))

        row = await DBEmailSendLog.get(adapter_key=ADAPTER)
        assert row.account_ref == "17"


class TestWriteFailureDoesNotPropagate:
    """记录写不进去不能把已经发出的信变成失败"""

    async def test_create_failure_is_swallowed(self, db, clock, monkeypatch: pytest.MonkeyPatch) -> None:
        async def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("数据库不可用")

        monkeypatch.setattr(DBEmailSendLog, "create", boom)

        await record()  # 不抛

    async def test_purge_failure_does_not_break_the_write(self, db, clock, monkeypatch: pytest.MonkeyPatch) -> None:
        """清理那一步炸了，写记录本身仍要成功（它只是 365 天窗口的维护动作）"""

        async def boom(*_args: Any, **_kwargs: Any) -> int:
            raise RuntimeError("删不掉")

        await record(subject="第一条")
        # 只在第二次写入时让清理失败；数行数之前要把补丁撤掉，否则查不了库
        with monkeypatch.context() as patch:
            patch.setattr(DBEmailSendLog, "filter", boom)
            mail.reset_send_log_state()
            await record(subject="第二条")

        subjects = set(await DBEmailSendLog.all().values_list("subject", flat=True))
        assert subjects == {"第一条", "第二条"}


class TestPurge:
    """365 天窗口，且一天最多清理一次"""

    async def test_rows_older_than_the_window_are_deleted(self, db, clock) -> None:
        old = db_now() - timedelta(days=mail.SEND_LOG_KEEP_DAYS + 1)
        await DBEmailSendLog.create(
            account_kind=mail.KIND_INSTANCE,
            account_ref="1",
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            chat_key="",
            tenant_id=TENANT_ID,
            purpose=mail.PURPOSE_SEND_EMAIL,
            to_address="someone@example.test",
            subject="旧记录",
            reason="",
            ok=True,
            error="",
            create_time=old,
        )

        await record(subject="新记录")

        subjects = set(await DBEmailSendLog.all().values_list("subject", flat=True))
        assert subjects == {"新记录"}

    async def test_rows_inside_the_window_are_kept(self, db, clock) -> None:
        recent = db_now() - timedelta(days=mail.SEND_LOG_KEEP_DAYS - 1)
        await DBEmailSendLog.create(
            account_kind=mail.KIND_INSTANCE,
            account_ref="1",
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            chat_key="",
            tenant_id=TENANT_ID,
            purpose=mail.PURPOSE_SEND_EMAIL,
            to_address="someone@example.test",
            subject="临界内",
            reason="",
            ok=True,
            error="",
            create_time=recent,
        )

        await record(subject="新记录")

        subjects = set(await DBEmailSendLog.all().values_list("subject", flat=True))
        assert subjects == {"临界内", "新记录"}

    async def test_second_write_in_the_same_day_does_not_purge_again(self, db, clock, monkeypatch: pytest.MonkeyPatch) -> None:
        """进程里第一次写入就算一次清理，之后一天内不再删"""
        calls: list[int] = []
        real_filter = DBEmailSendLog.filter

        def counting_filter(*args: Any, **kwargs: Any) -> Any:
            if "create_time__lt" in kwargs:
                calls.append(1)
            return real_filter(*args, **kwargs)

        await record(subject="第一条")
        monkeypatch.setattr(DBEmailSendLog, "filter", counting_filter)
        await record(subject="第二条")

        assert calls == []

    async def test_the_next_day_purges_again(self, db, clock) -> None:
        await record(subject="第一条")
        old = db_now() - timedelta(days=mail.SEND_LOG_KEEP_DAYS + 1)
        await DBEmailSendLog.filter(subject="第一条").update(create_time=old)

        clock.advance_days(1.1)
        await record(subject="第二天")

        subjects = set(await DBEmailSendLog.all().values_list("subject", flat=True))
        assert subjects == {"第二天"}

    async def test_reset_state_forces_a_purge_on_the_next_write(self, db, clock) -> None:
        await record(subject="第一条")
        mail.reset_send_log_state()
        old = db_now() - timedelta(days=mail.SEND_LOG_KEEP_DAYS + 1)
        await DBEmailSendLog.filter(subject="第一条").update(create_time=old)

        await record(subject="第二条")

        subjects = set(await DBEmailSendLog.all().values_list("subject", flat=True))
        assert subjects == {"第二条"}


class TestConstants:
    """窗口与用途常量是被读侧引用的契约"""

    def test_keep_days_is_one_year(self) -> None:
        assert mail.SEND_LOG_KEEP_DAYS == 365

    def test_purpose_values(self) -> None:
        assert mail.PURPOSE_SEND_EMAIL == "send_email"
        assert mail.PURPOSE_FESTIVAL == "festival"
