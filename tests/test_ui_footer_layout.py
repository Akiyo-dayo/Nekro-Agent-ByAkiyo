"""页脚把版权年份与「基于 NekroAgent 项目构建」合成一行

分两行时信息重复；合成一行后仍要满足 LICENSE：年份与归属在同一行、超链接与字号规则由
`test_upstream_attribution.py` 继续钉住，这里只钉住“只有一行、© 只出现一次、旧键不再引用”。
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
FRONTEND = REPO_ROOT / "frontend" / "src"

FOOTERS = ["layouts/MainLayout.tsx", "pages/login/index.tsx"]


def _read(rel: str) -> str:
    return (FRONTEND / rel).read_text(encoding="utf-8")


def _built_on_block(text: str) -> str:
    """从 builtOnPrefix 往前找到它所在的 <Typography …> 开头，往后到 </Typography>"""
    prefix = text.index("footer.builtOnPrefix")
    start = text.rindex("<Typography", 0, prefix)
    end = text.index("</Typography>", prefix)
    return text[start:end]


@pytest.mark.parametrize("rel", FOOTERS)
def test_the_footer_is_a_single_line(rel: str) -> None:
    text = _read(rel)
    block = _built_on_block(text)
    assert "© {new Date().getFullYear()}" in block
    assert text.count("©") == 1
    assert "footer.copyright" not in text
