"""技能库的归属、可见性与租户作用域（A4）

## 改前实测到的形态

`routers/skills.py` 的 13 个读端点全是 `Role.Admin` 且**不带任何过滤**，
而租户管理员正好就是 `Role.Admin`（`TENANT_MAX_PERM_LEVEL`）：

    GET /api/skills/tree                      -> 200，平台方整座库的目录
    GET /api/skills/content?path=ops-runbook  -> 200，平台方运维手册**全文**
    GET /api/skills/file?path=ops-runbook/x.md-> 200，同上

反过来 13 个写端点全是 `Role.Super`，租户一个字也写不了 ——
`需求文档 §R8`「可以自己制作并使用 skills」对定制版客户不成立。

而且**读接口不是最短的那条路**：`PUT /workspaces/{id}/skills` 是 `Role.Admin`，
选中列表是一串任意字符串，改前 `sync_skills` 拿它直接去全局库找目录 ——
租户填一个平台方私有技能的名字，正文就被复制进它自己的工作区，
由容器里的 Claude Code 逐字读走，全程不经过 `routers/skills.py`。
`TestWorkspaceSyncIsTheLastGate` 钉的就是这一跳。

## 为什么是表驱动 + 七个归属档

「一族端点里漏了一个」是这个洞每次的形态。所以可见性判定写成
`VISIBILITY_CASES` 七个档（内置 / 平台私有 / 平台共享 / 仓库私有 / 仓库共享 /
自己的 / 别的租户的）× 四个吃路径的读端点，一次 28 条，
再由 `TestEveryLiveSkillEndpointHasACase` 反过来钉住"路由表里有、用例表里没有"直接红。

## 每一档都有平台方的正向对照

只断言"租户拿到 404"证明不了拒绝来自作用域判定 —— 端点自己坏了（路径拼错、
桩没接上、目录没建）同样抛 404。所以每条用例都跑两遍**同一组实参**，只换身份：

- 租户 -> `NotFoundError`，且平台方那份文件**分毫未动**
- 平台方 / 未启用多租户的管理员 -> 成功，并对正文做逐字断言
"""

import datetime
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

import pytest
from fastapi import UploadFile
from tortoise import Tortoise

from nekro_agent.core import auto_inject_skills as auto_inject_module
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_model_group import ModelGroupVisibility
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.routers.skills import (
    SkillFileSaveRequest,
    SyncToWorkspacesRequest,
    VisibilityUpdateRequest,
    delete_skill,
    get_auto_inject_skills,
    get_skill_content,
    get_skill_file,
    get_skill_readme,
    get_skills_tree,
    list_all_skills,
    list_repos,
    list_skill_dir,
    list_skills,
    save_skill_file,
    sync_skill_to_workspaces,
    update_auto_inject_skills,
    update_skill_visibility,
    upload_skill,
)
from nekro_agent.routers.skills import router as skills_router
from nekro_agent.routers.workspaces import (
    DynamicSkillFileBody,
    get_dynamic_skill_file,
    list_dynamic_skill_dir,
    save_dynamic_skill_file,
)
from nekro_agent.routers.workspaces import promote_dynamic_skill as promote_dynamic_skill_route
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.schemas.workspace import AutoInjectSkillsUpdate
from nekro_agent.services import skill_scope
from nekro_agent.services.skill_scope import SkillOwnership, SkillVisibility
from nekro_agent.services.user.role import Role
from nekro_agent.services.workspace.manager import WorkspaceService

ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2

# 每个技能目录里都放一份同名附属文件，好让「读目录内文件」这一档也有确定的实参
EXTRA_DOC = "extra.md"


# ---------------------------------------------------------------------------
# 身份
# ---------------------------------------------------------------------------


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


def rival_admin() -> DBUser:
    return make_user(tenant_id=RIVAL_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


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
# 库的布局
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """一个归属档

    Attributes:
        skill_id: 技能标识（仓库技能是 `repo/skill`）
        source: `list_skill_dir` 的 `source` 参数
        visible_to_acme: 对 acme 租户是否可见
        body: SKILL.md 正文里的可断言标记
    """

    case_id: str
    skill_id: str
    source: str
    visible_to_acme: bool
    body: str


VISIBILITY_CASES: Tuple[Case, ...] = (
    Case("builtin", "builtin-skill", "builtin", True, "BUILTIN-BODY"),
    Case("platform-private", "ops-runbook", "user", False, "PLATFORM-PRIVATE-BODY"),
    Case("platform-shared", "shared-guide", "user", True, "PLATFORM-SHARED-BODY"),
    Case("repo-private", "secret-repo/inner", "repo", False, "REPO-PRIVATE-BODY"),
    Case("repo-shared", "open-repo/inner", "repo", True, "REPO-SHARED-BODY"),
    Case("own", "acme-notes", "user", True, "ACME-BODY"),
    Case("rival", "rival-notes", "user", False, "RIVAL-BODY"),
)

CASE_PARAMS = [pytest.param(case, id=case.case_id) for case in VISIBILITY_CASES]
# 平台方的可读根是「内置 + local + repos」，**不含**任何租户目录。
# 所以正向对照跑的是这五档；两个租户库那两档由
# `test_platform_does_not_browse_tenant_libraries` 单独钉住。
PLATFORM_VISIBLE = {case.case_id for case in VISIBILITY_CASES} - {"own", "rival"}


def _write_skill(base: Path, name: str, body: str) -> None:
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name.split('/')[-1]}\ndescription: desc-{name.split('/')[-1]}\n---\n{body}\n",
        encoding="utf-8",
    )
    (skill_dir / EXTRA_DOC).write_text(f"{body}-EXTRA\n", encoding="utf-8")


@pytest.fixture
def library(tmp_path, monkeypatch) -> Any:
    """把四个技能根目录与可见性登记表整体搬到 tmp_path

    不桩掉任何一层：技能真的落盘、正文真的读出来。这一族的判定全在文件系统上，
    把 IO 桩掉等于把被测对象桩掉。
    """
    builtin = tmp_path / "builtin_skills"
    local = tmp_path / "skills" / "local"
    repos = tmp_path / "skills" / "repos"
    tenants = tmp_path / "skills" / "tenants"
    for path in (builtin, local, repos, tenants):
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(skill_scope, "BUILTIN_SKILLS_SOURCE_DIR", str(builtin))
    monkeypatch.setattr(skill_scope, "SKILLS_LOCAL_DIR", str(local))
    monkeypatch.setattr(skill_scope, "SKILLS_REPOS_DIR", str(repos))
    monkeypatch.setattr(skill_scope, "SKILLS_TENANTS_DIR", str(tenants))
    monkeypatch.setattr(skill_scope, "VISIBILITY_PATH", tmp_path / "configs" / "skill-visibility.json")
    monkeypatch.setattr("nekro_agent.routers.skills.SKILLS_REPOS_DIR", str(repos))
    monkeypatch.setattr("nekro_agent.core.os_env.BUILTIN_SKILLS_SOURCE_DIR", str(builtin))
    monkeypatch.setattr("nekro_agent.services.workspace.manager.BUILTIN_SKILLS_SOURCE_DIR", str(builtin))
    monkeypatch.setattr(auto_inject_module, "_AUTO_INJECT_PATH", tmp_path / "configs" / "auto-inject-skills.json")

    _write_skill(builtin, "builtin-skill", "BUILTIN-BODY")
    _write_skill(local, "ops-runbook", "PLATFORM-PRIVATE-BODY")
    _write_skill(local, "shared-guide", "PLATFORM-SHARED-BODY")
    _write_skill(repos, "secret-repo/inner", "REPO-PRIVATE-BODY")
    _write_skill(repos, "open-repo/inner", "REPO-SHARED-BODY")
    _write_skill(skill_scope.tenant_library_dir(ACME_TENANT_ID), "acme-notes", "ACME-BODY")
    _write_skill(skill_scope.tenant_library_dir(RIVAL_TENANT_ID), "rival-notes", "RIVAL-BODY")

    skill_scope.set_visibility(skill_scope.LOCAL_KIND, "shared-guide", SkillVisibility.SHARED)
    skill_scope.set_visibility(skill_scope.REPOS_KIND, "open-repo", SkillVisibility.SHARED)

    return tmp_path


# ---------------------------------------------------------------------------
# 读：七档 × 四个吃路径的端点
# ---------------------------------------------------------------------------


async def _read_content(case: Case, user: DBUser) -> str:
    return (await get_skill_content(path=case.skill_id, _current_user=user)).readme


