"""节日祝福邮件：什么时候发、正文写什么

## 这里只负责两件事

1. **什么时候**：`_festival_callback` 在全局开关判过之后起一个后台任务，不阻塞群推送；
2. **正文是什么**：按实例的人设与模型组让模型写一封祝福邮件（每个实例一次调用）。

名单、入队、匀速发出、暂停作废都在核心 `services/email_outbox.py` 里：那些是归属与配额的事，
换一个插件也要守同样的规则。这里**不自己算收件人**。

## 给模型的消息里只有人设、节日名与说明

提示词刻意不带任何聊天内容、不带收件人信息：正文会被发给名单上的每一个人，模型从聊天里读到的
「今天小明失恋了」之类不该出现在给别人的祝福邮件里；带上收件人也会让同一批信各不相同，
既不好解释也更容易被当成垃圾邮件。

## 输出过滤

与出站消息同一套规则（`services/output_filter`）：`fragment` 模式把命中的片段换掉；
`whole` 模式命中一处就整批不发——整封换成替换语发出去没有意义，反而更像垃圾邮件。
引擎报错按 `whole` 处理（fail-closed），与出站那条路径同一条取舍。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Optional, Tuple

from nekro_agent.core.config import config as core_config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import TOMBSTONE_STATUS, DBAdapterInstance
from nekro_agent.services import email_outbox
from nekro_agent.services.agent.openai import gen_openai_chat_response
from nekro_agent.services.config_resolver import config_resolver
from nekro_agent.services.output_filter.engine import filter_text
from nekro_agent.services.output_filter.service import settings_for_instance
from nekro_agent.services.usage import LLMUsageContext, LLMUsageScene, usage_scope
from nekro_agent.tools.tenant_egress import is_tenant_owned_group

logger = get_sub_logger("festival_mail")

# 正文与标题的长度上限（超了截断，不让一次模型输出撑爆队列行）
SUBJECT_MAX_CHARS = 40
BODY_MAX_CHARS = 400
# 提示词里要求的标题长度（与截断上限一致，模型偶尔会超）
SUBJECT_PROMPT_LIMIT = 40

SYSTEM_PROMPT_SUFFIX = "你要为用户写一封节日祝福邮件。"


def _fallback_subject(festival_name: str) -> str:
    return f"{festival_name}祝福"[:SUBJECT_MAX_CHARS]


def _build_prompt(festival_name: str, festival_desc: str) -> str:
    """给模型的那一条用户消息：只有节日名与说明，没有任何聊天内容与收件人信息"""
    desc = (festival_desc or "").strip() or "这是一个值得祝福的日子"
    return (
        f"为『{festival_name}』写一封给用户的节日祝福邮件，{desc}，不超过 300 字，不要提到具体的人。"
        f'只输出 JSON：{{"subject": "不超过 {SUBJECT_PROMPT_LIMIT} 字的标题", "body": "正文"}}'
    )


def _parse_generated(raw: str, festival_name: str) -> Tuple[str, str]:
    """解析模型输出；解析失败或字段为空时退回「节日名 + 祝福」与模型原文"""
    text = (raw or "").strip()
    subject = ""
    body = ""
    try:
        # 模型常把 JSON 包在 ```json 里，先剥掉围栏
        payload = text
        if payload.startswith("```"):
            payload = payload.split("\n", 1)[1] if "\n" in payload else payload
            payload = payload.rsplit("```", 1)[0]
        data = json.loads(payload.strip())
        if isinstance(data, dict):
            subject = str(data.get("subject") or "").strip()
            body = str(data.get("body") or "").strip()
    except Exception:  # noqa: BLE001 - 解析失败不是错误，按原文用
        subject = ""
        body = ""

    if not subject:
        subject = _fallback_subject(festival_name)
    if not body:
        body = text
    return subject[:SUBJECT_MAX_CHARS], body[:BODY_MAX_CHARS]


def _apply_filter(adapter_key: str, instance_key: str, subject: str, body: str) -> Optional[Tuple[str, str]]:
    """按实例的输出过滤设置处理标题与正文；返回 None 表示整批不发

    `fragment`：标题与正文各跑一次，用过滤后的文字；
    `whole`：只要命中一处（或引擎报错）就整批不发——整封换成替换语没有意义。
    """
    settings = settings_for_instance(adapter_key, instance_key)
    if settings is None:
        return subject, body
    try:
        subject_result = filter_text(subject, settings.rules, settings.replacement)
        body_result = filter_text(body, settings.rules, settings.replacement)
    except Exception:  # noqa: BLE001 - 引擎报错按 whole 处理（fail-closed）
        logger.warning(f"节日邮件输出过滤引擎报错，这个实例整批不发: {adapter_key}/{instance_key}")
        return None
    if settings.mode == "whole":
        if subject_result.hit or body_result.hit:
            logger.info(f"节日邮件输出过滤命中（整封模式），这个实例整批不发: {adapter_key}/{instance_key}")
            return None
        return subject, body
    return subject_result.text, body_result.text


async def _generate_body(adapter_key: str, instance_key: str, festival_name: str, festival_desc: str) -> Tuple[str, str]:
    """这个实例的正文：人设走实例 → 系统默认 → 内置，模型组用主对话那个

    一次调用归这个实例的账（`LLMUsageScene.PLUGIN`，没有频道）：节日邮件不是任何一个人在
    某个群里说话触发的，硬塞一个 `chat_key` 只会把额度算到不相干的会话上。
    """
    from nekro_agent.services.preset_service import resolve_instance_preset

    preset = await resolve_instance_preset(adapter_key, instance_key)
    preset_content = str(getattr(preset, "content", "") or "").strip()

    effective = config_resolver.get_instance_effective_config(adapter_key, instance_key)
    group_name = effective.USE_MODEL_GROUP
    group = effective.MODEL_GROUPS.get(group_name)
    if group is None:
        logger.warning(f"节日邮件：模型组 {group_name} 不在配置里，这个实例跳过: {adapter_key}/{instance_key}")
        raise ValueError(f"模型组 '{group_name}' 未找到，请检查配置")

    messages = [
        {"role": "system", "content": f"{preset_content}\n\n{SYSTEM_PROMPT_SUFFIX}".strip()},
        {"role": "user", "content": _build_prompt(festival_name, festival_desc)},
    ]
    with usage_scope(
        LLMUsageContext(
            scene=LLMUsageScene.PLUGIN,
            adapter_key=adapter_key,
            instance_key=instance_key,
        ).with_model_group(group_name, group),
    ):
        response = await gen_openai_chat_response(
            model=group.CHAT_MODEL,
            messages=messages,
            base_url=group.BASE_URL,
            api_key=group.API_KEY,
            stream_mode=False,
            api_format=group.API_FORMAT,
            tenant_egress=is_tenant_owned_group(group),
        )
    return _parse_generated(response.response_content, festival_name)


def _not_ready_reason(adapter_key: str, instance_key: str) -> str:
    """`instance_festival_mail_ready` 为 None 时，日志要写明是哪一条不满足"""
    if not core_config.EMAIL_FESTIVAL_MAIL_ENABLED:
        return "节日邮件总闸关着"
    return "实例不可用、两个插件没都生效，或没有能用的发件人"


async def enqueue_festival_mail(festival_name: str, festival_desc: str) -> None:
    """把这次节日的邮件写进队列（每个实例一批）

    任何一个实例出问题都不影响别的实例：整批跳过并记 INFO 写明原因，不让一次模型调用失败
    把剩下所有实例的祝福一起拖没。
    """
    if not bool(core_config.EMAIL_FESTIVAL_MAIL_ENABLED):
        logger.info(f"节日邮件总闸关着，跳过入队: {festival_name}")
        return

    date_str = date.today().isoformat()
    instances = await DBAdapterInstance.exclude(status=TOMBSTONE_STATUS).order_by("id")
    for instance in instances:
        adapter_key = instance.adapter_key
        instance_key = instance.instance_key
        sender = await email_outbox.instance_festival_mail_ready(adapter_key, instance_key)
        if sender is None:
            logger.info(
                f"节日邮件跳过实例 {adapter_key}/{instance_key}：{_not_ready_reason(adapter_key, instance_key)}",
            )
            continue
        recipients = await email_outbox.festival_recipients(adapter_key, instance_key)
        if not recipients:
            logger.info(f"节日邮件跳过实例 {adapter_key}/{instance_key}：名单为空")
            continue
        try:
            subject, body = await _generate_body(adapter_key, instance_key, festival_name, festival_desc)
        except Exception as e:  # noqa: BLE001 - 一个实例的模型调用失败不该拖掉别的实例
            logger.warning(f"节日邮件生成正文失败，跳过实例 {adapter_key}/{instance_key}: {type(e).__name__}: {e}")
            continue
        filtered = _apply_filter(adapter_key, instance_key, subject, body)
        if filtered is None:
            continue
        subject, body = filtered
        queued = await email_outbox.enqueue_festival_batch(
            adapter_key=adapter_key,
            instance_key=instance_key,
            festival_name=festival_name,
            date_str=date_str,
            sender=sender,
            recipients=recipients,
            subject=subject,
            body=body,
        )
        logger.info(
            f"节日邮件入队：实例={adapter_key}/{instance_key} | 节日={festival_name} | "
            f"收件人={len(recipients)} | 入队={queued}",
        )


__all__ = [
    "BODY_MAX_CHARS",
    "SUBJECT_MAX_CHARS",
    "enqueue_festival_mail",
]
