"""全路由权限与租户作用域契约

## 这条测试要防的是什么

多租户改造把"能登录后台 == 是平台方管理员"这个隐含前提废掉了，但**每一个既有端点
都是在那个前提下写的**。历轮修的十几处全是撞上才发现的：工作区 72 个端点、
CC 预设的凭据裸奔、通用配置写入的归属、账号管理的平台接管链……
形态每次都一样 —— 一族端点一路加下来，没有任何一个带作用域，而且没有任何运行时信号。

`tests/test_workspace_tenant_scope.py` 里那条 `TestNoEndpointEscapesTheGuard`
把这件事钉在了 `routers/workspaces.py` 一个文件上。本模块把同一做法推广到**全部路由**：

> 路由表里的每个端点都必须在下面的登记表里，落进四类之一；
> **新增端点没登记就测试失败**。

所以这条测试的价值不在于它今天断言了什么，而在于它让"悄悄加一个没有作用域的端点"
这件事从"没人会发现"变成"CI 直接红"。

## 四类

1. `PLATFORM_ONLY` —— 平台方专属。宿主资源、容器生命周期、跨租户数据、
   能读走凭据、能写服务端可信代码。结构要求：`@require_role(Role.Super)`、
   `Depends(require_platform_owner())`，或（WebSocket 用不了装饰器时）函数体内比 `Role.Super`。
2. `TENANT_SCOPED` —— 租户作用域。结构要求：至少 `Role.Admin`，
   且（可经模块内的辅助函数间接）走到一处作用域守卫。**越权一律 404 而非 403**，
   这一条由各守卫自己的测试钉住（见 `assert_instance_accessible` 等的 docstring）。
3. `TENANT_AGNOSTIC` —— 本身与租户无关：纯静态/元数据、登录与自身信息、
   以及**全体共用一份的共享库的只读面**（技能库、素材库正文）。
   这一类没有可断言的结构特征，登记本身就是人工判定的落点。
4. `SIGNATURE_SCOPED` —— 拿不到 `Authorization` 头、改用**签名**表达作用域的端点。
   目前只有聊天上传文件的取件路由：它要被 `<img src>` 直接加载，
   授权发生在签发令牌的那条接口上（`/common/uploads-access-token`，走
   `assert_channel_accessible`），取件本身只校验签名。结构要求：端点源码里
   真的调用了签名校验函数（`SIGNATURE_VERIFIERS`）。
5. `MACHINE_TOKEN_SCOPED` —— 调用方是 NA 自己起的容器而不是人，作用域由**它专属的令牌**
   唯一确定。目前只有 CC 工作区的模型 API 中继（`routers/cc_relay.py`）：容器里的
   `claude` CLI 拿工作区专属令牌请求，令牌里编着工作区 ID，端点据此取出该工作区生效的
   模型预设去转发。这一类没有用户身份可言，结构要求是端点源码里真的调用了令牌解析函数
   （`MACHINE_TOKEN_RESOLVERS`）。
6. `IDENTITY_SPECIFIC` —— 需要更细的身份判定：不只看调用方角色，
   还要看**目标对象**的归属与等级。目前只有 `routers/user_manager.py`。
   结构要求：走 `assert_managed_user_accessible`。

## 第五个桶是有意为之

`UNSCOPED_TODO` 装的是**审计过、确认仍缺租户作用域、本轮没做**的端点，每条带一句理由。
把它们登记成 `TENANT_SCOPED` 会在测试里固化一个谎；不登记又会让契约测试失去意义。
所以单列一桶：数量会随修复减少，**只增不减就是回归**（`test_todo_bucket_does_not_grow`）。

## 守卫的间接调用

判定不硬编码辅助函数名单：先扫出模块内所有函数，标记其源码含守卫名的，
再让端点"调用了这样的函数"也算数（两层）。`workspace_kb._get_document_or_404`、
`chat_channel.visible_channels`、`commands._ensure_command_state_writable`、
`timers._accessible_item_or_404` 都是这样被认出来的 ——
换个写法收口守卫不会让这条测试误报，而真的绕过守卫会。
"""

import importlib
import inspect
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

import pytest

from nekro_agent.services.user.role import Role

# 全部挂载在 /api 下的路由模块，与 `routers/__init__.py:mount_api_routes` 一一对应。
# 插件路由是热挂载的动态路由，不在本表内（它们由插件隔离机制单独管辖）。
ROUTER_MODULES: Sequence[str] = (
    "adapters", "affection", "cc_model_presets", "chat_channel", "commands", "common",
    "config", "dashboard", "deployment", "email", "events", "kb_library", "logs", "mcp",
    "cc_relay",
    "plugin_editor", "plugins", "presets", "resources", "restart", "rpc", "sandbox",
    "skills", "space_cleanup", "timers", "user", "user_manager", "webhook",
    "workspace_kb", "workspaces",
    "console.containers", "console.health", "console.identity", "console.instances",
    "console.model_groups",
    "console.napcat_orphans",
    "console.network_rules",
    "console.ops_actions", "console.overview", "console.quota", "console.tenants",
    "console.usage",
    "cloud.announcement", "cloud.auth", "cloud.favorites", "cloud.plugins_market",
    "cloud.presets_market", "cloud.telemetry",
)

# 作用域守卫。新增守卫要加进来，否则用它的端点会被判成未防护。
SCOPE_GUARDS: Set[str] = {
    "assert_instance_accessible",
    "assert_channel_accessible",
    "assert_workspace_accessible",
    "assert_tenant_accessible",
    "assert_managed_user_accessible",
    "resolve_instance_scope",
    "resolve_console_identity",
    # 用量流水的作用域出口。它内部就是 `assert_instance_accessible` +
    # `assert_tenant_accessible` + `get_tenant_scope` 三条现成守卫的收口，
    # 单列一个名字是为了让 `USAGE_SCOPE_GUARDS` 能对这一族提出更强的要求。
    "resolve_usage_scope",
    "accessible_instance_keys",
    "accessible_workspace_ids",
    "get_tenant_scope",
    "require_tenant_admin",
    # 技能库的作用域出口。它就是 `get_tenant_scope` 的一层薄封装，
    # 单列一个名字是为了让 `SKILL_LIBRARY_SCOPE_GUARDS` 能对技能这一族提出更强的要求。
    "skill_scope_of",
}

# 技能库那一族**额外**要求的守卫：光"至少 Role.Admin"是不够的（那正是租户管理员的等级），
# 端点必须真的按当前身份解析出一个技能库作用域。
SKILL_LIBRARY_SCOPE_GUARDS: Set[str] = {"skill_scope_of"}

# 有意对租户**普通用户**也开放的读接口：读的是"我自己属于哪个租户、同事有谁"，
# 作用域仍由 `assert_tenant_accessible` 收在自己租户内。把它们要求成 Role.Admin
# 会让只读成员连自己团队都看不到。
#
# `dashboard` 一族同理，而且更硬：它是前端的默认落地页（`Navigate to="/dashboard"`，
# 路由本身没有角色守卫），任何登录身份进后台第一眼看的就是它。要求 Role.Admin
# 等于把只读成员的首页变成 403。作用域由 `dashboard.resolve_dashboard_scope` 收在
# 自己名下实例内，七处聚合查询无一例外。
TENANT_READABLE_BY_MEMBERS: Set[Tuple[str, str, str]] = {
    ("console.tenants", "GET", "/tenants/{tenant_id}"),
    ("console.tenants", "GET", "/tenants/{tenant_id}/members"),
    ("dashboard", "GET", "/dashboard/overview"),
    ("dashboard", "GET", "/dashboard/trends"),
    ("dashboard", "GET", "/dashboard/ranking"),
    ("dashboard", "GET", "/dashboard/distributions"),
    ("dashboard", "GET", "/dashboard/stats/stream"),
}

PLATFORM_OWNER_DEP = "require_platform_owner.<locals>.dependency"
TENANT_ADMIN_DEP = "require_tenant_admin.<locals>.dependency"


@dataclass(frozen=True)
class RouteFacts:
    module: str
    method: str
    path: str
    name: str
    min_role: Optional[Role]
    has_owner_dep: bool
    has_tenant_admin_dep: bool
    parameter_names: Tuple[str, ...]
    inline_super: bool
    reaches_guard: bool
    verifies_signature: bool
    resolves_machine_token: bool


def _min_role_of(endpoint) -> Optional[Role]:
    """读出 `require_role` 装饰器闭包里捕获的等级

    走闭包而不是扫源码：装饰器换写法时这里会直接失效并让测试红，而不是安静地返回 None。
    """
    try:
        return inspect.getclosurevars(endpoint).nonlocals.get("min_role")
    except TypeError:
        return None


def _has_dependency(endpoint, qualname: str) -> bool:
    try:
        signature = inspect.signature(inspect.unwrap(endpoint))
    except (TypeError, ValueError):
        return False
    return any(
        getattr(getattr(parameter.default, "dependency", None), "__qualname__", "") == qualname
        for parameter in signature.parameters.values()
    )


def _parameter_names(endpoint) -> Tuple[str, ...]:
    try:
        return tuple(inspect.signature(inspect.unwrap(endpoint)).parameters)
    except (TypeError, ValueError):
        return ()


def _source_of(function) -> str:
    try:
        return inspect.getsource(inspect.unwrap(function))
    except (OSError, TypeError):
        return ""


def _guarded_helpers(module) -> Set[str]:
    """模块内哪些函数自己走到了守卫

    两层展开：第一层是源码直接含守卫名的函数，第二层是调用了第一层函数的函数。
    足够覆盖 `_get_document_or_404 -> assert_workspace_accessible` 这种收口，
    又不至于把整个调用图跑一遍。
    """
    functions = {
        name: _source_of(member)
        for name, member in vars(module).items()
        if inspect.isfunction(member) and getattr(member, "__module__", "") == module.__name__
    }
    direct = {name for name, source in functions.items() if SCOPE_GUARDS & _identifiers(source)}
    indirect = {name for name, source in functions.items() if direct & _identifiers(source)}
    return direct | indirect


