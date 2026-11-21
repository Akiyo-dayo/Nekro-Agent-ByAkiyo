"""CC 工作区不向租户工作区下发平台方凭据

## 这条测试要防的是什么

多租户部署里有一条 API 层的租户隔离挡不住的出口：**工作区是租户自己的**，所以租户对它
调 `comm/send` 完全合法，而平台方凭据就摆在那个工作区的可读目录与进程环境里。四种凭据：

1. `NEKRO_CC_CREDENTIAL_MODE=direct` 时写进 `settings.json` 的真实模型 Key；
2. 工作区资源（SSH 私钥、口令、令牌），每次向 CC 发请求都会被注入 CC 进程环境；
3. 全局 MCP 库条目的 `env` / `headers`，写进 `.mcp.json`，工作区列表接口还明文返回；
4. CC 模型预设 `extra_env` 里键名像凭据的项，写进 CC 用户级设置（CC home 里的 `settings.json`）
   （早先写在 `.claude/settings.json`，那份旧文件写设置与启动复核时都会删掉）。

判据收在一处（`services/workspace/credential_policy.py`），本文件钉住它以及四个消费者。

## 判据的形态与几条容易写错的对照

- 「单租户部署」必须直接放行、且**一个标记文件都不读**：没有租户就没有这条边界，
  读文件只会在数据目录不可写时把单租户部署的 CC 一起弄坏。用例把 `taint_file` 换成
  一读就炸的函数来钉这条。
- 「曾经归过租户的工作区」必须一直判不合格：解绑回平台方之后目录里仍有租户留下的东西
  （写进 `CLAUDE.md` 的指令、下次运行会把环境变量发出去的脚本）。用例走真实的
  `bind_channel` 再解绑，靠标记文件里那条 id 而不是当前归属下结论。
- 只删文件不够：模型 Key 在 `claude` 进程环境里，所以归属变化时要停容器。用例把
  `SandboxContainerManager.stop` 换成一个记账的假函数——真停容器要 Docker。
- 撤销会重写三份配置文件，那条路径本身也要能跑（否则撤销一半就抛了）。
"""

from __future__ import annotations

import asyncio
import datetime
import inspect
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.core.cc_model_presets import CCModelPresetItem
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace, patch_workspace_metadata
from nekro_agent.models.db_workspace_resource_binding import DBWorkspaceResourceBinding
from nekro_agent.routers import resources as resources_router
from nekro_agent.routers import workspaces as ws_router
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.tenant import CREDENTIAL_MASK, TenantRole
from nekro_agent.schemas.workspace_resource import (
    ResourceField,
    WorkspaceResourceCheckBindBody,
    WorkspaceResourceCreate,
)
from nekro_agent.services.mcp import web_chat_workspace_taint as taint
from nekro_agent.services.mcp.schemas import McpServerConfig
from nekro_agent.services.resources import service as resource_service_module
from nekro_agent.services.resources import workspace_resource_service
from nekro_agent.services.user.role import Role
from nekro_agent.services.workspace import cc_credential, credential_policy, manager
from nekro_agent.services.workspace.cc_credential import (
    build_claude_settings_payload,
    resolve_workspace_credential,
)
from nekro_agent.services.workspace.container import SandboxContainerManager
from nekro_agent.services.workspace.credential_policy import (
    METADATA_DIRECT_WITHHELD_KEY,
    METADATA_SECRET_DELIVERED_KEY,
    METADATA_STARTUP_SWEPT_KEY,
    WITHHELD_MESSAGE,
    PlatformCredentialWithheldError,
    enforce_platform_secret_policy_on_startup,
    platform_credentials_allowed,
    prepare_cc_dispatch,
    wait_pending_revokes,
)
from nekro_agent.services.workspace.manager import WorkspaceService

TENANT_ID = 41
OTHER_TENANT_ID = 42

PLATFORM_KEY = "sk-test-platform"
EXTRA_ENV_SECRET = "s3cret-value"
HOST_VALUE = "10.0.0.7"
# 私钥这里只要一个「不像真密钥、但一定不会被别处写出来」的取值：断言的是它没被注入，
# 取值本身写成假密钥的形态只会让公开树扫描把测试当成泄漏
PRIVATE_KEY_VALUE = "fake-private-key-value-for-tests"

PRESET = CCModelPresetItem(
    id=3,
    name="test-preset",
    base_url="https://api.example.test/claude",
    auth_token=PLATFORM_KEY,
    model_type="manual",
    anthropic_model="claude-opus-4",
    extra_env={
        "FOO_TOKEN": EXTRA_ENV_SECRET,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    },
    is_default=True,
)
# 同一个预设去掉像凭据的 `extra_env`：要让别的条件单独起作用时用它
PLAIN_PRESET = PRESET.model_copy(update={"extra_env": {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}})


