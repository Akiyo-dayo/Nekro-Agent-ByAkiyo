"""邮箱适配器的两条安全底线

1. **账户管理接口必须整体处于鉴权之后**。这些接口读写邮箱凭据、OAuth token，能触发收信、读取原始邮件。
   鉴权声明在 router 级别（平台方邮箱管理员），新增路由时不会漏加。
2. **邮件正文的 HTML 不能注入后台页面**。正文来自任意发件人，直接 innerHTML 会让正文里的脚本 / 事件属性
   以当前登录管理员的身份执行。正文必须放进 `sandbox=""` 的 iframe 里隔离渲染，再加一层 CSP。

另有一条静态守卫（`TestCollectMessageHasOneCallSite`）：`collect_message` 只能出现在平台方那条收信
路径里。实例自有邮箱如果走了它，会建出一个没有实例段、因此归平台方的 `email-<地址>` 频道——
那是数据越界，而且不会有任何异常把它暴露出来，只能在代码形态上守住。
"""

import ast
import re
from pathlib import Path
from typing import List, Tuple

from nekro_agent.adapters.email.routers import require_platform_mailbox_admin, router

EMAILS_PAGE = Path(__file__).parent.parent / "frontend" / "src" / "pages" / "adapter" / "email" / "emails.tsx"
ADAPTER_SOURCE = Path(__file__).parent.parent / "nekro_agent" / "adapters" / "email" / "adapter.py"

#: 允许调用 `collect_message` 的那个函数（平台方收信路径）
COLLECT_MESSAGE_HOST = "_process_email"


class TestRoutesRequireAuth:
    def test_router_level_dependency_is_declared(self) -> None:
        assert require_platform_mailbox_admin in {d.dependency for d in router.dependencies}

    def test_every_route_carries_the_dependency(self) -> None:
        routes = [route for route in router.routes if hasattr(route, "dependant")]
        assert routes
        for route in routes:
            calls = {dep.call for dep in route.dependant.dependencies}
            assert require_platform_mailbox_admin in calls, f"路由 {route.path} 缺少鉴权依赖"


class TestEmailBodyIsRenderedIsolated:
    source = EMAILS_PAGE.read_text(encoding="utf-8")

    def test_no_raw_html_injection(self) -> None:
        assert "dangerouslySetInnerHTML" not in self.source

    def test_body_goes_through_a_sandboxed_iframe(self) -> None:
        block = re.search(r'component="iframe"[\s\S]{0,400}?srcDoc=\{buildIsolatedEmailDocument\(', self.source)
        assert block, "邮件正文必须通过 buildIsolatedEmailDocument 放进 iframe"
        assert 'sandbox=""' in block.group(0), "iframe 必须是空 sandbox：不允许脚本、表单、同源"

    def test_body_is_stripped_of_self_navigation_before_rendering(self) -> None:
        """sandbox 与 CSP 挡不住 iframe 自己跳走：meta refresh / base 要先去掉，链接改为新窗口（无 allow-popups 即无效）"""
        assert "buildIsolatedEmailDocument" in self.source and "sanitizeEmailHtml(html)" in self.source
        stripped = re.search(r"const EMAIL_STRIPPED_ELEMENTS = '([^']+)'", self.source)
        assert stripped
        for tag in ("meta", "base", "script", "iframe"):
            assert tag in stripped.group(1).split(", "), tag
        assert "setAttribute('target', '_blank')" in self.source

    def test_the_document_head_forbids_scripts(self) -> None:
        head = re.search(r"const EMAIL_DOCUMENT_HEAD =([\s\S]*?)\n\n", self.source)
        assert head
        text = head.group(1)
        assert "default-src \\'none\\'" in text
        assert "script-src" not in text, "CSP 不能放行任何脚本来源"


def find_collect_message_calls(source: str) -> List[Tuple[str, int]]:
    """扫出每个 `collect_message(...)` 调用所在的函数名与行号

    只看 `ast.Call` 且函数名是 `collect_message`（属性调用 `x.collect_message` 也算——
    换个拿法绕过去也是同一件事）。按「进入 def 时压栈、离开时弹栈」取**最内层**函数名：
    将来真有人把调用挪进一个小工具函数，扫描器报出来的名字也会跟着变，拦截点不会因为重构消失。
    """
    found: List[Tuple[str, int]] = []
    stack: List[str] = []

    def walk(node: ast.AST) -> None:
        pushed = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if pushed:
            stack.append(str(node.name))
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name == "collect_message":
                found.append((stack[-1] if stack else "", node.lineno))
        for child in ast.iter_child_nodes(node):
            walk(child)
        if pushed:
            stack.pop()

    walk(ast.parse(source))
    return sorted(found, key=lambda item: item[1])


class TestCollectMessageHasOneCallSite:
    """实例自有邮箱收信绝不能建频道：那个频道没有实例段，建出来就归平台方"""

    def test_exactly_one_call_site(self) -> None:
        calls = find_collect_message_calls(ADAPTER_SOURCE.read_text(encoding="utf-8"))

        assert len(calls) == 1, f"collect_message 只允许在平台方收信路径里出现一处，实际: {calls}"

    def test_the_call_site_is_the_platform_path(self) -> None:
        [(function_name, _line)] = find_collect_message_calls(ADAPTER_SOURCE.read_text(encoding="utf-8"))

        assert function_name == COLLECT_MESSAGE_HOST

    def test_an_injected_call_is_reported(self) -> None:
        """反向对照：扫描器真的会报，而且报得出所在函数"""
        polluted = ADAPTER_SOURCE.read_text(encoding="utf-8") + (
            "\n\nasync def _leak(self, channel, user, message):\n    await collect_message(self, channel, user, message)\n"
        )

        calls = find_collect_message_calls(polluted)

        assert len(calls) == 2
        assert "_leak" in [name for name, _line in calls]

    def test_an_attribute_call_is_reported_too(self) -> None:
        polluted = ADAPTER_SOURCE.read_text(encoding="utf-8") + (
            "\n\nasync def _leak(self, mod, *args):\n    await mod.collect_message(*args)\n"
        )

        assert len(find_collect_message_calls(polluted)) == 2
