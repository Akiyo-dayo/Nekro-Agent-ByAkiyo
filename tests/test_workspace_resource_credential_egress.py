"""工作区资源的凭据出口

## 这条测试要防的是什么

`DBWorkspaceResource` 装的是 SSH 私钥、root 口令、数据库密码、访问令牌
（`services/resources/registry.py` 里 `secret=True` 的那些）。它们**只该有一个出口**：
`resolve_workspace_resources_to_env()` 把它们注入容器环境变量。

历史上 `GET /resources/{id}` 把这些字段的明文原样放进响应体。
上一轮把整族提到了 `Role.Super`，那只挡住"谁能发这个请求"——真正把私钥搬出服务端的是
**响应体本身**：它会进浏览器缓存、devtools、抓包与工单截图（同类事故：
一条 DEBUG 日志就把 `ADMIN_PASSWORD` 写进了日志文件）。所以现在还有第二层：
`secret=True` 的取值一律换成 `CREDENTIAL_MASK`，写回时该哨兵表示"保持不变"。

两层各自独立，这里钉的是第二层。角色门槛那一层由
`tests/test_route_permission_contract.py` 的结构断言与
`tests/test_kb_and_resource_tenant_scope.py` 的行为用例钉住。

## 断言的形状：每条都要有能分辨真假的对照

"掩码"这种断言极容易写成空的——掩掉一个本来就是空串的字段、或者比较两份恰好相同的
清单，测试全绿而实现是错的。所以这份 fixture 里三种字段**取值互不相同**：

| 字段 | secret | 库里的值 | 详情里应当是 |
| --- | --- | --- | --- |
| `private_key` | 是 | 一整段私钥 | `********` |
| `passphrase` | 是 | 空串 | 空串（**不是**掩码） |
| `host` | 否 | `10.0.0.7` | `10.0.0.7`（**原文**） |

于是"全掩成星号"和"一个都没掩"两种错法都会红。响应体全文 grep 那一条同理：
先证明这个 grep 在同一份响应体里找得到 `host` 的原文，再断言私钥 0 命中。
"""

import datetime
from typing import Any, Callable, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tortoise import Tortoise

from nekro_agent.core.exception_handlers import register_exception_handlers
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_workspace_resource import DBWorkspaceResource
from nekro_agent.models.db_workspace_resource_binding import DBWorkspaceResourceBinding
from nekro_agent.routers import resources as resources_router
from nekro_agent.schemas.tenant import CREDENTIAL_MASK, TenantRole
from nekro_agent.schemas.workspace_resource import (
    ResourceField,
    WorkspaceResourceCreate,
    WorkspaceResourceDetail,
    WorkspaceResourceUpdate,
)
from nekro_agent.services.resources import workspace_resource_service
from nekro_agent.services.resources.crypto import decrypt_secret_payload
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.role import Role

# 逐字的"真实凭据"。全文 grep 用的就是这几个串，所以它们不能出现在任何别的位置。
REAL_PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----nekro-fork-test-key-----END-----"
REAL_ROOT_PASSWORD = "r00t-pass-must-not-leak"
PUBLIC_HOST = "10.0.0.7"
RESOURCE_NAME = "prod ssh"


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


def _fields(*, private_key: str, root_password: str, passphrase: str = "", host: str = PUBLIC_HOST) -> List[ResourceField]:
    return [
        ResourceField(field_key="host", label="主机", secret=False, value_kind="host", order=10, value=host),
        ResourceField(
            field_key="private_key", label="私钥", secret=True, value_kind="private_key", order=20, value=private_key
        ),
        ResourceField(
            field_key="root_password", label="Root 密码", secret=True, value_kind="password", order=30,
            value=root_password,
        ),
        ResourceField(
            field_key="passphrase", label="口令短语", secret=True, value_kind="password", order=40, value=passphrase
        ),
    ]


def _create_body(**kwargs: Any) -> WorkspaceResourceCreate:
    return WorkspaceResourceCreate(
        name=RESOURCE_NAME,
        template_key="ssh_key",
        resource_note="生产跳板机",
        resource_tags=["prod"],
        resource_prompt="用它连生产",
        fields=_fields(**kwargs),
        enabled=True,
    )


