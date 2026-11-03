"""从上游 NekroAgent 源码树生成「插件可见接口」的签名快照

用法：

    python scripts/gen_upstream_plugin_api_snapshot.py <上游源码根目录> <版本号> > tests/fixtures/upstream_plugin_api_<版本>.json

（`tests/data` 会被 `.gitignore` 的 `data` 规则忽略，快照必须放在 `tests/fixtures`。）

只读源码的语法树，不导入、不执行上游代码。产物供 `tests/test_upstream_plugin_compat.py` 使用：
它断言本分支的签名是上游的向后兼容超集，插件照上游签名写的调用与包装函数在这里都接得住。

快照范围：

- `nekro_agent.api.*` 各模块导出的名字，以及模块内定义的函数签名；
- `NekroPlugin`、`AgentCtx` 的公开方法签名与公开属性名；
- 市场插件实际调用过的内部函数（`CALLED`）：按同一套兼容规则检查；
- 市场插件实际替换过的内部函数（`STRICT`）：除了兼容规则，位置参数还必须与上游逐个相同，
  因为插件的包装函数通常写成固定个数的位置参数，框架多传一个也会出错。
"""

import ast
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

API_MODULES = ["core", "i18n", "message", "plugin", "recurring_timer", "schemas", "signal", "timer"]
CLASSES = {
    "nekro_agent.services.plugin.base:NekroPlugin": "nekro_agent/services/plugin/base.py",
    "nekro_agent.schemas.agent_ctx:AgentCtx": "nekro_agent/schemas/agent_ctx.py",
}
CALLED = {
    "nekro_agent.adapters.onebot_v11.tools.onebot_util:get_chat_info": "nekro_agent/adapters/onebot_v11/tools/onebot_util.py",
    "nekro_agent.adapters.onebot_v11.tools.onebot_util:get_chat_info_old": "nekro_agent/adapters/onebot_v11/tools/onebot_util.py",
    "nekro_agent.adapters.onebot_v11.core.bot:get_bot": "nekro_agent/adapters/onebot_v11/core/bot.py",
}
STRICT = {
    "nekro_agent.adapters.interface.collector:_try_handle_command": "nekro_agent/adapters/interface/collector.py",
    "nekro_agent.services.message_service:MessageService.push_bot_message": "nekro_agent/services/message_service.py",
    "nekro_agent.services.message_service:MessageService._run_chat_agent_task": "nekro_agent/services/message_service.py",
    "nekro_agent.api.message:send_text": "nekro_agent/api/message.py",
}


def params(fn: Any) -> List[List[Any]]:
    a = fn.args
    out: List[List[Any]] = []
    pos = a.posonlyargs + a.args
    first_default = len(pos) - len(a.defaults)
    for i, p in enumerate(pos):
        out.append([p.arg, "posonly" if i < len(a.posonlyargs) else "pos", i >= first_default])
    if a.vararg:
        out.append([a.vararg.arg, "var", True])
    for p, d in zip(a.kwonlyargs, a.kw_defaults):
        out.append([p.arg, "kwonly", d is not None])
    if a.kwarg:
        out.append([a.kwarg.arg, "varkw", True])
    return out


def is_property(fn: Any) -> bool:
    for d in fn.decorator_list:
        if isinstance(d, ast.Name) and d.id in ("property", "cached_property"):
            return True
        if isinstance(d, ast.Attribute) and d.attr in ("setter", "getter", "deleter"):
            return True
    return False


def find(tree: ast.Module, qualname: str) -> Optional[Any]:
    parts = qualname.split(".")
    body: List[Any] = tree.body
    node: Optional[Any] = None
    for part in parts:
        node = next(
            (n for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == part),
            None,
        )
        if node is None:
            return None
        body = getattr(node, "body", [])
    return node


def main(root: Path, version: str) -> Dict[str, Any]:
    parse = lambda rel: ast.parse((root / rel).read_text(encoding="utf-8-sig"))  # noqa: E731
    snapshot: Dict[str, Any] = {
        "upstream_version": version,
        "exports": {},
        "functions": {},
        "async_functions": [],
        "attributes": {},
        "strict": [],
    }

    for name in API_MODULES:
        mod = f"nekro_agent.api.{name}"
        tree = parse(f"nekro_agent/api/{name}.py")
        exported: List[str] = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
                exported = [e.value for e in node.value.elts if isinstance(e, ast.Constant)]  # type: ignore[attr-defined]
        if not exported:
            for node in tree.body:
                if isinstance(node, ast.ImportFrom):
                    exported += [al.asname or al.name for al in node.names if al.name != "*"]
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    exported.append(node.name)
        snapshot["exports"][mod] = sorted({n for n in exported if not n.startswith("_")})
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                snapshot["functions"][f"{mod}:{node.name}"] = params(node)

    for key, rel in CLASSES.items():
        cls = find(parse(rel), key.split(":")[1])
        assert isinstance(cls, ast.ClassDef), key
        attrs: List[str] = []
        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if is_property(node):
                    if not node.name.startswith("_"):
                        attrs.append(node.name)
                elif node.name == "__init__" or not node.name.startswith("_"):
                    snapshot["functions"][f"{key}.{node.name}"] = params(node)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and not node.target.id.startswith("_"):
                attrs.append(node.target.id)
        snapshot["attributes"][key] = sorted(set(attrs))

    for key, rel in {**CALLED, **STRICT}.items():
        node = find(parse(rel), key.split(":")[1])
        assert node is not None, key
        snapshot["functions"][key] = params(node)
    snapshot["strict"] = sorted(STRICT)

    # 同步 / 异步也是签名的一部分：调用方写的是 `await f()` 还是 `f()`
    for key in snapshot["functions"]:
        module_name, qualname = key.split(":")
        rel = module_name.replace(".", "/") + ".py"
        node = find(parse(rel), qualname)
        if isinstance(node, ast.AsyncFunctionDef):
            snapshot["async_functions"].append(key)
    snapshot["async_functions"].sort()

    return snapshot


if __name__ == "__main__":
    print(json.dumps(main(Path(sys.argv[1]), sys.argv[2]), ensure_ascii=False, indent=1, sort_keys=True))
