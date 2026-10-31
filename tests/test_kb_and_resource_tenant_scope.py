"""工作区知识库 / 工作区资源 / 全局素材库的租户作用域

补上 PROJECT_STATE §3 #29 记的那个洞：`routers/workspaces.py` 那 72 个端点先修好了，
同样挂在 `/workspaces/{workspace_id}/...` 下的 `routers/workspace_kb.py`(17) 与
`routers/resources.py`(5) 是同一个洞的第二块。实测（改前，真实 HTTP，租户管理员身份）：

    GET    /api/workspaces/{别的租户的 ws}/kb/documents    -> 200，返回对方文档标题
    DELETE /api/workspaces/{别的租户的 ws}/kb/documents/1  -> 200，对方文档被删掉
    GET    /api/resources/1                                -> 200，返回平台方 SSH 私钥**明文**
    PUT    /api/kb-library/assets/1/bindings               -> 200，整表覆盖别人的绑定

## 为什么是整表而不是抽样

上一轮修完只抽了 5 个知识库端点 + 1 个资源端点写测试，剩下 15 个的作用域**没有任何测试
盯着**——而"一族端点里有几个漏了守卫"正是这个洞每次的形态（§3 #26 与 #29 是同一件事
被发现了两次）。所以知识库那 17 条是**全表驱动**的 `KB_ENDPOINT_CASES`，
并由 `TestEveryLiveKnowledgeBaseEndpointHasACase` 反过来钉住"路由表里有、用例表里没有"
直接红；新增第 18 个知识库端点必须同时在这里出现。资源那 5 条端点数量少、
每条的判据各不相同（404 / 403 / 视角裁剪），逐条写成方法而不是拼一张表。

## 守卫测得再足，也不等于被守住的数据没漏

上面那 17 条把 `search_workspace_kb` 整个桩掉了 —— 它们要测的是路由层的归属判定。
但检索是本模块唯一直接吐**正文片段**的出口，它自己的过滤条件一旦漏掉，
租户搜**自己的**工作区就会搜出别人的正文，而路由守卫全程无感。
`TestSearchItselfStaysInsideTheWorkspace` 补的就是这一跳。

## 每条都有平台方的正向对照

只断言"租户拿到 404"证明不了拒绝来自权限判定——端点自己坏了（缺参数、桩没接上、
文件不存在）同样是异常。所以每个端点都跑两遍**同一组实参**，只换身份：

- 租户管理员 -> `NotFoundError`，且副作用桩一次都没被调到、数据行数分毫未动
- 平台方 / 未启用多租户的管理员 -> 成功，并对结果做逐字断言（不是"没抛异常"）

document 级端点在租户方向上传的 `(workspace_id, document_id)` 恰好是 rival-ws 里
真实存在的一对，所以正向对照用的是**完全相同的实参**，唯一的变量就是身份。
"""

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import pytest
from fastapi import HTTPException, UploadFile
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_kb_asset import DBKBAsset
from nekro_agent.models.db_kb_document import DBKBDocument
from nekro_agent.models.db_kb_document_reference import DBKBDocumentReference
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_workspace_resource import DBWorkspaceResource
from nekro_agent.models.db_workspace_resource_binding import DBWorkspaceResourceBinding
from nekro_agent.routers import workspace_kb as workspace_kb_module
from nekro_agent.routers.kb_library import (
    bind_kb_library_asset_workspace,
    get_kb_library_asset_bindings,
    unbind_kb_library_asset_workspace,
)
from nekro_agent.routers.resources import (
    bind_workspace_resource,
    check_workspace_resource_bind,
    list_workspace_resources,
    reorder_workspace_resources,
    unbind_workspace_resource,
)
from nekro_agent.routers.workspace_kb import (
    add_document_reference_route,
    create_workspace_kb_document,
    delete_workspace_kb_document,
    get_document_references_route,
    get_workspace_kb_document,
    get_workspace_kb_fulltext,
    get_workspace_kb_raw_file,
    get_workspace_kb_source_file_info,
    get_workspace_kb_tree,
    list_workspace_kb_documents,
    reindex_workspace_kb,
    reindex_workspace_kb_document,
    remove_document_reference_route,
    search_workspace_kb_route,
    update_document_reference_route,
    update_workspace_kb_document,
    upload_workspace_kb_file,
)
from nekro_agent.schemas.errors import NotFoundError
from nekro_agent.schemas.i18n import SupportedLang
from nekro_agent.schemas.kb import (
    KBAddReferenceBody,
    KBCreateTextDocumentBody,
    KBSearchRequest,
    KBSearchResponse,
    KBUpdateDocumentBody,
    KBUpdateReferenceBody,
)
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.schemas.workspace_resource import (
    WorkspaceResourceBindBody,
    WorkspaceResourceCheckBindBody,
    WorkspaceResourceReorderBody,
    WorkspaceResourceReorderItem,
)
from nekro_agent.services.user.role import Role

ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2
MISSING_WORKSPACE_ID = 9999

# (工作区名, 绑定频道的 instance_key)
WORKSPACE_LAYOUT = [
    ("acme-ws", "inst1"),
    ("rival-ws", "rival1"),
    ("platform-ws", None),
]

# 每个工作区里的三个文档。取名让 `order_by("source_path")` 的顺序可预期，
# 并且 plan -> appendix 预置一条引用，好让引用族的四个端点各有真实数据可动。
DOCUMENT_STEMS = ("appendix", "notes", "plan")
PRESET_REFERENCE_DESCRIPTION = "预置引用"


