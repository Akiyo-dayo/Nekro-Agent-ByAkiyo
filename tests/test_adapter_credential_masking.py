"""适配器凭据在租户视角下的脱敏契约

定制版客户（租户）能登录自己的后台，也就能直接调 `/config/*` 的读接口。因此
"平台方的凭据不下发给租户"这件事必须由接口层保证，而接口层的判据有两层：

1. `ExtraField(is_secret=True)` —— 权威标记，写在字段定义里；
2. `routers/config.py::_is_credential_field_name` —— 按字段名的兜底。

漏标一处，凭据是否泄露就完全取决于字段名恰好长得像不像凭据，这不是可以依赖的性质
（`MINECRAFT_SERVER_RCON` 就是反例：里面是 rcon 密码，名字里一个凭据词都没有）。
所以这里对**每一个承载凭据的适配器配置项**逐条钉死标记，并连带钉死它在读接口上真的被换成掩码、
以及掩码回写不会把真实凭据覆盖成一串星号。
"""

import json
from typing import Any, Dict, List, Tuple, Type

import pytest
from pydantic import BaseModel

from nekro_agent.adapters.discord.config import DiscordConfig
from nekro_agent.adapters.email.config import EmailAccount, EmailConfig
from nekro_agent.adapters.feishu.config import FeishuConfig
from nekro_agent.adapters.minecraft.adapter import MinecraftConfig
from nekro_agent.adapters.qqbot_openclaw.config import QQBotOpenClawConfig
from nekro_agent.adapters.sse.adapter import SSEConfig
from nekro_agent.adapters.telegram.config import TelegramConfig
from nekro_agent.adapters.wechatpad.config import WeChatPadConfig
from nekro_agent.adapters.wxwork.config import WxWorkConfig
from nekro_agent.adapters.wxwork_corp_app.config import WxWorkCorpAppConfig
from nekro_agent.routers.config import (
    _is_credential_field_name,  # noqa: PLC2701
    _mask_config_items,  # noqa: PLC2701
    mask_credentials,
    restore_masked_credentials,
)
from nekro_agent.schemas.tenant import CREDENTIAL_MASK
from nekro_agent.services.config_service import ConfigService

# (配置类, 字段名)：泄露它就能冒用这个身份或访问这个服务，因此必须显式标 is_secret。
CREDENTIAL_FIELDS: List[Tuple[Type[BaseModel], str]] = [
    (DiscordConfig, "BOT_TOKEN"),
    (DiscordConfig, "PROXY_PASSWORD"),
    (EmailAccount, "PASSWORD"),
    (EmailAccount, "CLIENT_SECRET"),
    (EmailAccount, "ACCESS_TOKEN"),
    (EmailAccount, "REFRESH_TOKEN"),
    (FeishuConfig, "APP_SECRET"),
    (MinecraftConfig, "MINECRAFT_ACCESS_TOKEN"),
    (MinecraftConfig, "MINECRAFT_SERVER_RCON"),
    (QQBotOpenClawConfig, "CLIENT_SECRET"),
    (SSEConfig, "ACCESS_KEY"),
    (TelegramConfig, "BOT_TOKEN"),
    (WeChatPadConfig, "WECHATPAD_AUTH_KEY"),
    (WxWorkConfig, "BOT_ID"),
    (WxWorkConfig, "BOT_SECRET"),
    (WxWorkConfig, "USER_INFO_APP_SECRET"),
    (WxWorkConfig, "USER_INFO_PROXY_SHARED_SECRET"),
    (WxWorkCorpAppConfig, "CORP_APP_SECRET"),
    (WxWorkCorpAppConfig, "CALLBACK_TOKEN"),
    (WxWorkCorpAppConfig, "CALLBACK_ENCODING_AES_KEY"),
]

# 长得沾边但**不是**凭据的配置项。它们一旦被误判成凭据，租户就再也改不了自己的实例配置
# （`_assert_tenant_may_write` 对承载凭据的项整体只读），因此反向也要钉住。
NON_CREDENTIAL_FIELDS: List[Tuple[Type[BaseModel], str]] = [
    (DiscordConfig, "PROXY_URL"),
    (DiscordConfig, "PROXY_USERNAME"),
    (EmailAccount, "USERNAME"),
    (EmailAccount, "CLIENT_ID"),
    (EmailAccount, "TOKEN_EXPIRES_AT"),
    (FeishuConfig, "APP_ID"),
    (QQBotOpenClawConfig, "APP_ID"),
    (WeChatPadConfig, "WECHATPAD_API_URL"),
    (WxWorkCorpAppConfig, "CORP_ID"),
    (WxWorkCorpAppConfig, "CORP_APP_AGENT_ID"),
]

