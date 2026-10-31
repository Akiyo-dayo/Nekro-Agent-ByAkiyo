"""频道行去重与合并修复的回归测试

保护两组契约：

**不再产生重复**
- 认领竞争的败者必须回查而不是新建
- 新建撞上唯一约束时必须回查而不是把异常抛给入站链路
- 数据库唯一约束本身必须挡住同三元组的第二行

**已损坏的数据能修好，且不误伤隔离性**
- 绑定字段搬到活跃行、旧行归档，重复执行结果不变
- 幸存行上已有的值不被旧行覆盖
- 不同实例、以及未认领的存量行，都不是「重复」，绝不能被合并掉
"""

import json
from typing import Any, List, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_exec_code import DBExecCode
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services.chat_key_migration import (
    ARCHIVED_DUPLICATES_KEY,
    merge_channel_duplicates,
    merge_duplicate_chat_channels,
)

ADAPTER = "onebot_v11"
GROUP = "group_123456"
INSTANCE_A = "ob10001"
INSTANCE_B = "ob10002"


async def seed_channel(
    *,
    instance_key: str = INSTANCE_A,
    channel_id: str = GROUP,
    chat_key: Optional[str] = None,
    is_active: bool = True,
    preset_id: Optional[int] = None,
    workspace_id: Optional[int] = None,
    channel_name: str = "",
    data: Optional[dict] = None,
) -> DBChatChannel:
    """写入一行频道记录，字段默认取「空壳行」的形态"""
    resolved = chat_key or (f"{ADAPTER}-{instance_key}-{channel_id}" if instance_key else f"{ADAPTER}-{channel_id}")
    return await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        channel_id=channel_id,
        channel_type=ChatType.GROUP.value,
        channel_name=channel_name,
        chat_key=resolved,
        is_active=is_active,
        observe_mode=False,
        preset_id=preset_id,
        workspace_id=workspace_id,
        data=json.dumps(data or {}),
    )


async def _open_db(*, with_unique_constraint: bool):
    """建一个内存库；关掉唯一约束即可复刻「约束尚未迁移上去」的存量库

    合并修复恰恰是为那种库准备的——唯一约束要等它跑完才敢加，测试必须造得出重复行。
    """
    original = DBChatChannel._meta.unique_together  # noqa: SLF001
    if not with_unique_constraint:
        DBChatChannel._meta.unique_together = ()  # noqa: SLF001
    try:
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
        await Tortoise.generate_schemas()
    finally:
        DBChatChannel._meta.unique_together = original  # noqa: SLF001


@pytest.fixture
async def db():
    await _open_db(with_unique_constraint=True)
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
async def damaged_db():
    """存量库形态：唯一约束还没加上，因此可能残留重复行"""
    await _open_db(with_unique_constraint=False)
    try:
        yield
    finally:
        await Tortoise.close_connections()


class TestUniqueConstraint:
    """唯一约束是止血的最后一道闸门"""

    async def test_schema_rejects_second_row_for_same_identity(self, db) -> None:  # noqa: ARG002
        from tortoise.exceptions import IntegrityError

        await seed_channel()
        with pytest.raises(IntegrityError):
            await seed_channel()

    async def test_two_instances_share_one_group(self, db) -> None:  # noqa: ARG002
        """实例段是隔离的基础：同一个群号在两个实例下必须能各存一行"""
        await seed_channel(instance_key=INSTANCE_A)
        await seed_channel(instance_key=INSTANCE_B)
        assert await DBChatChannel.filter(channel_id=GROUP).count() == 2