def make_user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
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


def acme_admin() -> DBUser:
    return make_user(tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def platform_owner() -> DBUser:
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


def single_tenant_admin() -> DBUser:
    """未启用多租户的部署里的普通管理员：无租户归属、角色为平台方"""
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Admin)


UNRESTRICTED_IDENTITIES = [
    pytest.param(platform_owner, id="platform-owner"),
    pytest.param(single_tenant_admin, id="single-tenant"),
]


# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------


@dataclass
class Env:
    """一次用例里用得到的全部标识与桩记录

    `rival_*` 是"别的租户的"，也就是租户方向必须被拒、平台方方向必须成功的那一组。
    """

    acme_ws: int
    rival_ws: int
    platform_ws: int
    rival_plan: int
    rival_appendix: int
    rival_notes: int
    calls: Dict[str, List[Any]]

    def foreign_workspace_ids(self) -> List[int]:
        return [self.rival_ws, self.platform_ws, MISSING_WORKSPACE_ID]


async def _init_schema() -> None:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()


@pytest.fixture
async def env(tmp_path, monkeypatch) -> Any:
    """三个工作区 × 三个文档 + 全部外部副作用的桩

    工作区根目录改到 `tmp_path`：知识库的创建/改名/下载/全文都真的落盘，
    不把仓库里的 `data/` 写脏，也不必为了避开磁盘而把被测端点整段桩掉。
    """
    monkeypatch.setattr("nekro_agent.services.workspace.manager.WORKSPACE_ROOT_DIR", str(tmp_path))

    calls: Dict[str, List[Any]] = {
        key: []
        for key in (
            "schedule",
            "cancel",
            "delete_files",
            "delete_points",
            "rebuild_workspace",
            "search",
            "detect_refs",
            "sync_metadata",
            "ensure_collection",
            "get_document",
        )
    }

    async def _schedule(document):
        calls["schedule"].append(document.id)
        return True

    async def _cancel(document_id: int):
        calls["cancel"].append(document_id)
        return True

    async def _delete_files(document):
        calls["delete_files"].append(document.id)

    async def _delete_points(chunk_ids):
        calls["delete_points"].append(list(chunk_ids))
        return 0

    async def _chunk_ids(document_id: int):
        return []

    async def _rebuild_workspace(workspace_id: int):
        calls["rebuild_workspace"].append(workspace_id)
        return 2, 1

    async def _search(*, workspace_id: int, query: str, **_kwargs):
        calls["search"].append((workspace_id, query))
        return KBSearchResponse(workspace_id=workspace_id, query=query, total=0, next_action_hint="stubbed")

    async def _detect_refs(workspace_id: int, document_id: int):
        calls["detect_refs"].append((workspace_id, document_id))
        return 0

    async def _sync_metadata(document):
        calls["sync_metadata"].append(document.id)
        return 0

    async def _ensure_collection():
        calls["ensure_collection"].append(True)
        return True

    # 这个不是桩而是**记账的转发**：文档查询本身必须照常发生，要钉的是"什么时候发生"。
    real_get_document = workspace_kb_module.get_document

    async def _get_document(workspace_id: int, document_id: int):
        calls["get_document"].append((workspace_id, document_id))
        return await real_get_document(workspace_id, document_id)

    for name, stub in (
        ("schedule_rebuild_document", _schedule),
        ("cancel_rebuild_document", _cancel),
        ("delete_document_files", _delete_files),
        ("delete_document_vector_points", _delete_points),
        ("list_document_chunk_ids", _chunk_ids),
        ("rebuild_workspace_documents", _rebuild_workspace),
        ("search_workspace_kb", _search),
        ("detect_and_sync_document_references", _detect_refs),
        ("sync_document_index_metadata", _sync_metadata),
        ("ensure_kb_collection", _ensure_collection),
        ("ensure_kb_embedding_configured", lambda: None),
        ("get_document", _get_document),
    ):
        monkeypatch.setattr(f"nekro_agent.routers.workspace_kb.{name}", stub)

    await _init_schema()
    try:
        from nekro_agent.services.workspace.manager import WorkspaceService

        for instance_key, tenant_id in [("inst1", ACME_TENANT_ID), ("rival1", RIVAL_TENANT_ID)]:
            await DBAdapterInstance.create(
                adapter_key="onebot_v11", instance_key=instance_key, display_name=instance_key,
                status="online", provider="remote_onebot", tenant_id=tenant_id, metadata_json="",
            )

        workspace_ids: Dict[str, int] = {}
        document_ids: Dict[Tuple[str, str], int] = {}
        for index, (name, instance_key) in enumerate(WORKSPACE_LAYOUT):
            workspace = await DBWorkspace.create(name=name, description=name)
            workspace_ids[name] = workspace.id
            if instance_key:
                await DBChatChannel.create(
                    adapter_key="onebot_v11", instance_key=instance_key,
                    channel_id=f"group_{index}", chat_key=f"onebot_v11-{instance_key}-group_{index}",
                    channel_type="group", data="{}", workspace_id=workspace.id,
                )
            WorkspaceService.ensure_kb_dirs(workspace.id)
            for stem in DOCUMENT_STEMS:
                source_path = f"{stem}.md"
                WorkspaceService.resolve_kb_source_path(workspace.id, source_path).write_text(
                    f"{name} {stem} body", encoding="utf-8"
                )
                WorkspaceService.resolve_kb_normalized_path(workspace.id, f"{stem}.txt").write_text(
                    f"{name} {stem} normalized", encoding="utf-8"
                )
                document = await DBKBDocument.create(
                    workspace_id=workspace.id, title=f"{name} {stem}", category="doc",
                    file_name=source_path, file_ext=".md", mime_type="text/markdown",
                    format="markdown", source_path=source_path, normalized_text_path=f"{stem}.txt",
                    content_hash=f"h-{name}-{stem}", file_size=1,
                    extract_status="ready", sync_status="ready",
                )
                document_ids[(name, stem)] = document.id
            await DBKBDocumentReference.create(
                workspace_id=workspace.id,
                source_document_id=document_ids[(name, "plan")],
                target_document_id=document_ids[(name, "appendix")],
                description=PRESET_REFERENCE_DESCRIPTION,
            )

        yield Env(
            acme_ws=workspace_ids["acme-ws"],
            rival_ws=workspace_ids["rival-ws"],
            platform_ws=workspace_ids["platform-ws"],
            rival_plan=document_ids[("rival-ws", "plan")],
            rival_appendix=document_ids[("rival-ws", "appendix")],
            rival_notes=document_ids[("rival-ws", "notes")],
            calls=calls,
        )
    finally:
        await Tortoise.close_connections()


