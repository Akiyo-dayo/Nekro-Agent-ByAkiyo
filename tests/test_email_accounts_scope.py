"""账户合并与归属：一个实例能读 / 能发哪些邮箱

## 这里钉的是什么

`services/email_accounts.py` 是发信与收信唯一的账户来源，它的核心是一张**合并表**：

| 账户来源 | 谁能读 | 谁能发 |
|---|---|---|
| 实例自有（`email_account` 表） | 该实例 | 该实例，且 `send_enabled` |
| 平台方（适配器 `RECEIVE_ACCOUNTS`） | 只有平台方实例 | 平台方实例，或共享给该实例 |

四类错误都能让功能**照常工作**，所以必须逐条钉住：

1. **合并方向反了**：租户实例读到平台方账户 → 收件箱越权；平台方实例读不到自己的 → 功能缺失。
2. **默认发件人算错**：`send_email` 不填发件人时用的是它，算错就是「用错身份发信」。
3. **解析不出归属时给了平台方那一份**：多租户部署里那条路等于 bypass，必须 fail-closed。
4. **地址或引用泄漏**：租户会话拿到的显示名里不能有平台方账户的地址。

## 与真实调用链的关系

会话路径（`session_accounts` / `chat_accounts`）走 `resource_scope`，这里一并覆盖：
用真的 `DBChatChannel` + `DBAdapterInstance` 行让 `resolve_chat_owner` 解析出归属，
而不是把 `SessionOwner` 直接塞进去 —— 后者会让「服务层偷偷自己算身份」这类改动不被发现。
"""

from __future__ import annotations

import pytest
from tortoise import Tortoise

from nekro_agent.adapters.email.config import EmailAccount
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_email_account import DBEmailAccount
from nekro_agent.services import email_accounts as mail

ADAPTER = "onebot_v11"
OWN_INSTANCE = "inst1"
FOREIGN_INSTANCE = "rival1"
PLATFORM_INSTANCE = "plat1"

ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2

INSTANCE_LAYOUT = [
    (OWN_INSTANCE, ACME_TENANT_ID),
    (FOREIGN_INSTANCE, RIVAL_TENANT_ID),
    (PLATFORM_INSTANCE, None),
]


def chat_key_of(instance_key: str) -> str:
    return f"{ADAPTER}-{instance_key}-group_{instance_key}"


def platform_account(
    username: str,
    *,
    display_name: str = "",
    send_enabled: bool = True,
    share: str = "all_instances",
    share_instances: list[str] | None = None,
) -> EmailAccount:
    return EmailAccount(
        DISPLAY_NAME=display_name,
        USERNAME=username,
        SEND_ENABLED=send_enabled,
        IS_DEFAULT_SENDER=False,
        SEND_SHARE=share,
        SEND_SHARE_INSTANCES=list(share_instances or []),
    )


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    mail.clear_default_sender_cache()
    try:
        for instance_key, tenant_id in INSTANCE_LAYOUT:
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
                chat_key=chat_key_of(instance_key),
                channel_name=f"{instance_key} 的群",
                channel_type="group",
                data="{}",
                is_active=True,
            )
        yield
    finally:
        mail.clear_default_sender_cache()
        await Tortoise.close_connections()


@pytest.fixture
def platform_config(monkeypatch: pytest.MonkeyPatch):
    """把平台方账户换成一组可控的假配置（不碰磁盘上的适配器配置）"""

    class FakeConfig:
        RECEIVE_ACCOUNTS: list[EmailAccount] = []

    def install(accounts: list[EmailAccount]) -> FakeConfig:
        config = FakeConfig()
        config.RECEIVE_ACCOUNTS = accounts
        # `_load_platform_config` 认适配器对象上的 `.config`
        monkeypatch.setattr(mail, "_load_platform_config", lambda adapter=None: config)
        return config

    return install


