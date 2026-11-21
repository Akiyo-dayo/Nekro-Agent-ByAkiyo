"""CC 工作区的 home（容器里的 `~/.claude`）搬到工作区目录之外

旧布局 `{工作区}/.claude_home` 同时也在读写挂载的 `/workspace` 里，容器能把它改名、换成链接；
新布局 `{工作区根}/.nk-homes/<id>/claude` 的上级只有 NA 写得到。这里钉住：

- 定位：新工作区用新位置；还没迁的用旧位置——旧 `.claude_home` 被换成了链接也算旧布局（重启沿用
  旧挂载，修复与核对得落在旧路径上）；
- 迁移：一次改名整个挪过去（不复制），已经迁过不再动，改名失败照旧按旧布局，旧的是链接不跟过去；
- 新旧并存（退回旧版本又升级回来）：工作区目录里那份旧的整个挪出工作区，挪不动就删掉 NA 写过的；
- 写的一方先算好旧位置、迁移随后跑完：按迁移后的位置写，不在工作区目录里把旧 home 建回来；
- 编号复用：新建工作区清掉同号残留的 home，清不掉就撤销创建；
- 删除：库里删除提交之后删 home（两种布局都删），事务失败不删；删了以后的预设同步不把 home 建回来；
- SSH 网关文件写到这个工作区当前的 home 里；
- 属主：NA 在 home 里建的、写的东西交给容器用户（uid / gid 1000）——home 本身、`settings.json`、
  `skills/` 与技能目录、网关私钥；`nk-ssh` 目录与配置留给 root。真改属主的用例要 POSIX 且以 root
  跑；另有一组把改属主那一步换成记录器、不依赖系统的用例，钉住每条写入路径都走到了它。

建不了符号链接的机器（Windows 没开开发者模式）上，用到链接的用例跳过。
"""

from __future__ import annotations

import datetime
import errno
import json
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tortoise import Tortoise

from nekro_agent.core.cc_model_presets import CCModelPresetItem
from nekro_agent.core.exception_handlers import register_exception_handlers
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.ssh_gateway import container_files
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.role import Role
from nekro_agent.services.workspace import manager as manager_module
from nekro_agent.services.workspace import safe_fs
from nekro_agent.services.workspace.cc_credential import WorkspaceCredential
from nekro_agent.services.workspace.manager import ClaudeHome, StaleHomeError, WorkspaceService

POSIX = os.name == "posix"
AS_ROOT = POSIX and hasattr(os, "geteuid") and os.geteuid() == 0
CONTAINER_IDS = (1000, 1000)


def _link(link: Path, target: Path, *, is_dir: bool) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("这台机器上建不了符号链接")


@pytest.fixture
def logs() -> Any:
    from loguru import logger

    messages: List[str] = []
    sink_id = logger.add(lambda message: messages.append(f"{message.record['level'].name}|{message.record['message']}"))
    try:
        yield messages
    finally:
        logger.remove(sink_id)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    root = tmp_path / "workspaces"
    root.mkdir()
    outside = tmp_path / "host-elsewhere"
    outside.mkdir()
    (outside / "keep.txt").write_text("host file", encoding="utf-8")
    monkeypatch.setattr(manager_module, "WORKSPACE_ROOT_DIR", str(root))
    return SimpleNamespace(root=root, outside=outside, tmp=tmp_path)


def _new(world: SimpleNamespace, workspace_id: int = 1) -> Path:
    """新位置，测试里自己拼，不借被测代码的函数"""
    return world.root / ".nk-homes" / str(workspace_id) / "claude"


def _legacy(world: SimpleNamespace, workspace_id: int = 1) -> Path:
    return world.root / str(workspace_id) / ".claude_home"


def _retired(world: SimpleNamespace, workspace_id: int = 1) -> List[Path]:
    """从工作区目录里挪出来的旧 home（`.nk-homes/<id>/legacy-*`）"""
    holder = world.root / ".nk-homes" / str(workspace_id)
    return sorted(holder.glob("legacy-*")) if holder.is_dir() else []