class TestGetOrCreateRaceRecovery:
    """竞争窗口里丢掉的那一次查询，必须由回查补上"""

    async def test_claim_loser_reuses_the_row_the_winner_claimed(self, db, monkeypatch) -> None:  # noqa: ARG002
        """认领竞争的败者：历史行已被赢家改写，此刻必须回查而不是新建

        用「首次查询看不到已存在的行」模拟败者视角：真实竞争里它读到的正是赢家提交前的快照。
        """
        existing = await seed_channel(preset_id=3, workspace_id=5)
        calls: List[int] = []
        original = DBChatChannel.get_by_identity.__func__  # type: ignore[attr-defined]

        async def flaky(cls, adapter_key: str, instance_key: str, channel_id: str) -> Any:
            calls.append(1)
            if len(calls) == 1:
                return None
            return await original(cls, adapter_key, instance_key, channel_id)

        monkeypatch.setattr(DBChatChannel, "get_by_identity", classmethod(flaky))

        channel = await DBChatChannel.get_or_create(
            adapter_key=ADAPTER,
            channel_id=GROUP,
            channel_type=ChatType.GROUP,
            chat_key=f"{ADAPTER}-{INSTANCE_A}-{GROUP}",
            instance_key=INSTANCE_A,
        )
        assert channel.id == existing.id
        assert channel.preset_id == 3
        assert await DBChatChannel.filter(channel_id=GROUP).count() == 1

    async def test_create_conflict_falls_back_to_the_existing_row(self, db, monkeypatch) -> None:  # noqa: ARG002
        """新建撞上唯一约束时必须回查：整条入站链路不能因为一次竞争就崩掉"""
        existing = await seed_channel(preset_id=7, workspace_id=9)
        calls: List[int] = []
        original = DBChatChannel.get_by_identity.__func__  # type: ignore[attr-defined]

        async def blind_twice(cls, adapter_key: str, instance_key: str, channel_id: str) -> Any:
            calls.append(1)
            if len(calls) <= 2:
                return None
            return await original(cls, adapter_key, instance_key, channel_id)

        monkeypatch.setattr(DBChatChannel, "get_by_identity", classmethod(blind_twice))

        channel = await DBChatChannel.get_or_create(
            adapter_key=ADAPTER,
            channel_id=GROUP,
            channel_type=ChatType.GROUP,
            chat_key=f"{ADAPTER}-{INSTANCE_A}-{GROUP}",
            instance_key=INSTANCE_A,
        )
        assert channel.id == existing.id
        assert await DBChatChannel.filter(channel_id=GROUP).count() == 1

    async def test_unclaimed_legacy_row_is_claimed_in_place(self, db) -> None:  # noqa: ARG002
        """存量未认领行被就地认领，绑定与引用都不能丢"""
        legacy = await seed_channel(instance_key="", preset_id=3, workspace_id=5, channel_name="老群")
        await DBExecCode.create(chat_key=legacy.chat_key, code_text="print(1)", outputs="1")

        channel = await DBChatChannel.get_or_create(
            adapter_key=ADAPTER,
            channel_id=GROUP,
            channel_type=ChatType.GROUP,
            chat_key=f"{ADAPTER}-{INSTANCE_A}-{GROUP}",
            instance_key=INSTANCE_A,
        )
        assert channel.id == legacy.id
        assert channel.chat_key == f"{ADAPTER}-{INSTANCE_A}-{GROUP}"
        assert channel.preset_id == 3
        assert await DBChatChannel.filter(channel_id=GROUP).count() == 1
        assert await DBExecCode.filter(chat_key=channel.chat_key).count() == 1


class TestGetByIdentity:
    """存量重复行还没被合并时，查询必须确定性降级而不是抛异常"""

    async def test_returns_oldest_row_when_duplicates_remain(self, damaged_db) -> None:  # noqa: ARG002
        oldest = await seed_channel(preset_id=3, workspace_id=5)
        await seed_channel()
        found = await DBChatChannel.get_by_identity(ADAPTER, INSTANCE_A, GROUP)
        assert found is not None
        assert found.id == oldest.id

    async def test_get_channel_prefers_oldest_row(self, damaged_db) -> None:  # noqa: ARG002
        oldest = await seed_channel(preset_id=3)
        await seed_channel()
        assert (await DBChatChannel.get_channel(oldest.chat_key)).id == oldest.id


