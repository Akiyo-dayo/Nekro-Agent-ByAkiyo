"""插件 WebUI 路径：文件型静态页面与路由型页面

覆盖上游 d7470d5 的对外契约：
- `webui_path` 指向插件目录内的 HTML 时，按 `file` 类型暴露在 `/__webui__/` 下；
- `webui_path` 以 `/` 开头时复用插件自定义路由，按 `route` 类型暴露；
- 没有自定义路由时 `get_plugin_router()` 仍然返回 `None`，保持改动前的行为；
- 自定义路由与 HTML WebUI 可以同时存在。

另外钉住本分支额外加的安全硬化（这些路由不鉴权）：
- 静态路由只提供网页资源后缀（白名单），源码、字节码、本地数据库、配置文件、`__pycache__` 与点开头的路径都不提供；
- 单文件插件的模块位于共用目录，只提供 HTML 入口本身；
- 越界、符号链接、非法字符一律 404，不会变成 500。
"""

from __future__ import annotations

import os
import types
from pathlib import Path
from typing import Dict, Tuple

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from nekro_agent.services.plugin.base import NekroPlugin


def make_plugin(module_name: str, webui_path: str | None = None) -> NekroPlugin:
    """构造一个不落盘的最小插件实例（构造函数只算路径，不建目录、不读配置）"""
    return NekroPlugin(
        name=f"测试插件-{module_name}",
        module_name=module_name,
        description="插件 WebUI 测试用",
        version="0.0.1",
        author="tester",
        url="",
        webui_path=webui_path,
    )


def bind_module(plugin: NekroPlugin, directory: Path, filename: str = "plugin.py") -> Path:
    """把插件的模块文件指向临时目录，`_get_source_dir()` 依赖模块的 `__file__`"""
    module_file = directory / filename
    module_file.write_text("# 测试插件模块\n", encoding="utf-8")
    module = types.ModuleType(plugin.module_name)
    module.__file__ = str(module_file)
    plugin._module = module  # noqa: SLF001
    return module_file


def build_client(plugin: NekroPlugin) -> TestClient:
    """按 router_manager 的挂载方式把插件路由挂到 FastAPI 上"""
    router = plugin.get_plugin_router()
    assert router is not None, "插件应当生成了可挂载的路由"
    app = FastAPI()
    app.include_router(router, prefix=f"/plugins/{plugin.key}")
    return TestClient(app)


