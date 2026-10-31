"""新建模型组的可见性默认值 = private（上线阻塞 #2 / 需求 R19）

## 这里钉的是什么

改造前 `DBModelGroup.visibility` 的字段默认是 `shared`，接口层还自带一句
`body.visibility or ModelGroupVisibility.SHARED`。后果不是越权，而是**授权**：
新建的每一个平台模型组开箱即对**所有租户**开放 —— 新开一个租户就能立刻烧平台方的账单。
#51 堵掉的是"租户把 `USE_MODEL_GROUP` 指向 private 组"那条越权路径，
它管不到"这个组本来就是 shared"。

## 四类断言

1. **默认值只有一份**（`TestDefaultIsSingleSourced`）：字段默认、`create_group()` 形参默认、
   `import_config_group()`、`upsert_platform_group()` 全部引用
   `models/db_model_group.DEFAULT_VISIBILITY`；任何一处自己写字面量都会被这组用例逮到。
2. **真落库是 private**（`TestNewGroupsLandPrivate`）：断言的是**原始列**的取值，
   不是 Python 对象上的属性 —— 那两者可以不一致（字段默认没写进 DDL 时就会）。
3. **存量不被动**（`TestExistingSharedGroupsAreNotTouched`）：用户已有的共享意图不能被
   一次升级偷偷改掉。启动导入、旧版覆盖端点都必须绕开已存在的行。
4. **前后端默认钉在一起**（`TestFrontendDefaultMatchesBackend`）：踩过一次
   —— `ENABLE_VISION` 前端默认 true、后端默认 false，运维一建组就落了个错的值（#32）。
   这一条直接读 `ModelGroupFormDialog.tsx`，两侧不一致即红。

每条断言都带正向对照：**显式提交 `shared` 仍然要能建出 shared 组**。
只断言"默认是 private"的话，一个把 visibility 写死成 private 的实现同样能通过，
而那会让"开放给某个租户"这个功能整个消失。
"""

import re
from pathlib import Path
from typing import List, Optional

import pytest
from tortoise import Tortoise, connections

from nekro_agent.core.config import ModelConfigGroup
from nekro_agent.models.db_model_group import (
    DEFAULT_VISIBILITY,
    DBModelGroup,
    ModelGroupVisibility,
)
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.console.model_groups import (
    ModelGroupCreateRequest,
    create_console_model_group,
)
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services import model_group_service
from nekro_agent.services.model_group_service import (
    create_group,
    resolve_console_identity,
    upsert_platform_group,
)
from nekro_agent.services.user.role import Role

ACME_TENANT_ID = 1

FRONTEND_FORM = (
    Path(__file__).resolve().parents[1]
    / "frontend"
    / "src"
    / "components"
    / "model-group"
    / "ModelGroupFormDialog.tsx"
)

# 前端那份默认值的声明。写成一条正则而不是子串匹配：要读出**它等于什么**，
# 而不是"文件里出现过 private 这个词"（§2.23：判"代码里有没有某个东西"不要用子串）
FRONTEND_DEFAULT_RE = re.compile(
    r"const\s+DEFAULT_VISIBILITY\s*:\s*ModelGroupVisibility\s*=\s*'(?P<value>[a-z]+)'",
)


