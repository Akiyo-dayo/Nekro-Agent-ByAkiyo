"""角色状态的核心部分：四张表、提示词明细的状态一栏、两项可覆盖配置

插件本体不在这里；这里只钉核心给它准备的东西：

1. 四张表能建、键唯一（频道 × 人设、域 × 人 × 人设、一个人设一份模板）；
2. 明细的 `state_items` 浅合并、只有状态一栏时也落库、没 `begin` 过的令牌是空操作；
3. `STATE_PROMPT_MAX_TOKENS` 可覆盖、租户只能往下调（0 是「不注入」，不是「不设上限」，
   所以租户调到 0 算往下调）；`STATE_PRESENT_WINDOW_MINUTES` 可覆盖、不限方向。
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from tortoise import Tortoise
from tortoise.exceptions import IntegrityError

from nekro_agent.core.config import CoreConfig, config
from nekro_agent.models.db_mem_prompt_round import DBMemPromptRound
from nekro_agent.models.db_state_channel import DBStateChannel
from nekro_agent.models.db_state_person import DBStatePerson
from nekro_agent.models.db_state_snapshot import DBStateSnapshot
from nekro_agent.models.db_state_template import DBStateTemplate
from nekro_agent.routers import config as config_router
from nekro_agent.services.memory import prompt_rounds

CHAT_KEY = "onebot_v11-s1a-group_3"


@pytest.fixture
async def db() -> AsyncIterator[None]:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def _clean_rounds() -> AsyncIterator[None]:
    prompt_rounds.reset_prompt_rounds()
    yield
    prompt_rounds.reset_prompt_rounds()


class TestTables:
    async def test_channel_state_is_unique_per_channel_and_preset(self, db: None) -> None:
        await DBStateChannel.create(chat_key=CHAT_KEY, preset_id=1, data={"时间": "早上"})
        await DBStateChannel.create(chat_key=CHAT_KEY, preset_id=2, data={})

        with pytest.raises(IntegrityError):
            await DBStateChannel.create(chat_key=CHAT_KEY, preset_id=1, data={})

    async def test_person_profile_uses_the_affection_key(self, db: None) -> None:
        values = {"adapter_key": "onebot_v11", "instance_key": "s1a", "domain_group_id": 0, "platform_userid": "u1"}
        await DBStatePerson.create(**values, preset_id=1, data={})
        await DBStatePerson.create(**values, preset_id=2, data={})
        await DBStatePerson.create(**{**values, "instance_key": "s1b"}, preset_id=1, data={})

        with pytest.raises(IntegrityError):
            await DBStatePerson.create(**values, preset_id=1, data={})

    async def test_one_template_per_preset(self, db: None) -> None:
        await DBStateTemplate.create(preset_id=5, field_defs={}, initial={})

        with pytest.raises(IntegrityError):
            await DBStateTemplate.create(preset_id=5, field_defs={}, initial={})

    async def test_snapshot_without_data_is_an_update_record(self, db: None) -> None:
        row = await DBStateSnapshot.create(
            target_kind="channel",
            target_id=1,
            source="update",
            result="parse_failed",
            ops_rejected=[{"path": "/时间", "op": "replace", "reason": "readonly"}],
        )

        fetched = await DBStateSnapshot.get(id=row.id)
        assert fetched.data is None
        assert fetched.ops_applied == []
        assert fetched.ops_rejected[0]["reason"] == "readonly"

    async def test_defaults(self, db: None) -> None:
        row = await DBStateChannel.create(chat_key=CHAT_KEY, preset_id=1, data={})

        assert row.version == 1
        assert row.paused is False
        assert row.consecutive_failures == 0
        assert row.msgs_since_update == 0


class TestPromptRoundStateItems:
    def test_note_without_begin_is_a_no_op(self) -> None:
        prompt_rounds.note_state("never-begun", state="injected")

        assert "never-begun" not in prompt_rounds._rounds  # noqa: SLF001

    def test_fields_are_merged(self) -> None:
        prompt_rounds.begin("code-s1", SimpleNamespace(chat_key=CHAT_KEY, adapter_key="onebot_v11", instance_key="s1a"))  # pyright: ignore[reportArgumentType]

        prompt_rounds.note_state("code-s1", state="injected", tokens=320)
        prompt_rounds.note_state("code-s1", trimmed=["伏笔"])

        assert prompt_rounds._rounds["code-s1"].state_items == {"state": "injected", "tokens": 320, "trimmed": ["伏笔"]}  # noqa: SLF001

    async def test_a_round_with_only_the_state_column_is_written(
        self, db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_PROMPT_ROUNDS_KEEP", 20)
        channel = SimpleNamespace(chat_key=CHAT_KEY, adapter_key="onebot_v11", instance_key="s1a")
        prompt_rounds.begin("code-s2", channel)  # pyright: ignore[reportArgumentType]
        prompt_rounds._rounds["code-s2"].preset_id = 1  # noqa: SLF001
        prompt_rounds.note_state("code-s2", state="injected", tokens=128)

        prompt_rounds.finish("code-s2", "ok")
        await prompt_rounds.drain()

        row = await DBMemPromptRound.get(chat_key=CHAT_KEY)
        assert row.state_items == {"state": "injected", "tokens": 128}
        detail = await prompt_rounds.round_detail(row)
        assert detail["state_items"] == {"state": "injected", "tokens": 128}


class TestConfigItems:
    def _extra(self, name: str) -> dict:
        extra = CoreConfig.model_fields[name].json_schema_extra
        assert isinstance(extra, dict)
        return extra

    def test_block_budget_is_overridable_and_only_lower(self) -> None:
        extra = self._extra("STATE_PROMPT_MAX_TOKENS")

        assert extra.get("overridable") is True
        assert extra.get("tenant_may_only_lower") is True
        assert not extra.get("zero_means_unlimited")
        assert "STATE_PROMPT_MAX_TOKENS" in config_router._tenant_only_lower_field_names()  # noqa: SLF001
        assert CoreConfig.model_fields["STATE_PROMPT_MAX_TOKENS"].default == 1200

    def test_zero_ranks_as_the_lowest_value(self) -> None:
        # 0 是「不注入」：租户从 1200 调到 0 是往下调，不能被当成「不设上限」挡掉或放大。
        assert config_router._int_rank("STATE_PROMPT_MAX_TOKENS", 0) == 0  # noqa: SLF001
        assert config_router._int_rank("WORLDBOOK_BUDGET_CAP_TOKENS", 0) == math.inf  # noqa: SLF001

    @pytest.mark.parametrize(
        ("before", "after", "inherited", "denied"),
        [
            (1200, 2000, 1200, True),
            (1200, 800, 1200, False),
            (1200, 0, 1200, False),
            (800, 1200, 1200, False),
            (800, 1500, 1200, True),
        ],
    )
    def test_raise_judgement(self, before: int, after: int, inherited: int, denied: bool) -> None:
        rank = config_router._int_rank  # noqa: SLF001
        name = "STATE_PROMPT_MAX_TOKENS"

        assert config_router._int_raise_denied(rank(name, before), rank(name, after), rank(name, inherited)) is denied  # noqa: SLF001

    def test_present_window_is_overridable_without_direction(self) -> None:
        extra = self._extra("STATE_PRESENT_WINDOW_MINUTES")

        assert extra.get("overridable") is True
        assert not extra.get("tenant_may_only_lower")
        field = CoreConfig.model_fields["STATE_PRESENT_WINDOW_MINUTES"]
        assert field.default == 30

    @pytest.mark.parametrize(
        ("name", "low", "high"), [("STATE_PROMPT_MAX_TOKENS", 0, 8192), ("STATE_PRESENT_WINDOW_MINUTES", 1, 1440)]
    )
    def test_ranges(self, name: str, low: int, high: int) -> None:
        CoreConfig.model_validate({name: low})
        CoreConfig.model_validate({name: high})
        with pytest.raises(ValueError):
            CoreConfig.model_validate({name: high + 1})
        with pytest.raises(ValueError):
            CoreConfig.model_validate({name: low - 1})