async def _read_file(case: Case, user: DBUser) -> str:
    return (await get_skill_file(path=f"{case.skill_id}/{EXTRA_DOC}", _current_user=user)).readme


async def _read_dir(case: Case, user: DBUser) -> str:
    response = await list_skill_dir(name=case.skill_id, source=case.source, _current_user=user)  # type: ignore[arg-type]
    return "".join(sorted(entry.rel_path for entry in response.entries))


async def _read_readme(case: Case, user: DBUser) -> str:
    return (await get_skill_readme(name=case.skill_id, _current_user=user)).readme


#: (端点名, 调用, 断言用的期望片段构造)
PATH_READERS: Tuple[Tuple[str, Callable[[Case, DBUser], Any], Callable[[Case], str]], ...] = (
    ("content", _read_content, lambda case: case.body),
    ("file", _read_file, lambda case: f"{case.body}-EXTRA"),
    ("dir", _read_dir, lambda case: EXTRA_DOC),
    # `/{name}/readme` 只服务扁平标识（仓库技能没有这条入口），仓库两档在下面单独跳过
    ("readme", _read_readme, lambda case: case.body),
)

READER_PARAMS = [pytest.param(reader, id=reader[0]) for reader in PATH_READERS]


def _skip_if_unsupported(reader_name: str, case: Case) -> None:
    """`/{name}/readme` 只服务「独立技能库」那一族

    改前它只读 `SKILLS_LOCAL_DIR`，内置与仓库技能各有自己的入口
    （`/skills/builtin/{name}`、`/skills/content`），这里保持同一形状。
    """
    if reader_name == "readme" and ("/" in case.skill_id or case.case_id == "builtin"):
        pytest.skip("/{name}/readme 只服务独立技能库（非内置、非仓库）")


