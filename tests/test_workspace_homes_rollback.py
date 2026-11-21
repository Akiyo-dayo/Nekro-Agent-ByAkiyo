"""`scripts/workspace_homes_rollback.py`：退回旧版本前把 CC 工作区的 home 搬回工作区目录里

脚本只用标准库、不导入 NA；这里按文件路径加载它。往返用例先用 NA 自己的迁移把旧 home 挪出去，
再用脚本搬回来，核对 NA 又按旧布局认它。
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional

import pytest

from nekro_agent.services.workspace import manager as manager_module
from nekro_agent.services.workspace.manager import WorkspaceService

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "workspace_homes_rollback.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("workspace_homes_rollback", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rollback = _load()


def _link(link: Path, target: Path, *, is_dir: bool) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("这台机器上建不了符号链接")


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    (tmp_path / "workspaces").mkdir()
    return tmp_path


def _migrated(data_dir: Path, workspace_id: int, *, with_workspace_dir: bool = True) -> Path:
    """新位置上一个用过的 home"""
    home = data_dir / "workspaces" / ".nk-homes" / str(workspace_id) / "claude"
    (home / "projects").mkdir(parents=True)
    (home / "projects" / "session.jsonl").write_text(f"history-{workspace_id}", encoding="utf-8")
    if with_workspace_dir:
        (data_dir / "workspaces" / str(workspace_id) / "default").mkdir(parents=True)
    return home


def _legacy(data_dir: Path, workspace_id: int) -> Path:
    return data_dir / "workspaces" / str(workspace_id) / ".claude_home"


def _run(
    data_dir: Path,
    *extra: str,
    running: Optional[List[str]] = None,
    processes: Optional[List[str]] = None,
    monkeypatch: Any = None,
) -> int:
    if monkeypatch is not None:
        monkeypatch.setattr(rollback, "running_deployment_containers", lambda _data_dir: running)
        monkeypatch.setattr(rollback, "running_na_processes", lambda _data_dir: [] if processes is None else processes)
    return rollback.main(["--data-dir", str(data_dir), *extra])


def test_without_apply_nothing_moves(data_dir, capsys) -> None:
    home = _migrated(data_dir, 1)

    assert _run(data_dir) == 0

    assert home.is_dir()
    assert not _legacy(data_dir, 1).exists()
    assert "将搬回：工作区 1" in capsys.readouterr().out


def test_apply_moves_every_home_back(data_dir, monkeypatch) -> None:
    _migrated(data_dir, 1)
    _migrated(data_dir, 2)

    assert _run(data_dir, "--apply", running=[], monkeypatch=monkeypatch) == 0

    for workspace_id in (1, 2):
        legacy = _legacy(data_dir, workspace_id)
        assert (legacy / "projects" / "session.jsonl").read_text(encoding="utf-8") == f"history-{workspace_id}"
        assert not (data_dir / "workspaces" / ".nk-homes" / str(workspace_id)).exists()


def test_an_occupied_legacy_path_is_skipped_and_the_rest_still_move(data_dir, monkeypatch, capsys) -> None:
    home_1 = _migrated(data_dir, 1)
    _legacy(data_dir, 1).mkdir()
    _migrated(data_dir, 2)

    assert _run(data_dir, "--apply", running=[], monkeypatch=monkeypatch) == 1

    assert home_1.is_dir(), "旧位置已经有东西时不覆盖"
    assert list(_legacy(data_dir, 1).iterdir()) == []
    assert _legacy(data_dir, 2).is_dir()
    assert "不覆盖" in capsys.readouterr().out


def test_a_home_without_its_workspace_dir_is_left_alone(data_dir, monkeypatch) -> None:
    home = _migrated(data_dir, 3, with_workspace_dir=False)

    assert _run(data_dir, "--apply", running=[], monkeypatch=monkeypatch) == 1

    assert home.is_dir()
    assert not (data_dir / "workspaces" / "3").exists()


def test_a_linked_home_is_not_followed(data_dir, monkeypatch, tmp_path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (data_dir / "workspaces" / "4" / "default").mkdir(parents=True)
    _link(data_dir / "workspaces" / ".nk-homes" / "4" / "claude", elsewhere, is_dir=True)

    assert _run(data_dir, "--apply", running=[], monkeypatch=monkeypatch) == 1

    assert not os.path.lexists(_legacy(data_dir, 4))
    assert elsewhere.is_dir()


def test_running_workspace_containers_block_the_move(data_dir, monkeypatch, capsys) -> None:
    home = _migrated(data_dir, 1)

    assert _run(data_dir, "--apply", running=["nekro-cc-abc"], monkeypatch=monkeypatch) == 2

    assert home.is_dir() and not _legacy(data_dir, 1).exists()
    assert "nekro-cc-abc" in capsys.readouterr().err


def test_without_docker_it_refuses_unless_told_to_skip_the_check(data_dir, monkeypatch) -> None:
    home = _migrated(data_dir, 1)

    assert _run(data_dir, "--apply", running=None, monkeypatch=monkeypatch) == 2
    assert home.is_dir()

    assert _run(data_dir, "--apply", "--skip-docker-check", running=None, monkeypatch=monkeypatch) == 0
    assert _legacy(data_dir, 1).is_dir()


def test_a_missing_data_dir_is_a_usage_error(tmp_path) -> None:
    assert rollback.main(["--data-dir", str(tmp_path / "nope")]) == 2


def test_a_running_na_blocks_the_move(data_dir, monkeypatch, capsys) -> None:
    home = _migrated(data_dir, 1)

    assert _run(data_dir, "--apply", running=[], processes=["4242 python bot --env=prod"], monkeypatch=monkeypatch) == 2

    assert home.is_dir() and not _legacy(data_dir, 1).exists()
    assert "4242" in capsys.readouterr().err


def test_without_proc_it_refuses_unless_told_to_skip_the_process_check(data_dir, monkeypatch) -> None:
    home = _migrated(data_dir, 1)
    monkeypatch.setattr(rollback, "running_deployment_containers", lambda _data_dir: [])
    monkeypatch.setattr(rollback, "running_na_processes", lambda _data_dir: None)

    assert rollback.main(["--data-dir", str(data_dir), "--apply"]) == 2
    assert home.is_dir()

    assert rollback.main(["--data-dir", str(data_dir), "--apply", "--skip-process-check"]) == 0
    assert _legacy(data_dir, 1).is_dir()


def test_not_being_root_is_a_clear_message_not_a_traceback(data_dir, monkeypatch, capsys) -> None:
    _migrated(data_dir, 1)

    def denied(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError(13, "Permission denied", str(data_dir / "workspaces" / ".nk-homes"))

    monkeypatch.setattr(rollback, "plan", denied)

    assert _run(data_dir, monkeypatch=monkeypatch) == 2
    err = capsys.readouterr().err
    assert "root" in err and "Traceback" not in err


def test_a_linked_holder_dir_is_reported_not_silently_skipped(data_dir, monkeypatch, capsys, tmp_path) -> None:
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "claude").mkdir(parents=True)
    (data_dir / "workspaces" / "6" / "default").mkdir(parents=True)
    _link(data_dir / "workspaces" / ".nk-homes" / "6", elsewhere, is_dir=True)

    assert _run(data_dir, "--apply", running=[], monkeypatch=monkeypatch) == 1

    assert not os.path.lexists(_legacy(data_dir, 6))
    assert (elsewhere / "claude").is_dir()
    assert "工作区 6" in capsys.readouterr().out


def test_homes_moved_out_by_na_are_left_and_mentioned(data_dir, monkeypatch, capsys) -> None:
    _migrated(data_dir, 1)
    retired = data_dir / "workspaces" / ".nk-homes" / "1" / "legacy-20261114T000000Z"
    retired.mkdir()

    assert _run(data_dir, "--apply", running=[], monkeypatch=monkeypatch) == 0

    assert _legacy(data_dir, 1).is_dir()
    assert retired.is_dir()
    assert "legacy-*" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 「这套部署停干净了没有」的两项检查本身
# ---------------------------------------------------------------------------


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def test_only_containers_writing_this_data_dir_count(data_dir, monkeypatch) -> None:
    other = data_dir.parent / "other-deployment"
    containers = [
        {"Name": "/nekro_agent", "Mounts": [{"Source": str(data_dir), "RW": True}]},
        {"Name": "/nekro-cc-1", "Mounts": [{"Source": str(data_dir / "workspaces" / "1"), "RW": True}]},
        {"Name": "/nekro_napcat", "Mounts": [{"Source": str(data_dir / "uploads"), "RW": False}]},
        {"Name": "/other_na", "Mounts": [{"Source": str(other), "RW": True}]},
        {"Name": "/prefix_twin", "Mounts": [{"Source": str(data_dir) + "-backup", "RW": True}]},
    ]

    def fake_run(command: List[str], **_kwargs: Any) -> _Completed:
        if command[1] == "ps":
            return _Completed("a\nb\nc\nd\ne\n")
        return _Completed(json.dumps(containers))

    monkeypatch.setattr(rollback.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(rollback.subprocess, "run", fake_run)

    assert rollback.running_deployment_containers(data_dir) == ["nekro_agent", "nekro-cc-1"]


def test_without_docker_the_container_check_says_so(data_dir, monkeypatch) -> None:
    monkeypatch.setattr(rollback.shutil, "which", lambda _name: None)
    assert rollback.running_deployment_containers(data_dir) is None


def _process(proc: Path, pid: int, args: List[str], environ: Dict[str, str]) -> None:
    entry = proc / str(pid)
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in args) + b"\0")
    (entry / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in environ.items()) + b"\0")


def test_only_na_processes_on_this_data_dir_count(data_dir, tmp_path) -> None:
    proc = tmp_path / "proc"
    _process(proc, 3999991, ["/app/.venv/bin/python", "/app/.venv/bin/bot", "--env=prod"], {"NEKRO_DATA_DIR": str(data_dir)})
    _process(proc, 3999993, ["/usr/bin/python3", "run_bot.py"], {"NEKRO_DATA_DIR": str(tmp_path / "other")})
    _process(proc, 3999995, ["bash"], {"NEKRO_DATA_DIR": str(data_dir)})  # 管理员 export 过的 shell 不算
    (proc / "self").mkdir()

    hits = rollback.running_na_processes(data_dir, proc=proc)

    assert hits is not None and len(hits) == 1
    assert hits[0].startswith("3999991 ")


def test_without_proc_the_process_check_says_so(data_dir, tmp_path) -> None:
    assert rollback.running_na_processes(data_dir, proc=tmp_path / "no-proc") is None


def test_round_trip_with_the_na_migration(data_dir, monkeypatch) -> None:
    """NA 迁出去、脚本搬回来：NA 又按旧布局认它，内容一点不少"""
    monkeypatch.setattr(manager_module, "WORKSPACE_ROOT_DIR", str(data_dir / "workspaces"))
    legacy = _legacy(data_dir, 5)
    (legacy / "nk-ssh").mkdir(parents=True)
    (legacy / "nk-ssh" / "id_gateway").write_text("PRIVATE", encoding="utf-8")
    assert WorkspaceService.migrate_claude_home(5).legacy is False

    assert _run(data_dir, "--apply", running=[], monkeypatch=monkeypatch) == 0

    home = WorkspaceService.claude_home(5)
    assert home.legacy is True
    assert (home.path / "nk-ssh" / "id_gateway").read_text(encoding="utf-8") == "PRIVATE"


# ---------------------------------------------------------------------------
# 搬回去的 home 交给容器用户（与 NA 同一口径）
# ---------------------------------------------------------------------------


def _home_with_na_files(data_dir: Path, workspace_id: int = 1) -> Path:
    home = _migrated(data_dir, workspace_id)
    (home / "settings.json").write_text("{}", encoding="utf-8")
    (home / "skills" / "demo" / "scripts").mkdir(parents=True)
    (home / "skills" / "demo" / "SKILL.md").write_text("x", encoding="utf-8")
    (home / "skills" / "demo" / "scripts" / "run.sh").write_text("x", encoding="utf-8")
    (home / "nk-ssh").mkdir()
    (home / "nk-ssh" / "config").write_text("x", encoding="utf-8")
    (home / "nk-ssh" / "id_gateway").write_text("KEY", encoding="utf-8")
    return home


def test_a_moved_home_is_handed_to_the_container_user(data_dir, monkeypatch) -> None:
    """不依赖系统：把 `lchown` 换成记录器"""
    _home_with_na_files(data_dir)
    calls: List[Any] = []
    monkeypatch.setattr(rollback.os, "lchown", lambda path, uid, gid: calls.append((Path(path), uid, gid)), raising=False)

    assert _run(data_dir, "--apply", running=[], monkeypatch=monkeypatch) == 0

    legacy = _legacy(data_dir, 1)
    handed = {path for path, _uid, _gid in calls}
    assert {
        legacy,
        legacy / "settings.json",
        legacy / "skills",
        legacy / "skills" / "demo",
        legacy / "skills" / "demo" / "SKILL.md",
        legacy / "skills" / "demo" / "scripts",
        legacy / "skills" / "demo" / "scripts" / "run.sh",
        legacy / "nk-ssh" / "id_gateway",
    } <= handed
    assert not handed & {legacy / "nk-ssh", legacy / "nk-ssh" / "config"}, "nk-ssh 目录与配置留给 root"
    assert not handed & {legacy / "projects", legacy / "projects" / "session.jsonl"}, "会话历史不动"
    assert {(uid, gid) for _path, uid, gid in calls} == {(1000, 1000)}


def test_a_refused_chown_still_counts_as_moved(data_dir, monkeypatch, capsys) -> None:
    _home_with_na_files(data_dir)

    def refuse(*_args: Any) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(rollback.os, "lchown", refuse, raising=False)

    assert _run(data_dir, "--apply", running=[], monkeypatch=monkeypatch) == 0

    assert _legacy(data_dir, 1).is_dir()
    assert "1000:1000" in capsys.readouterr().err


@pytest.mark.skipif(not (os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0), reason="真改属主要以 root 跑")
def test_ownership_on_disk_after_moving_back(data_dir, monkeypatch, tmp_path) -> None:
    home = _home_with_na_files(data_dir)
    victim = tmp_path / "victim.txt"
    victim.write_text("host", encoding="utf-8")
    _link(home / "skills" / "demo" / "leak", victim, is_dir=False)

    assert _run(data_dir, "--apply", running=[], monkeypatch=monkeypatch) == 0

    legacy = _legacy(data_dir, 1)
    for path in (legacy, legacy / "settings.json", legacy / "skills" / "demo" / "scripts" / "run.sh", legacy / "nk-ssh" / "id_gateway"):
        st = os.lstat(path)
        assert (st.st_uid, st.st_gid) == (1000, 1000), path
    assert (os.stat(legacy).st_mode & 0o777) == 0o777
    assert os.lstat(legacy / "nk-ssh" / "config").st_uid == 0
    assert os.lstat(victim).st_uid == 0, "链接那头不碰"
