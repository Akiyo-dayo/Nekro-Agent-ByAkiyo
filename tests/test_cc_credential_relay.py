"""CC 工作区的模型凭据不再进容器

## 这条测试要防的是什么

改造前 `/workspace/settings.json` 与 `/workspace/.claude/settings.json` 里写的是平台方向上游
付费的那把 Key 原文。CC 在容器里对自己的工作目录有完整读权限，于是租户对**自己的**工作区
调 `comm/send` 让 CC「读一下那个文件」就拿到了平台方凭据 —— API 层的租户隔离对这条路无效。

现在两份文件里写的都是**每工作区一把**的中继令牌，真实 Key 只存在于 NA 进程侧，
由 `routers/cc_relay.py` 在转发时换上。

钉死五条：

1. **落盘的两份文件里 0 命中真实 Key。** 这是整条改造的判据，而且必须覆盖两份文件 ——
   `.claude/settings.json` 当前其实不被 `claude` CLI 读（cwd 是 `/workspace/default`），
   但它照样是一个躺在容器可读目录里的凭据副本，漏掉它等于没修。
2. **`settings.json` 的形状逐字不变。** 容器里的 `nekro_cc_sandbox.settings.Settings.from_dict`
   按固定字段名读，多一层少一层都会让 CC 静默退回「没有任何 provider 配置」。
3. **中继令牌不是全局的**：令牌里编着工作区 ID，A 的令牌换到 B 的 ID 上必须解不出授权，
   否则跨工作区的隔离就只是换了个位置的同一个洞。
4. **转发时客户端带来的 Authorization 一定被丢掉、换成上游真 Key。** 透传客户端头是这类
   反向代理最容易写错的一处，写错的表现是「上游 401」而不是「凭据泄漏」，很难联想到这里。
5. **路径白名单**：这把令牌只能打 `claude` CLI 真正会用的那两条路径，
   否则它就是一个通往上游中转站全部接口的开放代理。

另外钉住 `NEKRO_CC_CREDENTIAL_MODE=direct` 时**逐字回到改造前的行为** ——
部署确实无法让容器回连 NA 时要有一条退路，而退路不能是「行为微妙地不一样」。
"""

import ast
import gzip
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import pytest
from fastapi import Request
from tortoise import Tortoise

from nekro_agent.core.cc_model_presets import CCModelPresetItem
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.routers import cc_relay
from nekro_agent.schemas.errors import NotFoundError, UnauthorizedError
from nekro_agent.services.workspace import cc_credential
from nekro_agent.services.workspace import network_guard as workspace_network_guard
from nekro_agent.services.workspace.cc_credential import (
    METADATA_BASE_URL_KEY,
    METADATA_TOKEN_KEY,
    build_claude_settings_payload,
    build_relay_token,
    build_settings_payload,
    parse_relay_token,
    relay_host_port,
    relay_listen_port,
    resolve_relay_base_url,
    resolve_relay_grant,
    resolve_workspace_credential,
)
from nekro_agent.services.workspace.manager import WorkspaceService
from nekro_agent.tools.redaction import REDACTED_CREDENTIAL

# 平台方真实凭据。测试里到处断言「这个串不出现在 X 里」，所以取一个足够独特的取值。
PLATFORM_TOKEN = "sk-platform-REAL-KEY-do-not-leak-0123456789"
UPSTREAM_BASE_URL = "https://api.nekro.ai/claude"

PRESET = CCModelPresetItem(
    id=7,
    name="default",
    base_url=UPSTREAM_BASE_URL,
    auth_token=PLATFORM_TOKEN,
    api_timeout_ms="180000",
    model_type="manual",
    anthropic_model="claude-opus-4",
    small_fast_model="claude-haiku-4",
    extra_env={"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
    is_default=True,
)


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def relay_env(monkeypatch: pytest.MonkeyPatch):
    """默认跑在 relay 模式、地址可预测的环境里"""
    monkeypatch.delenv("NEKRO_CC_CREDENTIAL_MODE", raising=False)
    monkeypatch.setenv("NEKRO_CC_RELAY_BASE_URL", "http://nekro_agent:8021/api/cc-relay")


@pytest.fixture
def preset_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """把预设仓库钉成上面这一份，避免测试读到部署方磁盘上的真实预设文件"""
    monkeypatch.setattr(
        "nekro_agent.core.cc_model_presets.cc_presets_store.get_by_id",
        lambda preset_id: PRESET if preset_id == PRESET.id else None,
    )
    monkeypatch.setattr(
        "nekro_agent.core.cc_model_presets.cc_presets_store.get_default",
        lambda: PRESET,
    )


async def make_workspace(name: str = "ws") -> DBWorkspace:
    return await DBWorkspace.create(name=name, metadata={})


class TestRelayToken:
    """令牌自身的形状与解析"""

    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("", None),
            ("nkcc", None),
            ("nkcc-12", None),
            ("nkcc--abcdefghijklmnop", None),
            ("nkcc-12-short", None),  # 秘密段至少 16 字符
            ("nkcc-abc-abcdefghijklmnop", None),  # 工作区 ID 必须是数字
            # 任意精度整数会被原样送进主键查询：SQLite 查不到，PostgreSQL 直接抛越界，
            # 一次本该 401 的伪造请求就变成 500
            ("nkcc-99999999999999999999-abcdefghijklmnop", None),
            ("nkcc-12-abcdefghijklmnop/x", None),  # urlsafe 之外的字符
            ("sk-platform-REAL-KEY-do-not-leak-0123456789", None),
            ("nkcc-12-abcdefghijklmnop", (12, "abcdefghijklmnop")),
        ],
    )
    def test_parse_rejects_everything_that_is_not_a_relay_token(
        self,
        token: str,
        expected: Optional[Tuple[int, str]],
    ) -> None:
        assert parse_relay_token(token) == expected

    def test_issued_token_round_trips_and_is_unique(self) -> None:
        first = build_relay_token(42)
        second = build_relay_token(42)
        assert parse_relay_token(first) is not None
        assert parse_relay_token(first)[0] == 42  # type: ignore[index]
        assert first != second


