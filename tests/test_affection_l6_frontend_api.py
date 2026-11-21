"""好感前端要用的两处后端接口：段落按来源筛选、里程碑反查好感。

## 钉什么

1. `GET /memory/paragraphs` 的 `origin_kind` 筛选：只认已知来源、空串不筛、未知值报参数错误，
   并且列表项自己带上 `origin_kind` / `origin_ref`——前端要靠这两列把里程碑行标出来；
2. `GET /affection/milestones/{paragraph_id}/record` 的归属判据：段落 → 流水 → 好感行 →
   该行对当前身份可见，四步任一不成立都是 404，平台方与租户管理员各自看到自己那一份。

## 为什么直接调路由函数

与记忆管理那组用例同一做法：这里要考的是条件拼得对不对、归属判得对不对，不是 HTTP
序列化。直接调用能拿到原样的 `ValidationError` / `NotFoundError`，断言语义比状态码更清楚。

## 数据

真表（内存 SQLite）+ 现编的租户、实例、账号与文案。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pytest
from tortoise import Tortoise
from tortoise.timezone import now as db_now

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_affection import DBAffection, DBAffectionEvent
from nekro_agent.models.db_identity import DBIdentityAlias
from nekro_agent.models.db_instance_group import DBInstanceDomainBinding, DBInstanceGroup
from nekro_agent.models.db_mem_paragraph import CognitiveType, DBMemParagraph, KnowledgeType, OriginKind
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import memory_admin
from nekro_agent.routers.affection import delete_affection_milestone, get_affection_detail, get_milestone_record
from nekro_agent.routers.affection import router as affection_router
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.memory_admin import BulkParagraphsRequest, BulkSelection, ParagraphFilterSelection
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.affection.key import AffectionKey
from nekro_agent.services.affection.milestones import clear_for_reset, latest_milestone_texts, strip_reason
from nekro_agent.services.affection.stage import AffectionEventSource, stage_of
from nekro_agent.services.affection.store import invalidate_cache, load_snapshot
from nekro_agent.services.identity_scope import PRESET_NONE, ScopeDomain
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
INSTANCE_OWN = "inst-own-alpha"
INSTANCE_FOREIGN = "inst-foreign-beta"
TENANT_OWN = 31
TENANT_FOREIGN = 47
PERSON_OWN = "acct-local"
PERSON_FOREIGN = "acct-elsewhere"
CHAT_OWN = f"{ADAPTER}-{INSTANCE_OWN}-group_alpha"
CHAT_FOREIGN = f"{ADAPTER}-{INSTANCE_FOREIGN}-group_beta"
WORKSPACE_ID = 0
MILESTONE_TEXT = "那天一起把收尾的活儿干完了，关系近了一步"
PLAIN_TEXT = "这台设备的默认时区是东八区"
MILESTONE_REF = "affection_event:sample"


@pytest.fixture
async def db(monkeypatch: pytest.MonkeyPatch):
    """内存 SQLite 上的真表 + 两个租户各一个实例。

    注册整个 `nekro_agent.models`：域解析会读实例组绑定表与人设表，少注册一张，
    整条解析链在测试里就直接报「表不存在」。
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    monkeypatch.setattr(config, "AI_CHAT_DEFAULT_PRESET_ID", None)
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
    try:
        for tenant_id, name in ((TENANT_OWN, "alpha"), (TENANT_FOREIGN, "beta")):
            await DBTenant.create(id=tenant_id, name=name, display_name=name)
        for instance_key, tenant_id in ((INSTANCE_OWN, TENANT_OWN), (INSTANCE_FOREIGN, TENANT_FOREIGN)):
            await DBAdapterInstance.create(
                adapter_key=ADAPTER,
                instance_key=instance_key,
                display_name=instance_key,
                status="online",
                provider=ADAPTER,
                tenant_id=tenant_id,
                metadata_json="",
            )
        yield
    finally:
        await Tortoise.close_connections()


def make_user(tenant_id: Optional[int]) -> DBUser:
    role = TenantRole.TENANT_ADMIN if tenant_id is not None else TenantRole.PLATFORM_OWNER
    return DBUser(
        username=f"operator-{tenant_id}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Admin if tenant_id is not None else Role.Super,
        login_time=datetime.now(timezone.utc),
        tenant_id=tenant_id,
        tenant_role=role.value,
    )


