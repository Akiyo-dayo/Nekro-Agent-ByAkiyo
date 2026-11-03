"""邮箱适配器的两条安全底线

1. **账户管理接口必须整体处于鉴权之后**。这些接口读写邮箱凭据、OAuth token，能触发收信、读取原始邮件。
   鉴权声明在 router 级别（平台方邮箱管理员），新增路由时不会漏加。
2. **邮件正文的 HTML 不能注入后台页面**。正文来自任意发件人，直接 innerHTML 会让正文里的脚本 / 事件属性
   以当前登录管理员的身份执行。正文必须放进 `sandbox=""` 的 iframe 里隔离渲染，再加一层 CSP。
"""

import re
from pathlib import Path

from nekro_agent.adapters.email.routers import require_platform_mailbox_admin, router

EMAILS_PAGE = Path(__file__).parent.parent / "frontend" / "src" / "pages" / "adapter" / "email" / "emails.tsx"


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
