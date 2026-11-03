"""JWT 两把签名密钥与 ADMIN 口令的仓库侧卫生（docs/operations.md §1.1 §1.2）

## 为什么这条最严重

`services/user/auth.py` 用 `OsEnv.JWT_SECRET_KEY` 签票，鉴权只验签 + 按 `sub` 回查数据库：
**拿到这把密钥的人可以直接自签一张 `sub=admin` 的令牌，登录口令不构成边界。**
而仓库一直在教人用「看起来能用」的值：`.env.example` 的 `dev-secret-key-for-development`，
安装脚本的随机化流水线从来不碰 JWT 两把。

## 这一轮钉的五件事

1. **启动审计**（`core/logger.py:audit_factory_default_credentials`）扩到 JWT 两把与 ADMIN 口令：
   示例值 / 占位符 / 过短 / 两把相同 → WARNING，**不拒启动**（与 Postgres 出厂口令那条告警同一口径）。
   **缺键语义与 Postgres 那条相反，且是有意的**：JWT 没设 = `OsEnv` 兜底每进程随机 = 安全（只是重启即全员重登），
   ADMIN 没设 = 登录不可用，都不告警；Postgres 没设 = 生效的正是出厂默认，所以告警。
2. **空串等于没设**：`.env` 里一行 `NEKRO_JWT_SECRET_KEY=`（或 compose 的 `${X:-}` 透传空值）
   不能变成「空的 HMAC 密钥」——那是比示例值更糟的状态（任何人都能自签）。
3. **`.env.example` 不再留能直接用的示例值**：JWT 两把与 ADMIN 口令都是 `CHANGE_ME` 占位，审计必定点名它。
4. **compose 把这两把传进容器**（`environment:` 是显式白名单，不写则 `docker/.env` 里的值进不了容器，
   安装脚本随机化等于白做）；`.env.prod` 里绝不能有这两个键（会被 `run_bot.py` 无条件覆盖进环境）。
5. **三份安装脚本随机化这两把，但已有值一律不覆盖**：覆盖会让已签发令牌与按 JWT 密钥派生的数据当场作废，
   该不该换由运维决定，审计会对示例值 / 过短点名。随机化函数在 bash 里真跑。
"""

import ast
import importlib
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from nekro_agent.core.logger import (
    ADMIN_PASSWORD_MIN_LENGTH,
    JWT_SECRET_MIN_LENGTH,
    audit_factory_default_credentials,
)
from nekro_agent.core.os_env import (
    LEGACY_EXAMPLE_JWT_SECRETS,
    SECRET_PLACEHOLDER_PREFIX,
    OsEnv,
    resolve_signing_secret,
)

REPO_ROOT = Path(__file__).parent.parent
DOCKER_DIR = REPO_ROOT / "docker"
OS_ENV_SOURCE = REPO_ROOT / "nekro_agent" / "core" / "os_env.py"
INSTALL_SCRIPTS = ["install.sh", "install_i18n.sh", "wrtinstall.sh"]
COMPOSE_FILES = ["docker-compose.yml", "docker-compose-x-napcat.yml", "docker-compose-x-hardened.yml"]

JWT = "NEKRO_JWT_SECRET_KEY"
JWT_REFRESH = "NEKRO_JWT_REFRESH_SECRET_KEY"
ADMIN = "NEKRO_ADMIN_PASSWORD"

# 两把互不相同、长度都够的强值（用作「对照组」，证明审计不是见谁都报）
STRONG_A = "k3F9vQ2xT7mB8nZ4pL1wR6yH0cJ5dG8sA3eU9iO2tYq"
STRONG_B = "Zp7Lw2QxN5vB9mT4kR8yC1hJ6dF3sG0aU7eI5oXtVbn"


def _alerts_for(key: str, alerts: List[str]) -> List[str]:
    return [a for a in alerts if key in a]


