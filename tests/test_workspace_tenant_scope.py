"""CC 工作区接口的租户作用域

`routers/workspaces.py` 的 72 个端点此前全是裸 `Role.Admin`，一次租户归属判定都没有。
租户管理员本身就是 `Role.Admin`（`TENANT_MAX_PERM_LEVEL`），于是换个 `workspace_id`
就能列出、读取、改写、删除别的租户的 CC 工作区，连同里面的会话历史、记忆库与产物文件 ——
工作区跑的是 Claude Code，这是整套改造里价值最高的一块数据。

同轮做的网络分组（一个归属一张 Docker 网络）只是纵深防御：不走网络、直接调接口就绕过去了。

这里钉死五条：

1. **归属推导只有一处**：`services/workspace/tenant_scope.py` 的批量版必须与网络分组用的
   `resolve_workspace_network_group` 在每一种边界上给出相同结论。两者分叉就会出现
   「API 认为是 A 租户的、网络却把它挂在平台组网桥上」这种没有任何运行时信号的矛盾状态。
2. **归属不唯一（`shared`）对所有租户 404**：工作目录、会话历史、记忆库、产物文件是整个工作区
   一份，两边的数据混在一起，给任何一侧哪怕只读都是把另一侧交出去。
3. **无频道绑定 / 平台方实例的工作区归平台方**：没有绑定就没有归属信号。
4. **越权是 404 不是 403**，且文案与「不存在」逐字相同，否则工作区 ID 变成可枚举的。
5. **平台方与未启用多租户的部署一个分支都不进**。

另外钉住「新端点不会漏掉守卫」：本次事故的形态就是 72 个端点一路加下来、没有任何一个带作用域，
所以路由表里每个吃 `workspace_id` 的端点都必须能追溯到那一个守卫。
"""

import datetime
import inspect
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.routers.workspaces import (
    bind_channel,
    get_bound_channels,
    get_memory_detail,
    get_workspace,
    get_workspace_cc_preset,
    get_workspace_skills,
    list_workspaces,
    unbind_channel,
    update_workspace,
)
from nekro_agent.routers.workspaces import (
    router as workspaces_router,
)
from nekro_agent.schemas.errors import NotFoundError, PermissionDeniedError
from nekro_agent.schemas.i18n import SupportedLang
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.schemas.workspace import ChannelBindRequest, WorkspaceUpdate
from nekro_agent.services.user.role import Role
from nekro_agent.services.workspace.network_guard import (
    PLATFORM_GROUP,
    SHARED_GROUP,
    resolve_workspace_network_group,
)
from nekro_agent.services.workspace.tenant_scope import (
    accessible_workspace_ids,
    assert_workspace_accessible,
    resolve_workspace_owner_groups,
    tenant_group_key,
)

ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2

ACME_GROUP = tenant_group_key(ACME_TENANT_ID)
RIVAL_GROUP = tenant_group_key(RIVAL_TENANT_ID)

MISSING_WORKSPACE_ID = 9999

# 实例布局取自测试环境：`acme_ltd` 名下 inst1/inst2，`rival1` 属另一租户，
# `ob1000000003` 是协议端自己认领出来、尚未划归任何租户的实例。
INSTANCE_LAYOUT: List[Tuple[str, Optional[int]]] = [
    ("inst1", ACME_TENANT_ID),
    ("inst2", ACME_TENANT_ID),
    ("rival1", RIVAL_TENANT_ID),
    ("ob1000000003", None),
]

# (工作区名, 该工作区绑定频道各自的 instance_key, 期望分组键)
WORKSPACE_LAYOUT: List[Tuple[str, List[str], str]] = [
    ("acme-single", ["inst1"], ACME_GROUP),
    ("acme-two-instances", ["inst1", "inst2"], ACME_GROUP),
    ("rival-single", ["rival1"], RIVAL_GROUP),
    ("shared-two-tenants", ["inst1", "rival1"], SHARED_GROUP),
    ("shared-tenant-and-platform", ["inst1", "ob1000000003"], SHARED_GROUP),
    ("platform-unassigned-instance", ["ob1000000003"], PLATFORM_GROUP),
    ("platform-unbound", [], PLATFORM_GROUP),
    # 多账号化之前的历史频道没有 instance_key，查不到实例行的同理，两者都按平台方计入
    ("platform-legacy-empty-key", [""], PLATFORM_GROUP),
    ("platform-ghost-instance", ["ghost-instance"], PLATFORM_GROUP),
]


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await seed_workspaces()
        yield
    finally:
        await Tortoise.close_connections()


