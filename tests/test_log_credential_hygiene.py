"""启动期凭据卫生

被测契约：`nonebot.init()` 那条 `Loaded Config` DEBUG 日志里不得出现凭据明文，
而 `driver.config` 上的取值必须在适配器注册之前恢复成明文。

写法上刻意做了三件事：

1. **先证明这组用例测得出差异**（§2.11）：每一条"遮住了"的断言都配一条
   "不加遮蔽就会漏"的对照，否则一个坏掉的 dump（永远返回空）也能满足它。
2. **打靶用的是真的 NoneBot `Config` 与真的 OneBot 适配器配置模型**，不是自造的 fake：
   这条泄漏与这条修复都长在第三方模型的序列化行为上，fake 掉它等于没测。
3. **单独盯住调用点**（§2.7）：零件全对而 `run_bot.py` 里没接上时，前面那些用例照样全绿。
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping

import pytest
from nonebot.adapters.onebot.v11.config import Config as OneBotConfig
from nonebot.compat import model_dump
from nonebot.config import Config as NoneBotConfig
from pydantic import SecretStr, ValidationError

import nekro_log_hygiene
from nekro_agent.services.sandbox.credential_guard import _SECRET_FIELD_NAME_PATTERN
from nekro_log_hygiene import (
    SECRET_ENV_KEY_PATTERN,
    build_env_secret_shield,
    coerce_like_nonebot,
    describe_shield,
    is_secret_env_key,
    nonebot_declared_field_names,
    resolve_env_files,
    restore_shielded_values,
    secret_env_keys,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_BOT_PATH = REPO_ROOT / "run_bot.py"


class TestSecretKeyClassification:
    """键名判据：命中的都是真凭据，不命中的都是真该看得见的运行参数"""

    @pytest.mark.parametrize(
        "key",
        [
            "NEKRO_ADMIN_PASSWORD",
            "nekro_admin_password",
            "NEKRO_POSTGRES_PASSWORD",
            "NEKRO_JWT_SECRET_KEY",
            "NEKRO_JWT_REFRESH_SECRET_KEY",
            "NEKRO_QDRANT_API_KEY",
            "NEKRO_RPC_SECRET_KEY",
            "NEKRO_WEBHOOK_SECRET_KEY",
            "NEKRO_SUPER_ACCESS_KEY",
            "ONEBOT_ACCESS_TOKEN",
            "TELEGRAM_BOT_TOKEN",
            "SOME_VENDOR_APIKEY",
            "MY_PRIVATE_KEY",
            "X_CREDENTIAL",
            "DB_PASSWD",
        ],
    )
    def test_credential_keys_are_recognised(self, key: str) -> None:
        assert is_secret_env_key(key) is True

    @pytest.mark.parametrize(
        "key",
        [
            "NEKRO_DATA_DIR",
            "NEKRO_EXPOSE_PORT",
            "NEKRO_POSTGRES_HOST",
            "NEKRO_POSTGRES_PORT",
            "NEKRO_POSTGRES_USER",
            "NEKRO_POSTGRES_DATABASE",
            "NEKRO_QDRANT_URL",
            "NEKRO_STATIC_DIR",
            "NEKRO_ALLOW_PRIVATE_NETWORK_DOWNLOAD",
            "ENVIRONMENT",
            "DRIVER",
            "HOST",
            "PORT",
            "LOG_LEVEL",
            "FASTAPI_RELOAD",
        ],
    )
    def test_operational_keys_stay_visible(self, key: str) -> None:
        assert is_secret_env_key(key) is False

    def test_pattern_is_the_same_one_the_sandbox_guard_uses(self) -> None:
        """两处判据必须逐字相等

        `credential_guard` 管"别把凭据发回沙盒"，本模块管"别把凭据打进日志"。
        它们漏判的后果同源，判据一旦分叉就等于其中一处悄悄失效，而没有任何东西会提醒。
        本模块不能 import 那个包（会提前触发 `core/config.py` 的模块级初始化），
        所以只能镜像一份，由这条用例把两份钉在一起。
        """
        assert SECRET_ENV_KEY_PATTERN.pattern == _SECRET_FIELD_NAME_PATTERN.pattern
        assert SECRET_ENV_KEY_PATTERN.flags == _SECRET_FIELD_NAME_PATTERN.flags


class TestEnvFileResolution:
    """必须与 `nonebot.init()` 读的是同一批文件：漏读一份 = 那份里的凭据不受保护"""

    def test_explicit_env_file_is_read_together_with_dot_env(self) -> None:
        assert resolve_env_files(Path(".env.dev")) == (Path(".env"), Path(".env.dev"))

    def test_missing_env_file_falls_back_to_environment_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENVIRONMENT", "staging")
        assert resolve_env_files(None) == (Path(".env"), Path(".env.staging"))

    def test_missing_everything_falls_back_to_nonebot_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.chdir(tmp_path)  # 保证读不到仓库根目录那份 .env
        assert resolve_env_files(None) == (Path(".env"), Path(".env.prod"))


class TestValueCoercion:
    """还原时必须逐字复刻 NoneBot 对额外字段的取值规则"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # 样例口令必须是**造出来的**。这一格曾经逐字就是测试部署的 `NEKRO_ADMIN_PASSWORD`
            # （见 `TestNoRealCredentialLeaksIntoTheSuite`），那等于把一台在跑的部署的后台口令
            # 提交进仓库。要一串"不像 JSON 的随机字符串"，自己编一个就够了。
            ("not-a-real-password-just-shaped-like-one", "not-a-real-password-just-shaped-like-one"),
            ("12345678", 12345678),  # 纯数字：NoneBot 会解析成 int，还原也必须是 int
            ("true", True),
            ('"quoted"', "quoted"),
            ("  spaced-secret  ", "  spaced-secret  "),  # 解析失败时保留**未 strip 的**原串
            ("", ""),
            ("   ", "   "),
        ],
    )
    def test_matches_nonebot_extra_field_rule(self, raw: str, expected: Any) -> None:
        assert coerce_like_nonebot(raw) == expected
        assert type(coerce_like_nonebot(raw)) is type(expected)


