"""插件自挂路由的鉴权守卫静态检查

`PluginRouterManager` 只检查插件是否启用，不检查登录、角色、实例归属。新增插件路由时
漏掉 `plugin_route_scope` 就是匿名可达，靠人工 review 挡不住。这里把所有 builtin 插件的
路由收集起来，沿 FastAPI 的依赖树递归找守卫；白名单只放三条例外（都不含数据或自己先验票），并说明为什么。
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import pytest
from fastapi.routing import APIRoute

BUILTIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "builtin"

# 只允许这几条无守卫路由；理由必须写在这里，新增白名单要同时回答"为什么匿名安全"
ALLOWED_WITHOUT_GUARD: Dict[Tuple[str, str, str], str] = {
    ("nekro.whiteboard", "GET", "/"): "只返回无数据的 HTML 壳；页面登录后自己调票据接口，匿名打开只会看到登录提示",
    ("nekro.whiteboard", "GET", "/events"): "事件流在响应生成前先校验只读令牌或短时效票据，失败即 404/401",
    ("KroMiose.emotion", "GET", "/console/"): "表情控制台的 HTML 壳，不含数据；页面读登录态再调挂了守卫的 /console/api/*，匿名打开只看到登录提示",
}


def _plugin_route_scope_dependencies(dependant: Any) -> List[Any]:
    found: List[Any] = []
    for dependency in dependant.dependencies:
        if getattr(dependency.call, "__plugin_route_scope__", False):
            found.append(dependency.call)
        found.extend(_plugin_route_scope_dependencies(dependency))
    return found


def _router_hooking_modules() -> List[str]:
    """`plugins/builtin/` 下所有出现 `mount_router` 的模块名（含包子目录）

    先按源码文本筛，再 import：直接把每个模块都 import 一遍会连带执行没有路由的插件，
    它们可能读配置、连宿主资源。筛出来的是"声明了路由的插件"，正是这条守卫要覆盖的集合。
    """
    modules: List[str] = []
    for path in sorted(BUILTIN_DIR.rglob("*.py")):
        if path.name.startswith("_"):
            continue
        if "mount_router" not in path.read_text(encoding="utf-8"):
            continue
        relative = path.relative_to(BUILTIN_DIR.parent.parent).with_suffix("")
        modules.append(".".join(relative.parts))
    return modules


def _iter_builtin_routes() -> List[Tuple[str, APIRoute]]:
    routes: List[Tuple[str, APIRoute]] = []
    for module_name in _router_hooking_modules():
        module = importlib.import_module(module_name)
        plugin = getattr(module, "plugin", None)
        if plugin is None or not hasattr(plugin, "get_plugin_router"):
            continue
        router = plugin.get_plugin_router()
        if router is None:
            continue
        for route in router.routes:
            if isinstance(route, APIRoute):
                routes.append((plugin.key, route))
    return routes


@pytest.mark.parametrize("plugin_key,route", _iter_builtin_routes(), ids=lambda value: getattr(value, "path", value))
def test_every_plugin_route_has_scope_guard_unless_whitelisted(plugin_key: str, route: APIRoute) -> None:
    scope_dependencies = _plugin_route_scope_dependencies(route.dependant)
    for method in sorted(route.methods):
        key = (plugin_key, method, route.path)
        if key in ALLOWED_WITHOUT_GUARD:
            assert not scope_dependencies, (
                f"{plugin_key} {method} {route.path} 已在白名单里，却仍挂了 plugin_route_scope；"
                "要么移除多余守卫，要么把它从白名单删掉"
            )
            continue
        assert scope_dependencies, (
            f"{plugin_key} {method} {route.path} 没有 plugin_route_scope 守卫。"
            "插件挂的路由默认匿名可达，必须补 `Depends(plugin_route_scope(plugin.key))`；"
            "确有匿名需求就写进 ALLOWED_WITHOUT_GUARD 并说明理由"
        )


def test_the_guard_is_not_vacuous() -> None:
    """用一个故意缺守卫的样例路由证明扫描器不是永远通过

    `_plugin_route_scope_dependencies` 是本模块唯一的判据，也是 `test_every_...` 的判据：
    它若恒回非空，上面那条断言就永远为真。样例里一条不挂依赖，必须被判出来。
    """
    from fastapi import APIRouter, Depends

    from nekro_agent.services.plugin.route_auth import plugin_route_scope

    sample = APIRouter()

    @sample.get("/open")
    async def open_route() -> Dict[str, bool]:
        return {"ok": True}

    @sample.get("/guarded")
    async def guarded_route(scope: Any = Depends(plugin_route_scope("sample.plugin"))) -> Dict[str, bool]:
        return {"ok": bool(scope)}

    open_route_obj = next(route for route in sample.routes if route.path == "/open")
    guarded_route_obj = next(route for route in sample.routes if route.path == "/guarded")
    assert _plugin_route_scope_dependencies(open_route_obj.dependant) == []
    assert _plugin_route_scope_dependencies(guarded_route_obj.dependant) != []


def test_the_production_routes_are_actually_seen() -> None:
    """守卫确实扫到了内置插件路由：一条都没扫到时上面的断言是空转"""
    assert _iter_builtin_routes(), "没有发现任何内置插件路由，守卫实际没有覆盖到任何东西"


def test_whitelist_only_contains_expected_entries() -> None:
    keys: Set[Tuple[str, str, str]] = set(ALLOWED_WITHOUT_GUARD)
    assert keys == {
        ("nekro.whiteboard", "GET", "/"),
        ("nekro.whiteboard", "GET", "/events"),
        ("KroMiose.emotion", "GET", "/console/"),
    }
