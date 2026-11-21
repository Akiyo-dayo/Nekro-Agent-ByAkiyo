"""启动时把旧的「每轮正文字数上限」迁移到按 token 计的预算

要钉住的事：

1. 没在用世界书的部署：什么都不写，用新默认值（25%、不另设上限），只置「已迁移」；
2. 在用的部署：全局写百分比 100；每层设过的字数上限换成同一层的 token 上限（× 0.8），覆盖开关照抄；
   全局没设过的按默认 3000 → 2400；
3. 原值 0（「不注入」）的层改成同一层的上限 1 token（仍是租户抬不上去的天花板），世界书开关不动；
   全局 0 时全局上限也是 1；
4. 覆盖层里旧的两个键删掉，其余键原样；全局旧值改回默认，之后再出现别的值打 WARNING；
5. 幂等：已迁移就不再动；上一次写到一半（覆盖层写了、全局没落盘）重跑结果相同；
6. 每层单独兜底：一层写不回去只跳过这一层并写进迁移提示，后面的层照常换算；手填的 `.inf` 按默认值；
7. 扫磁盘时认得三种覆盖层文件，坏文件跳过不挡启动。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, List

import pytest
import yaml
from loguru import logger

from nekro_agent.services.worldbook import budget_migration
from nekro_agent.services.worldbook.budget_migration import (
    OverrideLayerFile,
    chars_to_tokens,
    discover_override_layers,
    migrate_worldbook_budget,
)


def global_config(**values: Any) -> SimpleNamespace:
    """全局配置的替身：迁移只读写这几项"""
    base = {
        "WORLDBOOK_MAX_CHARS": 3000,
        "WORLDBOOK_ENABLED": False,
        "WORLDBOOK_BUDGET_PERCENT": 25,
        "WORLDBOOK_BUDGET_CAP_TOKENS": 0,
        "WORLDBOOK_BUDGET_MIGRATED": False,
        "WORLDBOOK_BUDGET_MIGRATION_NOTICE": [],
    }
    base.update(values)
    return SimpleNamespace(**base)


def layer(label: str, data: dict[str, Any]) -> OverrideLayerFile:
    return OverrideLayerFile(label=label, config_key=label, path=Path(f"/nonexistent/{label}.yaml"), data=dict(data))


class Recorder:
    def __init__(self) -> None:
        self.written: List[str] = []

    def __call__(self, item: OverrideLayerFile) -> None:
        self.written.append(item.label)


@pytest.fixture
def warnings_logged() -> Iterator[List[str]]:
    messages: List[str] = []
    sink_id = logger.add(lambda message: messages.append(message.record["message"]), level="WARNING")
    yield messages
    logger.remove(sink_id)


class TestNotInUse:
    def test_nothing_is_written_and_new_defaults_apply(self) -> None:
        cfg = global_config()
        recorder = Recorder()
        notes = migrate_worldbook_budget(cfg, layers=[layer("频道 a", {"enable_AI_X": True})], write_layer=recorder)

        assert notes == []
        assert (cfg.WORLDBOOK_BUDGET_PERCENT, cfg.WORLDBOOK_BUDGET_CAP_TOKENS) == (25, 0)
        assert cfg.WORLDBOOK_BUDGET_MIGRATED is True
        assert cfg.WORLDBOOK_BUDGET_MIGRATION_NOTICE == []
        assert recorder.written == []

    def test_a_stale_inactive_legacy_key_is_cleaned_without_counting_as_use(self) -> None:
        cfg = global_config()
        stale = layer("频道 b", {"enable_WORLDBOOK_MAX_CHARS": False, "WORLDBOOK_MAX_CHARS": 9000, "enable_X": True})
        recorder = Recorder()

        migrate_worldbook_budget(cfg, layers=[stale], write_layer=recorder)

        assert cfg.WORLDBOOK_BUDGET_PERCENT == 25
        assert recorder.written == ["频道 b"]
        assert stale.data == {"enable_X": True}


class TestInUse:
    def test_enabled_globally_with_the_default_chars(self) -> None:
        cfg = global_config(WORLDBOOK_ENABLED=True)

        notes = migrate_worldbook_budget(cfg, layers=[], write_layer=Recorder())

        assert (cfg.WORLDBOOK_BUDGET_PERCENT, cfg.WORLDBOOK_BUDGET_CAP_TOKENS) == (100, 2400)
        assert any("3000 字（默认值） → 上限 2400 token" in note for note in notes)
        assert cfg.WORLDBOOK_BUDGET_MIGRATION_NOTICE == notes
        assert cfg.WORLDBOOK_BUDGET_MIGRATED is True

    @pytest.mark.parametrize(("chars", "tokens"), [(3000, 2400), (6000, 4800), (20000, 16000), (1, 1), (7, 6)])
    def test_conversion(self, chars: int, tokens: int) -> None:
        assert chars_to_tokens(chars) == tokens

    def test_each_layer_keeps_its_own_cap_and_toggle(self) -> None:
        cfg = global_config(WORLDBOOK_MAX_CHARS=6000)
        instance = layer(
            "实例 onebot_v11/a",
            {"enable_WORLDBOOK_MAX_CHARS": True, "WORLDBOOK_MAX_CHARS": 2000, "enable_Y": True, "Y": 1},
        )
        channel = layer("频道 c", {"enable_WORLDBOOK_ENABLED": True, "WORLDBOOK_ENABLED": True})
        recorder = Recorder()

        notes = migrate_worldbook_budget(cfg, layers=[instance, channel], write_layer=recorder)

        assert (cfg.WORLDBOOK_BUDGET_PERCENT, cfg.WORLDBOOK_BUDGET_CAP_TOKENS) == (100, 4800)
        assert instance.data == {
            "enable_Y": True,
            "Y": 1,
            "enable_WORLDBOOK_BUDGET_CAP_TOKENS": True,
            "WORLDBOOK_BUDGET_CAP_TOKENS": 1600,
        }
        assert channel.data == {"enable_WORLDBOOK_ENABLED": True, "WORLDBOOK_ENABLED": True}, "没设过字数的层不动"
        assert recorder.written == ["实例 onebot_v11/a"]
        assert "实例 onebot_v11/a：原 2000 字 → 上限 1600 token" in notes
        assert any("全局：原 6000 字 → 上限 4800 token" in note for note in notes)
        assert cfg.WORLDBOOK_MAX_CHARS == 3000, "全局旧值改回默认，之后再出现别的值就是迁移后有人手改的"

    def test_a_zero_layer_gets_a_one_token_cap_on_that_layer(self) -> None:
        """旧的 0 是租户只能调低的天花板：换成同一层的上限 1，世界书开关不动（关掉的话下层自己打开就能拿到上一层的预算）"""
        cfg = global_config(WORLDBOOK_ENABLED=True)
        quiet = layer(
            "实例 onebot_v11/q",
            {
                "enable_WORLDBOOK_MAX_CHARS": True,
                "WORLDBOOK_MAX_CHARS": 0,
                "enable_WORLDBOOK_ENABLED": True,
                "WORLDBOOK_ENABLED": True,
            },
        )

        notes = migrate_worldbook_budget(cfg, layers=[quiet], write_layer=Recorder())

        assert quiet.data == {
            "enable_WORLDBOOK_ENABLED": True,
            "WORLDBOOK_ENABLED": True,
            "enable_WORLDBOOK_BUDGET_CAP_TOKENS": True,
            "WORLDBOOK_BUDGET_CAP_TOKENS": 1,
        }
        assert any("实例 onebot_v11/q：原 0 字" in note and "上限 1 token" in note for note in notes)
        assert cfg.WORLDBOOK_BUDGET_MIGRATION_NOTICE == notes

    def test_a_zero_global_gets_a_one_token_cap(self) -> None:
        cfg = global_config(WORLDBOOK_MAX_CHARS=0, WORLDBOOK_ENABLED=True)

        notes = migrate_worldbook_budget(cfg, layers=[], write_layer=Recorder())

        assert cfg.WORLDBOOK_ENABLED is True, "开关不动"
        assert cfg.WORLDBOOK_BUDGET_CAP_TOKENS == 1, "不是 2400：原来一个字都不注入"
        assert cfg.WORLDBOOK_BUDGET_PERCENT == 100
        assert any(note.startswith("全局：原 0 字") and "上限 1 token" in note for note in notes)

    def test_a_negative_layer_counts_as_zero(self) -> None:
        cfg = global_config(WORLDBOOK_ENABLED=True)
        odd = layer("频道 n", {"enable_WORLDBOOK_MAX_CHARS": True, "WORLDBOOK_MAX_CHARS": -5})

        migrate_worldbook_budget(cfg, layers=[odd], write_layer=Recorder())

        assert odd.data == {"enable_WORLDBOOK_BUDGET_CAP_TOKENS": True, "WORLDBOOK_BUDGET_CAP_TOKENS": 1}

    def test_the_notice_explains_what_cap_zero_means_now(self) -> None:
        cfg = global_config(WORLDBOOK_ENABLED=True)

        notes = migrate_worldbook_budget(cfg, layers=[], write_layer=Recorder())

        assert any("上限改成 0" in note and "65536" in note for note in notes), "上限 0 也不会超过硬上限"


class TestIdempotence:
    def test_already_migrated_does_nothing(self) -> None:
        cfg = global_config(WORLDBOOK_BUDGET_MIGRATED=True, WORLDBOOK_ENABLED=True, WORLDBOOK_BUDGET_PERCENT=25)
        recorder = Recorder()
        old = layer("频道 e", {"enable_WORLDBOOK_MAX_CHARS": True, "WORLDBOOK_MAX_CHARS": 5000})

        assert migrate_worldbook_budget(cfg, layers=[old], write_layer=recorder) == []
        assert cfg.WORLDBOOK_BUDGET_PERCENT == 25 and recorder.written == []

    def test_a_stray_legacy_value_after_migration_is_warned(self, warnings_logged: List[str]) -> None:
        cfg = global_config(WORLDBOOK_BUDGET_MIGRATED=True, WORLDBOOK_MAX_CHARS=8000)

        migrate_worldbook_budget(cfg, layers=[], write_layer=Recorder())

        assert any("WORLDBOOK_MAX_CHARS" in message and "已废弃" in message for message in warnings_logged)

    def test_a_half_done_run_is_redone_the_same_way(self) -> None:
        """覆盖层已经换成 token 上限、全局还没落盘：重跑仍判「在用」，全局得到同样的值"""
        cfg = global_config()
        converted = layer("频道 f", {"enable_WORLDBOOK_BUDGET_CAP_TOKENS": True, "WORLDBOOK_BUDGET_CAP_TOKENS": 1600})
        recorder = Recorder()

        migrate_worldbook_budget(cfg, layers=[converted], write_layer=recorder)

        assert (cfg.WORLDBOOK_BUDGET_PERCENT, cfg.WORLDBOOK_BUDGET_CAP_TOKENS) == (100, 2400)
        assert recorder.written == [], "已经换过的层不再写"

    def test_startup_wrapper_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("坏了")

        monkeypatch.setattr(budget_migration, "migrate_worldbook_budget", boom)
        budget_migration.migrate_worldbook_budget_at_startup()


class TestOneBadLayer:
    """一层出错只跳过它自己：后面的层照常换算，出错的层点名写进迁移提示"""

    def test_a_failed_write_skips_only_that_layer(self) -> None:
        cfg = global_config(WORLDBOOK_ENABLED=True)
        before = layer("频道 g1", {"enable_WORLDBOOK_MAX_CHARS": True, "WORLDBOOK_MAX_CHARS": 1000})
        broken = layer("频道 g2", {"enable_WORLDBOOK_MAX_CHARS": True, "WORLDBOOK_MAX_CHARS": 2000})
        after = layer("频道 g3", {"enable_WORLDBOOK_MAX_CHARS": True, "WORLDBOOK_MAX_CHARS": 500})
        written: List[str] = []

        def write(item: OverrideLayerFile) -> None:
            if item.label == "频道 g2":
                raise OSError("磁盘满")
            written.append(item.label)

        notes = migrate_worldbook_budget(cfg, layers=[before, broken, after], write_layer=write)

        assert written == ["频道 g1", "频道 g3"], "出错的层后面那层照常写"
        assert after.data == {"enable_WORLDBOOK_BUDGET_CAP_TOKENS": True, "WORLDBOOK_BUDGET_CAP_TOKENS": 400}
        failure = [note for note in notes if note.startswith("频道 g2")]
        assert len(failure) == 1 and "没能迁移" in failure[0] and "OSError" in failure[0] and "手工" in failure[0]
        assert "频道 g3：原 500 字 → 上限 400 token" in notes
        assert cfg.WORLDBOOK_BUDGET_MIGRATION_NOTICE == notes, "失败的层进迁移提示，平台方在页面上能看到"
        assert cfg.WORLDBOOK_BUDGET_MIGRATED is True, "一次性：别的层已经换好了，重跑会把全局的值改乱"

    def test_a_conversion_error_skips_only_that_layer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = global_config(WORLDBOOK_ENABLED=True)
        broken = layer("频道 h1", {"enable_WORLDBOOK_MAX_CHARS": True, "WORLDBOOK_MAX_CHARS": 1234})
        fine = layer("频道 h2", {"enable_WORLDBOOK_MAX_CHARS": True, "WORLDBOOK_MAX_CHARS": 1000})
        real = budget_migration.chars_to_tokens

        def picky(chars: int) -> int:
            if chars == 1234:
                raise ArithmeticError("换算出错")
            return real(chars)

        monkeypatch.setattr(budget_migration, "chars_to_tokens", picky)
        recorder = Recorder()

        notes = migrate_worldbook_budget(cfg, layers=[broken, fine], write_layer=recorder)

        assert recorder.written == ["频道 h2"]
        assert any(note.startswith("频道 h1：没能迁移（ArithmeticError）") for note in notes)
        assert fine.data == {"enable_WORLDBOOK_BUDGET_CAP_TOKENS": True, "WORLDBOOK_BUDGET_CAP_TOKENS": 800}

    @pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), "很多", None])
    def test_an_unreadable_layer_value_falls_back_to_the_default(self, value: Any) -> None:
        cfg = global_config(WORLDBOOK_ENABLED=True)
        odd = layer("频道 i", {"enable_WORLDBOOK_MAX_CHARS": True, "WORLDBOOK_MAX_CHARS": value})

        notes = migrate_worldbook_budget(cfg, layers=[odd], write_layer=Recorder())

        assert odd.data == {"enable_WORLDBOOK_BUDGET_CAP_TOKENS": True, "WORLDBOOK_BUDGET_CAP_TOKENS": 2400}
        assert "频道 i：原 3000 字 → 上限 2400 token" in notes

    def test_an_infinite_global_value_falls_back_to_the_default(self) -> None:
        cfg = global_config(WORLDBOOK_MAX_CHARS=float("inf"))

        migrate_worldbook_budget(cfg, layers=[], write_layer=Recorder())

        assert cfg.WORLDBOOK_BUDGET_MIGRATED is True
        assert cfg.WORLDBOOK_MAX_CHARS == 3000

    def test_a_dot_inf_written_in_yaml_does_not_stop_the_migration(self, tmp_path: Path) -> None:
        configs = tmp_path / "configs"
        channels = configs / "channels"
        (channels / "onebot_v11").mkdir(parents=True)
        odd = channels / "onebot_v11" / "onebot_v11-group_8.yaml"
        odd.write_text("enable_WORLDBOOK_MAX_CHARS: true\nWORLDBOOK_MAX_CHARS: .inf\n", encoding="utf-8")
        fine = channels / "onebot_v11" / "onebot_v11-group_9.yaml"
        fine.write_text("enable_WORLDBOOK_MAX_CHARS: true\nWORLDBOOK_MAX_CHARS: 1000\n", encoding="utf-8")
        cfg = global_config()

        migrate_worldbook_budget(cfg, layers=discover_override_layers(configs, channels))

        assert yaml.safe_load(odd.read_text(encoding="utf-8")) == {
            "enable_WORLDBOOK_BUDGET_CAP_TOKENS": True,
            "WORLDBOOK_BUDGET_CAP_TOKENS": 2400,
        }
        assert yaml.safe_load(fine.read_text(encoding="utf-8"))["WORLDBOOK_BUDGET_CAP_TOKENS"] == 800
        assert cfg.WORLDBOOK_BUDGET_MIGRATED is True

    def test_a_failed_cleanup_when_not_in_use_stays_out_of_the_notice(self) -> None:
        cfg = global_config()
        stale = layer("频道 j", {"enable_WORLDBOOK_MAX_CHARS": False, "WORLDBOOK_MAX_CHARS": 9000})

        def fail(_item: OverrideLayerFile) -> None:
            raise OSError("只读")

        notes = migrate_worldbook_budget(cfg, layers=[stale], write_layer=fail)

        assert notes == [] and cfg.WORLDBOOK_BUDGET_MIGRATION_NOTICE == []
        assert cfg.WORLDBOOK_BUDGET_MIGRATED is True


class TestDiscovery:
    def test_three_kinds_of_layer_files_are_found(self, tmp_path: Path) -> None:
        configs = tmp_path / "configs"
        channels = configs / "channels"
        (configs / "onebot_v11" / "instances" / "inst1").mkdir(parents=True)
        (channels / "onebot_v11").mkdir(parents=True)
        (configs / "onebot_v11" / "overrides.yaml").write_text("enable_WORLDBOOK_ENABLED: true\n", encoding="utf-8")
        (configs / "onebot_v11" / "instances" / "inst1" / "overrides.yaml").write_text("{}\n", encoding="utf-8")
        (channels / "onebot_v11" / "onebot_v11-group_1.yaml").write_text("", encoding="utf-8")
        (channels / "onebot_v11" / "broken.yaml").write_text("a: [unclosed\n", encoding="utf-8")
        (channels / "onebot_v11" / "list.yaml").write_text("- 1\n- 2\n", encoding="utf-8")

        found = discover_override_layers(configs, channels)

        assert [(item.config_key, item.data) for item in found] == [
            ("adapter_override_onebot_v11", {"enable_WORLDBOOK_ENABLED": True}),
            ("instance_override_onebot_v11__inst1", {}),
            ("channel_config_onebot_v11-group_1", {}),
        ]

    def test_written_files_keep_their_other_keys(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        configs = tmp_path / "configs"
        channels = configs / "channels"
        (channels / "onebot_v11").mkdir(parents=True)
        path = channels / "onebot_v11" / "onebot_v11-group_2.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "enable_AI_CHAT_CONTEXT_MAX_LENGTH": True,
                    "AI_CHAT_CONTEXT_MAX_LENGTH": 40,
                    "enable_WORLDBOOK_MAX_CHARS": True,
                    "WORLDBOOK_MAX_CHARS": 5000,
                },
            ),
            encoding="utf-8",
        )
        cfg = global_config()

        migrate_worldbook_budget(cfg, layers=discover_override_layers(configs, channels))

        on_disk = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert on_disk == {
            "enable_AI_CHAT_CONTEXT_MAX_LENGTH": True,
            "AI_CHAT_CONTEXT_MAX_LENGTH": 40,
            "enable_WORLDBOOK_BUDGET_CAP_TOKENS": True,
            "WORLDBOOK_BUDGET_CAP_TOKENS": 4000,
        }
        assert cfg.WORLDBOOK_BUDGET_MIGRATED is True
