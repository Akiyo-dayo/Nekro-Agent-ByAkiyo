"""插件可见接口与上游 NekroAgent 的向后兼容契约

本分支的插件市场直连上游社区，第三方插件按上游签名编写。快照 `fixtures/upstream_plugin_api_<版本>.json`
由 `scripts/gen_upstream_plugin_api_snapshot.py` 从上游源码生成，这里断言本分支的签名是它的**向后兼容超集**：

1. 上游的位置参数，在这里按同样的顺序、同样的名字出现；
2. 上游能按关键字传的参数，这里也能按关键字传；
3. 上游可省略的参数，这里也可省略；
4. 这里多出来的参数一律带默认值（不能新增必填参数）；
5. 上游有 `*args` / `**kwargs` 的，这里也要有；
6. 同步 / 异步与上游一致；
7. 「严格」清单里的函数会被插件整个替换成固定参数的包装函数：位置参数必须与上游逐个相同，
   也不能多出上游没有的仅限关键字参数或 `**kwargs`（调用点一旦传它，插件的包装函数就接不住）。

改这些签名时，新参数只能加在末尾并带默认值，最好写成仅限关键字参数。
上游发新版后：重新生成快照、换掉 `SNAPSHOT`，再跑本文件。
"""

import importlib
import inspect
import json
from pathlib import Path
from typing import Any, List, NamedTuple

import pytest

SNAPSHOT = Path(__file__).parent / "fixtures" / "upstream_plugin_api_2_4_0.json"
DATA = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

_KINDS = {
    inspect.Parameter.POSITIONAL_ONLY: "posonly",
    inspect.Parameter.POSITIONAL_OR_KEYWORD: "pos",
    inspect.Parameter.VAR_POSITIONAL: "var",
    inspect.Parameter.KEYWORD_ONLY: "kwonly",
    inspect.Parameter.VAR_KEYWORD: "varkw",
}


class Param(NamedTuple):
    name: str
    kind: str
    optional: bool


def _resolve(key: str) -> Any:
    """`模块:限定名` -> 本分支里的函数对象；类里的方法取原始函数（不经描述符绑定）"""
    module_name, qualname = key.split(":")
    obj: Any = importlib.import_module(module_name)
    parts = qualname.split(".")
    for i, part in enumerate(parts):
        if inspect.isclass(obj):
            raw = inspect.getattr_static(obj, part)
            obj = raw.__func__ if isinstance(raw, (staticmethod, classmethod)) else raw
        else:
            obj = getattr(obj, part)
        assert obj is not None, f"{key}: 缺少 {'.'.join(parts[: i + 1])}"
    return obj


def _fork_params(func: Any) -> List[Param]:
    out: List[Param] = []
    for p in inspect.signature(func).parameters.values():
        kind = _KINDS[p.kind]
        out.append(Param(p.name, kind, kind in ("var", "varkw") or p.default is not inspect.Parameter.empty))
    return out


def _compat_problems(upstream: List[Param], fork: List[Param], *, strict: bool) -> List[str]:
    problems: List[str] = []
    up_pos = [p for p in upstream if p.kind in ("posonly", "pos")]
    fk_pos = [p for p in fork if p.kind in ("posonly", "pos")]
    # 只有这两类能按关键字传；仅限位置参数（`/` 之前）不能，否则上游的 `f(x=1)` 会在这里报错
    fk_by_name = {p.name: p for p in fork if p.kind in ("pos", "kwonly")}
    fk_kinds = {p.kind for p in fork}

    for i, u in enumerate(up_pos):
        if i >= len(fk_pos):
            if "var" not in fk_kinds:
                problems.append(f"上游第 {i + 1} 个位置参数 {u.name!r} 在这里不存在")
        elif fk_pos[i].name != u.name:
            problems.append(f"第 {i + 1} 个位置参数：上游是 {u.name!r}，这里是 {fk_pos[i].name!r}（插入或调换了位置参数）")

    for u in upstream:
        if u.kind in ("pos", "kwonly"):
            f = fk_by_name.get(u.name)
            if f is None:
                if "varkw" not in fk_kinds:
                    problems.append(f"上游参数 {u.name!r} 在这里无法按关键字传入")
            elif u.optional and not f.optional:
                problems.append(f"上游可省略的参数 {u.name!r} 在这里变成了必填")
        if u.kind in ("var", "varkw") and u.kind not in fk_kinds:
            problems.append(f"上游有 {'*' if u.kind == 'var' else '**'}{u.name}，这里没有")

    up_names = {u.name for u in upstream}
    for f in fork:
        if f.kind in ("posonly", "pos", "kwonly") and f.name not in up_names and not f.optional:
            problems.append(f"新增参数 {f.name!r} 没有默认值，照上游写法的调用会缺参")

    if strict:
        if [p.name for p in fk_pos] != [p.name for p in up_pos]:
            problems.append(
                f"会被插件替换的函数，位置参数必须与上游逐个相同：上游 {[p.name for p in up_pos]}，这里 {[p.name for p in fk_pos]}",
            )
        if ("var" in fk_kinds) != any(p.kind == "var" for p in upstream):
            problems.append("会被插件替换的函数，`*args` 的有无必须与上游一致")
        if ("varkw" in fk_kinds) != any(p.kind == "varkw" for p in upstream):
            problems.append("会被插件替换的函数，`**kwargs` 的有无必须与上游一致")
        extra_kwonly = [p.name for p in fork if p.kind == "kwonly" and p.name not in up_names]
        if extra_kwonly:
            problems.append(f"会被插件替换的函数不能新增仅限关键字参数：{extra_kwonly}")
    return problems