async def add_instance_account(
    instance_key: str,
    username: str,
    *,
    adapter_key: str = ADAPTER,
    enabled: bool = True,
    send_enabled: bool = True,
    is_default_sender: bool = False,
) -> DBEmailAccount:
    return await DBEmailAccount.create(
        owner_adapter_key=adapter_key,
        owner_instance_key=instance_key,
        username=mail.normalize_username(username),
        display_name="",
        provider="自定义",
        settings={},
        password="secret",
        enabled=enabled,
        receive_enabled=True,
        send_enabled=send_enabled,
        is_default_sender=is_default_sender,
        attachment_dir="",
    )


class TestInstancesDoNotSeeEachOther:
    """一个实例的账户集合里不出现别的实例的账户"""

    async def test_instance_list_only_contains_its_own_rows(self, db, platform_config) -> None:
        platform_config([])
        await add_instance_account(OWN_INSTANCE, "own@example.test")
        await add_instance_account(FOREIGN_INSTANCE, "rival@example.test")

        accounts = await mail.instance_accounts(
            ADAPTER, OWN_INSTANCE, is_platform_instance=False,
        )

        assert [view.username for view in accounts.sendable] == ["own@example.test"]
        assert [view.username for view in accounts.readable] == ["own@example.test"]

    async def test_same_instance_key_under_another_adapter_is_a_different_row(self, db, platform_config) -> None:
        """`instance_key` 只在适配器内唯一：按实例对取账户，不能跨适配器串味"""
        platform_config([])
        await add_instance_account(OWN_INSTANCE, "own@example.test")
        await add_instance_account(OWN_INSTANCE, "elsewhere@example.test", adapter_key="other_adapter")

        accounts = await mail.instance_accounts(ADAPTER, OWN_INSTANCE, is_platform_instance=False)

        assert [view.username for view in accounts.sendable] == ["own@example.test"]

    async def test_disabled_rows_are_not_sendable(self, db, platform_config) -> None:
        platform_config([])
        await add_instance_account(OWN_INSTANCE, "off@example.test", enabled=False)
        await add_instance_account(OWN_INSTANCE, "on@example.test")

        accounts = await mail.instance_accounts(ADAPTER, OWN_INSTANCE, is_platform_instance=False)

        assert [view.username for view in accounts.sendable] == ["on@example.test"]


class TestPlatformAccountsAreSharedOnlyWhereIntended:
    """平台方账户按 `SEND_SHARE` 三态共享给租户实例"""

    async def test_tenant_instance_does_not_read_the_platform_mailbox(self, db, platform_config) -> None:
        """共享的是**发信**能力，不是收件箱：可读集合里一条平台方账户都不该有"""
        platform_config([platform_account("ops@example.test", display_name="通知")])

        accounts = await mail.instance_accounts(ADAPTER, OWN_INSTANCE, is_platform_instance=False)

        assert accounts.readable == []
        assert [view.kind for view in accounts.sendable] == [mail.KIND_PLATFORM]

    async def test_platform_instance_reads_platform_accounts(self, db, platform_config) -> None:
        platform_config([platform_account("ops@example.test", display_name="通知")])

        accounts = await mail.instance_accounts(ADAPTER, PLATFORM_INSTANCE, is_platform_instance=True)

        assert [view.username for view in accounts.readable] == ["ops@example.test"]
        assert view_readable(accounts, "ops@example.test") is True

    async def test_all_instances_is_the_default(self, db, platform_config) -> None:
        platform_config([platform_account("ops@example.test")])

        accounts = await mail.instance_accounts(ADAPTER, FOREIGN_INSTANCE, is_platform_instance=False)

        assert [view.username for view in accounts.sendable] == ["ops@example.test"]

    async def test_listed_only_reaches_the_named_instances(self, db, platform_config) -> None:
        platform_config(
            [
                platform_account(
                    "ops@example.test",
                    share="listed",
                    share_instances=[f"{ADAPTER}:{OWN_INSTANCE}"],
                ),
            ],
        )

        own = await mail.instance_accounts(ADAPTER, OWN_INSTANCE, is_platform_instance=False)
        rival = await mail.instance_accounts(ADAPTER, FOREIGN_INSTANCE, is_platform_instance=False)

        assert [view.username for view in own.sendable] == ["ops@example.test"]
        assert rival.sendable == []

    async def test_none_never_reaches_any_instance(self, db, platform_config) -> None:
        platform_config([platform_account("ops@example.test", share="none")])

        accounts = await mail.instance_accounts(ADAPTER, OWN_INSTANCE, is_platform_instance=False)

        assert accounts.sendable == []

    async def test_unknown_share_value_is_treated_as_none(self, db, platform_config) -> None:
        """配置里手改出一个拼错的取值时，宁可少给一个授权

        `SEND_SHARE` 是 `Literal`，正常读配置时拼错会被 pydantic 挡下；这条用鸭子类型的
        对象直插一个非法值，钉的是**服务层自己的兜底**（它也吃别处构造出来的对象）。
        """

        class OddShare:
            ENABLED = True
            SEND_ENABLED = True
            IS_DEFAULT_SENDER = False
            USERNAME = "ops@example.test"
            DISPLAY_NAME = ""
            EMAIL_ACCOUNT = "自定义"
            SEND_SHARE = "all_instance"
            SEND_SHARE_INSTANCES: list[str] = []

        platform_config([OddShare()])  # type: ignore[list-item]

        accounts = await mail.instance_accounts(ADAPTER, OWN_INSTANCE, is_platform_instance=False)

        assert accounts.sendable == []

    async def test_send_disabled_platform_account_is_not_sendable(self, db, platform_config) -> None:
        platform_config([platform_account("ops@example.test", send_enabled=False)])

        accounts = await mail.instance_accounts(ADAPTER, OWN_INSTANCE, is_platform_instance=False)

        assert accounts.sendable == []


