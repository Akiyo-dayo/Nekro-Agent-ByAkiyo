"""上传图片保留期：只按保留期清聊天上传目录里的图片，不越界、不误删、不阻塞事件循环

被测的是 `nekro_agent/services/upload_retention.py`。这一份用例守四类事：

- **删得准**：只删扩展名属于图片、修改时间早于截止时间的普通文件；其它类型、未过期的、
  根目录下的邮件附件都不动，嵌套层里恰好叫 `email_attachment` 的目录照常清。
- **不越界**：符号链接（文件或目录）、Windows 目录联接、指向根目录之外的子目录都不进；
  根目录自己是指向真实目录的链接时跟随一次（部署方把数据目录放在链接后面是正常做法）。
- **不打断**：单个文件删不掉只记警告并继续；警告条数有上限；扫描 / 删除在线程里分批做；
  跑到一半开关被关掉就在下一批之前停下并标 `stopped_early`。
- **开关与平台边界**：0 / 负数 / 非整数都算关，关着不扫描；这一项不进实例 / 频道覆盖层，
  租户既读不到也写不了，只有平台方一侧能读写。

用例直接按天推时间（`time.time() - N * 86400`），不写绝对时间戳。
"""

from __future__ import annotations

import asyncio
import datetime
import os
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, List, Optional, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import ConfigNotFoundError, PermissionDeniedError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services import upload_retention
from nekro_agent.services.upload_retention import SweepStats, UploadImageRetention
from nekro_agent.services.user.role import Role

RETENTION_KEY = "UPLOADS_IMAGE_RETENTION_DAYS"
TENANT_ID = 7
DAY_SECONDS = 86400


# ==== 固件与工具 ====


class _Recorder:
    """接管模块 logger：项目的日志不走标准 logging，caplog 收不到"""

    def __init__(self) -> None:
        self.records: List[Tuple[str, str]] = []

    def info(self, message: str, *_args: Any, **_kwargs: Any) -> None:
        self.records.append(("info", str(message)))

    def warning(self, message: str, *_args: Any, **_kwargs: Any) -> None:
        self.records.append(("warning", str(message)))

    def debug(self, message: str, *_args: Any, **_kwargs: Any) -> None:
        self.records.append(("debug", str(message)))

    def messages(self, level: str) -> List[str]:
        return [message for recorded_level, message in self.records if recorded_level == level]


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """抓日志；默认不替换的话会写进开发机的日志文件"""
    recorder = _Recorder()
    monkeypatch.setattr(upload_retention, "logger", recorder)
    return recorder


@pytest.fixture
async def db() -> AsyncIterator[None]:
    """租户 / 平台方两条身份都要落到 DBUser 上，路由判据会读它的 tenant_id 与角色"""
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await DBTenant.create(id=TENANT_ID, name="acme", display_name="acme")
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> List[tuple]:
    """拦住真正的写盘：只记下写了什么，不碰开发机上的配置文件"""
    from nekro_agent.routers import config as config_router

    calls: List[tuple] = []

    def record_set(config_key: str, item_key: str, value: str) -> tuple:
        calls.append(("set", config_key, item_key, value))
        return True, ""

    def record_batch(config_key: str, configs: dict) -> tuple:
        calls.append(("batch", config_key, dict(configs)))
        return True, ""

    def record_save(config_key: str) -> tuple:
        calls.append(("save", config_key))
        return True, ""

    monkeypatch.setattr(config_router.UnifiedConfigService, "set_config_value", record_set)
    monkeypatch.setattr(config_router.UnifiedConfigService, "batch_update_config", record_batch)
    monkeypatch.setattr(config_router.UnifiedConfigService, "save_config", record_save)
    return calls


