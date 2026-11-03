"""安装脚本取 compose 文件的来源

三份安装脚本（`install.sh` / `install_i18n.sh` / `wrtinstall.sh`）原先**无条件**从上游 main 下载
compose 文件覆盖本地。上游那份没有 `NEKRO_JWT_SECRET_KEY` / `NEKRO_JWT_REFRESH_SECRET_KEY` /
`NEKRO_SELF_HOSTNAME` 的透传（compose 的 `environment:` 是显式白名单、服务也没有 `env_file`），
于是脚本刚往 `.env` 里写好的随机 JWT 密钥进不了容器，Docker 部署照旧每次重启现场随机。

`BASE_URLS` 指向哪里取决于版本：私有版的仓库不能匿名下载，**不能**指向 fork，只能指向上游；
公开版（导出时把 raw 地址改写成公开仓库）指向公开仓库自己，且不带上游的 ep.nekro.ai 镜像站。改成：

1. 脚本所在目录或运行脚本时的当前目录里已有 **fork 的** compose 文件（判据：含三个透传键）时，优先用本地文件、
   **不下载**；脚本所在目录先于当前目录；
2. 否则从 `BASE_URLS` 下载，并在下载到的文件缺少透传键时明确告警；
3. 私有版的 `BASE_URLS` 保持指向上游、不出现 fork 的名字；公开版的指向公开仓库。

用假 `curl` / `wget`（只记录 URL、写一份不含透传的「上游」文件）与真 bash 跑，不联网。
"""

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).parent.parent
DOCKER_DIR = REPO_ROOT / "docker"
INSTALL_SCRIPTS = ["install.sh", "install_i18n.sh", "wrtinstall.sh"]
I18N_SCRIPTS = ["install_i18n.sh", "wrtinstall.sh"]

# 私有版的镜像包名带 `-private`；公开树导出时这个字面量被机械改写成公开包名（不带 `-private`）。据此区分两棵树。
_EDITION_PROBE_IMAGE = "ghcr.io/akiyo-dayo/nekro-agent-byakiyo"
PRIVATE_EDITION = _EDITION_PROBE_IMAGE.endswith("-private")

PASSTHROUGH_KEYS = ("NEKRO_JWT_SECRET_KEY", "NEKRO_JWT_REFRESH_SECRET_KEY", "NEKRO_SELF_HOSTNAME")
UPSTREAM_COMPOSE = "services:\n  nekro_agent:\n    image: upstream\n    environment:\n      - NEKRO_ADMIN_PASSWORD=${NEKRO_ADMIN_PASSWORD:-}\n"
FORK_COMPOSE = (DOCKER_DIR / "docker-compose.yml").read_text(encoding="utf-8")
FORK_NAPCAT_COMPOSE = (DOCKER_DIR / "docker-compose-x-napcat.yml").read_text(encoding="utf-8")

FUNCTIONS = ("get_remote_file", "compose_has_fork_passthrough", "find_local_compose_file", "obtain_compose_file")


def _bash() -> str:
    bash = shutil.which("bash")
    git = shutil.which("git")
    if bash is None and git is not None:
        candidate = Path(git).resolve().parent.parent / "usr" / "bin" / "bash.exe"
        bash = str(candidate) if candidate.is_file() else None
    if bash is None:
        pytest.skip("没有 bash")
    if subprocess.run([bash, "-c", "true"], capture_output=True).returncode != 0:
        pytest.skip("bash 不可用")
    return bash


def _source(script: str) -> str:
    return (DOCKER_DIR / script).read_text(encoding="utf-8")