@pytest.fixture
def html_webui(tmp_path: Path) -> Tuple[NekroPlugin, Path]:
    """插件根目录既是 HTML 所在目录，也放着源码与隐藏文件"""
    plugin_dir = tmp_path / "webui_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "index.html").write_text("<!doctype html><html><body>webui-index</body></html>", encoding="utf-8")
    (plugin_dir / "app.js").write_text("console.log('webui-app')\n", encoding="utf-8")
    (plugin_dir / "plugin.py").write_text("SECRET = 'source-code'\n", encoding="utf-8")
    pycache = plugin_dir / "__pycache__"
    pycache.mkdir()
    (pycache / "x.pyc").write_bytes(b"bytecode")
    (plugin_dir / ".env").write_text("SECRET=hidden\n", encoding="utf-8")
    (plugin_dir / "data.db").write_bytes(b"sqlite")
    (plugin_dir / "native.pyd").write_bytes(b"binary")
    (plugin_dir / "settings.yaml").write_text("token: hidden\n", encoding="utf-8")
    (plugin_dir / "data.json").write_text('{"ok": true}', encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside-root\n", encoding="utf-8")

    plugin = make_plugin("webui_demo_html", "index.html")
    # 包形式的插件：有自己的目录，静态路由可以提供同目录的网页资源
    bind_module(plugin, plugin_dir, "__init__.py")
    return plugin, plugin_dir


def test_relative_html_reports_file_type_and_url(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "webui_relative"
    plugin_dir.mkdir()
    (plugin_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    plugin = make_plugin("webui_demo_relative", "index.html")
    bind_module(plugin, plugin_dir)

    assert plugin.get_webui_type() == "file"
    assert plugin.get_webui_url_path() == f"/plugins/{plugin.key}/__webui__/index.html"


def test_route_path_with_mounted_router() -> None:
    plugin = make_plugin("webui_demo_route", "/")

    @plugin.mount_router()
    def create_router() -> APIRouter:
        router = APIRouter()

        @router.get("/api/ping")
        async def ping() -> Dict[str, bool]:
            return {"ok": True}

        return router

    assert plugin.get_webui_type() == "route"
    assert plugin.get_webui_url_path() == f"/plugins/{plugin.key}/"


def test_route_path_without_router_has_no_webui() -> None:
    plugin = make_plugin("webui_demo_route_missing", "/")

    assert plugin.get_webui_type() is None
    assert plugin.get_webui_url_path() is None


def test_no_webui_path_keeps_plugin_router_none() -> None:
    plugin = make_plugin("webui_demo_none")

    assert plugin.get_webui_type() is None
    assert plugin.get_webui_url_path() is None
    assert plugin.get_plugin_router() is None


def test_html_only_plugin_gets_a_router(html_webui: Tuple[NekroPlugin, Path]) -> None:
    plugin, _plugin_dir = html_webui

    assert plugin.get_plugin_router() is not None


def test_html_webui_serves_entry_and_static_files(html_webui: Tuple[NekroPlugin, Path]) -> None:
    plugin, _plugin_dir = html_webui
    client = build_client(plugin)
    base = f"/plugins/{plugin.key}/__webui__"

    root = client.get(base)
    assert root.status_code == 200
    assert root.headers["content-type"].startswith("text/html")
    assert "webui-index" in root.text

    assert client.get(f"{base}/").status_code == 200

    script = client.get(f"{base}/app.js")
    assert script.status_code == 200
    assert "webui-app" in script.text

    assert client.get(f"{base}/data.json").json() == {"ok": True}


def test_html_webui_rejects_traversal(html_webui: Tuple[NekroPlugin, Path]) -> None:
    """只用编码过的写法：未编码的 `/../` 会被客户端先折叠掉，根本到不了服务端"""
    plugin, _plugin_dir = html_webui
    client = build_client(plugin)
    base = f"/plugins/{plugin.key}/__webui__"

    for probe in ("%2e%2e/outside.txt", "..%2foutside.txt", "%2e%2e%2foutside.txt", "..%5coutside.txt"):
        assert client.get(f"{base}/{probe}").status_code == 404, probe


def test_html_webui_rejects_source_and_hidden_files(html_webui: Tuple[NekroPlugin, Path]) -> None:
    plugin, _plugin_dir = html_webui
    client = build_client(plugin)
    base = f"/plugins/{plugin.key}/__webui__"

    blocked_files = ("plugin.py", "__init__.py", "__pycache__/x.pyc", ".env", "data.db", "native.pyd", "settings.yaml")
    for blocked in blocked_files:
        assert client.get(f"{base}/{blocked}").status_code == 404, f"{blocked} 不应该被静态路由提供"


def test_html_webui_illegal_characters_are_404_not_500(html_webui: Tuple[NekroPlugin, Path]) -> None:
    plugin, _plugin_dir = html_webui
    client = build_client(plugin)  # TestClient 默认把服务端异常抛出来：500 会让本用例直接报错
    base = f"/plugins/{plugin.key}/__webui__"

    for probe in ("%00", "app.js%00.txt"):
        assert client.get(f"{base}/{probe}").status_code == 404, probe


def test_html_webui_does_not_follow_symlinks_out_of_the_root(html_webui: Tuple[NekroPlugin, Path], tmp_path: Path) -> None:
    plugin, plugin_dir = html_webui
    try:
        os.symlink(tmp_path / "outside.txt", plugin_dir / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("当前环境无权创建符号链接")
    client = build_client(plugin)

    assert client.get(f"/plugins/{plugin.key}/__webui__/link.txt").status_code == 404


def test_single_file_plugin_serves_only_its_entry(tmp_path: Path) -> None:
    """单文件插件的模块位于共用目录：同目录里别的插件的文件不能顺带被提供"""
    shared_dir = tmp_path / "workdir"
    shared_dir.mkdir()
    (shared_dir / "index.html").write_text("<html><body>single-entry</body></html>", encoding="utf-8")
    (shared_dir / "other_plugin_app.js").write_text("console.log('other')\n", encoding="utf-8")
    plugin = make_plugin("webui_demo_single", "index.html")
    bind_module(plugin, shared_dir, "webui_demo_single.py")
    client = build_client(plugin)
    base = f"/plugins/{plugin.key}/__webui__"

    assert "single-entry" in client.get(base).text
    assert client.get(f"{base}/other_plugin_app.js").status_code == 404


def test_custom_routes_and_html_webui_are_both_reachable(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "webui_both"
    plugin_dir.mkdir()
    (plugin_dir / "index.html").write_text("<html><body>webui-both</body></html>", encoding="utf-8")
    plugin = make_plugin("webui_demo_both", "index.html")
    bind_module(plugin, plugin_dir)

    @plugin.mount_router()
    def create_router() -> APIRouter:
        router = APIRouter()

        @router.get("/hello")
        async def hello() -> Dict[str, str]:
            return {"message": "hello"}

        return router

    client = build_client(plugin)

    assert client.get(f"/plugins/{plugin.key}/hello").json() == {"message": "hello"}
    page = client.get(f"/plugins/{plugin.key}/__webui__")
    assert page.status_code == 200
    assert "webui-both" in page.text


def test_backslash_webui_path_is_rejected() -> None:
    with pytest.raises(ValueError, match="反斜杠"):
        make_plugin("webui_demo_backslash", "sub\\index.html")