def _write_env(tmp_path: Path, body: str, name: str = ".env.dev") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


ENV_BODY = "\n".join(
    [
        "# comment",
        "NEKRO_DATA_DIR=./data/nekro_agent",
        "NEKRO_POSTGRES_HOST=127.0.0.1",
        "NEKRO_POSTGRES_PASSWORD=pg-pw-sentinel-4417",
        "NEKRO_ADMIN_PASSWORD=admin-pw-sentinel-9021",
        "NEKRO_JWT_SECRET_KEY=jwt-sentinel-aaaa",
        "NEKRO_JWT_REFRESH_SECRET_KEY=jwt-refresh-sentinel-bbbb",
        "NEKRO_QDRANT_API_KEY=qdrant-sentinel-cccc",
        "ONEBOT_ACCESS_TOKEN=onebot-sentinel-dddd",
        "LOG_LEVEL=DEBUG",
        "PORT=8021",
    ],
)

SENTINELS = (
    "pg-pw-sentinel-4417",
    "admin-pw-sentinel-9021",
    "jwt-sentinel-aaaa",
    "jwt-refresh-sentinel-bbbb",
    "qdrant-sentinel-cccc",
    "onebot-sentinel-dddd",
)


# ---------------------------------------------------------------------------
# 把这份测试与「跑它的那台机器上导出的真实配置」隔开
# ---------------------------------------------------------------------------

# 伪装成一台真部署：让下面那条隔离 fixture 在**任何**机器上都有活可干。
# 没有它，隔离 fixture 在开发机上是个空操作，"它确实清掉了"那条用例就是恒真的空断言（§2.11）。
_HOST_SHAPED_ENVIRON = {
    "NEKRO_ADMIN_PASSWORD": "host-environ-would-shadow-the-sentinel",
    "ONEBOT_ACCESS_TOKEN": "host-environ-would-shadow-this-one-too",
    # 故意让取值**逐字出现在本文件里**（它就是 `ENV_BODY` 里那一行）：
    # 下面「别把真凭据写进用例」那条检查靠它当正向对照，否则在一台没导出任何配置的
    # 开发机上，那条断言的输入集是空的，恒真。
    "NEKRO_PLANTED_PASSWORD": "pg-pw-sentinel-4417",
}

# 清理**之前**这台机器上真实导出的凭据取值。`TestNoRealCredentialLeaksIntoTheSuite`
# 用它来断言"本文件里没有任何一个字面量是真凭据"——在真部署上跑才有真输入。
_HOST_CREDENTIALS_SEEN: Dict[str, str] = {}


def _is_deployment_env_key(key: str) -> bool:
    """这个 env 键是否可能由一台真实 NA 部署导出

    判据 = 「NA 与协议端自己的命名空间」∪「凭据命名模式」。前者覆盖 `.env.dev` 里
    那一整批 `NEKRO_*` / `ONEBOT_*`，后者兜住将来换了前缀的凭据键。
    """
    upper = key.upper()
    return upper.startswith(("NEKRO_", "ONEBOT_")) or is_secret_env_key(key)