def _write_image(path: Path, *, mtime: float, size: int = 1) -> int:
    """写一个文件并把修改时间钉死；返回字节数（用来精确核对 deleted_bytes）"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))
    return size


def _write_text(path: Path, *, mtime: float, content: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (mtime, mtime))


def _user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    user = DBUser(
        username=f"user-{tenant_role.value}-{perm_level}",
        password="",
        adapter_key="",
        platform_userid="",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )
    user.id = 100 + int(perm_level)
    return user


def tenant_admin() -> DBUser:
    return _user(tenant_id=TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def platform_admin() -> DBUser:
    """平台方一侧、但不是超级管理员的账号"""
    return _user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Admin)


# ==== A. 开关 ====


async def test_disabled_by_zero_does_not_even_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0 是关：不扫、不删、不写 INFO。用 scandir 调用次数证明「没扫描」而不是「扫了没删」"""
    root = tmp_path / "uploads"
    now = time.time()
    old = now - 10 * DAY_SECONDS
    kept = root / "chat_a" / "old.png"
    _write_image(kept, mtime=old)

    monkeypatch.setattr(config, RETENTION_KEY, 0)
    scanned: List[str] = []
    real_scandir = os.scandir

    def counting_scandir(path: Any) -> Any:
        scanned.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", counting_scandir)

    retention = UploadImageRetention()
    assert await retention.run_once(root=root, now=now) is None
    assert kept.exists()
    assert scanned == []


@pytest.mark.parametrize("days", [-1, "abc"])
async def test_negative_and_non_integer_values_are_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    days: Any,
) -> None:
    """负数与非整数都按关处理：坏值不能变成「天天删全部」或异常打掉后台循环"""
    root = tmp_path / "uploads"
    now = time.time()
    old = now - 10 * DAY_SECONDS
    kept = root / "chat_a" / "old.png"
    _write_image(kept, mtime=old)

    monkeypatch.setattr(config, RETENTION_KEY, days)
    assert upload_retention.current_retention_days() == 0

    retention = UploadImageRetention()
    assert await retention.run_once(root=root, now=now) is None
    assert kept.exists()


