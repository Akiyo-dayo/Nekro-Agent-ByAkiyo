"""技能库路由（平台方库 / 租户自有库）

## 技能是可执行内容，也是可泄露内容

技能会被同步进工作区、由容器里的 Claude Code 当作指令与脚本读取执行。
写进技能库 == 写进服务端可信代码路径；同时技能正文是**给 AI 看的工作流文本**，
平台方很可能往里面写运维流程、内部地址、排障步骤。

改前这两件事各错了一半：

- 全部写操作 `Role.Super` —— 租户一个字也写不了，`需求文档 §R8`
  「可以自己制作并使用 skills」对定制版客户不成立。
- 全部读操作 `Role.Admin` 且**不带任何过滤** —— 而租户管理员正好就是 `Role.Admin`
  （`TENANT_MAX_PERM_LEVEL`）。`GET /skills/tree` 拿目录、`GET /skills/content` 拿正文，
  两跳读走平台方整座库。

## 现在的划分（判定全部收在 `services/skill_scope.py`）

| 归属 | 目录 | 读 | 写 |
|---|---|---|---|
| 内置 | 源码树 `builtin_skills/` | 所有人 | 没有人 |
| 平台方 | `{DATA_DIR}/skills/local`、`/repos` | 平台方；`shared` 的对全部租户可读 | `Role.Super` |
| 租户 | `{DATA_DIR}/skills/tenants/t{id}` | 该租户 | 该租户管理员（`Role.Admin` + 作用域） |

作用域**不出现在 URL 里**，一律由登录身份推出来（`skill_scope.skill_scope_of`）：
让调用方自己传 `tenant_id` 就等于多一个必须在每个端点上校验的越权入口。
写操作的落点只有 `skill_scope.writable_root()` 一个出口，因此租户的写入**结构上**
到不了平台方的目录，而不是靠每个端点记得判一次。

不可见与不存在对外**不可区分**（都是 404）：区分开来，租户就能用试错枚举出
平台方库里有哪些技能。

## 三条留在 `Role.Super` 的写操作，是能力边界而不是归属问题

`POST /skills/clone`、`POST /skills/pull` 与 `/skills/repos/*` 一族仍是平台方专属，
因此返回 **403**（能力边界）而不是 404：

git 拉取是**由宿主上的 `git` 进程发起的任意 URL 出站请求**。`file://` 能把宿主上
任意可读目录复制进技能库，`ext::` 传输干脆是「让 git 执行一条命令」，
`http://127.0.0.1:8021/...` 则是拿 NA 自己的网络身份打内网 —— 这与
`model_group_service.assert_outbound_urls_allowed` 拦租户填内网 base_url 是同一条线，
只是 git 这一侧没有等价的 URL 白名单可做。租户自制技能走编辑器与 zip 上传已经够用。
"""

import asyncio
import io
import json
import shutil
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, File, Query, UploadFile
from pydantic import BaseModel

from nekro_agent.core.logger import logger
from nekro_agent.core.os_env import SKILLS_REPOS_DIR
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import (
    AppFileNotFoundError,
    InvalidFileTypeError,
    NotFoundError,
    ValidationError,
)
from nekro_agent.schemas.workspace import AutoInjectSkillsResponse, AutoInjectSkillsUpdate, SkillItem
from nekro_agent.services import skill_scope
from nekro_agent.services.skill_scope import (
    LOCAL_KIND,
    REPOS_KIND,
    SkillOwnership,
    SkillRoot,
    SkillVisibility,
    skill_scope_of,
)
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.workspace.manager import WorkspaceService

router = APIRouter(prefix="/skills", tags=["Skills"])


# ─────────────────────────────────────────────────────────────
# 响应模型
# ─────────────────────────────────────────────────────────────


class SkillListResponse(BaseModel):
    total: int
    items: List[SkillItem]


class AllSkillItem(BaseModel):
    name: str
    display_name: str
    description: str
    source: Literal["builtin", "user", "repo"]
    repo_name: Optional[str] = None
    # 归属与可写性由后端判定后下发，前端不自行推断（与模型组的 `editable` 同一约定）
    ownership: Literal["builtin", "platform", "tenant"] = "platform"
    editable: bool = False


class AllSkillsResponse(BaseModel):
    total: int
    items: List[AllSkillItem]


class SkillReadmeResponse(BaseModel):
    readme: str


class ActionOkResponse(BaseModel):
    ok: bool = True


class SkillUploadResponse(BaseModel):
    name: str
    ok: bool = True


class SkillTreeNode(BaseModel):
    """技能库目录树节点"""

    name: str
    path: str  # 相对于所属根目录的路径
    type: Literal["skill", "repo", "dir", "doc"]
    skill_name: Optional[str] = None
    skill_description: Optional[str] = None
    has_git: bool = False
    repo_url: Optional[str] = None
    repo_branch: Optional[str] = None
    ownership: Literal["builtin", "platform", "tenant"] = "platform"
    editable: bool = False
    # 仅平台方视角有值：该条目对租户是 shared 还是 private
    visibility: Optional[Literal["shared", "private"]] = None
    children: Optional[List["SkillTreeNode"]] = None


SkillTreeNode.model_rebuild()


class SkillTreeResponse(BaseModel):
    nodes: List[SkillTreeNode]


