"""`/config` 读侧的归属收敛，与模型组运行时视图的按归属过滤

## 这里钉的是什么

之前把 `/config/set/...` 与 `/config/batch/...` 接上了 `_assert_config_key_accessible`，
**读侧一条都没接**。于是同一个 `config_key` 命名空间上，
写是收着的、读是敞开的：

1. `GET /config/{info,list,get}/{config_key}` 从路径原样收下 `config_key`，而它能编码
   `instance_override_{adapter}__{instance_key}` 与 `channel_config_{chat_key}`；
2. 更糟的是这不只是"读到"：`UnifiedConfigService._get_config_instance()` 对覆盖类配置键
   会 `dump_config()` **落盘**，所以租户随手拼一个别人的（甚至根本不存在的）实例标识，
   服务端就会在磁盘上凭空造出一份覆盖文件。

因此这里的断言分两半：**越权抛 404**，以及**守卫必须挡在 `UnifiedConfigService` 之前**
（用 spy 断言服务层一次都没被调到）。只断言状态码会漏掉第二半——那正是这个洞真正的代价。

共享作用域（`system` / `adapter_*` / `plugin_*`）读放行、写 403 是有意的不对称：
租户配置页要读得到系统配置（凭据已由 `_mask_config_items` 脱敏、平台方边界项已被滤掉），
但写入对所有租户同时生效，是权限边界。两侧共用 `_assert_config_key_accessible` 一个实现，
`for_write` 只切换这一个分支。

模型组那半钉的是另一条：`config.MODEL_GROUPS` 是一份**不带归属信息**的运行时视图，
`GET /config/model-groups` 照它返回等于把别的租户自建组的组名与模型名列给对方看。
事实来源是 `DBModelGroup`，判定必须和 `/console/model-groups` 同源（`can_view_group` /
`mask_config_group`），不能各判一次。
"""

import datetime
from typing import Any, Dict, List, Optional, Tuple, Type

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_model_group import DBModelGroup
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.config import (
    get_config_info,
    get_config_item,
    get_config_keys,
    get_config_list,
    get_effective_config_trace,
    get_model_groups,
    reload_config_api,
    save_config_api,
)
from nekro_agent.schemas.errors import NotFoundError, PermissionDeniedError
from nekro_agent.schemas.tenant import CREDENTIAL_MASK, TenantRole
from nekro_agent.services.config_resolver import (
    ConfigLayer,
    EffectiveConfigFieldTrace,
    EffectiveConfigTrace,
    config_resolver,
)
from nekro_agent.services.config_service import UnifiedConfigService
from nekro_agent.services.user.role import Role

ACME_TENANT_ID = 1
OTHER_TENANT_ID = 2

OWN_CHAT_KEY = "onebot_v11-inst1-group_10001"
FOREIGN_CHAT_KEY = "onebot_v11-rival1-group_20001"

OWN_INSTANCE_CONFIG_KEY = "instance_override_onebot_v11__inst1"
FOREIGN_INSTANCE_CONFIG_KEY = "instance_override_onebot_v11__rival1"
MISSING_INSTANCE_CONFIG_KEY = "instance_override_onebot_v11__nosuch"
MALFORMED_INSTANCE_CONFIG_KEY = "instance_override_onebot_v11"
OWN_CHANNEL_CONFIG_KEY = f"channel_config_{OWN_CHAT_KEY}"
FOREIGN_CHANNEL_CONFIG_KEY = f"channel_config_{FOREIGN_CHAT_KEY}"
MISSING_CHANNEL_CONFIG_KEY = "channel_config_onebot_v11-inst1-group_99999"

PLATFORM_API_KEY = "sk-platform-tenant-must-never-see-this"
PLATFORM_BASE_URL = "https://api.platform-internal.example/v1"
TENANT_API_KEY = "sk-acme-own-key"
TENANT_BASE_URL = "https://api.acme.example/v1"

