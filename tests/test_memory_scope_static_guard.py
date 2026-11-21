"""检索出口不得自己拼可见范围条件（源码级守卫）。

## 为什么用 AST 扫源码

可见范围的判据必须只由 `MemoryScope`（及它导入的条件树模块）产出。靠 review 盯「这一处
有没有漏传」挡不住新增的第五个出口：漏传的后果是过滤条件静默消失、检索照常返回结果、
没有任何报错。写死一份出口清单也对新增恒真，所以这里直接扫源码。

扫描逻辑写成接收源码文本的纯函数，因此可以对同一份源码做反向对照：把一处
`scope.apply(...)` 改回改造前的 `DBMemParagraph.filter(workspace_id=..., **scope.as_filter())`，
或把一处 `FieldCondition(key="workspace_id")` 改成实例字段，扫描器必须报违规。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_WORLDBOOK_SERVICE_DIR = ROOT / "nekro_agent" / "services" / "worldbook"

# 世界书服务目录下的每个模块都要扫：那里的取书出口必须走 `worldbook_condition`，一旦有人
# 在服务里直接 `DBWbBook.filter(instance_key=...)`，授权就被绕开了。
_WORLDBOOK_SOURCES = tuple(sorted(_WORLDBOOK_SERVICE_DIR.glob("*.py")))

# 世界书路由也必须扫：它按 ID 取书 / 取条目时判的是归属，一旦有人在那里直接
# `DBWbBook.filter(instance_key=...)`，授权就被绕开了。清单里写死而不是「存在才扫」：
# 路由文件被删掉时这条扫描会静默地什么都不做，那比报错更糟。
_WORLDBOOK_ROUTER = ROOT / "nekro_agent" / "routers" / "worldbook.py"

SCANNED_SOURCES = (
    ROOT / "nekro_agent" / "services" / "memory" / "retriever.py",
    ROOT / "nekro_agent" / "services" / "memory" / "query_service.py",
    ROOT / "nekro_agent" / "services" / "memory" / "qdrant_manager.py",
    ROOT / "nekro_agent" / "services" / "memory" / "round_recall.py",
    ROOT / "nekro_agent" / "services" / "memory" / "admin_service.py",
    ROOT / "nekro_agent" / "routers" / "memory_admin.py",
    ROOT / "nekro_agent" / "routers" / "worldbook.py",
    ROOT / "plugins" / "builtin" / "memory_tools" / "main.py",
    ROOT / "nekro_agent" / "services" / "agent" / "templates" / "history.py",
) + _WORLDBOOK_SOURCES

SCOPE_FIELDS = frozenset(
    {
        "workspace_id",
        "instance_key",
        "preset_id",
        "origin_chat_key",
        "adapter_key",
        "origin_chat_type",
    }
)

# 世界书书表的隔离键比记忆表多两维：它按「绑定的那个人」与频道标识隔离。这两列在书表上
# 是**范围**而不是普通筛选，直接拼到查询上就等于绕开范围。
#
# 记忆表的清单刻意不扩大：摘要类表本来就把 `chat_key` 当普通筛选在用，扩大会把现有出口
# 全部误报。判据的差别来自两张表的隔离键不同，不是「世界书更严格」。
#
# `scope_kind` 不在里面：书表要按作用域类型筛出「这一轮该看哪几条腿」，那是范围之外的
# 普通筛选，不是授权判据。
_WORLDBOOK_EXTRA_FIELDS = frozenset({"chat_key", "person_account"})

_MODEL_METHODS = frozenset({"filter", "get", "get_or_none", "exclude", "first"})

# 按工作区删除不是检索出口：它删的是「这个工作区里的全部向量」，不是「给这个频道看什么」。
# 这是唯一允许出现 `FieldCondition(key="workspace_id")` 的地方。
_FIELD_CONDITION_WHITELIST = {
    ("delete_by_workspace", "workspace_id"): "按工作区删除向量，不是可见范围过滤",
}


def _scoped_model_root(node: ast.AST) -> str | None:
    """找出链式调用最左边的记忆 / 世界书模型名（`DBMem*` 与 `DBWb*`）。"""
    if isinstance(node, ast.Name):
        if node.id.startswith("DBMem") or node.id.startswith("DBWb"):
            return node.id
        return None
    if isinstance(node, ast.Attribute):
        return _scoped_model_root(node.value)
    if isinstance(node, ast.Call):
        return _scoped_model_root(node.func)
    return None


def _forbidden_keywords(root: str) -> frozenset[str]:
    """这个模型根上不许直接拼的关键字参数

    记忆表只有 `SCOPE_FIELDS`；世界书书表还要加上 `chat_key` / `person_account`——
    这两列是它的隔离键，拼到查询上等于绕开 `worldbook_condition`。
    """
    if root.startswith("DBWb"):
        return SCOPE_FIELDS | _WORLDBOOK_EXTRA_FIELDS
    return SCOPE_FIELDS


def _function_name(node: ast.AST) -> str | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name
    return None


def scan_source(source: str, *, filename: str = "<source>") -> list[str]:
    """扫描一份源码，返回违规描述；空列表表示干净。

    三条规则：

    - 不得调用 `.as_filter(...)` / `build_instance_filter(...)`，不得出现 `_scope_filter`；
    - 对 `DBMem*` / `DBWb*` 模型（含链式调用）调用 `filter` / `get` / `get_or_none` /
      `exclude` / `first` 时，关键字参数里不得出现可见范围字段（世界书模型还多三列，见
      :func:`_forbidden_keywords`）；
    - `qdrant_models.FieldCondition(key=...)` 的 key 不得是可见范围字段，白名单除外。
    """
    tree = ast.parse(source)
    violations: list[str] = []

    def visit(node: ast.AST, current_function: str | None) -> None:
        next_function = _function_name(node) or current_function

        if isinstance(node, ast.Attribute) and node.attr == "_scope_filter":
            violations.append(f"{filename}:{node.lineno} 出现了 _scope_filter 属性")
        if isinstance(node, ast.Name) and node.id == "_scope_filter":
            violations.append(f"{filename}:{node.lineno} 出现了 _scope_filter 变量")

        if isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Attribute):
                if function.attr == "as_filter":
                    violations.append(f"{filename}:{node.lineno} 调用了 .as_filter()")
                if function.attr in _MODEL_METHODS:
                    root = _scoped_model_root(function.value)
                    if root is not None:
                        forbidden = _forbidden_keywords(root)
                        for keyword in node.keywords:
                            if keyword.arg in forbidden:
                                violations.append(
                                    f"{filename}:{node.lineno} {root}.{function.attr}() "
                                    f"直接传了范围字段 {keyword.arg}",
                                )
            elif isinstance(function, ast.Name) and function.id == "build_instance_filter":
                violations.append(f"{filename}:{node.lineno} 调用了 build_instance_filter()")

            is_field_condition = (
                isinstance(function, ast.Attribute) and function.attr == "FieldCondition"
            ) or (isinstance(function, ast.Name) and function.id == "FieldCondition")
            if is_field_condition:
                for keyword in node.keywords:
                    if keyword.arg != "key" or not isinstance(keyword.value, ast.Constant):
                        continue
                    key = keyword.value.value
                    if not isinstance(key, str) or key not in SCOPE_FIELDS:
                        continue
                    whitelist_key = (next_function or "", key)
                    if whitelist_key in _FIELD_CONDITION_WHITELIST:
                        continue
                    violations.append(
                        f"{filename}:{node.lineno} FieldCondition(key={key!r}) 直接按范围字段过滤",
                    )

        for child in ast.iter_child_nodes(node):
            visit(child, next_function)

    visit(tree, None)
    return violations


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _replace_first_scope_apply(source: str, replacement: str) -> str:
    """把第一处 `self.scope.apply(...)` 整体替换掉，保留其余源码结构。"""
    marker = "self.scope.apply("
    start = source.index(marker)
    open_paren = source.index("(", start)
    depth = 0
    end = open_paren
    for index in range(open_paren, len(source)):
        char = source[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                end = index
                break
    else:
        raise AssertionError("没有找到成对的括号")
    return source[:start] + replacement + source[end + 1 :]


class TestRetrievalSourcesDoNotComposeScopeConditions:
    def test_every_scanned_source_is_clean(self) -> None:
        all_violations: list[str] = []
        for path in SCANNED_SOURCES:
            assert path.exists(), f"扫描清单里的文件不存在: {path}"
            all_violations.extend(scan_source(_source(path), filename=str(path)))
        assert all_violations == [], "检索出口自己拼了可见范围条件：" + "; ".join(all_violations)

    def test_the_scan_actually_sees_the_models(self) -> None:
        """对照组：确认扫描器真的看到了 `scope.apply`，否则上一条是空转。"""
        retriever_source = _source(SCANNED_SOURCES[0])
        assert "self.scope.apply(" in retriever_source
        assert scan_source(retriever_source, filename="retriever.py") == []

    def test_reverse_control_rejects_the_old_retriever_form(self) -> None:
        mutated = _replace_first_scope_apply(
            _source(SCANNED_SOURCES[0]),
            "DBMemParagraph.filter(workspace_id=self.workspace_id, **self.scope.as_filter())",
        )
        violations = scan_source(mutated, filename="retriever_mutated.py")
        assert any("as_filter" in item for item in violations), violations
        assert any("workspace_id" in item for item in violations), violations

    def test_reverse_control_rejects_a_scope_field_in_field_condition(self) -> None:
        source = _source(SCANNED_SOURCES[2])
        assert 'key="workspace_id"' in source
        mutated = source.replace('key="workspace_id"', 'key="instance_key"', 1)
        violations = scan_source(mutated, filename="qdrant_manager_mutated.py")
        assert any("instance_key" in item for item in violations), violations

    def test_whitelist_is_documented(self) -> None:
        for (function_name, field), reason in _FIELD_CONDITION_WHITELIST.items():
            assert function_name
            assert field in SCOPE_FIELDS
            assert reason.strip()


class TestWorldbookSourcesAreCovered:
    """世界书：书表的隔离键比记忆表多三列，扫描范围也扩到新模块"""

    def test_the_service_directory_is_scanned(self) -> None:
        """目录里的每个模块都在清单里；目录消失时这条会先红，避免扫描变成空转"""
        assert _WORLDBOOK_SERVICE_DIR.is_dir(), "世界书服务目录不存在"
        assert _WORLDBOOK_SOURCES, "世界书服务目录里没有可扫的模块"
        for path in _WORLDBOOK_SOURCES:
            assert path in SCANNED_SOURCES

    def test_worldbook_service_sources_are_clean(self) -> None:
        all_violations: list[str] = []
        for path in _WORLDBOOK_SOURCES:
            assert path.exists(), f"扫描清单里的文件不存在: {path}"
            all_violations.extend(scan_source(_source(path), filename=str(path)))
        assert all_violations == [], "世界书服务自己拼了可见范围条件：" + "; ".join(all_violations)

    def test_router_is_scanned(self) -> None:
        """路由文件必须真的在清单里，并且扫得干净

        「存在才扫」的写法在文件被删掉时会静默通过，所以这里直接要求它存在。
        """
        assert _WORLDBOOK_ROUTER.exists(), "世界书路由文件不存在"
        assert _WORLDBOOK_ROUTER in SCANNED_SOURCES
        assert scan_source(_source(_WORLDBOOK_ROUTER), filename="worldbook.py") == []

    @pytest.mark.parametrize(
        "snippet",
        [
            'DBWbBook.filter(instance_key="x")',
            'DBWbBook.filter(chat_key="c")',
            'DBWbBook.filter(person_account="p")',
            'DBWbBook.filter(adapter_key="a").first()',
            'DBWbBook.exclude(preset_id=7)',
        ],
    )
    def test_scope_fields_on_book_model_are_reported(self, snippet: str) -> None:
        violations = scan_source(snippet, filename="sample.py")
        assert violations, f"没有报出违规：{snippet}"

    @pytest.mark.parametrize(
        "snippet",
        [
            'DBWbBook.filter(enabled=True)',
            'DBWbBook.filter(scope_kind="domain")',
            "DBWbEntry.filter(book_id=1)",
            'DBWbEntry.filter(book__instance_key="x")',
        ],
    )
    def test_non_scope_filters_are_not_reported(self, snippet: str) -> None:
        """按普通列筛选、按作用域类型筛腿、以及经外键跨表过滤都不算「自己拼范围」"""
        assert scan_source(snippet, filename="sample.py") == []

    def test_join_style_filter_is_still_allowed(self) -> None:
        """`book__instance_key` 是关键字名，不是 `instance_key`：扫描器不该误报"""
        assert scan_source('DBWbEntry.filter(book__instance_key="x")', filename="sample.py") == []

    def test_memory_model_chat_key_is_not_reported(self) -> None:
        """记忆表的清单不扩大：摘要类表本来就把 `chat_key` 当普通筛选用"""
        assert scan_source('DBMemChannelSummary.filter(chat_key="c")', filename="sample.py") == []

    def test_worldbook_extra_fields_do_not_leak_into_memory_models(self) -> None:
        """反向确认：`person_account` 在记忆表上不报，在书表上报"""
        assert scan_source('DBMemParagraph.filter(person_account="p")', filename="sample.py") == []
        assert scan_source('DBWbBook.filter(person_account="p")', filename="sample.py") != []