async def test_tick_while_off_does_not_remember_a_run_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关着的那次 tick 不能记「上次跑的时间」，否则运行中打开要再等一整天"""
    root = tmp_path / "uploads"
    now = time.time()
    old = now - 10 * DAY_SECONDS
    image = root / "chat_a" / "old.png"
    _write_image(image, mtime=old)

    monkeypatch.setattr(upload_retention, "USER_UPLOAD_DIR", str(root))
    monkeypatch.setattr(config, RETENTION_KEY, 0)

    retention = UploadImageRetention()
    assert await retention.tick() is None
    assert retention._last_run_at is None
    assert image.exists()

    monkeypatch.setattr(config, RETENTION_KEY, 7)
    stats = await retention.tick()
    assert isinstance(stats, SweepStats)
    assert stats.deleted_files == 1
    assert not image.exists()


# ==== B. 只删过期图片 ====


# 覆盖判断里点到的扩展名与大小写；`.JPG` / `.Png` 用来证明扩展名比较不分大小写
_IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".JPG", ".Png"]


async def test_only_expired_images_with_image_suffixes_are_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """频道目录与根目录散文件同规则：过期图片全删、未过期全留，计数与字节数精确"""
    root = tmp_path / "uploads"
    now = time.time()
    old = now - 10 * DAY_SECONDS
    fresh = now - 1 * DAY_SECONDS

    expected_files = 0
    expected_bytes = 0
    expired_paths: List[Path] = []
    fresh_paths: List[Path] = []

    for parent in (root, root / "chat_a"):
        for index, extension in enumerate(_IMAGE_EXTENSIONS):
            expired = parent / f"expired_{index}{extension}"
            size = index + 1
            _write_image(expired, mtime=old, size=size)
            expired_paths.append(expired)
            expected_files += 1
            expected_bytes += size

            kept = parent / f"fresh_{index}{extension}"
            _write_image(kept, mtime=fresh, size=size + 32)
            fresh_paths.append(kept)

    monkeypatch.setattr(config, RETENTION_KEY, 7)
    retention = UploadImageRetention()
    stats = await retention.run_once(root=root, now=now)

    assert isinstance(stats, SweepStats)
    assert stats.deleted_files == expected_files
    assert stats.deleted_bytes == expected_bytes
    assert stats.failed_files == 0
    assert all(not path.exists() for path in expired_paths)
    assert all(path.exists() for path in fresh_paths)


async def test_retention_boundary_is_strictly_on_mtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """截止时间两侧各留一分钟：早一分钟的删，晚一分钟的留"""
    root = tmp_path / "uploads"
    now = time.time()
    days = 3
    expired = root / "chat_a" / "just_expired.png"
    kept = root / "chat_a" / "just_fresh.png"
    _write_image(expired, mtime=now - days * DAY_SECONDS - 60, size=5)
    _write_image(kept, mtime=now - days * DAY_SECONDS + 60, size=5)

    monkeypatch.setattr(config, RETENTION_KEY, days)
    retention = UploadImageRetention()
    stats = await retention.run_once(root=root, now=now)

    assert isinstance(stats, SweepStats)
    assert stats.deleted_files == 1
    assert stats.deleted_bytes == 5
    assert not expired.exists()
    assert kept.exists()


async def test_nested_directories_are_scanned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """频道目录下面还有多层：深层旧图片也要删，扫描不能只看一层"""
    root = tmp_path / "uploads"
    now = time.time()
    deep = root / "chat_a" / "sub" / "deeper" / "old.png"
    _write_image(deep, mtime=now - 10 * DAY_SECONDS)

    monkeypatch.setattr(config, RETENTION_KEY, 7)
    retention = UploadImageRetention()
    stats = await retention.run_once(root=root, now=now)

    assert isinstance(stats, SweepStats)
    assert stats.deleted_files == 1
    assert not deep.exists()


# ==== C. 其它类型不动 ====


async def test_non_image_files_stay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """视频、文档、压缩包、数据文件、无扩展名、`photo.jpg.txt`、名字就叫 `jpg` 都不算图片"""
    root = tmp_path / "uploads"
    now = time.time()
    old = now - 10 * DAY_SECONDS
    names = [
        "old_video.mp4",
        "old_doc.pdf",
        "old_note.txt",
        "old_archive.zip",
        "old_data.json",
        "no_extension",
        "photo.jpg.txt",
        "jpg",
    ]
    paths: List[Path] = []
    for parent in (root, root / "chat_a"):
        for name in names:
            path = parent / name
            _write_text(path, mtime=old)
            paths.append(path)

    monkeypatch.setattr(config, RETENTION_KEY, 7)
    retention = UploadImageRetention()
    stats = await retention.run_once(root=root, now=now)

    assert isinstance(stats, SweepStats)
    assert stats.deleted_files == 0
    assert stats.deleted_bytes == 0
    assert all(path.exists() for path in paths)


# ==== D. 邮件附件 ====


async def test_email_attachment_is_skipped_only_at_root_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """根目录下的 `email_attachment` 整棵跳过；频道目录里同名目录只是普通目录，照常清"""
    root = tmp_path / "uploads"
    now = time.time()
    old = now - 10 * DAY_SECONDS
    root_mail = root / "email_attachment" / "acc" / "123" / "old.png"
    nested_mail = root / "chat_a" / "email_attachment" / "old.png"
    _write_image(root_mail, mtime=old)
    _write_image(nested_mail, mtime=old)

    monkeypatch.setattr(config, RETENTION_KEY, 7)
    retention = UploadImageRetention()
    stats = await retention.run_once(root=root, now=now)

    assert isinstance(stats, SweepStats)
    assert root_mail.exists()
    assert not nested_mail.exists()
    assert stats.deleted_files == 1


# ==== E. 符号链接与越界 ====


async def test_symlinks_are_never_followed_or_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """目录链接不进、文件链接不删，链接指向的根目录外文件也不动；同目录普通旧图片照删"""
    root = tmp_path / "uploads"
    outside = tmp_path / "outside"
    now = time.time()
    old = now - 10 * DAY_SECONDS

    victim = outside / "victim.jpg"
    _write_image(victim, mtime=old)
    normal_chat = root / "chat_a" / "normal.jpg"
    normal_root = root / "normal.jpg"
    _write_image(normal_chat, mtime=old)
    _write_image(normal_root, mtime=old)

    directory_link = root / "outside_dir"
    file_link = root / "chat_a" / "link_to_victim.jpg"
    try:
        os.symlink(str(outside), str(directory_link), target_is_directory=True)
        os.symlink(str(victim), str(file_link))
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不能创建符号链接")

    monkeypatch.setattr(config, RETENTION_KEY, 7)
    retention = UploadImageRetention()
    stats = await retention.run_once(root=root, now=now)

    assert isinstance(stats, SweepStats)
    assert stats.deleted_files == 2
    assert victim.exists()
    assert os.path.lexists(directory_link)
    assert os.path.lexists(file_link)
    assert not normal_chat.exists()
    assert not normal_root.exists()


async def test_subdirectory_resolving_outside_root_is_skipped_with_one_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
) -> None:
    """进每个子目录前都要确认 realpath 还在根目录里；不在就跳过整棵并记一条警告"""
    root = tmp_path / "uploads"
    now = time.time()
    old = now - 10 * DAY_SECONDS
    blocked = root / "chat_b" / "old.png"
    allowed = root / "chat_a" / "old.png"
    loose = root / "loose.png"
    for path in (blocked, allowed, loose):
        _write_image(path, mtime=old)

    original = upload_retention._ImageSweep._within_root

    def fake_within_root(self: Any, path: str) -> bool:
        if Path(path).name == "chat_b":
            return False
        return original(self, path)

    monkeypatch.setattr(upload_retention._ImageSweep, "_within_root", fake_within_root)
    monkeypatch.setattr(config, RETENTION_KEY, 7)
    retention = UploadImageRetention()
    stats = await retention.run_once(root=root, now=now)

    assert isinstance(stats, SweepStats)
    assert stats.deleted_files == 2
    assert blocked.exists()
    assert not allowed.exists()
    assert not loose.exists()
    warnings = recorder.messages("warning")
    assert len(warnings) == 1
    assert "chat_b" in warnings[0]


async def test_within_root_check_accepts_children_and_rejects_outside_paths(
    tmp_path: Path,
) -> None:
    """`_within_root` 本身：子目录 True；根目录外的目录 False；`uploads/../outside` 这种写法也 False"""
    root = tmp_path / "uploads"
    child = root / "chat_a"
    child.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    real_root = Path(os.path.realpath(root))
    sweep = upload_retention._ImageSweep(real_root, 0.0)

    assert sweep._within_root(str(child)) is True
    assert sweep._within_root(str(outside)) is False
    assert sweep._within_root(str(root / ".." / "outside")) is False


@pytest.mark.skipif(os.name != "nt", reason="目录联接只在 Windows 上存在")
async def test_windows_junction_pointing_outside_root_is_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows 的目录联接不是符号链接：即使 `is_symlink()` 为假，也不能进根目录外的目标"""
    root = tmp_path / "uploads"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    now = time.time()
    old = now - 10 * DAY_SECONDS
    victim = outside / "victim.jpg"
    _write_image(victim, mtime=old)

    try:
        import _winapi

        _winapi.CreateJunction(str(outside), str(root / "junc"))
    except (ImportError, OSError, NotImplementedError, AttributeError):
        pytest.skip("当前环境不能创建目录联接")

    monkeypatch.setattr(config, RETENTION_KEY, 7)
    retention = UploadImageRetention()
    stats = await retention.run_once(root=root, now=now)

    assert isinstance(stats, SweepStats)
    assert stats.deleted_files == 0
    assert victim.exists()
    assert (root / "junc").exists()


