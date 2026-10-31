"""命令状态文件名的路径安全与存量文件兼容

`services/command/manager.py` 曾把接口传来的 `chat_key` 原样拼进文件名
（`COMMAND_CHANNEL_STATE_DIR / f"{chat_key}.json"`），于是"往哪写"由调用方决定：
租户侧现在被"频道必须在库里存在"挡住了，但**未启用多租户的部署里普通 `Role.Admin`
仍可穿越**——那里 `get_tenant_scope` 恒为 None，归属校验整个不进。

这里钉死三条：

1. 清洗后的文件名恒为目标目录的直接子文件，任何 `..` / 路径分隔符都出不去；
2. 清洗只对含特殊字符的 `chat_key` 改变结果，常规 `chat_key`（绝大多数适配器）必须
   保持原文件名不变，否则一次升级就把所有既有频道配置弄丢；
3. 确实改名的那些频道，旧文件被**迁移**到新文件名而不是被无视——用户已经存在的
   命令启停/权限配置不能凭空消失，旧文件也不该永远留在磁盘上。
"""

import json
from pathlib import Path

import pytest

from nekro_agent.services.command import manager as manager_module
from nekro_agent.services.command.base import CommandPermission
from nekro_agent.services.command.manager import CommandManager, channel_state_file

# (chat_key, 期望文件名)
NAMING_CASES = [
    pytest.param("onebot_v11-ob1000000003-group_555000111", "onebot_v11-ob1000000003-group_555000111.json", id="plain"),
    pytest.param("wechat_ilink_multi-a:b-group_1", "wechat_ilink_multi-a_b-group_1.json", id="colon"),
    pytest.param("../../../etc/passwd", ".._.._.._etc_passwd.json", id="dot-dot-traversal"),
    pytest.param("..\\..\\windows\\win", ".._.._windows_win.json", id="backslash-traversal"),
    pytest.param("/absolute/path", "_absolute_path.json", id="absolute"),
    pytest.param("a b*c?", "a_b_c_.json", id="shell-and-glob-chars"),
]


class TestFileNameIsConfinedToStateDir:
    @pytest.mark.parametrize(("chat_key", "expected_name"), NAMING_CASES)
    def test_name_is_sanitized(self, tmp_path: Path, chat_key: str, expected_name: str) -> None:
        path = channel_state_file(str(tmp_path), chat_key)
        assert path.name == expected_name
        assert path.parent == tmp_path

    @pytest.mark.parametrize(("chat_key", "expected_name"), NAMING_CASES)
    def test_written_file_stays_inside(self, tmp_path: Path, chat_key: str, expected_name: str) -> None:
        """真的写一遍：落盘位置必须仍在目标目录内"""
        path = channel_state_file(str(tmp_path), chat_key)
        path.write_text("{}", encoding="utf-8")
        assert [child.name for child in tmp_path.iterdir()] == [expected_name]


class TestLegacyFilesAreAdopted:
    """清洗前命名的存量文件要迁移过来，不能让用户的既有配置失效

    这几条要真的落盘，所以取 `@`（会被清洗、同时在 Windows 上也是合法文件名）
    而不是上面命名表里的 `:`——后者在 Windows 上会被解释成 NTFS 数据流，
    造不出对应的存量文件。生产部署在 Linux，两者走的是同一条代码路径。
    """

    LEGACY_CHAT_KEY = "email-inst1-user@example.com"
    LEGACY_NAME = "email-inst1-user@example.com.json"
    SANITIZED_NAME = "email-inst1-user_example.com.json"

    def test_legacy_file_is_renamed_and_readable(self, tmp_path: Path) -> None:
        legacy = tmp_path / self.LEGACY_NAME
        legacy.write_text(json.dumps({"help": False}), encoding="utf-8")

        path = channel_state_file(str(tmp_path), self.LEGACY_CHAT_KEY)

        assert path.name == self.SANITIZED_NAME
        assert json.loads(path.read_text(encoding="utf-8")) == {"help": False}
        assert not legacy.exists()

    def test_sanitized_file_wins_over_legacy(self, tmp_path: Path) -> None:
        """两份都在时以清洗后的那份为准，绝不覆盖它"""
        legacy = tmp_path / self.LEGACY_NAME
        legacy.write_text(json.dumps({"help": False}), encoding="utf-8")
        current = tmp_path / self.SANITIZED_NAME
        current.write_text(json.dumps({"help": True}), encoding="utf-8")

        path = channel_state_file(str(tmp_path), self.LEGACY_CHAT_KEY)

        assert path == current
        assert json.loads(path.read_text(encoding="utf-8")) == {"help": True}
        assert legacy.exists()

    def test_traversal_legacy_file_is_not_adopted(self, tmp_path: Path) -> None:
        """穿越到目录外的"旧文件"不算存量数据，不搬进来

        搬运它等于把穿越写入换成了穿越读取，攻击者只要事先在别处放一个文件就能让它进入数据目录。
        """
        state_dir = tmp_path / "channel_states"
        state_dir.mkdir()
        outsider = tmp_path / "outsider.json"
        outsider.write_text(json.dumps({"help": False}), encoding="utf-8")

        path = channel_state_file(str(state_dir), "../outsider")

        assert path == state_dir / ".._outsider.json"
        assert not path.exists()
        assert json.loads(outsider.read_text(encoding="utf-8")) == {"help": False}


class TestManagerUsesSanitizedPaths:
    """经由 CommandManager 的读写同样受约束（状态与权限两套目录都要覆盖）"""

    @pytest.fixture
    def state_dirs(self, tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
        state_dir = tmp_path / "channel_states"
        permission_dir = tmp_path / "channel_permissions"
        state_dir.mkdir()
        permission_dir.mkdir()
        monkeypatch.setattr(manager_module, "COMMAND_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(manager_module, "COMMAND_CHANNEL_STATE_DIR", str(state_dir))
        monkeypatch.setattr(manager_module, "COMMAND_CHANNEL_PERMISSION_DIR", str(permission_dir))
        return state_dir, permission_dir

    def test_state_write_cannot_escape(self, state_dirs: tuple[Path, Path]) -> None:
        state_dir, _ = state_dirs
        manager = CommandManager()

        manager._save_channel_state("../../escaped", {"help": False})  # noqa: SLF001

        assert [child.name for child in state_dir.iterdir()] == [".._.._escaped.json"]

    def test_permission_write_cannot_escape(self, state_dirs: tuple[Path, Path]) -> None:
        _, permission_dir = state_dirs
        manager = CommandManager()

        manager._save_channel_permission_state("../../escaped", {"sh": CommandPermission.PUBLIC})  # noqa: SLF001

        assert [child.name for child in permission_dir.iterdir()] == [".._.._escaped.json"]

    def test_legacy_state_survives_round_trip(self, state_dirs: tuple[Path, Path]) -> None:
        """存量文件读得出来，改一次之后仍然只剩清洗后的那一份"""
        state_dir, _ = state_dirs
        chat_key = TestLegacyFilesAreAdopted.LEGACY_CHAT_KEY
        (state_dir / TestLegacyFilesAreAdopted.LEGACY_NAME).write_text(json.dumps({"help": False}), encoding="utf-8")

        manager = CommandManager()
        assert manager._load_channel_state(chat_key) == {"help": False}  # noqa: SLF001

        manager._save_channel_state(chat_key, {"help": False, "status": True})  # noqa: SLF001
        assert [child.name for child in state_dir.iterdir()] == [TestLegacyFilesAreAdopted.SANITIZED_NAME]