class TestMergeDuplicates:
    """合并修复：把绑定搬到活跃行，旧行归档"""

    async def test_restores_bindings_onto_the_active_row(self, damaged_db) -> None:  # noqa: ARG002
        """现场形态：带绑定的原始行被停用，空壳行在接管流量"""
        donor = await seed_channel(
            is_active=False, preset_id=3, workspace_id=5, channel_name="老群", data={"custom_channel_name": "我的群"},
        )
        survivor = await seed_channel(is_active=True)

        results = await merge_duplicate_chat_channels()
        assert [r.survivor_id for r in results] == [survivor.id]
        assert results[0].archived_ids == [donor.id]

        rows = await DBChatChannel.filter(channel_id=GROUP)
        assert len(rows) == 1
        merged = rows[0]
        assert merged.id == survivor.id
        assert merged.is_active is True
        assert merged.preset_id == 3
        assert merged.workspace_id == 5
        assert merged.channel_name == "老群"
        data = json.loads(merged.data)
        assert data["custom_channel_name"] == "我的群"
        assert [entry["id"] for entry in data[ARCHIVED_DUPLICATES_KEY]] == [donor.id]

    async def test_survivor_values_win_over_donor_values(self, damaged_db) -> None:  # noqa: ARG002
        """幸存行上已有的值是更晚的用户意图，只补空缺、绝不覆盖"""
        await seed_channel(is_active=False, preset_id=3, workspace_id=5)
        survivor = await seed_channel(is_active=True, preset_id=11)

        await merge_duplicate_chat_channels()

        merged = await DBChatChannel.get(id=survivor.id)
        assert merged.preset_id == 11
        assert merged.workspace_id == 5

    async def test_keeps_oldest_row_when_every_row_is_active(self, damaged_db) -> None:  # noqa: ARG002
        oldest = await seed_channel(is_active=True, preset_id=3, workspace_id=5)
        await seed_channel(is_active=True)

        await merge_duplicate_chat_channels()

        rows = await DBChatChannel.filter(channel_id=GROUP)
        assert len(rows) == 1
        assert rows[0].id == oldest.id
        assert rows[0].preset_id == 3

    async def test_is_idempotent(self, damaged_db) -> None:  # noqa: ARG002
        await seed_channel(is_active=False, preset_id=3, workspace_id=5)
        survivor = await seed_channel(is_active=True)

        first = await merge_duplicate_chat_channels()
        second = await merge_duplicate_chat_channels()

        assert len(first) == 1
        assert second == []
        merged = await DBChatChannel.get(id=survivor.id)
        assert merged.preset_id == 3
        assert len(json.loads(merged.data)[ARCHIVED_DUPLICATES_KEY]) == 1

    async def test_moves_references_when_donor_chat_key_differs(self, damaged_db) -> None:  # noqa: ARG002
        """两行的标识不一致时，删行前必须把引用搬到幸存行，否则历史数据变孤儿"""
        donor = await seed_channel(
            is_active=False, chat_key=f"{ADAPTER}-{GROUP}", preset_id=3, workspace_id=5,
        )
        survivor = await seed_channel(is_active=True)
        await DBExecCode.create(chat_key=donor.chat_key, code_text="print(1)", outputs="1")

        await merge_duplicate_chat_channels()

        assert await DBChatChannel.filter(channel_id=GROUP).count() == 1
        assert await DBExecCode.filter(chat_key=donor.chat_key).count() == 0
        assert await DBExecCode.filter(chat_key=survivor.chat_key).count() == 1

    async def test_single_row_identity_is_left_alone(self, damaged_db) -> None:  # noqa: ARG002
        channel = await seed_channel(preset_id=3)
        assert await merge_channel_duplicates(ADAPTER, INSTANCE_A, GROUP) is None
        assert (await DBChatChannel.get(id=channel.id)).preset_id == 3


class TestMergeKeepsIsolation:
    """去重绝不能以牺牲实例隔离为代价"""

    async def test_two_instances_in_the_same_group_are_not_merged(self, damaged_db) -> None:  # noqa: ARG002
        a = await seed_channel(instance_key=INSTANCE_A, preset_id=3)
        b = await seed_channel(instance_key=INSTANCE_B, preset_id=8)

        assert await merge_duplicate_chat_channels() == []

        assert (await DBChatChannel.get(id=a.id)).preset_id == 3
        assert (await DBChatChannel.get(id=b.id)).preset_id == 8

    async def test_unclaimed_legacy_row_is_not_merged_into_an_instance_row(self, damaged_db) -> None:  # noqa: ARG002
        """未认领状态（instance_key 为空）是有意设计的待认领态，不是重复行

        把它并进某个实例的行，等于替用户决定这段历史归谁，另一个实例就再也认领不到了。
        """
        legacy = await seed_channel(instance_key="", preset_id=3, workspace_id=5)
        claimed = await seed_channel(instance_key=INSTANCE_A)

        assert await merge_duplicate_chat_channels() == []

        assert await DBChatChannel.filter(channel_id=GROUP).count() == 2
        assert (await DBChatChannel.get(id=legacy.id)).instance_key == ""
        assert (await DBChatChannel.get(id=claimed.id)).preset_id is None

    async def test_same_channel_id_under_different_adapters_is_not_merged(self, damaged_db) -> None:  # noqa: ARG002
        await seed_channel(instance_key="", preset_id=3)
        await DBChatChannel.create(
            adapter_key="telegram",
            instance_key="",
            channel_id=GROUP,
            channel_type=ChatType.GROUP.value,
            channel_name="",
            chat_key=f"telegram-{GROUP}",
            is_active=True,
            observe_mode=False,
            data=json.dumps({}),
        )

        assert await merge_duplicate_chat_channels() == []
        assert await DBChatChannel.filter(channel_id=GROUP).count() == 2


class TestMergeSchemaGuard:
    """合并跑在 aerich 迁移之前，表结构可能还停在升级前的版本"""

    async def test_skips_when_instance_key_column_is_missing(self, damaged_db) -> None:  # noqa: ARG002
        conn = Tortoise.get_connection("default")
        await conn.execute_script('DROP INDEX "idx_chat_channe_instanc_461907"')
        await conn.execute_script('ALTER TABLE "chat_channel" DROP COLUMN "instance_key"')

        assert await merge_duplicate_chat_channels() == []