async def test_junction_like_entry_is_not_followed(
    tmp_path: Path,
) -> None:
    """跨平台守住「认出联接就不进」这条判据（真联接只能在 Windows 上建，见下面两条）

    这里用一个「目录联接形态」的桩条目（3.12 起 `DirEntry` 带 `is_junction`）直接走 `_handle_entry`：
    `is_junction()` 为真且 `is_dir(follow_symlinks=False)` 也为真（真实目录联接就是这样）。正确的实现必须在
    这里直接返回；去掉那条判据后，桩条目会被当成普通目录排进待扫队列，目标里的旧图片就被删了。
    """
    root = tmp_path / "uploads"
    inside = root / "inside"
    now = time.time()
    image = inside / "keep.jpg"
    _write_image(image, mtime=now - 10 * DAY_SECONDS)

    class _JunctionLikeEntry:
        name = "junc"
        path = str(inside)

        def is_symlink(self) -> bool:
            return False

        def is_junction(self) -> bool:
            return True

        def is_dir(self, *, follow_symlinks: bool = True) -> bool:
            return True

        def is_file(self, *, follow_symlinks: bool = True) -> bool:
            return False

        def stat(self, *, follow_symlinks: bool = True) -> Any:
            raise AssertionError("目录联接不该进入 stat 分支")

    # 用例直接调私有扫描器：与分批那条用例一样，为了逐条控制步进。
    # 先拿掉初始的根目录条目，让这一步只取决于桩条目本身，避免根目录被正常扫描后连累目标
    sweep = upload_retention._ImageSweep(Path(os.path.realpath(root)), now - 7 * DAY_SECONDS, 100)
    sweep._pending_dirs.pop()
    sweep._handle_entry(_JunctionLikeEntry())  # type: ignore[arg-type]

    assert sweep.step() is True
    assert image.exists()


