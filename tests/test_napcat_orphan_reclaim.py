"""孤儿 NapCat 容器与数据目录的盘点与回收

## 这条测试要防的是什么

测试部署上的实证（2026-08-19）：`nekro_napcat_inst1` / `inst2` 建于 07-31，库里同名的
两行 `create_time` 是 08-13 —— 建容器的那两行早被 `purge_instance()` 删掉了，而它
**从不碰容器**。两个容器于是卡在无限刷登录二维码的循环里空转 19 天，
合计 1.2 GB 内存 + 5.7 GB 磁盘。

`container_manager` 里每一个能停 / 删容器的函数都以 `DBAdapterInstance` 为入口，
所以库里那一行没了之后，WebUI 上一个出口都没有。本模块钉的就是这个出口的判据。

## 钉死的六条

1. **判据表**：没有行 → `ORPHANED`；行还在且仍是内置形态 → `IN_USE`（拒绝）；
   行还在但已改成别的形态 → `PROVIDER_CHANGED`（可回收，理由单独标出）。
2. **`napcat_data/napcat` 与 `napcat_data/QQ` 永远不是实例目录**。前者全小写、
   过得了 `INSTANCE_KEY_PATTERN`，少了保留名单会把单实例部署的登录态判成孤儿删掉。
3. **不属于本部署的容器一个都不出现**：归属校验走 `container_manager._assert_owned`，
   本模块不另写判据。
4. **判定在执行前重跑**：预览与确认之间有人把实例重新建成内置形态时，执行必须拒绝。
   这是「预览的结论不作数」那条不变式，与孤儿隔离规则同源。
5. **`remove_data_dir` 默认不删**：容器可重建，登录态删了要重新扫码。
6. **数据目录不能逃出 `napcat_data/`**：`validate_instance_key()` 挡的是 `../`，
   这一条挡的是符号链接。
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import aiodocker
import pytest
from aiodocker.exceptions import DockerError
from tortoise import Tortoise

from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.schemas.errors import PermissionDeniedError, ValidationError
from nekro_agent.services.console import container_manager, instance_manager, napcat_orphans
from nekro_agent.services.console.napcat_orphans import (
    NapcatOrphanRefused,
    OrphanVerdict,
    execute_napcat_orphan_cleanup,
    plan_napcat_orphan_cleanup,
)

OWNER_TAG = "testscope"
ACTIONS: List[str] = []
DEAD_KEY = "inst1"  # 库里没有行 —— 测试部署上那两个的形态之一
LIVE_KEY = "inst2"  # 行还在，仍是内置形态
MOVED_KEY = "inst3"  # 行还在，已改成外部协议端 —— 测试部署上 inst1/inst2 的真实形态


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        for instance_key, provider in ((LIVE_KEY, "builtin_napcat"), (MOVED_KEY, "remote_onebot")):
            await DBAdapterInstance.create(
                adapter_key="onebot_v11",
                instance_key=instance_key,
                display_name=instance_key,
                status="online",
                provider=provider,
                metadata_json="",
            )
        yield
    finally:
        await Tortoise.close_connections()


def _make_data_dir(root: Path, name: str, size: int = 32) -> Path:
    """在 `napcat_data/` 下造一个带内容的目录"""
    directory = root / "napcat_data" / name
    (directory / "QQ").mkdir(parents=True, exist_ok=True)
    (directory / "QQ" / "state.bin").write_bytes(b"x" * size)
    return directory


class _FakeContainer:
    """只实现本文件用到的那几个方法；动作记进 `actions` 供断言"""

    def __init__(self, info: Dict[str, Any], deleted: List[str], actions: Optional[List[str]] = None) -> None:
        self._info = info
        self._deleted = deleted
        self._actions = actions if actions is not None else []

    @property
    def _name(self) -> str:
        return str(self._info.get("Name", "")).lstrip("/")

    async def show(self) -> Dict[str, Any]:
        return self._info

    async def delete(self, force: bool = False) -> None:  # noqa: FBT001, FBT002 — 对齐 aiodocker 签名
        self._deleted.append(self._name)
        self._actions.append(f"delete:{self._name}")

    async def stop(self, t: int = 15) -> None:
        self._actions.append(f"stop:{self._name}")

    async def start(self) -> None:
        self._actions.append(f"start:{self._name}")

    async def restart(self, t: int = 15) -> None:
        self._actions.append(f"restart:{self._name}")


class _FakeDockerContainers:
    def __init__(self, infos: List[Dict[str, Any]], deleted: List[str], actions: List[str]) -> None:
        self._infos = infos
        self._deleted = deleted
        self._actions = actions

    async def list(self, all: bool = False) -> List[_FakeContainer]:  # noqa: A002, FBT001, FBT002
        return [_FakeContainer(info, self._deleted, self._actions) for info in self._infos]

    async def get(self, name: str) -> _FakeContainer:
        for info in self._infos:
            if str(info.get("Name", "")).lstrip("/") == name:
                return _FakeContainer(info, self._deleted, self._actions)
        raise DockerError(404, {"message": f"no such container: {name}"})


class _FakeDocker:
    def __init__(self, infos: List[Dict[str, Any]], deleted: List[str], actions: List[str]) -> None:
        self.containers = _FakeDockerContainers(infos, deleted, actions)

    async def close(self) -> None:
        return None


def _container_info(instance_key: str, data_root: Path, *, owner: str = OWNER_TAG) -> Dict[str, Any]:
    """构造一份足以通过 `_assert_owned` 的 inspect 结果"""
    base = data_root / "napcat_data" / instance_key
    return {
        "Name": f"/nekro_napcat_{instance_key}",
        "Config": {
            "Labels": {
                "nekro.napcat.instance": instance_key,
                "nekro.napcat.owner": owner,
            },
        },
        "State": {"Status": "running"},
        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
        "Mounts": [
            {"Destination": "/app/napcat/config", "Source": str(base / "napcat")},
            {"Destination": "/app/.config/QQ", "Source": str(base / "QQ")},
        ],
    }


@pytest.fixture
def host(tmp_path, monkeypatch):
    """把 DATA_DIR 与归属标签指到临时目录，并接管 aiodocker

    返回 `(data_root, infos, deleted)`：往 `infos` 里塞 inspect 结果就等于宿主上有那个容器，
    `deleted` 收集被删掉的容器名。`ACTIONS` 收集全部容器动作（供 `container_manager` 那组用）。
    """
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(napcat_orphans, "get_owner_tag", lambda: OWNER_TAG)
    monkeypatch.setattr(container_manager, "get_owner_tag", lambda: OWNER_TAG)
    infos: List[Dict[str, Any]] = []
    deleted: List[str] = []
    ACTIONS.clear()
    # 两个模块都是 `import aiodocker` + 调用时取属性，patch 共享的模块属性一次即可
    monkeypatch.setattr(aiodocker, "Docker", lambda *a, **k: _FakeDocker(infos, deleted, ACTIONS))
    return tmp_path, infos, deleted


def _verdicts(plan) -> Dict[str, OrphanVerdict]:
    return {c.instance_key: c.verdict for c in plan.candidates}


# ---------------------------------------------------------------------------
# 1. 判据表
# ---------------------------------------------------------------------------


async def test_no_db_row_is_orphan(db, host):
    """库里没有任何一行认领这个标识 → 可回收"""
    data_root, infos, _deleted = host
    _make_data_dir(data_root, DEAD_KEY)
    infos.append(_container_info(DEAD_KEY, data_root))

    plan = await plan_napcat_orphan_cleanup()

    assert _verdicts(plan)[DEAD_KEY] is OrphanVerdict.ORPHANED
    candidate = next(c for c in plan.candidates if c.instance_key == DEAD_KEY)
    assert candidate.reclaimable
    # 重启策略要回显出来：孤儿之所以能空转 19 天就是因为它
    assert candidate.container_restart_policy == "unless-stopped"
    assert candidate.data_dir_bytes == 32


async def test_builtin_napcat_row_is_refused(db, host):
    """行还在且仍是内置形态 → 拒绝，且 `execute` 也要拒绝"""
    data_root, infos, deleted = host
    _make_data_dir(data_root, LIVE_KEY)
    infos.append(_container_info(LIVE_KEY, data_root))

    plan = await plan_napcat_orphan_cleanup()
    assert _verdicts(plan)[LIVE_KEY] is OrphanVerdict.IN_USE
    assert not next(c for c in plan.candidates if c.instance_key == LIVE_KEY).reclaimable

    with pytest.raises(NapcatOrphanRefused):
        await execute_napcat_orphan_cleanup(LIVE_KEY, remove_data_dir=True)

    assert deleted == []
    assert (data_root / "napcat_data" / LIVE_KEY).exists()


async def test_provider_changed_row_is_reclaimable_but_labelled(db, host):
    """行还在、协议端已搬去外部 → 可回收，但理由必须与「实例被删了」区分开

    这是测试部署上 inst1/inst2 的真实形态。
    """
    data_root, infos, _deleted = host
    _make_data_dir(data_root, MOVED_KEY)
    infos.append(_container_info(MOVED_KEY, data_root))

    plan = await plan_napcat_orphan_cleanup()
    candidate = next(c for c in plan.candidates if c.instance_key == MOVED_KEY)

    assert candidate.verdict is OrphanVerdict.PROVIDER_CHANGED
    assert candidate.reclaimable
    # 理由里要点出新形态，否则运维分不清「实例没了」和「实例改了接入方式」
    assert "remote_onebot" in candidate.reason


# ---------------------------------------------------------------------------
# 2. 保留目录名
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reserved", ["napcat", "QQ"])
async def test_reserved_layout_dirs_are_never_candidates(db, host, reserved):
    """单实例年代的 `napcat_data/napcat` 与 `napcat_data/QQ` 不是实例目录

    这两个目录**存在、有内容、且库里没有同名行** —— 也就是说保留名单一旦失守，
    它们会被判成 `ORPHANED` 并可以被一键删掉，代价是单实例部署的登录态没了。
    """
    data_root, _infos, _deleted = host
    _make_data_dir(data_root, reserved)

    plan = await plan_napcat_orphan_cleanup()

    assert reserved not in _verdicts(plan)


# ---------------------------------------------------------------------------
# 3. 归属
# ---------------------------------------------------------------------------


async def test_foreign_container_is_not_enumerated(db, host):
    """归属标签指向另一套部署的容器，一个都不该出现"""
    data_root, infos, _deleted = host
    infos.append(_container_info(DEAD_KEY, Path("/srv/other-deploy"), owner="someone-else"))

    plan = await plan_napcat_orphan_cleanup()

    assert _verdicts(plan) == {}


async def test_docker_unavailable_still_reports_data_dirs(db, host, monkeypatch):
    """读不到容器清单时，目录侧的结论仍然有效 —— 磁盘大头本来就在目录"""
    data_root, _infos, _deleted = host
    _make_data_dir(data_root, DEAD_KEY)

    def _boom(*_a, **_k):
        raise RuntimeError("docker socket 不可用")

    monkeypatch.setattr(napcat_orphans.aiodocker, "Docker", _boom)

    plan = await plan_napcat_orphan_cleanup()

    assert not plan.docker_available
    assert plan.docker_unavailable_reason
    assert _verdicts(plan)[DEAD_KEY] is OrphanVerdict.ORPHANED
    assert plan.reclaimable_bytes == 32


# ---------------------------------------------------------------------------
# 4. 执行
# ---------------------------------------------------------------------------


async def test_execute_removes_container_and_keeps_data_by_default(db, host):
    """默认只删容器：登录态删掉要重新扫码，不可逆程度差一个量级"""
    data_root, infos, deleted = host
    _make_data_dir(data_root, DEAD_KEY)
    infos.append(_container_info(DEAD_KEY, data_root))

    outcome = await execute_napcat_orphan_cleanup(DEAD_KEY, remove_data_dir=False)

    assert outcome.container_removed
    assert not outcome.data_dir_removed
    assert outcome.freed_bytes == 0
    assert outcome.failures == ()
    assert deleted == [f"nekro_napcat_{DEAD_KEY}"]
    assert (data_root / "napcat_data" / DEAD_KEY).exists()


async def test_execute_can_remove_data_dir(db, host):
    data_root, infos, deleted = host
    _make_data_dir(data_root, DEAD_KEY)
    infos.append(_container_info(DEAD_KEY, data_root))

    outcome = await execute_napcat_orphan_cleanup(DEAD_KEY, remove_data_dir=True)

    assert outcome.container_removed
    assert outcome.data_dir_removed
    assert outcome.freed_bytes == 32
    assert deleted == [f"nekro_napcat_{DEAD_KEY}"]
    assert not (data_root / "napcat_data" / DEAD_KEY).exists()


async def test_verdict_is_recomputed_at_execution_time(db, host):
    """预览说是孤儿，确认之前有人把它重新建成内置形态 → 执行必须拒绝

    这是「预览的结论不作数」那条不变式。把 `execute` 里的重跑改成复用预览结果，
    这条会红。
    """
    data_root, infos, deleted = host
    _make_data_dir(data_root, DEAD_KEY)
    infos.append(_container_info(DEAD_KEY, data_root))

    plan = await plan_napcat_orphan_cleanup()
    assert _verdicts(plan)[DEAD_KEY] is OrphanVerdict.ORPHANED

    await DBAdapterInstance.create(
        adapter_key="onebot_v11",
        instance_key=DEAD_KEY,
        display_name=DEAD_KEY,
        status="online",
        provider="builtin_napcat",
        metadata_json="",
    )

    with pytest.raises(NapcatOrphanRefused):
        await execute_napcat_orphan_cleanup(DEAD_KEY, remove_data_dir=True)

    assert deleted == []
    assert (data_root / "napcat_data" / DEAD_KEY).exists()


async def test_unknown_key_is_refused(db, host):
    """宿主上根本没有的标识 —— 不能因为「库里也没有」就当成孤儿放行"""
    with pytest.raises(NapcatOrphanRefused):
        await execute_napcat_orphan_cleanup("nosuchkey", remove_data_dir=True)


# ---------------------------------------------------------------------------
# 5. 接线：`purge_instance` 真的会走到这套回收上
# ---------------------------------------------------------------------------
#
# 零件对了不等于调用点在。这个洞的形态恰恰是「回收逻辑压根没被调用」——
# 所以这一组驱动真正的 `purge_instance`，判据是宿主上有没有留下痕迹。


@pytest.fixture
def no_vectors(monkeypatch):
    """向量库不参与本契约；它不可用时产品代码本来就只是告警"""

    async def _noop(_instance_key: str) -> None:
        return None

    monkeypatch.setattr(instance_manager, "purge_instance_vectors", _noop)


async def test_purge_reclaims_builtin_container_and_data(db, host, no_vectors):
    """彻底删除内置 NapCat 实例 → 容器与数据目录一起回收

    把 `_reclaim_purged_instance_container` 的调用去掉，这条会红——它就是那个
    「十二张表全清了、容器还在跑」的洞。
    """
    data_root, infos, deleted = host
    _make_data_dir(data_root, LIVE_KEY)
    infos.append(_container_info(LIVE_KEY, data_root))
    instance = await DBAdapterInstance.get(instance_key=LIVE_KEY)

    await instance_manager.purge_instance(instance)

    assert deleted == [f"nekro_napcat_{LIVE_KEY}"]
    assert not (data_root / "napcat_data" / LIVE_KEY).exists()


async def test_purge_leaves_non_builtin_instances_alone(db, host, no_vectors):
    """实例不是内置形态 → 「删实例」不替人决定宿主上那个同名容器的死活

    那一档归孤儿盘点管，它有完整判据表与二次确认。
    """
    data_root, infos, deleted = host
    _make_data_dir(data_root, MOVED_KEY)
    infos.append(_container_info(MOVED_KEY, data_root))
    instance = await DBAdapterInstance.get(instance_key=MOVED_KEY)

    await instance_manager.purge_instance(instance)

    assert deleted == []
    assert (data_root / "napcat_data" / MOVED_KEY).exists()


async def test_purge_survives_docker_failure(db, host, no_vectors, monkeypatch):
    """Docker 侧炸了不能让「实例已删除」这个既成事实变成 500

    数据库那一半已经提交，抛出去只会让调用方以为整个删除失败。
    """
    data_root, infos, _deleted = host
    _make_data_dir(data_root, LIVE_KEY)
    infos.append(_container_info(LIVE_KEY, data_root))
    instance = await DBAdapterInstance.get(instance_key=LIVE_KEY)

    def _boom(*_a, **_k):
        raise RuntimeError("docker socket 不可用")

    monkeypatch.setattr(aiodocker, "Docker", _boom)

    counts = await instance_manager.purge_instance(instance)

    # 级联统计照常返回，说明数据库那一半确实走完了
    assert counts["instance_domain_binding"] == 0
    assert await DBAdapterInstance.filter(instance_key=LIVE_KEY).count() == 0


# ---------------------------------------------------------------------------
# 6. 回收方向的两个豁免：provider 变了之后，停 / 删仍然够得着
# ---------------------------------------------------------------------------


async def test_stop_and_delete_ignore_provider(db, host):
    """实例已改成外部协议端接入，宿主上那个容器仍然停得掉、删得掉

    改造前这两个操作看 `provider`，于是孤儿制造出来之后出口是封死的，
    只能 SSH 上去 `docker rm`。把 `_require_builtin_napcat` 加回这两个函数，这条会红。
    """
    data_root, infos, deleted = host
    infos.append(_container_info(MOVED_KEY, data_root))
    instance = await DBAdapterInstance.get(instance_key=MOVED_KEY)

    await container_manager.stop_container(instance)
    await container_manager.delete_container(instance)

    assert f"stop:nekro_napcat_{MOVED_KEY}" in ACTIONS
    assert deleted == [f"nekro_napcat_{MOVED_KEY}"]


@pytest.mark.parametrize("action", ["start", "restart"])
async def test_additive_operations_still_require_builtin(db, host, monkeypatch, action):
    """让宿主上的东西**变多**的方向一律保持严格

    豁免只给回收方向。这条钉的是「别顺手把整条校验删掉」。
    """
    data_root, infos, _deleted = host
    infos.append(_container_info(MOVED_KEY, data_root))
    instance = await DBAdapterInstance.get(instance_key=MOVED_KEY)

    async def _noop(_instance) -> None:
        return None

    monkeypatch.setattr(container_manager, "resync_network_config", _noop)

    with pytest.raises(ValidationError):
        if action == "start":
            await container_manager.start_container(instance)
        else:
            await container_manager.restart_container(instance)

    assert not any(a.startswith(action) for a in ACTIONS)


async def test_ownership_check_still_gates_the_exempted_operations(db, host):
    """豁免的是 provider 这一维，**归属校验一分未松**

    另一套部署的容器仍然停不掉、删不掉——否则这次放宽就成了跨部署接管的新出口。
    """
    _data_root, infos, deleted = host
    infos.append(_container_info(MOVED_KEY, Path("/srv/other-deploy"), owner="someone-else"))
    instance = await DBAdapterInstance.get(instance_key=MOVED_KEY)

    with pytest.raises(PermissionDeniedError):
        await container_manager.delete_container(instance)
    with pytest.raises(PermissionDeniedError):
        await container_manager.stop_container(instance)

    assert deleted == []
    assert ACTIONS == []


async def test_data_dir_escaping_root_is_refused(host, monkeypatch, tmp_path):
    """数据目录 `resolve()` 之后逃出 `napcat_data/` → 拒绝

    直接测判定函数：符号链接在 Windows 上要额外权限，用一个显式的外部路径等价。
    """
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    with pytest.raises(NapcatOrphanRefused):
        napcat_orphans._assert_data_dir_within_root(DEAD_KEY, outside)  # noqa: SLF001

    # 正常路径不该被这条误伤
    inside = tmp_path / "napcat_data" / DEAD_KEY
    inside.mkdir(parents=True)
    napcat_orphans._assert_data_dir_within_root(DEAD_KEY, inside)  # noqa: SLF001