# ---------------------------------------------------------------------------
# 知识库 17 个端点的用例表
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """一个端点的两面：怎么调、成功了该长什么样

    `call` 只吃 `(workspace_id, user, env)`，所以租户方向与平台方向用的是同一份实参。
    """

    id: str
    call: Callable[[int, DBUser, Env], Awaitable[Any]]
    check: Callable[[Any, Env], Awaitable[None]]


async def _check_list(result, env: Env) -> None:
    assert [item.title for item in result.items] == [
        "rival-ws appendix", "rival-ws notes", "rival-ws plan",
    ]
    assert result.total == 3


async def _check_tree(result, env: Env) -> None:
    assert [(node.path, node.type) for node in result.nodes] == [
        ("appendix.md", "file"), ("notes.md", "file"), ("plan.md", "file"),
    ]


async def _check_detail(result, env: Env) -> None:
    assert result.document.id == env.rival_plan
    assert result.document.title == "rival-ws plan"
    assert result.source_content == "rival-ws plan body"
    assert result.normalized_content == "rival-ws plan normalized"


async def _check_created(result, env: Env) -> None:
    assert result.document.title == "platform planted"
    assert result.source_content == "planted body"
    titles = await DBKBDocument.filter(workspace_id=env.rival_ws).values_list("title", flat=True)
    assert sorted(titles) == ["platform planted", "rival-ws appendix", "rival-ws notes", "rival-ws plan"]
    assert env.calls["schedule"] == [result.document.id]


async def _check_uploaded(result, env: Env) -> None:
    assert result.document.file_name == "uploaded.md"
    assert result.source_content == "uploaded body"
    assert await DBKBDocument.filter(workspace_id=env.rival_ws).count() == 4
    assert env.calls["schedule"] == [result.document.id]


async def _check_updated(result, env: Env) -> None:
    assert result.document.title == "platform renamed"
    assert (await DBKBDocument.get(id=env.rival_plan)).title == "platform renamed"
    # 只改了标题：不该重建索引，但要重扫引用关系
    assert env.calls["schedule"] == []
    assert env.calls["detect_refs"] == [(env.rival_ws, env.rival_plan)]


async def _check_deleted(result, env: Env) -> None:
    assert result.ok is True
    assert not await DBKBDocument.filter(id=env.rival_plan).exists()
    assert await DBKBDocument.filter(workspace_id=env.rival_ws).count() == 2
    assert env.calls["cancel"] == [env.rival_plan]
    assert env.calls["delete_files"] == [env.rival_plan]
    # 引用关系随文档一起清掉，不留悬空行
    assert await DBKBDocumentReference.filter(workspace_id=env.rival_ws).count() == 0


async def _check_raw(result, env: Env) -> None:
    assert result.filename == "plan.md"
    assert Path(str(result.path)).read_text(encoding="utf-8") == "rival-ws plan body"


async def _check_fulltext(result, env: Env) -> None:
    assert result.document_id == env.rival_plan
    assert result.content == "rival-ws plan normalized"
    assert result.truncated is False
    assert result.source_workspace_path == "/workspace/default/kb/files/plan.md"


async def _check_document_reindex(result, env: Env) -> None:
    assert (result.total, result.success, result.failed) == (1, 1, 0)
    assert env.calls["schedule"] == [env.rival_plan]


async def _check_workspace_reindex(result, env: Env) -> None:
    # 桩返回 (scheduled=2, skipped=1)
    assert (result.total, result.success, result.failed) == (3, 2, 0)
    assert env.calls["rebuild_workspace"] == [env.rival_ws]


async def _check_search(result, env: Env) -> None:
    assert env.calls["search"] == [(env.rival_ws, "对方的资料")]
    assert result.workspace_id == env.rival_ws
    assert result.next_action_hint == "stubbed"


async def _check_references(result, env: Env) -> None:
    assert [(item.document_id, item.description) for item in result.references_to] == [
        (env.rival_appendix, PRESET_REFERENCE_DESCRIPTION),
    ]
    assert result.referenced_by == []


async def _check_reference_added(result, env: Env) -> None:
    assert sorted((item.document_id, item.description) for item in result.references_to) == sorted(
        [(env.rival_appendix, PRESET_REFERENCE_DESCRIPTION), (env.rival_notes, "平台方新增")]
    )
    assert await DBKBDocumentReference.filter(workspace_id=env.rival_ws).count() == 2