def _audit(values: Dict[str, str]) -> List[str]:
    """审计，但先补一个合格的 Postgres 口令：Postgres 那条「缺键 = 出厂默认 → 告警」，
    不补的话每条「应当没有告警」的断言都会被它搅红"""
    return audit_factory_default_credentials({"NEKRO_POSTGRES_PASSWORD": "a-real-random-password", **values})


# ============================================================
# 1. 启动审计
# ============================================================


class TestJwtSecretAudit:
    @pytest.mark.parametrize("key", [JWT, JWT_REFRESH])
    @pytest.mark.parametrize(
        ("value", "should_alert"),
        [
            pytest.param("dev-secret-key-for-development", True, id="旧示例值-access"),
            pytest.param("dev-refresh-key-for-development", True, id="旧示例值-refresh"),
            pytest.param("CHANGE_ME_run_openssl_rand_hex_32", True, id="新占位符"),
            pytest.param("change_me_lowercase_but_long_enough_xxxxxxxx", True, id="占位符不分大小写"),
            pytest.param("x" * (JWT_SECRET_MIN_LENGTH - 1), True, id="长度差一位"),
            pytest.param("x" * JWT_SECRET_MIN_LENGTH, False, id="长度刚好够"),
            pytest.param(STRONG_A, False, id="强值"),
            pytest.param("secret:" + "A" * 43, False, id="OsEnv 随机兜底的形态"),
            pytest.param("", False, id="空串等于没设"),
            pytest.param("   ", False, id="纯空白等于没设"),
        ],
    )
    def test_table(self, key: str, value: str, should_alert: bool) -> None:
        alerts = _audit({key: value})

        assert bool(_alerts_for(key, alerts)) is should_alert, alerts

    @pytest.mark.parametrize("legacy", sorted(LEGACY_EXAMPLE_JWT_SECRETS))
    def test_legacy_literals_are_flagged_by_their_own_rule_not_only_by_being_short(
        self,
        monkeypatch: pytest.MonkeyPatch,
        legacy: str,
    ) -> None:
        """旧示例值恰好都不足 32 位，长度规则会顺带拦住它们——于是「认得示例值」这条规则自己没有被验到。
        把长度线压到 1，它们必须仍被点名（将来调长度线或示例值变长，这条规则不能悄悄失效）"""
        # 不能写 `import nekro_agent.core.logger as m`：`nekro_agent.core` 包里 `logger` 这个名字被 loguru 对象占了，
        # 那样拿到的是 loguru 的 logger 而不是模块
        logger_module = importlib.import_module("nekro_agent.core.logger")

        monkeypatch.setattr(logger_module, "JWT_SECRET_MIN_LENGTH", 1)

        assert _alerts_for(JWT, _audit({JWT: legacy}))

    @pytest.mark.parametrize("key", [JWT, JWT_REFRESH])
    def test_a_missing_key_is_not_an_alert(self, key: str) -> None:
        """未设 = OsEnv 每进程随机 = 安全；与 Postgres 那条「缺键按出厂默认处理」相反且有意"""
        assert _alerts_for(key, _audit({})) == []

    def test_each_key_is_judged_on_its_own(self) -> None:
        alerts = _audit({JWT: "dev-secret-key-for-development", JWT_REFRESH: STRONG_B})

        assert _alerts_for(JWT, alerts)
        assert not [a for a in alerts if JWT_REFRESH in a and "示例" in a]

    def test_the_two_keys_being_equal_is_its_own_alert(self) -> None:
        """refresh 与 access 用同一把 = refresh 没有独立吊销能力（docs/operations.md §1.1）"""
        same = _audit({JWT: STRONG_A, JWT_REFRESH: STRONG_A})
        different = _audit({JWT: STRONG_A, JWT_REFRESH: STRONG_B})

        assert len(same) == 1 and "同一个值" in same[0], same
        assert different == []

    @pytest.mark.parametrize(
        ("access", "refresh"),
        [
            pytest.param(STRONG_A, "", id="refresh 空"),
            pytest.param("", STRONG_A, id="access 空"),
            pytest.param("", "", id="两把都空（各自随机，必然不同）"),
        ],
    )
    def test_blank_keys_are_never_reported_as_equal(self, access: str, refresh: str) -> None:
        assert _audit({JWT: access, JWT_REFRESH: refresh}) == []

    @pytest.mark.parametrize("value", [*sorted(LEGACY_EXAMPLE_JWT_SECRETS), "CHANGE_ME_xyz", "short"])
    def test_alert_never_echoes_the_value(self, value: str) -> None:
        for key in (JWT, JWT_REFRESH):
            for alert in _audit({key: value}):
                assert value not in alert

    def test_alert_is_actionable(self) -> None:
        (alert,) = _audit({JWT: "dev-secret-key-for-development"})

        assert JWT in alert
        assert "docs/operations.md" in alert and "§1.1" in alert
        assert "openssl rand" in alert

    def test_alert_says_to_keep_the_old_key_because_legacy_resource_payloads_need_it(self) -> None:
        """换密钥前必须先留一份旧的：旧版 AES 资源载荷（`secret_payload_encrypted`）是用旧 `NEKRO_JWT_SECRET_KEY`
        加密的，恢复入口要求提供「创建资源时使用的旧密钥」——旧密钥丢了就恢复不了"""
        (alert,) = _audit({JWT: "dev-secret-key-for-development"})

        assert "先保留旧密钥" in alert
        assert "AES" in alert and "资源" in alert
        assert alert.index("先保留旧密钥") < alert.index("openssl rand")  # 先说留旧的，再说怎么生成新的