class TestRelayBaseUrl:
    """容器视角的中继地址推导"""

    def test_explicit_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEKRO_CC_RELAY_BASE_URL", "http://proxy.internal:9000/api/cc-relay/")
        assert resolve_relay_base_url("172.30.0.2") == "http://proxy.internal:9000/api/cc-relay"

    def test_controller_address_is_used_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """NA 接入了工作区网络时走网桥内部直连，不经过宿主"""
        monkeypatch.delenv("NEKRO_CC_RELAY_BASE_URL", raising=False)
        assert resolve_relay_base_url("172.30.0.2").startswith("http://172.30.0.2:")
        assert resolve_relay_base_url("172.30.0.2").endswith("/api/cc-relay")

    def test_falls_back_to_host_gateway(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """隔离未生效（含 NA 跑在宿主上）时只能经宿主网关，端口取发布到宿主的那个

        端口必须显式钉住：它来自 `OsEnv.EXPOSE_PORT`（`NEKRO_EXPOSE_PORT`），而 8021 只是默认值。
        改过端口的部署上跑这份测试，原先会红在一个与本条契约无关的地方；
        `RUN_IN_DOCKER` 同理——不钉的话 `relay_host_port()` 会转去问 nonebot 驱动，
        而驱动初没初始化取决于测试的执行顺序。
        """
        monkeypatch.delenv("NEKRO_CC_RELAY_BASE_URL", raising=False)
        monkeypatch.setattr(OsEnv, "EXPOSE_PORT", 8021)
        monkeypatch.setattr(OsEnv, "RUN_IN_DOCKER", True)
        assert resolve_relay_base_url("") == "http://host.docker.internal:8021/api/cc-relay"

    def test_host_gateway_port_follows_expose_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """改过 `NEKRO_EXPOSE_PORT` 的部署里，中继地址要跟着改——写死 8021 等于让容器打错端口"""
        monkeypatch.delenv("NEKRO_CC_RELAY_BASE_URL", raising=False)
        monkeypatch.setattr(OsEnv, "EXPOSE_PORT", 18021)
        monkeypatch.setattr(OsEnv, "RUN_IN_DOCKER", True)
        assert resolve_relay_base_url("") == "http://host.docker.internal:18021/api/cc-relay"


class _StubDriver:
    """只提供 `config.port` 的假 nonebot 驱动"""

    def __init__(self, port: int) -> None:
        self.config = SimpleNamespace(port=port)


@pytest.fixture()
def bind_port(monkeypatch: pytest.MonkeyPatch) -> Callable[[Optional[int]], None]:
    """把 `nonebot.get_driver()` 钉成确定的取值（`None` = 驱动没初始化）

    `relay_listen_port()` 问的就是它，而测试进程里驱动初没初始化取决于用例执行顺序 ——
    不钉的话这一组的期望值会随着「先跑了哪份测试」漂移，正是本轮给
    `test_falls_back_to_host_gateway` 钉 `RUN_IN_DOCKER` 的同一个理由。
    """
    import nonebot  # noqa: PLC0415  只在需要时引入，避免整份测试依赖驱动层

    def _pin(port: Optional[int]) -> None:
        if port is None:

            def _uninitialized():  # noqa: ANN202
                raise ValueError("driver not initialized")

            monkeypatch.setattr(nonebot, "get_driver", _uninitialized)
        else:
            monkeypatch.setattr(nonebot, "get_driver", lambda: _StubDriver(port))

    return _pin


class TestRelayPortDerivation:
    """两个端口各自来自哪里

    `resolve_relay_base_url` 有**两个**端口调用点，本仓库此前只有靠宿主网关那一个被覆盖：

    | 分支 | 端口 | 用在哪 |
    | --- | --- | --- |
    | `controller_address` 非空（**生产主路径**：NA 接进了工作区网络） | `relay_listen_port()` | 网桥内部直连 |
    | 兜底 `host.docker.internal` | `relay_host_port()` | 经宿主网关 |

    而 `relay_listen_port()` 还是工作区守卫放行端口（`network_guard.prepare_workspace_network`
    的 `relay_port`）的来源。**实测**：把 `relay_listen_port()` 钉成一个与 `EXPOSE_PORT` 无关的
    常数，本文件与 `test_workspace_network_isolation.py` 合计 126 条**全绿** —— 也就是说
    「容器要打的端口」与「防火墙放行的端口」同时失去覆盖，而它们一旦对不上，
    形态与 §2.6 那次生产事故一模一样：DROP 不回 RST，容器一直等到自己超时。
    """

    @pytest.mark.parametrize(
        ("run_in_docker", "driver_port", "expose_port", "expected_listen", "expected_host"),
        [
            # 标准 compose：`ports: "${NEKRO_EXPOSE_PORT}:8021"`，容器内固定 8021、宿主上是 18021。
            # 两个端口在这里**必须**不同，写死任何一个都会让另一条路径打错门
            (True, 8021, 18021, 8021, 18021),
            # 同上但驱动还没初始化（NA 起来之前就建工作区）：只剩 EXPOSE_PORT 可用
            (True, None, 18021, 18021, 18021),
            # 裸机部署（测试服务器正是这种）：宿主上监听的就是进程 bind 的那个，两者恒等。
            # 本轮把 `RUN_IN_DOCKER` 钉成 True 之后这一支一度零覆盖 ——
            # 实测把 `relay_host_port()` 改成无条件返回 `EXPOSE_PORT`，126 条照样全绿
            (False, 8021, 18021, 8021, 8021),
            (False, None, 18021, 18021, 18021),
            # 驱动答 0（配置缺失）与答不上来等价，不能把 0 当成端口用出去
            (False, 0, 18021, 18021, 18021),
        ],
    )
    def test_port_table(
        self,
        monkeypatch: pytest.MonkeyPatch,
        bind_port: Callable[[Optional[int]], None],
        run_in_docker: bool,
        driver_port: Optional[int],
        expose_port: int,
        expected_listen: int,
        expected_host: int,
    ) -> None:
        bind_port(driver_port)
        monkeypatch.setattr(OsEnv, "EXPOSE_PORT", expose_port)
        monkeypatch.setattr(OsEnv, "RUN_IN_DOCKER", run_in_docker)
        assert relay_listen_port() == expected_listen
        assert relay_host_port() == expected_host

    @pytest.mark.parametrize(
        ("driver_port", "expose_port", "expected_port"),
        [
            (8021, 18021, 8021),
            (None, 18021, 18021),
        ],
    )
    def test_controller_address_url_carries_the_bind_port(
        self,
        monkeypatch: pytest.MonkeyPatch,
        bind_port: Callable[[Optional[int]], None],
        driver_port: Optional[int],
        expose_port: int,
        expected_port: int,
    ) -> None:
        """网桥内部直连打的是 NA **进程 bind 的**端口，不是发布到宿主的那个

        既有的 `test_controller_address_is_used_verbatim` 只断言了前缀与后缀，端口段是空的，
        于是这一整条分支可以返回任何端口而测试照绿。
        """
        monkeypatch.delenv("NEKRO_CC_RELAY_BASE_URL", raising=False)
        bind_port(driver_port)
        monkeypatch.setattr(OsEnv, "EXPOSE_PORT", expose_port)
        monkeypatch.setattr(OsEnv, "RUN_IN_DOCKER", True)
        assert resolve_relay_base_url("172.30.0.2") == f"http://172.30.0.2:{expected_port}/api/cc-relay"

    def test_the_guard_opens_the_very_port_the_container_dials(self) -> None:
        """守卫放行的端口必须与容器要打的端口**同源**

        这两个数分居两个模块：容器那边是 `resolve_relay_base_url`，防火墙那边是
        `prepare_workspace_network` 里的 `relay_port`。它们对不上是一次没有任何日志线索的黑洞
        （§3 #33 现场就疑似这个形状），而两边各自的单测都只拿显式传进去的端口做输入，
        谁都覆盖不到这次派生。所以这里按 AST 钉住「它只能由那两个函数算出来」：
        写死一个字面量端口、或换用别的来源，都会在这里变红。
        """
        source = Path(workspace_network_guard.__file__).read_text(encoding="utf-8")
        target = next(
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "prepare_workspace_network"
        )
        assignment = next(
            node
            for node in ast.walk(target)
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "relay_port" for t in node.targets)
        )
        called = {
            node.func.id for node in ast.walk(assignment.value) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert called <= {"relay_listen_port", "relay_host_port"}, f"relay_port 换了来源: {called}"
        assert called, "relay_port 不再由 cc_credential 的端口推导算出，容器与防火墙可能各说各话"
        literals = {
            node.value
            for node in ast.walk(assignment.value)
            if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool)
        }
        assert literals <= {0}, f"relay_port 里出现了写死的端口 {literals}（0 是「不开口子」的哨兵，允许）"