def platform_owner() -> DBUser:
    return make_user(None)


def tenant_admin_own() -> DBUser:
    return make_user(TENANT_OWN)


def key_of(instance_key: str, person: str) -> AffectionKey:
    return AffectionKey.of_domain(
        ScopeDomain.of_instance(ADAPTER, instance_key),
        person_uid=person,
        preset_id=PRESET_NONE,
    )


async def seed_paragraph(
    *,
    instance_key: str,
    content: str,
    origin_kind: OriginKind,
    origin_ref: Optional[str] = None,
    chat_key: str = CHAT_OWN,
) -> DBMemParagraph:
    return await DBMemParagraph.create(
        workspace_id=WORKSPACE_ID,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        preset_id=PRESET_NONE,
        origin_chat_key=chat_key,
        origin_chat_type="group",
        memory_source="na",
        cognitive_type=CognitiveType.SEMANTIC,
        knowledge_type=KnowledgeType.EMOTION,
        content=content,
        summary=content,
        event_time=db_now(),
        half_life_seconds=90 * 24 * 3600,
        is_frozen=True,
        origin_kind=origin_kind,
        origin_ref=origin_ref,
    )


async def seed_affection_with_milestone(
    *,
    instance_key: str,
    person: str,
    chat_key: str,
    content: str = MILESTONE_TEXT,
    score: int = 42,
) -> tuple[DBAffection, DBMemParagraph, DBAffectionEvent]:
    """一份好感 + 一条指向里程碑段落的流水；段落、流水、好感三处同源。"""
    key = key_of(instance_key, person)
    record = await DBAffection.create(
        **key.as_columns(),
        nickname="小同事",
        score=score,
        stage=stage_of(score).value,
        state_text="聊得挺投机",
        last_event_text="一起赶完了收尾的活儿",
        interaction_count=4,
    )
    paragraph = await seed_paragraph(
        instance_key=instance_key,
        content=content,
        origin_kind=OriginKind.AFFECTION,
        origin_ref=MILESTONE_REF,
        chat_key=chat_key,
    )
    event = await DBAffectionEvent.create(
        **key.as_columns(),
        source_platform_userid=person,
        chat_key=chat_key,
        delta=8,
        score_after=record.score,
        source=AffectionEventSource.LLM.value,
        reason="那天一起赶完了收尾的活儿",
        memory_paragraph_id=paragraph.id,
    )
    record.milestone_refs = [
        {
            "paragraph_id": paragraph.id,
            "event_id": event.id,
            "from": "acquainted",
            "to": "friendly",
            "at": db_now().isoformat(),
        },
    ]
    await record.save(update_fields=["milestone_refs"])
    return record, paragraph, event