async def seed_workspaces() -> None:
    for instance_key, tenant_id in INSTANCE_LAYOUT:
        await DBAdapterInstance.create(
            adapter_key="onebot_v11",
            instance_key=instance_key,
            display_name=instance_key,
            status="online",
            provider="remote_onebot",
            tenant_id=tenant_id,
            metadata_json="",
        )
    for index, (name, instance_keys, _group) in enumerate(WORKSPACE_LAYOUT):
        workspace = await DBWorkspace.create(name=name, description=name)
        for seq, instance_key in enumerate(instance_keys):
            channel_id = f"group_{index}{seq}"
            await DBChatChannel.create(
                adapter_key="onebot_v11",
                instance_key=instance_key,
                channel_id=channel_id,
                chat_key=f"onebot_v11-{instance_key}-{channel_id}",
                channel_type="group",
                data="{}",
                workspace_id=workspace.id,
            )


async def workspace_id_of(name: str) -> int:
    workspace = await DBWorkspace.get(name=name)
    return workspace.id


async def ids_by_group(group: str) -> List[int]:
    return sorted([await workspace_id_of(name) for name, _keys, expected in WORKSPACE_LAYOUT if expected == group])


def make_user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    """构造一个未落库的登录身份

    作用域判定只读 `tenant_id` / `tenant_role` / `perm_level`，落库与否不影响判定。
    """
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


def rival_admin() -> DBUser:
    return make_user(tenant_id=RIVAL_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def platform_owner() -> DBUser:
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


def single_tenant_admin() -> DBUser:
    """未启用多租户的部署里的普通管理员：无租户归属、角色为平台方"""
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Admin)


UNRESTRICTED_IDENTITIES = [
    pytest.param(platform_owner, id="platform-owner"),
    pytest.param(single_tenant_admin, id="single-tenant"),
]


class TestOwnershipDerivation:
    """归属推导：批量版与网络分组那版必须逐条一致"""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [pytest.param(name, expected, id=name) for name, _keys, expected in WORKSPACE_LAYOUT],
    )
    async def test_single_workspace_group(self, db, name: str, expected: str) -> None:  # noqa: ARG002
        workspace = await DBWorkspace.get(name=name)
        assert await resolve_workspace_network_group(workspace) == expected

    async def test_batch_matches_single(self, db) -> None:  # noqa: ARG002
        """一次算一批与一个一个算必须给出同一张表

        列表接口一页可以有 100 个工作区，逐个推导是 200 次查询，所以批量版是必需的；
        代价是多了一处实现，这条测试就是它们不许分叉的约束。
        """
        workspaces = await DBWorkspace.all().order_by("id")
        expected = {workspace.id: await resolve_workspace_network_group(workspace) for workspace in workspaces}
        assert await resolve_workspace_owner_groups([workspace.id for workspace in workspaces]) == expected

    async def test_batch_covers_every_requested_id(self, db) -> None:  # noqa: ARG002
        """问到的每个 ID 都要有结论，问不到的按平台方兜底

        少一项就会让调用方 `groups.get(id)` 拿到 None，从而把「归属未知」静默当成「不是我的」，
        或者反过来被当成可访问。
        """
        assert await resolve_workspace_owner_groups([]) == {}
        assert await resolve_workspace_owner_groups([MISSING_WORKSPACE_ID]) == {MISSING_WORKSPACE_ID: PLATFORM_GROUP}