class SkillDirEntry(BaseModel):
    """skill 目录内的单个条目"""

    name: str
    rel_path: str  # 相对于 skill 根目录
    type: Literal["file", "dir"]
    size: Optional[int] = None  # 文件大小（字节），仅文件有效


class SkillDirResponse(BaseModel):
    entries: List[SkillDirEntry]


class SkillFileSaveRequest(BaseModel):
    path: str  # 相对于可写库的文件路径
    content: str


class CloneRequest(BaseModel):
    repo_url: str
    target_dir: str  # 顶层目录名（不含路径分隔符）


class CloneResponse(BaseModel):
    path: str
    ok: bool = True


class PullRequest(BaseModel):
    path: str  # 相对于 SKILLS_DIR 的路径


class PullResponse(BaseModel):
    ok: bool = True
    output: str = ""


class VisibilityUpdateRequest(BaseModel):
    """把平台方的某个条目共享给全部租户 / 收回"""

    kind: Literal["local", "repos"]
    name: str
    visibility: Literal["shared", "private"]


# ── 仓库订阅 ──

class RepoSubscribeRequest(BaseModel):
    repo_url: str
    repo_name: str  # 本地目录名


class RepoMeta(BaseModel):
    repo_name: str
    repo_url: Optional[str] = None
    repo_branch: Optional[str] = None
    subscribed_at: Optional[str] = None
    skill_count: int = 0
    visibility: Optional[Literal["shared", "private"]] = None


class RepoListResponse(BaseModel):
    repos: List[RepoMeta]


# ── 同步到工作区 ──

class SyncToWorkspacesRequest(BaseModel):
    skill_id: str


class SyncToWorkspacesResponse(BaseModel):
    ok: bool = True
    synced_count: int = 0


# ─────────────────────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────────────────────


_SKILL_MAIN_FILES = frozenset({"SKILL.md", "README.md", "README.txt", "readme.md", "readme.txt", "README"})