def _identifiers(source: str) -> Set[str]:
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", source))


def describe_routes() -> List[RouteFacts]:
    facts: List[RouteFacts] = []
    for dotted in ROUTER_MODULES:
        module = importlib.import_module(f"nekro_agent.routers.{dotted}")
        helpers = _guarded_helpers(module)
        for route in module.router.routes:
            endpoint = getattr(route, "endpoint", None)
            if endpoint is None:
                continue
            source = _source_of(endpoint)
            names = _identifiers(source)
            facts.append(
                RouteFacts(
                    module=dotted,
                    method=sorted(getattr(route, "methods", None) or ["WS"])[0],
                    path=route.path,
                    name=inspect.unwrap(endpoint).__name__,
                    min_role=_min_role_of(endpoint),
                    has_owner_dep=_has_dependency(endpoint, PLATFORM_OWNER_DEP),
                    has_tenant_admin_dep=_has_dependency(endpoint, TENANT_ADMIN_DEP),
                    parameter_names=_parameter_names(endpoint),
                    inline_super="Role.Super" in source,
                    reaches_guard=bool((SCOPE_GUARDS | helpers) & names),
                    verifies_signature=bool(SIGNATURE_VERIFIERS & names),
                    resolves_machine_token=bool(MACHINE_TOKEN_RESOLVERS & names),
                )
            )
    return facts


def is_platform_only(route: RouteFacts) -> bool:
    return route.min_role is Role.Super or route.has_owner_dep or (route.min_role is None and route.inline_super)


def has_write_level_gate(route: RouteFacts) -> bool:
    """够不够"至少租户管理员"

    两种等价写法：`@require_role(Role.Admin)` 装饰器，或
    `Depends(require_tenant_admin())` 依赖（`console/tenants.py` 用的是后者，
    它同样把只读的租户普通用户挡在外面）。
    """
    return route.has_tenant_admin_dep or (route.min_role is not None and route.min_role >= Role.Admin)


def is_tenant_scoped(route: RouteFacts) -> bool:
    return route.reaches_guard and (
        has_write_level_gate(route) or (route.module, route.method, route.path) in TENANT_READABLE_BY_MEMBERS
    )


# ---------------------------------------------------------------------------
# 登记表。新增端点必须在这里出现，否则 `test_every_route_is_registered` 失败。
# ---------------------------------------------------------------------------