async def _check_reference_updated(result, env: Env) -> None:
    assert [(item.document_id, item.description) for item in result.references_to] == [
        (env.rival_appendix, "平台方改写"),
    ]
    row = await DBKBDocumentReference.get(
        workspace_id=env.rival_ws, source_document_id=env.rival_plan, target_document_id=env.rival_appendix
    )
    assert row.description == "平台方改写"


async def _check_reference_removed(result, env: Env) -> None:
    assert result.references_to == []
    assert await DBKBDocumentReference.filter(workspace_id=env.rival_ws).count() == 0


async def _check_source_file(result, env: Env) -> None:
    assert result.document_id == env.rival_plan
    assert result.source_path == "plan.md"
    assert result.source_workspace_path == "/workspace/default/kb/files/plan.md"


def _upload_file() -> UploadFile:
    """每次调用现造一份，避免被读过一次的流在下一次调用里变成空文件"""
    import io

    payload = b"uploaded body"
    return UploadFile(file=io.BytesIO(payload), filename="uploaded.md", size=len(payload))


KB_ENDPOINT_CASES: List[Case] = [
    Case(
        "GET /kb/documents",
        lambda ws, user, env: list_workspace_kb_documents(workspace_id=ws, _current_user=user),
        _check_list,
    ),
    Case(
        "GET /kb/tree",
        lambda ws, user, env: get_workspace_kb_tree(workspace_id=ws, _current_user=user),
        _check_tree,
    ),
    Case(
        "GET /kb/documents/{id}",
        lambda ws, user, env: get_workspace_kb_document(
            workspace_id=ws, document_id=env.rival_plan, _current_user=user
        ),
        _check_detail,
    ),
    Case(
        "POST /kb/documents",
        lambda ws, user, env: create_workspace_kb_document(
            workspace_id=ws,
            body=KBCreateTextDocumentBody(
                title="platform planted", content="planted body", source_path="planted.md", format="markdown"
            ),
            _current_user=user,
        ),
        _check_created,
    ),
    Case(
        "POST /kb/files",
        lambda ws, user, env: upload_workspace_kb_file(
            workspace_id=ws, file=_upload_file(), title="", source_path="", category="",
            tags="", summary="", is_enabled=True, _current_user=user,
        ),
        _check_uploaded,
    ),
    Case(
        "PUT /kb/documents/{id}",
        lambda ws, user, env: update_workspace_kb_document(
            workspace_id=ws, document_id=env.rival_plan,
            body=KBUpdateDocumentBody(title="platform renamed"), _current_user=user,
        ),
        _check_updated,
    ),
    Case(
        "DELETE /kb/documents/{id}",
        lambda ws, user, env: delete_workspace_kb_document(
            workspace_id=ws, document_id=env.rival_plan, _current_user=user
        ),
        _check_deleted,
    ),
    Case(
        "GET /kb/documents/{id}/raw",
        lambda ws, user, env: get_workspace_kb_raw_file(
            workspace_id=ws, document_id=env.rival_plan, _current_user=user
        ),
        _check_raw,
    ),
    Case(
        "GET /kb/documents/{id}/fulltext",
        lambda ws, user, env: get_workspace_kb_fulltext(
            workspace_id=ws, document_id=env.rival_plan, max_chars=20000, _current_user=user
        ),
        _check_fulltext,
    ),
    Case(
        "POST /kb/documents/{id}/reindex",
        lambda ws, user, env: reindex_workspace_kb_document(
            workspace_id=ws, document_id=env.rival_plan, _current_user=user
        ),
        _check_document_reindex,
    ),
    Case(
        "POST /kb/reindex",
        lambda ws, user, env: reindex_workspace_kb(workspace_id=ws, _current_user=user),
        _check_workspace_reindex,
    ),
    Case(
        "POST /kb/search",
        lambda ws, user, env: search_workspace_kb_route(
            workspace_id=ws, body=KBSearchRequest(query="对方的资料"), _current_user=user
        ),
        _check_search,
    ),
    Case(
        "GET /kb/documents/{id}/references",
        lambda ws, user, env: get_document_references_route(
            workspace_id=ws, document_id=env.rival_plan, _current_user=user
        ),
        _check_references,
    ),
    Case(
        "POST /kb/documents/{id}/references",
        lambda ws, user, env: add_document_reference_route(
            workspace_id=ws, document_id=env.rival_plan,
            body=KBAddReferenceBody(target_id=env.rival_notes, description="平台方新增"), _current_user=user,
        ),
        _check_reference_added,
    ),
    Case(
        "PUT /kb/documents/{id}/references/{target}",
        lambda ws, user, env: update_document_reference_route(
            workspace_id=ws, document_id=env.rival_plan, target_id=env.rival_appendix,
            body=KBUpdateReferenceBody(description="平台方改写"), _current_user=user,
        ),
        _check_reference_updated,
    ),
    Case(
        "DELETE /kb/documents/{id}/references/{target}",
        lambda ws, user, env: remove_document_reference_route(
            workspace_id=ws, document_id=env.rival_plan, target_id=env.rival_appendix, _current_user=user
        ),
        _check_reference_removed,
    ),
    Case(
        "GET /kb/documents/{id}/source-file",
        lambda ws, user, env: get_workspace_kb_source_file_info(
            workspace_id=ws, document_id=env.rival_plan, _current_user=user
        ),
        _check_source_file,
    ),
]

