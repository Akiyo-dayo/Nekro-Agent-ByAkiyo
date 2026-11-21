"""好感块的两条新行、后台接口的称呼字段、入域折叠与默认启用。

## 这份用例钉什么

1. `call_as:` 只在有称呼且不处于抑制期时出现，位置在块内 `nickname:` 与 `closeness:` 之间；
2. `last_seen:` 是宿主按 `last_interact_at` 算出的事实，抑制期内照样给，门槛按天取整；
3. 一次写分把 `last_interact_at` 推到「现在」之后，久别提示自然消失；
4. 没有新数据时好感块与改动前逐字相同（不多出空行、占位符，行序不变）；
5. `absence_days` 是纯函数：门槛、缺数据、朴素时间、整 7 天四种边界；
6. 后台列表与详情都带 `call_name`，只清印象与重置都会清掉它，编辑印象不传就不动它；
7. 入域折叠时称呼跟「最近一次互动」的那一侧，只有一侧有时保留那一侧；
8. 内置插件默认启用只生效一次，平台方事后关掉不会被重新打开。

## 为什么直接建行

要考的是读侧与后台动作的取舍，不是「什么时候该记一个称呼」「什么时候该并入」。
走评估或工具链路会把模型替身、节流、配额都拖进来，一次失败要在四层里找原因。
用例里的账号、实例、群号都是现编的占位串。
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from tortoise import Tortoise
from tortoise.timezone import now as db_now

from nekro_agent.core.config import CoreConfig, config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_affection import DBAffection
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_instance_group import DBInstanceGroup
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.affection import (
    AffectionNarrativeEditRequest,
    clear_affection_narrative,
    edit_affection_narrative,
    get_affection_detail,
    get_affection_list,
    reset_affection,
)
from nekro_agent.schemas.errors import NotFoundError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.affection.evaluator import reset_evaluation_throttle
from nekro_agent.services.affection.key import AffectionKey
from nekro_agent.services.affection.prompt import (
    AFFECTION_BLOCK_KIND,
    absence_days,
    render_affection_prompt,
)
from nekro_agent.services.affection.stage import AffectionEventSource, stage_label, stage_of, stage_tone
from nekro_agent.services.affection.store import apply_delta, invalidate_cache
from nekro_agent.services.affection.suppression import (
    lift_narrative_suppression,
    reset_narrative_suppression,
    suppress_narrative,
)
from nekro_agent.services.identity_admin import merge_self_domain_into_group
from nekro_agent.services.identity_scope import PRESET_NONE, ScopeDomain
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
INSTANCE_A = "inst-alpha"
INSTANCE_B = "inst-rival"
TENANT_A = 11
TENANT_B = 22
PERSON = "acct-main"
PERSON_RIVAL = "acct-rival"
CHAT_KEY = f"{ADAPTER}-{INSTANCE_A}-group_1"
ONE_TIME_CODE = "a1b2c3d4e5f60718"


@pytest.fixture
async def db(monkeypatch: pytest.MonkeyPatch):
    """内存 SQLite 上的真表结构：域解析要读实例组绑定表，少注册一张整条链路就跑不起来"""
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    monkeypatch.setattr(config, "AI_CHAT_DEFAULT_PRESET_ID", None)
    monkeypatch.setattr(config, "AFFECTION_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "AFFECTION_ENABLE_PROMPT_INJECTION", True)
    # 关缓存：这些用例几乎每条都会在渲染之后写库，开着缓存只会读到旧快照
    monkeypatch.setattr(config, "AFFECTION_CACHE_TTL_SECONDS", 0)
    invalidate_cache()
    reset_evaluation_throttle()
    reset_narrative_suppression()
    try:
        for tenant_id, name in ((TENANT_A, "acme"), (TENANT_B, "rival")):
            await DBTenant.create(id=tenant_id, name=name, display_name=name)
        for instance_key, tenant_id in ((INSTANCE_A, TENANT_A), (INSTANCE_B, TENANT_B)):
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
        invalidate_cache()
        reset_evaluation_throttle()
        reset_narrative_suppression()
        await Tortoise.close_connections()


def make_user(tenant_id: Optional[int]) -> DBUser:
    role = TenantRole.TENANT_ADMIN if tenant_id is not None else TenantRole.PLATFORM_OWNER
    return DBUser(
        username=f"admin-{tenant_id}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Admin if tenant_id is not None else Role.Super,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=role.value,
    )


def admin_a() -> DBUser:
    return make_user(TENANT_A)


def platform_owner() -> DBUser:
    return make_user(None)


def key_of(instance_key: str, person: str = PERSON, preset_id: int = PRESET_NONE) -> AffectionKey:
    return AffectionKey.of_domain(
        ScopeDomain.of_instance(ADAPTER, instance_key),
        person_uid=person,
        preset_id=preset_id,
    )


def make_config(**overrides: Any) -> CoreConfig:
    base: Dict[str, Any] = {
        "AFFECTION_ENABLE_SYSTEM": True,
        "AFFECTION_ENABLE_PROMPT_INJECTION": True,
        "AFFECTION_PROMPT_MAX_USERS": 5,
        "AFFECTION_CACHE_TTL_SECONDS": 0,
        "AFFECTION_ABSENCE_HINT_DAYS": 7,
    }
    base.update(overrides)
    return config.model_copy(update=base)


async def seed_affection(
    key: AffectionKey,
    *,
    score: int = 45,
    state_text: str = "聊得挺投机",
    last_event_text: str = "一起赶完了一个东西",
    interaction_count: int = 3,
    call_name: str = "",
    last_interact_at: Optional[datetime.datetime] = None,
    nickname: str = "小同事",
) -> DBAffection:
    """建一行好感；分数有值、有过互动，`has_relationship` 才会为真"""
    return await DBAffection.create(
        **key.as_columns(),
        nickname=nickname,
        score=score,
        stage=stage_of(score).value,
        state_text=state_text,
        last_event_text=last_event_text,
        interaction_count=interaction_count,
        call_name=call_name,
        last_interact_at=last_interact_at,
    )


async def make_channel(instance_key: str = INSTANCE_A, chat_key: str = CHAT_KEY) -> DBChatChannel:
    channel = await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        channel_id=chat_key.rsplit("-", 1)[-1],
        channel_name=chat_key,
        channel_type="group",
        chat_key=chat_key,
        preset_id=None,
        data="{}",
    )
    # 新建频道的对话起点就是「现在」，而好感只取当前对话里的参与者：往前挪两小时
    channel.conversation_start_time = db_now() - datetime.timedelta(hours=2)
    await channel.save(update_fields=["conversation_start_time"])
    return channel


async def say(chat_key: str = CHAT_KEY, instance_key: str = INSTANCE_A, person: str = PERSON) -> DBChatMessage:
    return await DBChatMessage.create(
        sender_id=person,
        sender_name="小同事",
        sender_nickname="小同事",
        is_tome=1,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        message_id=f"msg-{person}-1",
        chat_key=chat_key,
        chat_type="group",
        platform_userid=person,
        content_text="在吗",
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=int(db_now().timestamp()) - 30,
    )


async def render(channel: DBChatChannel, **overrides: Any) -> str:
    return await render_affection_prompt(
        db_chat_channel=channel,
        one_time_code=ONE_TIME_CODE,
        config=make_config(**overrides),
    )


def block_text(rendered: str) -> str:
    """不可信块内部（开标签换行之后、闭标签之前）的正文"""
    open_tag = f"<{ONE_TIME_CODE} | {AFFECTION_BLOCK_KIND}"
    close_tag = f"<{ONE_TIME_CODE} | /{AFFECTION_BLOCK_KIND}>"
    start = rendered.index(open_tag)
    start = rendered.index("\n", start) + 1
    end = rendered.index(close_tag)
    return rendered[start:end].strip("\n")


def block_lines(rendered: str) -> List[str]:
    """块内每一行，按出现顺序；用来钉字段顺序而不是只看「在不在」"""
    return [line for line in block_text(rendered).splitlines() if line]


def line_index(rendered: str, prefix: str) -> int:
    lines = block_lines(rendered)
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            return index
    raise AssertionError(f"块里没有以 {prefix!r} 开头的行: {lines!r}")


# ---------------------------------------------------------------------------
# 一、call_as 行
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestCallAsLine:
    async def test_it_sits_between_nickname_and_closeness_inside_the_block(self) -> None:
        key = key_of(INSTANCE_A)
        await seed_affection(key, call_name="小新")
        channel = await make_channel()
        await say()

        rendered = await render(channel)

        assert "call_as: 小新" in rendered
        nick = line_index(rendered, "nickname:")
        call = line_index(rendered, "call_as:")
        close = line_index(rendered, "closeness:")
        assert nick < call < close
        # 称呼也是模型写的字，和两段描述一样必须落在不可信块里面
        start = rendered.index(f"<{ONE_TIME_CODE} | {AFFECTION_BLOCK_KIND}")
        end = rendered.index(f"<{ONE_TIME_CODE} | /{AFFECTION_BLOCK_KIND}>")
        assert start < rendered.index("call_as: 小新") < end

    async def test_suppressed_narrative_drops_the_call_as_line_only(self) -> None:
        key = key_of(INSTANCE_A)
        await seed_affection(key, call_name="小新")
        channel = await make_channel()
        await say()
        assert "call_as: 小新" in await render(channel)

        suppress_narrative(CHAT_KEY)
        try:
            rendered = await render(channel)
        finally:
            lift_narrative_suppression(CHAT_KEY)

        # 主对话刚被拦：模型写的称呼一样不给，昵称与阶段照留
        assert "call_as:" not in rendered
        assert "nickname: 小同事" in rendered
        assert "closeness:" in rendered

    async def test_a_row_that_only_has_a_call_name_is_still_rendered(self) -> None:
        # 模型只给一个从没调过分的人记了称呼：分数 0、没互动过、两段描述都空。
        # 不把称呼算作「实质」，这一行会被当成空壳跳过，记下的称呼永远出现不了
        key = key_of(INSTANCE_A)
        await seed_affection(key, score=0, state_text="", last_event_text="", interaction_count=0, call_name="小新")
        channel = await make_channel()
        await say()

        rendered = await render(channel)

        assert "call_as: 小新" in rendered


# ---------------------------------------------------------------------------
# 二、last_seen 行
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestLastSeenLine:
    async def test_eight_days_ago_is_reported_after_closeness(self) -> None:
        key = key_of(INSTANCE_A)
        # 多给两小时：按整天向下取整仍然是 8，这条用例不该卡在秒级的边界上
        await seed_affection(key, last_interact_at=db_now() - datetime.timedelta(days=8, hours=2))
        channel = await make_channel()
        await say()

        rendered = await render(channel)

        assert "last_seen: 8 days ago" in rendered
        assert line_index(rendered, "closeness:") < line_index(rendered, "last_seen:")
        assert line_index(rendered, "last_seen:") < line_index(rendered, "feeling:")
        # 块内且只有一行：块外没有第二处，块里也不该重复
        start = rendered.index(f"<{ONE_TIME_CODE} | {AFFECTION_BLOCK_KIND}")
        end = rendered.index(f"<{ONE_TIME_CODE} | /{AFFECTION_BLOCK_KIND}>")
        assert start < rendered.index("last_seen: 8 days ago") < end
        assert rendered.count("last_seen: 8 days ago") == 1

    async def test_suppression_still_shows_it(self) -> None:
        """久别天数是宿主算出来的事实，不是模型写的字，抑制期内照样给"""
        key = key_of(INSTANCE_A)
        await seed_affection(key, last_interact_at=db_now() - datetime.timedelta(days=8, hours=2))
        channel = await make_channel()
        await say()

        suppress_narrative(CHAT_KEY)
        try:
            rendered = await render(channel)
        finally:
            lift_narrative_suppression(CHAT_KEY)

        assert "last_seen: 8 days ago" in rendered
        assert "feeling:" not in rendered and "recently:" not in rendered

    async def test_zero_hint_days_never_shows_it(self) -> None:
        key = key_of(INSTANCE_A)
        await seed_affection(key, last_interact_at=db_now() - datetime.timedelta(days=8, hours=2))
        channel = await make_channel()
        await say()

        rendered = await render(channel, AFFECTION_ABSENCE_HINT_DAYS=0)

        assert "last_seen:" not in rendered
        assert "nickname: 小同事" in rendered

    async def test_six_days_ago_is_below_the_default_threshold(self) -> None:
        key = key_of(INSTANCE_A)
        await seed_affection(key, last_interact_at=db_now() - datetime.timedelta(days=6, hours=2))
        channel = await make_channel()
        await say()

        rendered = await render(channel)

        assert "last_seen:" not in rendered


# ---------------------------------------------------------------------------
# 三、写一次分之后久别提示消失
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestWritingAScoreRefreshesTheHint:
    @pytest.mark.parametrize("source", [AffectionEventSource.LLM, AffectionEventSource.TOOL], ids=["evaluated", "tool"])
    async def test_a_later_score_write_removes_last_seen(self, source: AffectionEventSource) -> None:
        key = key_of(INSTANCE_A)
        await seed_affection(key, last_interact_at=db_now() - datetime.timedelta(days=9, hours=2))
        channel = await make_channel()
        await say()
        assert "last_seen: 9 days ago" in await render(channel)

        # 一次真实的写分把 `last_interact_at` 推到此刻（评估与工具调分共用这条写入口）
        await apply_delta(
            key,
            delta=2,
            reason="刚聊完一阵",
            source=source,
            chat_key=CHAT_KEY,
        )

        rendered = await render(channel)
        assert "last_seen:" not in rendered
        assert "nickname: 小同事" in rendered


# ---------------------------------------------------------------------------
# 四、没有新数据时逐字不变
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestTheBlockIsUnchangedWithoutNewData:
    async def test_no_extra_lines_and_the_order_is_the_old_one(self) -> None:
        key = key_of(INSTANCE_A)
        await seed_affection(key, last_interact_at=db_now() - datetime.timedelta(days=3))
        channel = await make_channel()
        await say()

        rendered = await render(channel)

        stage = stage_of(45)
        # 逐字比对：多一个空行、一个占位符、一处顺序变化都会红
        assert block_text(rendered) == "\n".join(
            [
                "nickname: 小同事",
                f"closeness: {stage_label(stage)} — {stage_tone(stage)}",
                "feeling: 聊得挺投机",
                "recently: 一起赶完了一个东西",
            ],
        )
        assert "call_as:" not in rendered
        assert "last_seen:" not in rendered
        assert [line.split(":", 1)[0] for line in block_lines(rendered)] == [
            "nickname",
            "closeness",
            "feeling",
            "recently",
        ]


# ---------------------------------------------------------------------------
# 五、absence_days 纯函数
# ---------------------------------------------------------------------------


class TestAbsenceDays:
    def test_non_positive_hint_days_is_off(self) -> None:
        now = datetime.datetime(2025, 3, 12, 8, 0, 0, tzinfo=datetime.timezone.utc)
        last = now - datetime.timedelta(days=30)
        assert absence_days(last, now, 0) is None
        assert absence_days(last, now, -3) is None

    def test_missing_timestamp_is_none(self) -> None:
        now = datetime.datetime(2025, 3, 12, 8, 0, 0, tzinfo=datetime.timezone.utc)
        assert absence_days(None, now, 7) is None

    def test_naive_timestamp_is_read_as_utc_without_raising(self) -> None:
        # 库里存的一直是带时区的值，历史数据或手工改库可能出现朴素值：
        # 那时按 UTC 解释，而不是抛异常把整个好感块打空
        naive = datetime.datetime(2025, 3, 4, 8, 0, 0)
        now = datetime.datetime(2025, 3, 12, 8, 0, 0, tzinfo=datetime.timezone.utc)
        assert absence_days(naive, now, 7) == 8

    def test_exactly_seven_days_at_the_threshold_is_seven(self) -> None:
        now = datetime.datetime(2025, 3, 12, 8, 0, 0, tzinfo=datetime.timezone.utc)
        assert absence_days(now - datetime.timedelta(days=7), now, 7) == 7
        # 差一秒不到 7 天：按整天向下取整，不能提前说「7 天」
        assert absence_days(now - datetime.timedelta(days=7) + datetime.timedelta(seconds=1), now, 7) is None


# ---------------------------------------------------------------------------
# 六、后台接口：call_name
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestAdminEndpointsCarryTheCallName:
    async def test_list_and_detail_expose_it(self) -> None:
        key = key_of(INSTANCE_A)
        record = await seed_affection(key, call_name="小新")

        listing = await get_affection_list(
            page=1,
            page_size=50,
            search="",
            stage=None,
            sort_by="score_desc",
            instance_key=INSTANCE_A,
            _current_user=admin_a(),
        )
        detail = await get_affection_detail(
            record_id=record.id,
            instance_key=INSTANCE_A,
            _current_user=platform_owner(),
        )

        (item,) = [item for item in listing.items if item.id == record.id]
        assert item.call_name == "小新"
        assert detail.item.call_name == "小新"

    async def test_clear_narrative_empties_it(self) -> None:
        key = key_of(INSTANCE_A)
        record = await seed_affection(key, call_name="小新")

        item = await clear_affection_narrative(
            record_id=record.id,
            instance_key=INSTANCE_A,
            _current_user=admin_a(),
        )

        # 只清描述却留着称呼，等于「描述清空了，还是那样叫他」
        assert item.call_name == ""
        assert (await DBAffection.get(id=record.id)).call_name == ""

    async def test_reset_empties_it(self) -> None:
        key = key_of(INSTANCE_A)
        record = await seed_affection(key, call_name="小新")

        item = await reset_affection(
            record_id=record.id,
            instance_key=INSTANCE_A,
            _current_user=platform_owner(),
        )

        assert item.call_name == ""
        assert (await DBAffection.get(id=record.id)).call_name == ""

    async def test_edit_without_the_field_keeps_it(self) -> None:
        key = key_of(INSTANCE_A)
        record = await seed_affection(key, call_name="小新")

        item = await edit_affection_narrative(
            record_id=record.id,
            body=AffectionNarrativeEditRequest(state_text="换了个印象", last_event_text="换了个近况"),
            instance_key=INSTANCE_A,
            _current_user=admin_a(),
        )

        # 前端还没传这一栏时不能把模型记下的称呼顺手清掉
        assert item.call_name == "小新"
        assert (await DBAffection.get(id=record.id)).call_name == "小新"

    async def test_edit_with_the_field_replaces_it(self) -> None:
        key = key_of(INSTANCE_A)
        record = await seed_affection(key, call_name="旧称呼")

        item = await edit_affection_narrative(
            record_id=record.id,
            body=AffectionNarrativeEditRequest(
                state_text="换了个印象",
                last_event_text="换了个近况",
                call_name="小新",
            ),
            instance_key=INSTANCE_A,
            _current_user=platform_owner(),
        )

        assert item.call_name == "小新"
        assert (await DBAffection.get(id=record.id)).call_name == "小新"

    async def test_tenant_clearing_a_rival_record_is_404(self) -> None:
        rival_key = key_of(INSTANCE_B, PERSON_RIVAL)
        rival = await seed_affection(rival_key, call_name="别人的称呼")

        with pytest.raises(NotFoundError):
            await clear_affection_narrative(
                record_id=rival.id,
                instance_key=INSTANCE_B,
                _current_user=admin_a(),
            )

        assert (await DBAffection.get(id=rival.id)).call_name == "别人的称呼"

    async def test_tenant_editing_a_rival_record_is_404(self) -> None:
        rival_key = key_of(INSTANCE_B, PERSON_RIVAL)
        rival = await seed_affection(rival_key, call_name="别人的称呼")

        with pytest.raises(NotFoundError):
            await edit_affection_narrative(
                record_id=rival.id,
                body=AffectionNarrativeEditRequest(state_text="改掉", last_event_text="改掉", call_name="改掉"),
                instance_key=INSTANCE_B,
                _current_user=admin_a(),
            )

        assert (await DBAffection.get(id=rival.id)).call_name == "别人的称呼"


# ---------------------------------------------------------------------------
# 七、入域折叠时的称呼
# ---------------------------------------------------------------------------


def group_key(group: DBInstanceGroup) -> AffectionKey:
    return AffectionKey.of_domain(
        ScopeDomain.of_group(group.id),
        person_uid=PERSON,
        preset_id=PRESET_NONE,
    )


@pytest.mark.usefixtures("db")
class TestFoldingKeepsTheCallNameOfTheRightSide:
    async def test_only_the_source_side_has_one(self) -> None:
        group = await DBInstanceGroup.create(name="acme 的域", note="", tenant_id=TENANT_A)
        now = db_now()
        # 源行更旧、目标行更新：称呼仍然不能因为「谁更新」那一条而丢掉
        source = await seed_affection(
            key_of(INSTANCE_A),
            call_name="源侧的称呼",
            last_interact_at=now - datetime.timedelta(days=9),
        )
        target = await seed_affection(
            group_key(group),
            call_name="",
            last_interact_at=now - datetime.timedelta(days=4),
        )

        await merge_self_domain_into_group(adapter_key=ADAPTER, instance_key=INSTANCE_A, group=group, operator="ops")

        assert not await DBAffection.filter(id=source.id).exists()
        assert (await DBAffection.get(id=target.id)).call_name == "源侧的称呼"

    async def test_the_newer_source_side_wins(self) -> None:
        group = await DBInstanceGroup.create(name="acme 的域", note="", tenant_id=TENANT_A)
        now = db_now()
        source = await seed_affection(
            key_of(INSTANCE_A),
            call_name="源侧的称呼",
            last_interact_at=now - datetime.timedelta(hours=3),
        )
        target = await seed_affection(
            group_key(group),
            call_name="目标侧的称呼",
            last_interact_at=now - datetime.timedelta(days=6),
        )

        await merge_self_domain_into_group(adapter_key=ADAPTER, instance_key=INSTANCE_A, group=group, operator="ops")

        assert not await DBAffection.filter(id=source.id).exists()
        # 称呼跟着最近一次互动的那一侧：换另一边等于把「他最近怎么被叫」换成一段更旧的记忆
        assert (await DBAffection.get(id=target.id)).call_name == "源侧的称呼"

    async def test_the_newer_target_side_is_kept(self) -> None:
        group = await DBInstanceGroup.create(name="acme 的域", note="", tenant_id=TENANT_A)
        now = db_now()
        source = await seed_affection(
            key_of(INSTANCE_A),
            call_name="源侧的称呼",
            last_interact_at=now - datetime.timedelta(days=6),
        )
        target = await seed_affection(
            group_key(group),
            call_name="目标侧的称呼",
            last_interact_at=now - datetime.timedelta(hours=3),
        )

        await merge_self_domain_into_group(adapter_key=ADAPTER, instance_key=INSTANCE_A, group=group, operator="ops")

        assert not await DBAffection.filter(id=source.id).exists()
        assert (await DBAffection.get(id=target.id)).call_name == "目标侧的称呼"


# ---------------------------------------------------------------------------
# 八、默认启用只生效一次
# ---------------------------------------------------------------------------


def default_on_cfg(enabled: List[str], applied: List[str]) -> SimpleNamespace:
    return SimpleNamespace(PLUGIN_ENABLED=list(enabled), PLUGIN_DEFAULT_ON_APPLIED=list(applied))


class TestAffectionToolsIsDefaultOn:
    def test_the_key_is_in_the_default_on_list(self) -> None:
        from nekro_agent.services.plugin.default_on import DEFAULT_ON_BUILTIN_PLUGINS

        assert "Akiyo_dayo.affection_tools" in DEFAULT_ON_BUILTIN_PLUGINS

    def test_a_first_apply_enables_it_and_records_it(self) -> None:
        from nekro_agent.services.plugin.default_on import apply_default_on_plugins

        cfg = default_on_cfg(["KroMiose.basic"], [])
        apply_default_on_plugins(cfg)

        assert "Akiyo_dayo.affection_tools" in cfg.PLUGIN_ENABLED
        assert "Akiyo_dayo.affection_tools" in cfg.PLUGIN_DEFAULT_ON_APPLIED

        # 同一次启动里又调一遍：已应用过，两个清单都一字不变
        enabled_after_first = list(cfg.PLUGIN_ENABLED)
        applied_after_first = list(cfg.PLUGIN_DEFAULT_ON_APPLIED)
        apply_default_on_plugins(cfg)
        assert cfg.PLUGIN_ENABLED == enabled_after_first
        assert cfg.PLUGIN_DEFAULT_ON_APPLIED == applied_after_first

    def test_platform_disabling_it_afterwards_sticks(self) -> None:
        from nekro_agent.services.plugin.default_on import (
            DEFAULT_ON_BUILTIN_PLUGINS,
            apply_default_on_plugins,
        )

        # 平台方把它关掉时，它已经记在应用清单里（其余默认启用项同理）
        cfg = default_on_cfg(["KroMiose.basic"], list(DEFAULT_ON_BUILTIN_PLUGINS))
        newly = apply_default_on_plugins(cfg)

        # 默认值只决定「第一次见到时的状态」，不能盖过平台方的选择
        assert newly == []
        assert "Akiyo_dayo.affection_tools" not in cfg.PLUGIN_ENABLED
