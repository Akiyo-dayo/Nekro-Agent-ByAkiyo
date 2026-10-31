"""租户够得到的那几个入口：**四份清单**必须同时对得上

`tests/test_console_platform_only_paths.py` 守的是反面（标成平台方专属的页面两处都要登记）。
这一份守的是正面：**后端已经为租户建好并测过的能力，前端要真的可达**。

背景（round-19 前的实测）：

| 能力 | 后端 | 前端 |
|---|---|---|
| 实例级配置覆盖（R4 / §3 #15） | `PUT /config/instance/{adapter}/{instance}`，接 `assert_instance_accessible` + `_assert_tenant_may_write` | 唯一调用点 `pages/settings/system.tsx` 关在 `PLATFORM_ONLY_PATHS` 里 → **0 可达** |
| 租户自管成员 | `POST/DELETE /console/tenants/{id}/members` 用 `require_tenant_admin()` | 只挂在 `/console/tenants` 下，整条路径平台方专属 → **0 可达** |

两条后端分支都有用例守着，但用例守的是一段产品上永远跑不到的代码。本文件把
「前端确实开了这两个口」钉成判据，免得下一次"顺手收紧一下 platformOnly 清单"
把它们又关回去而没有任何信号。

判据按**前端源码文本**做：这个仓库没有前端测试设施，Python 侧解析源码是既有做法
（见 `tests/test_console_platform_only_paths.py` 与 `tests/test_upload_signed_url.py`）。
每一条断言都配一个「把那一行改掉之后它必须变红」的对照组（§2.11）。
"""

import re
from pathlib import Path
from typing import List, Set

import pytest

FRONTEND = Path("frontend/src")
NAVIGATION = FRONTEND / "config/navigation.tsx"
MAIN_LAYOUT = FRONTEND / "layouts/MainLayout.tsx"
ROUTER = FRONTEND / "router/index.tsx"
SETTINGS_PAGE = FRONTEND / "pages/settings/system.tsx"
TEAM_PAGE = FRONTEND / "pages/console/TenantTeamPage.tsx"
MEMBER_TABLE = FRONTEND / "pages/console/components/TenantMemberTable.tsx"


def _read(path: Path) -> str:
    assert path.exists(), f"{path} 不存在，判据的落点没了"
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def navigation_source() -> str:
    return _read(NAVIGATION)


def _platform_only_paths(source: str) -> List[str]:
    match = re.search(r"export const PLATFORM_ONLY_PATHS = \[(.*?)\] as const", source, re.S)
    assert match, "没找到 PLATFORM_ONLY_PATHS 数组——它被改名或删掉了"
    return re.findall(r"'([^']+)'", match.group(1))


def _covered(paths: List[str], candidate: str) -> bool:
    return any(candidate == prefix or candidate.startswith(f"{prefix}/") for prefix in paths)


def _menu_entry(source: str, path: str) -> str:
    """取出 `getPageConfigs()` 里 `path: '<path>'` 那一格的源码块

    菜单项是没有嵌套大括号的扁平对象字面量，按最内层 `{...}` 切分即可。
    """
    for block in re.findall(r"\{([^{}]*?)\}", source, re.S):
        if re.search(rf"path: '{re.escape(path)}',", block):
            return block
    raise AssertionError(f"菜单里没有 path: '{path}' 这一格")


def _visibility_of(block: str) -> str:
    match = re.search(r"visibility: '([a-zA-Z]+)'", block)
    return match.group(1) if match else ""


