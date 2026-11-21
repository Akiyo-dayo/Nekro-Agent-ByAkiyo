"""邮箱服务层的静态守卫

## 两条规矩没法靠"跑一遍"守住

1. **会话身份只从 `resource_scope` 取**。`ctx.chat_key` / `ctx.from_chat_key` /
   `ctx.adapter_key` 看起来与 `resolve_session_owner(ctx)` 同源，但沙盒方法的参数里也有
   叫 `chat_key` 的东西；服务层读这些属性，等于给"模型传入的标识"留了一条能长得一模一样的
   后门。这条必须在语法层查：读属性与读参数在运行时没法区分。

2. **不许 import `plugins.*`**。核心被插件拖住就会在插件还没加载时算不出账户集合；
   而插件是运行时可替换的，核心一旦引用它的常量，换插件就等于换核心的行为。

## 附带钉住的还有两条"没有第二种来源"

- 平台方账户只从 `platform_accounts()` 这一处取（它背后是适配器配置），不在别处读
  `RECEIVE_ACCOUNTS`；
- 会话账户只从 `session_accounts` / `chat_accounts` 进出，后台只从 `instance_accounts`，
  三条都收敛到同一个内部函数。

扫描逻辑写成接收源码文本的纯函数，因此可以对同一份文本做反向对照：故意塞一行 `ctx.chat_key`
或一句 `import plugins.builtin.email`，扫描器必须报出来。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
SERVICE = ROOT / "nekro_agent" / "services" / "email_accounts.py"
OUTBOX = ROOT / "nekro_agent" / "services" / "email_outbox.py"
ROUTER = ROOT / "nekro_agent" / "routers" / "instance_email.py"

# 服务层里不许出现的会话身份属性
FORBIDDEN_CTX_ATTRIBUTES = frozenset({"chat_key", "from_chat_key", "adapter_key", "instance_key"})

# 允许从 `resource_scope` 导入的东西：两个解析入口本身
ALLOWED_RESOURCE_SCOPE_NAMES = frozenset({"resolve_session_owner", "resolve_chat_owner"})


def read_source(path: Path = SERVICE) -> str:
    return path.read_text(encoding="utf-8")


def _is_ctx_like(node: ast.AST) -> bool:
    """判断一个表达式是不是"上下文对象"：`ctx`、`self.ctx`、`context`、`agent_ctx` 之类"""
    if isinstance(node, ast.Name):
        return node.id in {"ctx", "context", "agent_ctx", "agent_context"}
    if isinstance(node, ast.Attribute):
        return node.attr in {"ctx", "context", "agent_ctx", "agent_context"}
    return False


def find_attribute_reads(source: str, attributes: frozenset = FORBIDDEN_CTX_ATTRIBUTES) -> List[Tuple[int, str]]:
    """扫出「读某个上下文对象上的某个属性」的位置，返回 `[(行号, "ctx.chat_key")]`"""
    tree = ast.parse(source)
    found: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in attributes and _is_ctx_like(node.value):
            found.append((node.lineno, f"{ast.unparse(node.value)}.{node.attr}"))
    return sorted(found)


def find_plugin_imports(source: str) -> List[Tuple[int, str]]:
    """扫出任何形式的 `plugins.*` 导入（含动态导入）"""
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
            name = ""
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name in {"__import__", "import_module"}:
                for argument in node.args:
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                        value = argument.value
                        if value == "plugins" or value.startswith("plugins."):
                            found.append((node.lineno, f"{name}({value!r})"))
    return sorted(found)


def find_resource_scope_imports(source: str) -> Set[str]:
    """扫出从 `nekro_agent.services.resource_scope` 导入了哪些名字"""
    names: Set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("resource_scope"):
            names.update(alias.name for alias in node.names)
    return names


def find_config_reads(source: str) -> List[int]:
    """扫出直接读 `RECEIVE_ACCOUNTS` 的位置

    平台方账户只许从 `platform_accounts()` 出；别处读那份配置等于绕开「合并与共享」这一层。
    """
    lines: List[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr == "RECEIVE_ACCOUNTS":
            lines.append(node.lineno)
        elif isinstance(node, ast.Constant) and node.value == "RECEIVE_ACCOUNTS":
            lines.append(node.lineno)
    return sorted(lines)


class TestNoSessionIdentityIsReadInTheServiceLayer:
    async def test_the_service_layer_never_touches_ctx_identity_attributes(self) -> None:
        assert find_attribute_reads(read_source()) == []

    async def test_the_router_never_touches_ctx_identity_attributes(self) -> None:
        assert find_attribute_reads(read_source(ROUTER)) == []

    async def test_the_scanner_does_report_an_injected_read(self) -> None:
        """反向对照：扫描器真的会报，而不是恒返回空"""
        polluted = read_source() + "\n\ndef _probe(ctx):\n    return ctx.chat_key\n"

        assert [line for line, _ in find_attribute_reads(polluted)] != []

    async def test_only_the_two_resolvers_are_imported_from_resource_scope(self) -> None:
        imported = find_resource_scope_imports(read_source())

        assert imported
        assert imported <= ALLOWED_RESOURCE_SCOPE_NAMES


class TestNoPluginImports:
    async def test_the_service_layer_does_not_import_plugins(self) -> None:
        assert find_plugin_imports(read_source()) == []

    async def test_the_outbox_does_not_import_plugins(self) -> None:
        """发信队列也不许 import 插件：两个插件键是字符串常量，由用例比对"""
        assert find_plugin_imports(read_source(OUTBOX)) == []

    async def test_the_router_does_not_import_plugins(self) -> None:
        assert find_plugin_imports(read_source(ROUTER)) == []

    async def test_the_scanner_does_report_an_injected_import(self) -> None:
        polluted = read_source() + "\n\nfrom plugins.builtin.email import something\n"

        assert find_plugin_imports(polluted) != []

    async def test_the_scanner_does_report_an_injected_dynamic_import(self) -> None:
        polluted = read_source() + "\n\nimport importlib\nimportlib.import_module('plugins.builtin.email')\n"

        assert find_plugin_imports(polluted) != []


class TestPlatformAccountsComeFromOnePlace:
    async def test_the_service_layer_reads_receive_accounts_only_in_the_loader(self) -> None:
        """`RECEIVE_ACCOUNTS` 只许在 `platform_accounts` 背后的那一处出现"""
        lines = find_config_reads(read_source())
        assert len(lines) <= 1

    async def test_the_outbox_never_reads_the_platform_account_config(self) -> None:
        """队列只认 `instance_accounts` 算出来的发件人，不自己翻平台方配置"""
        assert find_config_reads(read_source(OUTBOX)) == []


class TestThePublicSurfaceIsWhatC2AndC3Use:
    """`__all__` 是给下一轮用的清单：漏登记等于"下一轮找不到入口" """

    async def test_the_documented_entry_points_are_exported(self) -> None:
        from nekro_agent.services import email_accounts as mail

        required = {
            "AccountView",
            "SessionAccounts",
            "SessionSpeaker",
            "KIND_INSTANCE",
            "KIND_PLATFORM",
            "PURPOSE_SEND_EMAIL",
            "PURPOSE_FESTIVAL",
            "all_runtime_accounts",
            "assert_address_available",
            "assert_custom_mail_host_allowed",
            "assert_instance_account_quota",
            "attachment_root_for",
            "chat_accounts",
            "clear_default_sender_cache",
            "clear_other_default_senders",
            "default_festival_address",
            "default_sender_for_instance",
            "display_label_of",
            "instance_account_quota",
            "instance_accounts",
            "list_instance_accounts",
            "mask_email_address",
            "new_attachment_dir",
            "normalize_username",
            "platform_account_ref",
            "platform_account_shared_with",
            "platform_accounts",
            "record_send",
            "remove_runtime_account",
            "reset_send_log_state",
            "resolve_readable",
            "resolve_sendable",
            "send_via_account",
            "session_accounts",
            "session_speaker",
            "sync_runtime_account",
            "to_runtime_account",
            "validate_mail_address",
            "visible_label_for",
        }

        assert required <= set(mail.__all__)
        for name in required:
            assert hasattr(mail, name), name


class TestTheOutboxSurfaceIsWhatTheRouterAndPluginUse:
    """队列的公开面：路由与节日插件只认这些名字"""

    async def test_the_documented_entry_points_are_exported(self) -> None:
        from nekro_agent.services import email_outbox

        required = {
            "EMAIL_UTILS_PLUGIN_KEY",
            "FESTIVAL_PLUGIN_KEY",
            "FestivalRecipient",
            "enqueue_festival_batch",
            "festival_batch_key",
            "festival_recipients",
            "instance_festival_mail_ready",
            "list_batches",
            "pause_batch",
            "process_due_outbox",
            "resume_batch",
            "start_outbox_worker",
            "stop_outbox_worker",
            "void_batch",
        }

        assert required <= set(email_outbox.__all__)
        for name in required:
            assert hasattr(email_outbox, name), name

    async def test_the_plugin_keys_match_the_plugins_themselves(self) -> None:
        """两个插件键在核心里是字符串常量（不 import 插件）：漂移了队列就整批静默不发"""
        from nekro_agent.services import email_outbox
        from plugins.builtin import email_utils
        from plugins.builtin.festival_greeting import plugin as festival_plugin

        assert email_outbox.EMAIL_UTILS_PLUGIN_KEY == email_utils.plugin.key
        assert email_outbox.FESTIVAL_PLUGIN_KEY == festival_plugin.key


class TestAttachmentsDirsAreRecognisedByOneRule:
    """附件目录「能不能删」的判据只有一处"""

    async def test_the_rule_matches_only_the_instance_form(self) -> None:
        from nekro_agent.services.console.instance_manager import is_instance_attachment_dir

        assert is_instance_attachment_dir("_i1") is True
        assert is_instance_attachment_dir("_i1024") is True
        # 地址形态（平台方账户的目录、或"转为实例自有"时沿用下来的原目录）一律不认
        assert is_instance_attachment_dir("ops@example.test") is False
        assert is_instance_attachment_dir("") is False
        assert is_instance_attachment_dir("../etc") is False
        assert is_instance_attachment_dir("_i1/../x") is False

    async def test_the_name_helper_round_trips(self) -> None:
        from nekro_agent.services import email_accounts as mail
        from nekro_agent.services.console.instance_manager import is_instance_attachment_dir

        assert is_instance_attachment_dir(mail.new_attachment_dir(42)) is True
