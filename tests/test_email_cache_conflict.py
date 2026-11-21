"""同一个地址换主人时，缓存行不会被新主人继承

## 这条要防的是什么

`DBEmail` 的唯一键是 `(account_username, email_uid)`——地址 + UID，**没有归属**。所以：

- 平台方把 `jobs@example.test` 交给实例 A，实例 A 收了几封信；后来这个地址转给了实例 B。
  实例 B 的第一封信如果 UID 与旧行相同，`update_or_create` 会把旧行整行改写（内容换成 B 的，
  这没问题），但**没撞上的那些行**仍带着 A 的快照留在表里；
- 读侧按快照过滤，所以 B 看不到它们——可它们占着 UID，且永远等不到下一次改写。

四处收口，这里各钉一个：

1. 写侧：两条收信路径都用 `update_or_create` + **全量默认值**（含内容列与快照），撞上旧行时
   整行改写，不会留下「归属是我的、内容是别人的」这种混合行；
2. 建账户 / 改地址 / 平台方建账户 / 平台方改地址：先 `purge_foreign_cache_rows`，
   把别人的残留行清掉；
3. 读侧：`cache_rows_for` 是唯一查询入口（插件与路由共用）；
4. 每日清理：`purge_orphan_cache_rows` 删掉快照对不上任何现存账户的行，挂在既有的 24h 清理上。
"""

from __future__ import annotations

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_email import DBEmail
from nekro_agent.models.db_email_account import DBEmailAccount
from nekro_agent.services import email_accounts as mail

ADAPTER = "onebot_v11"
INSTANCE_A = "inst1"
INSTANCE_B = "inst2"
PLATFORM = ("", "")
ADDRESS = "jobs@example.test"
UID = "501"


@pytest.fixture
async def db(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from nekro_agent.core import os_env

    monkeypatch.setattr(os_env.OsEnv, "DATA_DIR", str(tmp_path), raising=False)
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    mail.clear_default_sender_cache()
    for instance_key in (INSTANCE_A, INSTANCE_B):
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=instance_key,
            display_name=instance_key,
            status="online",
            provider="remote_onebot",
            tenant_id=1,
            metadata_json="",
        )
    yield tmp_path
    mail.clear_default_sender_cache()
    await Tortoise.close_connections()


async def add_account(instance_key: str = INSTANCE_A, username: str = ADDRESS) -> DBEmailAccount:
    row = await DBEmailAccount.create(
        owner_adapter_key=ADAPTER,
        owner_instance_key=instance_key,
        username=username,
        display_name="",
        provider="QQ邮箱",
        settings={},
        password="authorization-code",
        enabled=True,
        receive_enabled=True,
        send_enabled=False,
        attachment_dir="",
    )
    row.attachment_dir = mail.new_attachment_dir(row.id)
    await row.save()
    return row


async def add_cache_row(
    *,
    owner_adapter_key: str = "",
    owner_instance_key: str = "",
    subject: str = "旧主题",
    uid: str = UID,
    body: str = "旧正文",
) -> DBEmail:
    return await DBEmail.create(
        account_username=ADDRESS,
        email_uid=uid,
        subject=subject,
        sender="sender@example.test",
        date="2026-01-01 00:00:00",
        body_text=body,
        body_html="",
        has_attachments=False,
        attachment_names="[]",
        attachment_paths="[]",
        raw_content="",
        owner_adapter_key=owner_adapter_key,
        owner_instance_key=owner_instance_key,
    )


def view_of(row: DBEmailAccount) -> mail.AccountView:
    return mail.build_account_view(
        kind=mail.KIND_INSTANCE,
        username=row.username,
        display_name="",
        send_enabled=False,
        readable=True,
        owner_adapter_key=row.owner_adapter_key,
        owner_instance_key=row.owner_instance_key,
        instance_account_id=row.id,
    )


class TestPurgeForeignRows:
    async def test_rows_of_another_owner_are_removed(self, db) -> None:
        await add_cache_row(owner_adapter_key=ADAPTER, owner_instance_key=INSTANCE_A, subject="A 的信")
        await add_cache_row(owner_adapter_key="", owner_instance_key="", subject="平台方的信", uid="502")

        deleted = await mail.purge_foreign_cache_rows(
            ADDRESS,
            owner_adapter_key=ADAPTER,
            owner_instance_key=INSTANCE_B,
        )

        assert deleted == 2
        assert await DBEmail.filter(account_username=ADDRESS).count() == 0

    async def test_rows_of_the_same_owner_are_kept(self, db) -> None:
        await add_cache_row(owner_adapter_key=ADAPTER, owner_instance_key=INSTANCE_B, subject="B 的信")

        deleted = await mail.purge_foreign_cache_rows(
            ADDRESS,
            owner_adapter_key=ADAPTER,
            owner_instance_key=INSTANCE_B,
        )

        assert deleted == 0
        assert await DBEmail.filter(account_username=ADDRESS, subject="B 的信").exists()

    async def test_only_this_address_is_touched(self, db) -> None:
        await add_cache_row(owner_adapter_key=ADAPTER, owner_instance_key=INSTANCE_A)
        await add_cache_row(owner_adapter_key=ADAPTER, owner_instance_key=INSTANCE_A, uid="900")

        await mail.purge_foreign_cache_rows(
            "other@example.test",
            owner_adapter_key=ADAPTER,
            owner_instance_key=INSTANCE_B,
        )

        assert await DBEmail.filter(account_username=ADDRESS).count() == 2

    async def test_the_address_shaped_attachment_dir_goes_with_the_rows(self, db) -> None:
        """地址形态的附件目录只属于平台方账户：交给实例时一并删掉"""
        root = mail.email_attachment_root()
        directory = root / ADDRESS / UID
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "a.txt").write_text("x", encoding="utf-8")

        await mail.purge_foreign_cache_rows(
            ADDRESS,
            owner_adapter_key=ADAPTER,
            owner_instance_key=INSTANCE_A,
        )

        assert not (root / ADDRESS).exists()

    async def test_a_platform_giveaway_does_not_delete_an_instance_directory(self, db) -> None:
        """平台方接手时不动 `_i<id>` 目录：那是账户自己的，不是按地址起的"""
        row = await add_account()
        root = mail.email_attachment_root()
        kept = root / row.attachment_dir / UID
        kept.mkdir(parents=True, exist_ok=True)

        await mail.purge_foreign_cache_rows(ADDRESS, owner_adapter_key="", owner_instance_key="")

        assert kept.is_dir()


