"""用量流水的写侧：算钱 + 落库

## 两条硬约束（需求里逐字写着的）

1. **不拖慢主链路。** 记账不在请求路径上做任何 I/O：`record_llm_usage` 只组装一个
   不可变的 draft、丢进后台任务就返回。归属解析（查频道行、查实例的租户、解析域）
   与 INSERT 全部发生在那个任务里。
2. **记账失败不能让回复失败。** 整条写路径被 `except Exception` 兜住，只打 WARNING。
   `record_llm_usage` 自己也永不抛——它被放在 `openai.py` 的 `except` 块与
   异步生成器的 `finally` 里，那两个位置抛异常会**掩盖真正的失败原因**。

## 为什么不用 `asyncio.create_task` 就完事

裸 `create_task` 的返回值没人持有时会被 GC 回收，任务可能在跑到一半时消失
（CPython 只保留弱引用）。所以这里用一个模块级集合持有强引用，完成后自行摘除。

同一个集合顺带做**背压**：上游抽风时一轮对话会连着失败几次，每次都记一行，
而数据库慢下来的时候正是这种时候。积压超过 :data:`_MAX_PENDING` 就丢弃新记录并计数，
**丢弃是有意的**——记账是观测，不能反过来把内存吃光把 Bot 拖死。
丢了多少由 `usage_recorder_stats()` 报得出来，不是静默的。

## 倍率缺席时按 1.0 算，这不是特例

`DBModelGroup.token_input_rate` / `token_completion_rate` / `model_price_rate` 三列的
**数据库默认值本来就是 1.0**。所以"没声明归属、拿不到模型组"的那些行按 1.0 折算，
与"运维建了组但没配倍率"落在同一条公式上，账面口径一致，不需要在读侧再分一次支。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional, Set, Tuple

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.schemas.usage import LLMUsageScene, LLMUsageStatus

from .context import UNATTRIBUTED_CONTEXT, LLMUsageContext, current_usage_context

logger = get_sub_logger("llm_usage")

# 同时在飞的落库任务上限。超过就丢弃新记录（并计数），见模块文档。
_MAX_PENDING = 256

_pending: Set["asyncio.Task[None]"] = set()
_dropped_count = 0
_failed_count = 0


def compute_token_cost(
    token_input: int,
    token_output: int,
    *,
    input_rate: float = 1.0,
    completion_rate: float = 1.0,
    price_rate: float = 1.0,
) -> float:
    """按模型组的三个倍率把 token 折算成钱

    公式与 `OpenAIResponse.price_consumption` **是同一份**（那个方法现在直接调本函数）。
    分出来是因为失败的那些请求根本构造不出 `OpenAIResponse`，而它们同样要算钱；
    留两份实现就是本项目栽过六次的那个形态。

    注意 `input_rate` 同时乘在补全那一项上：`token_completion_rate` 的语义是
    "补全相对输入的倍数"而不是绝对单价，这是上游既有的口径，不要在这里改。
    """
    return (token_input * input_rate + token_output * input_rate * completion_rate) * price_rate


@dataclass(frozen=True)
class LLMUsageDraft:
    """一条待落库的用量流水

    归属那几列在这里仍是"调用方声明的原样"，真正的解析（频道 → 实例、实例 → 租户、
    实例 → 域）在 :func:`_resolve_attribution` 里做，因为那三步都要查库。
    """

    context: LLMUsageContext = field(default=UNATTRIBUTED_CONTEXT)
    model_name: str = ""
    stream_mode: bool = False
    token_input: int = 0
    token_output: int = 0
    token_total: int = 0
    status: LLMUsageStatus = LLMUsageStatus.SUCCESS
    error_type: str = ""
    finish_reason: str = ""
    generation_time_ms: int = 0

    @property
    def cost(self) -> float:
        return compute_token_cost(
            self.token_input,
            self.token_output,
            input_rate=self.context.token_input_rate,
            completion_rate=self.context.token_completion_rate,
            price_rate=self.context.model_price_rate,
        )


def record_llm_usage(
    *,
    model_name: str,
    stream_mode: bool,
    token_input: int = 0,
    token_output: int = 0,
    token_total: int = 0,
    status: LLMUsageStatus = LLMUsageStatus.SUCCESS,
    error_type: str = "",
    finish_reason: str = "",
    generation_time_ms: int = 0,
    context: Optional[LLMUsageContext] = None,
) -> None:
    """记一行用量流水（**非阻塞、永不抛**）

    Args:
        context: 归属声明；`None` 时取当前 contextvar（正常路径都走这条）。

    调用点在 `services/agent/openai.py` 的三个 usage 读取处。**每一处都必须有一条
    "删掉这次记账就变红"的用例**，见 `tests/test_llm_usage_metering.py`。
    """
    global _dropped_count  # noqa: PLW0603
    try:
        draft = LLMUsageDraft(
            context=context if context is not None else current_usage_context(),
            model_name=model_name,
            stream_mode=stream_mode,
            token_input=max(0, int(token_input or 0)),
            token_output=max(0, int(token_output or 0)),
            token_total=max(0, int(token_total or 0)),
            status=status,
            error_type=error_type,
            finish_reason=finish_reason,
            generation_time_ms=max(0, int(generation_time_ms or 0)),
        )
        if len(_pending) >= _MAX_PENDING:
            _dropped_count += 1
            logger.warning(
                f"用量流水积压超过 {_MAX_PENDING} 条，本条丢弃（累计丢弃 {_dropped_count} 条）："
                f"model={draft.model_name} scene={draft.context.scene.value}",
            )
            return
        # 先问有没有运行中的循环，**再**构造协程。反过来写（`create_task(_write_record(...))`
        # 放在 try 里靠 RuntimeError 兜底）会在没有循环时留下一个从未被 await 的协程对象，
        # 于是每条丢弃的记录都附赠一条 `coroutine was never awaited` 的 RuntimeWarning。
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 没有运行中的事件循环（同步上下文里被调到）。记账是观测，缺一条不能让调用方炸。
            _dropped_count += 1
            logger.warning(f"没有可用的事件循环，用量流水未落库: model={model_name}")
            return
        task = loop.create_task(_write_record(draft))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    except Exception as e:  # noqa: BLE001
        _dropped_count += 1
        logger.warning(f"用量流水入队失败[{type(e).__name__}]，已忽略: {e}")


async def flush_llm_usage(timeout: float = 10.0) -> int:
    """等待所有在飞的落库任务结束，返回本次等到的任务数

    两个用处：进程收尾时把最后几条写完；测试里让"异步落库"变成确定性的。
    """
    tasks = list(_pending)
    if not tasks:
        return 0
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning(f"等待用量流水落库超时（{timeout}s），仍有 {len(_pending)} 条在飞")
    return len(tasks)


def usage_recorder_stats() -> Tuple[int, int, int]:
    """`(在飞条数, 累计丢弃条数, 累计落库失败条数)`

    丢弃与失败都不是静默的：这三个数是"账本自己有没有漏"的唯一凭据。
    """
    return len(_pending), _dropped_count, _failed_count


def reset_usage_recorder() -> None:
    """清空计数与在飞集合（**仅供测试**，产品代码不要调）"""
    global _dropped_count, _failed_count  # noqa: PLW0603
    _pending.clear()
    _dropped_count = 0
    _failed_count = 0


async def _resolve_attribution(context: LLMUsageContext) -> Tuple[str, str, str, Optional[int]]:
    """把声明的归属补全成 `(adapter_key, instance_key, domain_id, tenant_id)`

    三步全部要查库，因此只在后台任务里跑：

    1. 只声明了 `chat_key` 时，按频道行取出 `(adapter_key, instance_key)`。
       **不解析 `chat_key` 字符串**——实例段的规则是适配器私有的（见 §1.1）。
    2. 域走 `identity_scope.resolve_memory_domain`，字符串编码只由 `ScopeDomain` 产生。
    3. 租户取实例行上的 `tenant_id`，是**写入时的快照**：实例改归属之后旧账不回溯。
       实例反查不出来（这一跳压根没有实例，例如总后台的连通性测试）时，退回调用方
       在 `LLMUsageContext.tenant_id` 上显式声明的那个租户；两者都没有才是 NULL。
       **顺序不能反**：主链路的归属永远以频道/实例这条链为准。

    任何一步查不到都按"补不全"处理并返回已知的部分——记一行归属不全的账，
    好过为了归属完整而丢掉一次真实花销。
    """
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance
    from nekro_agent.models.db_chat_channel import DBChatChannel
    from nekro_agent.services.identity_scope import resolve_memory_domain

    adapter_key = context.adapter_key
    instance_key = context.instance_key
    domain_id = context.domain_id
    tenant_id: Optional[int] = context.tenant_id

    if not adapter_key and context.chat_key:
        channel = await DBChatChannel.filter(chat_key=context.chat_key).order_by("id").first()
        if channel is not None:
            adapter_key = channel.adapter_key
            instance_key = channel.instance_key

    if adapter_key:
        if not domain_id:
            domain_id = (await resolve_memory_domain(adapter_key, instance_key)).domain_id
        instance = await DBAdapterInstance.filter(adapter_key=adapter_key, instance_key=instance_key).first()
        if instance is not None:
            tenant_id = instance.tenant_id

    return adapter_key, instance_key, domain_id, tenant_id


async def _write_record(draft: LLMUsageDraft) -> None:
    """后台落库；任何异常都只打 WARNING（见模块文档的第 2 条约束）"""
    global _failed_count  # noqa: PLW0603
    try:
        from nekro_agent.models.db_llm_usage import DBLLMUsageRecord

        context = draft.context
        adapter_key, instance_key, domain_id, tenant_id = await _resolve_attribution(context)
        await DBLLMUsageRecord.create(
            adapter_key=adapter_key,
            instance_key=instance_key,
            domain_id=domain_id,
            tenant_id=tenant_id,
            chat_key=context.chat_key,
            person_uid=context.person_uid,
            preset_id=context.preset_id,
            model_group_name=context.model_group_name,
            model_name=draft.model_name,
            stream_mode=draft.stream_mode,
            token_input=draft.token_input,
            token_output=draft.token_output,
            token_total=draft.token_total,
            token_input_rate=context.token_input_rate,
            token_completion_rate=context.token_completion_rate,
            model_price_rate=context.model_price_rate,
            cost=draft.cost,
            scene=context.scene.value,
            status=draft.status.value,
            error_type=draft.error_type,
            finish_reason=draft.finish_reason,
            attempt_index=context.attempt_index,
            attempt_total=context.attempt_total,
            is_fallback=context.is_fallback,
            generation_time_ms=draft.generation_time_ms,
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        _failed_count += 1
        logger.warning(
            f"用量流水落库失败[{type(e).__name__}]，已忽略（累计 {_failed_count} 条）："
            f"model={draft.model_name} scene={draft.context.scene.value} | {e}",
        )


__all__ = [
    "LLMUsageDraft",
    "LLMUsageScene",
    "LLMUsageStatus",
    "compute_token_cost",
    "flush_llm_usage",
    "record_llm_usage",
    "reset_usage_recorder",
    "usage_recorder_stats",
]
