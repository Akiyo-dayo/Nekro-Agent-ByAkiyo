"""插件接口的租户作用域

`routers/plugins.py` 的 19 条端点此前只有 `Role.Admin`，而租户管理员本身就是 `Role.Admin`
（`TENANT_MAX_PERM_LEVEL`）。本模块钉住三条改完之后必须成立、且**没有任何运行时信号**的契约：

1. **会话级覆盖要单独校验 `chat_key`。**
   `PUT /plugins/scope/{instance_key}/{plugin_key}?chat_key=...` 写的是 `chat_key` 那一层
   （见 `plugin/scope.py` 的三层优先级），只校验 `instance_key` 等于让租户拿自己的实例标识
   当通行证，去改**别人频道**里的插件可见性。这一条最隐蔽：路径参数看着是被守住的。

2. **插件数据按频道归属过滤。**
   `plugin_data` 是一张按 `plugin_key` 索引的跨租户表，`DELETE /plugins/data/{plugin_id}`
   原本一次清空所有人的数据。`target_chat_key` 为空的行是插件的全局存储，不挂在任何频道上，
   因此不属于任何租户 —— 租户既读不到也删不掉，否则"重置我的数据"会顺手抹掉平台方的状态。

3. **实例级插件配置不能把平台方的凭据递出去。**
   该接口解析出来的是「全局值 + 本实例覆盖」，未被覆盖的凭据字段回落的正是平台方全局配好的
   真实 Key（`google_search` 的 API_KEY/CX、`github` 的 TOKEN 都是 `is_secret=True`）。
   作用域守卫只挡得住"读别人的实例"，挡不住"从自己的实例读平台方的 Key"——
   这与 `cc_model_presets` 曾经的凭据裸奔是同一种洞。
   读侧脱敏之后写侧必须认掩码哨兵，否则一次无意的保存会把凭据覆盖成 `********`。
   哨兵必须能穿透 dict/list：读侧的 `mask_credentials` 是递归的，只判「整值等于掩码」
   接不住复杂字段里嵌着的凭据。

4. **加载失败条目不能把宿主路径递出去。**
   `filePath` / `stackTrace` 是显式字段，`errorMessage` 与 `description` 则是异常文本本身，
   `SyntaxError: invalid syntax (C:\\srv\\...\\broken.py, line 3)` 会把同一条路径原样带回来。

越权一律 **404**（403 会让别人的实例/频道变成可枚举的）；平台方一侧的每条断言都是
"行为与改造前逐字一致"，未启用多租户的部署 `get_tenant_scope()` 恒为 None，一个分支都不进。

全局启停 / 激活策略 / 重载 / 路由自省提到 `Role.Super` 这一档由
`tests/test_route_permission_contract.py` 的结构断言钉住，不在这里重复。
"""

import datetime
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest
from pydantic import Field
from tortoise import Tortoise

from nekro_agent.core.core_utils import ConfigBase, ExtraField
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_plugin_data import DBPluginData
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.plugins import (
    PluginScopeOverrideRequest,
    SavePluginConfigRequest,
    delete_plugin_data,
    get_plugin_data,
    get_plugin_detail_handler,
    get_plugin_instance_config_handler,
    get_plugins,
    reset_plugin_data,
    save_plugin_instance_config_handler,
    set_plugin_scope,
)
from nekro_agent.schemas.errors import NotFoundError
from nekro_agent.schemas.tenant import CREDENTIAL_MASK, TenantRole
from nekro_agent.services.plugin import scope as scope_mod
from nekro_agent.services.plugin.base import NekroPlugin
from nekro_agent.services.plugin.collector import FailedPluginInfo, plugin_collector
from nekro_agent.services.user.role import Role

ACME_TENANT_ID = 1
OTHER_TENANT_ID = 2

OWN_CHAT_KEY = "onebot_v11-inst1-group_10001"
FOREIGN_CHAT_KEY = "onebot_v11-rival1-group_20001"

PLATFORM_API_KEY = "AIzaSy-platform-owned-search-key"
PLATFORM_NESTED_KEY = "sk-platform-owned-nested-key"
PLATFORM_LIST_TOKEN = "ghp-platform-owned-list-token"

HOST_PLUGIN_PATH = r"C:\srv\nekro\plugins\builtin\broken.py"