class TestInstanceConfigIsReachableByTenants:
    """A1：实例级配置覆盖页对租户开着"""

    def test_settings_system_is_not_url_blocked(self, navigation_source: str) -> None:
        """兜底拦截清单里不能有它 —— 有它就等于 R4 那条链路整条封死"""
        paths = _platform_only_paths(navigation_source)
        assert not _covered(paths, "/settings/system"), (
            "/settings/system 又被登记进 PLATFORM_ONLY_PATHS 了。"
            "它是四层配置里实例覆盖层在整个前端的唯一写入口，关掉它，"
            "租户换模型组只能回到 `/chat-channel` 逐个频道点（R4 要消灭的正是这件事）"
        )

    def test_settings_system_menu_entry_is_not_platform_only(self, navigation_source: str) -> None:
        """菜单那一格不能标 `platformOnly`，否则租户看得到 URL、看不到入口

        它**有**可见性开关，但那个开关分的是「租户管理员 / 只读成员」这一档
        （见 `TestReadOnlyTenantMembersAreNotSentToADeadPage`），不是「平台方 / 租户」。
        """
        assert _visibility_of(_menu_entry(navigation_source, "/settings/system")) != "platformOnly"

    def test_the_criterion_can_see_a_reblocked_entry(self, navigation_source: str) -> None:
        """§2.11 对照组：把它加回 `PLATFORM_ONLY_PATHS`，上面第一条必须变红"""
        broken = navigation_source.replace(
            "  '/settings/deployment',\n",
            "  '/settings/system',\n  '/settings/deployment',\n",
            1,
        )
        assert broken != navigation_source, "样本没被改动，这条对照组什么都没验"
        assert _covered(_platform_only_paths(broken), "/settings/system")

    def test_the_quota_page_is_the_shape_being_copied(self, navigation_source: str) -> None:
        """对照组的另一半：`/console/quota` 一直是这个形态（URL 不拦、不标 platformOnly）"""
        assert not _covered(_platform_only_paths(navigation_source), "/console/quota")
        assert _visibility_of(_menu_entry(navigation_source, "/console/quota")) != "platformOnly"

    def test_the_visibility_parser_can_still_see_a_platform_only_entry(
        self,
        navigation_source: str,
    ) -> None:
        """§2.11 对照组：证明 `_visibility_of` 不是恒返回空串

        上面两条都是 `!= 'platformOnly'`，一个永远解析不出 visibility 的解析器
        能让它们无条件通过。拿一格真的标了 `platformOnly` 的来验它看得见。
        """
        assert _visibility_of(_menu_entry(navigation_source, "/settings/models")) == "platformOnly"

    def test_instance_scope_renders_as_an_override_page(self) -> None:
        """实例作用域必须按覆盖页渲染

        实例级列表返回的是成对的 `enable_X` / `X`。不传 `isOverridePage` 的话：
        覆盖开关那一列根本不画，`enable_X` 也不会随保存一起提交 ——
        用户改完按保存，值落进覆盖文件而开关仍是 false，**四层解析根本不会用它**。
        """
        source = _read(SETTINGS_PAGE)
        assert "const isOverrideScope = Boolean(currentInstanceKey)" in source
        assert "isOverridePage={isOverrideScope}" in source

    def test_override_toggles_are_filed_under_the_item_they_control(self) -> None:
        """`enable_X` 必须和 `X` 落在同一个分类页签里

        `ConfigTable` 的开关状态是从传进去的 `configs` 里算的（`enableStateMap`）。
        本页按分类切分后逐页传，`enable_X` 自己没有分类信息，不做配对就会全落进
        「其他」——于是每个开关都显示为关、点了也不亮，因为它压根不在这一页的 `configs` 里。
        """
        source = _read(SETTINGS_PAGE)
        assert "toggleOwnerCategory" in source
        assert "toggleOwnerCategory.set(config.enable_toggle, resolveCategory(config))" in source
        assert "toggleOwnerCategory.get(config.key) ?? resolveCategory(config)" in source

    def test_tenant_never_falls_back_to_the_global_config_service(self) -> None:
        """租户没有全局视图这一档（§1.4）

        拿不到实例时必须给出 `null`，而不是回落 `createConfigService('system')`：
        后者会把全局配置表摆到租户面前 —— 读得到（后端读侧对租户放行并脱敏），
        点保存却必然 403，是一页专门用来骗人点的表。
        """
        source = _read(SETTINGS_PAGE)
        tenant_branch = re.search(
            r"if \(isTenantScoped\) \{\s*return null\s*\}\s*return createConfigService\('system'\)",
            source,
        )
        assert tenant_branch, "`isTenantScoped → null` 这条分支没了，或跑到了全局回落之后"
        assert "enabled: configService !== null" in source
        assert "tenantWithoutInstanceScope" in source

    def test_the_oobe_button_is_not_offered_to_tenants(self) -> None:
        """OOBE 是部署级向导（写全局配置，且 `/oobe` 在 `MainLayout` 之外、兜不到）"""
        source = _read(SETTINGS_PAGE)
        assert "activeTab === otherCategoryLabel && !isTenantScoped" in source