PLATFORM_ONLY: Set[Tuple[str, str, str]] = {
    ("cc_model_presets", "DELETE", "/cc-model-presets/{preset_id}"),
    ("cc_model_presets", "PATCH", "/cc-model-presets/{preset_id}"),
    ("cc_model_presets", "POST", "/cc-model-presets"),
    ("cc_model_presets", "POST", "/cc-model-presets/fetch-models"),
    ("cc_model_presets", "POST", "/cc-model-presets/test"),
    ("cc_model_presets", "POST", "/cc-model-presets/test-inline"),
    ("cloud.auth", "GET", "/cloud/auth/community-user"),
    ("cloud.favorites", "DELETE", "/cloud/favorites"),
    ("cloud.favorites", "GET", "/cloud/favorites"),
    ("cloud.favorites", "POST", "/cloud/favorites"),
    ("cloud.plugins_market", "DELETE", "/cloud/plugins-market/plugin/{module_name}"),
    ("cloud.plugins_market", "GET", "/cloud/plugins-market/detail/{module_name}"),
    ("cloud.plugins_market", "GET", "/cloud/plugins-market/list"),
    ("cloud.plugins_market", "GET", "/cloud/plugins-market/repo/{module_name}"),
    ("cloud.plugins_market", "GET", "/cloud/plugins-market/user-plugins"),
    ("cloud.plugins_market", "POST", "/cloud/plugins-market/create"),
    ("cloud.plugins_market", "POST", "/cloud/plugins-market/download/{module_name}"),
    ("cloud.plugins_market", "POST", "/cloud/plugins-market/update/{module_name}"),
    ("cloud.plugins_market", "PUT", "/cloud/plugins-market/plugin/{module_name}"),
    ("cloud.presets_market", "GET", "/cloud/presets-market/user-presets"),
    ("commands", "POST", "/commands/execute"),
    ("commands", "POST", "/commands/reset-permission"),
    ("commands", "POST", "/commands/set-permission"),
    ("commands", "POST", "/commands/webui-execute"),
    ("common", "DELETE", "/common/wallpaper/{wallpaper_id}"),
    ("common", "POST", "/common/wallpaper/upload"),
    # 协议端容器的**宿主资源分配**那一半（另一半是租户自助运维，见 PROTOCOL_SELF_SERVICE）：
    # 建/删容器是宿主上的资源，起/停能让一台 Bot 长期停着，而 network-config 的响应里
    # 带着托管连接的 OneBot 访问令牌、写侧还能把协议端的出站地址改到别处。
    ("console.containers", "DELETE", "/instances/{instance_key}/container"),
    ("console.containers", "GET", "/instances/{instance_key}/network-config"),
    ("console.containers", "POST", "/instances/{instance_key}/container"),
    ("console.containers", "POST", "/instances/{instance_key}/container/start"),
    ("console.containers", "POST", "/instances/{instance_key}/container/stop"),
    ("console.containers", "PUT", "/instances/{instance_key}/network-config"),
    ("console.instances", "DELETE", "/instances/{instance_key}"),
    ("console.instances", "PATCH", "/instances/{instance_key}"),
    ("console.instances", "POST", "/instances"),
    ("console.instances", "POST", "/instances/{instance_key}/claim"),
    # 并入待认领协议端：与 `/claim` 是同一件事的另一条出路，会改写账号绑定并搬走会话、消息、
    # 记忆的归属。不登记的话它默认落进 TENANT_SCOPED，那一档只要求「至少 Role.Admin」——
    # 也就是说把它从 Super 降到 Admin 这条测试不会红，而租户管理员正好就是 Admin。
    ("console.instances", "POST", "/instances/{instance_key}/adopt"),
    ("console.instances", "POST", "/instances/{instance_key}/disable"),
    ("console.instances", "POST", "/instances/{instance_key}/enable"),
    # 待认领协议端清单：未认领实例恒归平台方（自动登记时不写 tenant_id），且这份清单
    # 的用途就是"接下来去认领谁"——认领会移动数据归属并改写账号绑定，与认领动作同一档
    ("console.instances", "GET", "/unclaimed-instances"),
    # 孤儿 NapCat 残留的运维出口：容器与宿主目录都是**宿主级**资源，一个都不属于任何租户。
    # 预览也必须是 Super —— 它回显宿主上的容器名、状态与数据目录绝对路径，那是宿主拓扑
    ("console.napcat_orphans", "GET", "/napcat-orphans/"),
    ("console.napcat_orphans", "POST", "/napcat-orphans/cleanup"),
    # 孤儿隔离规则的运维出口：iptables 规则是**宿主级**资源，一条都不属于任何租户，
    # 而清理动作会删掉规则集里的兜底 DROP。预览也必须是 Super —— 它会把链上全部标记、
    # 规则原文、以及同宿主上别的部署的网桥/容器名一并回显，那是宿主拓扑而不是租户资源。
    ("console.network_rules", "GET", "/network-rules/orphans"),
    ("console.network_rules", "POST", "/network-rules/orphans/cleanup"),
    ("console.ops_actions", "POST", "/ops/actions/confirm"),
    ("console.ops_actions", "POST", "/ops/actions/preview"),
    ("console.tenants", "DELETE", "/tenants/{tenant_id}"),
    ("console.tenants", "GET", "/tenants"),
    ("console.tenants", "PATCH", "/tenants/{tenant_id}"),
    ("console.tenants", "POST", "/tenants"),
    ("console.tenants", "PUT", "/tenants/{tenant_id}/instances"),
    # 按组名整体覆盖/删除，不看归属；租户自建组走 `/console/model-groups`
    ("config", "DELETE", "/config/model-groups/{group_name}"),
    ("config", "POST", "/config/model-groups/{group_name}"),
    # 收件箱是平台方自己配的邮箱账号（email 适配器的适配器层配置 `RECEIVE_ACCOUNTS`），
    # 那是个跨租户共享的作用域，且 email 是单实例适配器 —— `DBEmail` 里没有任何一列
    # 能把某封邮件判给某个租户，所以这一族**没有**可收敛的租户作用域，只能是平台方专属。
    # 判据与 `config.py:_assert_config_key_accessible` 对 `system`/`adapter_*` 的判法同源。
    ("email", "GET", "/emails"),
    ("email", "GET", "/emails/{email_id}"),
    ("email", "GET", "/emails/{email_id}/raw-content"),
    ("email", "GET", "/emails/{account_username}/{email_uid}/attachments/{filename}"),
    ("deployment", "GET", "/deployment/agent-version"),
    ("deployment", "GET", "/deployment/backups"),
    ("deployment", "GET", "/deployment/capabilities"),
    ("deployment", "GET", "/deployment/instance"),
    ("deployment", "GET", "/deployment/jobs/{job_id}"),
    ("deployment", "GET", "/deployment/jobs/{job_id}/events"),
    ("deployment", "GET", "/deployment/jobs/{job_id}/logs"),
    ("deployment", "POST", "/deployment/backup"),
    ("deployment", "POST", "/deployment/jobs/{job_id}/cancel"),
    ("deployment", "POST", "/deployment/restore"),
    ("deployment", "POST", "/deployment/update"),
    ("kb_library", "DELETE", "/kb-library/assets/{asset_id}"),
    ("kb_library", "DELETE", "/kb-library/assets/{asset_id}/references/{target_id}"),
    ("kb_library", "POST", "/kb-library/assets"),
    ("kb_library", "POST", "/kb-library/assets/files"),
    ("kb_library", "POST", "/kb-library/assets/{asset_id}/references"),
    ("kb_library", "POST", "/kb-library/assets/{asset_id}/reindex"),
    ("kb_library", "PUT", "/kb-library/assets/{asset_id}"),
    ("kb_library", "PUT", "/kb-library/assets/{asset_id}/bindings"),
    ("kb_library", "PUT", "/kb-library/assets/{asset_id}/references/{target_id}"),
    ("logs", "GET", "/logs"),
    ("logs", "GET", "/logs/download"),
    ("logs", "GET", "/logs/sources"),
    ("logs", "GET", "/logs/stream"),
    ("mcp", "GET", "/mcp/auto-inject"),
    ("mcp", "POST", "/mcp/auto-inject/servers/{server_name}/test"),
    ("mcp", "POST", "/mcp/auto-inject/test"),
    ("mcp", "PUT", "/mcp/auto-inject"),
    ("plugin_editor", "DELETE", "/plugin-editor/files/{file_path:path}"),
    ("plugin_editor", "GET", "/plugin-editor/file/{file_path:path}"),
    ("plugin_editor", "GET", "/plugin-editor/files"),
    ("plugin_editor", "POST", "/plugin-editor/apply"),
    ("plugin_editor", "POST", "/plugin-editor/file/{file_path:path}"),
    ("plugin_editor", "POST", "/plugin-editor/generate"),
    ("plugin_editor", "POST", "/plugin-editor/generate/stream"),
    ("plugin_editor", "POST", "/plugin-editor/template"),
    # 插件族的平台方一侧：全局启停 / 激活策略 / 重载 / 路由重建都是**全进程一份**的动作，
    # 一个进程跑着所有租户的实例，租户点一下就落到所有人头上；
    # 三条路由自省吐的是整个进程的路由表；云端插件的装卸等于写服务端可信代码。
    # 租户改自己的可见范围走 `/plugins/scope/...`（实例/会话层覆盖），不动这些。
    ("plugins", "DELETE", "/plugins/package/{module_name}"),
    ("plugins", "GET", "/plugins/debug-routes"),
    ("plugins", "GET", "/plugins/router-info"),
    ("plugins", "GET", "/plugins/verify-plugin-routes/{plugin_key}"),
    ("plugins", "POST", "/plugins/activation-strategy/{plugin_id}"),
    ("plugins", "POST", "/plugins/package/update/{module_name}"),
    ("plugins", "POST", "/plugins/refresh-routes"),
    ("plugins", "POST", "/plugins/reload"),
    ("plugins", "POST", "/plugins/toggle/{plugin_id}"),
    # 按**部署级社区账号**的发布清单回写 `on_shared`：既漏平台方发布总数，
    # 又是 `on_shared` 的第二个写入口（下载来的副本会被它掰回"已共享"，
    # 从而复活 `sync-to-cloud` / `unshare` 改写平台方公开发布的那条链）。
    ("presets", "POST", "/presets/refresh-shared-status"),
    ("resources", "DELETE", "/resources/{resource_id}"),
    ("resources", "GET", "/resources"),
    # 悬空绑定的体检与清理（PROJECT_STATE §3 #41）：读写的是整张
    # `workspace_resource_binding`，天然跨工作区跨租户，没有可收敛的作用域。
    ("resources", "GET", "/resources/binding-health"),
    ("resources", "GET", "/resources/secret-health"),
    ("resources", "GET", "/resources/templates"),
    ("resources", "GET", "/resources/{resource_id}"),
    ("resources", "PATCH", "/resources/{resource_id}"),
    ("resources", "POST", "/resources"),
    ("resources", "POST", "/resources/binding-purge"),
    ("resources", "POST", "/resources/secret-purge"),
    ("resources", "POST", "/resources/secret-recover"),
    ("resources", "POST", "/workspaces/{workspace_id}/resources/check-bind"),
    ("resources", "POST", "/workspaces/{workspace_id}/resources/{resource_id}"),
    ("restart", "GET", "/restart/capability"),
    ("restart", "POST", "/restart"),
    ("sandbox", "GET", "/sandbox/log-content"),
    ("sandbox", "GET", "/sandbox/logs"),
    ("sandbox", "GET", "/sandbox/models"),
    ("sandbox", "GET", "/sandbox/stats"),
    # 技能库的平台方一侧（另一侧是租户自有库，见 SKILL_LIBRARY_TENANT_REACHABLE）：
    # 三条 git 拉取 + 仓库订阅是**由宿主上的 git 进程发起的任意 URL 出站**——
    # `file://` 能把宿主上任意可读目录复制进技能库，`ext::` 传输干脆是"让 git 执行一条命令"，
    # `http://127.0.0.1:8021/...` 则是拿 NA 自己的网络身份打内网。这是能力边界（403），
    # 与"这条技能是谁的"（404）不是一回事，所以它留在这一档而不是靠归属判定。
    ("skills", "DELETE", "/skills/repos/{repo_name}"),
    ("skills", "POST", "/skills/clone"),
    ("skills", "POST", "/skills/pull"),
    ("skills", "POST", "/skills/repos/subscribe"),
    ("skills", "POST", "/skills/repos/{repo_name}/pull"),
    # 只有平台方能决定自己的技能共享给谁；租户自建技能恒为私有，没有租户分支可开
    ("skills", "PUT", "/skills/visibility"),
    ("space_cleanup", "GET", "/space-cleanup/cleanup/progress/{task_id}"),
    ("space_cleanup", "GET", "/space-cleanup/cleanup/result/{task_id}"),
    ("space_cleanup", "GET", "/space-cleanup/disk-info"),
    ("space_cleanup", "GET", "/space-cleanup/scan/load-cache"),
    ("space_cleanup", "GET", "/space-cleanup/scan/progress"),
    ("space_cleanup", "GET", "/space-cleanup/scan/result"),
    ("space_cleanup", "GET", "/space-cleanup/scan/status"),
    ("space_cleanup", "POST", "/space-cleanup/cleanup/start"),
    ("space_cleanup", "POST", "/space-cleanup/scan/start"),
    ("user", "DELETE", "/user/delete"),
    ("user_manager", "POST", "/user-manager/create"),
    ("workspaces", "GET", "/workspaces/{workspace_id}/sandbox/image/check"),
    ("workspaces", "POST", "/workspaces"),
    ("workspaces", "DELETE", "/workspaces/{workspace_id}"),
    # `dynamic-skills/{dir_name}/promote` 曾在这里，理由是"晋升 == 往全局库写服务端可信代码"。
    # 技能库有了归属之后这条理由只剩一半：租户的晋升落点是它自己的技能库，
    # 与它用编辑器写出来的东西同一个目录、同一个可达范围。现已降到 `Role.Admin` + 工作区作用域，
    # 见 SKILL_LIBRARY_TENANT_REACHABLE 的注释。
    ("workspaces", "POST", "/workspaces/{workspace_id}/mcp/servers/{server_name}/test"),
    ("workspaces", "POST", "/workspaces/{workspace_id}/sandbox/image/pull/stream"),
    ("workspaces", "POST", "/workspaces/{workspace_id}/sandbox/rebuild"),
    ("workspaces", "POST", "/workspaces/{workspace_id}/sandbox/restart"),
    ("workspaces", "POST", "/workspaces/{workspace_id}/sandbox/start"),
    ("workspaces", "POST", "/workspaces/{workspace_id}/sandbox/stop"),
    ("workspaces", "WS", "/workspaces/{workspace_id}/sandbox/terminal"),
}

IDENTITY_SPECIFIC: Set[Tuple[str, str, str]] = {
    ("user_manager", "GET", "/user-manager/list"),
    ("user_manager", "GET", "/user-manager/{user_id}"),
    ("user_manager", "PUT", "/user-manager/{user_id}"),
    ("user_manager", "DELETE", "/user-manager/{user_id}"),
    ("user_manager", "POST", "/user-manager/{user_id}/ban"),
    ("user_manager", "POST", "/user-manager/{user_id}/prevent-trigger"),
    ("user_manager", "POST", "/user-manager/{user_id}/reset-password"),
}