def view_readable(accounts: mail.SessionAccounts, username: str) -> bool:
    for view in accounts.readable:
        if view.username == username:
            return view.readable
    raise AssertionError(f"{username} 不在可读集合里")


class TestDefaultSender:
    """默认发件人的选法只有一处，实例自有优先于平台方"""

    async def test_own_account_wins_over_platform(self, db, platform_config) -> None:
        platform_config([platform_account("ops@example.test")])
        await add_instance_account(OWN_INSTANCE, "own@example.test")

        picked = await mail.default_sender_for_instance(
            ADAPTER, OWN_INSTANCE, is_platform_instance=False,
        )

        assert picked is not None
        assert picked.username == "own@example.test"

    async def test_flag_beats_table_order_within_the_instance(self, db, platform_config) -> None:
        platform_config([])
        await add_instance_account(OWN_INSTANCE, "first@example.test")
        await add_instance_account(OWN_INSTANCE, "flagged@example.test", is_default_sender=True)

        picked = await mail.default_sender_for_instance(
            ADAPTER, OWN_INSTANCE, is_platform_instance=False,
        )

        assert picked is not None
        assert picked.username == "flagged@example.test"

    async def test_platform_flag_is_used_when_the_instance_has_nothing(self, db, platform_config) -> None:
        flagged = platform_account("flagged@example.test")
        flagged.IS_DEFAULT_SENDER = True
        platform_config([platform_account("first@example.test"), flagged])

        picked = await mail.default_sender_for_instance(
            ADAPTER, OWN_INSTANCE, is_platform_instance=False,
        )

        assert picked is not None
        assert picked.username == "flagged@example.test"

    async def test_platform_order_is_kept_when_nothing_is_flagged(self, db, platform_config) -> None:
        """与改造前 `send_email` 的选法逐字相同：按 `RECEIVE_ACCOUNTS` 的顺序取第一个"""
        platform_config([platform_account("first@example.test"), platform_account("second@example.test")])

        picked = await mail.default_sender_for_instance(
            ADAPTER, OWN_INSTANCE, is_platform_instance=False,
        )

        assert picked is not None
        assert picked.username == "first@example.test"

    async def test_no_candidate_returns_none(self, db, platform_config) -> None:
        platform_config([])

        picked = await mail.default_sender_for_instance(
            ADAPTER, OWN_INSTANCE, is_platform_instance=False,
        )

        assert picked is None

    async def test_result_is_cached_until_it_is_cleared(self, db, platform_config) -> None:
        """5 秒缓存：同一轮里每个沙盒方法都查一次库没有意义，清缓存后必须立刻看到新值"""
        platform_config([platform_account("first@example.test")])
        first = await mail.default_sender_for_instance(
            ADAPTER, OWN_INSTANCE, is_platform_instance=False,
        )

        await add_instance_account(OWN_INSTANCE, "own@example.test")
        cached = await mail.default_sender_for_instance(
            ADAPTER, OWN_INSTANCE, is_platform_instance=False,
        )
        mail.clear_default_sender_cache()
        refreshed = await mail.default_sender_for_instance(
            ADAPTER, OWN_INSTANCE, is_platform_instance=False,
        )

        assert first is not None and cached is not None and refreshed is not None
        assert first.username == "first@example.test"
        assert cached.username == "first@example.test"
        assert refreshed.username == "own@example.test"


