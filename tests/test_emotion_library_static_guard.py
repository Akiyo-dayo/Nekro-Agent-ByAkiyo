"""表情库服务层的静态守卫

## 为什么用 AST 扫源码

这一层有两条规矩没法靠"跑一遍"守住，因为犯规时功能**照常工作**：

1. **不许 import `plugins.*`**。核心被插件拖住就会在插件还没加载时算不出向量集合名、
   文件目录；而且插件是运行时可替换的，核心一旦引用它的常量，换插件就等于换核心的行为。
2. **会话身份只从 `resolve_session_owner` / `resolve_chat_owner` 取**。`ctx` 上的
   `chat_key` / `from_chat_key` / `adapter_key` 看起来与它们同源，但沙盒方法的参数里也有
   叫 `chat_key` 的东西；在服务层读这些属性，等于给"模型传入的标识"留了一条能长得一模一样的
   后门。这条规矩必须在语法层查：读属性与读参数在运行时的行为没法区分。

扫描逻辑写成接收源码文本的纯函数，因此可以对同一份文本做反向对照：故意塞一行 `ctx.chat_key`
或一句 `import plugins.builtin.emotion`，扫描器必须报出来。

## 第三条：文案

`emotion_not_found_message` 必须与插件改造前逐字相同（越权与真的查不到同文），
`PLATFORM_READONLY_MESSAGE` 是给模型的唯一例外说明。两条都用断言钉住，改一个字就红。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parent.parent
SERVICE = ROOT / "nekro_agent" / "services" / "emotion_library.py"

# 服务层里不许出现的会话身份属性：出现即说明有人绕开 `resource_scope` 自己取身份
FORBIDDEN_CTX_ATTRIBUTES = frozenset({"chat_key", "from_chat_key", "adapter_key", "instance_key"})

# 允许从 `nekro_agent.services.resource_scope` 导入的东西：解析入口本身
ALLOWED_RESOURCE_SCOPE_NAMES = frozenset({"resolve_session_owner", "resolve_chat_owner"})


def read_source() -> str:
    return SERVICE.read_text(encoding="utf-8")


def _is_ctx_like(node: ast.AST) -> bool:
    """判断一个表达式是不是"上下文对象"：`ctx`、`self.ctx`、`context`、`agent_ctx` 之类"""
    if isinstance(node, ast.Name):
        return node.id in {"ctx", "context", "agent_ctx", "agent_context"}
    if isinstance(node, ast.Attribute):
        return node.attr in {"ctx", "context", "agent_ctx", "agent_context"}
    return False


def find_attribute_reads(source: str, attributes: frozenset) -> List[Tuple[int, str]]:
    """扫出「读某个上下文对象上的某个属性」的位置，返回 `[(行号, "ctx.chat_key")]`

    只认「读」：`ctx.chat_key = x` 是写，不算取身份。但服务层也不该写它，所以写也一并报。
    """
    tree = ast.parse(source)
    found: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in attributes and _is_ctx_like(node.value):
            found.append((node.lineno, f"{ast.unparse(node.value)}.{node.attr}"))
    return sorted(found)


def find_plugin_imports(source: str) -> List[Tuple[int, str]]:
    """扫出任何形式的 `plugins.*` 导入（`import plugins.x`、`from plugins.x import y`、相对导入）"""
    tree = ast.parse(source)
    found: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "plugins" or alias.name.startswith("plugins."):
                    found.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "plugins" or module.startswith("plugins."):
                found.append((node.lineno, f"from {module} import ..."))
        elif isinstance(node, ast.Call):
            # 动态导入也不许：`__import__("plugins...")` / `importlib.import_module("plugins...")`
            name = ""
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name in {"__import__", "import_module"}:
                for argument in node.args:
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                        if argument.value == "plugins" or argument.value.startswith("plugins."):
                            found.append((node.lineno, f"{name}({argument.value!r})"))
    return sorted(found)


def find_resource_scope_imports(source: str) -> List[Tuple[int, str]]:
    """扫出从 `resource_scope` 导入的东西（只允许两个解析入口）"""
    tree = ast.parse(source)
    found: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("resource_scope"):
            for alias in node.names:
                found.append((node.lineno, alias.name))
    return sorted(found)


def find_calls(source: str, names: frozenset) -> List[Tuple[int, str]]:
    """扫出对某几个函数名 / 方法名的调用

    按调用而不是按字符串匹配：注释里提到这个名字是正常的（正是为了说明「为什么写死」），
    真正要挡的是**去问插件**。
    """
    tree = ast.parse(source)
    found: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = ""
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name in names:
            found.append((node.lineno, name))
    return sorted(found)


class TestStaticSourceRules:
    def test_the_service_does_not_import_plugins(self) -> None:
        assert find_plugin_imports(read_source()) == []

    def test_the_service_does_not_read_chat_identity_off_a_context(self) -> None:
        """身份只能来自 `resolve_session_owner` / `resolve_chat_owner`，不能自己从 ctx 上取"""
        assert find_attribute_reads(read_source(), FORBIDDEN_CTX_ATTRIBUTES) == []

    def test_the_only_resource_scope_imports_are_the_two_entrypoints(self) -> None:
        imported = {name for _, name in find_resource_scope_imports(read_source())}
        assert imported <= ALLOWED_RESOURCE_SCOPE_NAMES
        # 两个入口都得真的用上，否则「只允许导入两个」这条就变成空转
        assert imported == ALLOWED_RESOURCE_SCOPE_NAMES

    def test_the_service_does_not_carry_the_old_store_key(self) -> None:
        source = read_source()
        assert 'store_key="emotion_store"' not in source
        assert "store_key='emotion_store'" not in source
        # 旧键名出现在文档里也会引导读者去读插件存储，这里连字符串都不留
        assert '"emotion_store"' not in source

    def test_the_service_does_not_ask_the_plugin_for_the_collection_name(self) -> None:
        """注释里提到这个函数是说明「为什么写死」，但**调用**它就是把核心绑到插件上了"""
        assert find_calls(read_source(), frozenset({"get_vector_collection_name"})) == []

    def test_calling_the_collection_name_getter_is_reported(self) -> None:
        assert find_calls("name = plugin.get_vector_collection_name()\n", frozenset({"get_vector_collection_name"})) == [
            (1, "get_vector_collection_name"),
        ]

    def test_the_collection_name_is_spelled_out_as_a_constant(self) -> None:
        from nekro_agent.services import emotion_library

        assert emotion_library.EMOTION_VECTOR_COLLECTION == "KroMiose.emotion"


class TestReverseControl:
    """反向对照：把这些犯规写进一份源码，扫描器必须报出来

    没有这一组，「扫描器什么都没报」也可能只是因为它什么都没扫。
    """

    def test_reading_the_chat_key_off_the_context_is_reported(self) -> None:
        violating = """
