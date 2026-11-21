"""租户账号保存自己的界面语言（`GET` / `PUT /user/me/locale`）

## 钉死什么

1. **三种身份都能用，包括只读成员。** 界面语言是"这台账号自己的偏好"，不是管理动作；
   只读成员连控制台都只能看，但它当然可以决定自己看中文还是英文。
2. **读写都不跨账号。** 请求体与路径里都没有目标标识，写入对象恒为 `_current_user`。
   甲保存之后乙读到的仍是乙自己的值（或 `None`），这是这条端点与全局配置的分界线。
3. **不认识的取值一律 422，且库里的值不变。** 前端 `locales/` 只有两套文案，
   存一个查不到的语种等于"设置成功但界面没变"，那种故障最难排查。
4. **只改 `ext_data.ui_locale`。** `ext_data` 是共享扩展字典，成员创建时会写进
   `created_by`；整块覆盖会把那些字段一起抹掉。
5. **不碰全局 `SYSTEM_LANG`。** 那是"这个部署默认说什么话"，租户改自己的偏好不该改它；
   真正的 403 出口是 `PUT /configs/system`（平台方鉴权），本文件的分工说明见端点 docstring。

判定都在路由层（`routers/user.py`），所以直接调端点函数、用内存 SQLite 上的真表 ——
与 `tests/test_self_service_password_change.py` 同一做法。
"""

import datetime
from typing import Any, Optional

import pytest
from pydantic import ValidationError as PydanticValidationError
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.user import get_my_locale, update_my_locale
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.schemas.user import (
    SUPPORTED_UI_LOCALES,
    UpdateUserLocaleRequest,
    UserLocaleResponse,
)
from nekro_agent.services.user.role import Role

ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2

# (用户名, tenant_id, 租户角色, perm_level) —— 三种身份各一行
ACCOUNT_LAYOUT: list[tuple[str, Optional[int], TenantRole, Role]] = [
    ("admin", None, TenantRole.PLATFORM_OWNER, Role.Super),
    ("acme_admin", ACME_TENANT_ID, TenantRole.TENANT_ADMIN, Role.Admin),
    ("acme_ro", ACME_TENANT_ID, TenantRole.TENANT_USER, Role.User),
]
ALL_USERNAMES = [row[0] for row in ACCOUNT_LAYOUT]


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        for username, tenant_id, tenant_role, perm_level in ACCOUNT_LAYOUT:
            await DBUser.create(
                username=username,
                password="",
                adapter_key="",
                platform_userid="",
                perm_level=perm_level,
                login_time=datetime.datetime.now(datetime.timezone.utc),
                tenant_id=tenant_id,
                tenant_role=tenant_role.value,
            )
        yield
    finally:
        await Tortoise.close_connections()


async def user_of(username: str) -> DBUser:
    """按用户名取一行**新查出来的**记录

    每条用例都重新取，不用模块级的临时对象：`save()` 之后 `ext_data` 是否回写到内存对象
    取决于实现，拿内存对象断言读接口等于在测缓存。
    """
    return await DBUser.get(username=username)


async def read(username: str) -> Optional[str]:
    return (await get_my_locale(_current_user=await user_of(username))).locale


async def write(username: str, locale: str) -> None:
    await update_my_locale(
        UpdateUserLocaleRequest(locale=locale),  # type: ignore[arg-type]
        _current_user=await user_of(username),
    )


def request_with_payload(payload: dict[str, Any]) -> UpdateUserLocaleRequest:
    return UpdateUserLocaleRequest(**payload)


class TestEveryIdentityCanSaveItsOwnLocale:
    def test_only_the_two_shipped_locales_are_supported(self) -> None:
        """列表本身就是契约：前端只有这两套文案，多一个值就是"存得进去、界面没变"。"""
        assert set(SUPPORTED_UI_LOCALES) == {"zh-CN", "en-US"}

    @pytest.mark.parametrize("username", ALL_USERNAMES)
    async def test_a_fresh_account_reads_none(self, db, username: str) -> None:  # noqa: ARG002
        """没设置过时是 `None`，不是空串：空串会与"传了一个空值"混起来。"""
        response = await get_my_locale(_current_user=await user_of(username))
        assert isinstance(response, UserLocaleResponse)
        assert response.locale is None

    @pytest.mark.parametrize("username", ALL_USERNAMES)
    @pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
    async def test_round_trip(self, db, username: str, locale: str) -> None:  # noqa: ARG002
        """三种身份 × 两种语言都要能存进去再读回来，包括只读成员。"""
        await write(username, locale)
        assert await read(username) == locale
        assert (await user_of(username)).ext_data["ui_locale"] == locale

    async def test_a_read_only_member_is_not_blocked(self, db) -> None:  # noqa: ARG002
        """单独点名只读成员：它用 `PUT /configs/system` 改语言会 403，本端点必须放行。

        与 `tests/test_readonly_member_console_surface.py` 是同一件事的两半：
        那边钉住"它不能改全局配置"，这边钉住"但它能改自己的偏好"。
        """
        await write("acme_ro", "en-US")
        assert await read("acme_ro") == "en-US"