class TestWorkspaceCredential:
    """算出来该写进容器的那一对凭据"""

    async def test_relay_mode_hands_out_a_token_not_the_platform_key(self, db) -> None:
        workspace = await make_workspace()
        credential = await resolve_workspace_credential(workspace, PRESET)

        assert credential.relayed
        assert credential.auth_token != PLATFORM_TOKEN
        assert parse_relay_token(credential.auth_token) is not None
        assert credential.base_url == "http://nekro_agent:8021/api/cc-relay"

        reloaded = await DBWorkspace.get(id=workspace.id)
        assert reloaded.metadata[METADATA_TOKEN_KEY] == credential.auth_token
        assert reloaded.metadata[METADATA_BASE_URL_KEY] == credential.base_url

    async def test_token_is_stable_without_rotation_and_changes_with_it(self, db) -> None:
        """热更新 settings.json 不能换令牌：那会让容器里正在跑的那一轮 LLM 调用当场 401"""
        workspace = await make_workspace()
        first = await resolve_workspace_credential(workspace, PRESET)
        again = await resolve_workspace_credential(workspace, PRESET)
        rotated = await resolve_workspace_credential(workspace, PRESET, rotate=True)

        assert again.auth_token == first.auth_token
        assert rotated.auth_token != first.auth_token
        assert (await DBWorkspace.get(id=workspace.id)).metadata[METADATA_TOKEN_KEY] == rotated.auth_token

    async def test_hot_update_does_not_point_a_running_container_at_an_unreachable_relay(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """升级后的第一次热更新不能把还没重建过的容器就地写死

        热更新不重启容器，也推不出中继地址，只剩 `host.docker.internal` 可写——而隔离生效的
        部署里发往宿主的包被工作区守卫一律挡掉，写进去等于让这个容器的每一次 LLM 调用都失败，
        而且这个地址会落进 metadata，连重启都修不回来。这种工作区保持改造前的形态，
        等下一次创建/重启（那时才拿得到真实地址）再切中继。
        """
        monkeypatch.delenv("NEKRO_CC_RELAY_BASE_URL", raising=False)
        workspace = await make_workspace()

        credential = await resolve_workspace_credential(workspace, PRESET)
        assert not credential.relayed
        assert credential.base_url == UPSTREAM_BASE_URL
        assert (await DBWorkspace.get(id=workspace.id)).metadata == {}

        # 创建/重启拿得到地址，这时才切过去；之后的热更新沿用它，不再回退
        rotated = await resolve_workspace_credential(
            workspace,
            PRESET,
            rotate=True,
            controller_address="172.30.0.2",
        )
        assert rotated.relayed
        assert rotated.base_url.startswith("http://172.30.0.2:")
        assert (await resolve_workspace_credential(workspace, PRESET)).base_url == rotated.base_url

    async def test_restart_keeps_the_stored_base_url(self, db) -> None:
        """重启不重新推导网络，沿用 metadata 里的地址；只有创建容器时才带着新地址进来"""
        workspace = await make_workspace()
        await resolve_workspace_credential(workspace, PRESET, controller_address="172.30.0.9")
        restarted = await resolve_workspace_credential(workspace, PRESET, rotate=True)
        assert restarted.base_url == "http://nekro_agent:8021/api/cc-relay"

    async def test_direct_mode_is_byte_identical_to_the_old_behaviour(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NEKRO_CC_CREDENTIAL_MODE", "direct")
        workspace = await make_workspace()
        credential = await resolve_workspace_credential(workspace, PRESET)

        assert not credential.relayed
        assert credential.auth_token == PLATFORM_TOKEN
        assert credential.base_url == UPSTREAM_BASE_URL
        # 关掉开关的部署不该被塞进任何新状态
        assert (await DBWorkspace.get(id=workspace.id)).metadata == {}


class TestTokenLifetime:
    """令牌活多久 —— 中继端点挂在 NA 主端口上，不随容器一起消失"""

    async def test_stopping_the_container_revokes_the_token(self, db, preset_store, monkeypatch) -> None:
        """停掉的工作区没有合法的调用方了，还认它的令牌等于「工作区停着、额度还能被烧」"""
        from nekro_agent.services.workspace.container import SandboxContainerManager

        workspace = await make_workspace()
        workspace.container_name = "nekro-cc-deadbeef"
        await workspace.save(update_fields=["container_name"])
        credential = await resolve_workspace_credential(workspace, PRESET, rotate=True)
        assert await resolve_relay_grant(credential.auth_token) is not None

        # 容器早就不在了（Docker 那步会失败并被忽略），吊销仍然要发生
        await SandboxContainerManager.stop(workspace)

        assert await resolve_relay_grant(credential.auth_token) is None
        reloaded = await DBWorkspace.get(id=workspace.id)
        assert METADATA_TOKEN_KEY not in reloaded.metadata
        # 地址留着：它是「这个工作区已经切到中继」的判据，热更新靠它才不会退回真实 Key
        assert reloaded.metadata[METADATA_BASE_URL_KEY] == credential.base_url

    async def test_hot_update_after_stop_issues_a_fresh_token_not_the_platform_key(
        self,
        db,
        preset_store,
        tmp_path: Path,
    ) -> None:
        """吊销之后的热更新要现签一把新的，既不能复活旧令牌，也不能退回写真实 Key"""
        workspace = await make_workspace()
        first = await resolve_workspace_credential(workspace, PRESET, rotate=True)
        metadata = dict(workspace.metadata)
        metadata.pop(METADATA_TOKEN_KEY)
        workspace.metadata = metadata
        await workspace.save(update_fields=["metadata"])

        again = await resolve_workspace_credential(workspace, PRESET)
        assert again.relayed
        assert again.auth_token != first.auth_token
        assert await resolve_relay_grant(first.auth_token) is None

        WorkspaceService._write_settings_files(tmp_path, PRESET, again, "热更新")
        assert PLATFORM_TOKEN not in (tmp_path / "settings.json").read_text(encoding="utf-8")


class TestSettingsOnDisk:
    """真正落进容器可读目录的那两份文件"""

    async def test_settings_json_keeps_its_shape(self, db) -> None:
        """容器侧按固定字段名读，形状改了会让 CC 静默退回「没有任何 provider 配置」"""
        workspace = await make_workspace()
        credential = await resolve_workspace_credential(workspace, PRESET)
        assert build_settings_payload(credential, "claude-opus-4", 180000) == {
            "provider": "anthropic",
            "providers": {
                "anthropic": {
                    "name": "Anthropic",
                    "base_url": "http://nekro_agent:8021/api/cc-relay",
                    "auth_token": credential.auth_token,
                    "model": "claude-opus-4",
                },
            },
            "active_provider": "anthropic",
            "timeout_ms": 180000,
        }

    async def test_claude_settings_keeps_model_mapping_and_extra_env(self, db) -> None:
        workspace = await make_workspace()
        credential = await resolve_workspace_credential(workspace, PRESET)
        env = build_claude_settings_payload(PRESET, credential)["env"]

        assert env["ANTHROPIC_AUTH_TOKEN"] == credential.auth_token
        assert env["ANTHROPIC_BASE_URL"] == "http://nekro_agent:8021/api/cc-relay"
        assert env["ANTHROPIC_MODEL"] == "claude-opus-4"
        assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "claude-haiku-4"
        assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"

    async def test_no_file_written_into_the_workspace_carries_the_platform_key(
        self,
        db,
        tmp_path: Path,
    ) -> None:
        """整条改造的判据：容器挂载进去的目录里 0 命中真实 Key"""
        workspace = await make_workspace()
        credential = await resolve_workspace_credential(workspace, PRESET)
        WorkspaceService._write_settings_files(tmp_path, PRESET, credential, "写入")

        written = sorted(path for path in tmp_path.rglob("*") if path.is_file())
        assert [path.relative_to(tmp_path).as_posix() for path in written] == [
            ".claude/settings.json",
            "settings.json",
        ]
        for path in written:
            assert PLATFORM_TOKEN not in path.read_text(encoding="utf-8")
            assert credential.auth_token in path.read_text(encoding="utf-8")

    async def test_direct_mode_still_writes_the_platform_key(
        self,
        db,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """退路必须是「回到改造前」而不是「微妙地不一样」，否则关掉开关会出别的故障"""
        monkeypatch.setenv("NEKRO_CC_CREDENTIAL_MODE", "direct")
        workspace = await make_workspace()
        credential = await resolve_workspace_credential(workspace, PRESET)
        WorkspaceService._write_settings_files(tmp_path, PRESET, credential, "写入")

        settings = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
        assert settings["providers"]["anthropic"] == {
            "name": "Anthropic",
            "base_url": UPSTREAM_BASE_URL,
            "auth_token": PLATFORM_TOKEN,
            "model": "claude-opus-4",
        }


class TestRelayGrant:
    """令牌 -> 该用哪把真实凭据转发"""

    async def test_valid_token_resolves_to_the_real_upstream(self, db, preset_store) -> None:
        workspace = await make_workspace()
        credential = await resolve_workspace_credential(workspace, PRESET)

        grant = await resolve_relay_grant(credential.auth_token)
        assert grant is not None
        assert grant.workspace_id == workspace.id
        assert grant.upstream_base_url == UPSTREAM_BASE_URL
        assert grant.upstream_token == PLATFORM_TOKEN
        assert grant.timeout_seconds == 180.0

    async def test_one_workspace_token_cannot_be_pointed_at_another(self, db, preset_store) -> None:
        """令牌里的工作区 ID 与秘密段必须同时对上，否则换个 ID 就是跨工作区越权"""
        first = await make_workspace("ws-a")
        second = await make_workspace("ws-b")
        credential = await resolve_workspace_credential(first, PRESET)
        await resolve_workspace_credential(second, PRESET)

        _, secret = parse_relay_token(credential.auth_token)  # type: ignore[misc]
        forged = f"nkcc-{second.id}-{secret}"
        assert await resolve_relay_grant(forged) is None

    @pytest.mark.parametrize("token", ["", "nonsense", "nkcc-999-abcdefghijklmnopqrstuvwx"])
    async def test_unusable_tokens_are_rejected(self, db, preset_store, token: str) -> None:
        assert await resolve_relay_grant(token) is None

    async def test_switching_back_to_direct_revokes_every_issued_token(
        self,
        db,
        preset_store,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """关掉开关必须真的关掉：否则切换之前签发的令牌在一个公网可达的端点上永久有效

        中继端点挂在 NA 主端口上，不随守卫规则一起消失。切回 `direct` 之后容器已经改用真实
        Key、不再走中继，这时还认旧令牌，能用它的就只剩「令牌泄漏出去的人」。
        """
        workspace = await make_workspace()
        credential = await resolve_workspace_credential(workspace, PRESET)
        assert await resolve_relay_grant(credential.auth_token) is not None

        monkeypatch.setenv("NEKRO_CC_CREDENTIAL_MODE", "direct")
        assert await resolve_relay_grant(credential.auth_token) is None

    async def test_preset_fallback_matches_the_one_used_to_write_settings_json(
        self,
        db,
        preset_store,
    ) -> None:
        """中继与 `_resolve_preset` 必须用同一套回落，否则是一个查不出原因的 401

        `cc_model_preset_id` 指向一个已经不存在的预设时，写 `settings.json` 的
        `SandboxContainerManager._resolve_preset` 会回落到默认预设、配置看起来完全正常，
        中继侧要是判成「没有生效的预设」，容器就在每一次 LLM 调用上吃 401。
        """
        workspace = await DBWorkspace.create(name="ws-stale-preset", metadata={"cc_model_preset_id": 999})
        credential = await resolve_workspace_credential(workspace, PRESET)

        grant = await resolve_relay_grant(credential.auth_token)
        assert grant is not None
        assert grant.upstream_token == PLATFORM_TOKEN


class TestRelayEndpoint:
    """中继端点本身的边界"""

    @staticmethod
    def _request(headers: Dict[str, str]) -> Request:
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/cc-relay/v1/messages",
            "raw_path": b"/api/cc-relay/v1/messages",
            "query_string": b"",
            "root_path": "",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": ("172.30.0.5", 51234),
            "server": ("172.30.0.2", 8021),
        }

        async def receive() -> Dict[str, Any]:
            return {"type": "http.request", "body": b'{"model":"opus"}', "more_body": False}

        return Request(scope, receive)  # type: ignore[arg-type]

    async def test_missing_token_is_401(self, db) -> None:
        with pytest.raises(UnauthorizedError):
            await cc_relay.relay_to_upstream("v1/messages", self._request({}))

    async def test_unknown_token_is_401(self, db) -> None:
        with pytest.raises(UnauthorizedError):
            await cc_relay.relay_to_upstream(
                "v1/messages",
                self._request({"authorization": "Bearer nkcc-1-abcdefghijklmnopqrstuvwx"}),
            )

    @pytest.mark.parametrize(
        "path",
        ["v1/models", "v1/organizations/api_keys", "admin", "v1/messages/../../admin", ""],
    )
    async def test_non_allowlisted_paths_are_404(self, db, preset_store, path: str) -> None:
        """令牌只能打 `claude` CLI 真正会用的那两条路径，否则它就是通往上游的开放代理"""
        workspace = await make_workspace()
        credential = await resolve_workspace_credential(workspace, PRESET)
        with pytest.raises(NotFoundError):
            await cc_relay.relay_to_upstream(
                path,
                self._request({"authorization": f"Bearer {credential.auth_token}"}),
            )


class TestForwarding:
    """真正转发出去的那一条请求"""

    @pytest.fixture
    def upstream(self, monkeypatch: pytest.MonkeyPatch, preset_store) -> List[httpx.Request]:
        """把中继端点建的 httpx 客户端换成打到本地假上游的那一个"""
        seen: List[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path.endswith("count_tokens"):
                # 真上游是压缩回的，这里也压缩：不压缩就测不到「改写内容后必须丢掉 content-encoding」
                body = gzip.compress(
                    json.dumps({"error": {"message": f"invalid Authorization: Bearer {PLATFORM_TOKEN}"}}).encode(),
                )
                return httpx.Response(
                    401,
                    headers={"content-type": "application/json", "content-encoding": "gzip"},
                    content=body,
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="event: message_start\ndata: {}\n\n",
            )

        # 直接把进程级共享客户端换成打到假上游的那一个，测完还原
        monkeypatch.setattr(
            cc_relay,
            "_upstream_client",
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        return seen

    async def test_client_authorization_is_replaced_by_the_real_key(self, db, upstream) -> None:
        workspace = await make_workspace()
        credential = await resolve_workspace_credential(workspace, PRESET)

        response = await cc_relay.relay_to_upstream(
            "v1/messages",
            TestRelayEndpoint._request(
                {
                    "authorization": f"Bearer {credential.auth_token}",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                    "host": "nekro_agent:8021",
                },
            ),
        )

        assert response.status_code == 200
        assert len(upstream) == 1
        forwarded = upstream[0]
        assert str(forwarded.url) == f"{UPSTREAM_BASE_URL}/v1/messages"
        assert forwarded.headers["authorization"] == f"Bearer {PLATFORM_TOKEN}"
        # 容器带来的头要透传（否则上游会因为缺 anthropic-version 报错），
        # 但 host 必须由 httpx 按目标地址重写，照抄会打到错误的虚拟主机
        assert forwarded.headers["anthropic-version"] == "2023-06-01"
        assert forwarded.headers["host"] == "api.nekro.ai"

    async def test_the_upstream_client_pool_is_reused(self, db, upstream) -> None:
        """每请求新建客户端 = 每次 LLM 调用重做一次 TLS 握手（PROJECT_STATE §3 #9 的同一个坑）"""
        workspace = await make_workspace()
        credential = await resolve_workspace_credential(workspace, PRESET)
        request_headers = {"authorization": f"Bearer {credential.auth_token}"}

        before = cc_relay._get_upstream_client()
        await cc_relay.relay_to_upstream("v1/messages", TestRelayEndpoint._request(request_headers))
        await cc_relay.relay_to_upstream("v1/messages", TestRelayEndpoint._request(request_headers))

        assert cc_relay._get_upstream_client() is before
        assert len(upstream) == 2

    async def test_upstream_error_body_is_scrubbed(self, db, upstream) -> None:
        """中转站的报错文案回显 Authorization 头是真实发生过的形态（PROJECT_STATE §5）"""
        workspace = await make_workspace()
        credential = await resolve_workspace_credential(workspace, PRESET)

        response = await cc_relay.relay_to_upstream(
            "v1/messages/count_tokens",
            TestRelayEndpoint._request({"authorization": f"Bearer {credential.auth_token}"}),
        )

        assert response.status_code == 401
        assert PLATFORM_TOKEN not in response.body.decode()
        # 内容被改写过，压缩标记必须跟着作废，否则客户端会拿着明文当 gzip 解
        assert "content-encoding" not in {key.lower() for key in response.headers}


# HTTP 的 field-value 不允许首尾空白（RFC 9110 §5.5）。h11 按同一条规矩校验，
# 不合规的值在**上线之前**抛异常，而 `relay_to_upstream` 会把它翻译成
# `502 upstream unreachable: ...` —— 一句指着上游喊冤的报错。
_LEGAL_FIELD_VALUE = re.compile(r"^[!-~]([ \t!-~]*[!-~])?$")


def _preset_with_token(token: str) -> CCModelPresetItem:
    """除 `auth_token` 外与 `PRESET` 逐字相同的一份预设"""
    return CCModelPresetItem(
        id=PRESET.id,
        name=PRESET.name,
        base_url=PRESET.base_url,
        auth_token=token,
        api_timeout_ms=PRESET.api_timeout_ms,
        model_type=PRESET.model_type,
        anthropic_model=PRESET.anthropic_model,
        small_fast_model=PRESET.small_fast_model,
        extra_env=dict(PRESET.extra_env),
        is_default=True,
    )


def _pin_preset(monkeypatch: pytest.MonkeyPatch, preset: CCModelPresetItem) -> None:
    monkeypatch.setattr(
        "nekro_agent.core.cc_model_presets.cc_presets_store.get_by_id",
        lambda preset_id: preset if preset_id == preset.id else None,
    )
    monkeypatch.setattr("nekro_agent.core.cc_model_presets.cc_presets_store.get_default", lambda: preset)


class TestUpstreamAuthorizationHeader:
    """转发时那一条 `Authorization` 的取值

    改动前是无条件 `f"Bearer {grant.upstream_token}"`。预设的 `auth_token` 为空串时
    （**出厂预设正是空串**）得到 `"Bearer "`，h11 拒绝，整条链路的表现是
    `502 {"error":{"message":"upstream unreachable: Illegal header value b'Bearer '"}}` ——
    真因是「没填 Key」，报错却说「上游连不上」。测试部署上真容器实测复现过这一条。

    这里钉三件事：非空取值逐字不变、首尾空白被削掉、空取值**整条头不发**
    （而不是发一个畸形的），且空取值时容器自己带来的 Authorization 仍然不会顺出去。
    """

    @pytest.fixture
    def forwarded(self, monkeypatch: pytest.MonkeyPatch) -> List[httpx.Request]:
        seen: List[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="data: {}\n\n")

        monkeypatch.setattr(
            cc_relay,
            "_upstream_client",
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        return seen

    async def _forward_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
        forwarded: List[httpx.Request],
        preset_token: str,
    ) -> httpx.Request:
        preset = _preset_with_token(preset_token)
        _pin_preset(monkeypatch, preset)

        workspace = await make_workspace()
        credential = await resolve_workspace_credential(workspace, preset)
        await cc_relay.relay_to_upstream(
            "v1/messages",
            TestRelayEndpoint._request(
                {
                    "authorization": f"Bearer {credential.auth_token}",
                    "content-type": "application/json",
                },
            ),
        )
        assert len(forwarded) == 1
        return forwarded[0]

    @pytest.mark.parametrize(
        ("preset_token", "expected"),
        [
            ("sk-upstream-real", "Bearer sk-upstream-real"),
            # 从后台文本框粘贴时最常见的两种脏值：改动前它们同样会造出非法 field-value
            (" sk-upstream-real", "Bearer sk-upstream-real"),
            ("sk-upstream-real\n", "Bearer sk-upstream-real"),
            # 空 / 纯空白：整条头都不发
            ("", None),
            ("   ", None),
            ("\n", None),
        ],
    )
    async def test_authorization_is_wellformed_or_absent(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
        forwarded: List[httpx.Request],
        preset_token: str,
        expected: Optional[str],
    ) -> None:
        request = await self._forward_once(monkeypatch, forwarded, preset_token)
        actual = request.headers.get("authorization")
        assert actual == expected
        if actual is not None:
            # 光断言取值相等还不够：把判据本身写出来，才拦得住「未来又拼出一个带空白的值」
            assert _LEGAL_FIELD_VALUE.match(actual), f"{actual!r} 不是合法的 HTTP field-value，h11 会在上线前拒绝"

    async def test_empty_upstream_token_still_never_leaks_the_relay_token(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
        forwarded: List[httpx.Request],
    ) -> None:
        """空凭据下「不设这个头」不能退化成「把容器带来的那个原样透传」

        中继令牌是容器唯一持有的凭据，顺出去等于把它交给上游中转站。
        """
        request = await self._forward_once(monkeypatch, forwarded, "")
        assert "authorization" not in request.headers
        assert "x-api-key" not in request.headers
        assert "nkcc-" not in str(request.headers)


class TestUpstreamBaseUrl:
    """`base_url` 与 `auth_token` 是同一张表单里的两个字段，必须同样耐得住粘贴脏值

    `auth_token` 那边已经 `strip()` 了，`base_url` 长期没有（`resolve_relay_grant` 里是
    裸 `rstrip("/")`）。而 URL 对空白的反应比首部更难看，实测三种形态：

    | 取值            | 中继                                              | 控制台连通性测试 |
    |-----------------|---------------------------------------------------|------------------|
    | `...claude\\n`   | `InvalidURL` 从 `build_request()` 抛出 -> **500** | 通过             |
    | `  ...claude`   | 502 `unknown url type`                            | 通过             |
    | `...claude `    | 静默打到 `/claude%20/v1/messages` -> 上游 404     | 通过             |

    第三行是最毒的一种：NA 侧一条错都不报，后台点「测试」还显示绿，而每一次 LLM 调用都废。
    根因是两处实现不一致——`anthropic._normalize_messages_endpoint` 一直是
    `.strip().rstrip("/")`，中继这边不是。

    判据钉在**真正发出去的那条 URL** 上，而不是 `grant.upstream_base_url` 的字符串：
    只比字符串的话，「strip 了但拼接时又拼错」照样绿。
    """

    @pytest.fixture
    def forwarded(self, monkeypatch: pytest.MonkeyPatch) -> List[httpx.Request]:
        seen: List[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="data: {}\n\n")

        monkeypatch.setattr(
            cc_relay,
            "_upstream_client",
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        return seen

    @pytest.mark.parametrize(
        "raw_base_url",
        [
            UPSTREAM_BASE_URL,  # 对照：干净取值，改动前后都必须绿
            f"{UPSTREAM_BASE_URL}/",  # 尾随斜杠：`rstrip("/")` 本来就管这一条
            f"{UPSTREAM_BASE_URL}\n",
            f"{UPSTREAM_BASE_URL} ",
            f"  {UPSTREAM_BASE_URL}",
            f"\t{UPSTREAM_BASE_URL}/\n",
        ],
    )
    async def test_pasted_whitespace_still_reaches_the_right_endpoint(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
        forwarded: List[httpx.Request],
        raw_base_url: str,
    ) -> None:
        preset = _preset_with_token(PLATFORM_TOKEN).model_copy(update={"base_url": raw_base_url})
        _pin_preset(monkeypatch, preset)
        workspace = await make_workspace()
        credential = await resolve_workspace_credential(workspace, preset)

        response = await cc_relay.relay_to_upstream(
            "v1/messages",
            TestRelayEndpoint._request({"authorization": f"Bearer {credential.auth_token}"}),
        )

        assert response.status_code == 200
        assert len(forwarded) == 1
        assert str(forwarded[0].url) == f"{UPSTREAM_BASE_URL}/v1/messages"

    async def test_malformed_base_url_is_a_redacted_502_not_a_500(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
        forwarded: List[httpx.Request],
    ) -> None:
        """`strip()` 只治首尾那一类；`base_url` 是自由文本，兜底不能是「端点抛 500」

        取值刻意选**中间**带换行的：`strip()` 对它无效，所以这一条测的确实是 try 的位置，
        而不是顺带被上面那条修复盖住。`build_request()` 曾在 `try` 之外，
        任何建请求阶段的异常都会绕开脱敏、直接变成一次服务端错误——
        而这是个按敌对调用方对待的端点。
        """
        preset = _preset_with_token(PLATFORM_TOKEN).model_copy(
            update={"base_url": "https://api.nekro.ai/cla\nude"},
        )
        _pin_preset(monkeypatch, preset)
        workspace = await make_workspace()
        credential = await resolve_workspace_credential(workspace, preset)

        response = await cc_relay.relay_to_upstream(
            "v1/messages",
            TestRelayEndpoint._request({"authorization": f"Bearer {credential.auth_token}"}),
        )

        assert response.status_code == 502
        assert forwarded == [], "畸形地址不该真的发出去"
        assert PLATFORM_TOKEN not in response.body.decode()


class TestErrorRedactionCoversWhatWasActuallySent:
    """脱敏遮的必须是**真正上了线的那个**凭据字面量

    `_build_upstream_headers` 发出去的是 `auth_token.strip()`，而 `_relay_secret_values`
    此前只收未 strip 的原值。`redact_sandbox_text` 做的是精确字面量替换（形态正则被刻意
    排除在外，见 `credential_guard` 的注释），所以两边一旦对不上就是彻底遮不住：
    预设里那把带尾随换行的 Key 在上游回显 Authorization 的报错原文里以 `Bearer sk-xxx`
    出现，脱敏集合里却是 `sk-xxx\\n` —— 真 Key 原样穿过，落进一个按敌对方对待的容器
    （模块 docstring 第 3 条：中转站报错回显 Authorization 是真实发生过的形态）。

    这个洞是被「空白 token 也照常转发」**打开**的：在那之前带空白的取值会先被 h11 拒掉，
    502 在本地产生、根本到不了上游。所以发送侧与脱敏侧是同一处改动的一体两面。

    上游 401 报错体里回显的是它**实际收到**的那一条 Authorization，而不是用例写死的期望值
    —— 写死就退化成「比较两个恰好相同的串」，测不出「发的和遮的不是同一个」。
    """

    @pytest.fixture
    def echoing_upstream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            received = request.headers.get("authorization", "<absent>")
            return httpx.Response(
                401,
                headers={"content-type": "application/json"},
                json={"error": {"message": f"invalid credential: {received}"}},
            )

        monkeypatch.setattr(
            cc_relay,
            "_upstream_client",
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    @pytest.mark.parametrize(
        "preset_token",
        [
            # 对照：干净取值，两边本来就一致，改动前后都必须绿（否则整组用例说明不了问题）
            PLATFORM_TOKEN,
            # 从后台文本框粘贴时带上的脏值：改动前它们连上游都到不了，改动后才走到脱敏这一步
            f"{PLATFORM_TOKEN}\n",
            f"{PLATFORM_TOKEN} \t",
            f"  {PLATFORM_TOKEN}",
            f"\n{PLATFORM_TOKEN}\n",
        ],
    )
    async def test_real_key_never_survives_the_error_body(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
        echoing_upstream,
        preset_token: str,
    ) -> None:
        _pin_preset(monkeypatch, _preset_with_token(preset_token))
        workspace = await make_workspace()
        credential = await resolve_workspace_credential(workspace, _preset_with_token(preset_token))

        response = await cc_relay.relay_to_upstream(
            "v1/messages",
            TestRelayEndpoint._request({"authorization": f"Bearer {credential.auth_token}"}),
        )
        body = response.body.decode()

        assert response.status_code == 401
        assert PLATFORM_TOKEN not in body, f"平台方真 Key 原样回到了容器: {body!r}"
        # 没有这一条，「上游压根没收到凭据」也能让上面那条绿——那时报错体里本来就没有可遮的东西
        assert REDACTED_CREDENTIAL in body, f"报错体里既没有凭据也没有掩码，这条用例没走到脱敏: {body!r}"


class TestCredentialModeSwitch:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(None, "relay"), ("relay", "relay"), ("direct", "direct"), ("DIRECT", "direct"), ("nonsense", "relay")],
    )
    def test_mode_falls_back_to_relay(
        self,
        monkeypatch: pytest.MonkeyPatch,
        raw: Optional[str],
        expected: str,
    ) -> None:
        """认不出来的取值按最安全的一档处理，而不是静默退回把真 Key 塞进容器"""
        if raw is None:
            monkeypatch.delenv("NEKRO_CC_CREDENTIAL_MODE", raising=False)
        else:
            monkeypatch.setenv("NEKRO_CC_CREDENTIAL_MODE", raw)
        assert cc_credential.credential_mode() == expected
