"""docker-guard 的 bind source 解析用例共用的内存文件系统（不依赖真实符号链接，Windows 上也能跑）。

这不是一个「按字典查路径」的桩，而是一个**迷你内核**：路径查找按 POSIX 语义逐分量走，
中间分量是符号链接就跟随、`..` 取物理上一级、有环报 ELOOP、不存在报 ENOENT。
这样两件事才成立：

1. 被测的解析器如果偷懒地对「还没展开的路径」直接 `lstat`，这里会拿到**跟随了中间链接**的结果，
   与真实内核一致——桩不会替它掩盖这个错误；
2. `physical()` 给出「Docker 守护进程实际会挂到哪里」的**真值**，属性测试拿它当裁判。

条目格式：`路径 -> "dir" | "file" | ("link", 目标)`；祖先目录缺省按 dir 补齐。
"""

from __future__ import annotations

import errno
import posixpath
from collections import deque
from typing import Any, Deque, Dict, List, Mapping, Optional, Tuple

_MAX_HOPS = 40
Entry = Any  # "dir" | "file" | ("link", str)


class FakeBindFs:
    def __init__(self, entries: Mapping[str, Entry], errors: Optional[Mapping[str, OSError]] = None) -> None:
        self.entries: Dict[str, Entry] = {"/": "dir"}
        for path, entry in entries.items():
            path = posixpath.normpath(path)
            self.entries[path] = entry
            parent = posixpath.dirname(path)
            while parent not in self.entries:
                self.entries[parent] = "dir"
                parent = posixpath.dirname(parent)
        # 对这些路径的 lstat / readlink 直接抛错（模拟 EACCES 之类）
        self.errors: Dict[str, OSError] = {posixpath.normpath(k): v for k, v in (errors or {}).items()}
        self.lstat_calls: List[str] = []

    # ---- 迷你内核 ----

    def _walk(self, path: str, follow_last: bool) -> Tuple[Optional[str], str]:
        """返回 (物理路径, 类型)。路径不存在返回 (None, "missing")；中间分量是文件抛 ENOTDIR；有环抛 ELOOP。"""
        if not path.startswith("/"):
            raise ValueError(f"FakeBindFs 只处理绝对路径: {path!r}")
        pending: Deque[str] = deque(p for p in path.split("/") if p)
        cur: List[str] = []
        hops = 0
        while pending:
            comp = pending.popleft()
            if comp == ".":
                continue
            if comp == "..":
                if cur:
                    cur.pop()
                continue
            cand = "/" + "/".join([*cur, comp])
            if cand in self.errors:
                raise self.errors[cand]
            entry = self.entries.get(cand)
            if entry is None:
                return None, "missing"
            if isinstance(entry, tuple):
                if pending or follow_last:
                    hops += 1
                    if hops > _MAX_HOPS:
                        raise OSError(errno.ELOOP, "Too many levels of symbolic links", path)
                    target: str = entry[1]
                    if target.startswith("/"):
                        cur = []
                    pending = deque([*(p for p in target.split("/") if p), *pending])
                    continue
                return "/" + "/".join([*cur, comp]), "symlink"
            if entry == "file" and pending:
                raise OSError(errno.ENOTDIR, "Not a directory", path)
            cur.append(comp)
        phys = "/" + "/".join(cur)
        return phys, "dir" if self.entries.get(phys) == "dir" else "file"

    # ---- BindFs 接口 ----

    def lstat_kind(self, path: str) -> str:
        self.lstat_calls.append(path)
        phys, kind = self._walk(path, follow_last=False)
        return "missing" if phys is None else kind

    def stat_is_dir(self, path: str) -> bool:
        try:
            phys, kind = self._walk(path, follow_last=True)
        except OSError:
            return False
        return phys is not None and kind == "dir"

    def readlink(self, path: str) -> str:
        if posixpath.normpath(path) in self.errors:
            raise self.errors[posixpath.normpath(path)]
        phys, kind = self._walk(path, follow_last=False)
        if phys is None or kind != "symlink":
            raise OSError(errno.EINVAL, "Invalid argument", path)
        return self.entries[phys][1]

    def realpath(self, path: str) -> str:
        return self.physical(path) or path

    # ---- 真值 ----

    def physical(self, path: str) -> Optional[str]:
        """Docker 守护进程挂载 `path` 时实际落到的物理路径（不存在的尾部按字面补上，因为 Docker 会建目录）。

        中间链接环 / 不存在分量之后还有 `..` 等「内核也解不出来」的情形返回 None。
        """
        pending: Deque[str] = deque(p for p in path.split("/") if p)
        cur: List[str] = []
        hops = 0
        while pending:
            comp = pending.popleft()
            if comp == ".":
                continue
            if comp == "..":
                if cur:
                    cur.pop()
                continue
            cand = "/" + "/".join([*cur, comp])
            entry = self.entries.get(cand)
            if entry is None:
                rest = [p for p in pending if p != "."]
                if ".." in rest:
                    return None
                return "/" + "/".join([*cur, comp, *rest])
            if isinstance(entry, tuple):
                hops += 1
                if hops > _MAX_HOPS:
                    return None
                target = entry[1]
                if target.startswith("/"):
                    cur = []
                pending = deque([*(p for p in target.split("/") if p), *pending])
                continue
            if entry == "file" and pending:
                return None
            cur.append(comp)
        return "/" + "/".join(cur)


def data_dir_fs(root: str) -> FakeBindFs:
    """只有一个空数据目录的文件系统：根下面的东西都「不存在」，bind 靠词法拼接放行（旧用例的前提）。"""
    return FakeBindFs({root: "dir"})
