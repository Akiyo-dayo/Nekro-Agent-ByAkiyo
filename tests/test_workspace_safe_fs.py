"""宿主侧读写容器可写目录：不跟随符号链接

## 这条测试钉的是什么

- 写：途中任何一级是链接就拒绝（或按要求删掉链接重建目录），写入是临时文件 + rename，
  目标是链接时只换掉链接、不写到链接指向的地方；
- 读：链接、目录、超长都拒绝，不存在返回 None；
- 删：递归删除遇到链接只删链接本身；只删空目录的 `rmdir` 不碰链接与非空目录；
- 改权限：是链接就拒绝；
- 复制：源目录树里的链接跳过；
- 路径：拒绝 `..`、绝对路径、NUL。

建不了符号链接的机器（Windows 没开开发者模式）上，需要链接的用例跳过。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from nekro_agent.services.workspace import safe_fs
from nekro_agent.services.workspace.safe_fs import UnsafePathError


def _link(link: Path, target: Path, *, is_dir: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("这台机器上建不了符号链接")


@pytest.fixture
def layout(tmp_path: Path):
    base = tmp_path / "ws"
    (base / ".claude_home").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("platform-key", encoding="utf-8")
    return base, outside, secret


def test_write_and_read_round_trip(layout) -> None:
    base, _outside, _secret = layout
    safe_fs.write_text(base, ".claude_home/skills/demo/SKILL.md", "hello")
    assert safe_fs.read_text(base, ".claude_home/skills/demo/SKILL.md") == "hello"
    assert safe_fs.read_text(base, ".claude_home/skills/missing.md") is None
    assert safe_fs.kind(base, ".claude_home/skills/demo") == "dir"
    assert safe_fs.kind(base, ".claude_home/nope/x") is None


def test_rejects_traversal_absolute_and_nul(layout) -> None:
    base, _outside, _secret = layout
    for rel in ("../escape.txt", "/etc/passwd", ".claude_home/../../x", "a/\x00b"):
        with pytest.raises(UnsafePathError):
            safe_fs.write_text(base, rel, "x")


def test_symlinked_directory_on_the_way_is_refused(layout) -> None:
    base, outside, _secret = layout
    _link(base / ".claude_home" / "skills", outside, is_dir=True)

    with pytest.raises(UnsafePathError):
        safe_fs.write_text(base, ".claude_home/skills/evil.md", "x")
    assert list(outside.iterdir()) == [outside / "secret.txt"], "绝不写到链接指向的地方"
    with pytest.raises(UnsafePathError):
        safe_fs.read_text(base, ".claude_home/skills/secret.txt")


def test_replace_links_rebuilds_the_directory(layout) -> None:
    base, outside, _secret = layout
    _link(base / ".claude_home" / "nk-ssh", outside, is_dir=True)

    safe_fs.write_text(base, ".claude_home/nk-ssh/config", "x", replace_links=True)

    assert not (base / ".claude_home" / "nk-ssh").is_symlink()
    assert (base / ".claude_home" / "nk-ssh" / "config").read_text(encoding="utf-8") == "x"
    assert sorted(item.name for item in outside.iterdir()) == ["secret.txt"]


def test_writing_over_a_symlinked_file_replaces_the_link(layout) -> None:
    base, _outside, secret = layout
    _link(base / ".claude_home" / "settings.json", secret, is_dir=False)

    safe_fs.write_text(base, ".claude_home/settings.json", "{}")

    assert secret.read_text(encoding="utf-8") == "platform-key", "链接指向的文件不能被改"
    assert not (base / ".claude_home" / "settings.json").is_symlink()
    assert (base / ".claude_home" / "settings.json").read_text(encoding="utf-8") == "{}"


def test_reading_a_symlinked_file_is_refused(layout) -> None:
    base, _outside, secret = layout
    _link(base / ".claude_home" / "CLAUDE.md", secret, is_dir=False)
    with pytest.raises(UnsafePathError):
        safe_fs.read_text(base, ".claude_home/CLAUDE.md")


def test_read_size_limit(layout) -> None:
    base, _outside, _secret = layout
    safe_fs.write_text(base, ".claude_home/big.txt", "x" * 100)
    with pytest.raises(UnsafePathError):
        safe_fs.read_text(base, ".claude_home/big.txt", max_bytes=10)


def test_remove_tree_only_removes_links_not_their_targets(layout) -> None:
    base, outside, secret = layout
    safe_fs.write_text(base, ".claude_home/skills/demo/SKILL.md", "hello")
    _link(base / ".claude_home" / "skills" / "demo" / "escape", outside, is_dir=True)

    assert safe_fs.remove(base, ".claude_home/skills") is True

    assert not (base / ".claude_home" / "skills").exists()
    assert secret.read_text(encoding="utf-8") == "platform-key"


def test_rmdir_only_removes_empty_real_directories(layout) -> None:
    base, outside, _secret = layout
    safe_fs.ensure_dir(base, ".claude")
    safe_fs.write_text(base, ".claude_home/keep/x", "1")
    with safe_fs.open_dir(base) as root:
        assert root.rmdir(".claude") is True
        with root.subdir(".claude_home") as home:
            assert home.rmdir("keep") is False, "不空的目录不删"
            _link(base / ".claude_home" / "linked", outside, is_dir=True)
            assert home.rmdir("linked") is False, "链接不当目录删"
    assert outside.exists()


def test_chmod_refuses_links(layout) -> None:
    base, _outside, secret = layout
    _link(base / ".claude_home" / "id_gateway", secret, is_dir=False)
    with safe_fs.open_dir(base, ".claude_home") as home:
        with pytest.raises(UnsafePathError):
            home.chmod("id_gateway", 0o600)


def test_copy_tree_in_skips_links_in_the_source(tmp_path: Path, layout) -> None:
    base, _outside, secret = layout
    source = tmp_path / "skill-src"
    (source / "nested").mkdir(parents=True)
    (source / "SKILL.md").write_text("skill", encoding="utf-8")
    (source / "nested" / "a.txt").write_text("a", encoding="utf-8")
    try:
        (source / "leak.txt").symlink_to(secret)
    except (OSError, NotImplementedError):
        pass

    with safe_fs.open_dir(base, ".claude_home/skills", create=True) as skills:
        skills.copy_tree_in(source, "demo")

    target = base / ".claude_home" / "skills" / "demo"
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "skill"
    assert (target / "nested" / "a.txt").read_text(encoding="utf-8") == "a"
    assert not (target / "leak.txt").exists()


@pytest.mark.skipif(not safe_fs.DIR_FD_SUPPORTED, reason="目录句柄只在 Linux / macOS 上有")
def test_fifo_is_not_read(layout) -> None:
    base, _outside, _secret = layout
    os.mkfifo(base / ".claude_home" / "pipe")
    with pytest.raises(UnsafePathError):
        safe_fs.read_text(base, ".claude_home/pipe")


# ---------------------------------------------------------------------------
# 保留权限与属主、流式写、独占新建、改名、流式读与复制出去
# ---------------------------------------------------------------------------


def test_rewrite_keeps_the_previous_mode_unless_told_otherwise(layout) -> None:
    base, _outside, _secret = layout
    safe_fs.write_text(base, ".claude_home/run.sh", "v1", mode=0o755)
    safe_fs.write_text(base, ".claude_home/run.sh", "v2")
    assert safe_fs.read_text(base, ".claude_home/run.sh") == "v2"
    if os.name != "nt":
        assert (base / ".claude_home" / "run.sh").stat().st_mode & 0o777 == 0o755
    safe_fs.write_text(base, ".claude_home/run.sh", "v3", mode=0o644)
    if os.name != "nt":
        assert (base / ".claude_home" / "run.sh").stat().st_mode & 0o777 == 0o644


def test_write_stream_replaces_a_link_instead_of_following_it(layout) -> None:
    import io

    base, _outside, secret = layout
    _link(base / ".claude_home" / "out.bin", secret, is_dir=False)
    with safe_fs.open_dir(base, ".claude_home") as directory:
        assert directory.write_stream("out.bin", io.BytesIO(b"data")) == 4
    assert secret.read_text(encoding="utf-8") == "platform-key"
    assert not (base / ".claude_home" / "out.bin").is_symlink()
    assert (base / ".claude_home" / "out.bin").read_bytes() == b"data"


def test_write_stream_enforces_the_size_cap(layout) -> None:
    import io

    base, _outside, _secret = layout
    with safe_fs.open_dir(base, ".claude_home") as directory:
        with pytest.raises(UnsafePathError):
            directory.write_stream("big.bin", io.BytesIO(b"x" * 10), max_bytes=4)
        assert directory.kind("big.bin") is None


def test_create_exclusive_refuses_an_existing_link(layout) -> None:
    base, _outside, secret = layout
    _link(base / ".claude_home" / "new.md", secret, is_dir=False)
    with safe_fs.open_dir(base, ".claude_home") as directory:
        with pytest.raises(FileExistsError):
            directory.create_exclusive("new.md", b"x")
        directory.create_exclusive("fresh.md", b"x")
        with pytest.raises(FileExistsError):
            directory.create_exclusive("fresh.md", b"y")
    assert secret.read_text(encoding="utf-8") == "platform-key"
    assert (base / ".claude_home" / "fresh.md").read_bytes() == b"x"


def test_open_read_refuses_links_and_streams_regular_files(layout) -> None:
    base, _outside, secret = layout
    safe_fs.write_text(base, ".claude_home/ok.txt", "fine")
    _link(base / ".claude_home" / "leak.txt", secret, is_dir=False)
    with safe_fs.open_dir(base, ".claude_home") as directory:
        with directory.open_read("ok.txt") as handle:
            assert handle.read() == b"fine"
        with pytest.raises(UnsafePathError):
            directory.open_read("leak.txt")
        with pytest.raises(FileNotFoundError):
            directory.open_read("missing.txt")


def test_copy_out_and_copy_tree_out_never_carry_link_targets(layout, tmp_path: Path) -> None:
    base, outside, secret = layout
    safe_fs.write_text(base, ".claude_home/skills/demo/SKILL.md", "skill")
    safe_fs.write_text(base, ".claude_home/skills/demo/nested/a.txt", "a")
    _link(base / ".claude_home" / "skills" / "demo" / "leak.txt", secret, is_dir=False)
    _link(base / ".claude_home" / "skills" / "demo" / "leakdir", outside, is_dir=True)

    with safe_fs.open_dir(base, ".claude_home/skills") as skills:
        skills.copy_tree_out("demo", tmp_path / "copied")
        with skills.subdir("demo") as demo:
            with pytest.raises(UnsafePathError):
                demo.copy_out("leak.txt", tmp_path / "single.txt")

    copied = tmp_path / "copied"
    assert (copied / "SKILL.md").read_text(encoding="utf-8") == "skill"
    assert (copied / "nested" / "a.txt").read_text(encoding="utf-8") == "a"
    assert not (copied / "leak.txt").exists()
    assert not (copied / "leakdir").exists()
    assert not (tmp_path / "single.txt").exists()


def test_rename_to_moves_the_entry_itself(layout) -> None:
    base, _outside, secret = layout
    safe_fs.write_text(base, "default/kb/files/a.md", "A")
    safe_fs.ensure_dir(base, "default/kb/files/sub")
    with safe_fs.open_dir(base, "default/kb/files") as src, safe_fs.open_dir(base, "default/kb/files/sub") as dst:
        src.rename_to("a.md", dst, "b.md")
        with pytest.raises(FileExistsError):
            dst.rename_to("b.md", dst, "b.md")
    assert (base / "default" / "kb" / "files" / "sub" / "b.md").read_text(encoding="utf-8") == "A"
    assert secret.read_text(encoding="utf-8") == "platform-key"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="只在 Linux 上要求目录句柄可用")
def test_linux_uses_the_directory_handle_path() -> None:
    """判据写错会让 Linux 上悄悄走退路（逐级 is_symlink 检查），这里钉死"""
    assert safe_fs.DIR_FD_SUPPORTED is True


def test_a_directory_target_is_an_unsafe_path_error(layout) -> None:
    base, _outside, _secret = layout
    safe_fs.ensure_dir(base, ".claude_home/settings.json")
    with safe_fs.open_dir(base, ".claude_home") as directory:
        with pytest.raises(UnsafePathError):
            directory.write_text("settings.json", "{}")
        with pytest.raises(UnsafePathError):
            directory.unlink("settings.json")
    assert (base / ".claude_home" / "settings.json").is_dir()
    assert not (base / ".claude_home" / ".settings.json.nk-tmp").exists(), "临时文件不留"


def test_chmod_on_a_linked_directory_is_refused(layout) -> None:
    base, outside, _secret = layout
    _link(base / "shared", outside, is_dir=True)
    with pytest.raises(UnsafePathError):
        safe_fs.chmod(base, ("shared",), 0o777)
    # 是 NA 自己的路径时：先删掉链接重建，再收权限
    safe_fs.ensure_dir(base, ("shared",), replace_links=True)
    safe_fs.chmod(base, ("shared",), 0o777)
    assert not (base / "shared").is_symlink()
    assert list(outside.iterdir()) == [outside / "secret.txt"]