class TestAdminPasswordAudit:
    @pytest.mark.parametrize(
        ("value", "should_alert"),
        [
            pytest.param("admin", True, id="旧示例口令"),
            pytest.param("CHANGE_ME_set_a_strong_password", True, id="占位符"),
            pytest.param("x" * (ADMIN_PASSWORD_MIN_LENGTH - 1), True, id="长度差一位"),
            pytest.param("x" * ADMIN_PASSWORD_MIN_LENGTH, False, id="长度刚好够"),
            pytest.param("A7kQ2mZx9RtL4vBn", False, id="安装脚本生成的 16 位随机口令不能被告警"),
            pytest.param("", False, id="空串 = 登录不可用，不是弱口令"),
        ],
    )
    def test_table(self, value: str, should_alert: bool) -> None:
        alerts = _audit({ADMIN: value})

        assert bool(_alerts_for(ADMIN, alerts)) is should_alert, alerts

    def test_a_missing_password_is_not_an_alert(self) -> None:
        assert _alerts_for(ADMIN, _audit({})) == []

    @pytest.mark.parametrize("value", ["CHANGE_ME_xyz", "short-pw", "Zq9xK"])
    def test_alert_never_echoes_the_password(self, value: str) -> None:
        # 不含字面量 "admin"：告警正文里本来就有「admin 账号」这个词，用它测「有没有回显」分不出来
        for alert in _audit({ADMIN: value}):
            assert value not in alert

    def test_alert_is_actionable(self) -> None:
        (alert,) = _audit({ADMIN: "admin"})

        assert ADMIN in alert
        assert "docs/operations.md" in alert and "§1.2" in alert