SENTINEL = "SENTINEL-CREDENTIAL-VALUE-9c1f"


def field_is_secret(model_class: Type[BaseModel], field_name: str) -> bool:
    """读出字段定义里的 `is_secret` 标记"""
    field = model_class.model_fields[field_name]
    extra = field.json_schema_extra
    return bool(isinstance(extra, dict) and extra.get("is_secret"))


def masked_item(config_obj: Any, item_key: str) -> Dict[str, Any]:
    """走真实读路径取出租户视角下的那一条配置项

    `ConfigService.get_config_list` + `_mask_config_items` 就是 `GET /config/list/{key}`
    在租户身份下实际执行的两步，这里不另造简化版本。
    """
    items = ConfigService.get_config_list(config_obj)
    masked = _mask_config_items(items)
    return next(item for item in masked if item["key"] == item_key)


class TestCredentialFieldsAreMarked:
    """凭据字段必须带权威标记，而不是靠字段名碰巧像凭据"""

    @pytest.mark.parametrize(("model_class", "field_name"), CREDENTIAL_FIELDS)
    def test_credential_field_declares_is_secret(self, model_class: Type[BaseModel], field_name: str) -> None:
        assert field_is_secret(model_class, field_name), f"{model_class.__name__}.{field_name} 承载凭据但未标 is_secret"

    @pytest.mark.parametrize(("model_class", "field_name"), NON_CREDENTIAL_FIELDS)
    def test_non_credential_field_is_not_marked(self, model_class: Type[BaseModel], field_name: str) -> None:
        """非凭据项不能被标成凭据：标了租户就永久失去对它的写权限"""
        assert not field_is_secret(model_class, field_name)
        assert not _is_credential_field_name(field_name)


class TestTenantReadSeesOnlyMask:
    """租户身份读配置时，凭据位置必须是掩码，且响应里不得残留真实值"""

    @pytest.mark.parametrize(
        ("config_cls", "item_key"),
        [
            (DiscordConfig, "BOT_TOKEN"),
            (DiscordConfig, "PROXY_PASSWORD"),
            (MinecraftConfig, "MINECRAFT_ACCESS_TOKEN"),
            (MinecraftConfig, "MINECRAFT_SERVER_RCON"),
            (SSEConfig, "ACCESS_KEY"),
            (WeChatPadConfig, "WECHATPAD_AUTH_KEY"),
            (FeishuConfig, "APP_SECRET"),
        ],
    )
    def test_scalar_credential_is_masked(self, config_cls: Type[Any], item_key: str) -> None:
        config_obj = config_cls()
        setattr(config_obj, item_key, SENTINEL)

        item = masked_item(config_obj, item_key)
        assert item["value"] == CREDENTIAL_MASK
        assert item["is_secret"] is True

    def test_whole_config_payload_contains_no_sentinel(self) -> None:
        """把整份响应序列化后 grep 一遍：真实值必须 0 命中

        逐字段断言只能覆盖想到的字段；这条覆盖的是"某个没想到的字段把它带了出去"。
        """
        config_obj = DiscordConfig(BOT_TOKEN=SENTINEL, PROXY_PASSWORD=SENTINEL)
        payload = json.dumps(_mask_config_items(ConfigService.get_config_list(config_obj)), default=str)
        assert SENTINEL not in payload

    def test_nested_account_credentials_are_masked(self) -> None:
        """`RECEIVE_ACCOUNTS` 是 List[EmailAccount]，掩码必须穿透进列表里的每个账户"""
        config_obj = EmailConfig(
            RECEIVE_ACCOUNTS=[
                EmailAccount(
                    EMAIL_ACCOUNT="custom",
                    USERNAME="bot@example.com",
                    PASSWORD=SENTINEL,
                    CLIENT_SECRET=SENTINEL,
                    ACCESS_TOKEN=SENTINEL,
                    REFRESH_TOKEN=SENTINEL,
                ),
            ],
        )
        item = masked_item(config_obj, "RECEIVE_ACCOUNTS")

        account = item["value"][0]
        assert account.PASSWORD == CREDENTIAL_MASK
        assert account.CLIENT_SECRET == CREDENTIAL_MASK
        assert account.ACCESS_TOKEN == CREDENTIAL_MASK
        assert account.REFRESH_TOKEN == CREDENTIAL_MASK
        # 同一账户里的非凭据字段必须原样保留，否则租户无法分辨自己配的是哪个账号
        assert account.USERNAME == "bot@example.com"
        assert SENTINEL not in json.dumps(item, default=str)

    def test_nested_credentials_are_masked_after_a_plain_dict_write(self) -> None:
        """写接口会把复杂字段落成普通 dict，脱敏在那之后仍须生效

        `ConfigService.set_config_value` 对 List[Pydantic] 是先校验、再把**解析出来的 dict**
        直接 setattr 回去，于是内存里的账户不再是 `EmailAccount` 实例。这时按模型字段读
        `is_secret` 的那条路走不通，只剩字段名兜底——它必须接得住。
        """
        config_obj = EmailConfig()
        ok, err = ConfigService.set_config_value(
            config_obj,
            "RECEIVE_ACCOUNTS",
            json.dumps([{"EMAIL_ACCOUNT": "custom", "USERNAME": "bot@example.com", "PASSWORD": SENTINEL}]),
        )
        assert ok, err

        item = masked_item(config_obj, "RECEIVE_ACCOUNTS")
        assert SENTINEL not in json.dumps(item, default=str)
        assert item["value"][0]["PASSWORD"] == CREDENTIAL_MASK
        assert item["value"][0]["USERNAME"] == "bot@example.com"

    def test_empty_credential_stays_empty(self) -> None:
        """没配过的凭据保持空串：租户要能分辨"还没配"和"配了但看不到"，
        并且空串回写不会被误判成掩码占位。
        """
        item = masked_item(DiscordConfig(), "BOT_TOKEN")
        assert item["value"] == ""


