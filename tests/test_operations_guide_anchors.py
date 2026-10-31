"""代码、配置示例和脚本里的 `docs/operations.md §x.y` 引用必须都指向真实存在的小节

启动告警、日志和 `.env.example` 的注释会把运维引到 `docs/operations.md` 的某一小节。文档改了编号、
或者代码引用了一个不存在的小节，运维点进去就是死路——而且没有任何别的信号。

这个文件**随公开树一起导出**：它只读 `docs/operations.md`，不依赖任何内部文档。
"""

import importlib
import re
from pathlib import Path
from typing import Dict, List, Set

import pytest

REPO_ROOT = Path(__file__).parent.parent
OPERATIONS = REPO_ROOT / "docs" / "operations.md"
SCAN_ROOTS = ("nekro_agent", "plugins", "docker", "frontend/src", "docs", "scripts")
SCAN_FILES = (".env.example", ".env.prod", "run_bot.py", "nekro_log_hygiene.py", "sandbox.sh", "dockerfile")
SCAN_SUFFIXES = {".py", ".md", ".yml", ".yaml", ".sh", ".ps1", ".ts", ".tsx", ".json", ".example", ".toml", ""}
SECTION_REF = re.compile(r"docs/operations\.md`?\s*§\s*(\d+(?:\.\d+)?)")
SKIP_PARTS = {"node_modules", "__pycache__", ".venv", "static"}


def headings(doc: str) -> Set[str]:
    """文档里的编号小节：`## 1.` / `### 1.1 …` -> {"1", "1.1"}"""
    return {m.group(1) for m in re.finditer(r"^#{2,4} (\d+(?:\.\d+)?)[ .]", doc, re.M)}


def dangling_refs(sources: Dict[str, str], doc: str) -> List[str]:
    known = headings(doc)
    out: List[str] = []
    for path, text in sources.items():
        for m in SECTION_REF.finditer(text):
            if m.group(1) not in known:
                out.append(f"{path}: §{m.group(1)}")
    return out


def _collect() -> Dict[str, str]:
    files: Dict[str, str] = {}
    candidates: List[Path] = [REPO_ROOT / f for f in SCAN_FILES]
    for root in SCAN_ROOTS:
        base = REPO_ROOT / root
        if base.is_dir():
            candidates.extend(p for p in base.rglob("*") if p.is_file())
    for p in candidates:
        if not p.is_file() or SKIP_PARTS & set(p.relative_to(REPO_ROOT).parts) or p.suffix not in SCAN_SUFFIXES:
            continue
        try:
            files[p.relative_to(REPO_ROOT).as_posix()] = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    return files


def test_the_guide_exists_and_has_numbered_sections() -> None:
    doc = OPERATIONS.read_text(encoding="utf-8")
    assert {"1.1", "1.2", "1.3", "1.4", "1.5", "2.1", "2.2", "2.3", "3.1"} <= headings(doc)


def test_every_reference_in_the_tree_resolves_to_a_real_section() -> None:
    doc = OPERATIONS.read_text(encoding="utf-8")
    sources = _collect()
    refs = [p for p, t in sources.items() if SECTION_REF.search(t)]
    assert refs, "没有任何引用：要么引用被改没了，要么扫描范围不对（这条判据会因此恒真）"
    assert dangling_refs(sources, doc) == []


def test_the_criterion_can_see_a_dangling_reference() -> None:
    """反向对照：引用一个不存在的小节，必须被报出来"""
    doc = OPERATIONS.read_text(encoding="utf-8")
    bad = {"x.py": "详见 docs/operations.md §9.9", "y.py": "详见 docs/operations.md §1.1"}
    assert dangling_refs(bad, doc) == ["x.py: §9.9"]


def test_the_startup_alerts_point_at_the_guide() -> None:
    """启动告警的文案本身（不是某个注释）必须指向这份指南，而不是一份公开树里不存在的文档"""
    # 不能写 `import nekro_agent.core.logger as m`：`nekro_agent.core` 把 loguru 的 logger 对象绑在了同名属性上
    mod = importlib.import_module("nekro_agent.core.logger")
    for name in ("_JWT_WEAK_ALERT", "_JWT_SAME_ALERT", "_ADMIN_WEAK_ALERT"):
        assert "docs/operations.md" in getattr(mod, name), name


@pytest.mark.parametrize("doc", ["docker-guard.md", "multi-deploy-same-host.md"])
def test_the_pages_linked_from_the_guide_exist(doc: str) -> None:
    assert (REPO_ROOT / "docs" / doc).is_file()
    assert doc in OPERATIONS.read_text(encoding="utf-8")
