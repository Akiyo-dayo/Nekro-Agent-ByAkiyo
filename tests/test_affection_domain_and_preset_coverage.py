"""好感度隔离键的**覆盖补口**：每一处 DB 过滤的域维、两个热路径上的人设维

## 这个文件为什么存在

`tests/test_affection_scope_and_merge.py` 钉的是接缝的**语义**（键怎么产生、合并怎么撤销）。
对着它做变异复核时发现两族判据一条都没被钉住 —— 17 条针对"过滤条件里那一维"的变异里
**11 条存活**：

- 把 `domain_id=` 从 `store.person_account_ids` 的别名查询里删掉、把 `**key.scope_columns()`
  从 `store.list_events` 里删掉、把 `**domain.as_columns()` 从 `merge` 的四个查询里删掉，
  全套用例仍然全绿。`store.window_usage` 上的**同一条**判据有用例，与它并排的
  `list_events` 没有 —— 这正是 #45「四个消费点只测了一个」的同一个形态。
- `prompt` 与 `evaluator` 把人设写死成 `0` 也全绿：那两处的用例里频道替身的人设一律是
  "无人设档"（0），于是那是个**等价变异** —— 换句话说，热路径上的人设维一条用例都没有。

域是**授权边界**。写漏一处的后果不是"少几行"，而是一个租户读到另一个租户的私密内容
（`models/db_identity.py`「人的标识必须与域绑定」）。因此这里按"每一个消费点各一条"补齐，
判据与被测代码里的那一份逐字对应。

## 固件复用，不另起一套

频道替身、域/组固件、键工厂全部从 `test_affection_scope_and_merge` 取。
隔离与合并是数据层的性质，这里同样跑真表 —— 用假存储验证等于把要保护的东西替换掉了。
"""

import json
from pathlib import Path
from typing import Tuple

import pytest

from nekro_agent.models.db_affection import DBAffection
from nekro_agent.models.db_identity import DBIdentityAlias
from nekro_agent.services.affection.evaluator import evaluate_channel
from nekro_agent.services.affection.key import AffectionKey
from nekro_agent.services.affection.merge import PresetTransfer, merge_accounts, unmerge_account
from nekro_agent.services.affection.prompt import render_affection_prompt
from nekro_agent.services.affection.stage import AffectionEventSource
from nekro_agent.services.affection.store import (
    affection_domain_of_instance,
    apply_delta,
    list_events,
    person_account_ids,
    set_score,
)
from nekro_agent.services.identity_scope import PRESET_NONE, ScopeDomain
from tests.test_affection_scope_and_merge import (
    ALT_ACCOUNT,
    GROUP_CHAT_A,
    INSTANCE_A,
    INSTANCE_B,
    MAIN_ACCOUNT,
    ONE_TIME_CODE,
    PRESET_ALPHA,
    PRESET_BETA,
    THIRD_ACCOUNT,
    _StubChannel,
    add_message,
    join_group,
    key_of,
    make_config,
    make_group,
    make_instance,
    scope_db,  # noqa: F401  # pytest 固件靠导入生效；用 `usefixtures` 引用，不做参数（会遮蔽同名导入）
    scores_of,
    self_domain,
    stub_llm,
)


async def seed_two_domains() -> Tuple[ScopeDomain, ScopeDomain]:
    """两个互不相干的域，供"这条查询会不会串门"用

    实例 A 被放进一个实例组（域是组），实例 B 自成一域 —— 刻意取两种不同的域形态，
    只用两个自成一域的实例会漏掉"组域的三列长什么样"这一半。
    """
    await make_instance(INSTANCE_A)
    await make_instance(INSTANCE_B)
    await join_group(INSTANCE_A, memory_group=await make_group("甲的域"))
    domain_a = await affection_domain_of_instance(INSTANCE_A)
    domain_b = await affection_domain_of_instance(INSTANCE_B)
    assert domain_a is not None and domain_b is not None
    assert domain_a != domain_b
    return domain_a, domain_b