# 一个「裸小写标识符」形态的取值，与源码里正常出现的名字无法区分。
# 实测：这台测试部署的 `NEKRO_POSTGRES_PASSWORD` 就是出厂默认 `nekro_agent`，
# 与包名逐字相同（docs/operations.md §1.3：扫描无法把它与正常文本区分开）。
# 拿它去查源码只会得到一串必然命中的假阳性（本文件顶上就 import 了 `nekro_agent.*`）。
_BARE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


def _is_a_checkable_secret_value(key: str, value: str) -> bool:
    """这条 env 取值适不适合拿去查「有没有被写成测试里的字面量」

    三个条件：键名是凭据形态、取值够长、取值不是一个裸小写标识符。
    非凭据键（`NEKRO_DATA_DIR=./data/nekro_agent` 之类）本来就该在样例里出现，
    把它们算进来会让这条守卫每轮都误报，误报几次之后就没人再看它了。
    """
    return is_secret_env_key(key) and len(value) >= 8 and not _BARE_IDENTIFIER.match(value)


@pytest.fixture(scope="module", autouse=True)
def _pretend_this_machine_is_a_real_deployment() -> Iterator[None]:
    """在本文件范围内把进程伪装成「导出了真实部署配置」的那种环境

    模块作用域 → 排在下面那条函数作用域的隔离 fixture **之前**生效，
    于是每一条用例都在敌对条件下跑，而不是"碰巧这台机器上没设这些变量"。
    """
    previous = {key: os.environ.get(key) for key in _HOST_SHAPED_ENVIRON}
    os.environ.update(_HOST_SHAPED_ENVIRON)
    _HOST_CREDENTIALS_SEEN.update(
        {key: value for key, value in os.environ.items() if _is_a_checkable_secret_value(key, value)},
    )
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(autouse=True)
def _isolate_from_the_host_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """跑每一条用例之前，把 `os.environ` 里属于本机部署的那批键清掉

    **这不是洁癖，是本文件的正确性前提。** `_read_env_file_keys()` 有意合并
    `{**env_file_vars, **os.environ}`（NoneBot 就是这么合的，遮的必须是最终生效的那个值），
    NoneBot 自己的 `EnvSettingsSource` 同样排在 `DotEnvSettingsSource` 之前。于是只要进程里
    导出了真实的 `NEKRO_ADMIN_PASSWORD`，本文件写进 tmp 目录的哨兵值就会被**静默替换**
    成那台机器上的真值，而不是报错：

    - `test_empty_values_are_left_alone` 拿到一个非空 shield；
    - `test_without_the_shield_the_dump_really_leaks` 的**对照组**再也复现不出泄漏，
      于是"遮住了"那一条从此可以被任何一个 dump 满足；
    - `test_the_onebot_adapter_would_break_without_the_restore` 里 `SecretStr` 压根没进 dump。

    这不是假设：在一份已配好的部署目录里 `set -a; . ./.env.dev; set +a` 之后跑
    `pytest tests/`（把部署环境变量导进 shell 再跑测试），实测就是这 4 条红
    （同一条教训：测试必须对环境变量封闭）。

    只做"清"这一件事：需要"环境变量赢过文件"的用例自己 `monkeypatch.setenv`，
    函数作用域里排在本 fixture 之后，照常生效。
    """
    for key in [key for key in os.environ if _is_deployment_env_key(key)]:
        monkeypatch.delenv(key, raising=False)


class TestTheseTestsAreClosedOverTheEnvironment:
    """盯住上面那两条 fixture 本身（§2.9）"""

    def test_every_credential_key_this_file_writes_is_covered_by_the_criterion(self) -> None:
        """往 `ENV_BODY` 里新加一个判据盖不到的凭据键时，这条会红

        盖不到的后果不是报错而是**静默换值**，所以判据本身必须有一条盯着。
        """
        credential_keys = [
            line.split("=", 1)[0]
            for line in ENV_BODY.splitlines()
            if "=" in line and not line.lstrip().startswith("#") and is_secret_env_key(line.split("=", 1)[0])
        ]
        assert credential_keys, "ENV_BODY 里一个凭据键都没有，这份测试失去了靶子"
        assert [key for key in credential_keys if not _is_deployment_env_key(key)] == []

    def test_the_isolation_really_happens(self) -> None:
        """判据对了但没接上（忘了 autouse / 拼错 key）时，这条会红

        `_pretend_this_machine_is_a_real_deployment` 保证这两个键在进入用例**之前**
        确实是设着的，所以这里不是"这台机器碰巧没设"的空断言。
        """
        for key in _HOST_SHAPED_ENVIRON:
            assert os.environ.get(key) is None, f"{key} 没被清掉，本文件的哨兵值会被它静默顶替"


