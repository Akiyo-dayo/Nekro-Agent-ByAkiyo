"""CI 工作流与安装脚本的静态契约（round-27 复核 P2-8 / P2-9）

两件事都是「改错了没有任何运行时信号」的类型，只能用静态用例钉住。

## backend-tests.yml

- PG 镜像必须与生产 compose 同一个主版本（生产是 14）；
- 测试步骤不能以 pytest 的退出码判定：pytest 打印摘要、写完 junit 之后进程偶尔不退出，
  会把步骤拖到超时，任务红在「超时」而不是测试结果上。通过与否由单独一步按 junit 判定；
- 这一步的判定脚本不只做语法检查，而是真的用合成的 junit 文件跑一遍（通过 / 有失败 / 有错误 / 缺报告 / 0 用例）。

## docker/install.sh

compose 文件里的嵌套默认值（`${NEKRO_SELF_HOSTNAME:-${CONTAINER_NAME_PREFIX}...}`）在 V1 的
`docker-compose` 下不成立，所以脚本必须只认 Compose v2（`docker compose`）：不再优先用 V1，
apt 备用安装不再装 V1 的 `docker-compose` 包。
"""

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "backend-tests.yml"
INSTALL_SH = REPO_ROOT / "docker" / "install.sh"
PROD_COMPOSE = REPO_ROOT / "docker" / "docker-compose.yml"


def _job() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["backend-tests"]


def _step(name_fragment: str) -> dict:
    matches = [step for step in _job()["steps"] if name_fragment in (step.get("name") or "")]
    assert len(matches) == 1, f"期望恰好一个名字含「{name_fragment}」的步骤，实际 {len(matches)}"
    return matches[0]


def _embedded_python(run_script: str) -> str:
    return run_script.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]


def _write_judge_inputs(
    directory: Path,
    *,
    suite_attrs: str,
    exit_code: "str | None",
    killed: bool = False,
    session_status: "str | None" = None,
) -> None:
    """布置判定步骤读的四个文件：junit、shell 退出码、强杀标记、pytest 自己落盘的会话状态"""
    (directory / "pytest-report.xml").write_text(
        f'<?xml version="1.0"?><testsuites><testsuite name="pytest" {suite_attrs}></testsuite></testsuites>',
        encoding="utf-8",
    )
    if exit_code is not None:
        (directory / "pytest-exit-code.txt").write_text(exit_code, encoding="utf-8")
    if killed:
        (directory / "pytest-killed.txt").write_text("killed-after-report", encoding="utf-8")
    if session_status is not None:
        (directory / "pytest-session-status.txt").write_text(session_status, encoding="utf-8")


def _judge(directory: Path) -> "subprocess.CompletedProcess[str]":
    body = _embedded_python(_step("junit")["run"])
    return subprocess.run([sys.executable, "-c", body], cwd=directory, capture_output=True, text=True)


class TestWorkflowUsesTheProductionPostgresMajor:
    def test_image_is_postgres_14_like_the_production_compose(self) -> None:
        image = _job()["services"]["postgres"]["image"]
        services = yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))["services"]
        images = [svc["image"] for svc in services.values() if str(svc.get("image", "")).startswith("postgres:")]
        assert len(images) == 1, f"生产 compose 里应当恰好一个 postgres 服务: {images}"
        prod = images[0]
        assert image == "postgres:14"
        assert image.split(":")[1].split(".")[0] == prod.split(":")[1].split(".")[0], "CI 的 PG 主版本与生产 compose 不一致"

    def test_job_name_does_not_claim_another_version(self) -> None:
        assert "PostgreSQL 14" in _job()["name"] and "16" not in _job()["name"]


