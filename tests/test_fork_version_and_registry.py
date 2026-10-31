"""本 fork 的版本号来源与镜像仓库（GHCR）静态判据

两件事，都是「改一处忘了另一处就悄悄失效」的那种：

1. **版本号**：展示版本只有一处真相 `nekro_agent/tools/common_util.py:FORK_VERSION`。`pyproject.toml` 的 `version` 是
   **上游版本**，驱动云端插件兼容判定 / 更新检查 / 遥测，不能改成 fork 的 0.x（见 `FORK_VERSION` 上方的注释）。
   CHANGELOG 顶部的版本标题、`publish.yml` 的镜像 tag 都必须跟着 `FORK_VERSION` 走。
2. **镜像**：CI、三份 compose、安装脚本、沙盒默认镜像都指向本 fork 的 GHCR（`ghcr.io/akiyo-dayo/...`），不再引用上游
   `kromiose/nekro-agent*` 镜像；工作流只用内置 `GITHUB_TOKEN`，不依赖 Docker Hub / PyPI 密钥（PyPI 任务被显式禁用）。
   **没改的**（上游项目链接、作者署名、LICENSE、README 里上游的 Docker Hub 徽章、`CC_SANDBOX_IMAGE`）不在判据范围内。

每条「不含上游镜像」的判据都带反向对照：用旧写法的样本喂给同一个判据函数，它必须报出命中。
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from nekro_agent.tools.common_util import FORK_VERSION, get_app_version, get_fork_version

REPO_ROOT = Path(__file__).parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
DOCKER_DIR = REPO_ROOT / "docker"
INSTALL_SCRIPTS = ["install.sh", "install_i18n.sh", "wrtinstall.sh"]
COMPOSE_WITH_APP_IMAGE = ["docker-compose.yml", "docker-compose-x-napcat.yml"]

AGENT_IMAGE = "ghcr.io/akiyo-dayo/nekro-agent-byakiyo"
SANDBOX_IMAGE = "ghcr.io/akiyo-dayo/nekro-agent-byakiyo-sandbox"
# 私有版的包名带 `-private`；公开树导出时这两个常量被机械改写成公开包名（`nekro-agent-byakiyo` / `nekro-agent-byakiyo-sandbox`）。
# 下面的包名、前缀、“是不是私有版”全从这两个常量推出，同一份用例在两棵树里都成立。
PRIVATE_EDITION = AGENT_IMAGE.endswith("-private")
AGENT_PACKAGE = AGENT_IMAGE.rsplit("/", 1)[1]
SANDBOX_PACKAGE = SANDBOX_IMAGE.rsplit("/", 1)[1]
OUR_GHCR_PREFIX = AGENT_IMAGE.rsplit("/", 1)[0] + "/"  # ghcr.io/<owner>/


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _upstream_image_refs(text: str) -> List[str]:
    """文本里（注释行除外）指向上游镜像的引用。

    镜像名一律小写（`kromiose/nekro-agent...`）；上游的 GitHub 地址 / 署名是 `KroMiose`，大小写不同，不会被误伤。
    """
    hits: List[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if re.search(r"\bkromiose/nekro-", line):
            hits.append(line.strip())
    return hits


# ============================================================
# 1. 版本号
# ============================================================


class TestForkVersion:
    def test_the_display_version_is_a_plain_semver(self) -> None:
        assert re.fullmatch(r"\d+\.\d+\.\d+", FORK_VERSION), FORK_VERSION
        assert get_fork_version() == FORK_VERSION

    def test_the_upstream_version_in_pyproject_is_not_touched(self) -> None:
        """`get_app_version()` 驱动兼容判定：把它写成 fork 的 0.x 会让声明了最低 NA 版本的云端插件全部被判不兼容"""
        assert get_app_version() != FORK_VERSION
        major = int(get_app_version().split(".")[0])
        assert major >= 2, get_app_version()

    def test_the_changelog_has_a_heading_for_the_current_version_above_all_dated_sections(self) -> None:
        headings = re.findall(r"^## (.+)$", _read(REPO_ROOT / "CHANGELOG.md"), re.M)
        assert headings[0].startswith(f"v{FORK_VERSION} ("), f"CHANGELOG 顶部标题应是 v{FORK_VERSION}，实际是 {headings[0]!r}"
        # 公开版的 CHANGELOG 只有版本小节、没有带日期的小节：没有日期节时“版本标题在它们之上”平凡成立
        first_dated = next((i for i, h in enumerate(headings) if re.match(r"\d{4}-\d{2}-\d{2}", h)), len(headings))
        assert first_dated > 0, "版本标题必须排在带日期的各节之上"

    def test_the_version_criterion_can_see_a_stale_changelog(self) -> None:
        stale = "## v0.1.4 (2026-08-08)\n\n## 2026-08-08\n"
        headings = re.findall(r"^## (.+)$", stale, re.M)
        assert not headings[0].startswith(f"v{FORK_VERSION} (")

    def test_publish_workflow_reads_the_fork_version_from_the_one_source(self) -> None:
        text = _read(WORKFLOWS / "publish.yml")
        assert "nekro_agent/tools/common_util.py" in text and "FORK_VERSION" in text
        # 不再读 pyproject 的上游版本当镜像 tag（PyPI 任务里读 SDK 的 pyproject 是另一回事，且已禁用）
        setup = yaml.safe_load(text)["jobs"]["setup-version"]
        assert "pyproject.toml" not in yaml.safe_dump(setup)

    def test_the_workflow_extraction_command_really_yields_the_fork_version(self) -> None:
        """把工作流里的 sed 原样拿来在真文件上跑：改了 `FORK_VERSION` 的写法而没改 sed，镜像 tag 就会是空的"""
        bash = shutil.which("bash")
        git = shutil.which("git")
        if bash is None and git is not None:
            candidate = Path(git).resolve().parent.parent / "usr" / "bin" / "bash.exe"
            bash = str(candidate) if candidate.is_file() else None
        if bash is None:
            pytest.skip("没有 bash")
        match = re.search(r"VERSION=\$\((sed -n .*? nekro_agent/tools/common_util\.py)\)", _read(WORKFLOWS / "publish.yml"))
        assert match, "publish.yml 里没有用 sed 读取 FORK_VERSION 的那一行"
        result = subprocess.run(
            [bash, "-c", f'cd "{REPO_ROOT.as_posix()}" && {match.group(1)}'],
            capture_output=True,
            text=True,
            env={"PATH": str(Path(bash).parent)},  # Windows 上 sed 与 bash 同在 usr/bin
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == FORK_VERSION

    def test_a_tag_that_differs_from_the_code_version_is_refused(self) -> None:
        text = _read(WORKFLOWS / "publish.yml")
        assert 'GITHUB_REF_NAME}" != "v${VERSION}"' in text


# ============================================================
# 2. 镜像：判据本身的反向对照
# ============================================================


class TestTheCriterionSeesTheOldForm:
    OLD = (
        "          tags: |\n"
        "            kromiose/nekro-agent:latest\n"
        "    image: kromiose/nekro-agent:latest\n"
        "if ! sudo docker pull kromiose/nekro-agent-sandbox; then\n"
    )

    def test_old_form_is_reported(self) -> None:
        assert len(_upstream_image_refs(self.OLD)) == 3

    def test_new_form_is_clean(self) -> None:
        new = self.OLD.replace("kromiose/nekro-agent-sandbox", SANDBOX_IMAGE).replace("kromiose/nekro-agent", AGENT_IMAGE)
        assert _upstream_image_refs(new) == []

    def test_comments_and_upstream_urls_and_attribution_are_not_image_references(self) -> None:
        text = (
            "#    里有——上游 kromiose/nekro-agent 镜像没有它\n"
            "git clone https://github.com/KroMiose/nekro-agent.git\n"
            'authors = [{name = "KroMiose"}]\n'
        )
        assert _upstream_image_refs(text) == []


# ============================================================
# 3. 工作流
# ============================================================


def _workflow(name: str) -> Dict[str, Any]:
    return yaml.safe_load(_read(WORKFLOWS / name))


def _disabled(job: Dict[str, Any]) -> bool:
    """任务被写成 `if: ${{ false }}`（YAML 里是字符串，不是布尔 False）"""
    return str(job.get("if", "")).replace(" ", "") == "${{false}}"


def _steps(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(job.get("steps", []))


@pytest.mark.parametrize("name", ["publish.yml", "preview.yml"])
class TestWorkflowsPushToOurGhcrOnly:
    def test_no_upstream_image_is_referenced(self, name: str) -> None:
        assert _upstream_image_refs(_read(WORKFLOWS / name)) == []

    def test_no_docker_hub_secret_or_login(self, name: str) -> None:
        text = _read(WORKFLOWS / name)
        assert "DOCKER_USERNAME" not in text and "DOCKER_PASSWORD" not in text
        for job in _workflow(name)["jobs"].values():
            for step in _steps(job):
                if str(step.get("uses", "")).startswith("docker/login-action"):
                    assert step["with"]["registry"] == "ghcr.io", step

    def test_the_only_credential_is_the_builtin_github_token(self, name: str) -> None:
        for job_name, job in _workflow(name)["jobs"].items():
            if _disabled(job):  # 被显式禁用的任务（见 publish.yml 的 PyPI 两项）不算
                continue
            secrets_used = set(re.findall(r"secrets\.(\w+)", yaml.safe_dump(job)))
            assert secrets_used <= {"GITHUB_TOKEN"}, (job_name, secrets_used)

    def test_packages_write_permission_is_granted(self, name: str) -> None:
        assert _workflow(name)["permissions"]["packages"] == "write"

    def test_every_pushed_tag_is_under_the_lowercased_owner_in_ghcr(self, name: str) -> None:
        pushed = 0
        for job in _workflow(name)["jobs"].values():
            steps = _steps(job)
            for step in steps:
                if not str(step.get("uses", "")).startswith("docker/build-push-action"):
                    continue
                pushed += 1
                tags = [t.strip() for t in str(step["with"]["tags"]).splitlines() if t.strip()]
                assert tags
                for tag in tags:
                    assert re.match(
                        r"^ghcr\.io/\$\{\{ steps\.repo\.outputs\.owner \}\}/(?:" + re.escape(AGENT_PACKAGE) + "|" + re.escape(SANDBOX_PACKAGE) + "):",
                        tag,
                    ), tag
                # owner 必须在同一个任务里先被转成小写（GHCR 不接受大写）
                owner_step = next(s for s in steps if s.get("id") == "repo")
                assert "tr '[:upper:]' '[:lower:]'" in owner_step["run"]
        assert pushed == 2, f"{name} 里应有主应用与沙盒两个构建推送步骤"


class TestPublishWorkflowPypiJobsAreDisabled:
    PYPI_JOBS = ("publish-main-to-pypi", "publish-sdk-to-pypi")

    def test_both_inherited_pypi_jobs_exist_but_are_switched_off(self) -> None:
        jobs = _workflow("publish.yml")["jobs"]
        for name in self.PYPI_JOBS:
            assert name in jobs
            assert _disabled(jobs[name]), f"{name} 必须保持禁用（要启用先确认包名归属与令牌来源）"

    def test_no_enabled_job_touches_pypi(self) -> None:
        for name, job in _workflow("publish.yml")["jobs"].items():
            if name in self.PYPI_JOBS:
                continue
            dumped = yaml.safe_dump(job)
            assert "PYPI" not in dumped and "uv publish" not in dumped, name

    def test_the_criterion_can_see_an_enabled_pypi_job(self) -> None:
        assert _disabled({"if": "${{ false }}"})
        assert not _disabled({"if": "${{ true }}"})
        assert not _disabled({})


# ============================================================
# 4. compose / 脚本 / 默认配置 / 文档
# ============================================================


@pytest.mark.parametrize("name", COMPOSE_WITH_APP_IMAGE)
def test_compose_runs_our_ghcr_image(name: str) -> None:
    text = _read(DOCKER_DIR / name)
    assert _upstream_image_refs(text) == []
    services = yaml.safe_load(text)["services"]
    images = {svc["image"] for svc in services.values() if "nekro-agent" in str(svc.get("image", ""))}
    assert images == {f"{AGENT_IMAGE}:latest"}, images


def test_the_hardened_compose_has_no_hardcoded_upstream_image() -> None:
    """硬化部署的镜像由 `NEKRO_IMAGE` 指定；文件里提到上游镜像只能出现在注释里（说明为什么不能用它）"""
    assert _upstream_image_refs(_read(DOCKER_DIR / "docker-compose-x-hardened.yml")) == []


@pytest.mark.parametrize("script", INSTALL_SCRIPTS)
class TestInstallScripts:
    def test_no_upstream_image_is_referenced(self, script: str) -> None:
        assert _upstream_image_refs(_read(DOCKER_DIR / script)) == []

    def test_the_sandbox_is_pulled_from_our_ghcr(self, script: str) -> None:
        assert re.search(rf"docker pull {re.escape(SANDBOX_IMAGE)}; then", _read(DOCKER_DIR / script))

    def test_both_pull_failures_print_the_login_hint_before_exiting(self, script: str) -> None:
        text = _read(DOCKER_DIR / script)
        assert re.search(r"^ghcr_login_hint\(\) \{", text, re.M)
        calls = re.findall(r"^\s*ghcr_login_hint\n\s*exit 1", text, re.M)
        assert len(calls) == 2, "服务镜像与沙盒镜像拉取失败后都应调用 ghcr_login_hint"

    def test_the_hint_is_about_login_not_about_the_network(self, script: str) -> None:
        text = _read(DOCKER_DIR / script)
        if PRIVATE_EDITION:  # 私有版的包是私有的：拉取失败要提示先登录
            assert "docker login ghcr.io" in text
            assert "read:packages" in text
            assert OUR_GHCR_PREFIX in text
        else:  # 公开版的包是公开的：不能再让用户去登录
            assert "docker login" not in text and "read:packages" not in text
            assert "private-only" not in text


@pytest.mark.skipif(not PRIVATE_EDITION, reason="登录提示只属于私有版（公开版的包是公开的）")
@pytest.mark.parametrize("script", ["install_i18n.sh", "wrtinstall.sh"])
def test_the_login_hint_is_translated_in_both_languages(script: str) -> None:
    text = _read(DOCKER_DIR / script)
    for key in ("ghcr_hint_logged_in", "ghcr_hint_not_logged_in"):
        bodies = re.findall(rf'"{key}"\) echo "(.*?)" ;;', text, re.S)
        assert len(bodies) == 2, f"{script}: {key} 应有中英两份"
        assert any(re.search(r"[一-鿿]", b) for b in bodies) and any(
            not re.search(r"[一-鿿]", b) for b in bodies
        )


def test_sandbox_helper_script_uses_our_ghcr() -> None:
    text = _read(REPO_ROOT / "sandbox.sh")
    assert _upstream_image_refs(text) == []
    assert SANDBOX_IMAGE in text


def test_the_default_sandbox_image_is_ours() -> None:
    from nekro_agent.core.config import CoreConfig

    assert CoreConfig.model_fields["SANDBOX_IMAGE_NAME"].default == SANDBOX_IMAGE


@pytest.mark.parametrize("name", ["README.md", "README_en.md"])
def test_readme_pull_commands_use_our_ghcr(name: str) -> None:
    text = _read(REPO_ROOT / name)
    pulls = re.findall(r"^docker pull (\S+)$", text, re.M)
    assert pulls, "README 里应有 docker pull 示例"
    assert all(p.startswith(OUR_GHCR_PREFIX + "nekro-agent") for p in pulls), pulls


def test_operations_guide_names_both_images_and_the_registry_login() -> None:
    text = _read(REPO_ROOT / "docs" / "operations.md")
    assert AGENT_IMAGE in text and SANDBOX_IMAGE in text
    if PRIVATE_EDITION:
        assert "docker login ghcr.io" in text and "read:packages" in text
    else:  # 公开版的包是公开的，指南里不再要求登录
        assert "docker login" not in text and "read:packages" not in text


def test_the_windows_extension_guide_pulls_our_sandbox() -> None:
    text = _read(REPO_ROOT / "docs" / "Extension_Development_For_Windows.md")
    assert _upstream_image_refs(text) == []
    assert f"docker pull {SANDBOX_IMAGE}:latest" in text