def _submit_back(
    detail: WorkspaceResourceDetail, mutate: Optional[Callable[[List[ResourceField]], None]] = None
) -> WorkspaceResourceUpdate:
    """把读接口拿到的详情原样提交回来 —— 前端 `ResourcesTab` 就是这么干的

    这正是哨兵要处理的场景：表单里那几个 `secret` 框显示的是掩码，
    用户只改了别的字段就点保存，整份表单（含掩码）一起 PATCH 回来。
    """
    fields = [ResourceField.model_validate(field.model_dump()) for field in detail.fields]
    if mutate is not None:
        mutate(fields)
    return WorkspaceResourceUpdate(
        name=detail.name,
        template_key=detail.template_key,
        resource_note=detail.resource_note,
        resource_tags=list(detail.resource_tags),
        resource_prompt=detail.resource_prompt,
        fields=fields,
        enabled=detail.enabled,
    )


async def _stored_secrets(resource_id: int) -> dict:
    """库里的真值 —— 掩码之后这是唯一的事实来源，断言一律以它为准"""
    row = await DBWorkspaceResource.get(id=resource_id)
    return decrypt_secret_payload(row.secret_payload_encrypted)


def _field_values(detail: WorkspaceResourceDetail) -> dict:
    return {field.field_key: field.value for field in detail.fields}


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
async def resource(db) -> Any:  # noqa: ARG001
    """走真实的 `create_resource` 建出来，而不是直接塞一行

    直接塞行会绕过 `_split_payload`，测出来的"库里存了什么"就不是产品路径的结论。
    """
    detail = await workspace_resource_service.create_resource(
        _create_body(private_key=REAL_PRIVATE_KEY, root_password=REAL_ROOT_PASSWORD)
    )
    return detail


@pytest.fixture
async def bound_workspace(resource) -> Any:
    """把资源绑到一个工作区上，好看到"注入容器"与"提示词"这两条下游出口"""
    workspace = await DBWorkspace.create(name="cc-ws", description="cc-ws")
    await DBWorkspaceResourceBinding.create(
        workspace_id=workspace.id, resource_id=resource.id, enabled=True, sort_order=10
    )
    return workspace


class TestSecretValuesNeverLeaveTheServer:
    async def test_detail_masks_secret_values_and_keeps_the_rest(self, resource) -> None:
        detail = await workspace_resource_service.get_resource_detail(resource.id)
        values = _field_values(detail)

        assert values["private_key"] == CREDENTIAL_MASK
        assert values["root_password"] == CREDENTIAL_MASK
        # 空的 secret 字段保持空串：前端靠这个差别显示"未填写"，
        # 而且这条同时证明掩码不是"把 secret 字段一律写成星号"
        assert values["passphrase"] == ""
        # 非 secret 字段原文照旧：证明掩码没有变成一把糊在所有字段上的刷子
        assert values["host"] == PUBLIC_HOST

    async def test_the_real_values_are_still_in_the_database(self, resource) -> None:
        """掩码只作用于出口，库里必须仍是明文，否则容器注入那条路会一起断掉"""
        assert await _stored_secrets(resource.id) == {
            "private_key": REAL_PRIVATE_KEY,
            "root_password": REAL_ROOT_PASSWORD,
        }

    @pytest.mark.parametrize(
        "build",
        [
            pytest.param(
                lambda service, resource: service.get_resource_detail(resource.id), id="detail"
            ),
            pytest.param(
                lambda service, resource: service.create_resource(
                    _create_body(private_key=REAL_PRIVATE_KEY, root_password=REAL_ROOT_PASSWORD)
                ),
                id="create",
            ),
            pytest.param(
                lambda service, resource: service.update_resource(
                    resource.id,
                    WorkspaceResourceUpdate(
                        name=RESOURCE_NAME, template_key="ssh_key", resource_note="", resource_tags=[],
                        resource_prompt="",
                        fields=_fields(private_key=REAL_PRIVATE_KEY, root_password=REAL_ROOT_PASSWORD),
                        enabled=True,
                    ),
                ),
                id="update",
            ),
        ],
    )
    async def test_every_exit_that_returns_a_detail_is_masked(self, resource, build) -> None:
        """详情/创建/更新三条端点共用 `WorkspaceResourceDetail`

        只掩住 `GET` 那一条等于没掩：创建与更新的响应里同样带着刚提交的明文，
        而"保存之后页面刷新一下"就走这条路。
        """
        detail = await build(workspace_resource_service, resource)
        values = _field_values(detail)
        assert values["private_key"] == CREDENTIAL_MASK
        assert values["root_password"] == CREDENTIAL_MASK

    async def test_summary_list_carries_no_field_values_at_all(self, resource) -> None:
        items = await workspace_resource_service.list_resources()
        dumped = "".join(item.model_dump_json() for item in items)

        assert [item.name for item in items] == [RESOURCE_NAME]
        assert items[0].field_count == 4
        assert REAL_PRIVATE_KEY not in dumped
        assert REAL_ROOT_PASSWORD not in dumped


