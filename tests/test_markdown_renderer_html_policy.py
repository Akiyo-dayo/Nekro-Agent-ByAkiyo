"""前端 Markdown 渲染器的原始 HTML 策略

`MarkdownRenderer` 打开 `enableHtml` 时走 `rehype-raw`，不做任何过滤：`<iframe srcdoc="<script>…">`、
事件属性都会以当前登录管理员的身份执行。它曾经默认打开，于是群聊消息、邮件附件（`.md` 预览）、
第三方插件文档里的 HTML 都能在后台执行脚本。

钉住两件事：默认关闭；显式打开只允许出现在内容随本项目发布的页面上（白名单）。
新增一个打开它的地方，必须先确认内容可信，再把文件加进白名单。
"""

import re
from pathlib import Path

FRONTEND_SRC = Path(__file__).parent.parent / "frontend" / "src"
RENDERER = FRONTEND_SRC / "components" / "common" / "MarkdownRenderer.tsx"

# 内容随本项目一起发布、可以保留 HTML 排版的页面
TRUSTED_HTML_PAGES = {"pages/adapter/AdapterHomePage.tsx"}

_USAGE = re.compile(r"<MarkdownRenderer\b[^>]*>", re.S)


def test_html_rendering_is_off_by_default() -> None:
    source = RENDERER.read_text(encoding="utf-8")
    assert re.search(r"\benableHtml\s*=\s*false\b", source), "MarkdownRenderer 的 enableHtml 默认值必须是 false"


def test_only_trusted_pages_turn_html_on() -> None:
    offenders = []
    for path in FRONTEND_SRC.rglob("*.tsx"):
        rel = path.relative_to(FRONTEND_SRC).as_posix()
        for tag in _USAGE.findall(path.read_text(encoding="utf-8")):
            turns_on = re.search(r"\benableHtml\b(?!\s*=\s*\{\s*false\s*\})", tag)
            if turns_on and rel not in TRUSTED_HTML_PAGES:
                offenders.append(f"{rel}: {tag[:80]}")
    assert not offenders, "这些地方对外部内容打开了原始 HTML 渲染：\n" + "\n".join(offenders)


def test_the_checker_recognises_both_spellings() -> None:
    """判据自检：`enableHtml` 与 `enableHtml={true}` 都算打开，`enableHtml={false}` 不算"""
    on = re.compile(r"\benableHtml\b(?!\s*=\s*\{\s*false\s*\})")
    assert on.search("<MarkdownRenderer enableHtml>")
    assert on.search("<MarkdownRenderer enableHtml={true}>")
    assert not on.search("<MarkdownRenderer enableHtml={false}>")