def _make_user(*, user_id: int, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    user = DBUser(
        username=f"user-{tenant_role.value}-{tenant_id}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm_level,
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )
    user.id = user_id
    return user


def platform_owner() -> DBUser:
    return _make_user(user_id=1, tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


def acme_admin() -> DBUser:
    return _make_user(user_id=11, tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


async def _stored_visibility(name: str) -> str:
    """读原始列。ORM 对象上的属性来自 Python 侧默认，与库里实际写进去的值不是一回事"""
    conn = connections.get("default")
    rows = await conn.execute_query_dict(
        f"SELECT \"visibility\" FROM \"model_group\" WHERE \"name\" = '{name}'",
    )
    assert len(rows) == 1, f"期望恰好一行 name={name}，实际 {len(rows)} 行"
    return rows[0]["visibility"]


def _sample_config(model: str = "gpt-4o") -> ModelConfigGroup:
    return ModelConfigGroup(
        CHAT_MODEL=model,
        BASE_URL="https://sentinel-upstream.example/v1",
        API_KEY="sk-visibility-SENTINEL-2f8c",
    )


@pytest.fixture
async def db(monkeypatch: pytest.MonkeyPatch):
    """一张空的 `model_group` 表 + 不落盘的配置

    `refresh_runtime_view()` 默认会 `save_config()` 把运行时视图 dump 回配置文件，
    在测试里那会真的改开发机上的 yaml。
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    monkeypatch.setattr(model_group_service, "save_config", lambda: None)
    monkeypatch.setattr(model_group_service.config, "MODEL_GROUPS", {})
    try:
        yield
    finally:
        await Tortoise.close_connections()


class TestDefaultIsSingleSourced:
    """默认值只有一份，且那一份是 private"""

    def test_the_constant_is_private(self) -> None:
        assert DEFAULT_VISIBILITY is ModelGroupVisibility.PRIVATE

    def test_model_field_default_uses_the_constant(self) -> None:
        field = DBModelGroup._meta.fields_map["visibility"]  # noqa: SLF001
        assert field.default == DEFAULT_VISIBILITY.value

    def test_service_default_is_not_a_literal(self) -> None:
        """`create_group()` 的 visibility 形参默认必须是 None（交给常量兜底）

        写成 `= ModelGroupVisibility.SHARED` 是这条缺陷的原始形态；
        写成 `= ModelGroupVisibility.PRIVATE` 虽然结论对，但又多了一份要同步的默认值。
        """
        import inspect

        signature = inspect.signature(create_group)
        assert signature.parameters["visibility"].default is None

    def test_response_schema_has_no_default_at_all(self) -> None:
        """`ModelGroupInfo.visibility` 不许有默认值 —— 那是这个判据的第**五**份副本

        它曾经写着 `default="shared"`。今天走不到（唯一构造点 `build_model_group_info()`
        每次都显式传行上的取值，把它改成 `"private"` **一条用例都不会红** ——
        复核变异 R3 正是这么活下来的），但它会经 OpenAPI schema 把 "shared" 宣称成默认，
        而下一个构造点忘了传就静默落回一个与后端真实默认相反的值。
        断言"没有默认"而不是"默认等于 private"：多一份要同步的默认值本身就是缺陷。
        """
        from nekro_agent.schemas.tenant import ModelGroupInfo

        field = ModelGroupInfo.model_fields["visibility"]
        assert field.is_required(), "ModelGroupInfo.visibility 不该有默认值，谁构造谁负责给出"


class TestPersistedVocabularyIsPinned:
    """`shared` / `private` 这两个**字符串**是库里已有数据的契约，不是内部命名

    本文件其余用例一律拿 `ModelGroupVisibility.X.value` 去比对，于是把枚举取值
    改掉（`"private"` → `"privado"`）之后**一条都不会红** —— 复核变异实测存活。
    而 `visibility` 是以裸字符串存进 `model_group.visibility` 列的：改了取值，
    存量行仍是老字符串，`can_view_group()` 拿新取值去比，结果是**平台方从没做过的
    一次授权撤销**（存量 shared 的组对所有租户静默消失）或反过来。

    前端那两个 `<MenuItem value="...">` 与 i18n 的 `modelGroup.visibility.*` 也按
    同一批字面量拼，改一处就是三处不同步。所以这里把字面量本身钉死。
    """

    def test_the_two_stored_values_are_exactly_these_strings(self) -> None:
        assert ModelGroupVisibility.SHARED.value == "shared"
        assert ModelGroupVisibility.PRIVATE.value == "private"

    def test_there_are_no_other_visibility_values(self) -> None:
        """正向对照：不是"这两个在集合里"，而是"只有这两个"

        多出第三档时，`can_view_group()` 那句 `== SHARED.value` 会把它默默归进
        "租户看不到"，而新增者未必是这个意思。
        """
        assert {member.value for member in ModelGroupVisibility} == {"shared", "private"}

    def test_the_frontend_offers_exactly_the_stored_values(self) -> None:
        """前端下拉里那两个 value 必须逐字等于库里存的两个字符串"""
        source = FRONTEND_FORM.read_text(encoding="utf-8")
        offered = set(re.findall(r'<MenuItem value="(\w+)">\{t\(\'modelGroup\.visibility\.', source))
        assert offered == {member.value for member in ModelGroupVisibility}


class TestNewGroupsLandPrivate:
    """新建路径：默认 private，显式 shared 仍然有效"""

    async def test_bare_model_save_lands_private(self, db: None) -> None:
        row = DBModelGroup(name="bare")
        await row.save()
        assert await _stored_visibility("bare") == ModelGroupVisibility.PRIVATE.value

    async def test_service_create_without_visibility(self, db: None) -> None:
        identity = resolve_console_identity(platform_owner())
        await create_group(identity, "svc_default", _sample_config())
        assert await _stored_visibility("svc_default") == ModelGroupVisibility.PRIVATE.value

    async def test_service_create_with_explicit_shared(self, db: None) -> None:
        """正向对照：平台方仍然能显式建一个 shared 组"""
        identity = resolve_console_identity(platform_owner())
        await create_group(identity, "svc_shared", _sample_config(), ModelGroupVisibility.SHARED)
        assert await _stored_visibility("svc_shared") == ModelGroupVisibility.SHARED.value

    async def test_console_api_create_without_visibility(self, db: None) -> None:
        """接口层不许自带一个与字段默认相反的默认值"""
        body = ModelGroupCreateRequest(name="api_default", chat_model="gpt-4o")
        info = await create_console_model_group(body=body, _current_user=platform_owner())
        assert info.visibility == ModelGroupVisibility.PRIVATE.value
        assert await _stored_visibility("api_default") == ModelGroupVisibility.PRIVATE.value

    async def test_console_api_create_with_explicit_shared(self, db: None) -> None:
        """正向对照：接口仍然接受显式的 shared"""
        body = ModelGroupCreateRequest(
            name="api_shared",
            chat_model="gpt-4o",
            visibility=ModelGroupVisibility.SHARED,
        )
        info = await create_console_model_group(body=body, _current_user=platform_owner())
        assert info.visibility == ModelGroupVisibility.SHARED.value

    async def test_tenant_created_group_is_always_private(
        self,
        db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """租户自建组恒私有，连显式提交 shared 也不行（既有契约，这里防回归）

        租户这条路径会对 `base_url` 做一次真实的 DNS 解析（SSRF 闸门，它有自己的测试）。
        这里把那一步换成 no-op：本组用例要钉的是可见性，让它去解析一个不存在的域名
        只会把结论变成"看这台机器有没有网"。
        """

        async def _allow_any(url: str) -> None:
            return None

        monkeypatch.setattr(model_group_service, "assert_tenant_endpoint_url_safe", _allow_any)

        identity = resolve_console_identity(acme_admin())
        await create_group(identity, "tenant_own", _sample_config(), ModelGroupVisibility.SHARED)
        assert await _stored_visibility("tenant_own") == ModelGroupVisibility.PRIVATE.value

    async def test_import_from_config_file_lands_private(self, db: None) -> None:
        created = await DBModelGroup.import_config_group("from_yaml", _sample_config())
        assert created is not None
        assert await _stored_visibility("from_yaml") == ModelGroupVisibility.PRIVATE.value

    async def test_legacy_platform_upsert_lands_private(self, db: None) -> None:
        await upsert_platform_group("legacy_upsert", _sample_config())
        assert await _stored_visibility("legacy_upsert") == ModelGroupVisibility.PRIVATE.value


class TestExistingSharedGroupsAreNotTouched:
    """存量 shared 的组不能被这次改动改掉：那是用户已经做过的一次授权决定"""

    async def test_bootstrap_leaves_existing_visibility_alone(self, db: None, monkeypatch: pytest.MonkeyPatch) -> None:
        await DBModelGroup.create(
            name="already_shared",
            ownership="platform",
            owner_tenant_id=None,
            visibility=ModelGroupVisibility.SHARED.value,
            group_name="already_shared",
            chat_model="gpt-4o",
            api_key="sk-visibility-SENTINEL-existing",
            model_type="chat",
        )
        # 配置文件里有同名组：走的是"已存在就跳过"那条分支
        monkeypatch.setattr(model_group_service.config, "MODEL_GROUPS", {"already_shared": _sample_config()})

        await model_group_service.bootstrap_model_groups()

        assert await _stored_visibility("already_shared") == ModelGroupVisibility.SHARED.value

    async def test_legacy_upsert_does_not_downgrade_existing(self, db: None) -> None:
        """旧版平台端点覆盖模型字段时，不许顺手把 shared 改成 private"""
        await DBModelGroup.create(
            name="already_shared",
            ownership="platform",
            owner_tenant_id=None,
            visibility=ModelGroupVisibility.SHARED.value,
            group_name="already_shared",
            chat_model="gpt-4o",
            api_key="sk-visibility-SENTINEL-existing",
            model_type="chat",
        )

        await upsert_platform_group("already_shared", _sample_config(model="gpt-4o-mini"))

        assert await _stored_visibility("already_shared") == ModelGroupVisibility.SHARED.value
        assert (await DBModelGroup.get(name="already_shared")).chat_model == "gpt-4o-mini"


class TestFrontendDefaultMatchesBackend:
    """前端表单默认值 == 后端默认值（#32 那条教训的看门人）"""

    def test_the_form_declares_a_default(self) -> None:
        source = FRONTEND_FORM.read_text(encoding="utf-8")
        assert FRONTEND_DEFAULT_RE.search(source), (
            f"没在 {FRONTEND_FORM.name} 里找到 DEFAULT_VISIBILITY 的声明；"
            "两侧默认值的比对因此无从谈起（判据自己失效了，不是产品对了）"
        )

    def test_frontend_default_equals_backend_default(self) -> None:
        source = FRONTEND_FORM.read_text(encoding="utf-8")
        match = FRONTEND_DEFAULT_RE.search(source)
        assert match is not None
        assert match.group("value") == DEFAULT_VISIBILITY.value, (
            f"前端表单默认 {match.group('value')!r} 与后端默认 {DEFAULT_VISIBILITY.value!r} 不一致"
        )

    def test_the_form_has_no_hardcoded_visibility_default(self) -> None:
        """初值与重置两处都必须引用那个常量，不能各写各的字面量

        踩过的形态正是"改了一处、另一处还是老值"：对话框第一次渲染用 A、
        每次打开时的 `useEffect` 重置用 B。
        """
        source = FRONTEND_FORM.read_text(encoding="utf-8")
        literals = re.findall(r"useState<ModelGroupVisibility>\('(\w+)'\)", source)
        assert literals == [], f"表单里仍有写死的可见性初值：{literals}"
        resets = re.findall(r"setVisibility\(mode === 'edit' \? \(source\?\.visibility \?\? '(\w+)'\)", source)
        assert resets == [], f"表单重置里仍有写死的可见性默认：{resets}"

    def test_both_options_are_still_offered(self) -> None:
        """正向对照：下拉里两个选项都还在

        只断言"默认是 private"的话，把 shared 这个选项整个删掉也能让上面几条通过 ——
        而那等于"开放平台组给租户"这个功能没有了出口。
        """
        source = FRONTEND_FORM.read_text(encoding="utf-8")
        offered: List[str] = re.findall(r'<MenuItem value="(shared|private)">', source)
        assert sorted(offered) == ["private", "shared"]