class TestRealResponseBodyHasZeroHits:
    """走一遍真实 HTTP 栈，在**整个响应体文本**里 grep

    直接 `await` 路由函数会跳过 FastAPI 的响应模型序列化，也验不到 `@require_role`
    ——那个装饰器只在被包函数的参数名叫 `_current_user` 时生效，名字不对就是 500。
    """

    @pytest.fixture
    def client(self) -> Any:
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(resources_router.router, prefix="/api")
        app.dependency_overrides[get_current_active_user] = platform_owner
        return TestClient(app, raise_server_exceptions=False)

    async def test_get_detail_returns_200_without_a_single_credential_byte(self, resource, client) -> None:
        response = client.get(f"/api/resources/{resource.id}")

        assert response.status_code == 200, response.text
        body = response.text
        # 先证明这个 grep 在同一份响应体里确实找得到东西，否则下面两条 0 命中毫无意义
        assert body.count(PUBLIC_HOST) == 1
        assert body.count(REAL_PRIVATE_KEY) == 0
        assert body.count(REAL_ROOT_PASSWORD) == 0
        assert body.count(CREDENTIAL_MASK) == 2

    async def test_list_returns_200_without_a_single_credential_byte(self, resource, client) -> None:
        response = client.get("/api/resources")

        assert response.status_code == 200, response.text
        assert RESOURCE_NAME in response.text
        assert response.text.count(REAL_PRIVATE_KEY) == 0
        assert response.text.count(REAL_ROOT_PASSWORD) == 0


