"""只读租户成员（`tenant_user`）的后台不能是「坏的」

## 修的是什么

盘点实测：`tenant_user` 打开总后台首页看到的是 **「实例总数 0」**，实例列表看到的是
**「没有符合条件的实例，调整搜索词或状态筛选后再试」**，机器人公告页看到的是一个空的
频道选择器加一个「发送公告」按钮。这三句全是编的 —— 真实情况是
`GET /console/overview`、`GET /console/instances`、`routers/chat_channel.py` 全族都带
`@require_role(Role.Admin)`，而 `tenant_user` 经 `tenant_perm_level()` 映射成 `Role.User`，
一条都拿不到。

**「零」是一个有效答案，用户会当真。** 定制版客户拉第二个人进来看数据，看到的是
「你什么都没有」而不是「你无权查看」，第一周就会以为系统坏了并开工单。
这不是权限设计问题，是**把 403 渲染成 0** 的 bug。

## 本文件钉四件事

1. **后端事实**：登记进 `TENANT_ADMIN_ONLY_PATHS` 的那些入口，后端确实最低 `Role.Admin`；
   而刻意留给只读成员的 `/console/team` 与 `/dashboard` 确实不是。清单不是拍脑袋来的。
2. **两份清单对齐**（与 `tests/test_console_platform_only_paths.py` 同一形态）：
   菜单里标了 `visibility: 'tenantAdminOnly'` 的格子，必须也被 URL 兜底清单覆盖。
3. **四份清单接线**（与 `tests/test_tenant_reachable_console_entries.py` 同一形态）：
   `MenuVisibility` 声明 + `DEFAULT_MENU_VISIBILITY` 取值 + `MainLayout` 赋值 + useMemo 依赖数组。
   少任何一份，tsc / eslint / build 三道全绿，而菜单对**所有人**消失或对**所有人**出现。
4. **页面自己的 403 分支**：藏菜单只管画不画，直接敲 URL 仍然进得来；而且 403 不是只有
   「只读成员」一条来路。每一页都必须能把「不让看」与「没有」分开说，且 403 分支
   **不摆重试按钮**（点了还是 403 —— 本项目在「点不动的按钮」上栽过两次）。

判据按**前端源码文本**做：这个仓库没有前端测试设施，Python 侧解析源码是既有做法
（见 `tests/test_console_platform_only_paths.py`）。每一条结构性断言都配一个
「把那一行改掉之后它必须变红」的对照组。
"""

import importlib
import inspect
import json
import re
from pathlib import Path
from typing import List, Optional, Set, Tuple

import pytest

from nekro_agent.services.user.role import Role
from nekro_agent.services.user.tenant_perm import TenantRole, tenant_perm_level

FRONTEND = Path("frontend/src")
NAVIGATION = FRONTEND / "config/navigation.tsx"
MAIN_LAYOUT = FRONTEND / "layouts/MainLayout.tsx"
AXIOS = FRONTEND / "services/api/axios.ts"
ERROR_STATE = FRONTEND / "components/common/QueryErrorState.tsx"

CONSOLE_HOME = FRONTEND / "pages/console/index.tsx"
INSTANCE_LIST = FRONTEND / "pages/console/InstanceListPage.tsx"
INSTANCE_DETAIL = FRONTEND / "pages/console/InstanceDetailPage.tsx"
INSTANCE_QUOTA = FRONTEND / "pages/console/InstanceQuotaPage.tsx"
IDENTITY_DOMAIN = FRONTEND / "pages/console/IdentityDomainPage.tsx"
TENANT_LIST = FRONTEND / "pages/console/TenantListPage.tsx"
MEMBER_TABLE = FRONTEND / "pages/console/components/TenantMemberTable.tsx"
WIZARD = FRONTEND / "pages/console/wizard/InstanceWizardPage.tsx"
ANNOUNCEMENT = FRONTEND / "pages/chat-channel/announcement.tsx"


def _read(path: Path) -> str:
    assert path.exists(), f"{path} 不存在，判据的落点没了"
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def navigation_source() -> str:
    return _read(NAVIGATION)


@pytest.fixture(scope="module")
def layout_source() -> str:
    return _read(MAIN_LAYOUT)


# --------------------------------------------------------------------------------------
# 解析工具（与既有两份契约测试保持同一套写法）
# --------------------------------------------------------------------------------------


def _string_array(source: str, name: str) -> List[str]:
    match = re.search(rf"export const {name} = \[(.*?)\] as const", source, re.S)
    assert match, f"没找到 {name} 数组——它被改名或删掉了，兜底拦截可能整条失效"
    return re.findall(r"'([^']+)'", match.group(1))


def _menu_blocks(source: str) -> List[str]:
    """菜单里最内层的那些对象字面量

    菜单项没有嵌套花括号（这是本文件里写死的约定，`navigation.tsx` 的注释也重申过），
    所以按最内层 `{...}` 切分即可。
    """
    return re.findall(r"\{([^{}]*?)\}", source, re.S)


def _pages_with_visibility(source: str, key: str) -> Set[str]:
    """标了 `visibility: '<key>'` 且**自己带 path** 的那些格子"""
    pages: Set[str] = set()
    for block in _menu_blocks(source):
        if f"visibility: '{key}'" not in block:
            continue
        path = re.search(r"path: '([^']+)'", block)
        if path:
            pages.add(path.group(1))
    return pages


def _group_children_with_visibility(source: str, key: str) -> Set[str]:
    """**菜单组**上标了 `visibility: '<key>'` 时，它名下那些子项的 path

    ⚠️ `_pages_with_visibility` 只认「自己带 path」的格子，而菜单组带的是 `key:` ——
    于是 `chatManagement` / `roleplay` / `plugins` / `workspace` 这几组整组藏起来时，
    两份清单的对齐用例**一格都检查不到**。组上标一次、子项不标是本文件允许的写法
    （`navigation.tsx` 明写「组上标一次比逐格标少一份漏登记的机会」），所以对齐判据
    必须自己把组展开。
    """
    children: Set[str] = set()
    for match in re.finditer(rf"key: '(\w+)',[^\[]*?visibility: '{key}',\s*\n\s*children: \[", source):
        tail = source[match.end() :]
        group_key = match.group(1)
        # 子项都带 `parent: '<组名>'`，用它划出这一组的范围，避免正则吃进下一组
        for block in _menu_blocks(tail):
            if f"parent: '{group_key}'" not in block:
                continue
            path = re.search(r"path: '([^']+)'", block)
            if path:
                children.add(path.group(1))
    return children


def _slice_between(source: str, start: str, end: str) -> str:
    """`start` 与 `end` 之间那一段；两个锚点都必须存在且有序"""
    assert start in source, f"锚点缺失：{start!r}"
    assert end in source, f"锚点缺失：{end!r}"
    start_at = source.index(start)
    end_at = source.index(end)
    assert start_at < end_at, f"锚点顺序反了：{start!r} 应排在 {end!r} 之前"
    return source[start_at + len(start) : end_at]


