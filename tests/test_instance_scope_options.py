"""实例作用域切换器的候选列表端点

顶栏那个「切换管理作用域」在几十个 Bot 的部署规模下有两个问题，一个在界面一个在接口：

- 界面：几十条一次性铺进下拉菜单，既找不到也渲染贵 —— 归前端（搜索 + 虚拟滚动）；
- 接口：它此前直接复用 `GET /console/instances`，而那个接口**每个 `builtin_napcat` 实例
  都要新建一个 Docker 客户端做一次 inspect**（`container_manager._docker_client` 按操作
  粒度开闭、不复用），外加三条跨 `chat_message` 全表的聚合。切换器挂在 `MainLayout` 上，
  每开一个后台标签页就要付一次这笔钱，而这两样它一个都不用。

因此有了 `GET /console/instance-options`。这个文件钉住它必须同时成立的三件事：

1. **两个列表接口对同一实例给出同一个答案**。它们是同一份数据的两个投影，一旦状态解析
   之类的判据只改了一边，就会出现「实例管理页说未绑定、切换器说离线」这种没有任何运行时
   信号的分叉。这条断言正是为此存在（`unbound` 状态就是在本轮并行开发中加进来的）。
2. **轻量不是形容词**。断言候选模型里根本没有 `container` / `stats` 字段，且组装候选列表
   的路径上一次容器查询都不发 —— 否则"轻量端点"会在某次顺手加个字段之后悄悄变回原样。
3. **归属判定与 `GET /instances` 是同一处**。租户只看得到自己名下的实例，平台方不受限。
"""

import datetime
from typing import Callable, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tortoise import Tortoise

from nekro_agent.core.exception_handlers import register_exception_handlers
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.console import instances as instances_router
from nekro_agent.routers.console.instances import (
    get_console_instance_options,
    get_console_instances,
)
from nekro_agent.schemas.console import InstanceOption, InstanceStatus
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.console import instance_manager
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2

# (instance_key, provider, provider_account_id, status, enabled, tenant_id)
#
# 覆盖切换器真正要分辨的几种形态：在线的、掉线的、协议端自己连进来待认领的、
# 建好了却从没绑过账号的空壳、被管理员停用的、以及一个别的租户的。
# 归档与软删墓碑各留一条：前者两个接口都要下发（前端自己滤掉），后者两个接口都不能下发。
# `mailbox` 是原生适配器：账号列恒为空但**不是**未绑定，见 `TestAccountlessRowsAreNotAllTheSame`。
INSTANCE_LAYOUT = [
    ("acme_live", "builtin_napcat", "10000001", "online", True, ACME_TENANT_ID),
    ("acme_down", "builtin_napcat", "10000002", "offline", True, ACME_TENANT_ID),
    ("ob10000003", "onebot_v11", "10000003", "unclaimed", True, None),
    ("shell1", "builtin_napcat", "", "offline", True, None),
    ("parked", "onebot_v11", "10000005", "disabled", False, None),
    ("rival1", "onebot_v11", "10000006", "online", True, RIVAL_TENANT_ID),
    ("old_archive", "onebot_v11", "10000007", "archived", False, None),
    ("gravestone", "onebot_v11", "10000008", instance_manager.STATUS_TOMBSTONE, False, None),
    ("mailbox", "native", "", "offline", True, None),
]

VISIBLE_KEYS = [row[0] for row in INSTANCE_LAYOUT if row[3] != instance_manager.STATUS_TOMBSTONE]
ACME_KEYS = [row[0] for row in INSTANCE_LAYOUT if row[5] == ACME_TENANT_ID]


def make_user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    return DBUser(
        username=f"user-{tenant_role.value}-{tenant_id}",
        password="",
        adapter_key="",
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


def single_tenant_admin() -> DBUser:
    """未启用多租户的部署里的普通管理员：无租户归属、角色为平台方"""
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Admin)


UNRESTRICTED_IDENTITIES = [
    pytest.param(platform_owner, id="platform-owner"),
    pytest.param(single_tenant_admin, id="single-tenant"),
]


