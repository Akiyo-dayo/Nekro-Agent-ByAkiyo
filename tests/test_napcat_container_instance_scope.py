"""NapCat 容器管理端点的实例作用域

多实例改造把容器创建改成了按实例（`container_manager.CONTAINER_NAME_TEMPLATE` =
`nekro_napcat_{instance_key}`），但 `adapters/onebot_v11/routers.py` 的消费端还在取
全局配置 `NAPCAT_CONTAINER_NAME`（默认 `nekro_napcat`）。于是凡是走 `get_container()`
的端点在多实例部署下**必然** 404 —— 实测 `/container/napcat-token` 就是这样 500 的。

这里钉死四条：

1. **按实例寻址**：给了 `instance_key` 就必须去找 `nekro_napcat_{instance_key}`，
   容器名由 `container_manager.build_container_name()` 生成，不在 adapter 侧另写一套；
2. **老部署不能因为升级而失联**：单实例时代那个不带归属标签的 `nekro_napcat` 容器，
   在实例模板容器不存在时仍要被找到。容器名因此是一个有序候选列表，而不是单值；
3. **整组端点是 `Role.Super`**：`/logs` 会把 NapCat 启动日志原样吐出来，里面带着 WebUI
   访问令牌（前端 `logs.tsx` 正是从日志里抠它），与 `/napcat-token` 是同一件事；
   而且 `instance_key` 参数化之后，停在 `Role.Admin` 等于让租户管理员填别人的实例标识
   去读别人的协议端日志；
4. **`get_container()` 不再改写调用方的异常**：旧写法把 `yield` 也包在 `except Exception`
   里，端点自己抛的业务错误会被统统改写成"获取容器失败"。
5. **模板名那一支要过归属校验**：模板名是**宿主全局**的且不带部署
   前缀，同宿主上另一套部署里同名的 `instance_key` 会解析到同一个容器，于是这批端点会成为
   「读走邻居的 WebUI 令牌 / 重启邻居的协议端」的第二个出口。判据与总后台同一份
   （`container_manager.assert_container_owned`）。**遗留名那一支不校验**——那个容器没有标签，
   校验它等于让所有单实例遗留部署集体 403，第 2 条会当场红。
   跨部署被拒的正面用例在 `tests/test_napcat_container_owner_tag.py`。
"""

import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from aiodocker.exceptions import DockerError
from fastapi import HTTPException
from tortoise import Tortoise

from nekro_agent.adapters.onebot_v11 import routers
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.console import container_manager
from nekro_agent.services.user.role import Role

LEGACY_CONTAINER = "nekro_napcat"
INSTANCE_KEY = "inst1"
REMOTE_INSTANCE_KEY = "remote1"


def scoped_container() -> str:
    """当前口径的模板容器名（调产品函数现算，§2.9：不写死由环境派生的值）"""
    return container_manager.build_container_name(INSTANCE_KEY)


def bare_template_container() -> str:
    """升级前的裸模板容器名（容器名带上部署前缀之前建的容器顶着它）"""
    return container_manager.build_legacy_container_name(INSTANCE_KEY)


ACME_TENANT_ID = 1


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        for instance_key, provider in ((INSTANCE_KEY, "builtin_napcat"), (REMOTE_INSTANCE_KEY, "remote_onebot")):
            await DBAdapterInstance.create(
                adapter_key="onebot_v11",
                instance_key=instance_key,
                display_name=instance_key,
                status="online",
                provider=provider,
                tenant_id=ACME_TENANT_ID,
                metadata_json="",
            )
        yield
    finally:
        await Tortoise.close_connections()


class _StubAdapterConfig:
    def __init__(self, container_name: str) -> None:
        self.NAPCAT_CONTAINER_NAME = container_name


class _StubAdapter:
    def __init__(self, container_name: str) -> None:
        self.config = _StubAdapterConfig(container_name)


@pytest.fixture
def legacy_name(monkeypatch) -> List[str]:
    """把适配器配置换成可控值，返回一个单元素列表以便用例就地改写

    `legacy_container_name()` 走的是 `adapter_utils.get_typed_adapter`，真实实现要求
    适配器已经加载完毕；这里替掉的是取适配器这一步，被测的判定逻辑本身照常执行。
    """
    holder = [LEGACY_CONTAINER]
    monkeypatch.setattr(routers.adapter_utils, "get_typed_adapter", lambda *_a, **_kw: _StubAdapter(holder[0]))
    return holder


