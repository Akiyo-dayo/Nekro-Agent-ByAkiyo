"""LLM 用量计量：记账点、金额、归属、作用域

## 这份测试要防的是什么

要解决的是「算不出每个客户烧了多少」。修法是一张流水表 + 三个记账点 +
一个带租户作用域的查询出口。这三件事各有各的失效形态，本文件逐条钉：

| 失效形态 | 表现 | 钉在哪 |
|---|---|---|
| 某个记账点被删/被绕开 | 账面上少一条，没有任何报错 | `TestEveryUsageReadPointIsBilled` |
| 失败的那几次不记账 | 上游拒答/空响应**已经计过费**，账面上却是 0 | `TestFailedAttemptsAreStillBilled` |
| fallback 套用主组的倍率 | 两个组单价差一个数量级，账直接算错 | `TestRetryAndFallbackAreDistinguishable` |
| 金额公式出现第二份实现 | 两处算出来的数不一样，对不上账 | `TestCostHasExactlyOneImplementation` |
| 查询漏了租户过滤 | 一个客户看到另一个客户的经营数据 | `TestUsageQueryIsTenantScoped` |
| 模型组名原样吐给租户 | 绕开 `can_view_group`「看不到的组根本不出现」 | `TestUsageQueryIsTenantScoped` |
| 记账失败把回复也带崩 | 数据库抖一下，Bot 就不说话了 | `TestBillingNeverBreaksTheMainPath` |

## `gen_openai_chat_stream` 的两条判据

`TestSimpleStreamHasTheSameGates` 是 #57 的行为侧。它与 `gen_openai_chat_response`
的那两条分支共用同一套判据（同一个常量集合、同一个救回函数、同一组异常类），
所以这里的每一条都能在"只修了一半"的实现上变红。

## 变异表（把每一处判据改回旧行为 / 改坏，看用例会不会红）

驱动器在 scratchpad 的 `usage_mutate.py`（逐处字符串替换 + 逐处还原，不做整文件写回）。

| # | 变异 | 结果 |
|---|---|---|
| 1 | 删掉 `gen_openai_chat_response` 成功侧的 `record_llm_usage` | KILLED |
| 2 | 删掉 `gen_openai_chat_response` 失败侧的 `record_llm_usage` | KILLED |
| 3 | 非流式 `res.usage` 挪回空正文闸门**之后**（改造前的写法） | KILLED |
| 4 | 流式 `chunk.usage` 挪回空 delta 早退**之后**（改造前的写法） | KILLED |
| 5 | `gen_openai_chat_stream` 不要 `stream_options={"include_usage": True}` | KILLED |
| 6 | `gen_openai_chat_stream` 的 `finally: _record()` 删掉 | KILLED |
| 7 | `gen_openai_chat_stream` 的 `finish_reason` 挪到空 delta 早退之后 | KILLED |
| 8 | `gen_openai_chat_stream` 的空正文闸门删掉 | KILLED |
| 9 | `gen_openai_chat_stream` 的内容拦截闸门删掉 | KILLED |
| 10 | `gen_openai_chat_stream` 的 reasoning 救回删掉 | KILLED |
| 11 | `compute_token_cost` 改公式（补全项不再乘 `input_rate`） | KILLED |
| 12 | `price_consumption` 自己再算一遍（同一判据的第二处实现） | KILLED |
| 13 | `send_agent_request` 的倍率固定取主模型组 | KILLED |
| 14 | `for_attempt` 不带尝试序号与 fallback 标记 | KILLED |
| 15 | `UsageScope.records()` 去掉租户过滤 | KILLED |
| 16 | `resolve_usage_scope` 去掉 `assert_instance_accessible` | KILLED |
| 17 | `resolve_usage_scope` 去掉 `assert_tenant_accessible` | KILLED |
| 18 | `visible_model_group_names` 退回恒等映射（不掩码） | KILLED |
| 19 | `_write_record` 的兜底改成 `raise` | KILLED（**第一轮存活**，见下） |
| 20 | 归属解析不再按 `chat_key` 查频道行 | KILLED |
| 21 | `_normalize_row` 不再把 `Sum` 的 `None` 补 0 | KILLED（第一版变异瞄错了列，见下） |
| 22 | 默认时间窗右端退回裸 `db_now()` | KILLED |
| 23 | 记账不再读当前 contextvar（归属整体丢失） | KILLED |
| 24 | 流式生成器被丢弃时记成 `success` 而不是 `aborted` | KILLED |
| 25 | 把迁移文件拿走（模型登记了、建表语句没有） | KILLED |

**第一轮的两个非 KILLED，各自的处理不同：**

- **#19 是真的覆盖缺口。** 落库任务是**脱手的**（`create_task` 之后没人 await），
  所以「回复照常返回 + `failed` 计数 +1」这两条断言对「异常被兜住了」与
  「异常被抛进一个没人接的任务里」**分不出来**。已把判据落到任务自己身上
  （`tasks[0].exception() is None`），第二轮 KILLED。
- **#21 是变异本身瞄错了列（等价变异，不是覆盖缺口）。** `Count(...)` 在空结果集上
  回的是 0 而不是 NULL，所以改 `agg_records` 那一行任何实现都分辨不出来；
  真正会回 NULL 的是 `Sum(...)`。变异改到 `agg_cost` 之后 KILLED。

"""

from __future__ import annotations

import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import httpx
import pytest
from tortoise import Tortoise

from nekro_agent.core.config import ModelConfigGroup
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_instance_group import DBInstanceDomainBinding, DBInstanceGroup
from nekro_agent.models.db_llm_usage import DBLLMUsageRecord
from nekro_agent.models.db_model_group import DBModelGroup, ModelGroupVisibility
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.console.usage import (
    get_usage_summary,
    get_usage_trend,
    list_usage_records,
)
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.tenant import ModelGroupOwnership, TenantRole
from nekro_agent.schemas.usage import LLMUsageScene, LLMUsageStatus
from nekro_agent.services.agent import openai as openai_module
from nekro_agent.services.agent import run_agent as run_agent_module
from nekro_agent.services.agent.openai import (
    EmptyLLMResponseError,
    LLMContentBlockedError,
    LLMMalformedToolCallError,
    OpenAIResponse,
    gen_openai_chat_response,
    gen_openai_chat_stream,
    gen_openai_embeddings,
)
from nekro_agent.services.agent.run_agent import send_agent_request
from nekro_agent.services.usage import (
    LLMUsageContext,
    compute_token_cost,
    flush_llm_usage,
    record_llm_usage,
    reset_usage_recorder,
    usage_recorder_stats,
    usage_scope,
)
from nekro_agent.services.usage.query import HIDDEN_MODEL_GROUP_LABEL
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2
ACME_INSTANCE = "acme1"
RIVAL_INSTANCE = "rival1"
ACME_CHAT_KEY = f"{ADAPTER}-{ACME_INSTANCE}-group_1001"


# ---------------------------------------------------------------------------
# 替身上游：只替换 openai SDK 客户端本身，本模块自己的判据逻辑一行不动
# ---------------------------------------------------------------------------


class _FakeDelta:
    def __init__(self, content=None, reasoning_content=None) -> None:
        self.content = content
        self.reasoning_content = reasoning_content


class _FakeChoice:
    def __init__(self, delta=None, message=None, finish_reason=None) -> None:
        self.delta = delta
        self.message = message
        self.finish_reason = finish_reason


class _FakeUsage:
    def __init__(self, total=30, prompt=20, completion=10) -> None:
        self.total_tokens = total
        self.prompt_tokens = prompt
        self.completion_tokens = completion


class _FakeChunk:
    def __init__(self, *, content=None, reasoning=None, usage=None, finish_reason=None, no_choices=False) -> None:
        self.choices = [] if no_choices else [_FakeChoice(delta=_FakeDelta(content, reasoning), finish_reason=finish_reason)]
        self.usage = usage


class _FakeStream:
    """替身流

    **迭代器只能有一个**：`gen_openai_chat_response` 先用 `__aiter__().__anext__()` 取首块
    （为了给首字超时单独计时），再用 `async for` 接着消费剩下的。每次 `__aiter__` 都新建
    生成器的话首块会被消费两次——§2.9 那条「fake 与被测代码的真实约定脱靶」的同款陷阱。
    """

    def __init__(self, chunks) -> None:
        self._chunks = list(chunks)
        self._iter = None
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.closed = True
        return False

    def __aiter__(self):
        if self._iter is None:

            async def _gen():
                for chunk in self._chunks:
                    yield chunk

            self._iter = _gen()
        return self._iter


class _FakeCompletion:
    def __init__(self, *, content, reasoning=None, usage=None, finish_reason=None) -> None:
        message = SimpleNamespace(content=content, reasoning_content=reasoning)
        self.choices = [_FakeChoice(message=message, finish_reason=finish_reason)]
        self.usage = usage if usage is not None else _FakeUsage()


class _FakeUpstream:
    """记录每一次真正发出去的请求参数，并按模型名给出不同的响应"""

    def __init__(self, behaviour) -> None:
        self.behaviour = behaviour
        self.calls: List[Dict[str, Any]] = []

    def install(self, monkeypatch) -> "_FakeUpstream":
        upstream = self

        class _Completions:
            async def create(self, **kwargs):
                upstream.calls.append(kwargs)
                result = upstream.behaviour(kwargs)
                if isinstance(result, Exception):
                    raise result
                if kwargs.get("stream"):
                    return _FakeStream(result)
                return result

        class _FakeClient:
            def __init__(self, **_kwargs) -> None:
                self.chat = SimpleNamespace(completions=_Completions())

        monkeypatch.setattr(openai_module, "AsyncOpenAI", _FakeClient)
        monkeypatch.setattr(openai_module, "_get_http_client", lambda **_kwargs: object())
        return self


def _install_upstream(monkeypatch, behaviour) -> _FakeUpstream:
    return _FakeUpstream(behaviour).install(monkeypatch)


# ---------------------------------------------------------------------------
# 固件
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    reset_usage_recorder()
    try:
        await DBTenant.create(id=ACME_TENANT_ID, name="acme", display_name="Acme")
        await DBTenant.create(id=RIVAL_TENANT_ID, name="rival", display_name="Rival")
        for instance_key, tenant_id in ((ACME_INSTANCE, ACME_TENANT_ID), (RIVAL_INSTANCE, RIVAL_TENANT_ID)):
            await DBAdapterInstance.create(
                adapter_key=ADAPTER,
                instance_key=instance_key,
                display_name=instance_key,
                status="online",
                provider="remote_onebot",
                tenant_id=tenant_id,
                metadata_json="",
            )
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=ACME_INSTANCE,
            channel_id="group_1001",
            chat_key=ACME_CHAT_KEY,
            channel_type="group",
            data="{}",
        )
        yield
    finally:
        reset_usage_recorder()
        await Tortoise.close_connections()


@pytest.fixture()
def _isolated_log_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(run_agent_module, "PROMPT_LOG_DIR", str(tmp_path / "prompts"))
    monkeypatch.setattr(run_agent_module, "PROMPT_ERROR_LOG_DIR", str(tmp_path / "prompts_error"))


