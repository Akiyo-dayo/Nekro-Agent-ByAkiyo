"""提示词试发：用管理员自己的模型组发一次，不碰频道

## 为什么只发一次

试发不是聊天回合：没有迭代、没有备用组、没有沙盒。发一次、把回复解析出来给管理员看，
就结束。唯一的第二次请求是「带预填被上游按请求内容拒绝」时的去预填重发——它与正式
回合里的那段修复同源，判据也共用（见 `run_agent.send_agent_request`）。

## 归属与不落痕迹

账本照记：场景 `prompt_trial`、实例对与模型组都写上，但 `chat_key` 留空。写侧只在
`chat_key` 非空时累加按群额度，因此试发不占任何群的额度；频道标识只进日志。这里不写
聊天记录、不进记忆、不写提示词明细，也不执行解析出来的代码。
"""

from __future__ import annotations

import time
from typing import Optional

from nekro_agent.core.config import CoreConfig, ModelConfigGroup
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.schemas.tavern import TavernTrialResponse, TavernTrialUsage
from nekro_agent.schemas.usage import LLMUsageScene
from nekro_agent.services.agent.model_capability import (
    _CONTENT_REJECT_STATUS,
    LLMFailureKind,
    _extract_status_code,
    classify_llm_failure,
    note_capability_ok,
    note_capability_unsupported,
)
from nekro_agent.services.agent.openai import OpenAIResponse, gen_openai_chat_response
from nekro_agent.services.agent.prefill import PREFILL_CAPABILITY, merge_prefill, prefill_message
from nekro_agent.services.agent.resolver import parse_chat_response
from nekro_agent.services.agent.round_prompt import RoundPrompt
from nekro_agent.services.agent.run_agent import detect_provider_refusal, is_code_missing
from nekro_agent.services.agent.upstream_redaction import collect_llm_secret_values, redact_upstream_text
from nekro_agent.services.usage.context import LLMUsageContext, usage_scope
from nekro_agent.tools.tenant_egress import is_tenant_owned_group

logger = get_sub_logger("tavern.trial")

# 失败原因回给前端时的上限。与 `run_agent` 的状态摘要同一个量级：够排障，又不至于把
# 上游整页 HTML 或长堆栈倒进后台页面。
ERROR_TEXT_LIMIT = 160


def _safe_error_text(error: Exception, group: ModelConfigGroup) -> str:
    """脱敏并压成一行；凭据按本次模型组的值遮蔽"""
    safe = redact_upstream_text(
        str(error),
        collect_llm_secret_values(group.API_KEY, group.CHAT_PROXY),
    )
    compact = " ".join(safe.strip().split())
    return compact if len(compact) <= ERROR_TEXT_LIMIT else compact[: ERROR_TEXT_LIMIT - 1] + "…"


def _trial_usage(response: OpenAIResponse) -> TavernTrialUsage:
    return TavernTrialUsage(
        input_tokens=int(response.token_input or 0),
        output_tokens=int(response.token_output or 0),
        total_tokens=int(response.token_consumption or 0),
    )


def _initial_prefill_state(prompt: RoundPrompt) -> str:
    """这次试发「有没有预填」的起点

    槽位关着且原文非空记 `gated`，没有文字记 `empty`；有文字则等发送侧给出最终状态。
    """
    if prompt.prefill is not None:
        return "pending"
    slots = prompt.tavern.slots
    if slots is not None and not slots.enabled and slots.prefill.text:
        return "gated"
    return "empty"


async def _call_model(
    *,
    messages: list,
    group: ModelConfigGroup,
    config: CoreConfig,
    usage_context: LLMUsageContext,
) -> OpenAIResponse:
    """按正式请求同形的参数发一次；没有迭代、备用组与日志文件"""
    with usage_scope(usage_context):
        return await gen_openai_chat_response(
            model=group.CHAT_MODEL,
            messages=messages,
            temperature=group.TEMPERATURE,
            top_p=group.TOP_P,
            top_k=group.TOP_K,
            frequency_penalty=group.FREQUENCY_PENALTY,
            presence_penalty=group.PRESENCE_PENALTY,
            extra_body=group.EXTRA_BODY,
            base_url=group.BASE_URL,
            api_key=group.API_KEY,
            stream_mode=config.AI_REQUEST_STREAM_MODE,
            proxy_url=group.CHAT_PROXY,
            max_wait_time=config.AI_GENERATE_TIMEOUT,
            first_token_timeout=config.AI_STREAM_FIRST_TOKEN_TIMEOUT,
            api_format=group.API_FORMAT,
            tenant_egress=is_tenant_owned_group(group),
        )