class TestReadSideFilter:
    async def test_only_the_matching_snapshot_is_readable(self, db) -> None:
        row = await add_account()
        await add_cache_row(owner_adapter_key=ADAPTER, owner_instance_key=INSTANCE_A, uid="1")
        await add_cache_row(owner_adapter_key=ADAPTER, owner_instance_key=INSTANCE_B, uid="2")
        await add_cache_row(owner_adapter_key="", owner_instance_key="", uid="3")

        rows = await (await mail.cache_rows_for(view_of(row))).order_by("email_uid")

        assert [item.email_uid for item in rows] == ["1"]

    async def test_the_platform_view_reads_only_platform_rows(self, db) -> None:
        await add_cache_row(owner_adapter_key=ADAPTER, owner_instance_key=INSTANCE_A, uid="1")
        await add_cache_row(owner_adapter_key="", owner_instance_key="", uid="3")
        view = mail.build_account_view(
            kind=mail.KIND_PLATFORM,
            username=ADDRESS,
            display_name="",
            send_enabled=False,
            readable=True,
        )

        rows = await (await mail.cache_rows_for(view)).order_by("email_uid")

        assert [item.email_uid for item in rows] == ["3"]

    async def test_the_view_of_one_instance_never_sees_another(self, db) -> None:
        # 地址在账户表里是唯一的：两个实例的账户用不同地址，但缓存行的地址与快照各自对应
        row_a = await add_account(INSTANCE_A, "a@example.test")
        row_b = await add_account(INSTANCE_B, "b@example.test")
        await DBEmail.create(
            account_username="a@example.test",
            email_uid="1",
            subject="",
            sender="",
            date="2026-01-01 00:00:00",
            body_text="",
            body_html="",
            has_attachments=False,
            attachment_names="[]",
            attachment_paths="[]",
            raw_content="",
            owner_adapter_key=ADAPTER,
            owner_instance_key=INSTANCE_A,
        )
        await DBEmail.create(
            account_username="b@example.test",
            email_uid="2",
            subject="",
            sender="",
            date="2026-01-01 00:00:00",
            body_text="",
            body_html="",
            has_attachments=False,
            attachment_names="[]",
            attachment_paths="[]",
            raw_content="",
            owner_adapter_key=ADAPTER,
            owner_instance_key=INSTANCE_B,
        )

        assert [item.email_uid for item in await (await mail.cache_rows_for(view_of(row_a))).order_by("email_uid")] == ["1"]
        assert [item.email_uid for item in await (await mail.cache_rows_for(view_of(row_b))).order_by("email_uid")] == ["2"]


class TestOrphanCleanup:
    async def test_rows_without_an_account_are_removed(self, db) -> None:
        await add_cache_row(owner_adapter_key=ADAPTER, owner_instance_key="gone", uid="1")
        await add_cache_row(owner_adapter_key="", owner_instance_key="", uid="2")

        deleted = await mail.purge_orphan_cache_rows()

        assert deleted == 2
        assert await DBEmail.all().count() == 0

    async def test_rows_with_a_live_account_survive(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        row = await add_account()
        await add_cache_row(owner_adapter_key=ADAPTER, owner_instance_key=INSTANCE_A, uid="1")

        deleted = await mail.purge_orphan_cache_rows()

        assert deleted == 0
        assert await DBEmail.filter(account_username=row.username).count() == 1

    async def test_platform_rows_follow_the_receive_accounts_list(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.adapters.email.config import EmailAccount

        await add_cache_row(owner_adapter_key="", owner_instance_key="", uid="1")
        monkeypatch.setattr(
            mail,
            "platform_accounts",
            lambda *args, **kwargs: [EmailAccount(USERNAME=ADDRESS, ENABLED=True, RECEIVE_ENABLED=True)],
        )

        assert await mail.purge_orphan_cache_rows() == 0
        assert await DBEmail.all().count() == 1


class TestOverwriteOnCollision:
    """写侧兜底：撞上同一个 (地址, UID) 时整行改写，不留混合行"""

    async def test_a_colliding_row_is_fully_rewritten(self, db) -> None:
        await add_cache_row(owner_adapter_key=ADAPTER, owner_instance_key=INSTANCE_A, subject="旧", body="旧正文")

        await DBEmail.update_or_create(
            defaults={
                "subject": "新",
                "body_text": "新正文",
                "sender": "new@example.test",
                "date": "2026-02-02 00:00:00",
                "body_html": "",
                "has_attachments": False,
                "attachment_names": "[]",
                "attachment_paths": "[]",
                "raw_content": "",
                "owner_adapter_key": ADAPTER,
                "owner_instance_key": INSTANCE_B,
            },
            account_username=ADDRESS,
            email_uid=UID,
        )

        row = await DBEmail.get(account_username=ADDRESS, email_uid=UID)
        assert row.subject == "新" and row.body_text == "新正文"
        assert (row.owner_adapter_key, row.owner_instance_key) == (ADAPTER, INSTANCE_B)
        assert await DBEmail.all().count() == 1