def _labels_a_real_container_would_carry(name: str) -> Dict[str, str]:
    """按容器名还原 `create_container` 当初写进去的那两个归属标签

    模板名的容器**只可能**是 `container_manager.create_container()` 建的，而它每次都写
    `nekro.napcat.instance` / `nekro.napcat.owner`；遗留名那个由 compose / 运维建，
    身上一个标签都没有。fake 照抄这条事实，否则 `get_container()` 的归属校验
    （见模块 docstring 第 5 条）会在一个现实中不存在的形态上被测。

    模板段 `nekro_napcat_` 之后的尾段就是 `instance_key`，带前缀的新名与升级前的裸名
    都满足这一形态；遗留配置名不满足，返回空标签。

    归属标签的取值**调产品自己的函数现算**，不写默认环境下的字面量。
    """
    prefix = container_manager.CONTAINER_NAME_TEMPLATE.format(instance_key="")
    _deployment_prefix, sep, tail = name.rpartition(prefix)
    if not sep or not tail:
        return {}
    return {
        container_manager.LABEL_INSTANCE: tail,
        container_manager.LABEL_OWNER: container_manager.get_owner_tag(),
    }


class FakeContainer:
    """只实现被测端点用到的那几个 Docker 容器动作"""

    def __init__(
        self,
        name: str,
        *,
        running: bool = True,
        logs: Optional[List[str]] = None,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        self.name = name
        self.running = running
        self.logs = logs or []
        self.restart_calls: List[Dict[str, Any]] = []
        self.labels = _labels_a_real_container_would_carry(name) if labels is None else dict(labels)

    async def show(self) -> Dict[str, Any]:
        return {
            "Name": f"/{self.name}",
            "Config": {"Labels": dict(self.labels)},
            "State": {"Running": self.running, "StartedAt": f"2026-01-01T00:00:00Z/{self.name}"},
        }

    async def log(self, **_kwargs: Any) -> List[str]:
        return list(self.logs)

    async def restart(self, **kwargs: Any) -> None:
        self.restart_calls.append(dict(kwargs))


class FakeDocker:
    def __init__(self, existing: Dict[str, FakeContainer], attempts: List[str]) -> None:
        self._existing = existing
        self._attempts = attempts
        self.closed = False
        self.containers = self

    async def get(self, name: str) -> FakeContainer:
        self._attempts.append(name)
        container = self._existing.get(name)
        if container is None:
            raise DockerError(404, {"message": f"No such container: {name}"})
        return container

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def docker(monkeypatch):
    """接管 Docker 客户端，记录每次按名字取容器的尝试顺序"""

    state: Dict[str, Any] = {"existing": {}, "attempts": [], "clients": []}

    async def fake_get_docker() -> FakeDocker:
        client = FakeDocker(state["existing"], state["attempts"])
        state["clients"].append(client)
        return client

    monkeypatch.setattr(routers, "get_docker", fake_get_docker)
    return state


def put_container(docker_state: Dict[str, Any], name: str, **kwargs: Any) -> FakeContainer:
    container = FakeContainer(name, **kwargs)
    docker_state["existing"][name] = container
    return container


def make_user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    """构造一个未落库的登录身份；权限判定只读 `perm_level`"""
    return DBUser(
        username=f"user-{tenant_role.value}-{tenant_id}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )


def platform_owner() -> DBUser:
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


def tenant_admin() -> DBUser:
    """租户管理员：`TENANT_MAX_PERM_LEVEL` 把他钉死在 Role.Admin"""
    return make_user(tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


class TestContainerNameCandidates:
    """容器名候选：越新越优先（带前缀模板名 → 裸模板名 → 遗留配置名）"""

    async def test_without_instance_key_uses_legacy_name(self, db, legacy_name) -> None:  # noqa: ARG002
        assert await routers.container_name_candidates(None) == [LEGACY_CONTAINER]
        assert await routers.container_name_candidates("") == [LEGACY_CONTAINER]
        assert await routers.container_name_candidates("   ") == [LEGACY_CONTAINER]

    async def test_instance_key_prefers_template_then_legacy(self, db, legacy_name) -> None:  # noqa: ARG002
        """三段候选按升级路径排序：新名、加部署前缀之前的裸模板名、单实例遗留名"""
        assert await routers.container_name_candidates(INSTANCE_KEY) == [
            scoped_container(),
            bare_template_container(),
            LEGACY_CONTAINER,
        ]

    async def test_template_name_matches_console_template(self, db, legacy_name) -> None:  # noqa: ARG002
        """容器名的唯一来源是 `container_manager`，不在 adapter 侧另拼一份"""
        candidates = await routers.container_name_candidates(INSTANCE_KEY)
        assert candidates[0] == container_manager.build_container_name(INSTANCE_KEY)
        assert candidates[1] == container_manager.build_legacy_container_name(INSTANCE_KEY)

    async def test_legacy_name_not_duplicated(self, db, legacy_name) -> None:  # noqa: ARG002
        """遗留配置名恰好等于某个模板候选时只保留一个"""
        legacy_name[0] = scoped_container()
        assert await routers.container_name_candidates(INSTANCE_KEY) == [
            scoped_container(),
            bare_template_container(),
        ]

    async def test_blank_legacy_name_still_allows_instance_lookup(self, db, legacy_name) -> None:  # noqa: ARG002
        """清空了遗留配置的部署，按实例找容器不受影响（裸模板名候选与配置无关）"""
        legacy_name[0] = ""
        assert await routers.container_name_candidates(INSTANCE_KEY) == [
            scoped_container(),
            bare_template_container(),
        ]

    async def test_blank_legacy_name_without_instance_key_is_rejected(self, db, legacy_name) -> None:  # noqa: ARG002
        legacy_name[0] = ""
        with pytest.raises(ValidationError):
            await routers.container_name_candidates(None)

    async def test_unknown_instance_key_is_404(self, db, legacy_name) -> None:  # noqa: ARG002
        """不存在的实例直接 404，不拿它去 Docker 那里试"""
        with pytest.raises(NotFoundError):
            await routers.container_name_candidates("nope")

    @pytest.mark.parametrize(
        "bad_key",
        [
            pytest.param("../../etc", id="path-traversal"),
            pytest.param("inst1/../x", id="slash"),
            pytest.param("INST1", id="uppercase"),
            pytest.param("inst-1", id="hyphen"),
            pytest.param("a" * 33, id="too-long"),
        ],
    )
    async def test_malformed_instance_key_is_rejected(self, db, legacy_name, bad_key: str) -> None:  # noqa: ARG002
        with pytest.raises(ValidationError):
            await routers.container_name_candidates(bad_key)


class TestGetContainerFallback:
    """`get_container()` 的候选顺序、回退与客户端关闭"""

    async def test_uses_scoped_container_when_present(self, db, legacy_name, docker) -> None:  # noqa: ARG002
        put_container(docker, scoped_container())
        put_container(docker, bare_template_container())
        put_container(docker, LEGACY_CONTAINER)

        async with routers.get_container(INSTANCE_KEY) as container:
            assert container.name == scoped_container()

        assert docker["attempts"] == [scoped_container()]

    async def test_falls_back_to_the_bare_template_name(self, db, legacy_name, docker) -> None:  # noqa: ARG002
        """容器名加部署前缀之后的升级路径：升级前建的容器顶着裸模板名，新名 404 后第二个候选兜住

        漏掉这一档的话，`/logs`（含 WebUI 令牌）与 `/restart` 在升级当天对存量容器集体 404。
        """
        put_container(docker, bare_template_container())

        async with routers.get_container(INSTANCE_KEY) as container:
            assert container.name == bare_template_container()

        assert docker["attempts"] == [scoped_container(), bare_template_container()]

    async def test_falls_back_to_legacy_container(self, db, legacy_name, docker) -> None:  # noqa: ARG002
        """单实例时代升级上来的部署：库里有实例记录，宿主机上只有那个老容器"""
        put_container(docker, LEGACY_CONTAINER)

        async with routers.get_container(INSTANCE_KEY) as container:
            assert container.name == LEGACY_CONTAINER

        assert docker["attempts"] == [scoped_container(), bare_template_container(), LEGACY_CONTAINER]

    async def test_missing_container_raises_not_found(self, db, legacy_name, docker) -> None:  # noqa: ARG002
        with pytest.raises(NotFoundError):
            async with routers.get_container(INSTANCE_KEY):
                pytest.fail("不应该产出容器")

        assert docker["attempts"] == [scoped_container(), bare_template_container(), LEGACY_CONTAINER]
        assert [client.closed for client in docker["clients"]] == [True]

    async def test_client_closed_after_use(self, db, legacy_name, docker) -> None:  # noqa: ARG002
        put_container(docker, scoped_container())

        async with routers.get_container(INSTANCE_KEY):
            assert docker["clients"][0].closed is False

        assert docker["clients"][0].closed is True

    async def test_body_exception_is_not_rewritten(self, db, legacy_name, docker) -> None:  # noqa: ARG002
        """with 语句体里抛出的业务异常必须原样传出，且客户端仍被关闭"""
        put_container(docker, scoped_container())

        with pytest.raises(ValidationError):
            async with routers.get_container(INSTANCE_KEY):
                raise ValidationError(reason="业务判定失败")

        assert docker["clients"][0].closed is True


class TestEndpointsAddressTheRightContainer:
    """端点确实把 `instance_key` 传到了容器查找上"""

    async def test_status_reads_scoped_container(self, db, legacy_name, docker) -> None:  # noqa: ARG002
        put_container(docker, scoped_container(), running=True)
        put_container(docker, LEGACY_CONTAINER, running=False)

        status = await routers.get_status(instance_key=INSTANCE_KEY, _current_user=platform_owner())

        assert status.running is True
        assert status.started_at.endswith(scoped_container())

    async def test_status_without_instance_key_reads_legacy_container(self, db, legacy_name, docker) -> None:  # noqa: ARG002
        put_container(docker, scoped_container(), running=True)
        put_container(docker, LEGACY_CONTAINER, running=False)

        status = await routers.get_status(_current_user=platform_owner())

        assert status.running is False
        assert status.started_at.endswith(LEGACY_CONTAINER)

    async def test_logs_read_scoped_container(self, db, legacy_name, docker) -> None:  # noqa: ARG002
        put_container(docker, scoped_container(), logs=["scoped line"])
        put_container(docker, LEGACY_CONTAINER, logs=["legacy line"])

        logs = await routers.get_logs(tail=10, instance_key=INSTANCE_KEY, _current_user=platform_owner())

        assert logs == ["scoped line"]

    async def test_log_stream_rejects_unknown_instance_before_streaming(self, db, legacy_name, docker) -> None:  # noqa: ARG002
        """建流之前就要拒绝：EventSourceResponse 一旦返回，状态码已经是 200"""
        with pytest.raises(NotFoundError):
            await routers.stream_logs(
                request=None,  # pyright: ignore[reportArgumentType] - 校验发生在碰请求对象之前
                instance_key="nope",
                _current_user=platform_owner(),
            )

    async def test_napcat_token_reads_instance_config(self, db, legacy_name, docker, monkeypatch, tmp_path) -> None:  # noqa: ARG002
        """令牌从实例自己的配置目录读，而不是单实例时代那个固定路径"""
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path))
        monkeypatch.setattr(routers, "NAPCAT_ONEBOT_ADAPTER_DIR", str(tmp_path / "napcat_data" / "napcat"))

        legacy_dir = Path(routers.NAPCAT_ONEBOT_ADAPTER_DIR)
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / "webui.json").write_text('{"token": "legacy-token"}', encoding="utf-8")

        scoped_dir = container_manager.get_napcat_config_dir(INSTANCE_KEY)
        scoped_dir.mkdir(parents=True, exist_ok=True)
        (scoped_dir / "webui.json").write_text('{"token": "scoped-token"}', encoding="utf-8")

        put_container(docker, scoped_container(), running=True)
        put_container(docker, LEGACY_CONTAINER, running=True)

        scoped = await routers.get_napcat_token(instance_key=INSTANCE_KEY, _current_user=platform_owner())
        legacy = await routers.get_napcat_token(_current_user=platform_owner())

        assert scoped.token == "scoped-token"
        assert legacy.token == "legacy-token"

    async def test_napcat_token_falls_back_to_legacy_config(self, db, legacy_name, docker, monkeypatch, tmp_path) -> None:  # noqa: ARG002
        """老部署升级后：库里有实例，配置仍在单实例时代那个目录"""
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path))
        monkeypatch.setattr(routers, "NAPCAT_ONEBOT_ADAPTER_DIR", str(tmp_path / "napcat_data" / "napcat"))

        legacy_dir = Path(routers.NAPCAT_ONEBOT_ADAPTER_DIR)
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / "webui.json").write_text('{"token": "legacy-token"}', encoding="utf-8")

        put_container(docker, LEGACY_CONTAINER, running=True)

        result = await routers.get_napcat_token(instance_key=INSTANCE_KEY, _current_user=platform_owner())

        assert result.token == "legacy-token"

    async def test_restart_targets_scoped_container(self, db, legacy_name, docker, monkeypatch) -> None:  # noqa: ARG002
        """指定实例时复用总后台的网络配置同步，再重启该实例自己的容器"""
        synced: List[str] = []

        async def fake_resync(instance: DBAdapterInstance) -> None:
            synced.append(instance.instance_key)

        monkeypatch.setattr(container_manager, "resync_network_config", fake_resync)
        scoped = put_container(docker, scoped_container())
        legacy = put_container(docker, LEGACY_CONTAINER)

        result = await routers.restart(instance_key=INSTANCE_KEY, _current_user=platform_owner())
        # 重启本身是后台任务，等它跑完再断言
        await _drain_pending_tasks()

        assert result.ok is True
        assert synced == [INSTANCE_KEY]
        assert len(scoped.restart_calls) == 1
        assert legacy.restart_calls == []