async def run_trial(
    *,
    prompt: RoundPrompt,
    channel: DBChatChannel,
    group: ModelConfigGroup,
    group_name: str,
    config: CoreConfig,
    operator: str,
) -> TavernTrialResponse:
    """发一次并组装试发响应

    Args:
        prompt: `build_round_prompt(dry_run=True, model_group_override=group, ...)` 的结果。
        channel: 这次试发借用的频道；只用它的适配器与实例归属，不往里写任何东西。
        group: 真正要用的运行时模型组。
        group_name: 它在 `config.MODEL_GROUPS` 里的键，账本按这个名记。
        config: 频道生效配置。
        operator: 发起人的用户名，只进那一行 INFO 日志。
    """
    started = time.perf_counter()
    messages: list = list(prompt.messages)
    prefill_state = _initial_prefill_state(prompt)
    sent_prefill = False
    if prompt.prefill is not None and prompt.prefill.text:
        payload = prefill_message(group, prompt.prefill.text)
        if payload is not None:
            messages.append(payload)
            sent_prefill = True
        else:
            prefill_state = "unsupported"
    elif prefill_state == "pending":
        prefill_state = "empty"

    # 显式带上实例的租户快照：账本写侧虽然也会按实例反查，但试发是「没有 chat_key」的
    # 一跳，把归属在这里写全，读侧不必依赖反查成功。实例行不在时留空，平台方即如此。
    instance = await DBAdapterInstance.get_or_none(
        adapter_key=channel.adapter_key,
        instance_key=channel.instance_key,
    )
    usage_context = (
        LLMUsageContext(
            scene=LLMUsageScene.PROMPT_TRIAL,
            adapter_key=channel.adapter_key,
            instance_key=channel.instance_key,
            chat_key="",
            tenant_id=instance.tenant_id if instance is not None else None,
        )
        .with_model_group(group_name, group)
        .for_attempt(index=1, total=1)
    )

    response: Optional[OpenAIResponse] = None
    error: Optional[Exception] = None
    used_prefill = sent_prefill
    try:
        response = await _call_model(messages=messages, group=group, config=config, usage_context=usage_context)
    except Exception as exc:  # noqa: BLE001 - 分类后回给管理员，不让接口 500
        kind = classify_llm_failure(exc, request_has_image=False)
        if (
            sent_prefill
            and kind is not LLMFailureKind.VISION_UNSUPPORTED
            and _extract_status_code(exc) in _CONTENT_REJECT_STATUS
        ):
            # 带预填的请求被当成请求内容不合法：同组去掉预填再发一次，并在这个组的 TTL
            # 内不再发预填。判据与正式回合完全一致，不靠「所有 400 都猜是预填」。
            note_capability_unsupported(group, PREFILL_CAPABILITY)
            prefill_state = "retried_without"
            used_prefill = False
            try:
                response = await _call_model(
                    messages=list(prompt.messages),
                    group=group,
                    config=config,
                    usage_context=usage_context,
                )
            except Exception as retry_exc:  # noqa: BLE001
                error = retry_exc
        else:
            error = exc

    latency_ms = int((time.perf_counter() - started) * 1000)
    if error is not None or response is None:
        failure = error if error is not None else RuntimeError("试发请求失败")
        kind = classify_llm_failure(failure, request_has_image=False)
        logger.info(
            f"[tavern.trial] 试发结束 | operator={operator} instance={channel.instance_key} "
            f"channel={channel.chat_key} group={group_name} outcome={kind.value}",
        )
        return TavernTrialResponse(
            model=group.CHAT_MODEL,
            model_group=group_name,
            latency_ms=latency_ms,
            error_kind=kind.value,
            error=_safe_error_text(failure, group),
            prefill_state=prefill_state,
        )

    if used_prefill and prompt.prefill is not None:
        response.response_content = merge_prefill(response.response_content, prompt.prefill.text)
        prefill_state = "sent"
        note_capability_ok(group, PREFILL_CAPABILITY)

    merged_content = response.response_content
    parsed = parse_chat_response(merged_content)
    refusal = detect_provider_refusal(parsed, response)
    logger.info(
        f"[tavern.trial] 试发结束 | operator={operator} instance={channel.instance_key} "
        f"channel={channel.chat_key} group={group_name} outcome={'refusal' if refusal else 'ok'}",
    )
    return TavernTrialResponse(
        model=response.use_model or group.CHAT_MODEL,
        model_group=group_name,
        latency_ms=latency_ms,
        reply=parsed.raw_content or merged_content,
        code="" if refusal else parsed.code_content,
        code_missing=is_code_missing(parsed),
        refusal=refusal or "",
        usage=_trial_usage(response),
        prefill_state=prefill_state,
    )


__all__ = ["run_trial"]