# 被拒时这几个桩必须一次都没被调到 —— 它们代表"端点已经开始干活了"。
#
# 后两个钉的是**顺序**，而顺序这件事此前一条测试都没有（复核时的变异测试实测：
# 把守卫挪到它们之后，17×2 条用例全绿）：
#
# - `ensure_collection`：`reindex` 那两条改前是"先建 Qdrant 集合再判归属"，
#   于是一次越权请求能先在外部服务上建出东西来。集合是全局一份、不是泄露，
#   但"能不能进这个函数"不该由外部服务的状态决定。
# - `get_document`：`_get_document_or_404` 必须**先**校验工作区再查文档。文档 ID 是
#   全局自增的，反过来写（按 id 取出来再比 `workspace_id`）行为上同样是 404，
#   却让"这个 ID 存在吗"从时序与查询次数上可分辨。这是记账的转发不是桩，
#   正向那 17 条照常查得到文档。
WORK_STARTED_STUB_KEYS = ("schedule", "cancel", "delete_files", "delete_points", "rebuild_workspace",
                          "search", "detect_refs", "sync_metadata", "ensure_collection", "get_document")


class TestWorkspaceKnowledgeBaseIsScoped:
    @pytest.mark.parametrize("case", KB_ENDPOINT_CASES, ids=lambda case: case.id)
    async def test_tenant_cannot_touch_a_foreign_workspace(self, env: Env, case: Case) -> None:
        """跨租户、平台方独占、以及压根不存在的工作区，三种都是 404

        文档 ID 是全局自增的，所以守卫必须先校验工作区再查文档；反过来
        （先查文档再比 workspace_id）会让"这个文档 ID 存在吗"从时序上可分辨。
        """
        for workspace_id in env.foreign_workspace_ids():
            with pytest.raises(NotFoundError):
                await case.call(workspace_id, acme_admin(), env)

        for key in WORK_STARTED_STUB_KEYS:
            assert env.calls[key] == [], f"{case.id} 在拒绝之前就已经开始干活了: {key}"
        assert await DBKBDocument.filter(workspace_id=env.rival_ws).count() == 3
        assert await DBKBDocumentReference.filter(workspace_id=env.rival_ws).count() == 1
        assert (await DBKBDocument.get(id=env.rival_plan)).title == "rival-ws plan"

    @pytest.mark.parametrize("case", KB_ENDPOINT_CASES, ids=lambda case: case.id)
    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_unrestricted_identity_succeeds_with_the_same_arguments(
        self, env: Env, case: Case, identity
    ) -> None:
        """正向对照：同一组实参换成不受限身份必须真的跑通

        没有这一条，上面那 17 条"租户拿到 404"可以被任何原因满足 ——
        端点自己坏了（桩没接上、文件不存在、参数名写错）同样会抛异常。
        """
        result = await case.call(env.rival_ws, identity(), env)
        await case.check(result, env)

    async def test_denial_is_indistinguishable_from_absence(self, env: Env) -> None:
        """越权与不存在的错误文案必须逐字相同，否则工作区 ID 可枚举"""
        messages: List[str] = []
        for workspace_id in (env.rival_ws, MISSING_WORKSPACE_ID):
            with pytest.raises(NotFoundError) as exc_info:
                await get_workspace_kb_tree(workspace_id=workspace_id, _current_user=acme_admin())
            messages.append(exc_info.value.get_message(SupportedLang.ZH_CN))
        assert messages == [f"工作区 {env.rival_ws}不存在", f"工作区 {MISSING_WORKSPACE_ID}不存在"]

    async def test_the_work_started_recorders_are_actually_wired(self, env: Env) -> None:
        """反向证明：上面那句"一次都没被调到"不是因为记账压根没接上

        `get_document` 与 `ensure_collection` 是两个记账点里唯一会在**正向**路径上
        被调到的，这里让它们各被调一次。没有这一条，把记账拆掉会让
        `test_tenant_cannot_touch_a_foreign_workspace` 里那 10 个断言一起变成 `[] == []`。
        """
        await get_workspace_kb_document(
            workspace_id=env.rival_ws, document_id=env.rival_plan, _current_user=platform_owner()
        )
        assert env.calls["get_document"] == [(env.rival_ws, env.rival_plan)]

        await reindex_workspace_kb(workspace_id=env.rival_ws, _current_user=platform_owner())
        assert env.calls["ensure_collection"] == [True]

    async def test_tenant_can_still_use_its_own_workspace(self, env: Env) -> None:
        listing = await list_workspace_kb_documents(workspace_id=env.acme_ws, _current_user=acme_admin())
        assert [item.title for item in listing.items] == [
            "acme-ws appendix", "acme-ws notes", "acme-ws plan",
        ]

    async def test_cross_workspace_reference_target_is_refused(self, env: Env) -> None:
        """引用的**目标**也要过守卫

        源文档在自己工作区、目标文档在别人工作区时，只校验源文档等于让租户
        把对方的文档挂进自己的引用图（并顺着 `get_document_references` 读到标题）。
        """
        acme_plan = await DBKBDocument.get(workspace_id=env.acme_ws, source_path="plan.md")
        with pytest.raises(NotFoundError):
            await add_document_reference_route(
                workspace_id=env.acme_ws,
                document_id=acme_plan.id,
                body=KBAddReferenceBody(target_id=env.rival_plan, description="偷渡"),
                _current_user=acme_admin(),
            )
        assert await DBKBDocumentReference.filter(workspace_id=env.acme_ws).count() == 1