def _list_skill_extra_docs(skill_dir: Path, skill_rel_path: str, root: SkillRoot) -> List[SkillTreeNode]:
    """扫描 skill 目录内除主文件以外的 .md / .txt 文档，返回 doc 类型节点列表。"""
    docs: List[SkillTreeNode] = []
    try:
        for f in sorted(skill_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in (".md", ".txt") and f.name not in _SKILL_MAIN_FILES:
                docs.append(
                    SkillTreeNode(
                        name=f.name,
                        path=f"{skill_rel_path}/{f.name}",
                        type="doc",
                        has_git=False,
                        ownership=root.ownership.value,  # type: ignore[arg-type]
                        editable=root.editable,
                    )
                )
    except Exception:
        pass
    return docs


def _parse_skill_meta(skill_dir: Path) -> Optional[Dict[str, str]]:
    """从 SKILL.md frontmatter 读取 skill 元信息，不存在时返回 None。"""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None
    try:
        content = skill_md.read_text(encoding="utf-8")
        meta: Dict[str, str] = {}
        lines = content.splitlines()
        in_frontmatter = False
        for i, line in enumerate(lines):
            if i == 0 and line.strip() == "---":
                in_frontmatter = True
                continue
            if in_frontmatter and line.strip() == "---":
                break
            if in_frontmatter and ":" in line and not line.startswith("#"):
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip().strip('"').strip("'")
        return {
            "name": meta.get("name", skill_dir.name),
            "description": meta.get("description", ""),
        }
    except Exception as e:
        logger.warning(f"读取 SKILL.md 失败: {skill_dir.name}: {e}")
        return None


def _parse_frontmatter_name(content: str) -> Optional[str]:
    """从 SKILL.md 内容中提取 frontmatter 的 name 字段，不存在返回 None。"""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("name:") or line.startswith("name :"):
            _, _, value = line.partition(":")
            return value.strip().strip('"').strip("'") or None
    return None


async def _get_git_info(repo_dir: Path) -> tuple:
    """获取 git 仓库的远程 URL 和当前分支"""
    repo_url: Optional[str] = None
    repo_branch: Optional[str] = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "remote",
            "get-url",
            "origin",
            cwd=str(repo_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            repo_url = stdout.decode(errors="replace").strip()
    except Exception:
        pass
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "branch",
            "--show-current",
            cwd=str(repo_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            repo_branch = stdout.decode(errors="replace").strip()
    except Exception:
        pass
    return repo_url, repo_branch


def _scan_directory_sync(
    directory: Path,
    base: Path,
    root: SkillRoot,
    scope: Optional[int],
    max_depth: int = 4,
    *,
    top_level: bool = True,
) -> List[SkillTreeNode]:
    """递归扫描目录，构建技能树（同步）

    `top_level` 那一层要过一次可见性：`local/` 的顶层是技能目录、`repos/` 的顶层是仓库目录，
    正好就是可见性登记表的两族键。
    """
    nodes: List[SkillTreeNode] = []
    if max_depth <= 0:
        return nodes
    try:
        entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except (PermissionError, FileNotFoundError):
        return nodes
    for entry in entries:
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        # 软链指出根外时不往下走：`_resolve` 那一层已经拒绝读它，树里却照样画出来
        # 就是「列得出、点进去 404」，而且列出来的目录名本身就是不该给的东西
        if entry.is_symlink() and not skill_scope.is_within(root.base, entry):
            continue
        if top_level and not skill_scope.is_entry_visible(root, entry.name, scope):
            continue
        rel_path = str(entry.relative_to(base)).replace("\\", "/")
        has_git = (entry / ".git").is_dir()
        skill_meta = _parse_skill_meta(entry)
        visibility = (
            skill_scope.get_visibility(root.kind, entry.name).value
            if top_level and scope is None and root.kind is not None
            else None
        )
        if skill_meta is not None:
            extra_docs = _list_skill_extra_docs(entry, rel_path, root)
            nodes.append(
                SkillTreeNode(
                    name=entry.name,
                    path=rel_path,
                    type="skill",
                    skill_name=skill_meta["name"],
                    skill_description=skill_meta["description"],
                    has_git=has_git,
                    ownership=root.ownership.value,  # type: ignore[arg-type]
                    editable=root.editable,
                    visibility=visibility,  # type: ignore[arg-type]
                    children=extra_docs if extra_docs else None,
                )
            )
        elif has_git:
            children = _scan_directory_sync(entry, base, root, scope, max_depth - 1, top_level=False)
            nodes.append(
                SkillTreeNode(
                    name=entry.name,
                    path=rel_path,
                    type="repo",
                    has_git=True,
                    ownership=root.ownership.value,  # type: ignore[arg-type]
                    editable=root.editable,
                    visibility=visibility,  # type: ignore[arg-type]
                    children=children if children else None,
                )
            )
        else:
            children = _scan_directory_sync(entry, base, root, scope, max_depth - 1, top_level=False)
            if children:
                nodes.append(
                    SkillTreeNode(
                        name=entry.name,
                        path=rel_path,
                        type="dir",
                        has_git=False,
                        ownership=root.ownership.value,  # type: ignore[arg-type]
                        editable=root.editable,
                        visibility=visibility,  # type: ignore[arg-type]
                        children=children,
                    )
                )
    return nodes


def _list_dir_entries(
    root: Path,
    current: Path,
    max_depth: int = 5,
    *,
    containment_base: Optional[Path] = None,
) -> List[SkillDirEntry]:
    """递归列出目录内所有文件和子目录（跳过 .git 等隐藏目录）

    Args:
        containment_base: 传了就按它做一次越界判定 —— 必须与 `_resolve` 用的**同一个**
            根（技能根，不是 skill 自己的目录），否则会出现「这个文件读得到却列不出来」。
            软链指到根外时既不列出也不往下走：`GET /skills/file` 读它是 404，
            而目录列举把里面的文件名交出去，就是同一件事漏了另一半。
    """
    entries: List[SkillDirEntry] = []
    if max_depth <= 0:
        return entries
    try:
        items = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return entries
    for item in items:
        if item.name.startswith("."):
            continue
        if containment_base is not None and item.is_symlink() and not skill_scope.is_within(containment_base, item):
            continue
        rel = str(item.relative_to(root)).replace("\\", "/")
        if item.is_dir():
            entries.append(SkillDirEntry(name=item.name, rel_path=rel, type="dir"))
            entries.extend(_list_dir_entries(root, item, max_depth - 1, containment_base=containment_base))
        elif item.is_file():
            try:
                size = item.stat().st_size
            except OSError:
                size = None
            entries.append(SkillDirEntry(name=item.name, rel_path=rel, type="file", size=size))
    return entries


async def _build_skill_tree(scope: Optional[int]) -> List[SkillTreeNode]:
    """构建当前作用域可见的技能库树，并填充 git 信息

    只扫「库」这一档（`local/` 与租户自有），内置与订阅仓库各有自己的入口
    （`GET /skills/builtin`、`GET /skills/repos`），与改造前一致。
    """
    nodes: List[SkillTreeNode] = []
    for root in skill_scope.readable_roots(scope):
        if root.ownership is SkillOwnership.BUILTIN or root.kind == REPOS_KIND:
            continue
        root.base.mkdir(parents=True, exist_ok=True)
        scanned = await asyncio.to_thread(_scan_directory_sync, root.base, root.base, root, scope)

        async def fill_git_info(node: SkillTreeNode, base: Path = root.base) -> None:
            if node.type == "repo" and node.has_git:
                repo_url, repo_branch = await _get_git_info(base / node.path)
                node.repo_url = repo_url
                node.repo_branch = repo_branch
            if node.children:
                await asyncio.gather(*[fill_git_info(child, base) for child in node.children])

        await asyncio.gather(*[fill_git_info(node) for node in scanned])
        nodes.extend(scanned)
    return nodes


def _resolved_or_404(scope: Optional[int], rel: str, *, want_dir: bool) -> skill_scope.ResolvedSkillPath:
    """解析一个可读路径，失败一律 404

    路径非法、不存在、以及"存在但对本作用域不可见"三种情况给出**同一个**响应，
    否则租户可以靠错误码差异枚举平台方库里有哪些技能。
    """
    resolved = (
        skill_scope.resolve_dir(scope, rel) if want_dir else skill_scope.resolve_file(scope, rel)
    )
    if resolved is None:
        raise NotFoundError(resource=f"skill '{rel}'")
    return resolved


def _writable_dir_or_404(scope: Optional[int], name: str) -> Path:
    """把一个技能名解析到当前作用域**可写库**里已存在的目录，否则 404

    删除 / 改写只认可写库：租户传平台方技能的名字，落点是它自己库里的同名目录，
    不存在就是 404 —— 平台方那份既碰不到，也不会因为"删失败"而暴露它存在。
    """
    target = skill_scope.resolve_writable_dir(scope, name)
    if target is None or not target.is_dir():
        raise NotFoundError(resource=f"skill '{name}'")
    return target


# ─────────────────────────────────────────────────────────────
# 路由
# ─────────────────────────────────────────────────────────────


@router.get("/builtin", summary="列出所有内置 skill", response_model=SkillListResponse)
@require_role(Role.Admin)
async def list_builtin_skills(
    _current_user: DBUser = Depends(get_current_active_user),
) -> SkillListResponse:
    """内置技能随源码发布、在公开仓库里就能读到，对所有身份一视同仁（见模块 docstring）"""
    from nekro_agent.core.os_env import BUILTIN_SKILLS_SOURCE_DIR

    raw = WorkspaceService.list_builtin_skills()
    builtin_root = SkillRoot(Path(BUILTIN_SKILLS_SOURCE_DIR), SkillOwnership.BUILTIN, editable=False)
    items = []
    for s in raw:
        skill_dir = Path(BUILTIN_SKILLS_SOURCE_DIR) / s["name"]
        docs = _list_skill_extra_docs(skill_dir, s["name"], builtin_root)
        items.append(
            SkillItem(
                name=s["name"],
                description=s["description"],
                docs=[d.name for d in docs],
                ownership="builtin",
                editable=False,
            )
        )
    return SkillListResponse(total=len(items), items=items)


@router.get("/builtin/{name}", summary="读取内置 skill 内容", response_model=SkillReadmeResponse)
@require_role(Role.Admin)
async def get_builtin_skill_content(
    name: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> SkillReadmeResponse:
    from nekro_agent.core.os_env import BUILTIN_SKILLS_SOURCE_DIR

    skill_dir = Path(BUILTIN_SKILLS_SOURCE_DIR) / name
    if ".." in name or "/" in name or "\\" in name or name.startswith("."):
        raise ValidationError(reason="内置 skill 名称不合法")
    if not skill_dir.exists() or not skill_dir.is_dir():
        raise NotFoundError(resource=f"内置 skill '{name}'")
    for candidate in ("SKILL.md", "README.md", "README.txt", "readme.md", "readme.txt", "README"):
        f = skill_dir / candidate
        if f.exists():
            return SkillReadmeResponse(readme=f.read_text(encoding="utf-8"))
    raise AppFileNotFoundError(filename=f"builtin/{name}/README")


@router.get("/builtin/{name}/doc", summary="读取内置 skill 附属文档", response_model=SkillReadmeResponse)
@require_role(Role.Admin)
async def get_builtin_skill_doc(
    name: str,
    filename: str = Query(..., description="附属文档文件名，如 install.md"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> SkillReadmeResponse:
    from nekro_agent.core.os_env import BUILTIN_SKILLS_SOURCE_DIR

    if filename in _SKILL_MAIN_FILES or ".." in filename or "/" in filename or "\\" in filename:
        raise ValidationError(reason="文件名不合法")
    if ".." in name or "/" in name or "\\" in name or name.startswith("."):
        raise ValidationError(reason="内置 skill 名称不合法")
    skill_dir = Path(BUILTIN_SKILLS_SOURCE_DIR) / name
    if not skill_dir.exists() or not skill_dir.is_dir():
        raise NotFoundError(resource=f"内置 skill '{name}'")
    target = skill_dir / filename
    if not target.exists() or not target.is_file():
        raise NotFoundError(resource=f"builtin/{name}/{filename}")
    return SkillReadmeResponse(readme=target.read_text(encoding="utf-8"))


@router.get("/builtin/{name}/file", summary="读取内置 skill 指定文件", response_model=SkillReadmeResponse)
@require_role(Role.Admin)
async def get_builtin_skill_file(
    name: str,
    path: str = Query(..., description="相对于 skill 目录的文件路径"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> SkillReadmeResponse:
    from nekro_agent.core.os_env import BUILTIN_SKILLS_SOURCE_DIR

    rel = path.strip("/")
    if not rel or ".." in rel.split("/"):
        raise ValidationError(reason="路径不合法")
    if ".." in name or "/" in name or "\\" in name or name.startswith("."):
        raise ValidationError(reason="内置 skill 名称不合法")
    skill_dir = Path(BUILTIN_SKILLS_SOURCE_DIR) / name
    if not skill_dir.exists() or not skill_dir.is_dir():
        raise NotFoundError(resource=f"内置 skill '{name}'")
    target = (skill_dir / rel).resolve()
    if not str(target).startswith(str(skill_dir.resolve())):
        raise ValidationError(reason="路径不合法")
    if not target.exists() or not target.is_file():
        raise NotFoundError(resource=f"builtin/{name}/{rel}")
    return SkillReadmeResponse(readme=target.read_text(encoding="utf-8"))


@router.get("/tree", summary="获取 skill 目录树", response_model=SkillTreeResponse)
@require_role(Role.Admin)
async def get_skills_tree(
    _current_user: DBUser = Depends(get_current_active_user),
) -> SkillTreeResponse:
    nodes = await _build_skill_tree(skill_scope_of(_current_user))
    return SkillTreeResponse(nodes=nodes)


@router.get("/content", summary="读取 skill 内容文件", response_model=SkillReadmeResponse)
@require_role(Role.Admin)
async def get_skill_content(
    path: str = Query(..., description="技能目录路径"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> SkillReadmeResponse:
    resolved = _resolved_or_404(skill_scope_of(_current_user), path, want_dir=True)
    for candidate_name in ("SKILL.md", "README.md", "README.txt", "readme.md", "readme.txt", "README"):
        f = resolved.path / candidate_name
        if f.exists():
            return SkillReadmeResponse(readme=f.read_text(encoding="utf-8"))
    raise AppFileNotFoundError(filename=f"{path}/README")


@router.get("/file", summary="读取 skill 目录内的指定文件", response_model=SkillReadmeResponse)
@require_role(Role.Admin)
async def get_skill_file(
    path: str = Query(..., description="技能目录内的文件路径，如 my-skill/install.md"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> SkillReadmeResponse:
    if len(path.strip("/").split("/")) < 2:
        raise ValidationError(reason="路径不合法")
    resolved = _resolved_or_404(skill_scope_of(_current_user), path, want_dir=False)
    return SkillReadmeResponse(readme=resolved.path.read_text(encoding="utf-8"))


@router.get("/dir", summary="列出 skill 目录内所有文件", response_model=SkillDirResponse)
@require_role(Role.Admin)
async def list_skill_dir(
    name: str = Query(..., description="skill 名称（仓库技能用 repo/skill 格式）"),
    source: Literal["builtin", "user", "repo"] = Query("user", description="skill 来源"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> SkillDirResponse:
    from nekro_agent.core.os_env import BUILTIN_SKILLS_SOURCE_DIR

    scope = skill_scope_of(_current_user)
    if source == "builtin":
        if ".." in name or "/" in name or "\\" in name or name.startswith("."):
            raise ValidationError(reason="内置 skill 名称不合法")
        skill_dir = Path(BUILTIN_SKILLS_SOURCE_DIR) / name
        containment_base = Path(BUILTIN_SKILLS_SOURCE_DIR)
        if not skill_dir.is_dir():
            raise NotFoundError(resource=f"skill '{name}'")
    else:
        # `source` 只用来选族（仓库 vs 独立库），归属与可见性仍由 `skill_scope` 判定：
        # 传 `source=user` 去读一个平台方私有技能，照样 404。
        roots = [
            root
            for root in skill_scope.readable_roots(scope)
            if root.ownership is not SkillOwnership.BUILTIN and (root.kind == REPOS_KIND) == (source == "repo")
        ]
        resolved = skill_scope.resolve_dir(scope, name, roots=roots)
        if resolved is None:
            raise NotFoundError(resource=f"skill '{name}'")
        skill_dir = resolved.path
        containment_base = resolved.root.base

    entries = await asyncio.to_thread(_list_dir_entries, skill_dir, skill_dir, 5, containment_base=containment_base)
    return SkillDirResponse(entries=entries)


@router.put("/file", summary="保存 skill 文件到自己的技能库", response_model=ActionOkResponse)
@require_role(Role.Admin)
async def save_skill_file(
    body: SkillFileSaveRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    scope = skill_scope_of(_current_user)
    rel = body.path.strip("/")
    parts = [segment for segment in rel.split("/") if segment]
    if len(parts) < 2:
        raise ValidationError(reason="路径不合法，需包含 skill 名和文件名")

    target = skill_scope.resolve_writable_dir(scope, rel)
    if target is None:
        raise ValidationError(reason="路径不合法，不允许跳出自己的技能库")

    # 一致性校验：如果保存的是 SKILL.md，检查 frontmatter name 与目录名是否一致
    if parts[-1] == "SKILL.md":
        skill_dir_name = parts[0]
        meta = _parse_frontmatter_name(body.content)
        if meta and meta != skill_dir_name:
            raise ValidationError(
                reason=f"SKILL.md 中的 name '{meta}' 与目录名 '{skill_dir_name}' 不一致，请修正"
            )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body.content, encoding="utf-8")
    return ActionOkResponse(ok=True)


@router.post("/clone", summary="从 git 仓库克隆独立 skill（平台方专属）", response_model=CloneResponse)
@require_role(Role.Super)
async def clone_skill_repo(
    body: CloneRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> CloneResponse:
    """理由见模块 docstring：git 拉取是宿主发起的任意 URL 出站，`ext::` 还能直接执行命令"""
    local_dir = skill_scope.platform_local_dir()
    local_dir.mkdir(parents=True, exist_ok=True)
    target_name = body.target_dir.strip("/").strip()
    if not target_name or "/" in target_name or ".." in target_name or target_name.startswith("."):
        raise ValidationError(reason=f"目标目录名无效: {body.target_dir!r}")
    target_dir = local_dir / target_name
    if target_dir.exists():
        raise ValidationError(reason=f"目录已存在: {target_name}")
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth=1", body.repo_url, str(target_dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            err_msg = stderr.decode(errors="replace").strip()
            raise ValidationError(reason=f"git clone 失败: {err_msg}")
    except ValidationError:
        raise
    except asyncio.TimeoutError as e:
        raise ValidationError(reason="克隆超时（120s）") from e
    except Exception as e:
        raise ValidationError(reason=f"克隆操作失败: {e}") from e

    # 写入 .skill-origin.json
    WorkspaceService._write_skill_origin(target_dir, origin="git", repo_url=body.repo_url)
    return CloneResponse(path=target_name, ok=True)


@router.post("/pull", summary="更新 git 仓库（平台方专属）", response_model=PullResponse)
@require_role(Role.Super)
async def pull_skill_repo(
    body: PullRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> PullResponse:
    rel_path = body.path.strip("/")
    if not rel_path or ".." in rel_path.split("/"):
        raise ValidationError(reason=f"路径无效: {body.path!r}")

    # 依次尝试 local/ 和 repos/ 下查找
    repo_dir: Optional[Path] = None
    for base in (skill_scope.platform_local_dir(), skill_scope.platform_repos_dir()):
        candidate = base / rel_path
        if candidate.is_dir() and (candidate / ".git").is_dir():
            repo_dir = candidate
            break
    if repo_dir is None:
        raise NotFoundError(resource=f"git 仓库 '{rel_path}'")

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "pull", cwd=str(repo_dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        output = (stdout + stderr).decode(errors="replace").strip()
        if proc.returncode != 0:
            raise ValidationError(reason=f"git pull 失败: {output}")
    except ValidationError:
        raise
    except asyncio.TimeoutError as e:
        raise ValidationError(reason="更新超时（120s）") from e
    except Exception as e:
        raise ValidationError(reason=f"更新操作失败: {e}") from e
    return PullResponse(ok=True, output=output)


@router.get("/all", summary="列出当前作用域可用的技能候选", response_model=AllSkillsResponse)
@require_role(Role.Admin)
async def list_all_skills(
    _current_user: DBUser = Depends(get_current_active_user),
) -> AllSkillsResponse:
    raw = WorkspaceService.list_all_skills(skill_scope_of(_current_user))
    items = [AllSkillItem(
        name=s["name"],
        display_name=s["display_name"],
        description=s["description"],
        source=s["source"],
        repo_name=s.get("repo_name"),
        ownership=s.get("ownership", "platform"),
        editable=bool(s.get("editable")),
    ) for s in raw]
    return AllSkillsResponse(total=len(items), items=items)


@router.get("/auto-inject", summary="获取自动注入技能列表", response_model=AutoInjectSkillsResponse)
@require_role(Role.Admin)
async def get_auto_inject_skills(
    _current_user: DBUser = Depends(get_current_active_user),
) -> AutoInjectSkillsResponse:
    """返回**本作用域实际生效**的清单

    租户没设过自己那份时看到的是平台方清单裁剪后的子集（见
    `core/auto_inject_skills.resolve_auto_inject_skills`），
    而不是一个空列表 —— 界面上"我什么都没勾，新工作区却有技能"才是让人报 bug 的形态。
    """
    from nekro_agent.core.auto_inject_skills import resolve_auto_inject_skills

    return AutoInjectSkillsResponse(skills=resolve_auto_inject_skills(skill_scope_of(_current_user)))


@router.put("/auto-inject", summary="更新自动注入技能列表", response_model=ActionOkResponse)
@require_role(Role.Admin)
async def update_auto_inject_skills(
    body: AutoInjectSkillsUpdate,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    """只写本作用域那一份

    写入时按可见性过滤一次是为了不把一条注定同步不成功的标识存进去；
    真正的判据在使用时重算（平台方随时可能把一条技能从 shared 改回 private）。
    """
    from nekro_agent.core.auto_inject_skills import AutoInjectStoreCorrupted
    from nekro_agent.core.auto_inject_skills import set_auto_inject_skills as _set

    scope = skill_scope_of(_current_user)
    visible = {skill["name"] for skill in WorkspaceService.list_all_skills(scope)}
    try:
        _set([name for name in body.skills if name in visible], scope)
    except AutoInjectStoreCorrupted as e:
        # 这份文件现在是全部作用域共用的一份，坏了就写等于替别人做主把清单清空
        raise ValidationError(
            reason=f"自动注入清单文件已损坏，已拒绝写入以免覆盖其他作用域的设置（{e}）",
        ) from e
    return ActionOkResponse(ok=True)


@router.get("", summary="列出当前作用域的独立技能库（扁平列表）", response_model=SkillListResponse)
@require_role(Role.Admin)
async def list_skills(
    _current_user: DBUser = Depends(get_current_active_user),
) -> SkillListResponse:
    raw = WorkspaceService.list_global_skills(skill_scope_of(_current_user))
    items = [
        SkillItem(
            name=s["name"],
            description=s["description"],
            ownership=s.get("ownership", "platform"),
            editable=bool(s.get("editable")),
        )
        for s in raw
    ]
    return SkillListResponse(total=len(items), items=items)


@router.post("", summary="上传 skill（zip 包）到自己的技能库", response_model=SkillUploadResponse)
@require_role(Role.Admin)
async def upload_skill(
    file: UploadFile = File(...),
    _current_user: DBUser = Depends(get_current_active_user),
) -> SkillUploadResponse:
    scope = skill_scope_of(_current_user)
    filename = file.filename or ""
    if not filename.endswith(".zip"):
        raise InvalidFileTypeError(file_type=filename.rsplit(".", 1)[-1] if "." in filename else "unknown")
    content = await file.read()
    if not zipfile.is_zipfile(io.BytesIO(content)):
        raise ValidationError(reason="上传的文件不是有效的 zip 压缩包")
    library_dir = skill_scope.writable_root(scope)
    library_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = zf.namelist()
        if not names:
            raise ValidationError(reason="zip 包为空")
        top_dirs = {n.split("/")[0] for n in names if n.split("/")[0]}
        if len(top_dirs) != 1:
            raise ValidationError(reason="zip 包必须包含唯一的顶层目录作为 skill 名称")
        skill_name = top_dirs.pop()
        # 顶层目录名会直接拼进落盘路径，交给同一个规整器判定，不再自己写一遍字符集
        if skill_scope.normalize_rel(skill_name) != [skill_name]:
            raise ValidationError(reason=f"skill 名称无效: {skill_name}")

        # 一致性校验：检查 zip 中 SKILL.md 的 frontmatter name
        skill_md_entry = f"{skill_name}/SKILL.md"
        if skill_md_entry in names:
            with zf.open(skill_md_entry) as f:
                md_content = f.read().decode("utf-8", errors="replace")
                fm_name = _parse_frontmatter_name(md_content)
                if fm_name and fm_name != skill_name:
                    raise ValidationError(
                        reason=f"SKILL.md 中的 name '{fm_name}' 与目录名 '{skill_name}' 不一致"
                    )

        target_dir = library_dir / skill_name
        # 第二层：这条路径上没有 `resolve_writable_dir`，而下一句是 `rmtree`。
        # 只靠规整器意味着它一旦被放宽（或某天被换掉），删除就直接落在库根之外
        if not skill_scope.is_within(library_dir, target_dir):
            raise ValidationError(reason=f"skill 名称无效: {skill_name}")
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        zf.extractall(library_dir)

    # 写入 .skill-origin.json
    WorkspaceService._write_skill_origin(library_dir / skill_name, origin="manual")
    return SkillUploadResponse(name=skill_name, ok=True)


@router.put("/visibility", summary="设置平台方技能/仓库对租户的可见性", response_model=ActionOkResponse)
@require_role(Role.Super)
async def update_skill_visibility(
    body: VisibilityUpdateRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    """只有平台方能决定自己的东西共享给谁

    租户自建技能恒为私有（与租户自建模型组同源），所以这里没有租户分支可开。
    """
    base = skill_scope.platform_local_dir() if body.kind == LOCAL_KIND else skill_scope.platform_repos_dir()
    if skill_scope.normalize_rel(body.name) != [body.name] or not (base / body.name).is_dir():
        raise NotFoundError(resource=f"skill '{body.name}'")
    skill_scope.set_visibility(body.kind, body.name, SkillVisibility(body.visibility))
    return ActionOkResponse(ok=True)


@router.delete("/{name}", summary="删除自己技能库中的 skill", response_model=ActionOkResponse)
@require_role(Role.Admin)
async def delete_skill(
    name: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    scope = skill_scope_of(_current_user)
    skill_dir = _writable_dir_or_404(scope, name)
    shutil.rmtree(skill_dir)
    if scope is None:
        # 不清理的话，下一个同名技能会继承这一个的共享状态
        skill_scope.prune_visibility(LOCAL_KIND, name)
    return ActionOkResponse(ok=True)


@router.get("/{name}/readme", summary="读取 skill README", response_model=SkillReadmeResponse)
@require_role(Role.Admin)
async def get_skill_readme(
    name: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> SkillReadmeResponse:
    scope = skill_scope_of(_current_user)
    roots = [
        root
        for root in skill_scope.readable_roots(scope)
        if root.ownership is not SkillOwnership.BUILTIN and root.kind != REPOS_KIND
    ]
    resolved = skill_scope.resolve_dir(scope, name, roots=roots)
    if resolved is None:
        raise NotFoundError(resource=f"skill '{name}'")
    for candidate in ("SKILL.md", "README.md", "README.txt", "readme.md", "readme.txt", "README"):
        readme_path = resolved.path / candidate
        if readme_path.exists():
            return SkillReadmeResponse(readme=readme_path.read_text(encoding="utf-8"))
    raise AppFileNotFoundError(filename=f"{name}/README")


# ── 仓库订阅 ──────────────────────────────────────────────────


@router.get("/repos", summary="列出已订阅仓库", response_model=RepoListResponse)
@require_role(Role.Admin)
async def list_repos(
    _current_user: DBUser = Depends(get_current_active_user),
) -> RepoListResponse:
    scope = skill_scope_of(_current_user)
    repos_root = next(
        (root for root in skill_scope.readable_roots(scope) if root.kind == REPOS_KIND),
        None,
    )
    repos_dir = Path(SKILLS_REPOS_DIR)
    repos_dir.mkdir(parents=True, exist_ok=True)
    repos: List[RepoMeta] = []
    if repos_root is None:
        return RepoListResponse(repos=repos)
    for d in sorted(repos_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if not skill_scope.is_entry_visible(repos_root, d.name, scope):
            continue
        meta_path = d / ".repo-meta.json"
        meta_data: Dict[str, Any] = {}
        if meta_path.exists():
            try:
                meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        # 统计内部 skill 数量
        skill_count = sum(1 for sd in d.iterdir() if sd.is_dir() and not sd.name.startswith(".") and (sd / "SKILL.md").exists())
        repo_url_val = meta_data.get("repo_url")
        repos.append(RepoMeta(
            repo_name=d.name,
            # 仓库地址只对平台方回显：它是平台方订阅了谁的运营信息，与技能正文同档
            repo_url=repo_url_val if isinstance(repo_url_val, str) and scope is None else None,
            subscribed_at=meta_data.get("subscribed_at"),
            skill_count=skill_count,
            visibility=skill_scope.get_visibility(REPOS_KIND, d.name).value if scope is None else None,  # type: ignore[arg-type]
        ))
    return RepoListResponse(repos=repos)


@router.post("/repos/subscribe", summary="订阅技能仓库（平台方专属）", response_model=CloneResponse)
@require_role(Role.Super)
async def subscribe_repo(
    body: RepoSubscribeRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> CloneResponse:
    repos_dir = Path(SKILLS_REPOS_DIR)
    repos_dir.mkdir(parents=True, exist_ok=True)
    repo_name = body.repo_name.strip("/").strip()
    if not repo_name or "/" in repo_name or ".." in repo_name or repo_name.startswith("."):
        raise ValidationError(reason=f"仓库名无效: {body.repo_name!r}")
    target_dir = repos_dir / repo_name
    if target_dir.exists():
        raise ValidationError(reason=f"仓库已存在: {repo_name}")
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth=1", body.repo_url, str(target_dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            err_msg = stderr.decode(errors="replace").strip()
            raise ValidationError(reason=f"git clone 失败: {err_msg}")
    except ValidationError:
        raise
    except asyncio.TimeoutError as e:
        raise ValidationError(reason="克隆超时（120s）") from e
    except Exception as e:
        raise ValidationError(reason=f"克隆操作失败: {e}") from e

    # 写入 .repo-meta.json
    import datetime as _dt
    meta = {
        "repo_url": body.repo_url,
        "community_id": None,
        "community_slug": None,
        "subscribed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    (target_dir / ".repo-meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return CloneResponse(path=repo_name, ok=True)


@router.post("/repos/{repo_name}/pull", summary="更新订阅仓库（平台方专属）", response_model=PullResponse)
@require_role(Role.Super)
async def pull_repo(
    repo_name: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> PullResponse:
    if ".." in repo_name or "/" in repo_name or repo_name.startswith("."):
        raise ValidationError(reason="仓库名无效")
    repo_dir = Path(SKILLS_REPOS_DIR) / repo_name
    if not repo_dir.is_dir():
        raise NotFoundError(resource=f"仓库 '{repo_name}'")
    if not (repo_dir / ".git").is_dir():
        raise ValidationError(reason=f"该目录不是 git 仓库: {repo_name}")
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "pull", cwd=str(repo_dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        output = (stdout + stderr).decode(errors="replace").strip()
        if proc.returncode != 0:
            raise ValidationError(reason=f"git pull 失败: {output}")
    except ValidationError:
        raise
    except asyncio.TimeoutError as e:
        raise ValidationError(reason="更新超时（120s）") from e
    except Exception as e:
        raise ValidationError(reason=f"更新操作失败: {e}") from e

    # 自动同步该仓库内所有技能到引用它们的工作区。
    # 平台方视角（tenant_id=None）是全量：一个 shared 仓库的技能本来就可能落在任何租户的
    # 工作区里，更新后不推过去等于让那些副本永远停在旧版本。
    synced = 0
    for skill_dir in sorted(repo_dir.iterdir()):
        if skill_dir.is_dir() and not skill_dir.name.startswith(".") and (skill_dir / "SKILL.md").exists():
            skill_id = f"{repo_name}/{skill_dir.name}"
            synced += await WorkspaceService.sync_skill_to_all_workspaces(skill_id)

    return PullResponse(ok=True, output=f"{output}\n同步了 {synced} 个工作区" if synced else output)


@router.delete("/repos/{repo_name}", summary="取消订阅仓库（平台方专属）", response_model=ActionOkResponse)
@require_role(Role.Super)
async def unsubscribe_repo(
    repo_name: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    if ".." in repo_name or "/" in repo_name or repo_name.startswith("."):
        raise ValidationError(reason="仓库名无效")
    repo_dir = Path(SKILLS_REPOS_DIR) / repo_name
    if not repo_dir.is_dir():
        raise NotFoundError(resource=f"仓库 '{repo_name}'")
    shutil.rmtree(repo_dir)
    skill_scope.prune_visibility(REPOS_KIND, repo_name)
    return ActionOkResponse(ok=True)


# ── 同步到工作区 ──────────────────────────────────────────────


@router.post("/sync-to-workspaces", summary="同步技能到自己名下使用它的工作区", response_model=SyncToWorkspacesResponse)
@require_role(Role.Admin)
async def sync_skill_to_workspaces(
    body: SyncToWorkspacesRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> SyncToWorkspacesResponse:
    """改前这条端点是 `Role.Super`，但底层 `sync_skill_to_all_workspaces` 不带归属过滤

    现在两件事一起收：调用方只推得动**自己作用域内**的工作区，
    而且推的技能本身也必须对该作用域可见（否则 `sync_single_skill` 解析不到，计数为 0）。
    """
    scope = skill_scope_of(_current_user)
    visible = {skill["name"] for skill in WorkspaceService.list_all_skills(scope)}
    if body.skill_id not in visible:
        raise NotFoundError(resource=f"skill '{body.skill_id}'")
    count = await WorkspaceService.sync_skill_to_all_workspaces(body.skill_id, scope)
    return SyncToWorkspacesResponse(ok=True, synced_count=count)