class TestSessionScope:
    """会话路径的身份只经 `resource_scope`，多租户下解析不出一律 fail-closed"""

    async def test_chat_key_resolves_to_its_own_instance(self, db, platform_config) -> None:
        platform_config([])
        await add_instance_account(OWN_INSTANCE, "own@example.test")
        await add_instance_account(FOREIGN_INSTANCE, "rival@example.test")

        accounts = await mail.chat_accounts(chat_key_of(OWN_INSTANCE))

        assert accounts.resolved is True
        assert (accounts.adapter_key, accounts.instance_key) == (ADAPTER, OWN_INSTANCE)
        assert [view.username for view in accounts.readable] == ["own@example.test"]

    async def test_unknown_chat_key_fails_closed_when_tenants_exist(self, db, platform_config) -> None:
        """多租户部署里解析不出归属 → 空集合，而不是把平台方那一份给出去"""
        platform_config([platform_account("ops@example.test")])

        accounts = await mail.chat_accounts(f"{ADAPTER}-ghost-group_ghost")

        assert accounts.resolved is False
        assert accounts.readable == []
        assert accounts.sendable == []

    async def test_unknown_chat_key_falls_back_to_platform_in_single_tenant(self, db, platform_config) -> None:
        """单租户部署没有租户边界可越，行为与改造前逐字一致"""
        platform_config([platform_account("ops@example.test")])
        await DBAdapterInstance.all().update(tenant_id=None)

        accounts = await mail.chat_accounts(f"{ADAPTER}-ghost-group_ghost")

        assert accounts.resolved is False
        assert [view.username for view in accounts.readable] == ["ops@example.test"]

    async def test_session_owner_comes_from_from_chat_key(self, db, platform_config) -> None:
        """`ctx.chat_key` 可以是模型传入的值，归属只看宿主签发的 `from_chat_key`"""
        platform_config([platform_account("ops@example.test")])
        await add_instance_account(OWN_INSTANCE, "own@example.test")

        class Ctx:
            chat_key = chat_key_of(PLATFORM_INSTANCE)
            from_chat_key = chat_key_of(OWN_INSTANCE)

        accounts = await mail.session_accounts(Ctx())

        assert accounts.instance_key == OWN_INSTANCE
        assert [view.username for view in accounts.readable] == ["own@example.test"]


