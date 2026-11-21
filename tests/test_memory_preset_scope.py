"""记忆的隔离键接上 `(域, 人设)` 之后的契约。

## 这份测试要钉死什么

1. **零配置 = 改造前，逐字。** 没有实例组、频道没绑人设时，可见范围必须退化成
   `memory.instance_scope.resolve_allowed_instance_keys` 的原样输出 + 人设 0，
   而库里所有存量行的 `preset_id` 都是 0（列默认值），于是一行都不会被筛掉。
   这是整套改造的前提：不配置就什么都没变。
2. **人设隔离真的成立，而且四条检索通道一条都不漏。** 段落（向量）、实体/关系（图谱）、
   Episode（叙事）都会直接进提示词，只堵一条等于没堵——#45 就是"四个消费点只测了一个"。
3. **写入侧标人设、但不产生副本。** 一段对话在人设甲下发生就写一次、标成甲
   （按人设隔离之所以不增加存储成本，靠的正是这一条）。
4. **向量侧的"字段缺失"这一档。** 改造前写入的 point 的 payload 里没有 `preset_id`，
   Qdrant 的 `must` 对缺字段的 point 判为不匹配——照直写等值条件的话，升级那一刻
   **全部存量向量集体落选**。零配置逐字相同在向量侧就断在这里。
5. **每一个消费点都带着作用域。** 用 AST 扫检索侧的每一次 `mem_*` 查询，逐个要求它
   展开了作用域过滤。写死一份"消费点清单"挡不住新增的第五个消费点，扫源码可以。

## 为什么用真表而不是假存储

隔离性是**数据层**的性质。用假存储验证等于把要保护的东西替换掉了
（与 `tests/test_identity_scope_foundation.py` 同源的判据）。向量通道那一层用替身，
而且替身**故意不做任何过滤**：这样只要 SQL 侧漏了条件，别的人设的段落会真的被捞出来。
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_instance_group import DBInstanceDomainBinding, DBInstanceGroup
from nekro_agent.models.db_mem_entity import DBMemEntity, EntityType, MemorySource
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import (
    CognitiveType,
    DBMemParagraph,
    KnowledgeType,
    OriginKind,
)
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.services import identity_scope as identity_scope_module
from nekro_agent.services.identity_scope import PRESET_NONE
from nekro_agent.services.memory import episode_aggregator as episode_module
from nekro_agent.services.memory import instance_scope as instance_scope_module
from nekro_agent.services.memory import qdrant_manager as qdrant_module
from nekro_agent.services.memory import query_service as query_service_module
from nekro_agent.services.memory import retriever as retriever_module
from nekro_agent.services.memory import semantic_writer as semantic_writer_module
from nekro_agent.services.memory.consolidator import EpisodicConsolidator
from nekro_agent.services.memory.instance_scope import (
    CONSOLE_ONLY_PRESET_ID,
    MemoryScope,
    build_instance_filter,
    resolve_allowed_instance_keys,
    resolve_memory_scope,
    resolve_memory_scope_by_chat_key,
)
from nekro_agent.services.memory.qdrant_manager import (
    PRESET_NONE_PAYLOAD,
    MemoryQdrantManager,
)
from nekro_agent.services.memory.query_service import (
    MemoryObjectType,
    get_workspace_memory_detail,
    search_workspace_memories,
    trace_workspace_memory_origin,
)
from nekro_agent.services.memory.retriever import (
    BatchRecallRequest,
    MemoryRetriever,
    retrieve_memories,
    retrieve_memories_batch,
)
from nekro_agent.services.memory.semantic_writer import persist_cc_task_memory

WORKSPACE_ID = 8801
ADAPTER = "onebot_v11"
INSTANCE_A = "ob8801a"
INSTANCE_B = "ob8801b"

CHAT_A = f"{ADAPTER}-{INSTANCE_A}-group_1"
CHAT_A2 = f"{ADAPTER}-{INSTANCE_A}-group_2"

PRESET_JIA = "人设甲"
PRESET_YI = "人设乙"

# 三条内容互不包含，断言里可以用 `in` 判定而不会互相误判
TEXT_JIA = "这是甲人设下说过的私密内容"
TEXT_YI = "这是乙人设下说过的私密内容"
TEXT_NONE = "这是无人设档时说过的私密内容"


@pytest.fixture
async def scope_db():
    """内存 SQLite 上的真实表结构。

    注册整个 `nekro_agent.models`：人设那一维走的是 `DBChatChannel.get_effective_preset_id`
    的完整回退链（频道 → 实例默认 → 系统默认 → 内置），少注册一张表就是在测另一件事。
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def memory_system_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """记忆总开关默认关闭；关着的话检索第一行就 return，测的是空转。"""
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_RETRIEVAL_MIN_SIMILARITY", 0.5)
    monkeypatch.setattr(config, "MEMORY_RETRIEVAL_REINFORCE_BOOST", 0.0)
    monkeypatch.setattr(config, "AI_CHAT_DEFAULT_PRESET_ID", None)


class _StubChannel:
    """`identity_scope.ChannelLike`：域两列 + 人设（闸开时还要频道类型、工作区与生效配置）。"""

    def __init__(
        self,
        instance_key: str,
        *,
        preset_id: int = PRESET_NONE,
        adapter_key: str = ADAPTER,
        chat_key: str = CHAT_A,
        channel_type: str = "group",
        workspace_id: Optional[int] = None,
    ) -> None:
        self.adapter_key = adapter_key
        self.instance_key = instance_key
        self.chat_key = chat_key
        self.channel_type = channel_type
        self.workspace_id = workspace_id
        self._preset_id = preset_id

    async def get_effective_preset_id(self) -> int:
        return self._preset_id

    async def get_effective_config(self) -> SimpleNamespace:
        # 三个跨频道召回开关一律关闭：本组用例只关心人设维，别的维度不参与
        return SimpleNamespace(
            MEMORY_RECALL_GROUP_TO_GROUP="off",
            MEMORY_RECALL_GROUP_TO_PRIVATE="off",
            MEMORY_RECALL_PRIVATE_TO_GROUP="off",
        )


class _PassThroughQdrant:
    """按段落 ID 返回固定相似度；**不做任何 payload 过滤**。

    过滤全部交给真实的 SQL 侧条件去做，于是"SQL 侧漏了人设"会真的把别的人设的段落捞出来。
    替身自己也过滤的话，这些用例测的是替身。
    """

    def __init__(self, scores: Dict[int, float]) -> None:
        self.scores = scores
        self.last_scope: Optional[MemoryScope] = None

    async def search(self, **kwargs: Any) -> List[Dict[str, Any]]:
        self.last_scope = kwargs.get("scope")
        threshold = float(kwargs.get("score_threshold", 0.0))
        return [
            {"id": paragraph_id, "score": score, "payload": {}}
            for paragraph_id, score in sorted(self.scores.items(), key=lambda item: -item[1])
            if score >= threshold
        ]


async def _seed_preset(name: str) -> DBPreset:
    return await DBPreset.create(
        name=name,
        title=name,
        avatar="",
        content=f"{name} 的设定",
        description=name,
        tags="",
        author="test",
    )


async def _seed_channel(chat_key: str, instance_key: str, *, preset_id: Optional[int] = None) -> DBChatChannel:
    return await DBChatChannel.create(
        chat_key=chat_key,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        channel_id=chat_key.rsplit("-", 1)[-1],
        channel_name=chat_key,
        channel_type="group",
        is_active=True,
        workspace_id=WORKSPACE_ID,
        preset_id=preset_id,
        data=json.dumps({}),
    )


async def _seed_paragraph(
    content: str,
    *,
    instance_key: str = INSTANCE_A,
    preset_id: int = PRESET_NONE,
    origin_chat_key: str = CHAT_A,
    origin_chat_type: str = "",
) -> DBMemParagraph:
    paragraph = await DBMemParagraph.create(
        workspace_id=WORKSPACE_ID,
        instance_key=instance_key,
        preset_id=preset_id,
        memory_source="na",
        cognitive_type=CognitiveType.SEMANTIC,
        knowledge_type=KnowledgeType.FACT,
        content=content,
        summary=content,
        half_life_seconds=30 * 24 * 3600,
        origin_kind=OriginKind.TASK,
        origin_chat_key=origin_chat_key,
        origin_chat_type=origin_chat_type,
    )
    # `default=` 只在 create 不带该列时生效，统一回写一次，不依赖写入时机
    await DBMemParagraph.filter(id=paragraph.id).update(
        instance_key=instance_key,
        preset_id=preset_id,
        origin_chat_type=origin_chat_type,
    )
    return paragraph