def _make_junction(target: Path, link: Path) -> None:
    """建 Windows 目录联接；建不了就 skip"""
    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    except (ImportError, OSError, NotImplementedError, AttributeError):
        pytest.skip("当前环境不能创建目录联接")


@pytest.mark.skipif(os.name != "nt", reason="目录联接只在 Windows 上存在")
async def test_windows_junction_inside_root_is_not_entered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """指向根目录**内部**的联接：realpath 那道兜不住（目标确实在根目录里），只能靠认出联接本身

    3.11 的 `DirEntry` 没有 `is_junction`，要看 reparse tag；认不出时联接会被当普通目录再扫一遍。
    """
    root = tmp_path / "uploads"
    inside = root / "inside"
    now = time.time()
    image = inside / "old.png"
    _write_image(image, mtime=now - 10 * DAY_SECONDS)
    _make_junction(inside, root / "junc")

    scanned: List[str] = []
    real_scandir = os.scandir

    def recording_scandir(path: Any) -> Any:
        scanned.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", recording_scandir)
    monkeypatch.setattr(config, RETENTION_KEY, 7)
    retention = UploadImageRetention()
    stats = await retention.run_once(root=root, now=now)

    assert isinstance(stats, SweepStats)
    assert stats.deleted_files == 1
    assert not image.exists()
    assert [path for path in scanned if Path(path).name == "junc"] == []
    assert os.path.lexists(root / "junc")


@pytest.mark.skipif(os.name != "nt", reason="目录联接只在 Windows 上存在")
async def test_is_junction_recognizes_a_real_junction(tmp_path: Path) -> None:
    """`_is_junction` 对真联接为真、对普通目录为假（不管当前 Python 有没有 `DirEntry.is_junction`）"""
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "plain").mkdir()
    _make_junction(target, tmp_path / "junc")

    with os.scandir(tmp_path) as iterator:
        entries = {entry.name: entry for entry in iterator}

    assert upload_retention._is_junction(entries["junc"]) is True
    assert upload_retention._is_junction(entries["plain"]) is False
    assert upload_retention._is_junction(entries["target"]) is False


async def test_missing_root_returns_empty_stats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """根目录不存在是正常的启动竞态：返回全 0 的 SweepStats，不能抛异常打掉循环"""
    monkeypatch.setattr(config, RETENTION_KEY, 7)
    retention = UploadImageRetention()
    stats = await retention.run_once(root=tmp_path / "nope", now=time.time())

    assert stats == SweepStats()
    assert stats.failed_files == 0


