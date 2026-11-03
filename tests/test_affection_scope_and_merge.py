"""好感度接上 `(域, 人, 人设)` 之后的契约测试

`tests/test_affection_system.py` 钉的是好感度**自己**的性质（fail-open、不可信内容边界、
数值与速率）。这一份钉的是它与隔离键的接缝，六条：

1. **键只能由解析器产生。** `AffectionKey.of_scope(scope).as_columns()` 必须与
   `IdentityScope.affection_columns()` 逐字相同——两边一旦分叉，写进去的行自己查不到
   （好一点）或读到别人的行（坏的那种），而且都不会报错。
2. **隔离键不含会话。** 同一个人在群里和私聊里是同一份好感；这一条只在**同一人设下**成立。
3. **人设就是不同的人。** 同一个人在两个人设下是两份好感，互不可见。
4. **好感域默认跟随记忆域。** 不配置覆盖时两者逐字相同；配了覆盖就只有好感跟着走。
5. **换账号不能丢。** 合并是别名指向：好感取高的、流水取并集且保留来源账号，
   **删别名即撤销，撤销后两边各自的原值完好**。这是整条设计的承重墙。
6. **"一个用户一份"的旧假设全部作废。** 提示词、评估、额度、后台作用域四处都按
   "一个人可能有好几个账号、一个实例可能属于一个更大的域"重新成立。

## 为什么全部跑真表

隔离与合并都是数据层的性质，用假存储验证等于把要保护的东西替换掉了。判据与
`tests/test_affection_system.py` / `tests/test_identity_scope_foundation.py` 的固件同源。
"""

from datetime import timedelta
from typing import Dict, List, Optional, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import CoreConfig, config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_affection import DBAffection, DBAffectionEvent
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_identity import DBIdentityAlias
from nekro_agent.models.db_instance_group import DBInstanceDomainBinding, DBInstanceGroup
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services.affection import prompt as prompt_module
from nekro_agent.services.affection.evaluator import evaluate_channel, reset_evaluation_throttle
from nekro_agent.services.affection.key import AffectionKey
from nekro_agent.services.affection.merge import (
    PresetTransfer,
    list_person_accounts,
    merge_accounts,
    unmerge_account,
)
from nekro_agent.services.affection.prompt import affection_score_of, render_affection_prompt
from nekro_agent.services.affection.stage import AffectionEventSource, AffectionRateLimits
from nekro_agent.services.affection.store import (
    affection_domain_of_instance,
    apply_delta,
    invalidate_cache,
    key_of_record,
    list_events,
    load_snapshot,
    person_account_ids,
    throttle_llm_delta,
    window_usage,
)
from nekro_agent.services.identity_scope import (
    PRESET_NONE,
    ScopeDomain,
    resolve_identity_scope,
    resolve_memory_domain,
)

ADAPTER = "onebot_v11"
INSTANCE_A = "ob10001"
INSTANCE_B = "ob10002"
GROUP_CHAT_A = f"{ADAPTER}-{INSTANCE_A}-group_777"
PRIVATE_CHAT_A = f"{ADAPTER}-{INSTANCE_A}-private_123456"
GROUP_CHAT_B = f"{ADAPTER}-{INSTANCE_B}-group_888"
MAIN_ACCOUNT = "123456"
ALT_ACCOUNT = "654321"
THIRD_ACCOUNT = "111222"
PRESET_ALPHA = 11
PRESET_BETA = 22
ONE_TIME_CODE = "a1b2c3d4e5f60718"


