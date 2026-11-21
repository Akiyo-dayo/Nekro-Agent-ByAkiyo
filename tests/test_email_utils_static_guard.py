"""`email_utils` 的两条规矩只能靠语法层守

## 为什么是静态检查

1. **账户不许自己翻**。改造前每个方法都直接遍历适配器的 `RECEIVE_ACCOUNTS`，于是租户群里的
   会话能列出、读到、用上平台方配的全部邮箱。这类写法在运行时**看不出区别**：它照样能跑通，
   只是答案不对（多给了）。所以只能查源码里还有没有 `RECEIVE_ACCOUNTS` 与
   `xxx.config.RECEIVE_` 这两种形态。

2. **偏好方法的身份不许是参数**。三个 `*_my_mail_preference` 一旦多接收一个
   `user_id` / `platform_userid` / `account` 之类的参数（哪怕先写成可选），群友就能让 Bot
   替别人改地址或退订。这条同样只能查签名：运行时调用方永远会传，测试也就永远过。

扫描函数都接收源码文本，因此可以对同一份文本做反向对照：故意塞一行
`config.RECEIVE_ACCOUNTS`、或给偏好方法加一个 `user_id`，扫描器必须报出来——否则它可能
只是个恒返回空的东西，本文件自己就成了摆设。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "builtin" / "email_utils.py"

# 偏好方法的身份只能来自宿主：除了 `_ctx` 之外，只允许这几个业务参数
ALLOWED_PREFERENCE_PARAMETERS = frozenset({"email", "festival"})

# 参数名里出现这些片段就说明"它在接受一个身份"（`email` 自己不算，它是要记下的地址）
IDENTITY_FRAGMENTS = ("user", "uid", "account", "chat_key", "openid", "sender")


def read_source(path: Path = PLUGIN) -> str:
    return path.read_text(encoding="utf-8")


def find_platform_account_reads(source: str) -> List[Tuple[int, str]]:
    """扫出 `RECEIVE_ACCOUNTS` 的一切形态：属性读取与字符串常量都算

    字符串也算：`getattr(config, "RECEIVE_ACCOUNTS")` 与 `adapter.config.RECEIVE_ACCOUNTS`
    是同一个口子，只是绕了一层。
    """
    found: List[Tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr == "RECEIVE_ACCOUNTS":
            found.append((node.lineno, ast.unparse(node)))
        elif isinstance(node, ast.Constant) and node.value == "RECEIVE_ACCOUNTS":
            found.append((node.lineno, repr(node.value)))
    return sorted(found)


def find_adapter_config_reads(source: str) -> List[Tuple[int, str]]:
    """扫出 `xxx.config.RECEIVE_*`：直接翻适配器配置的另一种写法"""
    found: List[Tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Attribute):
            continue
        if not str(node.attr).startswith("RECEIVE_"):
            continue
        if isinstance(node.value, ast.Attribute) and node.value.attr == "config":
            found.append((node.lineno, ast.unparse(node)))
    return sorted(found)


def find_preference_method_parameters(source: str) -> Set[str]:
    """扫出三个偏好方法除 `_ctx` 之外的参数名"""
    names: Set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not str(node.name).endswith("_my_mail_preference"):
            continue
        arguments = node.args
        for parameter in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]:
            if parameter.arg == "_ctx":
                continue
            names.add(parameter.arg)
    return names


def find_preference_methods(source: str) -> Set[str]:
    """扫出实际存在的偏好方法名（少一个的话下面的参数检查会静默通过）"""
    return {
        str(node.name)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and str(node.name).endswith("_my_mail_preference")
    }


def find_adapter_account_iteration(source: str) -> List[Tuple[int, str]]:
    """扫出 `all_accounts()` / `email_clients` 之外的"自己数账户"写法

    只认 `all_accounts`：发信出口要按地址找运行时账户，那是服务层的事；插件里出现它，
    说明又在插件内部拼了一份账户清单。
    """
    found: List[Tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "all_accounts":
            found.append((node.lineno, ast.unparse(node)))
    return sorted(found)


class TestThePluginNeverReadsThePlatformAccountConfig:
    async def test_there_is_no_receive_accounts_anywhere(self) -> None:
        assert find_platform_account_reads(read_source()) == []

    async def test_there_is_no_adapter_config_read(self) -> None:
        assert find_adapter_config_reads(read_source()) == []

    async def test_there_is_no_private_account_list(self) -> None:
        """账户清单只有一个来源：服务层的 `session_accounts` / `instance_accounts`"""
        assert find_adapter_account_iteration(read_source()) == []

    async def test_the_scanner_reports_an_injected_read(self) -> None:
        """反向对照：扫描器真的会报"""
        polluted = read_source() + "\n\ndef _probe(adapter):\n    return adapter.config.RECEIVE_ACCOUNTS\n"

        assert find_platform_account_reads(polluted) != []
        assert find_adapter_config_reads(polluted) != []

    async def test_the_scanner_reports_a_getattr_form(self) -> None:
        polluted = read_source() + '\n\ndef _probe(cfg):\n    return getattr(cfg, "RECEIVE_ACCOUNTS")\n'

        assert find_platform_account_reads(polluted) != []

    async def test_the_scanner_reports_an_injected_account_list(self) -> None:
        polluted = read_source() + "\n\nasync def _probe(adapter):\n    return await adapter.all_accounts()\n"

        assert find_adapter_account_iteration(polluted) != []


class TestThePreferenceMethodsTakeNoIdentity:
    async def test_all_three_methods_are_present(self) -> None:
        """少一个的话下面的检查会静默通过：缺一个就没人守它"""
        assert find_preference_methods(read_source()) == {
            "set_my_mail_preference",
            "get_my_mail_preference",
            "forget_my_mail_preference",
        }

    async def test_no_parameter_smells_like_an_identity(self) -> None:
        parameters = find_preference_method_parameters(read_source())

        assert parameters <= ALLOWED_PREFERENCE_PARAMETERS, parameters

    async def test_the_scanner_reports_an_injected_user_id(self) -> None:
        """反向对照：多一个 `user_id` 必须被报出来"""
        polluted = read_source().replace(
            "async def get_my_mail_preference(_ctx: AgentCtx) -> str:",
            "async def get_my_mail_preference(_ctx: AgentCtx, user_id: str = \"\") -> str:",
        )
        assert polluted != read_source(), "替换没生效，这条对照没有意义"

        parameters = find_preference_method_parameters(polluted)

        assert "user_id" in parameters
        assert not parameters <= ALLOWED_PREFERENCE_PARAMETERS
        assert any(fragment in "user_id" for fragment in IDENTITY_FRAGMENTS)

    async def test_the_scanner_reports_an_injected_account_parameter(self) -> None:
        polluted = read_source().replace(
            "async def forget_my_mail_preference(_ctx: AgentCtx) -> Dict[str, Any]:",
            "async def forget_my_mail_preference(_ctx: AgentCtx, account: str = \"\") -> Dict[str, Any]:",
        )
        assert polluted != read_source()

        assert find_preference_method_parameters(polluted) - ALLOWED_PREFERENCE_PARAMETERS == {"account"}


class TestTheMethodSurfaceIsDocumented:
    """插件方法的清单与文案：漏一个方法等于模型看不到那个能力"""

    async def test_get_my_mail_preference_is_the_agent_method(self) -> None:
        """只有它要回一段话给模型接着想；另外两个是工具，回一个结果字典就够了"""
        from nekro_agent.services.plugin.schema import SandboxMethodType
        from plugins.builtin import email_utils

        assert email_utils.get_my_mail_preference._method_type == SandboxMethodType.AGENT  # noqa: SLF001
        for name in ("set_my_mail_preference", "forget_my_mail_preference"):
            assert getattr(email_utils, name)._method_type == SandboxMethodType.TOOL  # noqa: SLF001

    async def test_send_email_documents_the_reason(self) -> None:
        from plugins.builtin import email_utils

        assert "reason" in email_utils.send_email.__doc__
        assert "理由" in email_utils.send_email.__doc__

    async def test_the_plugin_description_names_the_preference_methods(self) -> None:
        from plugins.builtin import email_utils

        assert "收件地址" in email_utils.plugin.description