class TestTestStepDoesNotHangOnAProcessThatWillNotExit:
    def test_the_run_step_never_fails_on_pytest_exit_code_or_waits_forever(self) -> None:
        script = _step("Run Backend Tests")["run"]
        assert "&" in script and "kill" in script, "pytest 必须后台跑、报告写出后对不退出的进程强杀"
        assert "pytest-report.xml" in script
        assert script.rstrip().endswith("exit 0"), "这一步不能以 pytest 退出码判定，通过与否看 junit"

    def test_a_judging_step_exists_and_runs_after_it(self) -> None:
        names = [step.get("name") or "" for step in _job()["steps"]]
        run_index = next(i for i, n in enumerate(names) if "Run Backend Tests" in n)
        judge_index = next(i for i, n in enumerate(names) if "junit" in n)
        assert judge_index > run_index

    @pytest.mark.parametrize(
        ("suite_attrs", "expected_ok"),
        [
            ('tests="10" failures="0" errors="0" skipped="1"', True),
            ('tests="10" failures="1" errors="0"', False),
            ('tests="10" failures="0" errors="2"', False),
            ('tests="0" failures="0" errors="0"', False),
        ],
    )
    def test_judging_script_verdicts(self, tmp_path: Path, suite_attrs: str, expected_ok: bool) -> None:
        """junit 判定：pytest 自己退出 0 的前提下，failures / errors / 零用例各自的结论"""
        _write_judge_inputs(tmp_path, suite_attrs=suite_attrs, exit_code="0")
        assert (_judge(tmp_path).returncode == 0) is expected_ok

    def test_judging_script_treats_a_missing_report_as_failure(self, tmp_path: Path) -> None:
        (tmp_path / "pytest-exit-code.txt").write_text("0", encoding="utf-8")
        assert _judge(tmp_path).returncode != 0

    @pytest.mark.parametrize(
        ("exit_code", "killed", "session_status", "expected_ok"),
        [
            # pytest 自己退出：只放行 0
            ("0", False, None, True),
            ("0", False, "0", True),
            ("1", False, None, False),  # 有测试失败
            ("2", False, "2", False),  # Interrupted：报告里可以一个 failure 都没有，但不能算过
            ("3", False, None, False),  # 内部错误
            ("5", False, None, False),  # 没有收集到用例
            ("137", False, None, False),  # 被超时强杀、报告碰巧存在
            (None, False, None, False),  # 退出码没落盘（步骤本身崩了）
            ("", False, None, False),
            # 写完报告后被我们强杀：shell 拿到的码是杀出来的（137），以 pytest 自己落盘的会话状态为准
            ("137", True, "0", True),
            ("143", True, "0", True),
            ("137", True, "2", False),  # 被中断的会话写完报告后挂住被杀：不能放行
            ("137", True, "1", False),
            ("137", True, None, False),  # 没有会话状态：无从证明会话正常收尾
            ("0", True, None, False),
        ],
    )
    def test_judging_script_exit_state_matrix(
        self,
        tmp_path: Path,
        exit_code: "str | None",
        killed: bool,
        session_status: "str | None",
        expected_ok: bool,
    ) -> None:
        """junit 本身是干净的（有用例、0 失败 0 错误），结论只由退出状态决定"""
        _write_judge_inputs(
            tmp_path,
            suite_attrs='tests="10" failures="0" errors="0"',
            exit_code=exit_code,
            killed=killed,
            session_status=session_status,
        )
        result = _judge(tmp_path)
        assert (result.returncode == 0) is expected_ok, result.stdout + result.stderr

    def test_a_failing_junit_still_fails_even_when_the_exit_state_is_clean(self, tmp_path: Path) -> None:
        _write_judge_inputs(
            tmp_path,
            suite_attrs='tests="10" failures="1" errors="0"',
            exit_code="137",
            killed=True,
            session_status="0",
        )
        assert _judge(tmp_path).returncode != 0

    def test_run_step_persists_the_exit_code_and_marks_only_a_kill_after_the_report(self) -> None:
        script = _step("Run Backend Tests")["run"]
        assert 'echo "$code" > pytest-exit-code.txt' in script
        assert script.count("pytest-killed.txt") == 2, "一次清理 + 恰好一处写标记"
        marker_write = script.index('> pytest-killed.txt')
        report_branch = script.index("写完报告后 60 秒仍未退出")
        deadline_branch = script.index("25 分钟内没有产出 junit 报告")
        assert report_branch < marker_write < deadline_branch, "标记只能写在「报告已写完后强杀」那个分支里"
        env = _step("Run Backend Tests")["env"]
        assert env["NA_PYTEST_SESSION_STATUS_FILE"] == "pytest-session-status.txt"

    def test_conftest_writes_pytest_own_exit_status(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("na_tests_conftest_probe", Path(__file__).parent / "conftest.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        target = tmp_path / "status.txt"

        monkeypatch.setenv(module.SESSION_STATUS_FILE_ENV, str(target))
        module.pytest_sessionfinish(None, pytest.ExitCode.INTERRUPTED)
        assert target.read_text(encoding="utf-8") == "2"
        module.pytest_sessionfinish(None, pytest.ExitCode.OK)
        assert target.read_text(encoding="utf-8") == "0"

        monkeypatch.delenv(module.SESSION_STATUS_FILE_ENV)
        target.unlink()
        module.pytest_sessionfinish(None, pytest.ExitCode.OK)
        assert not target.exists(), "没设环境变量时不该写任何文件"

    def test_the_skip_guard_step_is_still_there(self) -> None:
        assert _step("Assert PostgreSQL tests were not skipped") is not None


class TestInstallScriptOnlyAcceptsComposeV2:
    @pytest.fixture
    def source(self) -> str:
        return INSTALL_SH.read_text(encoding="utf-8")

    def test_detection_does_not_prefer_or_assign_compose_v1(self, source: str) -> None:
        assert not re.search(r"DOCKER_COMPOSE_CMD=docker-compose\b(?!-)", source), "不得把 V1 的 docker-compose 当作命令"
        assert not re.search(r'DOCKER_COMPOSE_CMD="docker-compose"', source)
        # 探测只认 `docker compose version`
        assert "docker compose version" in source

    def test_apt_fallback_does_not_install_the_v1_package(self, source: str) -> None:
        installs: List[str] = re.findall(r"apt-get install -y ([^\n;&|]+)", source)
        assert installs, "没找到 apt 安装行"
        for line in installs:
            packages = line.replace('"$pkg"', "PKG").split()
            assert "docker-compose" not in packages, f"apt 备用路径装了 V1 的 docker-compose: {line}"

    def test_a_final_guard_requires_compose_v2_before_going_on(self, source: str) -> None:
        guard = source.index("兜底：不论走的是哪条安装路径")
        assert "exit 1" in source[guard : guard + 400]
        assert guard < source.index('mkdir -p "$NEKRO_DATA_DIR"'), "兜底必须在动数据目录之前"

    def test_the_script_is_still_valid_bash(self) -> None:
        result = subprocess.run([_bash(), "-n", str(INSTALL_SH)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def _bash() -> str:
    import shutil

    bash = shutil.which("bash")
    git = shutil.which("git")
    if bash is None and git is not None:
        # Windows 上 bash 不在 PATH 时回落到 Git for Windows 自带的那份（`<git>/cmd/git.exe` → `<git>/usr/bin/bash.exe`）
        candidate = Path(git).resolve().parent.parent / "usr" / "bin" / "bash.exe"
        bash = str(candidate) if candidate.is_file() else None
    if bash is None:
        pytest.skip("没有 bash")
    probe = subprocess.run([bash, "-c", "true"], capture_output=True)
    if probe.returncode != 0:
        pytest.skip("bash 不可用（如未装发行版的 WSL 启动器）")
    return bash


def _compose_probe_function() -> str:
    """从 install.sh 里原样抠出 `compose_v2_command` 函数体，在假的 docker / docker-compose 下真跑"""
    source = INSTALL_SH.read_text(encoding="utf-8")
    match = re.search(r"^compose_v2_command\(\) \{\n.*?^\}\n", source, re.S | re.M)
    assert match, "install.sh 里没有 compose_v2_command 函数"
    return match.group(0)


class TestComposeV2DetectionRunsAgainstFakeBinaries:
    """独立二进制 `docker-compose` v2 也是 Compose v2（round-27 第二轮复核 P2-3）

    只认 `docker compose` 时，用 `docker-compose` v2 独立二进制的机器会被误判成 V1、默认进入安装，
    apt 机器还会走「卸载 docker-compose」那条分支。判定改成：`docker compose version` 成功，
    或 `docker-compose version --short` 的主版本 >= 2。这里用假二进制把每个分支真跑一遍。
    """

    @staticmethod
    def _detect(tmp_path: Path, *, plugin_ok: bool, standalone: "str | None") -> "tuple[int, str]":
        bindir = tmp_path / "bin"
        bindir.mkdir()
        docker = bindir / "docker"
        docker.write_text(
            "#!/bin/sh\n"
            + ('[ "$1" = compose ] && [ "$2" = version ] && exit 0\n' if plugin_ok else "")
            + "exit 1\n",
            encoding="utf-8",
            newline="\n",
        )
        compose = bindir / "docker-compose"
        if standalone is None:
            compose.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8", newline="\n")
        else:
            compose.write_text(
                f'#!/bin/sh\n[ "$1" = version ] && [ "$2" = --short ] && {{ echo "{standalone}"; exit 0; }}\nexit 1\n',
                encoding="utf-8",
                newline="\n",
            )
        for fake in (docker, compose):
            fake.chmod(0o755)
        script = _compose_probe_function() + "\ncompose_v2_command\n"
        # PATH 只给假二进制与 bash 自己所在目录：机器上真有的 docker / docker-compose 不能影响判定
        env = {"PATH": f"{bindir}{os.pathsep}{Path(_bash()).parent}"}
        result = subprocess.run([_bash(), "-c", script], capture_output=True, text=True, env=env)
        return result.returncode, result.stdout.strip()

    def test_the_compose_plugin_is_accepted(self, tmp_path: Path) -> None:
        assert self._detect(tmp_path, plugin_ok=True, standalone=None) == (0, "docker compose")

    def test_the_plugin_is_preferred_over_a_standalone_binary(self, tmp_path: Path) -> None:
        assert self._detect(tmp_path, plugin_ok=True, standalone="1.29.2") == (0, "docker compose")

    @pytest.mark.parametrize("version", ["2.29.1", "v2.5.0", "2", "10.0.0"])
    def test_standalone_v2_is_accepted(self, tmp_path: Path, version: str) -> None:
        assert self._detect(tmp_path, plugin_ok=False, standalone=version) == (0, "docker-compose")

    @pytest.mark.parametrize("version", ["1.29.2", "v1.27.4", "0.9", "", "unknown", "v"])
    def test_standalone_v1_or_garbage_is_not(self, tmp_path: Path, version: str) -> None:
        code, out = self._detect(tmp_path, plugin_ok=False, standalone=version)
        assert code != 0 and out == ""

    def test_nothing_installed_is_not(self, tmp_path: Path) -> None:
        code, out = self._detect(tmp_path, plugin_ok=False, standalone=None)
        assert code != 0 and out == ""

    def test_the_main_flow_uses_the_same_probe_for_detection_and_for_the_final_guard(self) -> None:
        source = INSTALL_SH.read_text(encoding="utf-8")
        assert source.count("compose_v2_command") >= 3, "定义 + 开头探测 + 兜底判定都要走同一个函数"
        # 兜底判定不能再写死 `docker compose version`：那会把独立 v2 在最后一关又拒掉
        guard = source[source.index("兜底：不论走的是哪条安装路径") :]
        guard = guard[: guard.index("设置应用目录")]
        assert "compose_v2_command" in guard and "exit 1" in guard
        assert "! docker compose version" not in guard


def _open_permissions_function() -> str:
    source = INSTALL_SH.read_text(encoding="utf-8")
    match = re.search(r"^open_data_dir_permissions\(\) \{\n.*?^\}\n", source, re.S | re.M)
    assert match, "install.sh 里没有 open_data_dir_permissions 函数"
    return match.group(0)


class TestInstallScriptDoesNotOpenUpTheHandwrittenKeyBackup:
    """`chmod -R 777` 会把含明文 API Key 的手填备份文件（应用创建时是仅属主可读写）改成全员可读
    （round-27 第三轮复核 P2-5）。放开目录权限的动作排除该文件。"""

    @staticmethod
    def _run(tmp_path: Path) -> List[str]:
        """用假的 sudo / chmod（只记录参数）与真的 find 跑一遍，返回被 chmod 点到的路径"""
        data = tmp_path / "data"
        (data / "configs").mkdir(parents=True)
        (data / "uploads").mkdir()
        (data / "configs" / "nekro-agent.yaml").write_text("x", encoding="utf-8")
        (data / "configs" / "nekro-agent.model-groups-handwritten-keys.20250101T000000000000Z.yaml").write_text(
            "secret",
            encoding="utf-8",
        )
        (data / "uploads" / "a.png").write_text("x", encoding="utf-8")

        bindir = tmp_path / "bin"
        bindir.mkdir()
        log = tmp_path / "chmod.log"
        (bindir / "sudo").write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8", newline="\n")
        (bindir / "chmod").write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{log.as_posix()}"\n', encoding="utf-8", newline="\n")
        for fake in (bindir / "sudo", bindir / "chmod"):
            fake.chmod(0o755)

        script = _open_permissions_function() + f'\nopen_data_dir_permissions "{data.as_posix()}"\n'
        env = {"PATH": f"{bindir}{os.pathsep}{Path(_bash()).parent}"}
        result = subprocess.run([_bash(), "-c", script], capture_output=True, text=True, env=env)
        assert result.returncode == 0, result.stderr
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def test_the_backup_is_never_handed_to_chmod_but_everything_else_is(self, tmp_path: Path) -> None:
        touched = self._run(tmp_path)

        names = [Path(item.replace("\\", "/")).name for item in touched]
        assert "777" in names or touched[0] == "777"
        assert not any("handwritten-keys" in item for item in touched), f"备份被放开了权限: {touched}"
        assert "nekro-agent.yaml" in names and "a.png" in names, f"其他文件应照常放开: {touched}"

    def test_the_excluded_name_is_the_one_the_application_writes(self) -> None:
        """脚本里排除的文件名前缀与应用写备份用的是同一个（改一边忘了另一边，排除就悄悄失效）"""
        from nekro_agent.services.model_group_service import HANDWRITTEN_KEYS_BACKUP_PREFIX

        assert HANDWRITTEN_KEYS_BACKUP_PREFIX in _open_permissions_function()

    def test_the_main_flow_uses_the_function_and_no_bare_recursive_777_is_left(self) -> None:
        source = INSTALL_SH.read_text(encoding="utf-8")
        assert 'open_data_dir_permissions "$NEKRO_DATA_DIR"' in source
        assert not re.search(r"chmod -R 777", source), "仍有无差别的 chmod -R 777"
        assert source.index('mkdir -p "$NEKRO_DATA_DIR"') < source.index('open_data_dir_permissions "$NEKRO_DATA_DIR"')