class ProbeConfig(ConfigBase):
    """带一个 `is_secret` 字段的插件配置，形态取自 `plugins/builtin/google_search.py`

    另外两个复杂字段用来钉住「掩码是递归打的」：`PROVIDERS` 里的 `openai_api_key`
    与 `EXTRA_TOKENS` 这个字段名都由 `config._is_credential_field_name` 兜底判成凭据，
    读侧会逐个位置打掩码，写侧因此必须逐个位置还原。
    """

    API_KEY: str = Field(default="", title="API 密钥", json_schema_extra=ExtraField(is_secret=True).model_dump())
    MODE: str = Field(default="fast", title="模式")
    PROVIDERS: Dict[str, str] = Field(default_factory=dict, title="供应商")
    EXTRA_TOKENS: List[str] = Field(default_factory=list, title="额外令牌")


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await seed_instances_and_channels()
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def plugin(tmp_path: Path):
    """一个只存在于本次测试里的插件，配置与作用域覆盖全部落在 tmp_path

    落盘目录必须整体改道：作用域覆盖是按层级聚合的单文件，写进真实 DATA_DIR 会污染开发机
    上真实部署的插件状态。
    """
    data_dir = tmp_path / "plugin_data"
    original = (scope_mod.PLUGIN_DATA_DIR, scope_mod.SCOPE_OVERRIDE_DIR)
    scope_mod.PLUGIN_DATA_DIR = data_dir
    scope_mod.SCOPE_OVERRIDE_DIR = data_dir / "_overrides"
    scope_mod._scope_override_cache.clear()  # noqa: SLF001
    scope_mod._scoped_config_cache.clear()  # noqa: SLF001

    probe = NekroPlugin(
        name="作用域探针",
        module_name="scope_probe",
        description="仅用于租户作用域测试",
        version="0.1.0",
        author="Probe",
        url="",
    )
    # 路径要在挂载配置类之前改掉：`mount_config()` 当场就会把默认值落盘一次
    probe._plugin_path = data_dir / probe.key  # noqa: SLF001
    probe._plugin_config_path = probe._plugin_path / "config.yaml"  # noqa: SLF001
    probe.mount_config()(ProbeConfig)

    global_config = probe.get_global_config(ProbeConfig)
    global_config.API_KEY = PLATFORM_API_KEY
    global_config.PROVIDERS = {"openai_api_key": PLATFORM_NESTED_KEY, "endpoint": "https://api.example.com"}
    global_config.EXTRA_TOKENS = [PLATFORM_LIST_TOKEN]
    probe.save_config(global_config)

    plugin_collector.loaded_plugins[probe.key] = probe
    try:
        yield probe
    finally:
        plugin_collector.loaded_plugins.pop(probe.key, None)
        scope_mod.PLUGIN_DATA_DIR, scope_mod.SCOPE_OVERRIDE_DIR = original
        scope_mod._scope_override_cache.clear()  # noqa: SLF001
        scope_mod._scoped_config_cache.clear()  # noqa: SLF001


async def seed_instances_and_channels() -> None:
    """两个租户各一个实例与一个频道，形态取自测试环境（`acme_ltd` 名下只有 `inst1`）"""
    layout: List[Tuple[str, int, str]] = [
        ("inst1", ACME_TENANT_ID, OWN_CHAT_KEY),
        ("rival1", OTHER_TENANT_ID, FOREIGN_CHAT_KEY),
    ]
    for instance_key, tenant_id, chat_key in layout:
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


