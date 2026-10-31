"""密钥文件 `credential_secret.key` 的权限（round-28 任务 1）

这个文件是模型组凭据（档 B）的**主密钥**。DECISIONS「运营模式」的硬约束是租户拿不到平台方的 Key；
主密钥全员可读，等于同机任何账户都能解开库里的全部模型组密文。应用自己建它时是 0600，
但三份安装脚本都会在建好数据目录之后「放开」它的权限（`chmod -R 777` 或等价的 `find ... chmod 777`）。

两道闸，缺一不可：

1. **安装脚本**：放开目录权限时**排除**这个文件（而不是先放开再收回——中间有一个全员可读的窗口），
   且对**已经存在**的（老版本脚本可能早已放开它）纠正回 0600。三份脚本共用同一个函数名
   `open_data_dir_permissions`，本文件对三份逐一跑。
2. **应用启动**：读到密钥文件时检查权限位，组 / 其他有任何位就打 WARNING（POSIX）。
   Windows 的 `st_mode` 恒显示 0666，按它告警是永远在误报，所以 Windows 上不检查。

脚本用假 `sudo` / `chmod`（只记录参数）+ 真 `find` 在 bash 里跑；真实权限位的断言放在 POSIX 用例里
（本机 Windows 上 skip，CI 的 Linux runner 上执行）。
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import pytest
from loguru import logger

from nekro_agent.core.os_env import OsEnv
from nekro_agent.tools import credential_cipher
from nekro_agent.tools.credential_cipher import KEY_FILE_NAME, reset_credential_cipher_cache

REPO_ROOT = Path(__file__).parent.parent
DOCKER_DIR = REPO_ROOT / "docker"
INSTALL_SCRIPTS = ["install.sh", "install_i18n.sh", "wrtinstall.sh"]


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


def _function_source(script: str, name: str) -> str:
    source = (DOCKER_DIR / script).read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", source, re.S | re.M)
    assert match, f"{script} 里没有 {name} 函数"
    return match.group(0)


# ============================================================
# 1. 安装脚本
# ============================================================


ENV_FILE_NAME = ".env"


def _run_open_permissions(
    script: str,
    tmp_path: Path,
    *,
    key_exists: bool,
    env_exists: bool = True,
    via_symlink: bool = False,
) -> List[str]:
    """用假 sudo / chmod（每次调用记一行 `模式 路径...`）与真 find 跑放开权限函数，返回 chmod 调用记录

    `via_symlink`：把数据目录做成一个**指向真目录的符号链接**再传给函数（`NEKRO_DATA_DIR` 是链接的部署）。
    """
    data = tmp_path / "data"
    (data / "configs").mkdir(parents=True)
    (data / "uploads").mkdir()
    (data / "configs" / "nekro-agent.yaml").write_text("x", encoding="utf-8")
    (data / "uploads" / "a.png").write_text("x", encoding="utf-8")
    if key_exists:
        (data / KEY_FILE_NAME).write_text("material", encoding="utf-8")
    if env_exists:
        (data / ENV_FILE_NAME).write_text("NEKRO_ADMIN_PASSWORD=x\n", encoding="utf-8")
    target = data
    if via_symlink:
        link = tmp_path / "data-link"
        try:
            os.symlink(data, link, target_is_directory=True)
        except OSError as exc:  # Windows 没有创建符号链接的权限
            pytest.skip(f"建不了符号链接: {exc}")
        target = link

    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "chmod.log"
    (bindir / "sudo").write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8", newline="\n")
    (bindir / "chmod").write_text(f'#!/bin/sh\necho "$*" >> "{log.as_posix()}"\n', encoding="utf-8", newline="\n")
    for fake in (bindir / "sudo", bindir / "chmod"):
        fake.chmod(0o755)

    script_text = _function_source(script, "open_data_dir_permissions") + f'\nopen_data_dir_permissions "{target.as_posix()}"\n'
    env = {"PATH": f"{bindir}{os.pathsep}{Path(_bash()).parent}"}
    result = subprocess.run([_bash(), "-c", script_text], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _calls_touching(calls: List[str], name: str) -> List[str]:
    return [call for call in calls if name in call.replace("\\", "/")]


def _calls_on_file(calls: List[str], basename: str) -> List[str]:
    """chmod 调用里（`-exec ... {} +` 会把多个路径并进一次调用）**任一路径**的文件名恰为 basename 的那些"""
    return [call for call in calls if any(token.replace("\\", "/").rsplit("/", 1)[-1] == basename for token in call.split()[1:])]


@pytest.mark.parametrize("script", INSTALL_SCRIPTS)
class TestInstallScriptsKeepTheKeyFileClosed:
    def test_the_key_file_is_never_opened_up_to_777(self, script: str, tmp_path: Path) -> None:
        calls = _run_open_permissions(script, tmp_path, key_exists=True)

        opening = [call for call in calls if call.split()[0] == "777"]
        assert opening, f"放开目录权限这一步根本没有执行: {calls}"
        assert not _calls_touching(opening, KEY_FILE_NAME), f"密钥文件被放开了权限: {opening}"
        # 其他文件照常放开（排除不能变成「什么都不放开」）
        assert _calls_touching(opening, "nekro-agent.yaml") and _calls_touching(opening, "a.png"), opening

    def test_an_existing_key_file_is_corrected_back_to_0600(self, script: str, tmp_path: Path) -> None:
        """老版本脚本可能已经把它放开了：这一趟要纠正，而不是只是「这次不再放开」"""
        calls = _run_open_permissions(script, tmp_path, key_exists=True)

        touching_key = _calls_touching(calls, KEY_FILE_NAME)
        assert len(touching_key) == 1, calls
        assert touching_key[0].split()[0] == "600", touching_key

    def test_no_key_file_means_no_chmod_on_it_at_all(self, script: str, tmp_path: Path) -> None:
        """全新安装时文件还不存在：不能凭空去 chmod 一个不存在的路径（那会让脚本报错退出）"""
        calls = _run_open_permissions(script, tmp_path, key_exists=False)

        assert not _calls_touching(calls, KEY_FILE_NAME)
        assert any(call.split()[0] == "777" for call in calls)

    def test_the_env_file_is_never_opened_up_to_777(self, script: str, tmp_path: Path) -> None:
        """`.env` 里是 ADMIN 口令、JWT 两把、OneBot 令牌：重跑脚本时它已经在数据目录里，不能被放成 777"""
        calls = _run_open_permissions(script, tmp_path, key_exists=True)

        opening = [call for call in calls if call.split()[0] == "777"]
        assert opening, f"放开目录权限这一步根本没有执行: {calls}"
        assert not _calls_on_file(opening, ENV_FILE_NAME), f".env 被放开了权限: {opening}"
        # 其他文件照常放开（排除不能变成「什么都不放开」）
        assert _calls_touching(opening, "nekro-agent.yaml") and _calls_touching(opening, "a.png"), opening

    def test_an_existing_env_file_is_tightened_to_0600(self, script: str, tmp_path: Path) -> None:
        """老版本脚本留下的 777 `.env` 要被纠正回 600，而不是只是「这次不再放开」"""
        calls = _run_open_permissions(script, tmp_path, key_exists=True)

        touching_env = _calls_on_file(calls, ENV_FILE_NAME)
        assert len(touching_env) == 1, calls
        assert touching_env[0].split()[0] == "600", touching_env

    def test_no_env_file_means_no_chmod_on_it_at_all(self, script: str, tmp_path: Path) -> None:
        calls = _run_open_permissions(script, tmp_path, key_exists=True, env_exists=False)

        assert not _calls_on_file(calls, ENV_FILE_NAME)

    def test_a_symlinked_data_dir_is_still_walked(self, script: str, tmp_path: Path) -> None:
        """`NEKRO_DATA_DIR` 本身是符号链接时，`find 链接` 不会进去（chmod 0 次），要写成 `find 链接/`"""
        calls = _run_open_permissions(script, tmp_path, key_exists=True, via_symlink=True)

        opening = [call for call in calls if call.split()[0] == "777"]
        assert _calls_touching(opening, "nekro-agent.yaml") and _calls_touching(opening, "a.png"), (
            f"数据目录是符号链接时一个文件都没放开: {calls}"
        )
        # 排除与纠正在链接路径下一样成立
        assert not _calls_touching(opening, KEY_FILE_NAME)
        assert [c.split()[0] for c in _calls_touching(calls, KEY_FILE_NAME)] == ["600"], calls
        assert [c.split()[0] for c in _calls_on_file(calls, ENV_FILE_NAME)] == ["600"], calls

    def test_every_find_starts_from_the_directory_with_a_trailing_slash(self, script: str) -> None:
        """符号链接那条用例在没有链接权限的机器上会 skip；这条是不依赖权限的静态兜底"""
        finds = re.findall(r"\bfind\s+(\S+)", _function_source(script, "open_data_dir_permissions"))
        assert finds, "函数里没有 find"
        assert all(start == '"$1/"' for start in finds), finds

    def test_the_excluded_name_is_the_one_the_application_writes(self, script: str) -> None:
        """脚本里排除的文件名与应用写密钥文件用的是同一个（改一边忘了另一边，排除就悄悄失效）"""
        assert KEY_FILE_NAME in _function_source(script, "open_data_dir_permissions")

    def test_the_main_flow_uses_the_function_and_no_bare_recursive_777_is_left(self, script: str) -> None:
        source = (DOCKER_DIR / script).read_text(encoding="utf-8")
        assert not re.search(r"chmod\s+-R\s+777", source), "仍有无差别的 chmod -R 777"
        calls = [m.start() for m in re.finditer(r'^\s*open_data_dir_permissions "\$NEKRO_DATA_DIR"', source, re.M)]
        assert len(calls) == 1, "主流程里应恰好调用一次 open_data_dir_permissions"
        assert source.index('mkdir -p "$NEKRO_DATA_DIR"') < calls[0] < source.index('cd "$NEKRO_DATA_DIR"')


# ============================================================
# 2. 应用启动：权限过宽告警
# ============================================================


@pytest.fixture
def warnings_logged():
    captured: List[str] = []
    sink_id = logger.add(lambda message: captured.append(message.record["message"]), level="WARNING")
    try:
        yield captured
    finally:
        logger.remove(sink_id)


@pytest.fixture
def key_in_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """数据目录钉到 tmp，里面已经有一份密钥文件；环境变量密钥清空（落在文件档）"""
    data = tmp_path / "data"
    data.mkdir()
    (data / KEY_FILE_NAME).write_text("sentinel-key-material", encoding="utf-8")
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(data))
    monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "")
    reset_credential_cipher_cache()
    yield data / KEY_FILE_NAME
    reset_credential_cipher_cache()


def _force_mode(monkeypatch: pytest.MonkeyPatch, mode: Optional[int]) -> None:
    monkeypatch.setattr(credential_cipher, "_key_file_mode", lambda _path: mode)


def _permission_warnings(captured: List[str]) -> List[str]:
    # 「权限过宽」是本告警独有的措辞：首次生成那条 WARNING 里也有文件名与「权限 0600」，不能混为一谈
    return [line for line in captured if KEY_FILE_NAME in line and "权限过宽" in line]


class TestStartupWarnsWhenTheKeyFileIsTooOpen:
    @pytest.mark.parametrize(
        ("mode", "should_warn"),
        [
            (0o600, False),
            (0o400, False),  # 只读但仅属主：不过宽
            (0o700, False),  # 属主可执行位与「别人能读」无关
            (0o640, True),  # 组可读
            (0o660, True),
            (0o604, True),  # 其他人可读
            (0o644, True),
            (0o666, True),
            (0o777, True),
            (0o010, True),  # 只有组可执行位也算「组有位」：判据是「组/其他有任何位」，不猜哪一位危险
            (0o001, True),
            (None, False),  # 读不到 / 非 POSIX：不告警
        ],
    )
    def test_table(
        self,
        monkeypatch: pytest.MonkeyPatch,
        key_in_data_dir: Path,  # noqa: ARG002
        warnings_logged: List[str],
        mode: Optional[int],
        should_warn: bool,
    ) -> None:
        _force_mode(monkeypatch, mode)

        credential_cipher.credential_cipher_status()

        assert bool(_permission_warnings(warnings_logged)) is should_warn, warnings_logged

    def test_the_warning_names_the_file_and_the_fix_but_never_the_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
        key_in_data_dir: Path,
        warnings_logged: List[str],
    ) -> None:
        _force_mode(monkeypatch, 0o666)

        credential_cipher.credential_cipher_status()

        (message,) = _permission_warnings(warnings_logged)
        assert str(key_in_data_dir) in message
        assert "0666" in message  # 如实说当前是什么
        assert "chmod 600" in message  # 给出可执行的修法
        assert "sentinel-key-material" not in message

    def test_the_env_key_tier_checks_the_old_file_too(
        self,
        monkeypatch: pytest.MonkeyPatch,
        key_in_data_dir: Path,  # noqa: ARG002
        warnings_logged: List[str],
    ) -> None:
        """配了环境变量之后，数据目录里那把旧密钥仍是「历史密钥」——一样能解开存量密文，一样不能全员可读"""
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "env-material-different-from-file")
        reset_credential_cipher_cache()
        _force_mode(monkeypatch, 0o644)

        status = credential_cipher.credential_cipher_status()

        assert status.source == "env" and status.fallback_count == 1
        assert _permission_warnings(warnings_logged), warnings_logged

    def test_a_freshly_generated_key_file_does_not_warn(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        warnings_logged: List[str],
    ) -> None:
        """应用自己建的文件是 0600：首次生成那次不能因为「读回来检查」而误报"""
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "fresh"))
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "")
        reset_credential_cipher_cache()
        try:
            status = credential_cipher.credential_cipher_status()
        finally:
            reset_credential_cipher_cache()

        assert status.source == "file" and status.generated
        assert _permission_warnings(warnings_logged) == []

    def test_checking_is_cached_with_the_key_ring_one_warning_per_process(
        self,
        monkeypatch: pytest.MonkeyPatch,
        key_in_data_dir: Path,  # noqa: ARG002
        warnings_logged: List[str],
    ) -> None:
        _force_mode(monkeypatch, 0o666)

        for _ in range(3):
            credential_cipher.credential_cipher_status()

        assert len(_permission_warnings(warnings_logged)) == 1

    def test_a_missing_file_has_no_mode_and_no_warning(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path))
        assert credential_cipher._key_file_mode(tmp_path / "nope.key") is None

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows 的 st_mode 不反映真实权限，那里不检查")
    @pytest.mark.parametrize(("mode", "should_warn"), [(0o600, False), (0o644, True), (0o660, True)])
    def test_real_permission_bits_on_posix(
        self,
        key_in_data_dir: Path,
        warnings_logged: List[str],
        mode: int,
        should_warn: bool,
    ) -> None:
        key_in_data_dir.chmod(mode)

        credential_cipher.credential_cipher_status()

        assert bool(_permission_warnings(warnings_logged)) is should_warn, warnings_logged

    @pytest.mark.skipif(sys.platform != "win32", reason="只验 Windows 不误报")
    def test_windows_never_warns_because_st_mode_is_not_permissions(
        self,
        key_in_data_dir: Path,  # noqa: ARG002
        warnings_logged: List[str],
    ) -> None:
        credential_cipher.credential_cipher_status()

        assert _permission_warnings(warnings_logged) == []


def test_call_site_exists_in_the_key_file_reader() -> None:
    """零件对、调用点没了会全绿（PROJECT_STATE §2.7）：读密钥文件的唯一函数里必须真的调用检查"""
    import ast

    source = Path(credential_cipher.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    reader = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_read_key_file")
    calls = [n for n in ast.walk(reader) if isinstance(n, ast.Call) and "permission" in ast.unparse(n.func)]
    assert calls, "_read_key_file 里没有调用权限检查"
