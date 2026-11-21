"""表情插件的静态守卫

## 为什么这些规矩要在语法层查

表情插件本轮接上了服务层（`nekro_agent/services/emotion_library.py`），于是有几条规矩一旦
破了**功能照常工作**，跑用例也未必红：

1. **旧版 JSON 的键只许在一个地方出现**。`store_key="emotion_store"` 是改造前的整库；库里
   已经是权威，但那一行不能改也不能删（回滚凭据、来源记录）。导入逻辑只该在启动时读它一次，
   别处再读就等于悄悄把库表与 JSON 变成两个真相。
2. **不许自己算向量集合名**。`plugin.get_vector_collection_name()` 在插件里调一次，服务层
   那份常量就不再是唯一口径；改插件 key 时两处会分叉，表情会写进一个搜不到的集合。
3. **不许有模块级可变单例**。改造前 `store` / `store_dir` 是模块级对象，测试改道数据目录后
   它仍指着旧路径；这类单例还会让「按实例」的配置与文件目录失去意义。
4. **会话身份只从服务层解析**。`_ctx.chat_key` / `_ctx.from_chat_key` 读起来与宿主签发值
   同源，但沙盒方法的参数里也有叫 `chat_key` 的东西；一旦有人拿它们判归属，模型就有了一条
   长得一模一样的后门。唯一的例外是记录「这条表情是哪个会话收的」——那只是一个字符串字段，
   不参与任何判定，所以只允许它以 `collected_chat_key=` 关键字实参的形态出现。

每条规矩都配一个**故意违规的样例**，证明判据不是恒真的空转。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "builtin" / "emotion.py"
LEGACY_STORE_KEY = "emotion_store"
LEGACY_STORE_FUNCTION = "_load_legacy_store_raw"


def read_source() -> str:
    return PLUGIN.read_text(encoding="utf-8")


def find_legacy_store_key_reads(source: str) -> List[Tuple[str, int]]:
    """找出所有出现旧版 JSON 键的位置，返回 `[(所在函数名, 行号)]`

    模块级（不在任何函数里）出现也算：那样就没有「只在启动时读一次」可言了。
    """
    tree = ast.parse(source)
    found: List[Tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == LEGACY_STORE_KEY:
            found.append((_enclosing_function(tree, node.lineno), node.lineno))
    return sorted(found, key=lambda item: item[1])


def _enclosing_function(tree: ast.AST, lineno: int) -> str:
    """行号所在的函数名；模块级返回空串"""
    best = ""
    best_span = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= lineno <= end and (best_span is None or end - node.lineno < best_span):
                best = node.name
                best_span = end - node.lineno
    return best


def find_vector_collection_name_calls(source: str) -> List[int]:
    """找出所有调用 `get_vector_collection_name()` 的行号（含 `self.` / `plugin.` 前缀）"""
    tree = ast.parse(source)
    found: List[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "get_vector_collection_name":
                found.append(node.lineno)
    return sorted(found)


# 框架要求的两个模块级对象：插件本体与按实例解析的配置代理。它们不是「可变单例」——
# 前者由插件管理器持有、后者每次读都按 `instance_scope` 现解析，恰好是本轮要的口径。
FRAMEWORK_SINGLETONS = frozenset({"plugin", "emotion_config"})


def _is_immutable_literal(node: ast.expr) -> bool:
    """判断一个表达式是不是「导入时求值一次、之后不会再变」的常量

    数字、字符串、f-string、常量之间的四则运算、以及元素全是常量的容器字面量都算。
    **空容器不算**：`{}` 与 `[]` 正是最容易长成全局状态的那两个东西。
    """
    if isinstance(node, (ast.Constant, ast.JoinedStr)):
        return True
    if isinstance(node, ast.BinOp):
        return _is_immutable_literal(node.left) and _is_immutable_literal(node.right)
    if isinstance(node, ast.UnaryOp):
        return _is_immutable_literal(node.operand)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return bool(node.elts) and all(_is_immutable_literal(element) for element in node.elts)
    if isinstance(node, ast.Dict):
        if not node.keys:
            return False
        return all(
            key is not None and _is_immutable_literal(key) and _is_immutable_literal(value)
            for key, value in zip(node.keys, node.values)
        )
    return False


def find_module_level_mutable_assignments(source: str) -> List[Tuple[int, str]]:
    """找出模块级的可变单例赋值，返回 `[(行号, 目标名)]`

    判据是「值不是导入时就能定死的常量」：`MAX = 10`、`TYPES = {'png': ...}` 这类无所谓；
    `store = {}`、`store_dir = Path(...)`、`cache = SomeClass()` 都算——它们会在导入时求值一次，
    之后全进程共享同一份可变状态，测试改道数据目录也就不再生效。
    """
    tree = ast.parse(source)
    found: List[Tuple[int, str]] = []
    for node in tree.body:
        targets: List[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        if value is None or _is_immutable_literal(value):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id not in FRAMEWORK_SINGLETONS:
                found.append((node.lineno, target.id))
    return sorted(found)


def find_ctx_identity_reads(source: str) -> List[Tuple[int, str, bool]]:
    """找出对 `_ctx.chat_key` / `_ctx.from_chat_key` 的读取，返回 `[(行号, 表达式, 是否合规)]`

    合规只有一种形态：作为 `collected_chat_key=` 的关键字实参（记录来源会话，不参与判定）。
    """
    tree = ast.parse(source)
    allowed_keywords: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "collected_chat_key":
                allowed_keywords.add(id(keyword.value))

    found: List[Tuple[int, str, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr not in {"chat_key", "from_chat_key"}:
            continue
        if not isinstance(node.value, ast.Name) or node.value.id != "_ctx":
            continue
        found.append((node.lineno, ast.unparse(node), id(node) in allowed_keywords))
    return sorted(found)


class TestLegacyStoreKey:
    def test_the_key_appears_only_in_the_import_helper(self) -> None:
        reads = find_legacy_store_key_reads(read_source())
        assert reads, "导入逻辑不见了：旧版 JSON 就再也导不进来"
        assert {name for name, _lineno in reads} == {LEGACY_STORE_FUNCTION}, reads

    def test_a_stray_read_is_reported(self) -> None:
        sample = (
            "async def _load_legacy_store_raw():\n"
            '    return await plugin.store.get(store_key="emotion_store")\n'
            "\n"
            "async def other():\n"
            '    return await plugin.store.get(store_key="emotion_store")\n'
        )
        reads = find_legacy_store_key_reads(sample)
        assert [name for name, _lineno in reads] == ["_load_legacy_store_raw", "other"]

    def test_a_module_level_read_is_reported(self) -> None:
        sample = 'STORE = "emotion_store"\n'
        assert find_legacy_store_key_reads(sample) == [("", 1)]


class TestVectorCollectionName:
    def test_the_plugin_never_computes_the_collection_name(self) -> None:
        """集合名只有服务层那一份常量；插件算一次就多一个会分叉的口径"""
        assert find_vector_collection_name_calls(read_source()) == []

    def test_a_call_is_reported(self) -> None:
        assert find_vector_collection_name_calls("name = plugin.get_vector_collection_name()\n") == [1]


class TestNoMutableSingletons:
    def test_there_is_no_module_level_mutable_singleton(self) -> None:
        found = find_module_level_mutable_assignments(read_source())
        assert found == [], f"模块级可变单例会让改道数据目录失效：{found}"

    def test_a_mutable_singleton_is_reported(self) -> None:
        sample = "store_dir = Path('/tmp')\ncache = {}\nqueue = []\nNAME = 'x'\nMAX = 3\n"
        assert [name for _lineno, name in find_module_level_mutable_assignments(sample)] == [
            "store_dir",
            "cache",
            "queue",
        ]

    def test_a_literal_container_is_not_reported(self) -> None:
        """媒体类型表这种「全是字面量的字典」是常量，不该被误报"""
        sample = "TYPES = {'png': 'image/png'}\nLIMIT = 10 * 1024 * 1024\n"
        assert find_module_level_mutable_assignments(sample) == []


class TestSessionIdentity:
    def test_the_only_read_is_the_collected_chat_key_record(self) -> None:
        reads = find_ctx_identity_reads(read_source())
        assert reads, "一条都不读说明这条守卫已经和代码脱节，需要重新确认判据"
        offenders = [entry for entry in reads if not entry[2]]
        assert offenders == [], f"会话身份只许由服务层解析，这里出现了自取：{offenders}"

    def test_a_ownership_read_is_reported(self) -> None:
        sample = 'key = _ctx.chat_key\nother = _ctx.from_chat_key\n'
        assert [ok for _lineno, _expr, ok in find_ctx_identity_reads(sample)] == [False, False]

    def test_the_record_keyword_is_allowed(self) -> None:
        sample = "item = await create_item(set_id=1, collected_chat_key=_ctx.from_chat_key)\n"
        assert find_ctx_identity_reads(sample) == [(1, "_ctx.from_chat_key", True)]

    def test_the_same_name_on_another_object_is_ignored(self) -> None:
        """`context.chat_key` 在命令里是宿主给的那条频道键，不是会话身份，不该被这条守卫管"""
        assert find_ctx_identity_reads("key = context.chat_key\n") == []


class TestWhiteboardKeyMatches:
    def test_the_service_key_is_the_whiteboard_plugins_key(self) -> None:
        """划转实例时要删白板令牌，键对不上就会留下打不开的旧链接与删不掉的存储行"""
        from nekro_agent.services import emotion_library
        from plugins.builtin import whiteboard

        assert emotion_library.WHITEBOARD_PLUGIN_KEY == whiteboard.plugin.key