async def test_root_symlink_is_followed_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """根目录自己是指向真实目录的链接时跟随一次（文档写明了）；目标是根，链本身不删"""
    real = tmp_path / "real_uploads"
    real.mkdir()
    now = time.time()
    image = real / "chat_a" / "old.png"
    _write_image(image, mtime=now - 10 * DAY_SECONDS)

    root_link = tmp_path / "uploads"
    try:
        os.symlink(str(real), str(root_link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不能创建符号链接")

    monkeypatch.setattr(config, RETENTION_KEY, 7)
    retention = UploadImageRetention()
    stats = await retention.run_once(root=root_link, now=now)

    assert isinstance(stats, SweepStats)
    assert stats.deleted_files == 1
    assert not image.exists()
    assert os.path.lexists(root_link)


# ==== F. 失败、分批、日志 ====


async def test_single_failure_is_counted_and_warned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
) -> None:
    """一个文件删不掉：计入 failed_files、记一条警告、其余照删，不能整轮失败"""
    root = tmp_path / "uploads"
    now = time.time()
    old = now - 10 * DAY_SECONDS
    locked = root / "chat_a" / "locked.jpg"
    other = root / "chat_a" / "other.jpg"
    _write_image(locked, mtime=old)
    _write_image(other, mtime=old)

    real_unlink = os.unlink

    def flaky_unlink(path: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(path).name == "locked.jpg":
            raise PermissionError(13, "denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", flaky_unlink)
    monkeypatch.setattr(config, RETENTION_KEY, 7)
    retention = UploadImageRetention()
    stats = await retention.run_once(root=root, now=now)

    assert isinstance(stats, SweepStats)
    assert stats.failed_files == 1
    assert stats.deleted_files == 1
    assert locked.exists()
    assert not other.exists()
    warnings = recorder.messages("warning")
    assert len(warnings) == 1
    assert "PermissionError" in warnings[0]


async def test_failure_warnings_are_capped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
) -> None:
    """警告最多 MAX_FAILURE_WARNINGS 条，但失败计数一个不能少（防止一次故障刷满日志）"""
    root = tmp_path / "uploads"
    now = time.time()
    old = now - 10 * DAY_SECONDS
    for index in range(5):
        _write_image(root / "chat_a" / f"locked_{index}.jpg", mtime=old)

    def always_fail(path: Any, *args: Any, **kwargs: Any) -> Any:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(os, "unlink", always_fail)
    monkeypatch.setattr(upload_retention, "MAX_FAILURE_WARNINGS", 2)
    monkeypatch.setattr(config, RETENTION_KEY, 7)
    retention = UploadImageRetention()
    stats = await retention.run_once(root=root, now=now)

    assert isinstance(stats, SweepStats)
    assert stats.failed_files == 5
    assert stats.deleted_files == 0
    assert len(recorder.messages("warning")) == 2


async def test_file_not_found_is_not_a_failure_or_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
) -> None:
    """扫描之后被别处删掉（FileNotFoundError）：结果一样，不算失败、不记警告"""
    root = tmp_path / "uploads"
    now = time.time()
    old = now - 10 * DAY_SECONDS
    gone = root / "chat_a" / "gone.jpg"
    other = root / "chat_a" / "other.jpg"
    _write_image(gone, mtime=old)
    _write_image(other, mtime=old)

    real_unlink = os.unlink

    def vanishing_unlink(path: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(path).name == "gone.jpg":
            raise FileNotFoundError(2, "missing")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", vanishing_unlink)
    monkeypatch.setattr(config, RETENTION_KEY, 7)
    retention = UploadImageRetention()
    stats = await retention.run_once(root=root, now=now)

    assert isinstance(stats, SweepStats)
    assert stats.failed_files == 0
    assert stats.deleted_files == 1
    assert not other.exists()
    assert recorder.messages("warning") == []


async def test_info_line_has_counts_but_no_file_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
) -> None:
    """每轮恰好一行 INFO：带删除个数与字节数；文件名一个都不能出现（日志会外流）"""
    root = tmp_path / "uploads"
    now = time.time()
    old = now - 10 * DAY_SECONDS
    names = ["alpha_secret.png", "beta_secret.gif", "gamma_secret.jpg"]
    expected_bytes = 0
    for index, name in enumerate(names):
        expected_bytes += _write_image(root / "chat_a" / name, mtime=old, size=index + 1)

    monkeypatch.setattr(config, RETENTION_KEY, 7)
    retention = UploadImageRetention()
    stats = await retention.run_once(root=root, now=now)

    infos = recorder.messages("info")
    assert len(infos) == 1
    message = infos[0]
    assert f"删除 {stats.deleted_files} 个图片" in message
    assert f"{expected_bytes} 字节" in message
    assert all(name not in message for name in names)


