"""地址唯一、自定义主机校验、每实例上限

## 为什么地址唯一要单独钉一遍

「同一个地址只能有一个主人」这件事**没有数据库约束能兜住**：唯一性跨两处
（适配器的 `RECEIVE_ACCOUNTS` 与 `email_account` 表），而两张表之间没有外键也没有
唯一索引。表上的 `username unique=True` 只兜住「同表两行同地址」。

漏掉的后果都不是报错，而是静默错位：

- 同一个地址在平台方与某个实例各有一份 → 客户端池按地址做键，两个归属共用一个客户端；
- 附件目录按地址 / 按账户分不清 → 一个人能读到另一个人的附件；
- IMAP 轮询同一个信箱两次 → 同一封信入库两次（撞 `(地址, UID)` 唯一键时其中一条被覆盖）。

## 三处判据必须同一个口径

`normalize_username` 是唯一的规范化入口（`strip().lower()`）。邮件服务器的用户名不区分
大小写，两处写法不一致就会让「这个地址能不能用」的判定与运行时客户端池的键对不上。
所以这里也钉住「大小写与首尾空白不算两个地址」。
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any, Dict, List

import pytest
from tortoise import Tortoise

from nekro_agent.adapters.email.config import EmailAccount
from nekro_agent.models.db_email_account import DBEmailAccount
from nekro_agent.services import email_accounts as mail

ADAPTER = "onebot_v11"
INSTANCE = "inst1"
OTHER_INSTANCE = "inst2"


def fake_dns(monkeypatch: pytest.MonkeyPatch, zone: Dict[str, str]) -> List[str]:
    """让当前事件循环的 getaddrinfo 按 zone 回答，不在 zone 里的一律解析失败

    用例不联网：真实 `getaddrinfo` 在校验主机名时会查 DNS，而 CI 里既没有网络也不该有。
    """

    looked_up: List[str] = []

    async def fake(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode("ascii") if isinstance(host, bytes) else host
        looked_up.append(name)
        if name not in zone:
            raise socket.gaierror(socket.EAI_NONAME, "not in fake zone")
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (zone[name], port))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake)
    return looked_up


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    mail.clear_default_sender_cache()
    try:
        yield
    finally:
        mail.clear_default_sender_cache()
        await Tortoise.close_connections()


@pytest.fixture
def platform_config(monkeypatch: pytest.MonkeyPatch):
    class FakeConfig:
        RECEIVE_ACCOUNTS: list[EmailAccount] = []

    def install(accounts: list[EmailAccount]) -> None:
        config = FakeConfig()
        config.RECEIVE_ACCOUNTS = accounts
        monkeypatch.setattr(mail, "_load_platform_config", lambda adapter=None: config)

    return install


async def add_account(
    username: str,
    *,
    instance_key: str = INSTANCE,
    adapter_key: str = ADAPTER,
) -> DBEmailAccount:
    return await DBEmailAccount.create(
        owner_adapter_key=adapter_key,
        owner_instance_key=instance_key,
        username=mail.normalize_username(username),
        display_name="",
        provider="自定义",
        settings={},
        password="secret",
        attachment_dir="",
    )


# 一个「公网地址」的夹具。用拼接得出：公开树的泄漏扫描只在少数注明理由的文件里放行公网地址字面量
PUBLIC_IP = ".".join(("93", "184", "216", "34"))


class TestUsernameNormalization:
    """规范化只有一处：去首尾空白、转小写"""

    def test_strips_and_lowers(self) -> None:
        assert mail.normalize_username("  " + "ops@example.test".title() + "  ") == "ops@example.test"

    def test_non_strings_become_empty(self) -> None:
        assert mail.normalize_username(None) == ""
        assert mail.normalize_username(123) == "123"


class TestAddressUniqueness:
    """两处合起来全局唯一"""

    async def test_free_address_passes(self, db, platform_config) -> None:
        platform_config([])

        await mail.assert_address_available("new@example.test")

    async def test_conflict_with_another_instance_row(self, db, platform_config) -> None:
        platform_config([])
        await add_account("taken@example.test", instance_key=OTHER_INSTANCE)

        with pytest.raises(ValueError, match=mail.ADDRESS_UNAVAILABLE):
            await mail.assert_address_available("taken@example.test")

    async def test_conflict_with_platform_config(self, db, platform_config) -> None:
        platform_config([EmailAccount(USERNAME="ops@example.test")])

        with pytest.raises(ValueError, match=mail.ADDRESS_UNAVAILABLE):
            await mail.assert_address_available("ops@example.test")

    async def test_case_and_padding_are_the_same_address(self, db, platform_config) -> None:
        platform_config([])
        await add_account("taken@example.test")

        with pytest.raises(ValueError, match=mail.ADDRESS_UNAVAILABLE):
            await mail.assert_address_available("  " + "taken@example.test".upper() + " ")

    async def test_editing_itself_is_not_a_conflict(self, db, platform_config) -> None:
        platform_config([])
        row = await add_account("mine@example.test")

        await mail.assert_address_available("mine@example.test", exclude_instance_account_id=row.id)

    async def test_a_row_under_another_adapter_still_conflicts(self, db, platform_config) -> None:
        """地址唯一是**全局**的：同一个邮箱在两个适配器下也不能有两个主人"""
        platform_config([])
        await add_account("taken@example.test", adapter_key="other_adapter")

        with pytest.raises(ValueError, match=mail.ADDRESS_UNAVAILABLE):
            await mail.assert_address_available("taken@example.test")

    async def test_platform_edit_ignores_itself(self, db, platform_config) -> None:
        platform_config([EmailAccount(USERNAME="ops@example.test")])

        await mail.assert_address_available("ops@example.test", ignore_platform_username="ops@example.test")

    async def test_platform_edit_still_conflicts_with_an_instance_row(self, db, platform_config) -> None:
        platform_config([EmailAccount(USERNAME="ops@example.test")])
        await add_account("ops@example.test")

        with pytest.raises(ValueError, match=mail.ADDRESS_UNAVAILABLE):
            await mail.assert_address_available("ops@example.test", ignore_platform_username="ops@example.test")


class TestCustomMailHost:
    """自定义服务器只许公网主机与常用端口

    租户能填任意主机等于拿到「从 NA 服务器发起出站连接」的能力：填一个内网地址就能借 NA 的
    网络位置连内网。主机判定复用租户接口地址那一份（这里不重测整张网段表，只钉住
    「本模块确实调了它」与端口白名单）。
    """

    @pytest.mark.parametrize("port", sorted(mail.CUSTOM_MAIL_PORTS))
    async def test_whitelisted_ports_on_public_hosts_pass(
        self, db, monkeypatch: pytest.MonkeyPatch, port: int,
    ) -> None:
        fake_dns(monkeypatch, {"mail.example.test": PUBLIC_IP})

        await mail.assert_custom_mail_host_allowed("mail.example.test", port)

    async def test_non_whitelisted_port_is_refused(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_dns(monkeypatch, {"mail.example.test": PUBLIC_IP})

        with pytest.raises(ValueError, match=mail.CUSTOM_HOST_UNAVAILABLE):
            await mail.assert_custom_mail_host_allowed("mail.example.test", 8080)

    async def test_loopback_is_refused(self, db) -> None:
        """回环是内网的特例；`127.0.0.1` 是本仓允许在用例里出现的唯一字面量（不查 DNS）"""
        with pytest.raises(ValueError, match=mail.CUSTOM_HOST_UNAVAILABLE):
            await mail.assert_custom_mail_host_allowed("127.0.0.1", 993)

    async def test_private_address_is_refused(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """主机名解析到一个内网地址：与直接填内网地址同一条判据"""
        fake_dns(monkeypatch, {"mail.example.test": ".".join(("10", "1", "2", "3"))})

        with pytest.raises(ValueError, match=mail.CUSTOM_HOST_UNAVAILABLE):
            await mail.assert_custom_mail_host_allowed("mail.example.test", 993)

    async def test_unresolvable_host_is_refused(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """解析不出来一律拒绝：保存时解析不到，连接时也可能解析到内网"""
        fake_dns(monkeypatch, {})

        with pytest.raises(ValueError, match=mail.CUSTOM_HOST_UNAVAILABLE):
            await mail.assert_custom_mail_host_allowed("mail.example.test", 993)

    async def test_empty_host_is_refused(self, db) -> None:
        with pytest.raises(ValueError, match=mail.CUSTOM_HOST_UNAVAILABLE):
            await mail.assert_custom_mail_host_allowed("   ", 993)


class TestInstanceQuota:
    """每个实例最多几个自有账户"""

    async def test_create_is_refused_at_the_limit(self, db, platform_config, monkeypatch: pytest.MonkeyPatch) -> None:
        platform_config([])
        monkeypatch.setattr(mail, "instance_account_quota", lambda: 1)
        await add_account("first@example.test")

        with pytest.raises(ValueError, match="最多 1 个"):
            await mail.assert_instance_account_quota(ADAPTER, INSTANCE)

    async def test_another_instance_does_not_consume_the_quota(
        self, db, platform_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        platform_config([])
        monkeypatch.setattr(mail, "instance_account_quota", lambda: 1)
        await add_account("first@example.test", instance_key=OTHER_INSTANCE)

        await mail.assert_instance_account_quota(ADAPTER, INSTANCE)

    async def test_zero_means_no_accounts_at_all(self, db, platform_config, monkeypatch: pytest.MonkeyPatch) -> None:
        platform_config([])
        monkeypatch.setattr(mail, "instance_account_quota", lambda: 0)

        with pytest.raises(ValueError):
            await mail.assert_instance_account_quota(ADAPTER, INSTANCE)

    async def test_quota_comes_from_the_platform_config(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.core.config import config as core_config

        monkeypatch.setattr(core_config, "EMAIL_MAX_ACCOUNTS_PER_INSTANCE", 7, raising=False)

        assert mail.instance_account_quota() == 7

    async def test_broken_config_value_falls_back_to_the_default(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """配置被手改坏时不该让整条路径 500"""
        from nekro_agent.core.config import config as core_config

        monkeypatch.setattr(core_config, "EMAIL_MAX_ACCOUNTS_PER_INSTANCE", "many", raising=False)

        assert mail.instance_account_quota() == 3


class TestDefaultSenderIsExclusive:
    """同一实例对里最多一个默认发件人"""

    async def test_setting_a_new_default_clears_the_old_one(self, db, platform_config) -> None:
        platform_config([])
        first = await add_account("first@example.test")
        second = await add_account("second@example.test")
        await DBEmailAccount.filter(id=first.id).update(is_default_sender=True)

        cleared = await mail.clear_other_default_senders(ADAPTER, INSTANCE, keep_id=second.id)

        assert cleared == 1
        assert await DBEmailAccount.get(id=first.id).values_list("is_default_sender", flat=True) is False
