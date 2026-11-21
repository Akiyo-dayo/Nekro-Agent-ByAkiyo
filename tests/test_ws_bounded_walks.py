"""宿主侧遍历容器可写目录：不跟随链接、深度有上限、删不完的不卡住启动

容器能在它可写的目录里放指向宿主别处的链接、造很深的目录、塞几百万个文件。宿主侧的遍历
（空间扫描统计大小、清理按时间挑文件、记忆目录树、清空记忆）原来用 `rglob` / `is_file()` /
`stat()`，会顺着链接去量宿主文件，也没有深度上限；同步技能时删旧目录碰上很深的树会让启动
整个失败。现在一律走 `safe_fs.walk_regular_files` 或带深度预算的目录句柄遍历；启动路径上删不完
的树挪进工作区顶层的 `.nk-trash-*`，下次再清。

建不了符号链接的机器上，用到链接的用例跳过。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List

import pytest

from nekro_agent.services.workspace import safe_fs
from nekro_agent.services.workspace.manager import WorkspaceService


def _link(link: Path, target: Path, *, is_dir: bool) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("这台机器上建不了符号链接")


def _deep(base: Path, levels: int) -> Path:
    deep = base
    for index in range(levels):
        deep = deep / f"d{index}"
    deep.mkdir(parents=True)
    return deep


@pytest.fixture
def logs() -> List[str]:
    from loguru import logger

    messages: List[str] = []
    sink_id = logger.add(lambda message: messages.append(message.record["message"]), level="DEBUG")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """宿主上的「别处」：一个很大的文件，不该被量到、列到"""
    target = tmp_path / "host-elsewhere"
    target.mkdir()
    (target / "big.bin").write_bytes(b"h" * 10_000)
    return target


class TestWalkRegularFiles:
    def test_links_are_neither_followed_nor_listed_by_default(self, tmp_path: Path, outside: Path) -> None:
        base = tmp_path / "base"
        (base / "sub").mkdir(parents=True)
        (base / "sub" / "plain.txt").write_text("abc", encoding="utf-8")
        _link(base / "to-host", outside, is_dir=True)
        _link(base / "big-link.bin", outside / "big.bin", is_dir=False)

        listed = {path.name: st.st_size for path, st in safe_fs.walk_regular_files(base)}

        assert listed == {"plain.txt": 3}

    def test_links_themselves_are_listed_when_asked(self, tmp_path: Path, outside: Path) -> None:
        """清理要把链接本身也删掉：列出来，`lstat` 是链接自己的，不是那头的大文件"""
        base = tmp_path / "base"
        base.mkdir()
        _link(base / "big-link.bin", outside / "big.bin", is_dir=False)
        _link(base / "to-host", outside, is_dir=True)

        listed = {path.name: st.st_size for path, st in safe_fs.walk_regular_files(base, include_links=True)}

        assert set(listed) == {"big-link.bin", "to-host"}
        assert listed["big-link.bin"] != 10_000
        assert not any(path.parent == base / "to-host" for path, _ in safe_fs.walk_regular_files(base, include_links=True))

    def test_a_tree_deeper_than_the_limit_is_cut_off_not_an_error(self, tmp_path: Path) -> None:
        base = tmp_path / "base"
        (base / "top.txt").parent.mkdir(parents=True)
        (base / "top.txt").write_text("t", encoding="utf-8")
        (_deep(base, 10) / "bottom.txt").write_text("b", encoding="utf-8")

        listed = [path.name for path, _ in safe_fs.walk_regular_files(base, max_depth=3)]

        assert listed == ["top.txt"]

    def test_a_missing_root_lists_nothing(self, tmp_path: Path) -> None:
        assert list(safe_fs.walk_regular_files(tmp_path / "missing")) == []


class TestSpaceScanner:
    def test_size_is_not_measured_through_links(self, tmp_path: Path, outside: Path) -> None:
        from nekro_agent.services.space_cleanup.scanner import scanner_service

        base = tmp_path / "uploads"
        base.mkdir()
        (base / "own.bin").write_bytes(b"o" * 5)
        _link(base / "to-host", outside, is_dir=True)
        _link(base / "big-link.bin", outside / "big.bin", is_dir=False)

        size = asyncio.run(scanner_service._get_directory_size(base))  # noqa: SLF001
        total, files = asyncio.run(scanner_service._scan_directory_simple(base))  # noqa: SLF001

        assert size == 5
        assert (total, files) == (5, 1)


class TestMemoryTree:
    @pytest.fixture
    def memory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        root = tmp_path / "workspaces"
        monkeypatch.setattr(WorkspaceService, "get_workspace_dir", staticmethod(lambda workspace_id: root / str(workspace_id)))
        monkeypatch.setattr(safe_fs, "MAX_TREE_DEPTH", 3)
        memory_dir = root / "1"
        for part in WorkspaceService._MEMORY_REL:  # noqa: SLF001
            memory_dir = memory_dir / part
        memory_dir.mkdir(parents=True)
        return memory_dir

    def test_a_very_deep_memory_tree_is_listed_up_to_the_limit(self, memory: Path) -> None:
        (memory / "top.md").write_text("# top", encoding="utf-8")
        (_deep(memory, 12) / "bottom.md").write_text("# bottom", encoding="utf-8")

        tree = WorkspaceService.list_memory_tree(1)

        names = []
        stack = list(tree)
        while stack:
            node = stack.pop()
            names.append(node["name"])
            stack.extend(node["children"] or [])
        assert "top.md" in names
        assert "bottom.md" not in names

    def test_reset_memory_on_a_very_deep_tree_does_not_raise(self, memory: Path) -> None:
        (memory / "top.md").write_text("# top", encoding="utf-8")
        (_deep(memory, 12) / "bottom.md").write_text("# bottom", encoding="utf-8")

        assert WorkspaceService.reset_memory(1) == 1
        assert not (memory / "top.md").exists()

    def test_reset_memory_does_not_walk_into_links(self, memory: Path, outside: Path) -> None:
        (outside / "note.md").write_text("# host", encoding="utf-8")
        _link(memory / "to-host", outside, is_dir=True)

        WorkspaceService.reset_memory(1)

        assert (outside / "note.md").read_text(encoding="utf-8") == "# host"


class TestSkillSyncOnStartup:
    @pytest.fixture
    def workspaces(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        root = tmp_path / "workspaces"
        monkeypatch.setattr(WorkspaceService, "get_workspace_dir", staticmethod(lambda workspace_id: root / str(workspace_id)))
        (root / "1").mkdir(parents=True)
        return root

    def test_a_tree_too_big_to_delete_is_moved_out_of_the_workspace_with_a_warning(
        self, workspaces: Path, monkeypatch: pytest.MonkeyPatch, logs: List[str]
    ) -> None:
        """删不完的挪进工作区之外的回收目录（容器写不到），不留在 `/workspace` 里"""
        ws = workspaces / "1"
        big = ws / ".claude_home" / "skills" / "old-skill"
        big.mkdir(parents=True)
        for index in range(6):
            (big / f"f{index}").write_text("x", encoding="utf-8")
        monkeypatch.setattr(safe_fs, "MAX_REMOVE_ENTRIES", 3)

        with WorkspaceService._open_trash_dir(1, create=True) as trash, safe_fs.open_dir(  # noqa: SLF001
            ws, (".claude_home", "skills")
        ) as skills:
            WorkspaceService._discard_tree(skills, "old-skill", trash)  # noqa: SLF001

        assert not big.exists()
        assert [p.name.startswith(".nk-trash-") for p in (workspaces / ".nk-trash" / "1").iterdir()] == [True]
        assert not [p for p in ws.iterdir() if p.name.startswith(".nk-trash")]
        assert any("回收目录" in message for message in logs)

    def test_purge_never_touches_trash_named_entries_the_container_made(self, workspaces: Path) -> None:
        """容器能在工作区里起 `.nk-trash-*` 这样的名字；清理只看 NA 自己的回收目录"""
        ws = workspaces / "1"
        (ws / ".nk-trash-planted").mkdir()
        (ws / ".nk-trash-planted" / "keep.txt").write_text("container", encoding="utf-8")
        (workspaces / ".nk-trash" / "1" / ".nk-trash-0123" / "deep").mkdir(parents=True)

        with WorkspaceService._open_trash_dir(1, create=False) as trash:  # noqa: SLF001
            assert trash.purge_trash() == 1

        assert (ws / ".nk-trash-planted" / "keep.txt").read_text(encoding="utf-8") == "container"
        assert list((workspaces / ".nk-trash" / "1").iterdir()) == []

    def test_without_a_trash_dir_the_error_still_surfaces(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """不在启动路径上（没给回收位置）：删不完照旧抛错，调用方自己决定"""
        big = tmp_path / "skills" / "old-skill"
        big.mkdir(parents=True)
        for index in range(6):
            (big / f"f{index}").write_text("x", encoding="utf-8")
        monkeypatch.setattr(safe_fs, "MAX_REMOVE_ENTRIES", 3)

        with safe_fs.open_dir(tmp_path, ("skills",)) as skills, pytest.raises(safe_fs.UnsafePathError):
            WorkspaceService._discard_tree(skills, "old-skill", None)  # noqa: SLF001