async def test_deletion_runs_in_a_worker_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """扫描与删除都走 asyncio.to_thread：unlink 的线程不能是跑用例的主线程"""
    root = tmp_path / "uploads"
    now = time.time()
    old = now - 10 * DAY_SECONDS
    _write_image(root / "chat_a" / "old.png", mtime=old)

    real_unlink = os.unlink
    thread_ids: List[int] = []

    def recording_unlink(path: Any, *args: Any, **kwargs: Any) -> Any:
        thread_ids.append(threading.get_ident())
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", recording_unlink)
    monkeypatch.setattr(config, RETENTION_KEY, 7)
    main_thread_id = threading.get_ident()
    retention = UploadImageRetention()
    stats = await retention.run_once(root=root, now=now)

    assert stats is not None
    assert stats.deleted_files == 1
    assert thread_ids
    assert all(thread_id != main_thread_id for thread_id in thread_ids)


async def test_sweep_processes_in_batches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """直接驱动扫描器：批大小 2 时 5 个文件不可能一步扫完，最后都要删掉"""
    root = tmp_path / "uploads"
    now = time.time()
    old = now - 10 * DAY_SECONDS
    paths = [root / f"file_{index}.png" for index in range(5)]
    for path in paths:
        _write_image(path, mtime=old)

    real_root = Path(os.path.realpath(root))
    sweep = upload_retention._ImageSweep(real_root, now - 7 * DAY_SECONDS, 2)

    saw_incomplete = False
    for _ in range(50):
        if sweep.step():
            break
        saw_incomplete = True
    else:
        pytest.fail("扫描器一直没有返回完成")

    assert saw_incomplete
    assert sweep.stats.deleted_files == 5
    assert sweep.stats.failed_files == 0
    assert all(not path.exists() for path in paths)


