"""目录句柄那条路（Linux）上的宿主侧加固：清理不跟随换掉的中间目录、深目录不撞递归上限、读有上限

只依赖 `safe_fs` 本身，可以打包进自包含脚本，在一次性容器里跑（做法同 `test_workspace_safe_fs.py`）。
不支持目录句柄的平台（Windows）整份跳过；那里的行为由 `test_ws_host_hardening.py` 覆盖。
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

from nekro_agent.services.workspace import safe_fs

pytestmark = pytest.mark.skipif(not safe_fs.DIR_FD_SUPPORTED, reason="只在支持目录句柄的平台（Linux）上跑")

SECRET = "platform-" + "key-linux"


@pytest.fixture
def world(tmp_path: Path) -> Path:
    outside = tmp_path / "host-elsewhere"
    outside.mkdir()
    (outside / "victim.txt").write_text(SECRET, encoding="utf-8")
    base = tmp_path / "sandboxes"
    (base / "key" / "sub").mkdir(parents=True)
    (base / "key" / "sub" / "victim.txt").write_text("sandbox file", encoding="utf-8")
    return tmp_path


def test_remove_file_does_not_follow_a_swapped_intermediate_dir(world: Path) -> None:
    """列完要删的文件之后，沙盒把中间一级目录换成指向宿主别处的链接：不删、宿主文件原样"""
    base = world / "sandboxes"
    (base / "key" / "sub").rename(base / "key" / "sub-moved")
    (base / "key" / "sub").symlink_to(world / "host-elsewhere", target_is_directory=True)

    with pytest.raises(safe_fs.UnsafePathError):
        safe_fs.remove_file(base, ["key", "sub", "victim.txt"])
    assert (world / "host-elsewhere" / "victim.txt").read_text(encoding="utf-8") == SECRET


def test_remove_file_takes_only_the_link_itself(world: Path) -> None:
    base = world / "sandboxes"
    (base / "key" / "link.txt").symlink_to(world / "host-elsewhere" / "victim.txt")

    assert safe_fs.remove_file(base, ["key", "link.txt"]) is True
    assert not (base / "key" / "link.txt").is_symlink()
    assert (world / "host-elsewhere" / "victim.txt").read_text(encoding="utf-8") == SECRET


def test_remove_file_reports_missing_and_refuses_directories(world: Path) -> None:
    base = world / "sandboxes"
    assert safe_fs.remove_file(base, ["key", "nope.txt"]) is False
    assert safe_fs.remove_file(base, ["gone", "nope.txt"]) is False
    with pytest.raises(safe_fs.UnsafePathError):
        safe_fs.remove_file(base, ["key", "sub"])


def test_remove_empty_dirs_does_not_walk_into_links(world: Path) -> None:
    base = world / "sandboxes"
    (world / "host-elsewhere" / "empty-on-host").mkdir()
    (base / "key" / "empty-in-sandbox").mkdir()
    (base / "key" / "to-host").symlink_to(world / "host-elsewhere", target_is_directory=True)

    safe_fs.remove_empty_dirs(base)

    assert (world / "host-elsewhere" / "empty-on-host").is_dir()
    assert not (base / "key" / "empty-in-sandbox").exists()
    assert (base / "key" / "to-host").is_symlink()


def _deep_tree(root: Path, depth: int) -> Path:
    """建一棵 `depth` 层的目录（逐级用目录句柄建，路径再长也不撞 PATH_MAX）；返回最深那一级的路径"""
    fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(depth):
            os.mkdir("d", dir_fd=fd)
            child = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
    finally:
        os.close(fd)
    return root.joinpath(*(["d"] * depth))


def _remove_deep_tree(root: Path, depth: int) -> None:
    """不靠递归地把那棵深树删掉（pytest 清临时目录时的递归删除会撞上递归上限）"""
    for level in range(depth, 0, -1):
        try:
            os.rmdir(root.joinpath(*(["d"] * level)))
        except OSError:
            pass


@pytest.mark.parametrize("depth", [1100])
def test_a_very_deep_tree_is_removed_completely(tmp_path: Path, depth: int) -> None:
    """嵌套 1100 层：原来按递归删会在第 1000 层上下撞上递归上限；之后改成到深度上限就停，
    同步技能时碰上这样一棵树整个启动都会失败。现在删除不递归、不设深度上限，删干净"""
    root = tmp_path / "deep"
    root.mkdir()
    (_deep_tree(root, depth) / "leaf.txt").write_text("x", encoding="utf-8")
    try:
        assert safe_fs.remove(tmp_path, ["deep", "d"]) is True
        assert sorted(p.name for p in root.iterdir()) == []
    finally:
        _remove_deep_tree(root, depth)


@pytest.mark.parametrize("depth", [1100])
def test_walking_a_very_deep_tree_stops_with_tree_too_deep(tmp_path: Path, depth: int) -> None:
    """遍历（复制、列文件）仍然到深度上限就停，抛的是「太深」不是 RecursionError"""
    root = tmp_path / "deep"
    root.mkdir()
    _deep_tree(root, depth)
    try:
        with safe_fs.open_dir(tmp_path, ["deep"]) as directory, pytest.raises(OSError) as raised:
            list(safe_fs.iter_files(directory))
        assert type(raised.value).__name__ == "TreeTooDeepError"
        assert [p for p, _ in safe_fs.walk_regular_files(root)] == []
    finally:
        _remove_deep_tree(root, depth)


def test_remove_tree_takes_links_inside_the_tree_as_links(world: Path) -> None:
    """删的那棵树里有指向宿主别处的链接（目录、文件都有）：只删链接本身"""
    base = world / "sandboxes"
    (base / "key" / "sub" / "deeper").mkdir()
    (base / "key" / "sub" / "deeper" / "to-host").symlink_to(world / "host-elsewhere", target_is_directory=True)
    (base / "key" / "sub" / "victim-link.txt").symlink_to(world / "host-elsewhere" / "victim.txt")

    assert safe_fs.remove(base, ["key", "sub"]) is True

    assert not (base / "key" / "sub").exists()
    assert sorted(p.name for p in (base / "key").iterdir()) == []
    assert (world / "host-elsewhere" / "victim.txt").read_text(encoding="utf-8") == SECRET


def test_names_are_resolved_without_ever_opening_a_special_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """按名字打开要读的文件：先用 `O_PATH` 拿到不开设备、不碰 FIFO 的句柄，判过是普通文件才重新打开

    原来直接 `O_RDONLY|O_NONBLOCK` 打开再判类型：打开那一下对设备节点就已经有副作用了。
    """
    if not safe_fs.PATH_FD_SUPPORTED:
        pytest.skip("没有 O_PATH 或 /proc/self/fd")
    os.mkfifo(tmp_path / "pipe")
    (tmp_path / "plain.txt").write_text("ok", encoding="utf-8")
    real_open = os.open
    opened = []

    def recording_open(path, flags, *args, **kwargs):  # noqa: ANN001, ANN202 - 透传 os.open
        opened.append((str(path), flags))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(safe_fs.os, "open", recording_open)
    with safe_fs.open_dir(tmp_path) as directory:
        with pytest.raises(safe_fs.UnsafePathError):
            directory.open_read("pipe")
        with directory.open_read("plain.txt") as handle:
            assert handle.read() == b"ok"

    by_name = [(name, flags) for name, flags in opened if name in ("pipe", "plain.txt")]
    assert by_name, "按名字的打开应当经过 os.open"
    assert all(flags & os.O_PATH for _name, flags in by_name), "按名字打开一律是 O_PATH"
    assert any(name.startswith("/proc/self/fd/") for name, _ in opened), "普通文件经 /proc/self/fd 重新打开"


def _take_write_lease(directory: Path) -> "tuple[Path, int] | None":
    """在 `directory` 里建一个文件并对它拿写租约；这个文件系统不支持租约时返回 None"""
    import fcntl

    set_lease = getattr(fcntl, "F_SETLEASE", 1024)
    write_lock = getattr(fcntl, "F_WRLCK", 1)
    target = directory / "leased.txt"
    target.write_text("leased", encoding="utf-8")
    for flags in (os.O_RDWR, os.O_RDONLY):
        fd = os.open(target, flags)
        try:
            fcntl.fcntl(fd, set_lease, write_lock)
        except OSError:
            os.close(fd)
            continue
        return target, fd
    target.unlink()
    return None


def test_a_file_under_a_write_lease_is_refused_at_once(tmp_path: Path) -> None:
    """文件的属主（容器里的 appuser）对自己的文件拿写租约：NA 按名字打开时不能在 break_lease 上
    等（默认 45 秒，而且能反复来），要立刻失败、说清是被占着"""
    import fcntl
    import signal
    import time

    if not safe_fs.PATH_FD_SUPPORTED:
        pytest.skip("没有 O_PATH 或 /proc/self/fd")
    # 租约被打断时内核给持有者发 SIGIO，默认动作是结束进程
    previous_handler = signal.signal(signal.SIGIO, signal.SIG_IGN)
    leased = None
    shm_dir = None
    try:
        leased = _take_write_lease(tmp_path)
        if leased is None and Path("/dev/shm").is_dir():
            shm_dir = Path("/dev/shm") / f"nk-lease-{os.getpid()}"
            shm_dir.mkdir()
            leased = _take_write_lease(shm_dir)
        if leased is None:
            pytest.skip("这台机器的文件系统上拿不到写租约（overlayfs 一类）")
        target, lease_fd = leased
        started = time.monotonic()
        with safe_fs.open_dir(target.parent) as directory:
            with pytest.raises(OSError) as raised:
                directory.open_read(target.name)
            with pytest.raises(OSError):
                directory.read_bytes(target.name)
        assert time.monotonic() - started < 5, "不该等租约"
        assert type(raised.value).__name__ == "UnsafePathError"
        assert "租约" in str(raised.value)
    finally:
        if leased is not None:
            target, lease_fd = leased
            with contextlib.suppress(OSError):
                fcntl.fcntl(lease_fd, getattr(fcntl, "F_SETLEASE", 1024), getattr(fcntl, "F_UNLCK", 2))
            os.close(lease_fd)
            with contextlib.suppress(OSError):
                target.unlink()
        if shm_dir is not None:
            with contextlib.suppress(OSError):
                shm_dir.rmdir()
        signal.signal(signal.SIGIO, previous_handler)


def test_reading_a_plain_file_still_works_after_the_non_blocking_reopen(tmp_path: Path) -> None:
    """对照：重新打开带 O_NONBLOCK、打开后改回阻塞，普通文件照常读完"""
    (tmp_path / "plain.bin").write_bytes(b"p" * 200_000)
    with safe_fs.open_dir(tmp_path) as directory:
        with directory.open_read("plain.bin") as handle:
            assert os.get_blocking(handle.fileno()) is True
            assert len(handle.read()) == 200_000
        assert directory.read_bytes("plain.bin", max_bytes=300_000) == b"p" * 200_000


def test_chmod_never_follows_a_link_and_never_opens_a_fifo(world: Path) -> None:
    if not safe_fs.PATH_FD_SUPPORTED:
        pytest.skip("没有 O_PATH 或 /proc/self/fd")
    base = world / "sandboxes"
    target = world / "host-elsewhere" / "victim.txt"
    os.chmod(target, 0o600)
    (base / "key" / "link.txt").symlink_to(target)
    os.mkfifo(base / "key" / "pipe")
    (base / "key" / "plain.txt").write_text("p", encoding="utf-8")

    with safe_fs.open_dir(base, ["key"]) as directory:
        with pytest.raises(safe_fs.UnsafePathError):
            directory.chmod("link.txt", 0o777)
        with pytest.raises(safe_fs.UnsafePathError):
            directory.chmod("pipe", 0o777)
        directory.chmod("plain.txt", 0o640)

    assert (os.stat(target).st_mode & 0o777) == 0o600
    assert (os.stat(base / "key" / "plain.txt").st_mode & 0o777) == 0o640


def test_a_sparse_file_is_refused_when_opened(tmp_path: Path) -> None:
    """容器建一个 1 GiB 的稀疏文件只要一瞬间：打开时就按大小拒绝，抛的是「太大」"""
    with (tmp_path / "sparse.bin").open("wb") as handle:
        handle.truncate(1024 * 1024 * 1024)
    with safe_fs.open_dir(tmp_path) as directory, pytest.raises(OSError) as raised:
        directory.open_read("sparse.bin", max_bytes=1024 * 1024)
    assert type(raised.value).__name__ == "FileTooLargeError"


def test_a_file_growing_while_read_through_a_dir_fd_is_cut_off(tmp_path: Path) -> None:
    target = tmp_path / "growing.bin"
    target.write_bytes(b"x" * 10)
    with safe_fs.open_dir(tmp_path) as directory:
        handle = directory.open_read("growing.bin", max_bytes=100)
    with target.open("ab") as appender:
        appender.write(b"y" * 4096)
    with handle, pytest.raises(OSError) as raised:
        handle.read()
    assert type(raised.value).__name__ == "FileTooLargeError"


def test_streaming_copy_into_a_container_dir_is_bounded(tmp_path: Path) -> None:
    """往容器可写目录里流式写（下载 / 上传都是这样）：写到超过上限就停，临时文件不留"""
    (tmp_path / "src.bin").write_bytes(b"s" * 5000)
    (tmp_path / "dst").mkdir()
    with safe_fs.open_dir(tmp_path) as source_dir, source_dir.open_read("src.bin") as source:
        with safe_fs.open_dir(tmp_path, ["dst"]) as target, pytest.raises(OSError) as raised:
            target.write_stream("out.bin", source, max_bytes=1000)
    assert type(raised.value).__name__ == "FileTooLargeError"
    assert sorted(p.name for p in (tmp_path / "dst").iterdir()) == []