def _fill_legacy(world: SimpleNamespace, workspace_id: int = 1) -> Path:
    """一个用过一阵子的旧 home：会话、全局状态、网关私钥、技能"""
    legacy = _legacy(world, workspace_id)
    (legacy / "projects" / "-workspace-default").mkdir(parents=True)
    (legacy / "projects" / "-workspace-default" / "session.jsonl").write_text("history", encoding="utf-8")
    (legacy / ".claude.json").write_text('{"hasCompletedOnboarding": true}', encoding="utf-8")
    (legacy / "nk-ssh").mkdir()
    (legacy / "nk-ssh" / "id_gateway").write_text("PRIVATE", encoding="utf-8")
    (legacy / "skills" / "demo").mkdir(parents=True)
    (legacy / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    return legacy


# ---------------------------------------------------------------------------
# 定位
# ---------------------------------------------------------------------------


class TestWhereTheHomeIs:
    def test_a_new_workspace_gets_its_home_outside_the_workspace_dir(self, world) -> None:
        home = WorkspaceService.claude_home(1)
        assert home.legacy is False
        assert home.path == _new(world)

    def test_an_unmigrated_workspace_keeps_the_legacy_home(self, world) -> None:
        _legacy(world).mkdir(parents=True)
        home = WorkspaceService.claude_home(1)
        assert home.legacy is True
        assert home.path == _legacy(world)

    def test_a_legacy_home_replaced_by_a_link_still_counts_as_legacy(self, world) -> None:
        """换成「新位置」的话旧路径上的链接没人修，重启沿用的旧挂载会顺着它挂出去"""
        _link(_legacy(world), world.outside, is_dir=True)
        assert WorkspaceService.claude_home(1).legacy is True

    def test_once_migrated_the_new_home_wins(self, world) -> None:
        _legacy(world).mkdir(parents=True)
        _new(world).mkdir(parents=True)
        assert WorkspaceService.claude_home(1).path == _new(world)

    def test_the_skills_dir_follows_the_home(self, world) -> None:
        assert WorkspaceService.get_workspace_skills_dir(1) == _new(world) / "skills"
        _legacy(world, 2).mkdir(parents=True)
        assert WorkspaceService.get_workspace_skills_dir(2) == _legacy(world, 2) / "skills"


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------


class TestMigration:
    def test_everything_moves_with_one_rename(self, world) -> None:
        legacy = _fill_legacy(world)
        inode_before = os.stat(legacy).st_ino

        home = WorkspaceService.migrate_claude_home(1)

        new = _new(world)
        assert home.legacy is False and home.path == new
        assert not os.path.lexists(legacy)
        assert (new / "projects" / "-workspace-default" / "session.jsonl").read_text(encoding="utf-8") == "history"
        assert (new / ".claude.json").is_file()
        assert (new / "nk-ssh" / "id_gateway").read_text(encoding="utf-8") == "PRIVATE"
        assert (new / "skills" / "demo" / "SKILL.md").is_file()
        if inode_before:
            assert os.stat(new).st_ino == inode_before, "应当是改名（同一个目录），不是复制"
        assert WorkspaceService.claude_home(1).path == new

    @pytest.mark.skipif(not POSIX, reason="权限位只在 POSIX 上有意义")
    def test_only_the_home_itself_is_world_writable(self, world) -> None:
        _fill_legacy(world)
        WorkspaceService.migrate_claude_home(1)
        assert stat.S_IMODE(os.stat(world.root / ".nk-homes").st_mode) == 0o700
        assert stat.S_IMODE(os.stat(world.root / ".nk-homes" / "1").st_mode) == 0o700
        assert stat.S_IMODE(os.stat(_new(world)).st_mode) == 0o777

    def test_a_workspace_without_any_home_gets_a_fresh_one(self, world) -> None:
        home = WorkspaceService.migrate_claude_home(1)
        assert home.legacy is False
        assert _new(world).is_dir()

    def test_migrating_twice_changes_nothing(self, world) -> None:
        _fill_legacy(world)
        WorkspaceService.migrate_claude_home(1)
        WorkspaceService.migrate_claude_home(1)
        assert (_new(world) / ".claude.json").is_file()
        assert not os.path.lexists(_legacy(world))

    def test_a_legacy_dir_next_to_a_migrated_home_is_moved_out_of_the_workspace(self, world, logs) -> None:
        """已经迁过、工作区目录里又出现了 `.claude_home`：用新的，旧的整个挪出工作区（不留在 `/workspace`）"""
        (_new(world)).mkdir(parents=True)
        (_new(world) / "mine.txt").write_text("new", encoding="utf-8")
        _fill_legacy(world)

        home = WorkspaceService.migrate_claude_home(1)

        assert home.path == _new(world)
        assert (_new(world) / "mine.txt").is_file()
        assert not os.path.lexists(_legacy(world))
        (retired,) = _retired(world)
        assert (retired / ".claude.json").is_file()
        assert any(line.startswith("WARNING|") and str(retired) in line for line in logs)

    def test_a_failed_rename_keeps_the_legacy_layout(self, world, monkeypatch, logs) -> None:
        """例如工作区目录单独挂在另一个文件系统上（EXDEV）：照旧按旧布局启动，不让启动失败"""
        legacy = _fill_legacy(world)

        def cross_device(*_args: Any, **_kwargs: Any) -> None:
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        monkeypatch.setattr(safe_fs.DirHandle, "rename_to", cross_device)

        home = WorkspaceService.migrate_claude_home(1)

        assert home.legacy is True and home.path == legacy
        assert (legacy / "nk-ssh" / "id_gateway").read_text(encoding="utf-8") == "PRIVATE"
        assert not _new(world).exists()
        assert WorkspaceService.claude_home(1).legacy is True
        assert any(line.startswith("ERROR|") and "没能挪到工作区之外" in line for line in logs)

    def test_a_legacy_home_that_is_a_link_is_not_followed(self, world, logs) -> None:
        """容器把 `.claude_home` 换成了指向宿主别处的链接：不挪、不碰链接那头，换一个空的新 home"""
        _link(_legacy(world), world.outside, is_dir=True)

        home = WorkspaceService.migrate_claude_home(1)

        assert home.legacy is False
        assert _new(world).is_dir() and not _new(world).is_symlink()
        assert list(_new(world).iterdir()) == []
        assert (world.outside / "keep.txt").read_text(encoding="utf-8") == "host file"
        assert sorted(p.name for p in world.outside.iterdir()) == ["keep.txt"]
        assert any(line.startswith("ERROR|") and "不是普通目录" in line for line in logs)


# ---------------------------------------------------------------------------
# 新旧两份同时存在：退回旧版本又升级回来
# ---------------------------------------------------------------------------

LEFTOVER_KEY = "sk-platform-" + "left-in-the-workspace"


def _old_version_writes_legacy(world: SimpleNamespace, workspace_id: int = 1) -> Path:
    """模拟没跑回滚脚本就退回旧版本：旧版本在工作区目录里从无到有建 `.claude_home`，写设置与网关钥匙"""
    legacy = _legacy(world, workspace_id)
    (legacy / "nk-ssh").mkdir(parents=True)
    (legacy / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": LEFTOVER_KEY}}), encoding="utf-8")
    (legacy / "nk-ssh" / "id_gateway").write_text("OLD-PRIVATE", encoding="utf-8")
    return legacy


def _upgraded_rolled_back_and_upgraded_again(world: SimpleNamespace) -> Path:
    _fill_legacy(world)
    WorkspaceService.migrate_claude_home(1)
    (world.root / "1" / "default").mkdir(parents=True, exist_ok=True)
    return _old_version_writes_legacy(world)


def _files_under(path: Path) -> List[Path]:
    return [p for p in path.rglob("*") if p.is_file()]


class TestALeftoverLegacyHomeAfterARollback:
    """升级（迁完）→ 没跑回滚脚本就退回旧版本 → 旧版本在工作区目录里重新写了一份 → 再升回来"""

    def test_the_next_start_moves_it_out_of_the_workspace(self, world, logs) -> None:
        _upgraded_rolled_back_and_upgraded_again(world)

        home = WorkspaceService.migrate_claude_home(1)

        assert home.path == _new(world)
        assert not os.path.lexists(_legacy(world))
        assert not any(LEFTOVER_KEY in p.read_text(encoding="utf-8") for p in _files_under(world.root / "1"))
        (retired,) = _retired(world)
        assert LEFTOVER_KEY in (retired / "settings.json").read_text(encoding="utf-8")
        assert (retired / "nk-ssh" / "id_gateway").read_text(encoding="utf-8") == "OLD-PRIVATE"
        # 在用的新 home 原样
        assert (_new(world) / "projects" / "-workspace-default" / "session.jsonl").read_text(encoding="utf-8") == "history"
        assert any(line.startswith("WARNING|") and str(retired) in line for line in logs)

    @pytest.mark.skipif(not POSIX, reason="权限位只在 POSIX 上有意义")
    def test_the_moved_copy_is_closed_to_other_users(self, world) -> None:
        _upgraded_rolled_back_and_upgraded_again(world)
        WorkspaceService.retire_legacy_home(1)
        (retired,) = _retired(world)
        assert stat.S_IMODE(os.stat(retired).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(retired.parent).st_mode) == 0o700

    def test_writing_settings_afterwards_never_brings_it_back(self, world) -> None:
        _upgraded_rolled_back_and_upgraded_again(world)
        WorkspaceService.retire_legacy_home(1)

        _write_user_settings(world, WorkspaceService.claude_home(1))

        assert not os.path.lexists(_legacy(world))
        assert "relay-token" in (_new(world) / "settings.json").read_text(encoding="utf-8")

    def test_two_leftovers_get_two_names(self, world) -> None:
        _upgraded_rolled_back_and_upgraded_again(world)
        WorkspaceService.retire_legacy_home(1)
        _old_version_writes_legacy(world)
        WorkspaceService.retire_legacy_home(1)
        assert len(_retired(world)) == 2

    def test_a_leftover_that_cannot_be_moved_loses_what_na_wrote(self, world, monkeypatch, logs) -> None:
        legacy = _upgraded_rolled_back_and_upgraded_again(world)
        (legacy / "notes.txt").write_text("container's own", encoding="utf-8")

        def busy(*_args: Any, **_kwargs: Any) -> None:
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        monkeypatch.setattr(safe_fs.DirHandle, "rename_to", busy)

        assert WorkspaceService.retire_legacy_home(1) is None

        assert not os.path.lexists(legacy / "settings.json")
        assert not os.path.lexists(legacy / "nk-ssh")
        assert (legacy / "notes.txt").is_file(), "只删 NA 写的那几样"
        assert any(line.startswith("ERROR|") and "没能挪出工作区" in line for line in logs)

    def test_a_leftover_link_is_left_alone(self, world, logs) -> None:
        _fill_legacy(world)
        WorkspaceService.migrate_claude_home(1)
        _link(_legacy(world), world.outside, is_dir=True)

        assert WorkspaceService.retire_legacy_home(1) is None

        assert _legacy(world).is_symlink()
        assert sorted(p.name for p in world.outside.iterdir()) == ["keep.txt"]
        assert any(line.startswith("WARNING|") and "不是 NA 写的" in line for line in logs)

    def test_a_workspace_still_on_the_legacy_layout_is_not_touched(self, world) -> None:
        _fill_legacy(world)
        assert WorkspaceService.retire_legacy_home(1) is None
        assert (_legacy(world) / ".claude.json").is_file()
        assert not (world.root / ".nk-homes").exists()


# ---------------------------------------------------------------------------
# 写的一方先算好了旧位置，迁移随后跑完
# ---------------------------------------------------------------------------

CREDENTIAL = WorkspaceCredential(base_url="http://relay", auth_token="relay-token", relayed=True)


def _preset() -> CCModelPresetItem:
    return CCModelPresetItem(
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


def _write_user_settings(world: SimpleNamespace, home: ClaudeHome) -> bool:
    return WorkspaceService._write_settings_files(  # noqa: SLF001
        world.root / "1",
        _preset(),
        CREDENTIAL,
        "热更新",
        allow_platform_secrets=False,
        home=home,
    )


class TestAWriterThatLocatedTheHomeBeforeTheMove:
    """建容器的迁移在线程里跑；热更新、撤销凭据、动态技能在事件循环里，算好 home 之后才写"""

    def test_user_settings_land_in_the_moved_home(self, world) -> None:
        _fill_legacy(world)
        stale = WorkspaceService.claude_home(1)
        assert stale.legacy is True
        WorkspaceService.migrate_claude_home(1)

        _write_user_settings(world, stale)

        assert not os.path.lexists(_legacy(world)), "不在工作区目录里把旧 home 建回来"
        assert "relay-token" in (_new(world) / "settings.json").read_text(encoding="utf-8")

    def test_ensuring_a_stale_legacy_home_returns_the_new_one(self, world) -> None:
        _legacy(world).mkdir(parents=True)
        stale = WorkspaceService.claude_home(1)
        WorkspaceService.migrate_claude_home(1)

        home = WorkspaceService.ensure_claude_home(stale)

        assert home.legacy is False and home.path == _new(world)
        assert not os.path.lexists(_legacy(world))

    def test_a_linked_legacy_home_is_not_rebuilt_once_a_new_home_exists(self, world) -> None:
        """旧的是链接：迁移不挪它、建一个空的新 home；之前算好旧位置的写入不能把链接修成目录再写进去"""
        _link(_legacy(world), world.outside, is_dir=True)
        stale = WorkspaceService.claude_home(1)
        assert stale.legacy is True
        WorkspaceService.migrate_claude_home(1)

        _write_user_settings(world, stale)

        assert _legacy(world).is_symlink()
        assert (_new(world) / "settings.json").is_file()
        assert sorted(p.name for p in world.outside.iterdir()) == ["keep.txt"]

    def test_a_legacy_home_the_container_deleted_is_not_built_back(self, world) -> None:
        (world.root / "1").mkdir()
        stale = WorkspaceService.legacy_claude_home(1)

        _write_user_settings(world, stale)

        assert not os.path.lexists(_legacy(world))
        assert (_new(world) / "settings.json").is_file()

    def test_dynamic_skills_after_the_move_go_to_the_new_home(self, world) -> None:
        _legacy(world).mkdir(parents=True)
        WorkspaceService.migrate_claude_home(1)

        WorkspaceService.write_dynamic_skill(1, "mine", "---\nname: mine\n---\n")

        assert (_new(world) / "skills" / "mine" / "SKILL.md").is_file()
        assert not os.path.lexists(_legacy(world))

    def test_the_move_waits_for_a_writer_holding_the_home_lock(self, world) -> None:
        _fill_legacy(world)
        finished = threading.Event()

        def migrate() -> None:
            WorkspaceService.migrate_claude_home(1)
            finished.set()

        with manager_module.home_lock(1):
            worker = threading.Thread(target=migrate)
            worker.start()
            assert not finished.wait(0.3), "写的一方拿着锁时迁移要等"
            assert (_legacy(world) / ".claude.json").is_file()
        worker.join(10)
        assert finished.is_set()
        assert not os.path.lexists(_legacy(world))


# ---------------------------------------------------------------------------
# 编号复用与删除
# ---------------------------------------------------------------------------


class TestStaleHomes:
    def test_both_layouts_of_a_reused_id_are_cleared(self, world, logs) -> None:
        _fill_legacy(world)
        _new(world).mkdir(parents=True)
        (_new(world) / ".claude.json").write_text("{}", encoding="utf-8")
        (world.root / "1" / "default").mkdir(parents=True)
        (world.root / "1" / "default" / "user-file.txt").write_text("work", encoding="utf-8")

        removed = WorkspaceService.discard_stale_home(1)

        assert len(removed) == 2
        assert not (world.root / ".nk-homes" / "1").exists()
        assert not os.path.lexists(_legacy(world))
        # 工作区目录里用户的东西不归这里管
        assert (world.root / "1" / "default" / "user-file.txt").is_file()
        assert any(line.startswith("WARNING|") and "以前用过" in line for line in logs)

    def test_a_fresh_id_clears_nothing(self, world, logs) -> None:
        assert WorkspaceService.discard_stale_home(7) == []
        assert not any("以前用过" in line for line in logs)

    def test_a_linked_legacy_home_is_removed_without_touching_its_target(self, world) -> None:
        _link(_legacy(world), world.outside, is_dir=True)
        WorkspaceService.discard_stale_home(1)
        assert not os.path.lexists(_legacy(world))
        assert (world.outside / "keep.txt").is_file()

    def test_removing_a_deleted_workspaces_home_cleans_up_after_itself(self, world) -> None:
        _fill_legacy(world)
        _new(world).mkdir(parents=True)
        (world.root / "1" / "default").mkdir(parents=True)

        removed = WorkspaceService.remove_workspace_home(1)

        assert len(removed) == 2
        assert not (world.root / ".nk-homes" / "1").exists()
        assert not os.path.lexists(_legacy(world))
        assert not (world.root / ".nk-trash" / "1").exists()
        assert (world.root / "1" / "default").is_dir(), "工作区目录本身不删"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


def _platform_owner() -> DBUser:
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


def _client() -> TestClient:
    from nekro_agent.routers import workspaces as workspaces_router

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(workspaces_router.router, prefix="/api")
    app.dependency_overrides[get_current_active_user] = _platform_owner
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def no_container(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager
    from nekro_agent.services.workspace.container import SandboxContainerManager

    async def nothing(*_args: Any, **_kwargs: Any) -> Any:
        return True

    monkeypatch.setattr(SandboxContainerManager, "remove", staticmethod(nothing))
    monkeypatch.setattr(memory_qdrant_manager, "delete_by_workspace", nothing)


class TestCreateAndDeleteThroughTheApi:
    async def test_creating_a_workspace_clears_the_home_left_by_the_same_id(self, db, world) -> None:  # noqa: ARG002
        """数据库重置之后编号从 1 重新数：上一个 1 号工作区的会话与网关私钥不能被新工作区接手"""
        _fill_legacy(world)
        _new(world).mkdir(parents=True)
        (_new(world) / ".claude.json").write_text("{}", encoding="utf-8")

        response = _client().post("/api/workspaces", json={"name": "fresh", "description": ""})

        assert response.status_code == 200, response.text
        assert response.json()["id"] == 1
        assert not (world.root / ".nk-homes" / "1").exists()
        assert not os.path.lexists(_legacy(world))

    async def test_a_home_that_cannot_be_cleared_cancels_the_creation(self, db, world, monkeypatch) -> None:  # noqa: ARG002
        def stuck(_workspace_id: int) -> List[str]:
            raise OSError(errno.EBUSY, "Device or resource busy")

        monkeypatch.setattr(WorkspaceService, "discard_stale_home", staticmethod(stuck))

        response = _client().post("/api/workspaces", json={"name": "fresh", "description": ""})

        assert response.status_code == 500, response.text
        assert await DBWorkspace.filter(name="fresh").count() == 0

    async def test_claim_fresh_home_raises_and_drops_the_row(self, db, world, monkeypatch) -> None:  # noqa: ARG002
        """插件自动建工作区走的也是这一步"""
        workspace = await DBWorkspace.create(name="auto", description="")

        def stuck(_workspace_id: int) -> List[str]:
            raise OSError(errno.EBUSY, "Device or resource busy")

        monkeypatch.setattr(WorkspaceService, "discard_stale_home", staticmethod(stuck))

        with pytest.raises(StaleHomeError):
            await WorkspaceService.claim_fresh_home(workspace)
        assert await DBWorkspace.get_or_none(id=workspace.id) is None

    async def test_deleting_a_workspace_removes_its_home(self, db, world, no_container) -> None:  # noqa: ARG002
        workspace = await DBWorkspace.create(name="doomed", description="", status="stopped")
        _new(world, workspace.id).mkdir(parents=True)
        (_new(world, workspace.id) / "nk-ssh").mkdir()
        (_new(world, workspace.id) / "nk-ssh" / "id_gateway").write_text("PRIVATE", encoding="utf-8")
        (world.root / str(workspace.id) / "default").mkdir(parents=True)

        response = _client().delete(f"/api/workspaces/{workspace.id}")

        assert response.status_code == 200, response.text
        assert not (world.root / ".nk-homes" / str(workspace.id)).exists()
        assert (world.root / str(workspace.id) / "default").is_dir(), "工作区目录照旧不删"

    async def test_deleting_an_unmigrated_workspace_removes_its_legacy_home_too(self, db, world, no_container) -> None:  # noqa: ARG002
        workspace = await DBWorkspace.create(name="old", description="", status="stopped")
        _fill_legacy(world, workspace.id)

        response = _client().delete(f"/api/workspaces/{workspace.id}")

        assert response.status_code == 200, response.text
        assert not os.path.lexists(_legacy(world, workspace.id))

    async def test_the_home_survives_a_delete_that_did_not_commit(self, db, world, no_container, monkeypatch) -> None:  # noqa: ARG002
        """删除排在提交之后：库里的删除失败回滚了，工作区还在，home 不能先没了"""
        workspace = await DBWorkspace.create(name="kept", description="", status="stopped")
        _new(world, workspace.id).mkdir(parents=True)
        original_delete = DBWorkspace.delete

        async def failing_delete(self: DBWorkspace, *args: Any, **kwargs: Any) -> None:
            if self.id == workspace.id:
                raise RuntimeError("database is locked")
            await original_delete(self, *args, **kwargs)

        monkeypatch.setattr(DBWorkspace, "delete", failing_delete)

        response = _client().delete(f"/api/workspaces/{workspace.id}")

        assert response.status_code == 500
        assert _new(world, workspace.id).is_dir()


class TestSettingsSyncAroundADelete:
    """预设同步一次查出所有工作区再逐个写：写到某一个时它可能已经删了（工作区目录删除时保留）"""

    async def test_a_deleted_workspace_gets_no_home_back(self, db, world) -> None:  # noqa: ARG002
        stale = await DBWorkspace.create(name="gone", description="", status="stopped")
        (world.root / str(stale.id)).mkdir()
        await DBWorkspace.filter(id=stale.id).delete()

        await WorkspaceService.update_workspace_settings(stale, None)

        assert not (world.root / ".nk-homes" / str(stale.id)).exists()

    async def test_a_workspace_being_deleted_gets_no_home_back(self, db, world) -> None:  # noqa: ARG002
        stale = await DBWorkspace.create(name="going", description="", status="stopped")
        (world.root / str(stale.id)).mkdir()
        await DBWorkspace.filter(id=stale.id).update(status="deleting")

        await WorkspaceService.update_workspace_settings(stale, None)

        assert not (world.root / ".nk-homes" / str(stale.id)).exists()

    async def test_control_a_live_workspace_is_written(self, db, world) -> None:  # noqa: ARG002
        workspace = await DBWorkspace.create(name="live", description="", status="stopped")
        (world.root / str(workspace.id)).mkdir()

        await WorkspaceService.update_workspace_settings(workspace, None)

        assert _new(world, workspace.id).is_dir()
        assert (world.root / str(workspace.id) / "settings.json").is_file()


# ---------------------------------------------------------------------------
# SSH 网关文件
# ---------------------------------------------------------------------------


class TestGatewayFilesFollowTheHome:
    def test_a_new_workspace_gets_them_outside_the_workspace_dir(self, world) -> None:
        container_files._write_nk_ssh_files_on_host(1, {"config": "cfg", "id_gateway": "KEY"})  # noqa: SLF001
        assert (_new(world) / "nk-ssh" / "id_gateway").read_text(encoding="utf-8") == "KEY"
        assert not os.path.lexists(_legacy(world))
        assert container_files.nk_ssh_host_dir(1) == _new(world) / "nk-ssh"

    def test_an_unmigrated_workspace_gets_them_where_its_container_looks(self, world) -> None:
        _legacy(world).mkdir(parents=True)
        container_files._write_nk_ssh_files_on_host(1, {"config": "cfg"})  # noqa: SLF001
        assert (_legacy(world) / "nk-ssh" / "config").read_text(encoding="utf-8") == "cfg"
        assert not _new(world).exists()

    def test_a_linked_legacy_home_gets_nothing(self, world) -> None:
        _link(_legacy(world), world.outside, is_dir=True)
        with pytest.raises(container_files.UnsafeNkSshPathError):
            container_files._write_nk_ssh_files_on_host(1, {"config": "cfg"})  # noqa: SLF001
        assert sorted(p.name for p in world.outside.iterdir()) == ["keep.txt"]

    def test_removing_them_works_in_the_new_home(self, world) -> None:
        container_files._write_nk_ssh_files_on_host(1, {"config": "cfg", "known_hosts": "kh"})  # noqa: SLF001
        container_files._remove_nk_ssh_files_on_host(1)  # noqa: SLF001
        assert not (_new(world) / "nk-ssh").exists()
        assert _new(world).is_dir()


# ---------------------------------------------------------------------------
# 属主：NA 在 home 里建的、写的东西交给容器用户
# ---------------------------------------------------------------------------


def _skill_source(world: SimpleNamespace) -> Path:
    """技能库里的一个技能（宿主侧、容器碰不到）"""
    src = world.tmp / "library" / "demo"
    (src / "scripts").mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    (src / "scripts" / "run.sh").write_text("echo hi\n", encoding="utf-8")
    return src


def _everything_na_writes(world: SimpleNamespace) -> None:
    """建工作区时 NA 往一个新 home 里写的那几样，外加动态技能与网关文件"""
    (world.root / "1").mkdir(parents=True, exist_ok=True)
    home = WorkspaceService.ensure_claude_home(WorkspaceService.claude_home(1))
    _write_user_settings(world, home)
    skills = WorkspaceService._open_workspace_skills(1, create=True)  # noqa: SLF001
    assert skills is not None
    with skills:
        WorkspaceService._sync_skill_into(skills, _skill_source(world), "demo", category="local", skill_id="demo")  # noqa: SLF001
    WorkspaceService.write_dynamic_skill(1, "mine", "---\nname: mine\n---\n")
    WorkspaceService.write_dynamic_skill_file(1, "mine", "refs/notes.md", "notes")
    container_files._write_nk_ssh_files_on_host(1, {"config": "cfg", "known_hosts": "kh", "id_gateway": "KEY"})  # noqa: SLF001


class TestEveryWritePathHandsItsEntryToTheContainerUser:
    """不依赖系统：把改属主那一步换成记录器，看每条写入路径交了哪些条目"""

    @pytest.fixture
    def handed(self, monkeypatch: pytest.MonkeyPatch) -> List[Any]:
        calls: List[Any] = []

        def chown(handle: safe_fs.DirHandle, name: str, uid: int, gid: int) -> None:
            calls.append((Path(handle.path) / name, uid, gid, "entry"))

        def chown_tree(handle: safe_fs.DirHandle, name: str, uid: int, gid: int, **_kwargs: Any) -> int:
            calls.append((Path(handle.path) / name, uid, gid, "tree"))
            return 1

        monkeypatch.setattr(safe_fs, "CHOWN_SUPPORTED", True)
        monkeypatch.setattr(safe_fs.DirHandle, "chown", chown)
        monkeypatch.setattr(safe_fs.DirHandle, "chown_tree", chown_tree)
        return calls

    @staticmethod
    def _seen(handed: List[Any]) -> set:
        return {(path, how) for path, _uid, _gid, how in handed}

    def test_creating_the_home(self, world, handed) -> None:
        WorkspaceService.ensure_claude_home(WorkspaceService.claude_home(1))
        assert self._seen(handed) == {(_new(world), "entry")}, "只交 home 本身，`.nk-homes` 与 `<id>` 两级是 NA 自己的"

    def test_writing_user_settings(self, world, handed) -> None:
        (world.root / "1").mkdir()
        home = WorkspaceService.ensure_claude_home(WorkspaceService.claude_home(1))
        handed.clear()
        _write_user_settings(world, home)
        assert (_new(world) / "settings.json", "entry") in self._seen(handed)

    def test_creating_the_skills_dir(self, world, handed) -> None:
        WorkspaceService._open_workspace_skills(1, create=True).close()  # noqa: SLF001
        assert (_new(world) / "skills", "entry") in self._seen(handed)

    def test_syncing_a_skill_from_the_library(self, world, handed) -> None:
        skills = WorkspaceService._open_workspace_skills(1, create=True)  # noqa: SLF001
        handed.clear()
        with skills:
            WorkspaceService._sync_skill_into(skills, _skill_source(world), "demo", category="local", skill_id="demo")  # noqa: SLF001
        assert self._seen(handed) == {(_new(world) / "skills" / "demo", "tree")}

    def test_writing_a_dynamic_skill(self, world, handed) -> None:
        WorkspaceService._open_workspace_skills(1, create=True).close()  # noqa: SLF001
        handed.clear()
        WorkspaceService.write_dynamic_skill(1, "mine", "---\nname: mine\n---\n")
        assert (_new(world) / "skills" / "mine", "tree") in self._seen(handed)

    def test_writing_a_file_into_a_dynamic_skill(self, world, handed) -> None:
        WorkspaceService.write_dynamic_skill(1, "mine", "---\nname: mine\n---\n")
        handed.clear()
        WorkspaceService.write_dynamic_skill_file(1, "mine", "refs/notes.md", "notes")
        assert self._seen(handed) == {(_new(world) / "skills" / "mine", "tree")}

    def test_writing_the_gateway_key(self, world, handed) -> None:
        WorkspaceService.ensure_claude_home(WorkspaceService.claude_home(1))
        handed.clear()
        container_files._write_nk_ssh_files_on_host(1, {"config": "cfg", "known_hosts": "kh", "id_gateway": "KEY"})  # noqa: SLF001
        home = _new(world)
        assert (home / "nk-ssh" / "id_gateway", "entry") in self._seen(handed)
        # 与容器里的收权限脚本同一口径：nk-ssh 目录与两个配置文件留给 root
        handed_paths = {path for path, _uid, _gid, _how in handed}
        assert not handed_paths & {home / "nk-ssh", home / "nk-ssh" / "config", home / "nk-ssh" / "known_hosts"}

    def test_everything_goes_to_uid_and_gid_1000(self, world, handed) -> None:
        _everything_na_writes(world)
        assert handed
        assert {(uid, gid) for _path, uid, gid, _how in handed} == {CONTAINER_IDS}

    def test_gateway_config_without_a_key_hands_nothing_in_nk_ssh(self, world, handed) -> None:
        container_files._write_nk_ssh_files_on_host(1, {"config": "cfg", "known_hosts": "kh"})  # noqa: SLF001
        assert all("nk-ssh" not in path.parts for path, _uid, _gid, _how in handed)

    def test_a_refused_chown_is_reported_once(self, world, monkeypatch, logs) -> None:
        def refuse(_handle: safe_fs.DirHandle, _name: str, _uid: int, _gid: int, **_kwargs: Any) -> int:
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(safe_fs, "CHOWN_SUPPORTED", True)
        monkeypatch.setattr(safe_fs.DirHandle, "chown", refuse)
        monkeypatch.setattr(safe_fs.DirHandle, "chown_tree", refuse)
        monkeypatch.setattr(manager_module, "_chown_refusal_reported", False)

        _everything_na_writes(world)

        refusals = [line for line in logs if line.startswith("WARNING|") and "改不了 CC home 里条目的属主" in line]
        assert len(refusals) == 1
        assert (_new(world) / "skills" / "mine" / "refs" / "notes.md").is_file(), "改不了属主不影响写入"

    def test_nothing_is_attempted_where_chown_does_not_exist(self, world, monkeypatch) -> None:
        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("不支持改属主的平台上不该调")

        monkeypatch.setattr(safe_fs, "CHOWN_SUPPORTED", False)
        monkeypatch.setattr(safe_fs.DirHandle, "chown", boom)
        monkeypatch.setattr(safe_fs.DirHandle, "chown_tree", boom)

        _everything_na_writes(world)


@pytest.mark.skipif(not AS_ROOT, reason="真改属主要 POSIX 且以 root 跑")
class TestOwnershipOnDisk:
    def _ids(self, path: Path) -> Any:
        st = os.lstat(path)
        return st.st_uid, st.st_gid

    def test_a_new_home_belongs_to_the_container_user(self, world) -> None:
        _everything_na_writes(world)

        home = _new(world)
        for path in (
            home,
            home / "settings.json",
            home / "skills",
            home / "skills" / "demo",
            home / "skills" / "demo" / "SKILL.md",
            home / "skills" / "demo" / "scripts" / "run.sh",
            home / "skills" / "demo" / ".skill-origin.json",
            home / "skills" / "mine" / "SKILL.md",
            home / "skills" / "mine" / "refs",
            home / "skills" / "mine" / "refs" / "notes.md",
            home / "nk-ssh" / "id_gateway",
        ):
            assert self._ids(path) == CONTAINER_IDS, path
        assert stat.S_IMODE(os.stat(home).st_mode) == 0o777
        assert stat.S_IMODE(os.stat(home / "nk-ssh" / "id_gateway").st_mode) == 0o600, "私钥不因改属主放宽"
        for path in (home / "nk-ssh", home / "nk-ssh" / "config", home / "nk-ssh" / "known_hosts"):
            assert self._ids(path) == (0, 0), path
        for parent in (world.root / ".nk-homes", world.root / ".nk-homes" / "1"):
            assert self._ids(parent) == (0, 0)
            assert stat.S_IMODE(os.stat(parent).st_mode) == 0o700

    def test_a_link_inside_a_skill_is_not_followed(self, world) -> None:
        victim = world.outside / "keep.txt"
        WorkspaceService.write_dynamic_skill(1, "mine", "---\nname: mine\n---\n")
        _link(_new(world) / "skills" / "mine" / "leak", victim, is_dir=False)

        WorkspaceService.write_dynamic_skill(1, "mine", "---\nname: mine\n---\nagain")

        assert self._ids(victim) == (0, 0)
        assert self._ids(_new(world) / "skills" / "mine" / "SKILL.md") == CONTAINER_IDS

    def test_a_migrated_home_keeps_what_is_inside(self, world) -> None:
        legacy = _fill_legacy(world)
        os.chown(legacy / "projects" / "-workspace-default" / "session.jsonl", 0, 0)

        WorkspaceService.migrate_claude_home(1)

        assert self._ids(_new(world) / "projects" / "-workspace-default" / "session.jsonl") == (0, 0), "迁进来的不动"
        assert self._ids(_new(world)) == CONTAINER_IDS