async def test_switch_turned_off_mid_run_stops_before_next_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
) -> None:
    """批大小 1：第一张删掉后关上开关，下一批之前就要停，并标 stopped_early"""
    root = tmp_path / "uploads"
    now = time.time()
    old = now - 10 * DAY_SECONDS
    for index in range(6):
        _write_image(root / f"file_{index}.png", mtime=old)

    real_unlink = os.unlink
    calls = {"count": 0}

    def closing_unlink(path: Any, *args: Any, **kwargs: Any) -> Any:
        result = real_unlink(path, *args, **kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            config.UPLOADS_IMAGE_RETENTION_DAYS = 0
        return result

    monkeypatch.setattr(os, "unlink", closing_unlink)
    monkeypatch.setattr(upload_retention, "BATCH_SIZE", 1)
    monkeypatch.setattr(config, RETENTION_KEY, 7)
    retention = UploadImageRetention()
    stats = await retention.run_once(root=root, now=now)

    assert isinstance(stats, SweepStats)
    assert stats.stopped_early is True
    assert stats.deleted_files == 1
    remaining = [path for path in root.iterdir() if path.is_file()]
    assert len(remaining) == 5
    infos = recorder.messages("info")
    assert len(infos) == 1
    assert "提前结束" in infos[0]


# ==== G. 循环 ====


async def test_tick_runs_once_per_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tick 连着调只用跑一轮；把上次时间往前推满一天后又跑一轮"""
    root = tmp_path / "uploads"
    now = time.time()
    old = now - 10 * DAY_SECONDS
    image = root / "chat_a" / "old.png"
    _write_image(image, mtime=old)

    monkeypatch.setattr(upload_retention, "USER_UPLOAD_DIR", str(root))
    monkeypatch.setattr(config, RETENTION_KEY, 7)
    retention = UploadImageRetention()

    first = await retention.tick()
    assert isinstance(first, SweepStats)
    assert first.deleted_files == 1
    assert await retention.tick() is None

    assert retention._last_run_at is not None
    retention._last_run_at -= upload_retention.RUN_INTERVAL_SECONDS + 1
    third = await retention.tick()
    assert isinstance(third, SweepStats)


async def test_background_loop_cleans_and_stop_clears_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start/stop：启动延迟与 tick 间隔都缩到 10 毫秒，旧图片在 5 秒内被清掉，stop 后 _task 归 None"""
    root = tmp_path / "uploads"
    now = time.time()
    image = root / "chat_a" / "old.png"
    _write_image(image, mtime=now - 10 * DAY_SECONDS)

    monkeypatch.setattr(upload_retention, "STARTUP_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(upload_retention, "TICK_SECONDS", 0.01)
    monkeypatch.setattr(upload_retention, "USER_UPLOAD_DIR", str(root))
    monkeypatch.setattr(config, RETENTION_KEY, 7)
    retention = UploadImageRetention()

    await retention.start()
    deadline = time.monotonic() + 5
    while image.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not image.exists()

    await retention.stop()
    assert retention._task is None


# ==== H. 平台级限制 ====


async def test_retention_key_is_registered_as_platform_only() -> None:
    """`_is_platform_only_config` 只对 system 作用域命中；plugin_basic 里的同名项不算"""
    from nekro_agent.routers import config as config_router

    assert config_router._is_platform_only_config("system", RETENTION_KEY) is True
    assert config_router._is_platform_only_config("plugin_basic", RETENTION_KEY) is False


async def test_config_field_default_and_metadata() -> None:
    """字段默认 0、不可覆盖、中英文案齐全：缺一项前端就会出现「只有中文」或空白的配置项"""
    from nekro_agent.core.config import CoreConfig

    field = CoreConfig.model_fields[RETENTION_KEY]
    assert field.default == 0
    extra = field.json_schema_extra
    assert isinstance(extra, dict)
    assert not extra.get("overridable")
    for key in ("i18n_category", "i18n_title", "i18n_description"):
        text = extra.get(key)
        assert isinstance(text, dict)
        assert str(text.get("zh-CN", "")).strip()
        assert str(text.get("en-US", "")).strip()


async def test_override_layer_has_no_retention_item() -> None:
    """覆盖层由 `overridable` 标记生成：没有这个字段，批量写也以「配置项不存在」返回失败"""
    from nekro_agent.core.overridable_config import OverridableConfig
    from nekro_agent.services.config_service import ConfigService

    assert RETENTION_KEY not in OverridableConfig.model_fields
    success, _message = ConfigService.batch_update_config(OverridableConfig(), {RETENTION_KEY: "1"})
    assert success is False


async def test_tenant_cannot_write_retention_days(db: None, writes: List[tuple]) -> None:
    """租户写这一项在三条路径上都被「平台方权限边界」挡住，且一次都没落到写盘"""
    from nekro_agent.routers import config as config_router

    with pytest.raises(PermissionDeniedError) as excinfo:
        await config_router._assert_tenant_may_write(tenant_admin(), "system", {RETENTION_KEY: "1"})
    assert "平台方权限边界" in str(excinfo.value.detail)

    with pytest.raises(PermissionDeniedError):
        await config_router.set_config_value(
            config_key="system",
            item_key=RETENTION_KEY,
            value="1",
            _current_user=tenant_admin(),
        )
    with pytest.raises(PermissionDeniedError):
        await config_router.batch_update_config(
            config_key="system",
            body=config_router.BatchUpdateConfig(configs={RETENTION_KEY: "1"}),
            _current_user=tenant_admin(),
        )
    assert writes == []


async def test_tenant_cannot_read_retention_days(db: None) -> None:
    """租户列表里没有这一项（别的系统项还在），单项读以「不存在」掩盖存在性"""
    from nekro_agent.routers import config as config_router

    items = await config_router.get_config_list(config_key="system", _current_user=tenant_admin())
    keys = {item.key for item in items}
    assert RETENTION_KEY not in keys
    assert "COMMAND_ENABLED" in keys

    with pytest.raises(ConfigNotFoundError):
        await config_router.get_config_item(
            config_key="system",
            item_key=RETENTION_KEY,
            _current_user=tenant_admin(),
        )


async def test_platform_admin_can_read_and_write_retention_days(db: None, writes: List[tuple]) -> None:
    """平台方普通管理员读得到默认值、也写得进去：这一项是平台方自己的运维参数"""
    from nekro_agent.routers import config as config_router

    item = await config_router.get_config_item(
        config_key="system",
        item_key=RETENTION_KEY,
        _current_user=platform_admin(),
    )
    assert item.value == 0

    await config_router.set_config_value(
        config_key="system",
        item_key=RETENTION_KEY,
        value="7",
        _current_user=platform_admin(),
    )
    assert writes == [("set", "system", RETENTION_KEY, "7")]
