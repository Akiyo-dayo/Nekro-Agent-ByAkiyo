"""#47 在 Docker 下落地：每份定义 NA 服务的 compose 都要把 `NEKRO_SELF_HOSTNAME` 传进容器

## 缺陷是什么

此前给 #47 加了 `NEKRO_SELF_HOSTNAME`（应用层三级回落，见 `tests/test_self_hostname_resolution.py`），
文档让运维把它写进 `docker/.env`。但 compose 的 `environment:` 是**显式白名单**、`nekro_agent`
服务也没有 `env_file`，于是写进 `.env` 的值**进不了容器**——应用层照旧回落到
`NEKRO_INSTANCE_NAME`，#47 的修复在 Docker 部署（主力形态）里等于没做
（以前出过同一种形态：`.env.example` 登记了不等于容器里有）。

## 修法与这里钉的东西

每份定义 NA 服务的 compose 都加一行

    - NEKRO_SELF_HOSTNAME=${NEKRO_SELF_HOSTNAME:-${INSTANCE_NAME:-}nekro_agent}

默认值就是该文件里 NA 的 `container_name`，所以运维零配置：设了 `INSTANCE_NAME=na_a_`，
容器名是 `na_a_nekro_agent`，反连主机名也是 `na_a_nekro_agent`。想显式覆盖仍可在 `.env` 里写。

1. 每份定义 `nekro_agent` 服务的 compose 都含这一行（逐字）；
2. 这一行的**默认值在 Compose 插值之后**逐场景等于该文件 `container_name` 插值之后的结果
   ——不是比字符串，因为 `docker-compose.yml` 的 `container_name` 写的是 `${INSTANCE_NAME}`
   而另两份写的是 `${INSTANCE_NAME:-}`，字面不同、语义相同，比字面会误报；
3. 一份 compose 若根本没有 `nekro_agent` 服务（dev 文件），要被**明确登记**为豁免，
   并断言它真的没有——哪天有人往里加了 NA 服务，豁免项会先红；
4. `.env.prod` 里**必须没有**这个键：它被 COPY 进镜像，`run_bot.py` 会把其中 `NEKRO_` 前缀的键
   无条件覆盖进 `os.environ`，一行空值就会抹掉 compose 传进来的值（以前踩过同一个坑）；
5. 判据本身带反向对照：删掉那行、默认值改错、`container_name` 改掉，判据都必须报警。
"""

import re
from pathlib import Path
from typing import Dict, List, Optional

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
DOCKER_DIR = REPO_ROOT / "docker"

SELF_HOSTNAME_KEY = "NEKRO_SELF_HOSTNAME"
EXPECTED_LINE = "NEKRO_SELF_HOSTNAME=${NEKRO_SELF_HOSTNAME:-${INSTANCE_NAME:-}nekro_agent}"
NA_SERVICE = "nekro_agent"

# 不定义 NA 服务的 compose：开发环境只起依赖，主应用走源码。登记在这里是为了让
# 「它没有 NA 服务」成为一条被断言的事实，而不是一个没人复核的默认放行。
COMPOSE_WITHOUT_NA_SERVICE = {"docker-compose.dev.yml"}

# 判据跑过的场景：INSTANCE_NAME 未设 / 空 / 有前缀；没有任何一格显式设 SELF_HOSTNAME
SCENARIOS: List[Dict[str, str]] = [
    {},
    {"INSTANCE_NAME": ""},
    {"INSTANCE_NAME": "na_a_"},
    {"INSTANCE_NAME": "prod-2_"},
]


def interpolate(template: str, env: Dict[str, str]) -> str:
    """Compose 的变量插值：`${VAR}`、`${VAR:-default}`，默认值里可以嵌套

    `:-` 的语义是「未设置**或为空**就用默认值」——与 Compose 一致，这也是 `.env` 里写了
    `NEKRO_SELF_HOSTNAME=`（空）时仍落到默认值的依据。
    """
    out: List[str] = []
    i = 0
    while i < len(template):
        if template.startswith("${", i):
            depth = 1
            j = i + 2
            while j < len(template) and depth:
                if template.startswith("${", j):
                    depth += 1
                    j += 2
                    continue
                if template[j] == "}":
                    depth -= 1
                j += 1
            assert depth == 0, f"插值表达式没有闭合: {template!r}"
            inner = template[i + 2 : j - 1]
            name, sep, default = inner.partition(":-")
            value = env.get(name, "")
            out.append(value if value else (interpolate(default, env) if sep else ""))
            i = j
        else:
            out.append(template[i])
            i += 1
    return "".join(out)