class TestParagraphListOriginKindFilter:
    """段落列表的来源类型筛选"""

    async def test_affection_returns_only_the_milestone_paragraph(self, db: None) -> None:
        _record, milestone, _event = await seed_affection_with_milestone(
            instance_key=INSTANCE_OWN,
            person=PERSON_OWN,
            chat_key=CHAT_OWN,
        )
        await seed_paragraph(
            instance_key=INSTANCE_OWN,
            content=PLAIN_TEXT,
            origin_kind=OriginKind.CONSOLIDATION,
        )

        response = await memory_admin.list_paragraphs(
            _current_user=platform_owner(),
            origin_kind="affection",
            status="all",
            page_size=100,
        )

        assert {item.id for item in response.items} == {milestone.id}
        assert response.total == 1

    async def test_empty_origin_kind_does_not_filter(self, db: None) -> None:
        _record, milestone, _event = await seed_affection_with_milestone(
            instance_key=INSTANCE_OWN,
            person=PERSON_OWN,
            chat_key=CHAT_OWN,
        )
        plain = await seed_paragraph(
            instance_key=INSTANCE_OWN,
            content=PLAIN_TEXT,
            origin_kind=OriginKind.CONSOLIDATION,
        )

        response = await memory_admin.list_paragraphs(
            _current_user=platform_owner(),
            origin_kind="",
            status="all",
            page_size=100,
        )

        assert {item.id for item in response.items} == {milestone.id, plain.id}

    async def test_unknown_origin_kind_is_a_validation_error(self, db: None) -> None:
        # 写错的值必须在入口就变成 400：交给数据库只会安静地返回零行，
        # 前端看到的是「一段记忆都没有」而不是「参数写错了」。
        with pytest.raises(ValidationError) as exc:
            await memory_admin.list_paragraphs(
                _current_user=platform_owner(),
                origin_kind="bogus",
                status="all",
            )

        assert exc.value.http_status == 400
        assert "未知的来源类型筛选" in str(exc.value)

    async def test_list_item_carries_origin_kind_and_ref(self, db: None) -> None:
        _record, milestone, _event = await seed_affection_with_milestone(
            instance_key=INSTANCE_OWN,
            person=PERSON_OWN,
            chat_key=CHAT_OWN,
        )
        plain = await seed_paragraph(
            instance_key=INSTANCE_OWN,
            content=PLAIN_TEXT,
            origin_kind=OriginKind.CONSOLIDATION,
            origin_ref=None,
        )

        response = await memory_admin.list_paragraphs(
            _current_user=platform_owner(),
            status="all",
            page_size=100,
        )
        by_id = {item.id: item for item in response.items}

        # 枚举在响应里是字符串，前端才可以拿它做相等比较
        assert by_id[milestone.id].origin_kind == "affection"
        assert by_id[milestone.id].origin_ref == MILESTONE_REF
        assert by_id[plain.id].origin_kind == "consolidation"
        # 库里是 NULL 的来源引用，对外给 None 而不是空串
        assert by_id[plain.id].origin_ref is None

    async def test_tenant_admin_can_filter_inside_own_instance(self, db: None) -> None:
        _record, milestone, _event = await seed_affection_with_milestone(
            instance_key=INSTANCE_OWN,
            person=PERSON_OWN,
            chat_key=CHAT_OWN,
        )
        await seed_affection_with_milestone(
            instance_key=INSTANCE_FOREIGN,
            person=PERSON_FOREIGN,
            chat_key=CHAT_FOREIGN,
        )

        response = await memory_admin.list_paragraphs(
            _current_user=tenant_admin_own(),
            instance_key=INSTANCE_OWN,
            origin_kind="affection",
            status="all",
            page_size=100,
        )

        # 筛选不能放宽可见范围：自己实例下只有这一条，别人的实例一条都不该出现
        assert {item.id for item in response.items} == {milestone.id}
        assert response.total == 1


class TestMilestoneParagraphRecordEndpoint:
    """里程碑段落反查它属于哪一份好感"""

    async def test_platform_owner_gets_the_record_id(self, db: None) -> None:
        record, milestone, _event = await seed_affection_with_milestone(
            instance_key=INSTANCE_OWN,
            person=PERSON_OWN,
            chat_key=CHAT_OWN,
        )

        response = await get_milestone_record(
            paragraph_id=milestone.id,
            instance_key="",
            _current_user=platform_owner(),
        )

        assert response.record_id == record.id

    async def test_tenant_admin_gets_the_record_of_own_instance(self, db: None) -> None:
        record, milestone, _event = await seed_affection_with_milestone(
            instance_key=INSTANCE_OWN,
            person=PERSON_OWN,
            chat_key=CHAT_OWN,
        )

        response = await get_milestone_record(
            paragraph_id=milestone.id,
            instance_key=INSTANCE_OWN,
            _current_user=tenant_admin_own(),
        )

        assert response.record_id == record.id

    async def test_tenant_admin_asking_a_foreign_paragraph_is_404(self, db: None) -> None:
        _foreign_record, foreign_milestone, _foreign_event = await seed_affection_with_milestone(
            instance_key=INSTANCE_FOREIGN,
            person=PERSON_FOREIGN,
            chat_key=CHAT_FOREIGN,
        )

        # 拿自己名下的实例作用域去找别人的段落：能定位到流水与好感行，
        # 但那一行不在自己的域里，必须按不存在处理（不暴露编号是否有效）。
        with pytest.raises(NotFoundError):
            await get_milestone_record(
                paragraph_id=foreign_milestone.id,
                instance_key=INSTANCE_OWN,
                _current_user=tenant_admin_own(),
            )
        # 直接报别人的实例标识，同样只是 404
        with pytest.raises(NotFoundError):
            await get_milestone_record(
                paragraph_id=foreign_milestone.id,
                instance_key=INSTANCE_FOREIGN,
                _current_user=tenant_admin_own(),
            )

    async def test_unknown_paragraph_is_404(self, db: None) -> None:
        with pytest.raises(NotFoundError):
            await get_milestone_record(
                paragraph_id=975_311,
                instance_key=INSTANCE_OWN,
                _current_user=platform_owner(),
            )

    async def test_existing_paragraph_without_an_event_is_404(self, db: None) -> None:
        # 段落存在、也在自己的实例下，但没有流水生成过它：归属判据不成立
        plain = await seed_paragraph(
            instance_key=INSTANCE_OWN,
            content=PLAIN_TEXT,
            origin_kind=OriginKind.CONSOLIDATION,
        )

        with pytest.raises(NotFoundError):
            await get_milestone_record(
                paragraph_id=plain.id,
                instance_key=INSTANCE_OWN,
                _current_user=platform_owner(),
            )

    async def test_a_plain_paragraph_is_404_even_when_other_milestones_exist(self, db: None) -> None:
        # 库里有别的里程碑流水时也不能「随便找一条流水」顶上：
        # 那样任何段落编号都会换出某一份好感的记录编号
        await seed_affection_with_milestone(
            instance_key=INSTANCE_OWN,
            person=PERSON_OWN,
            chat_key=CHAT_OWN,
        )
        plain = await seed_paragraph(
            instance_key=INSTANCE_OWN,
            content=PLAIN_TEXT,
            origin_kind=OriginKind.CONSOLIDATION,
        )

        with pytest.raises(NotFoundError):
            await get_milestone_record(
                paragraph_id=plain.id,
                instance_key=INSTANCE_OWN,
                _current_user=platform_owner(),
            )

    def test_the_record_route_is_declared_before_the_detail_route(self) -> None:
        """具体路径必须排在 `GET /{record_id}` 前面，否则会被路径参数先匹配走"""
        paths = [route.path for route in affection_router.routes]
        assert paths.index("/affection/milestones/{paragraph_id}/record") < paths.index("/affection/{record_id}")