class ContainerProbeCounter:
    """数一数组装列表的过程中查了几次协议端容器"""

    def __init__(self) -> None:
        self.calls: List[str] = []

    async def __call__(self, instance: DBAdapterInstance):
        self.calls.append(instance.instance_key)
        return None


@pytest.fixture
async def db(monkeypatch: pytest.MonkeyPatch):
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    # 测试环境没有 Docker，也不该因为有没有 Docker 而让断言飘：
    # 容器查询统一换成计数器，`GET /instances` 的那 N 次往返因此变成可断言的事实。
    counter = ContainerProbeCounter()
    monkeypatch.setattr(
        "nekro_agent.services.console.container_manager.get_container_info",
        counter,
        raising=True,
    )
    try:
        for instance_key, provider, account_id, status, enabled, tenant_id in INSTANCE_LAYOUT:
            await DBAdapterInstance.create(
                adapter_key=ADAPTER,
                instance_key=instance_key,
                display_name=f"名称-{instance_key}",
                provider=provider,
                provider_account_id=account_id,
                status=status,
                enabled=enabled,
                tenant_id=tenant_id,
            )
        yield counter
    finally:
        await Tortoise.close_connections()


class TestOptionsAgreeWithTheFullList:
    """两个列表接口是同一份数据的两个投影，答案必须逐字段相同"""

    async def test_same_instances_in_the_same_order(self, db) -> None:  # noqa: ARG002
        options = await instance_manager.list_instance_options()
        full = await instance_manager.list_instances()

        assert [item.instance_key for item in options] == VISIBLE_KEYS
        assert [item.instance_key for item in options] == [item.instance_key for item in full.items]

    async def test_shared_fields_are_identical(self, db) -> None:  # noqa: ARG002
        """特别是 `status`：`unbound`（建好了但从没绑过账号）是读侧现算的，
        两个接口各算一次就会分叉成「列表页说未绑定、切换器说离线」，而且没有任何运行时信号。
        """
        options = {item.instance_key: item for item in await instance_manager.list_instance_options()}
        full = {item.instance_key: item for item in (await instance_manager.list_instances()).items}

        assert set(options) == set(full)
        for key, option in options.items():
            info = full[key]
            assert (
                option.display_name,
                option.adapter_key,
                option.provider_account_id,
                option.status,
                option.enabled,
            ) == (
                info.display_name,
                info.adapter_key,
                info.provider_account_id,
                info.status,
                info.enabled,
            ), f"实例 {key} 在两个列表接口下的字段不一致"

    async def test_tombstone_is_excluded_from_both(self, db) -> None:  # noqa: ARG002
        options = await instance_manager.list_instance_options()
        full = await instance_manager.list_instances()

        assert "gravestone" not in {item.instance_key for item in options}
        assert "gravestone" not in {item.instance_key for item in full.items}


class TestOptionsStayLightweight:
    """"轻量"必须是可断言的事实，不是文档里的形容词"""

    def test_option_model_carries_no_container_or_stats(self) -> None:
        """字段集合整个钉死

        `container` / `stats` 是这个端点存在的全部理由；顺手加回去不会有任何报错，
        只会让切换器重新开始为每个 builtin_napcat 实例付一次 Docker 往返。
        """
        assert set(InstanceOption.model_fields) == {
            "instance_key",
            "display_name",
            "adapter_key",
            "provider_account_id",
            "status",
            "enabled",
        }

    async def test_option_model_carries_no_provider(self) -> None:
        """`provider` 不在候选模型里，这一条是给前端看的

        切换器因此**分辨不了**「账号为空」是异常还是常态（原生适配器的账号列恒为空），
        它只能、也只应该读 `status`。上面那条字段断言已经钉住了字段集合，
        这一条把「所以前端不许自己判 provider_account_id」的理由写在旁边。
        """
        assert "provider" not in InstanceOption.model_fields

    async def test_options_never_probe_containers(self, db: ContainerProbeCounter) -> None:
        """回归本体：候选列表一次容器查询都不发"""
        await instance_manager.list_instance_options()

        assert db.calls == []

    async def test_full_list_still_probes_every_napcat_instance(self, db: ContainerProbeCounter) -> None:
        """对照组：同一批数据走 `list_instances()` 时，每个 builtin_napcat 实例查一次

        这条不是在夸 `list_instances()`，而是把"省下来的到底是什么"写成数字：
        本表 8 条里有 3 个 builtin_napcat，大量实例的部署上这就是 N 次 Docker 往返（N 为实例数）。
        """
        await instance_manager.list_instances()

        assert sorted(db.calls) == sorted(
            row[0] for row in INSTANCE_LAYOUT if row[1] == "builtin_napcat" and row[3] != instance_manager.STATUS_TOMBSTONE
        )


