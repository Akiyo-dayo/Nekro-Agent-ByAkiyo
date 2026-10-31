"""镜像健康检查探的端口必须是进程真正 bind 的那个（§3 #48）

## 这份测试在守什么

`dockerfile` 的 `HEALTHCHECK` 从写死的 `127.0.0.1:8021` 改成了 `127.0.0.1:${PORT:-8021}`。
写死时的症状很难被当成 bug：**进程是好的、接口 200，容器却永久 `unhealthy`**，
靠 healthcheck 做滚动更新 / 负载摘除的编排会因此判死一个健康实例（PROJECT_STATE §2.19
就是被这条误导过一次）。

改动本身只有一行，而这一行**没有任何编译期或运行期信号**能证明它还在：
仓库里没有第二处代码读它，本地 `pytest` 与 `poe dev` 也都跑不到镜像形态。
把它改回写死的 8021，全量测试一条都不会红 —— §2.12「同一件事登记在两个清单里，
只改一个」那类事故的又一个入口。这份文件就是那个缺失的信号。

## 三张清单，同一件事实

「NA 进程在容器里监听哪个端口」这件事同时写在三处，彼此没有任何编译期联系：

| 清单 | 写法 | 谁读它 |
|---|---|---|
| `.env.prod` | `PORT=8021` | NoneBot 驱动（进程真正 bind 的端口）。镜像里由 `COPY .env.prod ./` 带进去 |
| `dockerfile` | `HEALTHCHECK ... ${PORT:-8021}` / `EXPOSE 8021` | 容器内的 `/bin/sh`（**读不到 `.env.prod`**，只读容器环境变量） |
| `docker/docker-compose*.yml` | `"${NEKRO_EXPOSE_PORT:-8021}:8021"` | Docker 守护进程（冒号右边那个是容器内端口） |

所以判据一律**从 `.env.prod` 现算**、不写字面量 8021：只有这样，改了 `.env.prod`
而忘了另外两张清单时才会红（反过来同理）。

## 一个仍然存在的缺口（有意不修，登记在这里）

`${PORT:-8021}` 由容器内的 shell 在运行时展开，读的是**容器环境变量**。
`docker run -e PORT=8051` 这条路径因此被修好了；但**改 `.env.prod` 里的 `PORT` 再重新
build** 这条路径没有 —— dotenv 文件不会进容器环境，shell 仍然回落到 `${PORT:-…}` 的默认值。
本文件的 `test_the_shell_fallback_equals_the_port_env_prod_binds` 就是这个缺口的看门人：
两边一致时缺口不显形，一旦有人只改一边就当场红。
（在 `dockerfile` 里加 `ENV PORT=…` 能让 shell 看见它，但那会**反向覆盖** `.env.prod`
——pydantic-settings 里环境变量优先级高于 dotenv——等于悄悄废掉 `.env.prod` 的 `PORT`
这个配置口，属于「单部署形态行为逐字不变」不允许的改动。）
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

import pytest
import yaml
from fastapi import APIRouter, FastAPI

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "dockerfile"
ENV_PROD = REPO_ROOT / ".env.prod"
APP_SERVICE = "nekro_agent"
COMPOSE_FILES: Tuple[str, ...] = (
    "docker-compose.yml",
    "docker-compose-x-napcat.yml",
    "docker-compose-x-hardened.yml",
)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _dockerfile_instructions() -> List[str]:
    """按 Docker 的续行规则把 dockerfile 折成「一条指令一行」"""
    text = DOCKERFILE.read_text(encoding="utf-8")
    joined: List[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            buffer += line[:-1].strip() + " "
            continue
        joined.append((buffer + line).strip())
        buffer = ""
    if buffer:
        joined.append(buffer.strip())
    return joined


def healthcheck_instruction() -> str:
    found = [line for line in _dockerfile_instructions() if line.upper().startswith("HEALTHCHECK")]
    if len(found) != 1:
        pytest.fail(f"dockerfile 里的 HEALTHCHECK 指令有 {len(found)} 条，本文件的前提是恰好 1 条：{found}")
    return found[0]


def healthcheck_command() -> str:
    """`HEALTHCHECK [选项...] CMD <这里>`"""
    instruction = healthcheck_instruction()
    match = re.search(r"\bCMD\b", instruction)
    if match is None:
        pytest.fail(f"HEALTHCHECK 里没有 CMD 子句，本文件的前提变了：{instruction!r}")
    return instruction[match.end() :].strip()


def probed_url() -> str:
    urls = re.findall(r"https?://[^\s\"']+", healthcheck_command())
    if len(urls) != 1:
        pytest.fail(f"HEALTHCHECK 命令里的 URL 有 {len(urls)} 个，本文件的前提是恰好 1 个：{urls}")
    return urls[0]


def env_file_values(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def env_prod_port() -> int:
    """`.env.prod` 里那个 `PORT` —— 容器里进程真正 bind 的端口"""
    raw = env_file_values(ENV_PROD).get("PORT", "")
    if not raw.isdigit():
        pytest.fail(f".env.prod 里读不到数字形态的 PORT（读到 {raw!r}），本文件的前提变了")
    return int(raw)


def compose_app_service(name: str) -> Dict[str, object]:
    parsed = yaml.safe_load((REPO_ROOT / "docker" / name).read_text(encoding="utf-8"))
    return parsed["services"][APP_SERVICE]


# ---------------------------------------------------------------------------
# 契约
# ---------------------------------------------------------------------------


class TestHealthcheckFollowsTheListeningPort:
    """把 `${PORT:-…}` 改回写死的端口，这一组要红"""

    def test_the_probe_url_is_derived_from_the_port_variable(self) -> None:
        """URL 里的端口必须是 `${PORT:-<默认>}`，不能是任何写死的数字

        写死的后果不是「探错一个端口」，而是**进程好好的、容器永久 unhealthy**：
        编排会把一个健康实例摘掉，而 NA 侧一条错误日志都没有。
        """
        url = probed_url()
        assert re.fullmatch(r"http://127\.0\.0\.1:\$\{PORT:-\d+\}/api/health", url), (
            f"HEALTHCHECK 探的是 {url!r}。它必须逐字形如 "
            f"http://127.0.0.1:${{PORT:-<默认端口>}}/api/health："
            f"`PORT` 是 NoneBot 驱动的监听端口，`NEKRO_EXPOSE_PORT` 只改宿主侧映射，不是它"
        )

    def test_the_healthcheck_stays_in_shell_form(self) -> None:
        """必须是 shell 形态：exec 形态（JSON 数组）里 `${PORT:-…}` **不会被展开**

        Docker 对 exec 形态不起 shell，`${PORT:-8021}` 会被原样当成主机名的一部分，
        于是每一次探测都是 DNS 失败 —— 症状与写死端口时一模一样，而且更难看出来。
        """
        command = healthcheck_command()
        assert not command.startswith("["), (
            f"HEALTHCHECK 被写成了 exec 形态：{command!r}。"
            f"`${{PORT:-…}}` 要由容器内的 /bin/sh 在运行时展开，exec 形态不起 shell"
        )

    def test_the_probe_fails_on_http_errors(self) -> None:
        """`curl -f`：少了它，HTTP 500 也会让 curl 退 0，healthcheck 就永远是 healthy"""
        command = healthcheck_command()
        assert re.search(r"(?:^|\s)(?:-f|--fail)(?:\s|$)", command), (
            f"HEALTHCHECK 的 curl 没带 -f/--fail：{command!r}。"
            f"不带时 curl 对 4xx/5xx 一样退 0，这个探针会永远报 healthy"
        )


class TestTheThreeListsAgreeOnTheContainerPort:
    """`.env.prod` / `dockerfile` / compose 三处说的是同一个端口

    三张清单没有任何编译期联系，判据一律从 `.env.prod` 现算（§2.9）。
    """

    def test_the_shell_fallback_equals_the_port_env_prod_binds(self) -> None:
        """shell 兜底值 = `.env.prod` 的 `PORT`

        `${PORT:-N}` 里的 N 是**没有环境变量时**探针会去打的端口，而没有环境变量时
        进程 bind 的正是 `.env.prod` 里那个值（镜像里由 `COPY .env.prod ./` 带进去）。
        两者一旦分叉，`docker build` 出来的镜像**一起来就是 unhealthy**，
        而 `.env.prod` 不会进容器环境变量，shell 这边永远看不到它的改动。
        """
        match = re.search(r"\$\{PORT:-(\d+)\}", probed_url())
        assert match is not None, "URL 里没有 ${PORT:-<默认>}，上一组已经覆盖这条"
        assert int(match.group(1)) == env_prod_port(), (
            f"HEALTHCHECK 的兜底端口是 {match.group(1)}，而 .env.prod 里 PORT={env_prod_port()}。"
            f"dotenv 不会进容器环境变量，探针只认兜底值 —— 两边分叉 = 镜像一起来就 unhealthy"
        )

    def test_expose_matches_the_port_the_process_binds(self) -> None:
        exposed = [line.split(None, 1)[1].strip() for line in _dockerfile_instructions() if line.upper().startswith("EXPOSE ")]
        assert exposed == [str(env_prod_port())], (
            f"dockerfile 的 EXPOSE 是 {exposed}，而 .env.prod 里 PORT={env_prod_port()}"
        )

    @pytest.mark.parametrize("compose_name", COMPOSE_FILES)
    def test_compose_publishes_the_port_the_process_binds(self, compose_name: str) -> None:
        """compose 端口映射冒号右边那个是**容器内**端口，它同样得等于 `.env.prod` 的 `PORT`

        左边是 `NEKRO_EXPOSE_PORT`（宿主侧，多部署时各不相同，与本条无关）。
        """
        ports = compose_app_service(compose_name).get("ports") or []
        container_ports = [str(entry).rsplit(":", 1)[-1].split("/")[0] for entry in ports]
        assert container_ports == [str(env_prod_port())], (
            f"docker/{compose_name} 的 {APP_SERVICE} 发布的容器内端口是 {container_ports}，"
            f"而 .env.prod 里 PORT={env_prod_port()}：映射到一个没人监听的端口 = 后台整个打不开"
        )


class TestTheProbedPathIsRealAndOpen:
    """探针打的那条路径必须真的存在、且不要鉴权

    零件对了不等于 URL 是真的：路径被改名 / 被挂上鉴权依赖时，探针会稳定拿到 404 / 401，
    表现同样是「进程好的、容器 unhealthy」。这条不看源码文本，直接看挂载后的路由表。
    """

    @staticmethod
    def _mounted_routes(monkeypatch: pytest.MonkeyPatch) -> Dict[str, object]:
        from nekro_agent import routers as routers_module

        # `load_adapters_api()` 会 clear/重填全局 `loaded_adapters`，在测试里调它会污染别的用例；
        # 协议端路由与本条无关，换成空 router
        monkeypatch.setattr(routers_module, "load_adapters_api", lambda: APIRouter())
        app = FastAPI()
        routers_module.mount_api_routes(app)
        return {str(getattr(route, "path", "")): route for route in app.routes}

    def test_the_health_endpoint_is_mounted_where_the_probe_looks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        path = probed_url().split("}", 1)[-1]
        routes = self._mounted_routes(monkeypatch)
        assert path in routes, (
            f"HEALTHCHECK 打的是 {path}，但挂载后的路由表里没有这条路径。"
            f"探针会稳定拿到 404，容器永久 unhealthy 而进程其实是好的"
        )
        assert "GET" in getattr(routes[path], "methods", set())

    def test_the_health_endpoint_needs_no_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """容器里的探针没有令牌可用；这条路径一旦挂上鉴权依赖，healthcheck 就恒 401"""
        route = self._mounted_routes(monkeypatch)[probed_url().split("}", 1)[-1]]
        dependencies = list(getattr(getattr(route, "dependant", None), "dependencies", []))
        assert dependencies == [], (
            f"/api/health 上挂了 {[getattr(d, 'call', d) for d in dependencies]}："
            f"容器内的 healthcheck 拿不到任何凭据，带鉴权 = 探针恒 401"
        )
