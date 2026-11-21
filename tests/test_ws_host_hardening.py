"""CC 工作区与沙盒共享目录的宿主侧加固：四个口子各钉一组

1. 启动 / 重启时 `.claude_home` 能被换成链接：Docker 挂 bind 源时会顺着链接走（测试部署上
   实测过），容器就能把宿主任意目录以读写方式挂进下一代容器。现在启动一律「先停、再修、再
   核对、再启动」，bind 源按字面路径、不 `resolve()`。
2. 空间清理按路径删：收集之后、删除之前，沙盒把中间某一级目录换成链接，NA 就会删掉宿主文件。
3. 读容器文件没有上限、递归没有深度上限：稀疏文件撑爆内存或磁盘，嵌套目录撞上递归上限。
4. 判「在不在沙盒共享目录下」的两种写法对不上时，退回会跟随链接的普通 `open()`。

建不了符号链接的机器（Windows 没开开发者模式）上，用到链接的用例跳过。目录句柄那条路
（Linux）的专属用例在 `test_ws_host_hardening_linux.py`。
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest
from aiodocker.exceptions import DockerError
from tortoise import Tortoise, timezone

from nekro_agent.core import os_env as os_env_module
from nekro_agent.core.config import config
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.services.workspace import container as container_module
from nekro_agent.services.workspace import manager as manager_module
from nekro_agent.services.workspace import safe_fs
from nekro_agent.services.workspace.container import SandboxContainerManager
from nekro_agent.services.workspace.manager import WorkspaceService
from nekro_agent.services.workspace.network_guard import WorkspaceNetworkPlan

SECRET = "platform-" + "key-hardening"


def _link(link: Path, target: Path, *, is_dir: bool) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("这台机器上建不了符号链接")


def _kind(path: Path) -> str:
    if path.is_symlink():
        return "link"
    if path.is_dir():
        return "dir"
    return "missing" if not path.exists() else "other"


@pytest.fixture
def logs() -> List[str]:
    from loguru import logger

    messages: List[str] = []
    sink_id = logger.add(lambda message: messages.append(message.record["message"]), level="DEBUG")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


# ---------------------------------------------------------------------------
# 1. `.claude_home` 与 bind 源
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """工作区根目录、一个宿主上的「别处」目录（里面放着不该被挂进容器的东西）"""
    root = tmp_path / "workspaces"
    root.mkdir()
    outside = tmp_path / "host-elsewhere"
    outside.mkdir()
    (outside / "nekro-agent.yaml").write_text(SECRET, encoding="utf-8")
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(OsEnv, "RUN_IN_DOCKER", False)
    monkeypatch.setattr(manager_module, "WORKSPACE_ROOT_DIR", str(root))
    monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", False)
    return SimpleNamespace(root=root, outside=outside, tmp=tmp_path)


def _mounts_of(binds: List[str]) -> List[Dict[str, Any]]:
    """Docker inspect 的 `Mounts`：bind 源按字面记（测试部署上实测，与 `HostConfig.Binds` 同一写法）"""
    mounts = []
    for bind in binds:
        source, destination, mode = bind.rsplit(":", 2)
        mounts.append({"Type": "bind", "Source": source, "Destination": destination, "RW": mode == "rw"})
    return mounts


class _Journal:
    """替身 Docker：按名字存容器；流水里记发生了什么、启动那一刻 `.claude_home` 是什么"""

    def __init__(self, claude_home: Path) -> None:
        self.events: List[str] = []
        self.kind_at_mount: List[str] = []
        self.configs: List[Dict[str, Any]] = []
        self.claude_home = claude_home
        self.containers: Dict[str, _Container] = {}
        self.undeletable: set = set()
        self.broken_inspect: set = set()

    def add(
        self,
        name: str,
        *,
        binds: List[str],
        labels: Optional[Dict[str, str]] = None,
        running: bool = True,
        extra_mounts: Optional[List[Dict[str, Any]]] = None,
        host_config: Optional[Dict[str, Any]] = None,
    ) -> "_Container":
        container = _Container(self, name, binds, labels or {}, running, extra_mounts or [])
        container.host_config = dict(host_config or {})
        self.containers[name] = container
        return container


class _Container:
    def __init__(
        self,
        journal: _Journal,
        name: str,
        binds: List[str],
        labels: Dict[str, str],
        running: bool,
        extra_mounts: List[Dict[str, Any]],
    ) -> None:
        self._journal = journal
        self.name = name
        self.binds = list(binds)
        self.labels = dict(labels)
        self.running = running
        self.extra_mounts = list(extra_mounts)
        self.host_config: Dict[str, Any] = {}

    @property
    def _container(self) -> Dict[str, Any]:
        """`containers.list()` 的那份摘要（aiodocker 放在 `_container` 上）"""
        return {
            "Id": self.name * 2,
            "Names": [f"/{self.name}"],
            "Labels": self.labels,
            "State": "running" if self.running else "exited",
            "Mounts": _mounts_of(self.binds) + self.extra_mounts,
        }

    async def show(self) -> Dict[str, Any]:
        return {
            "Id": "a" * 64,
            "Name": f"/{self.name}",
            "State": {"Running": self.running},
            "HostConfig": {"Binds": list(self.binds), **self.host_config},
            "Mounts": _mounts_of(self.binds) + self.extra_mounts,
            "NetworkSettings": {"Networks": {}},
        }

    async def stop(self, t: int = 10) -> None:
        self._journal.events.append(f"stop:{self.name}")
        self.running = False

    async def kill(self) -> None:
        self._journal.events.append(f"kill:{self.name}")
        self.running = False

    async def start(self) -> None:
        # Docker 在这一刻按 bind 源的路径挂载：记下那时 `.claude_home` 是什么
        self._journal.events.append(f"start:{self.name}")
        self._journal.kind_at_mount.append(_kind(self._journal.claude_home))
        self.running = True

    async def restart(self, t: int = 10) -> None:
        self._journal.events.append(f"restart:{self.name}")
        self._journal.kind_at_mount.append(_kind(self._journal.claude_home))

    async def delete(self, force: bool = False) -> None:
        self._journal.events.append(f"delete:{self.name}")
        if self.name in self._journal.undeletable:
            raise DockerError(500, {"message": "device or resource busy"})
        self._journal.containers.pop(self.name, None)


class _Containers:
    def __init__(self, journal: _Journal) -> None:
        self._journal = journal

    async def get(self, name: str) -> _Container:
        if name in self._journal.broken_inspect:
            raise DockerError(500, {"message": "Internal Server Error"})
        try:
            return self._journal.containers[name]
        except KeyError:
            raise DockerError(404, {"message": f"No such container: {name}"}) from None

    def container(self, name: str) -> _Container:
        """按名字拿句柄、不 inspect（aiodocker 的 `containers.container`）"""
        return self._journal.containers[name]

    async def list(self, **_kwargs: Any) -> List[_Container]:
        return list(self._journal.containers.values())

    async def create_or_replace(self, *, name: str, config: Dict[str, Any]) -> _Container:
        self._journal.configs.append(config)
        self._journal.events.append("create")
        host_config = {key: config["HostConfig"][key] for key in ("CapDrop", "SecurityOpt") if key in config["HostConfig"]}
        return self._journal.add(
            name,
            binds=config["HostConfig"]["Binds"],
            labels=config.get("Labels"),
            running=False,
            host_config=host_config,
        )


class _Docker:
    def __init__(self, journal: _Journal) -> None:
        self.containers = _Containers(journal)

    async def close(self) -> None:
        return None


def _labels(workspace_id: int, *, scope: Optional[str] = None) -> Dict[str, str]:
    return {
        "nekro.owner.scope": scope if scope is not None else container_module.deployment_scope(),
        "nekro.workspace.id": str(workspace_id),
    }


def _expected(workspace_id: int) -> List[str]:
    """这个工作区旧布局容器（home 还在工作区目录里）应有的挂载（字面路径），在测试里自己算，不借被测代码的函数"""
    ws_dir = os.path.abspath(str(Path(manager_module.WORKSPACE_ROOT_DIR) / str(workspace_id)))
    binds = [f"{ws_dir}:/workspace:rw", f"{ws_dir}{os.sep}.claude_home:/home/appuser/.claude:rw"]
    if Path("/etc/localtime").exists():
        binds.append("/etc/localtime:/etc/localtime:ro")
    return binds


def _new_home(workspace_id: int) -> Path:
    """建容器时 home 迁到的新位置（工作区目录之外），在测试里自己拼"""
    return Path(manager_module.WORKSPACE_ROOT_DIR) / ".nk-homes" / str(workspace_id) / "claude"


def _expected_new(workspace_id: int) -> List[str]:
    """新布局容器应有的挂载（字面路径）"""
    ws_dir = os.path.abspath(str(Path(manager_module.WORKSPACE_ROOT_DIR) / str(workspace_id)))
    binds = [f"{ws_dir}:/workspace:rw", f"{os.path.abspath(str(_new_home(workspace_id)))}:/home/appuser/.claude:rw"]
    if Path("/etc/localtime").exists():
        binds.append("/etc/localtime:/etc/localtime:ro")
    return binds


async def _until(predicate: Any, *, seconds: float = 10.0) -> None:
    """等到条件成立，按真实时间设上限（不会把用例卡死）

    不能只按「让出事件循环多少次」算：启动路径里有 `asyncio.to_thread` 的文件操作和真数据库的往返，
    要的是真实时间。在 CI（Linux + PG）上只让出一万次事件循环，条件偶尔还没成立就判失败。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("等的条件一直没有成立")
        await asyncio.sleep(0.005)


def _raised(excinfo: pytest.ExceptionInfo, name: str) -> None:
    """按异常类名判（改动前没有这些类，按类名判才是行为层面的失败，而不是取属性出错）"""
    assert type(excinfo.value).__name__ == name, repr(excinfo.value)