class TestAccountReferencesDoNotLeakAddresses:
    """给模型的引用不带地址；给租户的显示名不带平台方地址"""

    def test_platform_ref_does_not_contain_the_address(self) -> None:
        ref = mail.platform_account_ref("ops@example.test")

        assert "ops" not in ref
        assert "@" not in ref
        assert ref.startswith("platform:")

    def test_platform_ref_is_stable_and_case_insensitive(self) -> None:
        assert mail.platform_account_ref("ops@example.test".title()) == mail.platform_account_ref("ops@example.test")

    def test_instance_ref_is_the_table_id(self) -> None:
        assert mail.instance_account_ref(7) == "instance:7"

    def test_tenant_label_of_platform_account_hides_the_address(self) -> None:
        """显示名被管理员填成地址时整个换掉：宁可少给一个名字，也不能漏出地址"""
        view = mail.build_account_view(
            kind=mail.KIND_PLATFORM,
            username="ops@example.test",
            display_name="ops@example.test",
            send_enabled=True,
            readable=False,
        )

        label = mail.visible_label_for(view, tenant_viewer=True)

        assert "ops@example.test" not in label
        assert label.startswith("平台方提供：")

    def test_tenant_label_of_named_platform_account_keeps_the_name(self) -> None:
        view = mail.build_account_view(
            kind=mail.KIND_PLATFORM,
            username="ops@example.test",
            display_name="通知邮箱",
            send_enabled=True,
            readable=False,
        )

        assert mail.visible_label_for(view, tenant_viewer=True) == "平台方提供：通知邮箱"

    def test_platform_viewer_sees_the_plain_label(self) -> None:
        view = mail.build_account_view(
            kind=mail.KIND_PLATFORM,
            username="ops@example.test",
            display_name="通知邮箱",
            send_enabled=True,
            readable=True,
        )

        assert mail.visible_label_for(view, tenant_viewer=False) == "通知邮箱"

    def test_instance_account_label_is_its_own_name(self) -> None:
        view = mail.build_account_view(
            kind=mail.KIND_INSTANCE,
            username="own@example.test",
            display_name="本实例",
            send_enabled=True,
            readable=True,
            instance_account_id=3,
        )

        assert mail.visible_label_for(view, tenant_viewer=True) == "本实例"

    def test_mask_keeps_first_character_and_domain(self) -> None:
        assert mail.mask_email_address("alice@example.test") == "a***@example.test"

    def test_mask_leaves_non_addresses_alone(self) -> None:
        assert mail.mask_email_address("") == ""
        assert mail.mask_email_address("not-an-address") == "not-an-address"


class TestResolution:
    """`resolve_readable` / `resolve_sendable`：点名的账户不在集合里按「不存在」"""

    async def test_unreadable_platform_account_is_not_readable(self, db, platform_config) -> None:
        platform_config([platform_account("ops@example.test")])
        accounts = await mail.instance_accounts(ADAPTER, OWN_INSTANCE, is_platform_instance=False)

        with pytest.raises(ValueError, match="未连接或不存在"):
            mail.resolve_readable(accounts, "ops@example.test")

    async def test_sendable_by_ref(self, db, platform_config) -> None:
        """租户会话拿不到平台方地址，只能用 `ref` 点名"""
        platform_config([platform_account("ops@example.test")])
        accounts = await mail.instance_accounts(ADAPTER, OWN_INSTANCE, is_platform_instance=False)
        ref = mail.platform_account_ref("ops@example.test")

        assert mail.resolve_sendable(accounts, ref).ref == ref

    async def test_sendable_by_display_label(self, db, platform_config) -> None:
        """模型常直接抄显示名（它看不到地址，只能看到「平台方提供：通知」）"""
        platform_config([platform_account("ops@example.test", display_name="通知")])
        accounts = await mail.instance_accounts(ADAPTER, OWN_INSTANCE, is_platform_instance=False)
        label = accounts.sendable[0].display_label

        assert mail.resolve_sendable(accounts, label).username == "ops@example.test"

    async def test_unnamed_request_gets_the_default(self, db, platform_config) -> None:
        platform_config([platform_account("first@example.test"), platform_account("second@example.test")])
        accounts = await mail.instance_accounts(ADAPTER, OWN_INSTANCE, is_platform_instance=False)

        assert mail.resolve_sendable(accounts, None).username == "first@example.test"

    async def test_empty_collection_raises(self, db, platform_config) -> None:
        platform_config([])
        accounts = await mail.instance_accounts(ADAPTER, OWN_INSTANCE, is_platform_instance=False)

        with pytest.raises(ValueError, match="发件人"):
            mail.resolve_sendable(accounts, None)