class TestWritesStayInsideTheCaller:
    async def test_one_account_does_not_affect_another(self, db) -> None:  # noqa: ARG002
        await write("acme_admin", "en-US")

        assert await read("acme_admin") == "en-US"
        assert await read("acme_ro") is None
        assert await read("admin") is None

    async def test_two_accounts_can_hold_different_values(self, db) -> None:  # noqa: ARG002
        """两组值同时存在：证明写的是各自那一行，而不是某个共享的配置项。"""
        await write("acme_admin", "en-US")
        await write("acme_ro", "zh-CN")

        assert await read("acme_admin") == "en-US"
        assert await read("acme_ro") == "zh-CN"

    async def test_a_cross_tenant_account_is_unaffected(self, db) -> None:  # noqa: ARG002
        await DBUser.create(
            username="rival_admin",
            password="",
            adapter_key="",
            platform_userid="",
            perm_level=Role.Admin,
            login_time=datetime.datetime.now(datetime.timezone.utc),
            tenant_id=RIVAL_TENANT_ID,
            tenant_role=TenantRole.TENANT_ADMIN.value,
        )

        await write("acme_admin", "en-US")

        assert await read("rival_admin") is None
        assert await read("acme_admin") == "en-US"


class TestUnsupportedValuesAreRefused:
    @pytest.mark.parametrize("locale", ["fr-FR", "", "en", "zh_CN", "ZH-CN"])
    def test_the_request_model_rejects_them(self, locale: str) -> None:
        with pytest.raises(PydanticValidationError):
            request_with_payload({"locale": locale})

    @pytest.mark.parametrize("locale", ["fr-FR", ""])
    async def test_the_stored_value_is_unchanged_after_a_refused_write(self, db, locale: str) -> None:  # noqa: ARG002
        """422 必须发生在写库之前：半写半不写比直接拒绝更难排查。"""
        await write("acme_admin", "zh-CN")

        with pytest.raises(PydanticValidationError):
            request_with_payload({"locale": locale})

        assert await read("acme_admin") == "zh-CN"

    async def test_a_value_written_by_someone_else_is_filtered_on_read(self, db) -> None:  # noqa: ARG002
        """库里的值可能是旧版本写的、或者手工改的：读接口按白名单过滤后返回 `None`。

        照直返回会让前端拿着一个查不到的语种去加载资源，界面回退成中文而设置页仍显示
        "已选英文"。这类"设置了但没生效"最难排查。
        """
        row = await user_of("acme_admin")
        row.ext_data = {"ui_locale": "fr-FR"}
        await row.save(update_fields=["ext_data"])

        assert await read("acme_admin") is None

    async def test_non_dict_ext_data_is_treated_as_empty(self, db) -> None:  # noqa: ARG002
        """`ext_data` 是 JSON 列，历史行上可能不是对象；读接口不该因此 500。"""
        row = await user_of("acme_admin")
        row.ext_data = ["not", "a", "dict"]
        await row.save(update_fields=["ext_data"])

        assert await read("acme_admin") is None


class TestOtherExtDataSurvives:
    async def test_created_by_is_preserved(self, db) -> None:  # noqa: ARG002
        """成员创建时写进去的 `created_by` 必须留着。

        `ext_data` 是共享扩展字典，整块覆盖会把"这个账号是谁开的"一起抹掉——
        而那条信息正是排查后门账号时唯一的线索。
        """
        created_by = {"user_id": 4242, "username": "acme_admin", "role": TenantRole.TENANT_ADMIN.value}
        row = await user_of("acme_ro")
        row.ext_data = {"created_by": created_by}
        await row.save(update_fields=["ext_data"])

        await write("acme_ro", "en-US")

        fresh = await user_of("acme_ro")
        assert fresh.ext_data["ui_locale"] == "en-US"
        assert fresh.ext_data["created_by"] == created_by

    async def test_writing_twice_keeps_the_other_keys(self, db) -> None:  # noqa: ARG002
        row = await user_of("acme_ro")
        row.ext_data = {"created_by": {"user_id": 1, "username": "admin", "role": "platform_owner"}}
        await row.save(update_fields=["ext_data"])

        await write("acme_ro", "en-US")
        await write("acme_ro", "zh-CN")

        fresh = await user_of("acme_ro")
        assert fresh.ext_data == {
            "created_by": {"user_id": 1, "username": "admin", "role": "platform_owner"},
            "ui_locale": "zh-CN",
        }


class TestTheGlobalDefaultIsUntouched:
    async def test_a_tenant_write_does_not_change_system_lang(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """全局 `SYSTEM_LANG` 是"这个部署默认说什么话"，不是"某个账号想看什么话"。

        租户写自己的偏好时它必须一个字都不动：两者共用一个字段的话，任何租户切换语言
        都会改掉全站的默认值 —— 那是跨租户越权，不只是体验问题。
        """
        monkeypatch.setattr(config, "SYSTEM_LANG", "zh-CN")

        await write("acme_admin", "en-US")

        assert config.SYSTEM_LANG == "zh-CN"
        assert await read("acme_admin") == "en-US"