class TestTenantReadsAreScoped:
    """租户读技能正文：自己的与平台方共享的通，平台方私有与别的租户的 404"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", CASE_PARAMS)
    @pytest.mark.parametrize("reader", READER_PARAMS)
    async def test_tenant_sees_exactly_its_own_scope(self, library, case: Case, reader) -> None:
        name, call, expect = reader
        _skip_if_unsupported(name, case)
        user = acme_admin()
        if case.visible_to_acme:
            assert expect(case) in await call(case, user)
        else:
            with pytest.raises(NotFoundError):
                await call(case, user)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", CASE_PARAMS)
    @pytest.mark.parametrize("reader", READER_PARAMS)
    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_platform_positive_control(self, library, case: Case, reader, identity) -> None:
        """同一组实参，只换身份：平台方必须读得到自己的每一档

        没有这一条，上面那些 `NotFoundError` 可能只是因为端点自己坏了。
        """
        name, call, expect = reader
        _skip_if_unsupported(name, case)
        if case.case_id not in PLATFORM_VISIBLE:
            pytest.skip("租户自有技能不在平台方的可读根里，见 test_platform_does_not_browse_tenant_libraries")
        assert expect(case) in await call(case, identity())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", CASE_PARAMS)
    async def test_cross_tenant_is_indistinguishable_from_missing(self, library, case: Case) -> None:
        """跨租户与不存在给出同一个响应

        区分开来，租户就能靠试错枚举出别人库里有哪些技能。
        """
        user = rival_admin()
        expected_visible = case.case_id in {"builtin", "platform-shared", "repo-shared", "rival"}
        if expected_visible:
            assert case.body in await _read_content(case, user)
        else:
            with pytest.raises(NotFoundError) as seen:
                await _read_content(case, user)
            with pytest.raises(NotFoundError) as missing:
                await get_skill_content(path="no-such-skill-at-all", _current_user=rival_admin())
            assert type(seen.value) is type(missing.value)

    @pytest.mark.asyncio
    async def test_platform_does_not_browse_tenant_libraries(self, library) -> None:
        """平台方的可读根里**没有**租户目录

        这不是"平台方权限不够"，而是"技能库这条产品线上平台方没有浏览租户内容的入口"——
        与工作区那边不同（工作区平台方可见），这里刻意不开，因为技能正文是租户自己写的资料。
        真要排查，平台方本来就能直接读宿主磁盘。
        """
        with pytest.raises(NotFoundError):
            await get_skill_content(path="acme-notes", _current_user=platform_owner())


class TestListingsAreScoped:
    """列表族：树 / 全量候选 / 扁平库 / 仓库清单"""

    @pytest.mark.asyncio
    async def test_tree_shows_own_library_and_shared_platform_only(self, library) -> None:
        tenant_nodes = {node.name for node in (await get_skills_tree(_current_user=acme_admin())).nodes}
        assert tenant_nodes == {"acme-notes", "shared-guide"}

        platform_nodes = {node.name for node in (await get_skills_tree(_current_user=platform_owner())).nodes}
        assert platform_nodes == {"ops-runbook", "shared-guide"}

    @pytest.mark.asyncio
    async def test_tree_marks_ownership_and_editability(self, library) -> None:
        by_name = {node.name: node for node in (await get_skills_tree(_current_user=acme_admin())).nodes}
        assert (by_name["acme-notes"].ownership, by_name["acme-notes"].editable) == ("tenant", True)
        # 平台方共享出来的那份对租户是只读的：能选用，不能改
        assert (by_name["shared-guide"].ownership, by_name["shared-guide"].editable) == ("platform", False)
        # 可见性只对平台方回显：租户不需要知道自己看到的这份在对面被标成了什么
        assert by_name["shared-guide"].visibility is None

        platform_by_name = {node.name: node for node in (await get_skills_tree(_current_user=platform_owner())).nodes}
        assert platform_by_name["ops-runbook"].visibility == "private"
        assert platform_by_name["shared-guide"].visibility == "shared"
        assert platform_by_name["ops-runbook"].editable is True

    @pytest.mark.asyncio
    async def test_all_skills_candidates_follow_the_scope(self, library) -> None:
        tenant_names = {item.name for item in (await list_all_skills(_current_user=acme_admin())).items}
        assert tenant_names == {"builtin-skill", "acme-notes", "shared-guide", "open-repo/inner"}

        platform_names = {item.name for item in (await list_all_skills(_current_user=platform_owner())).items}
        assert platform_names == {
            "builtin-skill",
            "ops-runbook",
            "shared-guide",
            "secret-repo/inner",
            "open-repo/inner",
        }

    @pytest.mark.asyncio
    async def test_flat_library_listing_follows_the_scope(self, library) -> None:
        tenant_items = {item.name: item for item in (await list_skills(_current_user=acme_admin())).items}
        assert set(tenant_items) == {"acme-notes", "shared-guide"}
        assert tenant_items["acme-notes"].editable is True
        assert tenant_items["shared-guide"].editable is False

        platform_items = {item.name for item in (await list_skills(_current_user=platform_owner())).items}
        assert platform_items == {"ops-runbook", "shared-guide"}

    @pytest.mark.asyncio
    async def test_repo_listing_hides_private_repos_and_their_urls(self, library) -> None:
        (Path(library) / "skills" / "repos" / "secret-repo" / ".repo-meta.json").write_text(
            '{"repo_url": "https://git.internal/ops.git"}', encoding="utf-8"
        )
        (Path(library) / "skills" / "repos" / "open-repo" / ".repo-meta.json").write_text(
            '{"repo_url": "https://github.com/public/skills.git"}', encoding="utf-8"
        )

        tenant_repos = (await list_repos(_current_user=acme_admin())).repos
        assert [repo.repo_name for repo in tenant_repos] == ["open-repo"]
        # 仓库地址是"平台方订阅了谁"的运营信息，与技能正文同档，只对平台方回显
        assert tenant_repos[0].repo_url is None

        platform_repos = {repo.repo_name: repo for repo in (await list_repos(_current_user=platform_owner())).repos}
        assert set(platform_repos) == {"secret-repo", "open-repo"}
        assert platform_repos["secret-repo"].repo_url == "https://git.internal/ops.git"
        assert platform_repos["secret-repo"].visibility == "private"


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------


class TestTenantWritesLandInItsOwnLibrary:
    """R8：租户能自制技能，且只写得到自己那一层"""

    @pytest.mark.asyncio
    async def test_tenant_can_create_and_read_back_its_own_skill(self, library) -> None:
        await save_skill_file(
            SkillFileSaveRequest(path="acme-new/SKILL.md", content="---\nname: acme-new\n---\nNEW-BODY\n"),
            _current_user=acme_admin(),
        )
        assert (skill_scope.tenant_library_dir(ACME_TENANT_ID) / "acme-new" / "SKILL.md").is_file()
        assert "NEW-BODY" in await _read_content(Case("x", "acme-new", "user", True, ""), acme_admin())
        # 别的租户与平台方都看不到它
        with pytest.raises(NotFoundError):
            await get_skill_content(path="acme-new", _current_user=rival_admin())
        with pytest.raises(NotFoundError):
            await get_skill_content(path="acme-new", _current_user=platform_owner())

    @pytest.mark.asyncio
    async def test_tenant_write_cannot_overwrite_a_platform_skill_of_the_same_name(self, library) -> None:
        """同名写入落在自己库里，平台方那份逐字不动

        这条比"拒绝写入"更重要：落点只有 `writable_root()` 一个出口，
        所以越权在**结构上**不可能发生，而不是靠端点记得判一次。
        """
        platform_file = Path(library) / "skills" / "local" / "ops-runbook" / "SKILL.md"
        before = platform_file.read_text(encoding="utf-8")

        await save_skill_file(
            SkillFileSaveRequest(path="ops-runbook/SKILL.md", content="---\nname: ops-runbook\n---\nHIJACKED\n"),
            _current_user=acme_admin(),
        )

        assert platform_file.read_text(encoding="utf-8") == before
        assert "HIJACKED" in (
            skill_scope.tenant_library_dir(ACME_TENANT_ID) / "ops-runbook" / "SKILL.md"
        ).read_text(encoding="utf-8")
        # 租户此后看到的是自己那份（自有库排在解析顺序最前）
        assert "HIJACKED" in (await get_skill_content(path="ops-runbook", _current_user=acme_admin())).readme

    @pytest.mark.asyncio
    async def test_own_library_shadows_the_platform_one_for_that_tenant(self, library) -> None:
        await save_skill_file(
            SkillFileSaveRequest(path="shared-guide/SKILL.md", content="---\nname: shared-guide\n---\nMINE\n"),
            _current_user=acme_admin(),
        )
        assert "MINE" in (await get_skill_content(path="shared-guide", _current_user=acme_admin())).readme
        # 平台方与别的租户仍看到平台方那份
        assert "PLATFORM-SHARED-BODY" in (
            await get_skill_content(path="shared-guide", _current_user=platform_owner())
        ).readme
        assert "PLATFORM-SHARED-BODY" in (
            await get_skill_content(path="shared-guide", _current_user=rival_admin())
        ).readme

    @pytest.mark.asyncio
    async def test_path_traversal_out_of_the_tenant_library_is_rejected(self, library) -> None:
        for evil in ("../local/ops-runbook/SKILL.md", "..\\local\\x.md", "a/../../b.md", "./x/y.md", "C:/x/y.md"):
            with pytest.raises(ValidationError):
                await save_skill_file(
                    SkillFileSaveRequest(path=evil, content="X"), _current_user=acme_admin()
                )
        assert "PLATFORM-PRIVATE-BODY" in (
            Path(library) / "skills" / "local" / "ops-runbook" / "SKILL.md"
        ).read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_an_absolute_looking_path_is_reinterpreted_inside_the_library(self, library) -> None:
        """前导 `/` 不算越界，只是被当成"相对于自己那层库"的路径

        这是**行为断言**不是安全断言：把它判成 400 会让"技能名叫 etc"这种无害输入报错，
        而真正的越界（`..`、盘符、反斜杠）由上面那条钉住。
        """
        await save_skill_file(
            SkillFileSaveRequest(path="/etc/passwd", content="X"), _current_user=acme_admin()
        )
        assert (skill_scope.tenant_library_dir(ACME_TENANT_ID) / "etc" / "passwd").is_file()
        assert not (Path(library) / "skills" / "local" / "etc").exists()

    @pytest.mark.asyncio
    async def test_zip_upload_lands_in_the_tenant_library(self, library) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("acme-zip/SKILL.md", "---\nname: acme-zip\n---\nZIP-BODY\n")
        buffer.seek(0)
        result = await upload_skill(
            file=UploadFile(filename="acme-zip.zip", file=buffer), _current_user=acme_admin()
        )
        assert result.name == "acme-zip"
        assert (skill_scope.tenant_library_dir(ACME_TENANT_ID) / "acme-zip" / "SKILL.md").is_file()
        assert not (Path(library) / "skills" / "local" / "acme-zip").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_platform_writes_still_land_in_the_platform_library(self, library, identity) -> None:
        """正向对照：同一个端点、同一组实参，平台方写的还是 `local/`"""
        await save_skill_file(
            SkillFileSaveRequest(path="plat-new/SKILL.md", content="---\nname: plat-new\n---\nPLAT\n"),
            _current_user=identity(),
        )
        assert (Path(library) / "skills" / "local" / "plat-new" / "SKILL.md").is_file()
        assert not (skill_scope.tenant_library_dir(ACME_TENANT_ID) / "plat-new").exists()


class TestDeleteOnlyTouchesTheWritableLibrary:
    @pytest.mark.asyncio
    async def test_tenant_cannot_delete_a_platform_skill(self, library) -> None:
        platform_dir = Path(library) / "skills" / "local" / "ops-runbook"
        with pytest.raises(NotFoundError):
            await delete_skill(name="ops-runbook", _current_user=acme_admin())
        assert platform_dir.is_dir()

        # 连共享给它的那一份也删不掉：共享是"能读能选用"，不是"能改"
        shared_dir = Path(library) / "skills" / "local" / "shared-guide"
        with pytest.raises(NotFoundError):
            await delete_skill(name="shared-guide", _current_user=acme_admin())
        assert shared_dir.is_dir()

    @pytest.mark.asyncio
    async def test_tenant_can_delete_its_own_skill(self, library) -> None:
        await delete_skill(name="acme-notes", _current_user=acme_admin())
        assert not (skill_scope.tenant_library_dir(ACME_TENANT_ID) / "acme-notes").exists()
        # 别的租户的同名操作不受影响
        assert (skill_scope.tenant_library_dir(RIVAL_TENANT_ID) / "rival-notes").is_dir()

    @pytest.mark.asyncio
    async def test_tenant_cannot_delete_another_tenants_skill(self, library) -> None:
        with pytest.raises(NotFoundError):
            await delete_skill(name="rival-notes", _current_user=acme_admin())
        assert (skill_scope.tenant_library_dir(RIVAL_TENANT_ID) / "rival-notes").is_dir()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_platform_delete_positive_control(self, library, identity) -> None:
        await delete_skill(name="ops-runbook", _current_user=identity())
        assert not (Path(library) / "skills" / "local" / "ops-runbook").exists()

    @pytest.mark.asyncio
    async def test_deleting_a_shared_skill_clears_its_visibility_record(self, library) -> None:
        """不清理的话，下一个同名技能会**继承**上一个的共享状态"""
        await delete_skill(name="shared-guide", _current_user=platform_owner())
        assert skill_scope.list_visibility(skill_scope.LOCAL_KIND) == {}
        _write_skill(Path(library) / "skills" / "local", "shared-guide", "REBORN")
        with pytest.raises(NotFoundError):
            await get_skill_content(path="shared-guide", _current_user=acme_admin())


class TestVisibilityIsPlatformOnlyAndDefaultsPrivate:
    @pytest.mark.asyncio
    async def test_new_platform_skill_defaults_to_private(self, library) -> None:
        await save_skill_file(
            SkillFileSaveRequest(path="fresh/SKILL.md", content="---\nname: fresh\n---\nFRESH\n"),
            _current_user=platform_owner(),
        )
        with pytest.raises(NotFoundError):
            await get_skill_content(path="fresh", _current_user=acme_admin())

    @pytest.mark.asyncio
    async def test_sharing_and_revoking_flips_tenant_visibility(self, library) -> None:
        await update_skill_visibility(
            VisibilityUpdateRequest(kind="local", name="ops-runbook", visibility="shared"),
            _current_user=platform_owner(),
        )
        assert "PLATFORM-PRIVATE-BODY" in (
            await get_skill_content(path="ops-runbook", _current_user=acme_admin())
        ).readme

        await update_skill_visibility(
            VisibilityUpdateRequest(kind="local", name="ops-runbook", visibility="private"),
            _current_user=platform_owner(),
        )
        with pytest.raises(NotFoundError):
            await get_skill_content(path="ops-runbook", _current_user=acme_admin())

    @pytest.mark.asyncio
    async def test_visibility_target_must_exist(self, library) -> None:
        with pytest.raises(NotFoundError):
            await update_skill_visibility(
                VisibilityUpdateRequest(kind="local", name="no-such", visibility="shared"),
                _current_user=platform_owner(),
            )

    def test_a_corrupt_registry_falls_back_to_all_private(self, library) -> None:
        """读不出来时按"全部私有"处理

        回落成"全部共享"会让一次 JSON 损坏静默变成一次全量泄露。
        """
        skill_scope.VISIBILITY_PATH.write_text("{ not json", encoding="utf-8")
        assert skill_scope.get_visibility(skill_scope.LOCAL_KIND, "shared-guide") is SkillVisibility.PRIVATE

    def test_visibility_vocabulary_matches_model_groups(self) -> None:
        """技能与模型组共用一套可见性词，不许分叉出第二套"""
        assert {item.value for item in SkillVisibility} == {item.value for item in ModelGroupVisibility}
        assert SkillVisibility.SHARED.value == ModelGroupVisibility.SHARED.value
        assert SkillVisibility.PRIVATE.value == ModelGroupVisibility.PRIVATE.value


# ---------------------------------------------------------------------------
# auto-inject
# ---------------------------------------------------------------------------


class TestAutoInjectFollowsTheScope:
    @pytest.mark.asyncio
    async def test_tenant_falls_back_to_the_visible_subset_of_the_platform_list(self, library) -> None:
        await update_auto_inject_skills(
            AutoInjectSkillsUpdate(skills=["builtin-skill", "ops-runbook", "shared-guide"]),
            _current_user=platform_owner(),
        )
        platform_list = (await get_auto_inject_skills(_current_user=platform_owner())).skills
        assert platform_list == ["builtin-skill", "ops-runbook", "shared-guide"]

        # 租户没设过自己那份 -> 回落平台方清单，但**裁掉**私有的那条
        tenant_list = (await get_auto_inject_skills(_current_user=acme_admin())).skills
        assert tenant_list == ["builtin-skill", "shared-guide"]

    @pytest.mark.asyncio
    async def test_tenant_list_is_independent_once_set(self, library) -> None:
        await update_auto_inject_skills(
            AutoInjectSkillsUpdate(skills=["builtin-skill"]), _current_user=platform_owner()
        )
        await update_auto_inject_skills(
            AutoInjectSkillsUpdate(skills=["acme-notes"]), _current_user=acme_admin()
        )
        assert (await get_auto_inject_skills(_current_user=acme_admin())).skills == ["acme-notes"]
        assert (await get_auto_inject_skills(_current_user=platform_owner())).skills == ["builtin-skill"]
        assert (await get_auto_inject_skills(_current_user=rival_admin())).skills == ["builtin-skill"]

    @pytest.mark.asyncio
    async def test_tenant_cannot_write_an_invisible_skill_into_its_list(self, library) -> None:
        await update_auto_inject_skills(
            AutoInjectSkillsUpdate(skills=["ops-runbook", "acme-notes"]), _current_user=acme_admin()
        )
        assert (await get_auto_inject_skills(_current_user=acme_admin())).skills == ["acme-notes"]

    @pytest.mark.asyncio
    async def test_revoking_share_drops_it_from_an_already_saved_tenant_list(self, library) -> None:
        """判据在**每次使用时**重算，不是只在写入时校验一次"""
        await update_auto_inject_skills(
            AutoInjectSkillsUpdate(skills=["shared-guide"]), _current_user=acme_admin()
        )
        assert (await get_auto_inject_skills(_current_user=acme_admin())).skills == ["shared-guide"]
        skill_scope.set_visibility(skill_scope.LOCAL_KIND, "shared-guide", SkillVisibility.PRIVATE)
        assert (await get_auto_inject_skills(_current_user=acme_admin())).skills == []

    @pytest.mark.asyncio
    async def test_a_corrupt_store_is_refused_instead_of_overwritten(self, library) -> None:
        """这份文件从「单值」变成「全部作用域共用的一份」，读失败的回落方向必须跟着换

        读那一侧「解析不出来就按空表」是对的（顶多少注入几条技能）；
        写这一侧照抄同一条回落，就变成一个租户点一次保存把平台方与其余每个租户的
        清单一起覆盖成空 —— 同一个异常的两条出路，方向相反。
        """
        await update_auto_inject_skills(
            AutoInjectSkillsUpdate(skills=["builtin-skill"]), _current_user=platform_owner()
        )
        await update_auto_inject_skills(
            AutoInjectSkillsUpdate(skills=["acme-notes"]), _current_user=acme_admin()
        )
        # 模拟一次写到一半的崩溃：`write_text` 先截断再写，不是原子的
        healthy = auto_inject_module._AUTO_INJECT_PATH.read_text(encoding="utf-8")
        assert "builtin-skill" in healthy and "acme-notes" in healthy
        truncated = healthy[: len(healthy) // 2]
        auto_inject_module._AUTO_INJECT_PATH.write_text(truncated, encoding="utf-8")

        with pytest.raises(ValidationError):
            await update_auto_inject_skills(
                AutoInjectSkillsUpdate(skills=["shared-guide"]), _current_user=rival_admin()
            )
        # 文件**逐字未动**：没有被换成一份只剩 rival 那一条的新清单
        assert auto_inject_module._AUTO_INJECT_PATH.read_text(encoding="utf-8") == truncated
        assert "shared-guide" not in truncated

    @pytest.mark.asyncio
    async def test_a_healthy_store_still_accepts_writes(self, library) -> None:
        """对照组：文件正常时写得进去 —— 否则上面那条可以被「保存永远失败」满足"""
        await update_auto_inject_skills(
            AutoInjectSkillsUpdate(skills=["acme-notes"]), _current_user=acme_admin()
        )
        assert (await get_auto_inject_skills(_current_user=acme_admin())).skills == ["acme-notes"]


# ---------------------------------------------------------------------------
# 工作区同步：真正的正文出口
# ---------------------------------------------------------------------------


async def _init_schema() -> None:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()


@pytest.fixture
async def workspaces(library, tmp_path, monkeypatch) -> Any:
    """acme / rival / 平台方各一个工作区，归属由「频道 -> 实例 -> 租户」推出来"""
    monkeypatch.setattr("nekro_agent.services.workspace.manager.WORKSPACE_ROOT_DIR", str(tmp_path / "workspaces"))
    await _init_schema()
    try:
        for instance_key, tenant_id in (("inst-acme", ACME_TENANT_ID), ("inst-rival", RIVAL_TENANT_ID)):
            await DBAdapterInstance.create(
                adapter_key="onebot_v11", instance_key=instance_key, display_name=instance_key,
                status="online", provider="remote_onebot", tenant_id=tenant_id, metadata_json="",
            )
        created = {}
        for name, instance_key in (("acme-ws", "inst-acme"), ("rival-ws", "inst-rival"), ("platform-ws", None)):
            ws = await DBWorkspace.create(name=name, description="", runtime_policy="agent")
            if instance_key:
                await DBChatChannel.create(
                    chat_key=f"onebot_v11-{instance_key}-group_1",
                    channel_id="group_1",
                    channel_name=name,
                    channel_type="group",
                    adapter_key="onebot_v11",
                    instance_key=instance_key,
                    data="{}",
                    workspace_id=ws.id,
                )
            created[name] = ws
        yield created
    finally:
        await Tortoise.close_connections()


class TestWorkspaceSyncIsTheLastGate:
    """选中列表是一串任意字符串，`sync_skills` 必须自己按工作区归属重新解析

    改前这里不带作用域，于是 `PUT /workspaces/{id}/skills`（`Role.Admin`）填一个
    平台方私有技能的名字，正文就被复制进租户的工作区目录 ——
    比读接口那条路更直接，而且全程不经过 `routers/skills.py`。
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", CASE_PARAMS)
    async def test_only_visible_skills_reach_a_tenant_workspace(self, workspaces, case: Case) -> None:
        ws = workspaces["acme-ws"]
        ws.metadata = {"skills": [case.skill_id]}
        await ws.save(update_fields=["metadata"])
        await WorkspaceService.sync_skills(ws)

        target = WorkspaceService.get_workspace_dir(ws.id) / ".claude_home" / "skills" / case.skill_id.split("/")[-1]
        assert target.is_dir() is case.visible_to_acme
        if case.visible_to_acme:
            assert case.body in (target / "SKILL.md").read_text(encoding="utf-8")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", CASE_PARAMS)
    async def test_platform_workspace_positive_control(self, workspaces, case: Case) -> None:
        """同一组实参、同一个函数，平台方的工作区拿得到平台方的每一档"""
        if case.case_id not in PLATFORM_VISIBLE:
            pytest.skip("租户自有技能不在平台方工作区的可读根里")
        ws = workspaces["platform-ws"]
        ws.metadata = {"skills": [case.skill_id]}
        await ws.save(update_fields=["metadata"])
        await WorkspaceService.sync_skills(ws)

        target = WorkspaceService.get_workspace_dir(ws.id) / ".claude_home" / "skills" / case.skill_id.split("/")[-1]
        assert target.is_dir()
        assert case.body in (target / "SKILL.md").read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_an_already_synced_copy_is_removed_when_the_workspace_changes_hands(self, workspaces) -> None:
        """工作区先由平台方建好选好技能、之后才绑到租户频道上 —— 这条时序的收口

        `sync_skills` 的清理判据用的是**解析后**的名字集合，所以下一次同步会把
        那份已经落盘的私有技能删掉，而不是留在目录里等 CC 去读。
        """
        ws = workspaces["platform-ws"]
        ws.metadata = {"skills": ["ops-runbook"]}
        await ws.save(update_fields=["metadata"])
        await WorkspaceService.sync_skills(ws)
        target = WorkspaceService.get_workspace_dir(ws.id) / ".claude_home" / "skills" / "ops-runbook"
        assert target.is_dir()

        await DBChatChannel.create(
            chat_key="onebot_v11-inst-acme-group_2",
            channel_id="group_2",
            channel_name="late-bind",
            channel_type="group",
            adapter_key="onebot_v11",
            instance_key="inst-acme",
            data="{}",
            workspace_id=ws.id,
        )
        await WorkspaceService.sync_skills(ws)
        assert not target.exists()

    @pytest.mark.asyncio
    async def test_sync_to_workspaces_stays_inside_the_callers_scope(self, workspaces) -> None:
        """改前 `sync_skill_to_all_workspaces` 遍历全部工作区，不带归属过滤"""
        for name in ("acme-ws", "rival-ws"):
            ws = workspaces[name]
            ws.metadata = {"skills": ["shared-guide"]}
            await ws.save(update_fields=["metadata"])

        result = await sync_skill_to_workspaces(
            SyncToWorkspacesRequest(skill_id="shared-guide"), _current_user=acme_admin()
        )
        assert result.synced_count == 1
        assert (
            WorkspaceService.get_workspace_dir(workspaces["acme-ws"].id) / ".claude_home" / "skills" / "shared-guide"
        ).is_dir()
        assert not (
            WorkspaceService.get_workspace_dir(workspaces["rival-ws"].id) / ".claude_home" / "skills" / "shared-guide"
        ).exists()

    @pytest.mark.asyncio
    async def test_sync_to_workspaces_refuses_an_invisible_skill(self, workspaces) -> None:
        ws = workspaces["acme-ws"]
        ws.metadata = {"skills": ["ops-runbook"]}
        await ws.save(update_fields=["metadata"])
        with pytest.raises(NotFoundError):
            await sync_skill_to_workspaces(
                SyncToWorkspacesRequest(skill_id="ops-runbook"), _current_user=acme_admin()
            )

    @pytest.mark.asyncio
    async def test_platform_sync_to_workspaces_positive_control(self, workspaces) -> None:
        for name in ("acme-ws", "rival-ws", "platform-ws"):
            ws = workspaces[name]
            ws.metadata = {"skills": ["shared-guide"]}
            await ws.save(update_fields=["metadata"])
        result = await sync_skill_to_workspaces(
            SyncToWorkspacesRequest(skill_id="shared-guide"), _current_user=platform_owner()
        )
        assert result.synced_count == 3