async def resolve_emotion_scope(ctx):
    return ctx.chat_key
"""
        assert find_attribute_reads(violating, FORBIDDEN_CTX_ATTRIBUTES) == [(3, "ctx.chat_key")]

    def test_reading_the_from_chat_key_off_a_nested_attribute_is_reported(self) -> None:
        violating = """
async def resolve_emotion_scope(agent):
    return agent.ctx.from_chat_key
"""
        assert find_attribute_reads(violating, FORBIDDEN_CTX_ATTRIBUTES) == [(3, "agent.ctx.from_chat_key")]

    def test_reading_an_instance_key_off_the_context_is_reported(self) -> None:
        violating = """
def probe(ctx):
    return ctx.instance_key
"""
        assert [line for line, _ in find_attribute_reads(violating, FORBIDDEN_CTX_ATTRIBUTES)] == [3]

    def test_a_plain_local_variable_of_the_same_name_is_not_reported(self) -> None:
        """`chat_key` 作为普通局部变量 / 参数是正当的（`resolve_emotion_scope_for_chat` 就要它）"""
        fine = """
def resolve_emotion_scope_for_chat(chat_key):
    return chat_key, owner.chat_key
"""
        assert find_attribute_reads(fine, FORBIDDEN_CTX_ATTRIBUTES) == []

    def test_a_plugin_import_is_reported(self) -> None:
        assert find_plugin_imports("import plugins.builtin.emotion\n") == [(1, "import plugins.builtin.emotion")]
        assert find_plugin_imports("from plugins.builtin import emotion\n") == [
            (1, "from plugins.builtin import ..."),
        ]

    def test_a_dynamic_plugin_import_is_reported(self) -> None:
        assert find_plugin_imports('module = __import__("plugins.builtin.emotion")\n') == [
            (1, "__import__('plugins.builtin.emotion')"),
        ]

    def test_a_legitimate_import_is_not_reported(self) -> None:
        assert find_plugin_imports("from nekro_agent.services import emotion_library\n") == []


class TestWording:
    def test_not_found_matches_the_plugins_original_wording(self) -> None:
        """与插件里那句逐字相同：越权与真的查不到必须同文，模型才分不出两者"""
        from nekro_agent.services import emotion_library

        assert emotion_library.emotion_not_found_message("abcd1234") == (
            "Error: Emotion with ID 'abcd1234' not found"
        )

    def test_the_plugin_source_uses_the_service_wording_instead_of_its_own_copy(self) -> None:
        """插件不再自己抄一份那句话，而是调服务层那个函数

        B1 阶段插件还没接上服务层，只能靠「两处文案逐字相同」对齐；B2 之后插件里每个按
        不存在回答的路径都调 `emotion_library.emotion_not_found_message`，两处文案不可能
        再漂——这条用例因此改成钉「没有第二份实现」。
        """
        plugin_source = (ROOT / "plugins" / "builtin" / "emotion.py").read_text(encoding="utf-8")
        assert "emotion_library.emotion_not_found_message(" in plugin_source
        assert "Error: Emotion with ID '{emotion_id}' not found" not in plugin_source

    def test_the_platform_readonly_message_is_the_agreed_sentence(self) -> None:
        from nekro_agent.services import emotion_library

        assert emotion_library.PLATFORM_READONLY_MESSAGE == "这是平台方提供的表情，只能使用不能修改"


class TestVectorCollectionMatchesThePlugin:
    """集合名必须与插件算出来的一致：两边写死的字符串漂了，表情就会写进一个搜不到的集合"""

    def test_the_constant_equals_the_plugin_collection_name(self) -> None:
        from nekro_agent.services import emotion_library
        from plugins.builtin import emotion

        assert emotion_library.EMOTION_VECTOR_COLLECTION == emotion.plugin.get_vector_collection_name()

    def test_the_files_dir_is_the_plugin_data_dir_of_that_key(self) -> None:
        from nekro_agent.core.os_env import OsEnv
        from nekro_agent.services import emotion_library
        from plugins.builtin import emotion

        # 比的是默认值：`EMOTION_FILES_DIR` 本身被全局测试固件改道到了临时目录
        assert emotion_library.default_emotion_files_dir() == (
            Path(OsEnv.DATA_DIR) / "plugin_data" / emotion.plugin.get_vector_collection_name() / "emotions"
        )