TENANT_AGNOSTIC: Set[Tuple[str, str, str]] = {
    # ── 静态元数据 / 编译进代码的清单 ──
    ("adapters", "GET", "/adapters/list"),
    ("adapters", "GET", "/adapters/{adapter_key}/docs"),
    ("adapters", "GET", "/adapters/{adapter_key}/info"),
    ("adapters", "GET", "/adapters/{adapter_key}/status"),
    ("mcp", "GET", "/mcp/registry"),
    ("mcp", "GET", "/mcp/constraints"),
    # `/config/keys` 与 `/config/info/{config_key}` 曾登记在这里（"编译进代码的清单/静态元数据"），
    # 两条都不成立：
    # - `/config/keys` 的清单是运行时长出来的，`ConfigManager` 会把动态加载到的
    #   `channel_config_*` / `instance_override_*` 登记进去，实测租户能从中枚举出别人的
    #   实例标识与 chat_key；现已按归属裁剪。
    # - `/config/info/{config_key}` 的 `config_key` 能编码实例/频道作用域，响应里的
    #   `config_file_path` 会把标识拼出来，而取实例这个动作本身就会 `dump_config()`
    #   在磁盘上建出覆盖文件；现已接读侧作用域校验。
    # 两条都按默认的 TENANT_SCOPED 要求。
    ("config", "GET", "/config/model-types"),
    ("config", "GET", "/config/version"),
    ("config", "GET", "/config/ui-onboarding-guide/status"),
    ("commands", "GET", "/commands/completions"),
    ("commands", "GET", "/commands/tools"),
    # 用量筛选器的固定取值（场景/收尾形态枚举 + 两个上限常量）。它一行数据都不读，
    # 也没有任何可越权的目标标识——放进默认桶反而会因为"没接守卫"而误报。
    # 与它同族的三条**读数据**的端点全部按 `USAGE_CONSOLE_TENANT_REACHABLE` 钉死。
    ("console.usage", "GET", "/usage/enums"),
    # ── 登录与自身信息 ──
    ("user", "POST", "/user/login"),
    ("user", "POST", "/user/register"),
    ("user", "GET", "/user/me"),
    ("user", "GET", "/user/me/identity"),
    # `PUT /user/password` 只改 `_current_user` 自己的口令，没有任何可越权的目标标识，
    # 因此与 `/user/me` 同档。
    #
    # ⚠️ 它此前登记在 `PLATFORM_ONLY` 里，而且**结构断言是通过的** —— 但那是个假阳性：
    # `is_platform_only()` 的第三条分支是「没有 `@require_role` 且源码里出现过
    # `Role.Super`」，而当时函数体里那句 `Role.Super` 属于一条**授权旁路判断**
    # （`... and _current_user.perm_level < Role.Super: raise`），它非但不是门槛，
    # 恰恰是"低于 Super 才拒"的那一半。于是登记表宣称"租户碰不到这条端点"，
    # 实际上任何登录身份都能调，且不校验旧口令。
    #
    # 教训：`inline_super` 是文本判据，只说明"这个函数提到过 Role.Super"，
    # 不说明"Role.Super 是它的准入门槛"。往 `PLATFORM_ONLY` 里放没有装饰器、
    # 也没有 `require_platform_owner()` 的端点时，要自己确认那句 `Role.Super`
    # 到底是门槛还是旁路。
    ("user", "PUT", "/user/password"),
    # ── 技能库里唯一真正与租户无关的一档：**内置**技能 ──
    #
    # ⚠️ 这个注释块此前写的是「共享库的只读面：库本身就是全体共用的一份，没有'每租户一份'
    # 这回事」，下面挂着技能库的**全部 13 个读端点**。那句话是错的，而且错得很贵：
    # 技能正文是给 AI 看的工作流文本，平台方往里面写运维流程与内部地址，
    # 而租户管理员正好就是 `Role.Admin` —— `GET /skills/tree` 拿目录、
    # `GET /skills/content` 拿正文，两跳读走平台方整座库。
    # 现在那 13 条里的 9 条读 + 5 条写落进 `SKILL_LIBRARY_TENANT_REACHABLE`（带作用域）。
    #
    # 留在这一档的只有内置技能这 4 条：它们读的是**源码树**里的
    # `nekro_agent/builtin_skills/`，随代码发布、在公开仓库里就能读到，
    # 没有任何"某个租户的"或"平台方私有的"东西可泄露；
    # 而且 `auto-inject` 的默认三条全是内置技能，判成私有会让每个租户的新工作区一条都没有。
    ("skills", "GET", "/skills/builtin"),
    ("skills", "GET", "/skills/builtin/{name}"),
    ("skills", "GET", "/skills/builtin/{name}/doc"),
    ("skills", "GET", "/skills/builtin/{name}/file"),
    ("kb_library", "GET", "/kb-library/assets/{asset_id}/fulltext"),
    ("kb_library", "GET", "/kb-library/assets/{asset_id}/raw"),
    ("kb_library", "GET", "/kb-library/assets/{asset_id}/references"),
    # 插件目录同理：插件是装在这一个进程里的一份，不存在"每租户一份"。
    # 三条读接口都不含配置取值（详情只有方法/webhook/路由形状），
    # 加载失败条目里的宿主路径与堆栈已按身份裁掉（`plugins.visible_plugin_meta`）。
    # 各插件在**某个实例**下的生效状态是另一条端点 `/plugins/scope/{instance_key}`，它带作用域。
    ("plugins", "GET", "/plugins/list"),
    ("plugins", "GET", "/plugins/detail/{plugin_id}"),
    ("plugins", "GET", "/plugins/docs/{plugin_id}"),
    # ── 上游社区数据，本部署没有租户维度 ──
    ("cloud.announcement", "GET", "/cloud/announcement/latest"),
    ("cloud.announcement", "GET", "/cloud/announcement/updated-at"),
    ("cloud.announcement", "GET", "/cloud/announcement/{announcement_id}"),
    ("cloud.telemetry", "GET", "/cloud/telemetry/community-stats"),
    # Star 状态是两个公开仓库名列表 + 一个布尔值，没有账号标识也没有租户维度；
    # 它驱动的主题/壁纸门槛是整部署共用的一档，与上面的公告同源。
    ("cloud.auth", "GET", "/cloud/auth/github-stars"),
}

# 拿不到 Authorization 头、改用签名表达作用域的端点。
#
# `rpc_exec` 与 `webhook` 形式上也属于这一类（容器令牌 / 端点密钥），但它们的令牌
# 不是按租户资源签的，作用域仍是待办，因此留在 `UNSCOPED_TODO` 里不动。
SIGNATURE_SCOPED: Set[Tuple[str, str, str]] = {
    ("common", "GET", "/common/uploads/{chat_key}/{filename}"),
}

# 判定 `SIGNATURE_SCOPED` 的结构特征：端点必须真的调用签名校验。
# 校验函数换名字会让这条断言直接红，而不是安静地放行一个不再验签的端点。
SIGNATURE_VERIFIERS: Set[str] = {"verify_upload_access_token"}

# 调用方是容器而不是人、作用域由容器专属令牌唯一确定的端点。
#
# 与 `rpc_exec` / `webhook` 的差别正是它们留在 `UNSCOPED_TODO` 里的那一条：
# 这里的令牌是**按工作区签发**的，令牌本身就是作用域，调用方给不出任何可越权的标识
# （没有 `workspace_id` 参数可填）。
MACHINE_TOKEN_SCOPED: Set[Tuple[str, str, str]] = {
    ("cc_relay", "POST", "/cc-relay/{upstream_path:path}"),
}

# 判定 `MACHINE_TOKEN_SCOPED` 的结构特征：端点必须真的去解析并校验那把令牌。
MACHINE_TOKEN_RESOLVERS: Set[str] = {"resolve_relay_grant"}

# 审计过、确认仍缺租户作用域、本轮**没有**处理的端点。每条一句理由。
# 这不是"允许清单"，是待办清单：只许变短。
UNSCOPED_TODO: Dict[Tuple[str, str, str], str] = {
    ("common", "GET", "/common/wallpaper/list"): "全局壁纸清单，无租户维度但当前无角色校验",
    ("common", "GET", "/common/wallpaper/{filename}"): "有意不鉴权，静态图片",
    ("presets", "POST", "/presets/upload-avatar"): "头像上传落在全局目录，未按作用域分目录",
    ("rpc", "POST", "/ext/rpc_exec"): "沙盒回调入口，凭容器令牌鉴权而非用户身份；边界另有专测",
    ("webhook", "POST", "/webhook/{endpoint}"): "对外 webhook，按端点密钥鉴权而非用户身份",
}

# ---------------------------------------------------------------------------
# PROJECT_STATE §3 #29：`workspace_kb` 与 `resources` 两个模块的整表登记
#
# 这两族历史上是"整族零租户校验"，而且**同一个洞被发现了两次**（#26 修 workspaces.py
# 时漏了它们）。麻烦在于 `TENANT_SCOPED` 是**默认桶**：不登记也能通过，于是上面那套
# 契约对这两个模块只能保证"接了某个守卫"，保证不了"这一族到底有几个端点、
# 有没有谁被悄悄挪进/挪出租户可达的范围"。
#
# 所以这里把两个模块的**分类结果整体钉死**：新增、删除、或者换了类别，
# 都必须在下面这张表里显式改一次。行为一侧（跨租户 404 / 能力边界 403 /
# 平台方正向对照）在 `tests/test_kb_and_resource_tenant_scope.py`。
# ---------------------------------------------------------------------------

ISSUE_29_MODULES: Set[str] = {"workspace_kb", "resources"}

KB_AND_RESOURCE_TENANT_SCOPED: Set[Tuple[str, str, str]] = {
    ("workspace_kb", "GET", "/workspaces/{workspace_id}/kb/documents"),
    ("workspace_kb", "GET", "/workspaces/{workspace_id}/kb/tree"),
    ("workspace_kb", "GET", "/workspaces/{workspace_id}/kb/documents/{document_id}"),
    ("workspace_kb", "POST", "/workspaces/{workspace_id}/kb/documents"),
    ("workspace_kb", "POST", "/workspaces/{workspace_id}/kb/files"),
    ("workspace_kb", "PUT", "/workspaces/{workspace_id}/kb/documents/{document_id}"),
    ("workspace_kb", "DELETE", "/workspaces/{workspace_id}/kb/documents/{document_id}"),
    ("workspace_kb", "GET", "/workspaces/{workspace_id}/kb/documents/{document_id}/raw"),
    ("workspace_kb", "GET", "/workspaces/{workspace_id}/kb/documents/{document_id}/fulltext"),
    ("workspace_kb", "POST", "/workspaces/{workspace_id}/kb/documents/{document_id}/reindex"),
    ("workspace_kb", "POST", "/workspaces/{workspace_id}/kb/reindex"),
    ("workspace_kb", "POST", "/workspaces/{workspace_id}/kb/search"),
    ("workspace_kb", "GET", "/workspaces/{workspace_id}/kb/documents/{document_id}/references"),
    ("workspace_kb", "POST", "/workspaces/{workspace_id}/kb/documents/{document_id}/references"),
    ("workspace_kb", "PUT", "/workspaces/{workspace_id}/kb/documents/{document_id}/references/{target_id}"),
    ("workspace_kb", "DELETE", "/workspaces/{workspace_id}/kb/documents/{document_id}/references/{target_id}"),
    ("workspace_kb", "GET", "/workspaces/{workspace_id}/kb/documents/{document_id}/source-file"),
    # 资源侧租户能碰的只有这三条：看自己工作区上绑了什么、解绑、重排。
    # "绑定"与"检查绑定冲突"在 PLATFORM_ONLY 里——绑定 = 把平台方的生产 SSH 私钥
    # 交给那个工作区里的 Claude Code，那是能力边界（403）而不是存在性问题（404）。
    ("resources", "GET", "/workspaces/{workspace_id}/resources"),
    ("resources", "DELETE", "/workspaces/{workspace_id}/resources/{resource_id}"),
    ("resources", "PUT", "/workspaces/{workspace_id}/resources/reorder"),
}