class TestSearchItselfStaysInsideTheWorkspace:
    """守卫说"这个工作区是你的"之后，**取数据那一步**也得真的只取这个工作区

    上面 17 条用例把 `search_workspace_kb` 整个桩掉了（它们要测的是路由层的归属判定），
    于是检索**自己**那一跳一条测试都没有 —— 而它是全模块唯一一个直接吐**正文片段**的出口：
    过滤条件一旦漏掉，租户搜自己的工作区就会搜出别人的文档正文，而路由守卫全程无感
    （工作区确实是他自己的，越权发生在 SQL 里）。这与 §2.7 记的"零件全对、调用点没了"
    是同一种形态的另一半：这里零件（守卫）测得很足，被守住的那份数据反而没人看。

    走**关键词回退**那条路：`embed_kb_text` 抛异常时 `query_vector` 为 None，
    整条向量分支不进，于是不需要 Qdrant，判定是确定性的。

    **这两条断言的是端到端性质，不是逐个过滤条件**（复核时的变异实测，如实登记）：
    文档那一路有两层独立过滤（`DBKBDocument.filter(workspace_id=...)` 与
    `_collect_keyword_hits` 里的 `DBKBChunk.filter(workspace_id=...)`），
    只拆掉其中**一层**时另一层仍然兜得住、本组用例仍然全绿；两层一起拆才会红。
    素材那一路只有 `DBKBAssetBinding.filter(workspace_id=...)` 一层，单独拆掉即红。
    """

    DISTINCT_ASSET_TEXT = "只有 rival 那边挂着的素材正文"

    @pytest.fixture
    async def searchable(self, env: Env, monkeypatch) -> Any:
        """给三个工作区的 plan 文档各补一条 chunk，并把一条素材只绑到 rival 上

        素材正文用 monkeypatch 供给：真实实现读的是全局素材库目录（不在 `tmp_path` 下），
        测试不该往那儿落文件；而返回一个**取值互不相同**的串才能让"素材没被过滤掉"变红。
        """
        from nekro_agent.models.db_kb_asset_binding import DBKBAssetBinding
        from nekro_agent.models.db_kb_asset_chunk import DBKBAssetChunk
        from nekro_agent.models.db_kb_chunk import DBKBChunk
        from nekro_agent.services.kb import search_service

        async def _no_embedding(_text: str):
            raise RuntimeError("测试里不打 embedding，走关键词回退")

        monkeypatch.setattr(search_service, "embed_kb_text", _no_embedding)
        monkeypatch.setattr(
            search_service, "read_asset_normalized_content", lambda _asset: self.DISTINCT_ASSET_TEXT
        )

        for document in await DBKBDocument.all():
            text = f"{document.title} normalized"
            await DBKBChunk.create(
                workspace_id=document.workspace_id, document_id=document.id, chunk_index=0,
                char_start=0, char_end=len(text),
            )

        asset = await DBKBAsset.create(
            title="rival plan runbook", category="doc", file_name="r.md", file_ext=".md",
            mime_type="text/markdown", format="markdown", source_path="r.md",
            content_hash="rival-asset", file_size=1, extract_status="ready", sync_status="ready",
        )
        await DBKBAssetChunk.create(
            asset_id=asset.id, chunk_index=0, char_start=0, char_end=len(self.DISTINCT_ASSET_TEXT)
        )
        await DBKBAssetBinding.create(asset_id=asset.id, workspace_id=env.rival_ws)
        return asset

    async def _search(self, workspace_id: int) -> str:
        from nekro_agent.services.kb.search_service import search_workspace_kb

        response = await search_workspace_kb(workspace_id=workspace_id, query="plan", limit=10)
        return response.model_dump_json()

    async def test_own_workspace_search_never_surfaces_another_workspaces_text(
        self, env: Env, searchable  # noqa: ARG002
    ) -> None:
        body = await self._search(env.acme_ws)

        # 正向：自己的正文确实搜得到 —— 没有这条，下面两条 0 命中可以被"什么都没搜到"满足
        assert "acme-ws plan normalized" in body
        assert "rival-ws plan normalized" not in body
        assert "platform-ws plan normalized" not in body
        # 素材是**按绑定关系**进检索的，只挂在 rival 上的那条不该出现在 acme 的结果里
        assert self.DISTINCT_ASSET_TEXT not in body

    async def test_the_same_query_does_find_that_text_from_its_own_workspace(
        self, env: Env, searchable  # noqa: ARG002
    ) -> None:
        """反向对照：唯一的变量是 `workspace_id`

        没有这一条，上面那三条"不出现"可以是任何原因造成的（关键词没命中、
        chunk 建错了、素材被别的条件过滤掉了），与"作用域生效了"完全无法区分。
        """
        body = await self._search(env.rival_ws)

        assert "rival-ws plan normalized" in body
        assert self.DISTINCT_ASSET_TEXT in body
        assert "acme-ws plan normalized" not in body


class TestEveryLiveKnowledgeBaseEndpointHasACase:
    """用例表与真实路由表一一对应

    没有这一条，"漏测一个端点"与"这个端点不存在"在测试里长得一模一样 ——
    而 #26/#29 两轮的形态正是"一族端点里漏了几个"。
    """

    def test_case_table_covers_the_router(self) -> None:
        import inspect

        from nekro_agent.routers import workspace_kb

        live = {
            f"{sorted(route.methods)[0]} {route.path.replace('/workspaces/{workspace_id}', '')}"
            for route in workspace_kb.router.routes
            if getattr(route, "endpoint", None) is not None
        }
        covered = {
            case.id.replace("{id}", "{document_id}").replace("{target}", "{target_id}")
            for case in KB_ENDPOINT_CASES
        }
        assert covered == live
        assert len(KB_ENDPOINT_CASES) == 17
        assert inspect.getmodule(workspace_kb).__name__ == "nekro_agent.routers.workspace_kb"