class TestPromotionLandsInTheCallersLibrary:
    """把沙盒里 CC 自己写的技能收进技能库 —— `需求文档 §R8` 的主路径

    这条端点此前是 `Role.Super`，判据是"晋升 == 往全局库写服务端可信代码"。
    技能库有了归属之后落点是 `writable_root(调用方作用域)`，租户写进的是自己那一层，
    与它用 `PUT /skills/file` 写出来的东西同一个目录、同一个可达范围，不多给任何能力。
    """

    @staticmethod
    def _write_dynamic(workspace_id: int, dir_name: str, body: str) -> None:
        WorkspaceService.write_dynamic_skill(
            workspace_id, dir_name, f"---\nname: {dir_name}\ndescription: d\n---\n{body}\n"
        )

    @pytest.mark.asyncio
    async def test_tenant_promotion_lands_in_its_own_library(self, workspaces, library) -> None:
        ws = workspaces["acme-ws"]
        self._write_dynamic(ws.id, "cc-authored", "CC-AUTHORED-BODY")

        await promote_dynamic_skill_route(workspace_id=ws.id, dir_name="cc-authored", _current_user=acme_admin())

        assert (skill_scope.tenant_library_dir(ACME_TENANT_ID) / "cc-authored" / "SKILL.md").is_file()
        assert not (Path(library) / "skills" / "local" / "cc-authored").exists()
        # 晋升完立刻在自己的库里读得到，别的租户读不到
        assert "CC-AUTHORED-BODY" in (
            await get_skill_content(path="cc-authored", _current_user=acme_admin())
        ).readme
        with pytest.raises(NotFoundError):
            await get_skill_content(path="cc-authored", _current_user=rival_admin())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_platform_promotion_positive_control(self, workspaces, library, identity) -> None:
        """同一个端点、同一组实参，平台方晋升的落点还是 `local/`"""
        ws = workspaces["platform-ws"]
        self._write_dynamic(ws.id, "plat-authored", "PLAT-AUTHORED-BODY")

        await promote_dynamic_skill_route(workspace_id=ws.id, dir_name="plat-authored", _current_user=identity())

        assert (Path(library) / "skills" / "local" / "plat-authored" / "SKILL.md").is_file()
        assert not (skill_scope.tenant_library_dir(ACME_TENANT_ID) / "plat-authored").exists()
        # 平台方新建的技能默认私有
        with pytest.raises(NotFoundError):
            await get_skill_content(path="plat-authored", _current_user=acme_admin())

    @pytest.mark.asyncio
    async def test_promoting_out_of_another_tenants_workspace_is_404(self, workspaces) -> None:
        ws = workspaces["rival-ws"]
        self._write_dynamic(ws.id, "rival-authored", "RIVAL-AUTHORED-BODY")
        with pytest.raises(NotFoundError):
            await promote_dynamic_skill_route(
                workspace_id=ws.id, dir_name="rival-authored", _current_user=acme_admin()
            )
        assert not (skill_scope.tenant_library_dir(ACME_TENANT_ID) / "rival-authored").exists()