# #29 原文记的是"17 + 4"，那个 4 数的是**路径**；
# `/workspaces/{workspace_id}/resources/{resource_id}` 上挂着 POST（绑定）与
# DELETE（解绑）两个方法，按端点算是 5，合计 22。
ISSUE_29_WORKSPACE_SCOPED_ENDPOINT_COUNT = 22


# ---------------------------------------------------------------------------
# 协议端自助运维（A2）：`console.containers` / `console.health` 的整表登记
#
# 这两族原本 14 个端点全是 `Role.Super`，于是「QQ 掉线 / 要重新扫码」这类最高频的运营
# 事件对定制版客户是一个红点加零个按钮。R19 明写过租户**最多能「重启我的 NapCat」**，
# 于是这里按"宿主资源分配 vs 我自己那台 Bot 的日常运维"切成两档。
#
# 麻烦与 #29 同形：`TENANT_SCOPED` 是**默认桶**，把 `create` / `delete` /
# `network-config` 从 Super 降到 Admin 不会让上面任何一条断言变红（它们照样"接了守卫、
# 等级够 Admin"）。所以这里把两族的分档结果**整体钉死**：任何一条挪档、新增、删除，
# 都必须在下面这张表上显式改一次。
# ---------------------------------------------------------------------------

PROTOCOL_SELF_SERVICE_MODULES: Set[str] = {"console.containers", "console.health"}

# 租户（`Role.Admin` + 实例作用域）能达到的那一档，一条不多一条不少
PROTOCOL_SELF_SERVICE_TENANT_REACHABLE: Set[Tuple[str, str, str]] = {
    # 读容器状态：不给它，协议端面板对租户就是一片空白，连"要不要重启"都判断不了
    ("console.containers", "GET", "/instances/{instance_key}/container"),
    # R19 逐字写过的那一条
    ("console.containers", "POST", "/instances/{instance_key}/container/restart"),
    # 掉登录态时重启治不好，扫码/快速登录是自愈的另一半
    ("console.containers", "GET", "/instances/{instance_key}/container/qrcode"),
    ("console.containers", "POST", "/instances/{instance_key}/container/quick-login"),
    # "我的 Bot 现在怎么了"的唯一权威回答，按可见实例过滤后开放
    ("console.health", "GET", "/health/instances"),
    ("console.health", "GET", "/health/events"),
    # 租户给自己实例配的告警 Webhook 得能试发，否则一个静默失效的地址与没配是同一回事
    ("console.health", "POST", "/health/test-notification"),
}


# ---------------------------------------------------------------------------
# 技能库（A4）：`skills` 模块的整表登记
#
# 这一族是"默认桶太松"这条缝的**教科书案例**：13 个读端点被一句"共享库的只读面"
# 塞进 `TENANT_AGNOSTIC`，剩下的写端点全是 `Role.Super`。于是两头都错 ——
# 租户读得到平台方技能的**全文**（技能正文是给 AI 看的运维工作流），
# 又一个字都写不了（`需求文档 §R8`「可以自己制作并使用 skills」对定制版客户不成立）。
#
# 光把它们挪进默认桶 `TENANT_SCOPED` 是不够的：那一档只要求「至少 `Role.Admin` +
# 接了某个守卫」，而 `require_tenant_admin` 这类名字出现在源码里就算数。
# 所以这里额外加两条：
#
# 1. 租户可达的集合**逐条钉死**（新增/删除/挪档都得在下表显式改一次）；
# 2. 每一条都必须真的走到 `SKILL_LIBRARY_SCOPE_GUARDS` —— 也就是按当前身份解析出
#    技能库作用域。摘掉那一行、退回"直接读全局目录"，这条断言立刻红。
#
# 行为一侧（租户读平台方私有技能 404 / 读 shared 拿得到 / 跨租户 404 / 平台方正向对照）
# 在 `tests/test_skills_tenant_scope.py`。
# ---------------------------------------------------------------------------

SKILL_LIBRARY_MODULES: Set[str] = {"skills"}

SKILL_LIBRARY_TENANT_REACHABLE: Set[Tuple[str, str, str]] = {
    # ── 读：全部按作用域过滤，平台方私有技能对租户不可见（404，与不存在不可区分）──
    ("skills", "GET", "/skills"),
    ("skills", "GET", "/skills/all"),
    ("skills", "GET", "/skills/auto-inject"),
    ("skills", "GET", "/skills/content"),
    ("skills", "GET", "/skills/dir"),
    ("skills", "GET", "/skills/file"),
    ("skills", "GET", "/skills/repos"),
    ("skills", "GET", "/skills/tree"),
    ("skills", "GET", "/skills/{name}/readme"),
    # ── 写：只写得到自己那一层库（落点走 `skill_scope.writable_root()` 一个出口）──
    ("skills", "POST", "/skills"),
    ("skills", "PUT", "/skills/file"),
    ("skills", "DELETE", "/skills/{name}"),
    # 自动注入清单跟着作用域走，不再是全局单值
    ("skills", "PUT", "/skills/auto-inject"),
    # 改前是 `Role.Super`，但底层 `sync_skill_to_all_workspaces` 不带归属过滤 ——
    # 也就是说它能往**别的租户**的工作区目录里写文件。现在按作用域过滤工作区
    ("skills", "POST", "/skills/sync-to-workspaces"),
}


# ---------------------------------------------------------------------------
# 身份与域（本轮）：`console.identity` 的整表登记
#
# 这一族是本轮**最严重的那条风险面**的操作面：域配置写错一次，租户管理员就能把
# 别的租户的实例拉进自己的域，于是当场读到对方的记忆与好感；账号合并写错一次，
# 两个不相干的人的好感被并在一起。
#
# 光落进默认桶 `TENANT_SCOPED` 是不够的 —— 那一档只要求「至少 `Role.Admin` +
# 源码里出现过某个守卫名」，而租户管理员正好就是 `Role.Admin`。所以这里额外钉三条：
#
# 1. 这一族**全部**是租户可达的（没有平台方专属那一档：域与合并本来就是租户要自助配的），
#    集合逐条钉死，新增/删除/挪档都得在下表显式改一次；
# 2. 每一条都必须真的走到**实例或组的归属守卫**（`IDENTITY_SCOPE_GUARDS`），
#    而不只是"名字里有 tenant"；
# 3. 每一条的 `min_role` 都必须是 `Role.Admin` —— 掉到 `Role.User` 就等于让只读成员
#    改域配置，那是一次数据可见性的改动，不是查看。
#
# 行为一侧（跨租户 404 / 模型层再拒一次 / 撤销只能撤自己域内的）在
# `tests/test_identity_console_api.py`。
# ---------------------------------------------------------------------------

IDENTITY_CONSOLE_MODULES: Set[str] = {"console.identity"}

# 这一族**额外**要求的守卫：必须解析出一个真实的归属，
# 而不是像 `get_tenant_scope` 那样只回一个可能为 None 的作用域。
IDENTITY_SCOPE_GUARDS: Set[str] = {"assert_instance_accessible", "assert_tenant_accessible", "get_tenant_scope"}

IDENTITY_CONSOLE_TENANT_REACHABLE: Set[Tuple[str, str, str]] = {
    # ── 域（实例组）：读侧按租户收敛，写侧接口层 + 模型层各一道归属校验 ──
    ("console.identity", "GET", "/identity/domains"),
    ("console.identity", "POST", "/identity/domains"),
    ("console.identity", "PATCH", "/identity/domains/{group_id}"),
    ("console.identity", "DELETE", "/identity/domains/{group_id}"),
    # 本族最高危的一条：把实例编进域。三道门（实例归属、组归属、bind_to_groups）
    ("console.identity", "PUT", "/identity/instances/{instance_key}/domain"),
    # 入域并入存量好感的预览（同一组守卫，先算给操作者看）
    ("console.identity", "GET", "/identity/instances/{instance_key}/domain-join-preview"),
    # ── 账号合并：作用域用"一个你够得着的实例"表达，服务端据此解析出域 ──
    ("console.identity", "GET", "/identity/aliases"),
    ("console.identity", "POST", "/identity/merge/preview"),
    ("console.identity", "POST", "/identity/merge"),
    ("console.identity", "DELETE", "/identity/aliases/{alias_id}"),
    # ── 自助申报审批：申报单按 (域, 单号) 双条件定位，跨域审批拿到的是 404 ──
    ("console.identity", "GET", "/identity/alias-requests"),
    ("console.identity", "POST", "/identity/alias-requests/{request_id}/approve"),
    ("console.identity", "POST", "/identity/alias-requests/{request_id}/reject"),
    # ── 人设孤儿数据：可见 + 可手动清理，不级联删 ──
    ("console.identity", "GET", "/identity/orphans"),
    ("console.identity", "POST", "/identity/orphans/cleanup"),
}