# ---------------------------------------------------------------------------
# 工作区资源：3 条租户作用域 + 2 条平台方专属
# ---------------------------------------------------------------------------


@pytest.fixture
async def bound_resources(env: Env) -> Any:
    """两条资源各绑到 rival-ws 与 acme-ws 上

    绑两条而不是一条，是为了让"重排"有真实差异可断言：只有一条时任何实现都能通过。
    """
    resources = []
    for index, name in enumerate(("prod ssh", "prod db")):
        resource = await DBWorkspaceResource.create(
            resource_key=f"res-{index}", name=name, template_key="ssh_key",
            schema_json=[], public_payload={}, secret_payload_encrypted="",
        )
        resources.append(resource)
        for workspace_id in (env.rival_ws, env.acme_ws):
            await DBWorkspaceResourceBinding.create(
                workspace_id=workspace_id, resource_id=resource.id, sort_order=(index + 1) * 10,
            )
    return resources


async def _binding_order(workspace_id: int) -> List[int]:
    return [
        row.resource_id
        for row in await DBWorkspaceResourceBinding.filter(workspace_id=workspace_id).order_by("sort_order", "id")
    ]


class TestWorkspaceResourceBindingsAreScoped:
    async def test_tenant_cannot_list_foreign_workspace_resources(self, env: Env, bound_resources) -> None:
        for workspace_id in env.foreign_workspace_ids():
            with pytest.raises(NotFoundError):
                await list_workspace_resources(workspace_id=workspace_id, _current_user=acme_admin())

    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_unrestricted_identity_lists_the_same_workspace(self, env: Env, bound_resources, identity) -> None:
        view = await list_workspace_resources(workspace_id=env.rival_ws, _current_user=identity())
        assert [item.resource.name for item in view.items] == ["prod ssh", "prod db"]

    async def test_tenant_cannot_unbind_from_a_foreign_workspace(self, env: Env, bound_resources) -> None:
        for workspace_id in env.foreign_workspace_ids():
            with pytest.raises(NotFoundError):
                await unbind_workspace_resource(
                    workspace_id=workspace_id, resource_id=bound_resources[0].id, _current_user=acme_admin()
                )
        assert await _binding_order(env.rival_ws) == [bound_resources[0].id, bound_resources[1].id]

    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_unrestricted_identity_unbinds_the_same_workspace(self, env: Env, bound_resources, identity) -> None:
        await unbind_workspace_resource(
            workspace_id=env.rival_ws, resource_id=bound_resources[0].id, _current_user=identity()
        )
        assert await _binding_order(env.rival_ws) == [bound_resources[1].id]
        # 自己那一头分毫未动
        assert await _binding_order(env.acme_ws) == [bound_resources[0].id, bound_resources[1].id]

    async def test_tenant_cannot_reorder_a_foreign_workspace(self, env: Env, bound_resources) -> None:
        rows = await DBWorkspaceResourceBinding.filter(workspace_id=env.rival_ws).order_by("sort_order")
        reversed_body = WorkspaceResourceReorderBody(
            items=[
                WorkspaceResourceReorderItem(binding_id=rows[1].id, sort_order=1),
                WorkspaceResourceReorderItem(binding_id=rows[0].id, sort_order=2),
            ]
        )
        for workspace_id in env.foreign_workspace_ids():
            with pytest.raises(NotFoundError):
                await reorder_workspace_resources(
                    workspace_id=workspace_id, body=reversed_body, _current_user=acme_admin()
                )
        assert await _binding_order(env.rival_ws) == [bound_resources[0].id, bound_resources[1].id]

    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_unrestricted_identity_reorders_the_same_workspace(
        self, env: Env, bound_resources, identity
    ) -> None:
        rows = await DBWorkspaceResourceBinding.filter(workspace_id=env.rival_ws).order_by("sort_order")
        await reorder_workspace_resources(
            workspace_id=env.rival_ws,
            body=WorkspaceResourceReorderBody(
                items=[
                    WorkspaceResourceReorderItem(binding_id=rows[1].id, sort_order=1),
                    WorkspaceResourceReorderItem(binding_id=rows[0].id, sort_order=2),
                ]
            ),
            _current_user=identity(),
        )
        assert await _binding_order(env.rival_ws) == [bound_resources[1].id, bound_resources[0].id]

    async def test_own_workspace_hides_other_workspaces_of_the_same_resource(
        self, env: Env, bound_resources
    ) -> None:
        """同一条资源同时绑在自己和别人的工作区上时，只能看到"绑在我这儿"这一个事实

        `WorkspaceResourceSummary.bound_workspaces` 会连别的工作区的名字一起带出来。
        """
        tenant_view = await list_workspace_resources(workspace_id=env.acme_ws, _current_user=acme_admin())
        assert [item.resource.bound_workspaces for item in tenant_view.items] == [[], []]
        assert [item.resource.bound_workspace_count for item in tenant_view.items] == [0, 0]

        owner_view = await list_workspace_resources(workspace_id=env.acme_ws, _current_user=platform_owner())
        # 资源侧的绑定用的是 `BoundWorkspaceInfo`（字段名 `name`），
        # 与素材库侧的 `KBAssetBoundWorkspace`（`workspace_name`）不是同一个模型
        assert sorted(
            workspace.name for workspace in owner_view.items[0].resource.bound_workspaces
        ) == ["acme-ws", "rival-ws"]
        assert owner_view.items[0].resource.bound_workspace_count == 2


