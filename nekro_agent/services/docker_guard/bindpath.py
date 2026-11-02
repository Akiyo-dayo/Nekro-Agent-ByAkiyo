"""bind source 的真实路径解析（docker-guard 的符号链接防线，PROJECT_STATE §3 #73）。

## 要解决的问题

`policy` 过去只对 bind source 字符串做 `normpath` 再判前缀。可 Docker 守护进程挂载时会在**宿主**
上跟随符号链接：`<数据目录>/x` 若是指向 `/` 的软链，字符串判据看到的是「在数据目录之下」，实际挂进容器的
却是宿主根目录。`..` 同理：`<数据目录>/link/../y` 词法上是 `<数据目录>/y`，物理上却是 `link` 目标的上一级。

## 做法

不依赖 `os.path.realpath`，而是自己**逐分量解析**，且全程守一条不变量：

> 解析过程中的**每一个中间状态**都必须仍在允许根目录之内。

一旦某一步（`..` 爬出根、绝对链接指向根外、悬空到根外）离开了根，立即拒绝，而不是等到最后再比较。
这条不变量让「guard 看到的文件系统」与「宿主看到的文件系统」不必完全相同也能得出一致结论：
边车形态下 guard 把数据目录以**同一路径**只读挂进来，根之内两边是同一份目录树；根之外（`/`、`/etc`、
`/mnt/...`）两边可能不同，但我们从不依赖根外的任何解析结果——走到那里就已经拒绝了。

解析不了一律拒绝（fail-closed），**不回落到字符串判断**：
根目录在 guard 视角里不存在 / 不是目录、`lstat` / `readlink` 报错（权限、名字过长、含 NUL）、
链接环（超过 `MAX_LINK_HOPS` 跳）、不存在的分量之后还跟着 `..`。

不存在的分量允许（Docker 对不存在的 bind source 会自己建目录；沙盒的上传目录就是这样），
它们此刻不可能是链接，词法拼接即可。

## 工作量上限

只限链接跳数不够：每一跳的目标可以展开成上千个分量，多条 bind 又各自重新计跳。所以整个请求共享一份
`ResolveBudget`（按处理的分量数计，符号链接一步另算 `LINK_COST`），单条 source 不超过 `MAX_BIND_SOURCE_LENGTH`；
预算耗尽按解析失败处理（`too_complex`，fail-closed）。调用方（`proxy`）另外把解析放进专用线程并带超时，见 `_run_blocking`。

## 注入点

文件系统访问全部经 `BindFs`，测试里换成内存实现，不需要真的建符号链接（Windows 上建链接要权限）；
同时保留 `RealBindFs` 的真实链接用例让 Linux CI 跑。

## 残余（见 docs/docker-guard.md）

解析结果只对**检查那一刻**成立。检查与 Docker 实际挂载之间能换目录的一方（持有数据目录写权限者）
仍可抢一个很窄的窗口——本模块配合「转发解析后的规范路径」与「start / restart 前复核」把窗口收窄，不能归零。
"""

from __future__ import annotations

import os
import posixpath
import re
import stat
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Protocol, Sequence

# 与 Linux 内核 ELOOP 的上限同量级：链接环 / 过深的链接链按「解析不了」拒绝
MAX_LINK_HOPS = 40

KIND_DIR = "dir"
KIND_FILE = "file"
KIND_SYMLINK = "symlink"
KIND_MISSING = "missing"

# 失败规则后缀（policy 拼成 `create.binds.<后缀>` / `start.binds.<后缀>`）
RULE_OUTSIDE = "outside_data_dir"
RULE_ESCAPES = "escapes_data_dir"
RULE_UNRESOLVABLE = "unresolvable"
RULE_ROOT_UNVERIFIABLE = "root_unverifiable"
RULE_TOO_COMPLEX = "too_complex"

# 单条 bind source 的长度上限（Linux PATH_MAX）。更长的路径内核本来就解不出来，没必要为它花工作量。
MAX_BIND_SOURCE_LENGTH = 4096

# 单个请求里解析工作量的总预算（跨该请求的全部 bind source 共享）。
# 只限链接「跳数」是不够的：每一跳的目标可以展开成上千个分量，多条 bind 又各自重新计跳，
# 一个 1MB 的请求体就能让 guard 做上百万次 lstat（还是在事件循环里）。预算按「处理的路径分量数」计，
# 符号链接那一步（readlink + 重排队列）另加 `LINK_COST`。NA 自己的请求（4 条 bind、每条不到 10 个分量）
# 只用掉几十，2048 留足了余量。耗尽即拒绝（fail-closed），不放行「没解完」的路径。
DEFAULT_RESOLVE_BUDGET = 2048
LINK_COST = 8