class TestListIsFilteredByTenant:
    """列表只返回归属完全落在自己名下的工作区"""

    async def test_tenant_sees_only_own(self, db) -> None:  # noqa: ARG002
        result = await list_workspaces(page=1, page_size=50, _current_user=acme_admin())
        assert sorted(item.id for item in result.items) == await ids_by_group(ACME_GROUP)
        # total 必须跟着过滤：不然「一共有多少个工作区」连同翻页时的空白页一起泄露出去
        assert result.total == len(result.items)

    async def test_other_tenant_sees_only_its_own(self, db) -> None:  # noqa: ARG002
        result = await list_workspaces(page=1, page_size=50, _current_user=rival_admin())
        assert sorted(item.id for item in result.items) == await ids_by_group(RIVAL_GROUP)
        assert result.total == len(result.items)

    async def test_shared_and_platform_never_appear(self, db) -> None:  # noqa: ARG002
        """归属不唯一的与平台方的都不进任何租户的列表"""
        hidden = set(await ids_by_group(SHARED_GROUP)) | set(await ids_by_group(PLATFORM_GROUP))
        for identity in (acme_admin, rival_admin):
            result = await list_workspaces(page=1, page_size=50, _current_user=identity())
            assert hidden.isdisjoint({item.id for item in result.items})

    async def test_tenant_without_instances_sees_nothing(self, db) -> None:  # noqa: ARG002
        orphan = make_user(tenant_id=42, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)
        result = await list_workspaces(page=1, page_size=50, _current_user=orphan)
        assert result.total == 0
        assert result.items == []

    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_unrestricted_identity_sees_all(self, db, identity) -> None:  # noqa: ARG002
        result = await list_workspaces(page=1, page_size=50, _current_user=identity())
        assert result.total == len(WORKSPACE_LAYOUT)
        assert sorted(item.id for item in result.items) == sorted(
            [workspace.id for workspace in await DBWorkspace.all()],
        )

    async def test_search_still_narrows_within_own_scope(self, db) -> None:  # noqa: ARG002
        """检索词不能成为绕过过滤的入口"""
        result = await list_workspaces(page=1, page_size=50, search="rival", _current_user=acme_admin())
        assert result.total == 0
        assert result.items == []

    async def test_accessible_ids_is_unrestricted_for_platform(self, db) -> None:  # noqa: ARG002
        """平台方拿到的是 None（不受限），不是空列表（什么都看不到）"""
        assert await accessible_workspace_ids(platform_owner()) is None
        assert await accessible_workspace_ids(single_tenant_admin()) is None


class TestSingleResourceDeniesWithNotFound:
    """单资源越权一律 404，且与「不存在」不可区分"""

    @staticmethod
    async def denied_ids() -> List[int]:
        return [
            *await ids_by_group(RIVAL_GROUP),
            *await ids_by_group(SHARED_GROUP),
            *await ids_by_group(PLATFORM_GROUP),
            MISSING_WORKSPACE_ID,
        ]

    async def test_guard_rejects_every_foreign_workspace(self, db) -> None:  # noqa: ARG002
        for workspace_id in await self.denied_ids():
            with pytest.raises(NotFoundError):
                await assert_workspace_accessible(acme_admin(), workspace_id)

    async def test_guard_accepts_own_workspaces(self, db) -> None:  # noqa: ARG002
        for workspace_id in await ids_by_group(ACME_GROUP):
            workspace = await assert_workspace_accessible(acme_admin(), workspace_id)
            assert workspace.id == workspace_id

    async def test_denial_is_indistinguishable_from_absence(self, db) -> None:  # noqa: ARG002
        """存在的别人家工作区与压根不存在的，对外必须是同一个错误、同一段文案

        任何把两者区分开的实现（403 vs 404，或文案不同）都让租户能枚举出平台方有多少个工作区。
        """
        assert NotFoundError.http_status == 404

        foreign_id = (await ids_by_group(RIVAL_GROUP))[0]
        messages: List[str] = []
        for workspace_id in (foreign_id, MISSING_WORKSPACE_ID):
            with pytest.raises(NotFoundError) as exc_info:
                await assert_workspace_accessible(acme_admin(), workspace_id)
            messages.append(exc_info.value.get_message(SupportedLang.ZH_CN))
        assert messages == [f"工作区 {foreign_id}不存在", f"工作区 {MISSING_WORKSPACE_ID}不存在"]

    @pytest.mark.parametrize(
        "endpoint",
        [
            pytest.param(get_workspace, id="detail"),
            pytest.param(get_bound_channels, id="bound-channels"),
            pytest.param(get_workspace_skills, id="skills"),
            pytest.param(get_workspace_cc_preset, id="cc-model-preset"),
        ],
    )
    async def test_endpoints_reject_foreign_workspace(self, db, endpoint) -> None:  # noqa: ARG002
        for workspace_id in await self.denied_ids():
            with pytest.raises(NotFoundError):
                await endpoint(workspace_id=workspace_id, _current_user=acme_admin())

    async def test_memory_endpoints_share_the_same_guard(self, db) -> None:  # noqa: ARG002
        """记忆族的 9 个端点只经过 `_get_memory_target` 拿目标，守卫在那里统一生效

        它们此前连 `@require_role` 都没有，是这一族里最容易被漏掉的一批。
        """
        for workspace_id in await self.denied_ids():
            with pytest.raises(NotFoundError):
                await get_memory_detail(
                    workspace_id=workspace_id,
                    memory_type="paragraph",
                    memory_id=1,
                    _current_user=acme_admin(),
                )

    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_unrestricted_identity_reaches_every_workspace(self, db, identity) -> None:  # noqa: ARG002
        for workspace in await DBWorkspace.all():
            assert (await assert_workspace_accessible(identity(), workspace.id)).id == workspace.id

    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_unrestricted_identity_still_404s_on_absent(self, db, identity) -> None:  # noqa: ARG002
        with pytest.raises(NotFoundError):
            await assert_workspace_accessible(identity(), MISSING_WORKSPACE_ID)