class TestPromotionCannotSmuggleContentThroughASymlink:
    """晋升的源目录是**容器可写的**，所以它不是可信输入

    `writable_root()` 只保证「写到哪儿去」，它对「从哪儿读」一个字都没说。
    `shutil.copytree` 默认 `symlinks=False`，也就是**解引用**：动态 skill 目录里
    一条指向 `{DATA_DIR}/skills/local` 的软链，晋升之后就变成租户库里一份
    平台方私有技能的**真实副本**——之后 `GET /skills/file` 逐字读得到，
    `sync_skills` 还会把它复制进这个租户的每一个工作区。

    软链是谁放进去的：`POST /workspaces/{id}/comm/send`（`Role.Admin` + 工作区作用域）
    让容器里的 CC 跑一句 `ln -s`；相对目标在宿主上照样解析得到。
    指向 `../../{别的工作区}/.claude/settings.json` 时拿到的就是平台方的模型凭据。

    因此晋升必须在复制**之前**拒绝带软链的源目录，而不是指望落点判定兜住。
    """

    @staticmethod
    def _write_dynamic(workspace_id: int, dir_name: str, body: str) -> Path:
        WorkspaceService.write_dynamic_skill(
            workspace_id, dir_name, f"---\nname: {dir_name}\ndescription: d\n---\n{body}\n"
        )
        return WorkspaceService.get_workspace_skills_dir(workspace_id) / dir_name

    @staticmethod
    def _symlink_or_skip(link: Path, target: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except (OSError, NotImplementedError):
            pytest.skip("当前环境不允许创建符号链接")

    @pytest.mark.asyncio
    async def test_a_symlinked_subdir_does_not_become_a_real_copy(self, workspaces, library) -> None:
        ws = workspaces["acme-ws"]
        src = self._write_dynamic(ws.id, "cc-authored", "CC-AUTHORED-BODY")
        self._symlink_or_skip(src / "steal", Path(library) / "skills" / "local")

        with pytest.raises(ValidationError):
            await promote_dynamic_skill_route(
                workspace_id=ws.id, dir_name="cc-authored", _current_user=acme_admin()
            )

        promoted = skill_scope.tenant_library_dir(ACME_TENANT_ID) / "cc-authored"
        assert not promoted.exists()
        # 正文这一侧也直接钉一次：不能只信"目录不在"
        with pytest.raises(NotFoundError):
            await get_skill_content(path="cc-authored/steal/ops-runbook", _current_user=acme_admin())

    @pytest.mark.asyncio
    async def test_a_symlinked_file_does_not_become_a_real_copy(self, workspaces, library) -> None:
        """指向单个文件的软链同样会被 `copytree` 解引用成一份真实文件"""
        ws = workspaces["acme-ws"]
        src = self._write_dynamic(ws.id, "cc-authored", "CC-AUTHORED-BODY")
        self._symlink_or_skip(src / "stolen.md", Path(library) / "skills" / "local" / "ops-runbook" / "SKILL.md")

        with pytest.raises(ValidationError):
            await promote_dynamic_skill_route(
                workspace_id=ws.id, dir_name="cc-authored", _current_user=acme_admin()
            )
        assert not (skill_scope.tenant_library_dir(ACME_TENANT_ID) / "cc-authored").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_the_platform_side_is_refused_by_the_same_rule(self, workspaces, library, identity) -> None:
        """判据是「源目录不可信」，不是「谁在调用」

        平台方自己也走同一条：沙盒里的 CC 一样会被提示词注入牵着写软链，
        而 `local/` 里落一份 `.env` 的副本同样是把凭据搬到一个会被同步进容器的地方。
        """
        ws = workspaces["platform-ws"]
        src = self._write_dynamic(ws.id, "plat-authored", "PLAT-AUTHORED-BODY")
        self._symlink_or_skip(src / "steal", Path(library) / "skills" / "local")

        with pytest.raises(ValidationError):
            await promote_dynamic_skill_route(
                workspace_id=ws.id, dir_name="plat-authored", _current_user=identity()
            )
        assert not (Path(library) / "skills" / "local" / "plat-authored").exists()

    @pytest.mark.asyncio
    async def test_a_clean_dynamic_skill_still_promotes(self, workspaces, library) -> None:
        """对照组：没有软链的源目录照常晋升

        少了它，把整个晋升改成"无条件抛异常"也能让上面三条通过。
        """
        ws = workspaces["acme-ws"]
        self._write_dynamic(ws.id, "cc-authored", "CC-AUTHORED-BODY")
        await promote_dynamic_skill_route(
            workspace_id=ws.id, dir_name="cc-authored", _current_user=acme_admin()
        )
        assert (skill_scope.tenant_library_dir(ACME_TENANT_ID) / "cc-authored" / "SKILL.md").is_file()

    def test_the_promotion_target_comes_from_the_writable_root_resolver(self) -> None:
        """结构契约：晋升的落点只能由 `resolve_writable_dir` 算出来

        这一条没法行为化 —— `dir_name` 是单个 URL 路径段，进不来 `/`，
        而 `..` / `.` 开头在上一道 `_is_workspace_local_dynamic_skill` 就被挡了，
        所以「绕开规整器」在当前可达面上与现状等价。
        钉住它是为了让下一次放宽（比如给晋升加一个 `target_name` 参数）
        必须显式改这里，而不是悄悄多出一个写入出口。
        """
        import ast
        import inspect

        source = inspect.getsource(WorkspaceService.promote_dynamic_skill)
        tree = ast.parse(inspect.cleandoc(source))
        assigned_from = {
            node.value.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and any(isinstance(t, ast.Name) and t.id == "dst" for t in node.targets)
        }
        assert assigned_from == {"resolve_writable_dir"}

    def test_the_containment_helper_itself_is_not_vacuous(self, tmp_path) -> None:
        """`contains_symlink` 的确定性用例：不依赖机器是否允许建软链

        目录树里没有软链时必须是 `False` —— 否则上面那些 `ValidationError`
        可能只是因为判据恒为真。
        """
        clean = tmp_path / "clean"
        (clean / "sub").mkdir(parents=True)
        (clean / "sub" / "a.md").write_text("x", encoding="utf-8")
        assert skill_scope.contains_symlink(clean) is False
        assert skill_scope.contains_symlink(tmp_path / "nope") is False


class TestDynamicSkillBrowsingStaysInsideTheSkillDir:
    """晋升不是这个目录的唯一出口：动态 skill 的浏览/读/写三条也吃同一条软链

    这三条端点是 `Role.Admin` + 工作区作用域，租户管理员本来就够得到。
    它们此前只挡字面量 `..`，而目录是容器可写的 —— 一条相对软链在宿主上照样解析，
    于是「读」拿到别的工作区的 `.claude/settings.json`（那里面是该工作区的中继凭据，
    `CC_CREDENTIAL_MODE=direct` 时干脆是真 Key），「写」则把内容落到链接指向的地方。

    判据与 `skill_scope.is_within` 是同一条，不另写一套。
    """

    @staticmethod
    def _prepare(workspace_id: int, target: Path) -> Path:
        WorkspaceService.write_dynamic_skill(
            workspace_id, "cc-authored", "---\nname: cc-authored\ndescription: d\n---\nBODY\n"
        )
        skill_dir = WorkspaceService.get_workspace_skills_dir(workspace_id) / "cc-authored"
        try:
            (skill_dir / "steal").symlink_to(target, target_is_directory=target.is_dir())
        except (OSError, NotImplementedError):
            pytest.skip("当前环境不允许创建符号链接")
        return skill_dir

    @pytest.mark.asyncio
    async def test_reading_through_a_symlink_is_404(self, workspaces, library) -> None:
        ws = workspaces["acme-ws"]
        self._prepare(ws.id, Path(library) / "skills" / "local")
        with pytest.raises(NotFoundError):
            await get_dynamic_skill_file(
                workspace_id=ws.id,
                dir_name="cc-authored",
                rel_path="steal/ops-runbook/SKILL.md",
                _current_user=acme_admin(),
            )

    @pytest.mark.asyncio
    async def test_writing_through_a_symlink_is_refused(self, workspaces, library) -> None:
        ws = workspaces["acme-ws"]
        self._prepare(ws.id, Path(library) / "skills" / "local")
        victim = Path(library) / "skills" / "local" / "ops-runbook" / "SKILL.md"
        before = victim.read_text(encoding="utf-8")
        with pytest.raises(ValidationError):
            await save_dynamic_skill_file(
                workspace_id=ws.id,
                dir_name="cc-authored",
                body=DynamicSkillFileBody(rel_path="steal/ops-runbook/SKILL.md", content="OWNED"),
                _current_user=acme_admin(),
            )
        assert victim.read_text(encoding="utf-8") == before

    @pytest.mark.asyncio
    async def test_listing_does_not_walk_through_a_symlink(self, workspaces, library) -> None:
        ws = workspaces["acme-ws"]
        self._prepare(ws.id, Path(library) / "skills" / "local")
        entries = (
            await list_dynamic_skill_dir(workspace_id=ws.id, dir_name="cc-authored", _current_user=acme_admin())
        ).entries
        names = {entry.rel_path.replace("\\", "/") for entry in entries}
        # 目录名本身也是不该给的：列出来就等于告诉租户平台方库里有哪些技能
        assert not any(name.startswith("steal") for name in names)
        # 对照组：同一次调用里，skill 自己的文件照常列得出来
        assert "SKILL.md" in names

    @pytest.mark.asyncio
    async def test_a_symlinked_skill_dir_is_not_a_dynamic_skill_at_all(self, workspaces, library) -> None:
        """整个 skill 目录就是一条软链时，`is_dir()` 会跟着走 —— 三条端点一起中招"""
        ws = workspaces["acme-ws"]
        skills_home = WorkspaceService.get_workspace_skills_dir(ws.id)
        skills_home.mkdir(parents=True, exist_ok=True)
        try:
            (skills_home / "cc-authored").symlink_to(
                Path(library) / "skills" / "local" / "ops-runbook", target_is_directory=True
            )
        except (OSError, NotImplementedError):
            pytest.skip("当前环境不允许创建符号链接")
        with pytest.raises(NotFoundError):
            await get_dynamic_skill_file(
                workspace_id=ws.id, dir_name="cc-authored", rel_path="SKILL.md", _current_user=acme_admin()
            )
        with pytest.raises(NotFoundError):
            await list_dynamic_skill_dir(
                workspace_id=ws.id, dir_name="cc-authored", _current_user=acme_admin()
            )

    @pytest.mark.asyncio
    async def test_a_clean_dynamic_skill_is_still_readable_and_writable(self, workspaces) -> None:
        """对照组：没有软链时三条端点照常工作

        少了它，把三条全改成无条件抛异常也能让上面通过。
        """
        ws = workspaces["acme-ws"]
        WorkspaceService.write_dynamic_skill(
            ws.id, "cc-authored", "---\nname: cc-authored\ndescription: d\n---\nBODY\n"
        )
        await save_dynamic_skill_file(
            workspace_id=ws.id,
            dir_name="cc-authored",
            body=DynamicSkillFileBody(rel_path="notes/todo.md", content="HELLO"),
            _current_user=acme_admin(),
        )
        got = await get_dynamic_skill_file(
            workspace_id=ws.id, dir_name="cc-authored", rel_path="notes/todo.md", _current_user=acme_admin()
        )
        assert got.content == "HELLO"


class TestResolveTimeContainmentIsTheSecondLayer:
    """`_within` 是字面量检查之外的**第二层**

    `normalize_rel` 挡的是字面量越界（`..`、盘符、反斜杠），它挡不住软链：
    租户库里一条指向平台方库的软链，每一段都是合法名字。所以 `_within` 必须
    在 resolve **之后**再判一次归属。

    两条用例分工：真软链那条在支持软链的机器上跑；下面那条把第一层换成放行版本，
    在任何平台上都确定性地证明第二层单独成立 —— 少了它，"两层防御"就只是注释里的话。
    """

    @pytest.mark.asyncio
    async def test_a_symlink_out_of_the_tenant_library_is_refused(self, library) -> None:
        link = skill_scope.tenant_library_dir(ACME_TENANT_ID) / "sneaky"
        try:
            link.symlink_to(Path(library) / "skills" / "local" / "ops-runbook", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("当前环境不允许创建符号链接")
        with pytest.raises(NotFoundError):
            await get_skill_content(path="sneaky", _current_user=acme_admin())

    @pytest.mark.parametrize(
        "rel",
        [
            "..",
            "../local",
            "a/../../b",
            "./a",
            ".hidden",
            "a/.git/config",
            # 绝对路径这一档不在这里：前导 `/` 被剥掉后当相对路径处理（见下面的对照组），
            # 拼在根下仍然是库内路径。真正致命的是盘符与反斜杠
            "C:/windows",
            "a\\b",
            "a\x00b",
            "",
            "   ",
            "/",
        ],
    )
    def test_the_literal_layer_rejects_these_outright(self, rel: str) -> None:
        """第一层自己也要有用例：它是 `POST /skills`（上传）那条路上**唯一**的名字判据

        上传拿到的顶层目录名会直接拼进落盘路径，再 `rmtree`。这一层放宽一个字符，
        删除就落在库根之外 —— 所以现在那里还压了一道 `is_within`（见下一条），
        两层各自都得有自己的红。
        """
        assert skill_scope.normalize_rel(rel) is None

    @pytest.mark.parametrize("rel", ["a", "a/b", "a/b/c.md", " a/b ", "/a/b/"])
    def test_the_literal_layer_still_lets_normal_paths_through(self, rel: str) -> None:
        """对照组：少了它，把 `normalize_rel` 改成恒 `None` 也能让上面全绿"""
        assert skill_scope.normalize_rel(rel) is not None

    @pytest.mark.asyncio
    async def test_upload_has_its_own_containment_layer(self, library, monkeypatch) -> None:
        """把第一层换成"什么都放行"，上传那条路上的 `rmtree` 必须自己拦住

        与下面 `test_containment_holds_even_if_the_literal_check_is_bypassed` 同一个手法：
        这不是假场景 —— `normalize_rel` 是共用的，任何一次放宽都会同时作用到这条路上。
        """
        monkeypatch.setattr(
            skill_scope, "normalize_rel", lambda rel: [seg for seg in rel.strip("/").split("/") if seg]
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            # 不带 frontmatter 的 `name:`：否则一致性校验会先抛，
            # 这条用例就变成在测那个校验，而不是在测落盘路径的归属
            zf.writestr("../SKILL.md", "hello\n")
        buffer.seek(0)

        tenants_root = skill_scope.tenant_library_dir(ACME_TENANT_ID).parent
        with pytest.raises(ValidationError):
            await upload_skill(
                file=UploadFile(filename="evil.zip", file=buffer),
                _current_user=acme_admin(),
            )
        # 别的租户那一层没有被 `rmtree` 掉
        assert (tenants_root / f"t{RIVAL_TENANT_ID}" / "rival-notes").is_dir()

    @pytest.mark.asyncio
    async def test_a_symlink_out_of_the_root_is_neither_listed_nor_walked(self, library) -> None:
        """`_resolve` 拒绝**读**它，目录树与目录列举也不能把它的内容列出来

        「列得出、点进去 404」不是一个可以接受的中间状态：目录名与文件名本身
        就是这一层要挡的东西（租户据此就知道平台方库里有哪些技能）。
        判据必须与 `_resolve` 用**同一个根**，否则两侧会给出互相矛盾的答案。
        """
        tenant_root = skill_scope.tenant_library_dir(ACME_TENANT_ID)
        try:
            (tenant_root / "leak").symlink_to(Path(library) / "skills" / "local", target_is_directory=True)
            (tenant_root / "acme-notes" / "inner").symlink_to(
                Path(library) / "skills" / "local", target_is_directory=True
            )
        except (OSError, NotImplementedError):
            pytest.skip("当前环境不允许创建符号链接")

        tree = {node.name for node in (await get_skills_tree(_current_user=acme_admin())).nodes}
        assert "leak" not in tree
        # 对照组：同一次调用里，库内正常的那两条照样在
        assert {"acme-notes", "shared-guide"} <= tree

        entries = (await list_skill_dir(name="acme-notes", source="user", _current_user=acme_admin())).entries
        listed = {entry.rel_path.replace("\\", "/") for entry in entries}
        assert not any(name.startswith("inner") for name in listed)
        assert EXTRA_DOC in listed

    def test_containment_holds_even_if_the_literal_check_is_bypassed(self, library, monkeypatch) -> None:
        """把第一层换成"什么都放行"，第二层必须自己拦住

        这不是在测一个假场景：软链、junction、以及将来任何一次对 `normalize_rel`
        的放宽，走的都是这条路。
        """
        monkeypatch.setattr(
            skill_scope, "normalize_rel", lambda rel: [seg for seg in rel.strip("/").split("/") if seg]
        )
        assert skill_scope.resolve_dir(ACME_TENANT_ID, "../../local/ops-runbook") is None
        assert skill_scope.resolve_writable_dir(ACME_TENANT_ID, "../../local/ops-runbook/SKILL.md") is None
        # 同一次调用里，库内的正常路径仍然解析得到 —— 证明拦住的是越界而不是全部
        assert skill_scope.resolve_dir(ACME_TENANT_ID, "acme-notes") is not None


class TestSingleSkillSyncIsScopedToo:
    """`_resolve_skill_source` 的**第二个调用点**

    `sync_skills` 那一条被 `TestWorkspaceSyncIsTheLastGate` 钉住了，
    但 `POST /workspaces/{id}/skills/{name}/sync`（`Role.Admin` + 工作区作用域）
    走的是 `sync_single_skill`，它自己也要解析一次。

    这条路是完整可达的：`PUT /workspaces/{id}/skills` 原样收下任意字符串（`sync_skills`
    只是不复制，并不把它从选中列表里删掉），随后 `sync_single_skill` 的
    `skill_id not in selected_skills` 这道门就开着了。
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", CASE_PARAMS)
    async def test_single_sync_refuses_what_the_workspace_cannot_see(self, workspaces, case: Case) -> None:
        ws = workspaces["acme-ws"]
        ws.metadata = {"skills": [case.skill_id]}
        await ws.save(update_fields=["metadata"])

        ok = await WorkspaceService.sync_single_skill(ws, case.skill_id)
        assert ok is case.visible_to_acme

        target = WorkspaceService.get_workspace_skills_dir(ws.id) / case.skill_id.split("/")[-1]
        assert target.is_dir() is case.visible_to_acme
        if case.visible_to_acme:
            assert case.body in (target / "SKILL.md").read_text(encoding="utf-8")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", CASE_PARAMS)
    async def test_platform_workspace_positive_control(self, workspaces, case: Case) -> None:
        """同一个函数、同一组实参：平台方的工作区拿得到平台方的每一档"""
        if case.case_id not in PLATFORM_VISIBLE:
            pytest.skip("租户自有技能不在平台方工作区的可读根里")
        ws = workspaces["platform-ws"]
        ws.metadata = {"skills": [case.skill_id]}
        await ws.save(update_fields=["metadata"])
        assert await WorkspaceService.sync_single_skill(ws, case.skill_id) is True


class TestMainAgentExitIsScopedToo:
    """主 Agent 那条出口（`plugins/builtin/skill_loader`）用的是同一份判定

    索引提示词与 `load_skill()` 都过 `WorkspaceService.list_all_skills(scope)`，
    作用域由 `channel_skill_scope` 从会话频道推出来。
    """

    @pytest.mark.asyncio
    async def test_channel_scope_comes_from_the_bound_instance(self, workspaces) -> None:
        assert await skill_scope.channel_skill_scope("onebot_v11-inst-acme-group_1") == ACME_TENANT_ID
        assert await skill_scope.channel_skill_scope("onebot_v11-inst-rival-group_1") == RIVAL_TENANT_ID
        # 不存在的频道按平台方处理（未认领实例恒归平台方）
        assert await skill_scope.channel_skill_scope("onebot_v11-nobody-group_9") is None

    @pytest.mark.asyncio
    async def test_the_agent_visible_set_is_the_same_as_the_console_one(self, workspaces) -> None:
        scope = await skill_scope.channel_skill_scope("onebot_v11-inst-acme-group_1")
        agent_side = {skill["name"] for skill in WorkspaceService.list_all_skills(scope)}
        console_side = {item.name for item in (await list_all_skills(_current_user=acme_admin())).items}
        assert agent_side == console_side
        assert "ops-runbook" not in agent_side


class TestTheAgentSidePlumbingIsReallyWiredIn:
    """零件对不等于接线对：这一组**真的驱动**插件注册的那几个函数

    `TestMainAgentExitIsScopedToo` 断言的是 `channel_skill_scope` 与
    `list_all_skills(scope)` 这两个零件。把 `skill_loader` 里
    `_session_skill_scope` 的函数体换成 `return None`（也就是改回"不带作用域"），
    那两条照样全绿 —— 因为它们压根没走这个插件。
    见 `PROJECT_STATE §2.7`：接线用例要按调用点一个一个数。

    这里覆盖三个调用点：提示词索引、`list_skills()`、`load_skill()`。
    """

    @staticmethod
    def _ctx(chat_key: str) -> Any:
        from nekro_agent.api.schemas import AgentCtx

        return AgentCtx(from_chat_key=chat_key, channel_id="group_1", adapter_key="onebot_v11")

    ACME_CHAT_KEY = "onebot_v11-inst-acme-group_1"

    @pytest.mark.asyncio
    async def test_the_injected_index_only_lists_the_channels_own_scope(self, workspaces) -> None:
        from plugins.builtin.skill_loader.main import skill_loader_prompt

        prompt = await skill_loader_prompt(self._ctx(self.ACME_CHAT_KEY))
        assert "acme-notes" in prompt
        assert "shared-guide" in prompt
        assert "ops-runbook" not in prompt
        assert "rival-notes" not in prompt

    @pytest.mark.asyncio
    async def test_the_platform_channel_positive_control(self, workspaces) -> None:
        """同一个函数、只换频道：未认领实例的频道按平台方处理，看得到 `ops-runbook`"""
        from plugins.builtin.skill_loader.main import skill_loader_prompt

        prompt = await skill_loader_prompt(self._ctx("onebot_v11-nobody-group_9"))
        assert "ops-runbook" in prompt
        assert "acme-notes" not in prompt

    @pytest.mark.asyncio
    async def test_list_skills_method_is_scoped(self, workspaces) -> None:
        from plugins.builtin.skill_loader.main import list_skills as agent_list_skills

        listed = await agent_list_skills(self._ctx(self.ACME_CHAT_KEY))
        assert "acme-notes" in listed
        assert "ops-runbook" not in listed

    @pytest.mark.asyncio
    async def test_load_skill_refuses_a_skill_outside_the_channel_scope(self, workspaces) -> None:
        from plugins.builtin.skill_loader.main import load_skill

        # 正向对照：自己作用域里的那条读得到正文
        body = await load_skill(self._ctx(self.ACME_CHAT_KEY), "acme-notes")
        assert "ACME-BODY" in body

        with pytest.raises(ValueError):
            await load_skill(self._ctx(self.ACME_CHAT_KEY), "ops-runbook")
        with pytest.raises(ValueError):
            await load_skill(self._ctx(self.ACME_CHAT_KEY), "rival-notes")

    @pytest.mark.asyncio
    async def test_a_new_workspace_created_by_the_bot_gets_the_channels_own_defaults(self, workspaces) -> None:
        """`cc_workspace.create_and_bind_workspace` 是自动注入清单的第二个消费点

        它按**发起频道**建工作区，却在改前取平台方那份清单 ——
        平台方私有技能就这样被预装进租户的沙盒目录。
        """
        from plugins.builtin.cc_workspace.main import create_and_bind_workspace

        # 两份清单必须**不同**：都用「平台方那份裁剪后的子集」的话，下游那次
        # `list_all_skills(scope)` 过滤会把两条路径的结果重新拉平，
        # 于是把这里改回 `resolve_auto_inject_skills(None)` 一条测试都不红
        await update_auto_inject_skills(
            AutoInjectSkillsUpdate(skills=["ops-runbook", "shared-guide"]), _current_user=platform_owner()
        )
        await update_auto_inject_skills(
            AutoInjectSkillsUpdate(skills=["acme-notes"]), _current_user=acme_admin()
        )
        await DBChatChannel.create(
            chat_key="onebot_v11-inst-acme-group_7",
            channel_id="group_7",
            channel_name="fresh",
            channel_type="group",
            adapter_key="onebot_v11",
            instance_key="inst-acme",
            data="{}",
        )
        await create_and_bind_workspace(self._ctx("onebot_v11-inst-acme-group_7"), "acme-fresh")

        ws = await DBWorkspace.get(name="acme-fresh")
        assert ws.metadata.get("skills") == ["acme-notes"]

    @pytest.mark.asyncio
    async def test_a_platform_channel_positive_control_for_workspace_creation(self, workspaces) -> None:
        """同一个函数、只换频道：平台方频道照旧拿到平台方那份，逐字不变"""
        from plugins.builtin.cc_workspace.main import create_and_bind_workspace

        await update_auto_inject_skills(
            AutoInjectSkillsUpdate(skills=["ops-runbook", "shared-guide"]), _current_user=platform_owner()
        )
        await DBChatChannel.create(
            chat_key="onebot_v11-nobody-group_8",
            channel_id="group_8",
            channel_name="platform-fresh",
            channel_type="group",
            adapter_key="onebot_v11",
            instance_key="nobody",
            data="{}",
        )
        await create_and_bind_workspace(self._ctx("onebot_v11-nobody-group_8"), "plat-fresh")

        ws = await DBWorkspace.get(name="plat-fresh")
        assert ws.metadata.get("skills") == ["ops-runbook", "shared-guide"]


# ---------------------------------------------------------------------------
# 反向看门人
# ---------------------------------------------------------------------------


class TestEveryLiveSkillEndpointHasACase:
    def test_every_path_taking_read_endpoint_is_covered(self) -> None:
        """路由表里有、这份用例表里没有，直接红

        「一族端点里漏了一个」正是这个洞每次的形态，所以覆盖关系要反过来钉。
        """
        live = {
            (sorted(getattr(route, "methods", None) or ["WS"])[0], route.path)
            for route in skills_router.routes
        }
        covered_by_readers = {
            ("GET", "/skills/content"),
            ("GET", "/skills/file"),
            ("GET", "/skills/dir"),
            ("GET", "/skills/{name}/readme"),
        }
        assert covered_by_readers <= live

    def test_the_readable_roots_order_is_the_one_the_listing_uses(self) -> None:
        """列表按 A 顺序去重、解析按 B 顺序命中 == 界面写着内置、同步进去的是别人那份"""
        assert [root.ownership for root in skill_scope.readable_roots(None)] == [
            SkillOwnership.BUILTIN,
            SkillOwnership.PLATFORM,
            SkillOwnership.PLATFORM,
        ]
        assert [root.ownership for root in skill_scope.readable_roots(ACME_TENANT_ID)][0] is SkillOwnership.TENANT

    def test_writable_root_is_the_only_write_exit(self) -> None:
        assert skill_scope.writable_root(None) == skill_scope.platform_local_dir()
        assert skill_scope.writable_root(ACME_TENANT_ID) == skill_scope.tenant_library_dir(ACME_TENANT_ID)
        assert skill_scope.tenant_library_dir(ACME_TENANT_ID) != skill_scope.tenant_library_dir(RIVAL_TENANT_ID)