def _api_error_calls(axios_source: str) -> List[List[str]]:
    """拦截器里每一条 `throw new ApiError(...)` 的实参列表（顶层逗号切分）

    括号要**配平**着数，不能按固定窗口取字符：`i18n.t('unauthorized', ...)` 自带一层括号，
    而按窗口取会滑进下一条 throw —— 那样「漏传参数」也测不出来（见下方对照组）。
    """
    handler = axios_source[axios_source.index("const { status, data } = error.response") :]
    calls: List[List[str]] = []
    for match in re.finditer(r"throw new ApiError\(", handler):
        depth = 0
        start = match.end()
        for index in range(start - 1, len(handler)):
            char = handler[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
        else:  # pragma: no cover - 源码括号不配平时才会到这里
            raise AssertionError("`new ApiError(` 的括号没有配平，判据无法解析")
        body = handler[start:index]
        args, depth, current = [], 0, ""
        for char in body:
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            if char == "," and depth == 0:
                args.append(current.strip())
                current = ""
                continue
            current += char
        if current.strip():
            args.append(current.strip())
        calls.append(args)
    return calls


def _blocked_for_read_only(source: str, candidate: str) -> bool:
    """复刻 `isTenantAdminOnlyPath()` 的判定：精确一条 + 前缀若干"""
    exact = _string_array(source, "TENANT_ADMIN_ONLY_EXACT_PATHS")
    prefixes = _string_array(source, "TENANT_ADMIN_ONLY_PATHS")
    if candidate in exact:
        return True
    return any(candidate == prefix or candidate.startswith(f"{prefix}/") for prefix in prefixes)


def _blocked_as_platform_only(source: str, candidate: str) -> bool:
    """复刻 `isPlatformOnlyPath()` 的判定（全前缀）"""
    prefixes = _string_array(source, "PLATFORM_ONLY_PATHS")
    return any(candidate == prefix or candidate.startswith(f"{prefix}/") for prefix in prefixes)


# --------------------------------------------------------------------------------------
# 判据本体：每一条都写成「吃源码、给布尔」的函数
#
# ⚠️ 这不是为了整洁。对照组必须**把同一个判据重新跑在改坏的样本上**才算数。
# 本文件第一版的四条对照组写成了
#
#     broken = source.replace(X, Y, 1); assert X not in broken
#
# —— 那只证明了 `str.replace` 能工作、且 X 在源码里恰好出现一次，**从头到尾没有碰过
# 被验的那条断言**。把主断言换成 `assert True` 它照样绿。真正的对照组长这样：
#
#     assert _criterion(source)          # 修复在，判据说 True
#     assert not _criterion(broken)      # 修复不在，同一个判据必须说 False
# --------------------------------------------------------------------------------------


def _layout_computes_the_key(source: str) -> bool:
    return "tenantAdminOnly: !tenancy.isReadOnly" in source


def _forbidden_is_decided_by_status(source: str) -> bool:
    return "httpStatusOf(error) === 403" in source


def _retry_is_hidden_in_the_forbidden_branch(source: str) -> bool:
    return "{!forbidden && onRetry && (" in source


def _home_stops_drawing_zeroes(source: str) -> bool:
    return "{isLoadFailed ? (\n        <QueryErrorState" in source


def _gate_encloses(source: str, gate: str, needle: str) -> bool:
    """`needle` 是否真的落在离它最近的那个 `gate` 条件块**内部**

    ⚠️ 判据是**花括号配平**，不是字符距离。本文件第一版写的是
    `toggle_at - gate_at < 900` —— 那种「窗口够近就算数」的判据在同一份源码里已经
    栽过一次（拦截器那条，见 `_api_error_calls` 的注释）：门可以在按钮之前就闭合了，
    距离照样很近，而按钮其实在门外。
    """
    target = source.index(needle)
    try:
        gate_at = source.rindex(gate, 0, target)
    except ValueError:
        return False
    depth = 0
    for index in range(gate_at, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return gate_at < target < index
    return False


def _guard_back_target_follows_identity(source: str) -> bool:
    """兜底页的「返回」按钮是按**身份**选目标，而不是按「这次撞的是哪一堵墙」"""
    return (
        "const guardBackTarget = tenancy.isReadOnly ? '/dashboard' : '/console'" in source
        and "navigate(guardBackTarget)" in source
    )


# --------------------------------------------------------------------------------------
# A. 后端事实：清单里的每一条都真的够不着，留给成员的那两条真的够得着
# --------------------------------------------------------------------------------------


def _min_role(module_name: str, method: str, path: str) -> Optional[Role]:
    module = importlib.import_module(f"nekro_agent.routers.{module_name}")
    for route in module.router.routes:  # type: ignore[attr-defined]
        if getattr(route, "path", None) != path:
            continue
        if method not in (getattr(route, "methods", None) or set()):
            continue
        try:
            return inspect.getclosurevars(route.endpoint).nonlocals.get("min_role")
        except TypeError:
            return None
    raise AssertionError(f"路由表里没有 {module_name} {method} {path}——它被改名或删掉了")


#: 藏起来的那些入口，各取一条**读**接口做代表。写接口不算数：写不了不等于看不了。
ADMIN_ONLY_READ_ENDPOINTS: Tuple[Tuple[str, str, str, str], ...] = (
    ("/console", "console.overview", "GET", "/overview"),
    ("/console/instances", "console.instances", "GET", "/instances"),
    ("/console/quota", "console.quota", "GET", "/quota/instances"),
    # 用量与费用：它与 `/console/quota` 同档 —— 定制版客户要看得到
    # 自己的账，所以**不是** platformOnly，但整族端点都是 `Role.Admin`。
    # 这是这条依据的**第三份**手维护清单，前两份是菜单的 `visibility` 与 `TENANT_ADMIN_ONLY_PATHS`
    ("/console/usage", "console.usage", "GET", "/usage/summary"),
    ("/console/identity", "console.identity", "GET", "/identity/domains"),
    ("/console/model-groups", "console.model_groups", "GET", "/model-groups"),
    ("/chat-channel/management", "chat_channel", "GET", "/chat-channel/list"),
    ("/chat-channel/announcement", "chat_channel", "GET", "/chat-channel/announcement/instances"),
    ("/affection", "affection", "GET", "/affection/list"),
    ("/memory", "memory_admin", "GET", "/memory/paragraphs"),
    ("/worldbook", "worldbook", "GET", "/worldbook/books"),
    ("/presets", "presets", "GET", "/presets/list"),
    ("/plugins/management", "plugins", "GET", "/plugins/list"),
    ("/workspace", "workspaces", "GET", "/workspaces/list"),
)

#: 刻意**留着**的那两个入口（登记在 `TENANT_READABLE_BY_MEMBERS` 里）
MEMBER_READABLE_ENDPOINTS: Tuple[Tuple[str, str, str, str], ...] = (
    ("/console/team", "console.tenants", "GET", "/tenants/{tenant_id}/members"),
    ("/dashboard", "dashboard", "GET", "/dashboard/overview"),
)


class TestTheHidingListMatchesTheBackend:
    """清单不是拍脑袋：藏起来的每一条，后端确实最低 `Role.Admin`"""

    def test_read_only_member_really_is_below_admin(self) -> None:
        """整件事的前提：`tenant_user` 的等级低于 `Role.Admin`

        这一条如果哪天不成立了（比如有人把只读成员提到 Admin），下面所有断言都失去意义，
        本文件也应该整体重写而不是继续绿着。
        """
        assert tenant_perm_level(TenantRole.TENANT_USER) is Role.User
        assert Role.User < Role.Admin

    @pytest.mark.parametrize(("entry", "module", "method", "path"), ADMIN_ONLY_READ_ENDPOINTS)
    def test_hidden_entry_is_admin_gated_on_the_backend(
        self,
        entry: str,
        module: str,
        method: str,
        path: str,
    ) -> None:
        assert _min_role(module, method, path) is Role.Admin, (
            f"{entry} 的读接口 {method} {path} 已经不是 Role.Admin 了。"
            "如果它降级到了只读成员够得着的等级，就该把这一格从 TENANT_ADMIN_ONLY_PATHS 里拿掉，"
            "而不是继续对他隐藏一个他其实看得见的页面"
        )

    @pytest.mark.parametrize(("entry", "module", "method", "path"), ADMIN_ONLY_READ_ENDPOINTS)
    def test_hidden_entry_is_url_blocked(self, entry: str, module: str, method: str, path: str, navigation_source: str) -> None:
        del module, method, path
        assert _blocked_for_read_only(navigation_source, entry), (
            f"{entry} 后端是 Role.Admin，前端却没把它挡在只读成员之外：他敲这条 URL 会落在一页 403 上"
        )

    @pytest.mark.parametrize(("entry", "module", "method", "path"), MEMBER_READABLE_ENDPOINTS)
    def test_member_readable_entry_is_not_gated_at_all(
        self,
        entry: str,
        module: str,
        method: str,
        path: str,
    ) -> None:
        """这两条**没有** `require_role`，是有意开给只读成员的（`TENANT_READABLE_BY_MEMBERS`）"""
        del entry
        assert _min_role(module, method, path) is None

    @pytest.mark.parametrize(("entry", "module", "method", "path"), MEMBER_READABLE_ENDPOINTS)
    def test_member_readable_entry_is_still_reachable(
        self,
        entry: str,
        module: str,
        method: str,
        path: str,
        navigation_source: str,
    ) -> None:
        """把它们一起藏掉，只读成员的后台就一片空白了——那是另一种「坏掉」"""
        del module, method, path
        assert not _blocked_for_read_only(navigation_source, entry)

    def test_profile_stays_reachable_for_everyone(self, navigation_source: str) -> None:
        """自助改口令是只读成员唯一能做的写操作，两份清单都不能碰它"""
        assert not _blocked_for_read_only(navigation_source, "/profile")
        platform_only = _string_array(navigation_source, "PLATFORM_ONLY_PATHS")
        assert not any(
            "/profile" == prefix or "/profile".startswith(f"{prefix}/") for prefix in platform_only
        ), "/profile 被 PLATFORM_ONLY_PATHS 兜住了——租户成员又换不了自己的口令了"
        # 菜单那一格不能有任何可见性开关：有开关就意味着某种身份看不到它。
        # 判据取 `visibility: '` 而不是裸的 `visibility` —— 那一格的注释里恰好写着
        # 「**不设 visibility**」，按裸词判会把说明文字当成开关
        block = next(b for b in _menu_blocks(navigation_source) if "path: '/profile'," in b)
        assert "visibility: '" not in block

    def test_the_backend_probe_can_see_a_wrong_answer(self) -> None:
        """§2.11 对照组：证明 `_min_role` 不是恒返回 `Role.Admin`

        拿两条已知不同的：`POST /console/instances` 是 `Role.Super`，
        `GET /adapters/list` 是 `Role.User`。
        """
        assert _min_role("console.instances", "POST", "/instances") is Role.Super
        assert _min_role("adapters", "GET", "/adapters/list") is Role.User


class TestTheTwoListsAgree:
    """菜单那一格与 URL 兜底清单必须对得上（`PLATFORM_ONLY_PATHS` 的同一形态）"""

    def test_every_tagged_entry_is_url_blocked(self, navigation_source: str) -> None:
        tagged = _pages_with_visibility(navigation_source, "tenantAdminOnly")
        missing = sorted(page for page in tagged if not _blocked_for_read_only(navigation_source, page))
        assert not missing, (
            f"这些格子标了 visibility: 'tenantAdminOnly' 却没进 URL 兜底清单：{missing}。"
            "只读成员把它们粘进地址栏照样渲染出整页，然后撞一页 403"
        )

    def test_the_tag_parser_really_finds_entries(self, navigation_source: str) -> None:
        """§2.11 对照组的一半：解析器不是恒返回空集合（那样 missing 也恒为空）"""
        tagged = _pages_with_visibility(navigation_source, "tenantAdminOnly")
        assert "/console" in tagged
        assert "/chat-channel/announcement" in tagged
        assert len(tagged) >= 8

    def test_the_criterion_can_see_a_missing_registration(self, navigation_source: str) -> None:
        """§2.11 对照组的另一半：把 `/chat-channel` 从前缀清单里摘掉，第一条必须变红"""
        broken = navigation_source.replace("  '/chat-channel',\n", "", 1)
        assert broken != navigation_source, "样本没被改动，这条对照组什么都没验"
        assert not _blocked_for_read_only(broken, "/chat-channel/announcement")
        assert "/chat-channel/announcement" in _pages_with_visibility(broken, "tenantAdminOnly")

    def test_console_prefix_is_not_used_as_a_blanket(self, navigation_source: str) -> None:
        """`/console` 必须是**精确**匹配

        写成前缀会把 `/console/team` 一起兜进去 —— 那是只读成员唯一够得着的总后台页面，
        兜掉它等于「藏得太多」，与藏得太少一样是错的。
        """
        prefixes = _string_array(navigation_source, "TENANT_ADMIN_ONLY_PATHS")
        assert "/console" not in prefixes
        assert "/console" in _string_array(navigation_source, "TENANT_ADMIN_ONLY_EXACT_PATHS")
        assert _blocked_for_read_only(navigation_source, "/console")
        assert not _blocked_for_read_only(navigation_source, "/console/team")

    def test_platform_only_lists_grew_together(self, navigation_source: str) -> None:
        """顺带收掉的两页：`/logs` 与 `/sandbox-logs` 两族端点全是 `Role.Super`

        它们此前对**租户管理员**也画着，点进去是整页 403 —— 与本轮修的是同一个形态，
        只是换了一档身份。两份平台方清单要一起登记（`test_console_platform_only_paths.py`
        会把它们对起来，这里只钉住"确实登记了"）。
        """
        assert _min_role("logs", "GET", "/logs") is Role.Super
        assert _min_role("sandbox", "GET", "/sandbox/logs") is Role.Super
        platform_only_pages = _pages_with_visibility(navigation_source, "platformOnly")
        assert {"/logs", "/sandbox-logs"} <= platform_only_pages
        prefixes = _string_array(navigation_source, "PLATFORM_ONLY_PATHS")
        assert "/logs" in prefixes
        assert "/sandbox-logs" in prefixes


class TestTheVisibilityKeyIsActuallyWired:
    """四份清单（§2.7「零件全对、接线没了」的同一个形态）"""

    def test_the_key_is_declared_and_defaulted(self, navigation_source: str) -> None:
        assert "| 'tenantAdminOnly'" in navigation_source, "`MenuVisibilityKey` 没声明这个键"
        assert "tenantAdminOnly: boolean" in navigation_source, "`MenuVisibility` 没声明这个键"
        assert "tenantAdminOnly: true" in navigation_source, (
            "`DEFAULT_MENU_VISIBILITY` 缺这个键或取值不是 true："
            "未启用多租户的部署必须逐字不变，那里登录的永远是平台方"
        )

    def test_main_layout_computes_it(self, layout_source: str) -> None:
        assert _layout_computes_the_key(layout_source), (
            "`MainLayout` 没给这个开关赋值：`visibility[key]` 取到 undefined —— falsy ——"
            "于是这些格子对**任何身份**都不出现，而 tsc / eslint / build 三道全绿"
        )

    def test_the_dependency_array_carries_is_read_only(self, layout_source: str) -> None:
        """依赖数组要单独取出来比：useMemo 正文里本来就含 `tenancy.isReadOnly`，拿正文做判据等于恒真"""
        deps = re.search(r"\[tenancy\.[^\]]*\]\s*\)\n", layout_source)
        assert deps, "没找到 menuVisibility 那个 useMemo 的依赖数组"
        assert "tenancy.isReadOnly" in deps.group(0), (
            "`isReadOnly` 没进依赖数组：身份从「加载中」变成「只读」时菜单不重算，"
            "只读成员在那一帧之后仍然看得到这些格子"
        )

    def test_model_groups_entry_also_drops_read_only(self, layout_source: str) -> None:
        """`/console/model-groups` 用的是 `tenantModelGroups`（它还要挡平台方），要另外叠一个 `!isReadOnly`"""
        assert "tenantModelGroups: tenancy.isTenantScoped && !tenancy.isReadOnly" in layout_source

    def test_main_layout_consumes_the_url_list(self, layout_source: str) -> None:
        """接线的第二半：清单登记得再全，没人调用那个判定函数也是白写"""
        assert "isTenantAdminOnlyPath" in layout_source
        assert "tenancy.isReadOnly && isTenantAdminOnlyPath(location.pathname)" in layout_source
        assert "isPlatformOnlyBlocked || isReadOnlyBlocked" in layout_source

    def test_the_guard_tells_the_two_reasons_apart(self, layout_source: str) -> None:
        """两档挡人的理由不同，说法也要不同"""
        assert "guard.readOnlyDescription" in layout_source
        assert "guard.backToDashboard" in layout_source

    def test_the_two_path_lists_really_overlap_for_a_read_only_member(
        self,
        navigation_source: str,
    ) -> None:
        """下一条断言的前提：确实存在「只读成员撞上的是平台方那堵墙」的路径

        这一条先把事实钉住，否则下一条就成了一个永远走不到的分支上的空规定。
        `/logs`、`/user-manager`、`/settings/models` 只登记在 `PLATFORM_ONLY_PATHS` 里，
        只读成员敲它们时 `isPlatformOnlyBlocked` 为真、`isReadOnlyBlocked` 为假。
        """
        overlap = [
            path
            for path in ("/logs", "/sandbox-logs", "/user-manager", "/settings/models", "/cloud")
            if _blocked_as_platform_only(navigation_source, path)
            and not _blocked_for_read_only(navigation_source, path)
        ]
        assert overlap, (
            "两份清单不再相交了：如果这是有意的（比如所有平台方路径都同时进了只读清单），"
            "下面那条关于「返回」按钮的规定就可以删掉；在那之前它必须留着"
        )

    def test_guard_back_target_follows_the_identity_not_the_wall(self, layout_source: str) -> None:
        """「返回」按钮的目标只能按**身份**选，不能按「这次撞的是哪一堵墙」选

        上一条证明了两份清单相交。在交集里（只读成员敲 `/logs`）`isPlatformOnlyBlocked`
        为真，于是按「哪堵墙」分档会给他一个指向 `/console` 的按钮 —— 而 `/console` 在
        `TENANT_ADMIN_ONLY_EXACT_PATHS` 里，点下去落在**另一页**「无权访问」上。
        那就是「点不动的按钮」，只是藏在交集里，三种身份各看一遍也未必撞得到。
        """
        assert _guard_back_target_follows_identity(layout_source), (
            "「返回」的目标又跟着 `isPlatformOnlyBlocked` 走了：只读成员敲 /logs 之类的 URL 时，"
            "这个按钮会把他送去同样被挡住的 /console"
        )
        assert "navigate(isPlatformOnlyBlocked ? '/console' : '/dashboard')" not in layout_source, (
            "旧的按墙分档写法回来了"
        )

    def test_the_back_target_criterion_can_see_the_old_wall_based_form(
        self,
        layout_source: str,
    ) -> None:
        """§2.11 对照组：换回按墙分档，上面那条用的判据必须翻面"""
        broken = layout_source.replace(
            "const guardBackTarget = tenancy.isReadOnly ? '/dashboard' : '/console'",
            "const guardBackTarget = isPlatformOnlyBlocked ? '/console' : '/dashboard'",
            1,
        )
        assert broken != layout_source, "样本没被改动，这条对照组什么都没验"
        assert _guard_back_target_follows_identity(layout_source)
        assert not _guard_back_target_follows_identity(broken)

    def test_the_wiring_criterion_can_see_a_missing_assignment(self, layout_source: str) -> None:
        """§2.11 对照组：删掉赋值那一行，`test_main_layout_computes_it` 用的判据必须翻面"""
        broken = layout_source.replace("      tenantAdminOnly: !tenancy.isReadOnly,\n", "", 1)
        assert broken != layout_source, "样本没被改动，这条对照组什么都没验"
        assert _layout_computes_the_key(layout_source)
        assert not _layout_computes_the_key(broken)


# --------------------------------------------------------------------------------------
# B. 页面自己的 403 分支 —— 这一轮真正要修的东西
# --------------------------------------------------------------------------------------


class TestForbiddenIsDetectedByStatusNotByName:
    """判据必须是 HTTP 状态码

    `require_role` 抛的是原生 `HTTPException`，经 `http_exception_handler` 归一化之后
    `error` 字段恒为 `"HTTPException"` —— 403 与 400、409、502 在错误类名上**完全一样**。
    全部 console 端点用的都是 `require_role`，所以按类名判断等于判不出来。
    """

    def test_api_error_carries_the_status(self) -> None:
        source = _read(AXIOS)
        assert "public readonly status: number | null" in source
        assert "export function isForbiddenError" in source
        assert _forbidden_is_decided_by_status(source)

    def test_the_interceptor_actually_passes_the_status(self) -> None:
        """§2.7：字段声明了、拦截器不传 = 永远是 null = `isForbiddenError` 恒 false

        拦截器里有**四条**拿得到响应体的 throw 分支（401 / 标准错误体 / 协议错误体 / 兜底），
        漏掉任何一条，那一类响应上的 403 就判不出来 —— 而且只在那一类上失效，
        本地随手一试往往正好走的是另一条。
        """
        calls = _api_error_calls(_read(AXIOS))
        assert len(calls) == 4, f"拦截器里的 throw 分支数变了（{len(calls)} 条），判据要跟着改"
        for args in calls:
            assert args[-1] == "status", (
                f"这条 throw 的最后一个实参是 {args[-1]!r} 而不是 status："
                "那一类响应上的 HTTP 状态码进不了 ApiError，403 就判不出来"
            )

    def test_the_status_wiring_criterion_can_see_a_dropped_argument(self) -> None:
        """§2.11 对照组：从其中一条 throw 里把 status 摘掉，上面那条必须变红

        第一版判据是「call 起点后 420 字符内出现过 status」，这条对照组当场把它证伪了 ——
        窗口会滑进**下一条** throw，于是摘掉一个参数它照样绿。
        """
        source = _read(AXIOS)
        broken = source.replace(
            "throw new ApiError(data.error, data.message, data.detail, data.data, status)",
            "throw new ApiError(data.error, data.message, data.detail, data.data)",
            1,
        )
        assert broken != source, "样本没被改动，这条对照组什么都没验"
        assert any(args[-1] != "status" for args in _api_error_calls(broken))

    def test_not_found_stays_not_found(self) -> None:
        """404 是**越权语义**，不能被改口成「你无权查看」

        改口等于把后端刻意藏起来的「这个资源存在」又讲了出来。
        """
        source = _read(AXIOS)
        assert "export function isNotFoundError" in source
        detail = _read(INSTANCE_DETAIL)
        not_found_at = detail.index("isNotFoundError(instanceQuery.error)")
        generic_at = detail.index("instanceQuery.isError ? (")
        assert not_found_at < generic_at, (
            "404 分支排在通用错误分支后面 = 越权 404 会被渲染成「加载失败」，"
            "而它必须继续说「实例不存在」"
        )

    def test_the_criterion_can_see_a_type_based_check(self) -> None:
        """§2.11 对照组：把判据换回按错误类名，`test_api_error_carries_the_status` 必须翻面"""
        source = _read(AXIOS)
        broken = source.replace("httpStatusOf(error) === 403", "error.type === 'Forbidden'", 1)
        assert broken != source, "样本没被改动，这条对照组什么都没验"
        assert _forbidden_is_decided_by_status(source)
        assert not _forbidden_is_decided_by_status(broken)


class TestForbiddenStateNeverOffersADeadButton:
    """403 分支不摆重试按钮：再点一次还是 403"""

    def test_retry_is_rendered_only_outside_the_forbidden_branch(self) -> None:
        source = _read(ERROR_STATE)
        assert _retry_is_hidden_in_the_forbidden_branch(source), (
            "重试按钮没有被 `!forbidden` 收敛——那就是一个点了不会有任何变化的按钮，"
            "本项目在这个形态上栽过两次"
        )

    def test_it_says_two_different_things(self) -> None:
        source = _read(ERROR_STATE)
        assert "accessDenied.title" in source
        assert "loadFailed.title" in source

    def test_the_criterion_can_see_an_unconditional_retry(self) -> None:
        """§2.11 对照组：把 `!forbidden` 摘掉，上面那条用的判据必须翻面"""
        source = _read(ERROR_STATE)
        broken = source.replace("{!forbidden && onRetry && (", "{onRetry && (", 1)
        assert broken != source, "样本没被改动，这条对照组什么都没验"
        assert _retry_is_hidden_in_the_forbidden_branch(source)
        assert not _retry_is_hidden_in_the_forbidden_branch(broken)


#: 每一页：(文件, 空态里那句会骗人的文案, 必须排在它前面的失败分支判据)
#:
#: 顺序就是判据本身 —— 失败分支排在空态后面的话，403 照旧被渲染成「什么都没有」，
#: 两个分支的触发条件在只读身份上**同时成立**。
PAGES_WITH_AN_EMPTY_STATE: Tuple[Tuple[Path, str, str], ...] = (
    (INSTANCE_LIST, "instancesQuery.isError ? (", "filteredInstances.length === 0 ? ("),
    (INSTANCE_QUOTA, "quotaQuery.isError ? (", "items.length === 0 ? ("),
    (TENANT_LIST, "tenantsQuery.isError ? (", "tenants.length === 0 ? ("),
    (MEMBER_TABLE, "membersQuery.isError && !isEndpointMissing ? (", "members.length === 0 ? ("),
)


class TestEveryConsolePageTellsEmptyFromForbidden:
    @pytest.mark.parametrize(("path", "failure_branch", "empty_branch"), PAGES_WITH_AN_EMPTY_STATE)
    def test_failure_branch_runs_before_the_empty_state(
        self,
        path: Path,
        failure_branch: str,
        empty_branch: str,
    ) -> None:
        source = _read(path)
        assert failure_branch in source, f"{path} 没有失败分支：403 会被渲染成空列表"
        assert "QueryErrorState" in source, f"{path} 没用统一的失败呈现，「不让看」与「没有」又混在一起了"
        assert source.index(failure_branch) < source.index(empty_branch), (
            f"{path} 的失败分支排在空态后面 —— 两个条件在 403 下同时成立，先命中的那个会赢"
        )

    def test_console_home_stops_rendering_zeroes_on_failure(self) -> None:
        """首页那一排 `?? 0` 是缺陷本体：失败时整块不能再画

        判据取「统计卡片那一段被 `isLoadFailed` 收敛住」，而不是「文件里有 QueryErrorState」——
        后者在把组件塞进页脚时也成立。
        """
        source = _read(CONSOLE_HOME)
        assert "const isLoadFailed = overviewQuery.isError || instancesQuery.isError" in source
        assert _home_stops_drawing_zeroes(source), (
            "失败分支不在业务区最外层：只要还有任何一张 `?? 0` 的卡片画在外面，"
            "用户看到的仍然是「实例总数 0」"
        )
        # 「Docker 可用/不可用」也是从同一个响应里读的布尔位，取不到时会稳定地说错话
        assert "{!isLoadFailed && (\n          <SimpleTooltip" in source

    def test_quota_stat_cards_are_gated_too(self) -> None:
        """配额页三张卡片同理：403 之下会显示「全局并发上限 0」——一句像真的假话"""
        source = _read(INSTANCE_QUOTA)
        assert "{!quotaQuery.isError && (" in source

    def test_the_zero_criterion_can_see_the_old_shape(self) -> None:
        """§2.11 对照组：把收敛条件去掉，上面那条用的判据必须翻面"""
        source = _read(CONSOLE_HOME)
        broken = source.replace(
            "{isLoadFailed ? (\n        <QueryErrorState",
            "{false ? (\n        <QueryErrorState",
            1,
        )
        assert broken != source, "样本没被改动，这条对照组什么都没验"
        assert _home_stops_drawing_zeroes(source)
        assert not _home_stops_drawing_zeroes(broken)

    def test_announcement_page_stops_before_drawing_a_dead_send_button(self) -> None:
        """公告页六个端点全是 `Role.Admin`：读不到就不该把「发送公告」摆出来

        此前它会渲染成"你名下一个群都没有"的空选择器加一个必然 403 的发送按钮。
        判据取「失败分支是 early return」——它必须排在整个表单之前。
        """
        source = _read(ANNOUNCEMENT)
        # 频道列表那条查询按作用域开关，所以判据带着作用域
        # （见 `test_the_announcement_failure_follows_the_active_scope`）
        assert "const isLoadFailed = isInstancesError || isChannelsFailed" in source
        assert "if (isLoadFailed) {" in source
        assert source.index("if (isLoadFailed) {") < source.index("composer.send"), (
            "失败分支排在发送按钮之后 = 按钮照画不误"
        )

    def test_identity_page_swapped_its_bare_alerts(self) -> None:
        """三处 `<Alert severity=\"error\">` 原样吐后端的「权限不足」四个字，没有下一步"""
        source = _read(IDENTITY_DOMAIN)
        assert source.count("QueryErrorState") >= 4  # import + 三处
        assert "errorMessage(domainsQuery.error)}</Alert>" not in source

    def test_instance_detail_no_longer_calls_a_403_a_deleted_instance(self) -> None:
        source = _read(INSTANCE_DETAIL)
        assert "QueryErrorState" in source
        assert "isNotFoundError(instanceQuery.error)" in source


class TestNoPageOffersAWriteItCannotDo:
    """「不要摆出点不动的按钮」——这一条本项目栽过两次"""

    def test_create_instance_button_is_platform_only_on_the_home_page(self) -> None:
        """`POST /console/instances` 是 `Role.Super`：租户管理员点它必然 403"""
        assert _min_role("console.instances", "POST", "/instances") is Role.Super
        source = _read(CONSOLE_HOME)
        assert "const { isPlatformOwner } = useIdentity()" in source
        # 两处入口：顶栏那个，以及空态里那个
        assert source.count("{isPlatformOwner && (") >= 2, (
            "首页还有「新建实例」按钮没有身份门——空态里那一个最容易被漏掉"
        )

    def test_the_wizard_itself_also_stops_a_tenant(self) -> None:
        """按钮藏了，URL 还在：不拦的话租户管理员填完三步才在最后一步收 403"""
        source = _read(WIZARD)
        assert "if (!isPlatformOwner) {" in source
        assert "useIdentity" in source

    def test_enable_disable_stays_platform_only(self) -> None:
        """详情页顶栏那个「启用 / 停用」（之前修过的，别被改回去）"""
        assert _min_role("console.instances", "POST", "/instances/{instance_key}/enable") is Role.Super
        assert _min_role("console.instances", "POST", "/instances/{instance_key}/disable") is Role.Super
        source = _read(INSTANCE_DETAIL)
        assert _gate_encloses(
            source,
            "{isPlatformOwner && (",
            "toggleMutation.mutate(!instance.enabled)",
        ), "「启用 / 停用」按钮不在 isPlatformOwner 门里了：租户身上又摆着一个必然 403 的按钮"

    def test_the_containment_criterion_can_see_a_button_outside_the_gate(self) -> None:
        """§2.11 对照组：把那道门摘掉，上面那条用的判据必须翻面

        摘掉之后 `rindex` 会退到**更早**的那道 `isPlatformOwner` 门（迁移卡片那个），
        而它在按钮之前就闭合了 —— 这正是「按字符距离判」会漏掉、按配平判才抓得到的形态。
        """
        source = _read(INSTANCE_DETAIL)
        broken = source.replace("            {isPlatformOwner && (\n", "            {true && (\n", 1)
        assert broken != source, "样本没被改动，这条对照组什么都没验"
        needle = "toggleMutation.mutate(!instance.enabled)"
        assert _gate_encloses(source, "{isPlatformOwner && (", needle)
        assert not _gate_encloses(broken, "{isPlatformOwner && (", needle)

    def test_events_tab_stays_behind_the_protocol_capability(self) -> None:
        """`GET /console/health/events` 是 `Role.Admin`：只读成员点开就是一条红色的加载失败"""
        # 路由表里的 path 带模块自己的前缀（`routers/console/health.py` 挂在 `/health`）
        assert _min_role("console.health", "GET", "/health/events") is Role.Admin
        source = _read(INSTANCE_DETAIL)
        assert "...(canOperateProtocol" in source
        assert "activeTab === 'events' && !canOperateProtocol" in source, (
            "直接敲 `/console/instances/xxx/events` 时没有回落，页签藏了 URL 还进得去"
        )


class TestAFailureIsNeverRenamedIntoSomethingElse:
    """「失败」不能被改口成「不存在」「接口没上线」或「你什么都没有」

    `InstanceDetailPage` 那一处（403 被说成「实例不存在或已被删除」）是本轮修的原型，
    但同一个判据在租户页族里还有几处副本 —— 这个类钉的是那几处。
    """

    def test_authz_404_is_not_called_an_undeployed_backend(self) -> None:
        """越权 404 不是「后端接口尚未就绪」

        后端事实锚点：`assert_tenant_accessible` 抛的是 `NotFoundError`，而
        `NotFoundError.http_status == 404`（§1.4：越权一律 404，403 会泄露"该资源存在"）。
        前端 `isTenantEndpointUnavailable()` 曾把 `type === 'NotFoundError'` 直接算成
        「接口没上线」，于是「你没权限看这个租户」被渲染成「多租户功能还没上线」——
        `pages/settings/system.tsx` 顶部已经把这句话点名成一条错误，那是它的第二处实现。
        """
        from nekro_agent.schemas.errors import NotFoundError

        assert NotFoundError.http_status == 404

        tenant_perm_source = Path("nekro_agent/services/user/tenant_perm.py").read_text(
            encoding="utf-8"
        )
        assert "async def assert_tenant_accessible" in tenant_perm_source
        body = tenant_perm_source[tenant_perm_source.index("async def assert_tenant_accessible") :]
        body = body[: body.index("\ndef ") if "\ndef " in body else len(body)]
        assert "NotFoundError(" in body, "后端不再用 NotFoundError 表达越权了，前端判据要跟着改"

        source = _read(FRONTEND / "services/api/tenant.ts")
        assert "if (error.type === 'NotFoundError') {\n      return false\n    }" in source, (
            "`isTenantEndpointUnavailable` 又把后端的越权 404 算成「接口尚未就绪」了"
        )
        # 真正「路由没匹配上」的那一档要靠状态码认（`type` 恒为 "HTTPException"）。
        # 旧实现在 ApiError 分支直接 return，下面那段对 axios 调用是**死代码**
        assert "error.status === 404 || error.status === 405 || error.status === 501" in source

    @pytest.mark.parametrize(
        "page",
        (
            FRONTEND / "pages/console/TenantTeamPage.tsx",
            FRONTEND / "pages/console/TenantDetailPage.tsx",
        ),
    )
    def test_tenant_pages_tell_404_from_other_failures(self, page: Path) -> None:
        """「租户不存在或已被归档」只在 404 下成立

        `/console/team` 是只读租户成员唯一够得着的总后台页（`GET /console/tenants/{id}`
        与成员列表刻意开给成员）。在那一页上把一次 5xx 说成「你的租户已被归档」，
        对一个付费客户就是「我的服务被停了」。形态与详情页那条完全一样。
        """
        source = _read(page)
        assert "isNotFoundError(tenantQuery.error)" in source, f"{page} 没有把 404 单独摘出来"
        assert "QueryErrorState" in source, f"{page} 其余失败仍然被说成「不存在」"
        assert source.index("isNotFoundError(tenantQuery.error)") < source.index(
            "t('detail.notFound')"
        ), f"{page} 的 404 判据排在「不存在」文案之后，说明那句文案仍是无条件的"

    @pytest.mark.parametrize(
        ("page", "legacy_key"),
        (
            (FRONTEND / "pages/console/TenantListPage.tsx", "t('list.loadFailed')"),
            (
                FRONTEND / "pages/console/components/TenantMemberTable.tsx",
                "t('members.loadFailed')",
            ),
        ),
    )
    def test_one_failure_is_reported_exactly_once(self, page: Path, legacy_key: str) -> None:
        """同一次失败不能既画「加载失败 + 重试」又画「你无权查看」

        这两页在加 `QueryErrorState` 时，**旧的那条错误横幅没有删**，两者的渲染条件
        （`isError && !isEndpointMissing`）完全重合。403 之下用户同时读到两种说法，
        而旧横幅上那个重试按钮点了永远是同一个 403 —— 正是本轮在消灭的「点不动的按钮」，
        只不过它是被本轮自己留下的。
        """
        source = _read(page)
        assert "QueryErrorState" in source
        assert legacy_key not in source, (
            f"{page} 里旧的失败横幅还在，它会和 QueryErrorState 同时出现，且自带一个重试按钮"
        )

    def test_the_duplicate_criterion_would_have_caught_the_old_shape(self) -> None:
        """§2.11 对照组：把旧横幅塞回去，上面那条必须变红"""
        source = _read(FRONTEND / "pages/console/TenantListPage.tsx")
        broken = source + "\n// {t('list.loadFailed')}\n"
        assert "t('list.loadFailed')" not in source
        assert "t('list.loadFailed')" in broken


class TestBothLocalesCarryTheNewStrings:
    """缺一个键就是把 `errors.accessDenied.title` 原样显示给用户看"""

    @pytest.mark.parametrize("locale", ("zh-CN", "en-US"))
    def test_error_strings(self, locale: str) -> None:
        data = json.loads(_read(FRONTEND / f"locales/{locale}/errors.json"))
        assert data["accessDenied"]["title"]
        assert data["accessDenied"]["hint"]
        assert data["loadFailed"]["title"]
        assert data["loadFailed"]["retry"]

    @pytest.mark.parametrize("locale", ("zh-CN", "en-US"))
    def test_guard_strings(self, locale: str) -> None:
        data = json.loads(_read(FRONTEND / f"locales/{locale}/tenant.json"))
        assert data["guard"]["readOnlyDescription"]
        assert data["guard"]["backToDashboard"]

    @pytest.mark.parametrize("locale", ("zh-CN", "en-US"))
    def test_wizard_strings(self, locale: str) -> None:
        data = json.loads(_read(FRONTEND / f"locales/{locale}/console-setup.json"))
        assert data["wizard"]["backToList"]


# --------------------------------------------------------------------------------------
# C. 对抗式复核补的接线判据
#
# 上面那一批用例是「零件」判据：清单里有没有这一条、键声明了没有、文件里出现过
# `QueryErrorState` 没有。复核对本轮改动跑了 38 条**接线**变异（每一条把某个
# **调用点**改回旧行为），**9 条存活**——全部是同一个形态：值声明得好好的，
# 但没有任何断言盯着它被谁用、用在哪：零件全对、调用点没了 —— 测试会全绿。
#
# 存活的九条，逐条对应下面一个用例：
#
# | 变异 | 产品后果 | 补的用例 |
# |---|---|---|
# | 兜底页 JSX 改回 `{isPlatformOnlyBlocked ? (` | 只读成员的 URL 兜底**整层**失效 | `test_the_guard_page_consumes_the_combined_flag` |
# | `isTenantAdminOnlyPath` 不看精确清单 | `/console` 不再被兜住 | `test_the_url_predicate_reads_both_lists` |
# | `const forbidden = false && ...` | 每个 403 都变成「加载失败」+ 死重试按钮 | `test_the_forbidden_face_is_not_short_circuited` |
# | `httpStatusOf` 单独吞掉 403 | 同上，低一层 | `test_the_status_probe_returns_the_status_verbatim` |
# | 首页 / 公告页把 `error` 传成 null | 403 被说成「加载失败」 | `test_*_hands_the_real_error_object_to_the_failure_state` |
# | events 页签 / 隔离规则页换回裸 `<Alert>` | 同一个组件的第二、第三个调用点 | `test_*_uses_the_shared_failure_state` |
# | 详情页 404 分支的条件取反 | 404 说「加载失败」、5xx 说「实例不存在」 | `test_the_two_detail_branches_render_the_right_thing` |
#
# 每条都按 §2.34 写：锚点必须带上**让这段代码起作用的语法**，不能只是名字
# （`QueryErrorState` 这个名字光 import 那一行就有了）。每条都配 §2.11 的对照组：
# 把变异做出来，同一个判据必须翻面。
# --------------------------------------------------------------------------------------


NETWORK_RULES = FRONTEND / "pages/console/NetworkRuleOrphansPage.tsx"

#: 兜底页那个三元的**条件**。两档合流后它必须是 `isGuardBlocked`
GUARD_JSX = "            {isGuardBlocked ? ("
#: 详情页两支分支的条件，逐字（含缩进）
DETAIL_NOT_FOUND_BRANCH = "        ) : isNotFoundError(instanceQuery.error) ? ("
DETAIL_GENERIC_BRANCH = "        ) : instanceQuery.isError ? ("


def _guard_page_consumes_the_combined_flag(source: str) -> bool:
    return GUARD_JSX in source and "{isPlatformOnlyBlocked ? (" not in source


def _url_predicate_reads_both_lists(source: str) -> bool:
    body = _slice_between(
        source,
        "export const isTenantAdminOnlyPath = (pathname: string): boolean => {",
        "// 获取翻译的辅助函数",
    )
    return (
        "TENANT_ADMIN_ONLY_EXACT_PATHS.some(exact => normalized === exact)" in body
        and "prefix => normalized === prefix || normalized.startsWith(`${prefix}/`)" in body
        and "const normalized = stripTrailingSlash(pathname)" in body
    )


def _forbidden_face_is_not_short_circuited(source: str) -> bool:
    return "\n  const forbidden = isForbiddenError(error)\n" in source


def _status_probe_returns_the_status_verbatim(source: str) -> bool:
    return "  if (error instanceof ApiError) {\n    return error.status\n  }\n" in source


def _hands_the_real_error_object(source: str, derived: str, rendered: str) -> bool:
    """失败态拿到的是**真的那个错误对象**，不是 null / undefined

    传 null 进去时组件的 `isForbiddenError` 恒 false —— 403 会被说成「加载失败」并
    重新长出一个重试按钮。这条判据要同时钉「`loadError` 是从查询错误算出来的」
    与「它真的被传进了失败态」。
    """
    return derived in source and rendered in source


def _detail_branches_render_the_right_thing(source: str) -> bool:
    if DETAIL_NOT_FOUND_BRANCH not in source or DETAIL_GENERIC_BRANCH not in source:
        return False
    not_found_body = _slice_between(source, DETAIL_NOT_FOUND_BRANCH, DETAIL_GENERIC_BRANCH)
    generic_body = _slice_between(source, DETAIL_GENERIC_BRANCH, "        ) : !instance ? (")
    return (
        "t('detail.notFound')" in not_found_body
        and "QueryErrorState" not in not_found_body
        and "QueryErrorState" in generic_body
        and "t('detail.notFound')" not in generic_body
    )


class TestTheValuesActuallyFlowToWhereTheyMatter:
    """九条存活变异的看门人"""

    def test_the_guard_page_consumes_the_combined_flag(self, layout_source: str) -> None:
        """`isGuardBlocked` 声明了不等于用了

        变异：把兜底页那个三元的条件换回 `isPlatformOnlyBlocked`（声明照留）。
        后果是只读成员的 URL 兜底**整层**消失（菜单藏着、地址栏照进），
        而原有的三条断言（清单登记了、`isReadOnlyBlocked` 算出来了、两者或起来了）**全绿**。
        """
        assert _guard_page_consumes_the_combined_flag(layout_source), (
            "兜底页没有用合流后的 `isGuardBlocked`：只读成员那一档白算了"
        )

    def test_the_guard_consumption_criterion_can_see_the_old_form(self, layout_source: str) -> None:
        broken = layout_source.replace(GUARD_JSX, "            {isPlatformOnlyBlocked ? (", 1)
        assert broken != layout_source, "样本没被改动，这条对照组什么都没验"
        assert _guard_page_consumes_the_combined_flag(layout_source)
        assert not _guard_page_consumes_the_combined_flag(broken)

    def test_the_url_predicate_reads_both_lists(self, navigation_source: str) -> None:
        """清单登记得再全，判定函数不读它也是白写

        本文件的 `_blocked_for_read_only()` 是把 TS 的判定**在 Python 里重写了一遍**，
        于是 TS 那个函数体本身一条断言都没有（§2.10：判据与被判值的形态一起变了）。
        变异「让 `isTenantAdminOnlyPath` 不看精确清单」→ `/console` 不再被兜住 → 全绿。
        """
        assert _url_predicate_reads_both_lists(navigation_source)

    def test_the_url_predicate_criterion_can_see_a_dropped_list(
        self,
        navigation_source: str,
    ) -> None:
        broken = navigation_source.replace(
            "TENANT_ADMIN_ONLY_EXACT_PATHS.some(exact => normalized === exact)",
            "TENANT_ADMIN_ONLY_EXACT_PATHS.some(exact => normalized === `${exact}/__never__`)",
            1,
        )
        assert broken != navigation_source, "样本没被改动，这条对照组什么都没验"
        assert _url_predicate_reads_both_lists(navigation_source)
        assert not _url_predicate_reads_both_lists(broken)

    def test_the_exact_list_survives_a_trailing_slash(self, navigation_source: str) -> None:
        """`#/console/` 与 `#/console` 是同一页，兜底必须同样兜住

        react-router 匹配路由时忽略结尾的 `/`，`location.pathname` 却原样保留它。
        前缀那一档天然不受影响（`startsWith(prefix + '/')`），**只有精确匹配这一档**
        会被一个斜杠绕过去 —— 这也是同形态的 `PLATFORM_ONLY_PATHS` 那边看不到它的原因。
        """
        assert "const stripTrailingSlash = (pathname: string): string =>" in navigation_source
        assert "pathname.length > 1 && pathname.endsWith('/')" in navigation_source

    def test_the_forbidden_face_is_not_short_circuited(self) -> None:
        """判据在 axios 里写对了，组件里可以再把它短路掉

        变异 `const forbidden = false && isForbiddenError(error)`：403 从此长着
        「加载失败」的脸，还重新长出一个点了必然还是 403 的重试按钮 —— 本轮要消灭的
        那个形态原样回来，而 `{!forbidden && onRetry && (` 那条断言照旧成立。
        """
        assert _forbidden_face_is_not_short_circuited(_read(ERROR_STATE))

    def test_the_forbidden_face_criterion_can_see_a_short_circuit(self) -> None:
        source = _read(ERROR_STATE)
        broken = source.replace(
            "  const forbidden = isForbiddenError(error)",
            "  const forbidden = false && isForbiddenError(error)",
            1,
        )
        assert broken != source, "样本没被改动，这条对照组什么都没验"
        assert _forbidden_face_is_not_short_circuited(source)
        assert not _forbidden_face_is_not_short_circuited(broken)

    def test_the_status_probe_returns_the_status_verbatim(self) -> None:
        """同一件事在更低一层还能再被短路一次

        `isForbiddenError` → `httpStatusOf` → `error.status`。中间那一跳给 403 加一条
        特判（`return error.status === 403 ? null : error.status`）就够了：
        拦截器四条 throw 照传状态码，`isForbiddenError` 恒 false。
        """
        assert _status_probe_returns_the_status_verbatim(_read(AXIOS))

    def test_the_status_probe_criterion_can_see_a_special_case(self) -> None:
        source = _read(AXIOS)
        broken = source.replace(
            "  if (error instanceof ApiError) {\n    return error.status\n  }\n",
            "  if (error instanceof ApiError) {\n    return error.status === 403 ? null : error.status\n  }\n",
            1,
        )
        assert broken != source, "样本没被改动，这条对照组什么都没验"
        assert _status_probe_returns_the_status_verbatim(source)
        assert not _status_probe_returns_the_status_verbatim(broken)

    def test_console_home_hands_the_real_error_object_to_the_failure_state(self) -> None:
        assert _hands_the_real_error_object(
            _read(CONSOLE_HOME),
            "  const loadError = overviewQuery.error ?? instancesQuery.error",
            "<QueryErrorState error={loadError} onRetry={retryAll} />",
        )

    def test_announcement_hands_the_real_error_object_to_the_failure_state(self) -> None:
        assert _hands_the_real_error_object(
            _read(ANNOUNCEMENT),
            "  const loadError = instancesError ?? (isChannelsFailed ? channelsError : undefined)",
            "<QueryErrorState error={loadError} onRetry={retryAll} sx={{ flex: 1 }} />",
        )

    def test_the_error_object_criterion_can_see_a_nulled_error(self) -> None:
        source = _read(CONSOLE_HOME)
        derived = "  const loadError = overviewQuery.error ?? instancesQuery.error"
        rendered = "<QueryErrorState error={loadError} onRetry={retryAll} />"
        broken = source.replace(derived, "  const loadError = null", 1)
        assert broken != source, "样本没被改动，这条对照组什么都没验"
        assert _hands_the_real_error_object(source, derived, rendered)
        assert not _hands_the_real_error_object(broken, derived, rendered)

    def test_the_announcement_failure_follows_the_active_scope(self) -> None:
        """频道列表那条查询是按作用域开关的，判据也得按作用域算

        `enabled: scopeType === 'channels'` 只管**不再发请求**，不会把上一次的
        `isError` 抹掉。少了这个条件，一次与当前作用域无关的失败会把整页盖住。
        `retryAll` 里本来就写着同一个条件 —— 这是同一个判据的第二处实现。
        """
        source = _read(ANNOUNCEMENT)
        assert "const isChannelsScope = scopeType === 'channels'" in source
        assert "const isChannelsFailed = isChannelsError && isChannelsScope" in source
        assert "const isLoadFailed = isInstancesError || isChannelsFailed" in source
        assert "if (isChannelsScope) void refetchChannels()" in source

    @pytest.mark.parametrize(
        ("page", "rendered"),
        (
            (
                INSTANCE_DETAIL,
                "<QueryErrorState error={eventsQuery.error} onRetry={() => void eventsQuery.refetch()} />",
            ),
            (
                NETWORK_RULES,
                "<QueryErrorState compact error={planQuery.error} onRetry={() => void planQuery.refetch()} />",
            ),
        ),
    )
    def test_the_secondary_call_sites_use_the_shared_failure_state(
        self,
        page: Path,
        rendered: str,
    ) -> None:
        """`assert "QueryErrorState" in source` 光靠 import 那一行就成立

        详情页里有**两个**调用点（主查询 + events 页签），隔离规则页是第三个。
        把其中任意一个换回裸 `<Alert>`，「文件里出现过这个名字」照旧成立 ——
        §2.7「同一个 API 的第二个调用点」的原样复现。
        """
        assert rendered in _read(page), f"{page} 的这个调用点换回裸 Alert 了"

    def test_the_two_detail_branches_render_the_right_thing(self) -> None:
        """两支的**顺序**对了不等于两支**渲染的东西**对了

        原判据比的是 `source.index("isNotFoundError(...)") < source.index("isError ? (")`。
        把 404 那一支的条件取反成 `instanceQuery.isError && !isNotFoundError(...)`，
        那个名字仍在同一个位置出现 —— 判据全绿，而产品行为整个翻了面：
        404（越权）说「加载失败」，403/5xx 说「实例不存在或已被删除」。
        """
        assert _detail_branches_render_the_right_thing(_read(INSTANCE_DETAIL))

    def test_the_detail_branch_criterion_can_see_an_inverted_condition(self) -> None:
        source = _read(INSTANCE_DETAIL)
        broken = source.replace(
            DETAIL_NOT_FOUND_BRANCH,
            "        ) : instanceQuery.isError && !isNotFoundError(instanceQuery.error) ? (",
            1,
        )
        assert broken != source, "样本没被改动，这条对照组什么都没验"
        assert _detail_branches_render_the_right_thing(source)
        assert not _detail_branches_render_the_right_thing(broken)


class TestGroupLevelHidingIsAlignedToo:
    """菜单**组**上标的那一次，两份清单的对齐用例原本一格都检查不到"""

    def test_every_child_of_a_hidden_group_is_url_blocked(self, navigation_source: str) -> None:
        children = _group_children_with_visibility(navigation_source, "tenantAdminOnly")
        assert children, "没解析出任何被整组藏起来的子项——解析器脱靶了，下面的断言会恒真"
        missing = sorted(
            path for path in children if not _blocked_for_read_only(navigation_source, path)
        )
        assert not missing, (
            f"这些子项所在的组标了 tenantAdminOnly，却没被 URL 兜底清单覆盖：{missing}。"
            "组上标一次是允许的写法，但那只管画不画，地址栏照样进得去"
        )

    def test_the_group_parser_finds_the_tenant_admin_groups(self, navigation_source: str) -> None:
        """§2.11 对照组的一半：解析器不是恒返回空集合

        P3 把 `/presets`、`/memory`、`/worldbook`、`/affection`、`/state`、`/prompt-lab`
        收进了新的「角色」组；这一组同样整组标了 `tenantAdminOnly`，也必须被解析出来。
        """
        children = _group_children_with_visibility(navigation_source, "tenantAdminOnly")
        assert {"/chat-channel/management", "/plugins/management", "/workspace/skills"} <= children
        assert {"/presets", "/memory", "/worldbook", "/affection", "/state", "/prompt-lab"} <= children

    def test_the_group_criterion_can_see_a_missing_registration(
        self,
        navigation_source: str,
    ) -> None:
        """§2.11 对照组的另一半：把 `/workspace` 从前缀清单里摘掉，第一条必须变红"""
        broken = navigation_source.replace("  '/workspace',\n", "  '/workspace-DISABLED',\n", 1)
        assert broken != navigation_source, "样本没被改动，这条对照组什么都没验"
        children = _group_children_with_visibility(broken, "tenantAdminOnly")
        assert any(not _blocked_for_read_only(broken, path) for path in children)


# --------------------------------------------------------------------------------------
# D. 适配器配置子页
#
# `AdapterConfigPage.tsx` 的 `useQuery` 此前没有任何失败分支：`GET /config/list/adapter_*` 是
# `Role.Admin`，只读租户成员得 403，页面落成「空配置表 + 『暂无 XX 配置项』」——与 #52c 要消灭的
# 「把 403 渲染成 0 / 空表」同一形态。租户管理员读侧放行（凭据掩码），不受影响。
#
# 做法与 #52c 已定型的做法一致：失败分支排在表格前面；403 与其它失败分开说且 403 不摆重试按钮
# （组件内部已保证）；404 仍说「不存在」（越权 / 未注册，后端刻意用 404 藏存在性，不能改口说
# 「无权查看」）。判据按 §2.37：全部是**接线**判据（谁排在谁前面、每支渲染什么、真错误对象有没有
# 传下去），不是「文件里出现过这个名字」。每条配 §2.11 对照组。
# --------------------------------------------------------------------------------------

ADAPTER_CONFIG = FRONTEND / "pages/adapter/AdapterConfigPage.tsx"

#: 失败分支的开口、404 支、通用支、表格支（逐字含缩进）
#: 闸门用 `isLoadingError`（失败且手里没有数据），**不是** `isError`：后者在「已有数据、之后某次 refetch 失败」时
#: 也为真，会把整张表卸载，用户未保存的编辑随组件一起丢掉。
ADAPTER_FAILURE_GATE = "      {isLoadingError ? (\n"
ADAPTER_NOT_FOUND_BRANCH = "        isNotFoundError(error) ? (\n"
ADAPTER_GENERIC_BRANCH = "        ) : (\n"
ADAPTER_TABLE_BRANCH = "      ) : (\n        <ConfigTable"
ADAPTER_REAL_ERROR_RENDERED = "<QueryErrorState error={error} onRetry={() => void refetch()} />"


def _adapter_failure_runs_before_the_table(source: str) -> bool:
    return ADAPTER_FAILURE_GATE in source and "<ConfigTable" in source and (
        source.index(ADAPTER_FAILURE_GATE) < source.index("<ConfigTable")
    )


def _adapter_failure_branches_render_the_right_thing(source: str) -> bool:
    anchors = (ADAPTER_FAILURE_GATE, ADAPTER_NOT_FOUND_BRANCH, ADAPTER_GENERIC_BRANCH, ADAPTER_TABLE_BRANCH)
    if any(anchor not in source for anchor in anchors):
        return False
    not_found_body = _slice_between(source, ADAPTER_NOT_FOUND_BRANCH, ADAPTER_GENERIC_BRANCH)
    generic_body = _slice_between(source, ADAPTER_GENERIC_BRANCH, ADAPTER_TABLE_BRANCH)
    return (
        "t('config.notFound')" in not_found_body
        and "QueryErrorState" not in not_found_body
        and "QueryErrorState" in generic_body
        and "t('config.notFound')" not in generic_body
        and "<ConfigTable" not in not_found_body + generic_body
    )


def _adapter_hands_the_real_error_object(source: str) -> bool:
    """传 null 进去时组件的 `isForbiddenError` 恒 false：403 会被说成「加载失败」并长出重试按钮"""
    return ADAPTER_REAL_ERROR_RENDERED in source and "    isLoadingError,\n    error,\n" in source


class TestAdapterConfigPageTellsEmptyFromForbidden:
    def test_failure_branch_runs_before_the_table(self) -> None:
        source = _read(ADAPTER_CONFIG)
        assert "isNotFoundError" in source and "QueryErrorState" in source
        assert _adapter_failure_runs_before_the_table(source), (
            "失败分支没有排在 `<ConfigTable` 前面：只读成员的 403 会被渲染成「暂无配置项」的空表"
        )

    def test_the_order_criterion_can_see_the_old_shape(self) -> None:
        source = _read(ADAPTER_CONFIG)
        broken = source.replace(ADAPTER_FAILURE_GATE, "      {false ? (\n", 1)
        assert broken != source, "样本没被改动，这条对照组什么都没验"
        assert _adapter_failure_runs_before_the_table(source)
        assert not _adapter_failure_runs_before_the_table(broken)

    def test_the_two_failure_branches_render_the_right_thing(self) -> None:
        """404 说「不存在」且**不**交给通用失败态（那会把后端刻意藏起来的存在性又讲出来）；
        其余失败（含 403）交给通用失败态，且那一支里没有「不存在」"""
        assert _adapter_failure_branches_render_the_right_thing(_read(ADAPTER_CONFIG))

    def test_the_branch_criterion_can_see_an_inverted_condition(self) -> None:
        source = _read(ADAPTER_CONFIG)
        broken = source.replace(ADAPTER_NOT_FOUND_BRANCH, "        !isNotFoundError(error) ? (\n", 1)
        assert broken != source, "样本没被改动，这条对照组什么都没验"
        assert _adapter_failure_branches_render_the_right_thing(source)
        assert not _adapter_failure_branches_render_the_right_thing(broken)

    def test_the_branch_criterion_can_see_a_404_handed_to_the_generic_state(self) -> None:
        source = _read(ADAPTER_CONFIG)
        broken = source.replace("t('config.notFound')", "t('config.emptyMessage')", 1)
        assert broken != source, "样本没被改动，这条对照组什么都没验"
        assert not _adapter_failure_branches_render_the_right_thing(broken)

    def test_the_real_error_object_reaches_the_failure_state(self) -> None:
        assert _adapter_hands_the_real_error_object(_read(ADAPTER_CONFIG))

    def test_the_error_object_criterion_can_see_a_nulled_error(self) -> None:
        source = _read(ADAPTER_CONFIG)
        broken = source.replace(ADAPTER_REAL_ERROR_RENDERED, "<QueryErrorState error={null} onRetry={() => void refetch()} />", 1)
        assert broken != source, "样本没被改动，这条对照组什么都没验"
        assert _adapter_hands_the_real_error_object(source)
        assert not _adapter_hands_the_real_error_object(broken)

    def test_a_failed_refetch_does_not_unmount_the_table_and_lose_unsaved_edits(self) -> None:
        """`isError` 在「有数据之后 refetch 失败」时也为真，会卸载 ConfigTable、丢未保存编辑；
        只有「首次加载就失败」（`isLoadingError`）才该换成失败态"""
        code = re.sub(r"\{/\*.*?\*/\}", "", _read(ADAPTER_CONFIG), flags=re.S)  # 解释性的 JSX 注释里会提到旧名字
        assert "isLoadingError" in code
        assert not re.search(r"\bisError\b", code), "页面里仍在用 isError：refetch 失败会卸载表格"

    def test_the_refetch_criterion_can_see_the_old_isError_gate(self) -> None:
        code = re.sub(r"\{/\*.*?\*/\}", "", _read(ADAPTER_CONFIG), flags=re.S)
        broken = code.replace("isLoadingError", "isError")
        assert broken != code, "样本没被改动，这条对照组什么都没验"
        assert re.search(r"\bisError\b", broken)
        assert not re.search(r"\bisError\b", code)

    def test_the_query_is_still_gated_on_the_adapter_supporting_configuration(self) -> None:
        """加失败分支时不能把「适配器不支持配置」那一支改坏：该查询只在 `has_config` 时才发"""
        source = _read(ADAPTER_CONFIG)
        assert "enabled: adapterInfo.has_config" in source
        assert source.index("if (!adapterInfo.has_config) {") < source.index(ADAPTER_FAILURE_GATE)

    @pytest.mark.parametrize("locale", ("zh-CN", "en-US"))
    def test_both_locales_carry_the_not_found_string(self, locale: str) -> None:
        data = json.loads(_read(FRONTEND / f"locales/{locale}/adapter.json"))
        assert data["config"]["notFound"].strip()
        # 与既有键并存，没有顺手删掉别的
        assert data["config"]["emptyMessage"] and data["config"]["notSupportedTitle"]
