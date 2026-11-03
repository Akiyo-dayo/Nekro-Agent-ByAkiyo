"""容器回连 NA 时取哪个端口：内置 NapCat 走容器网络直连，沙盒走宿主网关

两条路径的端口口径不同，混用任何一边都会让容器打错门：

| 路径 | 地址形态 | 端口 |
| --- | --- | --- |
| 内置 NapCat 反连 | `ws://{NA 容器名}:{端口}/onebot/v11/ws`，容器网络内部直连 | NA 进程实际监听的端口（compose 下容器内恒为 8021） |
| 沙盒回调 | `http://host.docker.internal:{端口}/api`，经宿主网关 | 宿主侧端口：Docker 部署是 `NEKRO_EXPOSE_PORT`，裸机是监听端口 |

修复前 NapCat 的两个写入点（`container_manager.build_managed_entry` 与 `routers.py` 的
`ADAPTER_TEMPLATE` 分支）都拿 `EXPOSE_PORT`：同机第二套部署把它设成 8022 后，NapCat 去连
`nekro_agent:8022`，容器内没人监听，实例一直离线。

沙盒那边默认值的口径本来就对，但默认值在首次启动就被落盘进配置文件，之后再改
`NEKRO_EXPOSE_PORT` 也不跟着变——宿主旧端口上若是邻居部署，RPC 就打到邻居那里。

端口在调用时派生：`OsEnv.EXPOSE_PORT` / `OsEnv.RUN_IN_DOCKER` 换类属性，监听端口换
`nonebot.get_driver()`（`relay_listen_port()` 问的就是它；测试进程里驱动初没初始化取决于
用例执行顺序，不钉住期望值就会漂移）。
"""

from __future__ import annotations

import ast
import contextlib
import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Dict, List, Optional

import pytest

import nekro_agent
from nekro_agent.adapters.onebot_v11 import routers as onebot_routers
from nekro_agent.core.config import CoreConfig, config
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_user import DBUser
from nekro_agent.services.console import container_manager
from nekro_agent.services.sandbox import network_guard
from nekro_agent.services.user.perm import Role

HOST = "nekro_agent"
AUTO_URL = "http://host.docker.internal:{port}/api"

PinDeployment = Callable[..., None]


@pytest.fixture()
def deployment(monkeypatch: pytest.MonkeyPatch) -> PinDeployment:
    """把部署形态钉成确定的三元组：是否在容器里、发布到宿主的端口、进程监听的端口

    `listen_port=None` 表示 nonebot 驱动还没初始化（此时监听端口只能退回 `EXPOSE_PORT`）。
    主机名一并钉成默认回落值，URL 断言因此可以写成完整字面量。
    """
    import nonebot  # noqa: PLC0415  只在需要时引入，避免整份测试依赖驱动层

    def _pin(*, run_in_docker: bool, expose_port: int, listen_port: Optional[int]) -> None:
        monkeypatch.setattr(OsEnv, "RUN_IN_DOCKER", run_in_docker)
        monkeypatch.setattr(OsEnv, "EXPOSE_PORT", expose_port)
        monkeypatch.setattr(OsEnv, "SELF_HOSTNAME", "")
        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", "")
        if listen_port is None:

            def _uninitialized():  # noqa: ANN202
                raise ValueError("driver not initialized")

            monkeypatch.setattr(nonebot, "get_driver", _uninitialized)
        else:
            monkeypatch.setattr(
                nonebot, "get_driver", lambda: SimpleNamespace(config=SimpleNamespace(port=listen_port))
            )

    return _pin


def _unsaved_instance() -> DBAdapterInstance:
    """`build_managed_entry` 只读元数据字段拼 URL，不需要落库"""
    return DBAdapterInstance(
        adapter_key="onebot_v11",
        instance_key="inst_callback_port",
        display_name="回连端口用例",
        provider="builtin_napcat",
        provider_account_id="10086",
        status="online",
        metadata_json="{}",
    )


def _platform_owner() -> DBUser:
    return DBUser(
        username="platform-owner",
        password="",
        adapter_key="",
        platform_userid="0",
        perm_level=Role.Super,
        login_time=datetime.datetime.now(datetime.timezone.utc),
    )


async def _template_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """走真的 `restart()` 落盘路径（适配器文件缺失时按 `ADAPTER_TEMPLATE` 补写），返回写出的反连 URL"""

    class _FakeAdapter:
        config = SimpleNamespace(BOT_QQ="10086")

    @contextlib.asynccontextmanager
    async def _fake_container(_key=None):  # noqa: ANN001, ANN202
        yield SimpleNamespace(restart=_noop_restart)

    config_dir = tmp_path / "napcat_config"
    config_dir.mkdir()
    monkeypatch.setattr(onebot_routers.adapter_utils, "get_typed_adapter", lambda *_a, **_k: _FakeAdapter())
    monkeypatch.setattr(onebot_routers, "get_container", _fake_container)
    monkeypatch.setattr(onebot_routers, "NAPCAT_ONEBOT_ADAPTER_DIR", str(config_dir))
    # 账号不在库里：令牌按部署级令牌回落。这里只看端口，令牌值无关
    monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "irrelevant-to-port")

    await onebot_routers.restart(instance_key=None, _current_user=_platform_owner())

    target = config_dir / "onebot11_10086.json"
    assert target.is_file(), "适配器文件没写出来，测试口径已脱靶"
    return json.loads(target.read_text(encoding="utf-8"))["network"]["websocketClients"][0]["url"]