class TestTheMaskSentinelMeansKeepUnchanged:
    async def test_submitting_the_form_back_unchanged_keeps_the_secrets(self, resource) -> None:
        """不认哨兵的话，一次无意的保存就把私钥覆盖成一串星号

        而且这种损坏**查不出来**：`/resources/secret-health` 眼里 `********`
        是一份格式完全合法的凭据。
        """
        detail = await workspace_resource_service.get_resource_detail(resource.id)

        def rename_host(fields: List[ResourceField]) -> None:
            for field in fields:
                if field.field_key == "host":
                    field.value = "10.0.0.8"

        updated = await workspace_resource_service.update_resource(resource.id, _submit_back(detail, rename_host))

        assert await _stored_secrets(resource.id) == {
            "private_key": REAL_PRIVATE_KEY,
            "root_password": REAL_ROOT_PASSWORD,
        }
        # 同一次请求里非 secret 字段确实改掉了 —— 证明这次 PATCH 真的落了地，
        # 而不是整个请求被吞掉之后"凭据当然没变"
        assert _field_values(updated)["host"] == "10.0.0.8"

    async def test_a_real_new_value_still_overwrites(self, resource) -> None:
        detail = await workspace_resource_service.get_resource_detail(resource.id)

        def rotate(fields: List[ResourceField]) -> None:
            for field in fields:
                if field.field_key == "private_key":
                    field.value = "rotated-private-key"

        await workspace_resource_service.update_resource(resource.id, _submit_back(detail, rotate))

        assert await _stored_secrets(resource.id) == {
            "private_key": "rotated-private-key",
            "root_password": REAL_ROOT_PASSWORD,
        }

    async def test_an_empty_value_still_clears(self, resource) -> None:
        """清空要真的能清掉，否则哨兵会把"删掉这份凭据"变成做不到的操作"""
        detail = await workspace_resource_service.get_resource_detail(resource.id)

        def clear(fields: List[ResourceField]) -> None:
            for field in fields:
                if field.field_key == "root_password":
                    field.value = ""

        await workspace_resource_service.update_resource(resource.id, _submit_back(detail, clear))

        assert await _stored_secrets(resource.id) == {"private_key": REAL_PRIVATE_KEY}

    async def test_a_sentinel_with_nothing_to_keep_is_cleared_not_stored(self, resource) -> None:
        """库里没有对应现值时，哨兵按清空处理

        三种取值里只有"清空"不会凭空造出一份假凭据：字面量 `********` 会被注入容器，
        表现是一次谁也看不懂的鉴权失败，而 `secret-health` 报"一切正常"。
        """
        detail = await workspace_resource_service.get_resource_detail(resource.id)

        def mask_the_empty_one(fields: List[ResourceField]) -> None:
            for field in fields:
                if field.field_key == "passphrase":
                    field.value = CREDENTIAL_MASK

        await workspace_resource_service.update_resource(resource.id, _submit_back(detail, mask_the_empty_one))

        stored = await _stored_secrets(resource.id)
        assert "passphrase" not in stored
        assert CREDENTIAL_MASK not in stored.values()

    async def test_creating_with_the_sentinel_stores_nothing(self, db) -> None:  # noqa: ARG002
        """新建时没有"现值"可保持，哨兵一律落成空"""
        created = await workspace_resource_service.create_resource(
            _create_body(private_key=CREDENTIAL_MASK, root_password=REAL_ROOT_PASSWORD)
        )

        stored = await _stored_secrets(created.id)
        assert stored == {"root_password": REAL_ROOT_PASSWORD}
        assert _field_values(created)["private_key"] == ""

    async def test_turning_the_sensitive_switch_off_does_not_destroy_the_credential(self, resource) -> None:
        """把编辑框里的"敏感信息"开关关掉再保存，不能把私钥换成一串字面量星号

        `ResourcesTab.tsx` 里 `secret` 是一个可以随手拨的 `Switch`，而读接口给的掩码
        只跟**库里**的 secret 标志有关。哨兵判据一旦看提交上来的 `field.secret`，
        这一步就是：真私钥被丢弃 -> `********` 落进 `public_payload` -> 注入容器、
        印进 CLAUDE.md、`secret-health` 报"一切正常"、详情页看上去还是那 8 个圆点。
        **一次完全没有信号的凭据损毁**，而且造出来的正是掩码机制本来要防的假凭据。

        正确行为 = 掩码之前那一步：把明文挪进 `public_payload`（这正是关掉开关的语义，
        见 `detail.resources.editor.sensitiveHint`）。
        """
        detail = await workspace_resource_service.get_resource_detail(resource.id)
        assert _field_values(detail)["private_key"] == CREDENTIAL_MASK, "前提：读接口给的就是掩码"

        def declassify_the_private_key(fields: List[ResourceField]) -> None:
            for field in fields:
                if field.field_key == "private_key":
                    field.secret = False

        updated = await workspace_resource_service.update_resource(
            resource.id, _submit_back(detail, declassify_the_private_key)
        )

        row = await DBWorkspaceResource.get(id=resource.id)
        assert row.public_payload["private_key"] == REAL_PRIVATE_KEY
        assert CREDENTIAL_MASK not in row.public_payload.values()
        # 另一把还开着的私钥分毫未动，证明这次更新只搬了被拨开关的那一项
        assert await _stored_secrets(resource.id) == {"root_password": REAL_ROOT_PASSWORD}
        # 现在它是公开字段了，详情里就该是原文——否则用户看到的仍是 8 个圆点，
        # 分辨不出"还被掩着"与"已经被换成一串星号存进去了"
        assert _field_values(updated)["private_key"] == REAL_PRIVATE_KEY

    async def test_turning_the_switch_off_keeps_the_value_reaching_the_container(
        self, resource, bound_workspace
    ) -> None:
        """损毁的下游后果各有一条：容器拿到的必须还是真值，提示词里不能出现假凭据

        没有这两条，上一条只证明了"库里那一行是对的"——而这个 bug 真正难看的地方是
        CC 在容器里拿着一份 `********` 去连生产机，报出来的是一句谁也看不懂的鉴权失败。
        """
        detail = await workspace_resource_service.get_resource_detail(resource.id)

        def declassify_the_private_key(fields: List[ResourceField]) -> None:
            for field in fields:
                if field.field_key == "private_key":
                    field.secret = False

        await workspace_resource_service.update_resource(
            resource.id, _submit_back(detail, declassify_the_private_key)
        )

        env_vars = await workspace_resource_service.resolve_workspace_resources_to_env(bound_workspace.id)
        assert env_vars["NEKRO_RESOURCE_PROD_SSH_PRIVATE_KEY"] == REAL_PRIVATE_KEY
        assert CREDENTIAL_MASK not in env_vars.values()

        prompt_text, _ = await workspace_resource_service._build_workspace_prompt_section(  # noqa: SLF001
            bound_workspace.id
        )
        assert CREDENTIAL_MASK not in prompt_text

    async def test_the_sentinel_is_only_a_sentinel_for_secret_fields(self, resource) -> None:
        """非 secret 字段的值从不被掩码，所以那儿的 `********` 就是用户真的想存的字符串

        与上面两条的差别是**库里**那一项从来就不敏感（`host` 不在 secret 载荷里），
        所以哨兵在这里无处可还原，只能按字面量存下去。两组一起把判据钉在
        "库里这一项是不是被掩过"上，而不是"提交上来的 `secret` 是真是假"。
        """
        detail = await workspace_resource_service.get_resource_detail(resource.id)

        def type_stars_into_host(fields: List[ResourceField]) -> None:
            for field in fields:
                if field.field_key == "host":
                    field.value = CREDENTIAL_MASK

        updated = await workspace_resource_service.update_resource(
            resource.id, _submit_back(detail, type_stars_into_host)
        )

        assert (await DBWorkspaceResource.get(id=resource.id)).public_payload["host"] == CREDENTIAL_MASK
        assert _field_values(updated)["host"] == CREDENTIAL_MASK

    async def test_an_unreadable_payload_does_not_500_and_does_not_fabricate(self, resource) -> None:
        """坏载荷（`secret-health` 报得出来的那种）不能让详情与更新一起 500

        `decode_secret_payload` 里那句 `json.loads` 在 try 之外，抛的是
        `json.JSONDecodeError` 而不是 `SecretPayloadDecodeError` —— 只捕后者的话，
        用户连"去 secret-health 页面修它"这一步都走不到。
        """
        row = await DBWorkspaceResource.get(id=resource.id)
        row.secret_payload_encrypted = "这不是 JSON"
        await row.save(update_fields=["secret_payload_encrypted"])

        detail = await workspace_resource_service.get_resource_detail(resource.id)
        assert _field_values(detail)["private_key"] == ""
        assert _field_values(detail)["host"] == PUBLIC_HOST

        def keep_everything(fields: List[ResourceField]) -> None:
            for field in fields:
                if field.secret:
                    field.value = CREDENTIAL_MASK

        await workspace_resource_service.update_resource(resource.id, _submit_back(detail, keep_everything))
        assert await _stored_secrets(resource.id) == {}