@pytest.fixture
async def scope_db():
    """内存 SQLite 上的真实表结构（全量模型）

    隔离键的三维分别落在三张不同的表上，缺一张这条链路就跑不起来。
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    invalidate_cache()
    reset_evaluation_throttle()
    try:
        yield
    finally:
        invalidate_cache()
        reset_evaluation_throttle()
        await Tortoise.close_connections()


class _StubChannel:
    """频道替身：解析器要的三样 + 提示词要的会话标识与会话类型"""

    def __init__(
        self,
        chat_key: str,
        instance_key: str = INSTANCE_A,
        *,
        preset_id: int = PRESET_NONE,
        channel_type: str = "group",
    ) -> None:
        self.chat_key = chat_key
        self.adapter_key = ADAPTER
        self.instance_key = instance_key
        self.channel_type = channel_type
        self._preset_id = preset_id

    @property
    def chat_type(self) -> ChatType:
        return ChatType(self.channel_type)

    async def get_effective_preset_id(self) -> int:
        return self._preset_id

    async def get_preset(self):
        return type("_Preset", (), {"name": "测试人设", "content": "你是一只话很多的猫。"})()


def make_config(**overrides: object) -> CoreConfig:
    base: Dict[str, object] = {
        "AFFECTION_ENABLE_SYSTEM": True,
        "AFFECTION_ENABLE_PROMPT_INJECTION": True,
        "AFFECTION_PROMPT_MAX_USERS": 5,
        "AFFECTION_MAX_DELTA_PER_ROUND": 5,
        "AFFECTION_UPDATE_MIN_INTERVAL_SECONDS": 0,
        "AFFECTION_CACHE_TTL_SECONDS": 0,
    }
    base.update(overrides)
    return config.model_copy(update=base)


async def add_message(
    chat_key: str,
    platform_userid: str,
    content: str,
    *,
    instance_key: str = INSTANCE_A,
    nickname: str = "小明",
    timestamp: int = 4_000_000_000,
) -> DBChatMessage:
    return await DBChatMessage.create(
        sender_id=platform_userid,
        sender_name=nickname,
        sender_nickname=nickname,
        is_tome=1,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        message_id=f"m{platform_userid}{timestamp}",
        chat_key=chat_key,
        chat_type="group",
        platform_userid=platform_userid,
        content_text=content,
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=timestamp,
    )


def stub_llm(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    import json

    class _Response:
        response_content = payload if isinstance(payload, str) else json.dumps(payload)

    async def _fake_gen(**_kwargs: object) -> object:
        return _Response()

    from nekro_agent.services.agent import openai as openai_module

    monkeypatch.setattr(openai_module, "gen_openai_chat_response", _fake_gen)


async def make_instance(instance_key: str, *, tenant_id: Optional[int] = None) -> DBAdapterInstance:
    return await DBAdapterInstance.create(adapter_key=ADAPTER, instance_key=instance_key, tenant_id=tenant_id)


async def make_group(name: str = "family", *, tenant_id: Optional[int] = None) -> DBInstanceGroup:
    return await DBInstanceGroup.create(name=name, tenant_id=tenant_id)


async def join_group(
    instance_key: str,
    *,
    memory_group: Optional[DBInstanceGroup] = None,
    affection_group: Optional[DBInstanceGroup] = None,
) -> None:
    """把实例放进域——走**唯一的写入口**，跨租户校验也顺带在这里生效"""
    await DBInstanceDomainBinding.bind_to_groups(
        ADAPTER,
        instance_key,
        memory_group=memory_group,
        affection_group=affection_group,
    )


def self_domain(instance_key: str = INSTANCE_A) -> ScopeDomain:
    return ScopeDomain.of_instance(ADAPTER, instance_key)


async def key_of(
    platform_userid: str,
    *,
    chat_key: str = GROUP_CHAT_A,
    instance_key: str = INSTANCE_A,
    preset_id: int = PRESET_NONE,
    channel_type: str = "group",
) -> AffectionKey:
    """按"某人在某个频道说话"解析出好感隔离键——与热路径完全同一条链路"""
    channel = _StubChannel(chat_key, instance_key, preset_id=preset_id, channel_type=channel_type)
    return AffectionKey.of_scope(await resolve_identity_scope(channel, platform_userid))  # type: ignore[arg-type]


async def scores_of(key: AffectionKey) -> int:
    return (await load_snapshot(key, ttl_seconds=0)).score


class TestKeyIsProducedByTheResolver:
    """键只能由解析器产生，两边的列必须逐字相同"""

    @pytest.mark.parametrize(
        ("with_group", "with_alias", "preset_id"),
        [
            (False, False, PRESET_NONE),
            (False, False, PRESET_ALPHA),
            (True, False, PRESET_NONE),
            (True, True, PRESET_BETA),
            (False, True, PRESET_ALPHA),
        ],
    )
    async def test_key_columns_equal_scope_columns(
        self,
        scope_db: None,
        with_group: bool,
        with_alias: bool,
        preset_id: int,
    ) -> None:
        """`AffectionKey` 只是解析结果的另一种编码，不是第二份判据

        两边一旦分叉，写进去的行自己查不到——而且不报错。这条用例是那道保险。
        """
        if with_group:
            await make_instance(INSTANCE_A)
            await join_group(INSTANCE_A, memory_group=await make_group())
        domain = await resolve_memory_domain(ADAPTER, INSTANCE_A)
        if with_alias:
            await DBIdentityAlias.create(
                domain_id=domain.domain_id,
                platform_userid=ALT_ACCOUNT,
                person_uid=MAIN_ACCOUNT,
            )

        channel = _StubChannel(GROUP_CHAT_A, preset_id=preset_id)
        scope = await resolve_identity_scope(channel, ALT_ACCOUNT if with_alias else MAIN_ACCOUNT)  # type: ignore[arg-type]
        assert AffectionKey.of_scope(scope).as_columns() == scope.affection_columns()

    async def test_key_read_back_from_a_row_equals_the_key_that_wrote_it(self, scope_db: None) -> None:
        """后台是对着一行已有数据操作的，读回来的键必须还是同一个键"""
        await make_instance(INSTANCE_A)
        await join_group(INSTANCE_A, memory_group=await make_group())
        key = await key_of(MAIN_ACCOUNT, preset_id=PRESET_ALPHA)
        await apply_delta(key, delta=7, reason="", source=AffectionEventSource.LLM)

        record = await DBAffection.get(**key.as_columns())
        assert key_of_record(record) == key
        assert key.domain_id.startswith("g:")


class TestOneRecordAcrossChatTypes:
    """隔离键不含会话：同一个人在群里和私聊里是同一份好感"""

    async def test_group_and_private_share_one_record(self, scope_db: None) -> None:
        group_key = await key_of(MAIN_ACCOUNT, chat_key=GROUP_CHAT_A)
        private_key = await key_of(MAIN_ACCOUNT, chat_key=PRIVATE_CHAT_A, channel_type="private")
        assert group_key == private_key

        await apply_delta(group_key, delta=6, reason="群里聊得不错", source=AffectionEventSource.LLM, chat_key=GROUP_CHAT_A)
        await apply_delta(
            private_key,
            delta=4,
            reason="私聊也聊得不错",
            source=AffectionEventSource.LLM,
            chat_key=PRIVATE_CHAT_A,
        )

        # 一份数据、一行记录：分开存会让私聊聊很久的人一进群就变回陌生人
        assert await DBAffection.all().count() == 1
        assert await scores_of(group_key) == 10
        record = await DBAffection.get(**group_key.as_columns())
        # `last_chat_key` 只是"最近在哪儿聊的"，不参与隔离
        assert record.last_chat_key == PRIVATE_CHAT_A

    async def test_merging_chat_types_only_holds_within_one_preset(self, scope_db: None) -> None:
        """「群聊私聊合并记一份」只在同一人设下成立

        群 A 绑人设甲、私聊绑人设乙时那就是两份——这不是数据丢了，是"换了个人设就不会
        记得以前的恩怨情仇"的直接结果。
        """
        alpha_group = await key_of(MAIN_ACCOUNT, chat_key=GROUP_CHAT_A, preset_id=PRESET_ALPHA)
        alpha_private = await key_of(
            MAIN_ACCOUNT,
            chat_key=PRIVATE_CHAT_A,
            preset_id=PRESET_ALPHA,
            channel_type="private",
        )
        beta_private = await key_of(
            MAIN_ACCOUNT,
            chat_key=PRIVATE_CHAT_A,
            preset_id=PRESET_BETA,
            channel_type="private",
        )

        assert alpha_group == alpha_private
        assert alpha_group != beta_private


class TestPresetDimension:
    """人设就是不同的人：同一个人在两个人设下是两份好感"""

    async def test_two_presets_are_two_records(self, scope_db: None) -> None:
        alpha = await key_of(MAIN_ACCOUNT, preset_id=PRESET_ALPHA)
        beta = await key_of(MAIN_ACCOUNT, preset_id=PRESET_BETA)

        await apply_delta(alpha, delta=80, reason="跟甲很熟了", source=AffectionEventSource.LLM)

        assert await scores_of(alpha) == 80
        # 运营在群里把人设从甲换成乙的那一刻，该群所有人的好感"当场归零"——
        # 实为切到另一份，旧的那份仍在、换回来就回来。这条是那个语义的数据层落点。
        assert await scores_of(beta) == 0
        assert (await load_snapshot(beta, ttl_seconds=0)).exists is False

        await apply_delta(beta, delta=3, reason="刚认识乙", source=AffectionEventSource.LLM)
        assert await scores_of(alpha) == 80
        assert await scores_of(beta) == 3
        assert await DBAffection.all().count() == 2

    async def test_switching_back_restores_the_old_record(self, scope_db: None) -> None:
        """换回来就回来：旧的那一份原地不动，一个字节都没丢"""
        alpha = await key_of(MAIN_ACCOUNT, preset_id=PRESET_ALPHA)
        await apply_delta(alpha, delta=64, reason="", source=AffectionEventSource.LLM, state_text="老朋友了")

        beta = await key_of(MAIN_ACCOUNT, preset_id=PRESET_BETA)
        await apply_delta(beta, delta=1, reason="", source=AffectionEventSource.LLM)

        snapshot = await load_snapshot(await key_of(MAIN_ACCOUNT, preset_id=PRESET_ALPHA), ttl_seconds=0)
        assert (snapshot.score, snapshot.state_text) == (64, "老朋友了")

    async def test_orphan_record_survives_a_deleted_preset(self, scope_db: None) -> None:
        """人设被删掉之后挂在它下面的数据保留成孤儿，不级联删（人设可能是误删）"""
        orphan = await key_of(MAIN_ACCOUNT, preset_id=PRESET_ALPHA)
        await apply_delta(orphan, delta=42, reason="", source=AffectionEventSource.LLM)

        # 人设行没了，新对话按回退链落到另一个人设（这里是"无人设档"）
        fallback = await key_of(MAIN_ACCOUNT, preset_id=PRESET_NONE)
        await apply_delta(fallback, delta=1, reason="", source=AffectionEventSource.LLM)

        assert await scores_of(orphan) == 42
        assert await DBAffection.filter(preset_id=PRESET_ALPHA).count() == 1


class TestAffectionDomainFollowsMemoryDomain:
    """好感域默认跟随记忆域；覆盖字段存在但前端不暴露"""

    async def test_without_override_the_two_domains_are_identical(self, scope_db: None) -> None:
        await make_instance(INSTANCE_A)
        group = await make_group()
        await join_group(INSTANCE_A, memory_group=group)

        channel = _StubChannel(GROUP_CHAT_A)
        scope = await resolve_identity_scope(channel, MAIN_ACCOUNT)  # type: ignore[arg-type]
        assert scope.affection_domain == scope.domain
        assert AffectionKey.of_scope(scope).domain_id == f"g:{group.id}"

    async def test_zero_config_is_todays_shape(self, scope_db: None) -> None:
        """不配置域时键退化成今天的三元组：两个新列取默认值，其余三列逐字不变"""
        key = await key_of(MAIN_ACCOUNT)
        assert key.as_columns() == {
            "adapter_key": ADAPTER,
            "instance_key": INSTANCE_A,
            "domain_group_id": 0,
            "platform_userid": MAIN_ACCOUNT,
            "preset_id": 0,
        }

    async def test_instances_in_one_domain_share_one_record(self, scope_db: None) -> None:
        """互通 = 放进同一个域：两个实例下的同一个人是同一份好感"""
        await make_instance(INSTANCE_A)
        await make_instance(INSTANCE_B)
        group = await make_group()
        await join_group(INSTANCE_A, memory_group=group)
        await join_group(INSTANCE_B, memory_group=group)

        key_a = await key_of(MAIN_ACCOUNT, chat_key=GROUP_CHAT_A, instance_key=INSTANCE_A)
        key_b = await key_of(MAIN_ACCOUNT, chat_key=GROUP_CHAT_B, instance_key=INSTANCE_B)
        assert key_a == key_b

        await apply_delta(key_a, delta=30, reason="在 A 实例聊的", source=AffectionEventSource.LLM)
        assert await scores_of(key_b) == 30
        # 域是组时那两列必须是空串，否则这一行谁也读不到
        record = await DBAffection.get(**key_a.as_columns())
        assert (record.adapter_key, record.instance_key, record.domain_group_id) == ("", "", group.id)

    async def test_affection_override_moves_only_the_affection_side(self, scope_db: None) -> None:
        """覆盖字段一填，好感换域、记忆不动——「好感互通但记忆不互通」那天不用改表"""
        await make_instance(INSTANCE_A)
        memory_group = await make_group("记忆域")
        affection_group = await make_group("好感域")
        await join_group(INSTANCE_A, memory_group=memory_group, affection_group=affection_group)

        channel = _StubChannel(GROUP_CHAT_A)
        scope = await resolve_identity_scope(channel, MAIN_ACCOUNT)  # type: ignore[arg-type]
        assert scope.domain.group_id == memory_group.id
        assert AffectionKey.of_scope(scope).domain_id == f"g:{affection_group.id}"


class TestPersonDimension:
    """人是稳定的内部标识，`platform_userid` 只是它的一个别名"""

    async def test_alias_account_writes_into_the_person_record(self, scope_db: None) -> None:
        main_key = await key_of(MAIN_ACCOUNT)
        await apply_delta(main_key, delta=20, reason="", source=AffectionEventSource.LLM)

        await DBIdentityAlias.create(
            domain_id=self_domain().domain_id,
            platform_userid=ALT_ACCOUNT,
            person_uid=MAIN_ACCOUNT,
        )
        alt_key = await key_of(ALT_ACCOUNT)

        # 换了个号还是同一个人：写进去的是同一行
        assert alt_key == main_key
        await apply_delta(alt_key, delta=5, reason="", source=AffectionEventSource.LLM, source_platform_userid=ALT_ACCOUNT)
        assert await DBAffection.all().count() == 1
        assert await scores_of(main_key) == 25

    async def test_aliases_do_not_leak_into_another_domain(self, scope_db: None) -> None:
        """别名按域隔离：一个域里声明"这两个号是同一个人"不能泄进另一个域"""
        await DBIdentityAlias.create(
            domain_id=self_domain(INSTANCE_A).domain_id,
            platform_userid=ALT_ACCOUNT,
            person_uid=MAIN_ACCOUNT,
        )
        key_here = await key_of(ALT_ACCOUNT, chat_key=GROUP_CHAT_A, instance_key=INSTANCE_A)
        key_there = await key_of(ALT_ACCOUNT, chat_key=GROUP_CHAT_B, instance_key=INSTANCE_B)

        assert key_here.person_uid == MAIN_ACCOUNT
        assert key_there.person_uid == ALT_ACCOUNT

    async def test_person_accounts_list_starts_with_the_person(self, scope_db: None) -> None:
        await DBIdentityAlias.create(
            domain_id=self_domain().domain_id,
            platform_userid=ALT_ACCOUNT,
            person_uid=MAIN_ACCOUNT,
        )
        await DBIdentityAlias.create(
            domain_id=self_domain().domain_id,
            platform_userid=THIRD_ACCOUNT,
            person_uid=MAIN_ACCOUNT,
        )
        assert await person_account_ids(await key_of(MAIN_ACCOUNT)) == [MAIN_ACCOUNT, ALT_ACCOUNT, THIRD_ACCOUNT]


async def _seed_two_accounts(*, main_score: int, alt_score: int, preset_id: int = PRESET_NONE) -> Tuple[AffectionKey, AffectionKey]:
    """两个还没合并的账号，各自有一份好感"""
    main_key = await key_of(MAIN_ACCOUNT, preset_id=preset_id)
    alt_key = await key_of(ALT_ACCOUNT, preset_id=preset_id)
    if main_score:
        await apply_delta(main_key, delta=main_score, reason="主号攒的", source=AffectionEventSource.LLM)
    if alt_score:
        await apply_delta(alt_key, delta=alt_score, reason="旧号攒的", source=AffectionEventSource.LLM)
    return main_key, alt_key


class TestAccountMergeTakesTheHigherScore:
    """合并语义：好感取高的"""

    @pytest.mark.parametrize(
        ("main_score", "alt_score", "expected_person_score", "expected_transfer"),
        [
            # 旧号分高：换号的人认为"我还是我"，降级会让他不满
            (10, 80, 80, 70),
            # 主号更高：一点都不动，也不写补偿事件
            (80, 10, 80, 0),
            (0, 0, 0, 0),
            # 恰好相等同样不写事件——差额是 0
            (30, 30, 30, 0),
            # 旧号是负分：取高的意味着**不会**把主号拖下水
            (5, -40, 5, 0),
        ],
    )
    async def test_merge_lifts_the_person_to_the_higher_score(
        self,
        scope_db: None,
        main_score: int,
        alt_score: int,
        expected_person_score: int,
        expected_transfer: int,
    ) -> None:
        main_key, alt_key = await _seed_two_accounts(main_score=main_score, alt_score=alt_score)

        result = await merge_accounts(
            self_domain(),
            person_uid=MAIN_ACCOUNT,
            alias_platform_userid=ALT_ACCOUNT,
            operator="admin",
        )

        assert result.total_delta == expected_transfer
        assert await scores_of(await key_of(MAIN_ACCOUNT)) == expected_person_score
        # 副账号那一行**一个字节都没动**——合并是别名指向，不是把数据行并到一起
        assert await scores_of(alt_key) == alt_score
        # 合并之后副号解析成同一个人，两个键相等
        assert await key_of(ALT_ACCOUNT) == main_key

    async def test_merge_is_per_preset(self, scope_db: None) -> None:
        """好感按人设隔离，因此合并也逐人设进行——不能把人设甲的分数并进人设乙"""
        await _seed_two_accounts(main_score=60, alt_score=50, preset_id=PRESET_ALPHA)
        alt_beta = await key_of(ALT_ACCOUNT, preset_id=PRESET_BETA)
        await apply_delta(alt_beta, delta=5, reason="旧号在乙那边说过话", source=AffectionEventSource.LLM)

        result = await merge_accounts(self_domain(), person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)

        # 甲：主号 60 > 旧号 50，不动；乙：主号本来没有这一份，补到旧号的 5
        assert result.transfers == [PresetTransfer(preset_id=PRESET_BETA, delta=5, score_after=5)]
        assert await scores_of(await key_of(MAIN_ACCOUNT, preset_id=PRESET_ALPHA)) == 60
        assert await scores_of(await key_of(MAIN_ACCOUNT, preset_id=PRESET_BETA)) == 5

    async def test_merge_is_idempotent(self, scope_db: None) -> None:
        await _seed_two_accounts(main_score=10, alt_score=80)
        first = await merge_accounts(self_domain(), person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)
        second = await merge_accounts(self_domain(), person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)

        assert (first.total_delta, second.total_delta) == (70, 0)
        assert await scores_of(await key_of(MAIN_ACCOUNT)) == 80
        # 重复合并不写第二行别名（唯一约束之外，语义上也必须幂等）
        assert await DBIdentityAlias.filter(domain_id=self_domain().domain_id).count() == 1

    async def test_merge_does_not_touch_the_persona_narrative(self, scope_db: None) -> None:
        """补偿不是这段关系里发生的事，不能被下一轮提示词当成"最近发生了什么"念出来"""
        main_key, _ = await _seed_two_accounts(main_score=10, alt_score=80)
        await apply_delta(
            main_key,
            delta=0,
            reason="上次聊天",
            source=AffectionEventSource.LLM,
            state_text="他还挺有意思的",
            event_text="他讲了个冷笑话",
        )

        await merge_accounts(self_domain(), person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)

        record = await DBAffection.get(**main_key.as_columns())
        assert record.last_event_text == "他讲了个冷笑话"
        assert record.state_text == "他还挺有意思的"
        # 也不该让"聊了多少轮"凭空+1
        assert record.interaction_count == 2


class TestAccountMergeIsReversible:
    """删别名即撤销，撤销后两边各自的原值必须完好"""

    async def test_merge_then_unmerge_restores_both_sides(self, scope_db: None) -> None:
        main_key, alt_key = await _seed_two_accounts(main_score=10, alt_score=80)
        alt_events_before = [(event.delta, event.score_after) for event in await list_events(alt_key)]

        await merge_accounts(self_domain(), person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT, operator="admin")
        assert await scores_of(main_key) == 80

        result = await unmerge_account(self_domain(), alias_platform_userid=ALT_ACCOUNT, operator="admin")

        assert result.total_delta == -70
        # 两边各自回到自己的原值
        assert await scores_of(main_key) == 10
        assert await scores_of(alt_key) == 80
        # 副号立刻回到自己那份数据，它的流水从头到尾没被改过
        assert await key_of(ALT_ACCOUNT) == alt_key
        assert [(event.delta, event.score_after) for event in await list_events(alt_key)] == alt_events_before
        assert await DBIdentityAlias.filter(domain_id=self_domain().domain_id).count() == 0

    async def test_unmerge_keeps_what_was_earned_while_merged(self, scope_db: None) -> None:
        """撤销的是合并这个动作，不是时间：借来的还回去，自己挣的留下"""
        main_key, _ = await _seed_two_accounts(main_score=10, alt_score=80)
        await merge_accounts(self_domain(), person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)
        await apply_delta(main_key, delta=5, reason="合并期间又聊了聊", source=AffectionEventSource.LLM)
        assert await scores_of(main_key) == 85

        await unmerge_account(self_domain(), alias_platform_userid=ALT_ACCOUNT)
        assert await scores_of(main_key) == 15

    async def test_merge_unmerge_cycles_do_not_drift(self, scope_db: None) -> None:
        """来回几次不能越算越偏——补偿账目是从流水现算的，不是记在某个字段上"""
        main_key, _ = await _seed_two_accounts(main_score=10, alt_score=80)
        for _ in range(3):
            await merge_accounts(self_domain(), person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)
            assert await scores_of(main_key) == 80
            await unmerge_account(self_domain(), alias_platform_userid=ALT_ACCOUNT)
            assert await scores_of(main_key) == 10

    async def test_unmerging_one_account_leaves_the_other_merge_intact(self, scope_db: None) -> None:
        """一个人合了两个副号时，拆掉其中一个只能扣回**那一个**带来的那份

        补偿账目按来源账号分开记，不分开就会在拆第一个号的时候把第二个号带来的分数一起扣掉，
        而那个号还好好地合着——分数凭空少一截，没人查得出来为什么。
        """
        main_key = await key_of(MAIN_ACCOUNT)
        await apply_delta(main_key, delta=10, reason="", source=AffectionEventSource.LLM)
        await apply_delta(await key_of(ALT_ACCOUNT), delta=80, reason="", source=AffectionEventSource.LLM)
        await apply_delta(await key_of(THIRD_ACCOUNT), delta=90, reason="", source=AffectionEventSource.LLM)

        await merge_accounts(self_domain(), person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)
        await merge_accounts(self_domain(), person_uid=MAIN_ACCOUNT, alias_platform_userid=THIRD_ACCOUNT)
        assert await scores_of(main_key) == 90

        result = await unmerge_account(self_domain(), alias_platform_userid=ALT_ACCOUNT)

        # 旧号带来的是 10 → 80 那 70 点；第三个号带来的 10 点还留着
        assert result.total_delta == -70
        assert await scores_of(main_key) == 20
        assert (await key_of(THIRD_ACCOUNT)).person_uid == MAIN_ACCOUNT

    async def test_unmerge_without_a_merge_is_rejected(self, scope_db: None) -> None:
        with pytest.raises(ValueError, match="没有被合并过"):
            await unmerge_account(self_domain(), alias_platform_userid=ALT_ACCOUNT)


class TestAccountMergeGuards:
    """合错了代价很大，所以这些情况一律拒绝而不是猜"""

    async def test_cannot_merge_an_account_into_itself(self, scope_db: None) -> None:
        with pytest.raises(ValueError, match="不能把账号合并到它自己"):
            await merge_accounts(self_domain(), person_uid=MAIN_ACCOUNT, alias_platform_userid=MAIN_ACCOUNT)

    @pytest.mark.parametrize(("person", "alias"), [("", ALT_ACCOUNT), (MAIN_ACCOUNT, ""), ("  ", "  ")])
    async def test_empty_account_is_rejected(self, scope_db: None, person: str, alias: str) -> None:
        with pytest.raises(ValueError):
            await merge_accounts(self_domain(), person_uid=person, alias_platform_userid=alias)

    async def test_cannot_merge_into_an_account_that_is_itself_an_alias(self, scope_db: None) -> None:
        """目标自己是副号时会建出一条链，而解析器只跟一跳——链上的人当场失去自己的数据"""
        await merge_accounts(self_domain(), person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)
        with pytest.raises(ValueError, match="自己已经是"):
            await merge_accounts(self_domain(), person_uid=ALT_ACCOUNT, alias_platform_userid=THIRD_ACCOUNT)

    async def test_cannot_move_an_alias_to_another_person_without_unmerging(self, scope_db: None) -> None:
        await merge_accounts(self_domain(), person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)
        with pytest.raises(ValueError, match="已经被合并到"):
            await merge_accounts(self_domain(), person_uid=THIRD_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)

    async def test_existing_aliases_are_rewritten_to_keep_the_chain_flat(self, scope_db: None) -> None:
        """把 A 并进 C 时已有的 B→A 必须一并改写成 B→C，否则 B 就挂在一条链上"""
        await merge_accounts(self_domain(), person_uid=ALT_ACCOUNT, alias_platform_userid=THIRD_ACCOUNT)
        result = await merge_accounts(self_domain(), person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)

        assert result.rewritten_aliases == [THIRD_ACCOUNT]
        assert await list_person_accounts(self_domain(), MAIN_ACCOUNT) == [MAIN_ACCOUNT, THIRD_ACCOUNT, ALT_ACCOUNT]
        # 三个号解析成同一个人，且没有任何一条别名指向另一条别名
        assert (await key_of(THIRD_ACCOUNT)).person_uid == MAIN_ACCOUNT
        assert (await key_of(ALT_ACCOUNT)).person_uid == MAIN_ACCOUNT

    async def test_merging_in_the_wrong_domain_warns(self, scope_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
        """用错域是这套接口唯一会静默失败的用法，必须留下痕迹

        实例在实例组里、调用方却自己拼了"自成一域"，别名就建在一个谁也不会去查的位置上：
        界面显示"合并成功"，Bot 那边毫无变化。
        """
        from nekro_agent.services.affection import merge as merge_module

        warnings: List[str] = []
        monkeypatch.setattr(
            merge_module,
            "logger",
            type("_L", (), {"warning": lambda _self, m: warnings.append(m), "info": lambda *_: None, "debug": lambda *_: None})(),
        )
        await make_instance(INSTANCE_A)
        await join_group(INSTANCE_A, memory_group=await make_group())
        await apply_delta(await key_of(MAIN_ACCOUNT), delta=10, reason="", source=AffectionEventSource.LLM)

        await merge_accounts(self_domain(), person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)

        assert any("都没有任何数据" in line for line in warnings)

        # 正确的域下同样的合并不该有这条噪声
        warnings.clear()
        domain = await affection_domain_of_instance(INSTANCE_A)
        assert domain is not None
        await merge_accounts(domain, person_uid=MAIN_ACCOUNT, alias_platform_userid=THIRD_ACCOUNT)
        assert warnings == []

        # 换号最常见的形态就是"旧号有全部历史、新号一片空白"——把它报成用错域会天天喊狼来了，
        # 判据因此必须是"两个账号**都**没有数据"，不是"这个人没有数据"
        warnings.clear()
        alt_only = "700700"
        await apply_delta(
            AffectionKey.of_domain(domain, person_uid=alt_only, preset_id=PRESET_NONE),
            delta=30,
            reason="旧号的全部历史",
            source=AffectionEventSource.LLM,
        )
        await merge_accounts(domain, person_uid="800800", alias_platform_userid=alt_only)
        assert warnings == []

    async def test_merge_stays_inside_its_own_domain(self, scope_db: None) -> None:
        """跨域串号：在 A 的域里合并，绝不能影响 B 的域

        这是第二条高危风险面在服务层的落点——接口层的越权 404 是另一道门。
        """
        await merge_accounts(self_domain(INSTANCE_A), person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)

        key_b = await key_of(ALT_ACCOUNT, chat_key=GROUP_CHAT_B, instance_key=INSTANCE_B)
        assert key_b.person_uid == ALT_ACCOUNT
        assert await DBIdentityAlias.filter(domain_id=self_domain(INSTANCE_B).domain_id).count() == 0


class TestMergedEventStream:
    """流水取并集、按时间排序、保留来源账号"""

    async def test_events_are_unioned_and_time_ordered(self, scope_db: None) -> None:
        main_key = await key_of(MAIN_ACCOUNT)
        alt_key = await key_of(ALT_ACCOUNT)
        await apply_delta(alt_key, delta=3, reason="旧号第一次说话", source=AffectionEventSource.LLM)
        await apply_delta(main_key, delta=2, reason="新号第一次说话", source=AffectionEventSource.LLM)

        await merge_accounts(self_domain(), person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)
        events = await list_events(main_key)

        reasons = [event.reason for event in events]
        assert "旧号第一次说话" in reasons, "合并之后旧号的流水必须能在这个人名下看到"
        # 从新到旧：补偿事件最新，两条历史按发生顺序在后
        assert reasons.index("新号第一次说话") < reasons.index("旧号第一次说话")
        # 来源账号逐条保留：空串表示"与人相同"，见 models/db_affection.py
        by_reason = {event.reason: event for event in events}
        assert by_reason["旧号第一次说话"].platform_userid == ALT_ACCOUNT
        assert (by_reason["旧号第一次说话"].source_platform_userid or ALT_ACCOUNT) == ALT_ACCOUNT
        assert by_reason["新号第一次说话"].source_platform_userid == ""

    async def test_timeline_is_ordered_by_time_not_by_insertion(self, scope_db: None) -> None:
        """按 `create_time` 排而不是按 id 排

        单进程下两者恰好一致，所以这条用例得**造出**它们不一致的样子：一条带旧时间戳的行
        （历史数据导入、或多节点写入时的时钟差）。按 id 排会把它显示在最上面，
        而它明明是更早发生的事。
        """
        main_key = await key_of(MAIN_ACCOUNT)
        alt_key = await key_of(ALT_ACCOUNT)
        await apply_delta(main_key, delta=2, reason="新号说的话", source=AffectionEventSource.LLM)
        await apply_delta(alt_key, delta=3, reason="旧号很久以前说的话", source=AffectionEventSource.LLM)
        stale = await DBAffectionEvent.get(platform_userid=ALT_ACCOUNT)
        await DBAffectionEvent.filter(id=stale.id).update(create_time=stale.create_time - timedelta(days=30))

        await merge_accounts(self_domain(), person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)

        reasons = [event.reason for event in await list_events(main_key)]
        assert reasons.index("新号说的话") < reasons.index("旧号很久以前说的话")

    async def test_new_events_from_the_alias_account_record_their_source(self, scope_db: None) -> None:
        await merge_accounts(self_domain(), person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)
        key = await key_of(ALT_ACCOUNT)
        await apply_delta(
            key,
            delta=2,
            reason="用旧号说的话",
            source=AffectionEventSource.LLM,
            source_platform_userid=ALT_ACCOUNT,
        )

        event = (await list_events(key))[0]
        assert (event.platform_userid, event.source_platform_userid) == (MAIN_ACCOUNT, ALT_ACCOUNT)

    async def test_unmerge_splits_the_stream_back(self, scope_db: None) -> None:
        main_key = await key_of(MAIN_ACCOUNT)
        alt_key = await key_of(ALT_ACCOUNT)
        await apply_delta(alt_key, delta=3, reason="旧号第一次说话", source=AffectionEventSource.LLM)
        await apply_delta(main_key, delta=2, reason="新号第一次说话", source=AffectionEventSource.LLM)
        await merge_accounts(self_domain(), person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)

        await unmerge_account(self_domain(), alias_platform_userid=ALT_ACCOUNT)

        assert [event.reason for event in await list_events(alt_key)] == ["旧号第一次说话"]
        assert "旧号第一次说话" not in [event.reason for event in await list_events(main_key)]


class TestRateBudgetFollowsThePerson:
    """额度按人算：换个号接着刷正是这条额度要挡的事"""

    async def test_window_usage_unions_all_accounts_of_the_person(self, scope_db: None) -> None:
        main_key = await key_of(MAIN_ACCOUNT)
        alt_key = await key_of(ALT_ACCOUNT)
        await apply_delta(main_key, delta=6, reason="", source=AffectionEventSource.LLM)
        await apply_delta(alt_key, delta=9, reason="", source=AffectionEventSource.LLM)
        assert (await window_usage(main_key)).daily_gain == 6

        await merge_accounts(self_domain(), person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)

        usage = await window_usage(await key_of(ALT_ACCOUNT))
        # 6 + 9；合并的补偿事件不是 `llm`，不占额度
        assert usage.daily_gain == 15
        limits = AffectionRateLimits(daily_gain=15, daily_drop=15, weekly_gain=100, diminishing=False)
        assert await throttle_llm_delta(main_key, delta=5, current_score=15, limits=limits) == 0

    async def test_budget_is_still_isolated_per_preset(self, scope_db: None) -> None:
        alpha = await key_of(MAIN_ACCOUNT, preset_id=PRESET_ALPHA)
        beta = await key_of(MAIN_ACCOUNT, preset_id=PRESET_BETA)
        await apply_delta(alpha, delta=9, reason="", source=AffectionEventSource.LLM)

        assert (await window_usage(alpha)).daily_gain == 9
        assert (await window_usage(beta)).daily_gain == 0


class TestPromptAndEvaluatorSeeThePerson:
    """提示词与评估都不再假设「一个用户一份」"""

    async def test_prompt_renders_one_block_per_person(self, scope_db: None) -> None:
        """同一个人的两个号同时在群里说话时只能有一个块

        不去重就会渲染出两个内容完全一样、`user_id` 却不同的块，模型会把它读成
        "两个感情一模一样的人"。
        """
        await merge_accounts(self_domain(), person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)
        await apply_delta(
            await key_of(MAIN_ACCOUNT),
            delta=30,
            reason="",
            source=AffectionEventSource.LLM,
            state_text="他挺照顾我的",
        )
        await add_message(GROUP_CHAT_A, MAIN_ACCOUNT, "我在", timestamp=4_000_000_001)
        await add_message(GROUP_CHAT_A, ALT_ACCOUNT, "我也在", timestamp=4_000_000_002)

        rendered = await render_affection_prompt(
            db_chat_channel=_StubChannel(GROUP_CHAT_A),  # type: ignore[arg-type]
            one_time_code=ONE_TIME_CODE,
            config=make_config(),
        )
        assert rendered.count("他挺照顾我的") == 1

    async def test_prompt_reads_the_domain_record(self, scope_db: None) -> None:
        """实例进组之后提示词照样读得到——键换了，读的位置也要跟着换"""
        await make_instance(INSTANCE_A)
        await join_group(INSTANCE_A, memory_group=await make_group())
        await apply_delta(
            await key_of(MAIN_ACCOUNT),
            delta=30,
            reason="",
            source=AffectionEventSource.LLM,
            state_text="组里的老朋友",
        )
        await add_message(GROUP_CHAT_A, MAIN_ACCOUNT, "早")

        rendered = await render_affection_prompt(
            db_chat_channel=_StubChannel(GROUP_CHAT_A),  # type: ignore[arg-type]
            one_time_code=ONE_TIME_CODE,
            config=make_config(),
        )
        assert "组里的老朋友" in rendered

    async def test_threshold_helper_reads_the_person_in_this_channel(self, scope_db: None) -> None:
        """门槛判定的那个读接口同样按 `(域, 人, 人设)` 取数

        它以前的入参是 `(adapter_key, instance_key, platform_userid)`——那三个值算不出
        实例进组之后的域，也认不出换过的号，结果会稳定地读到 0 分：插件表现为
        "这个人明明很熟，功能却一直说不够亲近"。
        """
        await make_instance(INSTANCE_A)
        await join_group(INSTANCE_A, memory_group=await make_group())
        # 域必须解析出来：实例在组里时 `ScopeDomain.of_instance(...)` 是**另一个域**，
        # 别名会建在一个谁也不会去查的位置上（这条陷阱由 `test_merging_in_the_wrong_domain_warns` 钉住）
        domain = await affection_domain_of_instance(INSTANCE_A)
        assert domain is not None
        await merge_accounts(domain, person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)
        await apply_delta(await key_of(MAIN_ACCOUNT), delta=45, reason="", source=AffectionEventSource.LLM)

        cfg = make_config()
        channel = _StubChannel(GROUP_CHAT_A)
        assert await affection_score_of(channel, MAIN_ACCOUNT, cfg) == 45  # type: ignore[arg-type]
        # 换了个号还是同一个人
        assert await affection_score_of(channel, ALT_ACCOUNT, cfg) == 45  # type: ignore[arg-type]
        assert await affection_score_of(channel, THIRD_ACCOUNT, cfg) == 0  # type: ignore[arg-type]

    async def test_threshold_helper_falls_open_to_zero(self, scope_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
        """好感度是风味不是功能：读不到按 0 处理，插件不会因此拒绝服务"""

        async def _boom(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("库挂了")

        monkeypatch.setattr(prompt_module, "resolve_identity_scope", _boom)
        assert await affection_score_of(_StubChannel(GROUP_CHAT_A), MAIN_ACCOUNT, make_config()) == 0  # type: ignore[arg-type]

    async def test_evaluation_of_two_accounts_lands_on_one_record(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """两个号一轮里各被评了一次，落在同一行上，且第二次按**写过之后**的分数打折

        沿用快照里的旧分数会让高分段被当成低分段算递减收益，涨幅当场翻几倍。
        """
        await merge_accounts(self_domain(), person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)
        key = await key_of(MAIN_ACCOUNT)
        # 58 分是**故意**挑的：它落在"熟络"档（60%），第一次评估之后刚好跨进"亲近"档（40%）。
        # 沿用旧分数与用新分数会给出两个不同的结果（62 / 61），这条用例才分得出来。
        await apply_delta(key, delta=58, reason="", source=AffectionEventSource.MANUAL, operator="admin")
        await add_message(GROUP_CHAT_A, MAIN_ACCOUNT, "主号说话", timestamp=4_000_000_001)
        await add_message(GROUP_CHAT_A, ALT_ACCOUNT, "旧号说话", timestamp=4_000_000_002)
        stub_llm(
            monkeypatch,
            {
                "users": [
                    {"user_id": MAIN_ACCOUNT, "delta": 4, "reason": "主号很热情", "feeling": "老朋友"},
                    {"user_id": ALT_ACCOUNT, "delta": 4, "reason": "旧号也很热情", "feeling": "还是老朋友"},
                ],
            },
        )

        await evaluate_channel(_StubChannel(GROUP_CHAT_A), make_config())  # type: ignore[arg-type]

        # 58 在"熟络"段保留 60%（4 → 2）→ 60；第二条按 60 算，落在"亲近"段 40%（4 → 1）→ 61。
        # 沿用快照里的 58 会算出 62 —— 高分段被当成低分段，涨幅当场多一截。
        assert await DBAffection.all().count() == 1
        assert await scores_of(key) == 61
        # 两条都记了来源账号，其中旧号那条明确标出来
        sources = await DBAffectionEvent.filter(source=AffectionEventSource.LLM.value).values_list(
            "source_platform_userid",
            flat=True,
        )
        assert ALT_ACCOUNT in [str(item) for item in sources]


class TestRouterScopeFollowsTheDomain:
    """后台作用域：实例进组之后必须还查得到（进组前后都不能空）"""

    async def _scoped(self, scope: str) -> List[DBAffection]:
        from nekro_agent.routers.affection import _scoped_query

        return await (await _scoped_query(scope))

    async def test_scope_of_a_grouped_instance_finds_the_domain_rows(self, scope_db: None) -> None:
        """这条是修复：按 `instance_key` 过滤在实例进组后一行都查不到（fail-closed）

        表现是"配了域之后后台好感度整个空了"，而数据其实好好地在库里。
        """
        await make_instance(INSTANCE_A)
        await join_group(INSTANCE_A, memory_group=await make_group())
        await apply_delta(await key_of(MAIN_ACCOUNT), delta=10, reason="", source=AffectionEventSource.LLM)

        records = await self._scoped(INSTANCE_A)
        assert [record.platform_userid for record in records] == [MAIN_ACCOUNT]

    async def test_scope_of_an_ungrouped_instance_is_unchanged(self, scope_db: None) -> None:
        """零配置的部署逐字不变：仍然只看得到自己实例那一份"""
        await make_instance(INSTANCE_A)
        await make_instance(INSTANCE_B)
        await apply_delta(await key_of(MAIN_ACCOUNT, instance_key=INSTANCE_A), delta=10, reason="", source=AffectionEventSource.LLM)
        await apply_delta(
            await key_of(MAIN_ACCOUNT, chat_key=GROUP_CHAT_B, instance_key=INSTANCE_B),
            delta=20,
            reason="",
            source=AffectionEventSource.LLM,
        )

        assert [record.instance_key for record in await self._scoped(INSTANCE_A)] == [INSTANCE_A]
        assert len(await self._scoped("")) == 2

    async def test_unknown_instance_falls_back_to_todays_filter(self, scope_db: None) -> None:
        """实例行查不到（历史残留、尚未认领）时退化成按实例列过滤，也就是今天的行为"""
        await apply_delta(await key_of(MAIN_ACCOUNT), delta=10, reason="", source=AffectionEventSource.LLM)
        assert [record.instance_key for record in await self._scoped(INSTANCE_A)] == [INSTANCE_A]
        assert await self._scoped(INSTANCE_B) == []

    async def test_detail_reports_the_group_domain(self, scope_db: None) -> None:
        """详情页说的"这份好感属于哪个域"必须认得组域

        自己再写一遍"是组还是自成一域"的判据，第二份就会在下一次改判据时分叉：
        表现是一行属于某个组的好感在详情页上显示成某个实例的域，而那个实例可能是别人的。
        """
        from nekro_agent.routers.affection import _domain_of

        await make_instance(INSTANCE_A)
        group = await make_group()
        await join_group(INSTANCE_A, memory_group=group)
        key = await key_of(MAIN_ACCOUNT)
        await apply_delta(key, delta=5, reason="", source=AffectionEventSource.LLM)

        record = await DBAffection.get(**key.as_columns())
        assert _domain_of(record).domain_id == f"g:{group.id}"
        assert _domain_of(record).is_group is True

    async def test_another_domain_stays_invisible(self, scope_db: None) -> None:
        """两个实例各自成组时互相看不见——组是可见范围的边界"""
        await make_instance(INSTANCE_A)
        await make_instance(INSTANCE_B)
        await join_group(INSTANCE_A, memory_group=await make_group("A 的域"))
        await join_group(INSTANCE_B, memory_group=await make_group("B 的域"))
        await apply_delta(await key_of(MAIN_ACCOUNT), delta=10, reason="", source=AffectionEventSource.LLM)

        assert len(await self._scoped(INSTANCE_A)) == 1
        assert await self._scoped(INSTANCE_B) == []
