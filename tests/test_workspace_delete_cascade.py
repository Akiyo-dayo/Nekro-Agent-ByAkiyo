"""删除工作区的级联，与悬空资源绑定

## 复现出来的现象

建资源 → 绑到工作区 → 删工作区（200）→ 删资源得到 **400**：
`workspace_resource_binding` 里那一行还在，指着一个已经不存在的工作区，
于是 `delete_resource` 的「仍被 N 个工作区引用」判定把它当成一条真引用挡下来。
后台上又看不到这条绑定（`list_workspace_bindings` 要按 `workspace_id` 查，
而那个工作区已经没了；资源卡片上的 `bound_workspaces` 也是按工作区行反查的，
查不到就被过滤掉），所以上一轮只能直连数据库收拾。

## 两个方向都做，各自治的不是同一批数据

- **级联**（`delete_workspace` 解绑）治的是**源头**：从此不再产生新的悬空行。
  只有它是不够的——库里已经有的悬空行不会因为改了删除路径而消失。
- **无效判定**（`delete_resource` 不把悬空绑定算成引用）治的是**存量**：
  不需要任何一次性脚本，用户点一次「删除资源」就把它自己那几行带走了。
  只有它也不够——悬空行仍会不断新增，而且在它被清掉之前
  `bound_workspace_count` 会一直虚高。

第三件事是**看得见**：`binding-health` / `binding-purge` 两条端点 +
`orphan_binding_count` 字段。没有这一层，「存量有没有清干净」这个问题
仍然只能连数据库回答，而那正是 #41 最贵的部分。

## 断言的形状

每条断言都配了能分辨真假的对照，避免写成空的：

| 要防的错法 | 挡住它的对照 |
| --- | --- |
| 级联写成「删工作区时把绑定表清空」 | `test_deleting_one_workspace_leaves_the_other_alone` |
| 无效判定写成「删资源时无视一切绑定」 | `test_a_resource_bound_to_a_live_workspace_still_refuses` |
| 清理写成「把绑定表整表删掉」 | `test_purge_removes_only_the_orphans` |
| 计数写成 `len(bindings)`（改前就是这么错的） | 悬空与在世各一条，两个字段取值不同 |

删除路径上的 Docker / Qdrant 调用一律打桩成**记录调用**，
既让用例确定性（不依赖宿主机上有没有 Docker），又同时充当
「这些清理动作还在被调用」的正向对照。
"""

import ast
import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tortoise import Tortoise

from nekro_agent.core.exception_handlers import register_exception_handlers
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import (
    CognitiveType,
    DBMemParagraph,
    KnowledgeType,
    OriginKind,
)
from nekro_agent.models.db_plugin_data import DBPluginData
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_workspace_comm_log import DBWorkspaceCommLog
from nekro_agent.models.db_workspace_resource import DBWorkspaceResource
from nekro_agent.models.db_workspace_resource_binding import DBWorkspaceResourceBinding
from nekro_agent.routers import resources as resources_router
from nekro_agent.routers import workspaces as workspaces_router
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.schemas.workspace_resource import ResourceField, WorkspaceResourceCreate
from nekro_agent.services.resources import workspace_resource_service
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.role import Role

DEAD_WORKSPACE_ID = 4242
ACME_TENANT_ID = 1

REPO_ROOT = Path(__file__).resolve().parent.parent
CC_WORKSPACE_PLUGIN_DIR = REPO_ROOT / "plugins" / "builtin" / "cc_workspace"
WORKSPACES_ROUTER_SOURCE = REPO_ROOT / "nekro_agent" / "routers" / "workspaces.py"


def _string_kwargs(node: ast.Call) -> Dict[str, str]:
    return {
        keyword.arg: keyword.value.value
        for keyword in node.keywords
        if keyword.arg and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str)
    }


