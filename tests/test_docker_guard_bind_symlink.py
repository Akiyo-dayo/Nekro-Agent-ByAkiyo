"""docker-guard：bind source 符号链接绕过的回归用例。

## 被钉住的洞

guard 过去对 bind source 只做 `normpath` + 字符串前缀判据，**看不穿宿主上的符号链接**：
`<数据目录>/evil -> /` 的 source 词法上在数据目录内，Docker 挂载时却跟随链接，等于把宿主 `/` 交了出去。
`..` 同理：`<数据目录>/lnk/../y` 词法上是 `<数据目录>/y`，物理上是 `lnk` 目标的上一级。

## 用例分层（同一个契约用三种方式钉）

1. **解析器表驱动（内存迷你内核）**：攻击与合法路径成对。内存文件系统按真实内核语义走路径
   （中间链接跟随、`..` 取物理上一级、ENOENT/ENOTDIR/ELOOP），Windows 上不需要建链接的权限。
2. **属性测试**：随机目录树 + 随机 source，拿迷你内核的 `physical()` 当裁判——
   解析器放行就必须与真值一致（soundness），真值在根外就必须拒绝。
3. **真实符号链接**（`os.symlink`）：不依赖假文件系统的独立确认；建不了链接的机器自动 skip，
   Linux CI 上全部执行；放行的路径再用 `Path.resolve()` 对一遍。

再加 policy / proxy 两层的接线用例（§2.7：零件全对、调用点没了，测试会全绿）。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from unittest import mock

import pytest
from aiohttp import streams, web
from aiohttp.test_utils import make_mocked_request

from nekro_agent.services.docker_guard.bindpath import (
    MAX_LINK_HOPS,
    BindFs,
    RealBindFs,
    resolve_bind_source,
)
from nekro_agent.services.docker_guard.policy import (
    PolicyContext,
    bind_recheck_target,
    evaluate,
    evaluate_container_bind_sources,
    invisible_bind_roots,
)
from nekro_agent.services.docker_guard.proxy import DockerGuardProxy
from tests.docker_guard_fakefs import FakeBindFs

ROOT = "/srv/nekro_agent"

_BASE: Dict[str, Any] = {
    ROOT: "dir",
    f"{ROOT}/sandboxes/x": "dir",
    f"{ROOT}/uploads": "dir",
    f"{ROOT}/workspaces/12/.claude_home": "dir",
    f"{ROOT}/conf.yml": "file",
    "/etc/passwd": "file",
    "/etc/ssl/certs": "dir",
    "/root": "dir",
    "/srv/nekro_agent_evil": "dir",
}


def _fs() -> FakeBindFs:
    return FakeBindFs(_BASE)


def _fs_with(entries: Mapping[str, Any]) -> FakeBindFs:
    """基础目录树 + 额外条目（同名覆盖，例如把一个真目录换成链接）。"""
    return FakeBindFs({**_BASE, **entries})


def _request(method: str, path: str, body: bytes = b"") -> web.Request:
    if body:
        payload = streams.StreamReader(mock.Mock(), 2**16, loop=asyncio.get_event_loop())
        payload.feed_data(body)
        payload.feed_eof()
        return make_mocked_request(method, path, payload=payload)
    return make_mocked_request(method, path)


def _link(target: str) -> Tuple[str, str]:
    return ("link", target)


def _p(rel: str) -> str:
    return f"{ROOT}/{rel}"


# ---------------------------------------------------------------------------
# 1a. 解析器：合法路径必须放行（反向对照：不能误伤既有用法）
# ---------------------------------------------------------------------------

OK_CASES = [
    pytest.param({}, _p("sandboxes/x"), _p("sandboxes/x"), id="plain_existing_dir"),
    pytest.param({}, _p("uploads/chat1"), _p("uploads/chat1"), id="missing_leaf_docker_will_create_it"),
    pytest.param({}, _p("uploads/a/b/c"), _p("uploads/a/b/c"), id="missing_nested"),
    pytest.param({}, ROOT, ROOT, id="the_root_itself"),
    pytest.param({}, _p("conf.yml"), _p("conf.yml"), id="single_file_bind"),
    pytest.param({}, f"{ROOT}//sandboxes/./x/", _p("sandboxes/x"), id="dup_slash_dot_and_trailing_slash"),
    pytest.param({}, _p("sandboxes/x/../x"), _p("sandboxes/x"), id="lexical_dotdot_inside_root"),
    pytest.param({_p("data_link"): _link("sandboxes/x")}, _p("data_link"), _p("sandboxes/x"), id="in_tree_relative_link"),
    pytest.param({_p("abs"): _link(_p("uploads"))}, _p("abs"), _p("uploads"), id="in_tree_absolute_link"),
    pytest.param(
        {_p("sandboxes/x/up"): _link("../../uploads")},
        _p("sandboxes/x/up"),
        _p("uploads"),
        id="in_tree_link_climbing_but_staying_inside",
    ),
    pytest.param(
        {_p("l1"): _link("l2"), _p("l2"): _link("l3"), _p("l3"): _link("uploads")},
        _p("l1"),
        _p("uploads"),
        id="multi_hop_all_inside",
    ),
    pytest.param(
        {_p("lnk"): _link("sandboxes")},
        _p("lnk/x/sub"),
        _p("sandboxes/x/sub"),
        id="intermediate_link_inside_with_missing_tail",
    ),
    pytest.param(
        {_p("dang"): _link(_p("not_yet"))},
        _p("dang"),
        _p("not_yet"),
        id="dangling_link_whose_target_is_inside_root",
    ),
]


@pytest.mark.parametrize(("extra", "source", "expected"), OK_CASES)
def test_resolver_allows_legitimate_paths(extra: Mapping[str, Any], source: str, expected: str):
    fs = _fs_with(extra)
    res = resolve_bind_source(source, [ROOT], fs)
    assert res.ok, f"合法路径被误伤: {res.rule} {res.reason}"
    assert res.resolved == expected
    # 放行的路径必须与「内核真值」一致
    assert fs.physical(res.resolved) == res.resolved


# ---------------------------------------------------------------------------
# 1b. 解析器：绕过必须拒绝（每条对应一种链接形态）
# ---------------------------------------------------------------------------

DENY_CASES = [
    # —— 末段是链接 ——
    pytest.param({_p("evil"): _link("/")}, _p("evil"), "escapes_data_dir", id="final_link_to_fs_root"),
    pytest.param({_p("rel"): _link("../../etc")}, _p("rel"), "escapes_data_dir", id="final_relative_link_climbs_out"),
    pytest.param({_p("up"): _link("/srv")}, _p("up"), "escapes_data_dir", id="final_link_to_roots_parent"),
    pytest.param(
        {_p("sib"): _link("/srv/nekro_agent_evil")},
        _p("sib"),
        "escapes_data_dir",
        id="final_link_to_prefix_sibling",
    ),
    pytest.param(
        {_p("dot"): _link(f"{ROOT}/../../etc")},
        _p("dot"),
        "escapes_data_dir",
        id="absolute_link_with_dotdot_leaving_root",
    ),
    # —— 中间目录是链接 ——
    pytest.param({_p("evil"): _link("/")}, _p("evil/etc/passwd"), "escapes_data_dir", id="intermediate_link_to_fs_root"),
    pytest.param(
        {_p("sandboxes/x/e"): _link("/etc")},
        _p("sandboxes/x/e/passwd"),
        "escapes_data_dir",
        id="intermediate_link_two_levels_deep",
    ),
    pytest.param({_p("a"): _link("/etc")}, _p("a/ssl/certs"), "escapes_data_dir", id="intermediate_link_then_more_components"),
    # —— 多级链接 ——
    pytest.param(
        {_p("a"): _link("b"), _p("b"): _link("c"), _p("c"): _link("/")},
        _p("a"),
        "escapes_data_dir",
        id="multi_hop_last_hop_leaves_root",
    ),
    pytest.param(
        {_p("d1/l"): _link("../d2"), _p("d2/m"): _link("/root")},
        _p("d1/l/m"),
        "escapes_data_dir",
        id="multi_hop_through_directories",
    ),
    # —— 悬空链接 ——
    pytest.param({_p("dang"): _link("/nonexistent/x")}, _p("dang"), "escapes_data_dir", id="dangling_link_outside_root"),
    pytest.param(
        {_p("dang"): _link("/nonexistent/x")},
        _p("dang/deeper"),
        "escapes_data_dir",
        id="dangling_link_outside_with_tail",
    ),
    # —— `..` 语义 ——
    pytest.param({_p("m"): _link("nope/../../etc")}, _p("m"), "unresolvable", id="missing_component_followed_by_dotdot"),
    # 先爬出根再爬回来：两侧文件系统视图在根外可能不同，不依赖根外的任何解析——保守拒绝
    pytest.param(
        {_p("back"): _link("../nekro_agent/uploads")},
        _p("back"),
        "escapes_data_dir",
        id="leave_root_and_come_back_is_denied_conservatively",
    ),
    # —— 解析不了 → 拒绝（fail-closed） ——
    pytest.param({_p("l1"): _link("l2"), _p("l2"): _link("l1")}, _p("l1"), "unresolvable", id="link_cycle"),
    pytest.param({_p("self"): _link("self")}, _p("self"), "unresolvable", id="self_link"),
    pytest.param(
        {**{_p(f"c{i}"): _link(f"c{i + 1}") for i in range(MAX_LINK_HOPS + 1)}, _p(f"c{MAX_LINK_HOPS + 1}"): "dir"},
        _p("c0"),
        "unresolvable",
        id="chain_longer_than_hop_limit",
    ),
    pytest.param({}, _p("conf.yml/x"), "unresolvable", id="file_used_as_directory"),
    pytest.param({_p("empty"): _link("")}, _p("empty"), "unresolvable", id="empty_link_target"),
    pytest.param({}, ROOT + "/a\x00b", "unresolvable", id="nul_byte_in_source"),
    # —— 词法越界（旧判据本来就拦的，回归保留） ——
    pytest.param({}, "/", "outside_data_dir", id="fs_root"),
    pytest.param({}, "/etc", "outside_data_dir", id="etc"),
    pytest.param({}, f"{ROOT}/../../root", "outside_data_dir", id="lexical_dotdot_leaving_root"),
    pytest.param({}, f"{ROOT}_evil/x", "outside_data_dir", id="string_prefix_but_not_a_child"),
    pytest.param({}, "named_volume", "outside_data_dir", id="named_volume_not_allowed"),
    pytest.param({}, "", "outside_data_dir", id="empty_source"),
]


@pytest.mark.parametrize(("extra", "source", "rule"), DENY_CASES)
def test_resolver_denies_escapes(extra: Mapping[str, Any], source: str, rule: str):
    res = resolve_bind_source(source, [ROOT], _fs_with(extra))
    assert not res.ok, f"绕过被放行了: {source!r} -> {res.resolved!r}"
    assert res.rule == rule, (res.rule, res.reason)


def test_lstat_error_is_fail_closed():
    fs = FakeBindFs({**_BASE}, errors={_p("sandboxes/x"): PermissionError(13, "denied")})
    res = resolve_bind_source(_p("sandboxes/x"), [ROOT], fs)
    assert (res.ok, res.rule) == (False, "unresolvable")


def test_readlink_error_is_fail_closed():
    class _BadReadlink(FakeBindFs):
        def readlink(self, path: str) -> str:
            raise OSError(5, "io error")

    fs = _BadReadlink({**_BASE, _p("l"): _link("uploads")})
    res = resolve_bind_source(_p("l"), [ROOT], fs)
    assert (res.ok, res.rule) == (False, "unresolvable")


@pytest.mark.parametrize(
    ("entries", "label"),
    [({}, "root_absent"), ({ROOT: "file"}, "root_is_a_file")],
)
def test_invisible_root_denies_everything_below_it(entries: Mapping[str, Any], label: str):
    """guard 看不到数据目录（边车没挂）时不能回落到字符串判断，而是拒绝；根自身不需要文件系统。"""
    fs = FakeBindFs(entries)
    below = resolve_bind_source(_p("sandboxes/x"), [ROOT], fs)
    assert (below.ok, below.rule) == (False, "root_unverifiable"), label
    assert resolve_bind_source(ROOT, [ROOT], fs).ok  # 数据目录本身：运维给的，不依赖文件系统
    assert invisible_bind_roots(PolicyContext(allowed_bind_roots=(ROOT,), fs=fs)) == [ROOT]


def test_default_context_uses_the_real_filesystem_and_fails_closed():
    """没注入文件系统的 PolicyContext 默认用真实文件系统——允许根在这台机器上不存在 → 拒绝，而不是放行。"""
    root = "/nonexistent-nekro-root-for-test"
    ctx = PolicyContext(allowed_bind_roots=(root,))
    body = {"Image": "x", "HostConfig": {"Binds": [f"{root}/sandboxes/x:/app/shared:rw"]}}
    decision = evaluate("POST", "/containers/create", ctx, body)
    assert not decision.allow
    assert decision.rule == "create.binds.root_unverifiable"


# ---------------------------------------------------------------------------
# 1c. 允许根自身是符号链接（运维常见布局）不能误伤
# ---------------------------------------------------------------------------

ROOT_LINK_ENTRIES: Dict[str, Any] = {
    "/data": _link("/mnt/disk/nekro"),
    "/mnt/disk/nekro/sandboxes/x": "dir",
    "/mnt/disk/nekro/ln": _link("/mnt/disk/nekro/sandboxes/x"),
    "/mnt/disk/nekro/evil": _link("/"),
    "/mnt/disk/nekro/to_etc": _link("/etc"),
    "/etc/passwd": "file",
}


@pytest.mark.parametrize(
    ("source", "expected_ok", "expected"),
    [
        pytest.param("/data/sandboxes/x", True, "/data/sandboxes/x", id="through_the_link_the_operator_configured"),
        pytest.param(
            "/mnt/disk/nekro/sandboxes/x",
            True,
            "/mnt/disk/nekro/sandboxes/x",
            id="through_the_real_path_na_resolve_gives",
        ),
        pytest.param("/data", True, "/data", id="the_root_link_itself"),
        pytest.param("/data/ln", True, "/data/sandboxes/x", id="absolute_in_tree_link_written_with_the_real_path"),
        pytest.param("/data/evil", False, "escapes_data_dir", id="link_to_fs_root_below_a_linked_root"),
        pytest.param("/data/to_etc/passwd", False, "escapes_data_dir", id="link_to_etc_below_a_linked_root"),
        pytest.param("/mnt/disk/other", False, "outside_data_dir", id="sibling_of_the_real_root"),
    ],
)
def test_root_that_is_itself_a_symlink(source: str, expected_ok: bool, expected: str):
    res = resolve_bind_source(source, ["/data"], FakeBindFs(ROOT_LINK_ENTRIES))
    assert res.ok is expected_ok, (res.rule, res.reason)
    assert (res.resolved if expected_ok else res.rule) == expected


@pytest.mark.parametrize(
    "root",
    [
        pytest.param("/run/desktop/mnt/host/c/Users/me/nekro", id="docker_desktop_mount_form"),
        pytest.param("/c/Users/me/nekro", id="git_bash_style_drive_form"),
        pytest.param("/mnt/c/Users/me/nekro", id="wsl_form"),
    ],
)
def test_windows_and_docker_desktop_path_shapes_are_plain_posix_paths(root: str):
    fs = FakeBindFs({f"{root}/sandboxes/x": "dir", f"{root}/evil": _link("/")})
    assert resolve_bind_source(f"{root}/sandboxes/x", [root], fs).resolved == f"{root}/sandboxes/x"
    assert resolve_bind_source(f"{root}/new_dir", [root], fs).ok
    assert resolve_bind_source(f"{root}/evil", [root], fs).rule == "escapes_data_dir"


def test_extra_bind_roots_get_the_same_treatment():
    fs = _fs_with({"/srv/extra": "dir", "/srv/extra/bad": _link("/")})
    assert resolve_bind_source("/srv/extra/ok", [ROOT, "/srv/extra"], fs).ok
    res = resolve_bind_source("/srv/extra/bad", [ROOT, "/srv/extra"], fs)
    assert (res.ok, res.rule) == (False, "escapes_data_dir")


def test_resolver_never_lstats_through_an_unexpanded_link():
    """解析器只能对**已展开**的前缀做 lstat：对 `evil/etc` 直接 lstat 会被内核跟随到 `/etc`。"""
    fs = _fs_with({_p("evil"): _link("/etc")})
    resolve_bind_source(_p("evil/passwd"), [ROOT], fs)
    assert _p("evil/passwd") not in fs.lstat_calls, fs.lstat_calls


# ---------------------------------------------------------------------------
# 2. 属性测试：迷你内核的真值当裁判
# ---------------------------------------------------------------------------

_NAMES = ["a", "b", "c", "d"]


def _random_tree(rng: random.Random) -> Dict[str, Any]:
    entries: Dict[str, Any] = {ROOT: "dir", "/etc": "dir", "/etc/x": "dir", "/out": "dir", "/out/y": "file"}
    paths: List[str] = []
    for _ in range(rng.randint(3, 9)):
        depth = rng.randint(1, 3)
        base = rng.choice([ROOT, ROOT, ROOT, "/etc", "/out"])
        paths.append(base + "".join("/" + rng.choice(_NAMES) for _ in range(depth)))
    for path in paths:
        kind = rng.random()
        if kind < 0.45:
            pieces: List[str] = []
            if rng.random() < 0.4:
                pieces = ["/"] if rng.random() < 0.3 else [rng.choice([ROOT, "/etc", "/out", "/etc/x"])]
                target = pieces[0] + "".join("/" + rng.choice(_NAMES + [".."]) for _ in range(rng.randint(0, 2)))
            else:
                target = "/".join(rng.choice(_NAMES + ["..", ".."]) for _ in range(rng.randint(1, 4)))
            entries[path] = _link(target)
        elif kind < 0.7:
            entries[path] = "file"
        else:
            entries[path] = "dir"
    return entries


def _random_source(rng: random.Random) -> str:
    return ROOT + "".join("/" + rng.choice(_NAMES) for _ in range(rng.randint(1, 4)))


def _under_root(path: str) -> bool:
    return path == ROOT or path.startswith(ROOT + "/")


def test_property_resolver_agrees_with_the_kernel_oracle():
    rng = random.Random(7302)
    ok_with_link = denied_that_escapes = 0
    for _ in range(3000):
        entries = _random_tree(rng)
        # 同一棵树上多测几个 source，省生成成本
        fs = FakeBindFs(entries)
        for _ in range(4):
            source = _random_source(rng)
            res = resolve_bind_source(source, [ROOT], fs)
            truth = fs.physical(source)
            if res.ok:
                # soundness：放行的结果就是 Docker 实际会挂到的地方，且在根内
                assert truth == res.resolved, (entries, source, res, truth)
                assert _under_root(res.resolved), (entries, source, res)
                if any(isinstance(v, tuple) for v in entries.values()) and res.resolved != source:
                    ok_with_link += 1
            if truth is None or not _under_root(truth):
                # safety：真值在根外（或内核都解不出）就必须拒绝
                assert not res.ok, (entries, source, res, truth)
                denied_that_escapes += 1
    # 发生器不能退化成「全拒绝」或「没有逃逸」：两类都要有足够样本，否则上面的断言是空转
    assert ok_with_link > 100, ok_with_link
    assert denied_that_escapes > 100, denied_that_escapes


# ---------------------------------------------------------------------------
# 3. 真实符号链接（Linux CI 上全部执行；建不了链接的机器 skip）
# ---------------------------------------------------------------------------


def _symlink(target: str, link: Path, *, is_dir: bool = True) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as e:  # Windows 无符号链接权限
        pytest.skip(f"当前环境建不了符号链接: {e}")


@pytest.fixture
def real_tree(tmp_path: Path) -> Dict[str, Path]:
    base = tmp_path.resolve()
    data = base / "data"
    outside = base / "outside"
    for d in (data / "sandboxes" / "x", data / "uploads", outside):
        d.mkdir(parents=True)
    (outside / "secret.txt").write_text("host secret", encoding="utf-8")
    return {"base": base, "data": data, "outside": outside}


def _posix(path: Path) -> str:
    return path.as_posix()


def test_real_symlinks_escape_attempts_are_denied(real_tree: Dict[str, Path]):
    data, outside, base = real_tree["data"], real_tree["outside"], real_tree["base"]
    _symlink(_posix(outside), data / "abs_out")
    _symlink("../outside", data / "rel_out")
    _symlink(_posix(outside), data / "sandboxes" / "x" / "mid")
    _symlink("l2", data / "l1")
    _symlink("l3", data / "l2")
    _symlink(_posix(outside), data / "l3")
    _symlink(_posix(base / "nonexistent" / "t"), data / "dangling")
    _symlink("loop_b", data / "loop_a")
    _symlink("loop_a", data / "loop_b")
    _symlink("/", data / "to_fs_root")
    root = _posix(data)
    cases = {
        f"{root}/abs_out": "escapes_data_dir",
        f"{root}/abs_out/secret.txt": "escapes_data_dir",
        f"{root}/rel_out": "escapes_data_dir",
        f"{root}/sandboxes/x/mid/secret.txt": "escapes_data_dir",
        f"{root}/l1": "escapes_data_dir",
        f"{root}/dangling": "escapes_data_dir",
        f"{root}/loop_a": "unresolvable",
        f"{root}/to_fs_root": "escapes_data_dir",
        f"{root}/to_fs_root/etc": "escapes_data_dir",
    }
    for source, rule in cases.items():
        res = resolve_bind_source(source, [root], RealBindFs())
        assert (res.ok, res.rule) == (False, rule), (source, res)


def test_real_legitimate_paths_pass_and_match_the_kernel(real_tree: Dict[str, Path]):
    data = real_tree["data"]
    _symlink("sandboxes/x", data / "alias")
    _symlink(_posix(data / "uploads"), data / "abs_alias")
    root = _posix(data)
    for source in (
        root,
        f"{root}/sandboxes/x",
        f"{root}/uploads/new_chat",
        f"{root}/alias",
        f"{root}/abs_alias",
        f"{root}/alias/../uploads",
    ):
        res = resolve_bind_source(source, [root], RealBindFs())
        assert res.ok, (source, res)
        # 独立确认：放行结果与内核对「规范化后的 source」的解析一致
        assert Path(res.resolved).resolve() == Path(os.path.normpath(source)).resolve(), (source, res)


def test_real_root_that_is_a_symlink_is_not_misjudged(real_tree: Dict[str, Path]):
    data, base = real_tree["data"], real_tree["base"]
    _symlink(_posix(data), base / "data_link")
    link_root = _posix(base / "data_link")
    real_path = _posix(data)
    assert resolve_bind_source(f"{link_root}/sandboxes/x", [link_root], RealBindFs()).ok
    # NA 用 Path.resolve() 给出的是真实路径，与配置的链接路径是同一个目录
    assert resolve_bind_source(f"{real_path}/sandboxes/x", [link_root], RealBindFs()).ok
    _symlink("/", data / "to_fs_root")
    assert resolve_bind_source(f"{link_root}/to_fs_root", [link_root], RealBindFs()).ok is False


@pytest.mark.skipif(os.name == "nt", reason="Binds 串按 `:` 切分，盘符路径不是 NA 会发给 guard 的形态；Linux CI 执行")
def test_real_end_to_end_create_policy(real_tree: Dict[str, Path]):
    data, outside = real_tree["data"], real_tree["outside"]
    _symlink(_posix(outside), data / "evil")
    root = _posix(data)
    ctx = PolicyContext(allowed_bind_roots=(root,))  # 默认真实文件系统

    def _create(*binds: str) -> Any:
        return evaluate("POST", "/containers/create", ctx, {"Image": "x", "HostConfig": {"Binds": list(binds)}})

    attack = _create(f"{root}/evil:/host:rw")
    assert not attack.allow
    assert attack.rule == "create.binds.escapes_data_dir"
    control = _create(f"{root}/sandboxes/x:/app/shared:rw")
    assert control.allow


# ---------------------------------------------------------------------------
# 4. policy.evaluate：拒绝 + 转发的是「判过的那一份」
# ---------------------------------------------------------------------------


def _ctx(fs: BindFs, roots: Tuple[str, ...] = (ROOT,)) -> PolicyContext:
    return PolicyContext(allowed_bind_roots=roots, fs=fs)


def _create_body(*binds: str, **host_config: Any) -> Dict[str, Any]:
    return {"Image": "sandbox:latest", "HostConfig": {"Binds": list(binds), **host_config}}


def test_create_with_link_bind_is_denied_end_to_end():
    ctx = _ctx(_fs_with({_p("evil"): _link("/")}))
    decision = evaluate("POST", "/containers/create", ctx, _create_body(f"{_p('evil')}:/host:rw"))
    assert not decision.allow
    assert decision.rule == "create.binds.escapes_data_dir"
    assert decision.rewritten_body is None


def test_one_bad_bind_among_good_ones_denies_the_whole_create():
    ctx = _ctx(_fs_with({_p("evil"): _link("/")}))
    body = _create_body(f"{_p('sandboxes/x')}:/app/shared:rw", f"{_p('evil')}:/host:rw", f"{_p('uploads')}:/u:ro")
    assert not evaluate("POST", "/containers/create", ctx, body).allow


def test_forwarded_body_carries_resolved_canonical_sources():
    """转发的是判过的那一份：source 换成规范路径，目标与选项原样保留；原请求体不被改动。"""
    ctx = _ctx(_fs_with({_p("alias"): _link("sandboxes/x"), _p("esc"): _link("/etc/ssl")}))
    original = _create_body(
        f"{ROOT}//sandboxes/./x/:/app/shared:rw",
        f"{_p('alias')}:/a:ro,z",
        f"{_p('esc')}/../uploads:/app/uploads:ro",  # 词法 `..` 越过了一个链接：转发规范化后的路径
        Memory=123,
    )
    snapshot = json.loads(json.dumps(original))
    decision = evaluate("POST", "/containers/create", ctx, original)
    assert decision.allow, decision
    assert decision.rewritten_body is not None
    assert decision.rewritten_body["HostConfig"]["Binds"] == [
        f"{_p('sandboxes/x')}:/app/shared:rw",
        f"{_p('sandboxes/x')}:/a:ro,z",
        f"{_p('uploads')}:/app/uploads:ro",
    ]
    assert decision.rewritten_body["HostConfig"]["Memory"] == 123  # 其它字段原样
    assert original == snapshot  # 不原地改入参


def test_lexical_dotdot_over_a_link_is_forwarded_normalized_not_as_sent():
    """`<根>/lnk/../y`：词法是 `<根>/y`，物理是 lnk 目标的上一级。只判不改写的话 Docker 会挂到后者。"""
    fs = _fs_with({_p("lnk"): _link("/etc/ssl/certs")})
    sent = f"{_p('lnk')}/../y"
    assert fs.physical(sent) == "/etc/ssl/y"  # 前提：未改写时 Docker 会挂到根外
    decision = evaluate("POST", "/containers/create", _ctx(fs), _create_body(f"{sent}:/m:rw"))
    assert decision.allow
    forwarded = decision.rewritten_body["HostConfig"]["Binds"][0].split(":")[0]  # type: ignore[index]
    assert forwarded == _p("y")
    assert fs.physical(forwarded) == _p("y")


def test_create_without_binds_is_still_forwarded_from_the_checked_structure():
    decision = evaluate("POST", "/containers/create", _ctx(_fs()), {"Image": "x", "Cmd": ["true"]})
    assert decision.allow
    assert decision.rewritten_body == {"Image": "x", "Cmd": ["true"]}


def test_ro_exempt_file_is_normalized_and_not_resolved():
    # /etc/localtime 在多数宿主上本身就是指向 zoneinfo 的链接：豁免项是运维的文件，不过解析
    fs = _fs_with({"/etc/localtime": _link("/usr/share/zoneinfo/UTC")})
    ctx = _ctx(fs)
    ok = evaluate("POST", "/containers/create", ctx, _create_body("/etc/x/../localtime:/etc/localtime:ro"))
    assert ok.allow
    assert ok.rewritten_body["HostConfig"]["Binds"] == ["/etc/localtime:/etc/localtime:ro"]  # type: ignore[index]
    rw = evaluate("POST", "/containers/create", ctx, _create_body("/etc/localtime:/etc/localtime:rw"))
    assert rw.rule == "create.binds.ro_required"


def test_netguard_host_network_still_may_not_mount_anything():
    """host 网络容器不得挂任何目录——链接检查不能替它放宽。"""
    body = {
        "Image": "x",
        "Cmd": ["iptables", "-L"],
        "Entrypoint": [],
        "Labels": {"nekro.netguard": "1"},
        "HostConfig": {"NetworkMode": "host", "CapAdd": ["NET_ADMIN"], "Binds": [f"{_p('sandboxes/x')}:/m"]},
    }
    assert evaluate("POST", "/containers/create", _ctx(_fs()), body).rule == "create.hostnet.binds"


# ---------------------------------------------------------------------------
# 5. start / restart 复核（检查与挂载之间的间隔：create 到 start 任意长）
# ---------------------------------------------------------------------------

RECHECK_TARGET_CASES = [
    ("POST", "/containers/abc123/start", "abc123"),
    ("POST", "/v1.51/containers/abc123/start?x=1", "abc123"),
    ("POST", "/containers/abc123/restart?t=5", "abc123"),
    ("POST", "/containers/abc123/stop", None),
    ("POST", "/containers/abc123/kill", None),
    ("POST", "/containers/abc123/wait", None),
    ("GET", "/containers/abc123/json", None),
    ("GET", "/containers/abc123/logs", None),
    ("DELETE", "/containers/abc123", None),
    ("GET", "/containers/abc123/start", None),
    ("POST", "/containers/create", None),
    ("POST", "/containers/x/../abc123/start", None),
]


@pytest.mark.parametrize(("method", "path", "expected"), RECHECK_TARGET_CASES)
def test_bind_recheck_target(method: str, path: str, expected: Optional[str]):
    assert bind_recheck_target(method, path) == expected


RECHECK_CASES = [
    pytest.param({}, [_p("sandboxes/x"), _p("uploads/chat1")], True, "", id="unchanged"),
    pytest.param({}, [], True, "", id="no_binds"),
    pytest.param({}, ["/etc/localtime", "/some/operator/mount"], True, "", id="outside_root_sources_are_not_our_business"),
    pytest.param({}, [ROOT], True, "", id="data_dir_itself_na_restarting_itself"),
    pytest.param({_p("sandboxes/x"): _link("/")}, [_p("sandboxes/x")], False, "start.binds.escapes_data_dir", id="swapped_for_link_to_root"),
    pytest.param(
        {_p("sandboxes/x"): _link("/etc")},
        [_p("uploads"), _p("sandboxes/x")],
        False,
        "start.binds.escapes_data_dir",
        id="swapped_one_of_several",
    ),
    pytest.param(
        {_p("sandboxes/x"): _link("../uploads")},
        [_p("sandboxes/x")],
        False,
        "start.binds.changed",
        id="swapped_for_in_tree_link_is_still_a_change",
    ),
    pytest.param(
        {_p("sandboxes/x"): _link("../loop"), _p("loop"): _link("sandboxes/x")},
        [_p("sandboxes/x")],
        False,
        "start.binds.unresolvable",
        id="swapped_for_cycle",
    ),
]


@pytest.mark.parametrize(("swap", "sources", "allow", "rule"), RECHECK_CASES)
def test_start_recheck_catches_a_swap_after_create(swap: Mapping[str, Any], sources: List[str], allow: bool, rule: str):
    # 1) create 时：目录是真的 → 放行（根之外的 source 本来就进不了 create，不参与这一步）
    in_root = [s for s in sources if s == ROOT or s.startswith(ROOT + "/")]
    if in_root:
        created = evaluate("POST", "/containers/create", _ctx(_fs()), _create_body(*[f"{s}:/m" for s in in_root]))
        assert created.allow
    # 2) 之后目录被换成链接，start 前复核
    decision = evaluate_container_bind_sources(sources, _ctx(_fs_with(swap)))
    assert decision.allow is allow, decision
    assert decision.rule == (rule or "containers.start")


def test_start_recheck_fails_closed_when_the_root_is_not_visible():
    decision = evaluate_container_bind_sources([_p("sandboxes/x")], _ctx(FakeBindFs({})))
    assert (decision.allow, decision.rule) == (False, "start.binds.root_unverifiable")


def _inspect_payload(*sources: str) -> Dict[str, Any]:
    return {
        "Config": {"Labels": {"nekro.owner.scope": "t"}},
        "HostConfig": {"Binds": [f"{s}:/m" for s in sources]},
        "Mounts": [{"Type": "bind", "Source": s, "Destination": "/m"} for s in sources] + [{"Type": "volume", "Name": "v"}],
    }


def _proxy(fs: BindFs) -> DockerGuardProxy:
    return DockerGuardProxy("/nonexistent.sock", _ctx(fs))


async def _forward_should_not_run(*_a: Any, **_k: Any) -> web.StreamResponse:
    raise AssertionError("被拒绝的请求不该转发到宿主 docker.sock")


@pytest.mark.parametrize("action", ["start", "restart"])
async def test_handle_rechecks_binds_before_start_and_restart(action: str):
    swapped = _fs_with({_p("sandboxes/x"): _link("/")})
    proxy = _proxy(swapped)
    proxy._is_owned_container = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
    proxy._inspect_json = mock.AsyncMock(return_value=_inspect_payload(_p("sandboxes/x")))  # type: ignore[method-assign]
    proxy._forward = _forward_should_not_run  # type: ignore[method-assign]

    resp = await proxy.handle(_request("POST", f"/containers/{'a' * 64}/{action}"))
    assert resp.status == 403
    assert "数据目录" in json.loads(resp.body)["message"]  # type: ignore[arg-type]
    proxy._inspect_json.assert_awaited_once()


async def test_handle_start_forwards_when_binds_are_still_clean():
    proxy = _proxy(_fs())
    proxy._is_owned_container = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
    proxy._inspect_json = mock.AsyncMock(return_value=_inspect_payload(_p("sandboxes/x")))  # type: ignore[method-assign]
    forward = mock.AsyncMock(return_value=web.Response(status=204))
    proxy._forward = forward  # type: ignore[method-assign]

    resp = await proxy.handle(_request("POST", f"/containers/{'a' * 64}/start"))
    assert resp.status == 204
    forward.assert_awaited_once()


async def test_handle_start_is_denied_when_inspect_fails():
    proxy = _proxy(_fs())
    proxy._is_owned_container = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
    proxy._inspect_json = mock.AsyncMock(return_value=None)  # type: ignore[method-assign]
    proxy._forward = _forward_should_not_run  # type: ignore[method-assign]

    resp = await proxy.handle(_request("POST", f"/containers/{'a' * 64}/start"))
    assert resp.status == 403


@pytest.mark.parametrize(("method", "path"), [
    ("POST", "/containers/{id}/stop"),
    ("POST", "/containers/{id}/kill"),
    ("GET", "/containers/{id}/logs"),
    ("GET", "/containers/{id}/json"),
])
async def test_handle_does_not_recheck_binds_for_other_container_actions(method: str, path: str):
    proxy = _proxy(_fs_with({_p("sandboxes/x"): _link("/")}))
    proxy._is_owned_container = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
    proxy._inspect_json = mock.AsyncMock(side_effect=AssertionError("这些动作不解析 bind source，不该多一次 inspect"))  # type: ignore[method-assign]
    proxy._forward = mock.AsyncMock(return_value=web.Response(status=204))  # type: ignore[method-assign]
    resp = await proxy.handle(_request(method, path.format(id="a" * 64)))
    assert resp.status == 204


async def test_handle_foreign_container_is_rejected_before_any_bind_inspect():
    proxy = _proxy(_fs())
    proxy._is_owned_container = mock.AsyncMock(return_value=False)  # type: ignore[method-assign]
    proxy._inspect_json = mock.AsyncMock(side_effect=AssertionError("归属门在前，别人的容器连复核都不该做"))  # type: ignore[method-assign]
    proxy._forward = _forward_should_not_run  # type: ignore[method-assign]
    resp = await proxy.handle(_request("POST", f"/containers/{'a' * 64}/start"))
    assert resp.status == 404


def _bind_mount(source: str) -> Dict[str, Any]:
    return {"Type": "bind", "Source": source, "Destination": "/m"}


# 两路数据（`Mounts` 与 `HostConfig.Binds`）和 `Type` 过滤必须**分别**被钉住。
# 原先的用例让两路给同一批 source、`Mounts` 里的卷又恰好没有 `Source` 键，于是删掉任何一路解析、
# 甚至删掉 `Type == "bind"` 过滤，结果都不变——一条什么都没验证的断言。这里每一行只靠一路（或一个过滤）成立。
INSPECT_SOURCE_CASES = [
    pytest.param({"Mounts": [_bind_mount(_p("a")), _bind_mount(_p("b"))]}, {_p("a"), _p("b")}, id="mounts_only"),
    pytest.param(
        {"HostConfig": {"Binds": [f"{_p('a')}:/m", f"{_p('b')}:/n:ro"]}},
        {_p("a"), _p("b")},
        id="binds_only_split_off_destination_and_options",
    ),
    pytest.param(
        {"Mounts": [_bind_mount(_p("a"))], "HostConfig": {"Binds": [f"{_p('b')}:/m"]}},
        {_p("a"), _p("b")},
        id="disjoint_sources_are_unioned",
    ),
    pytest.param(
        {
            "Mounts": [
                _bind_mount(_p("a")),
                # 非 bind 类型但带着字符串 Source：卷 / tmpfs 的 Source 不是宿主 bind 路径，不能进复核
                {"Type": "volume", "Name": "v", "Source": "/var/lib/docker/volumes/v/_data", "Destination": "/v"},
                {"Type": "tmpfs", "Source": "/should/not/count", "Destination": "/t"},
            ],
        },
        {_p("a")},
        id="only_bind_type_mounts_count_even_when_others_carry_a_source",
    ),
    pytest.param(
        {
            "Mounts": [{"Type": "bind"}, "junk", {"Type": "bind", "Source": 5}, _bind_mount(_p("a"))],
            "HostConfig": {"Binds": [7, None, f"{_p('b')}:/m"]},
        },
        {_p("a"), _p("b")},
        id="malformed_entries_are_skipped_not_fatal",
    ),
    pytest.param({}, set(), id="nothing_to_recheck"),
    pytest.param({"Mounts": "x", "HostConfig": "y"}, set(), id="wrong_container_types"),
]


@pytest.mark.parametrize(("payload", "expected"), INSPECT_SOURCE_CASES)
async def test_inspect_bind_sources_reads_each_data_route_and_filters_by_type(payload: Dict[str, Any], expected: set):
    proxy = _proxy(_fs())
    proxy._inspect_json = mock.AsyncMock(return_value=payload)  # type: ignore[method-assign]
    sources = await proxy._inspect_bind_sources("x")
    assert sources is not None
    assert set(sources) == expected


async def test_inspect_bind_sources_is_none_when_inspect_fails():
    proxy = _proxy(_fs())
    proxy._inspect_json = mock.AsyncMock(return_value=None)  # type: ignore[method-assign]
    assert await proxy._inspect_bind_sources("x") is None


# ---------------------------------------------------------------------------
# 解析工作量上限（P2）：跳数上限只管「链长」，不管总量；预算按请求共享
# ---------------------------------------------------------------------------


def _deep(n: int) -> str:
    return _p("/".join(f"d{i}" for i in range(n)))


def test_source_longer_than_path_max_is_refused_without_touching_the_filesystem():
    from nekro_agent.services.docker_guard.bindpath import MAX_BIND_SOURCE_LENGTH

    fs = _fs()
    result = resolve_bind_source(ROOT + "/" + "a/" * MAX_BIND_SOURCE_LENGTH, [ROOT], fs)
    assert (result.ok, result.rule) == (False, "too_complex")
    assert fs.lstat_calls == []


def test_the_resolution_budget_is_shared_between_binds_of_one_request():
    from nekro_agent.services.docker_guard.bindpath import ResolveBudget

    path = _deep(60)  # 61 步：够一次，不够两次（预算 100）
    fs = FakeBindFs({**_BASE, path: "dir"})
    shared = ResolveBudget(total=100)
    assert resolve_bind_source(path, [ROOT], fs, shared).ok
    second = resolve_bind_source(path, [ROOT], fs, shared)
    assert (second.ok, second.rule) == (False, "too_complex")
    # 对照：各用各的预算时，同一条路径单独解析永远没问题——所以上面拒绝的原因只能是「共享」
    assert resolve_bind_source(path, [ROOT], fs, ResolveBudget(total=100)).ok
    assert resolve_bind_source(path, [ROOT], fs).ok

def test_symlink_hops_cost_more_than_plain_components():
    from nekro_agent.services.docker_guard.bindpath import LINK_COST, ResolveBudget

    chain = {_p(f"l{i}"): _link(f"l{i + 1}") for i in range(20)}
    chain[_p("l20")] = "dir"
    fs = _fs_with(chain)
    plain_steps = 21
    # 预算够「按分量数」走完，但不够再付 20 次链接开销
    budget = ResolveBudget(total=plain_steps + LINK_COST * 10)
    result = resolve_bind_source(_p("l0"), [ROOT], fs, budget)
    assert (result.ok, result.rule) == (False, "too_complex")
    # 对照：预算充足时同一条链接链解析得出来
    ok = resolve_bind_source(_p("l0"), [ROOT], fs, ResolveBudget(total=plain_steps * (1 + LINK_COST)))
    assert (ok.ok, ok.resolved) == (True, _p("l20"))


def test_start_recheck_has_its_own_source_count_cap_and_shared_budget():
    from nekro_agent.services.docker_guard.policy import MAX_RECHECK_SOURCES

    many = [_p("sandboxes/x")] * (MAX_RECHECK_SOURCES + 1)
    denied = evaluate_container_bind_sources(many, _ctx(_fs()))
    assert (denied.allow, denied.rule) == (False, "start.binds.too_many")

    path = _deep(70)
    fs = FakeBindFs({**_BASE, path: "dir"})
    assert evaluate_container_bind_sources([path], _ctx(fs)).allow
    expensive = evaluate_container_bind_sources([path] * MAX_RECHECK_SOURCES, _ctx(fs))
    assert (expensive.allow, expensive.rule) == (False, "start.binds.too_complex")


async def test_handle_create_forwards_the_rewritten_body_not_the_original_bytes():
    """接线证据：转发给 Docker 的字节必须是判过的那一份（source 已是规范路径）。"""
    fs = _fs_with({_p("lnk"): _link("/etc/ssl/certs")})
    proxy = _proxy(fs)
    seen: Dict[str, Any] = {}

    async def fake_forward(request: web.Request, body_bytes: bytes, capture_created: bool = False) -> web.StreamResponse:
        seen["body"] = json.loads(body_bytes)
        return web.Response(status=201)

    proxy._forward = fake_forward  # type: ignore[method-assign]
    sent = _create_body(f"{_p('lnk')}/../y:/m:rw")
    await proxy.handle(_request("POST", "/containers/create", body=json.dumps(sent).encode()))
    assert seen["body"]["HostConfig"]["Binds"] == [f"{_p('y')}:/m:rw"]


async def test_handle_create_with_link_bind_never_reaches_docker():
    proxy = _proxy(_fs_with({_p("evil"): _link("/")}))
    proxy._forward = _forward_should_not_run  # type: ignore[method-assign]
    sent = _create_body(f"{_p('evil')}:/host:rw")
    resp = await proxy.handle(_request("POST", "/containers/create", body=json.dumps(sent).encode()))
    assert resp.status == 403


# ---------------------------------------------------------------------------
# 6. 部署清单：边车必须能看见数据目录，否则上面的一切都是 fail-closed 的全拒绝
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_hardened_compose_mounts_the_data_dir_read_only_into_the_guard_at_the_same_path():
    import yaml

    compose = yaml.safe_load((REPO_ROOT / "docker" / "docker-compose-x-hardened.yml").read_text(encoding="utf-8"))
    guard_volumes: List[str] = compose["services"]["nekro_docker_guard"]["volumes"]
    wanted = "${NEKRO_DATA_DIR}:${NEKRO_DATA_DIR}:ro"
    assert wanted in guard_volumes, (
        "guard 边车没有把数据目录以『同一路径、只读』挂进来：它看不到 bind source 的真实文件系统，"
        f"符号链接校验只能 fail-closed 地拒绝所有挂载。期望包含 {wanted!r}，实际 {guard_volumes}"
    )
    # 对照：NA 自己挂的是读写、同一路径；guard 只读（绝不能比 NA 更能写）
    app_volumes: List[str] = compose["services"]["nekro_agent"]["volumes"]
    assert "${NEKRO_DATA_DIR}:${NEKRO_DATA_DIR}:rw" in app_volumes
    assert not any(v.endswith(":rw") and "NEKRO_DATA_DIR" in v for v in guard_volumes)