class TestAuditWiring:
    def test_default_source_is_the_effective_os_env_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """不传参时取 `OsEnv` 上的生效值——三个新判据都要接上，不只是 Postgres"""
        monkeypatch.setattr(OsEnv, "POSTGRES_PASSWORD", "changed-for-real")
        monkeypatch.setattr(OsEnv, "JWT_SECRET_KEY", STRONG_A)
        monkeypatch.setattr(OsEnv, "JWT_REFRESH_SECRET_KEY", STRONG_B)
        monkeypatch.setattr(OsEnv, "ADMIN_PASSWORD", "A7kQ2mZx9RtL4vBn")
        assert audit_factory_default_credentials() == []

        monkeypatch.setattr(OsEnv, "JWT_SECRET_KEY", "dev-secret-key-for-development")
        assert _alerts_for(JWT, audit_factory_default_credentials())
        monkeypatch.setattr(OsEnv, "JWT_SECRET_KEY", STRONG_A)

        monkeypatch.setattr(OsEnv, "JWT_REFRESH_SECRET_KEY", "dev-refresh-key-for-development")
        assert _alerts_for(JWT_REFRESH, audit_factory_default_credentials())
        monkeypatch.setattr(OsEnv, "JWT_REFRESH_SECRET_KEY", STRONG_B)

        monkeypatch.setattr(OsEnv, "ADMIN_PASSWORD", "admin")
        assert _alerts_for(ADMIN, audit_factory_default_credentials())

    def test_postgres_semantics_are_unchanged_by_the_new_checks(self) -> None:
        """Postgres 那条仍是「缺键 = 生效的正是出厂默认 → 告警」；新判据不能把它带偏"""
        assert audit_factory_default_credentials({})  # 缺 Postgres 键 → 告警
        assert audit_factory_default_credentials({"NEKRO_POSTGRES_PASSWORD": "a-real-random-password"}) == []

    def test_the_example_values_are_not_a_second_copy_of_the_judgement(self) -> None:
        """旧示例值的字面量只有一处（os_env 里的集合），`.env.example` 的新值由下面的用例对拍（§2.10）"""
        assert LEGACY_EXAMPLE_JWT_SECRETS == frozenset(
            {"dev-secret-key-for-development", "dev-refresh-key-for-development"},
        )
        assert SECRET_PLACEHOLDER_PREFIX == "CHANGE_ME"


# ============================================================
# 2. 空串 = 没设
# ============================================================


class TestBlankSigningSecretFallsBackToRandom:
    @pytest.mark.parametrize("environ", [{}, {"NEKRO_JWT_SECRET_KEY": ""}, {"NEKRO_JWT_SECRET_KEY": "   "}])
    def test_unset_or_blank_gives_a_fresh_random_value_with_the_old_prefix(self, environ: Dict[str, str]) -> None:
        first = resolve_signing_secret("JWT_SECRET_KEY", "secret", environ)
        second = resolve_signing_secret("JWT_SECRET_KEY", "secret", environ)

        assert first.startswith("secret:") and len(first) >= 40
        assert first != second  # 每次都是新的，不是写死的常量

    def test_a_real_value_is_returned_verbatim(self) -> None:
        assert resolve_signing_secret("JWT_SECRET_KEY", "secret", {"NEKRO_JWT_SECRET_KEY": STRONG_A}) == STRONG_A

    def test_the_two_prefixes_and_keys_are_independent(self) -> None:
        environ = {"NEKRO_JWT_SECRET_KEY": STRONG_A}

        assert resolve_signing_secret("JWT_REFRESH_SECRET_KEY", "refresh", environ).startswith("refresh:")
        assert resolve_signing_secret("JWT_SECRET_KEY", "secret", environ) == STRONG_A

    def test_os_env_declarations_go_through_the_resolver(self) -> None:
        """AST 接线判据（§2.7）：`OsEnv.JWT_*` 必须真的调用 resolver，不是直接 `OsEnvTypes.Str`（后者会把空串当值）"""
        tree = ast.parse(OS_ENV_SOURCE.read_text(encoding="utf-8"))
        os_env_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "OsEnv")
        found: Dict[str, str] = {}
        for node in os_env_class.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id.startswith("JWT_"):
                assert node.value is not None
                found[node.target.id] = ast.unparse(node.value)

        assert found == {
            "JWT_SECRET_KEY": "resolve_signing_secret('JWT_SECRET_KEY', 'secret')",
            "JWT_REFRESH_SECRET_KEY": "resolve_signing_secret('JWT_REFRESH_SECRET_KEY', 'refresh')",
        }