class TestNoRealCredentialLeaksIntoTheSuite:
    """别把跑这份测试的那台机器上的真凭据当成"样例字符串"写进用例

    **真踩过（本轮）**：`TestValueCoercion` 里那格"普通口令"曾经逐字就是测试部署的
    `NEKRO_ADMIN_PASSWORD`。它长得就像一串随便编的乱码，`ruff` / `basedpyright` /
    全部测试一路全绿，是提交前对着 `.env.dev` 做凭据扫描才抓到的 ——
    再晚一步就把一台在跑的部署的后台口令提交进了仓库。

    判据取的是**隔离 fixture 清理之前**的那一份 `os.environ` 快照，所以在真部署上跑
    （`set -a; . ./.env.dev; set +a; pytest`）就能当场红。
    """

    @staticmethod
    def _leaked(candidates: Mapping[str, str]) -> list[str]:
        source = Path(__file__).read_text(encoding="utf-8")
        return sorted(key for key, value in candidates.items() if value in source)

    def test_the_check_can_actually_see_a_literal_in_this_file(self) -> None:
        """正向对照：判据必须真的看得见文件里的字面量

        没有它，一台没导出任何配置的开发机上输入集是空的，下面那条恒真（§2.11）。
        `NEKRO_PLANTED_PASSWORD` 的取值就是 `ENV_BODY` 里那个哨兵，必须被抓到。
        """
        assert self._leaked(_HOST_CREDENTIALS_SEEN) != [], "判据看不见任何字面量，下面那条等于没断言"
        assert "NEKRO_PLANTED_PASSWORD" in self._leaked(_HOST_CREDENTIALS_SEEN)

    def test_no_sample_literal_is_a_real_credential_from_this_machine(self) -> None:
        planted = set(_HOST_SHAPED_ENVIRON)
        real = {key: value for key, value in _HOST_CREDENTIALS_SEEN.items() if key not in planted}
        leaked = self._leaked(real)
        assert leaked == [], f"用例里出现了本机真实凭据的字面量: {leaked}（改成自己编的样例值）"


