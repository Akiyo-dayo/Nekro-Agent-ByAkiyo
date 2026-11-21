"""登录令牌不进链接：前端取数据一律走 Authorization 头

邮件附件原来把登录令牌以 `?token=` 拼进 `img` / `iframe` / `a` 的链接。令牌长期有效，拼进链接就会落进
uvicorn 与反向代理的访问日志、浏览器历史（插件路由正是因此拒收 `?token=`）。现在附件按 Authorization 头
取回成 Blob，再用 `blob:` 链接显示（`services/api/emails.ts` 的 `fetchAttachment`）。这条用例防止别处再这样拼。

例外只有两处，都写明了原因：沙盒终端的 WebSocket（浏览器的 WebSocket 不能带请求头），以及 OneBot 日志页
从 NapCat 自己的日志里解析它面板链接上的令牌（那是 NapCat 的令牌，不是 NA 的登录令牌，也不是拼链接）。
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND_SRC = Path(__file__).resolve().parents[1] / "frontend" / "src"

ALLOWED = {
    "pages/workspace/tabs/SandboxTab.tsx": "WebSocket 不能带请求头",
    "pages/adapter/onebot_v11/logs.tsx": "解析 NapCat 日志里它自己的面板链接",
}

# `?token=` / `&token=`、`{ token }` 当查询参数、`params: { … token … }`
TOKEN_IN_URL = re.compile(r"[?&]token=|appendQuery\([^)]*\btoken\b|params:\s*\{[^}]*\btoken\b|searchParams\.(?:set|append)\(\s*['\"]token['\"]")


def _sources() -> list[Path]:
    return [path for path in FRONTEND_SRC.rglob("*") if path.suffix in {".ts", ".tsx"} and path.is_file()]


def test_the_frontend_tree_is_there() -> None:
    assert len(_sources()) > 100, "前端源码没找到，下面那条用例就成了空跑"


def test_no_login_token_is_put_into_a_url() -> None:
    offenders = []
    for path in _sources():
        relative = path.relative_to(FRONTEND_SRC).as_posix()
        if relative in ALLOWED:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if TOKEN_IN_URL.search(line):
                offenders.append(f"{relative}:{number}: {line.strip()}")
    assert not offenders, "登录令牌被拼进了链接：\n" + "\n".join(offenders)


def test_the_allowed_exceptions_still_exist() -> None:
    """例外的文件改名或删了，就该把它从名单里拿掉，免得名单越积越长"""
    for relative in ALLOWED:
        assert (FRONTEND_SRC / relative).is_file(), relative


def test_mail_attachments_are_fetched_with_the_header() -> None:
    source = (FRONTEND_SRC / "services" / "api" / "emails.ts").read_text(encoding="utf-8")

    assert "responseType: 'blob'" in source
    assert "useAuthStore" not in source, "附件不该再自己读令牌拼链接"
    page = (FRONTEND_SRC / "pages" / "adapter" / "email" / "emails.tsx").read_text(encoding="utf-8")
    assert "getAttachmentAccessUrl" not in page
    assert "URL.revokeObjectURL" in page