class TestReadOnlyTenantMembersAreNotSentToADeadPage:
    """A1 的另一半：「租户」不是一个身份，是两个

    `/settings/system` 对**租户管理员**是要开的（上面那一组），但对**只读租户成员**
    （`tenant_user` → `Role.User`）整页零可用 —— 它要打的两条请求都带
    `@require_role(Role.Admin)`：

    | 请求 | 守卫 | `tenant_user` |
    |---|---|---|
    | `GET /console/instance-options`（`useInstanceScope` 的实例列表） | `@require_role(Role.Admin)` | 403 |
    | `GET /config/instance/{adapter}/{instance}`（本页的配置列表） | `@require_role(Role.Admin)` | 403 |

    测试部署上用真的 `tenant_user` 账号实测过这两条，并真渲染确认过后果：
    实例列表被 403 挡掉 → `instances` 恒为空 → 页面弹出
    **「当前没有可管理的实例 / 名下暂时没有实例，请联系服务提供方」**，
    而同一时刻 `/user/me/identity` 返回的是 `accessible_instance_keys: ["inst1"]`。
    这与本轮自己修掉的「把越权 404 说成接口没上线」是同一个错误，只是换了个身份。
    """

    def test_the_menu_entry_is_gated_on_the_new_visibility_key(self, navigation_source: str) -> None:
        assert (
            _visibility_of(_menu_entry(navigation_source, "/settings/system")) == "instanceConfigOverrides"
        ), "只读租户成员会重新看到一格点进去只有 403 的入口"

    def test_the_visibility_key_is_declared_and_defaulted(self, navigation_source: str) -> None:
        """未启用多租户的部署必须逐字不变，所以默认值只能是 true"""
        assert "instanceConfigOverrides: boolean" in navigation_source
        assert "instanceConfigOverrides: true" in navigation_source

    def test_the_visibility_key_is_actually_computed(self) -> None:
        """§2.7 接线：`MainLayout` 不算它 → `visibility[key]` 是 undefined → 对**所有人**隐藏

        这一格与「我的团队」那一格的差别在于失效方向：那一格不算就是谁都看不到（明显），
        这一格不算会把平台方的系统配置也一并藏掉（更明显），但两者 tsc / eslint / build 都全绿。
        """
        layout = _read(MAIN_LAYOUT)
        assert "instanceConfigOverrides: !tenancy.isReadOnly" in layout
        # 依赖数组要单独取出来比：整段 useMemo 的正文里本来就含 `tenancy.isReadOnly`
        # （就是上面那一行），拿正文做判据等于恒真——第一版就是这么写的，变异实测存活。
        deps = re.search(r"\[tenancy\.[^\]]*\]\s*\)\n", layout)
        assert deps, "没找到 menuVisibility 那个 useMemo 的依赖数组"
        assert "tenancy.isReadOnly" in deps.group(0), (
            "`isReadOnly` 没进 useMemo 的依赖数组：身份从「加载中」变成「只读」时菜单不重算，"
            "只读成员在那一帧之后仍然看得到这一格"
        )

    def test_the_wiring_criterion_can_see_a_missing_assignment(self) -> None:
        """§2.11 对照组：删掉那一行，上面那条必须变红"""
        layout = _read(MAIN_LAYOUT)
        broken = layout.replace("      instanceConfigOverrides: !tenancy.isReadOnly,\n", "", 1)
        assert broken != layout, "样本没被改动，这条对照组什么都没验"
        assert "instanceConfigOverrides: !tenancy.isReadOnly" not in broken

    def test_the_page_itself_also_stops_a_read_only_member(self) -> None:
        """菜单只管画不画；直接敲 URL 的那条路由由页面自己兜（与 `PLATFORM_ONLY_PATHS` 同一条理由）"""
        source = _read(SETTINGS_PAGE)
        assert "const tenantCannotReadInstanceConfig = isTenantScoped && isReadOnly" in source
        assert "if (tenantCannotReadInstanceConfig) {" in source
        assert "tenantScope.readOnlyTitle" in source

    def test_the_read_only_branch_runs_before_the_no_instance_branch(self) -> None:
        """顺序就是判据本身

        `tenantWithoutInstanceScope` 排在前面的话，只读成员照旧拿到那句假话 ——
        两个分支的**触发条件在只读身份上同时成立**。
        """
        source = _read(SETTINGS_PAGE)
        read_only_at = source.index("if (tenantCannotReadInstanceConfig) {")
        no_instance_at = source.index("if (tenantWithoutInstanceScope) {")
        assert read_only_at < no_instance_at

    def test_no_instance_is_distinguished_from_failed_list(self) -> None:
        """「名下没有实例」与「实例列表没取到」不能共用一句文案

        判据取 `accessible_instance_keys`（身份接口下发，与实例列表是两条独立请求）：
        它非空而实例列表为空，说明是那次请求失败，不是名下真的没有实例。
        """
        source = _read(SETTINGS_PAGE)
        assert "accessibleInstanceKeys.length === 0" in source
        assert "accessibleInstanceKeys.length > 0" in source
        assert "tenantInstanceListUnavailable" in source

    def test_the_retry_button_retries_the_request_that_actually_failed(self) -> None:
        """失败的是实例列表那条查询，不是本页的配置查询

        本页的配置查询此刻压根没发出去（`enabled: configService !== null`），
        对它 `refetch()` 是个什么都不做的按钮 —— 一个「空断言」的界面版。
        """
        source = _read(SETTINGS_PAGE)
        assert "INSTANCE_SCOPE_QUERY_KEY" in source
        assert "invalidateQueries({ queryKey: INSTANCE_SCOPE_QUERY_KEY })" in source

    def test_both_locales_carry_the_new_strings(self) -> None:
        """缺一个键就是把 `tenantScope.readOnlyTitle` 原样显示给用户看"""
        import json

        for locale in ("zh-CN", "en-US"):
            data = json.loads(
                _read(FRONTEND / f"locales/{locale}/instance-scope.json"),
            )
            scope = data["tenantScope"]
            for key in ("readOnlyTitle", "readOnlyHint", "listFailedTitle", "listFailedHint"):
                assert scope.get(key), f"{locale}/instance-scope.json 缺 tenantScope.{key}"