def _install_qdrant(monkeypatch: pytest.MonkeyPatch, scores: Dict[int, float]) -> _PassThroughQdrant:
    fake = _PassThroughQdrant(scores)

    async def fake_embed(_text: str, **_kwargs: object) -> List[float]:
        return [1.0, 0.0, 0.0, 0.0]

    monkeypatch.setattr(retriever_module, "embed_text", fake_embed)
    monkeypatch.setattr(retriever_module, "memory_qdrant_manager", fake)
    return fake


async def _boot_deployment() -> DBPreset:
    """跑一次 `init_default_preset()`，得到任何部署启动之后的状态。"""
    from nekro_agent.services import preset_service

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(config, "AI_CHAT_PRESET_NAME", "")
        patch.setattr(config, "AI_CHAT_PRESET_SETTING", "")
        patch.setattr(preset_service, "save_config", lambda: None)
        await preset_service.init_default_preset()
    assert config.AI_CHAT_DEFAULT_PRESET_ID is not None
    return await DBPreset.get(id=config.AI_CHAT_DEFAULT_PRESET_ID)


async def _seed_console_direct_cc(monkeypatch: pytest.MonkeyPatch, *, origin_ref: str = "cc-1") -> DBMemParagraph:
    """走真实写入出口沉淀一条控制台直发的 CC 结论（向量库换成不落盘的替身）。"""

    async def fake_embed(_text: str, **_kwargs: object) -> List[float]:
        return [1.0, 0.0, 0.0, 0.0]

    class _NoopQdrant:
        async def upsert_paragraph(self, **_kwargs: Any) -> bool:
            return True

    monkeypatch.setattr(semantic_writer_module, "embed_text", fake_embed)
    monkeypatch.setattr(semantic_writer_module, "memory_qdrant_manager", _NoopQdrant())

    paragraph = await persist_cc_task_memory(
        workspace_id=WORKSPACE_ID,
        task_content="整理季度预算",
        result_content="季度预算已按部门拆分完毕，三条线的上限与结转规则都已确认。" * 3,
        source_chat_key="__user__",
        origin_ref=origin_ref,
    )
    assert paragraph is not None
    return paragraph


# --------------------------------------------------------------------------------------
# 1. 零配置 = 改造前，逐字
# --------------------------------------------------------------------------------------