# 读侧：`config_key` -> 期望的异常类型（None 表示放行）
READ_SCOPE_CASES: List[Tuple[str, Optional[Type[Exception]]]] = [
    ("system", None),
    ("adapter_onebot_v11", None),
    ("plugin_whatever", None),
    (OWN_INSTANCE_CONFIG_KEY, None),
    (OWN_CHANNEL_CONFIG_KEY, None),
    (FOREIGN_INSTANCE_CONFIG_KEY, NotFoundError),
    (FOREIGN_CHANNEL_CONFIG_KEY, NotFoundError),
    (MISSING_INSTANCE_CONFIG_KEY, NotFoundError),
    (MALFORMED_INSTANCE_CONFIG_KEY, NotFoundError),
    (MISSING_CHANNEL_CONFIG_KEY, NotFoundError),
]

# 写侧：同一套归属判据，唯一的差别是共享作用域从"放行"变成 403
WRITE_SCOPE_CASES: List[Tuple[str, Optional[Type[Exception]]]] = [
    ("system", PermissionDeniedError),
    ("adapter_onebot_v11", PermissionDeniedError),
    ("plugin_whatever", PermissionDeniedError),
    (OWN_INSTANCE_CONFIG_KEY, None),
    (OWN_CHANNEL_CONFIG_KEY, None),
    (FOREIGN_INSTANCE_CONFIG_KEY, NotFoundError),
    (FOREIGN_CHANNEL_CONFIG_KEY, NotFoundError),
    (MISSING_INSTANCE_CONFIG_KEY, NotFoundError),
    (MISSING_CHANNEL_CONFIG_KEY, NotFoundError),
]


