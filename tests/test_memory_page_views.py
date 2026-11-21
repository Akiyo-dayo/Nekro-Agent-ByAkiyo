"""记忆页「看得清楚」：涉及的人显示名字、按人 / 按事件分组、摘要那一版的生成时间

## 这条测试钉的是什么

- 段落列表与详情里「涉及的人」带昵称（来源频道里最近一条发言，Bot 的发言不算；来源频道里没说过话
  就退到好感记录里的昵称，先取段落自己人设上的，并标明来自好感记录）与称呼（段落自己实例所在的
  好感域、段落自己的人设，合并过的账号按人查）。别的租户的频道、别的好感域里的名字带不出来。
- `GET /memory/persons`：按段数倒序；选了实例时同一个人的几个账号并成一行；`account_q` 只按账号筛；
  两个人的最近一段是同一段时两人都有名字；范围与段落列表一致（租户看不到别人的人，别人的实例
  404，不带实例 400）。
- `GET /memory/topics`：按事件分组、没归入事件的那一组放最后；范围外的事件只给编号。段落详情里的
  所属事件同一个口径。
- 段落列表的 `episode` 筛选（`none` / 事件 ID / 非法值与超出 32 位的值 400），批量「按筛选全选」
  认同一个筛选、同样校验。
- 提示词明细列表带那一版摘要的生成时间，详情带那一版的正文；那一版已经不保留、或摘要行的频道 /
  人设与这一轮对不上时两样都为空。

## 环境

内存 SQLite；这些接口都不碰向量库。账号、昵称、称呼都是现编的。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_affection import DBAffection
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_identity import DBIdentityAlias
from nekro_agent.models.db_mem_channel_summary import DBMemChannelSummary
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import CognitiveType, DBMemParagraph, KnowledgeType, OriginKind
from nekro_agent.models.db_mem_paragraph_person import DBMemParagraphPerson
from nekro_agent.models.db_mem_prompt_round import DBMemPromptRound
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import memory_admin
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.memory_admin import BulkParagraphsRequest, BulkSelection, ParagraphFilterSelection
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.affection.key import AffectionKey
from nekro_agent.services.identity_scope import ScopeDomain
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
CHAT_A1 = "onebot_v11-a1-group_1"
CHAT_B1 = "onebot_v11-b1-group_9"
_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _platform() -> DBUser:
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


def _tenant() -> DBUser:
    return DBUser(
        id=21,
        username="tenant-admin",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Admin,
        tenant_id=1,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )


@dataclass
class World:
    p: dict[str, DBMemParagraph]
    e: dict[str, DBMemEpisode]


_message_seq = 0


async def _message(chat_key: str, *, account: str, nickname: str, sender_id: Optional[str] = None) -> None:
    global _message_seq  # noqa: PLW0603 - 用例内递增的消息编号
    _message_seq += 1
    await DBChatMessage.create(
        sender_id=sender_id if sender_id is not None else account,
        sender_name=nickname,
        sender_nickname=nickname,
        is_tome=0,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key="",
        message_id=f"m{_message_seq}",
        chat_key=chat_key,
        chat_type="group",
        platform_userid=account,
        content_text="消息",
        content_data="{}",
        raw_cq_code="",
        ext_data="",
        send_timestamp=1_700_000 + _message_seq,
    )


async def _paragraph(
    *,
    instance_key: str = "a1",
    chat_key: str = CHAT_A1,
    preset_id: int = 7,
    persons: tuple[str, ...] = (),
    episode: Optional[DBMemEpisode] = None,
    inactive: bool = False,
) -> DBMemParagraph:
    row = await DBMemParagraph.create(
        workspace_id=0,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        preset_id=preset_id,
        origin_chat_key=chat_key,
        origin_chat_type="group",
        memory_source="na",
        cognitive_type=CognitiveType.SEMANTIC,
        knowledge_type=KnowledgeType.FACT,
        content="一段记忆",
        summary="一段记忆",
        origin_kind=OriginKind.CONSOLIDATION,
        event_time=_T0,
        episode_id=episode.id if episode is not None else None,
        is_inactive=inactive,
        quarantined_at=_T0 if inactive else None,
        quarantine_reason="manual" if inactive else "",
    )
    for account in persons:
        await DBMemParagraphPerson.create(paragraph_id=row.id, account=account)
    return row


async def _episode(instance_key: str, title: str, chat_key: str = CHAT_A1) -> DBMemEpisode:
    return await DBMemEpisode.create(
        workspace_id=0,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        preset_id=7,
        origin_chat_key=chat_key,
        origin_chat_type="group",
        title=title,
        narrative_summary=f"{title}的经过",
        paragraph_ids=[],
        participant_entity_ids=[],
        phase_mapping={},
    )


async def _affection(instance_key: str, person: str, preset_id: int, *, nickname: str, call_name: str) -> None:
    key = AffectionKey.of_domain(ScopeDomain.of_instance(ADAPTER, instance_key), person_uid=person, preset_id=preset_id)
    await DBAffection.create(**key.as_columns(), nickname=nickname, call_name=call_name, score=0, stage="stranger")


async def _seed() -> World:
    for key, tenant in (("a1", 1), ("b1", 2)):
        await DBAdapterInstance.create(adapter_key=ADAPTER, instance_key=key, display_name=key, tenant_id=tenant)
    for chat_key, instance_key in ((CHAT_A1, "a1"), (CHAT_B1, "b1")):
        await DBChatChannel.create(
            chat_key=chat_key,
            adapter_key=ADAPTER,
            instance_key=instance_key,
            channel_id=chat_key.rsplit("-", 1)[-1],
            channel_name=chat_key,
            channel_type="group",
            data="{}",
        )

    # u1 在本群改过一次昵称：取最近那条；最后一条是 Bot 用 u1 的号发的（sender_id = -1），不算
    await _message(CHAT_A1, account="u1", nickname="小明")
    await _message(CHAT_A1, account="u1", nickname="明明")
    await _message(CHAT_A1, account="u1", nickname="机器人", sender_id="-1")
    # u9 只在别的租户的群里说过话：那里的昵称不能出现在 a1 的记忆上
    await _message(CHAT_B1, account="u9", nickname="外人")

    await _affection("a1", "u1", 7, nickname="阿明", call_name="明哥")
    await _affection("a1", "u3", 7, nickname="阿三", call_name="")
    await _affection("b1", "u9", 7, nickname="外人", call_name="外号")
    # u4 是 u1 的副号（a1 的域里合并过）
    await DBIdentityAlias.create(
        domain_id=ScopeDomain.of_instance(ADAPTER, "a1").domain_id,
        platform_userid="u4",
        person_uid="u1",
    )

    e1 = await _episode("a1", "周末开黑")
    e2 = await _episode("a1", "搬家")
    eb = await _episode("b1", "别人家的事", CHAT_B1)
    p = {
        "p1": await _paragraph(persons=("u1", "u2"), episode=e1),
        "p2": await _paragraph(preset_id=8, persons=("u1",), episode=e1),
        "p3": await _paragraph(persons=("u3",)),
        "p4": await _paragraph(persons=("u4",), episode=e2),
        "p5": await _paragraph(persons=("u9",)),
        "p6": await _paragraph(persons=("u2",), inactive=True),
        # 段落在 a1，指着的事件却在 b1：分组时只能给编号
        "p7": await _paragraph(persons=(), episode=eb),
        "pb": await _paragraph(instance_key="b1", chat_key=CHAT_B1, persons=("u9",)),
    }
    return World(p=p, e={"e1": e1, "e2": e2, "eb": eb})


@pytest.fixture
async def world() -> AsyncIterator[World]:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield await _seed()
    finally:
        await Tortoise.close_connections()


def _names(item) -> list[tuple[str, str, str]]:  # noqa: ANN001 - 列表行或详情里的人
    return [(person.account, person.display_name, person.call_name) for person in item.persons]


class TestPersonNames:
    async def test_the_list_shows_nicknames_and_call_names(self, world: World) -> None:
        result = await memory_admin.list_paragraphs(
            instance_key="a1",
            adapter_key=ADAPTER,
            page_size=100,
            _current_user=_platform(),
        )
        rows = {item.id: item for item in result.items}

        # 昵称取本群最近一条（Bot 用这个号发的不算），称呼取段落自己人设上的
        assert _names(rows[world.p["p1"].id]) == [("u1", "明明", "明哥"), ("u2", "", "")]
        # 换一个人设：称呼是按人设记的，这个人设上没有
        assert _names(rows[world.p["p2"].id]) == [("u1", "明明", "")]
        # 本群没说过话：退到好感记录里的昵称
        assert _names(rows[world.p["p3"].id]) == [("u3", "阿三", "")]
        # 副号：好感按人存，昵称与称呼都按主账号查到
        assert _names(rows[world.p["p4"].id]) == [("u4", "阿明", "明哥")]
        # 只在别的租户的群里说过话、好感也在别的域：一个名字都带不出来
        assert _names(rows[world.p["p5"].id]) == [("u9", "", "")]
        # 账号列表与带名字的列表一一对应
        assert rows[world.p["p1"].id].person_accounts == ["u1", "u2"]
        assert rows[world.p["p1"].id].episode_id == world.e["e1"].id
        # 昵称从哪来：来源频道 / 好感记录（好感域共用，界面要注明）/ 没查到
        assert [person.name_source for person in rows[world.p["p1"].id].persons] == ["chat", ""]
        assert [person.name_source for person in rows[world.p["p3"].id].persons] == ["affection"]

    async def test_the_affection_nickname_follows_the_paragraph_s_own_preset(self, world: World) -> None:
        # u3 在人设 8 上另有一条好感记录、昵称不同：哪一段用哪个人设的，不能拿一页段落人设的并集去比
        await _affection("a1", "u3", 8, nickname="三哥", call_name="")
        other = await _paragraph(preset_id=8, persons=("u3",))

        result = await memory_admin.list_paragraphs(
            instance_key="a1",
            adapter_key=ADAPTER,
            page_size=100,
            _current_user=_platform(),
        )
        rows = {item.id: item for item in result.items}

        assert _names(rows[world.p["p3"].id]) == [("u3", "阿三", "")]
        assert _names(rows[other.id]) == [("u3", "三哥", "")]
        # 自己人设上没有好感记录：退到这个人任一人设上最近更新的那条（人设 8 那条是后建的）
        third = await _paragraph(preset_id=9, persons=("u3",))
        detail = await memory_admin.get_memory_item("paragraph", third.id, _current_user=_platform())
        assert detail.model_dump()["persons"] == [
            {"account": "u3", "display_name": "三哥", "call_name": "", "name_source": "affection"},
        ]

    async def test_the_detail_carries_the_call_name(self, world: World) -> None:
        detail = await memory_admin.get_memory_item("paragraph", world.p["p1"].id, _current_user=_platform())

        assert detail.model_dump()["persons"] == [
            {"account": "u1", "display_name": "明明", "call_name": "明哥", "name_source": "chat"},
            {"account": "u2", "display_name": "", "call_name": "", "name_source": ""},
        ]

    async def test_the_detail_shows_only_visible_events(self, world: World) -> None:
        platform = await memory_admin.get_memory_item("paragraph", world.p["p7"].id, _current_user=_platform())
        assert platform.model_dump()["episode"] == {"id": world.e["eb"].id, "title": "别人家的事", "visible": True}

        # 段落在 a1、指着的事件在别的租户的 b1：与按事件分组同一个口径，只给编号
        mine = await memory_admin.get_memory_item("paragraph", world.p["p7"].id, _current_user=_tenant())
        assert mine.model_dump()["episode"] == {"id": world.e["eb"].id, "title": "", "visible": False}
        own = await memory_admin.get_memory_item("paragraph", world.p["p1"].id, _current_user=_tenant())
        assert own.model_dump()["episode"] == {"id": world.e["e1"].id, "title": "周末开黑", "visible": True}

        # 事件行已经没了：同样只给编号
        orphan = await _paragraph(persons=())
        orphan.episode_id = world.e["eb"].id + 1000
        await orphan.save(update_fields=["episode_id"])
        gone = await memory_admin.get_memory_item("paragraph", orphan.id, _current_user=_platform())
        assert gone.model_dump()["episode"] == {"id": orphan.episode_id, "title": "", "visible": False}


class TestPersonsEndpoint:
    async def test_one_row_per_person_in_an_instance(self, world: World) -> None:
        result = await memory_admin.list_memory_persons(
            instance_key="a1",
            adapter_key=ADAPTER,
            _current_user=_platform(),
        )

        rows = {item.account: item for item in result.items}
        assert result.total == 4
        assert result.items[0].account == "u1"
        assert rows["u1"].accounts == ["u1", "u4"]
        assert rows["u1"].paragraph_count == 3
        # 名字取这个人最近那段记忆里出现的那个账号（u4 那段）：本群没说过话，退到好感昵称
        assert (rows["u1"].display_name, rows["u1"].call_name) == ("阿明", "明哥")
        assert rows["u1"].last_paragraph_id == world.p["p4"].id
        # 隔离区里的不算（默认只看正常行）
        assert rows["u2"].paragraph_count == 1
        assert rows["u9"].paragraph_count == 1
        assert (rows["u9"].display_name, rows["u9"].call_name) == ("", "")

    async def test_two_people_whose_latest_paragraph_is_the_same_both_get_names(self, world: World) -> None:  # noqa: ARG002
        # 两人对话：u1 与 u3 最近的一段是同一段
        shared = await _paragraph(persons=("u1", "u3"))

        result = await memory_admin.list_memory_persons(
            instance_key="a1",
            adapter_key=ADAPTER,
            _current_user=_platform(),
        )

        rows = {item.account: item for item in result.items}
        assert rows["u1"].last_paragraph_id == rows["u3"].last_paragraph_id == shared.id
        assert (rows["u1"].display_name, rows["u1"].call_name, rows["u1"].name_source) == ("明明", "明哥", "chat")
        assert (rows["u3"].display_name, rows["u3"].call_name, rows["u3"].name_source) == ("阿三", "", "affection")

    async def test_the_platform_wide_view_counts_accounts(self, world: World) -> None:  # noqa: ARG002
        result = await memory_admin.list_memory_persons(_current_user=_platform())

        counts = {item.account: item.paragraph_count for item in result.items}
        assert counts == {"u1": 2, "u2": 1, "u3": 1, "u4": 1, "u9": 2}

    async def test_filters_and_account_search(self, world: World) -> None:  # noqa: ARG002
        searched = await memory_admin.list_memory_persons(
            instance_key="a1",
            adapter_key=ADAPTER,
            account_q="U9",
            _current_user=_platform(),
        )
        assert [item.account for item in searched.items] == ["u9"]

        everything = await memory_admin.list_memory_persons(
            instance_key="a1",
            adapter_key=ADAPTER,
            status="all",
            _current_user=_platform(),
        )
        assert {item.account: item.paragraph_count for item in everything.items}["u2"] == 2

        by_preset = await memory_admin.list_memory_persons(
            instance_key="a1",
            adapter_key=ADAPTER,
            preset_id=8,
            _current_user=_platform(),
        )
        assert [(item.account, item.paragraph_count) for item in by_preset.items] == [("u1", 1)]

        paged = await memory_admin.list_memory_persons(
            instance_key="a1",
            adapter_key=ADAPTER,
            page=2,
            page_size=3,
            _current_user=_platform(),
        )
        assert paged.total == 4
        assert len(paged.items) == 1

    async def test_tenants_see_only_their_own_people(self, world: World) -> None:  # noqa: ARG002
        mine = await memory_admin.list_memory_persons(
            instance_key="a1",
            adapter_key=ADAPTER,
            _current_user=_tenant(),
        )
        # b1 那段（也挂着 u9）不在范围里
        assert {item.account: item.paragraph_count for item in mine.items}["u9"] == 1

        with pytest.raises(NotFoundError):
            await memory_admin.list_memory_persons(instance_key="b1", adapter_key=ADAPTER, _current_user=_tenant())
        with pytest.raises(ValidationError):
            await memory_admin.list_memory_persons(_current_user=_tenant())


class TestPersonResolutionBatches:
    async def test_large_batches_are_split_and_deduplicated(
        self,
        world: World,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services import identity_scope

        sizes: list[int] = []
        original = DBIdentityAlias.filter

        def spy(*args: object, **kwargs: object):  # noqa: ANN202 - 原样转交给模型的查询
            values = kwargs.get("platform_userid__in")
            if isinstance(values, list):
                sizes.append(len(values))
            return original(*args, **kwargs)

        monkeypatch.setattr(DBIdentityAlias, "filter", spy)
        # 分组视图一次最多交进来几千个账号；重复的只算一次
        accounts = [f"x{index}" for index in range(2500)] + ["u4", "x1", "u4"]
        resolved = await identity_scope.resolve_person_uids(ScopeDomain.of_instance(ADAPTER, "a1"), accounts)

        assert len(resolved) == 2501
        assert resolved["u4"] == "u1"
        assert resolved["x1"] == "x1"
        assert sizes and max(sizes) <= 1000


class TestTopicsEndpoint:
    async def test_groups_by_event_with_the_loose_ones_last(self, world: World) -> None:
        result = await memory_admin.list_memory_topics(
            instance_key="a1",
            adapter_key=ADAPTER,
            _current_user=_platform(),
        )

        groups = {item.episode_id: item for item in result.items}
        assert result.total == 4
        assert result.items[-1].episode_id is None
        assert groups[None].paragraph_count == 2
        assert groups[world.e["e1"].id].paragraph_count == 2
        assert (groups[world.e["e1"].id].title, groups[world.e["e1"].id].visible) == ("周末开黑", True)
        assert groups[world.e["e1"].id].narrative_summary == "周末开黑的经过"
        assert groups[world.e["e2"].id].paragraph_count == 1
        # 范围外的事件：只给编号，标题与经过都不漏出来
        hidden = groups[world.e["eb"].id]
        assert (hidden.visible, hidden.title, hidden.narrative_summary) == (False, "", "")

    async def test_the_person_filter_applies(self, world: World) -> None:
        result = await memory_admin.list_memory_topics(
            instance_key="a1",
            adapter_key=ADAPTER,
            person="u1",
            _current_user=_platform(),
        )

        # 选了实例，按人筛选按别名展开：u4 那段（搬家）也算 u1 的
        assert {item.episode_id: item.paragraph_count for item in result.items} == {
            world.e["e1"].id: 2,
            world.e["e2"].id: 1,
        }

    async def test_tenants_cannot_group_someone_elses_memory(self, world: World) -> None:  # noqa: ARG002
        with pytest.raises(NotFoundError):
            await memory_admin.list_memory_topics(instance_key="b1", adapter_key=ADAPTER, _current_user=_tenant())


class TestEpisodeFilter:
    async def test_the_list_filters_by_event(self, world: World) -> None:
        loose = await memory_admin.list_paragraphs(
            instance_key="a1",
            adapter_key=ADAPTER,
            episode="none",
            _current_user=_platform(),
        )
        assert {item.id for item in loose.items} == {world.p["p3"].id, world.p["p5"].id}

        one = await memory_admin.list_paragraphs(
            instance_key="a1",
            adapter_key=ADAPTER,
            episode=str(world.e["e1"].id),
            _current_user=_platform(),
        )
        assert {item.id for item in one.items} == {world.p["p1"].id, world.p["p2"].id}

    # 后三个：超出 32 位有符号整数、超过 10 位（哪怕前面是 0）、超长数字串——在 PostgreSQL 上
    # 原来是溢出成 500（数字用表达式写：长数字字面量会被公开树扫描当成账号）
    @pytest.mark.parametrize("value", ["abc", "0", "-1", "1.5", "１", str(2**31), "0" * 10 + "1", "9" * 400])
    async def test_odd_values_are_a_400(self, world: World, value: str) -> None:  # noqa: ARG002
        with pytest.raises(ValidationError):
            await memory_admin.list_paragraphs(episode=value, _current_user=_platform())

    async def test_the_largest_id_is_still_accepted(self, world: World) -> None:  # noqa: ARG002
        result = await memory_admin.list_paragraphs(episode=str(2**31 - 1), _current_user=_platform())
        assert result.total == 0

    @pytest.mark.parametrize("value", [str(2**31), "9" * 400])
    async def test_bulk_rejects_the_same_values(self, world: World, value: str) -> None:  # noqa: ARG002
        with pytest.raises(ValidationError):
            await memory_admin.bulk_paragraphs(
                BulkParagraphsRequest(
                    selection=BulkSelection(filter=ParagraphFilterSelection(episode=value)),
                    action="freeze",
                    dry_run=True,
                ),
                instance_key="a1",
                adapter_key=ADAPTER,
                _current_user=_platform(),
            )

    async def test_bulk_select_all_honours_the_event(self, world: World) -> None:
        result = await memory_admin.bulk_paragraphs(
            BulkParagraphsRequest(
                selection=BulkSelection(filter=ParagraphFilterSelection(episode=str(world.e["e1"].id))),
                action="freeze",
                dry_run=True,
            ),
            instance_key="a1",
            adapter_key=ADAPTER,
            _current_user=_platform(),
        )

        assert result.matched == 2


class TestPromptRoundSummaryTime:
    async def _rounds(self) -> tuple[DBMemChannelSummary, DBMemChannelSummary, list[DBMemPromptRound]]:
        old = await DBMemChannelSummary.create(
            chat_key=CHAT_A1, adapter_key=ADAPTER, instance_key="a1", preset_id=7, version=1, content="旧的摘要。",
        )
        new = await DBMemChannelSummary.create(
            chat_key=CHAT_A1, adapter_key=ADAPTER, instance_key="a1", preset_id=7, version=2, content="新的摘要。",
        )
        rounds: list[DBMemPromptRound] = []
        for index, (summary_id, version) in enumerate(((old.id, 1), (new.id, 2), (old.id + new.id + 100, 0))):
            rounds.append(
                await DBMemPromptRound.create(
                    round_id=f"r{index}",
                    chat_key=CHAT_A1,
                    adapter_key=ADAPTER,
                    instance_key="a1",
                    preset_id=7,
                    summary_state="injected",
                    summary_id=summary_id,
                    summary_version=version,
                    summary_chars=5,
                    started_at=_T0,
                    finished_at=_T0,
                ),
            )
        return old, new, rounds

    async def test_the_list_names_the_version_by_its_time(self, world: World) -> None:  # noqa: ARG002
        old, new, rounds = await self._rounds()

        result = await memory_admin.list_prompt_rounds(
            chat_key=CHAT_A1,
            instance_key="a1",
            adapter_key=ADAPTER,
            _current_user=_platform(),
        )

        created = {item.id: item.summary_created_at for item in result.items}
        assert created[rounds[0].id] == old.create_time
        assert created[rounds[1].id] == new.create_time
        # 那一版已经不保留：没有时间，界面退回版号
        assert created[rounds[2].id] is None

    async def test_the_detail_carries_that_version_s_text(self, world: World) -> None:  # noqa: ARG002
        old, _new, rounds = await self._rounds()

        detail = await memory_admin.get_prompt_round(rounds[0].id, _current_user=_platform())
        assert detail.summary["state"] == "outdated"
        assert detail.summary["content"] == "旧的摘要。"
        assert detail.summary["create_time"] == old.create_time

        gone = await memory_admin.get_prompt_round(rounds[2].id, _current_user=_platform())
        assert gone.summary["state"] == "deleted"
        assert (gone.summary["content"], gone.summary["create_time"]) == ("", None)

    @pytest.mark.parametrize("mismatch", ["chat", "preset"])
    async def test_a_summary_of_another_chat_or_preset_is_not_shown(self, world: World, mismatch: str) -> None:  # noqa: ARG002
        # 脏数据：明细行指着的摘要行属于别的频道（别的租户的）或别的人设。明细行本身可见，
        # 摘要正文与时间都不能借它带出来
        other = await DBMemChannelSummary.create(
            chat_key=CHAT_B1 if mismatch == "chat" else CHAT_A1,
            adapter_key=ADAPTER,
            instance_key="b1" if mismatch == "chat" else "a1",
            preset_id=7 if mismatch == "chat" else 8,
            version=1,
            content="别处的摘要。",
        )
        row = await DBMemPromptRound.create(
            round_id="r-mismatch",
            chat_key=CHAT_A1,
            adapter_key=ADAPTER,
            instance_key="a1",
            preset_id=7,
            summary_state="injected",
            summary_id=other.id,
            summary_version=1,
            summary_chars=6,
            started_at=_T0,
            finished_at=_T0,
        )

        detail = await memory_admin.get_prompt_round(row.id, _current_user=_platform())
        assert detail.summary["state"] == "deleted"
        assert (detail.summary["content"], detail.summary["create_time"]) == ("", None)

        listed = await memory_admin.list_prompt_rounds(
            chat_key=CHAT_A1,
            instance_key="a1",
            adapter_key=ADAPTER,
            _current_user=_platform(),
        )
        assert {item.id: item.summary_created_at for item in listed.items}[row.id] is None