class TestZeroConfigIsByteForByteToday:
    """不配置实例组、不绑人设时，检索条件与结果都必须与改造前逐字相同。"""

    @pytest.mark.parametrize("instance_key", ["", INSTANCE_A, INSTANCE_B])
    async def test_scope_degrades_to_the_legacy_allowed_keys_plus_preset_zero(
        self,
        scope_db: None,
        instance_key: str,
    ) -> None:
        """域这一维逐字等于旧函数的输出，**包括 `None`（不过滤）那一档**。

        退化成 `[""]` 形态上很像"没变"，实际会给每次检索多一个 SQL 条件，是另一件事。
        """
        scope = await resolve_memory_scope(_StubChannel(instance_key))
        assert scope.allowed_instance_keys == resolve_allowed_instance_keys(instance_key)
        assert scope.preset_id == PRESET_NONE

    @pytest.mark.parametrize("instance_key", ["", INSTANCE_A])
    async def test_filter_dict_is_the_legacy_one_plus_preset_zero(
        self,
        scope_db: None,
        instance_key: str,
    ) -> None:
        """查询条件字典 = 旧的那份 + `preset_id=0`，一个键不多一个键不少。"""
        scope = await resolve_memory_scope(_StubChannel(instance_key))
        assert scope.as_filter() == {
            **build_instance_filter(resolve_allowed_instance_keys(instance_key)),
            "preset_id": PRESET_NONE,
        }

    @pytest.mark.parametrize("instance_key", ["", INSTANCE_A])
    async def test_a_real_preset_still_lets_the_no_preset_bucket_through(
        self,
        scope_db: None,
        instance_key: str,
    ) -> None:
        """绑了真人设时，条件是 `in (人设, 0)`——0 这一档必须仍然在里面。

        对照组是上一条（人设本来就是 0 时不该退化成 `__in`）。两条一起才能说明
        「0 是不属于任何人设的那一档」而不是「过滤被整个关掉了」。
        """
        scope = await resolve_memory_scope(_StubChannel(instance_key, preset_id=7))
        assert scope.as_filter() == {
            **build_instance_filter(resolve_allowed_instance_keys(instance_key)),
            "preset_id__in": [7, PRESET_NONE],
        }

    async def test_rows_written_before_the_change_stay_retrievable(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """按改造前的写法建的行（新列取默认值）必须原样召回，一条都不能少。

        存量行的 `preset_id` 是列默认值 0，而零配置的频道解析出来的人设也是 0。
        这条落不住，测试部署上现有的记忆就等于消失了。
        """
        legacy_rows = [
            await DBMemParagraph.create(
                workspace_id=WORKSPACE_ID,
                instance_key="",
                memory_source="na",
                cognitive_type=CognitiveType.SEMANTIC,
                knowledge_type=KnowledgeType.FACT,
                content=text,
                summary=text,
                half_life_seconds=30 * 24 * 3600,
                origin_kind=OriginKind.TASK,
                origin_chat_key=CHAT_A,
            )
            for text in (TEXT_JIA, TEXT_YI, TEXT_NONE)
        ]
        assert {row.preset_id for row in legacy_rows} == {0}

        _install_qdrant(monkeypatch, {row.id: 0.9 for row in legacy_rows})
        scope = await resolve_memory_scope(_StubChannel(""))
        memories = await retrieve_memories(workspace_id=WORKSPACE_ID, query="私密内容", limit=10, scope=scope)

        assert sorted(memory.target_id for memory in memories) == sorted(row.id for row in legacy_rows)

    async def test_single_instance_deployment_sends_no_instance_condition(self, scope_db: None) -> None:
        """单实例部署（`instance_key` 为空）不附加任何实例条件——改造前零影响的那一条。"""
        scope = await resolve_memory_scope(_StubChannel(""))
        assert "instance_key__in" not in scope.as_filter()


class TestZeroConfigOnARealDeploymentNotJustAStub:
    """⚠️ 上面那一组用的是 `_StubChannel` + `AI_CHAT_DEFAULT_PRESET_ID=None`。

    真实部署里这两个前提**都不成立**：频道是真的 `DBChatChannel`，走的是
    `get_effective_preset_id` 的完整回退链；而 `preset_service.init_default_preset()`
    在**每次启动**时都保证系统默认那一层指向一行真实人设（全新安装会直接建一行）。
    于是"什么都没绑的频道解析出 0"是假的——测试部署上 65/65 个频道 `preset_id IS NULL`、
    `AI_CHAT_DEFAULT_PRESET_ID: 2`，全部解析成 2。

    这一组不 monkeypatch 那个配置，用真频道跑，钉住"零配置仍然逐字相同"这条结论
    在**真实前提**下也成立。
    """

    @pytest.fixture(autouse=True)
    def _a_started_deployment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """覆盖掉外层 autouse 固件里的 `AI_CHAT_DEFAULT_PRESET_ID=None`。"""
        monkeypatch.setattr(config, "AI_CHAT_DEFAULT_PRESET_ID", None)

    async def _boot(self) -> DBPreset:
        """跑一次 `init_default_preset()`，得到任何部署启动之后的状态。"""
        return await _boot_deployment()

    async def test_an_unbound_channel_resolves_to_the_system_default_not_zero(self, scope_db: None) -> None:
        """先把真实前提本身钉死，否则下面两条会因为"恰好是 0"而空转。"""
        system_default = await self._boot()
        channel = await _seed_channel(CHAT_A, INSTANCE_A, preset_id=None)
        scope = await resolve_memory_scope(channel)
        assert scope.preset_id == system_default.id
        assert scope.preset_id != PRESET_NONE

    async def test_rows_written_before_the_change_survive_a_started_deployment(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """存量行（`preset_id` 取列默认值 0）在**有系统默认人设**的部署里必须仍然召回得到。

        这是"升级即失忆"那条：等值条件下这里返回空，而且没有任何报错指向那一行。
        """
        await self._boot()
        legacy = await DBMemParagraph.create(
            workspace_id=WORKSPACE_ID,
            instance_key="",
            memory_source="na",
            cognitive_type=CognitiveType.SEMANTIC,
            knowledge_type=KnowledgeType.FACT,
            content=TEXT_NONE,
            summary=TEXT_NONE,
            half_life_seconds=30 * 24 * 3600,
            origin_kind=OriginKind.TASK,
            origin_chat_key=CHAT_A,
        )
        assert legacy.preset_id == PRESET_NONE

        _install_qdrant(monkeypatch, {legacy.id: 0.9})
        channel = await _seed_channel(CHAT_A, INSTANCE_A, preset_id=None)
        memories = await retrieve_memories(
            workspace_id=WORKSPACE_ID,
            query="私密内容",
            limit=10,
            scope=await resolve_memory_scope(channel),
        )
        assert [memory.target_id for memory in memories] == [legacy.id]

    async def test_console_direct_cc_memory_is_never_recalled_in_a_channel(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """控制台直发 CC 的结论落在**仅控制台**的负数人设档，任何频道都召回不到。

        这些结论是一个人对着工作区下的指令与产出，不属于任何频道。它以前落在
        `(instance_key="", preset_id=0)`——而 0 是所有频道都放行的「无人设档」，
        于是任何群和私聊都能把它当成自己的记忆召回。

        现在写入侧把它收进 `CONSOLE_ONLY_PRESET_ID`（负数），频道侧的人设维只放行
        「当前人设」与 0，因此**闸关闸开都读不到**，而条件本身一个字都没改。
        后台记忆页与 CC 握手用的 `MemoryScope.unrestricted()` 不按人设过滤，照旧读得到。
        """
        await self._boot()

        console_row = await _seed_console_direct_cc(monkeypatch)
        assert (console_row.instance_key, console_row.preset_id) == ("", CONSOLE_ONLY_PRESET_ID)

        _install_qdrant(monkeypatch, {console_row.id: 0.9})
        no_preset_channel = await _seed_channel(CHAT_A, INSTANCE_A, preset_id=None)
        assert console_row.id not in [
            memory.target_id
            for memory in await retrieve_memories(
                workspace_id=WORKSPACE_ID,
                query="季度预算",
                limit=10,
                scope=await resolve_memory_scope(no_preset_channel),
            )
        ]

        real_preset = await _seed_preset(PRESET_JIA)
        preset_channel = await _seed_channel(CHAT_A2, INSTANCE_A, preset_id=real_preset.id)
        assert console_row.id not in [
            memory.target_id
            for memory in await retrieve_memories(
                workspace_id=WORKSPACE_ID,
                query="季度预算",
                limit=10,
                scope=await resolve_memory_scope(preset_channel),
            )
        ]

        # 后台记忆页与握手走的是这一条：不按人设过滤，要读得到
        everything = await retrieve_memories(
            workspace_id=WORKSPACE_ID,
            query="季度预算",
            limit=10,
            scope=MemoryScope.unrestricted(),
        )
        assert console_row.id in [memory.target_id for memory in everything]

    async def test_a_real_preset_still_cannot_see_another_real_preset(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """对照组：放行 0 这一档**不等于**把人设过滤关掉。

        没有它，上面三条在"人设维整个不过滤"的实现下也全绿。
        """
        await self._boot()
        preset_jia = await _seed_preset(PRESET_JIA)
        preset_yi = await _seed_preset(PRESET_YI)
        jia = await _seed_paragraph(TEXT_JIA, preset_id=preset_jia.id)
        yi = await _seed_paragraph(TEXT_YI, preset_id=preset_yi.id)
        _install_qdrant(monkeypatch, {jia.id: 0.9, yi.id: 0.9})

        channel = await _seed_channel(CHAT_A, INSTANCE_A, preset_id=preset_jia.id)
        memories = await retrieve_memories(
            workspace_id=WORKSPACE_ID,
            query="私密内容",
            limit=10,
            scope=await resolve_memory_scope(channel),
        )
        assert [memory.target_id for memory in memories] == [jia.id]


# --------------------------------------------------------------------------------------
# 2. 人设隔离：四条检索通道一条都不漏
# --------------------------------------------------------------------------------------


class TestPresetIsolatesEveryRetrievalChannel:
    """段落、实体、关系、Episode 都会直接进提示词，只堵一条等于没堵。"""

    async def test_paragraph_channel_only_returns_the_current_preset(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        jia = await _seed_paragraph(TEXT_JIA, preset_id=1)
        yi = await _seed_paragraph(TEXT_YI, preset_id=2)
        none = await _seed_paragraph(TEXT_NONE, preset_id=PRESET_NONE)
        _install_qdrant(monkeypatch, {jia.id: 0.9, yi.id: 0.9, none.id: 0.9})

        # 真人设之间互不可见；`preset_id=0`（不属于任何人设：存量记忆、控制台直发 CC）
        # 对每个人设都可见，理由见 `MemoryScope.as_filter` 的 docstring。
        expectations = (
            (1, {jia.id, none.id}),
            (2, {yi.id, none.id}),
            (PRESET_NONE, {none.id}),
        )
        for preset_id, expected in expectations:
            memories = await retrieve_memories(
                workspace_id=WORKSPACE_ID,
                query="私密内容",
                limit=10,
                scope=await resolve_memory_scope(_StubChannel(INSTANCE_A, preset_id=preset_id)),
            )
            # 正例先立住：没有它，"不含别的人设"会因为结果恒空而永远成立
            assert {memory.target_id for memory in memories} == expected

    async def test_entity_and_relation_channel_is_preset_scoped(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """图谱那一支：实体名命中之后带出来的关系，不能跨人设。"""
        jia_paragraph = await _seed_paragraph(TEXT_JIA, preset_id=1)
        subject, _ = await DBMemEntity.find_or_create(
            workspace_id=WORKSPACE_ID,
            entity_type=EntityType.PERSON,
            name="甲方老板",
            source=MemorySource.NA,
            instance_key=INSTANCE_A,
            preset_id=1,
        )
        obj, _ = await DBMemEntity.find_or_create(
            workspace_id=WORKSPACE_ID,
            entity_type=EntityType.CONCEPT,
            name="季度预算",
            source=MemorySource.NA,
            instance_key=INSTANCE_A,
            preset_id=1,
        )
        await DBMemRelation.find_or_create(
            workspace_id=WORKSPACE_ID,
            subject_entity_id=subject.id,
            predicate="批准了",
            object_entity_id=obj.id,
            paragraph_id=jia_paragraph.id,
            memory_source="na",
            cognitive_type=CognitiveType.SEMANTIC.value,
            instance_key=INSTANCE_A,
            preset_id=1,
        )
        _install_qdrant(monkeypatch, {})

        mine = await MemoryRetriever(
            WORKSPACE_ID,
            scope=await resolve_memory_scope(_StubChannel(INSTANCE_A, preset_id=1)),
        ).retrieve("甲方老板 季度预算", limit=10, record_access=False)
        theirs = await MemoryRetriever(
            WORKSPACE_ID,
            scope=await resolve_memory_scope(_StubChannel(INSTANCE_A, preset_id=2)),
        ).retrieve("甲方老板 季度预算", limit=10, record_access=False)

        assert [memory.source_type for memory in mine.memories] == ["relation"]
        assert theirs.memories == []

    async def test_episode_channel_is_preset_scoped(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Episode 的叙事摘要会整段进提示词，是泄漏面最大的一条通道。"""
        await DBMemEpisode.create(
            workspace_id=WORKSPACE_ID,
            instance_key=INSTANCE_A,
            preset_id=1,
            origin_chat_key=CHAT_A,
            title="甲人设的那场对话",
            narrative_summary=TEXT_JIA,
            participant_entity_ids=[],
            paragraph_ids=[],
            phase_mapping={},
        )
        _install_qdrant(monkeypatch, {})

        mine = await MemoryRetriever(
            WORKSPACE_ID,
            scope=await resolve_memory_scope(_StubChannel(INSTANCE_A, preset_id=1)),
        ).retrieve("甲人设的那场对话", limit=10, record_access=False)
        theirs = await MemoryRetriever(
            WORKSPACE_ID,
            scope=await resolve_memory_scope(_StubChannel(INSTANCE_A, preset_id=2)),
        ).retrieve("甲人设的那场对话", limit=10, record_access=False)

        assert [memory.source_type for memory in mine.memories] == ["episode"]
        assert theirs.memories == []

    async def test_direct_by_id_exits_are_preset_scoped(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """按 ID 直取的两个出口（`memory_id` 是自增整数，模型可以直接猜）。"""
        jia = await _seed_paragraph(TEXT_JIA, preset_id=1)
        _install_qdrant(monkeypatch, {jia.id: 0.9})

        mine_scope = await resolve_memory_scope(_StubChannel(INSTANCE_A, preset_id=1))
        other_scope = await resolve_memory_scope(_StubChannel(INSTANCE_A, preset_id=2))

        detail = await get_workspace_memory_detail(
            WORKSPACE_ID,
            MemoryObjectType.PARAGRAPH,
            jia.id,
            scope=mine_scope,
        )
        assert detail.memory_id == jia.id
        with pytest.raises(ValueError, match=str(jia.id)):
            await get_workspace_memory_detail(WORKSPACE_ID, MemoryObjectType.PARAGRAPH, jia.id, scope=other_scope)

        trace = await trace_workspace_memory_origin(
            WORKSPACE_ID,
            MemoryObjectType.PARAGRAPH,
            jia.id,
            scope=mine_scope,
        )
        assert trace.memory_id == jia.id
        with pytest.raises(ValueError, match=str(jia.id)):
            await trace_workspace_memory_origin(WORKSPACE_ID, MemoryObjectType.PARAGRAPH, jia.id, scope=other_scope)

    async def test_search_service_is_preset_scoped(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        jia = await _seed_paragraph(TEXT_JIA, preset_id=1)
        yi = await _seed_paragraph(TEXT_YI, preset_id=2)
        _install_qdrant(monkeypatch, {jia.id: 0.9, yi.id: 0.9})

        result = await search_workspace_memories(
            workspace_id=WORKSPACE_ID,
            query="私密内容",
            limit=10,
            scope=await resolve_memory_scope(_StubChannel(INSTANCE_A, preset_id=1)),
        )
        assert [item.target_id for item in result.items] == [jia.id]

    async def test_batch_entry_point_is_preset_scoped(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """提示词注入走的是批量入口，它自己也得带着人设。"""
        jia = await _seed_paragraph(TEXT_JIA, preset_id=1)
        yi = await _seed_paragraph(TEXT_YI, preset_id=2)
        _install_qdrant(monkeypatch, {jia.id: 0.9, yi.id: 0.9})

        results = await retrieve_memories_batch(
            workspace_id=WORKSPACE_ID,
            requests=[BatchRecallRequest(query="私密内容", limit=10)],
            scope=await resolve_memory_scope(_StubChannel(INSTANCE_A, preset_id=1)),
        )
        assert [memory.target_id for memory in results[0]] == [jia.id]


# --------------------------------------------------------------------------------------
# 3. 域维仍然成立，而且两维互不吃掉对方
# --------------------------------------------------------------------------------------


class TestDomainAndPresetAreIndependent:
    async def test_same_preset_across_domains_still_isolated(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """同一个人设、不同域：仍然互不可见（加人设维没有削弱域维）。"""
        mine = await _seed_paragraph(TEXT_JIA, instance_key=INSTANCE_A, preset_id=1)
        theirs = await _seed_paragraph(TEXT_YI, instance_key=INSTANCE_B, preset_id=1)
        _install_qdrant(monkeypatch, {mine.id: 0.9, theirs.id: 0.9})

        memories = await retrieve_memories(
            workspace_id=WORKSPACE_ID,
            query="私密内容",
            limit=10,
            scope=await resolve_memory_scope(_StubChannel(INSTANCE_A, preset_id=1)),
        )
        assert [memory.target_id for memory in memories] == [mine.id]

    async def test_instance_group_shares_the_domain_but_not_across_presets(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """把两个实例放进同一个域 = 记忆互通；但**仍然**按人设分开。

        这条同时钉住"域配置真的生效"和"域互通不会顺手把人设维也打开"。
        """
        group = await DBInstanceGroup.create(name="一家人")
        for instance_key in (INSTANCE_A, INSTANCE_B):
            await DBInstanceDomainBinding.create(
                adapter_key=ADAPTER,
                instance_key=instance_key,
                memory_group_id=group.id,
            )

        same_preset = await _seed_paragraph(TEXT_JIA, instance_key=INSTANCE_B, preset_id=1)
        other_preset = await _seed_paragraph(TEXT_YI, instance_key=INSTANCE_B, preset_id=2)
        _install_qdrant(monkeypatch, {same_preset.id: 0.9, other_preset.id: 0.9})

        memories = await retrieve_memories(
            workspace_id=WORKSPACE_ID,
            query="私密内容",
            limit=10,
            scope=await resolve_memory_scope(_StubChannel(INSTANCE_A, preset_id=1)),
        )
        assert [memory.target_id for memory in memories] == [same_preset.id]

    async def test_scope_by_chat_key_refuses_when_the_channel_is_gone(self, scope_db: None) -> None:
        """定位不到频道时返回 `None` 而不是退化成全量——这里是授权边界。"""
        await _seed_channel(CHAT_A, INSTANCE_A)
        assert await resolve_memory_scope_by_chat_key(CHAT_A) is not None
        assert await resolve_memory_scope_by_chat_key("onebot_v11-gone-group_404") is None


# --------------------------------------------------------------------------------------
# 4. 人设那一维走的是完整回退链，不是裸的 `channel.preset_id`
# --------------------------------------------------------------------------------------


class TestPresetDimensionUsesTheEffectiveChain:
    async def test_channel_bound_preset_is_used(self, scope_db: None) -> None:
        preset = await _seed_preset(PRESET_JIA)
        channel = await _seed_channel(CHAT_A, INSTANCE_A, preset_id=preset.id)
        scope = await resolve_memory_scope(channel)
        assert scope.preset_id == preset.id

    async def test_unbound_channel_falls_back_to_no_preset(self, scope_db: None) -> None:
        channel = await _seed_channel(CHAT_A, INSTANCE_A, preset_id=None)
        scope = await resolve_memory_scope(channel)
        assert scope.preset_id == PRESET_NONE

    async def test_deleted_preset_leaves_old_memories_as_orphans(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """人设被删之后，挂在它下面的记忆**原地保留成孤儿**，不级联删、也不改判归属。

        人设可能是误删，所以删除后新数据落到回退链的下一档，旧数据留在原地。
        """
        preset = await _seed_preset(PRESET_JIA)
        channel = await _seed_channel(CHAT_A, INSTANCE_A, preset_id=preset.id)
        orphan = await _seed_paragraph(TEXT_JIA, preset_id=preset.id)

        await preset.delete()
        channel = await DBChatChannel.get(chat_key=CHAT_A)
        assert (await resolve_memory_scope(channel)).preset_id == PRESET_NONE

        # 行还在，只是当前人设读不到它
        assert await DBMemParagraph.filter(id=orphan.id).exists()
        _install_qdrant(monkeypatch, {orphan.id: 0.9})
        memories = await retrieve_memories(
            workspace_id=WORKSPACE_ID,
            query="私密内容",
            limit=10,
            scope=await resolve_memory_scope(channel),
        )
        assert memories == []


# --------------------------------------------------------------------------------------
# 5. 写入侧：标上人设，但不产生副本
# --------------------------------------------------------------------------------------


class TestWriteSideTagsPresetWithoutCopies:
    async def test_consolidator_tags_the_channel_preset(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        preset = await _seed_preset(PRESET_JIA)
        await _seed_channel(CHAT_A, INSTANCE_A, preset_id=preset.id)
        consolidator = EpisodicConsolidator(WORKSPACE_ID)

        async def fake_embed(_text: str, **_kwargs: object) -> List[float]:
            return [1.0, 0.0, 0.0, 0.0]

        class _NoopQdrant:
            async def upsert_paragraph(self, **_kwargs: Any) -> bool:
                return True

        monkeypatch.setattr("nekro_agent.services.memory.consolidator.embed_text", fake_embed)
        monkeypatch.setattr("nekro_agent.services.memory.consolidator.memory_qdrant_manager", _NoopQdrant())

        await consolidator._load_channel(CHAT_A)  # noqa: SLF001
        assert consolidator.instance_key == INSTANCE_A
        assert consolidator.preset_id == preset.id

        paragraph, entities, relations = await consolidator._store_memory(  # noqa: SLF001
            {
                "content": TEXT_JIA,
                "summary": TEXT_JIA,
                "knowledge_type": "fact",
                "entities": ["甲方老板", "季度预算"],
                "relations": [{"subject": "甲方老板", "predicate": "批准了", "object": "季度预算"}],
            },
            CHAT_A,
            [],
        )

        assert paragraph is not None
        assert paragraph.preset_id == preset.id
        assert {entity.preset_id for entity, _ in entities} == {preset.id}
        assert {relation.preset_id for relation, _ in relations} == {preset.id}

    async def test_one_conversation_is_written_once_not_once_per_preset(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """按人设隔离**不产生副本**——隔离之所以不增加存储成本，靠的正是这一条。

        库里已经存在另一个人设时，一次沉淀仍然只落一行、只标一个人设。
        """
        jia = await _seed_preset(PRESET_JIA)
        yi = await _seed_preset(PRESET_YI)
        assert yi.id != jia.id
        await _seed_channel(CHAT_A, INSTANCE_A, preset_id=jia.id)

        async def fake_embed(_text: str, **_kwargs: object) -> List[float]:
            return [1.0, 0.0, 0.0, 0.0]

        class _NoopQdrant:
            async def upsert_paragraph(self, **_kwargs: Any) -> bool:
                return True

        monkeypatch.setattr("nekro_agent.services.memory.consolidator.embed_text", fake_embed)
        monkeypatch.setattr("nekro_agent.services.memory.consolidator.memory_qdrant_manager", _NoopQdrant())

        consolidator = EpisodicConsolidator(WORKSPACE_ID)
        await consolidator._load_channel(CHAT_A)  # noqa: SLF001
        await consolidator._store_memory(  # noqa: SLF001
            {"content": TEXT_JIA, "summary": TEXT_JIA, "knowledge_type": "fact", "entities": [], "relations": []},
            CHAT_A,
            [],
        )

        rows = await DBMemParagraph.filter(workspace_id=WORKSPACE_ID).all()
        assert len(rows) == 1
        assert rows[0].preset_id == jia.id

    async def test_cc_semantic_writer_carries_the_preset(self, scope_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_embed(_text: str, **_kwargs: object) -> List[float]:
            return [1.0, 0.0, 0.0, 0.0]

        class _NoopQdrant:
            async def upsert_paragraph(self, **_kwargs: Any) -> bool:
                return True

        monkeypatch.setattr(semantic_writer_module, "embed_text", fake_embed)
        monkeypatch.setattr(semantic_writer_module, "memory_qdrant_manager", _NoopQdrant())

        paragraph = await persist_cc_task_memory(
            workspace_id=WORKSPACE_ID,
            task_content="整理季度预算",
            result_content="结论正文足够长，能过最小沉淀阈值。" * 5,
            source_chat_key=CHAT_A,
            origin_ref="reply-1",
            instance_key=INSTANCE_A,
            preset_id=7,
        )
        assert paragraph is not None
        assert (paragraph.instance_key, paragraph.preset_id) == (INSTANCE_A, 7)

    async def test_console_direct_cc_lands_in_the_console_only_bucket(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """控制台直发（`__user__`）落成「不属于任何实例 + 仅控制台可见」的那一档。

        它与 `_build_cc_memory_handshake` 的全量读是配套的一对：写在负数档、读的时候不按
        人设过滤。任何一侧单独改都会让控制台直发读不到自己写的东西——或者更糟，
        让这些结论重新跑进频道的召回里。
        """
        await _boot_deployment()
        paragraph = await _seed_console_direct_cc(monkeypatch, origin_ref="reply-2")
        assert (paragraph.instance_key, paragraph.preset_id) == ("", CONSOLE_ONLY_PRESET_ID)

    async def test_console_identity_overrides_whatever_the_caller_passed(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """以 `__user__` 写回时，调用方传的实例与人设一律被覆盖。

        收口放在 `persist_cc_task_memory` 内部而不是各个调用点：控制台身份是"这条结论从哪来"，
        任何新增的写回路径只要带着 `__user__` 就自动落进仅控制台档。
        这里传一个像模像样的实例与人设，落库仍必须是 `("", CONSOLE_ONLY_PRESET_ID)`。
        """

        async def fake_embed(_text: str, **_kwargs: object) -> List[float]:
            return [1.0, 0.0, 0.0, 0.0]

        class _NoopQdrant:
            async def upsert_paragraph(self, **_kwargs: Any) -> bool:
                return True

        monkeypatch.setattr(semantic_writer_module, "embed_text", fake_embed)
        monkeypatch.setattr(semantic_writer_module, "memory_qdrant_manager", _NoopQdrant())

        paragraph = await persist_cc_task_memory(
            workspace_id=WORKSPACE_ID,
            task_content="控制台直发但传了频道参数",
            result_content="结论正文足够长，能过最小沉淀阈值。" * 5,
            source_chat_key="__user__",
            origin_ref="reply-3",
            instance_key="x",
            preset_id=5,
        )
        assert paragraph is not None
        assert (paragraph.instance_key, paragraph.preset_id) == ("", CONSOLE_ONLY_PRESET_ID)

    async def test_a_channel_sourced_cc_conclusion_is_still_recalled_there(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """对照组：频道里自己发起的 CC 任务照常召回得到。

        没有它，上一条在"CC 记忆整体关掉"或"人设维把 CC 一律挡掉"的实现下也会绿。
        """

        async def fake_embed(_text: str, **_kwargs: object) -> List[float]:
            return [1.0, 0.0, 0.0, 0.0]

        class _NoopQdrant:
            async def upsert_paragraph(self, **_kwargs: Any) -> bool:
                return True

        monkeypatch.setattr(semantic_writer_module, "embed_text", fake_embed)
        monkeypatch.setattr(semantic_writer_module, "memory_qdrant_manager", _NoopQdrant())

        preset = await _seed_preset(PRESET_JIA)
        paragraph = await persist_cc_task_memory(
            workspace_id=WORKSPACE_ID,
            task_content="频道里委托的任务",
            result_content="频道委托的结论正文，长度足够过最小沉淀阈值。" * 5,
            source_chat_key=CHAT_A,
            origin_ref="reply-4",
            instance_key=INSTANCE_A,
            preset_id=preset.id,
        )
        assert paragraph is not None
        assert (paragraph.instance_key, paragraph.preset_id) == (INSTANCE_A, preset.id)

        _install_qdrant(monkeypatch, {paragraph.id: 0.9})
        channel = await _seed_channel(CHAT_A, INSTANCE_A, preset_id=preset.id)
        memories = await retrieve_memories(
            workspace_id=WORKSPACE_ID,
            query="频道委托的结论",
            limit=10,
            scope=await resolve_memory_scope(channel),
        )
        assert paragraph.id in [memory.target_id for memory in memories if memory.source_type == "paragraph"]

    async def test_console_direct_cc_relations_are_also_channel_invisible(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """实体与关系跟着段落一起落进仅控制台档。

        关系召回那一支单独按 `MemTable.RELATION` 过滤，只把段落堵住的话，这条三元组照样
        会以"关系记忆"的身份进频道上下文。
        """
        await _boot_deployment()
        paragraph = await _seed_console_direct_cc(monkeypatch, origin_ref="reply-5")

        entities = await DBMemEntity.filter(workspace_id=WORKSPACE_ID).all()
        relations = await DBMemRelation.filter(workspace_id=WORKSPACE_ID).all()
        assert entities, "控制台直发的结论没有产出实体，下面的断言会空转"
        assert relations, "控制台直发的结论没有产出关系，下面的断言会空转"
        assert {entity.preset_id for entity in entities} == {CONSOLE_ONLY_PRESET_ID}
        assert {relation.preset_id for relation in relations} == {CONSOLE_ONLY_PRESET_ID}

        _install_qdrant(monkeypatch, {})
        channel = await _seed_channel(CHAT_A, INSTANCE_A, preset_id=None)
        scope = await resolve_memory_scope(channel)
        query = " ".join([entities[0].canonical_name, entities[-1].canonical_name])
        memories = await retrieve_memories(workspace_id=WORKSPACE_ID, query=query, limit=10, scope=scope)
        assert [memory for memory in memories if memory.source_type == "relation"] == []

        everything = await retrieve_memories(
            workspace_id=WORKSPACE_ID,
            query=query,
            limit=10,
            scope=MemoryScope.unrestricted(),
        )
        relation_hits = [memory for memory in everything if memory.source_type == "relation"]
        assert relation_hits, "全量检索也读不到这些关系，说明上一条断言是空转"
        assert {memory.target_id for memory in relation_hits} == {relation.id for relation in relations}
        # 段落本身同样要读得到：控制台侧的握手靠的就是这一条
        assert paragraph.id in [memory.target_id for memory in everything]

    async def test_entities_of_different_presets_are_separate_nodes(self, scope_db: None) -> None:
        """同名实体在两个人设下是两个节点。

        共用一行的话它只能带一个 `preset_id`，另一个人设按人设过滤时读不到它，
        而那个人设新建的关系又指着这个 ID——关系召回那一支会静默丢结果。
        """
        first, created_first = await DBMemEntity.find_or_create(
            workspace_id=WORKSPACE_ID,
            entity_type=EntityType.PERSON,
            name="甲方老板",
            source=MemorySource.NA,
            instance_key=INSTANCE_A,
            preset_id=1,
        )
        second, created_second = await DBMemEntity.find_or_create(
            workspace_id=WORKSPACE_ID,
            entity_type=EntityType.PERSON,
            name="甲方老板",
            source=MemorySource.NA,
            instance_key=INSTANCE_A,
            preset_id=2,
        )
        again, created_again = await DBMemEntity.find_or_create(
            workspace_id=WORKSPACE_ID,
            entity_type=EntityType.PERSON,
            name="甲方老板",
            source=MemorySource.NA,
            instance_key=INSTANCE_A,
            preset_id=1,
        )
        assert (created_first, created_second, created_again) == (True, True, False)
        assert first.id != second.id
        assert again.id == first.id

    async def test_relations_of_different_presets_are_separate_rows(self, scope_db: None) -> None:
        made = []
        for preset_id in (1, 2, 1):
            relation, created = await DBMemRelation.find_or_create(
                workspace_id=WORKSPACE_ID,
                subject_entity_id=11,
                predicate="批准了",
                object_entity_id=22,
                memory_source="na",
                cognitive_type=CognitiveType.SEMANTIC.value,
                instance_key=INSTANCE_A,
                preset_id=preset_id,
            )
            made.append((relation.id, created))
        assert [created for _, created in made] == [True, True, False]
        assert made[0][0] != made[1][0]
        assert made[2][0] == made[0][0]

    @pytest.mark.parametrize(
        ("preset_sequence", "expected_groups"),
        [
            # 六段连续、同频道、同实例：只有人设不同这一个变量
            ((1, 1, 1, 2, 2, 2), [[1], [2]]),
            # 对照组：人设全同 => 聚成**一条**。少了它，上面那条在"聚合器整个失效、
            # 一条都不产出"的实现下也会红得看不出区别，而在"永远按每 3 段切一刀"的
            # 实现下反而会绿。
            ((1, 1, 1, 1, 1, 1), [[1]]),
        ],
    )
    async def test_episode_never_merges_two_presets(
        self,
        scope_db: None,
        preset_sequence: Tuple[int, ...],
        expected_groups: List[List[int]],
    ) -> None:
        """Episode 只带一个 `preset_id`，跨人设聚合会把一边的段落打包进另一边的叙事。"""
        from datetime import datetime, timedelta, timezone

        aggregator = episode_module.EpisodeAggregator(WORKSPACE_ID)
        base = datetime(2026, 3, 1, tzinfo=timezone.utc)
        for index, preset_id in enumerate(preset_sequence):
            paragraph = await DBMemParagraph.create(
                workspace_id=WORKSPACE_ID,
                instance_key=INSTANCE_A,
                preset_id=preset_id,
                memory_source="na",
                cognitive_type=CognitiveType.EPISODIC,
                knowledge_type=KnowledgeType.CONVERSATION,
                content=f"第 {index} 段",
                summary=f"第 {index} 段",
                event_time=base + timedelta(minutes=index),
                half_life_seconds=3600,
                origin_kind=OriginKind.CONSOLIDATION,
                origin_chat_key=CHAT_A,
            )
            await DBMemParagraph.filter(id=paragraph.id).update(preset_id=preset_id)

        groups = await aggregator._collect_candidate_groups(CHAT_A)  # noqa: SLF001
        assert [sorted({p.preset_id for p in group}) for group in groups] == expected_groups


# --------------------------------------------------------------------------------------
# 6. 向量侧：payload、索引，以及"字段缺失"这一档
# --------------------------------------------------------------------------------------


def _condition_keys(conditions: Any) -> List[str]:
    """递归收集一组 Qdrant 条件里出现的 payload 字段名（含嵌套 `Filter`）。"""
    collected: List[str] = []
    for condition in conditions or []:
        key = getattr(condition, "key", None)
        if isinstance(key, str):
            collected.append(key)
        is_empty = getattr(condition, "is_empty", None)
        if is_empty is not None and isinstance(getattr(is_empty, "key", None), str):
            collected.append(is_empty.key)
        for branch in ("must", "should", "must_not"):
            nested = getattr(condition, branch, None)
            if nested:
                collected.extend(_condition_keys(nested))
    return collected


class TestQdrantPresetPayload:
    async def test_payload_carries_the_preset(self, scope_db: None) -> None:
        paragraph = await _seed_paragraph(TEXT_JIA, preset_id=5)
        assert paragraph.to_qdrant_payload()["preset_id"] == 5

    def test_payload_constant_matches_the_resolver(self) -> None:
        """向量侧的"无人设档"取值必须与解析器同值。

        `qdrant_manager` 刻意不 import `identity_scope`（它会把模型层整条导入链拖进启动早期），
        代价就是这里有第二个字面量——所以要有一条用例把两者钉在一起。
        """
        assert PRESET_NONE_PAYLOAD == PRESET_NONE

    def test_preset_zero_also_accepts_points_without_the_field(self) -> None:
        """改造前写入的 point 的 payload 里没有 `preset_id`。

        Qdrant 的 `must` 对缺字段的 point 判为**不匹配**，照直写等值条件的话，
        升级那一刻每一个存量向量都会落选——表现为 Bot 集体失忆，
        而且没有任何报错指向这一行。
        """
        condition = MemoryQdrantManager._build_preset_condition(PRESET_NONE_PAYLOAD)  # noqa: SLF001
        assert condition.must is None
        assert condition.should is not None
        empty = [item for item in condition.should if type(item).__name__ == "IsEmptyCondition"]
        assert [item.is_empty.key for item in empty] == ["preset_id"]
        # 人设本来就是 0 时不该出现两条重复的等值条件
        assert [item.match.value for item in condition.should if type(item).__name__ == "FieldCondition"] == [
            PRESET_NONE_PAYLOAD,
        ]

    def test_a_real_preset_accepts_itself_and_the_no_preset_bucket_only(self) -> None:
        """真人设那一档：自己 + 0 + 缺字段，**恰好三条**，别的人设一条都没有。

        这条同时是两个方向的对照组：
        - 少了 0 与"缺字段"，控制台直发 CC 的记忆与全部存量向量在向量侧集体落选，
          而数据库侧（`MemoryScope.as_filter`）是放行的——两条腿给出不同的可见集合，
          症状是"召回了但取不到行"，没有任何报错。
        - 多了别的人设，人设隔离在向量侧就整个没有了。
        """
        condition = MemoryQdrantManager._build_preset_condition(7)  # noqa: SLF001
        assert condition.must is None
        assert condition.should is not None
        values = [item.match.value for item in condition.should if type(item).__name__ == "FieldCondition"]
        assert values == [7, PRESET_NONE_PAYLOAD]
        empty = [item for item in condition.should if type(item).__name__ == "IsEmptyCondition"]
        assert [item.is_empty.key for item in empty] == ["preset_id"]

    def test_vector_side_and_db_side_agree_on_the_no_preset_bucket(self) -> None:
        """两条腿必须对同一批 point 给出同一个答案。

        向量侧召回一批 ID、数据库侧再按 `as_filter()` 取回行：松的那一侧白召回，
        紧的那一侧静默丢结果。这里把"0 这一档两边都放行"钉成一条不变式，
        以后任何一侧单独收紧都会红。
        """
        db_filter = MemoryScope(allowed_instance_keys=None, preset_id=7).as_filter()
        assert PRESET_NONE_PAYLOAD in db_filter["preset_id__in"]

        condition = MemoryQdrantManager._build_preset_condition(7)  # noqa: SLF001
        vector_values = {
            item.match.value for item in (condition.should or []) if type(item).__name__ == "FieldCondition"
        }
        assert set(db_filter["preset_id__in"]) == vector_values

    @pytest.mark.asyncio
    async def test_search_forwards_the_preset_condition(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: Dict[str, Any] = {}

        class _RecordingClient:
            async def search(self, **kwargs: Any) -> List[Any]:
                captured["filter"] = kwargs["query_filter"]
                return []

        async def get_client() -> _RecordingClient:
            return _RecordingClient()

        monkeypatch.setattr(qdrant_module, "get_qdrant_client", get_client)
        await MemoryQdrantManager(collection_name="t").search(
            query_vector=[0.0],
            workspace_id=1,
            scope=MemoryScope(allowed_instance_keys=["a", ""], preset_id=3),
        )
        # 人设那一条是**嵌套 Filter**（自己 or 0 or 缺字段），扁平地读 `.key` 会漏掉它——
        # 这正是"新过滤字段没进条件里"最容易溜过去的形状，所以要递归收键。
        keys = _condition_keys(captured["filter"].must)
        assert "preset_id" in keys
        assert "instance_key" in keys

    @pytest.mark.asyncio
    async def test_unrestricted_scope_sends_neither_condition(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """两维都不设限时，向量侧的过滤条件与改造前逐字相同（只有 workspace + is_inactive）。"""
        captured: Dict[str, Any] = {}

        class _RecordingClient:
            async def search(self, **kwargs: Any) -> List[Any]:
                captured["filter"] = kwargs["query_filter"]
                return []

        async def get_client() -> _RecordingClient:
            return _RecordingClient()

        monkeypatch.setattr(qdrant_module, "get_qdrant_client", get_client)
        await MemoryQdrantManager(collection_name="t").search(
            query_vector=[0.0],
            workspace_id=1,
            scope=MemoryScope.unrestricted(),
        )
        keys = [getattr(condition, "key", None) for condition in captured["filter"].must]
        assert keys == ["workspace_id", "is_inactive"]


# --------------------------------------------------------------------------------------
# 7. 消费点的完整性（#45：四个消费点只测了一个）
# --------------------------------------------------------------------------------------

_SCOPED_MODELS = frozenset({"DBMemParagraph", "DBMemEntity", "DBMemRelation", "DBMemEpisode"})
_SCOPE_SPREADS = frozenset({"_scope_filter", "scope_filter"})
_RETRIEVAL_SOURCES = (
    pathlib.Path(retriever_module.__file__),
    pathlib.Path(query_service_module.__file__),
)


def _unscoped_queries(path: pathlib.Path) -> List[str]:
    """找出检索侧所有**没有**展开作用域过滤的 `mem_*` 查询。

    写死一份"消费点清单"挡不住第五个消费点——它对任何新增都恒真。这里改成扫源码：
    `DBMemParagraph.filter(...)` / `.get_or_none(...)` 这类调用，必须要么出现
    `**self._scope_filter` / `**scope_filter`，要么整个调用作为参数交给
    `scope.apply(...)`。两条都是「作用域被展开」的形状；裸查询就是无过滤的旁路。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def is_scope_apply(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            return False
        if node.func.attr != "apply":
            return False
        owner = node.func.value
        return getattr(owner, "attr", getattr(owner, "id", "")) == "scope"

    offenders: List[str] = []

    def visit(node: ast.AST, inside_scope_apply: bool) -> None:
        next_inside = inside_scope_apply or is_scope_apply(node)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if (
                isinstance(owner, ast.Name)
                and owner.id in _SCOPED_MODELS
                and node.func.attr in {"filter", "get_or_none", "get"}
            ):
                spread = any(
                    keyword.arg is None
                    and isinstance(keyword.value, (ast.Attribute, ast.Name))
                    and getattr(keyword.value, "attr", getattr(keyword.value, "id", "")) in _SCOPE_SPREADS
                    for keyword in node.keywords
                )
                if not spread and not next_inside:
                    offenders.append(f"{path.name}:{node.lineno} {owner.id}.{node.func.attr}()")
        for child in ast.iter_child_nodes(node):
            visit(child, next_inside)

    visit(tree, False)
    return offenders


class TestEveryRetrievalConsumerCarriesTheScope:
    def test_no_unscoped_mem_query_survives_in_the_retrieval_path(self) -> None:
        offenders = [entry for path in _RETRIEVAL_SOURCES for entry in _unscoped_queries(path)]
        assert offenders == [], "检索侧存在没有展开作用域过滤的 mem_* 查询：" + "; ".join(offenders)

    def test_the_scan_actually_finds_something_to_check(self) -> None:
        """对照组：确认扫描器真的看到了查询，否则上一条是空转。

        实测：把 `_SCOPED_MODELS` 写错一个字母，上一条恒绿。
        """
        total = 0
        for path in _RETRIEVAL_SOURCES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            total += sum(
                1
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in _SCOPED_MODELS
                and node.func.attr in {"filter", "get_or_none", "get"}
            )
        assert total >= 15, f"检索侧只扫到 {total} 处 mem_* 查询，扫描器多半失效了"

    @pytest.mark.parametrize(
        "func",
        [
            retrieve_memories,
            retrieve_memories_batch,
            search_workspace_memories,
            get_workspace_memory_detail,
            trace_workspace_memory_origin,
        ],
    )
    def test_scope_is_a_required_keyword_on_every_public_entry_point(self, func: Any) -> None:
        """`scope` **没有默认值**。

        有默认值就等于"少传一个参数，过滤条件静默消失"，那正是 #45 的形状：
        不报错、照常返回结果，只是返回的是别人的。
        """
        parameter = inspect.signature(func).parameters["scope"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty

    def test_retriever_constructor_also_requires_it(self) -> None:
        parameter = inspect.signature(MemoryRetriever.__init__).parameters["scope"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty

    def test_qdrant_search_also_requires_it(self) -> None:
        parameter = inspect.signature(MemoryQdrantManager.search).parameters["scope"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty


class TestTraceRecursionKeepsTheScope:
    """`trace_workspace_memory_origin` 对关系是**递归**的：关系 → 它的来源段落。

    递归时把作用域丢掉，关系就成了这条转发链上的一个绕过口——而且它吐的是**原始消息正文**，
    比详情页漏得更狠。关系行上的 `paragraph_id` 只是一个整数，没有任何约束保证它指向的
    段落与自己同域同人设（人设改判、清理半途中断、历史数据都会造出这种漂移）。
    """

    async def test_relation_pointing_at_another_presets_paragraph_traces_to_nothing(
        self,
        scope_db: None,
    ) -> None:
        other_preset_paragraph = await _seed_paragraph(TEXT_YI, preset_id=2, origin_chat_key=CHAT_A2)
        relation = await DBMemRelation.create(
            workspace_id=WORKSPACE_ID,
            instance_key=INSTANCE_A,
            preset_id=1,
            subject_entity_id=11,
            predicate="提到了",
            object_entity_id=22,
            paragraph_id=other_preset_paragraph.id,
            memory_source="na",
            cognitive_type=CognitiveType.SEMANTIC.value,
        )
        await DBMemRelation.filter(id=relation.id).update(preset_id=1, instance_key=INSTANCE_A)

        scope = await resolve_memory_scope(_StubChannel(INSTANCE_A, preset_id=1))
        with pytest.raises(ValueError, match=str(other_preset_paragraph.id)):
            await trace_workspace_memory_origin(
                WORKSPACE_ID,
                MemoryObjectType.RELATION,
                relation.id,
                scope=scope,
            )

    async def test_relation_and_its_own_paragraph_still_trace_through(self, scope_db: None) -> None:
        """对照组：同人设的关系照常追溯得到来源段落。

        没有它，上一条在"关系追溯整个坏掉、永远抛异常"的实现下也会绿。
        """
        paragraph = await _seed_paragraph(TEXT_JIA, preset_id=1, origin_chat_key=CHAT_A)
        relation = await DBMemRelation.create(
            workspace_id=WORKSPACE_ID,
            instance_key=INSTANCE_A,
            preset_id=1,
            subject_entity_id=11,
            predicate="提到了",
            object_entity_id=22,
            paragraph_id=paragraph.id,
            memory_source="na",
            cognitive_type=CognitiveType.SEMANTIC.value,
        )
        await DBMemRelation.filter(id=relation.id).update(preset_id=1, instance_key=INSTANCE_A)

        trace = await trace_workspace_memory_origin(
            WORKSPACE_ID,
            MemoryObjectType.RELATION,
            relation.id,
            scope=await resolve_memory_scope(_StubChannel(INSTANCE_A, preset_id=1)),
        )
        assert trace.memory_id == paragraph.id
        assert trace.origin_chat_key == CHAT_A


# --------------------------------------------------------------------------------------
# 8. 两个刻意保留的行为：控制台全量读，以及同域同人设的跨频道共享
# --------------------------------------------------------------------------------------


class TestDeliberateNonIsolationIsDocumented:
    async def test_console_cc_handshake_is_explicitly_unrestricted(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """控制台直发 CC 的记忆握手**故意**读全量，理由写在它的 docstring 里。

        它手上没有频道，既算不出域也算不出人设（`resolve_memory_scope` 没有可喂的入参）。
        断言落在**真的传下去的那个值**上而不是源码文本：源码里那串字面量在 docstring 里
        也出现，拿 `in source` 判定会被"改了代码没改注释"整个骗过去（实测这个变异存活）。

        它与 `persist_cc_task_memory` 的默认值是配套的一对：写在 `CONSOLE_ONLY_PRESET_ID`
        这一档、读的时候不按人设过滤。频道侧的人设维永远命不中那个负数档，
        所以控制台直发的结论只有这里读得到——任何一侧单独改都会让这条链路断掉。
        写侧落在负数档这件事由 `TestWriteSideTagsPresetWithoutCopies` 的用例钉住。
        """
        from nekro_agent.routers import workspaces as workspaces_module

        captured: Dict[str, Any] = {}

        async def fake_retrieve(**kwargs: Any) -> List[Any]:
            captured["scope"] = kwargs["scope"]
            return []

        monkeypatch.setattr(workspaces_module, "is_memory_system_enabled", lambda: True)
        monkeypatch.setattr(workspaces_module, "retrieve_memories", fake_retrieve)

        await workspaces_module._build_cc_memory_handshake(WORKSPACE_ID, "整理季度预算")  # noqa: SLF001

        assert captured["scope"] == MemoryScope.unrestricted()
        assert captured["scope"].allowed_instance_keys is None
        assert captured["scope"].preset_id is None

    async def test_two_channels_of_the_same_instance_and_preset_still_share(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """**既有余量，有意保留**：同实例、同人设的两个群绑同一工作区仍然互相可见。

        `origin_chat_key` 从来没有进过检索过滤，隔离键按设计就**不含** `chat_key`：
        群聊与私聊合并记一份，同一个人在群里和私聊里是同一个人。
        把 `chat_key` 加进过滤会当场推翻这条设计。

        加了人设维之后这条余量**变窄但仍然成立**：两个群人设不同就自动分开
        （下一条用例），人设相同才共享。而"把两个频道绑进同一个工作区"本身是运营的显式动作。

        这条用例是**行为文档**：它保护的是"共享"而不是"隔离"，
        哪天有人顺手给检索加上 `origin_chat_key` 过滤，这里会红，提醒他这是在改隔离设计。
        """
        from_group_1 = await _seed_paragraph(TEXT_JIA, preset_id=1, origin_chat_key=CHAT_A)
        from_group_2 = await _seed_paragraph(TEXT_YI, preset_id=1, origin_chat_key=CHAT_A2)
        _install_qdrant(monkeypatch, {from_group_1.id: 0.9, from_group_2.id: 0.9})

        memories = await retrieve_memories(
            workspace_id=WORKSPACE_ID,
            query="私密内容",
            limit=10,
            scope=await resolve_memory_scope(_StubChannel(INSTANCE_A, preset_id=1)),
        )
        assert sorted(memory.target_id for memory in memories) == sorted([from_group_1.id, from_group_2.id])

    async def test_two_channels_with_different_presets_are_now_separated(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """上一条余量被人设维收窄的那一半：两个群人设不同就不再串。"""
        from_group_1 = await _seed_paragraph(TEXT_JIA, preset_id=1, origin_chat_key=CHAT_A)
        from_group_2 = await _seed_paragraph(TEXT_YI, preset_id=2, origin_chat_key=CHAT_A2)
        _install_qdrant(monkeypatch, {from_group_1.id: 0.9, from_group_2.id: 0.9})

        memories = await retrieve_memories(
            workspace_id=WORKSPACE_ID,
            query="私密内容",
            limit=10,
            scope=await resolve_memory_scope(_StubChannel(INSTANCE_A, preset_id=1)),
        )
        assert [memory.target_id for memory in memories] == [from_group_1.id]


# --------------------------------------------------------------------------------------
# 9. 闸门打开时，仅控制台档依然进不了群与私聊
# --------------------------------------------------------------------------------------


class TestConsoleOnlyBucketUnderAnOpenGate:
    """闸关时人设维是 `in (人设, 0)`，闸开时是 `PresetVisible` —— 两条不同的渲染路径。

    闸关那组用例只钉住了前者。闸开时频道还多出本地集与域成员两维，正好是"某一维
    顺手把负数档放过去"最容易发生的形态，所以这一组在闸开下把两个频道类型各跑一遍。
    """

    @pytest.fixture(autouse=True)
    def _gate_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)

    async def test_neither_a_group_nor_a_private_chat_recalls_it(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _boot_deployment()
        console_row = await _seed_console_direct_cc(monkeypatch, origin_ref="cc-gate")
        group = await _seed_channel(CHAT_A, INSTANCE_A, preset_id=None)
        private = await DBChatChannel.create(
            chat_key=CHAT_A2,
            adapter_key=ADAPTER,
            instance_key=INSTANCE_A,
            channel_id="private_1",
            channel_name="私聊",
            channel_type="private",
            is_active=True,
            workspace_id=WORKSPACE_ID,
            preset_id=None,
            data=json.dumps({}),
        )
        preset_id = await group.get_effective_preset_id()
        # 两个频道各自的原生记忆：证明"读不到"不是因为检索整个空转
        own_group = await _seed_paragraph(TEXT_JIA, preset_id=preset_id, origin_chat_type="group")
        own_private = await _seed_paragraph(
            TEXT_YI,
            preset_id=preset_id,
            origin_chat_key=CHAT_A2,
            origin_chat_type="private",
        )
        _install_qdrant(monkeypatch, {console_row.id: 0.9, own_group.id: 0.9, own_private.id: 0.9})

        for channel, own in ((group, own_group), (private, own_private)):
            scope = await resolve_memory_scope(channel)
            assert scope.visibility is not None, "闸必须真的开着，否则这条用例测的是另一条路径"
            memories = await retrieve_memories(
                workspace_id=WORKSPACE_ID,
                query="季度预算",
                limit=10,
                scope=scope,
            )
            ids = {memory.target_id for memory in memories}
            assert console_row.id not in ids, f"控制台直发的结论被 {channel.chat_key} 召回了"
            assert own.id in ids, f"{channel.chat_key} 自己的记忆也没读到，上一条断言是空转"

        everything = await retrieve_memories(
            workspace_id=WORKSPACE_ID,
            query="季度预算",
            limit=10,
            scope=MemoryScope.unrestricted(),
        )
        assert console_row.id in {memory.target_id for memory in everything}


# --------------------------------------------------------------------------------------
# 10. 人设维的防线：频道解析出负数时回落到无人设档，而不是沿用或清空
# --------------------------------------------------------------------------------------


class TestNegativePresetIdIsDefused:
    """负数档是控制台专用的标记，不是合法人设。

    一个频道如果解析出负数，说明人设那一维的数据或回退链坏了。沿用它会读走控制台直发的
    结论；而"什么都不放行"会让这个频道永久失忆、且现场只表现为"Bot 突然不记得任何事"。
    正确的退化是当成"没有配置人设"。
    """

    @pytest.mark.parametrize("gate_open", [False, True])
    async def test_negative_preset_falls_back_to_no_preset(
        self,
        scope_db: None,
        monkeypatch: pytest.MonkeyPatch,
        gate_open: bool,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", gate_open)
        warnings: List[str] = []

        class _Recorder:
            def __getattr__(self, _name: str) -> Any:
                return lambda *_a, **_k: None

            def warning(self, message: str, *_a: Any, **_k: Any) -> None:
                warnings.append(message)

        async def fake_resolve_preset_id(_channel: Any) -> int:
            return CONSOLE_ONLY_PRESET_ID

        monkeypatch.setattr(instance_scope_module, "logger", _Recorder())
        monkeypatch.setattr(identity_scope_module, "resolve_preset_id", fake_resolve_preset_id)

        channel = _StubChannel(INSTANCE_A, chat_key=CHAT_A, workspace_id=WORKSPACE_ID)
        scope = await resolve_memory_scope(channel)

        assert scope.preset_id == PRESET_NONE
        assert scope.as_filter()["preset_id"] == PRESET_NONE
        assert any(str(CONSOLE_ONLY_PRESET_ID) in message for message in warnings), "回落必须留痕"
        assert any(CHAT_A in message for message in warnings), "告警要带上出问题的频道"