def _na_service(compose: Dict[str, object]) -> Optional[Dict[str, object]]:
    services = compose.get("services")
    assert isinstance(services, dict)
    service = services.get(NA_SERVICE)
    return service if isinstance(service, dict) else None


def check_compose_text(text: str) -> List[str]:
    """判据：一份 compose 文本里，NA 服务是否正确地传了 `NEKRO_SELF_HOSTNAME`

    返回违规描述列表；没有 NA 服务的文件由调用方决定是否豁免（这里返回空）。
    """
    compose = yaml.safe_load(text)
    service = _na_service(compose)
    if service is None:
        return []
    problems: List[str] = []
    environment = service.get("environment")
    assert isinstance(environment, list), "NA 服务的 environment 应为列表形态"
    lines = [str(item) for item in environment if str(item).startswith(f"{SELF_HOSTNAME_KEY}=")]
    if len(lines) != 1:
        return [f"NA 服务的 environment 里应恰好有一行 {SELF_HOSTNAME_KEY}=，实际 {len(lines)} 行"]
    line = lines[0]
    if line != EXPECTED_LINE:
        problems.append(f"该行应逐字为 {EXPECTED_LINE!r}，实际 {line!r}")

    container_name = service.get("container_name")
    if not isinstance(container_name, str):
        return [*problems, "NA 服务没有 container_name，无法核对默认值与容器名一致"]
    value_template = line.partition("=")[2]
    for env in SCENARIOS:
        hostname = interpolate(value_template, env)
        actual_name = interpolate(container_name, env)
        if hostname != actual_name:
            problems.append(
                f"INSTANCE_NAME={env.get('INSTANCE_NAME', '<未设>')!r} 时反连主机名 {hostname!r}"
                f" ≠ 容器名 {actual_name!r}（Docker 内嵌 DNS 解析不到，协议端连不上）",
            )
    # 显式覆盖仍然生效（运维在 .env 里写了值）
    explicit = interpolate(value_template, {"NEKRO_SELF_HOSTNAME": "custom-host", "INSTANCE_NAME": "x_"})
    if explicit != "custom-host":
        problems.append(f"显式配置 NEKRO_SELF_HOSTNAME 时应优先于默认值，实际得到 {explicit!r}")
    return problems


def _compose_files() -> List[Path]:
    return sorted(DOCKER_DIR.glob("docker-compose*.yml"))


