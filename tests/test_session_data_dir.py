"""测试会话的数据目录在仓库之外，用例不读写仓库自己的 `data/`

`tests/conftest.py` 在导入 nekro_agent 之前把 `NEKRO_DATA_DIR` 指到本会话新建的临时目录：配置文件、
系统数据、上传、日志、工作区都落在那里，用例不再读到仓库 `data/` 里以前生成的旧配置，换 worktree、
跑几遍结果都一样。会话结束时再把仓库 `data/` 和开始时的快照比一次，有新增、改动或删除就报出路径、
判这次会话失败。子进程（用例里起的 pytest、python）继承环境变量，沿用同一个目录，也不在退出时删它。

这里钉住：

- 本进程里数据目录、按它拼出的路径常量、核心配置文件都在会话的临时目录里，不在仓库下；
- 快照比对认得出新增、改动、删除，什么都没动时不报；
- 子进程 pytest 沿用同一个目录、退出后目录还在；会话结束时的检查在子进程里真的会报、会判失败
  （子进程里把被检查的目录换成一个临时目录，不往仓库里写东西）。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_CONFTEST = Path(__file__).resolve().parent / "conftest.py"
# 子进程要导入 nekro_agent，机器繁忙时一两分钟也算正常
CHILD_TIMEOUT_SECONDS = 300


def load_repo_conftest() -> ModuleType:
    spec = importlib.util.spec_from_file_location("na_repo_conftest_for_data_dir", REPO_CONFTEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def session_data_dir() -> Path:
    return Path(os.environ["NA_PYTEST_DATA_DIR"])


class TestThisSession:
    def test_the_data_dir_is_the_session_temp_dir_outside_the_repo(self) -> None:
        from nekro_agent.core.os_env import OsEnv

        assert OsEnv.DATA_DIR == os.environ["NEKRO_DATA_DIR"] == str(session_data_dir())
        assert session_data_dir().is_dir()
        assert not session_data_dir().resolve().is_relative_to(REPO_ROOT.resolve())

    def test_paths_built_from_it_and_the_core_config_file_live_there(self) -> None:
        from nekro_agent.core import os_env
        from nekro_agent.core.config import CONFIG_PATH

        root = session_data_dir().resolve()
        for path in (
            CONFIG_PATH,
            os_env.APP_SYSTEM_DIR,
            os_env.USER_UPLOAD_DIR,
            os_env.APP_LOG_DIR,
            os_env.WORKSPACE_ROOT_DIR,
        ):
            assert Path(path).resolve().is_relative_to(root), path
        assert CONFIG_PATH.is_file(), "核心配置导入时按代码里的默认值写出的那一份，就在会话目录里"


class TestRepoDataSnapshot:
    def test_new_changed_and_deleted_entries_are_reported(self, tmp_path: Path) -> None:
        conftest = load_repo_conftest()
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "nekro-agent.yaml").write_text("old: 1\n", encoding="utf-8")
        (tmp_path / "gone.txt").write_text("x", encoding="utf-8")
        before = conftest.snapshot_repo_data_dir(tmp_path)

        (tmp_path / "configs" / "nekro-agent.yaml").write_text("old: 1\nnew: 2\n", encoding="utf-8")
        (tmp_path / "gone.txt").unlink()
        (tmp_path / "system").mkdir()
        (tmp_path / "system" / "marker.json").write_text("{}", encoding="utf-8")

        assert conftest.compare_repo_data_dir(before, conftest.snapshot_repo_data_dir(tmp_path)) == [
            "新增 data/system/",
            "新增 data/system/marker.json",
            "改动 data/configs/nekro-agent.yaml",
            "删除 data/gone.txt",
        ]

    def test_nothing_touched_reports_nothing(self, tmp_path: Path) -> None:
        conftest = load_repo_conftest()
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        assert (
            conftest.compare_repo_data_dir(
                conftest.snapshot_repo_data_dir(tmp_path),
                conftest.snapshot_repo_data_dir(tmp_path),
            )
            == []
        )

    def test_a_missing_data_dir_is_an_empty_snapshot(self, tmp_path: Path) -> None:
        conftest = load_repo_conftest()
        assert conftest.snapshot_repo_data_dir(tmp_path / "no-such-dir") == {}


CHILD_TEST = """
import os
from pathlib import Path


def test_reports_its_data_dir():
    from nekro_agent.core.os_env import OsEnv

    print("CHILD_DATA_DIR=" + OsEnv.DATA_DIR)
    print("CHILD_ENV=" + os.environ["NEKRO_DATA_DIR"])
    target = os.environ.get("CHILD_WRITE_INTO")
    if target:
        Path(target).write_text("x", encoding="utf-8")
"""


def child_conftest(watched: Path) -> str:
    return f"""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("na_repo_conftest_in_child", {str(REPO_CONFTEST)!r})
_repo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_repo)

# 会话结束时的检查看这个临时目录，不看仓库里真的 data/
_repo.REPO_DATA_DIR = Path({str(watched)!r})
_repo._REPO_DATA_AT_START = _repo.snapshot_repo_data_dir()

pytest_configure = _repo.pytest_configure
pytest_sessionfinish = _repo.pytest_sessionfinish
pytest_unconfigure = _repo.pytest_unconfigure
"""


def run_child_pytest(tmp_path: Path, *, write_into: Path | None = None) -> subprocess.CompletedProcess[str]:
    project = tmp_path / "project"
    watched = tmp_path / "watched_data"
    project.mkdir()
    watched.mkdir(exist_ok=True)
    (watched / "existing.txt").write_text("keep", encoding="utf-8")
    (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (project / "conftest.py").write_text(child_conftest(watched), encoding="utf-8")
    (project / "test_child.py").write_text(CHILD_TEST, encoding="utf-8")

    env = dict(os.environ)  # 与用例里起子进程的常见写法一样：整份继承
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        env.pop(name, None)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH", "")]))
    if write_into is not None:
        env["CHILD_WRITE_INTO"] = str(write_into)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider", "--rootdir", str(project), str(project)],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CHILD_TIMEOUT_SECONDS,
        check=False,
    )


class TestChildPytest:
    def test_a_child_session_uses_the_same_data_dir_and_leaves_it_in_place(self, tmp_path: Path) -> None:
        result = run_child_pytest(tmp_path)
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert f"CHILD_DATA_DIR={session_data_dir()}" in output, output
        assert f"CHILD_ENV={session_data_dir()}" in output, output
        assert session_data_dir().is_dir(), "子进程退出时不能把上级会话的数据目录删掉"
        assert "用例读写了仓库里的 data/" not in output

    def test_a_write_into_the_watched_data_dir_is_reported_and_fails_the_session(self, tmp_path: Path) -> None:
        result = run_child_pytest(tmp_path, write_into=tmp_path / "watched_data" / "stray.json")
        output = result.stdout + result.stderr
        assert result.returncode == int(pytest.ExitCode.TESTS_FAILED), output
        assert "1 passed" in output, "用例本身是过的，失败来自会话结束时的检查"
        assert "用例读写了仓库里的 data/ 目录（1 处）" in output, output
        assert "新增 data/stray.json" in output, output