@pytest.mark.usefixtures("scope_db")
class TestEveryQueryCarriesItsDomain:
    """域这一维必须出现在**每一个**消费点的过滤条件里"""

    async def test_person_accounts_do_not_cross_domains(self) -> None:
        """`person_account_ids` 漏掉域 = 另一个域里的合并关系在这里当场生效

        表现：B 域那个同号用户的流水与刷分额度被算进 A 域这个人名下。
        """
        domain_a, domain_b = await seed_two_domains()
        await DBIdentityAlias.create(
            domain_id=domain_b.domain_id,
            platform_userid=ALT_ACCOUNT,
            person_uid=MAIN_ACCOUNT,
        )
        key_in_a = AffectionKey.of_domain(domain_a, person_uid=MAIN_ACCOUNT, preset_id=PRESET_NONE)
        assert await person_account_ids(key_in_a) == [MAIN_ACCOUNT]

    async def test_event_timeline_stays_inside_its_domain_and_preset(self) -> None:
        """`list_events` 漏掉 `scope_columns()` = 详情页把别的域、别的人设的流水一并列出来

        `window_usage` 上的同一条判据本来就有用例，这一条补的是与它并排的那个出口。
        """
        domain_a, domain_b = await seed_two_domains()
        mine = AffectionKey.of_domain(domain_a, person_uid=MAIN_ACCOUNT, preset_id=PRESET_ALPHA)
        other_domain = AffectionKey.of_domain(domain_b, person_uid=MAIN_ACCOUNT, preset_id=PRESET_ALPHA)
        other_preset = AffectionKey.of_domain(domain_a, person_uid=MAIN_ACCOUNT, preset_id=PRESET_BETA)
        await apply_delta(mine, delta=1, reason="本域本人设", source=AffectionEventSource.LLM)
        await apply_delta(other_domain, delta=2, reason="另一个域", source=AffectionEventSource.LLM)
        await apply_delta(other_preset, delta=3, reason="另一个人设", source=AffectionEventSource.LLM)

        assert [event.reason for event in await list_events(mine)] == ["本域本人设"]

    async def test_merge_only_reads_and_writes_its_own_domain(self) -> None:
        """合并的三个查询（副号有哪些行、主号各人设的分数、域告警）都必须带域

        漏掉任何一个，一次在 A 域里的合并就会照着 B 域的分数补偿。
        这里把两个域的高低**故意反着**摆：读错域的实现会补到 90 而不是 20。
        """
        domain_a, domain_b = await seed_two_domains()
        await apply_delta(
            AffectionKey.of_domain(domain_b, person_uid=ALT_ACCOUNT, preset_id=PRESET_NONE),
            delta=90,
            reason="",
            source=AffectionEventSource.LLM,
        )
        await apply_delta(
            AffectionKey.of_domain(domain_a, person_uid=ALT_ACCOUNT, preset_id=PRESET_NONE),
            delta=20,
            reason="",
            source=AffectionEventSource.LLM,
        )
        await apply_delta(
            AffectionKey.of_domain(domain_a, person_uid=MAIN_ACCOUNT, preset_id=PRESET_NONE),
            delta=5,
            reason="",
            source=AffectionEventSource.LLM,
        )

        result = await merge_accounts(domain_a, person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)
        assert result.transfers == [PresetTransfer(preset_id=PRESET_NONE, delta=15, score_after=20)]
        assert await scores_of(AffectionKey.of_domain(domain_a, person_uid=MAIN_ACCOUNT, preset_id=PRESET_NONE)) == 20
        # B 域一个字节都没动，别名也只建在 A 域
        assert await scores_of(AffectionKey.of_domain(domain_b, person_uid=ALT_ACCOUNT, preset_id=PRESET_NONE)) == 90
        assert await DBIdentityAlias.filter(domain_id=domain_b.domain_id).count() == 0

    async def test_unmerge_does_not_claw_back_another_domains_compensation(self) -> None:
        """撤销账目漏掉域 = 把另一个域里同一对账号的补偿也扣掉一遍

        同一对账号在两个域里各合并过一次是很正常的（同一个人在两个实例上都换了号）。
        """
        domain_a, domain_b = await seed_two_domains()
        for domain, alias_score in ((domain_a, 40), (domain_b, 70)):
            await apply_delta(
                AffectionKey.of_domain(domain, person_uid=ALT_ACCOUNT, preset_id=PRESET_NONE),
                delta=alias_score,
                reason="",
                source=AffectionEventSource.LLM,
            )
            await merge_accounts(domain, person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)

        await unmerge_account(domain_a, alias_platform_userid=ALT_ACCOUNT)
        assert await scores_of(AffectionKey.of_domain(domain_a, person_uid=MAIN_ACCOUNT, preset_id=PRESET_NONE)) == 0
        assert await scores_of(AffectionKey.of_domain(domain_b, person_uid=MAIN_ACCOUNT, preset_id=PRESET_NONE)) == 70

    async def test_backend_scope_follows_the_affection_domain(self) -> None:
        """配了好感域覆盖时，后台作用域必须跟**好感**那一侧走

        覆盖字段前端不暴露，但它就是"将来好感互通、记忆不互通"的那半张表；
        这里用记忆域的话，配了覆盖的部署会看到一页空白，而数据好好地在库里。
        """
        await make_instance(INSTANCE_A)
        memory_group = await make_group("记忆域")
        affection_group = await make_group("好感域")
        await join_group(INSTANCE_A, memory_group=memory_group, affection_group=affection_group)

        domain = await affection_domain_of_instance(INSTANCE_A)
        assert domain is not None
        assert domain.group_id == affection_group.id


