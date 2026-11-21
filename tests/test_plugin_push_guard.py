"""内置插件不得绕过插件推送判据的静态守卫

后台推送、消息发送、定时器与适配器转发都必须经过 `NekroPlugin` 的推送方法，
这样「插件在这个频道是否生效」只在一处判。例外只有沙盒方法里目标就是当前会话
（`_ctx.chat_key` / `_ctx.from_chat_key`）的回显——能被调用本身就说明插件生效；
其余例外必须逐条写明理由。

守卫本身也要能变红：文件末尾用一段故意违规的源码验证它确实抓得到，而不是永远返回空表。
"""

import ast
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
BUILTIN_ROOT = ROOT / "plugins" / "builtin"

Violation = Tuple[str, int, str, str]

_SEND_NAMES = {"send_text", "send_image", "send_file", "send_segments"}
_DIRECT_PUSH_NAMES = {"push_system_message", "push_untrusted_message", "schedule_agent_task"}

# (文件相对 plugins/builtin 的路径, 所在函数, 被调用的名字) -> 理由
# 理由必须写「为什么不走插件推送方法」，不能只写「历史原因」。
WHITELIST: Dict[Tuple[str, str, str], str] = {
    ("festival_greeting/handlers.py", "_set_festival_timer", "timer_service.set_timer"): (
        "节日定时器挂在专用伪频道 system_festival 上，不是任何一个用户频道；"
        "到点后由 _festival_callback 先判全局开关、再按每个目标频道过插件推送方法"
    ),
    ("festival_greeting/handlers.py", "_clear_all_festival_timers", "timer_service.set_timer"): (
        "清的是上面那个专用伪频道上的定时器，不向任何用户频道推送"
    ),
    ("judgement.py", "mute_user", "message.send_text"): (
        "禁言报告频道：发送前按租户校验，由同轮另一份改动负责"
    ),
}


def _enclosing_function(node: ast.AST, parents: Dict[ast.AST, ast.AST]) -> str:
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return "<module>"


def _target_chat_key(call: ast.Call) -> Optional[str]:
    if call.args:
        return ast.unparse(call.args[0])
    for keyword in call.keywords:
        if keyword.arg == "chat_key":
            return ast.unparse(keyword.value)
    return None


def _is_current_channel(expr: Optional[str]) -> bool:
    return expr in {"_ctx.chat_key", "_ctx.from_chat_key", "ctx.chat_key", "ctx.from_chat_key"}


def _forbidden_callee(call: ast.Call) -> Optional[str]:
    name = ast.unparse(call.func)
    parts = name.split(".")
    last = parts[-1]

    if name.startswith("message_service.") and last in _DIRECT_PUSH_NAMES:
        return name
    if isinstance(call.func, ast.Name) and name in _DIRECT_PUSH_NAMES:
        return name
    if last in _SEND_NAMES:
        # `message.send_text`（from nekro_agent.api import message）、`_ctx.ms.send_text`、
        # `nekro_agent.api.message.send_text` 与直接 import 进来的 `send_text` 都是同一个出口。
        # `ctx.send_text` / `_ctx.send_text` 是「发到当前会话」的便捷方法，不在此列。
        if (
            name.startswith("message.")
            or ".ms." in name
            or name.startswith("nekro_agent.api.message.")
            or isinstance(call.func, ast.Name)
        ):
            return name
    if last == "set_timer" and (name == "timer_service.set_timer" or name.endswith(".timer_service.set_timer")):
        return name
    if last == "forward_message" and parts and parts[0] == "adapter":
        return name
    return None


def find_violations(relative_path: str, source: str) -> List[Violation]:
    """返回 `(文件, 行号, 函数, 被调用名字)`；白名单与当前会话例外在这里一起放行"""
    tree = ast.parse(source, filename=relative_path)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    found: List[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _forbidden_callee(node)
        if callee is None:
            continue
        function = _enclosing_function(node, parents)
        if _is_current_channel(_target_chat_key(node)):
            continue
        if (relative_path, function, callee) in WHITELIST:
            continue
        found.append((relative_path, node.lineno, function, callee))
    return found


def format_violations(violations: List[Violation]) -> str:
    return "\n".join(f"{path}:{line} {function} 直接调用了 {callee}" for path, line, function, callee in violations)


def scan_builtin_plugins() -> List[Violation]:
    violations: List[Violation] = []
    for path in sorted(BUILTIN_ROOT.rglob("*.py")):
        relative = path.relative_to(BUILTIN_ROOT).as_posix()
        violations.extend(find_violations(relative, path.read_text(encoding="utf-8")))
    return violations


def test_no_builtin_plugin_bypasses_the_push_guard() -> None:
    violations = scan_builtin_plugins()
    assert not violations, "内置插件里仍有绕过插件推送判据的直接调用:\n" + format_violations(violations)


def test_whitelist_entries_all_have_reasons() -> None:
    assert all(reason.strip() for reason in WHITELIST.values())


def test_the_guard_catches_a_deliberate_violation() -> None:
    sample = """
from nekro_agent.services.message_service import message_service

async def leak(chat_key):
    await message_service.push_system_message(chat_key, "hi")
"""
    violations = find_violations("<sample>", sample)

    assert violations, "故意违规的样例没有被抓到，守卫等于没写"
    path, line, function, callee = violations[0]
    assert (path, line, function, callee) == ("<sample>", 5, "leak", "message_service.push_system_message")
    assert "<sample>:5 leak" in format_violations(violations)