class TestContainerEndpointsArePlatformOwnerOnly:
    """整组容器端点都是 `Role.Super`

    `/logs` 与 `/logs/stream` 吐出的 NapCat 启动日志里就带着 WebUI 访问令牌，
    和 `/napcat-token` 是同一件事；`/restart` 是容器生命周期操作，总后台的同类端点
    同样要求 `Role.Super`。参数化之后若停在 `Role.Admin`，租户管理员填个别人的
    `instance_key` 就能读到别人的协议端日志。
    """

    async def test_status_denies_tenant_admin(self, db, legacy_name, docker) -> None:  # noqa: ARG002
        with pytest.raises(HTTPException) as exc:
            await routers.get_status(instance_key=INSTANCE_KEY, _current_user=tenant_admin())
        assert exc.value.status_code == 403
        assert docker["attempts"] == []

    async def test_logs_deny_tenant_admin(self, db, legacy_name, docker) -> None:  # noqa: ARG002
        with pytest.raises(HTTPException) as exc:
            await routers.get_logs(tail=10, instance_key=INSTANCE_KEY, _current_user=tenant_admin())
        assert exc.value.status_code == 403
        assert docker["attempts"] == []

    async def test_log_stream_denies_tenant_admin(self, db, legacy_name, docker) -> None:  # noqa: ARG002
        with pytest.raises(HTTPException) as exc:
            await routers.stream_logs(
                request=None,  # pyright: ignore[reportArgumentType] - 权限在碰请求对象之前就拒绝
                instance_key=INSTANCE_KEY,
                _current_user=tenant_admin(),
            )
        assert exc.value.status_code == 403
        assert docker["attempts"] == []

    async def test_restart_denies_tenant_admin(self, db, legacy_name, docker) -> None:  # noqa: ARG002
        with pytest.raises(HTTPException) as exc:
            await routers.restart(instance_key=INSTANCE_KEY, _current_user=tenant_admin())
        assert exc.value.status_code == 403
        assert docker["attempts"] == []

    async def test_napcat_token_denies_tenant_admin(self, db, legacy_name, docker) -> None:  # noqa: ARG002
        with pytest.raises(HTTPException) as exc:
            await routers.get_napcat_token(instance_key=INSTANCE_KEY, _current_user=tenant_admin())
        assert exc.value.status_code == 403
        assert docker["attempts"] == []

    async def test_onebot_token_denies_tenant_admin(self, db, legacy_name) -> None:  # noqa: ARG002
        with pytest.raises(HTTPException) as exc:
            await routers.get_onebot_token(_current_user=tenant_admin())
        assert exc.value.status_code == 403


async def _drain_pending_tasks() -> None:
    """等待端点用 `asyncio.create_task` 丢出去的后台任务跑完"""
    import asyncio

    pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