def _function_source(script: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", _source(script), re.S | re.M)
    assert match, f"{script} 里没有 {name} 函数"
    return match.group(0)


def _base_urls_literal(script: str) -> str:
    """BASE_URLS 的字面量（install.sh / install_i18n.sh 是数组，wrtinstall.sh 是一行字符串）"""
    match = re.search(r"^BASE_URLS=(\(.*?^\)|\".*?\")$", _source(script), re.S | re.M)
    assert match, f"{script} 里找不到 BASE_URLS"
    return match.group(0)


class Run:
    def __init__(self, returncode: int, stdout: str, stderr: str, downloads: List[str], data_dir: Path) -> None:
        self.returncode = returncode
        self.output = stdout + stderr
        self.downloads = downloads
        self.data_dir = data_dir

    @property
    def compose(self) -> str:
        return (self.data_dir / "docker-compose.yml").read_text(encoding="utf-8")


def _run(
    script: str,
    tmp_path: Path,
    *,
    script_dir_file: Optional[Tuple[str, str]] = None,
    start_dir_file: Optional[Tuple[str, str]] = None,
    start_in_data_dir: bool = False,
    compose_name: str = "docker-compose.yml",
    download_fails: bool = False,
    downloaded_text: str = UPSTREAM_COMPOSE,
) -> Run:
    """在 `data/` 里跑 `obtain_compose_file <compose_name> docker-compose.yml`，模拟主流程 `cd` 之后的状态"""
    data_dir = tmp_path / "data"
    script_dir = tmp_path / "scriptdir"
    start_dir = data_dir if start_in_data_dir else tmp_path / "startdir"
    for directory in (data_dir, script_dir, start_dir):
        directory.mkdir(exist_ok=True)
    if script_dir_file:
        (script_dir / script_dir_file[0]).write_text(script_dir_file[1], encoding="utf-8", newline="\n")
    if start_dir_file:
        (start_dir / start_dir_file[0]).write_text(start_dir_file[1], encoding="utf-8", newline="\n")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "downloads.log"
    payload = tmp_path / "payload.txt"
    payload.write_text(downloaded_text, encoding="utf-8", newline="\n")
    fail = "exit 1" if download_fails else ""
    fake = (
        "#!/bin/sh\n"
        'out=""; url=""\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    -o|-O) out=$2; shift 2 ;;\n'
        '    http*) url=$1; shift ;;\n'
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        f'echo "$url" >> "{log.as_posix()}"\n'
        f"{fail}\n"
        f'cat "{payload.as_posix()}" > "$out"\n'
    )
    for name in ("curl", "wget"):
        (bindir / name).write_text(fake, encoding="utf-8", newline="\n")
        (bindir / name).chmod(0o755)

    base_urls = (
        'BASE_URLS="https://raw.example.invalid/up/docker"'
        if script == "wrtinstall.sh"
        else 'BASE_URLS=("https://raw.example.invalid/up/docker")'
    )
    harness = "\n".join(
        [
            f'NEKRO_SCRIPT_DIR="{script_dir.as_posix()}"',
            f'NEKRO_START_DIR="{start_dir.as_posix()}"',
            base_urls,
            # i18n 脚本的告警 / 提示走 `t <键>`：这里让它原样回显键，测试断言键；真文案由静态用例核对
            *(['t() { echo "$1"; }'] if script in I18N_SCRIPTS else []),
            *(_function_source(script, name) for name in FUNCTIONS),
            f'cd "{data_dir.as_posix()}"',
            f'obtain_compose_file "{compose_name}" docker-compose.yml',
        ]
    )
    env = {"PATH": f"{bindir}{os.pathsep}{Path(_bash()).parent}"}
    result = subprocess.run([_bash(), "-c", harness], capture_output=True, text=True, env=env)
    downloads = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return Run(result.returncode, result.stdout, result.stderr, downloads, data_dir)


def _warned_upstream_lacks_passthrough(script: str, output: str) -> bool:
    """i18n 脚本回显的是键（见 `_run` 的 `t` 桩），install.sh 直接是中文文案"""
    if script in I18N_SCRIPTS:
        return "warn_upstream_compose" in output
    return "NEKRO_JWT_SECRET_KEY" in output and "上游" in output


@pytest.mark.parametrize("script", INSTALL_SCRIPTS)
class TestPreferTheLocalForkCompose:
    def test_a_fork_compose_next_to_the_script_is_used_and_nothing_is_downloaded(self, script: str, tmp_path: Path) -> None:
        run = _run(script, tmp_path, script_dir_file=("docker-compose.yml", FORK_COMPOSE))

        assert run.returncode == 0, run.output
        assert run.downloads == [], f"本地已有 fork 的 compose，却还去下载了: {run.downloads}"
        assert run.compose == FORK_COMPOSE
        assert not _warned_upstream_lacks_passthrough(script, run.output), run.output

    def test_a_fork_compose_in_the_current_directory_is_used_too(self, script: str, tmp_path: Path) -> None:
        run = _run(script, tmp_path, start_dir_file=("docker-compose.yml", FORK_COMPOSE))

        assert run.returncode == 0, run.output
        assert run.downloads == []
        assert run.compose == FORK_COMPOSE

    def test_the_script_directory_wins_over_the_current_directory(self, script: str, tmp_path: Path) -> None:
        marked_other = FORK_COMPOSE + "\n# 当前目录那份\n"
        run = _run(
            script,
            tmp_path,
            script_dir_file=("docker-compose.yml", FORK_COMPOSE),
            start_dir_file=("docker-compose.yml", marked_other),
        )

        assert run.returncode == 0, run.output
        assert run.compose == FORK_COMPOSE

    def test_a_local_compose_without_the_passthrough_is_not_taken_for_the_forks(self, script: str, tmp_path: Path) -> None:
        """上次安装留下的上游 compose（或任何不含透传的文件）不是 fork 的：不能因为「本地有文件」就跳过下载"""
        run = _run(script, tmp_path, start_dir_file=("docker-compose.yml", UPSTREAM_COMPOSE))

        assert run.returncode == 0, run.output
        assert len(run.downloads) == 1, run.downloads
        assert _warned_upstream_lacks_passthrough(script, run.output), run.output

    def test_a_partial_passthrough_is_not_enough(self, script: str, tmp_path: Path) -> None:
        for missing in PASSTHROUGH_KEYS:
            run_dir = tmp_path / missing
            run_dir.mkdir()
            partial = "\n".join(line for line in FORK_COMPOSE.splitlines() if missing not in line) + "\n"
            assert missing not in partial
            run = _run(script, run_dir, script_dir_file=("docker-compose.yml", partial))

            assert run.returncode == 0, run.output
            assert len(run.downloads) == 1, f"缺 {missing} 的文件被当成了 fork 的: {run.downloads}"

    def test_the_napcat_variant_is_picked_by_its_own_file_name(self, script: str, tmp_path: Path) -> None:
        run = _run(
            script,
            tmp_path,
            script_dir_file=("docker-compose-x-napcat.yml", FORK_NAPCAT_COMPOSE),
            compose_name="docker-compose-x-napcat.yml",
        )

        assert run.returncode == 0, run.output
        assert run.downloads == []
        assert run.compose == FORK_NAPCAT_COMPOSE

    def test_rerunning_inside_the_data_dir_with_the_same_file_is_not_an_error(self, script: str, tmp_path: Path) -> None:
        """当前目录就是数据目录、里面已是 fork 的 docker-compose.yml：源与目标是同一个文件，`cp` 会报错"""
        run = _run(script, tmp_path, start_dir_file=("docker-compose.yml", FORK_COMPOSE), start_in_data_dir=True)

        assert run.returncode == 0, run.output
        assert run.downloads == []
        assert run.compose == FORK_COMPOSE


@pytest.mark.parametrize("script", INSTALL_SCRIPTS)
class TestWithoutALocalForkFileTheOldBehaviourStaysAndSaysSo:
    def test_downloads_from_the_configured_base_url_and_warns_the_upstream_file_lacks_the_passthrough(
        self, script: str, tmp_path: Path
    ) -> None:
        run = _run(script, tmp_path)

        assert run.returncode == 0, run.output
        assert run.downloads == ["https://raw.example.invalid/up/docker/docker-compose.yml"], run.downloads
        assert run.compose == UPSTREAM_COMPOSE
        assert _warned_upstream_lacks_passthrough(script, run.output), run.output

    def test_an_upstream_that_does_carry_the_passthrough_gets_no_warning(self, script: str, tmp_path: Path) -> None:
        run = _run(script, tmp_path, downloaded_text=FORK_COMPOSE)

        assert run.returncode == 0, run.output
        assert len(run.downloads) == 1
        assert not _warned_upstream_lacks_passthrough(script, run.output), run.output

    def test_a_failed_download_still_fails_the_step(self, script: str, tmp_path: Path) -> None:
        run = _run(script, tmp_path, download_fails=True)

        assert run.returncode != 0, "下载失败却返回了成功：主流程会带着没有 compose 的目录继续"
        assert not (run.data_dir / "docker-compose.yml").exists() or run.compose != UPSTREAM_COMPOSE


@pytest.mark.parametrize("script", INSTALL_SCRIPTS)
class TestWiring:
    def test_base_urls_point_where_this_edition_can_be_downloaded_from(self, script: str) -> None:
        literal = _base_urls_literal(script)

        if PRIVATE_EDITION:  # 私有仓库匿名下载不到：只能指向上游
            assert "KroMiose/nekro-agent" in literal
            for fork_marker in ("Akiyo", "akiyo", "ByAkiyo"):
                assert fork_marker not in literal, "BASE_URLS 指向了 fork：脚本匿名下载不到，安装会直接失败"
        else:  # 公开版：取自公开仓库 main 分支，不再经过上游的镜像站
            assert "raw.githubusercontent.com/Akiyo-dayo/Nekro-Agent-ByAkiyo/main/docker" in literal
            assert "KroMiose" not in literal and "ep.nekro.ai" not in literal

    def test_the_main_flow_goes_through_obtain_compose_file_only(self, script: str) -> None:
        source = _source(script)
        # 函数定义之外，主流程里不再有直接 `get_remote_file "$compose_file"`
        main_flow = source.replace(_function_source(script, "obtain_compose_file"), "")
        assert not re.search(r'get_remote_file\s+"\$compose_file"', main_flow), "主流程仍在无条件下载覆盖"
        calls = re.findall(r'obtain_compose_file\s+"\$compose_file"\s+docker-compose\.yml', main_flow)
        assert len(calls) == 1, calls

    def test_the_two_locations_are_captured_before_the_script_changes_directory(self, script: str) -> None:
        source = _source(script)
        start = re.search(r"^NEKRO_START_DIR=", source, re.M)
        where = re.search(r"^NEKRO_SCRIPT_DIR=", source, re.M)
        assert start and where, "没有记录运行时的当前目录 / 脚本所在目录"
        first_cd = source.index('cd "$NEKRO_DATA_DIR"')
        assert start.start() < first_cd and where.start() < first_cd, "记录得比 cd 晚了：记下的是数据目录"

    def test_the_script_directory_is_resolved_from_where_the_script_file_lives(self, script: str, tmp_path: Path) -> None:
        source = _source(script)
        lines = re.findall(r"^NEKRO_(?:START|SCRIPT)_DIR=.*$", source, re.M)
        assert len(lines) == 2, lines

        script_home = tmp_path / "elsewhere"
        script_home.mkdir()
        probe = script_home / "probe.sh"
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        # 期望值也在 bash 里求（Windows 上 bash 的 `/c/...` 与 Python 的 `C:\...` 写法不同，不能跨语言比字符串）
        probe.write_text(
            "\n".join(
                [
                    *lines,
                    'echo "S=$NEKRO_SCRIPT_DIR"',
                    'echo "C=$NEKRO_START_DIR"',
                    f'echo "ES=$(cd "{script_home.as_posix()}" && pwd)"',
                    f'echo "EC=$(cd "{cwd.as_posix()}" && pwd)"',
                ]
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        env = {"PATH": str(Path(_bash()).parent)}
        result = subprocess.run([_bash(), probe.as_posix()], capture_output=True, text=True, env=env, cwd=cwd)

        assert result.returncode == 0, result.stderr
        values = dict(line.split("=", 1) for line in result.stdout.splitlines())
        assert values["S"] == values["ES"], values
        assert values["C"] == values["EC"], values

    def test_the_marker_keys_are_exactly_what_the_forks_compose_files_carry(self, script: str) -> None:
        """判据里认的键与 compose 里真有的键一致（改一边忘了另一边，识别就悄悄失效）"""
        body = _function_source(script, "compose_has_fork_passthrough")
        for key in PASSTHROUGH_KEYS:
            assert key in body
            assert key in FORK_COMPOSE and key in FORK_NAPCAT_COMPOSE


@pytest.mark.parametrize("script", I18N_SCRIPTS)
def test_the_warning_is_translated_in_both_languages_and_names_the_missing_variables(script: str) -> None:
    source = _source(script)
    bodies = re.findall(r'"warn_upstream_compose"\) echo "(.*?)" ;;', source)
    assert len(bodies) == 2, f"{script}: 告警应有中英两份文案，实际 {len(bodies)} 份"
    for body in bodies:
        assert "NEKRO_JWT_SECRET_KEY" in body and "NEKRO_SELF_HOSTNAME" in body, body
    assert any(re.search(r"[一-鿿]", body) for body in bodies) and any(
        not re.search(r"[一-鿿]", body) for body in bodies
    )


def test_install_sh_warning_names_the_missing_variables() -> None:
    body = _function_source("install.sh", "obtain_compose_file")
    assert "NEKRO_JWT_SECRET_KEY" in body and "NEKRO_SELF_HOSTNAME" in body and "上游" in body