def _cc_prompt_cache_identity() -> Tuple[str, str]:
    """从**插件源码**里读出它实际写入的 `(plugin_key, data_key)`

    这份缓存的唯一写入方是 `plugins/builtin/cc_workspace/main.py` 里那句
    `plugin.store.set(chat_key=..., store_key=...)`，而清理它的两处
    （`delete_workspace` 的级联、`reset_session`）都在核心里**硬编码**同一对字面量 ——
    核心不能反向 import 插件，所以这条跨模块契约没有任何
    编译期/类型层面的保障。

    在测试里也照抄那对字面量就等于什么都没测：实测把插件里的 `last_cc_task_prompt`
    改名（4 处），两处清理同时变成静默空操作，而 185 条相关用例**全绿**（变异存活）。
    所以判据必须取自插件那一侧，不能自己写死（同一条教训见 §2.9 / §2.11）。

    Returns:
        `(plugin_key, data_key)`，其中 `plugin_key` 按 `NekroPlugin._key` 的
        `f"{author}.{module_name}"` 合成。
    """
    meta = ast.parse((CC_WORKSPACE_PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8"))
    author = ""
    module_name = ""
    for node in ast.walk(meta):
        if not isinstance(node, ast.Call):
            continue
        kwargs = _string_kwargs(node)
        if "author" in kwargs and "module_name" in kwargs:
            author = kwargs["author"]
            module_name = kwargs["module_name"]
            break
    if not author or not module_name:
        raise AssertionError("没能从 cc_workspace/plugin.py 里读出 author/module_name —— 判据失效，不是产品没问题")

    main = ast.parse((CC_WORKSPACE_PLUGIN_DIR / "main.py").read_text(encoding="utf-8"))
    prompt_keys = sorted(
        {
            _string_kwargs(node)["store_key"]
            for node in ast.walk(main)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "set"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "store"
            and "store_key" in _string_kwargs(node)
            # 只认写任务正文的那一处：插件还写了一本 SSH 授权别名（值不是 task_prompt），
            # 它不是这份缓存的契约，不能拿来当判据。
            and any(
                keyword.arg == "value" and isinstance(keyword.value, ast.Name) and keyword.value.id == "task_prompt"
                for keyword in node.keywords
            )
        }
    )
    if len(prompt_keys) != 1:
        raise AssertionError(f"cc_workspace 插件的任务正文缓存写入点不是恰好一个，判据需要重写: {prompt_keys}")

    return f"{author}.{module_name}", prompt_keys[0]


CC_WORKSPACE_PLUGIN_KEY, CC_LAST_TASK_PROMPT_KEY = _cc_prompt_cache_identity()


def _user(*, perm_level: Role, tenant_id: int | None, tenant_role: TenantRole) -> DBUser:
    return DBUser(
        username=f"user-{tenant_role.value}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )


def platform_owner() -> DBUser:
    return _user(perm_level=Role.Super, tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER)


def acme_admin() -> DBUser:
    return _user(perm_level=Role.Admin, tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN)


def _resource_body(name: str) -> WorkspaceResourceCreate:
    return WorkspaceResourceCreate(
        name=name,
        template_key=None,
        resource_note="",
        resource_tags=[],
        resource_prompt="",
        fields=[
            ResourceField(field_key="host", label="主机", secret=False, value_kind="host", order=10, value="10.0.0.7"),
        ],
        enabled=True,
    )


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def calls(monkeypatch) -> Dict[str, List[Any]]:
    """把删除路径上的外部副作用打桩成记录

    打桩而不是跳过：这些调用本身就是被测行为的一部分（工作区被删掉时向量、容器
    必须一起清），断言"它们被调用过"才是级联的正向对照。
    """
    from nekro_agent.services.kb import qdrant_manager as kb_qdrant_module
    from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager
    from nekro_agent.services.workspace.container import SandboxContainerManager

    recorded: Dict[str, List[Any]] = {
        "memory_vectors": [],
        "kb_chunk_points": [],
        "container_stop": [],
        "container_remove": [],
    }

    async def fake_delete_by_workspace(workspace_id: int) -> bool:
        recorded["memory_vectors"].append(workspace_id)
        return True

    async def fake_delete_chunk_points(chunk_ids) -> bool:
        recorded["kb_chunk_points"].append(list(chunk_ids))
        return True

    async def fake_stop(workspace: DBWorkspace) -> None:
        recorded["container_stop"].append(workspace.id)

    async def fake_remove(workspace: DBWorkspace) -> None:
        recorded["container_remove"].append(workspace.id)

    monkeypatch.setattr(memory_qdrant_manager, "delete_by_workspace", fake_delete_by_workspace)
    monkeypatch.setattr(kb_qdrant_module.kb_qdrant_manager, "delete_chunk_points", fake_delete_chunk_points)
    monkeypatch.setattr(SandboxContainerManager, "stop", staticmethod(fake_stop))
    monkeypatch.setattr(SandboxContainerManager, "remove", staticmethod(fake_remove), raising=False)
    return recorded


def _build_client(user_factory) -> TestClient:
    """两族路由挂在同一个 app 上 —— #41 就是跨这两族才复现得出来的

    走真实 HTTP 栈而不是直接 `await` 路由函数：`@require_role` 只在被包函数的
    参数名叫 `_current_user` 时生效，直接 await 验不到它。
    """
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(workspaces_router.router, prefix="/api")
    app.include_router(resources_router.router, prefix="/api")
    app.dependency_overrides[get_current_active_user] = user_factory
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client(db) -> TestClient:  # noqa: ARG001
    return _build_client(platform_owner)


async def _bound_pair(name: str = "cc-ws", resource_name: str = "prod ssh") -> Tuple[DBWorkspace, int]:
    """一个工作区 + 一条绑在它上面的资源"""
    workspace = await DBWorkspace.create(name=name, description=name)
    resource = await workspace_resource_service.create_resource(_resource_body(resource_name))
    await workspace_resource_service.bind_resource(workspace.id, resource.id)
    return workspace, resource.id


async def _binding_rows(**filters: Any) -> List[DBWorkspaceResourceBinding]:
    return await DBWorkspaceResourceBinding.filter(**filters).all()


async def _make_orphan(*, resource_name: str = "orphaned") -> Tuple[int, int]:
    """一条指向已不存在工作区的绑定 —— 直接塞，因为级联修好后产品路径已经造不出它"""
    resource = await workspace_resource_service.create_resource(_resource_body(resource_name))
    binding = await DBWorkspaceResourceBinding.create(
        workspace_id=DEAD_WORKSPACE_ID, resource_id=resource.id, enabled=True, sort_order=10
    )
    return resource.id, binding.id


class TestDeletingAWorkspaceUnbindsItsResources:
    """治源头：删工作区时级联解绑"""

    async def test_the_binding_row_is_gone_after_the_workspace_is_deleted(self, client, calls) -> None:  # noqa: ARG002
        workspace, resource_id = await _bound_pair()
        assert len(await _binding_rows(resource_id=resource_id)) == 1, "前提：绑定确实建起来了"

        response = client.delete(f"/api/workspaces/{workspace.id}")

        assert response.status_code == 200, response.text
        assert await DBWorkspace.get_or_none(id=workspace.id) is None
        assert await _binding_rows(resource_id=resource_id) == []

    async def test_the_resource_can_then_be_deleted_through_the_api(self, client, calls) -> None:  # noqa: ARG002
        """#41 现场逐字复现：删工作区 200，再删资源应当也是 200（改前是 400）"""
        workspace, resource_id = await _bound_pair()

        assert client.delete(f"/api/workspaces/{workspace.id}").status_code == 200
        response = client.delete(f"/api/resources/{resource_id}")

        assert response.status_code == 200, response.text
        assert await DBWorkspaceResource.get_or_none(id=resource_id) is None

    async def test_deleting_one_workspace_leaves_the_other_alone(self, client, calls) -> None:  # noqa: ARG002
        """级联不能写成「把这条资源的绑定全清掉」

        同一条资源同时绑在两个工作区上，删掉其中一个之后，另一个的绑定必须分毫未动，
        而且资源本身仍然删不掉（它还被一个活着的工作区引用着）。
        """
        doomed, resource_id = await _bound_pair(name="doomed")
        survivor = await DBWorkspace.create(name="survivor", description="survivor")
        await workspace_resource_service.bind_resource(survivor.id, resource_id)

        assert client.delete(f"/api/workspaces/{doomed.id}").status_code == 200

        remaining = await _binding_rows(resource_id=resource_id)
        assert [row.workspace_id for row in remaining] == [survivor.id]
        assert client.delete(f"/api/resources/{resource_id}").status_code == 400


class TestNormalDeletePathsStillWork:
    """正向对照：级联不能把原本能用的路径改坏"""

    async def test_a_resource_bound_to_a_live_workspace_still_refuses(self, client, calls) -> None:  # noqa: ARG002
        """无效判定不能退化成「删资源时无视一切绑定」"""
        workspace, resource_id = await _bound_pair()

        response = client.delete(f"/api/resources/{resource_id}")

        assert response.status_code == 400, response.text
        assert "1" in response.text
        assert await DBWorkspaceResource.get_or_none(id=resource_id) is not None
        assert len(await _binding_rows(workspace_id=workspace.id)) == 1

    async def test_remove_bindings_still_forces_the_delete_through(self, client, calls) -> None:  # noqa: ARG002
        workspace, resource_id = await _bound_pair()

        response = client.delete(f"/api/resources/{resource_id}?remove_bindings=true")

        assert response.status_code == 200, response.text
        assert await DBWorkspaceResource.get_or_none(id=resource_id) is None
        assert await _binding_rows(workspace_id=workspace.id) == []

    async def test_an_unbound_resource_deletes_as_before(self, client, calls) -> None:  # noqa: ARG002
        resource = await workspace_resource_service.create_resource(_resource_body("standalone"))

        assert client.delete(f"/api/resources/{resource.id}").status_code == 200

    async def test_unbinding_by_hand_still_works(self, client, calls) -> None:  # noqa: ARG002
        workspace, resource_id = await _bound_pair()

        response = client.delete(f"/api/workspaces/{workspace.id}/resources/{resource_id}")

        assert response.status_code == 200, response.text
        assert await _binding_rows(workspace_id=workspace.id) == []
        assert await DBWorkspace.get_or_none(id=workspace.id) is not None


class TestOrphanBindingsAreVisibleAndFixable:
    """治存量 + 后台看得见"""

    async def test_health_endpoint_reports_the_orphan(self, client) -> None:
        resource_id, binding_id = await _make_orphan()

        response = client.get("/api/resources/binding-health")

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["ok"] is False
        assert payload["total"] == 1
        assert payload["live_count"] == 0
        assert payload["orphan_count"] == 1
        assert [item["binding_id"] for item in payload["orphans"]] == [binding_id]
        orphan = payload["orphans"][0]
        assert orphan["workspace_id"] == DEAD_WORKSPACE_ID
        assert orphan["resource_id"] == resource_id
        assert orphan["resource_name"] == "orphaned"
        assert orphan["reason"] == "workspace_missing"

    async def test_health_endpoint_is_clean_when_every_binding_is_live(self, client, calls) -> None:  # noqa: ARG002
        await _bound_pair()

        payload = client.get("/api/resources/binding-health").json()

        assert payload["ok"] is True
        assert (payload["total"], payload["live_count"], payload["orphan_count"]) == (1, 1, 0)
        assert payload["orphans"] == []

    async def test_a_binding_pointing_at_a_deleted_resource_counts_too(self, client) -> None:
        """另一半：绑定指向的**资源**没了同样是悬空，而且同样看不见

        `list_workspace_bindings` 对这种行是直接过滤掉的
        （`if binding.resource_id in resource_map`），所以它在工作区页面上也不存在。
        """
        workspace = await DBWorkspace.create(name="cc-ws", description="cc-ws")
        binding = await DBWorkspaceResourceBinding.create(
            workspace_id=workspace.id, resource_id=9999, enabled=True, sort_order=10
        )

        payload = client.get("/api/resources/binding-health").json()

        assert payload["orphan_count"] == 1
        assert payload["orphans"][0]["binding_id"] == binding.id
        assert payload["orphans"][0]["reason"] == "resource_missing"
        assert payload["orphans"][0]["resource_name"] == ""

    async def test_purge_removes_only_the_orphans(self, client, calls) -> None:  # noqa: ARG002
        """清理不能写成「把绑定表整表删掉」"""
        live_workspace, live_resource_id = await _bound_pair(name="live-ws", resource_name="live res")
        _resource_id, orphan_binding_id = await _make_orphan()

        response = client.post("/api/resources/binding-purge")

        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True, "purged_count": 1}
        assert await DBWorkspaceResourceBinding.get_or_none(id=orphan_binding_id) is None
        remaining = await _binding_rows()
        assert [(row.workspace_id, row.resource_id) for row in remaining] == [(live_workspace.id, live_resource_id)]

    async def test_purge_on_a_clean_database_changes_nothing(self, client, calls) -> None:  # noqa: ARG002
        await _bound_pair()

        assert client.post("/api/resources/binding-purge").json() == {"ok": True, "purged_count": 0}
        assert len(await _binding_rows()) == 1

    async def test_the_resource_summary_separates_live_from_orphan(self, client, calls) -> None:  # noqa: ARG002
        """`bound_workspace_count` 改前数的是 `len(bindings)`，悬空行一起算了进去

        表现就是资源卡片上写着「已绑定 1 个工作区」而下面的工作区清单是空的 ——
        用户看到的是一个自相矛盾的界面，且没有任何入口能处理它。
        """
        workspace, resource_id = await _bound_pair(resource_name="two-faced")
        await DBWorkspaceResourceBinding.create(
            workspace_id=DEAD_WORKSPACE_ID, resource_id=resource_id, enabled=True, sort_order=20
        )

        items = await workspace_resource_service.list_resources()

        assert len(items) == 1
        summary = items[0]
        assert summary.bound_workspace_count == 1
        assert [item.id for item in summary.bound_workspaces] == [workspace.id]
        assert summary.orphan_binding_count == 1

    async def test_deleting_a_resource_ignores_its_orphan_bindings(self, client) -> None:
        """存量的自愈路径：用户点一次「删除资源」就把自己那几行带走

        不需要 `remove_bindings=true`（那一档的语义是"我知道有工作区在用，照删"），
        悬空绑定压根不是引用。
        """
        resource_id, orphan_binding_id = await _make_orphan()

        assert client.delete(f"/api/resources/{resource_id}").status_code == 200
        assert await DBWorkspaceResource.get_or_none(id=resource_id) is None
        assert await DBWorkspaceResourceBinding.get_or_none(id=orphan_binding_id) is None


class TestTheRestOfTheWorkspaceCascade:
    """#41 只是「另一半」里的一件事，删除路径上还漏了几张表

    每一条都单独可红：把对应的那一行改动去掉，只有它会失败。
    """

    async def _populated_workspace(self) -> Tuple[DBWorkspace, DBChatChannel]:
        workspace = await DBWorkspace.create(
            name="cc-ws", description="cc-ws", status="active", container_name="nekro-cc-dead0001"
        )
        channel = await DBChatChannel.create(
            adapter_key="onebot_v11",
            instance_key="inst1",
            channel_id="group_1",
            chat_key="onebot_v11-inst1-group_1",
            channel_type="group",
            data="{}",
            workspace_id=workspace.id,
        )
        await DBMemEpisode.create(
            workspace_id=workspace.id, instance_key="inst1", title="ep", narrative_summary="s"
        )
        await DBMemParagraph.create(
            workspace_id=workspace.id,
            instance_key="inst1",
            memory_source="cc",
            cognitive_type=CognitiveType.EPISODIC,
            knowledge_type=KnowledgeType.CONVERSATION,
            origin_kind=OriginKind.MANUAL,
            content="p",
        )
        await DBWorkspaceCommLog.create(
            workspace_id=workspace.id, direction="SYSTEM", source_chat_key="", content="hello"
        )
        # 键取自插件源码而不是这里写死：写死等于让「插件改名」这个变异静默存活
        await DBPluginData.create(
            plugin_key=CC_WORKSPACE_PLUGIN_KEY,
            target_chat_key=channel.chat_key,
            target_user_id="",
            data_key=CC_LAST_TASK_PROMPT_KEY,
            data_value="stale",
        )
        return workspace, channel

    async def test_episodes_are_deleted_like_the_other_memory_tables(self, client, calls) -> None:  # noqa: ARG002
        """`memory/reset` 删 5 张表，删除工作区只删了 4 张 —— `mem_episode` 漏了"""
        workspace, _channel = await self._populated_workspace()

        assert client.delete(f"/api/workspaces/{workspace.id}").status_code == 200

        assert await DBMemEpisode.filter(workspace_id=workspace.id).count() == 0
        # 对照：本来就删着的那张表仍然被删（证明这条断言不是靠"整个块没跑"过的）
        assert await DBMemParagraph.filter(workspace_id=workspace.id).count() == 0

    async def test_comm_logs_are_deleted(self, client, calls) -> None:  # noqa: ARG002
        workspace, _channel = await self._populated_workspace()

        assert client.delete(f"/api/workspaces/{workspace.id}").status_code == 200

        assert await DBWorkspaceCommLog.filter(workspace_id=workspace.id).count() == 0

    async def test_bound_channels_are_released(self, client, calls) -> None:  # noqa: ARG002
        """频道上那一列不清掉，频道就一直"绑在"一个不存在的工作区上"""
        workspace, channel = await self._populated_workspace()

        assert client.delete(f"/api/workspaces/{workspace.id}").status_code == 200

        refreshed = await DBChatChannel.get(id=channel.id)
        assert refreshed.workspace_id is None
        # 频道本身不能跟着一起删：那是聊天记录的宿主
        assert await DBChatChannel.filter(chat_key=channel.chat_key).count() == 1

    async def test_the_stale_cc_prompt_cache_is_cleared(self, client, calls) -> None:  # noqa: ARG002
        """`会话重置` 会清这份缓存，删掉整个工作区更应该清"""
        workspace, channel = await self._populated_workspace()

        assert client.delete(f"/api/workspaces/{workspace.id}").status_code == 200

        assert (
            await DBPluginData.filter(
                plugin_key=CC_WORKSPACE_PLUGIN_KEY,
                target_chat_key=channel.chat_key,
                data_key=CC_LAST_TASK_PROMPT_KEY,
            ).count()
            == 0
        )

    async def test_the_container_is_removed_not_just_stopped(self, client, calls) -> None:
        """只 `stop` 会把容器永远留在宿主机上：工作区行没了，再没有任何入口能碰它"""
        workspace, _channel = await self._populated_workspace()

        assert client.delete(f"/api/workspaces/{workspace.id}").status_code == 200

        assert calls["container_remove"] == [workspace.id]

    async def test_a_workspace_that_never_had_a_container_still_goes_through_remove(self, client, calls) -> None:
        """没有容器名也调 `remove()`：它先在工作区的锁里把状态换成 deleting，删行之前别的请求起不来
        新容器。没有容器名时 `remove()` 自己不碰 Docker（`test_ws_host_hardening.py` 钉着）；不 stop"""
        workspace = await DBWorkspace.create(name="never-started", description="", status="stopped")

        assert client.delete(f"/api/workspaces/{workspace.id}").status_code == 200

        assert calls["container_remove"] == [workspace.id]
        assert calls["container_stop"] == []

    async def test_memory_vectors_are_still_cleaned(self, client, calls) -> None:
        """正向对照：改动没有把原本就在做的向量清理弄丢"""
        workspace, _channel = await self._populated_workspace()

        assert client.delete(f"/api/workspaces/{workspace.id}").status_code == 200

        assert calls["memory_vectors"] == [workspace.id]

    async def test_docker_being_unavailable_does_not_block_the_delete(self, db, monkeypatch) -> None:  # noqa: ARG002
        """宿主上没有 Docker 时也必须删得掉

        这条路径改前只在 `status == "active"` 时才碰 Docker，现在只要有容器名就碰 ——
        所以"构造 Docker 客户端本身就抛"这一支必须被吃掉，否则等于给无 Docker 的部署
        新增一条"工作区删不掉"的 500。这里**不打桩 `remove`**，走的是真实实现。
        """
        from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager
        from nekro_agent.services.workspace import container as container_module

        async def noop(workspace_id: int) -> bool:  # noqa: ARG001
            return True

        def explode(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("Cannot connect to Docker daemon")

        monkeypatch.setattr(memory_qdrant_manager, "delete_by_workspace", noop)
        monkeypatch.setattr(container_module.aiodocker, "Docker", explode)

        workspace = await DBWorkspace.create(
            name="dockerless", description="", status="active", container_name="nekro-cc-dead0002"
        )
        resource = await workspace_resource_service.create_resource(_resource_body("res"))
        await workspace_resource_service.bind_resource(workspace.id, resource.id)

        response = _build_client(platform_owner).delete(f"/api/workspaces/{workspace.id}")

        assert response.status_code == 200, response.text
        assert await DBWorkspace.get_or_none(id=workspace.id) is None
        # Docker 那一步失败没有把后面的数据库级联一起吞掉
        assert await _binding_rows(workspace_id=workspace.id) == []


class TestTheCcPromptCacheKeyIsNotJustCopiedAround:
    """跨模块的字面量契约：核心里清缓存用的那对键必须与插件写入的那对逐字相同

    上面那条行为用例只驱动得到**删除工作区**这一条路径。同一对字面量在
    `routers/workspaces.py` 里还有第二份（`reset_session`，改前就在），
    而它需要一个在跑的 CC 沙盒才走得到，行为上覆盖不了。核心又不能 import 插件
    （§0.5），所以这一份只能按 AST 钉：把文件里所有带字面量 `plugin_key=` 的调用
    收上来，要求它们**全部**等于插件那一侧算出来的那一对。

    实测（变异）：把插件里的 `last_cc_task_prompt` 改名，本类与上面那条行为用例
    同时变红；改前这个变异在 185 条相关用例下完全存活。
    """

    def _plugin_data_call_pairs(self) -> List[Tuple[str, str]]:
        tree = ast.parse(WORKSPACES_ROUTER_SOURCE.read_text(encoding="utf-8"))
        pairs: List[Tuple[str, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            kwargs = _string_kwargs(node)
            if "plugin_key" in kwargs and "data_key" in kwargs:
                pairs.append((kwargs["plugin_key"], kwargs["data_key"]))
        return pairs

    def test_the_extracted_criterion_is_not_degenerate(self) -> None:
        """先证明判据本身取到了东西 —— 空串对空串同样"相等" """
        assert CC_WORKSPACE_PLUGIN_KEY.count(".") == 1
        assert CC_WORKSPACE_PLUGIN_KEY.split(".")[0]
        assert CC_WORKSPACE_PLUGIN_KEY.split(".")[1]
        assert CC_LAST_TASK_PROMPT_KEY

    def test_every_core_call_site_uses_the_plugins_own_keys(self) -> None:
        pairs = self._plugin_data_call_pairs()

        assert pairs, "核心里一处 DBPluginData 清理都找不到了 —— 要么被删了，要么判据脱靶"
        assert set(pairs) == {(CC_WORKSPACE_PLUGIN_KEY, CC_LAST_TASK_PROMPT_KEY)}


class TestGlobalCountersStayOutOfTenantView:
    """`orphan_binding_count` 与 `bound_workspaces` 同性质：描述的是别的工作区

    租户视角下必须清零，否则新加的这个字段就是一条新的跨租户信息泄漏
    （"平台方另有 3 条绑定挂在别处"）。
    """

    async def test_tenant_view_zeroes_the_global_counters(self, db, calls) -> None:  # noqa: ARG002
        await DBAdapterInstance.create(
            adapter_key="onebot_v11",
            instance_key="inst1",
            display_name="inst1",
            status="online",
            provider="remote_onebot",
            tenant_id=ACME_TENANT_ID,
            metadata_json="",
        )
        workspace, resource_id = await _bound_pair()
        await DBChatChannel.create(
            adapter_key="onebot_v11",
            instance_key="inst1",
            channel_id="group_1",
            chat_key="onebot_v11-inst1-group_1",
            channel_type="group",
            data="{}",
            workspace_id=workspace.id,
        )
        await DBWorkspaceResourceBinding.create(
            workspace_id=DEAD_WORKSPACE_ID, resource_id=resource_id, enabled=True, sort_order=20
        )

        owner_view = _build_client(platform_owner).get(f"/api/workspaces/{workspace.id}/resources").json()
        assert owner_view["items"][0]["resource"]["orphan_binding_count"] == 1, "前提：平台方看得到这一条"

        tenant_view = _build_client(acme_admin).get(f"/api/workspaces/{workspace.id}/resources")

        assert tenant_view.status_code == 200, tenant_view.text
        item = tenant_view.json()["items"][0]["resource"]
        assert item["orphan_binding_count"] == 0
        assert item["bound_workspace_count"] == 0
        assert item["bound_workspaces"] == []
