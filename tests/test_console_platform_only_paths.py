"""平台方专属页面的**两份清单**必须对得上

前端把「这一页只给平台方」写在两个互不相干的地方：

| 清单 | 位置 | 管什么 |
|---|---|---|
| `visibility: 'platformOnly'` | `getPageConfigs()` 里的那一格 | 侧边栏**画不画**这个入口 |
| `PLATFORM_ONLY_PATHS` | 同文件的数组，`MainLayout` 用它算 `isPlatformOnlyBlocked` | 租户**直接敲 URL** 时进不进得去 |

只登记一份不会有任何运行时信号：

- 只写 `visibility` —— 菜单藏住了，租户把 `/console/network-rules` 粘进地址栏照样渲染出整页
  （后端两个端点是 `Role.Super`，会一路 403，但那已经是「拦在最后一道」而不是「进不去」）；
- 只写 `PLATFORM_ONLY_PATHS` —— 页面进不去，菜单却还画着一格点了就被踢走的入口。

本轮新增 `/console/network-rules` 时正是漏了后者（`visibility: 'platformOnly'` 写了、
数组没写），而前端没有任何测试、`tsc`/`eslint`/`vite build` 三道也全绿 —— 这条用例就是
为这一类「同一份清单的第二个副本」补的看门人（PROJECT_STATE §2.12 的同一个形态）。

判据按 **`navigation.tsx` 的源码文本**做：前端没有测试设施，Python 侧解析源码是这个仓库
已有的做法（见 `tests/test_upload_signed_url.py`）。
"""

import re
from pathlib import Path
from typing import List, Set

import pytest

NAVIGATION = Path("frontend/src/config/navigation.tsx")
MAIN_LAYOUT = Path("frontend/src/layouts/MainLayout.tsx")


@pytest.fixture(scope="module")
def navigation_source() -> str:
    assert NAVIGATION.exists(), f"{NAVIGATION} 不存在，判据的落点没了"
    return NAVIGATION.read_text(encoding="utf-8")


def _platform_only_paths(source: str) -> List[str]:
    """`PLATFORM_ONLY_PATHS` 数组里登记的路径前缀"""
    match = re.search(r"export const PLATFORM_ONLY_PATHS = \[(.*?)\] as const", source, re.S)
    assert match, "没找到 PLATFORM_ONLY_PATHS 数组——它被改名或删掉了，兜底拦截可能整条失效"
    return re.findall(r"'([^']+)'", match.group(1))


def _platform_only_pages(source: str) -> Set[str]:
    """标了 `visibility: 'platformOnly'` 的那些格子各自的 `path`

    菜单项是 `{ path, text, ..., visibility }` 或 `{ key, text, ..., children: [...] }`。
    分组（`MenuGroup`）没有自己的 `path`，它的 `visibility` 覆盖整组 —— 那种情况下按
    组内子项的公共前缀登记即可，所以这里只收**自己带 `path`** 的那些格子。
    """
    pages: Set[str] = set()
    for block in re.findall(r"\{([^{}]*?)\}", source, re.S):
        if "visibility: 'platformOnly'" not in block:
            continue
        path = re.search(r"path: '([^']+)'", block)
        if path:
            pages.add(path.group(1))
    return pages


def _covered(paths: List[str], candidate: str) -> bool:
    return any(candidate == prefix or candidate.startswith(f"{prefix}/") for prefix in paths)


class TestPlatformOnlyListsAgree:
    def test_every_platform_only_page_is_also_url_blocked(self, navigation_source: str) -> None:
        """菜单藏了但 URL 还开着 = 只做了一半"""
        paths = _platform_only_paths(navigation_source)
        missing = sorted(page for page in _platform_only_pages(navigation_source) if not _covered(paths, page))
        assert not missing, (
            f"这些页面标了 visibility: 'platformOnly' 却没进 PLATFORM_ONLY_PATHS：{missing}。"
            "租户把这些 URL 粘进地址栏就能渲染出整页"
        )

    def test_the_new_network_rules_page_is_covered(self, navigation_source: str) -> None:
        """本轮新增的那一页 —— 直接钉住，免得上面那条被将来的正则改动悄悄放空"""
        assert _covered(_platform_only_paths(navigation_source), "/console/network-rules")

    def test_the_criterion_can_actually_see_a_missing_entry(self, navigation_source: str) -> None:
        """§2.11：先证明这条判据测得出差异

        把 `/console/network-rules` 从数组里摘掉之后，第一条断言必须变红。
        不做这一步的话，一个「永远返回空 missing」的解析器也能让上面两条通过。
        """
        broken = navigation_source.replace("  '/console/network-rules',\n", "", 1)
        assert broken != navigation_source, "样本没被改动，这条对照组什么都没验"
        paths = _platform_only_paths(broken)
        assert not _covered(paths, "/console/network-rules")
        assert "/console/network-rules" in _platform_only_pages(broken)

    def test_the_pages_side_parser_really_finds_entries(self, navigation_source: str) -> None:
        """另一半对照：解析器不是恒返回空集合（那样 missing 也恒为空）"""
        pages = _platform_only_pages(navigation_source)
        assert "/console/network-rules" in pages
        assert "/user-manager" in pages
        assert len(pages) >= 5

    def test_the_menu_entry_has_a_route_behind_it(self, navigation_source: str) -> None:
        """第三份清单：`router/index.tsx`

        菜单画了一格、`PLATFORM_ONLY_PATHS` 也登记了，但路由表里没有对应的 `element`，
        点进去就是一页空白。三份清单谁都不会因为另一份缺了而报错。
        """
        assert "path: '/console/network-rules'" in navigation_source
        routes = Path("frontend/src/router/index.tsx").read_text(encoding="utf-8")
        assert "path: 'network-rules'" in routes
        assert "NetworkRuleOrphansPage" in routes
        assert Path("frontend/src/pages/console/NetworkRuleOrphansPage.tsx").exists()

    def test_main_layout_still_consumes_the_list(self) -> None:
        """接线：清单登记得再全，没人调用 `isPlatformOnlyPath` 也是白写（§2.7）"""
        text = MAIN_LAYOUT.read_text(encoding="utf-8")
        assert "isPlatformOnlyPath" in text
        assert "isTenantScoped && isPlatformOnlyPath(location.pathname)" in text