class TestEveryNaComposeForwardsSelfHostname:
    def test_the_compose_files_are_actually_discovered(self) -> None:
        names = {path.name for path in _compose_files()}
        # 非空对照：目前仓库里就是这四份；少于它说明 glob 失效，多出来的会被下一条用例一并核查
        assert {"docker-compose.yml", "docker-compose-x-napcat.yml", "docker-compose-x-hardened.yml"} <= names
        assert COMPOSE_WITHOUT_NA_SERVICE <= names

    @pytest.mark.parametrize("path", _compose_files(), ids=lambda p: p.name)
    def test_na_service_forwards_it_with_a_default_equal_to_the_container_name(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        if path.name in COMPOSE_WITHOUT_NA_SERVICE:
            assert _na_service(yaml.safe_load(text)) is None, (
                f"{path.name} 登记为「没有 NA 服务」，但现在有了——请从豁免清单里去掉，并让它传 {SELF_HOSTNAME_KEY}"
            )
            return
        assert _na_service(yaml.safe_load(text)) is not None, f"{path.name} 没有 {NA_SERVICE} 服务，也不在豁免清单里"
        assert check_compose_text(text) == []

    @pytest.mark.parametrize("path", _compose_files(), ids=lambda p: p.name)
    def test_no_service_sets_instance_name_without_being_the_na_service(self, path: Path) -> None:
        """任何传了 `NEKRO_INSTANCE_NAME` 的服务都必须叫 `nekro_agent`——否则它是一个被改名的 NA 服务，
        上面按服务名找的判据会对它视而不见"""
        compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        services = compose["services"]
        for name, service in services.items():
            environment = service.get("environment") or []
            if isinstance(environment, dict):
                environment = [f"{k}={v}" for k, v in environment.items()]
            sets_it = any(str(item).startswith("NEKRO_INSTANCE_NAME=") for item in environment)
            assert not sets_it or name == NA_SERVICE, (
                f"{path.name} 的服务 {name} 传了 NEKRO_INSTANCE_NAME，却不叫 {NA_SERVICE}"
            )


class TestCriterionHasTeeth:
    """反向对照：把正确的 compose 改坏，判据必须报警；没改坏时必须干净"""

    def _text(self, name: str = "docker-compose.yml") -> str:
        return (DOCKER_DIR / name).read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "name", ["docker-compose.yml", "docker-compose-x-napcat.yml", "docker-compose-x-hardened.yml"]
    )
    def test_untouched_files_are_clean(self, name: str) -> None:
        assert check_compose_text(self._text(name)) == []

    @pytest.mark.parametrize(
        "name", ["docker-compose.yml", "docker-compose-x-napcat.yml", "docker-compose-x-hardened.yml"]
    )
    def test_removing_the_line_is_caught(self, name: str) -> None:
        text = self._text(name)
        assert EXPECTED_LINE in text
        broken = re.sub(rf"^\s*- {re.escape(EXPECTED_LINE)}\n", "", text, flags=re.MULTILINE)
        assert broken != text
        problems = check_compose_text(broken)
        assert problems and "恰好有一行" in problems[0]

    def test_a_default_that_disagrees_with_the_container_name_is_caught(self) -> None:
        text = self._text()
        broken = text.replace(EXPECTED_LINE, "NEKRO_SELF_HOSTNAME=${NEKRO_SELF_HOSTNAME:-nekro_agent}")
        assert broken != text
        problems = check_compose_text(broken)
        # 单看字面这一行「看起来也对」，只有带前缀的场景才会暴露它——所以判据必须逐场景插值
        assert any("na_a_" in p for p in problems)

    def test_a_default_that_ignores_the_prefix_the_other_way_round_is_caught(self) -> None:
        """另一个方向：容器名被改掉而这一行没跟着改"""
        text = self._text("docker-compose-x-hardened.yml")
        broken = text.replace(
            "container_name: ${INSTANCE_NAME:-}nekro_agent", "container_name: ${INSTANCE_NAME:-}na_core"
        )
        assert broken != text
        assert check_compose_text(broken)

    def test_an_explicit_override_that_cannot_win_is_caught(self) -> None:
        text = self._text()
        broken = text.replace(EXPECTED_LINE, "NEKRO_SELF_HOSTNAME=${INSTANCE_NAME:-}nekro_agent")
        assert broken != text
        problems = check_compose_text(broken)
        assert any("优先于默认值" in p for p in problems)

    def test_interpolator_matches_compose_semantics(self) -> None:
        """判据依赖的插值器自己的行为：未设与空都落默认值，默认值可嵌套，无默认时为空串"""
        template = "${A:-${B:-}tail}"
        assert interpolate(template, {}) == "tail"
        assert interpolate(template, {"B": "p_"}) == "p_tail"
        assert interpolate(template, {"A": "x", "B": "p_"}) == "x"
        assert interpolate(template, {"A": ""}) == "tail"  # 空 = 未设（`:-` 语义）
        assert interpolate("${B}nekro_agent", {}) == "nekro_agent"
        assert interpolate("${B}nekro_agent", {"B": "p_"}) == "p_nekro_agent"


class TestEnvProdMustNotCarryIt:
    def test_env_prod_has_no_self_hostname_key(self) -> None:
        """`.env.prod` 被 COPY 进镜像，run_bot.py 会把其中 `NEKRO_` 前缀的键无条件覆盖进环境，
        一行（哪怕是空值）就会抹掉 compose 传进来的值 —— 所以这个键在那份文件里必须缺席"""
        env_prod = (REPO_ROOT / ".env.prod").read_text(encoding="utf-8")
        assert SELF_HOSTNAME_KEY not in env_prod

    def test_the_reason_is_still_true_run_bot_overrides_nekro_keys_unconditionally(self) -> None:
        """只钉结论的话，哪天 run_bot 改了行为，这条禁令会变成没人敢动的迷信——
        把理由一起钉住：run_bot 仍是把环境文件（`.env.{env}`，镜像里是 `.env.prod`）里
        `NEKRO_` 前缀的键无条件写进 os.environ"""
        source = (REPO_ROOT / "run_bot.py").read_text(encoding="utf-8")
        assert 'key.startswith("NEKRO_")' in source
        assert "os.environ[key] = value" in source
        assert 'Path(f".env.{args.env}")' in source


class TestOperatorDocsMentionIt:
    def test_docker_env_example_documents_the_key_without_setting_it(self) -> None:
        """`docker/.env.example` 要让运维知道有这个开关，但不能作为一个空值被 install.sh 拷成 `.env` 里的活值
        （空值虽然会落默认值，但没必要让每个部署都带一行无意义的配置）——所以只能以注释形态出现"""
        text = (DOCKER_DIR / ".env.example").read_text(encoding="utf-8")
        assert SELF_HOSTNAME_KEY in text
        active = [line for line in text.splitlines() if line.startswith(f"{SELF_HOSTNAME_KEY}=")]
        assert active == []