class TestAccountlessRowsAreNotAllTheSame:
    """「`provider_account_id` 是空串」不是一个可以自己下结论的信号

    切换器要把「需处理」排在前面、并在行里标出「未绑定账号」。它一度是自己判的
    （`enabled && !provider_account_id`），而这个判据对**原生适配器**是错的：
    email / minecraft / sse 的连接来自适配器自身配置，账号列**恒为空**，
    判它们未绑定就是给一句永远不适用的引导，并把它们永久钉进「需处理」组与角标里，
    管理员没有任何办法让这个计数归零。

    正确的做法是只读后端算好的 `status`：真正没绑账号的实例是 `unbound`，
    原生适配器仍然是 `offline`。本类把这条差别钉在切换器实际消费的那个端点上 ——
    仓库里没有前端测试运行器，这是唯一能拦住它再次分叉的地方。
    """

    async def test_onebot_shell_is_unbound_but_native_stays_offline(self, db) -> None:  # noqa: ARG002
        options = {item.instance_key: item for item in await instance_manager.list_instance_options()}

        # 两行的 `provider_account_id` 都是空串，只有 `status` 分得出它们
        assert options["shell1"].provider_account_id == ""
        assert options["mailbox"].provider_account_id == ""
        assert options["shell1"].status is InstanceStatus.UNBOUND
        assert options["mailbox"].status is InstanceStatus.OFFLINE

    async def test_no_accountless_row_is_silently_unbound(self, db) -> None:  # noqa: ARG002
        """反过来钉一次：账号为空 ≠ unbound，判据在 `account_binding_required()` 那一处"""
        accountless = {
            row[0]: row[1] for row in INSTANCE_LAYOUT if row[2] == "" and row[3] != instance_manager.STATUS_TOMBSTONE
        }
        options = {item.instance_key: item for item in await instance_manager.list_instance_options()}

        for key, provider in accountless.items():
            expected_unbound = instance_manager.account_binding_required(provider)
            assert (options[key].status is InstanceStatus.UNBOUND) is expected_unbound, key


class TestTenantScope:
    """归属判定与 `GET /instances` 同一处（`_visible_instance_keys`）"""

    async def test_tenant_sees_only_its_own_instances(self, db) -> None:  # noqa: ARG002
        response = await get_console_instance_options(_current_user=acme_admin())

        assert [item.instance_key for item in response.items] == ACME_KEYS
        assert response.total == len(ACME_KEYS)

    async def test_tenant_never_sees_another_tenant_or_platform_instances(self, db) -> None:  # noqa: ARG002
        response = await get_console_instance_options(_current_user=acme_admin())
        visible = {item.instance_key for item in response.items}

        assert "rival1" not in visible
        assert "shell1" not in visible

    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_unrestricted_identities_see_everything(
        self,
        db,  # noqa: ARG002
        identity: Callable[[], DBUser],
    ) -> None:
        """未启用多租户的部署行为必须逐字不变：`get_tenant_scope()` 恒为 None，不进任何过滤分支"""
        response = await get_console_instance_options(_current_user=identity())

        assert [item.instance_key for item in response.items] == VISIBLE_KEYS

    @pytest.mark.parametrize(
        "identity",
        [pytest.param(acme_admin, id="tenant-admin"), pytest.param(platform_owner, id="platform-owner")],
    )
    async def test_both_endpoints_expose_the_same_instance_set(
        self,
        db,  # noqa: ARG002
        identity: Callable[[], DBUser],
    ) -> None:
        """同一身份下两个端点可见的实例集合必须相同

        判据分叉的表现是「切换器里有这个实例、点进去 404」或反过来，两种都不会报错。
        """
        user = identity()
        options = await get_console_instance_options(_current_user=user)
        full = await get_console_instances(_current_user=user)

        assert [item.instance_key for item in options.items] == [item.instance_key for item in full.items]