# ---------------------------------------------------------------------------
# 库与身份
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
def relay_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认跑在 relay 模式、中继地址可预测；要别的模式的用例自己覆盖"""
    monkeypatch.delenv("NEKRO_CC_CREDENTIAL_MODE", raising=False)
    monkeypatch.setenv("NEKRO_CC_RELAY_BASE_URL", "http://nekro_agent:8021/api/cc-relay")


@pytest.fixture(autouse=True)
def workspace_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """工作区目录与技能根改到临时目录：撤销与初始化都会真的落盘"""
    monkeypatch.setattr(manager, "WORKSPACE_ROOT_DIR", str(tmp_path / "workspaces"))
    monkeypatch.setattr(manager, "SKILLS_LOCAL_DIR", str(tmp_path / "skills" / "local"))
    monkeypatch.setattr(manager, "SKILLS_REPOS_DIR", str(tmp_path / "skills" / "repos"))
    # 全局库读的是部署方磁盘上的真实文件，测试里一律当空库
    monkeypatch.setattr(manager, "_global_library_by_name", lambda: {})


@pytest.fixture
def preset_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "nekro_agent.core.cc_model_presets.cc_presets_store.get_by_id",
        lambda preset_id: PRESET if preset_id == PRESET.id else None,
    )
    monkeypatch.setattr("nekro_agent.core.cc_model_presets.cc_presets_store.get_default", lambda: PRESET)


@pytest.fixture
def stop_calls(monkeypatch: pytest.MonkeyPatch) -> List[DBWorkspace]:
    """把停容器换成记账：真停要 Docker，这里只要「有没有停、停了谁」"""
    calls: List[DBWorkspace] = []

    async def fake_stop(workspace: DBWorkspace) -> None:
        calls.append(workspace)

    monkeypatch.setattr(SandboxContainerManager, "stop", fake_stop)
    return calls


@pytest.fixture
def captured_warnings():
    """收 loguru 的 WARNING：`get_sub_logger` 不走 stdlib 的 logging，`caplog` 收不到"""
    from nekro_agent.core.logger import logger

    captured: List[str] = []
    sink_id = logger.add(lambda message: captured.append(str(message)), level="WARNING", format="{message}")
    try:
        yield captured
    finally:
        logger.remove(sink_id)


async def make_instance(instance_key: str, tenant_id: Any) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key="onebot_v11",
        instance_key=instance_key,
        display_name=instance_key,
        status="online",
        provider="remote_onebot",
        tenant_id=tenant_id,
        metadata_json="",
    )


async def add_channel(workspace: DBWorkspace, instance_key: str) -> DBChatChannel:
    channel_id = f"group_{workspace.id}_{instance_key}"
    return await DBChatChannel.create(
        adapter_key="onebot_v11",
        instance_key=instance_key,
        channel_id=channel_id,
        chat_key=f"onebot_v11-{instance_key}-{channel_id}",
        channel_type="group",
        data="{}",
        workspace_id=workspace.id,
    )


async def make_workspace(name: str, instance_keys: Tuple[str, ...] = ()) -> DBWorkspace:
    workspace = await DBWorkspace.create(name=name, description=name)
    for instance_key in instance_keys:
        await add_channel(workspace, instance_key)
    return workspace


@pytest.fixture
async def multi_tenant(db):  # noqa: ARG001
    await make_instance("tenant-a", TENANT_ID)
    await make_instance("platform-bot", None)


@pytest.fixture
async def single_tenant(db):  # noqa: ARG001
    await make_instance("platform-bot", None)


def tenant_admin() -> DBUser:
    return DBUser(
        username="tenant-admin",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Admin,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )


def platform_owner() -> DBUser:
    return DBUser(
        username="owner",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Super,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


async def settle_background() -> None:
    """把撤销与 `.mcp.json` 清理两条后台任务都等完，免得它们落进下一条用例的库会话"""
    await wait_pending_revokes()
    pending = [task for task in list(taint._scrub_tasks) if not task.done()]  # noqa: SLF001
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def make_resource(name: str = "prod ssh") -> WorkspaceResourceCreate:
    return WorkspaceResourceCreate(
        name=name,
        template_key="ssh_key",
        resource_note="生产跳板机",
        fields=[
            ResourceField(field_key="host", label="主机", secret=False, value_kind="host", order=10, value=HOST_VALUE),
            ResourceField(
                field_key="private_key",
                label="私钥",
                secret=True,
                value_kind="private_key",
                order=20,
                value=PRIVATE_KEY_VALUE,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# 判据本体
# ---------------------------------------------------------------------------


class TestTheCriterion:
    async def test_single_tenant_deployment_allows_everything_and_reads_no_marker_file(
        self,
        single_tenant: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """没有租户就没有这条边界；同时钉住「一个标记文件都不读」"""
        workspace = await make_workspace("plain")

        def explode() -> Path:
            raise AssertionError("单租户部署不该读标记文件")

        monkeypatch.setattr(taint, "taint_file", explode)

        assert await platform_credentials_allowed(workspace) is True

    async def test_workspace_without_tenant_channels_is_allowed(self, multi_tenant: Any) -> None:
        workspace = await make_workspace("platform-only")

        assert await platform_credentials_allowed(workspace) is True

    async def test_workspace_bound_to_a_tenant_channel_is_rejected(self, multi_tenant: Any) -> None:
        workspace = await make_workspace("tenant-owned", ("tenant-a",))

        assert await platform_credentials_allowed(workspace) is False

    async def test_mixing_platform_and_tenant_channels_is_rejected(self, multi_tenant: Any) -> None:
        """混绑属于「归属不唯一」，比单租户更危险：两侧都在用同一个工作区"""
        workspace = await make_workspace("mixed", ("tenant-a", "platform-bot"))

        assert await platform_credentials_allowed(workspace) is False

    async def test_a_workspace_that_once_belonged_to_a_tenant_stays_rejected(self, multi_tenant: Any) -> None:
        workspace = await make_workspace("came-back", ("tenant-a",))
        assert await platform_credentials_allowed(workspace) is False

        channel = await DBChatChannel.get(workspace_id=workspace.id)
        channel.workspace_id = None  # type: ignore[assignment]
        await channel.save(update_fields=["workspace_id", "update_time"])

        # 归属已回到平台方，但目录里可能留着租户放的东西，所以判据不跟着回去
        assert await platform_credentials_allowed(workspace) is False

    async def test_an_unavailable_marker_record_is_rejected(
        self,
        multi_tenant: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = await make_workspace("platform-only")

        async def fake_record() -> bool:
            return False

        monkeypatch.setattr(taint, "ensure_taint_record", fake_record)

        assert await platform_credentials_allowed(workspace) is False


# ---------------------------------------------------------------------------
# 模型 Key
# ---------------------------------------------------------------------------


class TestModelKey:
    async def test_direct_mode_withholds_the_key_from_a_tenant_workspace(
        self,
        multi_tenant: Any,
        preset_store: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(cc_credential, "credential_mode", lambda: "direct")
        workspace = await make_workspace("tenant-owned", ("tenant-a",))

        credential = await resolve_workspace_credential(workspace, PRESET)

        assert credential.auth_token == ""
        assert credential.base_url == ""
        assert credential.relayed is False
        metadata = (await DBWorkspace.get(id=workspace.id)).metadata
        assert metadata[METADATA_DIRECT_WITHHELD_KEY] is True
        assert METADATA_SECRET_DELIVERED_KEY not in metadata

    async def test_direct_mode_still_hands_the_key_to_a_platform_workspace(
        self,
        multi_tenant: Any,
        preset_store: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(cc_credential, "credential_mode", lambda: "direct")
        workspace = await make_workspace("platform-only")

        credential = await resolve_workspace_credential(workspace, PRESET)

        assert credential.auth_token == PLATFORM_KEY
        assert credential.base_url == PRESET.base_url
        metadata = (await DBWorkspace.get(id=workspace.id)).metadata
        assert metadata[METADATA_SECRET_DELIVERED_KEY] is True
        assert METADATA_DIRECT_WITHHELD_KEY not in metadata

    async def test_a_single_tenant_deployment_keeps_the_old_behaviour(
        self,
        single_tenant: Any,
        preset_store: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(cc_credential, "credential_mode", lambda: "direct")
        workspace = await make_workspace("anything")

        credential = await resolve_workspace_credential(workspace, PRESET)

        assert credential.auth_token == PLATFORM_KEY

    async def test_relay_mode_hands_out_a_relay_token_regardless_of_ownership(
        self,
        multi_tenant: Any,
        preset_store: None,
    ) -> None:
        """relay 下真 Key 本来就不进容器，判据在这里不该改变凭据的形态"""
        workspace = await make_workspace("tenant-owned", ("tenant-a",))

        credential = await resolve_workspace_credential(workspace, PRESET, rotate=True)

        assert credential.relayed is True
        assert credential.auth_token.startswith("nkcc-")
        assert PLATFORM_KEY not in credential.auth_token

    async def test_the_hot_update_fallback_is_withheld_from_a_tenant_workspace(
        self,
        multi_tenant: Any,
        preset_store: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """没有中继落点的热更新会退回直连凭据，那条路同样要过判据"""
        monkeypatch.delenv("NEKRO_CC_RELAY_BASE_URL", raising=False)
        workspace = await make_workspace("tenant-owned", ("tenant-a",))

        credential = await resolve_workspace_credential(workspace, PRESET)

        assert credential.auth_token == ""
        assert credential.base_url == ""
        assert credential.relayed is False


class TestOnDiskFiles:
    async def test_a_tenant_workspace_gets_neither_the_key_nor_secret_extra_env(
        self,
        multi_tenant: Any,
        preset_store: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(cc_credential, "credential_mode", lambda: "direct")
        workspace = await make_workspace("tenant-owned", ("tenant-a",))

        await WorkspaceService.init_workspace_dir(workspace, PRESET)

        settings_text = (WorkspaceService.get_workspace_dir(workspace.id) / "settings.json").read_text(encoding="utf-8")
        claude_path = WorkspaceService.claude_home(workspace.id).path / "settings.json"
        claude_text = claude_path.read_text(encoding="utf-8")
        for text in (settings_text, claude_text):
            assert PLATFORM_KEY not in text
            assert EXTRA_ENV_SECRET not in text
        # 非凭据的开关照写：去掉它会把 CC 的功能一起削掉
        assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" in claude_text
        assert json.loads(claude_text)["env"]["ANTHROPIC_AUTH_TOKEN"] == ""

    async def test_writing_settings_removes_the_legacy_project_level_copy(
        self,
        multi_tenant: Any,
        preset_store: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """旧位置 `{ws}/.claude/settings.json` CC 从来不读，里面却可能留着以前写出去的
        真实 Key。写设置时必须删掉它，而不是只是不再写——否则租户工作区的目录里一直躺着平台方凭据"""
        monkeypatch.setattr(cc_credential, "credential_mode", lambda: "direct")
        workspace = await make_workspace("tenant-with-legacy", ("tenant-a",))
        ws_dir = WorkspaceService.get_workspace_dir(workspace.id)
        legacy = ws_dir / ".claude" / "settings.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": PLATFORM_KEY}}), encoding="utf-8")

        await WorkspaceService.init_workspace_dir(workspace, PRESET)

        assert not legacy.exists()
        assert not legacy.parent.exists()
        home = WorkspaceService.claude_home(workspace.id).path
        written = [path for root in (ws_dir, home) for path in root.rglob("*") if path.is_file()]
        assert all(PLATFORM_KEY not in path.read_text(encoding="utf-8", errors="ignore") for path in written)
        assert (home / "settings.json").is_file()

    async def test_a_platform_workspace_still_gets_both(
        self,
        multi_tenant: Any,
        preset_store: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(cc_credential, "credential_mode", lambda: "direct")
        workspace = await make_workspace("platform-only")

        await WorkspaceService.init_workspace_dir(workspace, PRESET)

        settings_text = (WorkspaceService.get_workspace_dir(workspace.id) / "settings.json").read_text(encoding="utf-8")
        claude_path = WorkspaceService.claude_home(workspace.id).path / "settings.json"
        claude_text = claude_path.read_text(encoding="utf-8")
        assert PLATFORM_KEY in settings_text
        assert PLATFORM_KEY in claude_text
        assert EXTRA_ENV_SECRET in claude_text
        assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" in claude_text

    async def test_writing_secret_extra_env_marks_the_workspace(
        self,
        multi_tenant: Any,
        preset_store: None,
    ) -> None:
        """relay 下模型 Key 不进容器，`extra_env` 里像凭据的项却照样写进 CC 用户级设置：
        写出去的那一刻就要记「下发过」；预设里没有这类项时不记（不白白多停一次容器）"""
        with_secret = await make_workspace("platform-secret-extra-env")
        await WorkspaceService.init_workspace_dir(with_secret, PRESET)
        assert (await DBWorkspace.get(id=with_secret.id)).metadata[METADATA_SECRET_DELIVERED_KEY] is True

        without_secret = await make_workspace("platform-plain-extra-env")
        await WorkspaceService.init_workspace_dir(without_secret, PLAIN_PRESET)
        assert METADATA_SECRET_DELIVERED_KEY not in (await DBWorkspace.get(id=without_secret.id)).metadata

    def test_the_payload_drops_only_keys_that_look_like_credentials(self) -> None:
        """判据是键名而不是取值：运维把凭据塞进 `extra_env` 时只有名字能表达这件事"""
        credential = cc_credential.WorkspaceCredential(base_url="http://relay", auth_token="nkcc-1-abcdefghijklmnop", relayed=True)

        withheld = build_claude_settings_payload(PRESET, credential, allow_platform_secrets=False)
        allowed = build_claude_settings_payload(PRESET, credential, allow_platform_secrets=True)

        assert "FOO_TOKEN" not in withheld["env"]
        assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" in withheld["env"]
        assert "FOO_TOKEN" in allowed["env"]
        # 两项凭据始终按传入的 credential 覆盖写入
        assert withheld["env"]["ANTHROPIC_AUTH_TOKEN"] == "nkcc-1-abcdefghijklmnop"
        assert withheld["env"]["ANTHROPIC_BASE_URL"] == "http://relay"


# ---------------------------------------------------------------------------
# 发消息前的统一入口
# ---------------------------------------------------------------------------


class TestPrepareDispatch:
    async def test_direct_mode_refuses_outright_with_the_agreed_message(
        self,
        multi_tenant: Any,
        preset_store: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(cc_credential, "credential_mode", lambda: "direct")
        workspace = await make_workspace("tenant-owned", ("tenant-a",))

        with pytest.raises(PlatformCredentialWithheldError) as exc_info:
            await prepare_cc_dispatch(workspace)

        assert str(exc_info.value) == WITHHELD_MESSAGE

    async def test_relay_mode_skips_resource_injection_for_a_tenant_workspace(
        self,
        multi_tenant: Any,
        preset_store: None,
    ) -> None:
        workspace = await make_workspace("tenant-owned", ("tenant-a",))
        resource = await workspace_resource_service.create_resource(make_resource())
        await DBWorkspaceResourceBinding.create(
            workspace_id=workspace.id, resource_id=resource.id, enabled=True, sort_order=10
        )

        assert await prepare_cc_dispatch(workspace) == {}

    async def test_a_platform_workspace_still_gets_its_resource_env(
        self,
        multi_tenant: Any,
        preset_store: None,
    ) -> None:
        workspace = await make_workspace("platform-only")
        resource = await workspace_resource_service.create_resource(make_resource())
        await workspace_resource_service.bind_resource(workspace.id, resource.id)

        env = await prepare_cc_dispatch(workspace)

        assert env["NEKRO_RESOURCE_PROD_SSH_HOST"] == HOST_VALUE
        assert env["NEKRO_RESOURCE_PROD_SSH_PRIVATE_KEY"] == PRIVATE_KEY_VALUE
        metadata = (await DBWorkspace.get(id=workspace.id)).metadata
        assert metadata[METADATA_SECRET_DELIVERED_KEY] is True

    async def test_a_marked_workspace_is_revoked_before_the_call(
        self,
        multi_tenant: Any,
        preset_store: None,
        stop_calls: List[DBWorkspace],
    ) -> None:
        """归属在别的路径上变了、撤销没跑到时，这一步是兜底"""
        workspace = await make_workspace("tenant-owned", ("tenant-a",))
        # 这个标记是受保护的键，只能按键写（见 `GUARDED_METADATA_KEYS`）
        await patch_workspace_metadata(workspace, set_keys={METADATA_SECRET_DELIVERED_KEY: True})
        workspace.container_name = "nekro-cc-test0001"
        workspace.status = "active"
        await workspace.save(update_fields=["container_name", "status"])

        assert await prepare_cc_dispatch(workspace) == {}

        assert [item.id for item in stop_calls] == [workspace.id]
        metadata = (await DBWorkspace.get(id=workspace.id)).metadata
        assert METADATA_SECRET_DELIVERED_KEY not in metadata


# ---------------------------------------------------------------------------
# 归属变化
# ---------------------------------------------------------------------------


class TestOwnershipChange:
    async def test_binding_a_tenant_channel_stops_the_running_container_and_drops_the_key(
        self,
        multi_tenant: Any,
        preset_store: None,
        stop_calls: List[DBWorkspace],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = await make_workspace("was-platform")
        await patch_workspace_metadata(workspace, set_keys={METADATA_SECRET_DELIVERED_KEY: True})
        workspace.container_name = "nekro-cc-test0002"
        workspace.status = "active"
        await workspace.save(update_fields=["container_name", "status"])
        channel = await add_channel(workspace, "tenant-a")

        await WorkspaceService.bind_channel(workspace, channel.chat_key)
        await settle_background()

        assert [item.id for item in stop_calls] == [workspace.id]
        monkeypatch.setattr(cc_credential, "credential_mode", lambda: "direct")
        credential = await resolve_workspace_credential(workspace, PRESET)
        assert credential.auth_token == ""

    async def test_a_workspace_that_never_had_the_key_keeps_running(
        self,
        multi_tenant: Any,
        stop_calls: List[DBWorkspace],
    ) -> None:
        """没下发过凭据的工作区停它只是白白打断任务"""
        workspace = await make_workspace("never-had-key")
        workspace.container_name = "nekro-cc-test0003"
        workspace.status = "active"
        await workspace.save(update_fields=["container_name", "status"])
        channel = await add_channel(workspace, "tenant-a")

        await WorkspaceService.bind_channel(workspace, channel.chat_key)
        await settle_background()

        assert stop_calls == []

    async def test_mcp_and_extra_env_secrets_alone_are_revoked_too(
        self,
        multi_tenant: Any,
        preset_store: None,
        stop_calls: List[DBWorkspace],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """没有模型 Key（relay）、没有资源，只有带凭据的 MCP 条目和像凭据的 `extra_env`：
        归属变成租户以后两份文件里都不能再有凭据，正在跑的容器也要停（MCP 服务进程是带着
        那份 `env` 拉起来的）"""
        monkeypatch.setattr(manager, "_global_library_by_name", credentialed_library)
        workspace = await make_workspace("platform-mcp-and-extra-env")
        workspace.metadata = {"mcp_servers_enabled": ["with-env", "plain"]}
        await workspace.save(update_fields=["metadata"])
        await WorkspaceService.init_workspace_dir(workspace, PRESET)
        workspace.container_name = "nekro-cc-test0004"
        workspace.status = "active"
        await workspace.save(update_fields=["container_name", "status"])

        ws_dir = WorkspaceService.get_workspace_dir(workspace.id)
        mcp_path = ws_dir / "default" / ".mcp.json"
        claude_path = WorkspaceService.claude_home(workspace.id).path / "settings.json"
        # 前提：两份文件里确实写出了凭据，而模型 Key 没进容器
        assert "ghp-secret" in mcp_path.read_text(encoding="utf-8")
        assert EXTRA_ENV_SECRET in claude_path.read_text(encoding="utf-8")
        assert PLATFORM_KEY not in (ws_dir / "settings.json").read_text(encoding="utf-8")

        channel = await add_channel(workspace, "tenant-a")
        await WorkspaceService.bind_channel(workspace, channel.chat_key)
        await settle_background()

        assert [item.id for item in stop_calls] == [workspace.id]
        mcp_text = mcp_path.read_text(encoding="utf-8")
        assert "ghp-secret" not in mcp_text
        assert list(json.loads(mcp_text)["mcpServers"]) == ["plain"]
        claude_text = claude_path.read_text(encoding="utf-8")
        assert EXTRA_ENV_SECRET not in claude_text
        assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" in claude_text
        assert METADATA_SECRET_DELIVERED_KEY not in (await DBWorkspace.get(id=workspace.id)).metadata


# ---------------------------------------------------------------------------
# 工作区资源
# ---------------------------------------------------------------------------


class TestWorkspaceResources:
    async def test_binding_a_resource_to_a_tenant_workspace_is_rejected_and_creates_no_row(
        self,
        multi_tenant: Any,
    ) -> None:
        workspace = await make_workspace("tenant-owned", ("tenant-a",))
        resource = await workspace_resource_service.create_resource(make_resource())

        with pytest.raises(ValidationError) as exc_info:
            await workspace_resource_service.bind_resource(workspace.id, resource.id)

        assert "租户工作区不能绑定平台方资源" in str(exc_info.value)
        assert await DBWorkspaceResourceBinding.filter(workspace_id=workspace.id).exists() is False

    async def test_an_existing_binding_injects_nothing_into_a_tenant_workspace(
        self,
        multi_tenant: Any,
    ) -> None:
        workspace = await make_workspace("tenant-owned", ("tenant-a",))
        resource = await workspace_resource_service.create_resource(make_resource())
        await DBWorkspaceResourceBinding.create(
            workspace_id=workspace.id, resource_id=resource.id, enabled=True, sort_order=10
        )

        assert (
            await workspace_resource_service.resolve_workspace_resources_to_env(
                workspace.id, allow_platform_secrets=False
            )
            == {}
        )

    async def test_the_prompt_cache_is_blanked_for_a_tenant_workspace(
        self,
        multi_tenant: Any,
    ) -> None:
        """资源说明里有非敏感字段的取值，等同于一页平台方资产清单"""
        workspace = await make_workspace("tenant-owned", ("tenant-a",))
        resource = await workspace_resource_service.create_resource(make_resource())
        await DBWorkspaceResourceBinding.create(
            workspace_id=workspace.id, resource_id=resource.id, enabled=True, sort_order=10
        )

        await workspace_resource_service.refresh_workspace_cache(workspace.id)

        metadata = (await DBWorkspace.get(id=workspace.id)).metadata
        assert metadata["resource_prompt_cache"] == ""
        assert metadata["resource_count_cache"] == 1

    async def test_a_platform_workspace_keeps_env_and_prompt(
        self,
        multi_tenant: Any,
    ) -> None:
        workspace = await make_workspace("platform-only")
        resource = await workspace_resource_service.create_resource(make_resource())
        await workspace_resource_service.bind_resource(workspace.id, resource.id)

        env = await workspace_resource_service.resolve_workspace_resources_to_env(
            workspace.id, allow_platform_secrets=True
        )
        await workspace_resource_service.refresh_workspace_cache(workspace.id)

        assert env["NEKRO_RESOURCE_PROD_SSH_HOST"] == HOST_VALUE
        metadata = (await DBWorkspace.get(id=workspace.id)).metadata
        assert HOST_VALUE in metadata["resource_prompt_cache"]

    async def test_check_bind_refuses_a_tenant_workspace_and_404s_an_unknown_one(
        self,
        multi_tenant: Any,
    ) -> None:
        """前端先调 check-bind 再调绑定：这一步放行的话，界面会先显示「可以绑定」"""
        workspace = await make_workspace("tenant-owned", ("tenant-a",))
        resource = await workspace_resource_service.create_resource(make_resource())
        body = WorkspaceResourceCheckBindBody(resource_id=resource.id)

        with pytest.raises(ValidationError) as exc_info:
            await resources_router.check_workspace_resource_bind(workspace.id, body, _current_user=platform_owner())
        assert "租户工作区不能绑定平台方资源" in str(exc_info.value)

        with pytest.raises(NotFoundError):
            await resources_router.check_workspace_resource_bind(999999, body, _current_user=platform_owner())

    async def test_check_bind_still_answers_for_a_platform_workspace(
        self,
        multi_tenant: Any,
    ) -> None:
        workspace = await make_workspace("platform-only")
        resource = await workspace_resource_service.create_resource(make_resource())
        body = WorkspaceResourceCheckBindBody(resource_id=resource.id)

        result = await resources_router.check_workspace_resource_bind(
            workspace.id, body, _current_user=platform_owner()
        )

        assert result.ok is True


# ---------------------------------------------------------------------------
# 全局 MCP 库条目
# ---------------------------------------------------------------------------


def credentialed_library() -> Dict[str, Dict[str, Any]]:
    return {
        "with-env": {
            "name": "with-env",
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "pkg"],
            "env": {"GITHUB_TOKEN": "ghp-secret"},
            "validation": {"status": "validated"},
        },
        "with-headers": {
            "name": "with-headers",
            "type": "http",
            "url": "https://example.test/mcp",
            "headers": {"Authorization": "Bearer secret"},
            "validation": {"status": "validated"},
        },
        "plain": {
            "name": "plain",
            "type": "http",
            "url": "https://example.test/plain",
            "validation": {"status": "validated"},
        },
    }


def mcp_configs() -> List[McpServerConfig]:
    return [
        McpServerConfig(name="with-env", type="stdio", command="npx", env={"GITHUB_TOKEN": "ghp-secret"}),
        McpServerConfig(name="with-headers", type="http", url="https://example.test/mcp", headers={"X-Api-Key": "hdr"}),
        McpServerConfig(name="plain", type="http", url="https://example.test/plain"),
    ]


class TestGlobalMcpEntries:
    def test_disk_config_skips_credentialed_entries_only(self) -> None:
        metadata = {"mcp_servers_enabled": ["with-env", "with-headers", "plain"]}

        allowed = manager._build_disk_mcp_config(  # noqa: SLF001
            metadata, library=credentialed_library(), allow_platform_secrets=True
        )
        withheld = manager._build_disk_mcp_config(  # noqa: SLF001
            metadata, library=credentialed_library(), allow_platform_secrets=False
        )

        assert sorted(allowed["mcpServers"]) == ["plain", "with-env", "with-headers"]
        assert list(withheld["mcpServers"]) == ["plain"]
        assert "ghp-secret" not in json.dumps(withheld)

    async def test_writing_a_credentialed_entry_marks_the_workspace(
        self,
        multi_tenant: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """真写出了带凭据的条目才记「下发过」：只有不带凭据的条目、或者条目被判据挡下时都不记"""
        monkeypatch.setattr(manager, "_global_library_by_name", credentialed_library)

        async def written(name: str, enabled: List[str], instance_keys: Tuple[str, ...] = ()) -> Dict[str, Any]:
            workspace = await make_workspace(name, instance_keys)
            workspace.metadata = {"mcp_servers_enabled": enabled}
            await workspace.save(update_fields=["metadata"])
            await WorkspaceService.sync_mcp_to_disk(workspace)
            return (await DBWorkspace.get(id=workspace.id)).metadata

        assert (await written("platform-headers", ["with-headers", "plain"]))[METADATA_SECRET_DELIVERED_KEY] is True
        assert METADATA_SECRET_DELIVERED_KEY not in await written("platform-plain", ["plain"])
        assert METADATA_SECRET_DELIVERED_KEY not in await written("tenant-env", ["with-env"], ("tenant-a",))

    async def test_tenant_reads_masked_values_and_no_candidates_with_credentials(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_accessible(_user: DBUser, _workspace_id: int) -> object:
            return object()

        async def fake_enabled(_ws: object) -> List[McpServerConfig]:
            return mcp_configs()

        async def fake_available(_ws: object) -> List[McpServerConfig]:
            return mcp_configs()

        monkeypatch.setattr(ws_router, "assert_workspace_accessible", fake_accessible)
        monkeypatch.setattr(ws_router.WorkspaceService, "list_mcp_servers", fake_enabled)
        monkeypatch.setattr(ws_router.WorkspaceService, "list_available_mcp_servers", fake_available)

        joined = await ws_router.list_mcp_servers(1, _current_user=tenant_admin())
        available = await ws_router.list_available_mcp_servers(1, _current_user=tenant_admin())

        by_name = {server.name: server for server in joined.servers}
        assert set(by_name["with-env"].env.values()) == {CREDENTIAL_MASK}
        assert set(by_name["with-headers"].headers.values()) == {CREDENTIAL_MASK}
        assert by_name["plain"].env == {}
        # 键名保留：租户仍然看得出这一条要哪几个变量，只是看不到值
        assert list(by_name["with-env"].env) == ["GITHUB_TOKEN"]
        assert [server.name for server in available.servers] == ["plain"]

    async def test_platform_owner_reads_the_plain_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_accessible(_user: DBUser, _workspace_id: int) -> object:
            return object()

        async def fake_enabled(_ws: object) -> List[McpServerConfig]:
            return mcp_configs()

        async def fake_available(_ws: object) -> List[McpServerConfig]:
            return []

        monkeypatch.setattr(ws_router, "assert_workspace_accessible", fake_accessible)
        monkeypatch.setattr(ws_router.WorkspaceService, "list_mcp_servers", fake_enabled)
        monkeypatch.setattr(ws_router.WorkspaceService, "list_available_mcp_servers", fake_available)

        joined = await ws_router.list_mcp_servers(1, _current_user=platform_owner())

        by_name = {server.name: server for server in joined.servers}
        assert by_name["with-env"].env["GITHUB_TOKEN"] == "ghp-secret"
        assert by_name["with-headers"].headers["X-Api-Key"] == "hdr"

    async def test_a_tenant_cannot_add_a_credentialed_entry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        enabled: List[str] = []

        async def fake_accessible(_user: DBUser, _workspace_id: int) -> object:
            return object()

        async def fake_available(_ws: object) -> List[McpServerConfig]:
            return mcp_configs()

        async def fake_enabled(_ws: object) -> List[McpServerConfig]:
            return []

        async def fake_set(_ws: object, name: str, _enabled: bool) -> None:
            enabled.append(name)

        monkeypatch.setattr(ws_router, "assert_workspace_accessible", fake_accessible)
        monkeypatch.setattr(ws_router.WorkspaceService, "list_available_mcp_servers", fake_available)
        monkeypatch.setattr(ws_router.WorkspaceService, "list_mcp_servers", fake_enabled)
        monkeypatch.setattr(ws_router.WorkspaceService, "set_mcp_server_enabled", fake_set)

        with pytest.raises(ValidationError):
            await ws_router.add_mcp_server_to_workspace(1, "with-env", _current_user=tenant_admin())
        assert enabled == []

        # 平台方还能加：候选列表对他没有过滤
        await ws_router.add_mcp_server_to_workspace(1, "with-env", _current_user=platform_owner())
        assert enabled == ["with-env"]


# ---------------------------------------------------------------------------
# 启动复核
# ---------------------------------------------------------------------------


class TestStartupSweep:
    async def test_multi_tenant_direct_warns_and_stops_the_leftover_container(
        self,
        multi_tenant: Any,
        preset_store: None,
        stop_calls: List[DBWorkspace],
        captured_warnings: List[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(cc_credential, "credential_mode", lambda: "direct")
        legacy = await make_workspace("legacy-tenant", ("tenant-a",))
        legacy.container_name = "nekro-cc-test0004"
        legacy.status = "active"
        await legacy.save(update_fields=["container_name", "status"])
        platform_ws = await make_workspace("platform-only")
        platform_ws.container_name = "nekro-cc-test0005"
        platform_ws.status = "active"
        await platform_ws.save(update_fields=["container_name", "status"])

        await enforce_platform_secret_policy_on_startup()
        await settle_background()

        assert any("改为 relay" in message for message in captured_warnings)
        assert [item.id for item in stop_calls] == [legacy.id]

    async def test_legacy_claude_settings_is_removed_on_every_startup(
        self,
        multi_tenant: Any,
        preset_store: None,
        stop_calls: List[DBWorkspace],
    ) -> None:
        """已经复核过一次（带 swept 标记）、此后再也不会写设置的存量工作区，旧位置的
        `.claude/settings.json` 只能靠启动复核删。删文件不必停容器：CC 从来没读过它"""
        workspace = await make_workspace("already-swept")
        workspace.metadata = {METADATA_STARTUP_SWEPT_KEY: True}
        workspace.container_name = "nekro-cc-test0011"
        workspace.status = "active"
        await workspace.save(update_fields=["metadata", "container_name", "status"])
        ws_dir = WorkspaceService.get_workspace_dir(workspace.id)
        legacy = ws_dir / ".claude" / "settings.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"env": {"FOO_TOKEN": EXTRA_ENV_SECRET}}), encoding="utf-8")
        # 同一目录里别的东西不归这里管：只删那份设置，目录留着
        (legacy.parent / "keep.txt").write_text("x", encoding="utf-8")

        await enforce_platform_secret_policy_on_startup()
        await settle_background()

        assert not legacy.exists()
        assert (legacy.parent / "keep.txt").exists()
        assert stop_calls == []

    async def test_each_workspace_is_swept_only_once(
        self,
        multi_tenant: Any,
        preset_store: None,
        stop_calls: List[DBWorkspace],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """复核过一次之后，重启不再停同一个工作区的容器（之后的写入路径都按判据走）"""
        monkeypatch.setattr(cc_credential, "credential_mode", lambda: "direct")
        legacy = await make_workspace("legacy-tenant", ("tenant-a",))
        legacy.container_name = "nekro-cc-test0008"
        legacy.status = "active"
        await legacy.save(update_fields=["container_name", "status"])

        await enforce_platform_secret_policy_on_startup()
        await settle_background()
        assert [item.id for item in stop_calls] == [legacy.id]

        # 模拟它之后又被拉起来（容器名还在、状态回到运行），再重启一次
        again = await DBWorkspace.get(id=legacy.id)
        again.status = "active"
        await again.save(update_fields=["status"])
        await enforce_platform_secret_policy_on_startup()
        await settle_background()
        assert [item.id for item in stop_calls] == [legacy.id]

    async def test_a_single_tenant_deployment_does_nothing(
        self,
        single_tenant: Any,
        stop_calls: List[DBWorkspace],
        captured_warnings: List[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(cc_credential, "credential_mode", lambda: "direct")
        workspace = await make_workspace("plain")
        workspace.container_name = "nekro-cc-test0006"
        workspace.status = "active"
        await workspace.save(update_fields=["container_name", "status"])

        await enforce_platform_secret_policy_on_startup()
        await settle_background()

        assert stop_calls == []
        assert [message for message in captured_warnings if "改为 relay" in message] == []

    async def test_relay_mode_leaves_a_tenant_workspace_alone(
        self,
        multi_tenant: Any,
        stop_calls: List[DBWorkspace],
    ) -> None:
        """relay 下模型 Key 本来就不进容器，没有可撤的东西；资源绑定才是要停容器的条件"""
        workspace = await make_workspace("tenant-owned", ("tenant-a",))
        workspace.container_name = "nekro-cc-test0007"
        workspace.status = "active"
        await workspace.save(update_fields=["container_name", "status"])

        await enforce_platform_secret_policy_on_startup()
        await settle_background()

        assert stop_calls == []

    async def test_a_stock_platform_workspace_is_marked_then_revoked_when_it_turns_tenant(
        self,
        multi_tenant: Any,
        stop_calls: List[DBWorkspace],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """升级前就跑着的平台方工作区，带资源绑定、没有「下发过」标记（改造前的写盘不记）：
        启动复核按配置补记、不解密资源，也不停它；之后绑上租户频道，撤销照样停容器、重写文件"""
        monkeypatch.setattr("nekro_agent.core.cc_model_presets.cc_presets_store.get_default", lambda: PLAIN_PRESET)
        workspace = await make_workspace("stock-platform")
        await WorkspaceService.init_workspace_dir(workspace, PLAIN_PRESET)
        resource = await workspace_resource_service.create_resource(make_resource())
        await workspace_resource_service.bind_resource(workspace.id, resource.id)
        stock = await DBWorkspace.get(id=workspace.id)
        stock.container_name = "nekro-cc-test0009"
        stock.status = "active"
        await stock.save(update_fields=["container_name", "status"])
        # 前提：没有标记；资源说明（含主机名）写进了 CLAUDE.md；中继落点已知、预设里没有像凭据的
        # `extra_env`——补记只能是因为那条资源绑定
        assert METADATA_SECRET_DELIVERED_KEY not in stock.metadata
        assert stock.metadata.get(cc_credential.METADATA_BASE_URL_KEY)
        claude_md = WorkspaceService.get_workspace_dir(workspace.id) / "default" / "CLAUDE.md"
        assert HOST_VALUE in claude_md.read_text(encoding="utf-8")

        guard = {"forbid": True}
        real_decrypt = resource_service_module.decrypt_secret_payload

        def guarded_decrypt(*args: Any, **kwargs: Any) -> Any:
            if guard["forbid"]:
                raise AssertionError("启动复核推断时不该解密资源")
            return real_decrypt(*args, **kwargs)

        monkeypatch.setattr(resource_service_module, "decrypt_secret_payload", guarded_decrypt)

        await enforce_platform_secret_policy_on_startup()
        await settle_background()
        guard["forbid"] = False

        assert (await DBWorkspace.get(id=workspace.id)).metadata[METADATA_SECRET_DELIVERED_KEY] is True
        assert stop_calls == []

        channel = await add_channel(workspace, "tenant-a")
        await WorkspaceService.bind_channel(await DBWorkspace.get(id=workspace.id), channel.chat_key)
        await settle_background()

        assert [item.id for item in stop_calls] == [workspace.id]
        assert HOST_VALUE not in claude_md.read_text(encoding="utf-8")
        assert METADATA_SECRET_DELIVERED_KEY not in (await DBWorkspace.get(id=workspace.id)).metadata

    async def test_stock_inference_runs_once_per_workspace(
        self,
        multi_tenant: Any,
        preset_store: None,
        stop_calls: List[DBWorkspace],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """补标记与撤销共用「复核过」标记：第二次启动不再推断，也不再停容器"""
        calls: List[int] = []
        real_inference = credential_policy._stock_may_hold_platform_secrets  # noqa: SLF001

        async def counting(workspace: DBWorkspace, *, direct: bool) -> bool:
            calls.append(workspace.id)
            return await real_inference(workspace, direct=direct)

        monkeypatch.setattr(credential_policy, "_stock_may_hold_platform_secrets", counting)
        # 一个推断为「下发过」（有容器、预设里有像凭据的 `extra_env`），一个推断为「没有」（从没起过容器）
        marked_ws = await make_workspace("stock-once-marked")
        marked_ws.container_name = "nekro-cc-test0010"
        marked_ws.status = "active"
        await marked_ws.save(update_fields=["container_name", "status"])
        untouched_ws = await make_workspace("stock-once-untouched")

        await enforce_platform_secret_policy_on_startup()
        await settle_background()

        after_first = {ws.id: (await DBWorkspace.get(id=ws.id)).metadata for ws in (marked_ws, untouched_ws)}
        assert after_first[marked_ws.id][METADATA_SECRET_DELIVERED_KEY] is True
        assert METADATA_SECRET_DELIVERED_KEY not in after_first[untouched_ws.id]
        # 两条路径都记「复核过」：补记过的那个以后被划给租户、撤销清掉「下发过」之后，
        # 重启也不会再被当成存量停一次容器
        assert all(metadata[METADATA_STARTUP_SWEPT_KEY] is True for metadata in after_first.values())

        await enforce_platform_secret_policy_on_startup()
        await settle_background()

        assert sorted(calls) == sorted([marked_ws.id, untouched_ws.id])
        assert stop_calls == []

    async def test_a_single_tenant_deployment_marks_stock_without_reading_the_marker_file(
        self,
        single_tenant: Any,
        stop_calls: List[DBWorkspace],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """单租户部署也补记：之后建了租户、把它划过去，走的是同一条撤销路径；但不调判据、不读标记文件"""

        def explode() -> Path:
            raise AssertionError("单租户部署不该读标记文件")

        monkeypatch.setattr(taint, "taint_file", explode)
        workspace = await make_workspace("single-tenant-stock")
        workspace.container_name = "nekro-cc-test0011"
        workspace.status = "active"
        await workspace.save(update_fields=["container_name", "status"])
        resource = await workspace_resource_service.create_resource(make_resource())
        await DBWorkspaceResourceBinding.create(workspace_id=workspace.id, resource_id=resource.id, enabled=True, sort_order=10)

        await enforce_platform_secret_policy_on_startup()
        await settle_background()

        assert (await DBWorkspace.get(id=workspace.id)).metadata[METADATA_SECRET_DELIVERED_KEY] is True
        assert stop_calls == []


class TestStockInference:
    """改造前的写盘会不会已经把平台方凭据交给了这个工作区：只看配置，不看值，更不解密"""

    @pytest.mark.parametrize(
        ("case", "expected"),
        [
            ("plain", False),
            ("never_started", False),
            ("resource_binding", True),
            ("credentialed_mcp", True),
            ("secret_extra_env_key", True),
            ("direct_mode_key", True),
            ("no_relay_address_key", True),
        ],
    )
    async def test_each_kind_counts_and_nothing_else_does(
        self,
        multi_tenant: Any,
        monkeypatch: pytest.MonkeyPatch,
        case: str,
        expected: bool,
    ) -> None:
        monkeypatch.setattr(manager, "_global_library_by_name", credentialed_library)
        # 键名像凭据、值为空也算：推断只看配置的形状
        preset = PLAIN_PRESET.model_copy(update={"extra_env": {"FOO_TOKEN": ""}}) if case == "secret_extra_env_key" else PLAIN_PRESET
        monkeypatch.setattr("nekro_agent.core.cc_model_presets.cc_presets_store.get_default", lambda: preset)

        workspace = await make_workspace(f"stock-{case}")
        metadata: Dict[str, Any] = {cc_credential.METADATA_BASE_URL_KEY: "http://nekro_agent:8021/api/cc-relay"}
        if case == "no_relay_address_key":
            metadata = {}
        if case == "credentialed_mcp":
            metadata["mcp_servers_enabled"] = ["with-env", "plain"]
        workspace.metadata = metadata
        if case != "never_started":
            workspace.container_name = f"nekro-cc-{case}"
        await workspace.save(update_fields=["metadata", "container_name"])
        if case in ("resource_binding", "never_started"):
            # 从没起过容器、目录也不存在的工作区，配置里有资源也什么都没拿到过
            resource = await workspace_resource_service.create_resource(make_resource(f"res {case}"))
            await DBWorkspaceResourceBinding.create(
                workspace_id=workspace.id, resource_id=resource.id, enabled=True, sort_order=10
            )

        inferred = await credential_policy._stock_may_hold_platform_secrets(  # noqa: SLF001
            workspace, direct=case == "direct_mode_key"
        )

        assert inferred is expected


# ---------------------------------------------------------------------------
# 静态守卫
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _call_names(node: Any) -> set:
    import ast

    names = set()
    for item in ast.walk(node):
        if not isinstance(item, ast.Call):
            continue
        if isinstance(item.func, ast.Name):
            names.add(item.func.id)
        elif isinstance(item.func, ast.Attribute):
            names.add(item.func.attr)
    return names


class TestStaticGuards:
    @pytest.mark.parametrize(
        "function",
        [
            workspace_resource_service.resolve_workspace_resources_to_env,
            manager._build_disk_mcp_config,  # noqa: SLF001
            build_claude_settings_payload,
            WorkspaceService._write_settings_files,  # noqa: SLF001
        ],
    )
    def test_the_switch_is_required_and_has_no_default(self, function: Any) -> None:
        """必填参数是这次的判据之一：加了默认值，漏改的调用方会静默走「允许」那一档"""
        parameter = inspect.signature(function).parameters.get("allow_platform_secrets")

        assert parameter is not None, f"{function.__qualname__} 少了 allow_platform_secrets"
        assert parameter.default is inspect.Parameter.empty

    def test_resource_env_resolution_has_exactly_one_call_site(self) -> None:
        import ast

        offenders: List[str] = []
        for path in sorted((_repo_root() / "nekro_agent").rglob("*.py")) + sorted(
            (_repo_root() / "plugins").rglob("*.py")
        ):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr != "resolve_workspace_resources_to_env":
                    continue
                if path.name != "credential_policy.py":
                    offenders.append(f"{path.relative_to(_repo_root())}:{node.lineno}")

        assert offenders == [], f"资源解密应该只在 prepare_cc_dispatch 里发生: {offenders}"

    def test_every_dispatch_site_goes_through_the_policy(self) -> None:
        """发消息的那一层（含它所在的闭包链）必须先过判据

        `user_send_to_cc` 把 `stream_message` 放在后台闭包 `_cc_task` 里，判据在外层函数里算好；
        所以检查的是「自己或任一外层函数」——闭包里再算一次反而会重复解密。
        """
        import ast

        def enclosing(node: Any, ancestors: List[Any]) -> set:
            names: set = set()
            for item in [node, *reversed(ancestors)]:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names |= _call_names(item)
            return names

        checked = 0
        for path in sorted((_repo_root() / "nekro_agent").rglob("*.py")) + sorted(
            (_repo_root() / "plugins").rglob("*.py")
        ):
            source = path.read_text(encoding="utf-8")
            if ".stream_message(" not in source:
                continue
            tree = ast.parse(source)

            def visit(node: Any, ancestors: List[Any]) -> None:
                nonlocal checked
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and "stream_message" in _call_names(node):
                    checked += 1
                    assert "prepare_cc_dispatch" in enclosing(node, ancestors), (
                        f"{path.relative_to(_repo_root())} 的 {node.name} 向 CC 发消息却没走 "
                        "prepare_cc_dispatch：判据会被绕过"
                    )
                for child in ast.iter_child_nodes(node):
                    visit(child, [*ancestors, node])

            visit(tree, [])
        assert checked >= 2, "两处发消息的调用点应当都被扫到"