# ============================================================
# 2b. NEKRO_SUPER_ACCESS_KEY：环境变量读得到，没设仍 fail-closed
#
# `OsEnv.SUPER_ACCESS_KEY` 原先是 `OsEnvTypes.Str("SUPER_ACCESS_KEY", default=lambda: ...)`，而
# `OsEnvTypes.Str` 遇到可调用默认值**根本不读环境变量**：`NEKRO_SUPER_ACCESS_KEY` 设了也读不到，
# `/register` 与 `/create` 的第二重证明永远是「每进程随机、没人知道值」。fail-closed 不是漏洞，
# 但「配了环境变量」形同虚设。改走与 JWT 两把同一个 resolver：有真值用它，没设 / 空 / 纯空白 = 每进程随机
# （绝不能变成空串：`access_key == ""` 的请求就会通过校验）。
# ============================================================

# 导入 os_env 本身会往 stdout 打印（Linux 上的「Set permission of ... to 755」、日志横幅），所以密钥用定界符包起来再取，
# 取出来的仍是逐字节的密钥，判据不放宽。
PROBE_OPEN = "<<SUPER_ACCESS_KEY<<"
PROBE_CLOSE = ">>SUPER_ACCESS_KEY>>"
SUPER_KEY_PROBE = (
    "import sys; sys.path.insert(0, sys.argv[1]);"
    f"from nekro_agent.core.os_env import OsEnv; sys.stdout.write({PROBE_OPEN!r} + OsEnv.SUPER_ACCESS_KEY + {PROBE_CLOSE!r})"
)


def _super_access_key_in_a_fresh_process(**env: str) -> str:
    """在全新进程里按给定环境变量读 `OsEnv.SUPER_ACCESS_KEY`（取值在类体里一次性求出，进程内改环境变量没有意义）"""
    import os
    import sys

    environ = {k: v for k, v in os.environ.items() if k != "NEKRO_SUPER_ACCESS_KEY"}
    environ.update(env)
    result = subprocess.run(
        [sys.executable, "-I", "-c", SUPER_KEY_PROBE, str(REPO_ROOT)],
        capture_output=True,
        text=True,
        env=environ,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.count(PROBE_OPEN) == 1 and result.stdout.count(PROBE_CLOSE) == 1, result.stdout[-2000:]
    return result.stdout.split(PROBE_OPEN, 1)[1].split(PROBE_CLOSE, 1)[0]


class TestSuperAccessKeyReadsTheEnvironment:
    def test_a_configured_value_is_the_effective_value(self) -> None:
        assert _super_access_key_in_a_fresh_process(NEKRO_SUPER_ACCESS_KEY=STRONG_A) == STRONG_A

    def test_unset_is_a_fresh_random_unguessable_value_per_process(self) -> None:
        first = _super_access_key_in_a_fresh_process()
        second = _super_access_key_in_a_fresh_process()

        assert len(first) >= 32 and len(second) >= 32
        assert first != second

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_value_stays_fail_closed_and_never_becomes_an_empty_key(self, blank: str) -> None:
        """`.env` 里一行 `NEKRO_SUPER_ACCESS_KEY=`：不能变成空密钥（`access_key=""` 的请求就会通过校验）"""
        effective = _super_access_key_in_a_fresh_process(NEKRO_SUPER_ACCESS_KEY=blank)

        assert effective.strip() and len(effective) >= 32
        assert effective != blank

    def test_the_declaration_goes_through_the_resolver(self) -> None:
        """AST 接线判据（§2.7）：不是 `OsEnvTypes.Str(..., default=lambda)`（lambda 默认值让环境变量读不到）"""
        tree = ast.parse(OS_ENV_SOURCE.read_text(encoding="utf-8"))
        os_env_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "OsEnv")
        declared = [
            ast.unparse(node.value)
            for node in os_env_class.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "SUPER_ACCESS_KEY"
            and node.value is not None
        ]

        assert declared == ["resolve_signing_secret('SUPER_ACCESS_KEY', 'super')"], declared

    def test_the_criterion_can_see_the_old_lambda_default(self) -> None:
        """对照组：旧写法下 resolver 的断言会红，且旧写法确实读不到环境变量"""
        from nekro_agent.core.core_utils import OsEnvTypes

        old = OsEnvTypes.Str("SUPER_ACCESS_KEY", default=lambda: "from-lambda")
        import os

        previous = os.environ.get("NEKRO_SUPER_ACCESS_KEY")
        os.environ["NEKRO_SUPER_ACCESS_KEY"] = STRONG_A
        try:
            assert OsEnvTypes.Str("SUPER_ACCESS_KEY", default=lambda: "from-lambda") == "from-lambda"
        finally:
            if previous is None:
                os.environ.pop("NEKRO_SUPER_ACCESS_KEY", None)
            else:
                os.environ["NEKRO_SUPER_ACCESS_KEY"] = previous
        assert old == "from-lambda"