class ResolveBudget:
    """一个请求内共享的解析工作量预算。"""

    def __init__(self, total: int = DEFAULT_RESOLVE_BUDGET) -> None:
        self.remaining = total

    def charge(self, units: int = 1) -> bool:
        """扣减；预算不够时返回 False（调用方必须当作解析失败）。"""
        self.remaining -= units
        return self.remaining >= 0

_WIN_DRIVE = re.compile(r"^[A-Za-z]:/")


class BindFs(Protocol):
    """解析 bind source 所需的最小文件系统面。生产用 `RealBindFs`，测试注入内存实现。"""

    def lstat_kind(self, path: str) -> str:
        """不跟随末段链接地取类型：dir / file / symlink / missing。其它错误抛 OSError / ValueError。"""
        ...

    def stat_is_dir(self, path: str) -> bool:
        """跟随链接地判断是否目录（只用于确认允许根在 guard 视角里存在）。"""
        ...

    def readlink(self, path: str) -> str: ...

    def realpath(self, path: str) -> str:
        """只用来给「本身是符号链接的允许根」补一个别名；失败时调用方忽略别名。"""
        ...


def _from_native(path: str) -> str:
    """Windows 原生路径转成 posix 形态（`\\` -> `/`，去 `\\\\?\\` 前缀）；POSIX 上原样返回。"""
    if os.name != "nt":
        return path
    path = path.replace("\\", "/")
    if path.startswith("//?/"):
        path = path[4:]
    return path


class RealBindFs:
    """真实文件系统。`lstat` 只把 ENOENT 当「不存在」，其余错误一律上抛（由调用方按解析失败处理）。"""

    def lstat_kind(self, path: str) -> str:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return KIND_MISSING
        if stat.S_ISLNK(st.st_mode):
            return KIND_SYMLINK
        if stat.S_ISDIR(st.st_mode):
            return KIND_DIR
        return KIND_FILE

    def stat_is_dir(self, path: str) -> bool:
        try:
            return os.path.isdir(path)
        except (OSError, ValueError):
            return False

    def readlink(self, path: str) -> str:
        return _from_native(os.readlink(path))

    def realpath(self, path: str) -> str:
        return _from_native(os.path.realpath(path))


@dataclass(frozen=True)
class BindResolution:
    ok: bool
    # ok 时：解析后的规范路径（不含任何符号链接分量，位于某个允许根之内）
    resolved: str = ""
    # 失败时：规则后缀（见 RULE_*）与人话原因
    rule: str = ""
    reason: str = ""


def _fail(rule: str, reason: str) -> BindResolution:
    return BindResolution(False, rule=rule, reason=reason)


def _parts(path: str) -> List[str]:
    """按 `/` 切分，丢掉空段与 `.`，**保留 `..`**（`..` 必须在解析时按物理语义处理）。"""
    return [p for p in path.split("/") if p not in ("", ".")]


def _join(anchor: str, comps: Sequence[str]) -> str:
    return anchor if not comps else anchor.rstrip("/") + "/" + "/".join(comps)


def _is_absolute(path: str) -> bool:
    return path.startswith("/") or bool(_WIN_DRIVE.match(path))


def _anchors_of(root: str, fs: BindFs) -> List[str]:
    """允许根的锚点：根本身 + 它的 realpath（根自己是符号链接时，NA 用 `.resolve()` 给出的是真实路径）。

    根是运维给的、可信的，它自身是不是链接不构成问题；要防的是根**之下**的链接。
    """
    root = posixpath.normpath(root)
    anchors = [root]
    try:
        real = posixpath.normpath(fs.realpath(root))
    except (OSError, ValueError):
        return anchors
    if real != root:
        anchors.append(real)
    return anchors


def _under(anchor: str, path: str) -> bool:
    return path == anchor or path.startswith(anchor.rstrip("/") + "/")