@pytest.mark.usefixtures("scope_db")
class TestPresetReachesTheHotPathConsumers:
    """人设这一维在提示词与评估这两个热路径上也要真的走到

    其余用例里频道替身的人设一律是"无人设档"（0），因此"把这两处的人设写死成 0"
    在那些用例下是个等价变异 —— 这两条是唯一会红的。
    """

    async def test_prompt_reads_the_record_of_the_current_preset(self) -> None:
        channel = _StubChannel(GROUP_CHAT_A, preset_id=PRESET_ALPHA)
        await apply_delta(
            await key_of(MAIN_ACCOUNT, preset_id=PRESET_ALPHA),
            delta=80,
            reason="",
            source=AffectionEventSource.LLM,
            state_text="人设甲下的印象",
        )
        await apply_delta(
            await key_of(MAIN_ACCOUNT, preset_id=PRESET_NONE),
            delta=5,
            reason="",
            source=AffectionEventSource.LLM,
            state_text="无人设档下的印象",
        )
        await add_message(GROUP_CHAT_A, MAIN_ACCOUNT, "在吗")

        text = await render_affection_prompt(
            db_chat_channel=channel,  # type: ignore[arg-type]
            one_time_code=ONE_TIME_CODE,
            config=make_config(),
        )
        assert "人设甲下的印象" in text
        assert "无人设档下的印象" not in text

    async def test_evaluation_writes_into_the_current_preset(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        channel = _StubChannel(GROUP_CHAT_A, preset_id=PRESET_ALPHA)
        await add_message(GROUP_CHAT_A, MAIN_ACCOUNT, "今天谢谢你")
        stub_llm(monkeypatch, {"users": [{"user_id": MAIN_ACCOUNT, "delta": 3, "reason": "道谢", "feeling": "很暖"}]})

        assert await evaluate_channel(channel, make_config()) == 1  # type: ignore[arg-type]
        assert await scores_of(await key_of(MAIN_ACCOUNT, preset_id=PRESET_ALPHA)) == 3
        assert await scores_of(await key_of(MAIN_ACCOUNT, preset_id=PRESET_NONE)) == 0


@pytest.mark.usefixtures("scope_db")
class TestBackendSaysWhichRowIsStillInUse:
    """后台那一行到底是不是"还在用的那一份"，界面上必须说得出来"""

    async def test_dormant_alias_row_is_marked_and_scoped_to_its_domain(self) -> None:
        """`merged_into` 少了域这一维 = 把另一个域的合并关系贴到本页的行上

        平台方的全局视图里一页确实会同时出现好几个域的行，所以这一维不是理论风险。
        """
        from nekro_agent.routers.affection import _merged_into_map

        domain_a, domain_b = await seed_two_domains()
        for domain in (domain_a, domain_b):
            await apply_delta(
                AffectionKey.of_domain(domain, person_uid=ALT_ACCOUNT, preset_id=PRESET_NONE),
                delta=10,
                reason="",
                source=AffectionEventSource.LLM,
            )
        await merge_accounts(domain_a, person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)

        rows = await DBAffection.filter(platform_userid=ALT_ACCOUNT).order_by("id")
        merged = await _merged_into_map(rows)
        assert merged.get((domain_a.domain_id, ALT_ACCOUNT)) == MAIN_ACCOUNT
        assert (domain_b.domain_id, ALT_ACCOUNT) not in merged

    async def test_detail_lists_only_this_domains_alias_accounts(self) -> None:
        """详情页的「已并入这个人的账号」同样按域收口 —— 别的域那条是另一个租户的合并关系"""
        from nekro_agent.routers.affection import _scope_info

        domain_a, domain_b = await seed_two_domains()
        await DBIdentityAlias.create(
            domain_id=domain_a.domain_id,
            platform_userid=ALT_ACCOUNT,
            person_uid=MAIN_ACCOUNT,
        )
        await DBIdentityAlias.create(
            domain_id=domain_b.domain_id,
            platform_userid=THIRD_ACCOUNT,
            person_uid=MAIN_ACCOUNT,
        )
        key = AffectionKey.of_domain(domain_a, person_uid=MAIN_ACCOUNT, preset_id=PRESET_NONE)
        await apply_delta(key, delta=1, reason="", source=AffectionEventSource.LLM)

        record = await DBAffection.get(**key.as_columns())
        scope = await _scope_info(record)
        assert scope.alias_accounts == [ALT_ACCOUNT]
        assert scope.person_uid == MAIN_ACCOUNT

    async def test_timeline_restores_the_source_account_of_ordinary_events(self) -> None:
        """库里空串表示"与本行的人相同"，展示层必须还原成具体账号

        不还原的话，没合并过的人每一条流水的来源账号都是空的，前端就得自己再写一遍
        这条回落规则 —— 而它已经在后端写过一次了。
        """
        from nekro_agent.routers.affection import _to_event

        key = await key_of(MAIN_ACCOUNT)
        await apply_delta(key, delta=1, reason="自己说的话", source=AffectionEventSource.LLM)
        event = (await list_events(key))[0]
        assert event.source_platform_userid == ""
        assert _to_event(event).source_platform_userid == MAIN_ACCOUNT


@pytest.mark.usefixtures("scope_db")
class TestEventSourceIsRenderable:
    """后端加一种事件来源，前端就得有一句话能说

    `merge` / `unmerge` 这两个取值加上去的时候两个语言包都没跟着改，于是后台时间线上
    那两条会显示成 `detail.source.merge` 这样的 i18n 键名 —— 而它们恰好出现在
    "他的分数怎么突然涨了 50"最需要解释的那一行上。
    """

    @pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
    def test_every_source_value_has_a_label(self, locale: str) -> None:
        path = Path(__file__).resolve().parents[1] / "frontend" / "src" / "locales" / locale / "affection.json"
        labels = json.loads(path.read_text(encoding="utf-8"))["detail"]["source"]
        missing = [item.value for item in AffectionEventSource if item.value not in labels]
        assert not missing, f"{locale}/affection.json 缺少事件来源文案: {missing}（前端会把键名原样显示出来）"


@pytest.mark.usefixtures("scope_db")
class TestUnmergeLeavesNoGhostInThePrompt:
    """撤销合并留下的空壳行不能变成提示词里凭空多出来的「陌生人」"""

    async def test_prompt_skips_the_shell_row_left_by_unmerge(self) -> None:
        """合并的补偿会把主号那一行建出来；撤销之后它是一行没有任何实质的空壳

        物理删行与「不搬行、可撤销」这条原则冲突，因此行留着，由读侧判"有没有实质"。
        """
        channel = _StubChannel(GROUP_CHAT_A)
        await apply_delta(
            await key_of(ALT_ACCOUNT),
            delta=40,
            reason="旧号的历史",
            source=AffectionEventSource.LLM,
            nickname="老号",
        )
        await merge_accounts(self_domain(), person_uid=MAIN_ACCOUNT, alias_platform_userid=ALT_ACCOUNT)
        await unmerge_account(self_domain(), alias_platform_userid=ALT_ACCOUNT)
        await add_message(GROUP_CHAT_A, MAIN_ACCOUNT, "在吗")

        assert await DBAffection.filter(**(await key_of(MAIN_ACCOUNT)).as_columns()).exists()
        text = await render_affection_prompt(
            db_chat_channel=channel,  # type: ignore[arg-type]
            one_time_code=ONE_TIME_CODE,
            config=make_config(),
        )
        assert text == ""

    async def test_a_reset_person_still_shows_up(self) -> None:
        """判据看的是"有没有实质"而不是"分数是不是 0"：重置过的人打过交道，仍然要注入"""
        channel = _StubChannel(GROUP_CHAT_A)
        key = await key_of(MAIN_ACCOUNT)
        await apply_delta(key, delta=20, reason="", source=AffectionEventSource.LLM, nickname="小明")
        await set_score(key, score=0, reason="重置", source=AffectionEventSource.RESET, clear_state_text=True)
        await add_message(GROUP_CHAT_A, MAIN_ACCOUNT, "在吗")

        text = await render_affection_prompt(
            db_chat_channel=channel,  # type: ignore[arg-type]
            one_time_code=ONE_TIME_CODE,
            config=make_config(),
        )
        assert MAIN_ACCOUNT in text