@pytest.fixture
def client():
    """真实路由 + 真实异常处理器，身份用依赖覆盖注入"""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(instances_router.router, prefix="/api/console")
    test_client = TestClient(app, raise_server_exceptions=False)

    def login_as(user: Optional[DBUser]) -> None:
        if user is None:
            app.dependency_overrides.pop(get_current_active_user, None)
            return
        app.dependency_overrides[get_current_active_user] = lambda: user

    test_client.login_as = login_as  # type: ignore[attr-defined]
    return test_client


class TestRealRequest:
    """走一遍真实 HTTP 栈

    直接 `await` 路由函数会跳过 FastAPI 的响应模型校验，也验不到 `@require_role`。
    而这个装饰器**只在被包函数的参数名叫 `_current_user` 时生效**，名字不对时第一行就是
    `AttributeError` —— 表现为 500，直接调用函数是看不出来的。
    """

    async def test_platform_owner_gets_200_with_the_full_payload(self, db, client: TestClient) -> None:  # noqa: ARG002
        client.login_as(platform_owner())  # type: ignore[attr-defined]

        response = client.get("/api/console/instance-options")

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["total"] == len(VISIBLE_KEYS)
        assert [item["instance_key"] for item in payload["items"]] == VISIBLE_KEYS
        assert set(payload["items"][0]) == {
            "instance_key",
            "display_name",
            "adapter_key",
            "provider_account_id",
            "status",
            "enabled",
        }

    async def test_tenant_admin_gets_only_its_own(self, db, client: TestClient) -> None:  # noqa: ARG002
        client.login_as(acme_admin())  # type: ignore[attr-defined]

        response = client.get("/api/console/instance-options")

        assert response.status_code == 200, response.text
        assert [item["instance_key"] for item in response.json()["items"]] == ACME_KEYS

    async def test_read_only_member_is_refused(self, db, client: TestClient) -> None:  # noqa: ARG002
        """租户普通用户（`Role.User`）够不到 `Role.Admin`

        这条同时证明 `@require_role` 真的挂上了：参数名写错时它会在 500 而不是 403 上失败。
        """
        reader = make_user(tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_USER, perm_level=Role.User)
        client.login_as(reader)  # type: ignore[attr-defined]

        response = client.get("/api/console/instance-options")

        assert response.status_code == 403, response.text

    async def test_instance_named_options_is_still_reachable(self, db, client: TestClient) -> None:  # noqa: ARG002
        """`options` 是合法的实例标识：它的详情端点不能被候选列表路由遮住"""
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key="options",
            display_name="名字就叫 options",
            provider="onebot_v11",
            provider_account_id="10000099",
            status="online",
            enabled=True,
        )
        client.login_as(platform_owner())  # type: ignore[attr-defined]

        detail = client.get("/api/console/instances/options")

        assert detail.status_code == 200, detail.text
        assert detail.json()["instance_key"] == "options"


class TestPathDoesNotCollideWithInstanceKeys:
    """路径不能挂在 `/instances/` 下面"""

    def test_options_path_cannot_shadow_an_instance_named_options(self) -> None:
        """`options` 是一个合法的 `instance_key`（`^[a-z0-9_]{1,32}$`）

        挂成 `/instances/options` 就要抢在 `/instances/{instance_key}` 之前注册：
        顺序一变就静默变成「查一个叫 options 的实例」，而真有实例叫 `options` 时，
        它的详情端点会被永久遮住。两种故障都没有运行时信号，所以换了个不会碰撞的路径。
        """
        from nekro_agent.routers.console.instances import router

        paths = {route.path for route in router.routes}  # type: ignore[attr-defined]

        assert "/instance-options" in paths
        assert "/instances/options" not in paths