# ---------------------------------------------------------------------------
# 用量与费用（本轮）：`console.usage` 的整表登记
#
# 这一族读的是**账本**：谁在哪个实例上烧了多少 token、折算多少钱。它同时踩两条线 ——
# 跨租户读到就是把别人的经营数据（活跃度、成本、用了什么模型）整份端走；
# 而模型组名那一列若原样吐给租户，`can_view_group`「租户看不到的组根本不出现」
# 这条不变式就从另一扇门被绕开了。
#
# 光落进默认桶 `TENANT_SCOPED` 不够（那一档只要求「至少 `Role.Admin` + 源码里出现过
# 某个守卫名」，而租户管理员正好就是 `Role.Admin`），所以这里额外钉三条：
#
# 1. 租户可达的集合逐条钉死，新增/删除/挪档都得在下表显式改一次；
# 2. 每一条都必须真的走到 `USAGE_SCOPE_GUARDS` —— 也就是按当前身份解析出一个
#    `UsageScope`。摘掉那一行、退回裸的 `DBLLMUsageRecord.filter(...)`，这条断言立刻红；
# 3. 每一条的 `min_role` 都必须至少 `Role.Admin`。
#
# 行为一侧（跨租户 404 / 平台方正向对照 / 模型组名掩码 / 金额算对）在
# `tests/test_llm_usage_metering.py`。
# ---------------------------------------------------------------------------

USAGE_CONSOLE_MODULES: Set[str] = {"console.usage"}

# 这一族**额外**要求的守卫：必须解析出一个真实的 `UsageScope`，
# 而不只是"源码里出现过 tenant 这个词"。
USAGE_SCOPE_GUARDS: Set[str] = {"resolve_usage_scope"}

USAGE_CONSOLE_TENANT_REACHABLE: Set[Tuple[str, str, str]] = {
    ("console.usage", "GET", "/usage/summary"),
    ("console.usage", "GET", "/usage/trend"),
    ("console.usage", "GET", "/usage/records"),
}


REGISTRY: Dict[str, Set[Tuple[str, str, str]]] = {
    "PLATFORM_ONLY": PLATFORM_ONLY,
    "IDENTITY_SPECIFIC": IDENTITY_SPECIFIC,
    "TENANT_AGNOSTIC": TENANT_AGNOSTIC,
    "SIGNATURE_SCOPED": SIGNATURE_SCOPED,
    "MACHINE_TOKEN_SCOPED": MACHINE_TOKEN_SCOPED,
    "UNSCOPED_TODO": set(UNSCOPED_TODO),
}

# 本轮结束时的待办数量。修好一条就把它从 UNSCOPED_TODO 拿掉并调小这个数；
# 变大意味着新增了一个没有作用域的端点，或者某处守卫被摘掉了。
UNSCOPED_TODO_CEILING = 5


@pytest.fixture(scope="module")
def routes() -> List[RouteFacts]:
    return describe_routes()


def _key(route: RouteFacts) -> Tuple[str, str, str]:
    return (route.module, route.method, route.path)


def _categorize(route: RouteFacts) -> str:
    for name, members in REGISTRY.items():
        if _key(route) in members:
            return name
    return "TENANT_SCOPED"


class TestRegistryCoversTheRouteTable:
    """登记表与真实路由表必须一一对应"""

    def test_every_route_is_registered_exactly_once(self, routes: List[RouteFacts]) -> None:
        """一个端点落进两个桶就说明分类含糊，必须先想清楚再登记"""
        duplicated = []
        for route in routes:
            hits = [name for name, members in REGISTRY.items() if _key(route) in members]
            if len(hits) > 1:
                duplicated.append((_key(route), hits))
        assert duplicated == []

    def test_no_stale_registry_entries(self, routes: List[RouteFacts]) -> None:
        """端点被删掉/改路径后登记表要跟着改，否则它会永远掩护一个不存在的契约"""
        live = {_key(route) for route in routes}
        stale = sorted({key for members in REGISTRY.values() for key in members} - live)
        assert stale == []

    def test_unregistered_routes_are_tenant_scoped_by_default(self, routes: List[RouteFacts]) -> None:
        """没登记的端点默认按"必须带租户作用域"要求

        这是整条契约的支点：新加一个吃资源 ID 的端点，如果既没登记成平台方专属、
        也没接守卫，这里直接红。作者要么接上守卫，要么把它明确登记成别的类别 ——
        无论哪种，都是一次**有意识的**决定。
        """
        unguarded = [
            f"{route.module} {route.method} {route.path}"
            for route in routes
            if _categorize(route) == "TENANT_SCOPED" and not is_tenant_scoped(route)
        ]
        assert unguarded == []


class TestCategoryStructuralClaims:
    """每一类的结构断言：登记表说它是什么，代码就得真的是什么"""

    def test_platform_only_routes_really_require_platform_identity(self, routes: List[RouteFacts]) -> None:
        """`Role.Super` / `require_platform_owner()` 是租户永远够不到的门槛

        租户成员的等级被 `TENANT_MAX_PERM_LEVEL` 钉死在 `Role.Admin`，
        所以这一档不需要再做归属判定。
        """
        weakened = [
            f"{route.module} {route.method} {route.path}"
            for route in routes
            if _key(route) in PLATFORM_ONLY and not is_platform_only(route)
        ]
        assert weakened == []

    def test_tenant_scoped_routes_reach_a_guard(self, routes: List[RouteFacts]) -> None:
        missing = [
            f"{route.module} {route.method} {route.path}"
            for route in routes
            if _categorize(route) == "TENANT_SCOPED" and not route.reaches_guard
        ]
        assert missing == []

    def test_tenant_scoped_routes_require_at_least_admin(self, routes: List[RouteFacts]) -> None:
        """只读的租户普通用户（`Role.User`）不该能改自己租户的资源

        工作区的记忆族此前就是这样：连 `@require_role` 都没有，任何登录账号都能清空记忆库。
        """
        too_open = [
            f"{route.module} {route.method} {route.path}"
            for route in routes
            if _categorize(route) == "TENANT_SCOPED"
            and not has_write_level_gate(route)
            and _key(route) not in TENANT_READABLE_BY_MEMBERS
        ]
        assert too_open == []

    def test_signature_scoped_routes_really_verify_a_signature(self, routes: List[RouteFacts]) -> None:
        """签名鉴权的端点必须真的验签

        这一类没有 `@require_role`、没有守卫、没有登录态，唯一挡住越权的就是那一次验签。
        摘掉它，端点会**静默**退回成"知道两个名字就能取任意租户的聊天文件"——
        没有任何运行时信号，图片照样显示。
        """
        by_key = {_key(route): route for route in routes}
        for key in sorted(SIGNATURE_SCOPED):
            assert by_key[key].verifies_signature, key

    def test_machine_token_scoped_routes_really_resolve_the_token(self, routes: List[RouteFacts]) -> None:
        """容器令牌鉴权的端点必须真的解析并校验那把令牌

        这一类同样没有 `@require_role`、没有登录态。摘掉令牌校验，模型 API 中继就变成
        一个**任何人都能用平台方 Key 打上游**的开放代理，而且不会有任何运行时信号。
        """
        by_key = {_key(route): route for route in routes}
        for key in sorted(MACHINE_TOKEN_SCOPED):
            assert by_key[key].resolves_machine_token, key

    def test_identity_specific_routes_check_the_target(self, routes: List[RouteFacts]) -> None:
        """账号管理不能只看调用方：租户管理员本身就是 `Role.Admin`

        改前 `user_manager` 8 个端点只写 `if _current_user.perm_level < Role.Admin`，
        于是租户管理员可以重置平台方口令后直接登录。
        """
        by_key = {_key(route): route for route in routes}
        for key in sorted(IDENTITY_SPECIFIC):
            route = by_key[key]
            assert route.reaches_guard, key


class TestTodoBucketIsABacklogNotAnAllowlist:
    def test_every_todo_entry_carries_a_reason(self) -> None:
        assert [key for key, reason in UNSCOPED_TODO.items() if not reason.strip()] == []

    def test_todo_bucket_does_not_grow(self) -> None:
        """只许变短

        修好一条就从表里删掉并调小上限。变大只有两种可能：
        新增了一个没有作用域的端点，或者某处守卫被摘掉了 —— 两种都要立刻被看见。
        """
        assert len(UNSCOPED_TODO) <= UNSCOPED_TODO_CEILING


class TestProtocolSelfServiceSplitIsPinned:
    """协议端自助运维（A2）：`console.containers` / `console.health` 的整表登记

    与 #29 同形，理由也同形 —— `TENANT_SCOPED` 是默认桶，把 `create` / `delete` /
    `network-config` 从 `Role.Super` 降到 `Role.Admin` 通不过任何一条既有断言。
    这一组把分档结果本身钉死。
    """

    def test_tenant_reachable_set_is_exactly_the_registered_one(self, routes: List[RouteFacts]) -> None:
        actual = {
            _key(route)
            for route in routes
            if route.module in PROTOCOL_SELF_SERVICE_MODULES and _categorize(route) == "TENANT_SCOPED"
        }
        assert actual == PROTOCOL_SELF_SERVICE_TENANT_REACHABLE

    def test_no_endpoint_of_these_modules_escapes_into_a_third_bucket(self, routes: List[RouteFacts]) -> None:
        """这两个模块只允许两类：租户作用域，或平台方专属

        每条端点要么吃 `instance_key`、要么回实例清单，没有"与租户无关"这回事。
        """
        stray = sorted(
            f"{route.module} {route.method} {route.path} -> {_categorize(route)}"
            for route in routes
            if route.module in PROTOCOL_SELF_SERVICE_MODULES and _categorize(route) not in {"TENANT_SCOPED", "PLATFORM_ONLY"}
        )
        assert stray == []

    def test_every_tenant_reachable_one_really_reaches_a_scope_guard(self, routes: List[RouteFacts]) -> None:
        """降权的那七条必须真的走到作用域守卫

        只降 `Role.Super` → `Role.Admin` 而不接守卫，就是把「填别人的 instance_key」
        从"需要平台方账号"变成"随便一个租户管理员就行"。
        """
        by_key = {_key(route): route for route in routes}
        for key in sorted(PROTOCOL_SELF_SERVICE_TENANT_REACHABLE):
            route = by_key[key]
            assert route.reaches_guard, key
            assert route.min_role is Role.Admin, key