class TestTenantTeamPageIsWiredEndToEnd:
    """A3：租户自管成员的入口三份清单齐全"""

    def test_menu_entry_exists_and_is_tenant_only(self, navigation_source: str) -> None:
        assert _visibility_of(_menu_entry(navigation_source, "/console/team")) == "tenantSelfService"

    def test_menu_entry_is_not_url_blocked(self, navigation_source: str) -> None:
        """画了一格却被兜底拦截 = 点了就被踢走"""
        assert not _covered(_platform_only_paths(navigation_source), "/console/team")

    def test_the_route_and_the_page_behind_it_exist(self) -> None:
        """第三份清单：`router/index.tsx`。菜单画了、清单也对，路由没有就是一页空白"""
        routes = _read(ROUTER)
        assert "path: 'team'," in routes
        assert "TenantTeamPage" in routes
        assert TEAM_PAGE.exists()

    def test_the_visibility_key_is_actually_computed(self) -> None:
        """第四份清单：`MainLayout` 必须给出这个开关的取值

        `MenuVisibility` 里声明了一个键、`MainLayout` 没算它的话，
        `visibility[entry.visibility]` 取到 `undefined` —— falsy ——
        于是这一格对**任何身份**都不出现，而 tsc / eslint / build 三道全绿。
        这就是 §2.7「零件全对、接线没了」的同一个形态。
        """
        nav = _read(NAVIGATION)
        layout = _read(MAIN_LAYOUT)
        assert "tenantSelfService: boolean" in nav, "`MenuVisibility` 没声明这个键"
        assert "tenantSelfService: false" in nav, "`DEFAULT_MENU_VISIBILITY` 缺这个键（未启用多租户时的取值）"
        assert "tenantSelfService: tenancy.isTenantScoped" in layout, "`MainLayout` 没给这个开关赋值"

    def test_the_wiring_criterion_can_see_a_missing_assignment(self) -> None:
        """§2.11 对照组：把 `MainLayout` 里那一行删掉，上面那条必须变红"""
        layout = _read(MAIN_LAYOUT)
        broken = layout.replace("      tenantSelfService: tenancy.isTenantScoped,\n", "", 1)
        assert broken != layout, "样本没被改动，这条对照组什么都没验"
        assert "tenantSelfService: tenancy.isTenantScoped" not in broken

    def test_the_page_scopes_itself_by_identity_not_by_url(self) -> None:
        """作用域取自身份，不取 URL

        本页刻意没有 `:tenantId` 路径段：租户连"把别人的租户号敲进地址栏"这个动作
        都做不出来。后端 `assert_tenant_accessible` 仍是最终判据（越权 404）。
        """
        source = _read(TEAM_PAGE)
        assert "useIdentity()" in source
        assert "useParams" not in source, "本页一旦从 URL 取 tenantId，就又多出一条要防的越权路径"

    def test_the_page_offers_no_platform_only_write(self) -> None:
        """平台方专属的写操作一个都不能出现在这一页上

        `POST /tenants`（建租户）、`PATCH /tenants/{id}`（改客户资料 / 停用）、
        `PUT /tenants/{id}/instances`（划归实例）全是 `require_platform_owner()`，
        摆出来就是三个必然 403 的按钮。
        """
        source = _read(TEAM_PAGE)
        for forbidden in ("createTenant", "updateTenant", "archiveTenant", "bindInstances", "TenantInstanceBinder"):
            assert forbidden not in source, f"「我的团队」页出现了平台方专属能力 {forbidden}"

    def test_member_table_hides_what_the_backend_will_refuse(self) -> None:
        """两条「后端会拒绝、按钮就不该摆」的判定

        - 只读身份（`tenant_user`）：`require_tenant_admin()` 直接 403；
        - 移除自己：`remove_tenant_member` 明确「不能移除自己」→ 400。
          租户自助场景下成员表第一行往往就是登录者本人，这一条天天会碰到。
        """
        source = _read(MEMBER_TABLE)
        assert "const canManageMembers = !isReadOnly" in source
        assert "{canManageMembers && (" in source, "添加成员按钮没有按只读身份收敛"
        assert "member.user_id === currentUserId" in source, "移除自己那一条没有收敛"

    def test_assignable_roles_still_stop_at_tenant_admin(self) -> None:
        """`TENANT_MAX_PERM_LEVEL = Role.Admin`：角色下拉里不能出现平台方

        后端 `create_tenant_member` 对 `platform_owner` 直接 400，
        但入口开给租户之后，这个下拉是租户唯一碰得到的角色选择器。
        """
        source = _read(MEMBER_TABLE)
        match = re.search(r"const ASSIGNABLE_ROLES: TenantRole\[\] = \[(.*?)\]", source, re.S)
        assert match, "没找到 ASSIGNABLE_ROLES"
        roles: Set[str] = set(re.findall(r"'([a-z_]+)'", match.group(1)))
        assert roles == {"tenant_admin", "tenant_user"}