async def _noop_restart(timeout: int = 30) -> None:  # noqa: ARG001
    return None


# ---------------------------------------------------------------------------
# 1. 内置 NapCat：容器网络直连，端口是 NA 进程监听的那个
# ---------------------------------------------------------------------------


class TestBuiltinNapcatDialsTheListenPort:
    def test_managed_entry_ignores_the_published_port(self, deployment: PinDeployment) -> None:
        """同机第二套部署：宿主侧 8022，容器内仍监听 8021——NapCat 必须连 8021"""
        deployment(run_in_docker=True, expose_port=8022, listen_port=8021)
        entry = container_manager.build_managed_entry(_unsaved_instance())
        assert entry.url == f"ws://{HOST}:8021/onebot/v11/ws"

    async def test_adapter_template_ignores_the_published_port(
        self, deployment: PinDeployment, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """第二个写入点（单实例时代的 `onebot11_{QQ}.json` 模板）同一个口径"""
        deployment(run_in_docker=True, expose_port=8022, listen_port=8021)
        assert await _template_url(monkeypatch, tmp_path) == f"ws://{HOST}:8021/onebot/v11/ws"

    async def test_both_write_points_agree(
        self, deployment: PinDeployment, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """两个写入点在同一部署形态下写出同一条 URL——口径分叉的表现就是协议端连不上"""
        deployment(run_in_docker=True, expose_port=8022, listen_port=8021)
        from_template = await _template_url(monkeypatch, tmp_path)
        assert from_template == container_manager.build_managed_entry(_unsaved_instance()).url

    @pytest.mark.parametrize(
        ("run_in_docker", "expose_port", "listen_port"),
        [
            # 默认 compose 部署：两个端口都是 8021
            (True, 8021, 8021),
            # 驱动还没初始化：监听端口只能退回 EXPOSE_PORT，与修复前逐字相同
            (True, 8022, None),
            (False, 8021, 8021),
        ],
    )
    def test_unchanged_where_the_two_ports_coincide(
        self,
        deployment: PinDeployment,
        run_in_docker: bool,
        expose_port: int,
        listen_port: Optional[int],
    ) -> None:
        """两个端口相等（或只拿得到一个）时，产出与修复前的公式逐字相同"""
        deployment(run_in_docker=run_in_docker, expose_port=expose_port, listen_port=listen_port)
        legacy = f"ws://{HOST}:{OsEnv.EXPOSE_PORT}/onebot/v11/ws"
        assert container_manager.build_managed_entry(_unsaved_instance()).url == legacy


# ---------------------------------------------------------------------------
# 2. 沙盒回调：经宿主网关，端口是宿主侧那个；只纠正自动生成的形态
# ---------------------------------------------------------------------------


@pytest.fixture()
def logged(monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[str]]:
    records: Dict[str, List[str]] = {"warning": []}
    recorder = SimpleNamespace(warning=lambda message: records["warning"].append(message))
    monkeypatch.setattr(network_guard, "logger", recorder)
    return records


@pytest.fixture()
def callback_url(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """设定配置里的回调地址；被测函数直接改 `config` 上的值，这里登记原值让 monkeypatch 负责还原"""

    def _set(value: str) -> None:
        monkeypatch.setattr(config, "SANDBOX_CHAT_API_URL", value)

    return _set


class TestSandboxCallbackUrlIsReconciled:
    @pytest.mark.parametrize(
        ("run_in_docker", "expose_port", "listen_port", "frozen_port", "expected_port"),
        [
            # Docker：首次启动时 EXPOSE_PORT=8021 被落盘，之后改成 8022。
            # 走宿主网关，要的是宿主侧 8022，**不是**容器内监听的 8021
            (True, 8022, 8021, 8021, 8022),
            # 裸机：默认值按 EXPOSE_PORT 生成，而进程实际监听的是 PORT=9000
            (False, 8021, 9000, 8021, 9000),
        ],
    )
    def test_a_frozen_default_follows_the_deployment(
        self,
        deployment: PinDeployment,
        callback_url: Callable[[str], None],
        logged: Dict[str, List[str]],
        run_in_docker: bool,
        expose_port: int,
        listen_port: int,
        frozen_port: int,
        expected_port: int,
    ) -> None:
        deployment(run_in_docker=run_in_docker, expose_port=expose_port, listen_port=listen_port)
        callback_url(AUTO_URL.format(port=frozen_port))

        assert network_guard.reconcile_sandbox_chat_api_url() is True

        assert config.SANDBOX_CHAT_API_URL == AUTO_URL.format(port=expected_port)
        assert len(logged["warning"]) == 1
        # 运维要能从这一条日志看出改了什么：旧值与新值都在
        assert AUTO_URL.format(port=frozen_port) in logged["warning"][0]
        assert AUTO_URL.format(port=expected_port) in logged["warning"][0]

    @pytest.mark.parametrize(
        ("run_in_docker", "expose_port", "listen_port"),
        [
            (True, 8021, 8021),
            (False, 8021, 8021),
            # 驱动没初始化时裸机也只能拿 EXPOSE_PORT，与字段默认值同源，不动
            (False, 8021, None),
        ],
    )
    def test_the_default_deployment_is_left_alone(
        self,
        deployment: PinDeployment,
        callback_url: Callable[[str], None],
        logged: Dict[str, List[str]],
        run_in_docker: bool,
        expose_port: int,
        listen_port: Optional[int],
    ) -> None:
        deployment(run_in_docker=run_in_docker, expose_port=expose_port, listen_port=listen_port)
        callback_url(AUTO_URL.format(port=8021))

        assert network_guard.reconcile_sandbox_chat_api_url() is False

        assert config.SANDBOX_CHAT_API_URL == AUTO_URL.format(port=8021)
        assert logged["warning"] == []

    @pytest.mark.parametrize(
        "handwritten",
        [
            "http://192.168.1.10:8021/api",
            "http://nekro_agent:8021/api",
            "https://host.docker.internal:8021/api",
            "http://host.docker.internal:8021/api/",
            "http://host.docker.internal:8021/custom/api",
            "http://host.docker.internal/api",
            " http://host.docker.internal:8021/api",
        ],
    )
    def test_handwritten_addresses_are_never_touched(
        self,
        deployment: PinDeployment,
        callback_url: Callable[[str], None],
        logged: Dict[str, List[str]],
        handwritten: str,
    ) -> None:
        """只认逐字等于自动生成形态的值；其余一律视为用户手填，哪怕端口看起来也过时了"""
        deployment(run_in_docker=True, expose_port=8022, listen_port=8021)
        callback_url(handwritten)

        assert network_guard.reconcile_sandbox_chat_api_url() is False

        assert config.SANDBOX_CHAT_API_URL == handwritten
        assert logged["warning"] == []

    @pytest.mark.parametrize(
        ("run_in_docker", "expose_port", "listen_port", "expected_port"),
        [
            (True, 8022, 8021, 8022),
            (False, 8021, 9000, 9000),
        ],
    )
    def test_the_suggested_default_uses_the_host_side_port(
        self,
        deployment: PinDeployment,
        run_in_docker: bool,
        expose_port: int,
        listen_port: int,
        expected_port: int,
    ) -> None:
        """隔离装不上时报错里建议的「改回」地址，与纠正用的是同一个值"""
        deployment(run_in_docker=run_in_docker, expose_port=expose_port, listen_port=listen_port)
        assert network_guard.default_sandbox_chat_api_url() == AUTO_URL.format(port=expected_port)

    def test_the_field_default_is_the_shape_being_reconciled(self) -> None:
        """字段默认值改了形态而匹配没跟上，纠正就会对所有冻结值静默失效"""
        default = CoreConfig.model_fields["SANDBOX_CHAT_API_URL"].default
        assert network_guard._AUTO_CHAT_API_URL_PATTERN.fullmatch(default)


class TestReconcileRunsBeforeTheFirstUse:
    """纠正必须排在启动钩子里两件事之前：

    - `bootstrap_model_groups()`：它每次启动落盘一次配置，纠正后的值随之写回配置文件；
    - `setup_sandbox_network_isolation()`：隔离规则按回调端点放行，晚了就按旧端口放行到邻居。

    又必须在启动钩子里而不是导入期：裸机下 `relay_host_port()` 要问 nonebot 驱动。
    """

    @staticmethod
    def _startup_call_lines() -> Dict[str, int]:
        source = Path(nekro_agent.__file__).read_text(encoding="utf-8")
        hook = next(
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_startup"
        )
        lines: Dict[str, int] = {}
        for node in ast.walk(hook):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                lines.setdefault(node.func.id, node.lineno)
        return lines

    def test_reconcile_precedes_the_config_dump_and_the_isolation_setup(self) -> None:
        lines = self._startup_call_lines()
        assert "reconcile_sandbox_chat_api_url" in lines, "启动钩子里没有纠正沙盒回调地址"
        assert lines["reconcile_sandbox_chat_api_url"] < lines["bootstrap_model_groups"]
        assert lines["reconcile_sandbox_chat_api_url"] < lines["setup_sandbox_network_isolation"]
