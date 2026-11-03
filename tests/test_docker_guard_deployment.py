"""硬化部署清单（compose + dockerfile）之间的一致性契约（docs/docker-guard.md）。

判据代码写得再对，边车起不来就等于没有这层。这份测试盯的是**代码之外的那两张清单**：
`docker/docker-compose-x-hardened.yml` 与 `dockerfile`，它们由不同的人在不同的时候改，
彼此没有任何编译期联系（§2.12：同一件事登记在两个清单里，只改一个 = docker 部署直接起不来）。

钉住三条本轮实际踩到 / 差点踩到的：

1. **guard 边车的解释器**：镜像里依赖装在 `/app/.venv`，`PATH` 上的 `python` 是基础镜像自带的
   系统解释器，那儿没有 aiohttp / loguru / nonebot。`command: ["python", "-m", ...]` 会在
   import 阶段 ModuleNotFoundError 退出——本地 pytest 与 lab 里手工起的 guard 全绿，
   因为都不走这个 command。
2. **NA 自己那个容器的归属标签**：归属门只放行带 `nekro.*` 标签的容器，而 NA 容器不是 NA 建的，
   没有任何代码会给它打标签。少了它，`restart_self` / `resolve_self_container`（网络隔离）/
   `_resolve_nekro_network`（NapCat 接网）三条自指路径一起被自己的 guard 挡掉。
3. **硬化的定义本身**：NA 服务不得再直挂 docker.sock，且两个服务都必须用 fork 镜像。
"""

from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
HARDENED_COMPOSE = REPO_ROOT / "docker" / "docker-compose-x-hardened.yml"
STANDARD_COMPOSE = REPO_ROOT / "docker" / "docker-compose.yml"
DOCKERFILE = REPO_ROOT / "dockerfile"

GUARD_SERVICE = "nekro_docker_guard"
APP_SERVICE = "nekro_agent"


def _load(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _services(path: Path) -> Dict[str, Any]:
    return _load(path)["services"]


def _venv_python_from_dockerfile() -> str:
    """从 dockerfile 的 CMD 里取出「镜像里真正装了依赖的那个解释器」所在目录。

    不写死 `/app/.venv/bin/python`：dockerfile 换了 venv 位置时这条测试要跟着变红，
    而不是继续断言一个已经不存在的路径（§2.9 的同源问题）。
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("CMD") and "/bin/bot" in stripped:
            start = stripped.index("/")
            end = stripped.index("/bin/bot") + len("/bin/")
            return stripped[start:end]
    pytest.fail("dockerfile 的 CMD 里找不到 venv 内的入口脚本，本用例的前提变了")


def test_guard_sidecar_uses_the_interpreter_that_has_the_dependencies():
    command: List[str] = _services(HARDENED_COMPOSE)[GUARD_SERVICE]["command"]
    assert command[1:] == ["-m", "nekro_agent.services.docker_guard"], command

    interpreter = command[0]
    venv_bin = _venv_python_from_dockerfile()
    assert interpreter.startswith("/"), (
        f"guard 边车的解释器写成了 {interpreter!r}：PATH 上的 python 是基础镜像的系统解释器，"
        f"没有 aiohttp/loguru/nonebot，边车会在 import 阶段 ModuleNotFoundError 退出。"
        f"应当用镜像里装了依赖的那个：{venv_bin}python"
    )
    assert interpreter.startswith(venv_bin), f"{interpreter!r} 不在 dockerfile 装依赖的 {venv_bin!r} 里"


def test_hardened_compose_labels_the_na_container_for_the_ownership_gate():
    """NA 有三条路径要 inspect/restart **自己这个容器**，而它不带任何 nekro.* 标签就会被归属门挡掉。"""
    labels = _services(HARDENED_COMPOSE)[APP_SERVICE].get("labels") or {}
    assert isinstance(labels, dict), f"labels 期望是映射形态，实际 {labels!r}"
    assert any(key.startswith("nekro.") for key in labels), (
        "硬化 compose 没给 nekro_agent 服务打 nekro.* 标签："
        "docker-guard 的归属门会把 restart_self / resolve_self_container（沙盒网络隔离）/"
        "_resolve_nekro_network（NapCat 接网）这三条自指调用一并挡掉"
    )


def test_hardened_compose_removes_direct_socket_mount_from_app():
    """硬化的定义：只有 guard 边车持有真实 docker.sock。"""
    services = _services(HARDENED_COMPOSE)
    app_volumes: List[str] = services[APP_SERVICE]["volumes"]
    assert not any("docker.sock" in v for v in app_volumes), f"NA 仍直挂着 docker.sock: {app_volumes}"

    guard_volumes: List[str] = services[GUARD_SERVICE]["volumes"]
    assert any("docker.sock" in v for v in guard_volumes), "guard 边车没有拿到宿主 docker.sock"

    # 对照：标准 compose 保持直挂（默认部署行为逐字不变，硬化是 opt-in）
    std_volumes: List[str] = _services(STANDARD_COMPOSE)[APP_SERVICE]["volumes"]
    assert any("docker.sock" in v for v in std_volumes), "标准 compose 的行为不该被硬化改动带走"


def test_hardened_compose_requires_fork_image_for_both_services():
    """上游镜像没有 docker_guard 模块；两个服务都必须用同一个 fork 镜像变量。"""
    services = _services(HARDENED_COMPOSE)
    for name in (GUARD_SERVICE, APP_SERVICE):
        image = services[name]["image"]
        assert image.startswith("${NEKRO_IMAGE"), f"{name} 的镜像写死成了 {image!r}"
        assert ":?" in image, f"{name} 的 NEKRO_IMAGE 没设成必填，缺变量时会起一个空镜像名"


def test_guard_module_entrypoint_exists():
    """compose 里 `-m` 指的那个模块必须真的可执行（有 __main__）。"""
    module_dir = REPO_ROOT / "nekro_agent" / "services" / "docker_guard"
    assert (module_dir / "__main__.py").is_file(), "docker_guard 缺 __main__.py，`python -m` 起不来"
