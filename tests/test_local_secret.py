"""数据目录里的不轮换密钥文件：写完再放到位，读到空文件当损坏重建，并发生成时只认一把

`load_or_create_local_secret` 先写临时文件、刷盘，再 `os.link` 到正式文件名：正式文件一出现就是写完的。
别的进程抢先放好时读它那把；文件系统不支持硬链接时退回 `O_EXCL` 直接建；任何情况下都不留临时文件。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, List

import pytest

from nekro_agent.core.os_env import OsEnv
from nekro_agent.tools import local_secret
from nekro_agent.tools.local_secret import load_or_create_local_secret

NAME = "unit_test.key"


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    target = tmp_path / "data"
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(target))
    return target


def _leftovers(directory: Path) -> List[str]:
    return sorted(item.name for item in directory.iterdir() if item.name != NAME)


class TestCreation:
    def test_it_is_created_once_and_then_reused(self, data_dir: Path) -> None:
        first = load_or_create_local_secret(NAME, purpose="test")

        assert first
        assert (data_dir / NAME).read_text(encoding="utf-8") == first
        assert load_or_create_local_secret(NAME, purpose="test") == first
        assert _leftovers(data_dir) == []

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX 权限位")
    def test_only_the_owner_can_read_it(self, data_dir: Path) -> None:
        load_or_create_local_secret(NAME, purpose="test")

        assert (data_dir / NAME).stat().st_mode & 0o777 == 0o600

    def test_an_empty_file_is_replaced(self, data_dir: Path) -> None:
        """旧写法在「建好了、还没写进去」时崩溃会留下空文件：不能从此一直退回 JWT 派生"""
        data_dir.mkdir(parents=True)
        (data_dir / NAME).write_text("", encoding="utf-8")

        material = load_or_create_local_secret(NAME, purpose="test")

        assert material
        assert (data_dir / NAME).read_text(encoding="utf-8") == material
        assert _leftovers(data_dir) == []

    def test_the_file_appears_only_once_it_is_complete(self, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """放到位（`link`）之前内容已经写完：任何时刻都读不到半截或空的正式文件"""
        seen: List[str] = []
        real_link = os.link

        def watching_link(source: Any, target: Any) -> None:
            seen.append(Path(source).read_text(encoding="utf-8"))
            assert not Path(target).exists()
            real_link(source, target)

        monkeypatch.setattr(local_secret.os, "link", watching_link)

        material = load_or_create_local_secret(NAME, purpose="test")

        assert seen == [material]


class TestConcurrentCreation:
    def test_the_loser_reads_the_winner(self, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def another_process_wins(_source: Any, target: Any) -> None:
            Path(target).write_text("winner-material", encoding="utf-8")
            raise FileExistsError(target)

        monkeypatch.setattr(local_secret.os, "link", another_process_wins)

        assert load_or_create_local_secret(NAME, purpose="test") == "winner-material"
        assert (data_dir / NAME).read_text(encoding="utf-8") == "winner-material"
        assert _leftovers(data_dir) == [], "临时文件要删掉"

    def test_without_hard_links_it_falls_back_to_exclusive_create(self, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def unsupported(_source: Any, _target: Any) -> None:
            raise OSError("hard links are not supported here")

        monkeypatch.setattr(local_secret.os, "link", unsupported)

        material = load_or_create_local_secret(NAME, purpose="test")

        assert material
        assert (data_dir / NAME).read_text(encoding="utf-8") == material
        assert _leftovers(data_dir) == []


class TestItNeverRaises:
    def test_an_unreadable_file_is_left_alone(self, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        data_dir.mkdir(parents=True)
        (data_dir / NAME).write_text("existing-material", encoding="utf-8")
        real_read_text = Path.read_text

        def denied(self: Path, *args: Any, **kwargs: Any) -> str:
            if self.name == NAME:
                raise PermissionError(str(self))
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", denied)

        assert load_or_create_local_secret(NAME, purpose="test") is None
        monkeypatch.setattr(Path, "read_text", real_read_text)
        assert (data_dir / NAME).read_text(encoding="utf-8") == "existing-material", "读不了时不覆盖"

    def test_an_unwritable_place_returns_none(self, data_dir: Path) -> None:
        data_dir.parent.mkdir(parents=True, exist_ok=True)
        data_dir.write_text("a file where the directory should be", encoding="utf-8")

        assert load_or_create_local_secret(NAME, purpose="test") is None
