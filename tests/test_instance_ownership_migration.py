"""总后台「源实例 → 目标实例」历史归属迁移的回归测试

现场形态：协议端在管理员建实例**之前**先连上，被自动登记成 `ob{账号}` 并认领了历史频道；
管理员随后新建 `inst1` 绑同一账号，流量带着 `inst1` 进来，同一个群于是有两行 `instance_key`
不同的频道行。两行按隔离模型是合法的两个会话，所以不能自动合并——本模块保护的是那个
**显式**迁移动作的行为契约：

- 冲突（源与目标在同一个 `channel_id` 上各有一行）时**目标行幸存**，源行按「只补空缺、不覆盖」
  并进去后归档删除；
- 预览的计数与实际执行的计数逐项相等，"看到多少就搬多少"；
- 重复执行是空操作；
- 单个频道失败不回退已完成的频道，并且能看出来哪些没搬；
- 平台方专属：租户拿到 403（能力边界），越权访问某个实例仍是 404（资源存在性）。
"""

import json
from typing import Dict, List, Optional

import pytest
from fastapi import HTTPException
from tortoise import Tortoise

from nekro_agent.adapters.interface.base import BaseAdapter
from nekro_agent.adapters.onebot_v11.core import channel as onebot_channel
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_affection import DBAffection
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_exec_code import DBExecCode
from nekro_agent.models.db_mem_entity import DBMemEntity, EntityType
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import (
    CognitiveType,
    DBMemParagraph,
    KnowledgeType,
    OriginKind,
)
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.console.instances import (
    preview_console_instance_migration,
    run_console_instance_migration,
)
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.schemas.console import InstanceMigrationRequest
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.chat_key_migration import (
    ARCHIVED_DUPLICATES_KEY,
    build_instance_chat_key,
    migrate_instance_ownership,
    plan_instance_migration,
)
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
ACCOUNT = "123456"
# 协议端自行连入时由 `resolve_instance_key` 生成的标识
SOURCE = "ob123456"
# 管理员事后在总后台新建的实例
TARGET = "inst1"
FOREIGN = "rival1"
GROUP = "group_888"
OTHER_GROUP = "group_999"

ACME_TENANT_ID = 1
OTHER_TENANT_ID = 2


def source_chat_key(channel_id: str = GROUP) -> str:
    return f"{ADAPTER}-{SOURCE}-{channel_id}"


def target_chat_key(channel_id: str = GROUP) -> str:
    return f"{ADAPTER}-{TARGET}-{channel_id}"


@pytest.fixture(autouse=True)
def offline_qdrant(monkeypatch: pytest.MonkeyPatch):
    """向量库不参与用例：段落 payload 同步在真实部署里走网络，这里按"全部同步成功"处理

    保留调用而不是绕过它，是为了让「改了段落行就必须同步向量」这条链路仍然被执行到。
    """
    from nekro_agent.services.memory import qdrant_manager as qdrant_module

    async def fake_set(paragraph_ids: List[int], instance_key: str) -> int:  # noqa: ARG001
        return len(paragraph_ids)

    monkeypatch.setattr(qdrant_module.memory_qdrant_manager, "set_paragraph_instance_key", fake_set)


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await seed_instance(SOURCE, account=ACCOUNT, status="unclaimed")
        await seed_instance(TARGET, account=ACCOUNT, status="online")
        await seed_instance(FOREIGN, account="999999", status="online", tenant_id=OTHER_TENANT_ID)
        yield
    finally:
        await Tortoise.close_connections()


async def seed_instance(
    instance_key: str,
    *,
    account: str,
    status: str,
    tenant_id: Optional[int] = None,
    adapter_key: str = ADAPTER,
) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=adapter_key,
        instance_key=instance_key,
        display_name=instance_key,
        provider="onebot_v11",
        provider_account_id=account,
        status=status,
        tenant_id=tenant_id,
    )


async def seed_channel(
    instance_key: str,
    *,
    channel_id: str = GROUP,
    is_active: bool = True,
    preset_id: Optional[int] = None,
    workspace_id: Optional[int] = None,
    channel_name: str = "",
    data: Optional[dict] = None,
) -> DBChatChannel:
    return await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        channel_id=channel_id,
        channel_type=ChatType.GROUP.value,
        channel_name=channel_name,
        chat_key=f"{ADAPTER}-{instance_key}-{channel_id}",
        is_active=is_active,
        observe_mode=False,
        preset_id=preset_id,
        workspace_id=workspace_id,
        data=json.dumps(data or {}),
    )