def make_user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    """构造一个未落库的登录身份；作用域判定只读这几个字段

    `id` 必须显式给一个：`model_group_service.resolve_console_identity` 会把它塞进
    `CurrentIdentity.user_id`（`int`，不可为空），未落库的行上它是 `None`，
    于是模型组可见性那一路会以一条 pydantic 校验错误收场——而那与本文件要测的东西无关。
    """
    return DBUser(
        id=abs(hash((tenant_id, tenant_role.value))) % 100000 + 1,
        username=f"user-{tenant_role.value}-{tenant_id}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )


def acme_admin() -> DBUser:
    return make_user(tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def rival_admin() -> DBUser:
    return make_user(tenant_id=RIVAL_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def platform_owner() -> DBUser:
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


def _group(
    name: str,
    model: str,
    *,
    input_rate: float = 1.0,
    completion_rate: float = 1.0,
    price_rate: float = 1.0,
) -> ModelConfigGroup:
    return ModelConfigGroup(
        GROUP_NAME=name,
        CHAT_MODEL=model,
        BASE_URL="https://upstream.invalid/v1",
        API_KEY="sk-not-a-real-key",
        MODEL_TYPE="chat",
        ENABLE_VISION=False,
        TOKEN_INPUT_RATE=input_rate,
        TOKEN_COMPLETION_RATE=completion_rate,
        MODEL_PRICE_RATE=price_rate,
    )


def _make_config(groups: Dict[str, ModelConfigGroup], primary: str, fallback: str) -> SimpleNamespace:
    return SimpleNamespace(
        MODEL_GROUPS=groups,
        USE_MODEL_GROUP=primary,
        DEBUG_MIGRATION_MODEL_GROUP="",
        FALLBACK_MODEL_GROUP=fallback,
        SAVE_PROMPTS_LOG=False,
        AI_CHAT_LLM_API_MAX_RETRIES=3,
        AI_REQUEST_STREAM_MODE=False,
        AI_GENERATE_TIMEOUT=180,
        AI_STREAM_FIRST_TOKEN_TIMEOUT=30,
    )


async def fetch_summary(
    user: DBUser,
    *,
    instance_key: str = "",
    tenant_id: Optional[int] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
):
    """直接调端点函数时**每个查询参数都要显式传**

    FastAPI 的 `Query(default=...)` 只在请求解析时生效；直接调用时不传就会把
    `Query` 对象本身当成取值传下去（实测表现是"实例标识 = 一整个 FieldInfo 的 repr"，
    而端点照样跑完、只是恒 404）。这三个 wrapper 就是那道防线。
    """
    return await get_usage_summary(
        instance_key=instance_key,
        tenant_id=tenant_id,
        start_time=start_time,
        end_time=end_time,
        _current_user=user,
    )


async def fetch_trend(
    user: DBUser,
    *,
    instance_key: str = "",
    tenant_id: Optional[int] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    bucket_hours: int = 24,
):
    return await get_usage_trend(
        instance_key=instance_key,
        tenant_id=tenant_id,
        start_time=start_time,
        end_time=end_time,
        bucket_hours=bucket_hours,
        _current_user=user,
    )


async def fetch_records(
    user: DBUser,
    *,
    instance_key: str = "",
    tenant_id: Optional[int] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    scene: str = "",
    status: str = "",
    page: int = 1,
    page_size: int = 50,
):
    return await list_usage_records(
        instance_key=instance_key,
        tenant_id=tenant_id,
        start_time=start_time,
        end_time=end_time,
        scene=scene,
        status=status,
        page=page,
        page_size=page_size,
        _current_user=user,
    )


async def _rows() -> List[DBLLMUsageRecord]:
    await flush_llm_usage()
    return await DBLLMUsageRecord.all().order_by("id")


async def _seed_row(**overrides: Any) -> DBLLMUsageRecord:
    """直接造一行流水（读侧的用例不需要真的打一次上游）"""
    payload: Dict[str, Any] = {
        "adapter_key": ADAPTER,
        "instance_key": ACME_INSTANCE,
        "domain_id": f"i:{ADAPTER}:{ACME_INSTANCE}",
        "tenant_id": ACME_TENANT_ID,
        "chat_key": ACME_CHAT_KEY,
        "model_group_name": "platform-shared",
        "model_name": "gpt-4o",
        "token_input": 100,
        "token_output": 10,
        "token_total": 110,
        "token_input_rate": 1.0,
        "token_completion_rate": 1.0,
        "model_price_rate": 1.0,
        "cost": 110.0,
        "scene": LLMUsageScene.CHAT.value,
        "status": LLMUsageStatus.SUCCESS.value,
    }
    payload.update(overrides)
    return await DBLLMUsageRecord.create(**payload)


# ---------------------------------------------------------------------------
# 1. 金额只有一份实现
# ---------------------------------------------------------------------------


class TestCostHasExactlyOneImplementation:
    """`DBModelGroup.token_input_rate` / `token_completion_rate` 两列此前全仓零消费点

    现在它们是唯一的计费依据，所以口径必须钉死：`token_input_rate` 同时乘在补全那一项上
    （`token_completion_rate` 的语义是"补全相对输入的倍数"，不是绝对单价），
    这是上游既有的公式，改它等于把所有存量模型组的账重算一遍。
    """

    @pytest.mark.parametrize(
        ("token_input", "token_output", "input_rate", "completion_rate", "price_rate", "expected"),
        [
            # 全 1.0（也是数据库默认值）：费用 = 总 token 数
            (100, 10, 1.0, 1.0, 1.0, 110.0),
            # 只有输入倍率
            (100, 10, 2.0, 1.0, 1.0, 220.0),
            # 补全倍率是**相对输入**的倍数：10 * 2.0 * 3.0 = 60
            (100, 10, 2.0, 3.0, 1.0, 260.0),
            # 价格倍率乘在最外层
            (100, 10, 2.0, 3.0, 0.5, 130.0),
            # 零 token（网络层就失败的那些）恒为 0，不因为倍率而凭空长出钱来
            (0, 0, 7.0, 9.0, 11.0, 0.0),
            # 只有输出（reasoning-only 的那一类：prompt 计在上游、completion 才是我们付的）
            (0, 40, 1.5, 2.0, 1.0, 120.0),
        ],
    )
    def test_formula_is_pinned(
        self,
        token_input: int,
        token_output: int,
        input_rate: float,
        completion_rate: float,
        price_rate: float,
        expected: float,
    ) -> None:
        assert compute_token_cost(
            token_input,
            token_output,
            input_rate=input_rate,
            completion_rate=completion_rate,
            price_rate=price_rate,
        ) == pytest.approx(expected)

    def test_price_consumption_delegates_to_the_shared_formula(self) -> None:
        """`OpenAIResponse.price_consumption` 与账本必须**逐字**给出同一个数

        两处各留一份实现正是本项目栽过六次的形态；这条用例是那道闸门。
        """
        response = OpenAIResponse(
            response_content="hi",
            thought_chain="",
            messages=[],
            message_cnt=1,
            token_consumption=110,
            token_input=100,
            token_output=10,
            use_model="m",
            speed_tokens_per_second=1.0,
            first_token_cost_ms=1,
            generation_time_ms=1,
            stream_mode=False,
        )
        assert response.price_consumption(2.0, 3.0, 0.5) == pytest.approx(
            compute_token_cost(100, 10, input_rate=2.0, completion_rate=3.0, price_rate=0.5),
        )


# ---------------------------------------------------------------------------
# 2. 三个 usage 读取处都记账
# ---------------------------------------------------------------------------


class TestEveryUsageReadPointIsBilled:
    """`services/agent/openai.py` 里拿到 usage 的地方有**四**处，每处各一条用例

    ① `gen_openai_chat_response` 非流式（`res.usage`）
    ② `gen_openai_chat_response` 流式（`chunk.usage` 累加）
    ③ `gen_openai_chat_stream`（本轮才接上，此前连 `include_usage` 都没要）
    ④ `gen_openai_embeddings`（见 `TestEmbeddingsAreBilledToo`）
    """

    async def test_non_stream_success_is_billed(self, db, monkeypatch) -> None:
        _install_upstream(monkeypatch, lambda _kw: _FakeCompletion(content="hello", usage=_FakeUsage(30, 20, 10)))
        context = LLMUsageContext(scene=LLMUsageScene.CHAT, chat_key=ACME_CHAT_KEY).with_model_group(
            "grp",
            _group("grp", "m", input_rate=2.0, completion_rate=3.0, price_rate=1.0),
        )
        with usage_scope(context):
            await gen_openai_chat_response(model="m", messages=[], stream_mode=False)

        rows = await _rows()
        assert len(rows) == 1
        row = rows[0]
        assert (row.token_input, row.token_output, row.token_total) == (20, 10, 30)
        assert row.status == LLMUsageStatus.SUCCESS.value
        assert row.stream_mode is False
        assert row.model_name == "m"
        assert row.model_group_name == "grp"
        # 20*2 + 10*2*3 = 100
        assert row.cost == pytest.approx(100.0)

    async def test_stream_success_is_billed(self, db, monkeypatch) -> None:
        """流式的 usage 只在收尾块上，而收尾块常常没有 choices——漏了它账就恒为 0"""
        _install_upstream(
            monkeypatch,
            lambda _kw: [
                _FakeChunk(content="he"),
                _FakeChunk(content="llo"),
                _FakeChunk(no_choices=True, usage=_FakeUsage(90, 60, 30)),
            ],
        )
        with usage_scope(LLMUsageContext(scene=LLMUsageScene.CHAT, chat_key=ACME_CHAT_KEY)):
            response = await gen_openai_chat_response(model="m", messages=[], stream_mode=True)
        assert response.response_content == "hello"

        rows = await _rows()
        assert len(rows) == 1
        assert (rows[0].token_input, rows[0].token_output, rows[0].token_total) == (60, 30, 90)
        assert rows[0].stream_mode is True
        assert rows[0].status == LLMUsageStatus.SUCCESS.value

    async def test_simple_stream_success_is_billed(self, db, monkeypatch) -> None:
        """第三个入口（WebUI 实时代码生成）。改造前这条链路的花销一分钱都算不出来"""
        _install_upstream(
            monkeypatch,
            lambda _kw: [_FakeChunk(content="print(1)"), _FakeChunk(no_choices=True, usage=_FakeUsage(50, 40, 10))],
        )
        with usage_scope(LLMUsageContext(scene=LLMUsageScene.PLUGIN_GENERATE).with_model_group("g", _group("g", "m"))):
            chunks = [chunk async for chunk in gen_openai_chat_stream(model="m", messages=[])]
        assert chunks == ["print(1)"]

        rows = await _rows()
        assert len(rows) == 1
        assert (rows[0].token_input, rows[0].token_output) == (40, 10)
        assert rows[0].scene == LLMUsageScene.PLUGIN_GENERATE.value
        assert rows[0].status == LLMUsageStatus.SUCCESS.value

    async def test_simple_stream_asks_for_usage(self, db, monkeypatch) -> None:
        """`include_usage` 是这条链路能记账的**前提**

        不要它，上游根本不会在流末尾补那个只带 usage 的块——记账代码写得再全，
        两个 token 列也永远是 0。这条判据必须单独钉，因为它在别的用例里被替身兜住了。
        """
        upstream = _install_upstream(
            monkeypatch,
            lambda _kw: [_FakeChunk(content="x"), _FakeChunk(no_choices=True, usage=_FakeUsage())],
        )
        async for _ in gen_openai_chat_stream(model="m", messages=[]):
            pass
        assert upstream.calls[0]["stream_options"] == {"include_usage": True}

    async def test_simple_stream_abort_is_billed_as_aborted(self, db, monkeypatch) -> None:
        """消费方提前丢弃生成器：token 已经烧了，但它既不是成功也不是失败

        混进 `FAILED` 会让"失败率"这个运营指标失真，所以单列一档。
        """
        _install_upstream(
            monkeypatch,
            lambda _kw: [
                _FakeChunk(content="a", usage=_FakeUsage(7, 5, 2)),
                _FakeChunk(content="b"),
                _FakeChunk(content="c"),
            ],
        )
        generator = gen_openai_chat_stream(model="m", messages=[])
        assert await generator.__anext__() == "a"
        await generator.aclose()

        rows = await _rows()
        assert len(rows) == 1
        assert rows[0].status == LLMUsageStatus.ABORTED.value
        assert (rows[0].token_input, rows[0].token_output) == (5, 2)


# ---------------------------------------------------------------------------
# 3. 失败的那几次也是真金白银
# ---------------------------------------------------------------------------


class TestFailedAttemptsAreStillBilled:
    """上游 2xx 但正文为空/被拦下的那一类**已经计过费**

    实测有中转站在 `completion_tokens=0` 而 `reasoning_tokens=1116` 时照样计费
    （见 `EmptyLLMResponseError` 的 docstring）。这一类若不记账，账面上会凭空少掉
    "上游今天抽风的那两小时"——而那恰恰是花钱最多、最需要看见的两小时。
    """

    async def test_blocked_non_stream_still_bills_the_tokens(self, db, monkeypatch) -> None:
        """这条同时钉住「usage 要在闸门**之前**读」：挪回去 token 就是 0"""
        _install_upstream(
            monkeypatch,
            lambda _kw: _FakeCompletion(
                content=None,
                finish_reason="PROHIBITED_CONTENT",
                usage=_FakeUsage(1200, 1000, 200),
            ),
        )
        with usage_scope(LLMUsageContext(scene=LLMUsageScene.CHAT, chat_key=ACME_CHAT_KEY)), pytest.raises(
            LLMContentBlockedError,
        ):
            await gen_openai_chat_response(model="m", messages=[], stream_mode=False)

        rows = await _rows()
        assert len(rows) == 1
        assert rows[0].status == LLMUsageStatus.FAILED.value
        assert (rows[0].token_input, rows[0].token_output) == (1000, 200)
        assert rows[0].error_type == "LLMContentBlockedError"
        # 归一化后的取值，且属于固定集合——不含任何上游原文
        assert rows[0].finish_reason == "prohibited_content"

    async def test_empty_stream_still_bills_what_arrived(self, db, monkeypatch) -> None:
        _install_upstream(
            monkeypatch,
            lambda _kw: [_FakeChunk(content=""), _FakeChunk(no_choices=True, usage=_FakeUsage(1116, 0, 1116))],
        )
        with usage_scope(LLMUsageContext(scene=LLMUsageScene.CHAT, chat_key=ACME_CHAT_KEY)), pytest.raises(
            EmptyLLMResponseError,
        ):
            await gen_openai_chat_response(model="m", messages=[], stream_mode=True)

        rows = await _rows()
        assert len(rows) == 1
        assert rows[0].status == LLMUsageStatus.FAILED.value
        assert rows[0].token_output == 1116
        assert rows[0].error_type == "EmptyLLMResponseError"

    async def test_network_failure_is_recorded_with_zero_tokens(self, db, monkeypatch) -> None:
        """网络层就挂了拿不到 usage，两个 token 列是 0——**但那一行仍然要在**

        「这个实例今天失败了 400 次」本身就是运营信号，而且失败次数与成功次数的比值
        是判断"要不要换上游"的唯一依据。
        """
        _install_upstream(monkeypatch, lambda _kw: TimeoutError("upstream gone"))
        with usage_scope(LLMUsageContext(scene=LLMUsageScene.CHAT, chat_key=ACME_CHAT_KEY)), pytest.raises(
            TimeoutError,
        ):
            await gen_openai_chat_response(model="m", messages=[], stream_mode=False)

        rows = await _rows()
        assert len(rows) == 1
        assert rows[0].status == LLMUsageStatus.FAILED.value
        assert (rows[0].token_input, rows[0].token_output, rows[0].cost) == (0, 0, 0.0)
        assert rows[0].error_type == "TimeoutError"

    async def test_simple_stream_failure_is_recorded(self, db, monkeypatch) -> None:
        _install_upstream(monkeypatch, lambda _kw: RuntimeError("boom"))
        with pytest.raises(RuntimeError), usage_scope(LLMUsageContext(scene=LLMUsageScene.PLUGIN_GENERATE)):
            async for _ in gen_openai_chat_stream(model="m", messages=[]):
                pass

        rows = await _rows()
        assert len(rows) == 1
        assert rows[0].status == LLMUsageStatus.FAILED.value
        assert rows[0].error_type == "RuntimeError"


# ---------------------------------------------------------------------------
# 4. 重试 / fallback 要能分开看，而且各按各的倍率算
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_isolated_log_dirs")
class TestRetryAndFallbackAreDistinguishable:
    """一轮对话可能发出 N 次请求，每一次都真金白银

    这一组不打桩 `gen_openai_chat_response`（那样会把记账整条绕过去），
    而是替换 openai SDK 客户端本身，让 `send_agent_request` 的完整链路真的跑一遍。
    """

    async def test_each_attempt_lands_its_own_row(self, db, monkeypatch) -> None:
        """`build_attempt_plan` 是「前 N-1 次主组，最后一次备用组」，这里 max_retries=3

        所以主组失败两次、备用组成功一次，账上恰好三行，且三行各自可辨识。
        """
        primary = _group("p", "primary-model", input_rate=1.0)
        fallback = _group("f", "fallback-model", input_rate=1.0)
        config = _make_config({"p": primary, "f": fallback}, "p", "f")

        def behaviour(kwargs):
            if kwargs["model"] == "primary-model":
                return TimeoutError("primary down")
            return _FakeCompletion(content="ok", usage=_FakeUsage(30, 20, 10))

        _install_upstream(monkeypatch, behaviour)
        await send_agent_request([], config, chat_key=ACME_CHAT_KEY)  # type: ignore[arg-type]

        rows = await _rows()
        assert [(row.attempt_index, row.model_name, row.status, row.is_fallback) for row in rows] == [
            (1, "primary-model", LLMUsageStatus.FAILED.value, False),
            (2, "primary-model", LLMUsageStatus.FAILED.value, False),
            (3, "fallback-model", LLMUsageStatus.SUCCESS.value, True),
        ]
        # 计划的尝试总数落在每一行上，否则"第 3 次"这个数字读不出分母
        assert {row.attempt_total for row in rows} == {3}
        assert {row.scene for row in rows} == {LLMUsageScene.CHAT.value}
        # 归属由 chat_key 解析出来，三行都落在同一个实例/租户上
        assert {(row.instance_key, row.tenant_id) for row in rows} == {(ACME_INSTANCE, ACME_TENANT_ID)}

    async def test_fallback_attempt_is_billed_at_the_fallback_rate(self, db, monkeypatch) -> None:
        """两个组的单价往往差一个数量级，套用主组的倍率就是直接算错账"""
        primary = _group("p", "primary-model", input_rate=1.0, completion_rate=1.0, price_rate=1.0)
        fallback = _group("f", "fallback-model", input_rate=10.0, completion_rate=2.0, price_rate=1.0)
        config = _make_config({"p": primary, "f": fallback}, "p", "f")

        def behaviour(kwargs):
            if kwargs["model"] == "primary-model":
                return _FakeCompletion(content=None, usage=_FakeUsage(60, 50, 10))
            return _FakeCompletion(content="ok", usage=_FakeUsage(30, 20, 10))

        _install_upstream(monkeypatch, behaviour)
        await send_agent_request([], config, chat_key=ACME_CHAT_KEY)  # type: ignore[arg-type]

        rows = await _rows()
        assert len(rows) == 3
        assert [row.model_group_name for row in rows] == ["p", "p", "f"]
        # 前两次（主组，1.0/1.0/1.0）：50 + 10 = 60。**空响应照样计费**，见类文档
        assert rows[0].cost == pytest.approx(60.0)
        assert rows[1].cost == pytest.approx(60.0)
        # 第三次（备用组，10.0/2.0/1.0）：20*10 + 10*10*2 = 400
        assert rows[2].cost == pytest.approx(400.0)
        # 倍率快照随行落库：日后运维改倍率，历史账单不会被改写
        assert (rows[2].token_input_rate, rows[2].token_completion_rate) == (10.0, 2.0)
        assert (rows[0].token_input_rate, rows[0].token_completion_rate) == (1.0, 1.0)

    async def test_a_round_that_never_succeeds_still_bills_every_attempt(self, db, monkeypatch) -> None:
        primary = _group("p", "primary-model")
        config = _make_config({"p": primary}, "p", "p")
        _install_upstream(monkeypatch, lambda _kw: TimeoutError("dead"))

        with pytest.raises(run_agent_module.AllLLMRequestsFailedError):
            await send_agent_request([], config, chat_key=ACME_CHAT_KEY)  # type: ignore[arg-type]

        rows = await _rows()
        # 尝试总数由 `build_attempt_plan(max_retries=3)` 决定，账上一次不漏
        assert len(rows) == 3
        assert {row.status for row in rows} == {LLMUsageStatus.FAILED.value}
        assert [row.attempt_index for row in rows] == [1, 2, 3]
        assert {row.error_type for row in rows} == {"TimeoutError"}


# ---------------------------------------------------------------------------
# 5. 归属复用 identity_scope
# ---------------------------------------------------------------------------


class TestAttributionReusesIdentityScope:
    async def test_attribution_is_resolved_from_the_chat_key(self, db, monkeypatch) -> None:
        """调用方只声明 `chat_key`，实例 / 域 / 租户由写侧解析（后台任务，不占主链路）

        **不解析 `chat_key` 字符串**：实例段的规则是适配器私有的（§1.1），
        走的是频道行上的 `(adapter_key, instance_key)`。
        """
        _install_upstream(monkeypatch, lambda _kw: _FakeCompletion(content="hi"))
        with usage_scope(LLMUsageContext(scene=LLMUsageScene.CHAT, chat_key=ACME_CHAT_KEY)):
            await gen_openai_chat_response(model="m", messages=[], stream_mode=False)

        row = (await _rows())[0]
        assert (row.adapter_key, row.instance_key) == (ADAPTER, ACME_INSTANCE)
        assert row.tenant_id == ACME_TENANT_ID
        # 零配置 = 自成一域，编码由 `ScopeDomain.domain_id` 产生
        assert row.domain_id == f"i:{ADAPTER}:{ACME_INSTANCE}"

    async def test_domain_follows_the_instance_group(self, db, monkeypatch) -> None:
        """实例被编进域之后，流水上的域跟着变，但**计费单元仍然是实例本身**

        这正是不用 `ScopeDomain.as_columns()` 的原因：那套编码在属于实例组时会把
        `adapter_key` / `instance_key` 写成空串，账就算不回到具体哪台 Bot 头上了。
        """
        group = await DBInstanceGroup.create(name="acme-domain", note="", tenant_id=ACME_TENANT_ID)
        await DBInstanceDomainBinding.create(
            adapter_key=ADAPTER,
            instance_key=ACME_INSTANCE,
            memory_group_id=group.id,
        )
        _install_upstream(monkeypatch, lambda _kw: _FakeCompletion(content="hi"))
        with usage_scope(LLMUsageContext(scene=LLMUsageScene.CHAT, chat_key=ACME_CHAT_KEY)):
            await gen_openai_chat_response(model="m", messages=[], stream_mode=False)

        row = (await _rows())[0]
        assert row.domain_id == f"g:{group.id}"
        assert (row.adapter_key, row.instance_key) == (ADAPTER, ACME_INSTANCE)
        assert row.tenant_id == ACME_TENANT_ID

    async def test_no_declaration_lands_in_the_unattributed_bucket(self, db, monkeypatch) -> None:
        """没有任何调用方声明归属时**照样记账**，只是算不到人头上

        这一档在平台方的用量页上独立可见：它涨起来就说明有一条链路没接上记账。
        静默丢弃的话，同样的缺陷是不可发现的。
        """
        _install_upstream(monkeypatch, lambda _kw: _FakeCompletion(content="hi", usage=_FakeUsage(9, 6, 3)))
        await gen_openai_chat_response(model="m", messages=[], stream_mode=False)

        row = (await _rows())[0]
        assert row.scene == LLMUsageScene.UNATTRIBUTED.value
        assert (row.instance_key, row.tenant_id, row.chat_key) == ("", None, "")
        assert row.token_total == 9


# ---------------------------------------------------------------------------
# 6. 查询出口的作用域
# ---------------------------------------------------------------------------


class TestUsageQueryIsTenantScoped:
    """平台方看全部；租户只看自己的，越权 **404**（不是 403）

    403 会告诉租户"这个实例/租户确实存在"，等于把平台方的清单变成可枚举的。
    """

    async def test_platform_owner_sees_everything(self, db) -> None:
        await _seed_row(cost=10.0)
        await _seed_row(instance_key=RIVAL_INSTANCE, tenant_id=RIVAL_TENANT_ID, cost=30.0)

        summary = await fetch_summary(platform_owner())
        assert summary.total.records == 2
        assert summary.total.cost == pytest.approx(40.0)
        assert {item.key for item in summary.by_instance} == {ACME_INSTANCE, RIVAL_INSTANCE}

    async def test_tenant_only_sees_its_own_rows(self, db) -> None:
        await _seed_row(cost=10.0)
        await _seed_row(instance_key=RIVAL_INSTANCE, tenant_id=RIVAL_TENANT_ID, cost=30.0)
        # 平台方自己的那些行（tenant_id IS NULL）对租户同样不可见
        await _seed_row(instance_key="", tenant_id=None, scene=LLMUsageScene.PLUGIN_GENERATE.value, cost=99.0)

        summary = await fetch_summary(acme_admin())
        assert summary.total.records == 1
        assert summary.total.cost == pytest.approx(10.0)
        assert [item.key for item in summary.by_instance] == [ACME_INSTANCE]

        records = await fetch_records(acme_admin())
        assert records.total == 1
        assert records.items[0].instance_key == ACME_INSTANCE

    async def test_tenant_asking_for_a_foreign_instance_gets_404(self, db) -> None:
        await _seed_row(instance_key=RIVAL_INSTANCE, tenant_id=RIVAL_TENANT_ID)
        with pytest.raises(NotFoundError):
            await fetch_summary(acme_admin(), instance_key=RIVAL_INSTANCE)
        with pytest.raises(NotFoundError):
            await fetch_records(acme_admin(), instance_key=RIVAL_INSTANCE)
        with pytest.raises(NotFoundError):
            await fetch_trend(acme_admin(), instance_key=RIVAL_INSTANCE)

    async def test_platform_owner_can_read_that_same_instance(self, db) -> None:
        """§2.11 的正向对照：上面那三条 404 不是因为"这条路径谁都走不通" """
        await _seed_row(instance_key=RIVAL_INSTANCE, tenant_id=RIVAL_TENANT_ID, cost=30.0)
        summary = await fetch_summary(platform_owner(), instance_key=RIVAL_INSTANCE)
        assert summary.total.records == 1
        assert summary.total.cost == pytest.approx(30.0)

    async def test_the_owning_tenant_can_read_its_own_instance(self, db) -> None:
        await _seed_row(cost=10.0)
        summary = await fetch_summary(acme_admin(), instance_key=ACME_INSTANCE)
        assert summary.total.records == 1

    async def test_tenant_asking_for_a_foreign_tenant_gets_404(self, db) -> None:
        await _seed_row(instance_key=RIVAL_INSTANCE, tenant_id=RIVAL_TENANT_ID)
        with pytest.raises(NotFoundError):
            await fetch_summary(acme_admin(), tenant_id=RIVAL_TENANT_ID)
        # 平台方查同一个租户是正常的
        summary = await fetch_summary(platform_owner(), tenant_id=RIVAL_TENANT_ID)
        assert summary.total.records == 1

    async def test_a_nonexistent_tenant_is_404_for_the_platform_owner_too(self, db) -> None:
        with pytest.raises(NotFoundError):
            await fetch_summary(platform_owner(), tenant_id=999)

    async def test_rival_admin_sees_only_its_own(self, db) -> None:
        await _seed_row(cost=10.0)
        await _seed_row(instance_key=RIVAL_INSTANCE, tenant_id=RIVAL_TENANT_ID, cost=30.0)
        summary = await fetch_summary(rival_admin())
        assert summary.total.cost == pytest.approx(30.0)
        assert [item.key for item in summary.by_instance] == [RIVAL_INSTANCE]


class TestModelGroupNamesAreMaskedForTenants:
    """`can_view_group` 的契约是「租户看不到的组根本不出现」

    用量页要是原样吐出 `model_group_name`，这条不变式就从另一扇门被绕开了：
    租户的会话跑在平台方的组上，那个组名会直接出现在他的账单里。
    """

    @staticmethod
    async def _seed_groups() -> None:
        await DBModelGroup.create(
            name="platform-private",
            ownership=ModelGroupOwnership.PLATFORM.value,
            owner_tenant_id=None,
            visibility=ModelGroupVisibility.PRIVATE.value,
        )
        await DBModelGroup.create(
            name="platform-shared",
            ownership=ModelGroupOwnership.PLATFORM.value,
            owner_tenant_id=None,
            visibility=ModelGroupVisibility.SHARED.value,
        )
        await DBModelGroup.create(
            name="acme-own",
            ownership=ModelGroupOwnership.TENANT.value,
            owner_tenant_id=ACME_TENANT_ID,
            visibility=ModelGroupVisibility.PRIVATE.value,
        )

    async def test_tenant_does_not_see_a_platform_private_group_name(self, db) -> None:
        await self._seed_groups()
        await _seed_row(model_group_name="platform-private")

        summary = await fetch_summary(acme_admin())
        assert [item.key for item in summary.by_model_group] == [HIDDEN_MODEL_GROUP_LABEL]
        records = await fetch_records(acme_admin())
        assert records.items[0].model_group_name == HIDDEN_MODEL_GROUP_LABEL

    async def test_tenant_sees_shared_and_own_group_names(self, db) -> None:
        """掩码不是"一律遮住"：能看见的组照常显示，否则账单就没有明细了"""
        await self._seed_groups()
        await _seed_row(model_group_name="platform-shared")
        await _seed_row(model_group_name="acme-own")

        summary = await fetch_summary(acme_admin())
        assert {item.key for item in summary.by_model_group} == {"platform-shared", "acme-own"}

    async def test_platform_owner_sees_the_real_name(self, db) -> None:
        """§2.11 的正向对照：掩码只对租户生效"""
        await self._seed_groups()
        await _seed_row(model_group_name="platform-private")
        summary = await fetch_summary(platform_owner())
        assert [item.key for item in summary.by_model_group] == ["platform-private"]

    async def test_a_deleted_group_is_masked_rather_than_leaked(self, db) -> None:
        """库里已经没有那一行时归属无从判定，"判不出来就放行"正是要堵的形态"""
        await _seed_row(model_group_name="gone-forever")
        summary = await fetch_summary(acme_admin())
        assert [item.key for item in summary.by_model_group] == [HIDDEN_MODEL_GROUP_LABEL]

    async def test_model_name_is_not_masked(self, db) -> None:
        """模型是计费单位本身，遮掉它等于给了一张没有明细的发票（有意的取舍）"""
        await self._seed_groups()
        await _seed_row(model_group_name="platform-private", model_name="gpt-4o")
        summary = await fetch_summary(acme_admin())
        assert [item.key for item in summary.by_model] == ["gpt-4o"]

    async def test_several_hidden_groups_fold_into_one_row(self, db) -> None:
        """掩码**必然**制造同名行，不合并就是一张读不懂的账单

        平台方配两个组（主 + fallback 是常态）时，租户的账单上会出现两行
        一模一样的「(未公开的模型组)」：数字不同、标签相同，"这两行差在哪"无解；
        前端拿 `key` 当 React 列表键还会撞。这与之前修过的 `display_name`
        撞名是同一形态，只是解法必须相反 —— 那边可以退回唯一的 `name` 把两行分开，
        这边"分开"恰恰是掩码禁止的（能数出平台方有几个组就已经泄露了清单规模）。
        """
        await self._seed_groups()
        await DBModelGroup.create(
            name="platform-private-2",
            ownership=ModelGroupOwnership.PLATFORM.value,
            owner_tenant_id=None,
            visibility=ModelGroupVisibility.PRIVATE.value,
        )
        await _seed_row(model_group_name="platform-private", cost=3.0, token_input=10, token_output=1, token_total=11)
        await _seed_row(model_group_name="platform-private-2", cost=4.0, token_input=20, token_output=2, token_total=22)
        # 看得见的那个组照常单独成行，不能被一起折进去
        await _seed_row(model_group_name="platform-shared", cost=1.0, token_input=5, token_output=0, token_total=5)

        summary = await fetch_summary(acme_admin())
        keys = [item.key for item in summary.by_model_group]
        assert keys.count(HIDDEN_MODEL_GROUP_LABEL) == 1, f"掩码后出现了同名行: {keys}"
        assert set(keys) == {HIDDEN_MODEL_GROUP_LABEL, "platform-shared"}
        hidden = next(item for item in summary.by_model_group if item.key == HIDDEN_MODEL_GROUP_LABEL)
        # 合并是**相加**不是丢弃：钱一分都不能在掩码这一步蒸发
        assert hidden.records == 2
        assert hidden.cost == pytest.approx(7.0)
        assert (hidden.token_input, hidden.token_output, hidden.token_total) == (30, 3, 33)
        # 合计仍然对得上（合并只影响分档，不影响总数）
        assert summary.total.cost == pytest.approx(8.0)
        # 合并之后仍然按费用从高到低
        assert keys == [HIDDEN_MODEL_GROUP_LABEL, "platform-shared"]

    async def test_the_platform_owner_still_sees_them_apart(self, db) -> None:
        """§2.11 反向对照：合并只发生在被掩掉的那一档，平台方那边一行不并"""
        await self._seed_groups()
        await DBModelGroup.create(
            name="platform-private-2",
            ownership=ModelGroupOwnership.PLATFORM.value,
            owner_tenant_id=None,
            visibility=ModelGroupVisibility.PRIVATE.value,
        )
        await _seed_row(model_group_name="platform-private", cost=3.0)
        await _seed_row(model_group_name="platform-private-2", cost=4.0)
        summary = await fetch_summary(platform_owner())
        assert [item.key for item in summary.by_model_group] == ["platform-private-2", "platform-private"]


class TestAggregationShape:
    async def test_an_empty_window_aggregates_to_zero_not_none(self, db) -> None:
        """`Sum` 在没有匹配行时回 `None`；透传出去前端会在 `undefined` 上做算术"""
        summary = await fetch_summary(platform_owner())
        assert summary.total.records == 0
        assert summary.total.cost == 0.0
        assert summary.by_instance == []

    async def test_breakdowns_are_ordered_by_cost(self, db) -> None:
        """按费用排而不是按条数：一条超长上下文能顶一千条"好的" """
        await _seed_row(instance_key=ACME_INSTANCE, cost=5.0)
        await _seed_row(instance_key=ACME_INSTANCE, cost=5.0)
        await _seed_row(instance_key=RIVAL_INSTANCE, tenant_id=RIVAL_TENANT_ID, cost=1000.0)
        summary = await fetch_summary(platform_owner())
        assert [item.key for item in summary.by_instance] == [RIVAL_INSTANCE, ACME_INSTANCE]
        assert summary.by_instance[0].records == 1
        assert summary.by_instance[1].records == 2

    async def test_status_breakdown_separates_failures(self, db) -> None:
        await _seed_row(status=LLMUsageStatus.SUCCESS.value, cost=3.0)
        await _seed_row(status=LLMUsageStatus.FAILED.value, cost=1.0)
        await _seed_row(status=LLMUsageStatus.ABORTED.value, cost=2.0)
        summary = await fetch_summary(platform_owner())
        assert {item.key: item.records for item in summary.by_status} == {"success": 1, "failed": 1, "aborted": 1}

    async def test_trend_buckets_cover_the_window(self, db) -> None:
        series = await fetch_trend(
            platform_owner(),
            start_time="2026-08-01T00:00:00+08:00",
            end_time="2026-08-04T00:00:00+08:00",
        )
        assert len(series.points) == 3
        assert series.points[0].bucket_start.startswith("2026-08-01T00:00:00")

    async def test_an_over_long_window_is_rejected(self, db) -> None:
        with pytest.raises(ValidationError):
            await fetch_summary(
                platform_owner(),
                start_time="2020-01-01T00:00:00+08:00",
                end_time="2026-08-01T00:00:00+08:00",
            )

    async def test_an_unparseable_time_is_rejected_rather_than_silently_defaulted(self, db) -> None:
        """悄悄回落到默认窗的代价是"我查了 3 月，页面给我最近 30 天"且毫无提示"""
        with pytest.raises(ValidationError):
            await fetch_summary(platform_owner(), start_time="上个月")

    async def test_too_many_trend_buckets_is_rejected(self, db) -> None:
        with pytest.raises(ValidationError):
            await fetch_trend(
                platform_owner(),
                start_time="2026-01-01T00:00:00+08:00",
                end_time="2026-08-01T00:00:00+08:00",
                bucket_hours=1,
            )


# ---------------------------------------------------------------------------
# 7. 记账不能拖慢主链路，更不能把主链路带崩
# ---------------------------------------------------------------------------


class TestBillingNeverBreaksTheMainPath:
    async def test_a_broken_ledger_does_not_break_the_reply(self, db, monkeypatch) -> None:
        """落库炸了只打 WARNING，回复照常返回

        这条是需求里逐字写着的第 5 条约束。写侧整条被 `except Exception` 兜住，
        计数由 `usage_recorder_stats()` 报出来——**失败是可见的，只是不致命**。
        """
        from nekro_agent.services.usage import ledger as ledger_module

        async def _explode(_context):
            raise RuntimeError("database is on fire")

        monkeypatch.setattr(ledger_module, "_resolve_attribution", _explode)
        _install_upstream(monkeypatch, lambda _kw: _FakeCompletion(content="still here"))

        response = await gen_openai_chat_response(model="m", messages=[], stream_mode=False)
        assert response.response_content == "still here"

        # 落库任务是**脱手的**（`create_task` 之后没人 await 它），所以"回复照常返回"
        # 这一条对「兜住了异常」与「把异常抛进一个没人接的任务里」是**分不出来的** ——
        # 实测把 `_write_record` 的 except 改成 `raise`，只断言回复与计数的版本照样全绿。
        # 判据必须落在任务自己身上：它要以**正常结束**收尾，异常在写侧就被吃掉了。
        tasks = list(ledger_module._pending)  # noqa: SLF001 — 这条契约没有公开出口
        assert len(tasks) == 1
        await flush_llm_usage()
        assert tasks[0].done()
        assert tasks[0].exception() is None, "落库失败必须在写侧被兜住，不能变成没人接的任务异常"

        assert await DBLLMUsageRecord.all().count() == 0
        _pending, _dropped, failed = usage_recorder_stats()
        assert failed == 1

    def test_recording_outside_an_event_loop_does_not_raise(self) -> None:
        """`record_llm_usage` 被放在 `except` 块与异步生成器的 `finally` 里

        那两个位置抛异常会**掩盖真正的失败原因**，所以它必须永不抛。
        本条**故意是同步用例**：async 用例里事件循环恒在、`create_task` 不会失败，
        写成 async 就等于测了另一件事（§2.9「fake 与真实约定脱靶」的同款）。
        """
        reset_usage_recorder()
        try:
            record_llm_usage(model_name="m", stream_mode=False, context=LLMUsageContext())
            _pending, dropped, _failed = usage_recorder_stats()
            assert dropped == 1
        finally:
            reset_usage_recorder()

    async def test_the_request_path_does_no_database_io(self, db, monkeypatch) -> None:
        """记账只组装一个 draft 就返回；查频道行/查租户/INSERT 全在后台任务里

        判据：`gen_openai_chat_response` 返回的那一刻，流水行还**没有**落库。
        它是"不拖慢主链路"这条约束唯一可断言的形态（计时断言在 CI 上不确定，不用）。
        """
        _install_upstream(monkeypatch, lambda _kw: _FakeCompletion(content="hi"))
        with usage_scope(LLMUsageContext(scene=LLMUsageScene.CHAT, chat_key=ACME_CHAT_KEY)):
            await gen_openai_chat_response(model="m", messages=[], stream_mode=False)

        assert await DBLLMUsageRecord.all().count() == 0
        pending, _dropped, _failed = usage_recorder_stats()
        assert pending == 1
        # 冲一次之后才落库——证明上面那个 0 是"还没写"而不是"根本没记"
        await flush_llm_usage()
        assert await DBLLMUsageRecord.all().count() == 1


class TestEveryModelHasAMigration:
    """模型登记进 `models/__init__.py` **与** migrations 里真的有建表语句，是两份清单

    §2.12 那条教训的同款形态（`pyproject.toml` 的 force-include vs dockerfile 的 COPY 清单）：
    本轮如果只加了模型而忘了 `poe db-revision`，**本机 pytest 会全绿**（sqlite 里
    `generate_schemas()` 按模型直接建表），而生产上 aerich 没有这张表 ——
    每一次记账都只是 `_write_record` 里一条 WARNING，账本从上线第一天起就是空的，
    而且没有任何东西会报错。

    这条扫的是全部模型而不只是本轮那一张：判据一旦只盯着自己那一行，
    下一轮别人漏了迁移它照样绿。

    ## 判据必须是 `CREATE TABLE`，不是"表名在 SQL 里出现过"（复核修正）

    第一版判的是 `f'"{table}"' in sql`。本轮这张表在迁移文件里出现 **41 次**，
    其中 40 次是 `CREATE INDEX` 与 `COMMENT ON COLUMN` —— 也就是说一份"建了索引、
    写了注释、唯独没有建表语句"的迁移**照样通过**。复核时把建表语句改名做变异，
    第一版判据当场存活。现在改成必须匹配到真正的 `CREATE TABLE "<表名>"`。
    """

    #: 建表语句的形态。aerich 生成的是 `CREATE TABLE IF NOT EXISTS "x"`，
    #: 手写的历史迁移里也有不带 `IF NOT EXISTS` 的，两种都认。
    CREATE_TABLE = r'CREATE TABLE(?: IF NOT EXISTS)?\s+"{table}"'

    def test_no_model_is_missing_its_create_table(self) -> None:
        import pathlib
        import re

        from tortoise import Model

        import nekro_agent.models as models_package

        sql = "\n".join(
            path.read_text(encoding="utf-8") for path in pathlib.Path("migrations/models").glob("*.py")
        )
        missing = []
        seen = 0
        for name in dir(models_package):
            candidate = getattr(models_package, name)
            if not (isinstance(candidate, type) and issubclass(candidate, Model) and candidate is not Model):
                continue
            seen += 1
            table = getattr(getattr(candidate, "Meta", None), "table", None) or candidate.__name__.lower()
            if not re.search(self.CREATE_TABLE.format(table=re.escape(table)), sql):
                missing.append((name, table))
        # 探测器自己不能是空转的（§2.11）：确实扫到了模型，且本轮那张表确实有**建表语句**
        assert seen >= 30, seen
        assert re.search(self.CREATE_TABLE.format(table="llm_usage_record"), sql)
        # 反向对照：一个不存在的表名必须判不出来（否则上面那条可能只是恒真）
        assert not re.search(self.CREATE_TABLE.format(table="no_such_table"), sql)
        assert missing == []


# ---------------------------------------------------------------------------
# 8. `gen_openai_chat_stream` 的两条判据
# ---------------------------------------------------------------------------


class TestSimpleStreamHasTheSameGates:
    """第三个入口此前**一条判据都没有**

    表现是 WebUI 的实时代码生成在被内容策略拦下、或遇上原生思考模型时
    一个字都不产出、也不报错，页面停在"生成中"直到用户自己放弃。
    """

    REASONING_ONLY = '```python\nprint("hi")\n```'

    async def test_simple_stream_content_blocked_is_a_distinct_error(self, db, monkeypatch) -> None:
        """`finish_reason` 只出现在收尾块上，而收尾块的 delta 常常是空的

        判据要是排在"没有正文就跳过"之后，这条链路永远看不到 `prohibited_content`。
        """
        _install_upstream(
            monkeypatch,
            lambda _kw: [
                _FakeChunk(content=None, finish_reason="PROHIBITED_CONTENT"),
                _FakeChunk(no_choices=True, usage=_FakeUsage()),
            ],
        )
        with pytest.raises(LLMContentBlockedError) as excinfo:
            async for _ in gen_openai_chat_stream(model="m", messages=[]):
                pass
        assert excinfo.value.finish_reason == "prohibited_content"

    async def test_a_blocked_stream_that_already_emitted_text_still_raises(self, db, monkeypatch) -> None:
        """半截正文必然是被截断的，宁可归成一次可辨识的失败，也不把残句当成答案"""
        _install_upstream(
            monkeypatch,
            lambda _kw: [
                _FakeChunk(content="def foo("),
                _FakeChunk(content=None, finish_reason="content_filter"),
            ],
        )
        emitted: List[str] = []
        with pytest.raises(LLMContentBlockedError):
            async for chunk in gen_openai_chat_stream(model="m", messages=[]):
                emitted.append(chunk)
        assert emitted == ["def foo("]

    async def test_simple_stream_truly_empty_raises(self, db, monkeypatch) -> None:
        """改造前：一个字都不产出、也不报错，页面停在"生成中" """
        _install_upstream(monkeypatch, lambda _kw: [_FakeChunk(content=""), _FakeChunk(no_choices=True)])
        with pytest.raises(EmptyLLMResponseError):
            async for _ in gen_openai_chat_stream(model="m", messages=[]):
                pass

    async def test_simple_stream_malformed_tool_call_is_its_own_class(self, db, monkeypatch) -> None:
        _install_upstream(
            monkeypatch,
            lambda _kw: [_FakeChunk(content=None, finish_reason="MALFORMED_FUNCTION_CALL")],
        )
        with pytest.raises(LLMMalformedToolCallError):
            async for _ in gen_openai_chat_stream(model="m", messages=[]):
                pass

    async def test_simple_stream_salvages_reasoning_only_output(self, db, monkeypatch) -> None:
        """原生思考模型把整段答案走了 reasoning 通道

        流式没法回溯，所以救回来的内容补在流末尾；字符串与非流式分支逐字相同
        （同一个 `_salvage_reasoning_only_output`，不是"等价的另一种写法"）。
        """
        _install_upstream(
            monkeypatch,
            lambda _kw: [
                _FakeChunk(reasoning=self.REASONING_ONLY),
                _FakeChunk(no_choices=True, usage=_FakeUsage()),
            ],
        )
        chunks = [chunk async for chunk in gen_openai_chat_stream(model="m", messages=[])]
        assert chunks == [self.REASONING_ONLY]

    async def test_normal_stream_is_untouched(self, db, monkeypatch) -> None:
        """正向对照：正常的流一个字节都不该被上面那几道闸门改动"""
        _install_upstream(
            monkeypatch,
            lambda _kw: [
                _FakeChunk(content="pri"),
                _FakeChunk(content="nt(1)", finish_reason="stop"),
                _FakeChunk(no_choices=True, usage=_FakeUsage()),
            ],
        )
        chunks = [chunk async for chunk in gen_openai_chat_stream(model="m", messages=[])]
        assert chunks == ["pri", "nt(1)"]

    async def test_reasoning_is_not_appended_when_content_exists(self, db, monkeypatch) -> None:
        """有正文时思考通道不该被吐出去——救回只在正文**全空**时成立"""
        _install_upstream(
            monkeypatch,
            lambda _kw: [
                _FakeChunk(content="print(1)", reasoning="先想一想"),
                _FakeChunk(no_choices=True, usage=_FakeUsage()),
            ],
        )
        chunks = [chunk async for chunk in gen_openai_chat_stream(model="m", messages=[])]
        assert chunks == ["print(1)"]


# ---------------------------------------------------------------------------
# 9. 对抗式复核补的四条（本轮交接复核）
#
# 复核的立场是「默认它漏了另一半」。这一节钉的都是**同一个 API 的第二个调用点 /
# 同一份清单的第二个副本 / 同一条不变式的第二个出口**：
#
# | 漏的那一半 | 表现 | 钉在哪 |
# |---|---|---|
# | 三个调用点没声明归属，两个场景档因此没有任何生产者 | 前端筛选器里选得中、永远查不到行 | `TestEverySceneHasAProducer` |
# | 连通性测试落的是**显示名**、且 `tenant_id` 恒 NULL | 租户在自己的账单上看不到自己刚烧的钱，组名还被掩成"未公开" | `TestModelGroupTestIsBilledToTheCaller` |
# | `CancelledError` 不是 `Exception` | `asyncio.wait_for` 超时收尾时账本上一行都没有 | `TestCancellationIsStillBilled` |
# | `flush_llm_usage()` 写了但没人调 | 进程退出，队列里的账静默消失 | `TestShutdownFlushesTheLedger` |
# ---------------------------------------------------------------------------


class TestEverySceneHasAProducer:
    """`LLMUsageScene` 的每一档都必须真的有代码写得出来

    枚举同时是**前端筛选器的取值表**（`/usage/enums` 直接吐它，`console-usage.json`
    两份 locale 逐档给了文案）。一个没有任何生产者的档位在页面上选得中、
    却永远查不到一行——它看起来像"这一档今天没花钱"，而真相是"这条链路根本没接记账"。
    本轮交接时 `COMMAND` 与 `PLUGIN` 就是这个形态：枚举、locale、筛选器三处都齐了，
    而 `command/built_in/debug.py`、`command/built_in/model.py` 与
    `plugins/builtin/email_utils.py` 三个调用点一个都没声明归属，钱全部落进 `unattributed`。

    ## 扫描器的覆盖面（§2.11 / 复核要求 4）

    - 两个根目录**都**扫（`nekro_agent/` 与 `plugins/`）——插件那一半正是漏掉的那个；
    - 不跳过任何子目录、除 `.py` 外不设后缀过滤（记账只可能发生在 Python 里）；
    - 判据是**构造点**的形态 `scene=LLMUsageScene.X`，不是"文件里出现过这个名字"：
      后者会把枚举定义、模型默认值、文档串全算成生产者，于是恒真。
    """

    #: 唯一允许没有显式生产者的一档：它是**缺省值**（`UNATTRIBUTED_CONTEXT`），
    #: 没有任何调用方声明归属时自动落进来。下面的对照断言会确认这一点仍然成立。
    IMPLICIT = {"UNATTRIBUTED"}

    @staticmethod
    def _scan():
        import pathlib
        import re

        files = [path for root in ("nekro_agent", "plugins") for path in pathlib.Path(root).rglob("*.py")]
        produced = set()
        for path in files:
            produced |= set(re.findall(r"scene=LLMUsageScene\.([A-Z_]+)", path.read_text(encoding="utf-8")))
        return files, produced

    def test_the_scanner_is_not_vacuous(self) -> None:
        """先证明扫描器**看得见真的、也看不见假的**（§2.11）

        #38 那次「0 命中」是假的：跳过 `__pycache__`、只扫一个根目录、还有后缀白名单。
        所以这里先把覆盖面本身钉住，再谈下面那条断言的含义。
        """
        files, produced = self._scan()
        assert len(files) > 300, f"只扫到 {len(files)} 个文件，覆盖面不对"
        # 两个根都真的被扫到了（插件那一半是本轮漏掉的那个）
        assert any(str(path).startswith("plugins") for path in files)
        assert any(str(path).startswith("nekro_agent") for path in files)
        # 看得见真的生产者
        assert "CHAT" in produced
        # 也看不见不存在的（否则下面那条可能只是"什么都算命中"）
        assert "NO_SUCH_SCENE" not in produced

    def test_no_scene_is_unreachable(self) -> None:
        _files, produced = self._scan()
        declared = {scene.name for scene in LLMUsageScene}
        orphans = sorted(declared - produced - self.IMPLICIT)
        assert orphans == [], f"这些场景档没有任何生产者，页面上选得中却永远是空的: {orphans}"

    def test_the_implicit_scene_really_is_the_default(self) -> None:
        """`UNATTRIBUTED` 被豁免的前提：它确实是缺省值，不是被忘掉的那一档"""
        from nekro_agent.services.usage import UNATTRIBUTED_CONTEXT

        assert UNATTRIBUTED_CONTEXT.scene is LLMUsageScene.UNATTRIBUTED
        assert LLMUsageContext().scene is LLMUsageScene.UNATTRIBUTED

    def test_every_scene_has_both_locales(self) -> None:
        """枚举与两份 locale 是**三份清单**：漏一份，页面上就显示成生的枚举字面量"""
        import json
        import pathlib

        for locale in ("zh-CN", "en-US"):
            payload = json.loads(
                pathlib.Path(f"frontend/src/locales/{locale}/console-usage.json").read_text(encoding="utf-8"),
            )
            missing = sorted(scene.value for scene in LLMUsageScene if scene.value not in payload.get("scene", {}))
            assert missing == [], f"{locale} 缺这些场景的文案: {missing}"
            missing_status = sorted(
                item.value for item in LLMUsageStatus if item.value not in payload.get("status", {})
            )
            assert missing_status == [], f"{locale} 缺这些收尾形态的文案: {missing_status}"


class TestModelGroupTestIsBilledToTheCaller:
    """总后台的「连通性测试」是**租户唯一能自己点出来的非对话花销**

    改造前它落的是 `group.GROUP_NAME`（= `DBModelGroup.group_name`，**显示名**）
    而且 `tenant_id` 恒为 NULL，两个后果都指向同一句话——租户看不到自己刚烧的钱：

    1. `UsageScope.records()` 按 `tenant_id` 过滤，NULL 的行租户一行都取不到；
    2. 就算取到了，`visible_model_group_names` 按 `DBModelGroup.name` 反查，
       显示名反查不出来 → 判成"看不见的组" → 掩成 `(未公开的模型组)`，
       而那是**他自己的组**。
    """

    @staticmethod
    async def _run_test(*, tenant_id, group_name):
        from nekro_agent.services.model_group_test import run_model_group_test

        # 显示名与身份键**故意不同**：两者相等的话这条用例分辨不出修没修
        group = _group("Acme 的自建组（显示名）", "gpt-4o-mini")
        return await run_model_group_test(
            group,
            credential_visible=True,
            group_name=group_name,
            tenant_id=tenant_id,
        )

    async def test_the_row_lands_on_the_identity_key_not_the_display_name(self, db, monkeypatch) -> None:
        _install_upstream(monkeypatch, lambda _kw: _FakeCompletion(content="pong"))
        outcome = await self._run_test(tenant_id=ACME_TENANT_ID, group_name="acme-private")
        assert outcome.ok

        rows = await _rows()
        assert len(rows) == 1
        assert rows[0].scene == LLMUsageScene.MODEL_TEST.value
        assert rows[0].model_group_name == "acme-private", "落的必须是身份键，不是 GROUP_NAME 那个显示名"
        assert rows[0].tenant_id == ACME_TENANT_ID, "不声明租户的话这一笔在租户自己的账单上根本不出现"

    async def test_the_tenant_can_see_its_own_test_spend_unmasked(self, db, monkeypatch) -> None:
        """端到端：租户点测试 → 打开用量页 → 看得到，而且组名没被掩掉"""
        await DBModelGroup.create(
            name="acme-private",
            group_name="Acme 的自建组（显示名）",
            chat_model="gpt-4o-mini",
            base_url="https://upstream.invalid/v1",
            api_key="sk-x",
            ownership=ModelGroupOwnership.TENANT.value,
            owner_tenant_id=ACME_TENANT_ID,
            visibility=ModelGroupVisibility.PRIVATE.value,
        )
        _install_upstream(monkeypatch, lambda _kw: _FakeCompletion(content="pong"))
        await self._run_test(tenant_id=ACME_TENANT_ID, group_name="acme-private")
        await flush_llm_usage()

        summary = await fetch_summary(acme_admin())
        groups = {item.key: item for item in summary.by_model_group}
        assert "acme-private" in groups, "租户看不到自己刚点出来的那笔支出"
        assert HIDDEN_MODEL_GROUP_LABEL not in groups, "自己的组不该被掩成「未公开」"

        # 对照：别的租户仍然一行都看不到（租户过滤没有因为这条新链路被放开）
        assert (await fetch_summary(rival_admin())).total.records == 0

    async def test_a_platform_owner_test_stays_on_the_platform(self, db, monkeypatch) -> None:
        """平台方点的那次仍然是 `tenant_id IS NULL`——租户不该在账单上看到它"""
        _install_upstream(monkeypatch, lambda _kw: _FakeCompletion(content="pong"))
        await self._run_test(tenant_id=None, group_name="platform-shared")

        rows = await _rows()
        assert rows[0].tenant_id is None
        assert (await fetch_summary(acme_admin())).total.records == 0
        assert (await fetch_summary(platform_owner())).total.records == 1


class TestDeclaredTenantNeverOverridesTheInstance:
    """显式租户只是"没有实例时的退路"，不能反过来盖住实例反查的结果

    实例才是计费单元，实例行上的 `tenant_id` 是权威。顺序反了的话，
    任何一个调用方填错一次 `tenant_id`，那条会话的账就整片记到别人头上。
    """

    async def test_the_instance_wins(self, db, monkeypatch) -> None:
        _install_upstream(monkeypatch, lambda _kw: _FakeCompletion(content="hi"))
        # 声明一个**错的**租户，同时给出会话——实例反查得到的 ACME 必须赢
        with usage_scope(
            LLMUsageContext(
                scene=LLMUsageScene.CHAT,
                chat_key=ACME_CHAT_KEY,
                tenant_id=RIVAL_TENANT_ID,
            ),
        ):
            await gen_openai_chat_response(model="m", messages=[], stream_mode=False)

        rows = await _rows()
        assert rows[0].tenant_id == ACME_TENANT_ID
        assert rows[0].instance_key == ACME_INSTANCE

    async def test_the_declared_tenant_is_used_when_there_is_no_instance(self, db, monkeypatch) -> None:
        """对照组：没有会话也没有实例时，声明的那个租户才生效

        缺了这一条，上面那条断言与"`tenant_id` 这个字段根本没被读过"分辨不出来。
        """
        _install_upstream(monkeypatch, lambda _kw: _FakeCompletion(content="hi"))
        with usage_scope(LLMUsageContext(scene=LLMUsageScene.MODEL_TEST, tenant_id=RIVAL_TENANT_ID)):
            await gen_openai_chat_response(model="m", messages=[], stream_mode=False)

        rows = await _rows()
        assert rows[0].tenant_id == RIVAL_TENANT_ID
        assert rows[0].instance_key == ""


class TestCancellationIsStillBilled:
    """`CancelledError` **不是** `Exception`

    3.8 起它挂在 `BaseException` 下，`except Exception` 一个字都接不到。
    而取消是这条函数真实存在的收尾方式：`templates/history.py` 用
    `asyncio.wait_for(planning_call, timeout=...)` 裹住它，超时那一下就是往里抛取消。
    改造前那条链路超时收尾时，账本上**一行都没有**——而模型文档要求的正是
    "失败的那几次也记，『今天失败了 400 次』本身就是运营信号"。
    """

    async def test_a_cancelled_request_still_lands_a_row(self, db, monkeypatch) -> None:
        import asyncio as _asyncio

        async def _never_finishes(**_kwargs):
            await _asyncio.sleep(3600)

        class _Completions:
            create = staticmethod(_never_finishes)

        class _FakeClient:
            def __init__(self, **_kwargs) -> None:
                self.chat = SimpleNamespace(completions=_Completions())

        monkeypatch.setattr(openai_module, "AsyncOpenAI", _FakeClient)
        monkeypatch.setattr(openai_module, "_get_http_client", lambda **_kwargs: object())

        call = gen_openai_chat_response(model="m", messages=[], stream_mode=False)
        with usage_scope(LLMUsageContext(scene=LLMUsageScene.HISTORY_PLAN, chat_key=ACME_CHAT_KEY)):
            with pytest.raises((TimeoutError, _asyncio.TimeoutError)):
                await _asyncio.wait_for(call, timeout=0.05)

        rows = await _rows()
        assert len(rows) == 1, "取消收尾的那次请求必须也在账上"
        # 既不是成功也不是失败：token 可能已经烧了，混进 failed 会让失败率失真
        assert rows[0].status == LLMUsageStatus.ABORTED.value
        assert rows[0].error_type == "CancelledError"
        # 归属照常解析（取消发生在 contextvar 仍然生效的那个上下文里）
        assert rows[0].tenant_id == ACME_TENANT_ID
        assert rows[0].scene == LLMUsageScene.HISTORY_PLAN.value


class TestEmbeddingsAreBilledToo:
    """第四个 usage 读取处：`gen_openai_embeddings`

    交接报告把它记成"账本上唯一一条结构性缺口"，理由是"上游回的是纯向量、
    要先改返回形态"。**那条理由是错的**：上游回的是一整个 JSON，`usage` 就在里面，
    而本函数本来就 `res.json()` 了一次；记账是副作用，不需要动签名。

    量上它恰恰是最大的一档：一次增强记忆检索按 query 条数打出 N 次，
    知识库入库与表情包各还有一条。不记的话，账面上少掉的正是**条数最多**的那一类。
    """

    @staticmethod
    def _install(monkeypatch, payload=None, *, status_error: bool = False):
        """只替换 HTTP 客户端本身；`gen_openai_embeddings` 的判据一行不动"""

        class _FakeResponse:
            def __init__(self) -> None:
                self._payload = payload

            def raise_for_status(self) -> None:
                if status_error:
                    raise httpx.HTTPStatusError("401 unauthorized", request=None, response=None)  # type: ignore[arg-type]

            def json(self):
                return self._payload

        class _FakeClient:
            async def post(self, *_args, **_kwargs):
                return _FakeResponse()

        monkeypatch.setattr(openai_module, "_get_http_client", lambda **_kwargs: _FakeClient())

    async def test_a_successful_embedding_lands_a_row(self, db, monkeypatch) -> None:
        self._install(
            monkeypatch,
            {"data": [{"embedding": [0.1, 0.2]}], "usage": {"prompt_tokens": 12, "total_tokens": 12}},
        )
        with usage_scope(
            LLMUsageContext(scene=LLMUsageScene.EMBEDDING).with_model_group("emb", _group("emb", "text-emb", input_rate=3.0)),
        ):
            vector = await gen_openai_embeddings(
                model="text-emb",
                input="hello",
                api_key="sk-x",
                base_url="https://upstream.invalid/v1",
            )
        assert vector == [0.1, 0.2]

        rows = await _rows()
        assert len(rows) == 1, "向量化这一跳没有进账本"
        row = rows[0]
        assert row.scene == LLMUsageScene.EMBEDDING.value
        assert (row.token_input, row.token_total) == (12, 12)
        # 嵌入接口没有补全通道
        assert row.token_output == 0
        assert row.model_group_name == "emb"
        assert row.status == LLMUsageStatus.SUCCESS.value
        # 12 * 3.0 = 36：倍率随组走，不是恒 1.0
        assert row.cost == pytest.approx(36.0)

    async def test_a_failed_embedding_still_lands_a_row(self, db, monkeypatch) -> None:
        """4xx/5xx 拿不到 usage，但"今天向量化失败了 400 次"本身就是运营信号"""
        self._install(monkeypatch, None, status_error=True)
        with usage_scope(LLMUsageContext(scene=LLMUsageScene.EMBEDDING)), pytest.raises(httpx.HTTPStatusError):
            await gen_openai_embeddings(
                model="text-emb",
                input="hello",
                api_key="sk-x",
                base_url="https://upstream.invalid/v1",
            )

        rows = await _rows()
        assert len(rows) == 1
        assert rows[0].status == LLMUsageStatus.FAILED.value
        assert rows[0].error_type == "HTTPStatusError"
        assert (rows[0].token_input, rows[0].token_total) == (0, 0)

    async def test_usage_is_read_before_the_vector_is_taken(self, db, monkeypatch) -> None:
        """响应体不合形态（中转站包了一层 / data 为空）时**这一跳同样计过费**

        取向量那一行会抛 `IndexError`，而 usage 在它之前就读出来了 ——
        与非流式 chat 分支"usage 必须在任何一道闸门之前读"是同一条判据。
        把 `usage` 的读取挪到取向量之后，这条会红。
        """
        self._install(monkeypatch, {"data": [], "usage": {"prompt_tokens": 9, "total_tokens": 9}})
        with usage_scope(LLMUsageContext(scene=LLMUsageScene.EMBEDDING)), pytest.raises(IndexError):
            await gen_openai_embeddings(
                model="text-emb",
                input="hello",
                api_key="sk-x",
                base_url="https://upstream.invalid/v1",
            )

        rows = await _rows()
        assert len(rows) == 1
        assert rows[0].status == LLMUsageStatus.FAILED.value
        assert (rows[0].token_input, rows[0].token_total) == (9, 9), "响应体坏了，但这次调用的 token 已经烧掉了"

    async def test_the_embedding_test_branch_declares_attribution(self, db, monkeypatch) -> None:
        """`run_model_group_test` 有**两支**：chat 与 embedding

        最初只给 chat 那一支接了归属。嵌入组的连通性测试同样是租户自己点出来的花销，
        落进 `unattributed` 的话他在自己的用量页上看不到自己刚烧的钱。
        """
        from nekro_agent.services import model_group_test as mgt

        self._install(monkeypatch, {"data": [{"embedding": [0.5]}], "usage": {"prompt_tokens": 4, "total_tokens": 4}})
        monkeypatch.setattr(mgt, "prepare_model_group_test", _noop_prepare)
        group = _group("acme-emb", "text-emb")
        group.MODEL_TYPE = "embedding"

        outcome = await mgt.run_model_group_test(
            group,
            credential_visible=True,
            group_name="acme-emb",
            tenant_id=ACME_TENANT_ID,
        )
        assert outcome.ok

        rows = await _rows()
        assert len(rows) == 1
        assert rows[0].scene == LLMUsageScene.EMBEDDING.value
        assert rows[0].model_group_name == "acme-emb", "落的是显示名而不是身份键，租户会被掩成「未公开的模型组」"
        assert rows[0].tenant_id == ACME_TENANT_ID, "嵌入组的连通性测试没算到点它的那个租户头上"


async def _noop_prepare(*_args: Any, **_kwargs: Any) -> None:
    """`prepare_model_group_test` 会做出站公网校验，本文件测的不是那一条"""
    return None


class TestTheAbandonedStreamKeepsItsAttribution:
    """异步生成器**没有自己的上下文**，而 `ABORTED` 这一档恰好是被别人关掉的

    PEP 568 至今未落地：async generator 的每一步都跑在**当次驱动它的那个任务**里。
    消费方提前丢弃生成器时（WebUI 关掉插件代码生成页面就是这一支），收尾不是消费方跑的——
    asyncio 的 asyncgen finalizer 用 `loop.create_task(agen.aclose())` 关它，
    那个任务的上下文是**新拷贝的**，于是：

    1. `gen_openai_chat_stream` 的 `finally: _record()` 里 `current_usage_context()`
       读到的是**默认值**，整条账落进 `unattributed` —— 这一档唯一真正需要它的场景，
       归属恰好丢光；
    2. `usage_scope` 的 `finally: reset(token)` 抛
       `ValueError: <Token …> was created in a different Context`，在 GeneratorExit
       处理途中变成一条 `an error occurred during closing of asynchronous generator`。

    既有的 `test_simple_stream_abort_is_billed_as_aborted` 在**同一个任务**里 `aclose()`，
    两条都分辨不出来（那条路径下 contextvar 还在、token 也还是同一个上下文的）。
    本类补的就是"换个任务关它"这一维。
    """

    @staticmethod
    def _install_three_chunks(monkeypatch) -> None:
        _install_upstream(
            monkeypatch,
            lambda _kw: [
                _FakeChunk(content="a", usage=_FakeUsage(7, 5, 2)),
                _FakeChunk(content="b"),
                _FakeChunk(content="c"),
            ],
        )

    async def test_attribution_survives_a_foreign_context_finalizer(self, db, monkeypatch) -> None:
        """在另一个任务里 aclose：账仍然算在声明的那个场景/模型组上"""
        import asyncio as _asyncio

        self._install_three_chunks(monkeypatch)
        group = _group("plugin-gen", "m", input_rate=2.0)

        with usage_scope(
            LLMUsageContext(scene=LLMUsageScene.PLUGIN_GENERATE).with_model_group("plugin-gen", group),
        ):
            generator = gen_openai_chat_stream(model="m", messages=[])
            assert await generator.__anext__() == "a"

        # 声明已经退出（消费方的那个 with 块结束了），再由**另一个任务**去关生成器 ——
        # 与 asyncio 的 asyncgen finalizer 逐字同形
        await _asyncio.create_task(generator.aclose())

        rows = await _rows()
        assert len(rows) == 1
        assert rows[0].status == LLMUsageStatus.ABORTED.value
        assert rows[0].scene == LLMUsageScene.PLUGIN_GENERATE.value, "被外部 finalizer 关掉的流把归属丢了"
        assert rows[0].model_group_name == "plugin-gen"
        # 倍率也随快照走：丢了归属就等于按 1.0 折算，账面直接少一半
        assert rows[0].token_input_rate == 2.0
        assert (rows[0].token_input, rows[0].token_output) == (5, 2)

    async def test_a_yield_spanning_scope_can_be_closed_from_another_task(self, db, monkeypatch) -> None:
        """`usage_scope` 跨过 yield 时，异地收尾不能炸

        形态与 `plugin/generator.py:generate_plugin_code_stream` 逐字相同：
        `with usage_scope(...): async for chunk in ...: yield chunk`。
        """
        import asyncio as _asyncio

        self._install_three_chunks(monkeypatch)

        async def relay():
            with usage_scope(LLMUsageContext(scene=LLMUsageScene.PLUGIN_GENERATE)):
                async for chunk in gen_openai_chat_stream(model="m", messages=[]):
                    yield chunk

        outer = relay()
        assert await outer.__anext__() == "a"
        # 不加保护时这一行抛 ValueError（token 属于上面那个任务的上下文）
        await _asyncio.create_task(outer.aclose())

    async def test_the_probe_really_changes_context(self) -> None:
        """§2.11 对照：上面两条用的"另一个任务"确实换了上下文

        换不掉的话（例如哪天 `create_task` 不再拷贝上下文），两条用例会退化成
        与既有那条 `aclose()` 用例逐字相同的东西，而它们本来是**恒绿**的。
        """
        import asyncio as _asyncio
        import contextvars as _contextvars

        probe: _contextvars.ContextVar[str] = _contextvars.ContextVar("probe", default="outer")
        token = probe.set("inner")

        async def read_and_reset() -> str:
            seen = probe.get()
            with pytest.raises(ValueError, match="different Context"):
                probe.reset(token)
            return seen

        assert await _asyncio.create_task(read_and_reset()) == "inner"
        probe.reset(token)


class TestShutdownFlushesTheLedger:
    """写完一个 `flush_llm_usage()` 而没人调它，等于没写

    落库是**脱手**的后台任务，进程退出时事件循环一关，队列里没跑完的那些
    连同它们记录的花销一起静默消失。`ledger.py` 的模块文档白纸黑字写着
    "两个用处：进程收尾时把最后几条写完"，而交接时 `on_shutdown` 里一个字都没有。
    """

    def test_the_shutdown_hook_actually_flushes(self) -> None:
        import pathlib

        source = pathlib.Path("nekro_agent/__init__.py").read_text(encoding="utf-8")
        head, _, body = source.partition("async def on_shutdown()")
        assert body, "找不到 on_shutdown，判据本身失效了"
        # 判据必须是**真的调了**，不能只是"这个名字在函数体里出现过"：
        # 收尾块里那行 `from ... import flush_llm_usage` 本身就带着这个名字，
        # 只判名字的话，把 `await flush_llm_usage()` 换成 `0` 这条变异会**存活**
        # （变异实测如此）。
        assert "await flush_llm_usage(" in body, "用量流水没有在进程收尾时冲干净，队列里的账会静默消失"
        # 顺序也要对：必须排在 HTTP 连接池关闭**之前**
        assert body.index("await flush_llm_usage(") < body.index("reset_http_clients")
        # §2.11 对照：判据不是"整个文件里出现过这个名字"
        assert "flush_llm_usage" not in head

    def test_the_ledger_summary_line_survives_the_default_log_level(self) -> None:
        """收尾那行必须是 **INFO**，因为 `APP_LOG_LEVEL` 默认就是 INFO

        运维拿 `grep "llm usage ledger flushed"` 当"账本收尾过、这次运行一条都没丢"的
        唯一凭据（docs/operations.md §2.3）。写成 `logger.debug` 的话
        它在**健康部署上恒为 0 命中**——而真的丢账时同样是 0 命中，两者分不开，
        于是这条运维判据既报假警又漏真警。周围那些收尾步骤是 DEBUG，这一条故意不是。
        """
        import pathlib
        import re

        from nekro_agent.core.config import CoreConfig

        # 前提：默认级别确实是 INFO（哪天默认改成 DEBUG，本条的理由就不成立了）
        assert CoreConfig.model_fields["APP_LOG_LEVEL"].default == "INFO"

        source = pathlib.Path("nekro_agent/__init__.py").read_text(encoding="utf-8")
        _head, _, body = source.partition("async def on_shutdown()")
        match = re.search(r"logger\.(\w+)\(\s*\n?\s*f?\"\[shutdown\] llm usage ledger flushed", body)
        assert match, "找不到收尾汇总那一行——运维排查用 grep 这一行做判据"
        assert match.group(1) == "info", f"它是 logger.{match.group(1)}，生产上不落盘"
        # §2.11 对照：同一段里那条 DEBUG 必须被这个正则判成**不是** info
        debug_line = re.search(r"logger\.(\w+)\(\"\[shutdown\] flushing llm usage ledger\"\)", body)
        assert debug_line and debug_line.group(1) == "debug"

    async def test_flush_actually_drains_the_queue(self, db, monkeypatch) -> None:
        """行为侧：冲之前队列里有、冲之后库里有"""
        _install_upstream(monkeypatch, lambda _kw: _FakeCompletion(content="hi"))
        with usage_scope(LLMUsageContext(scene=LLMUsageScene.CHAT, chat_key=ACME_CHAT_KEY)):
            await gen_openai_chat_response(model="m", messages=[], stream_mode=False)

        pending, _dropped, _failed = usage_recorder_stats()
        assert pending == 1
        assert await DBLLMUsageRecord.all().count() == 0
        assert await flush_llm_usage() == 1
        assert await DBLLMUsageRecord.all().count() == 1


class TestEveryEmbeddingCallSiteDeclaresAttribution:
    """`gen_openai_embeddings` 有**三个**消费点，改了函数不等于接上了线

    记账在函数里面，所以"钱有没有进账本"与"账算不算得到人头上"是两件事：
    漏了 `usage_scope` 的那一路照样落一行，只是 `scene` 退回 `unattributed` ——
    页面上那一档涨起来时，谁都说不出是哪条链路。而这正是本项目
    「9 个组件变异全死、6 个接线变异活了 5 个」的原话形态。

    判据落在调用点自己身上：每一处 `gen_openai_embeddings(` 都必须**词法上**
    落在某个 `usage_scope(` 的 `with` 块里。
    """

    DEFINITION = "nekro_agent/services/agent/openai.py"

    @staticmethod
    def _call_sites():
        import ast
        import pathlib

        scanned = 0
        sites = []
        roots = ("nekro_agent", "plugins")
        for root in roots:
            for path in pathlib.Path(root).rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                scanned += 1
                if "gen_openai_embeddings(" not in text:
                    continue
                tree = ast.parse(text)
                # 所有 `with usage_scope(...)` 块的行区间
                scopes = []
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.With, ast.AsyncWith)):
                        continue
                    for item in node.items:
                        call = item.context_expr
                        if not isinstance(call, ast.Call):
                            continue
                        func = call.func
                        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                        if name == "usage_scope":
                            scopes.append((node.lineno, node.end_lineno or node.lineno))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    func = node.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                    if name != "gen_openai_embeddings":
                        continue
                    inside = any(start <= node.lineno <= end for start, end in scopes)
                    sites.append((path.as_posix(), node.lineno, inside))
        return scanned, sites

    def test_the_scanner_finds_every_call_site(self) -> None:
        """§2.11 对照：两个根都扫（插件那一半正是最容易漏的），定义处不算调用点"""
        scanned, sites = self._call_sites()
        assert scanned > 300, f"只扫了 {scanned} 个文件，覆盖面不对"
        callers = sorted({path for path, _line, _inside in sites})
        assert self.DEFINITION not in callers
        assert callers == [
            "nekro_agent/services/memory/embedding_service.py",
            "nekro_agent/services/model_group_test.py",
            "plugins/builtin/emotion.py",
        ], callers

    def test_every_call_site_is_inside_a_usage_scope(self) -> None:
        _scanned, sites = self._call_sites()
        naked = [(path, line) for path, line, inside in sites if not inside]
        assert naked == [], f"这些向量化调用点没有声明归属，账会落进 unattributed: {naked}"

    def test_the_scanner_can_see_a_missing_declaration(self) -> None:
        """判据不是恒真：一段**没有**声明的同形代码必须被判成 False"""
        import ast

        tree = ast.parse("async def f():\n    await gen_openai_embeddings(model='m')\n")
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        assert any(getattr(node.func, "id", "") == "gen_openai_embeddings" for node in calls)


class TestEveryModelTestCallSiteDeclaresAttribution:
    """三个入口是**同一个 API 的三个调用点**，改了函数不等于改了接线

    上面那组用例直接调 `run_model_group_test(...)`，因此对"路由忘了传 `group_name` /
    `tenant_id`"是分辨不出来的——本项目栽过的原话是「9 个组件变异全死、6 个接线变异活了 5 个」。
    两个新参数都有默认值（不传也能跑），所以漏一处不会有任何编译期或运行期信号：
    那一路的账会静默退回"显示名 + NULL 租户"，也就是复核前的错法。

    判据落在调用点自己身上：每一处 `run_model_group_test(` 的实参里必须同时出现
    `group_name=` 与 `tenant_id=`。
    """

    #: 定义处所在的模块不算调用点
    DEFINITION = "nekro_agent/services/model_group_test.py"

    @staticmethod
    def _call_sites():
        import ast
        import pathlib

        sites = []
        scanned = 0
        for path in pathlib.Path("nekro_agent").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            scanned += 1
            if "run_model_group_test(" not in text:
                continue
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name != "run_model_group_test":
                    continue
                sites.append((path.as_posix(), node.lineno, {kw.arg for kw in node.keywords}))
        return scanned, sites

    def test_the_scanner_finds_every_call_site(self) -> None:
        """§2.11 对照：先证明扫到了东西，再谈"每一处都合规"这句话的含义"""
        scanned, sites = self._call_sites()
        assert scanned > 200, f"只扫了 {scanned} 个文件"
        callers = sorted({path for path, _line, _kwargs in sites})
        # 定义处不是调用点；三个真实入口一个都不能少
        assert self.DEFINITION not in callers
        assert callers == [
            "nekro_agent/routers/config.py",
            "nekro_agent/routers/console/model_groups.py",
        ], callers
        assert len(sites) == 3, sites

    def test_every_call_site_declares_group_name_and_tenant(self) -> None:
        _scanned, sites = self._call_sites()
        offenders = sorted(
            f"{path}:{line} 缺 {sorted({'group_name', 'tenant_id'} - kwargs)}"
            for path, line, kwargs in sites
            if not {"group_name", "tenant_id"} <= kwargs
        )
        assert offenders == [], offenders


class TestTheUsageEntryIsHiddenForTheRightReason:
    """`/console/usage` 被藏起来的**依据**必须自己有人守

    这一格标了 `visibility: 'tenantAdminOnly'` 并进了 `TENANT_ADMIN_ONLY_PATHS`，
    理由只有一条：它的每一条读端点都最低 `Role.Admin`，只读成员进来是一页 403。
    可这条理由本身在本轮**没有任何用例守着**——

    `tests/test_readonly_member_console_surface.py` 里那张
    `ADMIN_ONLY_READ_ENDPOINTS` 是**手维护**的第三份清单（前两份是菜单的 visibility
    标记与 URL 兜底清单）。交接时那条支路正在被整文件改写，因此本族当时只更新了前两份，
    "后端确实是 Role.Admin"这句话对本族端点**没被检查过**。

    **后来已把 `("/console/usage", "console.usage", "GET", "/usage/summary")`
    并进那张表**（它顺带把「URL 兜底清单真的挡住只读成员」也钉上了，那一维本文件测不到）。
    本类保留，因为它覆盖的是**四条**读端点而不是一条代表：`/usage/trend`、`/usage/records`、
    `/usage/enums` 任意一条被降级，那张表都不会红。
    """

    #: 本族的读端点。与 `USAGE_CONSOLE_TENANT_REACHABLE` 是同一批，
    #: 但这里问的是另一个问题：它们的**角色门**够不够高。
    READ_ENDPOINTS = (
        ("GET", "/usage/summary"),
        ("GET", "/usage/trend"),
        ("GET", "/usage/records"),
        ("GET", "/usage/enums"),
    )

    @staticmethod
    def _min_role(method: str, path: str):
        import importlib
        import inspect

        module = importlib.import_module("nekro_agent.routers.console.usage")
        for route in module.router.routes:
            if getattr(route, "path", None) != path or method not in getattr(route, "methods", set()):
                continue
            return inspect.getclosurevars(route.endpoint).nonlocals.get("min_role")
        raise AssertionError(f"路由表里没有 {method} {path}——它被改名或删掉了")

    def test_every_usage_read_endpoint_is_admin_gated(self) -> None:
        from nekro_agent.services.user.role import Role as _Role

        for method, path in self.READ_ENDPOINTS:
            assert self._min_role(method, path) is _Role.Admin, (
                f"{method} {path} 已经不是 Role.Admin 了。"
                "要么把 /console/usage 从 TENANT_ADMIN_ONLY_PATHS 里拿掉，"
                "要么把这条端点的角色门加回去——不能一边对只读成员藏页面、"
                "一边让他其实读得到数据"
            )

    def test_the_probe_can_see_a_wrong_answer(self) -> None:
        """§2.11 对照：探测器不是恒真（不存在的路径必须炸，不是静默返回 None）"""
        with pytest.raises(AssertionError):
            self._min_role("GET", "/usage/no-such-endpoint")

    def test_the_entry_is_in_both_navigation_lists(self) -> None:
        """菜单标记与 URL 兜底清单是**两份**，只登记一份没有任何运行时信号"""
        import pathlib

        source = pathlib.Path("frontend/src/config/navigation.tsx").read_text(encoding="utf-8")
        assert "'/console/usage',\n" in source, "URL 兜底清单 TENANT_ADMIN_ONLY_PATHS 里没有它"
        # 菜单那一格必须紧跟着标了 tenantAdminOnly
        entry = source.split("path: '/console/usage',", 1)
        assert len(entry) == 2, "菜单里没有 /console/usage 这一格"
        assert "visibility: 'tenantAdminOnly'" in entry[1][:400], "菜单格子没标 tenantAdminOnly"


class TestTheTenantBreakdownIsReadable:
    """「每个客户烧了多少」是这本账要回答的**第一个**问题

    后端一直在算 `by_tenant`，但分组键是裸的 `tenant_id`，而前端此前**根本没画这一档**。
    也就是说：平台方打开用量页，看得到"哪个实例最贵"，却看不到"哪个客户最贵"——
    而共用版 Bot 烧的是平台方自己的 Key，按客户分账正是它存在的理由。
    """

    async def test_the_platform_owner_sees_tenant_names(self, db) -> None:
        await _seed_row(tenant_id=ACME_TENANT_ID, cost=110.0)
        await _seed_row(tenant_id=RIVAL_TENANT_ID, instance_key=RIVAL_INSTANCE, cost=7.0)

        summary = await fetch_summary(platform_owner())
        keys = [item.key for item in summary.by_tenant]
        # 按费用从高到低；显示的是名字而不是 `1` / `2`
        assert keys == ["Acme", "Rival"], keys

    async def test_a_deleted_tenant_keeps_its_id_instead_of_vanishing(self, db) -> None:
        """账要对得上号：查不到名字宁可显示号码，也不能让这一行凭空消失或并进空档"""
        await _seed_row(tenant_id=4242, cost=5.0)
        summary = await fetch_summary(platform_owner())
        assert [item.key for item in summary.by_tenant] == ["4242"]

    async def test_the_platform_own_spend_stays_in_its_own_bucket(self, db) -> None:
        """`tenant_id IS NULL`（平台方自己 / 归不了属）不该被安上任何一个租户的名字"""
        await _seed_row(tenant_id=None, cost=3.0)
        summary = await fetch_summary(platform_owner())
        assert [item.key for item in summary.by_tenant] == [""]

    async def test_a_tenant_only_ever_sees_itself(self, db) -> None:
        """对照：租户身份下这一档只可能有他自己——名字换出来也不构成新的泄露面"""
        await _seed_row(tenant_id=ACME_TENANT_ID, cost=110.0)
        await _seed_row(tenant_id=RIVAL_TENANT_ID, instance_key=RIVAL_INSTANCE, cost=999.0)

        summary = await fetch_summary(acme_admin())
        assert [item.key for item in summary.by_tenant] == ["Acme"]

    def test_the_page_actually_renders_this_breakdown(self) -> None:
        """后端算了、接口返了、页面不画，等于没做（本轮交接时正是这个形态）

        判据落在页面源码上：`by_tenant` 必须真的喂进一张分档表。
        """
        import pathlib

        source = pathlib.Path("frontend/src/pages/console/UsagePage.tsx").read_text(encoding="utf-8")
        assert "summaryQuery.data?.by_tenant" in source, "接口返了 by_tenant，页面却一个字都没画"
        assert "breakdown.byTenant" in source
        # §2.11 对照：判据不是"文件里出现过 by_ 开头的东西"
        assert "summaryQuery.data?.by_nothing" not in source

    def test_the_refresh_button_moves_the_time_window(self) -> None:
        """刷新按钮不能是「点不动的按钮」

        时间窗由 `useMemo` 算出来并进了 queryKey。只调 `refetch()` 的话查的还是
        打开页面那一刻的 `[start, end)`，页面开着一小时、刷新多少次也看不到这一小时里
        新写进来的流水——按钮有反应（转一圈）就是不起作用，比灰掉更难发现。

        判据要求那个重算信号**真的进了 useMemo 的依赖数组**，而不只是"文件里有这个名字"
        （§2.30：源码级判据的锚点必须带上让这段代码起作用所需要的语法）。
        """
        import pathlib
        import re

        source = pathlib.Path("frontend/src/pages/console/UsagePage.tsx").read_text(encoding="utf-8")
        deps = re.search(r"const filters: UsageFilters = useMemo\((.*?)\n  \)", source, re.S)
        assert deps, "filters 那个 useMemo 不见了——判据的落点没了"
        assert "refreshEpoch" in deps.group(1).rsplit("[", 1)[-1], "刷新信号没有进 filters 的依赖数组，窗口仍然是冻住的"
        assert "setRefreshEpoch(" in source, "刷新按钮没有推进那个信号"

    def test_the_breakdown_has_both_locales(self) -> None:
        import json
        import pathlib

        for locale in ("zh-CN", "en-US"):
            payload = json.loads(
                pathlib.Path(f"frontend/src/locales/{locale}/console-usage.json").read_text(encoding="utf-8"),
            )
            assert payload["breakdown"].get("byTenant"), locale
            assert payload["columns"].get("tenant"), locale

    async def test_two_tenants_with_the_same_display_name_stay_distinguishable(self, db) -> None:
        """`display_name` **没有唯一约束**（唯一的是 `name`）

        两个客户都叫"测试"时，分档表会出现两行长得一模一样的账单——
        "哪个客户最贵"当场变成无解，而前端拿 `key` 当 React 列表键还会撞。
        撞名时退回唯一的 `name`；**没撞的那些不该跟着变难读**。
        """
        await DBTenant.create(id=7, name="acme-eu", display_name="Acme")
        await _seed_row(tenant_id=ACME_TENANT_ID, cost=110.0)  # display_name 也是 "Acme"
        await _seed_row(tenant_id=7, cost=50.0)
        await _seed_row(tenant_id=RIVAL_TENANT_ID, instance_key=RIVAL_INSTANCE, cost=7.0)

        summary = await fetch_summary(platform_owner())
        keys = [item.key for item in summary.by_tenant]
        assert len(keys) == len(set(keys)), f"两行同名，账单分不出是哪个客户: {keys}"
        # 撞名的两个退回唯一 slug；没撞的 Rival 保持好读的显示名
        assert set(keys) == {"acme", "acme-eu", "Rival"}, keys
