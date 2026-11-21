"""人设卡扩展、配置变更审计与两个新列能不能用

只钉库表层：两个后端上真的建出表、`preset_card.preset` 的唯一约束与级联删除、
JSON 列的默认值、`mem_prompt_round.tavern_items` 的默认空对象，以及模型组新增的
预填方式在 ORM 与 `ModelConfigGroup` 之间往返。业务判据在别的文件里。
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import pytest
from tortoise import Tortoise, connections
from tortoise.backends.base.executor import EXECUTOR_CACHE
from tortoise.exceptions import IntegrityError

from nekro_agent.core.config import ModelConfigGroup
from nekro_agent.models.db_config_audit import DBConfigChangeAudit
from nekro_agent.models.db_mem_prompt_round import DBMemPromptRound
from nekro_agent.models.db_model_group import DBModelGroup
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_preset_card import DBPresetCard

PG_DSN_ENV = "NA_TEST_PG_DSN"

# 整包登记：`preset_card` 的外键指向 `models.DBPreset`，只登记单模块时解析不到关系。
_MODEL_MODULES = ["nekro_agent.models"]


def _pg_dsn_or_skip() -> str:
    dsn = os.environ.get(PG_DSN_ENV, "")
    if not dsn:
        pytest.skip(f"需要真实 PostgreSQL：设 {PG_DSN_ENV} 后运行；SQLite 腿始终运行。")
    return dsn


@pytest.fixture(params=["sqlite", "pg"], ids=["sqlite", "pg"])
async def db(request: pytest.FixtureRequest) -> Any:
    """每个后端一套真表；PG 在随机 schema 里建、结束 DROP SCHEMA CASCADE"""
    if request.param == "sqlite":
        EXECUTOR_CACHE.clear()
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": _MODEL_MODULES})
        await Tortoise.generate_schemas()
        try:
            yield "sqlite"
        finally:
            await Tortoise.close_connections()
            EXECUTOR_CACHE.clear()
        return

    dsn = _pg_dsn_or_skip()
    import asyncpg

    schema = f"na_test_tavern_models_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(dsn=dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    sep = "&" if "?" in dsn else "?"
    EXECUTOR_CACHE.clear()
    await Tortoise.init(
        db_url=f"{dsn}{sep}{urlencode({'schema': schema})}",
        modules={"models": _MODEL_MODULES},
    )
    await Tortoise.generate_schemas()
    try:
        rows = await connections.get("default").execute_query_dict(
            "SELECT table_schema FROM information_schema.tables WHERE table_name = 'preset_card'",
        )
        assert schema in {str(row["table_schema"]) for row in rows}, "没有在真 PG 的目标 schema 里建出表"
        yield schema
    finally:
        await Tortoise.close_connections()
        EXECUTOR_CACHE.clear()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def _make_preset(name: str = "人设") -> DBPreset:
    """建一个最小可用的人设行；本表只关心扩展行怎么挂上去"""
    return await DBPreset.create(
        name=name,
        title=name,
        avatar="",
        content="",
        description="",
        tags="",
        author="",
    )


class TestTablesExist:
    async def test_preset_card_row_and_defaults(self, db: Any) -> None:
        """一个人设一行；所有默认值都符合字段声明"""
        preset = await _make_preset()
        card = await DBPresetCard.create(preset=preset)

        assert card.id > 0
        assert card.preset_id == preset.id
        assert card.card_spec == ""
        assert card.card_raw is None
        assert card.nickname == ""
        assert card.source_fields == {}
        assert card.assembled_sha256 == ""
        assert card.macros_enabled is True
        assert card.greetings == []
        assert card.group_greetings == []
        assert card.greeting_pick == "first"
        assert card.post_history == ""
        assert card.prefill == ""
        assert card.depth_prompt == ""
        assert card.depth_prompt_depth == 4
        assert card.depth_prompt_role == "system"
        assert card.cleanup_rules == []
        assert card.character_version == ""
        assert card.imported_by == ""
        assert card.imported_at is None

    async def test_config_change_audit_defaults(self, db: Any) -> None:
        """审计行按层记录；文字摘要列缺省为空串"""
        row = await DBConfigChangeAudit.create(layer="system", field="PROMPT_POST_HISTORY")

        assert row.id > 0
        assert row.operator == ""
        assert row.operator_is_platform is False
        assert row.adapter_key == ""
        assert row.instance_key == ""
        assert row.chat_key == ""
        assert row.preset_id == 0
        assert row.old_value == ""
        assert row.new_value == ""
        assert row.created_at is not None


class TestConstraints:
    async def test_one_card_row_per_preset(self, db: Any) -> None:
        """一个人设最多一行：第二次写入同一外键被数据库拒绝"""
        preset = await _make_preset()
        await DBPresetCard.create(preset=preset)
        with pytest.raises(IntegrityError):
            await DBPresetCard.create(preset=preset)

    async def test_deleting_preset_deletes_card(self, db: Any) -> None:
        """删人设连带删扩展行，不留看不见的副本"""
        preset = await _make_preset()
        await DBPresetCard.create(preset=preset)
        await preset.delete()
        assert await DBPresetCard.all().count() == 0


class TestPromptRoundColumn:
    async def test_tavern_items_defaults_to_empty_object(self, db: Any) -> None:
        """没跑过酒馆兼容轮次时是空对象，不是 NULL"""
        now = datetime(2026, 2, 1, 12, 0, 0)
        row = await DBMemPromptRound.create(
            round_id=uuid.uuid4().hex[:16],
            chat_key="g1",
            started_at=now,
            finished_at=now + timedelta(seconds=1),
        )
        assert row.tavern_items == {}

    async def test_tavern_items_round_trip(self, db: Any) -> None:
        """本轮状态与字数按 JSON 对象读回来"""
        now = datetime(2026, 2, 1, 12, 0, 0)
        row = await DBMemPromptRound.create(
            round_id=uuid.uuid4().hex[:16],
            chat_key="g1",
            tavern_items={"card": {"depth": 2, "chars": 9}},
            started_at=now,
            finished_at=now + timedelta(seconds=1),
        )
        loaded = await DBMemPromptRound.get(id=row.id)
        assert loaded.tavern_items == {"card": {"depth": 2, "chars": 9}}


class TestModelGroupPrefillMode:
    async def test_default_is_off(self, db: Any) -> None:
        row = await DBModelGroup.create(name="prefill_default")
        assert row.prefill_mode == "off"
        assert row.to_config_group().PREFILL_MODE == "off"

    async def test_round_trip_through_config_group(self, db: Any) -> None:
        """ORM 行与运行时视图双向映射，三个合法值都保留"""
        row = await DBModelGroup.create(name="prefill_roundtrip", prefill_mode="deepseek")
        assert row.to_config_group().PREFILL_MODE == "deepseek"

        group = ModelConfigGroup(GROUP_NAME="prefill_roundtrip", CHAT_MODEL="m", PREFILL_MODE="assistant")
        row.apply_config_group(group)
        await row.save()
        loaded = await DBModelGroup.get(id=row.id)
        assert loaded.prefill_mode == "assistant"
        assert loaded.to_config_group().PREFILL_MODE == "assistant"

    async def test_unknown_stored_value_reads_as_off(self, db: Any) -> None:
        """手工改库或旧数据里的意外值按 off 处理，不把未知值传给运行时"""
        row = await DBModelGroup.create(name="prefill_unknown", prefill_mode="something")
        assert row.to_config_group().PREFILL_MODE == "off"


class TestModelRegistration:
    def test_models_are_exported_from_the_models_package(self) -> None:
        import nekro_agent.models as models

        assert models.DBPresetCard is DBPresetCard
        assert models.DBConfigChangeAudit is DBConfigChangeAudit
