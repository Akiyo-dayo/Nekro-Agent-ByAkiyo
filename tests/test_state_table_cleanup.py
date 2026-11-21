"""角色状态的四张表跟着实例与频道标识走

实例标识与频道标识都可能被重新认领（甚至落到别的租户手里）：

1. 彻底删除实例：这个实例的频道状态、自成一域的个人档案、这个实例对上的快照一起删；别的实例的、
   属于实例组的（挂在组上）不动；
2. 租户划转时勾「一并清空」：与摘要、好感同一口径删这三样；
3. 换频道标识：频道状态与快照跟到新标识上并回填实例；新标识下已经有同一人设的那一行时它幸存，
   旧标识下的那一行连同它的快照删掉，不撞唯一约束；个人档案按人存，不随频道变。

模板跟人设走，不在这几条链里。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_state_channel import DBStateChannel
from nekro_agent.models.db_state_person import DBStatePerson
from nekro_agent.models.db_state_snapshot import DBStateSnapshot
from nekro_agent.services import chat_key_migration
from nekro_agent.services.console import instance_manager
from nekro_agent.services.identity_scope import ScopeDomain
from nekro_agent.services.memory import admin_service

ADAPTER = "onebot_v11"
INSTANCE = "stc1"
OTHER = "stc2"


@pytest.fixture
async def db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setattr(config, "MEMORY_INSTANCE_SHARING_GROUPS", {})
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _channel(instance_key: str, chat_key: str, preset_id: int = 1, **data: Any) -> DBStateChannel:
    return await DBStateChannel.create(
        chat_key=chat_key,
        preset_id=preset_id,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        data=data or {"当前时间": "早上"},
    )


async def _person(domain: ScopeDomain) -> DBStatePerson:
    return await DBStatePerson.create(**domain.as_columns(), platform_userid="u1", preset_id=1, data={"档案": "x"})


async def _snapshot(kind: str, target_id: int, instance_key: str, chat_key: str = "") -> DBStateSnapshot:
    return await DBStateSnapshot.create(
        target_kind=kind,
        target_id=target_id,
        chat_key=chat_key,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        source="update",
        result="applied",
        data={"当前时间": "早上"},
    )


async def _seed() -> dict[str, Any]:
    own_channel = await _channel(INSTANCE, f"{ADAPTER}-{INSTANCE}-group_1")
    other_channel = await _channel(OTHER, f"{ADAPTER}-{OTHER}-group_1")
    own_person = await _person(ScopeDomain.of_instance(ADAPTER, INSTANCE))
    other_person = await _person(ScopeDomain.of_instance(ADAPTER, OTHER))
    group_person = await _person(ScopeDomain.of_group(7))
    await _snapshot("channel", own_channel.id, INSTANCE, own_channel.chat_key)
    await _snapshot("person", own_person.id, INSTANCE)
    await _snapshot("channel", other_channel.id, OTHER, other_channel.chat_key)
    await _snapshot("person", group_person.id, "")
    return {
        "own_channel": own_channel,
        "other_channel": other_channel,
        "own_person": own_person,
        "other_person": other_person,
        "group_person": group_person,
    }


async def _assert_only_this_instance_is_gone(rows: dict[str, Any]) -> None:
    assert not await DBStateChannel.filter(id=rows["own_channel"].id).exists()
    assert not await DBStatePerson.filter(id=rows["own_person"].id).exists()
    assert not await DBStateSnapshot.filter(instance_key=INSTANCE).exists()
    assert await DBStateChannel.filter(id=rows["other_channel"].id).exists(), "别的实例的频道状态不动"
    assert await DBStatePerson.filter(id=rows["other_person"].id).exists(), "别的实例的个人档案不动"
    assert await DBStatePerson.filter(id=rows["group_person"].id).exists(), "实例组的个人档案挂在组上"
    assert await DBStateSnapshot.filter(instance_key=OTHER).count() == 1
    assert await DBStateSnapshot.filter(instance_key="").count() == 1


@pytest.mark.usefixtures("db")
class TestStateFollowsTheInstance:
    async def test_purge_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def no_vectors(_instance_key: str) -> None:
            """向量库不在这条用例里"""

        monkeypatch.setattr(instance_manager, "purge_instance_vectors", no_vectors)
        instance = await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            display_name=INSTANCE,
            status="online",
            provider=ADAPTER,
            tenant_id=None,
            metadata_json="",
        )
        rows = await _seed()

        counts = await instance_manager.purge_instance(instance)

        assert (counts["state_channel"], counts["state_person"], counts["state_snapshot"]) == (1, 1, 2)
        await _assert_only_this_instance_is_gone(rows)

    async def test_clearing_on_tenant_transfer(self) -> None:
        rows = await _seed()

        counts = await admin_service.purge_instance_domain_memory(ADAPTER, INSTANCE)

        assert (counts["state_channels"], counts["state_people"], counts["state_snapshots"]) == (1, 1, 2)
        await _assert_only_this_instance_is_gone(rows)


@pytest.mark.usefixtures("db")
class TestStateFollowsTheChatKey:
    async def test_rows_and_snapshots_move_with_the_new_key(self) -> None:
        old, new = f"{ADAPTER}-group_5", f"{ADAPTER}-{INSTANCE}-group_5"
        row = await _channel("", old)
        await _snapshot("channel", row.id, "", old)
        person = await _person(ScopeDomain.of_instance(ADAPTER, INSTANCE))

        counts = await chat_key_migration.migrate_chat_key_references(old, new, instance_key=INSTANCE)

        assert counts["state_channel"] == 1 and counts["state_snapshot"] == 1
        moved = await DBStateChannel.get(id=row.id)
        assert (moved.chat_key, moved.instance_key) == (new, INSTANCE)
        snapshot = await DBStateSnapshot.get(target_id=row.id)
        assert (snapshot.chat_key, snapshot.instance_key) == (new, INSTANCE)
        assert (await DBStatePerson.get(id=person.id)).instance_key == INSTANCE, "个人档案不随频道变"

    async def test_profile_snapshots_stay_on_the_profiles_domain(self) -> None:
        old, new = f"{ADAPTER}-{OTHER}-group_7", f"{ADAPTER}-{INSTANCE}-group_7"
        person = await _person(ScopeDomain.of_instance(ADAPTER, OTHER))
        await _snapshot("person", person.id, OTHER, old)  # 在这个频道里写进个人档案时留下的

        counts = await chat_key_migration.migrate_chat_key_references(old, new, instance_key=INSTANCE)

        snapshot = await DBStateSnapshot.get(target_kind="person")
        assert counts["state_snapshot"] == 0
        assert snapshot.instance_key == OTHER  # 不挂到目标实例（可能是别的租户）名下

    async def test_the_migration_preview_counts_state(self) -> None:
        chat_key = f"{ADAPTER}-{OTHER}-group_8"
        await DBChatChannel.create(
            chat_key=chat_key,
            adapter_key=ADAPTER,
            instance_key=OTHER,
            channel_id="group_8",
            channel_name="测试",
            channel_type="group",
            data="{}",
        )
        row = await _channel(OTHER, chat_key)
        await _snapshot("channel", row.id, OTHER, chat_key)
        person = await _person(ScopeDomain.of_instance(ADAPTER, OTHER))
        await _snapshot("person", person.id, OTHER, chat_key)

        plan = await chat_key_migration.plan_instance_migration(ADAPTER, OTHER, INSTANCE)

        assert (plan.counts["state_channel"], plan.counts["state_snapshot"]) == (1, 1)
        assert (plan.residues["state_person"], plan.residues["state_person_snapshot"]) == (1, 1)
        assert chat_key_migration.INSTANCE_MIGRATION_RESIDUE_REASONS["state_person"]

    async def test_the_new_keys_row_survives_a_merge(self) -> None:
        old, new = f"{ADAPTER}-{OTHER}-group_6", f"{ADAPTER}-{INSTANCE}-group_6"
        stale = await _channel(OTHER, old, 1, 当前时间="旧的")
        other_preset = await _channel(OTHER, old, 2, 当前时间="另一个人设")
        survivor = await _channel(INSTANCE, new, 1, 当前时间="新的")
        await _snapshot("channel", stale.id, OTHER, old)
        await _snapshot("channel", survivor.id, INSTANCE, new)

        await chat_key_migration.migrate_chat_key_references(old, new, instance_key=INSTANCE)

        assert not await DBStateChannel.filter(id=stale.id).exists()
        assert not await DBStateSnapshot.filter(target_id=stale.id).exists(), "删掉的那一行的快照一起删"
        assert (await DBStateChannel.get(chat_key=new, preset_id=1)).data == {"当前时间": "新的"}
        assert (await DBStateChannel.get(id=other_preset.id)).chat_key == new
        assert await DBStateSnapshot.filter(target_id=survivor.id).count() == 1