class TestTenantCanStillManageOwnWorkspace:
    """别把隔离做过头：自己名下的工作区功能必须完好"""

    async def test_read_own_detail_and_channels(self, db) -> None:  # noqa: ARG002
        workspace_id = await workspace_id_of("acme-single")
        detail = await get_workspace(workspace_id=workspace_id, _current_user=acme_admin())
        assert detail.id == workspace_id
        bound = await get_bound_channels(workspace_id=workspace_id, _current_user=acme_admin())
        assert [channel.chat_key for channel in bound.channels] == ["onebot_v11-inst1-group_00"]

    async def test_update_own_name_and_description(self, db) -> None:  # noqa: ARG002
        workspace_id = await workspace_id_of("acme-single")
        detail = await update_workspace(
            workspace_id=workspace_id,
            body=WorkspaceUpdate(description="renamed by tenant"),
            _current_user=acme_admin(),
        )
        assert detail.description == "renamed by tenant"

    async def test_bind_and_unbind_own_channel(self, db) -> None:  # noqa: ARG002
        workspace_id = await workspace_id_of("acme-single")
        target = await DBChatChannel.create(
            adapter_key="onebot_v11",
            instance_key="inst2",
            channel_id="group_free",
            chat_key="onebot_v11-inst2-group_free",
            channel_type="group",
            data="{}",
        )
        await bind_channel(
            workspace_id=workspace_id,
            body=ChannelBindRequest(chat_key=target.chat_key),
            _current_user=acme_admin(),
        )
        await target.refresh_from_db()
        assert target.workspace_id == workspace_id

        await unbind_channel(workspace_id=workspace_id, chat_key=target.chat_key, _current_user=acme_admin())
        await target.refresh_from_db()
        assert target.workspace_id is None