def make_user(*, user_id: int, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    """构造一个未落库的登录身份

    作用域判定只读 `tenant_id` / `tenant_role` / `perm_level`；`id` 是
    `resolve_console_identity()` 组装 `CurrentIdentity` 时要的，落库与否不影响判定。
    """
    user = DBUser(
        username=f"user-{tenant_role.value}-{tenant_id}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )
    user.id = user_id
    return user


def acme_admin() -> DBUser:
    return make_user(
        user_id=11, tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin
    )


def platform_owner() -> DBUser:
    return make_user(user_id=1, tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


class ConfigServiceSpy:
    """记录配置服务被调到了哪些 `config_key`

    守卫的价值不只是"回了 404"，还有"根本没让服务层跑起来"——服务层一跑就会
    `dump_config()` 落盘。所以整份配置服务在测试里换成 spy：既让断言能覆盖这一半，
    也让这个文件完全不碰磁盘。
    """

    def __init__(self) -> None:
        self.touched: List[str] = []

    def get_config_info(self, config_key: str) -> Dict[str, Any]:
        self.touched.append(config_key)
        return {
            "config_key": config_key,
            "config_class": "SpyConfig",
            "config_file_path": f"/spy/{config_key}.yaml",
            "config_type": "system",
            "field_count": 1,
        }

    def get_config_list(self, config_key: str) -> List[Dict[str, Any]]:
        self.touched.append(config_key)
        return [
            {"key": "USE_MODEL_GROUP", "value": "default", "title": "模型组", "type": "str"},
            # `CoreConfig.MODEL_GROUPS` 的取值就是整张模型组表，`/config/list/system`
            # 会把它原样交给调用方 —— 这一项必须在 spy 里如实复现，否则测不到那条泄露路径
            {"key": "MODEL_GROUPS", "value": config.MODEL_GROUPS, "title": "模型组配置", "type": "dict"},
        ]

    def get_config_item(self, config_key: str, item_key: str) -> Dict[str, Any]:
        self.touched.append(config_key)
        if item_key == "MODEL_GROUPS":
            return {"key": item_key, "value": config.MODEL_GROUPS, "title": "模型组配置", "type": "dict"}
        return {"key": item_key, "value": "default", "title": "模型组", "type": "str"}

    def save_config(self, config_key: str) -> Tuple[bool, Optional[str]]:
        self.touched.append(config_key)
        return True, None

    def reload_config(self, config_key: str) -> Tuple[bool, Optional[str]]:
        self.touched.append(config_key)
        return True, None


async def _no_inherited_values(_config_key: str) -> None:
    return None


@pytest.fixture
async def spy(monkeypatch: pytest.MonkeyPatch):
    """两个租户各一批实例/频道/模型组，外加一份不落盘的配置服务"""
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()

    for instance_key, tenant_id, chat_key in (
        ("inst1", ACME_TENANT_ID, OWN_CHAT_KEY),
        ("rival1", OTHER_TENANT_ID, FOREIGN_CHAT_KEY),
    ):
        await DBAdapterInstance.create(
            adapter_key="onebot_v11",
            instance_key=instance_key,
            display_name=instance_key,
            status="online",
            provider="remote_onebot",
            tenant_id=tenant_id,
            metadata_json="",
        )
        await DBChatChannel.create(
            adapter_key="onebot_v11",
            instance_key=instance_key,
            channel_id=chat_key.rsplit("-", maxsplit=1)[-1],
            chat_key=chat_key,
            channel_type="group",
            data="{}",
        )

    for name, ownership, owner_tenant_id, visibility, base_url, api_key in (
        ("platform_shared", "platform", None, "shared", PLATFORM_BASE_URL, PLATFORM_API_KEY),
        ("platform_private", "platform", None, "private", PLATFORM_BASE_URL, PLATFORM_API_KEY),
        ("acme_own", "tenant", ACME_TENANT_ID, "private", TENANT_BASE_URL, TENANT_API_KEY),
        ("rival_own", "tenant", OTHER_TENANT_ID, "private", "https://rival.example/v1", "sk-rival"),
    ):
        await DBModelGroup.create(
            name=name,
            ownership=ownership,
            owner_tenant_id=owner_tenant_id,
            visibility=visibility,
            group_name=name,
            chat_model="gpt-4o",
            base_url=base_url,
            api_key=api_key,
            model_type="chat",
        )

    service_spy = ConfigServiceSpy()
    monkeypatch.setattr(UnifiedConfigService, "get_config_info", service_spy.get_config_info)
    monkeypatch.setattr(UnifiedConfigService, "get_config_list", service_spy.get_config_list)
    monkeypatch.setattr(UnifiedConfigService, "get_config_item", service_spy.get_config_item)
    monkeypatch.setattr(UnifiedConfigService, "save_config", service_spy.save_config)
    monkeypatch.setattr(UnifiedConfigService, "reload_config", service_spy.reload_config)
    # 继承值展示是另一条正交特性（见 test_overridable_config_snapshot.py），
    # 它会去读适配器层的覆盖文件，与本文件要钉的作用域无关
    monkeypatch.setattr(config_resolver, "inherited_values", _no_inherited_values)

    previous_groups = config.MODEL_GROUPS
    config.MODEL_GROUPS = {
        row.name: row.to_config_group() for row in await DBModelGroup.all().order_by("id")
    }
    try:
        yield service_spy
    finally:
        config.MODEL_GROUPS = previous_groups
        await Tortoise.close_connections()


class TestReadSideHonoursOwnership:
    """读侧与写侧共用一套归属判据，只在共享作用域上分叉"""

    @pytest.mark.parametrize(("config_key", "expected"), READ_SCOPE_CASES)
    async def test_tenant_read_paths(
        self, spy: ConfigServiceSpy, config_key: str, expected: Optional[Type[Exception]]
    ) -> None:
        user = acme_admin()
        for call in (
            lambda: get_config_info(config_key=config_key, _current_user=user),
            lambda: get_config_list(config_key=config_key, _current_user=user),
            lambda: get_config_item(config_key=config_key, item_key="USE_MODEL_GROUP", _current_user=user),
        ):
            spy.touched.clear()
            if expected is None:
                await call()
                assert spy.touched == [config_key]
            else:
                with pytest.raises(expected):
                    await call()
                # 守卫必须挡在服务层之前：服务层一跑就会把覆盖文件落到磁盘上
                assert spy.touched == []

    @pytest.mark.parametrize(("config_key", "expected"), WRITE_SCOPE_CASES)
    async def test_tenant_save_and_reload(
        self, spy: ConfigServiceSpy, config_key: str, expected: Optional[Type[Exception]]
    ) -> None:
        user = acme_admin()
        for call in (
            lambda: save_config_api(config_key=config_key, _current_user=user),
            lambda: reload_config_api(config_key=config_key, _current_user=user),
        ):
            spy.touched.clear()
            if expected is None:
                await call()
                assert spy.touched == [config_key]
            else:
                with pytest.raises(expected):
                    await call()
                assert spy.touched == []

    @pytest.mark.parametrize(
        "config_key",
        sorted({key for key, _ in READ_SCOPE_CASES} | {key for key, _ in WRITE_SCOPE_CASES}),
    )
    async def test_platform_owner_is_never_filtered(self, spy: ConfigServiceSpy, config_key: str) -> None:
        """未启用多租户的部署里全部账号都走这条分支，行为必须与改造前逐字一致"""
        user = platform_owner()
        for call in (
            lambda: get_config_info(config_key=config_key, _current_user=user),
            lambda: get_config_list(config_key=config_key, _current_user=user),
            lambda: get_config_item(config_key=config_key, item_key="USE_MODEL_GROUP", _current_user=user),
            lambda: save_config_api(config_key=config_key, _current_user=user),
            lambda: reload_config_api(config_key=config_key, _current_user=user),
        ):
            spy.touched.clear()
            await call()
            assert spy.touched == [config_key]


class TestConfigKeyListDoesNotEnumerateOtherTenants:
    """`/config/keys` 的清单是运行时长出来的，不是编译期常量

    `UnifiedConfigService._get_config_instance()` 把动态加载到的配置登记进 `ConfigManager`，
    所以每有一个频道来消息、或平台方看一眼别人的实例配置，对应的 `channel_config_*` /
    `instance_override_*` 就会长进这张表。逐条读已经 404，但清单里的 `instance_key` 与
    `chat_key` 本身就是别处端点的入参 —— 泄露它等于把别人的实例与群变成可枚举的。
    """

    async def test_tenant_only_sees_its_own_scoped_keys(
        self, spy: ConfigServiceSpy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registered = [
            "system",
            "adapter_onebot_v11",
            "plugin_whatever",
            OWN_INSTANCE_CONFIG_KEY,
            FOREIGN_INSTANCE_CONFIG_KEY,
            OWN_CHANNEL_CONFIG_KEY,
            FOREIGN_CHANNEL_CONFIG_KEY,
        ]
        monkeypatch.setattr(UnifiedConfigService, "get_all_config_keys", lambda: list(registered))

        assert await get_config_keys(_current_user=acme_admin()) == [
            "system",
            "adapter_onebot_v11",
            "plugin_whatever",
            OWN_INSTANCE_CONFIG_KEY,
            OWN_CHANNEL_CONFIG_KEY,
        ]
        assert await get_config_keys(_current_user=platform_owner()) == registered


class TestModelGroupRuntimeViewIsFilteredByOwnership:
    """`GET /config/model-groups` 必须与 `/console/model-groups` 判定同源"""

    async def test_tenant_sees_only_visible_groups_with_masked_platform_credentials(
        self, spy: ConfigServiceSpy
    ) -> None:
        groups = await get_model_groups(_current_user=acme_admin())

        assert sorted(groups) == ["acme_own", "platform_shared"]

        platform = groups["platform_shared"]
        assert platform.API_KEY == CREDENTIAL_MASK
        # 地址同样暴露平台方接的是哪家上游，契约里与凭据同等对待
        assert platform.BASE_URL == CREDENTIAL_MASK

        own = groups["acme_own"]
        assert own.API_KEY == TENANT_API_KEY
        assert own.BASE_URL == TENANT_BASE_URL

    async def test_masking_does_not_pollute_the_runtime_view(self, spy: ConfigServiceSpy) -> None:
        """脱敏必须发生在副本上，否则一次租户请求会把全局运行时视图写成一串星号"""
        await get_model_groups(_current_user=acme_admin())

        assert config.MODEL_GROUPS["platform_shared"].API_KEY == PLATFORM_API_KEY
        assert config.MODEL_GROUPS["platform_shared"].BASE_URL == PLATFORM_BASE_URL

    async def test_platform_owner_gets_the_runtime_view_verbatim(self, spy: ConfigServiceSpy) -> None:
        groups = await get_model_groups(_current_user=platform_owner())

        assert groups is config.MODEL_GROUPS
        assert sorted(groups) == ["acme_own", "platform_private", "platform_shared", "rival_own"]
        assert groups["platform_shared"].API_KEY == PLATFORM_API_KEY


class TestSystemConfigReadIsNotABypassOfModelGroupOwnership:
    """`MODEL_GROUPS` 是 `system` 的一个普通配置项，取值是整张模型组表

    只收敛 `GET /config/model-groups` 是不够的：凡是把整份 `CoreConfig` 导出给租户的路径
    都是同一份数据的等价出口。`mask_credentials` 按字段名判凭据，`API_KEY` 挡得住，
    **组名 / `BASE_URL` / `CHAT_PROXY` 挡不住** —— 而后三样正是那次收敛要挡的东西。

    这里逐条钉住三个出口：`/config/list/system`、`/config/get/system/MODEL_GROUPS`、
    `/config/effective/{chat_key}`。
    """

    async def test_config_list_hands_the_tenant_the_filtered_view(self, spy: ConfigServiceSpy) -> None:
        items = await get_config_list(config_key="system", _current_user=acme_admin())

        groups = next(item.value for item in items if item.key == "MODEL_GROUPS")
        assert sorted(groups) == ["acme_own", "platform_shared"]
        assert groups["platform_shared"].BASE_URL == CREDENTIAL_MASK
        assert groups["platform_shared"].API_KEY == CREDENTIAL_MASK
        assert groups["acme_own"].BASE_URL == TENANT_BASE_URL

    async def test_single_item_read_matches_the_list_view(self, spy: ConfigServiceSpy) -> None:
        item = await get_config_item(config_key="system", item_key="MODEL_GROUPS", _current_user=acme_admin())

        assert sorted(item.value) == ["acme_own", "platform_shared"]
        assert item.value["platform_shared"].BASE_URL == CREDENTIAL_MASK

    async def test_effective_config_trace_filters_model_groups(
        self, spy: ConfigServiceSpy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """溯源逐字段遍历整个 `CoreConfig`，模型组这一项同样会把全部租户的组带出来

        溯源按 JSON 模式导出，因此这里断言的是裁剪后的**原生结构**，形态与改前一致。
        """

        async def _trace(chat_key: str, instance_key: Optional[str] = None) -> EffectiveConfigTrace:
            return EffectiveConfigTrace(
                chat_key=chat_key,
                adapter_key="onebot_v11",
                instance_key="inst1",
                version=1,
                active_layers=[],
                items=[
                    EffectiveConfigFieldTrace(
                        key="MODEL_GROUPS",
                        title="模型组配置",
                        value={name: group.model_dump(mode="json") for name, group in config.MODEL_GROUPS.items()},
                        source=ConfigLayer.SYSTEM,
                        overridable=False,
                    ),
                ],
            )

        monkeypatch.setattr(config_resolver, "explain_effective_config", _trace)

        trace = await get_effective_config_trace(chat_key=OWN_CHAT_KEY, _current_user=acme_admin())
        groups = trace.items[0].value
        assert sorted(groups) == ["acme_own", "platform_shared"]
        assert groups["platform_shared"]["BASE_URL"] == CREDENTIAL_MASK
        assert groups["acme_own"]["BASE_URL"] == TENANT_BASE_URL

        owner_trace = await get_effective_config_trace(chat_key=OWN_CHAT_KEY, _current_user=platform_owner())
        assert sorted(owner_trace.items[0].value) == [
            "acme_own", "platform_private", "platform_shared", "rival_own",
        ]

    @pytest.mark.parametrize("item_key", ["MODEL_GROUPS", "USE_MODEL_GROUP"])
    async def test_platform_owner_read_is_untouched(self, spy: ConfigServiceSpy, item_key: str) -> None:
        """未启用多租户的部署走的就是这条分支，取值必须与配置服务给出的逐字一致"""
        item = await get_config_item(config_key="system", item_key=item_key, _current_user=platform_owner())
        expected = ConfigServiceSpy().get_config_item("system", item_key)["value"]

        assert item.value == expected
