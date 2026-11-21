"""世界书在生命周期事件里的归属变化

新表最容易漏的地方不是接口，而是「别的事件把它搬走了 / 删掉了，它没跟上」。本文件按四类
事件各钉一组：

1. 换会话标识：频道书跟着改 `chat_key` 并回填实例；新标识下已有频道书时两本都留着
   （书表没有唯一约束，不该像摘要那样删掉一本）；
2. 实例归属迁移：域书、个人书、实例的人设书、以及频道行早已不在的频道书都改判到目标实例，
   迁移后源实例名下不该再有书；
3. 彻底删除实例：它的书与条目都没了，别的实例的书不动，进程内正则熔断表里这些条目的记录
   也一并清掉；
4. 划转与入组预览：数字只数这个实例对的书，勾选清空时真删、不勾不动。

跑在真表（SQLite 内存库）上：这些用例的价值在归属与删除顺序，不在网络调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_instance_group import DBInstanceGroup
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.routers.console import identity as identity_router
from nekro_agent.routers.console import tenants as tenants_router
from nekro_agent.schemas.tenant import TenantInstanceBindRequest, TenantRole, TenantStatus
from nekro_agent.services import chat_key_migration
from nekro_agent.services.console import instance_manager
from nekro_agent.services.user.role import Role
from nekro_agent.services.worldbook import safe_regex

ADAPTER = "test_adapter"
OTHER_ADAPTER = "other_adapter"


def _platform_user() -> DBUser:
    return DBUser(
        id=1,
        username="platform",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Super,
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


@dataclass
class World:
    platform: DBUser
    tenant: DBTenant
    group: DBInstanceGroup
    instances: dict[str, DBAdapterInstance]


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    # 共享组配置在别的用例里可能被改过；这里的域成员必须只来自本用例建的实例组。
    monkeypatch.setattr(config, "MEMORY_INSTANCE_SHARING_GROUPS", {})

    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    # 正则熔断表是进程级状态，用例之间必须从空白开始，否则上一条用例留下的键会污染断言。
    safe_regex._reset_runtime_state_for_tests()
    try:
        platform = _platform_user()
        tenant = await DBTenant.create(
            id=1,
            name="alpha",
            display_name="租户甲",
            status=TenantStatus.ACTIVE.value,
        )
        group = await DBInstanceGroup.create(name="甲的目标域", tenant_id=tenant.id)
        instances = {
            "a1": await DBAdapterInstance.create(
                adapter_key=ADAPTER,
                instance_key="a1",
                display_name="甲一",
                tenant_id=None,
            ),
            "a2": await DBAdapterInstance.create(
                adapter_key=ADAPTER,
                instance_key="a2",
                display_name="甲二",
                tenant_id=None,
            ),
            "j1": await DBAdapterInstance.create(
                adapter_key=ADAPTER,
                instance_key="j1",
                display_name="甲三",
                tenant_id=None,
            ),
        }
        yield World(platform=platform, tenant=tenant, group=group, instances=instances)
    finally:
        safe_regex._reset_runtime_state_for_tests()
        await Tortoise.close_connections()


async def _book(
    name: str,
    *,
    scope_kind: str = "domain",
    adapter_key: str = ADAPTER,
    instance_key: str = "",
    preset_id: int = 0,
    chat_key: str = "",
    person_account: str = "",
) -> DBWbBook:
    return await DBWbBook.create(
        name=name,
        scope_kind=scope_kind,
        adapter_key=adapter_key,
        instance_key=instance_key,
        preset_id=preset_id,
        chat_key=chat_key,
        person_account=person_account,
    )


async def _entry(book: DBWbBook, *, uid: int, content: str = "正文") -> DBWbEntry:
    return await DBWbEntry.create(book=book, uid=uid, content=content)


class TestChatKeyMigration:
    """认领 / 合并频道时，频道书必须跟着新标识走"""

    async def test_channel_book_follows_the_new_key_and_backfills_instance(self, world: World) -> None:
        old_chat = "test_adapter-old-group_1"
        new_chat = "test_adapter-a1-group_1"
        book = await _book("旧频道书", scope_kind="channel", instance_key="old", chat_key=old_chat)

        counts = await chat_key_migration.migrate_chat_key_references(old_chat, new_chat, instance_key="a1")

        moved = await DBWbBook.get(id=book.id)
        assert moved.chat_key == new_chat
        assert moved.instance_key == "a1"
        assert counts["wb_book"] == 1

    async def test_both_books_survive_when_the_new_key_already_has_one(self, world: World) -> None:
        """书表没有唯一约束：新标识下已经有一本时，两本都留着而不是删掉一本"""
        old_chat = "test_adapter-old-group_2"
        new_chat = "test_adapter-a1-group_2"
        old_book = await _book("旧频道的书", scope_kind="channel", instance_key="old", chat_key=old_chat)
        new_book = await _book("新频道的书", scope_kind="channel", instance_key="a1", chat_key=new_chat)

        await chat_key_migration.migrate_chat_key_references(old_chat, new_chat, instance_key="a1")

        assert await DBWbBook.filter(chat_key=new_chat).count() == 2
        assert await DBWbBook.filter(id=old_book.id).exists()
        assert await DBWbBook.filter(id=new_book.id).exists()

    async def test_non_channel_books_are_not_touched(self, world: World) -> None:
        domain_book = await _book("域书", instance_key="old")
        person_book = await _book("个人书", scope_kind="person", instance_key="old", person_account="u1")

        await chat_key_migration.migrate_chat_key_references(
            "test_adapter-old-group_3",
            "test_adapter-a1-group_3",
            instance_key="a1",
        )

        # 实例回填只落在频道书上：域书与个人书由实例归属迁移负责，这里动它们会让
        # 「换标识」顺手改判了实例，是两件事。
        for book in await DBWbBook.filter(id__in=[domain_book.id, person_book.id]):
            assert book.instance_key == "old"

    async def test_second_run_is_all_zero(self, world: World) -> None:
        old_chat = "test_adapter-old-group_4"
        new_chat = "test_adapter-a1-group_4"
        await _book("旧频道书", scope_kind="channel", instance_key="old", chat_key=old_chat)

        first = await chat_key_migration.migrate_chat_key_references(old_chat, new_chat, instance_key="a1")
        second = await chat_key_migration.migrate_chat_key_references(old_chat, new_chat, instance_key="a1")

        assert first["wb_book"] == 1
        assert all(count == 0 for count in second.values())


class TestInstanceOwnershipMigration:
    """整个实例改判时，它名下的书必须一起过去"""

    async def test_instance_scoped_books_move_to_the_target(self, world: World) -> None:
        domain_book = await _book("源实例的域书", instance_key="src")
        person_book = await _book("源实例的个人书", scope_kind="person", instance_key="src", person_account="u1")
        preset_book = await _book("源实例的人设书", scope_kind="preset", instance_key="src", preset_id=7)
        # 频道行早已不在的频道书：逐个频道的迁移够不着它，只能靠实例级那一步接住。
        orphan_channel_book = await _book(
            "频道行已不在的书",
            scope_kind="channel",
            instance_key="src",
            chat_key="test_adapter-src-group_gone",
        )

        outcome = await chat_key_migration.migrate_instance_ownership(ADAPTER, "src", "dst")

        for book in await DBWbBook.filter(id__in=[domain_book.id, person_book.id, preset_book.id, orphan_channel_book.id]):
            assert book.instance_key == "dst"
        assert outcome.counts["wb_book"] == 4
        # 孤儿频道书的标识换成目标实例段：同一个频道在目标实例下重新出现时还能选中它。
        assert (await DBWbBook.get(id=orphan_channel_book.id)).chat_key == "test_adapter-dst-group_gone"

    async def test_unrecognised_channel_key_is_left_alone(self, world: World) -> None:
        # 认不出格式（不带源实例段）的标识原样留着，只改归属。
        odd = await _book("旧格式标识的书", scope_kind="channel", instance_key="src", chat_key="test_adapter-group_9")

        await chat_key_migration.migrate_instance_ownership(ADAPTER, "src", "dst")

        refreshed = await DBWbBook.get(id=odd.id)
        assert (refreshed.instance_key, refreshed.chat_key) == ("dst", "test_adapter-group_9")

    async def test_same_instance_key_under_another_adapter_is_untouched(self, world: World) -> None:
        # 同一个实例标识在另一个适配器下是另一行（存量数据可能有）：它名下的书不跟着改判。
        mine = await _book("本适配器的书", instance_key="src")
        theirs_domain = await _book("别的适配器的域书", adapter_key="other_adapter", instance_key="src")
        theirs_channel = await _book(
            "别的适配器的频道书",
            scope_kind="channel",
            adapter_key="other_adapter",
            instance_key="src",
            chat_key="test_adapter-src-group_x",
        )

        outcome = await chat_key_migration.migrate_instance_ownership(ADAPTER, "src", "dst")

        assert (await DBWbBook.get(id=mine.id)).instance_key == "dst"
        for book_id in (theirs_domain.id, theirs_channel.id):
            book = await DBWbBook.get(id=book_id)
            assert (book.adapter_key, book.instance_key) == ("other_adapter", "src")
        assert (await DBWbBook.get(id=theirs_channel.id)).chat_key == "test_adapter-src-group_x"
        assert outcome.counts["wb_book"] == 1

    async def test_residual_counts_books_before_and_after(self, world: World) -> None:
        await _book("源实例的域书", instance_key="src")
        await DBAdapterInstance.create(adapter_key=ADAPTER, instance_key="src", display_name="源实例")
        await DBAdapterInstance.create(adapter_key=ADAPTER, instance_key="dst", display_name="目标实例")

        before = await chat_key_migration._count_instance_residual(ADAPTER, "src")
        assert before["wb_book"] == 1

        await chat_key_migration.migrate_instance_ownership(ADAPTER, "src", "dst")

        after = await chat_key_migration._count_instance_residual(ADAPTER, "src")
        assert after["wb_book"] == 0


class TestPurgeInstance:
    """彻底删除实例：书与条目一起消失，别的实例与进程内状态都要清干净"""

    async def test_books_and_entries_are_deleted_and_regex_state_is_forgotten(
        self,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        book = await _book("要删掉的域书", instance_key="a1")
        first = await _entry(book, uid=0)
        second = await _entry(book, uid=1)

        async def no_vectors(_instance_key: str) -> None:
            """向量库不在本用例的契约里"""

        monkeypatch.setattr(instance_manager, "purge_instance_vectors", no_vectors)

        # 正则在进程内按（条目, 关键词）记「慢了几次」与「已停用」，删除后这些键必须消失：
        # 留着会指向已经不存在的主键，下一个拿到同一主键的条目会继承旧记录。
        for entry in (first, second):
            safe_regex._slow_counts[(entry.id, "龙")] = 1
            safe_regex._disabled[(entry.id, "龙")] = safe_regex.DISABLED_REASON

        counts = await instance_manager.purge_instance(world.instances["a1"])

        assert counts["wb_book"] == 1
        assert counts["wb_entry"] == 2
        assert not await DBWbBook.filter(id=book.id).exists()
        assert not await DBWbEntry.filter(book_id=book.id).exists()
        assert not [key for key in safe_regex.disabled_regex_keys() if key[0] in (first.id, second.id)]
        assert not [key for key in safe_regex._slow_counts if key[0] in (first.id, second.id)]

    async def test_other_instances_books_survive(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        doomed = await _book("要删掉的书", instance_key="a1")
        kept = await _book("别的实例的书", instance_key="a2")
        kept_entry = await _entry(kept, uid=0)

        async def no_vectors(_instance_key: str) -> None:
            """向量库不在本用例的契约里"""

        monkeypatch.setattr(instance_manager, "purge_instance_vectors", no_vectors)

        await instance_manager.purge_instance(world.instances["a1"])

        assert not await DBWbBook.filter(id=doomed.id).exists()
        assert await DBWbBook.filter(id=kept.id).exists()
        assert await DBWbEntry.filter(id=kept_entry.id).exists()


class TestTransferPreview:
    """划转对话框：数字对得上，勾选清空时书与条目真删"""

    async def _preview(self, world: World, keys: list[str]) -> Any:
        return await tenants_router.preview_tenant_instances(
            tenant_id=world.tenant.id,
            body=TenantInstanceBindRequest(instance_keys=keys),
            _current_user=world.platform,
        )

    async def test_counts_only_this_instances_books(self, world: World) -> None:
        own = await _book("甲一的域书", instance_key="a1")
        await _entry(own, uid=0)
        await _entry(own, uid=1)
        other = await _book("甲二的书", instance_key="a2")
        await _entry(other, uid=0)
        # 归属为空的全局人设书不属于任何实例，不该出现在任何一行的数字里。
        global_book = await _book("全局人设书", scope_kind="preset", adapter_key="", instance_key="", preset_id=9)
        await _entry(global_book, uid=0)

        preview = await self._preview(world, ["a1"])

        change = next(item for item in preview.changes if item.instance_key == "a1")
        assert change.worldbook_books == 1
        assert change.worldbook_entries == 2

    async def test_clear_selected_deletes_books_and_entries(self, world: World) -> None:
        doomed = await _book("要清空的书", instance_key="a1")
        first = await _entry(doomed, uid=0)
        second = await _entry(doomed, uid=1)
        # 级联删掉的条目不经过删条目接口，熔断表里的记录要在清空时一并清掉。
        for entry in (first, second):
            safe_regex._slow_counts[(entry.id, "龙")] = 1
            safe_regex._disabled[(entry.id, "龙")] = safe_regex.DISABLED_REASON
        kept = await _book("别的实例的书", instance_key="a2")
        await _entry(kept, uid=0)
        # 全局人设书的归属实例对为空，任何一次划转都不该碰它。
        global_book = await _book("全局人设书", scope_kind="preset", adapter_key="", instance_key="", preset_id=9)

        await tenants_router.bind_tenant_instances(
            tenant_id=world.tenant.id,
            body=TenantInstanceBindRequest(instance_keys=["a1"], clear_memory_and_affection=True),
            _current_user=world.platform,
        )

        assert not await DBWbBook.filter(id=doomed.id).exists()
        assert not await DBWbEntry.filter(book_id=doomed.id).exists()
        assert await DBWbBook.filter(id=kept.id).exists()
        assert await DBWbBook.filter(id=global_book.id).exists()
        assert not [key for key in safe_regex.disabled_regex_keys() if key[0] in (first.id, second.id)]
        assert not [key for key in safe_regex._slow_counts if key[0] in (first.id, second.id)]

    async def test_clear_unchecked_keeps_books(self, world: World) -> None:
        kept = await _book("划走但保留的书", instance_key="a1")
        await _entry(kept, uid=0)

        await tenants_router.bind_tenant_instances(
            tenant_id=world.tenant.id,
            body=TenantInstanceBindRequest(instance_keys=["a1"], clear_memory_and_affection=False),
            _current_user=world.platform,
        )

        assert await DBWbBook.filter(id=kept.id).exists()
        assert await DBWbEntry.filter(book_id=kept.id).exists()


class TestDomainJoinPreview:
    """入组对话框：有书时要说清楚入组后这些书会对组内其它实例生效"""

    async def _preview(self, world: World, instance_key: str) -> Any:
        return await identity_router.get_domain_join_preview(
            instance_key=instance_key,
            group_id=world.group.id,
            _current_user=world.platform,
        )

    async def test_reports_book_counts_and_note(self, world: World) -> None:
        domain_book = await _book("域书", instance_key="j1")
        person_book = await _book("个人书", scope_kind="person", instance_key="j1", person_account="u1")
        await _entry(domain_book, uid=0)
        await _entry(domain_book, uid=1)
        await _entry(person_book, uid=0)

        preview = await self._preview(world, "j1")

        assert preview.worldbook_books == 2
        assert preview.worldbook_entries == 3
        assert any("2 本世界书" in note and "3 条" in note for note in preview.notes)

    async def test_no_note_without_books(self, world: World) -> None:
        preview = await self._preview(world, "j1")

        assert preview.worldbook_books == 0
        assert preview.worldbook_entries == 0
        assert not [note for note in preview.notes if "本世界书" in note]