class TestCrossTenantWritesAreBlocked:
    """越权不只是「读到别人的」，还有几条把别人的东西拽进自己作用域的路径"""

    async def test_cannot_bind_foreign_channel(self, db) -> None:  # noqa: ARG002
        """把别人的频道绑进自己的工作区 = 把那个频道的 CC 委托引到自己看得见的地方

        顺带这个工作区的归属会变成 `shared`，绑定方自己反倒失去访问权 —— 两头都不该发生。
        """
        workspace_id = await workspace_id_of("acme-single")
        foreign = await DBChatChannel.get(workspace_id=await workspace_id_of("rival-single"))
        with pytest.raises(NotFoundError):
            await bind_channel(
                workspace_id=workspace_id,
                body=ChannelBindRequest(chat_key=foreign.chat_key),
                _current_user=acme_admin(),
            )
        await foreign.refresh_from_db()
        assert foreign.workspace_id == await workspace_id_of("rival-single")

    async def test_cannot_unbind_foreign_channel(self, db) -> None:  # noqa: ARG002
        workspace_id = await workspace_id_of("acme-single")
        foreign = await DBChatChannel.get(workspace_id=await workspace_id_of("rival-single"))
        with pytest.raises(NotFoundError):
            await unbind_channel(
                workspace_id=workspace_id,
                chat_key=foreign.chat_key,
                _current_user=acme_admin(),
            )
        await foreign.refresh_from_db()
        assert foreign.workspace_id is not None

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param("sandbox_image", "attacker/evil", id="image"),
            pytest.param("sandbox_version", "malicious-tag", id="version"),
        ],
    )
    async def test_tenant_cannot_change_sandbox_image(self, db, field: str, value: str) -> None:  # noqa: ARG002
        """镜像可改 + 镜像可拉 + 容器可启 = 在宿主上跑任意镜像

        后两步已经提到 `Role.Super`，第一步留在这里因为 PATCH 整体仍是租户可用的。
        """
        workspace_id = await workspace_id_of("acme-single")
        with pytest.raises(PermissionDeniedError):
            await update_workspace(
                workspace_id=workspace_id,
                body=WorkspaceUpdate(**{field: value}),
                _current_user=acme_admin(),
            )
        workspace = await DBWorkspace.get(id=workspace_id)
        assert getattr(workspace, field) != value

    async def test_unchanged_image_round_trip_is_accepted(self, db) -> None:  # noqa: ARG002
        """配置页每次保存都会把当前镜像值一并回传，见到字段就拒会把普通保存一起打掉

        「当前值」有两种形态：列里存的空串，以及读接口回给前端的那个全局默认
        （`_summary` 会把空串换成 `CC_SANDBOX_IMAGE`）。只认前者的话，从没显式设过镜像的
        工作区会把前端原样回传的默认值判成一次修改 —— 实测里就是「租户改个描述就 403」。
        """
        from nekro_agent.core.config import config as app_config

        workspace_id = await workspace_id_of("acme-single")
        workspace = await DBWorkspace.get(id=workspace_id)
        assert workspace.sandbox_image == ""
        for image, version in (
            (workspace.sandbox_image, workspace.sandbox_version),
            (app_config.CC_SANDBOX_IMAGE, app_config.CC_SANDBOX_IMAGE_TAG),
        ):
            detail = await update_workspace(
                workspace_id=workspace_id,
                body=WorkspaceUpdate(description="kept", sandbox_image=image, sandbox_version=version),
                _current_user=acme_admin(),
            )
            assert detail.description == "kept"
        assert (await DBWorkspace.get(id=workspace_id)).sandbox_image == ""

    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_unrestricted_identity_may_change_image(self, db, identity) -> None:  # noqa: ARG002
        workspace_id = await workspace_id_of("acme-single")
        detail = await update_workspace(
            workspace_id=workspace_id,
            body=WorkspaceUpdate(sandbox_image="platform/custom"),
            _current_user=identity(),
        )
        assert detail.sandbox_image == "platform/custom"

    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_saving_the_current_image_does_not_pin_the_workspace(self, db, identity) -> None:  # noqa: ARG002
        """配置页保存会回传当前生效值；平台方照写就会把工作区钉死在当时的默认镜像上，
        以后平台方调整默认镜像它也不跟着变。提交空串则让单独设置过的工作区重新跟随默认"""
        from nekro_agent.core.config import config as app_config

        workspace_id = await workspace_id_of("acme-single")
        await update_workspace(
            workspace_id=workspace_id,
            body=WorkspaceUpdate(
                description="kept",
                sandbox_image=app_config.CC_SANDBOX_IMAGE,
                sandbox_version=app_config.CC_SANDBOX_IMAGE_TAG,
            ),
            _current_user=identity(),
        )
        workspace = await DBWorkspace.get(id=workspace_id)
        assert (workspace.description, workspace.sandbox_image, workspace.sandbox_version) == ("kept", "", "latest")

        await update_workspace(
            workspace_id=workspace_id,
            body=WorkspaceUpdate(sandbox_image="platform/custom", sandbox_version="v1"),
            _current_user=identity(),
        )
        await update_workspace(
            workspace_id=workspace_id,
            body=WorkspaceUpdate(sandbox_image="", sandbox_version=""),
            _current_user=identity(),
        )
        workspace = await DBWorkspace.get(id=workspace_id)
        assert (workspace.sandbox_image, workspace.sandbox_version) == ("", "")