class TestBindingIsPlatformOnly:
    """把资源绑上工作区 = 把平台方的生产 SSH 私钥发给那个工作区里的 Claude Code

    这是**能力边界**而不是存在性问题，所以用 403 而不是 404：
    租户在任何工作区上（包括自己的）都不该有这个动作。
    """

    async def test_tenant_cannot_bind_even_its_own_workspace(self, env: Env, bound_resources) -> None:
        await unbind_workspace_resource(
            workspace_id=env.acme_ws, resource_id=bound_resources[0].id, _current_user=platform_owner()
        )
        with pytest.raises(HTTPException) as exc_info:
            await bind_workspace_resource(
                workspace_id=env.acme_ws, resource_id=bound_resources[0].id,
                body=WorkspaceResourceBindBody(note=""), _current_user=acme_admin(),
            )
        assert exc_info.value.status_code == 403
        assert await _binding_order(env.acme_ws) == [bound_resources[1].id]

    async def test_platform_owner_binds_the_same_workspace(self, env: Env, bound_resources) -> None:
        await unbind_workspace_resource(
            workspace_id=env.acme_ws, resource_id=bound_resources[0].id, _current_user=platform_owner()
        )
        await bind_workspace_resource(
            workspace_id=env.acme_ws, resource_id=bound_resources[0].id,
            body=WorkspaceResourceBindBody(note="平台方绑定"), _current_user=platform_owner(),
        )
        assert await _binding_order(env.acme_ws) == [bound_resources[1].id, bound_resources[0].id]

    async def test_tenant_cannot_check_bind_conflicts(self, env: Env, bound_resources) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await check_workspace_resource_bind(
                workspace_id=env.acme_ws,
                body=WorkspaceResourceCheckBindBody(resource_id=bound_resources[0].id),
                _current_user=acme_admin(),
            )
        assert exc_info.value.status_code == 403

    async def test_platform_owner_checks_bind_conflicts(self, env: Env, bound_resources) -> None:
        result = await check_workspace_resource_bind(
            workspace_id=env.acme_ws,
            body=WorkspaceResourceCheckBindBody(resource_id=bound_resources[0].id),
            _current_user=platform_owner(),
        )
        assert result.ok is True
        assert result.conflicts == []


# ---------------------------------------------------------------------------
# 全局素材库的绑定（`routers/kb_library.py`，与上面两族同一条 `workspace_id` 通道）
# ---------------------------------------------------------------------------


class TestKnowledgeBaseLibraryBindings:
    @pytest.fixture
    async def asset(self, env: Env):
        return await DBKBAsset.create(
            title="platform runbook", category="doc", file_name="a.md", file_ext=".md",
            mime_type="text/markdown", format="markdown", source_path="a.md",
            content_hash="asset", file_size=1,
        )

    async def test_cannot_bind_asset_to_foreign_workspace(self, env: Env, asset) -> None:
        """把素材塞进别人的工作区 = 把内容投喂进别人的 Claude Code"""
        for workspace_id in env.foreign_workspace_ids():
            with pytest.raises(NotFoundError):
                await bind_kb_library_asset_workspace(
                    asset_id=asset.id, workspace_id=workspace_id, _current_user=acme_admin(),
                )
        assert await DBKBAsset.filter(id=asset.id).exists()

    async def test_cannot_unbind_asset_from_foreign_workspace(self, env: Env, asset) -> None:
        await bind_kb_library_asset_workspace(
            asset_id=asset.id, workspace_id=env.rival_ws, _current_user=platform_owner(),
        )
        with pytest.raises(NotFoundError):
            await unbind_kb_library_asset_workspace(
                asset_id=asset.id, workspace_id=env.rival_ws, _current_user=acme_admin(),
            )
        remaining = await get_kb_library_asset_bindings(asset_id=asset.id, _current_user=platform_owner())
        assert [item.workspace_id for item in remaining.items] == [env.rival_ws]

    async def test_bind_and_unbind_own_workspace(self, env: Env, asset) -> None:
        bound = await bind_kb_library_asset_workspace(
            asset_id=asset.id, workspace_id=env.acme_ws, _current_user=acme_admin(),
        )
        assert [item.workspace_id for item in bound.items] == [env.acme_ws]

        unbound = await unbind_kb_library_asset_workspace(
            asset_id=asset.id, workspace_id=env.acme_ws, _current_user=acme_admin(),
        )
        assert unbound.items == []

    async def test_bindings_of_other_tenants_are_hidden(self, env: Env, asset) -> None:
        """一条素材同时挂在两个租户上时，各自只看得到自己那一头"""
        for workspace_id in (env.acme_ws, env.rival_ws, env.platform_ws):
            await bind_kb_library_asset_workspace(
                asset_id=asset.id, workspace_id=workspace_id, _current_user=platform_owner(),
            )

        tenant_view = await get_kb_library_asset_bindings(asset_id=asset.id, _current_user=acme_admin())
        assert [item.workspace_name for item in tenant_view.items] == ["acme-ws"]
        assert tenant_view.binding_count == 1

        owner_view = await get_kb_library_asset_bindings(asset_id=asset.id, _current_user=platform_owner())
        assert sorted(item.workspace_name for item in owner_view.items) == [
            "acme-ws", "platform-ws", "rival-ws",
        ]
        assert owner_view.binding_count == 3