# ---------------------------------------------------------------------------
# 复核补的几条：合并过的人、随实例划走的里程碑、批量按来源筛选
# ---------------------------------------------------------------------------

INSTANCE_OWN_SECOND = "inst-own-gamma"
PERSON_ALIAS = "acct-old-number"


class TestMilestoneRecordFollowsTheMergedPerson:
    async def test_a_milestone_written_by_a_merged_account_opens_the_persons_row(self, db: None) -> None:
        # 副号 B 并入 A 之前写过一条里程碑：流水上记的是 B。反查要落到 A 那一行（详情页列出它的那一份），
        # 而不是 B 自己那一行休眠数据
        person_record = await DBAffection.create(
            **key_of(INSTANCE_OWN, PERSON_OWN).as_columns(),
            nickname="小同事",
            score=50,
            stage=stage_of(50).value,
        )
        _alias_record, milestone, _event = await seed_affection_with_milestone(
            instance_key=INSTANCE_OWN,
            person=PERSON_ALIAS,
            chat_key=CHAT_OWN,
        )
        await DBIdentityAlias.create(
            domain_id=ScopeDomain.of_instance(ADAPTER, INSTANCE_OWN).domain_id,
            platform_userid=PERSON_ALIAS,
            person_uid=PERSON_OWN,
        )

        response = await get_milestone_record(paragraph_id=milestone.id, instance_key="", _current_user=platform_owner())

        assert response.record_id == person_record.id


async def _group_world() -> tuple[DBAffection, DBMemParagraph, AffectionKey]:
    """租户自己的两个实例放进同一个组；组域上一份好感，里程碑段落由第一个实例写"""
    await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE_OWN_SECOND,
        display_name=INSTANCE_OWN_SECOND,
        status="online",
        provider=ADAPTER,
        tenant_id=TENANT_OWN,
        metadata_json="",
    )
    group = await DBInstanceGroup.create(name="甲的组", tenant_id=TENANT_OWN)
    for instance_key in (INSTANCE_OWN, INSTANCE_OWN_SECOND):
        await DBInstanceDomainBinding.create(adapter_key=ADAPTER, instance_key=instance_key, memory_group_id=group.id)
    key = AffectionKey.of_domain(ScopeDomain.of_group(group.id), person_uid=PERSON_OWN, preset_id=PRESET_NONE)
    record = await DBAffection.create(**key.as_columns(), nickname="小同事", score=50, stage=stage_of(50).value)
    paragraph = await seed_paragraph(instance_key=INSTANCE_OWN, content=MILESTONE_TEXT, origin_kind=OriginKind.AFFECTION)
    event = await DBAffectionEvent.create(
        **key.as_columns(),
        source_platform_userid=PERSON_OWN,
        chat_key=CHAT_OWN,
        delta=6,
        score_after=50,
        source=AffectionEventSource.LLM.value,
        reason="一起把收尾的活儿干完了",
        memory_paragraph_id=paragraph.id,
    )
    record.milestone_refs = [
        {"paragraph_id": paragraph.id, "event_id": event.id, "from": "acquainted", "to": "friendly", "at": db_now().isoformat()},
    ]
    await record.save(update_fields=["milestone_refs"])
    invalidate_cache()
    return record, paragraph, key