class TestEffectiveSandboxImage:
    """建容器、检查 / 拉取镜像、读接口展示都走 `effective_sandbox_image`"""

    async def test_the_column_default_latest_follows_the_global_tag(self, db) -> None:  # noqa: ARG002
        """`sandbox_version` 列默认值是 latest，意思是「跟着全局默认」：默认镜像从不发 latest，
        照字面用会让所有没单独设过版本的工作区去拉一个不存在的标签"""
        from nekro_agent.core.config import config as app_config
        from nekro_agent.services.workspace.container import effective_sandbox_image

        workspace = await DBWorkspace.get(id=await workspace_id_of("acme-single"))
        assert (workspace.sandbox_image, workspace.sandbox_version) == ("", "latest")
        assert effective_sandbox_image(workspace) == (app_config.CC_SANDBOX_IMAGE, app_config.CC_SANDBOX_IMAGE_TAG)

        workspace.sandbox_version = ""
        assert effective_sandbox_image(workspace) == (app_config.CC_SANDBOX_IMAGE, app_config.CC_SANDBOX_IMAGE_TAG)
        # 平台方单独钉过的具体标签照用
        workspace.sandbox_version = "0.1.13"
        assert effective_sandbox_image(workspace) == (app_config.CC_SANDBOX_IMAGE, "0.1.13")
        # 单独设了镜像名的工作区，标签照它自己写的，latest 也照字面
        workspace.sandbox_image = "platform/custom"
        workspace.sandbox_version = "latest"
        assert effective_sandbox_image(workspace) == ("platform/custom", "latest")
        workspace.sandbox_version = ""
        assert effective_sandbox_image(workspace) == ("platform/custom", app_config.CC_SANDBOX_IMAGE_TAG)

    def test_the_default_image_is_pinned_and_never_latest(self) -> None:
        """默认镜像的标签必须是固定不变的具体版本：自建镜像不发 latest"""
        from nekro_agent.core.config import CoreConfig

        fields = CoreConfig.model_fields
        assert fields["CC_SANDBOX_IMAGE"].default == "ghcr.io/akiyo-dayo/nekro-cc-sandbox"
        assert fields["CC_SANDBOX_IMAGE_TAG"].default not in ("", "latest")