# ============================================================
# 3. .env.example 与 docker/.env.example
# ============================================================


def _env_values(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


class TestEnvExamples:
    @pytest.mark.parametrize("key", [JWT, JWT_REFRESH, ADMIN])
    def test_dev_example_has_no_usable_value_and_the_audit_names_it(self, key: str) -> None:
        values = _env_values(REPO_ROOT / ".env.example")

        assert key in values, f".env.example 里没有 {key}"
        value = values[key]
        assert value.startswith(SECRET_PLACEHOLDER_PREFIX), f"{key} 不是占位符"
        assert value not in LEGACY_EXAMPLE_JWT_SECRETS and value != "admin"
        # 逐字照抄这份示例的部署，启动审计必须点名它
        assert _alerts_for(key, _audit({key: value}))

    def test_the_two_placeholders_differ(self) -> None:
        values = _env_values(REPO_ROOT / ".env.example")

        assert values[JWT] != values[JWT_REFRESH]

    def test_no_legacy_example_literal_survives_anywhere_in_the_dev_example(self) -> None:
        text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

        for legacy in LEGACY_EXAMPLE_JWT_SECRETS:
            assert legacy not in text

    @pytest.mark.parametrize("key", [JWT, JWT_REFRESH])
    def test_docker_example_lists_the_key_empty_for_the_installer_to_fill(self, key: str) -> None:
        values = _env_values(DOCKER_DIR / ".env.example")

        assert values.get(key) == ""

    def test_env_prod_never_carries_the_keys(self) -> None:
        """`.env.prod` 被打进镜像，`run_bot.py` 把其中 `NEKRO_` 前缀的键无条件覆盖进 `os.environ`：
        一行空值就会抹掉 compose 传进来的真密钥（与 NEKRO_CREDENTIAL_SECRET_KEY / SELF_HOSTNAME 同一个坑）"""
        values = _env_values(REPO_ROOT / ".env.prod")

        assert JWT not in values and JWT_REFRESH not in values


# ============================================================
# 4. compose 透传
# ============================================================


def _na_environment(compose: str) -> List[str]:
    services: Dict[str, Any] = yaml.safe_load((DOCKER_DIR / compose).read_text(encoding="utf-8"))["services"]
    na = services["nekro_agent"]
    return [str(item) for item in na["environment"]]


@pytest.mark.parametrize("compose", COMPOSE_FILES)
class TestComposePassesTheKeysIntoTheContainer:
    def test_both_keys_are_in_the_whitelist_with_an_empty_default(self, compose: str) -> None:
        environment = _na_environment(compose)

        assert f"{JWT}=${{{JWT}:-}}" in environment
        assert f"{JWT_REFRESH}=${{{JWT_REFRESH}:-}}" in environment

    def test_the_admin_password_line_is_still_there(self, compose: str) -> None:
        """对照：同一份白名单里的老成员还在（确认解析到的就是那个服务的 environment）"""
        assert f"{ADMIN}=${{{ADMIN}:-}}" in _na_environment(compose)


# ============================================================
# 5. 安装脚本：随机化函数真跑
# ============================================================


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


def _run_ensure(script: str, workdir: Path, env_text: str, calls: List[str]) -> str:
    """在 workdir 里放一份 `.env`，用该脚本**自己的** generate_random_string + ensure_random_env_secret 跑 calls，
    返回跑完后的 `.env` 内容"""
    (workdir / ".env").write_bytes(env_text.encode("utf-8"))
    script_text = (
        _function_source(script, "generate_random_string")
        + _function_source(script, "ensure_random_env_secret")
        + "\n".join(calls)
        + "\n"
    )
    env = {"PATH": str(Path(_bash()).parent)}
    result = subprocess.run([_bash(), "-c", script_text], capture_output=True, text=True, env=env, cwd=workdir)
    assert result.returncode == 0, result.stderr
    return (workdir / ".env").read_bytes().decode("utf-8")


def _lines(text: str) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            out.setdefault(key, []).append(value)
    return out


BOTH = [f"ensure_random_env_secret {JWT} 64", f"ensure_random_env_secret {JWT_REFRESH} 64"]


@pytest.mark.parametrize("script", INSTALL_SCRIPTS)
class TestInstallerRandomisesTheJwtKeysButNeverOverwrites:
    def test_a_fresh_env_gets_two_different_long_random_values(self, script: str, tmp_path: Path) -> None:
        out = _lines(_run_ensure(script, tmp_path, f"A=1\n{JWT}=\n{JWT_REFRESH}=\nB=2\n", BOTH))

        assert out[JWT][0] != out[JWT_REFRESH][0]
        for key in (JWT, JWT_REFRESH):
            assert len(out[key]) == 1, "不能留下重复的行"
            assert re.fullmatch(r"[A-Za-z0-9]{64}", out[key][0]), out[key]
            # 随机出来的值必须能通过启动审计（否则安装脚本一跑完启动就告警）
            assert _audit({key: out[key][0]}) == []
        assert out["A"] == ["1"] and out["B"] == ["2"]

    def test_an_env_without_the_lines_gets_them_appended(self, script: str, tmp_path: Path) -> None:
        """老部署的 `.env` 是旧模板铺的，根本没有这两行：sed 替换会静默落空，必须追加"""
        out = _lines(_run_ensure(script, tmp_path, "A=1\n", BOTH))

        assert all(re.fullmatch(r"[A-Za-z0-9]{64}", out[key][0]) for key in (JWT, JWT_REFRESH))

    def test_missing_trailing_newline_does_not_glue_the_new_line_to_the_last_value(self, script: str, tmp_path: Path) -> None:
        out = _lines(_run_ensure(script, tmp_path, "A=1\nLAST=keep", BOTH))

        assert out["LAST"] == ["keep"]
        assert JWT in out and JWT_REFRESH in out

    @pytest.mark.parametrize(
        "existing",
        [
            pytest.param("dev-secret-key-for-development", id="仓库旧示例值也不覆盖"),
            pytest.param("my-own-key-with=equals=signs", id="含等号的值不被截断也不被覆盖"),
            pytest.param("short", id="过短的值也不覆盖（该不该换交给审计去说）"),
        ],
    )
    def test_an_existing_value_is_never_overwritten(self, script: str, tmp_path: Path, existing: str) -> None:
        before = f"{JWT}={existing}\n{JWT_REFRESH}={existing}-r\n"

        after = _run_ensure(script, tmp_path, before, BOTH)

        assert after == before

    def test_running_twice_changes_nothing_the_second_time(self, script: str, tmp_path: Path) -> None:
        first = _run_ensure(script, tmp_path, f"{JWT}=\n", BOTH)
        second = _run_ensure(script, tmp_path, first, BOTH)

        assert second == first

    def test_only_the_empty_one_is_filled(self, script: str, tmp_path: Path) -> None:
        out = _lines(_run_ensure(script, tmp_path, f"{JWT}={STRONG_A}\n{JWT_REFRESH}=\n", BOTH))

        assert out[JWT] == [STRONG_A]
        assert re.fullmatch(r"[A-Za-z0-9]{64}", out[JWT_REFRESH][0])

    @pytest.mark.parametrize(
        "generator",
        [
            pytest.param(":", id="什么都不产"),
            pytest.param("echo only-ten-ch", id="产出比要求的短（随机源降级）"),
        ],
    )
    def test_a_generator_that_cannot_fill_the_length_fails_loudly_and_writes_nothing(
        self,
        script: str,
        tmp_path: Path,
        generator: str,
    ) -> None:
        """随机源不可用 / 给不满时：返回非 0、不往 `.env` 里写空值或短值（宁可失败也不落一把短密钥）"""
        (tmp_path / ".env").write_text("A=1\n", encoding="utf-8", newline="\n")
        script_text = (
            f"generate_random_string() {{ {generator}; }}\n"
            + _function_source(script, "ensure_random_env_secret")
            + f"\nensure_random_env_secret {JWT} 64\necho rc=$?\n"
        )
        env = {"PATH": str(Path(_bash()).parent)}
        result = subprocess.run([_bash(), "-c", script_text], capture_output=True, text=True, env=env, cwd=tmp_path)

        assert "rc=1" in result.stdout, result.stdout + result.stderr
        assert (tmp_path / ".env").read_text(encoding="utf-8") == "A=1\n"

    def test_the_main_flow_randomises_both_keys_exactly_once_after_the_env_exists(self, script: str) -> None:
        source = (DOCKER_DIR / script).read_text(encoding="utf-8")

        for key in (JWT, JWT_REFRESH):
            calls = [m.start() for m in re.finditer(rf"^\s*ensure_random_env_secret {key} \d+", source, re.M)]
            assert len(calls) == 1, f"{script} 里 {key} 的随机化调用应恰好一处，实际 {len(calls)}"
            assert source.index("cp .env.example .env") < calls[0]
            # 在询问「是否继续安装」/ 启动 compose 之前
            assert calls[0] < source.index("NEKRO_EXPOSE_PORT=$(grep")

    def test_the_script_does_not_print_the_generated_values(self, script: str) -> None:
        source = _function_source(script, "ensure_random_env_secret")

        assert "echo \"$value\"" not in source and "echo $value" not in source
        assert "printf" not in source


class TestWrtinstallUrandomFallbackFillsTheLength:
    """软路由脚本没有 openssl 时走 `dd /dev/urandom | tr -dc 字母数字`：随机字节里只有约 24% 是字母数字，
    原来读 `length * 2` 字节只能凑出约一半长度（32 位令牌实际十几位）。现在这条路径还要产 64 位 JWT 密钥。"""

    @pytest.mark.parametrize("length", [16, 32, 64])
    def test_the_urandom_branch_yields_exactly_the_requested_length(self, length: int) -> None:
        hide_openssl = (
            'command() { if [ "$1" = "-v" ] && [ "$2" = "openssl" ]; then return 1; fi; builtin command "$@"; }\n'
        )
        script_text = (
            hide_openssl
            + _function_source("wrtinstall.sh", "generate_random_string")
            + '\ncommand -v openssl >/dev/null 2>&1 && echo OPENSSL_VISIBLE\n'
            + f"generate_random_string {length}\n"
        )
        env = {"PATH": str(Path(_bash()).parent)}
        result = subprocess.run([_bash(), "-c", script_text], capture_output=True, text=True, env=env)

        assert result.returncode == 0, result.stderr
        assert "OPENSSL_VISIBLE" not in result.stdout, "openssl 没有被藏起来，这条用例没有走 urandom 分支"
        assert re.fullmatch(rf"[A-Za-z0-9]{{{length}}}", result.stdout.strip()), result.stdout