class TestMaskWriteBackKeepsRealValue:
    """掩码是"保持不变"的哨兵，回写它绝不能把真实凭据覆盖成一串星号"""

    def test_scalar_mask_restores_current_value(self) -> None:
        assert restore_masked_credentials(CREDENTIAL_MASK, SENTINEL) == SENTINEL

    def test_real_new_value_overwrites(self) -> None:
        assert restore_masked_credentials("new-token", SENTINEL) == "new-token"

    def test_empty_string_overwrites_and_is_not_treated_as_mask(self) -> None:
        """显式清空是有意操作，不能被"保持不变"吃掉"""
        assert restore_masked_credentials("", SENTINEL) == ""

    def test_nested_mask_restores_per_position(self) -> None:
        """整份配置原样提交回来时，逐个位置还原，非凭据字段的改动照常生效"""
        current = EmailConfig(
            RECEIVE_ACCOUNTS=[
                EmailAccount(USERNAME="bot@example.com", PASSWORD=SENTINEL, REFRESH_TOKEN=SENTINEL),
            ],
        )
        submitted = json.loads(json.dumps(mask_credentials(current.RECEIVE_ACCOUNTS), default=lambda v: v.model_dump()))
        submitted[0]["USERNAME"] = "changed@example.com"

        restored = restore_masked_credentials(submitted, current.RECEIVE_ACCOUNTS)
        assert restored[0]["PASSWORD"] == SENTINEL
        assert restored[0]["REFRESH_TOKEN"] == SENTINEL
        assert restored[0]["USERNAME"] == "changed@example.com"

    def test_mask_round_trip_through_set_config_value_keeps_credential(self) -> None:
        """读到掩码 → 原样提交 → 真实凭据仍在

        这是租户在后台点一次"保存"的完整往返，也是"掩码被存成真实值"这个 BUG 的直接回归位。
        """
        config_obj = EmailConfig(RECEIVE_ACCOUNTS=[EmailAccount(USERNAME="bot@example.com", PASSWORD=SENTINEL)])
        item = masked_item(config_obj, "RECEIVE_ACCOUNTS")
        submitted = json.dumps(item["value"], default=lambda v: v.model_dump())
        assert CREDENTIAL_MASK in submitted

        restored = restore_masked_credentials(json.loads(submitted), config_obj.RECEIVE_ACCOUNTS)
        ok, err = ConfigService.set_config_value(config_obj, "RECEIVE_ACCOUNTS", json.dumps(restored))
        assert ok, err

        stored = config_obj.RECEIVE_ACCOUNTS[0]
        password = stored["PASSWORD"] if isinstance(stored, dict) else stored.PASSWORD
        assert password == SENTINEL
        assert password != CREDENTIAL_MASK
