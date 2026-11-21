"""宿主侧碰容器可写目录的调用点：逐个钉住「不跟随符号链接」

`safe_fs` 自己的行为在 `test_workspace_safe_fs.py`；这里钉的是**每个调用点都真的走了它**：
容器（CC 工作区容器，或主沙盒）先在自己能写的目录里放好链接，NA 宿主侧照常调用，断言
链接指向的宿主文件一个字节都没被读出、写入、删掉或改权限。

覆盖：CLAUDE.md / `.mcp.json` / `settings.json` 写入、`_na_context.md` 读写、共享目录扫描、
记忆文件、技能同步与动态技能、工作区知识库、沙盒代码文件清理、沙盒共享目录读取与转交、
cc_workspace 的上传下载。

建不了符号链接的机器（Windows 没开开发者模式）上，这些用例跳过。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

from nekro_agent.services.kb import kb_files
from nekro_agent.services.workspace import manager as manager_module
from nekro_agent.services.workspace import safe_fs
from nekro_agent.services.workspace.cc_credential import WorkspaceCredential
from nekro_agent.services.workspace.manager import WorkspaceService, write_workspace_text

SECRET = "platform-" + "key-callsite"


def _link(link: Path, target: Path, *, is_dir: bool) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("这台机器上建不了符号链接")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    root = tmp_path / "workspaces"
    ws = root / "1"
    ws.mkdir(parents=True)
    outside = tmp_path / "host"
    outside.mkdir()
    secret = outside / "nekro-agent.yaml"
    secret.write_text(SECRET, encoding="utf-8")
    monkeypatch.setattr(WorkspaceService, "get_workspace_dir", staticmethod(lambda workspace_id: root / str(workspace_id)))
    monkeypatch.setattr(manager_module, "WORKSPACE_ROOT_DIR", str(root))
    return SimpleNamespace(root=root, ws=ws, outside=outside, secret=secret, tmp=tmp_path)


def _untouched(world: SimpleNamespace) -> None:
    assert world.secret.read_text(encoding="utf-8") == SECRET
    assert sorted(p.name for p in world.outside.iterdir()) == ["nekro-agent.yaml"]


# ---------------------------------------------------------------------------
# CLAUDE.md / .mcp.json / settings.json
# ---------------------------------------------------------------------------


def test_claude_md_write_replaces_a_linked_default_dir(world) -> None:
    _link(world.ws / "default", world.outside, is_dir=True)
    write_workspace_text(1, ("default", "CLAUDE.md"), "# hi")
    _untouched(world)
    assert not (world.ws / "default").is_symlink()
    assert (world.ws / "default" / "CLAUDE.md").read_text(encoding="utf-8") == "# hi"


def test_mcp_json_write_replaces_a_linked_file(world) -> None:
    (world.ws / "default").mkdir()
    _link(world.ws / "default" / ".mcp.json", world.secret, is_dir=False)
    write_workspace_text(1, ("default", ".mcp.json"), "{}")
    _untouched(world)
    assert (world.ws / "default" / ".mcp.json").read_text(encoding="utf-8") == "{}"


def test_settings_json_write_does_not_follow_a_link(world) -> None:
    _link(world.ws / "settings.json", world.secret, is_dir=False)
    WorkspaceService._write_settings_files(  # noqa: SLF001
        world.ws,
        None,
        WorkspaceCredential(base_url="http://relay", auth_token="relay-token", relayed=True),
        "测试",
        allow_platform_secrets=False,
        home=WorkspaceService.claude_home(1),
    )
    _untouched(world)
    payload = json.loads((world.ws / "settings.json").read_text(encoding="utf-8"))
    assert payload["providers"]["anthropic"]["auth_token"] == "relay-token"


# 新布局的 home 在工作区目录之外，上级只有 NA 写得到；home 里面的条目照样是容器写得到的


def test_user_level_settings_in_the_new_home_do_not_follow_a_link(world) -> None:
    home = WorkspaceService.ensure_claude_home(WorkspaceService.claude_home(1))
    assert home.legacy is False
    _link(home.path / "settings.json", world.secret, is_dir=False)
    from nekro_agent.core.cc_model_presets import CCModelPresetItem

    preset = CCModelPresetItem(
        id=1,
        name="p",
        base_url="http://upstream",
        auth_token="",
        api_timeout_ms="180000",
        model_type="manual",
        anthropic_model="m",
        small_fast_model="",
        extra_env={},
        is_default=True,
    )
    WorkspaceService._write_settings_files(  # noqa: SLF001
        world.ws,
        preset,
        WorkspaceCredential(base_url="http://relay", auth_token="relay-token", relayed=True),
        "测试",
        allow_platform_secrets=False,
        home=home,
    )
    _untouched(world)
    assert not (home.path / "settings.json").is_symlink()
    assert "relay-token" in (home.path / "settings.json").read_text(encoding="utf-8")

    # 没有预设时删掉的也只是链接本身
    (home.path / "settings.json").unlink()
    _link(home.path / "settings.json", world.secret, is_dir=False)
    WorkspaceService._write_settings_files(  # noqa: SLF001
        world.ws,
        None,
        WorkspaceCredential(base_url="http://relay", auth_token="relay-token", relayed=True),
        "测试",
        allow_platform_secrets=False,
        home=home,
    )
    assert not (home.path / "settings.json").exists()
    _untouched(world)


def test_gateway_files_in_the_new_home_replace_a_linked_nk_ssh_dir(world) -> None:
    from nekro_agent.services.ssh_gateway import container_files

    home = WorkspaceService.ensure_claude_home(WorkspaceService.claude_home(1))
    _link(home.path / "nk-ssh", world.outside, is_dir=True)
    container_files._write_nk_ssh_files_on_host(1, {"config": "cfg", "id_gateway": "KEY"})  # noqa: SLF001
    _untouched(world)
    assert not (home.path / "nk-ssh").is_symlink()
    assert (home.path / "nk-ssh" / "id_gateway").read_text(encoding="utf-8") == "KEY"

    # 里面的文件换成链接：删的时候只删链接本身
    (home.path / "nk-ssh" / "config").unlink()
    _link(home.path / "nk-ssh" / "config", world.secret, is_dir=False)
    container_files._remove_nk_ssh_files_on_host(1)  # noqa: SLF001
    _untouched(world)
    assert not (home.path / "nk-ssh").exists()


def test_skills_in_the_new_home_replace_a_linked_skills_dir(world) -> None:
    fake_skill = world.outside / "stolen"
    fake_skill.mkdir()
    (fake_skill / "SKILL.md").write_text("---\nname: stolen\n---\nplatform", encoding="utf-8")
    home = WorkspaceService.ensure_claude_home(WorkspaceService.claude_home(1))
    _link(home.path / "skills", world.outside, is_dir=True)

    assert WorkspaceService.list_dynamic_skills(1) == []
    assert WorkspaceService.read_dynamic_skill(1, "stolen") is None
    assert WorkspaceService.delete_dynamic_skill(1, "stolen") is False

    WorkspaceService.write_dynamic_skill(1, "mine", "---\nname: mine\n---\n")
    assert not (home.path / "skills").is_symlink()
    assert (home.path / "skills" / "mine" / "SKILL.md").is_file()
    assert (fake_skill / "SKILL.md").is_file()
    assert sorted(p.name for p in world.outside.iterdir()) == ["nekro-agent.yaml", "stolen"]


# ---------------------------------------------------------------------------
# _na_context.md 与记忆目录
# ---------------------------------------------------------------------------


def test_na_context_read_through_a_link_returns_nothing(world) -> None:
    _link(world.ws / "default" / "memory" / "_na_context.md", world.secret, is_dir=False)
    assert WorkspaceService.read_na_context(1) == ("", "")


def test_na_context_read_through_a_linked_memory_dir_returns_nothing(world) -> None:
    (world.outside / "_na_context.md").write_text("---\nupdated: x\n---\nleaked", encoding="utf-8")
    _link(world.ws / "default" / "memory", world.outside, is_dir=True)
    assert WorkspaceService.read_na_context(1) == ("", "")


def test_na_context_write_does_not_land_in_a_linked_memory_dir(world) -> None:
    _link(world.ws / "default" / "memory", world.outside, is_dir=True)
    WorkspaceService.write_na_context(1, "协作现状")
    _untouched(world)
    body, _updated = WorkspaceService.read_na_context(1)
    assert body.strip() == "协作现状"


def test_memory_files_never_follow_links(world) -> None:
    WorkspaceService.write_memory_file(1, "notes/a.md", "---\ntitle: A\n---\nbody")
    _link(world.ws / "default" / "memory" / "leak.md", world.secret, is_dir=False)
    _link(world.ws / "default" / "memory" / "linked", world.outside, is_dir=True)

    assert WorkspaceService.read_memory_file(1, "leak.md") is None
    assert WorkspaceService.read_memory_file(1, "linked/nekro-agent.yaml") is None
    with pytest.raises(ValueError):
        WorkspaceService.write_memory_file(1, "linked/new.md", "x")
    assert WorkspaceService.delete_memory_file(1, "leak.md") is False
    tree = WorkspaceService.list_memory_tree(1)
    assert [node["name"] for node in tree] == ["notes"]

    assert WorkspaceService.reset_memory(1) == 1
    _untouched(world)
    assert (world.ws / "default" / "memory" / "leak.md").is_symlink()


# ---------------------------------------------------------------------------
# 共享目录扫描（进提示词）
# ---------------------------------------------------------------------------


def test_shared_dir_scan_lists_only_regular_files(world) -> None:
    shared = world.ws / "default" / "shared"
    shared.mkdir(parents=True)
    (shared / "report.csv").write_text("a,b", encoding="utf-8")
    _link(shared / "leak.yaml", world.secret, is_dir=False)
    _link(shared / "hostdir", world.outside, is_dir=True)

    names = {item["rel_path"] for item in WorkspaceService.scan_shared_dir(1)}
    assert names == {"report.csv"}


def test_shared_dir_scan_refuses_a_linked_shared_dir(world) -> None:
    _link(world.ws / "default" / "shared", world.outside, is_dir=True)
    assert WorkspaceService.scan_shared_dir(1) == []


# ---------------------------------------------------------------------------
# 技能同步与动态技能
# ---------------------------------------------------------------------------


def test_sync_skills_replaces_a_linked_skills_dir_instead_of_pruning_through_it(world, monkeypatch) -> None:
    """`skills` 被换成指向技能库的链接时，旧写法会按 `.skill-origin.json` 把库里没选中的技能 rmtree 掉"""
    library = world.tmp / "library"
    victim = library / "ops-runbook"
    victim.mkdir(parents=True)
    (victim / "SKILL.md").write_text("platform private", encoding="utf-8")
    (victim / ".skill-origin.json").write_text(json.dumps({"origin": "user"}), encoding="utf-8")
    source = world.tmp / "src-demo"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    _link(world.ws / ".claude_home" / "skills", library, is_dir=True)

    from nekro_agent.services import skill_scope

    async def _scope(_workspace: Any) -> Optional[int]:
        return None

    monkeypatch.setattr(skill_scope, "workspace_skill_scope", _scope)
    monkeypatch.setattr(
        WorkspaceService,
        "_resolve_skill_source",
        staticmethod(lambda skill_id, _scope=None: (source, "user")),
    )
    monkeypatch.setattr(manager_module, "SKILLS_LOCAL_DIR", str(world.tmp / "skills-local"))
    monkeypatch.setattr(manager_module, "SKILLS_REPOS_DIR", str(world.tmp / "skills-repos"))

    asyncio.run(WorkspaceService.sync_skills(SimpleNamespace(id=1, metadata={"skills": ["demo"]})))

    assert (victim / "SKILL.md").read_text(encoding="utf-8") == "platform private"
    skills_home = world.ws / ".claude_home" / "skills"
    assert not skills_home.is_symlink()
    assert (skills_home / "demo" / "SKILL.md").is_file()
    origin = json.loads((skills_home / "demo" / ".skill-origin.json").read_text(encoding="utf-8"))
    assert origin["source_skill_id"] == "demo"


def test_dynamic_skill_helpers_do_not_follow_a_linked_claude_home(world) -> None:
    fake_skill = world.outside / "stolen"
    fake_skill.mkdir()
    (fake_skill / "SKILL.md").write_text("---\nname: stolen\n---\nplatform", encoding="utf-8")
    _link(world.ws / ".claude_home", world.outside, is_dir=True)

    assert WorkspaceService.list_dynamic_skills(1) == []
    assert WorkspaceService.read_dynamic_skill(1, "stolen") is None
    assert WorkspaceService.read_dynamic_skill_file(1, "stolen", "SKILL.md") is None
    assert WorkspaceService.list_dynamic_skill_tree(1, "stolen") is None
    assert WorkspaceService.delete_dynamic_skill(1, "stolen") is False
    assert (fake_skill / "SKILL.md").is_file()


@pytest.mark.parametrize("legacy_layout", [False, True], ids=["home-outside-workspace", "legacy-claude-home"])
def test_dynamic_skill_write_refuses_a_linked_skill_dir(world, legacy_layout: bool) -> None:
    if legacy_layout:
        (world.ws / ".claude_home").mkdir()
    WorkspaceService.write_dynamic_skill(1, "mine", "---\nname: mine\n---\n")
    home = WorkspaceService.claude_home(1)
    assert home.legacy is legacy_layout
    _link(home.path / "skills" / "other", world.outside, is_dir=True)
    with pytest.raises(ValueError):
        WorkspaceService.write_dynamic_skill(1, "other", "x")
    with pytest.raises(ValueError):
        WorkspaceService.write_dynamic_skill_file(1, "mine", "../other/x.md", "x")
    _untouched(world)


# ---------------------------------------------------------------------------
# 工作区知识库
# ---------------------------------------------------------------------------


def test_kb_reads_do_not_follow_links(world) -> None:
    files = world.ws / "default" / "kb" / "files"
    files.mkdir(parents=True)
    _link(files / "plan.md", world.secret, is_dir=False)
    assert kb_files.read_text(1, kb_files.SOURCE, "plan.md") == ""
    assert kb_files.exists(1, kb_files.SOURCE, "plan.md") is False
    assert kb_files.copy_to(1, kb_files.SOURCE, "plan.md", world.tmp / "copy.md") is False
    with pytest.raises(FileNotFoundError):
        kb_files.open_read(1, kb_files.SOURCE, "plan.md")
    assert not (world.tmp / "copy.md").exists()


def test_kb_writes_replace_a_linked_files_dir(world) -> None:
    _link(world.ws / "default" / "kb", world.outside, is_dir=True)
    kb_files.create_exclusive(1, kb_files.SOURCE, "notes/new.md", b"body")
    _untouched(world)
    assert (world.ws / "default" / "kb" / "files" / "notes" / "new.md").read_bytes() == b"body"


def test_kb_unlink_and_rename_only_touch_the_entry_itself(world) -> None:
    files = world.ws / "default" / "kb" / "files"
    files.mkdir(parents=True)
    _link(files / "plan.md", world.secret, is_dir=False)
    assert kb_files.rename(1, kb_files.SOURCE, "plan.md", "moved.md") is False
    assert kb_files.unlink(1, kb_files.SOURCE, "plan.md") is True
    _untouched(world)
    with pytest.raises(ValueError):
        kb_files.read_text(1, kb_files.SOURCE, "../../settings.json")


# ---------------------------------------------------------------------------
# 沙盒共享目录（主沙盒）
# ---------------------------------------------------------------------------


@pytest.fixture
def sandbox(world, monkeypatch) -> SimpleNamespace:
    from nekro_agent.core import os_env
    from nekro_agent.tools import sandbox_shared_files

    shared_root = world.tmp / "sandboxes"
    shared = shared_root / "ck"
    shared.mkdir(parents=True)
    uploads = world.tmp / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(sandbox_shared_files, "SANDBOX_SHARED_HOST_DIR", str(shared_root))
    monkeypatch.setattr(os_env, "USER_UPLOAD_DIR", str(uploads))
    return SimpleNamespace(shared=shared, uploads=uploads)


def test_scrub_code_files_skips_a_link(world, monkeypatch) -> None:
    from nekro_agent.services.ssh_gateway import scrub

    code = world.tmp / "sb"
    code.mkdir()
    (code / "api_caller.py.code").write_text(f"pw = '{SECRET}'", encoding="utf-8")
    _link(code / "run_script.py.code", world.secret, is_dir=False)
    monkeypatch.setattr(scrub, "pending_fragments", lambda _chat_key: [SECRET])

    hits = scrub.scrub_code_files("ck", [code / "api_caller.py.code", code / "run_script.py.code"])

    assert hits == 1
    assert SECRET not in (code / "api_caller.py.code").read_text(encoding="utf-8")
    _untouched(world)


def test_shared_file_reads_refuse_links(world, sandbox) -> None:
    from nekro_agent.tools.sandbox_shared_files import (
        UnsafeSharedFileError,
        host_file_is_regular,
        read_host_file_bytes,
        snapshot_for_handoff,
    )

    (sandbox.shared / "ok.png").write_bytes(b"png")
    _link(sandbox.shared / "leak.png", world.secret, is_dir=False)
    _link(sandbox.shared / "dir", world.outside, is_dir=True)

    assert host_file_is_regular(sandbox.shared / "ok.png") is True
    assert host_file_is_regular(sandbox.shared / "leak.png") is False
    assert host_file_is_regular(sandbox.shared / "dir" / "nekro-agent.yaml") is False
    assert read_host_file_bytes(sandbox.shared / "ok.png") == b"png"
    with pytest.raises(UnsafeSharedFileError):
        read_host_file_bytes(sandbox.shared / "leak.png")
    with pytest.raises(UnsafeSharedFileError):
        read_host_file_bytes(sandbox.shared / "dir" / "nekro-agent.yaml")
    with pytest.raises(UnsafeSharedFileError):
        snapshot_for_handoff(sandbox.shared / "leak.png", "chat-1")

    handed = snapshot_for_handoff(sandbox.shared / "ok.png", "chat-1")
    assert handed.name == "ok.png"
    assert handed.read_bytes() == b"png"
    assert handed.resolve().is_relative_to(sandbox.uploads.resolve())


def test_mixed_forward_file_replaces_a_link_in_the_shared_dir(world, sandbox, monkeypatch) -> None:
    from nekro_agent.tools.file_utils import FileSystem

    source = world.tmp / "asset.pdf"
    source.write_bytes(b"pdf")
    _link(sandbox.shared / "asset.pdf", world.secret, is_dir=False)
    monkeypatch.setattr(FileSystem, "shared_path", property(lambda _self: sandbox.shared))

    sandbox_path = asyncio.run(FileSystem("chat-1", "ck").mixed_forward_file(source))

    assert sandbox_path.endswith("asset.pdf")
    assert (sandbox.shared / "asset.pdf").read_bytes() == b"pdf"
    _untouched(world)


# ---------------------------------------------------------------------------
# cc_workspace：主沙盒 <-> CC 工作区
# ---------------------------------------------------------------------------


class _FakeFs:
    def __init__(self, shared: Path) -> None:
        self.shared_path = shared

    def get_file(self, file_path: str) -> Path:
        return self.shared_path / Path(file_path).name

    def forward_file(self, host_path: Path) -> str:
        return f"/app/shared/{Path(host_path).name}"


def _cc_ctx(shared: Path) -> Any:
    async def _bound() -> Any:
        return SimpleNamespace(id=1)

    return SimpleNamespace(get_bound_workspace=_bound, fs=_FakeFs(shared))


def test_download_from_cc_refuses_config_files_links_and_traversal(world, sandbox) -> None:
    from plugins.builtin.cc_workspace.main import download_file_from_cc

    ctx = _cc_ctx(sandbox.shared)
    (world.ws / "default" / "shared").mkdir(parents=True)
    (world.ws / "default" / "shared" / "result.txt").write_text("ok", encoding="utf-8")
    (world.ws / "settings.json").write_text('{"auth_token": "relay"}', encoding="utf-8")
    (world.ws / "default" / ".mcp.json").write_text("{}", encoding="utf-8")
    _link(world.ws / "default" / "shared" / "leak.txt", world.secret, is_dir=False)

    for denied in ("/workspace/settings.json", "/workspace/.claude_home/nk-ssh/id_gateway", "/workspace/default/.mcp.json"):
        with pytest.raises(ValueError):
            asyncio.run(download_file_from_cc(ctx, denied))
    with pytest.raises(ValueError):
        asyncio.run(download_file_from_cc(ctx, "leak.txt"))

    # 目标名只取纯文件名：`../escape.txt` 落在共享目录里，不会写到上一级
    result = asyncio.run(download_file_from_cc(ctx, "result.txt", dest_name="../escape.txt"))
    assert result == "/app/shared/escape.txt"
    assert (sandbox.shared / "escape.txt").read_text(encoding="utf-8") == "ok"
    assert not (sandbox.shared.parent / "escape.txt").exists()
    assert not any(p.name == "leak.txt" for p in sandbox.shared.iterdir())
    _untouched(world)


def test_upload_to_cc_does_not_follow_links_on_either_side(world, sandbox) -> None:
    from plugins.builtin.cc_workspace.main import upload_file_to_cc

    ctx = _cc_ctx(sandbox.shared)
    (sandbox.shared / "data.csv").write_text("a,b", encoding="utf-8")
    _link(sandbox.shared / "leak.csv", world.secret, is_dir=False)
    _link(world.ws / "default" / "shared", world.outside, is_dir=True)

    with pytest.raises(ValueError):
        asyncio.run(upload_file_to_cc(ctx, "/app/shared/leak.csv"))
    cc_path = asyncio.run(upload_file_to_cc(ctx, "/app/shared/data.csv"))

    assert cc_path == "/workspace/default/shared/data.csv"
    assert not (world.ws / "default" / "shared").is_symlink()
    assert (world.ws / "default" / "shared" / "data.csv").read_text(encoding="utf-8") == "a,b"
    _untouched(world)


def test_tenant_scoped_helpers_reject_traversal_names(world) -> None:
    checks: Dict[str, Any] = {
        "read": WorkspaceService.read_dynamic_skill(1, ".."),
        "tree": WorkspaceService.list_dynamic_skill_tree(1, "../x"),
        "file": WorkspaceService.read_dynamic_skill_file(1, "mine", "../../settings.json"),
    }
    assert checks == {"read": None, "tree": None, "file": None}
    assert safe_fs.kind(world.ws, ("default",)) is None