class TestMilestonesThatLeftWithTheirInstance:
    """组内的一个实例被单独划走、没勾一并清空：段落跟着实例走了，好感行还在原来的组上"""

    async def test_before_the_move_everything_reads_it(self, db: None) -> None:
        record, paragraph, key = await _group_world()

        detail = await get_affection_detail(record_id=record.id, instance_key="", _current_user=platform_owner())
        texts = await latest_milestone_texts([key], {key: await load_snapshot(key, 0)})

        assert [item.status for item in detail.milestones] == ["active"]
        assert texts[key] == (paragraph.id, MILESTONE_TEXT)

    async def test_after_the_move_this_side_no_longer_reads_or_touches_it(self, db: None) -> None:
        record, paragraph, key = await _group_world()
        await DBInstanceDomainBinding.release_instances([INSTANCE_OWN], reason="测试：划给别的租户")
        await DBMemParagraph.filter(id=paragraph.id).update(summary="只剩模板的那一句")

        texts = await latest_milestone_texts([key], {key: await load_snapshot(key, 0)})
        detail = await get_affection_detail(record_id=record.id, instance_key="", _current_user=platform_owner())

        assert texts == {}, "好感块不能再注入已经归了别人的段落"
        assert len(detail.milestones) == 1
        assert detail.milestones[0].status == "moved"
        assert detail.milestones[0].content == "", "正文已经是别人的数据，详情里不给看"
        with pytest.raises(NotFoundError):
            await delete_affection_milestone(
                record_id=record.id,
                paragraph_id=paragraph.id,
                instance_key="",
                _current_user=platform_owner(),
            )

        await clear_for_reset(key)
        await strip_reason(paragraph.id, domain=key.domain)

        row = await DBMemParagraph.get(id=paragraph.id)
        assert row.is_inactive is False, "重置不能顺手软删别人的记忆"
        assert row.content == MILESTONE_TEXT, "删理由不能改写别人的记忆"


class TestBulkByFilterHonoursOriginKind:
    async def test_select_all_by_filter_only_touches_that_origin(self, db: None) -> None:
        _record, milestone, _event = await seed_affection_with_milestone(
            instance_key=INSTANCE_OWN,
            person=PERSON_OWN,
            chat_key=CHAT_OWN,
        )
        plain = await seed_paragraph(instance_key=INSTANCE_OWN, content=PLAIN_TEXT, origin_kind=OriginKind.CONSOLIDATION)

        selection = BulkSelection(filter=ParagraphFilterSelection(status="all", origin_kind="affection"))
        # 按筛选执行要先预览、再带着预览条数提交：预览数出来的也只能是那一条里程碑
        preview = await memory_admin.bulk_paragraphs(
            body=BulkParagraphsRequest(action="unfreeze", selection=selection, dry_run=True),
            _current_user=platform_owner(),
        )
        assert preview.matched == 1
        await memory_admin.bulk_paragraphs(
            body=BulkParagraphsRequest(action="unfreeze", selection=selection, expected_count=preview.matched),
            _current_user=platform_owner(),
        )

        assert (await DBMemParagraph.get(id=milestone.id)).is_frozen is False
        assert (await DBMemParagraph.get(id=plain.id)).is_frozen is True, "按来源筛选的批量不能动到别的来源"

    async def test_an_unknown_origin_in_the_bulk_filter_is_rejected(self, db: None) -> None:
        with pytest.raises(ValidationError):
            await memory_admin.bulk_paragraphs(
                body=BulkParagraphsRequest(
                    action="unfreeze",
                    selection=BulkSelection(filter=ParagraphFilterSelection(status="all", origin_kind="bogus")),
                ),
                _current_user=platform_owner(),
            )