class TestTheOnlyLegitimateExitStillWorks:
    """掩码不能把**容器注入**那条路一起掩掉

    没有这一条，"把所有 secret 都换成星号"是能让上面全部用例通过的 —— 代价是
    每个绑了资源的 CC 工作区都拿到一份 `********` 当私钥，而且没有任何报错。
    """

    async def test_env_injection_still_gets_the_plaintext(self, resource, bound_workspace) -> None:  # noqa: ARG002
        env_vars = await workspace_resource_service.resolve_workspace_resources_to_env(bound_workspace.id)

        assert env_vars["NEKRO_RESOURCE_PROD_SSH_PRIVATE_KEY"] == REAL_PRIVATE_KEY
        assert env_vars["NEKRO_RESOURCE_PROD_SSH_ROOT_PASSWORD"] == REAL_ROOT_PASSWORD
        assert env_vars["NEKRO_RESOURCE_PROD_SSH_HOST"] == PUBLIC_HOST
        assert CREDENTIAL_MASK not in env_vars.values()

    async def test_the_claude_md_prompt_names_the_variable_without_the_value(
        self, resource, bound_workspace  # noqa: ARG002
    ) -> None:
        """提示词是 `_build_detail` 的第二个消费者，掩码之后它必须仍然可用

        它对 secret 字段本来就只写"已注入环境变量 X"，所以掩码在这里是零影响；
        钉住是因为这条依赖关系在代码里看不出来。
        """
        prompt_text, summary_text = await workspace_resource_service._build_workspace_prompt_section(  # noqa: SLF001
            bound_workspace.id
        )

        assert "NEKRO_RESOURCE_PROD_SSH_PRIVATE_KEY" in prompt_text
        assert PUBLIC_HOST in prompt_text
        assert REAL_PRIVATE_KEY not in prompt_text
        assert REAL_ROOT_PASSWORD not in prompt_text
        assert CREDENTIAL_MASK not in prompt_text
        assert RESOURCE_NAME in summary_text