class TestPlatformCredentialsStayHidden:
    """租户看不到平台方的模型凭据，这是定制版运营模式的前提"""

    async def test_cc_preset_config_is_withheld_from_tenant(self, db, monkeypatch) -> None:  # noqa: ARG002
        from nekro_agent.core import cc_model_presets

        preset = cc_model_presets.CCModelPresetItem(
            id=7,
            name="platform-default",
            is_default=True,
            base_url="https://api.example.invalid",
            auth_token="sk-platform-secret",
        )
        monkeypatch.setattr(cc_model_presets.cc_presets_store, "get_default", lambda: preset)

        workspace_id = await workspace_id_of("acme-single")
        tenant_view = await get_workspace_cc_preset(workspace_id=workspace_id, _current_user=acme_admin())
        assert tenant_view == {"preset_id": 7, "config_json": None}

        owner_view = await get_workspace_cc_preset(workspace_id=workspace_id, _current_user=platform_owner())
        assert owner_view["config_json"]["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-platform-secret"


def _min_role_of(endpoint: Any) -> Optional[Role]:
    """读出 `require_role` 装饰器捕获的等级

    走闭包而不是扫源码：装饰器改写法时这里会直接失效，而不是安静地永远返回 None。
    """
    try:
        return inspect.getclosurevars(endpoint).nonlocals.get("min_role")
    except TypeError:
        return None


def _routes() -> Sequence[Any]:
    return [route for route in workspaces_router.routes if getattr(route, "endpoint", None) is not None]


# 平台方专属端点。判据三条：动宿主资源（容器生命周期、镜像仓库、宿主上的 docker exec）、
# 写平台级共享库（全局技能库、全局 MCP 库的验证状态），或者建出来根本没有租户归属的东西。
PLATFORM_ONLY_ENDPOINTS = {
    "create_workspace",
    "delete_workspace",
    "start_sandbox",
    "stop_sandbox",
    "restart_sandbox",
    "rebuild_sandbox",
    "check_sandbox_image",
    "pull_sandbox_image_stream",
    # `promote_dynamic_skill` 曾在这里，判据是"写平台级共享库（全局技能库）"。
    # 技能库有了归属之后（`services/skill_scope.py`）这条判据不再成立：
    # 晋升的落点是 `writable_root(调用方作用域)`，租户写进的是**自己那一层库**，
    # 与它用 `PUT /skills/file` 写出来的东西同一个目录、同一个可达范围。
    # 现在它是 `Role.Admin` + `assert_workspace_accessible`，因此落回上面那条通用要求里。
    "test_mcp_server",
    "workspace_terminal",
}


class TestNoEndpointEscapesTheGuard:
    """这次事故的形态是「72 个端点一路加下来，没有一个带作用域」

    所以约束不能只钉在今天这批端点上：路由表里每个吃 `workspace_id` 的端点，
    都必须能追溯到那唯一一处守卫，新加的也一样。
    """

    def test_every_workspace_scoped_endpoint_calls_the_guard(self) -> None:
        """要么走守卫，要么是平台方专属

        `Role.Super` 之下不需要归属判定：租户成员的等级被 `TENANT_MAX_PERM_LEVEL` 钉死在
        `Role.Admin`，一个租户身份都到不了那里。哪些端点算平台方专属由下面那条测试单独钉住，
        所以这里放行 Super 不会变成一个可以随手扩大的口子。
        """
        missing: List[str] = []
        for route in _routes():
            if "{workspace_id}" not in route.path:
                continue
            if route.endpoint.__name__ in PLATFORM_ONLY_ENDPOINTS:
                continue
            source = inspect.getsource(inspect.unwrap(route.endpoint))
            # `_get_memory_target` 是记忆族的统一入口，它内部第一句就是守卫
            if "assert_workspace_accessible" not in source and "_get_memory_target" not in source:
                missing.append(route.endpoint.__name__)
        assert missing == []

    def test_list_and_create_are_handled_explicitly(self) -> None:
        """两个不吃 `workspace_id` 的端点各有各的处置，不能靠上面那条覆盖"""
        by_name = {route.endpoint.__name__: route for route in _routes()}
        assert "accessible_workspace_ids" in inspect.getsource(inspect.unwrap(by_name["list_workspaces"].endpoint))
        assert _min_role_of(by_name["create_workspace"].endpoint) is Role.Super

    def test_platform_only_endpoints_require_super(self) -> None:
        by_name = {route.endpoint.__name__: route for route in _routes()}
        # 终端是 WebSocket，装饰器在这里用不了（它抛的是 HTTPException），校验写在函数体里
        assert "Role.Super" in inspect.getsource(inspect.unwrap(by_name["workspace_terminal"].endpoint))
        for name in PLATFORM_ONLY_ENDPOINTS - {"workspace_terminal"}:
            assert _min_role_of(by_name[name].endpoint) is Role.Super, name

    def test_every_other_endpoint_requires_at_least_admin(self) -> None:
        """记忆族此前连角色都没有：任何登录用户（含只读的租户普通用户）都能清空记忆库"""
        levels: Dict[str, Optional[Role]] = {}
        for route in _routes():
            name = route.endpoint.__name__
            if name in PLATFORM_ONLY_ENDPOINTS or name == "workspace_terminal":
                continue
            levels[name] = _min_role_of(route.endpoint)
        assert [name for name, level in levels.items() if level is not Role.Admin] == []