def make_user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    """构造一个未落库的登录身份；作用域判定只读这三个字段"""
    return DBUser(
        username=f"user-{tenant_role.value}-{tenant_id}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )


def acme_admin() -> DBUser:
    return make_user(tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def platform_owner() -> DBUser:
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


async def seed_plugin_data(plugin_key: str) -> dict:
    """三种归属各一行：自己的频道、别人的频道、无频道（插件全局存储）"""
    rows = {}
    for label, chat_key in (("own", OWN_CHAT_KEY), ("foreign", FOREIGN_CHAT_KEY), ("global", "")):
        rows[label] = await DBPluginData.create(
            plugin_key=plugin_key,
            data_key=f"k_{label}",
            data_value=f"v_{label}",
            target_chat_key=chat_key,
            target_user_id="",
        )
    return rows


class TestSessionOverrideChecksTheChannelNotJustTheInstance:
    """会话级覆盖的作用域键是 `chat_key`，它必须自己过一次守卫"""

    @pytest.mark.parametrize(
        ("chat_key", "expected_scope_keys"),
        [
            ("", {"inst1"}),
            (OWN_CHAT_KEY, {OWN_CHAT_KEY}),
        ],
    )
    async def test_tenant_may_write_its_own_scopes(self, db, plugin, chat_key, expected_scope_keys) -> None:
        await set_plugin_scope(
            instance_key="inst1",
            plugin_key=plugin.key,
            body=PluginScopeOverrideRequest(enabled=True),
            chat_key=chat_key,
            _current_user=acme_admin(),
        )
        written = scope_mod.OverrideScope.SESSION if chat_key else scope_mod.OverrideScope.INSTANCE
        assert set(scope_mod._load_scope_overrides(written)) == expected_scope_keys  # noqa: SLF001

    async def test_tenant_cannot_write_a_session_override_for_a_foreign_channel(self, db, plugin) -> None:
        """自己的实例 + 别人的频道：这是只校验 `instance_key` 时唯一漏出去的形状"""
        with pytest.raises(NotFoundError):
            await set_plugin_scope(
                instance_key="inst1",
                plugin_key=plugin.key,
                body=PluginScopeOverrideRequest(enabled=False),
                chat_key=FOREIGN_CHAT_KEY,
                _current_user=acme_admin(),
            )
        assert scope_mod.get_scope_overrides(scope_mod.OverrideScope.SESSION, FOREIGN_CHAT_KEY) == {}

    async def test_tenant_cannot_write_a_foreign_instance_override(self, db, plugin) -> None:
        with pytest.raises(NotFoundError):
            await set_plugin_scope(
                instance_key="rival1",
                plugin_key=plugin.key,
                body=PluginScopeOverrideRequest(enabled=False),
                chat_key="",
                _current_user=acme_admin(),
            )
        assert scope_mod.get_scope_overrides(scope_mod.OverrideScope.INSTANCE, "rival1") == {}

    async def test_platform_owner_is_unrestricted(self, db, plugin) -> None:
        """未启用多租户的部署走的就是这条分支，行为必须与改造前逐字一致"""
        await set_plugin_scope(
            instance_key="rival1",
            plugin_key=plugin.key,
            body=PluginScopeOverrideRequest(enabled=False),
            chat_key=FOREIGN_CHAT_KEY,
            _current_user=platform_owner(),
        )
        assert scope_mod.get_scope_overrides(scope_mod.OverrideScope.SESSION, FOREIGN_CHAT_KEY) == {plugin.key: False}


class TestPluginDataFollowsChannelOwnership:
    """`plugin_data` 是一张跨租户表，按 `target_chat_key` 的归属裁剪"""

    async def test_listing_is_filtered_for_tenants_and_intact_for_the_platform(self, db, plugin) -> None:
        await seed_plugin_data(plugin.key)

        tenant_view = await get_plugin_data(plugin_id=plugin.key, _current_user=acme_admin())
        assert [item.data_key for item in tenant_view] == ["k_own"]

        platform_view = await get_plugin_data(plugin_id=plugin.key, _current_user=platform_owner())
        assert sorted(item.data_key for item in platform_view) == ["k_foreign", "k_global", "k_own"]

    @pytest.mark.parametrize("label", ["foreign", "global"])
    async def test_tenant_cannot_delete_a_row_it_cannot_see(self, db, plugin, label) -> None:
        """越权与不存在同样 404；`global` 那行是插件的全局存储，不属于任何租户"""
        rows = await seed_plugin_data(plugin.key)
        with pytest.raises(NotFoundError):
            await delete_plugin_data(plugin_id=plugin.key, data_id=rows[label].id, _current_user=acme_admin())
        assert await DBPluginData.filter(id=rows[label].id).exists()

    async def test_tenant_deletes_its_own_row(self, db, plugin) -> None:
        rows = await seed_plugin_data(plugin.key)
        await delete_plugin_data(plugin_id=plugin.key, data_id=rows["own"].id, _current_user=acme_admin())
        assert not await DBPluginData.filter(id=rows["own"].id).exists()

    async def test_reset_clears_only_what_the_caller_can_see(self, db, plugin) -> None:
        await seed_plugin_data(plugin.key)

        await reset_plugin_data(plugin_id=plugin.key, _current_user=acme_admin())
        remaining = sorted(await DBPluginData.filter(plugin_key=plugin.key).values_list("data_key", flat=True))
        assert remaining == ["k_foreign", "k_global"]

        await reset_plugin_data(plugin_id=plugin.key, _current_user=platform_owner())
        assert await DBPluginData.filter(plugin_key=plugin.key).count() == 0


class TestInstanceConfigNeverHandsOutPlatformCredentials:
    """实例级插件配置读到的未覆盖字段就是平台方的全局值"""

    async def test_tenant_reads_masked_credentials(self, db, plugin) -> None:
        items = await get_plugin_instance_config_handler(
            plugin_key=plugin.key,
            instance_key="inst1",
            _current_user=acme_admin(),
        )
        by_key = {item["key"]: item for item in items}
        assert by_key["API_KEY"]["value"] == CREDENTIAL_MASK
        assert by_key["API_KEY"]["is_secret"] is True
        assert by_key["MODE"]["value"] == "fast"

    async def test_platform_owner_still_reads_the_real_value(self, db, plugin) -> None:
        items = await get_plugin_instance_config_handler(
            plugin_key=plugin.key,
            instance_key="inst1",
            _current_user=platform_owner(),
        )
        assert {item["key"]: item["value"] for item in items}["API_KEY"] == PLATFORM_API_KEY

    async def test_tenant_cannot_touch_a_foreign_instance(self, db, plugin) -> None:
        with pytest.raises(NotFoundError):
            await get_plugin_instance_config_handler(
                plugin_key=plugin.key,
                instance_key="rival1",
                _current_user=acme_admin(),
            )
        with pytest.raises(NotFoundError):
            await save_plugin_instance_config_handler(
                plugin_key=plugin.key,
                instance_key="rival1",
                body=SavePluginConfigRequest(configs={"MODE": "slow"}),
                _current_user=acme_admin(),
            )

    async def test_submitting_the_mask_back_means_keep_unchanged(self, db, plugin) -> None:
        """前端把读到的掩码原样提交回来，不能被当成一个新取值落成实例覆盖"""
        await save_plugin_instance_config_handler(
            plugin_key=plugin.key,
            instance_key="inst1",
            body=SavePluginConfigRequest(configs={"API_KEY": CREDENTIAL_MASK, "MODE": "slow"}),
            _current_user=acme_admin(),
        )
        overrides = scope_mod.load_instance_config_overrides(plugin.key, "inst1")
        assert overrides == {"MODE": "slow"}
        assert plugin.get_global_config(ProbeConfig).API_KEY == PLATFORM_API_KEY


class TestMaskingReachesIntoComplexFields:
    """掩码是**递归**打的，写侧的还原必须逐个位置对齐

    只判「整值等于掩码」时，租户把配置页原样保存一次，`PROVIDERS` 里的 `openai_api_key`
    就会被落成字面量 `********`：既没泄露也没保住，是把这份配置直接配坏。
    """

    async def test_tenant_reads_nested_credentials_as_masks(self, db, plugin) -> None:
        items = {
            item["key"]: item["value"]
            for item in await get_plugin_instance_config_handler(
                plugin_key=plugin.key,
                instance_key="inst1",
                _current_user=acme_admin(),
            )
        }
        assert items["PROVIDERS"] == {"openai_api_key": CREDENTIAL_MASK, "endpoint": "https://api.example.com"}
        assert items["EXTRA_TOKENS"] == [CREDENTIAL_MASK]

    async def test_round_tripping_the_form_leaves_every_credential_intact(self, db, plugin) -> None:
        """读到什么就提交什么：不该产生任何覆盖，更不该把嵌套凭据写成星号"""
        items = {
            item["key"]: item["value"]
            for item in await get_plugin_instance_config_handler(
                plugin_key=plugin.key,
                instance_key="inst1",
                _current_user=acme_admin(),
            )
        }
        await save_plugin_instance_config_handler(
            plugin_key=plugin.key,
            instance_key="inst1",
            body=SavePluginConfigRequest(
                configs={
                    "API_KEY": items["API_KEY"],
                    "PROVIDERS": json.dumps(items["PROVIDERS"]),
                    "EXTRA_TOKENS": json.dumps(items["EXTRA_TOKENS"]),
                },
            ),
            _current_user=acme_admin(),
        )
        assert scope_mod.load_instance_config_overrides(plugin.key, "inst1") == {}

    async def test_editing_one_position_keeps_the_others_real(self, db, plugin) -> None:
        """只改 `endpoint`，未动的嵌套凭据要还原成真实值而不是被剔除或写成星号"""
        items = {
            item["key"]: item["value"]
            for item in await get_plugin_instance_config_handler(
                plugin_key=plugin.key,
                instance_key="inst1",
                _current_user=acme_admin(),
            )
        }
        edited = {**items["PROVIDERS"], "endpoint": "https://tenant.example.org"}
        await save_plugin_instance_config_handler(
            plugin_key=plugin.key,
            instance_key="inst1",
            body=SavePluginConfigRequest(configs={"PROVIDERS": json.dumps(edited)}),
            _current_user=acme_admin(),
        )
        assert scope_mod.load_instance_config_overrides(plugin.key, "inst1") == {
            "PROVIDERS": {"openai_api_key": PLATFORM_NESTED_KEY, "endpoint": "https://tenant.example.org"},
        }

    async def test_tenant_can_still_write_its_own_credential(self, db, plugin) -> None:
        """脱敏不能把正常写入也吃掉：非掩码的取值照常落成本实例的覆盖，全局值不受影响"""
        await save_plugin_instance_config_handler(
            plugin_key=plugin.key,
            instance_key="inst1",
            body=SavePluginConfigRequest(configs={"API_KEY": "tenant-own-key"}),
            _current_user=acme_admin(),
        )
        assert scope_mod.load_instance_config_overrides(plugin.key, "inst1") == {"API_KEY": "tenant-own-key"}
        assert plugin.get_global_config(ProbeConfig).API_KEY == PLATFORM_API_KEY


class TestFailedPluginMetaHidesHostInternals:
    """加载失败条目里的宿主路径要连异常文本一起挡住

    `filePath` / `stackTrace` 是显式字段，删掉即可；但 `errorMessage` 就是异常文本本身、
    `description` 又是它的副本，`SyntaxError: ... (C:\\srv\\...\\broken.py, line 3)`
    会把同一条路径原样带回来 —— 只删字段等于没删。
    """

    @pytest.fixture
    def failed_plugin(self):
        info = FailedPluginInfo(
            module_name="broken",
            file_path=HOST_PLUGIN_PATH,
            error_message=f"SyntaxError: invalid syntax ({HOST_PLUGIN_PATH}, line 3)",
            error_type="SyntaxError",
            is_builtin=True,
            stack_trace=f'Traceback (most recent call last):\n  File "{HOST_PLUGIN_PATH}", line 3\n',
        )
        plugin_collector.failed_plugins["builtin_plugins.broken"] = info
        try:
            yield info
        finally:
            plugin_collector.failed_plugins.pop("builtin_plugins.broken", None)

    async def test_tenant_sees_the_error_without_the_host_path(self, db, failed_plugin) -> None:
        entry = next(item for item in await get_plugins(_current_user=acme_admin()) if item.get("loadFailed"))
        detail = await get_plugin_detail_handler(plugin_id="broken", _current_user=acme_admin())

        for payload in (entry, detail):
            assert "filePath" not in payload
            assert "stackTrace" not in payload
            assert HOST_PLUGIN_PATH not in payload["errorMessage"]
            assert HOST_PLUGIN_PATH not in payload["description"]
            # 「挂在什么错上」这条线索必须留下
            assert payload["errorType"] == "SyntaxError"
            assert "SyntaxError: invalid syntax" in payload["errorMessage"]

    async def test_platform_owner_still_gets_everything_verbatim(self, db, failed_plugin) -> None:
        """未启用多租户的部署走的就是这条分支，返回值必须与改造前逐字一致"""
        entry = next(item for item in await get_plugins(_current_user=platform_owner()) if item.get("loadFailed"))
        detail = await get_plugin_detail_handler(plugin_id="broken", _current_user=platform_owner())

        for payload in (entry, detail):
            assert payload["filePath"] == HOST_PLUGIN_PATH
            assert payload["stackTrace"] == failed_plugin.stack_trace
            assert payload["errorMessage"] == failed_plugin.error_message
            assert payload["description"] == failed_plugin.error_message