class TestShieldConstruction:
    def test_only_credential_keys_are_shielded_and_keys_are_lowercased(self, tmp_path: Path) -> None:
        env_file = _write_env(tmp_path, ENV_BODY)
        shield = build_env_secret_shield([env_file])

        assert sorted(shield) == [
            "nekro_admin_password",
            "nekro_jwt_refresh_secret_key",
            "nekro_jwt_secret_key",
            "nekro_postgres_password",
            "nekro_qdrant_api_key",
            "onebot_access_token",
        ]
        assert all(isinstance(value, SecretStr) for value in shield.values())
        assert shield["nekro_admin_password"].get_secret_value() == "admin-pw-sentinel-9021"

    def test_lowercase_matters_because_nonebot_lowercases_extra_keys(self, tmp_path: Path) -> None:
        """键名大小写写错 = 多出一个额外字段，而明文那一份仍然照打

        NoneBot 的 env 键大小写不敏感（`_apply_case_sensitive`），额外字段一律以小写落进
        `Config`。掩码 kwargs 用原始大小写传进去只会新增一个键，遮不住任何东西。
        """
        env_file = _write_env(tmp_path, ENV_BODY)
        shield = build_env_secret_shield([env_file])
        assert all(key == key.lower() for key in shield)

    def test_empty_values_are_left_alone(self, tmp_path: Path) -> None:
        env_file = _write_env(tmp_path, "ONEBOT_ACCESS_TOKEN=\nNEKRO_ADMIN_PASSWORD=   \n")
        assert build_env_secret_shield([env_file]) == {}

    def test_nested_keys_are_not_taken_over(self, tmp_path: Path) -> None:
        """带 `__` 的键会被 NoneBot 摊成嵌套 dict，形态不同，本模块有意不接管"""
        env_file = _write_env(tmp_path, "SOMETHING__NESTED_TOKEN=abc123456\n")
        assert build_env_secret_shield([env_file]) == {}

    def test_environment_variable_wins_over_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """NoneBot 合并的是 `{**env_file_vars, **os.environ}`，遮的必须是最终生效的那个值"""
        env_file = _write_env(tmp_path, "NEKRO_ADMIN_PASSWORD=from-file-sentinel\n")
        monkeypatch.setenv("NEKRO_ADMIN_PASSWORD", "from-environ-sentinel")
        shield = build_env_secret_shield([env_file])
        assert shield["nekro_admin_password"].get_secret_value() == "from-environ-sentinel"

    def test_keys_only_in_environ_are_not_invented(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """只在 os.environ 里的键不会成为 NoneBot 的额外字段，凭空加进去等于改配置形态"""
        env_file = _write_env(tmp_path, "LOG_LEVEL=DEBUG\n")
        monkeypatch.setenv("SOME_OTHER_API_KEY", "not-in-the-env-file")
        assert build_env_secret_shield([env_file]) == {}

    def test_excluded_keys_are_skipped(self, tmp_path: Path) -> None:
        """调用方自己也要传的键必须排除，否则 `nonebot.init(**a, **b)` 直接 TypeError"""
        env_file = _write_env(tmp_path, ENV_BODY)
        shield = build_env_secret_shield([env_file], exclude_keys=("NEKRO_ADMIN_PASSWORD",))
        assert "nekro_admin_password" not in shield

    def test_nonebot_declared_fields_are_never_shielded(self, tmp_path: Path) -> None:
        """声明字段有具体类型，塞 SecretStr 进去是 `nonebot.init()` 当场校验失败

        当前 NoneBot 没有任何声明字段命中凭据命名模式，这条是给未来版本留的保险；
        少遮一个非凭据字段没有代价，遮错一个声明字段是启动即崩。
        """
        declared = nonebot_declared_field_names()
        assert "port" in declared and "log_level" in declared and "driver" in declared

        env_file = _write_env(tmp_path, "PORT=8021\nLOG_LEVEL=DEBUG\nDRIVER=~fastapi\n")
        assert build_env_secret_shield([env_file]) == {}

    def test_the_declared_field_exclusion_really_is_wired_in(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """上一条杀不掉"把 `nonebot_declared_field_names()` 从排除集里删掉"这个变异

        原因很实在：当前 NoneBot 一个声明字段都不命中凭据命名模式，所以那半个并集
        今天是个空操作，用真实字段名怎么测都是全绿。这里把"未来 NoneBot 新增了一个
        叫 `api_token` 的声明字段"这一天先演出来——它是那条排除存在的唯一理由，
        真到了那天，没有这条用例就只剩启动即崩一种反馈。
        """
        monkeypatch.setattr(
            nekro_log_hygiene,
            "nonebot_declared_field_names",
            lambda: frozenset({"api_token"}),
        )
        env_file = _write_env(tmp_path, "API_TOKEN=would-crash-nonebot-init\nNEKRO_ADMIN_PASSWORD=still-shielded\n")
        shield = build_env_secret_shield([env_file])
        assert "api_token" not in shield, "声明字段被喂了 SecretStr，nonebot.init() 会当场校验失败"
        assert "nekro_admin_password" in shield, "排除得过头了，真凭据反而漏出去"

    def test_over_inclusive_by_design(self, tmp_path: Path) -> None:
        """含 `TOKEN` 却只是个天数的键也会被遮——这是有意的偏向，写死在这里以免被当成 bug

        实测 `.env.dev` 上就有 `NEKRO_ACCESS_TOKEN_EXPIRE_DAYS=30`。代价只是那条 DEBUG
        日志里少看见一个数字；还原之后类型与取值与没遮过时逐字相同。
        """
        env_file = _write_env(tmp_path, "NEKRO_ACCESS_TOKEN_EXPIRE_DAYS=30\n")
        shield = build_env_secret_shield([env_file])
        assert sorted(shield) == ["nekro_access_token_expire_days"]
        assert coerce_like_nonebot(shield["nekro_access_token_expire_days"].get_secret_value()) == 30

    def test_secret_env_keys_helper_matches_shield(self, tmp_path: Path) -> None:
        env_file = _write_env(tmp_path, ENV_BODY)
        assert secret_env_keys([env_file]) == sorted(build_env_secret_shield([env_file]))

    def test_describe_shield_never_prints_values(self, tmp_path: Path) -> None:
        env_file = _write_env(tmp_path, ENV_BODY)
        text = describe_shield(build_env_secret_shield([env_file]))
        assert "nekro_admin_password" in text
        for sentinel in SENTINELS:
            assert sentinel not in text

    def test_describe_shield_handles_empty(self) -> None:
        assert describe_shield({}) == "无凭据类配置项需要遮蔽"


class TestTheLoadedConfigLineIsActuallyClean:
    """打靶用真的 NoneBot `Config`，因为泄漏与修复都长在它的序列化行为上"""

    @staticmethod
    def _dump_text(config: NoneBotConfig) -> str:
        # 与 `nonebot/__init__.py:init()` 打那条 DEBUG 日志时用的是同一个表达式
        return str(model_dump(config))

    def test_without_the_shield_the_dump_really_leaks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """对照组：证明这条断言测得出差异（§2.11）

        没有这一条，"遮住了"那一条可以被任何一个不含这些键的 dump 满足。
        """
        env_file = _write_env(tmp_path, ENV_BODY)
        monkeypatch.chdir(tmp_path)
        leaked = self._dump_text(NoneBotConfig(_env_file=env_file))
        for sentinel in SENTINELS:
            assert sentinel in leaked, f"对照组没能复现泄漏，用例失去意义: {sentinel}"

    def test_with_the_shield_no_plaintext_survives(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = _write_env(tmp_path, ENV_BODY)
        monkeypatch.chdir(tmp_path)
        shield = build_env_secret_shield([env_file])
        dumped = self._dump_text(NoneBotConfig(_env_file=env_file, **shield))

        for sentinel in SENTINELS:
            assert sentinel not in dumped
        # 键本身仍然看得见——"这个部署配没配后台口令"是运维要的信息，遮的是取值
        assert "nekro_admin_password" in dumped
        assert dumped.count("SecretStr('**********')") == len(shield)
        # 非凭据项必须逐字照旧，否则这条日志就白留了
        assert "'nekro_postgres_host': '127.0.0.1'" in dumped
        assert "'log_level': 'DEBUG'" in dumped

    def test_restore_puts_the_plaintext_back(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = _write_env(tmp_path, ENV_BODY)
        monkeypatch.chdir(tmp_path)
        shield = build_env_secret_shield([env_file])
        config = NoneBotConfig(_env_file=env_file, **shield)

        restored = restore_shielded_values(config, shield)

        assert restored == sorted(shield)
        for key, secret in shield.items():
            assert getattr(config, key) == secret.get_secret_value()
            assert isinstance(getattr(config, key), str)
        # 还原之后与"从来没遮过"逐字相等
        baseline = self._dump_text(NoneBotConfig(_env_file=env_file))
        assert self._dump_text(config) == baseline

    def test_the_onebot_adapter_would_break_without_the_restore(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """把还原这一步删掉的后果，用真的适配器配置模型钉住

        `Adapter.__init__` 做的就是 `Config(**driver.config.model_dump())`，
        而 `onebot_access_token` 声明的是 `Optional[str]`。
        """
        env_file = _write_env(tmp_path, ENV_BODY)
        monkeypatch.chdir(tmp_path)
        shield = build_env_secret_shield([env_file])
        config = NoneBotConfig(_env_file=env_file, **shield)

        with pytest.raises(ValidationError):
            OneBotConfig(**model_dump(config))

        restore_shielded_values(config, shield)
        assert OneBotConfig(**model_dump(config)).onebot_access_token == "onebot-sentinel-dddd"

    def test_secret_living_only_in_the_root_dot_env_is_also_covered(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """走 `resolve_env_files()` 的全链路：`.env` 那一份漏读 = 里面的凭据照样明文出去

        `nonebot.init(_env_file=X)` 读的是 `(".env", X)` **两份**。只把 `--env=` 指定的
        那一份喂给 `build_env_secret_shield()` 时，前面这些用例全绿——因为它们本来就只
        传了一份。这条用例把两份都放上凭据，专门盯住"少读一份"这个变异。
        """
        (tmp_path / ".env").write_text("NEKRO_ROOT_ONLY_API_KEY=root-env-sentinel-eeee\n", encoding="utf-8")
        _write_env(tmp_path, ENV_BODY)
        monkeypatch.chdir(tmp_path)

        # 对照组：不遮就漏（含 `.env` 那一份）
        leaked = self._dump_text(NoneBotConfig(_env_file=(".env", ".env.dev")))
        assert "root-env-sentinel-eeee" in leaked

        shield = build_env_secret_shield(resolve_env_files(Path(".env.dev")))
        assert "nekro_root_only_api_key" in shield, "`.env` 那一份没被读进来"
        dumped = self._dump_text(NoneBotConfig(_env_file=(".env", ".env.dev"), **shield))
        for sentinel in (*SENTINELS, "root-env-sentinel-eeee"):
            assert sentinel not in dumped

    def test_numeric_secret_round_trips_to_the_same_type(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """纯数字口令：还原后必须还是 int，与没遮过时一致"""
        env_file = _write_env(tmp_path, "NEKRO_ADMIN_PASSWORD=12345678\n")
        monkeypatch.chdir(tmp_path)
        shield = build_env_secret_shield([env_file])
        config = NoneBotConfig(_env_file=env_file, **shield)
        restore_shielded_values(config, shield)

        baseline = NoneBotConfig(_env_file=env_file)
        assert getattr(config, "nekro_admin_password") == getattr(baseline, "nekro_admin_password")
        assert type(getattr(config, "nekro_admin_password")) is type(getattr(baseline, "nekro_admin_password"))


class TestTheFixIsActuallyWiredIntoRunBot:
    """零件全对而调用点没了，上面每一条都照样全绿（§2.7）

    `run_bot.py` 在 Windows 上导不进来（模块级 `signal.SIGUSR2`），所以按 AST 断言。
    """

    @staticmethod
    def _tree() -> ast.Module:
        return ast.parse(RUN_BOT_PATH.read_text(encoding="utf-8"))

    @staticmethod
    def _calls(tree: ast.Module, dotted_name: str) -> list[ast.Call]:
        def name_of(func: ast.expr) -> str:
            if isinstance(func, ast.Attribute):
                return f"{name_of(func.value)}.{func.attr}" if isinstance(func.value, (ast.Name, ast.Attribute)) else func.attr
            if isinstance(func, ast.Name):
                return func.id
            return ""

        return [node for node in ast.walk(tree) if isinstance(node, ast.Call) and name_of(node.func) == dotted_name]

    def test_shield_is_passed_into_nonebot_init(self) -> None:
        init_calls = self._calls(self._tree(), "nonebot.init")
        assert len(init_calls) == 1
        starred = [kw.value.id for kw in init_calls[0].keywords if kw.arg is None and isinstance(kw.value, ast.Name)]
        assert "env_secret_shield" in starred, "nonebot.init 没有收到掩码 kwargs，那条日志会照打明文"

    def test_restore_is_called_before_register_adapter(self) -> None:
        tree = self._tree()
        restore_calls = self._calls(tree, "restore_shielded_values")
        register_calls = self._calls(tree, "driver.register_adapter")
        assert len(restore_calls) == 1, "掩码没有还原，协议端连接会因 access_token 类型不符被拒"
        assert len(register_calls) == 1
        assert restore_calls[0].lineno < register_calls[0].lineno

    def test_shield_is_built_from_the_env_files_nonebot_will_actually_read(self) -> None:
        """光"把 env_secret_shield 传进去"是不够的——它可以是个恒为空的字典

        变异实验：把 `build_env_secret_shield(resolve_env_files(env_file), ...)` 改成
        `build_env_secret_shield([])`，上面那条"传进去了"的用例照样全绿，而那条日志
        重新打回明文。所以调用点的**实参**也要钉住：
        必须是 `resolve_env_files(env_file)`，即 `.env` 与 `--env=` 指定的那份都要读。
        """
        tree = self._tree()
        build_calls = self._calls(tree, "build_env_secret_shield")
        assert len(build_calls) == 1, "掩码字典的构造点不唯一，无法判断传给 init 的是不是它"

        args = build_calls[0].args
        assert args, "build_env_secret_shield 没有收到 env 文件，掩码必然为空"
        first = args[0]
        assert isinstance(first, ast.Call), "第一个实参不是 resolve_env_files(...) 的调用结果"
        assert self._calls(tree, "resolve_env_files"), "没有调用 resolve_env_files"
        resolved_name = first.func.id if isinstance(first.func, ast.Name) else ""
        assert resolved_name == "resolve_env_files", f"env 文件来源是 {resolved_name!r}，不是 resolve_env_files"
        assert [isinstance(a, ast.Name) and a.id == "env_file" for a in first.args] == [
            True,
        ], "resolve_env_files 收到的不是 run_bot 解析出来的 env_file"

    def test_restore_is_applied_to_the_driver_config_with_the_same_shield(self) -> None:
        """还原的两个实参也要钉住

        变异实验：把 `restore_shielded_values(driver.config, env_secret_shield)` 改成
        `restore_shielded_values(driver.config, {})`——"调用点还在、顺序也对"的那条用例
        依旧全绿，而 `onebot_access_token` 会以 `SecretStr` 留在配置里，协议端全部连不上。
        """
        call = self._calls(self._tree(), "restore_shielded_values")[0]
        assert len(call.args) == 2, "还原要的是 (driver.config, 掩码字典) 两个实参"
        target, shield_arg = call.args
        assert isinstance(target, ast.Attribute) and target.attr == "config"
        assert isinstance(target.value, ast.Name) and target.value.id == "driver"
        assert isinstance(shield_arg, ast.Name) and shield_arg.id == "env_secret_shield"

    def test_run_bot_does_not_import_the_nekro_agent_package_before_init(self) -> None:
        """提前 import `nekro_agent` 会让 `load_config_to_env()` 在没有 driver 时静默失败

        失败的形态不是报错，而是 `load_to_nonebot_env` 那批配置从此进不了 nonebot——
        所以这条边界必须有测试盯着，不能只写在注释里。
        """
        tree = self._tree()
        init_lineno = self._calls(tree, "nonebot.init")[0].lineno
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if node.lineno < init_lineno:
                assert not any(
                    name == "nekro_agent" or name.startswith("nekro_agent.") for name in names
                ), f"第 {node.lineno} 行在 nonebot.init() 之前导入了 nekro_agent"


class TestHygieneModuleStaysImportLight:
    """本模块的全部意义在于"能在 nonebot.init() 之前安全导入"，这条要有测试守着"""

    def test_it_does_not_depend_on_the_nekro_agent_package(self) -> None:
        source = Path(nekro_log_hygiene.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not any(name == "nekro_agent" or name.startswith("nekro_agent.") for name in names)

    def test_shipped_in_the_wheel_next_to_run_bot(self) -> None:
        """不进包就意味着 pip 装出来的部署里 `run_bot.py` 一启动就 ImportError"""
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert '"nekro_log_hygiene.py" = "nekro_log_hygiene.py"' in pyproject

    def test_shipped_in_the_docker_image_next_to_run_bot(self) -> None:
        """镜像是本项目的主部署形态，而它的文件清单是 dockerfile 里手写的 COPY，不是 wheel

        `run_bot.py` 里那句 `from nekro_log_hygiene import ...` 在 `try` 块**之外**：
        漏拷这个文件不是"少了个功能"，是容器一起来就 ModuleNotFoundError 直接退出。
        pyproject 的 force-include 覆盖不到这里——同一个文件必须在两个清单里都登记，
        这条用例就是那个"第二处"的看门人。
        """
        dockerfile = (REPO_ROOT / "dockerfile").read_text(encoding="utf-8")
        copy_lines = [
            line.strip()
            for line in dockerfile.splitlines()
            if line.strip().upper().startswith("COPY") and "run_bot.py" in line
        ]
        assert copy_lines, "dockerfile 里没有 COPY run_bot.py，这条用例的前提变了"
        assert any(
            "nekro_log_hygiene.py" in line for line in copy_lines
        ), f"dockerfile 拷了 run_bot.py 却没拷 nekro_log_hygiene.py：{copy_lines}"

    def test_dockerignore_does_not_swallow_it(self) -> None:
        """`.dockerignore` 里一条 `*.py` 之类的规则会让上面那条 COPY 静默拷不到东西"""
        import fnmatch

        patterns = [
            line.strip()
            for line in (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#") and not line.strip().startswith("!")
        ]
        hit = [p for p in patterns if fnmatch.fnmatch("nekro_log_hygiene.py", p)]
        assert not hit, f".dockerignore 会把 nekro_log_hygiene.py 排除掉: {hit}"


class TestRealEnvExampleIsCovered:
    """拿仓库里真的 `.env.example` 跑一遍：判据对真实部署的键集有效"""

    def test_every_credential_key_in_env_example_is_shielded(self, tmp_path: Path) -> None:
        example = REPO_ROOT / ".env.example"
        body = example.read_text(encoding="utf-8")
        # `.env.example` 里凭据项是有值的（开发默认值），照搬即可
        target = tmp_path / ".env.dev"
        target.write_text(body, encoding="utf-8")
        shielded = set(secret_env_keys([target]))

        expected = {
            "nekro_postgres_password",
            "nekro_qdrant_api_key",
            "nekro_jwt_secret_key",
            "nekro_jwt_refresh_secret_key",
            "nekro_admin_password",
        }
        assert expected <= shielded

    def test_operational_keys_from_env_example_stay_visible(self, tmp_path: Path) -> None:
        target = tmp_path / ".env.dev"
        target.write_text((REPO_ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
        shielded = set(secret_env_keys([target]))
        assert "nekro_postgres_host" not in shielded
        assert "nekro_expose_port" not in shielded
        assert "log_level" not in shielded


def test_shield_is_json_serialisable_for_diagnostics() -> None:
    """排查脚本经常要把键名 dump 出来；这里顺手钉住"键名可序列化、取值不可"""
    shield: Dict[str, SecretStr] = {"nekro_admin_password": SecretStr("x")}
    assert json.loads(json.dumps(sorted(shield))) == ["nekro_admin_password"]
