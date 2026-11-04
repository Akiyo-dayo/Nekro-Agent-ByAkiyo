"""配置写盘必须是原子的

`ConfigBase.dump_config` 与 `OverridableConfigBase.dump_config` 原来都是 `Path.write_text`：
先截断再写。写到一半失败（磁盘满、进程被杀）留下一个截断的 YAML，而 `CoreConfig` 的加载是
严格校验的，下次启动直接起不来——配置是"丢了就回不来"的那类数据。

这里钉死五件事，每条都写清了"撤掉哪一行修复会红"：

1. 正常写出的字节与 `Path.write_text` 完全一致（不能为了原子性改变换行/编码行为）；
2. 失败点落在写内容之后（`fsync`）与原文件换名之前（`os.replace`）时，原文件逐字节不变、
   目录里不留临时文件；失败注入前先断言临时文件确实存在且已有内容，证明"写到一半"真的发生过；
3. 目标原本不存在时失败，目录里什么都不该多出来；
4. 权限位：目标已存在时保留原权限；目标不存在时与 `write_text` 新建的一致；
5. `.json` 后缀同样走原子路径（两个分支都要改，只改一个就是漏）。
"""

import os
import shutil
import sys
from pathlib import Path
from typing import Any, List

import pytest
import yaml

from nekro_agent.core.core_utils import ConfigBase, atomic_write_text
from nekro_agent.core.overridable_config import OverridableConfig

OLD_CONTENT = "NAME: 旧内容\nVALUE: 1\n"
NEW_CONTENT = "NAME: 新内容\nVALUE: 2\n"


class ToyConfig(ConfigBase):
    NAME: str = "默认"
    VALUE: int = 0


class ToyOverride(OverridableConfig):
    """最小覆盖配置：只留一个真正启用的覆盖对，避免用例被无关字段淹没"""

    enable_NAME: bool = True
    NAME: str = "覆盖名"


def make_config(target: Path, *, content: str = OLD_CONTENT) -> Any:
    """造一个指向 `target` 的最小配置实例（不碰任何真实配置路径）"""
    if not target.exists():
        target.write_text(content, encoding="utf-8")
    config = ToyConfig()
    config.set_instance_config_file_path(target)
    return config


def make_override(target: Path, *, content: str = OLD_CONTENT) -> Any:
    if not target.exists():
        target.write_text(content, encoding="utf-8")
    override = ToyOverride()
    override.set_instance_config_file_path(target)
    return override


def directory_entries(tmp_path: Path) -> List[str]:
    return sorted(entry.name for entry in tmp_path.iterdir())


def boom_factory(monkeypatch: pytest.MonkeyPatch, target_name: str, tmp_path: Path) -> None:
    """把 `atomic_write_text` 内部用到的某个 OS 操作换成"先断言临时文件已在、再抛磁盘满"

    断言放在抛错之前，是为了证明失败真的发生在"内容已经写进临时文件之后"——
    否则这条用例可能只是证明了"写入没开始所以原文件没动"。
    """

    def boom(*_args: Any, **_kwargs: Any) -> None:
        leftovers = [name for name in directory_entries(tmp_path) if name.startswith(".")]
        assert leftovers, "失败注入时应当已经存在临时文件（否则没测到'写到一半'）"
        for name in leftovers:
            assert (tmp_path / name).read_bytes(), "临时文件里应当已经写进了内容"
        raise OSError("disk full")

    monkeypatch.setattr(f"nekro_agent.core.core_utils.{target_name}", boom)


class TestNormalWrite:
    def test_bytes_match_write_text(self, tmp_path: Path) -> None:
        """换行 / 编码行为必须与 `Path.write_text` 完全一致"""
        target = tmp_path / "config.yaml"
        reference = tmp_path / "reference.yaml"
        config = ToyConfig()
        config.set_instance_config_file_path(target)

        config.dump_config()

        expected = yaml.dump(data=config._dump_data(), default_flow_style=False, sort_keys=False, allow_unicode=True)  # noqa: SLF001
        # 对照文件用 write_text 写同一份文本：`os.fdopen` 若设了 newline= 之类就会在这里对不上
        reference.write_text(expected, encoding="utf-8")

        assert target.read_bytes() == reference.read_bytes()

    def test_no_temp_files_left_behind(self, tmp_path: Path) -> None:
        target = tmp_path / "config.yaml"
        config = make_config(target)

        config.dump_config()

        assert directory_entries(tmp_path) == ["config.yaml"]

    def test_newline_behaviour_matches_write_text(self, tmp_path: Path) -> None:
        """内容里带 `\\n` 时逐字节相同：`os.fdopen` 不能额外设 `newline=`"""
        target = tmp_path / "config.yaml"
        reference = tmp_path / "reference.yaml"
        text = "NAME: a\nb\n"

        atomic_write_text(target, text)
        reference.write_text(text, encoding="utf-8")

        assert target.read_bytes() == reference.read_bytes()