async def seed_message(chat_key: str, instance_key: str, index: int) -> DBChatMessage:
    return await DBChatMessage.create(
        sender_id="1001",
        sender_name="用户",
        sender_nickname="用户",
        is_tome=0,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        message_id=f"{chat_key}-{index}",
        chat_key=chat_key,
        chat_type="group",
        platform_userid="1001",
        content_text="历史消息",
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=1_700_000_000 + index,
    )


async def seed_paragraph(instance_key: str, origin_chat_key: Optional[str]) -> DBMemParagraph:
    return await DBMemParagraph.create(
        workspace_id=1,
        instance_key=instance_key,
        memory_source="na",
        cognitive_type=CognitiveType.EPISODIC,
        knowledge_type=KnowledgeType.FACT,
        content="他们聊过一次部署",
        origin_kind=OriginKind.CONSOLIDATION,
        origin_chat_key=origin_chat_key,
    )


async def seed_episode(instance_key: str, origin_chat_key: Optional[str]) -> DBMemEpisode:
    return await DBMemEpisode.create(
        workspace_id=1,
        instance_key=instance_key,
        origin_chat_key=origin_chat_key,
        title="一次部署讨论",
        narrative_summary="他们把服务重启了一遍",
    )


def make_user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    """构造一个未落库的登录身份；权限判定只读这三个字段"""
    import datetime

    return DBUser(
        username=f"user-{tenant_role.value}-{tenant_id}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )


def platform_owner() -> DBUser:
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


def acme_admin() -> DBUser:
    return make_user(tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


class TestChatKeyFormat:
    """迁移自己拼 chat_key，必须与适配器的构建规则逐字一致

    迁移常发生在源实例已停用、适配器整体加载失败的时候，走 `adapter_utils.get_adapter()`
    会把一个纯数据库操作变成对运行时状态的依赖；代价是格式在两处各写了一遍，由本用例钉住。
    """

    @pytest.mark.parametrize(
        ("instance_key", "channel_id"),
        [("inst1", "group_888"), ("ob123456", "private_10001"), ("", "group_1")],
    )
    def test_matches_adapter_builders(self, instance_key: str, channel_id: str) -> None:
        built = build_instance_chat_key(ADAPTER, instance_key, channel_id)
        assert built == BaseAdapter.build_chat_key(_FakeAdapter(), channel_id, instance_key)
        assert built == onebot_channel.build_chat_key(instance_key, channel_id)


class _FakeAdapter:
    """只提供 `BaseAdapter.build_chat_key` 需要的 `key` 属性"""

    key = ADAPTER


class TestPreview:
    """预览必须先于执行给出准确的规模与冲突清单"""

    async def test_lists_channels_conflicts_and_counts(self, db) -> None:  # noqa: ARG002
        await seed_channel(SOURCE, channel_id=GROUP, preset_id=3, workspace_id=5, channel_name="老群")
        await seed_channel(SOURCE, channel_id=OTHER_GROUP, channel_name="另一个群")
        await seed_channel(TARGET, channel_id=GROUP)  # 空壳行，只在 GROUP 上冲突
        await seed_message(source_chat_key(GROUP), SOURCE, 1)
        await seed_message(source_chat_key(GROUP), SOURCE, 2)
        await seed_message(source_chat_key(OTHER_GROUP), SOURCE, 3)
        await DBExecCode.create(chat_key=source_chat_key(GROUP), code_text="print(1)", outputs="1")
        await seed_paragraph(SOURCE, source_chat_key(GROUP))

        plan = await plan_instance_migration(ADAPTER, SOURCE, TARGET)

        assert [(item.channel_id, item.conflict, item.message_count) for item in plan.channels] == [
            (GROUP, True, 2),
            (OTHER_GROUP, False, 1),
        ]
        assert [item.target_chat_key for item in plan.channels] == [
            target_chat_key(GROUP),
            target_chat_key(OTHER_GROUP),
        ]
        assert plan.counts["chat_channel"] == 2
        assert plan.counts["chat_message"] == 3
        assert plan.counts["exec_code"] == 1
        assert plan.counts["mem_paragraph"] == 1

    async def test_reports_data_that_will_not_move(self, db) -> None:  # noqa: ARG002
        """好感度不随迁移走，必须在预览里显式报出来而不是让用户事后才发现"""
        await seed_channel(SOURCE)
        await DBAffection.create(adapter_key=ADAPTER, instance_key=SOURCE, platform_userid="1001", score=42)

        plan = await plan_instance_migration(ADAPTER, SOURCE, TARGET)
        assert plan.residues["affection"] == 1

    async def test_counts_match_execution(self, db) -> None:  # noqa: ARG002
        """预览说要改多少行，执行就得改多少行——两条统计口径必须钉在一起"""
        await seed_channel(SOURCE, channel_id=GROUP, preset_id=3, workspace_id=5)
        await seed_channel(SOURCE, channel_id=OTHER_GROUP)
        await seed_channel(TARGET, channel_id=GROUP)
        for index in range(3):
            await seed_message(source_chat_key(GROUP), SOURCE, index)
        await seed_message(source_chat_key(OTHER_GROUP), SOURCE, 9)
        await DBExecCode.create(chat_key=source_chat_key(OTHER_GROUP), code_text="print(2)", outputs="2")
        await seed_paragraph(SOURCE, source_chat_key(GROUP))
        await seed_paragraph(SOURCE, None)  # CC 任务记忆：只挂实例、不挂频道
        await seed_episode(SOURCE, source_chat_key(GROUP))
        await DBMemEntity.create(
            workspace_id=1,
            instance_key=SOURCE,
            entity_type=EntityType.PERSON,
            name="小明",
            canonical_name="小明",
        )
        await DBMemRelation.create(
            workspace_id=1,
            instance_key=SOURCE,
            subject_entity_id=1,
            predicate="喜欢",
            object_entity_id=2,
            memory_source="na",
            cognitive_type="semantic",
        )
        await DBPreset.create(
            owner_instance_key=SOURCE,
            name="专属人设",
            title="专属",
            avatar="",
            content="内容",
            description="",
            tags="",
            author="tester",
        )

        plan = await plan_instance_migration(ADAPTER, SOURCE, TARGET)
        outcome = await migrate_instance_ownership(ADAPTER, SOURCE, TARGET)

        assert {key: outcome.counts[key] for key in plan.counts} == plan.counts
        assert plan.counts["mem_paragraph"] == 2
        assert plan.counts["mem_entity"] == 1
        assert plan.counts["mem_relation"] == 1
        assert plan.counts["preset"] == 1


class TestMigrationWithoutConflict:
    """目标实例还没有同名频道时，源行整行改判"""

    async def test_moves_the_row_and_all_references(self, db) -> None:  # noqa: ARG002
        source_row = await seed_channel(SOURCE, preset_id=3, workspace_id=5, channel_name="老群")
        await seed_message(source_chat_key(), SOURCE, 1)
        await DBExecCode.create(chat_key=source_chat_key(), code_text="print(1)", outputs="1")
        paragraph = await seed_paragraph(SOURCE, source_chat_key())
        episode = await seed_episode(SOURCE, source_chat_key())

        outcome = await migrate_instance_ownership(ADAPTER, SOURCE, TARGET)

        assert outcome.moved_channels == [GROUP]
        assert outcome.merged_channels == []
        migrated = await DBChatChannel.get(id=source_row.id)
        assert (migrated.instance_key, migrated.chat_key) == (TARGET, target_chat_key())
        assert (migrated.preset_id, migrated.workspace_id, migrated.channel_name) == (3, 5, "老群")

        assert await DBChatMessage.filter(chat_key=target_chat_key(), instance_key=TARGET).count() == 1
        assert await DBExecCode.filter(chat_key=target_chat_key()).count() == 1
        assert (await DBMemParagraph.get(id=paragraph.id)).instance_key == TARGET
        assert (await DBMemParagraph.get(id=paragraph.id)).origin_chat_key == target_chat_key()
        assert (await DBMemEpisode.get(id=episode.id)).instance_key == TARGET

    async def test_source_instance_is_left_clean(self, db) -> None:  # noqa: ARG002
        await seed_channel(SOURCE)
        await seed_message(source_chat_key(), SOURCE, 1)
        await seed_paragraph(SOURCE, source_chat_key())

        outcome = await migrate_instance_ownership(ADAPTER, SOURCE, TARGET)
        assert outcome.residual == dict.fromkeys(outcome.residual, 0)

    async def test_orphan_rows_are_reported_not_silently_left(self, db) -> None:  # noqa: ARG002
        """频道行早被删掉、消息还在的存量脏数据够不着，必须报出来而不是把「源实例干净」说成既成事实"""
        await seed_channel(SOURCE, channel_id=GROUP)
        await seed_message(f"{ADAPTER}-{SOURCE}-group_no_channel_row", SOURCE, 1)

        outcome = await migrate_instance_ownership(ADAPTER, SOURCE, TARGET)
        assert outcome.residual["chat_channel"] == 0
        assert outcome.residual["chat_message"] == 1


class TestMigrationWithConflict:
    """源与目标在同一个频道上各有一行时的合并规则"""

    async def test_target_row_survives_and_absorbs_bindings(self, db) -> None:  # noqa: ARG002
        """现场形态：源行带着人设/工作区绑定，目标行是接管流量的空壳

        幸存行取**目标行**而不是同城合并那条「取活跃者」：跨实例迁移里目标行就是当前在收消息
        的那一行，且它的 `instance_key` / `chat_key` 已经是迁移后应有的取值。
        """
        source_row = await seed_channel(
            SOURCE, preset_id=3, workspace_id=5, channel_name="老群", data={"custom_channel_name": "我的群"},
        )
        target_row = await seed_channel(TARGET)
        await seed_message(source_chat_key(), SOURCE, 1)
        await DBExecCode.create(chat_key=source_chat_key(), code_text="print(1)", outputs="1")

        outcome = await migrate_instance_ownership(ADAPTER, SOURCE, TARGET)

        assert outcome.merged_channels == [GROUP]
        assert outcome.moved_channels == []
        rows = await DBChatChannel.filter(channel_id=GROUP)
        assert [row.id for row in rows] == [target_row.id]
        merged = rows[0]
        assert (merged.instance_key, merged.chat_key) == (TARGET, target_chat_key())
        assert (merged.preset_id, merged.workspace_id, merged.channel_name) == (3, 5, "老群")

        data = json.loads(merged.data)
        assert data["custom_channel_name"] == "我的群"
        archived = data[ARCHIVED_DUPLICATES_KEY]
        assert [(entry["id"], entry["instance_key"]) for entry in archived] == [(source_row.id, SOURCE)]

        assert await DBChatMessage.filter(chat_key=target_chat_key(), instance_key=TARGET).count() == 1
        assert await DBExecCode.filter(chat_key=target_chat_key()).count() == 1
        assert await DBExecCode.filter(chat_key=source_chat_key()).count() == 0

    async def test_target_values_are_not_overwritten(self, db) -> None:  # noqa: ARG002
        """目标行上已有的值是管理员更晚的配置意图，只补空缺、绝不覆盖"""
        await seed_channel(SOURCE, preset_id=3, workspace_id=5, channel_name="老群")
        target_row = await seed_channel(TARGET, preset_id=11)

        await migrate_instance_ownership(ADAPTER, SOURCE, TARGET)

        merged = await DBChatChannel.get(id=target_row.id)
        assert merged.preset_id == 11
        assert merged.workspace_id == 5

    async def test_target_row_survives_even_when_only_the_source_is_active(self, db) -> None:  # noqa: ARG002
        """目标行被管理员停用过时同样由它幸存——那是针对目标实例作出的、更晚的决定"""
        await seed_channel(SOURCE, is_active=True, preset_id=3)
        target_row = await seed_channel(TARGET, is_active=False)

        await migrate_instance_ownership(ADAPTER, SOURCE, TARGET)

        rows = await DBChatChannel.filter(channel_id=GROUP)
        assert [row.id for row in rows] == [target_row.id]
        assert rows[0].is_active is False
        assert rows[0].preset_id == 3


class TestIdempotency:
    """重复执行不应产生新的破坏"""

    async def test_second_run_is_a_no_op(self, db) -> None:  # noqa: ARG002
        await seed_channel(SOURCE, preset_id=3, workspace_id=5)
        await seed_channel(TARGET, channel_id=OTHER_GROUP)
        await seed_message(source_chat_key(), SOURCE, 1)

        first = await migrate_instance_ownership(ADAPTER, SOURCE, TARGET)
        snapshot = sorted(
            (row.id, row.instance_key, row.chat_key, row.preset_id)
            for row in await DBChatChannel.all()
        )

        second = await migrate_instance_ownership(ADAPTER, SOURCE, TARGET)

        assert first.moved_channels == [GROUP]
        assert (second.moved_channels, second.merged_channels, second.failed_channels) == ([], [], [])
        assert second.counts["chat_channel"] == 0
        assert sorted(
            (row.id, row.instance_key, row.chat_key, row.preset_id)
            for row in await DBChatChannel.all()
        ) == snapshot

    async def test_empty_source_still_reports_every_counter(self, db) -> None:  # noqa: ARG002
        """源实例一个会话都没有时也要给出完整的 0 计数，不能让"没改"与"没统计"长得一样"""
        plan = await plan_instance_migration(ADAPTER, SOURCE, TARGET)
        outcome = await migrate_instance_ownership(ADAPTER, SOURCE, TARGET)
        assert {key: outcome.counts[key] for key in plan.counts} == plan.counts
        assert set(plan.counts) <= set(outcome.counts)
        assert max(plan.counts.values()) == 0

    async def test_merge_does_not_stack_archive_snapshots(self, db) -> None:  # noqa: ARG002
        await seed_channel(SOURCE, preset_id=3)
        target_row = await seed_channel(TARGET)

        await migrate_instance_ownership(ADAPTER, SOURCE, TARGET)
        await migrate_instance_ownership(ADAPTER, SOURCE, TARGET)

        merged = await DBChatChannel.get(id=target_row.id)
        assert len(json.loads(merged.data)[ARCHIVED_DUPLICATES_KEY]) == 1


class TestFailureIsolation:
    """中途失败不能留下比开始前更坏的状态，且要看得出来哪些没搬"""

    async def test_failed_channel_stays_behind_while_others_migrate(self, db, monkeypatch) -> None:  # noqa: ARG002
        await seed_channel(SOURCE, channel_id=GROUP, preset_id=3)
        await seed_channel(SOURCE, channel_id=OTHER_GROUP, preset_id=7)
        await seed_message(source_chat_key(GROUP), SOURCE, 1)
        await seed_message(source_chat_key(OTHER_GROUP), SOURCE, 2)

        from nekro_agent.services import chat_key_migration

        original = chat_key_migration.migrate_chat_key_references

        async def fail_on_second_group(old_chat_key: str, new_chat_key: str, instance_key: str = "") -> Dict[str, int]:
            if old_chat_key == source_chat_key(OTHER_GROUP):
                raise RuntimeError("模拟改写引用时数据库中断")
            return await original(old_chat_key, new_chat_key, instance_key)

        monkeypatch.setattr(chat_key_migration, "migrate_chat_key_references", fail_on_second_group)

        outcome = await migrate_instance_ownership(ADAPTER, SOURCE, TARGET)

        assert outcome.moved_channels == [GROUP]
        assert outcome.failed_channels == [OTHER_GROUP]
        # 成功的那个完整迁移
        assert await DBChatMessage.filter(chat_key=target_chat_key(GROUP), instance_key=TARGET).count() == 1
        # 失败的那个原封不动地留在源实例，消息与频道行仍然彼此对得上
        stalled = await DBChatChannel.get(adapter_key=ADAPTER, instance_key=SOURCE, channel_id=OTHER_GROUP)
        assert stalled.chat_key == source_chat_key(OTHER_GROUP)
        assert await DBChatMessage.filter(chat_key=source_chat_key(OTHER_GROUP), instance_key=SOURCE).count() == 1
        assert outcome.residual["chat_channel"] == 1

    async def test_rerun_picks_up_the_stalled_channel(self, db, monkeypatch) -> None:  # noqa: ARG002
        """"搬了一半"是可恢复状态：重跑一次即可补齐剩下的"""
        await seed_channel(SOURCE, channel_id=GROUP)
        await seed_channel(SOURCE, channel_id=OTHER_GROUP)

        from nekro_agent.services import chat_key_migration

        original = chat_key_migration.migrate_chat_key_references
        broken = {"enabled": True}

        async def fail_once(old_chat_key: str, new_chat_key: str, instance_key: str = "") -> Dict[str, int]:
            if broken["enabled"] and old_chat_key == source_chat_key(OTHER_GROUP):
                raise RuntimeError("模拟改写引用时数据库中断")
            return await original(old_chat_key, new_chat_key, instance_key)

        monkeypatch.setattr(chat_key_migration, "migrate_chat_key_references", fail_once)
        first = await migrate_instance_ownership(ADAPTER, SOURCE, TARGET)
        broken["enabled"] = False
        second = await migrate_instance_ownership(ADAPTER, SOURCE, TARGET)

        assert first.failed_channels == [OTHER_GROUP]
        assert second.moved_channels == [OTHER_GROUP]
        assert second.failed_channels == []
        assert await DBChatChannel.filter(instance_key=SOURCE).count() == 0


class TestConsolePermission:
    """平台方专属：租户不能自助把别人的历史迁到自己名下"""

    async def test_tenant_admin_is_rejected_by_the_role_gate(self, db) -> None:  # noqa: ARG002
        """`Role.Super` 是能力边界，拒绝发生在进入函数体之前，因此是 403 且不区分实例标识"""
        with pytest.raises(HTTPException) as excinfo:
            await preview_console_instance_migration(TARGET, SOURCE, _current_user=acme_admin())
        assert excinfo.value.status_code == 403

        with pytest.raises(HTTPException) as excinfo:
            await run_console_instance_migration(
                TARGET,
                InstanceMigrationRequest(source_instance_key=SOURCE, confirm_source_instance_key=SOURCE),
                _current_user=acme_admin(),
            )
        assert excinfo.value.status_code == 403

    async def test_role_gate_fires_before_any_data_is_touched(self, db) -> None:  # noqa: ARG002
        await seed_channel(SOURCE, preset_id=3)
        with pytest.raises(HTTPException):
            await run_console_instance_migration(
                TARGET,
                InstanceMigrationRequest(source_instance_key=SOURCE, confirm_source_instance_key=SOURCE),
                _current_user=acme_admin(),
            )
        assert await DBChatChannel.filter(instance_key=SOURCE).count() == 1

    async def test_unknown_instance_is_404(self, db) -> None:  # noqa: ARG002
        with pytest.raises(NotFoundError):
            await preview_console_instance_migration(TARGET, "ghost1", _current_user=platform_owner())
        with pytest.raises(NotFoundError):
            await preview_console_instance_migration("ghost1", SOURCE, _current_user=platform_owner())


class TestConsoleContract:
    """总后台端点自身的约束：二次确认、非法配对"""

    async def test_confirmation_must_match_source_instance_key(self, db) -> None:  # noqa: ARG002
        await seed_channel(SOURCE, preset_id=3)
        with pytest.raises(ValidationError):
            await run_console_instance_migration(
                TARGET,
                InstanceMigrationRequest(source_instance_key=SOURCE, confirm_source_instance_key=TARGET),
                _current_user=platform_owner(),
            )
        stalled = await DBChatChannel.get(adapter_key=ADAPTER, instance_key=SOURCE, channel_id=GROUP)
        assert stalled.chat_key == source_chat_key()

    async def test_same_instance_is_rejected(self, db) -> None:  # noqa: ARG002
        with pytest.raises(ValidationError):
            await preview_console_instance_migration(TARGET, TARGET, _current_user=platform_owner())

    async def test_cross_adapter_pair_is_rejected(self, db) -> None:  # noqa: ARG002
        """`chat_key` 的适配器段与 `channel_id` 命名空间都跟着适配器走，跨适配器搬运没有意义"""
        await seed_instance("tg1", account="tg", status="online", adapter_key="telegram")
        with pytest.raises(ValidationError):
            await preview_console_instance_migration("tg1", SOURCE, _current_user=platform_owner())

    async def test_preview_warns_about_shared_account_and_tenant_move(self, db) -> None:  # noqa: ARG002
        """两个实例绑同一个账号时消息路由不确定；归属跨租户则是把数据交给另一方"""
        await DBAdapterInstance.filter(instance_key=TARGET).update(tenant_id=ACME_TENANT_ID)
        preview = await preview_console_instance_migration(TARGET, SOURCE, _current_user=platform_owner())
        assert any(ACCOUNT in warning for warning in preview.warnings)
        assert any("租户" in warning for warning in preview.warnings)

    async def test_execute_reports_per_channel_outcome(self, db) -> None:  # noqa: ARG002
        await seed_channel(SOURCE, channel_id=GROUP, preset_id=3)
        await seed_channel(SOURCE, channel_id=OTHER_GROUP)
        await seed_channel(TARGET, channel_id=GROUP)

        result = await run_console_instance_migration(
            TARGET,
            InstanceMigrationRequest(source_instance_key=SOURCE, confirm_source_instance_key=SOURCE),
            _current_user=platform_owner(),
        )
        assert result.ok is True
        assert result.merged_channels == [GROUP]
        assert result.moved_channels == [OTHER_GROUP]
        assert result.residual["chat_channel"] == 0