def _patch_docker(monkeypatch: pytest.MonkeyPatch, journal: _Journal, *, on_init: Any = None) -> None:
    """把 Docker、网络、配置写入与健康检查都换成替身；`on_init` 在「修复配置」那一步被调用"""

    async def no_preset(_workspace: DBWorkspace) -> None:
        return None

    async def image_exists(_image: str) -> bool:
        return True

    async def healthy(_endpoint: str, _timeout: int = 60) -> bool:
        return True

    async def relay(_docker: Any, _name: str) -> Tuple[str, str]:
        return "", "172.29.0.1"

    async def prepare(_docker: Any, _group: str) -> WorkspaceNetworkPlan:
        return WorkspaceNetworkPlan(network_mode="nekro-cc-test", isolated=True)

    async def group(_workspace: DBWorkspace) -> str:
        return "platform"

    async def init(workspace: DBWorkspace, **_kwargs: Any) -> None:
        journal.events.append("init")
        if on_init is not None:
            on_init(workspace)

    async def nothing(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def not_exists(_docker: Any, _name: str) -> bool:
        return False

    async def free_port() -> int:
        return 18080

    monkeypatch.setattr(container_module.aiodocker, "Docker", lambda: _Docker(journal))
    monkeypatch.setattr(SandboxContainerManager, "_resolve_preset", no_preset)
    monkeypatch.setattr(SandboxContainerManager, "check_image_exists", image_exists)
    monkeypatch.setattr(SandboxContainerManager, "_wait_healthy", healthy)
    monkeypatch.setattr(container_module, "resolve_container_relay_addresses", relay)
    monkeypatch.setattr(container_module, "prepare_workspace_network", prepare)
    monkeypatch.setattr(container_module, "resolve_workspace_network_group", group)
    monkeypatch.setattr(container_module, "remove_nk_ssh_files", nothing)
    monkeypatch.setattr(container_module, "_container_exists", not_exists)
    monkeypatch.setattr(container_module, "_find_free_port", free_port)
    monkeypatch.setattr(WorkspaceService, "init_workspace_dir", init)


async def _workspace(container_name: Optional[str] = "cc-old", *, status: str = "active") -> DBWorkspace:
    return await DBWorkspace.create(
        name="ws",
        description="ws",
        status=status,
        container_name=container_name,
        host_port=18080,
    )


async def _status(workspace: DBWorkspace) -> Tuple[str, Optional[str], Optional[str]]:
    row = await DBWorkspace.get(id=workspace.id)
    return row.status, row.last_error, row.container_name


def _swap_claude_home_for_link(host: SimpleNamespace, workspace: DBWorkspace) -> None:
    """模拟容器在「修好配置」之后、挂载之前把 `.claude_home` 换成指向宿主别处的链接"""
    claude_home = host.root / str(workspace.id) / ".claude_home"
    if claude_home.is_symlink() or claude_home.is_file():
        claude_home.unlink()
    elif claude_home.exists():
        claude_home.rename(claude_home.with_name(".claude_home.moved"))
    _link(claude_home, host.outside, is_dir=True)


async def _restartable(host: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, **patch: Any) -> Tuple[DBWorkspace, _Journal]:
    """一个 active 的工作区，库里记的容器 `cc-old` 在跑、挂载与预期一致"""
    workspace = await _workspace()
    (host.root / str(workspace.id) / ".claude_home").mkdir(parents=True)
    journal = _Journal(host.root / str(workspace.id) / ".claude_home")
    journal.add("cc-old", binds=_expected(workspace.id), labels=_labels(workspace.id))
    _patch_docker(monkeypatch, journal, **patch)
    return workspace, journal


class TestRestart:
    async def test_restart_stops_before_repairing_and_starts_after(self, db, host, monkeypatch) -> None:
        """不再用 Docker 的 restart：修复必须发生在容器停下来以后"""
        del db
        workspace, journal = await _restartable(host, monkeypatch)

        await SandboxContainerManager.restart(workspace)

        assert journal.events == ["stop:cc-old", "init", "start:cc-old"]
        assert (await _status(workspace))[0] == "active"

    async def test_a_link_put_in_after_the_repair_is_never_mounted(self, db, host, monkeypatch, logs) -> None:
        """修完配置那一刻 `.claude_home` 又被换成了链接：挂载时它必须已经是普通目录，链接那头一点没动"""
        del db, logs
        workspace, journal = await _restartable(
            host, monkeypatch, on_init=lambda ws: _swap_claude_home_for_link(host, ws)
        )

        await SandboxContainerManager.restart(workspace)

        assert journal.kind_at_mount == ["dir"]
        assert not journal.claude_home.is_symlink()
        assert (host.outside / "nekro-agent.yaml").read_text(encoding="utf-8") == SECRET

    async def test_a_link_found_after_stopping_is_reported(self, db, host, monkeypatch, logs) -> None:
        """容器停下来时 `.claude_home` 已经是链接：记一条 ERROR（安全事件），然后修好再启动"""
        del db
        workspace = await _workspace()
        _link(host.root / str(workspace.id) / ".claude_home", host.outside, is_dir=True)
        journal = _Journal(host.root / str(workspace.id) / ".claude_home")
        journal.add("cc-old", binds=_expected(workspace.id), labels=_labels(workspace.id))
        _patch_docker(monkeypatch, journal)

        await SandboxContainerManager.restart(workspace)

        assert journal.kind_at_mount == ["dir"]
        assert any(".claude_home 被换成了 link" in message for message in logs)
        assert sorted(p.name for p in host.outside.iterdir()) == ["nekro-agent.yaml"]

    async def test_a_container_whose_mount_points_elsewhere_is_not_restarted(self, db, host, monkeypatch, logs) -> None:
        """早先按 `resolve()` 建的容器把宿主别处的目录固化进了挂载：重启照挂不误，所以拒绝重启、要求重建"""
        del db
        workspace, journal = await _restartable(host, monkeypatch)
        ws_dir = os.path.abspath(str(host.root / str(workspace.id)))
        journal.containers["cc-old"].binds = [
            f"{ws_dir}:/workspace:rw",
            f"{host.outside}:/home/appuser/.claude:rw",
        ]

        with pytest.raises(container_module.UnsafeBindSourceError):
            await SandboxContainerManager.restart(workspace)

        assert not any(event.startswith("start:") for event in journal.events)
        assert journal.containers["cc-old"].running is False
        status, last_error, _ = await _status(workspace)
        assert status == "failed"
        assert last_error is not None and "重建" in last_error
        assert any("挂载与预期不一致" in message for message in logs)

    async def test_an_extra_mount_outside_binds_is_also_refused(self, db, host, monkeypatch) -> None:
        """`Binds` 对得上、`Mounts` 里却多一条（经 `HostConfig.Mounts` 声明的）：同样拒绝"""
        del db
        workspace, journal = await _restartable(host, monkeypatch)
        journal.containers["cc-old"].extra_mounts = [
            {"Type": "bind", "Source": str(host.outside), "Destination": "/mnt/host", "RW": True},
        ]

        with pytest.raises(container_module.UnsafeBindSourceError):
            await SandboxContainerManager.restart(workspace)
        assert not any(event.startswith("start:") for event in journal.events)

    async def test_other_containers_of_the_workspace_are_removed_before_repairing(self, db, host, monkeypatch) -> None:
        """并发启动留下的、库里不认识的容器也能换 `.claude_home`：修之前一并停掉删掉，只留要重启的那个"""
        del db
        workspace, journal = await _restartable(host, monkeypatch)
        journal.add("cc-orphan", binds=_expected(workspace.id), labels=_labels(workspace.id))

        await SandboxContainerManager.restart(workspace)

        assert "cc-orphan" not in journal.containers
        assert journal.events.index("delete:cc-orphan") < journal.events.index("init")
        assert journal.events[-1] == "start:cc-old"

    async def test_a_failure_after_stopping_marks_the_workspace_failed(self, db, host, monkeypatch) -> None:
        """停了容器之后才失败（这里是写配置出错）：不能留着 active，记 failed 和原因"""
        del db

        def broken(_workspace: DBWorkspace) -> None:
            raise RuntimeError("disk full")

        workspace, journal = await _restartable(host, monkeypatch, on_init=broken)

        with pytest.raises(RuntimeError, match="disk full"):
            await SandboxContainerManager.restart(workspace)

        assert journal.containers["cc-old"].running is False
        status, last_error, _ = await _status(workspace)
        assert status == "failed"
        assert last_error is not None and "disk full" in last_error


class TestCreateAndStart:
    async def test_a_still_running_old_container_is_removed_first(self, db, host, monkeypatch) -> None:
        """状态是 failed、旧容器却还活着：先停掉删掉它，再修、再挂载"""
        del db
        workspace = await _workspace("cc-old", status="failed")
        journal = _Journal(_new_home(workspace.id))
        journal.add("cc-old", binds=_expected(workspace.id), labels=_labels(workspace.id))
        _patch_docker(monkeypatch, journal)

        await SandboxContainerManager.create_and_start(workspace)

        assert journal.events.index("delete:cc-old") < journal.events.index("init") < journal.events.index("create")
        assert "cc-old" not in journal.containers
        assert journal.kind_at_mount == ["dir"]

    async def test_bind_sources_are_the_literal_paths(self, db, host, monkeypatch) -> None:
        """bind 源不 `resolve()`：`resolve()` 会顺着链接走到目标，正是这个漏洞要的那一步"""
        del db
        real = host.tmp / "real-data"
        real.mkdir()
        alias = host.tmp / "data-alias"
        _link(alias, real, is_dir=True)
        monkeypatch.setattr(manager_module, "WORKSPACE_ROOT_DIR", str(alias / "workspaces"))
        workspace = await _workspace(None, status="stopped")
        journal = _Journal(_new_home(workspace.id))
        _patch_docker(monkeypatch, journal)

        await SandboxContainerManager.create_and_start(workspace)

        binds = journal.configs[0]["HostConfig"]["Binds"]
        literal = os.path.abspath(str(alias / "workspaces" / str(workspace.id)))
        literal_home = os.path.abspath(str(alias / "workspaces" / ".nk-homes" / str(workspace.id) / "claude"))
        assert binds[0] == f"{literal}:/workspace:rw"
        assert binds[1] == f"{literal_home}:/home/appuser/.claude:rw"
        assert binds == _expected_new(workspace.id)

    async def test_every_container_of_the_workspace_is_removed_before_mounting(self, db, host, monkeypatch) -> None:
        """库里记的、按标签认出的孤儿、升级前建的（没标签、按挂载认）：一律删掉；别的工作区、别的部署的不动"""
        del db
        workspace = await _workspace("cc-old", status="failed")
        journal = _Journal(_new_home(workspace.id))
        real_ws = os.path.realpath(str(host.root / str(workspace.id)))
        journal.add("cc-old", binds=_expected(workspace.id), labels=_labels(workspace.id))
        journal.add("cc-orphan", binds=[], labels=_labels(workspace.id))
        journal.add("cc-legacy", binds=[f"{real_ws}:/workspace:rw"], labels={"nekro.owner.scope": "x"})
        journal.add("cc-neighbour", binds=_expected(workspace.id + 1), labels=_labels(workspace.id + 1))
        journal.add("cc-other-deployment", binds=[], labels=_labels(workspace.id, scope="another-deployment"))
        _patch_docker(monkeypatch, journal)

        result = await SandboxContainerManager.create_and_start(workspace)

        init_at = journal.events.index("init")
        for name in ("cc-old", "cc-orphan", "cc-legacy"):
            assert name not in journal.containers
            assert journal.events.index(f"delete:{name}") < init_at
        assert {"cc-neighbour", "cc-other-deployment", result.container_name} <= set(journal.containers)

    async def test_a_container_that_cannot_be_removed_blocks_the_start(self, db, host, monkeypatch, logs) -> None:
        """删不掉、还在跑的同工作区容器：拒绝启动、记 ERROR，不建新容器"""
        del db
        workspace = await _workspace(None, status="stopped")
        journal = _Journal(_new_home(workspace.id))
        journal.add("cc-stuck", binds=_expected(workspace.id), labels=_labels(workspace.id))
        journal.undeletable.add("cc-stuck")
        _patch_docker(monkeypatch, journal)

        with pytest.raises(RuntimeError) as raised:
            await SandboxContainerManager.create_and_start(workspace)
        _raised(raised, "WorkspaceContainerConflictError")

        assert "create" not in journal.events
        assert "init" not in journal.events
        assert (await _status(workspace))[0] == "failed"
        assert any("删不掉，拒绝启动" in message for message in logs)

    async def test_the_new_container_is_labelled_and_cannot_gain_privileges(self, db, host, monkeypatch) -> None:
        del db
        workspace = await _workspace(None, status="stopped")
        journal = _Journal(_new_home(workspace.id))
        _patch_docker(monkeypatch, journal)

        await SandboxContainerManager.create_and_start(workspace)

        config_sent = journal.configs[0]
        assert config_sent["Labels"]["nekro.workspace.id"] == str(workspace.id)
        assert config_sent["Labels"]["nekro.owner.scope"] == container_module.deployment_scope()
        assert config_sent["HostConfig"]["CapDrop"] == ["MKNOD"]
        assert config_sent["HostConfig"]["SecurityOpt"] == ["no-new-privileges"]

    async def test_each_new_container_gets_its_own_api_token(self, db, host, monkeypatch) -> None:
        """旧容器（包括没删掉的孤儿）拿着的那把 token 在新容器上作废"""
        del db
        workspace = await _workspace(None, status="stopped")
        journal = _Journal(_new_home(workspace.id))
        _patch_docker(monkeypatch, journal)

        await SandboxContainerManager.create_and_start(workspace)
        await SandboxContainerManager.rebuild(workspace)

        tokens = [
            next(item.split("=", 1)[1] for item in sent["Env"] if item.startswith("INTERNAL_API_TOKEN="))
            for sent in journal.configs
        ]
        assert len(tokens) == 2 and tokens[0] != tokens[1]
        row = await DBWorkspace.get(id=workspace.id)
        assert (row.metadata or {}).get("sandbox_api_token") == tokens[1]

    async def test_a_container_started_but_not_recorded_is_deleted(self, db, host, monkeypatch) -> None:
        """容器起来了、记进库之前出了异常：当场删掉，不留一个库里不认识的容器"""
        del db
        workspace = await _workspace(None, status="stopped")
        journal = _Journal(_new_home(workspace.id))
        _patch_docker(monkeypatch, journal)
        original_show = _Container.show

        async def broken_show(self: _Container) -> Dict[str, Any]:
            if self.running:
                raise RuntimeError("inspect failed")
            return await original_show(self)

        monkeypatch.setattr(_Container, "show", broken_show)

        with pytest.raises(RuntimeError, match="inspect failed"):
            await SandboxContainerManager.create_and_start(workspace)

        assert journal.containers == {}
        assert any(event.startswith("delete:") for event in journal.events)
        assert (await _status(workspace))[0] == "failed"


class TestSerialisation:
    async def test_a_second_start_while_the_first_is_running_is_refused(self, db, host, monkeypatch) -> None:
        """两次启动同时到：后到的直接拒绝（「正在启停」），只建一个容器"""
        del db
        workspace = await _workspace(None, status="stopped")
        journal = _Journal(_new_home(workspace.id))
        _patch_docker(monkeypatch, journal)
        gate = asyncio.Event()

        async def slow_health(_endpoint: str, _timeout: int = 60) -> bool:
            await gate.wait()
            return True

        monkeypatch.setattr(SandboxContainerManager, "_wait_healthy", slow_health)
        other = await DBWorkspace.get(id=workspace.id)

        first = asyncio.create_task(SandboxContainerManager.create_and_start(workspace))
        await _until(lambda: "create" in journal.events or first.done())
        second = asyncio.create_task(SandboxContainerManager.create_and_start(other))
        try:
            done, _ = await asyncio.wait({second}, timeout=5)
        finally:
            gate.set()
        await first
        await asyncio.gather(second, return_exceptions=True)

        assert done, "第二次启动应当立刻被拒绝，而不是排在第一次后面"
        with pytest.raises(RuntimeError) as raised:
            second.result()
        _raised(raised, "WorkspaceBusyError")
        assert journal.events.count("create") == 1
        assert (await _status(workspace))[0] == "active"

    async def test_a_stop_during_a_start_waits_and_then_stops(self, db, host, monkeypatch) -> None:
        """停止排在启动后面（撤销凭据时必须停得下来），停的是启动刚建好的那个容器"""
        del db
        workspace = await _workspace(None, status="stopped")
        journal = _Journal(_new_home(workspace.id))
        _patch_docker(monkeypatch, journal)
        gate = asyncio.Event()

        async def slow_health(_endpoint: str, _timeout: int = 60) -> bool:
            await gate.wait()
            return True

        monkeypatch.setattr(SandboxContainerManager, "_wait_healthy", slow_health)
        stale = await DBWorkspace.get(id=workspace.id)

        start = asyncio.create_task(SandboxContainerManager.create_and_start(workspace))
        await _until(lambda: "create" in journal.events or start.done())
        stop = asyncio.create_task(SandboxContainerManager.stop(stale))
        await asyncio.sleep(0)
        assert not stop.done()
        gate.set()
        await start
        await stop

        status, _, name = await _status(workspace)
        assert status == "stopped"
        assert name is not None and journal.containers[name].running is False

    async def test_the_database_state_is_compared_not_just_the_lock(self, db, host, monkeypatch) -> None:
        """库里已经是 transitioning（别处在启停）：比较替换不成就拒绝"""
        del db
        workspace = await _workspace(None, status="stopped")
        journal = _Journal(_new_home(workspace.id))
        _patch_docker(monkeypatch, journal)
        await DBWorkspace.filter(id=workspace.id).update(status="transitioning")

        with pytest.raises(RuntimeError) as raised:
            await SandboxContainerManager.create_and_start(workspace)
        _raised(raised, "WorkspaceBusyError")
        assert journal.events == []


class TestHomeLayoutInTheContainerFlow:
    """home 搬到工作区目录之外：建容器时迁移，重启沿用旧挂载、不迁移"""

    async def test_starting_moves_the_legacy_home_out_before_mounting(self, db, host, monkeypatch) -> None:
        del db
        workspace = await _workspace(None, status="stopped")
        legacy = host.root / str(workspace.id) / ".claude_home"
        (legacy / "projects").mkdir(parents=True)
        (legacy / "projects" / "session.jsonl").write_text("history", encoding="utf-8")
        seen_at_init: List[bool] = []
        journal = _Journal(_new_home(workspace.id))
        _patch_docker(monkeypatch, journal, on_init=lambda ws: seen_at_init.append(_new_home(ws.id).is_dir()))

        await SandboxContainerManager.create_and_start(workspace)

        assert seen_at_init == [True], "迁移要排在写配置之前"
        assert (_new_home(workspace.id) / "projects" / "session.jsonl").read_text(encoding="utf-8") == "history"
        assert not os.path.lexists(legacy)
        assert journal.configs[0]["HostConfig"]["Binds"] == _expected_new(workspace.id)
        assert journal.kind_at_mount == ["dir"]

    async def test_a_failed_move_starts_with_the_legacy_home(self, db, host, monkeypatch, logs) -> None:
        del db
        workspace = await _workspace(None, status="stopped")
        (host.root / str(workspace.id) / ".claude_home").mkdir(parents=True)
        journal = _Journal(host.root / str(workspace.id) / ".claude_home")
        _patch_docker(monkeypatch, journal)

        def cross_device(*_args: Any, **_kwargs: Any) -> None:
            raise OSError(18, "Invalid cross-device link")

        monkeypatch.setattr(safe_fs.DirHandle, "rename_to", cross_device)

        result = await SandboxContainerManager.create_and_start(workspace)

        assert result.status == "active"
        assert journal.configs[0]["HostConfig"]["Binds"] == _expected(workspace.id)
        assert journal.kind_at_mount == ["dir"]
        assert any("没能挪到工作区之外" in message for message in logs)

    async def test_restart_keeps_the_legacy_mount_and_does_not_migrate(self, db, host, monkeypatch) -> None:
        """重启沿用容器创建时的挂载：这时候挪走旧 home，容器重启后就看不到自己的会话了"""
        del db
        workspace, journal = await _restartable(host, monkeypatch)

        await SandboxContainerManager.restart(workspace)

        assert journal.events[-1] == "start:cc-old"
        assert (host.root / str(workspace.id) / ".claude_home").is_dir()
        assert not (host.root / ".nk-homes").exists()

    async def test_restart_refuses_when_the_legacy_home_vanished_while_stopped(self, db, host, monkeypatch) -> None:
        """旧容器停下来之前把工作区里的 `.claude_home` 删了：home 落到新位置，容器记着的旧挂载
        不再指向刚核对过的目录——不重启，要求重建"""
        del db

        def vanish(ws: DBWorkspace) -> None:
            legacy = host.root / str(ws.id) / ".claude_home"
            for path in sorted(legacy.rglob("*"), reverse=True):
                path.rmdir() if path.is_dir() else path.unlink()
            legacy.rmdir()

        workspace, journal = await _restartable(host, monkeypatch, on_init=vanish)

        with pytest.raises(container_module.UnsafeBindSourceError):
            await SandboxContainerManager.restart(workspace)

        assert not any(event.startswith("start:") for event in journal.events)
        status, last_error, _ = await _status(workspace)
        assert status == "failed"
        assert last_error is not None and "重建" in last_error

    async def test_a_migrated_container_passes_the_startup_check(self, db, host, monkeypatch) -> None:
        del db
        workspace = await _workspace()
        _new_home(workspace.id).mkdir(parents=True)
        (host.root / str(workspace.id)).mkdir()
        journal = _Journal(_new_home(workspace.id))
        journal.add("cc-old", binds=_expected_new(workspace.id), labels=_labels(workspace.id))
        _patch_docker(monkeypatch, journal)

        await SandboxContainerManager.recover_on_startup()

        assert (await _status(workspace))[0] == "active"
        assert journal.containers["cc-old"].running is True

    async def test_a_legacy_mount_is_flagged_once_the_home_has_moved(self, db, host, monkeypatch, logs) -> None:
        """home 已经在新位置，容器却挂着旧路径（例如回滚后又升级、手工搬过）：停掉，要求重建"""
        del db
        workspace = await _workspace()
        _new_home(workspace.id).mkdir(parents=True)
        (host.root / str(workspace.id) / ".claude_home").mkdir(parents=True)
        journal = _Journal(_new_home(workspace.id))
        journal.add("cc-old", binds=_expected(workspace.id), labels=_labels(workspace.id))
        _patch_docker(monkeypatch, journal)

        await SandboxContainerManager.recover_on_startup()

        assert journal.containers["cc-old"].running is False
        assert (await _status(workspace))[0] == "failed"
        assert any("挂载与预期不一致" in message for message in logs)
        # 容器停下来之后，工作区目录里那份旧 home 挪出工作区：重建之后容器在 `/workspace` 里看不到它
        assert not os.path.lexists(host.root / str(workspace.id) / ".claude_home")
        assert list((host.root / ".nk-homes" / str(workspace.id)).glob("legacy-*"))

    async def test_restart_moves_a_leftover_legacy_home_out_while_stopped(self, db, host, monkeypatch) -> None:
        """已经在用新位置，工作区目录里又有一份旧的（退回旧版本又升级回来）：重启不迁移，但停着的时候把它挪走"""
        del db
        workspace = await _workspace()
        _new_home(workspace.id).mkdir(parents=True)
        legacy = host.root / str(workspace.id) / ".claude_home"
        (legacy / "nk-ssh").mkdir(parents=True)
        (legacy / "settings.json").write_text(SECRET, encoding="utf-8")
        seen_at_init: List[bool] = []
        journal = _Journal(_new_home(workspace.id))
        journal.add("cc-old", binds=_expected_new(workspace.id), labels=_labels(workspace.id))
        _patch_docker(monkeypatch, journal, on_init=lambda _ws: seen_at_init.append(os.path.lexists(legacy)))

        await SandboxContainerManager.restart(workspace)

        assert journal.events[-1] == "start:cc-old"
        assert seen_at_init == [False]
        assert not os.path.lexists(legacy)
        (retired,) = list((host.root / ".nk-homes" / str(workspace.id)).glob("legacy-*"))
        assert (retired / "settings.json").read_text(encoding="utf-8") == SECRET

    async def test_the_startup_check_moves_a_leftover_out_next_to_a_running_container(self, db, host, monkeypatch) -> None:
        del db
        workspace = await _workspace()
        _new_home(workspace.id).mkdir(parents=True)
        legacy = host.root / str(workspace.id) / ".claude_home"
        legacy.mkdir(parents=True)
        journal = _Journal(_new_home(workspace.id))
        journal.add("cc-old", binds=_expected_new(workspace.id), labels=_labels(workspace.id))
        _patch_docker(monkeypatch, journal)

        await SandboxContainerManager.recover_on_startup()

        assert journal.containers["cc-old"].running is True
        assert not os.path.lexists(legacy)

    async def test_the_startup_check_lists_workspaces_still_on_the_legacy_layout(self, db, host, monkeypatch, logs) -> None:
        del db
        running = await _workspace()
        (host.root / str(running.id) / ".claude_home").mkdir(parents=True)
        stopped = await DBWorkspace.create(name="ws-stopped", description="", status="stopped")
        (host.root / str(stopped.id) / ".claude_home").mkdir(parents=True)
        migrated = await DBWorkspace.create(name="ws-migrated", description="", status="stopped")
        _new_home(migrated.id).mkdir(parents=True)
        journal = _Journal(host.root / str(running.id) / ".claude_home")
        journal.add("cc-old", binds=_expected(running.id), labels=_labels(running.id))
        _patch_docker(monkeypatch, journal)

        await SandboxContainerManager.recover_on_startup()

        lines = [message for message in logs if "旧布局" in message]
        assert len(lines) == 1, lines
        assert f"{running.id}, {stopped.id}" in lines[0]
        assert str(migrated.id) not in lines[0].split(":", 1)[1]
        # 在跑的旧布局容器照旧在跑，home 不动
        assert journal.containers["cc-old"].running is True
        assert (host.root / str(running.id) / ".claude_home").is_dir()

    async def test_a_container_mounting_only_the_new_home_counts_as_this_workspaces(self, db, host, monkeypatch) -> None:
        """没有标签、只挂了这个工作区新位置 home 的容器，建新容器前同样要清掉"""
        del db
        workspace = await _workspace(None, status="stopped")
        journal = _Journal(_new_home(workspace.id))
        home_literal = os.path.abspath(str(_new_home(workspace.id)))
        journal.add("cc-stray", binds=[f"{home_literal}:/home/appuser/.claude:rw"], labels={"nekro.owner.scope": "x"})
        _patch_docker(monkeypatch, journal)

        await SandboxContainerManager.create_and_start(workspace)

        assert "cc-stray" not in journal.containers
        assert journal.events.index("delete:cc-stray") < journal.events.index("create")


class TestRebuild:
    async def test_rebuild_gives_up_when_the_old_container_cannot_be_deleted(self, db, host, monkeypatch) -> None:
        """旧容器删不掉：放弃重建，库里的容器名留着（不把一个还在的容器从库里抹掉），记 failed"""
        del db
        workspace, journal = await _restartable(host, monkeypatch)
        journal.undeletable.add("cc-old")

        with pytest.raises(RuntimeError, match="放弃重建"):
            await SandboxContainerManager.rebuild(workspace)

        assert "create" not in journal.events
        status, _, name = await _status(workspace)
        assert (status, name) == ("failed", "cc-old")

    async def test_a_missing_image_touches_nothing(self, db, host, monkeypatch) -> None:
        del db
        workspace, journal = await _restartable(host, monkeypatch)

        async def missing(_image: str) -> bool:
            return False

        monkeypatch.setattr(SandboxContainerManager, "check_image_exists", missing)

        with pytest.raises(container_module.ImageNotFoundError):
            await SandboxContainerManager.rebuild(workspace)

        assert journal.events == []
        assert await _status(workspace) == ("active", None, "cc-old")


class TestRecoverOnStartup:
    async def test_orphans_are_removed_and_recorded_containers_kept(self, db, host, monkeypatch) -> None:
        del db
        workspace, journal = await _restartable(host, monkeypatch)
        journal.add("cc-orphan", binds=_expected(workspace.id), labels=_labels(workspace.id))
        journal.add("cc-gone", binds=[], labels=_labels(workspace.id + 100))
        journal.add("cc-other-deployment", binds=[], labels=_labels(workspace.id + 100, scope="another-deployment"))
        journal.add("unrelated", binds=[])

        await SandboxContainerManager.recover_on_startup()

        assert set(journal.containers) == {"cc-old", "cc-other-deployment", "unrelated"}
        assert (await _status(workspace))[0] == "active"

    async def test_a_leftover_transitioning_with_a_good_container_becomes_active(self, db, host, monkeypatch) -> None:
        del db
        workspace, journal = await _restartable(host, monkeypatch)
        await DBWorkspace.filter(id=workspace.id).update(status="transitioning")

        await SandboxContainerManager.recover_on_startup()

        assert (await _status(workspace))[0] == "active"
        assert journal.containers["cc-old"].running is True

    @pytest.mark.parametrize("status", ["active", "transitioning"])
    async def test_a_container_with_a_wrong_mount_is_stopped_and_marked_failed(
        self, db, host, monkeypatch, logs, status
    ) -> None:
        del db
        workspace, journal = await _restartable(host, monkeypatch)
        await DBWorkspace.filter(id=workspace.id).update(status=status)
        ws_dir = os.path.abspath(str(host.root / str(workspace.id)))
        journal.containers["cc-old"].binds = [
            f"{ws_dir}:/workspace:rw",
            f"{host.outside}:/home/appuser/.claude:rw",
        ]

        await SandboxContainerManager.recover_on_startup()

        assert journal.containers["cc-old"].running is False
        result_status, last_error, _ = await _status(workspace)
        assert result_status == "failed"
        assert last_error is not None and "重建" in last_error
        assert any("挂载与预期不一致" in message for message in logs)

    async def test_a_container_built_with_resolved_paths_is_still_accepted(self, db, host, monkeypatch) -> None:
        """升级前按 `resolve()` 建的容器：工作区目录的上级有部署者放的链接时，挂载是真实路径那一份，照样认"""
        del db
        real = host.tmp / "real-data"
        (real / "workspaces").mkdir(parents=True)
        alias = host.tmp / "data-alias"
        _link(alias, real, is_dir=True)
        monkeypatch.setattr(manager_module, "WORKSPACE_ROOT_DIR", str(alias / "workspaces"))
        workspace = await _workspace()
        (real / "workspaces" / str(workspace.id) / ".claude_home").mkdir(parents=True)
        journal = _Journal(alias / "workspaces" / str(workspace.id) / ".claude_home")
        resolved = os.path.realpath(str(alias / "workspaces" / str(workspace.id)))
        binds = [f"{resolved}:/workspace:rw", f"{resolved}{os.sep}.claude_home:/home/appuser/.claude:rw"]
        binds += [bind for bind in _expected(workspace.id) if bind.startswith("/etc/localtime")]
        journal.add("cc-old", binds=binds)
        _patch_docker(monkeypatch, journal)

        await SandboxContainerManager.recover_on_startup()

        assert (await _status(workspace))[0] == "active"
        assert journal.containers["cc-old"].running is True

    async def test_a_running_container_of_a_stopped_workspace_is_stopped(self, db, host, monkeypatch) -> None:
        del db
        workspace, journal = await _restartable(host, monkeypatch)
        await DBWorkspace.filter(id=workspace.id).update(status="stopped")

        await SandboxContainerManager.recover_on_startup()

        assert journal.containers["cc-old"].running is False
        assert (await _status(workspace))[0] == "stopped"


class TestMountCheckAfterReview:
    """挂载核对只看「有没有预期之外的源 / 目标」：缺 `/etc/localtime` 不算，换版后不误停容器"""

    async def test_a_container_without_localtime_is_still_accepted(self, db, host, monkeypatch) -> None:
        del db
        workspace, journal = await _restartable(host, monkeypatch)
        journal.containers["cc-old"].binds = [b for b in _expected(workspace.id) if not b.startswith("/etc/localtime")]

        await SandboxContainerManager.recover_on_startup()

        assert (await _status(workspace))[0] == "active"
        assert journal.containers["cc-old"].running is True

    async def test_a_localtime_mount_is_accepted_even_when_na_has_none(self, db, host, monkeypatch) -> None:
        del db
        workspace, journal = await _restartable(host, monkeypatch)
        journal.containers["cc-old"].binds = [
            *[b for b in _expected(workspace.id) if not b.startswith("/etc/localtime")],
            "/etc/localtime:/etc/localtime:ro",
        ]

        await SandboxContainerManager.restart(workspace)

        assert journal.events[-1] == "start:cc-old"

    @pytest.mark.parametrize(
        "extra",
        [
            "/etc/localtime:/etc/localtime:rw",  # 能写宿主的 localtime
            "/srv/elsewhere:/mnt/elsewhere:rw",  # 预期之外的目标
        ],
    )
    async def test_an_unexpected_bind_is_refused(self, db, host, monkeypatch, extra) -> None:
        del db
        workspace, journal = await _restartable(host, monkeypatch)
        journal.containers["cc-old"].binds = [*journal.containers["cc-old"].binds, extra]

        with pytest.raises(container_module.UnsafeBindSourceError):
            await SandboxContainerManager.restart(workspace)
        assert not any(event.startswith("start:") for event in journal.events)

    async def test_a_source_spelled_through_a_link_inside_a_workspace_is_refused(self, db, host, monkeypatch) -> None:
        """`<根>/2/inner/1/.claude_home`：`inner` 是 2 号容器放的指回工作区根的链接，按目录身份比对得上，
        可下一次启动 Docker 会顺着它走，而 2 号容器随时能把它改指别处"""
        del db
        workspace, journal = await _restartable(host, monkeypatch)
        _link(host.root / "2" / "inner", host.root, is_dir=True)
        ws_dir = os.path.abspath(str(host.root / str(workspace.id)))
        planted = os.path.join(str(host.root), "2", "inner", str(workspace.id), ".claude_home")
        journal.containers["cc-old"].binds = [f"{ws_dir}:/workspace:rw", f"{planted}:/home/appuser/.claude:rw"]

        with pytest.raises(container_module.UnsafeBindSourceError):
            await SandboxContainerManager.restart(workspace)

    async def test_a_source_with_dotdot_is_refused(self, db, host, monkeypatch) -> None:
        del db
        workspace, journal = await _restartable(host, monkeypatch)
        ws_dir = os.path.abspath(str(host.root / str(workspace.id)))
        sneaky = os.path.join(ws_dir, "x", "..", ".claude_home")
        journal.containers["cc-old"].binds = [f"{ws_dir}:/workspace:rw", f"{sneaky}:/home/appuser/.claude:rw"]

        with pytest.raises(container_module.UnsafeBindSourceError):
            await SandboxContainerManager.restart(workspace)

    async def test_restarting_an_old_container_warns_that_hardening_is_missing(self, db, host, monkeypatch, logs) -> None:
        del db
        workspace, journal = await _restartable(host, monkeypatch)

        await SandboxContainerManager.restart(workspace)

        assert journal.events[-1] == "start:cc-old"
        assert any("建议重建" in message for message in logs)

    async def test_a_hardened_container_restarts_without_the_warning(self, db, host, monkeypatch, logs) -> None:
        del db
        workspace, journal = await _restartable(host, monkeypatch)
        journal.containers["cc-old"].host_config = {"CapDrop": ["MKNOD"], "SecurityOpt": ["no-new-privileges"]}

        await SandboxContainerManager.restart(workspace)

        assert not any("建议重建" in message for message in logs)


class TestDeletingAndSettling:
    async def test_remove_without_a_container_marks_deleting_and_touches_no_docker(self, db, host, monkeypatch) -> None:
        del db, host
        workspace = await _workspace(None, status="stopped")

        def no_docker() -> Any:
            raise AssertionError("没有容器名时不该碰 Docker")

        monkeypatch.setattr(container_module.aiodocker, "Docker", no_docker)

        await SandboxContainerManager.remove(workspace)

        assert (await _status(workspace))[0] == "deleting"

    async def test_nothing_starts_once_the_workspace_is_being_deleted(self, db, host, monkeypatch) -> None:
        del db
        workspace = await _workspace(None, status="stopped")
        journal = _Journal(_new_home(workspace.id))
        _patch_docker(monkeypatch, journal)
        await SandboxContainerManager.remove(workspace)

        stale = await DBWorkspace.get(id=workspace.id)
        stale.status = "stopped"  # 调用方手里的对象是删除前读的
        with pytest.raises(RuntimeError) as raised:
            await SandboxContainerManager.create_and_start(stale)
        _raised(raised, "WorkspaceBusyError")
        assert "create" not in journal.events

    async def test_remove_during_a_fresh_transition_is_refused(self, db, host, monkeypatch) -> None:
        del db
        workspace = await _workspace("cc-old")
        journal = _Journal(_new_home(workspace.id))
        _patch_docker(monkeypatch, journal)
        await DBWorkspace.filter(id=workspace.id).update(status="transitioning", update_time=timezone.now())

        with pytest.raises(RuntimeError) as raised:
            await SandboxContainerManager.remove(workspace)
        _raised(raised, "WorkspaceBusyError")
        assert journal.events == []

    async def test_a_failed_final_save_does_not_leave_transitioning(self, db, host, monkeypatch) -> None:
        """最终状态在内存里改成了 active、落库那一下失败：库里要是 failed，不能卡在 transitioning"""
        del db
        workspace = await _workspace(None, status="stopped")
        journal = _Journal(_new_home(workspace.id))
        _patch_docker(monkeypatch, journal)
        original_save = DBWorkspace.save

        async def flaky_save(self: DBWorkspace, *args: Any, **kwargs: Any) -> None:
            if "container_id" in (kwargs.get("update_fields") or []) and self.container_id:
                raise RuntimeError("database is locked")
            await original_save(self, *args, **kwargs)

        monkeypatch.setattr(DBWorkspace, "save", flaky_save)

        with pytest.raises(RuntimeError, match="database is locked"):
            await SandboxContainerManager.create_and_start(workspace)

        status, last_error, _ = await _status(workspace)
        assert status == "failed"
        assert last_error is not None and "database is locked" in last_error
        assert journal.containers == {}

    async def test_a_failed_start_leaves_no_name_of_a_deleted_container(self, db, host, monkeypatch) -> None:
        del db

        def broken(_workspace: DBWorkspace) -> None:
            raise RuntimeError("disk full")

        workspace = await _workspace("cc-old", status="failed")
        journal = _Journal(_new_home(workspace.id))
        journal.add("cc-old", binds=_expected(workspace.id), labels=_labels(workspace.id))
        _patch_docker(monkeypatch, journal, on_init=broken)

        with pytest.raises(RuntimeError, match="disk full"):
            await SandboxContainerManager.create_and_start(workspace)

        assert "cc-old" not in journal.containers
        status, _, name = await _status(workspace)
        assert (status, name) == ("failed", None)

    async def test_stop_refuses_a_fresh_transition_but_takes_over_a_stale_one(self, db, host, monkeypatch) -> None:
        """锁是空的、库里却是 transitioning：可能是另一个进程在做，没过期就不接；过期了按卡住接管"""
        del db
        workspace, journal = await _restartable(host, monkeypatch)
        await DBWorkspace.filter(id=workspace.id).update(status="transitioning", update_time=timezone.now())

        with pytest.raises(RuntimeError) as raised:
            await SandboxContainerManager.stop(workspace)
        _raised(raised, "WorkspaceBusyError")

        long_ago = timezone.now() - timedelta(seconds=container_module.STALE_TRANSITION_SECONDS + 60)
        await DBWorkspace.filter(id=workspace.id).update(update_time=long_ago)
        await SandboxContainerManager.stop(workspace)

        assert (await _status(workspace))[0] == "stopped"
        assert journal.containers["cc-old"].running is False


class TestGuardedMetadata:
    def test_the_guarded_keys_are_the_ones_the_services_use(self) -> None:
        from nekro_agent.models.db_workspace import GUARDED_METADATA_KEYS
        from nekro_agent.services.workspace.cc_credential import METADATA_TOKEN_KEY
        from nekro_agent.services.workspace.credential_policy import METADATA_SECRET_DELIVERED_KEY

        assert GUARDED_METADATA_KEYS == {
            "sandbox_api_token",
            METADATA_TOKEN_KEY,
            "ssh_gateway_client_key",
            METADATA_SECRET_DELIVERED_KEY,
        }

    async def test_a_stale_object_cannot_put_the_old_api_token_back(self, db) -> None:
        """请求开头读出来的对象改一个无关的键整份写回：这期间新建容器换掉的 token 不能被写回旧值"""
        del db
        workspace = await _workspace(None, status="stopped")
        old = await SandboxContainerManager._rotate_internal_api_token(workspace)  # noqa: SLF001
        stale = await DBWorkspace.get(id=workspace.id)
        new = await SandboxContainerManager._rotate_internal_api_token(workspace)  # noqa: SLF001
        assert new != old

        stale.metadata = {**(stale.metadata or {}), "skills": ["demo"]}
        await stale.save(update_fields=["metadata", "update_time"])

        stored = (await DBWorkspace.get(id=workspace.id)).metadata
        assert stored["sandbox_api_token"] == new
        assert stored["skills"] == ["demo"]

    async def test_a_stale_object_cannot_bring_back_a_revoked_relay_token(self, db) -> None:
        del db
        from nekro_agent.models.db_workspace import patch_workspace_metadata

        workspace = await _workspace(None, status="stopped")
        await patch_workspace_metadata(workspace, set_keys={"cc_relay_token": "nkcc-1-old"})
        stale = await DBWorkspace.get(id=workspace.id)
        await patch_workspace_metadata(workspace, drop_keys=["cc_relay_token"])

        await stale.save(update_fields=["metadata"])

        assert "cc_relay_token" not in (await DBWorkspace.get(id=workspace.id)).metadata


class TestForeignWriters:
    async def test_a_foreign_container_writing_the_workspace_root_blocks_the_start(self, db, host, monkeypatch, logs) -> None:
        """别的容器以读写方式挂着工作区根：它能在核对之后换掉 `.claude_home`，拒绝启动——而且在迁移之前
        拒绝：迁移要在没有别的容器写得到工作区目录与 `.nk-homes` 的时候做"""
        del db
        workspace = await _workspace(None, status="stopped")
        legacy = host.root / str(workspace.id) / ".claude_home"
        legacy.mkdir(parents=True)
        journal = _Journal(_new_home(workspace.id))
        journal.add("backup-sidecar", binds=[f"{os.path.abspath(str(host.root))}:/data:rw"])
        _patch_docker(monkeypatch, journal)

        with pytest.raises(RuntimeError) as raised:
            await SandboxContainerManager.create_and_start(workspace)
        _raised(raised, "WorkspaceContainerConflictError")
        assert "create" not in journal.events
        assert any("backup-sidecar" in message for message in logs)
        assert legacy.is_dir() and not (host.root / ".nk-homes").exists(), "拒绝排在迁移之前"

    async def test_a_foreign_container_writing_the_homes_dir_blocks_the_start(self, db, host, monkeypatch, logs) -> None:
        """新布局 home 的上级（`.nk-homes`）被别的容器读写挂着：它能把 home 换成链接，同样拒绝启动"""
        del db
        workspace = await _workspace(None, status="stopped")
        journal = _Journal(_new_home(workspace.id))
        journal.add("homes-sidecar", binds=[f"{os.path.abspath(str(host.root / '.nk-homes'))}:/homes:rw"])
        _patch_docker(monkeypatch, journal)

        with pytest.raises(RuntimeError) as raised:
            await SandboxContainerManager.create_and_start(workspace)
        _raised(raised, "WorkspaceContainerConflictError")
        assert "create" not in journal.events
        assert any("homes-sidecar" in message for message in logs)

    async def test_read_only_mounts_legacy_napcat_and_na_itself_are_allowed(self, db, host, monkeypatch, logs) -> None:
        """还按旧挂法（读写挂着整个数据目录）的 NapCat 这个版本放行，但每次启动记一条 WARNING"""
        del db
        workspace = await _workspace(None, status="stopped")
        journal = _Journal(_new_home(workspace.id))
        data_dir = os.path.abspath(str(host.tmp))
        journal.add("reader", binds=[f"{data_dir}:/data:ro"])
        journal.add("napcat-console", binds=[f"{data_dir}:/app/nekro_agent_data:rw"], labels={"nekro.napcat.instance": "1"})
        journal.add("napcat-compose", binds=[f"{data_dir}:/app/nekro_agent_data:rw"], labels={"com.docker.compose.service": "napcat"})
        journal.add("na-self", binds=[f"{data_dir}:{data_dir}:rw"])
        _patch_docker(monkeypatch, journal)
        monkeypatch.setattr(OsEnv, "RUN_IN_DOCKER", True)
        monkeypatch.setenv("HOSTNAME", "na-self")

        result = await SandboxContainerManager.create_and_start(workspace)

        assert result.status == "active"
        warnings = [message for message in logs if "旧的挂法" in message]
        assert len(warnings) == 1
        assert "napcat-console" in warnings[0] and "napcat-compose" in warnings[0]
        assert "na-self" not in warnings[0] and "reader" not in warnings[0]

    async def test_a_napcat_mounting_only_uploads_read_only_is_not_warned_about(self, db, host, monkeypatch, logs) -> None:
        """新的挂法：数据目录只挂 uploads、只读，QQ 数据目录读写——碰不到工作区，不记 WARNING"""
        del db
        workspace = await _workspace(None, status="stopped")
        journal = _Journal(_new_home(workspace.id))
        data_dir = os.path.abspath(str(host.tmp))
        journal.add(
            "napcat-narrow",
            binds=[
                f"{data_dir}{os.sep}uploads:/app/nekro_agent_data/uploads:ro",
                f"{data_dir}{os.sep}napcat_data{os.sep}inst{os.sep}QQ:/app/.config/QQ:rw",
            ],
            labels={"nekro.napcat.instance": "inst"},
        )
        _patch_docker(monkeypatch, journal)

        result = await SandboxContainerManager.create_and_start(workspace)

        assert result.status == "active"
        assert not any("旧的挂法" in message for message in logs)

    async def test_napcat_from_the_bundled_compose_file_is_allowed(self, db, host, monkeypatch) -> None:
        """随项目发布的 compose 文件里 NapCat 的服务名是 `nekro_napcat`；升级前的那份读写挂着整个数据目录：
        只认 `napcat` 的话，用这份 compose 起了 NapCat 的部署上 CC 工作区就起不来（旧挂法这个版本还放行）"""
        del db
        workspace = await _workspace(None, status="stopped")
        journal = _Journal(_new_home(workspace.id))
        data_dir = os.path.abspath(str(host.tmp))
        journal.add(
            "nekro_napcat",
            binds=[f"{data_dir}:/app/nekro_agent_data:rw"],
            labels={"com.docker.compose.service": "nekro_napcat"},
        )
        _patch_docker(monkeypatch, journal)

        result = await SandboxContainerManager.create_and_start(workspace)

        assert result.status == "active"


def test_every_napcat_service_in_the_bundled_compose_files_is_recognized() -> None:
    """compose 文件里改了 NapCat 的服务名而这里没跟上，就会回到「CC 工作区起不来」"""
    compose_dir = Path(__file__).resolve().parents[1] / "docker"
    service_line = re.compile(r"^  ([A-Za-z0-9_.-]*napcat[A-Za-z0-9_.-]*):\s*$")
    services = {
        match.group(1)
        for compose in sorted(compose_dir.glob("docker-compose*.yml"))
        for line in compose.read_text(encoding="utf-8").splitlines()
        if (match := service_line.match(line))
    }
    assert services, "compose 文件里应当至少有一个 NapCat 服务"
    for service in services:
        assert container_module._is_trusted_napcat({"Labels": {"com.docker.compose.service": service}}), service
    assert not container_module._is_trusted_napcat({"Labels": {"com.docker.compose.service": "backup"}})


def test_the_bundled_napcat_mounts_only_uploads_read_only() -> None:
    """随项目发布的 compose 文件里，NapCat 只把数据目录下的 uploads 只读挂进去，整个数据目录不给它"""
    compose_dir = Path(__file__).resolve().parents[1] / "docker"
    mounts: List[Tuple[str, str]] = []
    for compose in sorted(compose_dir.glob("docker-compose*.yml")):
        in_napcat = in_volumes = False
        for line in compose.read_text(encoding="utf-8").splitlines():
            if re.match(r"^\S", line):
                in_napcat = in_volumes = False
                continue
            service = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
            if service:
                in_napcat, in_volumes = "napcat" in service.group(1), False
                continue
            if not in_napcat:
                continue
            if re.match(r"^    volumes:\s*$", line):
                in_volumes = True
            elif re.match(r"^    \S", line):
                in_volumes = False
            elif in_volumes and (volume := re.match(r"^      - (\S+)\s*$", line)):
                mounts.append((compose.name, volume.group(1)))
    data_mounts = [(name, volume) for name, volume in mounts if ":/app/nekro_agent_data" in volume]
    assert data_mounts, "没认出 NapCat 的数据目录挂载——判据脱靶，不是产品没问题"
    for name, volume in data_mounts:
        assert volume.endswith("/uploads:/app/nekro_agent_data/uploads:ro"), (name, volume)


class TestRecoverInspectErrors:
    async def test_an_inspect_error_is_not_taken_as_a_missing_container(self, db, host, monkeypatch, logs) -> None:
        """inspect 报的不是 404：不当成「不存在」降级 stopped，重试几次还不行就按名字杀掉、标 failed"""
        del db
        workspace, journal = await _restartable(host, monkeypatch)
        journal.broken_inspect.add("cc-old")
        monkeypatch.setattr(container_module, "_INSPECT_RETRY_DELAY", 0)

        await SandboxContainerManager.recover_on_startup()

        assert "kill:cc-old" in journal.events
        status, last_error, _ = await _status(workspace)
        assert status == "failed"
        assert last_error is not None and "检查容器失败" in last_error

    async def test_a_404_still_means_stopped(self, db, host, monkeypatch) -> None:
        del db
        workspace, journal = await _restartable(host, monkeypatch)
        del journal.containers["cc-old"]

        await SandboxContainerManager.recover_on_startup()

        assert (await _status(workspace))[0] == "stopped"


def test_a_linked_workspace_dir_is_refused(host, logs) -> None:
    """工作区目录本身不是普通目录（是链接）：拒绝启动、记 ERROR，绝不替它 resolve"""
    elsewhere = host.tmp / "elsewhere-ws"
    elsewhere.mkdir()
    _link(host.root / "7", elsewhere, is_dir=True)

    with pytest.raises(container_module.UnsafeBindSourceError):
        container_module._checked_bind_sources(7)  # noqa: SLF001
    assert any("拒绝启动容器" in message for message in logs)


# ---------------------------------------------------------------------------
# 2. 空间清理
# ---------------------------------------------------------------------------


@pytest.fixture
def cleanup_world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    from nekro_agent.services.space_cleanup import cleaner as cleaner_module

    data = tmp_path / "data"
    sandboxes = data / "sandboxes"
    chat_dir = sandboxes / "sandbox-key-1"
    (chat_dir / "sub").mkdir(parents=True)
    (chat_dir / "sub" / "victim.txt").write_text("in the sandbox", encoding="utf-8")
    outside = tmp_path / "host-elsewhere"
    outside.mkdir()
    (outside / "victim.txt").write_text(SECRET, encoding="utf-8")

    monkeypatch.setattr(OsEnv, "DATA_DIR", str(data))
    for name in (
        "USER_UPLOAD_DIR",
        "SANDBOX_PIP_CACHE_DIR",
        "SANDBOX_PACKAGE_DIR",
        "PLUGIN_DYNAMIC_PACKAGE_DIR",
        "PROMPT_LOG_DIR",
        "PROMPT_ERROR_LOG_DIR",
        "MEMORY_PARSE_LOG_DIR",
        "NAPCAT_TEMPFILE_DIR",
    ):
        monkeypatch.setattr(cleaner_module, name, str(data / name.lower()))
    monkeypatch.setattr(cleaner_module, "SANDBOX_SHARED_HOST_DIR", str(sandboxes))
    return SimpleNamespace(module=cleaner_module, sandboxes=sandboxes, chat_dir=chat_dir, outside=outside)


async def _collect_shared(world: SimpleNamespace) -> List[Path]:
    from nekro_agent.schemas.space_cleanup import CleanupRequest, ResourceType

    return await world.module.cleanup_service._collect_files_to_clean(  # noqa: SLF001
        CleanupRequest(resource_types=[ResourceType.SANDBOX_SHARED]),
    )


class TestCleaner:
    async def test_a_dir_swapped_for_a_link_after_collection_is_not_followed(self, cleanup_world) -> None:
        """收集之后沙盒把 `sub` 换成指向宿主别处的链接：删的时候不能顺着它删掉宿主文件"""
        world = cleanup_world
        files = await _collect_shared(world)
        assert [p.name for p in files] == ["victim.txt"]

        (world.chat_dir / "sub").rename(world.chat_dir / "sub-moved")
        _link(world.chat_dir / "sub", world.outside, is_dir=True)

        removed = world.module.cleanup_service._safe_remove_file(files[0])  # noqa: SLF001

        assert removed is False
        assert (world.outside / "victim.txt").read_text(encoding="utf-8") == SECRET

    async def test_a_plain_file_is_still_removed(self, cleanup_world) -> None:
        """对照：没被动过的文件照常删掉"""
        world = cleanup_world
        files = await _collect_shared(world)

        assert world.module.cleanup_service._safe_remove_file(files[0]) is True  # noqa: SLF001
        assert not (world.chat_dir / "sub" / "victim.txt").exists()

    async def test_size_is_not_measured_through_a_link(self, cleanup_world) -> None:
        """统计释放空间时不跟着链接去量宿主别处的文件"""
        world = cleanup_world
        files = await _collect_shared(world)
        (world.chat_dir / "sub").rename(world.chat_dir / "sub-moved")
        _link(world.chat_dir / "sub", world.outside, is_dir=True)

        assert world.module.cleanup_service._safe_file_size(files[0]) == 0  # noqa: SLF001

    async def test_empty_dir_cleanup_does_not_walk_into_links(self, cleanup_world) -> None:
        world = cleanup_world
        (world.outside / "empty-on-the-host").mkdir()
        (world.chat_dir / "empty-in-sandbox").mkdir()
        _link(world.chat_dir / "to-host", world.outside, is_dir=True)

        await world.module.cleanup_service._cleanup_empty_subdirectories(world.sandboxes)  # noqa: SLF001

        assert (world.outside / "empty-on-the-host").is_dir()
        assert not (world.chat_dir / "empty-in-sandbox").exists()


# ---------------------------------------------------------------------------
# 3. 大小与深度上限
# ---------------------------------------------------------------------------


class TestLimits:
    def test_a_file_growing_past_the_limit_while_read_is_cut_off(self, tmp_path: Path) -> None:
        """打开时 10 字节、读的时候被追加到 4 KB：读到超过上限的那一刻报错，不会一口气读完"""
        target = tmp_path / "growing.bin"
        target.write_bytes(b"x" * 10)
        with safe_fs.open_dir(tmp_path) as directory:
            handle = directory.open_read("growing.bin", max_bytes=100)
        with target.open("ab") as appender:
            appender.write(b"y" * 4096)
        with handle, pytest.raises(OSError) as raised:
            handle.read()
        assert type(raised.value).__name__ == "FileTooLargeError"

    def test_no_limit_means_no_limit(self, tmp_path: Path) -> None:
        """对照：不给上限的调用方行为不变"""
        (tmp_path / "f.bin").write_bytes(b"z" * 5000)
        with safe_fs.open_dir(tmp_path) as directory, directory.open_read("f.bin") as handle:
            assert len(handle.read()) == 5000

    def test_knowledge_base_reads_have_a_ceiling(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logs) -> None:
        """容器把知识库源文件撑成稀疏大文件：读的时候按「读不了」处理并说清是太大，不读进内存"""
        from nekro_agent.services.kb import kb_files

        root = tmp_path / "workspaces"
        files_dir = root / "1" / "default" / "kb" / "files"
        files_dir.mkdir(parents=True)
        # 65 MiB：比后台上传上限（50 MB）与读取上限（64 MiB）都大
        with (files_dir / "huge.bin").open("wb") as handle:
            handle.truncate(65 * 1024 * 1024)
        monkeypatch.setattr(WorkspaceService, "get_workspace_dir", staticmethod(lambda workspace_id: root / str(workspace_id)))

        assert kb_files.read_bytes(1, kb_files.SOURCE, "huge.bin") is None
        assert any("上限" in message for message in logs)

    def test_remove_tree_removes_a_tree_deeper_than_the_walk_limit(self, tmp_path: Path, monkeypatch) -> None:
        """删除不递归、不设深度上限：比遍历上限还深的树照样删干净（删不掉的话启动时同步技能会整个失败）"""
        monkeypatch.setattr(safe_fs, "MAX_TREE_DEPTH", 3)
        deep = tmp_path / "a"
        for index in range(8):
            deep = deep / f"d{index}"
        deep.mkdir(parents=True)
        (deep / "leaf.txt").write_text("x", encoding="utf-8")

        with safe_fs.open_dir(tmp_path) as directory:
            directory.remove_tree("a")

        assert not (tmp_path / "a").exists()
        assert [p.name for p in tmp_path.iterdir()] == []

    def test_remove_tree_has_an_entry_budget(self, tmp_path: Path) -> None:
        """条目总数有上限（防止被塞进几百万个文件拖住启动）；超了抛错，调用方决定挪走还是报错"""
        (tmp_path / "a" / "b").mkdir(parents=True)
        for index in range(5):
            (tmp_path / "a" / "b" / f"f{index}").write_text("x", encoding="utf-8")

        with safe_fs.open_dir(tmp_path) as directory, pytest.raises(safe_fs.UnsafePathError):
            directory.remove_tree("a", max_entries=3)

    def test_a_tree_over_the_budget_is_moved_aside_and_purged_later(self, tmp_path: Path) -> None:
        """启动路径上删不完的树：整个挪到 `.nk-trash-*`，下次再清，不让启动失败"""
        (tmp_path / "ws" / "skills" / "big").mkdir(parents=True)
        for index in range(5):
            (tmp_path / "ws" / "skills" / "big" / f"f{index}").write_text("x", encoding="utf-8")

        with safe_fs.open_dir(tmp_path, ("ws",)) as trash, safe_fs.open_dir(tmp_path, ("ws", "skills")) as skills:
            original = safe_fs.MAX_REMOVE_ENTRIES
            try:
                safe_fs.MAX_REMOVE_ENTRIES = 3
                outcome = skills.remove_tree_or_discard("big", trash)
            finally:
                safe_fs.MAX_REMOVE_ENTRIES = original

        assert outcome == "discarded"
        assert not (tmp_path / "ws" / "skills" / "big").exists()
        leftovers = [p.name for p in (tmp_path / "ws").iterdir() if p.name.startswith(".nk-trash-")]
        assert len(leftovers) == 1

        with safe_fs.open_dir(tmp_path, ("ws",)) as directory:
            assert directory.purge_trash() == 1
        assert sorted(p.name for p in (tmp_path / "ws").iterdir()) == ["skills"]

    def test_iter_files_and_copy_tree_out_stop_at_the_depth_limit(self, tmp_path: Path) -> None:
        deep = tmp_path / "src"
        for index in range(5):
            deep = deep / f"d{index}"
        deep.mkdir(parents=True)
        (deep / "f.txt").write_text("x", encoding="utf-8")

        with safe_fs.open_dir(tmp_path, ("src",)) as directory, pytest.raises(safe_fs.TreeTooDeepError):
            list(safe_fs.iter_files(directory, max_depth=2))
        with safe_fs.open_dir(tmp_path) as directory, pytest.raises(safe_fs.TreeTooDeepError):
            directory.copy_tree_out("src", tmp_path / "copy", max_depth=2)

    def test_a_too_deep_skill_tree_is_treated_like_one_with_links(self, tmp_path: Path) -> None:
        deep = tmp_path / "skill"
        for index in range(5):
            deep = deep / f"d{index}"
        deep.mkdir(parents=True)

        with safe_fs.open_dir(tmp_path, ("skill",)) as directory:
            assert WorkspaceService._tree_has_link(directory, max_depth=2) is True  # noqa: SLF001
        with safe_fs.open_dir(tmp_path, ("skill",)) as directory:
            assert WorkspaceService._tree_has_link(directory) is False  # noqa: SLF001

    def test_download_from_cc_has_a_ceiling(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from plugins.builtin.cc_workspace import main as cc_main

        root = tmp_path / "workspaces"
        (root / "1" / "default" / "shared").mkdir(parents=True)
        (root / "1" / "default" / "shared" / "big.bin").write_bytes(b"b" * 64)
        shared = tmp_path / "sandbox-shared"
        shared.mkdir()
        monkeypatch.setattr(WorkspaceService, "get_workspace_dir", staticmethod(lambda workspace_id: root / str(workspace_id)))
        monkeypatch.setattr(cc_main, "_CC_TRANSFER_MAX_BYTES", 16, raising=False)

        async def bound() -> Any:
            return SimpleNamespace(id=1)

        ctx = SimpleNamespace(
            get_bound_workspace=bound,
            fs=SimpleNamespace(shared_path=shared, forward_file=lambda path: f"/app/shared/{Path(path).name}"),
        )

        with pytest.raises(ValueError, match="上限"):
            asyncio.run(cc_main.download_file_from_cc(ctx, "big.bin"))
        assert not (shared / "big.bin").exists()


# ---------------------------------------------------------------------------
# 4. 「在不在沙盒共享目录下」的两种写法
# ---------------------------------------------------------------------------


@pytest.fixture
def aliased_shared(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """NA 的工作目录经一个链接进入（部署里常见：`/app` 是指向别处的链接），`DATA_DIR` 是相对路径

    共享目录根于是有两种写法：按字面展开（经链接的那条）与解析掉链接后的真实路径。
    `FileSystem.shared_path` 用的是后者（`to_absolute_path` 对相对路径会 `resolve()`）。
    """
    from nekro_agent.tools import file_utils, sandbox_shared_files

    real = tmp_path / "real-cwd"
    (real / "data" / "sandboxes" / "sandbox-key-1").mkdir(parents=True)
    alias = tmp_path / "cwd-alias"
    _link(alias, real, is_dir=True)
    outside = tmp_path / "host-elsewhere"
    outside.mkdir()
    (outside / "nekro-agent.yaml").write_text(SECRET, encoding="utf-8")

    monkeypatch.chdir(alias)
    monkeypatch.setattr(sandbox_shared_files, "SANDBOX_SHARED_HOST_DIR", "data/sandboxes")
    monkeypatch.setattr(file_utils, "SANDBOX_SHARED_HOST_DIR", "data/sandboxes")
    return SimpleNamespace(module=sandbox_shared_files, real=real, alias=alias, outside=outside, tmp=tmp_path)


class TestSharedPathForms:
    def test_the_path_the_file_system_hands_out_is_recognised(self, aliased_shared) -> None:
        from nekro_agent.tools.file_utils import FileSystem

        shared_path = FileSystem("chat-1", "sandbox-key-1").shared_path
        assert aliased_shared.module.split_shared_path(shared_path / "report.pdf") is not None

    def test_a_link_in_the_shared_dir_is_refused_whichever_form_the_path_takes(self, aliased_shared) -> None:
        """现场的形状：共享目录路径是解析后的写法、判据只认字面写法，原来就退回普通 open 读到了宿主文件"""
        from nekro_agent.tools.file_utils import FileSystem

        shared_path = FileSystem("chat-1", "sandbox-key-1").shared_path
        _link(shared_path / "leak.txt", aliased_shared.outside / "nekro-agent.yaml", is_dir=False)

        with pytest.raises(aliased_shared.module.UnsafeSharedFileError):
            aliased_shared.module.open_host_file(shared_path / "leak.txt")
        assert aliased_shared.module.host_file_is_regular(shared_path / "leak.txt") is False

    def test_a_third_spelling_of_the_shared_dir_is_refused_not_opened(self, aliased_shared) -> None:
        """经第三个别名绕进共享目录的路径：对不上任何一种写法，也不退回普通 open"""
        second_alias = aliased_shared.tmp / "another-alias"
        _link(second_alias, aliased_shared.real, is_dir=True)
        target = aliased_shared.real / "data" / "sandboxes" / "sandbox-key-1" / "plain.txt"
        target.write_text("ok", encoding="utf-8")

        with pytest.raises(aliased_shared.module.UnsafeSharedFileError):
            aliased_shared.module.open_host_file(second_alias / "data" / "sandboxes" / "sandbox-key-1" / "plain.txt")

    def test_paths_in_the_upload_dir_are_opened_as_before(self, aliased_shared, monkeypatch) -> None:
        """对照：上传目录里的路径（NA 写、沙盒只读）照旧按普通路径打开，NA 拼得出的两种写法都算

        NA 拼上传路径只有两种写法：`Path(USER_UPLOAD_DIR) / ...`（`convert_to_host_path` 等；相对路径
        由本进程按自己的工作目录展开）与 `resolve()` 之后的真实路径（`FileSystem.get_file`）。这里
        `USER_UPLOAD_DIR` 是相对路径、工作目录经链接进入。

        不拿「经链接的工作目录」手拼绝对路径：Linux 上 `os.getcwd()` 给的是解析后的真实路径
        （内核不记得进来时走的链接），NA 自己拼不出那种写法；Windows 会保留链接写法。原来的用例
        这样拼，只在 Windows 上过（CI 的 Ubuntu 上挂了），生产路径不受影响。
        """
        uploads = aliased_shared.real / "data" / "uploads"
        (uploads / "chat-1").mkdir(parents=True)
        (uploads / "chat-1" / "file.txt").write_text("ok", encoding="utf-8")
        monkeypatch.setattr(os_env_module, "USER_UPLOAD_DIR", "data/uploads")

        as_built = Path(os_env_module.USER_UPLOAD_DIR) / "chat-1" / "file.txt"
        for path in (as_built, as_built.resolve(), Path(os.path.abspath(str(as_built)))):
            with aliased_shared.module.open_host_file(path) as handle:
                assert handle.read() == b"ok"
            assert aliased_shared.module.host_file_is_regular(path) is True

    def test_an_absolute_upload_dir_through_a_link_is_recognised_in_both_spellings(
        self, aliased_shared, monkeypatch
    ) -> None:
        """部署里常见的另一种：`DATA_DIR` 是绝对路径、其中某一级是链接（`/app` 指向别处）。
        字面写法（经链接）与解析后的写法都认——这才是「两种写法」在 Linux 上真正出现的样子"""
        uploads = aliased_shared.real / "data" / "uploads"
        (uploads / "chat-1").mkdir(parents=True)
        (uploads / "chat-1" / "file.txt").write_text("ok", encoding="utf-8")
        monkeypatch.setattr(os_env_module, "USER_UPLOAD_DIR", str(aliased_shared.alias / "data" / "uploads"))

        literal = Path(os_env_module.USER_UPLOAD_DIR) / "chat-1" / "file.txt"
        for path in (literal, literal.resolve()):
            with aliased_shared.module.open_host_file(path) as handle:
                assert handle.read() == b"ok"
            assert aliased_shared.module.host_file_is_regular(path) is True

    def test_paths_anywhere_else_are_refused(self, aliased_shared, monkeypatch) -> None:
        """既不在共享目录、也不在上传目录：不再退回会跟随链接的普通 `open()`"""
        monkeypatch.setattr(os_env_module, "USER_UPLOAD_DIR", "data/uploads")
        plain = aliased_shared.tmp / "elsewhere.txt"
        plain.write_text("ok", encoding="utf-8")

        with pytest.raises(aliased_shared.module.UnsafeSharedFileError):
            aliased_shared.module.open_host_file(plain)
        with pytest.raises(aliased_shared.module.UnsafeSharedFileError):
            aliased_shared.module.snapshot_for_handoff(aliased_shared.outside / "nekro-agent.yaml", "chat-1")
        assert aliased_shared.module.host_file_is_regular(plain) is False

    def test_dotdot_in_an_upload_path_is_refused(self, aliased_shared, monkeypatch) -> None:
        """上传目录按字面写法判：`<上传目录>/a/../x` 规整后在里面，系统打开时却先走 `a`（可能是链接）"""
        uploads = aliased_shared.real / "data" / "uploads"
        (uploads / "a").mkdir(parents=True)
        (uploads / "file.txt").write_text("ok", encoding="utf-8")
        monkeypatch.setattr(os_env_module, "USER_UPLOAD_DIR", "data/uploads")

        with pytest.raises(aliased_shared.module.UnsafeSharedFileError):
            aliased_shared.module.open_host_file(uploads / "a" / ".." / "file.txt")
        assert aliased_shared.module.host_file_is_regular(uploads / "a" / ".." / "file.txt") is False

    def test_a_backslash_in_a_shared_name_is_not_an_exception(self, aliased_shared) -> None:
        """名字里带 `\\`：按「不是普通文件」回答，不往上抛"""
        from nekro_agent.tools.file_utils import FileSystem

        shared_path = FileSystem("chat-1", "sandbox-key-1").shared_path
        assert aliased_shared.module.host_file_is_regular(shared_path / "a\\b.txt") is False
