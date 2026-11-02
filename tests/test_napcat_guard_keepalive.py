"""硬化部署（走 docker-guard）下 NapCat 容器的保活（PR #8 复核 P1：RestartPolicy 绕过）。

## 背景

带 Binds 的容器若交给 Docker 守护进程自动重启（`unless-stopped`），重启时的挂载源解析**不经过 guard**：
创建时绑 `<数据目录>/x`，之后把 `x` 换成指向 `/` 的链接，等容器退出，守护进程重启它时就把宿主根目录挂了进去。
所以 guard 在 create 时拒绝「带 Binds 且 RestartPolicy 不是 no」的容器（见 `test_docker_guard_bypass.py`）。

NapCat 是长命的协议端，崩溃 / OOM / 宿主重启后要回来，于是保活从 Docker 手里收回到 NA 手里：

- 走 guard 时 NapCat 建成 `RestartPolicy=no`，并在实例元数据里记下 `desired_state`；
- `reconcile_container_liveness()` 搭在实例健康监测的探活循环上，把「应当在跑却已退出」的容器
  经 guard 重新拉起（`start_container`，与控制台「启动」同一条路径）。

## 这份文件钉的契约

| 契约 | 把它改回去 |
|---|---|
| 不走 guard 时逐字不变（`unless-stopped`、元数据不多一个键、reconcile 什么都不做） | 无条件改成 `no` / 无条件写 `desired_state` / reconcile 不看 guard |
| 走 guard 时建 `no`，且这份配置过得了 guard 的 RestartPolicy 判据 | 仍写 `unless-stopped` |
| 主动停止的不被拉回来 | stop 不记 `stopped` / reconcile 不看 `desired_state` |
| 存量（没有记录）的容器不碰 | 把空 `desired_state` 当 running |
| 拉起失败按实例指数退避，不刷日志 | 去掉退避 |
| 探活循环真的调用了 reconcile | 删掉 `_probe_all` 里的调用 |
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest
from aiodocker.exceptions import DockerError
from tortoise import Tortoise

from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.services.console import container_manager, instance_health
from nekro_agent.services.docker_guard.policy import _check_restart_policy

INSTANCE_KEY = "inst1"


# ---------------------------------------------------------------------------
# Docker 替身
# ---------------------------------------------------------------------------


class _FakeContainer:
    def __init__(self, name: str, status: str, labels: Dict[str, str]) -> None:
        self.name = name
        self.status = status
        self.labels = labels
        self.calls: List[str] = []
        self.start_error: Optional[DockerError] = None
        self.restart_policy = ""
        self.binds: List[str] = []
        self.id = "c" * 12 + name

    async def show(self) -> Dict[str, Any]:
        return {
            "Name": f"/{self.name}",
            "Config": {"Labels": dict(self.labels), "Image": "mlikiowa/napcat-docker:latest"},
            "HostConfig": {"RestartPolicy": {"Name": self.restart_policy}, "Binds": list(self.binds)},
            "State": {"Status": self.status, "StartedAt": "2026-01-01T00:00:00Z"},
        }

    async def start(self) -> None:
        self.calls.append("start")
        if self.start_error is not None:
            raise self.start_error
        self.status = "running"

    async def stop(self, t: int = 0) -> None:
        self.calls.append(f"stop:{t}")
        self.status = "exited"

    async def restart(self, t: int = 0) -> None:
        self.calls.append(f"restart:{t}")
        self.status = "running"

    async def delete(self, force: bool = False) -> None:
        self.calls.append(f"delete:{force}")


class _FakeContainers:
    def __init__(self, state: Dict[str, Any]) -> None:
        self._state = state

    async def get(self, name: str) -> _FakeContainer:
        found = self._state["containers"].get(name)
        if found is None:
            raise DockerError(404, {"message": f"No such container: {name}"})
        return found

    async def list(self, **kwargs: Any) -> List[_FakeContainer]:
        self._state.setdefault("list_calls", []).append(kwargs)
        return list(self._state["containers"].values())

    async def create(self, config: Dict[str, Any], name: str) -> _FakeContainer:
        self._state["created"].append((name, config))
        created = _FakeContainer(name, "running", dict(config.get("Labels") or {}))
        self._state["containers"][name] = created
        return created


class _FakeImages:
    async def inspect(self, image: str) -> Dict[str, Any]:  # noqa: ARG002
        return {"Id": "sha256:" + "0" * 64}


class _FakeDocker:
    def __init__(self, state: Dict[str, Any]) -> None:
        self.containers = _FakeContainers(state)
        self.images = _FakeImages()
        self._state = state

    async def _query_json(self, path: str, method: str = "GET", *, data: Any = None, **_k: Any) -> Any:
        error = self._state.get("update_error")
        if error is not None:
            raise error
        self._state["updates"].append((path, data))
        return {"Warnings": []}

    async def close(self) -> None:
        return None


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    state: Dict[str, Any] = {"containers": {}, "created": [], "updates": []}
    monkeypatch.setattr(container_manager.aiodocker, "Docker", lambda *a, **k: _FakeDocker(state))  # noqa: ARG005
    return state


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """默认摆成标准部署：没有 guard 相关环境变量；数据目录落在 tmp 下（create 会真的 mkdir）。"""
    monkeypatch.delenv(container_manager.DOCKER_GUARD_ACTIVE_ENV, raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    data_dir = tmp_path / "data" / "nekro_agent"
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(OsEnv, "INSTANCE_NAME", "")
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(data_dir))
    container_manager._reconcile_state.clear()
    yield
    container_manager._reconcile_state.clear()


@pytest.fixture
def guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(container_manager.DOCKER_GUARD_ACTIVE_ENV, "true")


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield await DBAdapterInstance.create(
            adapter_key="onebot_v11",
            instance_key=INSTANCE_KEY,
            display_name=INSTANCE_KEY,
            status="online",
            provider="builtin_napcat",
            tenant_id=1,
            metadata_json="",
        )
    finally:
        await Tortoise.close_connections()


def _metadata(instance: DBAdapterInstance) -> Dict[str, Any]:
    return json.loads(instance.metadata_json) if instance.metadata_json else {}


async def _reload(instance: DBAdapterInstance) -> DBAdapterInstance:
    return await DBAdapterInstance.get(id=instance.id)


def _put_container(docker_state: Dict[str, Any], status: str, key: str = INSTANCE_KEY) -> _FakeContainer:
    name = container_manager.build_container_name(key)
    container = _FakeContainer(
        name,
        status,
        {container_manager.LABEL_INSTANCE: key, container_manager.LABEL_OWNER: container_manager.get_owner_tag()},
    )
    docker_state["containers"][name] = container
    return container


async def _set_desired(instance: DBAdapterInstance, state: Optional[str]) -> DBAdapterInstance:
    metadata = _metadata(instance)
    if state is not None:
        metadata["desired_state"] = state
    instance.metadata_json = json.dumps(metadata)
    await instance.save(update_fields=["metadata_json"])
    return instance


# ---------------------------------------------------------------------------
# 走不走 guard 的判断
# ---------------------------------------------------------------------------

GUARD_DETECTION_CASES = [
    ({}, False),
    ({"DOCKER_HOST": "unix:///var/run/docker.sock"}, False),
    ({"DOCKER_HOST": "tcp://docker-proxy:2375"}, False),
    ({"DOCKER_HOST": "unix:///run/nekro-guard/guard.sock"}, True),  # 硬化 compose 的取值
    ({"DOCKER_HOST": "unix:///somewhere/else/guard.sock"}, True),
    ({"NEKRO_DOCKER_GUARD_ACTIVE": "true"}, True),
    ({"NEKRO_DOCKER_GUARD_ACTIVE": "on"}, True),
    ({"NEKRO_DOCKER_GUARD_ACTIVE": "no", "DOCKER_HOST": "unix:///run/nekro-guard/guard.sock"}, False),
    ({"NEKRO_DOCKER_GUARD_ACTIVE": "off", "DOCKER_HOST": "unix:///run/nekro-guard/guard.sock"}, False),
    ({"NEKRO_DOCKER_GUARD_ACTIVE": "1"}, True),
    ({"NEKRO_DOCKER_GUARD_ACTIVE": " YES "}, True),
    # 显式声明优先于推断
    ({"NEKRO_DOCKER_GUARD_ACTIVE": "false", "DOCKER_HOST": "unix:///run/nekro-guard/guard.sock"}, False),
    ({"NEKRO_DOCKER_GUARD_ACTIVE": "0"}, False),
    ({"NEKRO_DOCKER_GUARD_ACTIVE": "true", "DOCKER_HOST": "unix:///var/run/docker.sock"}, True),
    # 认不出的值按「没声明」处理，继续看 DOCKER_HOST
    ({"NEKRO_DOCKER_GUARD_ACTIVE": "maybe"}, False),
    ({"NEKRO_DOCKER_GUARD_ACTIVE": "maybe", "DOCKER_HOST": "unix:///run/nekro-guard/guard.sock"}, True),
]


@pytest.mark.parametrize(("env", "expected"), GUARD_DETECTION_CASES)
def test_docker_guard_in_path(monkeypatch: pytest.MonkeyPatch, env: Dict[str, str], expected: bool):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert container_manager.docker_guard_in_path() is expected
    assert container_manager.napcat_restart_policy() == ("no" if expected else "unless-stopped")


def test_hardened_compose_declares_the_guard_for_na():
    import yaml

    root = Path(__file__).resolve().parent.parent
    compose = yaml.safe_load((root / "docker" / "docker-compose-x-hardened.yml").read_text(encoding="utf-8"))
    env = compose["services"]["nekro_agent"]["environment"]
    assert f"{container_manager.DOCKER_GUARD_ACTIVE_ENV}=true" in env
    standard = yaml.safe_load((root / "docker" / "docker-compose.yml").read_text(encoding="utf-8"))
    assert not any(str(e).startswith(container_manager.DOCKER_GUARD_ACTIVE_ENV) for e in standard["services"]["nekro_agent"]["environment"])


# ---------------------------------------------------------------------------
# 创建：标准部署逐字不变；走 guard 时建 no
# ---------------------------------------------------------------------------


async def test_standard_deployment_keeps_unless_stopped_and_metadata_verbatim(docker: Dict[str, Any], db: DBAdapterInstance):
    await container_manager.create_container(db, image_tag="latest", auto_pull=False)
    _name, config = docker["created"][0]
    assert config["HostConfig"]["RestartPolicy"] == {"Name": "unless-stopped"}
    assert set(_metadata(await _reload(db))) == {"container_name", "webui_port", "image"}, "标准部署的元数据不该多出任何键"


async def test_guarded_deployment_creates_no_restart_policy_and_records_intent(
    docker: Dict[str, Any],
    db: DBAdapterInstance,
    guarded: None,  # noqa: ARG001
):
    await container_manager.create_container(db, image_tag="latest", auto_pull=False)
    _name, config = docker["created"][0]
    assert config["HostConfig"]["RestartPolicy"] == {"Name": "no"}
    assert _metadata(await _reload(db))["desired_state"] == "running"
    # 这份配置必须过得了 guard 的判据；对照：标准部署那份（unless-stopped + Binds）过不了
    assert _check_restart_policy(config["HostConfig"]) is None
    assert _check_restart_policy({**config["HostConfig"], "RestartPolicy": {"Name": "unless-stopped"}}) is not None


# ---------------------------------------------------------------------------
# 生命周期记录期望状态（只在走 guard 时）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "expected"),
    [("start", "running"), ("restart", "running"), ("stop", "stopped"), ("delete", "stopped")],
)
async def test_lifecycle_actions_record_the_desired_state_when_guarded(
    docker: Dict[str, Any],
    db: DBAdapterInstance,
    guarded: None,  # noqa: ARG001
    action: str,
    expected: str,
):
    _put_container(docker, "exited" if action == "start" else "running")
    await getattr(container_manager, f"{action}_container")(db)
    assert _metadata(await _reload(db))["desired_state"] == expected


async def test_stop_records_the_intent_before_stopping_and_restores_it_when_the_stop_fails(
    docker: Dict[str, Any],
    db: DBAdapterInstance,
    guarded: None,  # noqa: ARG001
):
    """先记后停：两步之间撞上一轮 reconcile 时，刚被停掉的容器不能被当成崩溃又拉起来。"""
    container = _put_container(docker, "running")
    seen_at_stop: List[Optional[str]] = []
    original_stop = container.stop

    async def stop_and_look(t: int = 0) -> None:
        seen_at_stop.append(_metadata(await DBAdapterInstance.get(id=db.id)).get("desired_state"))
        await original_stop(t)

    container.stop = stop_and_look  # type: ignore[method-assign]
    await container_manager.stop_container(db)
    assert seen_at_stop == ["stopped"], "停止动作发出时，期望状态应该已经是 stopped"

    # 停止失败（容器还在跑）：期望还原成 running，否则之后它崩了也没人拉
    failing = _put_container(docker, "running")

    async def failing_stop(t: int = 0) -> None:  # noqa: ARG001
        raise DockerError(500, {"message": "boom"})

    failing.stop = failing_stop  # type: ignore[method-assign]
    with pytest.raises(Exception, match="停止容器|boom|OperationFailed"):
        await container_manager.stop_container(await _reload(db))
    assert _metadata(await _reload(db))["desired_state"] == "running"


@pytest.mark.parametrize("action", ["start", "restart", "stop", "delete"])
async def test_lifecycle_actions_leave_metadata_alone_on_standard_deployments(
    docker: Dict[str, Any],
    db: DBAdapterInstance,
    action: str,
):
    _put_container(docker, "exited" if action == "start" else "running")
    await getattr(container_manager, f"{action}_container")(db)
    assert "desired_state" not in _metadata(await _reload(db))


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------


async def test_reconcile_does_nothing_on_standard_deployments(docker: Dict[str, Any], db: DBAdapterInstance):
    """不走 guard：Docker 的 `unless-stopped` 自己保活，NA 不插手（即使元数据里恰好有 running）。"""
    container = _put_container(docker, "exited")
    await _set_desired(db, "running")
    assert await container_manager.reconcile_container_liveness() == []
    assert container.calls == []


async def test_reconcile_restarts_an_exited_container_that_should_be_running(
    docker: Dict[str, Any],
    db: DBAdapterInstance,
    guarded: None,  # noqa: ARG001
):
    container = _put_container(docker, "exited")
    await _set_desired(db, "running")
    assert await container_manager.reconcile_container_liveness() == [INSTANCE_KEY]
    assert container.calls == ["start"]
    assert container.status == "running"
    # 已经在跑了：下一轮什么都不做
    assert await container_manager.reconcile_container_liveness() == []
    assert container.calls == ["start"]


@pytest.mark.parametrize(
    ("desired", "status", "why"),
    [
        ("stopped", "exited", "运维 / NA 主动停掉的，不能拉回来"),
        (None, "exited", "没有记录的存量容器不碰（不知道它该不该在跑）"),
        ("running", "running", "本来就在跑"),
        ("running", "restarting", "Docker 正在处理，别插手"),
        ("running", "paused", "暂停是运维动作"),
    ],
)
async def test_reconcile_leaves_these_alone(
    docker: Dict[str, Any],
    db: DBAdapterInstance,
    guarded: None,  # noqa: ARG001
    desired: Optional[str],
    status: str,
    why: str,
):
    container = _put_container(docker, status)
    await _set_desired(db, desired)
    assert await container_manager.reconcile_container_liveness() == [], why
    assert container.calls == [], why


async def test_reconcile_skips_absent_containers_and_inactive_or_remote_instances(
    docker: Dict[str, Any],
    db: DBAdapterInstance,
    guarded: None,  # noqa: ARG001
):
    # 容器不存在
    await _set_desired(db, "running")
    assert await container_manager.reconcile_container_liveness() == []

    container = _put_container(docker, "exited")
    for status in ("disabled", "archived", "deleted"):
        db.status = status
        await db.save(update_fields=["status"])
        assert await container_manager.reconcile_container_liveness() == [], status
    db.status = "online"
    db.enabled = False
    await db.save(update_fields=["status", "enabled"])
    assert await container_manager.reconcile_container_liveness() == []

    db.enabled = True
    db.provider = "remote_onebot"
    await db.save(update_fields=["enabled", "provider"])
    assert await container_manager.reconcile_container_liveness() == []
    assert container.calls == []


async def test_reconcile_backs_off_exponentially_and_never_raises(
    docker: Dict[str, Any],
    db: DBAdapterInstance,
    guarded: None,  # noqa: ARG001
):
    container = _put_container(docker, "exited")
    container.start_error = DockerError(403, {"message": "docker-guard denied: 测试"})
    await _set_desired(db, "running")
    min_delay = container_manager.RECONCILE_MIN_DELAY_SECONDS

    assert await container_manager.reconcile_container_liveness(now=1000.0) == []  # 失败不抛
    assert container.calls == ["start"]
    state = container_manager._reconcile_state[INSTANCE_KEY]
    assert (state.next_try, state.delay, state.started_at) == (1000.0 + min_delay, min_delay, None)

    # 退避期内不重试
    await container_manager.reconcile_container_liveness(now=1000.0 + min_delay - 1)
    assert container.calls == ["start"]

    # 到点重试，仍失败 -> 间隔翻倍
    await container_manager.reconcile_container_liveness(now=1000.0 + min_delay)
    assert container.calls == ["start", "start"]
    assert container_manager._reconcile_state[INSTANCE_KEY].delay == min_delay * 2

    # 封顶
    for i in range(20):
        await container_manager.reconcile_container_liveness(now=1e9 + i * 1e6)
    assert container_manager._reconcile_state[INSTANCE_KEY].delay == container_manager.RECONCILE_MAX_DELAY_SECONDS


async def test_a_crash_loop_keeps_backing_off_and_only_a_stable_run_resets_it(
    docker: Dict[str, Any],
    db: DBAdapterInstance,
    guarded: None,  # noqa: ARG001
):
    """拉起成功不等于恢复：刚拉起就又崩的容器要继续退避，跑满一段时间才清零。"""
    container = _put_container(docker, "exited")
    await _set_desired(db, "running")
    reconcile = container_manager.reconcile_container_liveness
    min_delay = container_manager.RECONCILE_MIN_DELAY_SECONDS
    stable = container_manager.RECONCILE_STABLE_SECONDS

    assert await reconcile(now=1000.0) == [INSTANCE_KEY]
    container.status = "exited"  # 立刻又崩
    assert await reconcile(now=1000.0 + min_delay - 1) == [], "退避期内不能再拉起（崩溃循环不能每个周期拉一次）"
    assert await reconcile(now=1000.0 + min_delay) == [INSTANCE_KEY]
    assert container_manager._reconcile_state[INSTANCE_KEY].delay == min_delay * 2  # 第二次崩，间隔翻倍

    # 在跑，但还没跑满稳定期：状态保留
    assert await reconcile(now=1000.0 + min_delay + stable - 1) == []
    assert INSTANCE_KEY in container_manager._reconcile_state
    # 跑满稳定期：清零
    assert await reconcile(now=1000.0 + min_delay + stable) == []
    assert INSTANCE_KEY not in container_manager._reconcile_state
    # 之后再崩，立刻（无退避）拉起
    container.status = "exited"
    assert await reconcile(now=1000.0 + min_delay + stable + 1) == [INSTANCE_KEY]
    assert container_manager._reconcile_state[INSTANCE_KEY].delay == min_delay


async def _second_instance(key: str) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key="onebot_v11",
        instance_key=key,
        display_name=key,
        status="online",
        provider="builtin_napcat",
        tenant_id=1,
        metadata_json=json.dumps({"desired_state": "running"}),
    )


async def test_one_hanging_instance_does_not_hold_up_the_others(
    docker: Dict[str, Any],
    db: DBAdapterInstance,
    guarded: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
):
    """某个实例的 `container.start()` 挂住（守护进程卡死）时：它自己按时限放弃并退避，别的实例照常拉起。"""
    monkeypatch.setattr(container_manager, "RECONCILE_INSTANCE_TIMEOUT_SECONDS", 0.2)
    await _set_desired(db, "running")
    healthy = _put_container(docker, "exited")
    await _second_instance("hung1")
    hung = _put_container(docker, "exited", key="hung1")

    async def never_returns() -> None:
        await asyncio.Event().wait()

    hung.start = never_returns  # type: ignore[method-assign]
    started_at = time.monotonic()
    result = await container_manager.reconcile_container_liveness(now=1000.0)
    assert time.monotonic() - started_at < 3.0
    assert result == [INSTANCE_KEY]
    assert healthy.status == "running"
    state = container_manager._reconcile_state["hung1"]
    assert state.next_try > 1000.0 and state.started_at is None


async def test_a_hanging_reconcile_never_stalls_the_probe_loop(db: DBAdapterInstance, monkeypatch: pytest.MonkeyPatch):
    """回归：reconcile 曾经 await 在探活前面，`start()` 一挂起全部实例的探活都停摆。"""
    release = asyncio.Event()

    async def hangs() -> List[str]:
        await release.wait()
        return []

    monkeypatch.setattr(container_manager, "reconcile_container_liveness", hangs)
    service = instance_health.InstanceHealthService()
    probe = mock.AsyncMock()
    monkeypatch.setattr(service, "_probe_instance", probe)
    await asyncio.wait_for(service._probe_all(), timeout=2.0)
    probe.assert_awaited_once()
    # 上一轮还在挂着，下一个周期不叠加新任务
    first = service._reconcile_task
    await asyncio.wait_for(service._probe_all(), timeout=2.0)
    assert service._reconcile_task is first
    release.set()
    await first


async def test_stop_arriving_during_a_reconcile_start_is_honoured_and_metadata_is_not_clobbered(
    docker: Dict[str, Any],
    db: DBAdapterInstance,
    guarded: None,  # noqa: ARG001
):
    """一轮 reconcile 里控制台点了「停止」：不能被改回 running，`webui_port` 等别处写入的键也不能被快照覆盖掉。"""
    container = _put_container(docker, "exited")
    await _set_desired(db, "running")
    original_start = container.start

    async def start_while_console_clicks_stop() -> None:
        await original_start()
        await DBAdapterInstance.filter(id=db.id).update(
            metadata_json=json.dumps({"desired_state": "stopped", "webui_port": 4321, "access_token": "tok"}),
        )

    container.start = start_while_console_clicks_stop  # type: ignore[method-assign]
    assert await container_manager.reconcile_container_liveness(now=1000.0) == []
    stored = _metadata(await _reload(db))
    assert stored["desired_state"] == "stopped"
    assert (stored["webui_port"], stored["access_token"]) == (4321, "tok")
    assert any(call.startswith("stop") for call in container.calls), "期间被要求停止，刚拉起的容器应当被停回去"


async def test_metadata_writes_are_read_modify_write_on_the_current_row_not_the_callers_snapshot(db: DBAdapterInstance):
    stale = await _reload(db)  # 快照：此时元数据为空
    await DBAdapterInstance.filter(id=db.id).update(metadata_json=json.dumps({"webui_port": 1234, "access_token": "t"}))
    await container_manager._write_container_metadata(stale, {"desired_state": "running"})
    stored = _metadata(await _reload(db))
    assert stored == {"webui_port": 1234, "access_token": "t", "desired_state": "running"}
    assert _metadata(stale) == stored, "调用方手里的那份也要刷新成最新值"


# ---------------------------------------------------------------------------
# 启动阶段：存量 NapCat 的 RestartPolicy 迁移（走 guard）/ 回滚告警（不走 guard）
# ---------------------------------------------------------------------------


class _LogSink:
    def __init__(self, level: str) -> None:
        from nekro_agent.core.logger import logger as root_logger

        self._logger = root_logger
        self.messages: List[str] = []
        self._id = root_logger.add(lambda m: self.messages.append(str(m)), level=level, format="{level}|{message}")

    def close(self) -> None:
        self._logger.remove(self._id)

    @property
    def text(self) -> str:
        return "\n".join(self.messages)


def _legacy_napcat(docker_state: Dict[str, Any], *, policy: str, status: str = "running", binds: bool = True) -> _FakeContainer:
    container = _put_container(docker_state, status)
    container.restart_policy = policy
    container.binds = ["/srv/na:/app/nekro_agent_data"] if binds else []
    return container


async def test_guarded_startup_migrates_unless_stopped_napcat_to_no_and_records_the_intent(
    docker: Dict[str, Any],
    db: DBAdapterInstance,
    guarded: None,  # noqa: ARG001
):
    container = _legacy_napcat(docker, policy="unless-stopped", status="running")
    await container_manager.audit_napcat_restart_policies()
    assert docker["updates"] == [(f"containers/{container.id}/update", {"RestartPolicy": {"Name": "no"}})]
    assert _metadata(await _reload(db))["desired_state"] == "running"


async def test_guarded_startup_records_stopped_for_an_exited_legacy_container_and_keeps_existing_intent(
    docker: Dict[str, Any],
    db: DBAdapterInstance,
    guarded: None,  # noqa: ARG001
):
    _legacy_napcat(docker, policy="always", status="exited")
    await container_manager.audit_napcat_restart_policies()
    assert _metadata(await _reload(db))["desired_state"] == "stopped"

    # 已经有记录的不覆盖
    fresh = await _set_desired(await _reload(db), "running")
    docker["updates"].clear()
    _legacy_napcat(docker, policy="always", status="exited")
    await container_manager.audit_napcat_restart_policies()
    assert _metadata(await _reload(fresh))["desired_state"] == "running"


@pytest.mark.parametrize(
    ("policy", "binds", "label_owner", "why"),
    [
        ("no", True, None, "已经是 no"),
        ("", True, None, "空 = 不重启"),
        ("unless-stopped", False, None, "没有挂载源可换，不在这条防线的范围内"),
        ("unless-stopped", True, "someone-elses-deployment", "邻居部署的容器不归我们管"),
    ],
)
async def test_guarded_startup_leaves_these_containers_alone(
    docker: Dict[str, Any],
    db: DBAdapterInstance,
    guarded: None,  # noqa: ARG001
    policy: str,
    binds: bool,
    label_owner: Optional[str],
    why: str,
):
    container = _legacy_napcat(docker, policy=policy, binds=binds)
    if label_owner is not None:
        container.labels[container_manager.LABEL_OWNER] = label_owner
    sink = _LogSink("ERROR")
    try:
        await container_manager.audit_napcat_restart_policies()
    finally:
        sink.close()
    assert docker["updates"] == [], why
    # 邻居的容器是「跳过」，不是「检查时出错」：后者会在每次启动刷一条 ERROR
    assert sink.text == "", sink.text
    assert "desired_state" not in _metadata(await _reload(db)), why


async def test_guarded_startup_names_the_container_when_the_migration_fails(
    docker: Dict[str, Any],
    db: DBAdapterInstance,
    guarded: None,  # noqa: ARG001
):
    container = _legacy_napcat(docker, policy="unless-stopped")
    docker["update_error"] = DockerError(403, {"message": "docker-guard denied: 测试"})
    sink = _LogSink("ERROR")
    try:
        await container_manager.audit_napcat_restart_policies()  # 不抛
    finally:
        sink.close()
    assert container.name in sink.text and "unless-stopped" in sink.text and "删除" in sink.text, sink.text
    assert "desired_state" not in _metadata(await _reload(db)), "没改成 no，就不该宣称由 NA 保活"


async def test_standard_startup_warns_about_no_policy_containers_after_a_rollback_and_changes_nothing(
    docker: Dict[str, Any],
    db: DBAdapterInstance,  # noqa: ARG001
):
    container = _legacy_napcat(docker, policy="no")
    sink = _LogSink("WARNING")
    try:
        await container_manager.audit_napcat_restart_policies()
    finally:
        sink.close()
    assert container.name in sink.text and "回滚" in sink.text and "删除" in sink.text, sink.text
    assert docker["updates"] == []

    # 对照：标准部署里本来就是 unless-stopped 的容器不告警
    docker["containers"].clear()
    _legacy_napcat(docker, policy="unless-stopped")
    quiet = _LogSink("WARNING")
    try:
        await container_manager.audit_napcat_restart_policies()
    finally:
        quiet.close()
    assert "RestartPolicy" not in quiet.text, quiet.text


async def test_startup_scan_without_docker_does_not_raise(monkeypatch: pytest.MonkeyPatch, db: DBAdapterInstance):  # noqa: ARG001
    def broken(*_a: Any, **_k: Any) -> Any:
        raise OSError("no docker socket")

    monkeypatch.setattr(container_manager.aiodocker, "Docker", broken)
    await container_manager.audit_napcat_restart_policies()


async def test_health_service_runs_the_startup_scan_and_cancels_background_tasks_on_stop(
    db: DBAdapterInstance,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
):
    audit = mock.AsyncMock()
    monkeypatch.setattr(container_manager, "audit_napcat_restart_policies", audit)
    service = instance_health.InstanceHealthService()
    await service.start()
    assert service._audit_task is not None
    await service._audit_task
    audit.assert_awaited_once()
    await service.stop()


# ---------------------------------------------------------------------------
# 接线：探活循环真的调用了它（零件对、调用点没了，上面的用例会全绿）
# ---------------------------------------------------------------------------


async def test_probe_loop_calls_reconcile_even_when_there_is_nothing_to_probe(db: DBAdapterInstance, monkeypatch: pytest.MonkeyPatch):
    await DBAdapterInstance.filter(id=db.id).update(status="disabled")  # 没有可探活的实例：_probe_all 本会提前返回
    reconcile = mock.AsyncMock(return_value=[])
    monkeypatch.setattr(container_manager, "reconcile_container_liveness", reconcile)
    service = instance_health.InstanceHealthService()
    await service._probe_all()
    assert service._reconcile_task is not None
    await service._reconcile_task
    reconcile.assert_awaited_once()


async def test_reconcile_failure_never_breaks_probing(db: DBAdapterInstance, monkeypatch: pytest.MonkeyPatch):
    reconcile = mock.AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(container_manager, "reconcile_container_liveness", reconcile)
    probe = mock.AsyncMock()
    service = instance_health.InstanceHealthService()
    monkeypatch.setattr(service, "_probe_instance", probe)
    await service._probe_all()  # 不抛
    assert service._reconcile_task is not None
    await service._reconcile_task  # 任务内部吞掉异常
    reconcile.assert_awaited_once()
    probe.assert_awaited_once()  # 探活照常进行