class TestIssue29ModulesAreAuditedEndpointByEndpoint:
    """`workspace_kb` 与 `resources` 的整表登记（PROJECT_STATE §3 #29）

    上面的通用契约对这两个模块只能说"每条都接了某个守卫"。这一组补的是
    **集合层面**的约束：一族端点里悄悄多一条、少一条、或者从平台方专属滑到
    租户可达，都要在 `KB_AND_RESOURCE_TENANT_SCOPED` 上显式改一次。
    """

    def test_tenant_reachable_set_is_exactly_the_registered_one(self, routes: List[RouteFacts]) -> None:
        actual = {
            _key(route)
            for route in routes
            if route.module in ISSUE_29_MODULES and _categorize(route) == "TENANT_SCOPED"
        }
        assert actual == KB_AND_RESOURCE_TENANT_SCOPED

    def test_no_endpoint_of_these_modules_escapes_into_a_third_bucket(self, routes: List[RouteFacts]) -> None:
        """这两个模块只允许两类：租户作用域，或平台方专属

        `TENANT_AGNOSTIC` / `UNSCOPED_TODO` 在这里都是错的——每条端点都吃
        `workspace_id` 或 `resource_id`，没有"与租户无关"这回事。
        """
        stray = sorted(
            f"{route.module} {route.method} {route.path} -> {_categorize(route)}"
            for route in routes
            if route.module in ISSUE_29_MODULES and _categorize(route) not in {"TENANT_SCOPED", "PLATFORM_ONLY"}
        )
        assert stray == []

    def test_the_workspace_scoped_surface_still_has_the_same_size(self, routes: List[RouteFacts]) -> None:
        """#29 数的那 21 条（按端点算 22）都还在，而且没有多出没审过的

        数量本身不是目的：它是"有人往这两个文件里加了端点"的信号，
        而这两个文件正是同一个洞被发现两次的地方。
        """
        workspace_scoped = [
            route
            for route in routes
            if route.module in ISSUE_29_MODULES and "{workspace_id}" in route.path
        ]
        assert len(workspace_scoped) == ISSUE_29_WORKSPACE_SCOPED_ENDPOINT_COUNT
        assert all(_categorize(route) in {"TENANT_SCOPED", "PLATFORM_ONLY"} for route in workspace_scoped)

    def test_the_global_resource_surface_is_platform_only(self, routes: List[RouteFacts]) -> None:
        """`/resources*`（不带 `workspace_id` 的那些）一条都不能是租户可达的

        它们读写的是**没有归属列**的平台方基础设施凭据表，
        不存在"租户的资源"这回事，所以这里没有可收敛的作用域，只能是平台方专属。
        """
        leaked = sorted(
            f"{route.method} {route.path}"
            for route in routes
            if route.module == "resources" and "{workspace_id}" not in route.path and not is_platform_only(route)
        )
        assert leaked == []


def _reaches_skill_library_guard(route: RouteFacts) -> bool:
    """端点是否真的按当前身份解析出了技能库作用域

    与 `describe_routes` 的通用探测同一套写法（两层展开：直接含守卫名的函数，
    以及调用了这类函数的函数），只是把判据换成更严的 `SKILL_LIBRARY_SCOPE_GUARDS`。
    单独写在这里而不是往 `RouteFacts` 上加字段，是为了让这一族的收紧是**纯增量**的。
    """
    module = importlib.import_module(f"nekro_agent.routers.{route.module}")
    functions = {
        name: _source_of(member)
        for name, member in vars(module).items()
        if inspect.isfunction(member) and getattr(member, "__module__", "") == module.__name__
    }
    direct = {name for name, source in functions.items() if SKILL_LIBRARY_SCOPE_GUARDS & _identifiers(source)}
    indirect = {name for name, source in functions.items() if direct & _identifiers(source)}
    endpoint_source = functions.get(route.name, "")
    return bool((SKILL_LIBRARY_SCOPE_GUARDS | direct | indirect) & _identifiers(endpoint_source))


class TestSkillLibraryScopeIsPinned:
    """技能库（A4）的整表登记与更强的结构要求

    上面的通用契约对这一族只能说"每条都接了某个守卫、等级够 Admin"。
    这一组补两件事：集合本身不许悄悄变化，以及每条都必须真的解析作用域。
    """

    def test_tenant_reachable_set_is_exactly_the_registered_one(self, routes: List[RouteFacts]) -> None:
        actual = {
            _key(route)
            for route in routes
            if route.module in SKILL_LIBRARY_MODULES and _categorize(route) == "TENANT_SCOPED"
        }
        assert actual == SKILL_LIBRARY_TENANT_REACHABLE

    def test_no_skill_endpoint_escapes_into_a_fourth_bucket(self, routes: List[RouteFacts]) -> None:
        """技能库只允许三类：租户作用域、平台方专属、内置技能的公开只读面

        `UNSCOPED_TODO` 在这里是错的 —— 这一族已经整体审过一遍，不留待办。
        """
        allowed = {"TENANT_SCOPED", "PLATFORM_ONLY", "TENANT_AGNOSTIC"}
        stray = sorted(
            f"{route.module} {route.method} {route.path} -> {_categorize(route)}"
            for route in routes
            if route.module in SKILL_LIBRARY_MODULES and _categorize(route) not in allowed
        )
        assert stray == []

    def test_the_agnostic_surface_is_only_builtin_skills(self, routes: List[RouteFacts]) -> None:
        """留在 `TENANT_AGNOSTIC` 的必须**只有** `/skills/builtin*`

        这一条是对那句"共享库的只读面"的直接反制：任何一条读 `{DATA_DIR}` 下技能正文的
        端点被塞回这一档，都会在这里红。
        """
        agnostic = sorted(
            route.path
            for route in routes
            if route.module in SKILL_LIBRARY_MODULES and _categorize(route) == "TENANT_AGNOSTIC"
        )
        assert agnostic == [
            "/skills/builtin",
            "/skills/builtin/{name}",
            "/skills/builtin/{name}/doc",
            "/skills/builtin/{name}/file",
        ]

    def test_every_tenant_reachable_skill_route_really_resolves_a_scope(self, routes: List[RouteFacts]) -> None:
        """"至少 Role.Admin"不是作用域

        租户管理员**就是** `Role.Admin`。这一族的每个端点都必须真的按当前身份解析出
        技能库作用域，摘掉那一行就等于退回改前的"任何后台管理员读全库"。
        """
        by_key = {_key(route): route for route in routes}
        missing = sorted(key for key in SKILL_LIBRARY_TENANT_REACHABLE if not _reaches_skill_library_guard(by_key[key]))
        assert missing == []

    def test_the_guard_detector_itself_is_not_vacuous(self, routes: List[RouteFacts]) -> None:
        """反向证明：平台方专属那几条**没有**解析作用域

        没有这一条，上面那个断言可能因为"探测器把所有端点都判成 True"而空转。
        `POST /skills/clone` 一族是能力边界（`Role.Super`），本来就不该有作用域解析。
        """
        by_key = {_key(route): route for route in routes}
        assert not _reaches_skill_library_guard(by_key[("skills", "POST", "/skills/clone")])
        assert _reaches_skill_library_guard(by_key[("skills", "GET", "/skills/tree")])


def _reaches_usage_scope_guard(route: RouteFacts) -> bool:
    """端点是否真的解析出了一个 `UsageScope`

    写法与 `_reaches_skill_library_guard` / `_reaches_identity_scope_guard` 同源
    （模块内两层展开），判据换成 `USAGE_SCOPE_GUARDS`。
    """
    module = importlib.import_module(f"nekro_agent.routers.{route.module}")
    functions = {
        name: _source_of(member)
        for name, member in vars(module).items()
        if inspect.isfunction(member) and getattr(member, "__module__", "") == module.__name__
    }
    direct = {name for name, source in functions.items() if USAGE_SCOPE_GUARDS & _identifiers(source)}
    indirect = {name for name, source in functions.items() if direct & _identifiers(source)}
    return bool((USAGE_SCOPE_GUARDS | direct | indirect) & _identifiers(functions.get(route.name, "")))