class TestHalfwayFailureKeepsOldFile:
    @pytest.mark.parametrize("os_op", ["os.fsync", "os.replace"])
    def test_config_base(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, os_op: str) -> None:
        """撤掉 `atomic_write_text` 的临时文件 + `os.replace` 会在这里红（原文件会被截断）"""
        target = tmp_path / "config.yaml"
        config = make_config(target)
        old_bytes = target.read_bytes()
        boom_factory(monkeypatch, os_op, tmp_path)

        with pytest.raises(OSError):
            config.dump_config()

        assert target.read_bytes() == old_bytes
        assert directory_entries(tmp_path) == ["config.yaml"]

    @pytest.mark.parametrize("os_op", ["os.fsync", "os.replace"])
    def test_overridable_config_base(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, os_op: str) -> None:
        """覆盖层配置同样不能写一半：撤掉 `overridable_config.py` 改走 `atomic_write_text` 就红"""
        target = tmp_path / "override.yaml"
        override = make_override(target)
        old_bytes = target.read_bytes()
        boom_factory(monkeypatch, os_op, tmp_path)

        with pytest.raises(OSError):
            override.dump_config()

        assert target.read_bytes() == old_bytes
        assert directory_entries(tmp_path) == ["override.yaml"]

    def test_write_failure_also_cleans_up(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """写入本身失败（不是 fsync）时同样不能留临时文件"""
        target = tmp_path / "config.yaml"
        config = make_config(target)
        old_bytes = target.read_bytes()
        real_fdopen = os.fdopen

        class _FailingFile:
            """包住真实的文件对象，只让 `write` 失败；close 仍走真实句柄"""

            def __init__(self, handle: Any) -> None:
                self._handle = handle

            def write(self, _text: str) -> int:
                raise OSError("disk full")

            def __getattr__(self, name: str) -> Any:
                return getattr(self._handle, name)

        def failing_fdopen(fd: int, *args: Any, **kwargs: Any) -> _FailingFile:
            return _FailingFile(real_fdopen(fd, *args, **kwargs))

        monkeypatch.setattr("nekro_agent.core.core_utils.os.fdopen", failing_fdopen)

        with pytest.raises(OSError):
            config.dump_config()

        assert target.read_bytes() == old_bytes
        assert directory_entries(tmp_path) == ["config.yaml"]


class TestFailureWithNoExistingTarget:
    def test_directory_is_empty_after_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """目标原本不存在：失败后既不该出现目标文件，也不该留下临时文件"""
        target = tmp_path / "config.yaml"
        config = ToyConfig()
        config.set_instance_config_file_path(target)
        boom_factory(monkeypatch, "os.fsync", tmp_path)

        with pytest.raises(OSError):
            config.dump_config()

        assert directory_entries(tmp_path) == []


class TestCopymodeIsCalled:
    """权限位的两条用例在 Windows 上 skip，这里不依赖平台：盯 `shutil.copymode` 的调用

    撤掉 `atomic_write_text` 里的 `shutil.copymode(path, tmp_path)` 就红（Windows 开发机上
    权限位用例全 skip，删掉这一行不会有任何用例变红，所以单独钉住）。
    """

    @staticmethod
    def _spy(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
        calls: List[Any] = []
        real_copymode = shutil.copymode

        def spy(src: Any, dst: Any) -> None:
            calls.append((Path(src), Path(dst)))
            real_copymode(src, dst)

        monkeypatch.setattr("nekro_agent.core.core_utils.shutil.copymode", spy)
        return calls

    def test_existing_target_mode_is_carried_to_temp_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "config.yaml"
        target.write_text(OLD_CONTENT, encoding="utf-8")
        calls = self._spy(monkeypatch)

        atomic_write_text(target, NEW_CONTENT)

        assert len(calls) == 1
        source, temp = calls[0]
        assert source == target
        assert temp.parent == tmp_path
        assert temp.name.startswith(".config.yaml.")

    def test_new_target_has_nothing_to_copy(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._spy(monkeypatch)

        atomic_write_text(tmp_path / "config.yaml", NEW_CONTENT)

        assert calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 没有 POSIX 权限位")
class TestPermissionBits:
    def test_existing_file_mode_is_preserved(self, tmp_path: Path) -> None:
        """就地改写时权限本来不变；改成"新建再换名"之后必须显式 `copymode`"""
        target = tmp_path / "config.yaml"
        config = make_config(target)
        os.chmod(target, 0o640)

        config.dump_config()

        assert target.stat().st_mode & 0o777 == 0o640

    def test_new_file_mode_matches_write_text(self, tmp_path: Path) -> None:
        target = tmp_path / "config.yaml"
        reference = tmp_path / "reference.yaml"
        config = ToyConfig()
        config.set_instance_config_file_path(target)

        config.dump_config()
        reference.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")

        assert target.stat().st_mode & 0o777 == reference.stat().st_mode & 0o777


class TestJsonBranch:
    def test_json_uses_atomic_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """撤掉 `ConfigBase.dump_config` 的 `.json` 分支就红：它会走回 `write_text`，注入不生效"""
        target = tmp_path / "config.json"
        config = make_config(target, content='{"NAME": "旧内容", "VALUE": 1}\n')
        old_bytes = target.read_bytes()
        boom_factory(monkeypatch, "os.fsync", tmp_path)

        with pytest.raises(OSError):
            config.dump_config()

        assert target.read_bytes() == old_bytes
        assert directory_entries(tmp_path) == ["config.json"]
