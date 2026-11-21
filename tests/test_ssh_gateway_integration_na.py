"""SSH 网关接进 NA 的集成测试

## 这条测试钉的是什么

网关本身（监听、协议、授权）在地基与网关后端两轮里已经测过。这一轮接的是 NA 侧的
「生命周期」：网络规则里留出网关端口、容器里落三份文件、开关跳变时补配、凭据变更后
重写、CLAUDE.md 与任务提示词里出现服务器别名，以及**总开关关着时一切与改造前逐字一致**。

最后一条是这里最重要的不变量：关闸时不能多查一次库、不能多碰一次 Docker，规则集、
`CLAUDE.md`、任务正文都必须和没有这个特性时完全相同。下面每一组都尽量把这条钉住，
而不是只测「开闸后功能可用」。

不起真 Docker、不起真网络、不导入 `asyncssh`：Docker 执行通道、数据库与健康检查全部打桩。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest
from aiodocker.exceptions import DockerError
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_workspace_resource import DBWorkspaceResource
from nekro_agent.models.db_workspace_resource_binding import DBWorkspaceResourceBinding
from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
from nekro_agent.schemas.workspace import CommSendBody
from nekro_agent.schemas.workspace_resource import ResourceField
from nekro_agent.services.plugin.task import AsyncTaskHandle
from nekro_agent.services.resources.crypto import encrypt_secret_payload
from nekro_agent.services.resources.service import workspace_resource_service
from nekro_agent.services.ssh_gateway import constants as C
from nekro_agent.services.ssh_gateway import container_files, events, image_support, keys, provisioning, registry
from nekro_agent.services.ssh_gateway import supervisor as supervisor_module
from nekro_agent.services.ssh_gateway.authz import Coverage
from nekro_agent.services.ssh_gateway.lease import create_lease, reset_leases
from nekro_agent.services.user.role import Role
from nekro_agent.services.workspace import container as container_module
from nekro_agent.services.workspace import manager as manager_module
from nekro_agent.services.workspace import network_guard
from nekro_agent.services.workspace.container import SandboxContainerManager
from nekro_agent.services.workspace.manager import WorkspaceService
from nekro_agent.services.workspace.network_guard import (
    INPUT_CHAIN,
    PLATFORM_GROUP,
    WorkspaceNetwork,
    WorkspaceNetworkIsolationError,
    WorkspaceNetworkPlan,
    WorkspaceNetworkSpec,
    build_workspace_isolation_rules,
    reset_isolation_state,
)
from nekro_agent.services.workspace.prompt_envelope import CcPromptEnvelope
from nekro_agent.tools import credential_cipher

FORWARD_CHAIN = "DOCKER-USER"
MARKER = "nekro-cc:test"
API_PORT = 7021
RELAY_PORT = 8021
GATEWAY_PORT = 2222
GATEWAY = "172.29.0.1"
CONTROLLER = "172.29.0.2"
NETWORK = WorkspaceNetwork(
    name="nekro-cc-test",
    bridge_interface="nekro-cc0",
    ipv4_subnet="172.29.0.0/16",
    ipv4_gateway=GATEWAY,
)

SPEC = WorkspaceNetworkSpec(
    bridge_interface=NETWORK.bridge_interface,
    ipv4_subnet=NETWORK.ipv4_subnet or "",
    controller_addresses=(CONTROLLER,),
    api_port=API_PORT,
    marker=MARKER,
)
HOST_SPEC = replace(SPEC, controller_addresses=())


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def clean_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    saved_callbacks = list(supervisor_module._enabled_change_callbacks)  # noqa: SLF001
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(OsEnv, "RUN_IN_DOCKER", False)
    monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "integration-" + "secret-key")
    credential_cipher.reset_credential_cipher_cache()
    monkeypatch.setattr(manager_module, "WORKSPACE_ROOT_DIR", str(tmp_path / "workspaces"))
    monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", False)
    monkeypatch.setattr(config, "SSH_GATEWAY_PORT", GATEWAY_PORT)
    monkeypatch.setattr(config, "CC_SANDBOX_INTERNAL_PORT", API_PORT)
    monkeypatch.setattr(config, "CC_WORKSPACE_NETWORK_ISOLATION", "best_effort")
    monkeypatch.setattr(config, "NETWORK_ISOLATION_EXTRA_BLOCKED_CIDRS", [])
    monkeypatch.setattr(config, "RESOURCE_PAYLOAD_AT_REST", "plain")
    registry.reset_registry()
    container_files.reset_ssh_servers_section_cache()
    events.reset_credential_callbacks()
    reset_isolation_state()
    reset_leases()
    provisioning._installed = False  # noqa: SLF001 - 测试之间要能重新订阅
    yield
    registry.reset_registry()
    container_files.reset_ssh_servers_section_cache()
    events.reset_credential_callbacks()
    reset_isolation_state()
    reset_leases()
    supervisor_module._enabled_change_callbacks[:] = saved_callbacks  # noqa: SLF001


@pytest.fixture
def logs() -> List[str]:
    from loguru import logger

    messages: List[str] = []
    sink_id = logger.add(lambda message: messages.append(message.record["message"]), level="DEBUG")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


async def make_workspace(name: str = "ws", *, metadata: Optional[dict] = None, status: str = "active") -> DBWorkspace:
    return await DBWorkspace.create(
        name=name,
        description=name,
        status=status,
        container_name=f"cc-{name}",
        metadata=metadata or {},
    )


async def make_row(
    workspace: DBWorkspace,
    alias: str = "web1",
    *,
    status: str = C.STATUS_ACTIVE,
    owner_kind: str = C.OWNER_PLATFORM,
    username: str = "root",
) -> DBWsSshCredential:
    return await DBWsSshCredential.create(
        workspace_id=workspace.id,
        owner_kind=owner_kind,
        owner_account="",
        adapter_key="",
        instance_key="",
        created_via=C.CREATED_VIA_ADMIN,
        import_ref="",
        alias=alias,
        host="192.0.2.40",
        port=22,
        username=username,
        password_enc="",
        private_key_enc="",
        passphrase_enc="",
        host_keys=[],
        status=status,
        status_reason="",
        note="内部备注不该出现在容器里",
        created_by="tester",
        updated_by="tester",
        last_error="",
    )


class _RecorderLogger:
    def __init__(self) -> None:
        self.warnings: List[str] = []
        self.debugs: List[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def debug(self, message: str) -> None:
        self.debugs.append(message)

    def info(self, message: str) -> None:
        self.debugs.append(message)


# ---------------------------------------------------------------------------
# 一、网络规则
# ---------------------------------------------------------------------------


def _records(spec: WorkspaceNetworkSpec, family: str = "ipv4") -> List[Any]:
    return list(build_workspace_isolation_rules(spec, family, FORWARD_CHAIN))


def _option(rule: Any, flag: str) -> str:
    args = list(rule.args)
    return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else ""


def _first_drop_index(rules: List[Any]) -> int:
    return next(index for index, rule in enumerate(rules) if rule.args[-1] == "DROP")


class TestGatewayPortRules:
    def test_container_mode_allows_gateway_port_before_drop(self) -> None:
        rules = _records(replace(SPEC, relay_port=RELAY_PORT, gateway_port=GATEWAY_PORT))
        gateway_rules = [
            rule
            for rule in rules
            if rule.args[-1] == "ACCEPT"
            and _option(rule, "--dport") == str(GATEWAY_PORT)
            and _option(rule, "-d") == CONTROLLER
        ]
        assert gateway_rules, "NA 在容器里时必须放行容器到 controller 的网关端口"
        assert rules.index(gateway_rules[0]) < _first_drop_index(rules)

    def test_host_mode_allows_gateway_on_input_before_drop(self) -> None:
        spec = replace(
            HOST_SPEC,
            relay_port=RELAY_PORT,
            host_relay_addresses=(GATEWAY,),
            gateway_port=GATEWAY_PORT,
            host_gateway_addresses=(GATEWAY,),
        )
        rules = [rule for rule in _records(spec) if rule.chain == INPUT_CHAIN]
        gateway_rules = [
            rule
            for rule in rules
            if rule.args[-1] == "ACCEPT"
            and _option(rule, "--dport") == str(GATEWAY_PORT)
            and _option(rule, "-d") == GATEWAY
        ]
        assert gateway_rules, "NA 在宿主上时必须放行容器经网桥网关访问网关端口"
        assert rules.index(gateway_rules[0]) < _first_drop_index(rules)

    def test_port_without_any_allowed_address_is_refused(self) -> None:
        broken = replace(HOST_SPEC, gateway_port=GATEWAY_PORT, host_gateway_addresses=())
        with pytest.raises(WorkspaceNetworkIsolationError, match="SSH 网关端口"):
            build_workspace_isolation_rules(broken, "ipv4", FORWARD_CHAIN)

    def test_gateway_off_rules_are_byte_identical(self) -> None:
        """关闸时规则集必须与改造前逐字相同（对两族规则取哈希，避免把 30 条规则抄进测试）"""
        expected = {
            "ipv4": "6cafceec1ec4536a55e2f099336cb0152ae61ff68c4df2158b62c9fec35eed3f",
            "ipv6": "74899be0fbc8a4923724854d1ff1869eef5fa65c93df7b58e795f8b7261ee450",
        }
        for family, digest in expected.items():
            payload = [[rule.chain, list(rule.args)] for rule in _records(SPEC, family)]
            assert hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest() == digest
            assert all(str(GATEWAY_PORT) not in rule.args for rule in _records(SPEC, family))


async def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    *,
    in_docker: bool,
    mode: str = "best_effort",
) -> Tuple[WorkspaceNetworkPlan, List[bool]]:
    installs: List[bool] = []

    async def install(_executor: Any, _marker: str, builder: Any) -> None:
        rules = builder("ipv4", FORWARD_CHAIN)
        installs.append(any(_option(rule, "--dport") == str(GATEWAY_PORT) for rule in rules))

    async def ensure_network(_docker: Any, _group: str) -> WorkspaceNetwork:
        return NETWORK

    async def attach(_docker: Any, _name: str) -> str:
        return CONTROLLER

    async def executor(_docker: Any) -> object:
        return object()

    monkeypatch.setattr(network_guard, "_ensure_network", ensure_network)
    monkeypatch.setattr(network_guard, "_attach_controller", attach)
    monkeypatch.setattr(network_guard, "resolve_executor", executor)
    monkeypatch.setattr(network_guard, "install_marked_rules", install)
    monkeypatch.setattr(network_guard, "mark_rules_verified", lambda _marker: None)
    monkeypatch.setattr(network_guard, "schedule_rule_verification", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(OsEnv, "RUN_IN_DOCKER", in_docker)
    monkeypatch.setattr(config, "CC_WORKSPACE_NETWORK_ISOLATION", mode)

    plan = await network_guard.prepare_workspace_network(object(), PLATFORM_GROUP)
    return plan, installs


class TestPrepareWorkspaceNetwork:
    async def test_gateway_off_keeps_plan_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plan, installs = await _prepare(monkeypatch, in_docker=False)
        assert plan.gateway_listen_address == ""
        assert installs == [False]

    async def test_host_mode_listen_address_is_bridge_gateway(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
        plan, installs = await _prepare(monkeypatch, in_docker=False)
        assert plan.gateway_listen_address == GATEWAY
        assert installs == [True]
        assert registry.desired_listen_addresses() == frozenset({GATEWAY})

    async def test_container_mode_listen_address_is_controller_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
        plan, _installs = await _prepare(monkeypatch, in_docker=True)
        assert plan.gateway_listen_address == CONTROLLER

    async def test_disabled_isolation_has_no_gateway_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
        monkeypatch.setattr(config, "CC_WORKSPACE_NETWORK_ISOLATION", "off")

        async def legacy(_docker: Any) -> WorkspaceNetworkPlan:
            return WorkspaceNetworkPlan(network_mode="", isolated=False)

        monkeypatch.setattr(network_guard, "_legacy_plan", legacy)
        plan = await network_guard.prepare_workspace_network(object(), PLATFORM_GROUP)
        assert plan.gateway_listen_address == ""

    async def test_switch_reinstalls_rules_with_and_without_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plan_off, installs_off = await _prepare(monkeypatch, in_docker=False)
        assert plan_off.gateway_listen_address == ""
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
        plan_on, installs_on = await _prepare(monkeypatch, in_docker=False)
        assert plan_on.gateway_listen_address == GATEWAY
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", False)
        plan_off_again, installs_off_again = await _prepare(monkeypatch, in_docker=False)
        assert plan_off_again.gateway_listen_address == ""
        assert installs_off + installs_on + installs_off_again == [False, True, False]


# ---------------------------------------------------------------------------
# 二、容器文件
# ---------------------------------------------------------------------------


class TestContainerFiles:
    def test_config_has_alias_only_and_sorted_blocks(self) -> None:
        rows = [
            SimpleNamespace(alias="web2", status=C.STATUS_ACTIVE, username="root", owner_kind=C.OWNER_USER),
            SimpleNamespace(alias="web1", status=C.STATUS_ACTIVE, username="deploy", owner_kind=C.OWNER_PLATFORM),
            SimpleNamespace(alias="old", status=C.STATUS_DISABLED, username="root", owner_kind=C.OWNER_PLATFORM),
        ]
        text = container_files.render_ssh_config(rows, listen_address="172.29.0.2", port=GATEWAY_PORT)
        assert text.index("Host web1") < text.index("Host web2")
        assert "Host old" not in text
        assert "192.0.2.40" not in text
        assert "Port 22\n" not in text
        assert "HostName 172.29.0.2" in text
        assert f"Port {GATEWAY_PORT}" in text
        assert "User web1" in text
        assert "/home/appuser/.claude/nk-ssh/id_gateway" in text
        assert "HostKeyAlias nk-gateway" in text

    def test_empty_config_is_comment_only(self) -> None:
        text = container_files.render_ssh_config([], listen_address="172.29.0.2", port=GATEWAY_PORT)
        assert text.splitlines()
        assert all(line.startswith("#") for line in text.splitlines())

    def test_known_hosts_line(self) -> None:
        assert container_files.render_known_hosts("ssh-ed25519 AAAA test") == "nk-gateway ssh-ed25519 AAAA test\n"
        assert container_files.render_known_hosts("") == ""

    async def test_write_is_atomic_and_keeps_private_key(
        self, db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path  # noqa: ARG002
    ) -> None:
        workspace = await make_workspace()
        await make_row(workspace, "web1")
        monkeypatch.setattr(keys, "gateway_host_public_key", lambda: "ssh-ed25519 AAAAGW test")
        fixed: List[str] = []

        async def fix(name: str) -> None:
            fixed.append(name)

        monkeypatch.setattr(container_files, "fix_permissions_in_container", fix)
        await container_files.write_nk_ssh_files(workspace, listen_address=CONTROLLER, private_key="PRIVATE")
        directory = Path(manager_module.WORKSPACE_ROOT_DIR) / str(workspace.id) / ".claude_home" / "nk-ssh"
        assert (directory / "config").read_text(encoding="utf-8").startswith("# 由 NekroAgent 生成")
        assert (directory / "known_hosts").read_text(encoding="utf-8") == "nk-gateway ssh-ed25519 AAAAGW test\n"
        assert (directory / "id_gateway").read_text(encoding="utf-8") == "PRIVATE"
        assert fixed == ["cc-ws"]
        assert list(directory.glob("*.tmp")) == []

        await container_files.write_nk_ssh_files(workspace, listen_address=CONTROLLER, private_key=None)
        assert (directory / "id_gateway").read_text(encoding="utf-8") == "PRIVATE", "只改配置时不该动私钥"

    async def test_fix_permissions_runs_root_exec_with_both_ownerships(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: Dict[str, Any] = {}

        class _Stream:
            async def __aenter__(self) -> "_Stream":
                return self

            async def __aexit__(self, *_args: Any) -> bool:
                return False

            async def read_out(self) -> None:
                return None

        class _Exec:
            async def start(self, detach: bool = False) -> _Stream:
                captured["detach"] = detach
                return _Stream()

            async def inspect(self) -> Dict[str, int]:
                return {"ExitCode": 0}

        class _Container:
            async def exec(self, cmd: List[str], **kwargs: Any) -> _Exec:
                captured["cmd"] = cmd
                captured["kwargs"] = kwargs
                return _Exec()

        class _Containers:
            async def get(self, name: str) -> _Container:
                captured["container"] = name
                return _Container()

        class _Docker:
            containers = _Containers()

            async def close(self) -> None:
                return None

        monkeypatch.setattr(container_files, "_open_docker", lambda: _Docker())
        await container_files.fix_permissions_in_container("cc-1")
        assert captured["container"] == "cc-1"
        assert captured["kwargs"]["user"] == "0"
        assert captured["cmd"][:2] == ["sh", "-c"]
        script = captured["cmd"][2]
        assert "chown root:root" in script
        assert "chmod 0755" in script
        assert "chmod 0644" in script
        assert "chown 1000:1000" in script
        assert "chmod 0600" in script

    async def test_fix_permissions_failure_only_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = _RecorderLogger()
        monkeypatch.setattr(container_files, "logger", recorder)

        class _Docker:
            class containers:  # noqa: N801 - 只做属性容器
                @staticmethod
                async def get(_name: str) -> Any:
                    raise RuntimeError("docker down")

            async def close(self) -> None:
                return None

        monkeypatch.setattr(container_files, "_open_docker", lambda: _Docker())
        await container_files.fix_permissions_in_container("cc-1")
        assert recorder.warnings
        assert "SSH 暂时不可用" in recorder.warnings[0]
        assert "docker down" in recorder.warnings[0]


# ---------------------------------------------------------------------------
# 三、容器生命周期
# ---------------------------------------------------------------------------


class _FakeContainer:
    def __init__(self, info: dict) -> None:
        self._info = info
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self, t: int = 10) -> None:
        self.stopped += 1

    async def restart(self, t: int = 10) -> None:
        return None

    async def delete(self, force: bool = False) -> None:
        return None

    async def show(self) -> dict:
        return self._info


class _FakeContainers:
    def __init__(self, info: dict, fail_get: bool = False) -> None:
        self._info = info
        self.fail_get = fail_get
        self.gets: List[str] = []

    async def get(self, name: str) -> _FakeContainer:
        self.gets.append(name)
        if self.fail_get:
            raise DockerError(404, {"message": f"No such container: {name}"})
        return _FakeContainer(self._info)

    async def list(self, **_kwargs: Any) -> List[_FakeContainer]:
        # 这组用例只关心网关登记：同一工作区没有别的容器
        return []

    async def create_or_replace(self, *, name: str, config: dict) -> _FakeContainer:
        return _FakeContainer(self._info)


class _FakeDocker:
    opened = 0

    def __init__(self, info: Optional[dict] = None, fail_get: bool = False) -> None:
        type(self).opened += 1
        self.containers = _FakeContainers(info or _running_info(), fail_get)
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


def _running_info() -> dict:
    return {
        "Id": "a" * 64,
        "State": {"Running": True},
        "NetworkSettings": {"Networks": {NETWORK.name: {"IPAddress": "172.29.0.10"}}},
    }


def _patch_create_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    plan: WorkspaceNetworkPlan,
    info: Optional[dict] = None,
    version: str = "0.1.14+ak2",
) -> Dict[str, List[Any]]:
    calls: Dict[str, List[Any]] = {"write": [], "remove": [], "fix": [], "register": []}

    async def image_exists(_image: str) -> bool:
        return True

    async def preset(_workspace: DBWorkspace) -> None:
        return None

    async def token(_workspace: DBWorkspace) -> str:
        return "token"

    async def prepare(_docker: Any, _group: str) -> WorkspaceNetworkPlan:
        return plan

    async def group(_workspace: DBWorkspace) -> str:
        return PLATFORM_GROUP

    async def init(_workspace: DBWorkspace, **_kwargs: Any) -> None:
        return None

    async def exists(_docker: Any, _name: str) -> bool:
        return False

    async def free_port() -> int:
        return 18080

    async def healthy(_endpoint: str, _timeout: int = 60) -> bool:
        return True

    async def write(workspace: DBWorkspace, **kwargs: Any) -> None:
        calls["write"].append((workspace.id, kwargs))

    async def remove(workspace_id: int) -> None:
        calls["remove"].append(workspace_id)

    async def fix(name: str) -> None:
        calls["fix"].append(name)

    def register(peer: Any) -> None:
        calls["register"].append(peer)

    class _Client:
        def __init__(self, _workspace: Any) -> None:
            pass

        async def get_cc_version(self) -> str:
            return version

    monkeypatch.setattr(SandboxContainerManager, "check_image_exists", image_exists)
    monkeypatch.setattr(SandboxContainerManager, "_resolve_preset", preset)
    monkeypatch.setattr(SandboxContainerManager, "_rotate_internal_api_token", token)
    monkeypatch.setattr(SandboxContainerManager, "_wait_healthy", healthy)
    monkeypatch.setattr(container_module, "prepare_workspace_network", prepare)
    monkeypatch.setattr(container_module, "resolve_workspace_network_group", group)
    monkeypatch.setattr(WorkspaceService, "init_workspace_dir", init)
    monkeypatch.setattr(container_module, "_container_exists", exists)
    monkeypatch.setattr(container_module, "_find_free_port", free_port)
    monkeypatch.setattr(container_module, "write_nk_ssh_files", write)
    monkeypatch.setattr(container_module, "remove_nk_ssh_files", remove)
    monkeypatch.setattr(container_module, "fix_permissions_in_container", fix)
    monkeypatch.setattr(container_module.ssh_gateway_registry, "register_peer", register)
    monkeypatch.setattr(container_module.aiodocker, "Docker", lambda: _FakeDocker(info or _running_info()))
    monkeypatch.setattr("nekro_agent.services.workspace.client.CCSandboxClient", _Client)
    _accept_any_mounts(monkeypatch)
    return calls


def _accept_any_mounts(monkeypatch: pytest.MonkeyPatch) -> None:
    """这组用例只关心网关登记；挂载核对有自己的用例（`test_ws_host_hardening.py`）"""
    monkeypatch.setattr(container_module, "_mount_mismatch", lambda _info, _workspace_id: None)


class TestContainerLifecycle:
    async def test_gate_on_mints_key_writes_files_and_registers(
        self, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
        workspace = await make_workspace(status="stopped")
        plan = WorkspaceNetworkPlan(
            network_mode=NETWORK.name,
            isolated=True,
            controller_address=CONTROLLER,
            host_address=GATEWAY,
            gateway_listen_address=CONTROLLER,
        )
        calls = _patch_create_dependencies(monkeypatch, plan=plan)
        _FakeDocker.opened = 0

        result = await SandboxContainerManager.create_and_start(workspace)

        key = str((result.metadata or {}).get("ssh_gateway_client_key") or "")
        assert key.startswith("ssh-ed25519 ")
        assert calls["write"] and calls["write"][0][1]["listen_address"] == CONTROLLER
        assert calls["write"][0][1]["private_key"]
        assert calls["fix"] == [result.container_name]
        assert _FakeDocker.opened == 1, "建容器只该开一个 Docker 客户端"
        assert len(calls["register"]) == 1
        peer = calls["register"][0]
        assert peer.workspace_id == workspace.id
        assert peer.peer_address == "172.29.0.10"
        assert peer.listen_address == CONTROLLER
        assert peer.client_public_key == key
        assert peer.isolation_ok is True
        assert peer.image_ok is True

    async def test_gate_off_removes_files_and_drops_key_without_extra_docker(
        self, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        workspace = await make_workspace(metadata={"ssh_gateway_client_key": "ssh-ed25519 OLD"}, status="stopped")
        plan = WorkspaceNetworkPlan(network_mode=NETWORK.name, isolated=True, controller_address=CONTROLLER)
        calls = _patch_create_dependencies(monkeypatch, plan=plan)
        _FakeDocker.opened = 0

        async def explode(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("关闸时不该查凭据行")

        monkeypatch.setattr(container_files, "_load_rows", explode)

        result = await SandboxContainerManager.create_and_start(workspace)

        assert "ssh_gateway_client_key" not in (result.metadata or {})
        assert calls["remove"] == [workspace.id]
        assert calls["write"] == []
        assert calls["fix"] == []
        assert calls["register"] == []
        assert _FakeDocker.opened == 1, "关闸时除了建容器本身不多开 Docker 客户端"

    async def test_gateway_registration_failure_does_not_fail_start(
        self, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
        workspace = await make_workspace(status="failed")
        plan = WorkspaceNetworkPlan(
            network_mode=NETWORK.name,
            isolated=True,
            gateway_listen_address=CONTROLLER,
        )
        calls = _patch_create_dependencies(monkeypatch, plan=plan)

        def broken_register(_peer: Any) -> None:
            raise RuntimeError("registry broken")

        monkeypatch.setattr(container_module.ssh_gateway_registry, "register_peer", broken_register)

        result = await SandboxContainerManager.create_and_start(workspace)
        assert result.status == "active"
        assert calls["write"], "登记失败不该回滚已经写好的文件"

    async def test_stop_unregisters_and_drops_gateway_key(
        self, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        workspace = await make_workspace(metadata={"cc_relay_token": "t", "ssh_gateway_client_key": "k"})
        unregistered: List[int] = []
        monkeypatch.setattr(container_module.ssh_gateway_registry, "unregister_workspace", unregistered.append)
        monkeypatch.setattr(
            container_module.aiodocker,
            "Docker",
            lambda: _FakeDocker(_running_info()),
        )

        await SandboxContainerManager.stop(workspace)
        assert unregistered == [workspace.id]
        assert "ssh_gateway_client_key" not in (workspace.metadata or {})
        assert "cc_relay_token" not in (workspace.metadata or {})

    async def test_stop_without_container_still_unregisters(
        self, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        workspace = await make_workspace(metadata={"ssh_gateway_client_key": "k"})
        workspace.container_name = None  # type: ignore[assignment]
        unregistered: List[int] = []
        monkeypatch.setattr(container_module.ssh_gateway_registry, "unregister_workspace", unregistered.append)

        await SandboxContainerManager.stop(workspace)
        assert unregistered == [workspace.id]

    async def test_remove_unregisters_even_when_container_is_gone(
        self, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        workspace = await make_workspace()
        unregistered: List[int] = []
        monkeypatch.setattr(container_module.ssh_gateway_registry, "unregister_workspace", unregistered.append)
        monkeypatch.setattr(container_module.aiodocker, "Docker", lambda: _FakeDocker(fail_get=True))

        await SandboxContainerManager.remove(workspace)
        assert unregistered == [workspace.id]

    async def test_rebuild_unregisters_before_creating(
        self, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        workspace = await make_workspace()
        unregistered: List[int] = []
        monkeypatch.setattr(container_module.ssh_gateway_registry, "unregister_workspace", unregistered.append)
        monkeypatch.setattr(container_module.aiodocker, "Docker", lambda: _FakeDocker(fail_get=True))
        created: List[int] = []

        async def image_exists(_image: str) -> bool:
            return True

        async def create(_workspace: DBWorkspace) -> DBWorkspace:
            created.append(len(unregistered))
            _workspace.status = "active"
            await _workspace.save(update_fields=["status"])
            return _workspace

        monkeypatch.setattr(SandboxContainerManager, "check_image_exists", image_exists)
        monkeypatch.setattr(SandboxContainerManager, "_create_and_start_locked", create)
        await SandboxContainerManager.rebuild(workspace)
        assert unregistered == [workspace.id]
        assert created == [1], "先注销旧登记、再建新容器"

    async def test_recover_registers_running_container_and_warns_without_key(
        self, db, monkeypatch: pytest.MonkeyPatch, logs: List[str]
    ) -> None:
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
        with_key = await make_workspace("with-key", metadata={"ssh_gateway_client_key": "ssh-ed25519 KEY"})
        without_key = await make_workspace("without-key")
        monkeypatch.setattr(container_module.aiodocker, "Docker", lambda: _FakeDocker(_running_info()))

        async def relay(_docker: Any, _name: str) -> Tuple[str, str]:
            return "", GATEWAY

        monkeypatch.setattr(container_module, "resolve_container_relay_addresses", relay)

        class _Client:
            def __init__(self, _workspace: Any) -> None:
                pass

            async def get_cc_version(self) -> str:
                return "0.1.14+ak2"

        monkeypatch.setattr("nekro_agent.services.workspace.client.CCSandboxClient", _Client)
        _accept_any_mounts(monkeypatch)
        await SandboxContainerManager.recover_on_startup()

        peer = registry.peer_for_workspace(with_key.id)
        assert peer is not None
        assert peer.client_public_key == "ssh-ed25519 KEY"
        assert peer.peer_address == "172.29.0.10"
        assert registry.peer_for_workspace(without_key.id) is None
        assert any("重建后才能用 SSH" in message for message in logs)

    async def test_restart_reregisters_with_the_new_address(
        self, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        """重启后容器地址可能变：按新地址重新登记，旧登记作废，钥匙沿用 metadata 里那把"""
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
        workspace = await make_workspace("restarted", metadata={"ssh_gateway_client_key": "ssh-ed25519 KEY"})
        workspace.container_name = "na-ws-restarted"
        workspace.host_port = 18080
        await workspace.save(update_fields=["container_name", "host_port"])
        registry.register_peer(
            registry.ContainerPeer(
                workspace_id=workspace.id,
                peer_address="172.29.0.99",
                listen_address=GATEWAY,
                client_public_key="ssh-ed25519 KEY",
                isolation_ok=True,
                image_ok=True,
            ),
        )
        monkeypatch.setattr(container_module.aiodocker, "Docker", lambda: _FakeDocker(_running_info()))

        async def relay(_docker: Any, _name: str) -> Tuple[str, str]:
            return "", GATEWAY

        async def no_preset(_workspace: DBWorkspace) -> None:
            return None

        async def init(_workspace: DBWorkspace, **_kwargs: Any) -> None:
            return None

        async def healthy(_endpoint: str, _timeout: int = 60) -> bool:
            return True

        fixed: List[str] = []

        async def fix(name: str) -> None:
            fixed.append(name)

        class _Client:
            def __init__(self, _workspace: Any) -> None:
                pass

            async def get_cc_version(self) -> str:
                return "0.1.14+ak2"

        monkeypatch.setattr(container_module, "resolve_container_relay_addresses", relay)
        monkeypatch.setattr(SandboxContainerManager, "_resolve_preset", no_preset)
        monkeypatch.setattr(container_module.WorkspaceService, "init_workspace_dir", init)
        monkeypatch.setattr(SandboxContainerManager, "_wait_healthy", healthy)
        monkeypatch.setattr(container_module, "fix_permissions_in_container", fix)
        monkeypatch.setattr("nekro_agent.services.workspace.client.CCSandboxClient", _Client)
        _accept_any_mounts(monkeypatch)

        await SandboxContainerManager.restart(workspace)

        peer = registry.peer_for_workspace(workspace.id)
        assert peer is not None
        assert peer.peer_address == "172.29.0.10", "按重启后的实际地址重新登记"
        assert peer.client_public_key == "ssh-ed25519 KEY"
        assert fixed == ["na-ws-restarted"]


# ---------------------------------------------------------------------------
# 四、开关跳变时的补配
# ---------------------------------------------------------------------------


class TestProvisioningOnSwitch:
    async def test_enable_rewrites_files_and_registers_existing_key(
        self, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        workspace = await make_workspace(metadata={"ssh_gateway_client_key": "ssh-ed25519 KEY"})
        await make_row(workspace)
        writes: List[Tuple[int, bool]] = []
        registered: List[Any] = []

        async def prepare(_docker: Any, _group: str) -> WorkspaceNetworkPlan:
            return WorkspaceNetworkPlan(
                network_mode=NETWORK.name,
                isolated=True,
                gateway_listen_address=CONTROLLER,
            )

        async def write(ws: DBWorkspace, *, listen_address: str, private_key: Optional[str]) -> None:
            writes.append((ws.id, private_key is None))
            assert listen_address == CONTROLLER

        async def register(*_args: Any, **_kwargs: Any) -> bool:
            registered.append(_kwargs)
            return True

        async def noop(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(network_guard, "prepare_workspace_network", prepare)
        monkeypatch.setattr(network_guard, "resolve_workspace_network_group", noop)
        monkeypatch.setattr(provisioning, "_open_docker", lambda: _FakeDocker())
        monkeypatch.setattr(provisioning, "write_nk_ssh_files", write)
        monkeypatch.setattr(provisioning, "_register_existing_container", register)
        monkeypatch.setattr(provisioning, "_refresh_claude_md", noop)

        await provisioning._on_enabled_change(True)  # noqa: SLF001
        assert writes == [(workspace.id, True)], "运行中只改配置，不补发私钥"
        assert registered and registered[0]["public_key"] == "ssh-ed25519 KEY"

    async def test_enable_skips_container_without_key(
        self, db, monkeypatch: pytest.MonkeyPatch, logs: List[str]  # noqa: ARG002
    ) -> None:
        await make_workspace()
        writes: List[Any] = []

        async def write(*_args: Any, **_kwargs: Any) -> None:
            writes.append(_args)

        monkeypatch.setattr(provisioning, "_open_docker", lambda: _FakeDocker())
        monkeypatch.setattr(provisioning, "write_nk_ssh_files", write)
        await provisioning._on_enabled_change(True)  # noqa: SLF001
        assert writes == []
        assert any("重建后才能用 SSH" in message for message in logs)

    async def test_disable_clears_registry_without_touching_docker(
        self, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        registry.register_peer(
            registry.ContainerPeer(
                workspace_id=1,
                peer_address="172.29.0.10",
                listen_address=CONTROLLER,
                client_public_key="ssh-ed25519 KEY",
                isolation_ok=True,
                image_ok=True,
            )
        )
        monkeypatch.setattr(
            provisioning,
            "_open_docker",
            lambda: (_ for _ in ()).throw(AssertionError("关闸不该碰 Docker")),
        )
        await provisioning._on_enabled_change(False)  # noqa: SLF001
        assert registry.peer_for_workspace(1) is None


# ---------------------------------------------------------------------------
# 五、凭据变更后的重写
# ---------------------------------------------------------------------------


class TestProvisioningOnCredentialChange:
    async def test_edit_rewrites_config_and_claude_md(
        self, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
        workspace = await make_workspace(metadata={"ssh_gateway_client_key": "k"})
        row = await make_row(workspace)
        writes: List[int] = []
        refreshed: List[int] = []

        async def relay(_docker: Any, _name: str) -> Tuple[str, str]:
            return "", CONTROLLER

        async def write(ws: DBWorkspace, *, listen_address: str, private_key: Optional[str]) -> None:
            assert listen_address == CONTROLLER
            assert private_key is None
            writes.append(ws.id)

        async def refresh(ws: DBWorkspace) -> None:
            refreshed.append(ws.id)

        monkeypatch.setattr(network_guard, "resolve_container_relay_addresses", relay)
        monkeypatch.setattr(provisioning, "_open_docker", lambda: _FakeDocker())
        monkeypatch.setattr(provisioning, "write_nk_ssh_files", write)
        monkeypatch.setattr(provisioning, "_refresh_claude_md", refresh)

        await provisioning._on_credential_changed(row.id)  # noqa: SLF001
        assert writes == [workspace.id]
        assert refreshed == [workspace.id]

    async def test_delete_falls_back_to_running_workspaces(
        self, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
        workspace = await make_workspace(metadata={"ssh_gateway_client_key": "k"})
        row = await make_row(workspace)
        deleted_id = row.id
        await row.delete()
        writes: List[int] = []

        async def relay(_docker: Any, _name: str) -> Tuple[str, str]:
            return "", CONTROLLER

        async def write(ws: DBWorkspace, **_kwargs: Any) -> None:
            writes.append(ws.id)

        async def noop(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(network_guard, "resolve_container_relay_addresses", relay)
        monkeypatch.setattr(provisioning, "_open_docker", lambda: _FakeDocker())
        monkeypatch.setattr(provisioning, "write_nk_ssh_files", write)
        monkeypatch.setattr(provisioning, "_refresh_claude_md", noop)

        await provisioning._on_credential_changed(deleted_id)  # noqa: SLF001
        assert writes == [workspace.id], "删掉行之后也要把那个工作区的配置重写掉"

    async def test_gate_off_queries_nothing(
        self, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        called: List[int] = []

        async def explode(credential_id: int) -> List[Any]:
            called.append(credential_id)
            raise AssertionError("关闸时不该查库")

        monkeypatch.setattr(provisioning, "_running_workspaces_for_credential", explode)
        await provisioning._on_credential_changed(123)  # noqa: SLF001
        assert called == []

    async def test_create_row_notifies_after_commit(
        self, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        from nekro_agent.services.ssh_gateway.credentials import ssh_credential_service

        workspace = await make_workspace()
        seen: List[int] = []
        events.on_credential_changed(seen.append)

        async def audit(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(ssh_credential_service, "_write_audit", audit)
        row = await ssh_credential_service.create_credential_row(
            workspace_id=workspace.id,
            owner_kind=C.OWNER_PLATFORM,
            owner_account="",
            adapter_key="",
            instance_key="",
            alias="web1",
            host="192.0.2.40",
            port=22,
            username="root",
            created_via=C.CREATED_VIA_ADMIN,
            created_by="tester",
            password="pw-integration-a1",
            private_key="",
            passphrase="",
            note="",
            import_ref="",
        )
        assert seen == [row.id]


# ---------------------------------------------------------------------------
# 六、CLAUDE.md 的服务器小节
# ---------------------------------------------------------------------------


def _baseline(workspace: DBWorkspace, *, resource_section: str = "") -> str:
    return (
        WorkspaceService._CLAUDE_MD_TEMPLATE.replace("{runtime_policy}", workspace.runtime_policy or "agent")
        .replace("{resource_section}", resource_section)
        .replace("{shared_section}", "")
        .replace("{extra_section}", "")
    )


class TestClaudeMdSection:
    async def test_gate_off_is_byte_identical_even_with_cache(
        self, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        workspace = await make_workspace()
        await make_row(workspace)
        await container_files.refresh_ssh_servers_section(workspace.id)
        assert WorkspaceService._generate_claude_md_content(workspace) == _baseline(workspace)

    async def test_gate_on_appends_section_without_host_or_note(
        self, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
        workspace = await make_workspace()
        await make_row(workspace, "web1", username="deploy")
        await make_row(workspace, "web2")
        await make_row(workspace, "old", status=C.STATUS_DISABLED)
        await container_files.refresh_ssh_servers_section(workspace.id)
        content = WorkspaceService._generate_claude_md_content(workspace)
        assert "## 可用的服务器（SSH）" in content
        assert "`web1`" in content and "`deploy`" in content
        assert "`web2`" in content
        assert "`old`" not in content
        assert "192.0.2.40" not in content
        assert "内部备注不该出现在容器里" not in content
        assert content.startswith(_baseline(workspace))

    async def test_no_rows_adds_nothing(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
        workspace = await make_workspace()
        await container_files.refresh_ssh_servers_section(workspace.id)
        assert WorkspaceService._generate_claude_md_content(workspace) == _baseline(workspace)

    async def test_update_claude_md_writes_section(
        self, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
        workspace = await make_workspace()
        await make_row(workspace)
        directory = Path(manager_module.WORKSPACE_ROOT_DIR) / str(workspace.id) / "default"
        directory.mkdir(parents=True)
        (directory / "CLAUDE.md").write_text("old", encoding="utf-8")
        await container_files.refresh_ssh_servers_section(workspace.id)
        WorkspaceService.update_claude_md(workspace)
        assert "## 可用的服务器（SSH）" in (directory / "CLAUDE.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 七、资源注入
# ---------------------------------------------------------------------------


SSH_VALUES = {
    "server_ssh_key": {
        "fields": [
            ResourceField(field_key="host", label="主机", value_kind="host", order=1),
            ResourceField(field_key="private_key", label="私钥", secret=True, value_kind="private_key", order=2),
        ],
        "public": {"host": "192.0.2.40"},
        "secret": {"private_key": "PRIVATE-" + "payload"},
        "note": "老资源说明：从环境变量读私钥",
    },
    "server_ssh_password": {
        "fields": [
            ResourceField(field_key="host", label="主机", value_kind="host", order=1),
            ResourceField(field_key="password", label="口令", secret=True, value_kind="password", order=2),
        ],
        "public": {"host": "192.0.2.41"},
        "secret": {"password": "PASSWORD-" + "payload"},
        "note": "老资源说明：从环境变量读口令",
    },
    "server_root_password": {
        "fields": [
            ResourceField(field_key="host", label="主机", value_kind="host", order=1),
            ResourceField(field_key="password", label="口令", secret=True, value_kind="password", order=2),
        ],
        "public": {"host": "192.0.2.42"},
        "secret": {"password": "ROOT-" + "payload"},
        "note": "老资源说明：root 口令",
    },
    "postgres": {
        "fields": [
            ResourceField(field_key="host", label="主机", value_kind="host", order=1),
            ResourceField(field_key="password", label="口令", secret=True, value_kind="password", order=2),
        ],
        "public": {"host": "192.0.2.50"},
        "secret": {"password": "DB-" + "payload"},
        "note": "数据库连接",
    },
}


async def _bind_resources(workspace: DBWorkspace) -> None:
    for order, (key, spec) in enumerate(SSH_VALUES.items()):
        resource = await DBWorkspaceResource.create(
            # 资源自己的标识故意与模板键不同：跳过注入的判据是「用的哪个模板」，不是资源叫什么
            resource_key=f"res-{key}",
            name=key,
            template_key=key if key != "postgres" else None,
            resource_note=spec["note"],
            resource_prompt=spec["note"],
            schema_json=[field.model_dump() for field in spec["fields"]],
            public_payload=spec["public"],
            secret_payload_encrypted=encrypt_secret_payload(spec["secret"]),
            enabled=True,
        )
        await DBWorkspaceResourceBinding.create(
            workspace_id=workspace.id,
            resource_id=resource.id,
            enabled=True,
            sort_order=order,
        )


class TestResourceInjection:
    async def test_gate_off_injects_all_resources(
        self, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        workspace = await make_workspace()
        await _bind_resources(workspace)
        env = await workspace_resource_service.resolve_workspace_resources_to_env(
            workspace.id,
            allow_platform_secrets=True,
        )
        assert "192.0.2.40" in env.values()
        assert "192.0.2.50" in env.values()
        prompt, _summary = await workspace_resource_service._build_workspace_prompt_section(  # noqa: SLF001
            workspace.id
        )
        assert "老资源说明：从环境变量读私钥" in prompt

    async def test_gate_on_skips_only_the_three_ssh_templates(
        self, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
        workspace = await make_workspace()
        await _bind_resources(workspace)
        env = await workspace_resource_service.resolve_workspace_resources_to_env(
            workspace.id,
            allow_platform_secrets=True,
        )
        assert "192.0.2.40" not in env.values()
        assert "192.0.2.41" not in env.values()
        assert "192.0.2.42" not in env.values()
        assert "192.0.2.50" in env.values(), "别的模板照旧注入"
        prompt, summary = await workspace_resource_service._build_workspace_prompt_section(  # noqa: SLF001
            workspace.id
        )
        assert "老资源说明：从环境变量读私钥" not in prompt
        assert "数据库连接" in prompt
        assert "server_ssh_key" in summary, "能力摘要仍要说明工作区绑着这些资源"


# ---------------------------------------------------------------------------
# 八、镜像与任务信封
# ---------------------------------------------------------------------------


class TestImageSupport:
    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            ("0.1.14+ak2", True),
            ("0.1.14+ak3", True),
            ("0.1.14+ak1", False),
            ("0.1.15+ak1", True),
            ("0.2.0+ak0", True),
            ("0.1.14", False),
            ("unknown", False),
            (None, False),
        ],
    )
    def test_version_matrix(self, version: Optional[str], expected: bool) -> None:
        assert image_support.image_supports_gateway(version) is expected


class TestPromptEnvelope:
    def _envelope(self, *, user_path: bool = False, ssh: Tuple[str, ...] = ()) -> CcPromptEnvelope:
        envelope = CcPromptEnvelope.for_user_path(
            current_time="2026-01-01 00:00 UTC",
            task_body="做点事",
        ) if user_path else CcPromptEnvelope.for_na_path(
            source_chat_key="onebot:g1",
            current_time="2026-01-01 00:00 UTC",
            task_body="做点事",
        )
        envelope.ssh_servers = ssh
        return envelope

    def test_empty_servers_is_byte_identical(self) -> None:
        expected = "[任务来源频道: onebot:g1] [当前时间: 2026-01-01 00:00 UTC]\n\n做点事"
        assert self._envelope().to_prompt() == expected
        assert self._envelope(user_path=True).to_prompt() == "[当前时间: 2026-01-01 00:00 UTC]\n\n做点事"

    def test_servers_add_a_note_after_task(self) -> None:
        text = self._envelope(ssh=("game", "web1")).to_prompt()
        assert text.startswith("[任务来源频道: onebot:g1]")
        assert text.endswith(
            "本任务可用的服务器：game、web1。"
            "用 `ssh -T <别名> '命令'`；`scp` / `rsync` / `git` 用 `<别名>:路径`。"
            "其余登记过的服务器本任务没有授权。"
        )
        assert "其余登记过的服务器" in text

    def test_metadata_json_is_unchanged(self) -> None:
        assert json.loads(self._envelope().to_metadata_json()) == json.loads(
            self._envelope(ssh=("web1",)).to_metadata_json()
        )


# ---------------------------------------------------------------------------
# 九、接线：监督任务跳变、插件委托、后台直发
# ---------------------------------------------------------------------------


class _Backend:
    def __init__(self) -> None:
        self.calls: List[Any] = []

    async def start(self) -> None:
        self.calls.append(("start",))

    async def sync_listeners(self, addresses: frozenset[str], port: int) -> None:
        self.calls.append(("sync", addresses, port))

    async def stop(self) -> None:
        self.calls.append(("stop",))

    def counters(self) -> dict:
        return {}


class TestSupervisorHook:
    async def test_switch_dispatches_once_per_transition(
        self, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        supervisor = supervisor_module.Supervisor(tick_seconds=60.0)
        backend = _Backend()
        seen: List[bool] = []

        async def callback(enabled: bool) -> None:
            seen.append(enabled)

        supervisor_module._enabled_change_callbacks.append(callback)  # noqa: SLF001
        monkeypatch.setattr(
            supervisor_module.importlib,
            "import_module",
            lambda _name: object(),
        )
        supervisor.register_backend(backend)

        await supervisor.tick()
        assert seen == [], "第一拍就是关着时不该通知"

        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
        await supervisor.tick()
        await supervisor.tick()
        assert seen == [True], "连续两拍都开着只该通知一次"

        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", False)
        await supervisor.tick()
        await supervisor.tick()
        assert seen == [True, False]

    def test_install_is_idempotent(self) -> None:
        before = len(supervisor_module._enabled_change_callbacks)  # noqa: SLF001
        provisioning.install()
        provisioning.install()
        assert len(supervisor_module._enabled_change_callbacks) == before + 1  # noqa: SLF001
        assert events.is_subscribed(provisioning._on_credential_changed)  # noqa: SLF001


class _Store:
    def __init__(self) -> None:
        self.data: Dict[Tuple[str, str], str] = {}

    async def set(self, *, chat_key: str, store_key: str, value: str) -> None:
        self.data[(chat_key, store_key)] = value

    async def get(self, *, chat_key: str, store_key: str) -> str:
        return self.data.get((chat_key, store_key), "")


TASK_REF = "integration:chat"


def _import_cc() -> Any:
    plugin_root = str(Path(__file__).resolve().parents[1] / "plugins")
    if plugin_root not in sys.path:
        sys.path.insert(0, plugin_root)
    return importlib.import_module("builtin.cc_workspace.main")


class TestPluginDelegationPrompt:
    async def _run(self, monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> str:
        cc = _import_cc()
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", enabled)
        workspace = await make_workspace()
        row = await make_row(workspace, "web1")
        game = await make_row(workspace, "game")
        await make_row(workspace, "other")
        store = _Store()
        # 上一次委托留下的存储：别名必须按这份租约现查，不能读它
        await store.set(chat_key=TASK_REF, store_key="ssh_gateway_servers", value=json.dumps(["stale"]))
        monkeypatch.setattr(type(cc.plugin), "store", property(lambda _self: store))
        captured: List[str] = []

        class _Client:
            def __init__(self, _workspace: Any) -> None:
                pass

            async def stream_message(self, prompt: str, **_kwargs: Any) -> Any:
                captured.append(prompt)
                yield "done"

        async def prepare(_workspace: Any) -> Dict[str, str]:
            return {}

        async def quiet(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr("nekro_agent.services.workspace.credential_policy.prepare_cc_dispatch", prepare)
        monkeypatch.setattr(cc, "CCSandboxClient", _Client)
        monkeypatch.setattr(cc, "publish_system_event", quiet)
        monkeypatch.setattr(cc, "_broadcast_cc_status", quiet)
        monkeypatch.setattr(cc.comm_broadcast, "publish", quiet)

        lease = create_lease(
            workspace_id=workspace.id,
            task_ref=TASK_REF,
            origin="chat",
            coverage=Coverage(credential_ids=frozenset({row.id, game.id}), triggered_by=("10001",)),
            chat_key=TASK_REF,
        )
        assert lease is not None
        handle = AsyncTaskHandle(task_id=TASK_REF, chat_key=TASK_REF, plugin=SimpleNamespace())
        async for _ctl in cc._run_cc_delegate_task(handle, workspace.id, "做点事", lease):  # noqa: SLF001
            pass
        assert captured
        return captured[0]

    async def test_gate_on_adds_authorized_aliases(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        prompt = await self._run(monkeypatch, enabled=True)
        assert "本任务可用的服务器：game、web1" in prompt
        assert "其余登记过的服务器本任务没有授权" in prompt
        assert "stale" not in prompt and "other" not in prompt

    async def test_gate_off_leaves_prompt_unchanged(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        prompt = await self._run(monkeypatch, enabled=False)
        assert "本任务可用的服务器" not in prompt
        assert "做点事" in prompt


class TestConsoleSendPrompt:
    async def test_console_send_passes_covered_aliases(
        self, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        from nekro_agent.routers import workspaces as router
        from nekro_agent.services.ssh_gateway import authz as authz_module
        from nekro_agent.services.workspace import comm_broadcast as comm_module

        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
        workspace = await make_workspace()
        row = await make_row(workspace, "web1")
        captured: List[str] = []
        envelopes: List[Any] = []

        class _Envelope:
            ssh_servers: Tuple[str, ...] = ()

            def to_prompt(self) -> str:
                return json.dumps(list(self.ssh_servers))

            def to_metadata_json(self) -> str:
                return "{}"

        class _Client:
            def __init__(self, _workspace: Any) -> None:
                pass

            async def stream_message(self, prompt: str, **_kwargs: Any) -> Any:
                captured.append(prompt)
                yield "done"

        async def accessible(_user: Any, _workspace_id: int) -> DBWorkspace:
            return workspace

        async def handshake(_workspace_id: int, _content: str) -> Any:
            envelope = _Envelope()
            envelopes.append(envelope)
            return envelope

        async def prepare(_workspace: Any) -> Dict[str, str]:
            return {}

        async def coverage(_workspace_id: int, _user: Any) -> Coverage:
            return Coverage(credential_ids=frozenset({row.id}), triggered_by=())

        async def quiet(*_args: Any, **_kwargs: Any) -> None:
            return None

        async def persist(**_kwargs: Any) -> None:
            return None

        tasks: List[Any] = []

        def capture(coro: Any) -> Any:
            task = asyncio.ensure_future(coro)
            tasks.append(task)
            return task

        monkeypatch.setattr(router, "assert_workspace_accessible", accessible)
        monkeypatch.setattr(router, "_build_cc_memory_handshake", handshake)
        monkeypatch.setattr(router, "persist_cc_task_memory", persist)
        monkeypatch.setattr(router, "CCSandboxClient", _Client)
        monkeypatch.setattr(router, "publish_system_event", quiet)
        monkeypatch.setattr(comm_module, "publish", quiet)
        monkeypatch.setattr("nekro_agent.services.workspace.credential_policy.prepare_cc_dispatch", prepare)
        monkeypatch.setattr(authz_module, "compute_console_coverage", coverage)
        monkeypatch.setattr(asyncio, "create_task", capture)

        user = DBUser(
            username="owner",
            password="",
            adapter_key="webui",
            platform_userid="0",
            perm_level=Role.Super,
        )
        result = await router.user_send_to_cc(workspace.id, CommSendBody(content="hi"), _current_user=user)
        assert result == {"ok": True}
        assert tasks
        await asyncio.gather(*tasks)
        assert captured == ['["web1"]']
        assert envelopes and envelopes[0].ssh_servers == ("web1",)

    async def test_console_send_gate_off_has_no_note(
        self, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        from nekro_agent.routers import workspaces as router
        from nekro_agent.services.workspace import comm_broadcast as comm_module

        workspace = await make_workspace()
        await make_row(workspace, "web1")
        captured: List[str] = []

        class _Envelope:
            def to_prompt(self) -> str:
                return "plain"

            def to_metadata_json(self) -> str:
                return "{}"

        class _Client:
            def __init__(self, _workspace: Any) -> None:
                pass

            async def stream_message(self, prompt: str, **_kwargs: Any) -> Any:
                captured.append(prompt)
                yield "done"

        async def accessible(_user: Any, _workspace_id: int) -> DBWorkspace:
            return workspace

        async def handshake(_workspace_id: int, _content: str) -> Any:
            return _Envelope()

        async def prepare(_workspace: Any) -> Dict[str, str]:
            return {}

        async def quiet(*_args: Any, **_kwargs: Any) -> None:
            return None

        async def persist(**_kwargs: Any) -> None:
            return None

        tasks: List[Any] = []

        def capture(coro: Any) -> Any:
            task = asyncio.ensure_future(coro)
            tasks.append(task)
            return task

        async def explode(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("关闸时不查覆盖")

        monkeypatch.setattr(router, "assert_workspace_accessible", accessible)
        monkeypatch.setattr(router, "_build_cc_memory_handshake", handshake)
        monkeypatch.setattr(router, "persist_cc_task_memory", persist)
        monkeypatch.setattr(router, "CCSandboxClient", _Client)
        monkeypatch.setattr(router, "publish_system_event", quiet)
        monkeypatch.setattr(comm_module, "publish", quiet)
        monkeypatch.setattr("nekro_agent.services.workspace.credential_policy.prepare_cc_dispatch", prepare)
        monkeypatch.setattr("nekro_agent.services.ssh_gateway.authz.compute_console_coverage", explode)
        monkeypatch.setattr(asyncio, "create_task", capture)

        user = DBUser(
            username="owner",
            password="",
            adapter_key="webui",
            platform_userid="0",
            perm_level=Role.Super,
        )
        result = await router.user_send_to_cc(workspace.id, CommSendBody(content="hi"), _current_user=user)
        assert result == {"ok": True}
        await asyncio.gather(*tasks)
        assert captured == ["plain"]