class TestUsageConsoleScopeIsPinned:
    """用量与费用（本轮）：`console.usage` 的整表登记与更强的结构要求

    账本跨租户读到 = 把别人的经营数据整份端走，所以这一族的分档不许悄悄变化。
    """

    def test_tenant_reachable_set_is_exactly_the_registered_one(self, routes: List[RouteFacts]) -> None:
        actual = {
            _key(route)
            for route in routes
            if route.module in USAGE_CONSOLE_MODULES and _categorize(route) == "TENANT_SCOPED"
        }
        assert actual == USAGE_CONSOLE_TENANT_REACHABLE

    def test_no_usage_endpoint_escapes_into_another_bucket(self, routes: List[RouteFacts]) -> None:
        """只允许两类：读数据的走租户作用域，纯常量的走 `TENANT_AGNOSTIC`

        `UNSCOPED_TODO` 在这里是错的——这一族是本轮新建的，不留待办；
        `PLATFORM_ONLY` 也是错的——定制版客户必须看得到自己的账。
        """
        allowed = {"TENANT_SCOPED", "TENANT_AGNOSTIC"}
        stray = sorted(
            f"{route.module} {route.method} {route.path} -> {_categorize(route)}"
            for route in routes
            if route.module in USAGE_CONSOLE_MODULES and _categorize(route) not in allowed
        )
        assert stray == []

    def test_the_agnostic_surface_is_only_the_enum_endpoint(self, routes: List[RouteFacts]) -> None:
        """留在 `TENANT_AGNOSTIC` 的必须**只有**那个纯常量端点

        任何一条真的读 `llm_usage_record` 的端点被塞回这一档都会在这里红——
        那正是"一句『这是元数据』把 13 个读端点放进无作用域档"（技能库 A4）的同款错法。
        """
        agnostic = sorted(
            route.path
            for route in routes
            if route.module in USAGE_CONSOLE_MODULES and _categorize(route) == "TENANT_AGNOSTIC"
        )
        assert agnostic == ["/usage/enums"]

    def test_every_usage_route_really_resolves_a_scope(self, routes: List[RouteFacts]) -> None:
        """「至少 Role.Admin」不是作用域：租户管理员**就是** `Role.Admin`"""
        by_key = {_key(route): route for route in routes}
        missing = sorted(key for key in USAGE_CONSOLE_TENANT_REACHABLE if not _reaches_usage_scope_guard(by_key[key]))
        assert missing == []

    def test_every_usage_route_requires_at_least_admin(self, routes: List[RouteFacts]) -> None:
        by_key = {_key(route): route for route in routes}
        for key in sorted(USAGE_CONSOLE_TENANT_REACHABLE):
            assert has_write_level_gate(by_key[key]), key

    def test_the_usage_guard_detector_is_not_vacuous(self, routes: List[RouteFacts]) -> None:
        """§2.11 对照组：探测器不是恒真

        同一个模块里的 `/usage/enums` 不读任何数据、也不解析作用域，
        它必须被判成 False——否则上面那条断言可能只是"什么都判成 True"。
        """
        by_key = {_key(route): route for route in routes}
        assert not _reaches_usage_scope_guard(by_key[("console.usage", "GET", "/usage/enums")])
        assert _reaches_usage_scope_guard(by_key[("console.usage", "GET", "/usage/summary")])


def _reaches_identity_scope_guard(route: RouteFacts) -> bool:
    """端点是否真的解析出了实例或组的归属

    写法与 `_reaches_skill_library_guard` 同源（两层展开），判据换成
    `IDENTITY_SCOPE_GUARDS`。域配置这一族的守卫大多收在模块内的两个辅助函数里
    （`_assert_domain_accessible` / `_resolve_domain`），直接扫端点源码是看不见的。
    """
    module = importlib.import_module(f"nekro_agent.routers.{route.module}")
    functions = {
        name: _source_of(member)
        for name, member in vars(module).items()
        if inspect.isfunction(member) and getattr(member, "__module__", "") == module.__name__
    }
    direct = {name for name, source in functions.items() if IDENTITY_SCOPE_GUARDS & _identifiers(source)}
    indirect = {name for name, source in functions.items() if direct & _identifiers(source)}
    return bool((IDENTITY_SCOPE_GUARDS | direct | indirect) & _identifiers(functions.get(route.name, "")))


class TestIdentityConsoleScopeIsPinned:
    """身份与域（本轮）：`console.identity` 的整表登记与更强的结构要求

    跨租户串域是本轮最严重的风险面，而它的操作面就是这一族端点。
    """

    def test_tenant_reachable_set_is_exactly_the_registered_one(self, routes: List[RouteFacts]) -> None:
        actual = {
            _key(route)
            for route in routes
            if route.module in IDENTITY_CONSOLE_MODULES and _categorize(route) == "TENANT_SCOPED"
        }
        assert actual == IDENTITY_CONSOLE_TENANT_REACHABLE

    def test_no_identity_endpoint_escapes_into_another_bucket(self, routes: List[RouteFacts]) -> None:
        """这一族只允许一类：租户作用域

        每条端点要么吃 `instance_key`、要么吃组 ID，没有"与租户无关"这回事；
        也不该有平台方专属的那一档 —— 定制版客户要能自助配自己的域与合并自己的账号。
        """
        stray = sorted(
            f"{route.module} {route.method} {route.path} -> {_categorize(route)}"
            for route in routes
            if route.module in IDENTITY_CONSOLE_MODULES and _categorize(route) != "TENANT_SCOPED"
        )
        assert stray == []

    def test_every_identity_route_really_resolves_an_ownership(self, routes: List[RouteFacts]) -> None:
        """「至少 Role.Admin」不是作用域

        摘掉任何一处归属解析，租户管理员就能拿别人的 `instance_key` / 组 ID 进来 ——
        而那正好是"把别的租户的实例拉进自己的域"这条路径。
        """
        by_key = {_key(route): route for route in routes}
        missing = sorted(
            key for key in IDENTITY_CONSOLE_TENANT_REACHABLE if not _reaches_identity_scope_guard(by_key[key])
        )
        assert missing == []

    def test_every_identity_route_requires_at_least_admin(self, routes: List[RouteFacts]) -> None:
        """只读的租户普通用户不能改域归属：那是一次数据可见性的改动"""
        by_key = {_key(route): route for route in routes}
        for key in sorted(IDENTITY_CONSOLE_TENANT_REACHABLE):
            assert by_key[key].min_role is Role.Admin, key

    def test_the_identity_guard_detector_is_not_vacuous(self, routes: List[RouteFacts]) -> None:
        """§2.11 对照组：探测器不是恒真

        拿一条**确实没有**归属解析的端点来验它看得见差别。`/skills/clone` 是能力边界
        （`Role.Super`），源码里一个归属守卫都没有。
        """
        by_key = {_key(route): route for route in routes}
        assert not _reaches_identity_scope_guard(by_key[("skills", "POST", "/skills/clone")])
        assert _reaches_identity_scope_guard(
            by_key[("console.identity", "PUT", "/identity/instances/{instance_key}/domain")],
        )


class TestGuardDetectionItself:
    """检测器自身也要有约束，否则它悄悄失效会让上面所有断言一起变成空转"""

    def test_indirect_guards_are_detected(self, routes: List[RouteFacts]) -> None:
        """守卫收在模块内的辅助函数里时也必须算数

        这四处分别是四种收口写法，任何一处认不出来都会让一整族端点被误判成未防护。
        """
        by_key = {_key(route): route for route in routes}
        for key in [
            ("workspace_kb", "GET", "/workspaces/{workspace_id}/kb/documents"),  # _get_workspace_or_404
            ("chat_channel", "GET", "/chat-channel/list"),                       # visible_channels
            ("commands", "POST", "/commands/set-state"),                         # _ensure_command_state_writable
            ("timers", "POST", "/timers/{task_type}/{task_id}/run-now"),         # _accessible_item_or_404
        ]:
            assert by_key[key].reaches_guard, key

    def test_role_is_read_from_the_decorator_closure(self, routes: List[RouteFacts]) -> None:
        """`min_role` 必须真的读得出来，否则 `Role.Super` 的断言会全体变成 None==None"""
        by_key = {_key(route): route for route in routes}
        assert by_key[("plugin_editor", "POST", "/plugin-editor/apply")].min_role is Role.Super
        assert by_key[("workspaces", "GET", "/workspaces/list")].min_role is Role.Admin

    def test_platform_owner_dependency_is_detected(self, routes: List[RouteFacts]) -> None:
        by_key = {_key(route): route for route in routes}
        assert by_key[("console.tenants", "GET", "/tenants")].has_owner_dep
        assert by_key[("deployment", "POST", "/deployment/restore")].has_owner_dep

    def test_a_new_unregistered_endpoint_would_fail(self) -> None:
        """反向证明：契约的默认判决是"不通过"

        没有这一条，上面所有断言都可能因为"登记表恰好覆盖了当前全部端点"而空转。
        这里合成一条"没登记、没角色、没守卫"的端点，它必须既落进 TENANT_SCOPED，
        又通不过 `is_tenant_scoped` —— 也就是新加一个裸端点一定会让测试红。
        """
        newcomer = RouteFacts(
            module="workspaces",
            method="POST",
            path="/workspaces/{workspace_id}/brand-new-thing",
            name="brand_new_thing",
            min_role=None,
            has_owner_dep=False,
            has_tenant_admin_dep=False,
            parameter_names=("workspace_id", "_current_user"),
            inline_super=False,
            reaches_guard=False,
            verifies_signature=False,
            resolves_machine_token=False,
        )
        assert _categorize(newcomer) == "TENANT_SCOPED"
        assert not is_tenant_scoped(newcomer)
        assert not is_platform_only(newcomer)

    def test_route_table_is_not_empty(self, routes: List[RouteFacts]) -> None:
        """路由收集要是静默失败返回空表，上面每一条断言都会变成 `[] == []`

        阈值取当前规模的一个下界，不追着实际数量走。
        """
        assert len(routes) > 300
        assert len({route.module for route in routes}) == len(ROUTER_MODULES)


class TestRequireRoleIsActuallyWired:
    """`@require_role` 只在参数**名叫** `_current_user` 时才生效

    装饰器包出来的 wrapper 签名是 `(*args, _current_user=Depends(...), **kwargs)`，
    但 `functools.wraps` 设了 `__wrapped__`，FastAPI 的 `inspect.signature` 会跟过去
    读**原函数**的签名。于是参数叫 `current_user`（没有下划线）时：
    FastAPI 按原签名把 `current_user=<DBUser>` 传进 wrapper，
    而 wrapper 自己的 `_current_user` 没人填，取到默认值 —— 那是个 `Depends` 对象，
    第一行 `_current_user.perm_level` 立刻 `AttributeError`，端点 500。

    也就是说：**装饰器在那里、`min_role` 读得出来、契约测试通过，端点却是坏的。**
    本轮给 `common.py` 的壁纸上传/删除加 `Role.Super` 时就真的踩了这个，
    靠实打实发一次请求才看见。这条把它变成静态可查。
    """

    def test_every_role_decorated_endpoint_names_the_parameter_correctly(
        self, routes: List[RouteFacts]
    ) -> None:
        broken = [
            f"{route.module} {route.method} {route.path} params={list(route.parameter_names)}"
            for route in routes
            if route.min_role is not None and "_current_user" not in route.parameter_names
        ]
        assert broken == []