def resolve_bind_source(
    source: str,
    roots: Sequence[str],
    fs: BindFs,
    budget: Optional[ResolveBudget] = None,
) -> BindResolution:
    """把 bind source 解析成规范路径，并确认解析的每一步都没离开允许根。

    Args:
        source: 请求里的 bind source（任意字符串，含 `..` 也行）
        roots: 允许的根（`NEKRO_DATA_DIR` 与额外根）
        fs: 文件系统面
        budget: 本请求共享的工作量预算；不传则本次调用独享一份默认预算
    """
    if budget is None:
        budget = ResolveBudget()
    if "\x00" in source:
        return _fail(RULE_UNRESOLVABLE, "bind source 含 NUL 字符")
    if len(source) > MAX_BIND_SOURCE_LENGTH:
        return _fail(RULE_TOO_COMPLEX, f"bind source 过长（{len(source)} > {MAX_BIND_SOURCE_LENGTH}）")
    src = posixpath.normpath(source)

    # 精确等于允许根：不需要访问文件系统（根是运维给的，下面没有分量可解析）。
    # 同时让「NA 重启自己」这类只挂数据目录本身的路径不依赖 guard 能否看见数据目录。
    for root in roots:
        if src == posixpath.normpath(root):
            return BindResolution(True, resolved=src)

    for root in roots:
        anchors = _anchors_of(root, fs)
        for anchor in anchors:
            if src == anchor:
                return BindResolution(True, resolved=src)
            if _under(anchor, src):
                return _resolve_below(anchor, anchors, src, fs, budget)
    return _fail(RULE_OUTSIDE, f"bind source 越界（不在数据目录内）: {source}")


def _resolve_below(anchor: str, anchors: Sequence[str], src: str, fs: BindFs, budget: ResolveBudget) -> BindResolution:
    if not fs.stat_is_dir(anchor):
        return _fail(
            RULE_ROOT_UNVERIFIABLE,
            f"docker-guard 看不到允许根目录 {anchor}（边车没有把数据目录以同一路径挂进来？），"
            "无法确认 bind source 没有经符号链接逃出数据目录，按安全侧拒绝",
        )

    anchor_parts = _parts(anchor)
    pending: Deque[str] = deque(_parts(src)[len(anchor_parts) :])
    cur: List[str] = []  # 相对 anchor 的、已解析（不含链接）的目录分量
    hops = 0

    while pending:
        comp = pending.popleft()
        if not budget.charge():
            return _fail(RULE_TOO_COMPLEX, f"解析 bind source 的工作量超过上限（{DEFAULT_RESOLVE_BUDGET}），按安全侧拒绝")
        if comp in ("", "."):
            continue
        if comp == "..":
            if not cur:
                return _fail(RULE_ESCAPES, "路径经 `..` 爬出了数据目录")
            cur.pop()
            continue

        candidate = _join(anchor, [*cur, comp])
        try:
            kind = fs.lstat_kind(candidate)
        except (OSError, ValueError) as e:
            return _fail(RULE_UNRESOLVABLE, f"无法解析路径分量 {candidate}: {e}")

        if kind == KIND_MISSING:
            rest = [p for p in pending if p not in ("", ".")]
            if ".." in rest:
                return _fail(RULE_UNRESOLVABLE, f"不存在的分量 {candidate} 之后还有 `..`，无法确定物理路径")
            cur.append(comp)
            cur.extend(rest)
            break

        if kind == KIND_SYMLINK:
            hops += 1
            if hops > MAX_LINK_HOPS:
                return _fail(RULE_UNRESOLVABLE, f"符号链接层数超过 {MAX_LINK_HOPS}（链接环？）: {src}")
            if not budget.charge(LINK_COST):
                return _fail(RULE_TOO_COMPLEX, f"解析 bind source 的工作量超过上限（{DEFAULT_RESOLVE_BUDGET}），按安全侧拒绝")
            try:
                target = fs.readlink(candidate)
            except (OSError, ValueError) as e:
                return _fail(RULE_UNRESOLVABLE, f"无法读取符号链接 {candidate}: {e}")
            if not target or "\x00" in target:
                return _fail(RULE_UNRESOLVABLE, f"符号链接 {candidate} 的目标为空或非法")
            tparts = _parts(target)
            if _is_absolute(target):
                remainder: Optional[List[str]] = None
                for a in anchors:
                    ap = _parts(a)
                    if tparts[: len(ap)] == ap:
                        remainder = tparts[len(ap) :]
                        break
                if remainder is None:
                    return _fail(RULE_ESCAPES, f"符号链接 {candidate} 指向数据目录之外: {target}")
                cur = []
                pending = deque([*remainder, *pending])
            else:
                # 相对链接相对其所在目录（即当前 cur）
                pending = deque([*tparts, *pending])
            continue

        if kind == KIND_FILE and any(p not in ("", ".") for p in pending):
            return _fail(RULE_UNRESOLVABLE, f"{candidate} 不是目录，却还有后续分量")
        cur.append(comp)

    resolved = _join(anchor, cur)
    if not _under(anchor, resolved):  # 理论上不可达：不变量由上面每一步保证
        return _fail(RULE_ESCAPES, "解析结果落在数据目录之外")
    return BindResolution(True, resolved=resolved)