@pytest.mark.parametrize("key", sorted(DATA["functions"]))
def test_signature_is_backward_compatible(key: str) -> None:
    upstream = [Param(*p) for p in DATA["functions"][key]]
    fork = _fork_params(_resolve(key))
    problems = _compat_problems(upstream, fork, strict=key in DATA["strict"])
    assert not problems, f"{key} 与上游 {DATA['upstream_version']} 不兼容:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("key", sorted(DATA["functions"]))
def test_sync_or_async_matches_upstream(key: str) -> None:
    func = inspect.unwrap(_resolve(key))
    assert inspect.iscoroutinefunction(func) == (key in DATA["async_functions"]), (
        f"{key}：上游是{'异步' if key in DATA['async_functions'] else '同步'}函数，插件按那个写法调用"
    )


@pytest.mark.parametrize("module_name", sorted(DATA["exports"]))
def test_api_module_exports(module_name: str) -> None:
    module = importlib.import_module(module_name)
    missing = [n for n in DATA["exports"][module_name] if not hasattr(module, n)]
    assert not missing, f"{module_name} 缺少上游导出的名字: {missing}"


@pytest.mark.parametrize("class_key", sorted(DATA["attributes"]))
def test_public_attributes_exist(class_key: str) -> None:
    cls = _resolve(class_key)
    fields = getattr(cls, "model_fields", {})
    missing = [n for n in DATA["attributes"][class_key] if n not in fields and not hasattr(cls, n)]
    assert not missing, f"{class_key} 缺少上游的公开属性: {missing}"


def test_compat_rules_catch_the_known_breakages() -> None:
    """规则自检：用两次真实踩过的坑验证这里的判据确实能拦住它们"""
    upstream_hook = [Param(n, "pos", False) for n in ("adapter", "chat_key", "platform_channel", "platform_user", "platform_message", "content_text")]
    legacy_hook = [Param(n, "pos", False) for n in ("adapter", "chat_key", "platform_user", "platform_message", "content_text")]
    assert _compat_problems(upstream_hook, legacy_hook, strict=True)

    upstream_info = [Param("event", "pos", False)]
    required_bot = [Param("event", "pos", False), Param("bot", "pos", False)]
    optional_bot = [Param("event", "pos", False), Param("bot", "pos", True)]
    assert _compat_problems(upstream_info, required_bot, strict=False)
    assert not _compat_problems(upstream_info, optional_bot, strict=False)
    assert _compat_problems(upstream_info, optional_bot, strict=True), "被替换的函数连可选的位置参数也不能多"

    extra_kwonly = [Param("event", "pos", False), Param("trace", "kwonly", True)]
    assert not _compat_problems(upstream_info, extra_kwonly, strict=False), "被调用的函数可以加带默认值的仅限关键字参数"
    assert _compat_problems(upstream_info, extra_kwonly, strict=True), "被替换的函数不能加"

    positional_only = [Param("event", "posonly", False)]
    assert _compat_problems(upstream_info, positional_only, strict=False), "上游能按关键字传 event，这里改成仅限位置就接不住了"
