"""角色状态插件的挂载点：状态块注入、世界书模板的变量来源、回合收尾、群聊计数、频道重置

- **注入**：状态块按 `STATE_PROMPT_MAX_TOKENS`（核心配置，可按实例 / 频道覆盖、租户只能往下调）渲染，
  返回 `PromptInjection(untrusted=True)`，由核心清洗并用本轮令牌包成不可信块，默认插在最新一条消息
  之前。上一轮被拦时不注入（核心也会统一丢掉）。提示词明细只记计数与组名，不记值。
- **变量来源**：在注册表里登记一个工厂，插件在这个频道生效时给出这一轮的状态视图；不生效、没有模板时
  返回 None（世界书照没有变量时算）。工厂只在异步部分查库，给出的提供者只读内存。
- **回合收尾**：私聊、结局为 ok 时排一次更新（只建任务，回调立刻返回）。
- **群聊计数**：每条用户消息数一次，到 N 条排一次更新。
- **频道重置**：按配置把频道状态清回初值并写一份快照；个人档案不动。
"""

from __future__ import annotations

from typing import Any, Optional

from nekro_agent.api.plugin import PromptInjection
from nekro_agent.core.config import config as global_config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_state_channel import DBStateChannel
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.chat_message import ChatMessage, ChatType
from nekro_agent.services.affection.suppression import is_narrative_suppressed
from nekro_agent.services.identity_scope import resolve_preset_id
from nekro_agent.services.memory import prompt_rounds
from nekro_agent.services.plugin.scope import plugin_effective_with_instance
from nekro_agent.services.tavern import runtime as tavern_runtime
from nekro_agent.services.worldbook import template_lite, tokens
from nekro_agent.services.worldbook.variables import RoundVariableContext, register_variable_source

from . import card, inline, session, store, updater
from .plugin import person_scope_of, plugin
from .render import BlockInput, elapsed_line, render_state_block

logger = get_sub_logger("state_table")

PREFACE = "下面是故事的当前状态，由系统维护，只作参考；不要在回复里复述它，也不要输出状态的格式。"


def _effective(channel: Any) -> bool:
    return bool(
        plugin.is_enabled
        and plugin_effective_with_instance(
            plugin.key, str(getattr(channel, "instance_key", "") or ""), str(channel.chat_key)
        ),
    )


async def _channel_of(ctx: AgentCtx) -> Optional[DBChatChannel]:
    channel = ctx.db_chat_channel
    if channel is not None:
        return channel
    try:
        chat_key = ctx.chat_key
    except (AttributeError, ValueError):
        return None
    return await DBChatChannel.filter(chat_key=chat_key).order_by("id").first()


async def _load(
    *,
    code: str,
    channel: DBChatChannel,
    config: Any,
    trigger_uid: str,
    preset_id: Optional[int] = None,
    dry_run: bool = False,
) -> Optional[session.RoundState]:
    return await session.load_round_state(
        one_time_code=code,
        channel=channel,
        preset_id=preset_id,
        config=config,
        trigger_uid=trigger_uid,
        platform_words=updater.platform_words(),
        extra_words=updater.runtime_words(str(channel.instance_key or "")),
        expand=tavern_runtime.expander(code) if code else None,
        dry_run=dry_run,
        person_scope=person_scope_of(updater.scoped_config(str(channel.instance_key or ""))),
    )


@plugin.mount_prompt_inject_method("state_block", "角色状态块")
async def state_block(ctx: AgentCtx) -> Any:
    """状态块注入；自己出的错自己接住（返回空、明细里记 `error`），不算到整个插件的隔离计数上

    这个插件的错多半出在某一个频道的数据上（初值、模板），不该因为一个频道把整个插件对所有频道停掉。
    """
    code = str(getattr(ctx, "one_time_code", "") or "")
    try:
        return await _state_block(ctx, code)
    except Exception as error:  # noqa: BLE001
        logger.warning(f"状态块渲染失败，这一轮不注入: error={type(error).__name__}")
        if code:
            prompt_rounds.note_state(code, state="error")
        return ""


async def _state_block(ctx: AgentCtx, code: str) -> Any:
    channel = await _channel_of(ctx)
    if channel is None or not code:
        return ""
    if is_narrative_suppressed(channel.chat_key, global_config.AI_CHAT_CONTEXT_EXPIRE_SECONDS):
        prompt_rounds.note_state(code, state="suppressed")
        return ""
    config = await ctx.get_core_config()
    budget = int(getattr(config, "STATE_PROMPT_MAX_TOKENS", 1200) or 0)
    state = await _load(code=code, channel=channel, config=config, trigger_uid=str(ctx.from_platform_userid or ""))
    if state is None:
        prompt_rounds.note_state(code, state="no_template")
        return ""
    await tokens.ensure_loaded_async()
    factor = tokens.factor_for_group(config.MODEL_GROUPS.get(config.USE_MODEL_GROUP)).value
    block = render_state_block(
        BlockInput(
            template=state.template,
            channel=state.channel,
            people=state.people,
            is_private=state.is_private,
            affection_enabled=state.affection_enabled,
            elapsed_line=elapsed_line(state.elapsed_seconds),
            budget_tokens=budget,
            count_tokens=lambda text: tokens.count_tokens(text, factor),
            private_words=state.private_words,
        ),
    )
    cfg = updater.scoped_config(str(channel.instance_key or ""))
    depth = None if cfg.STATE_POSITION == "history_top" else int(cfg.STATE_DEPTH)
    row = state.channel_row
    prompt_rounds.note_state(
        code,
        state="injected" if block.text else "empty",
        budget=budget,
        position="history_top" if depth is None else f"depth:{depth}",
        paused=bool(row.paused) if row is not None else False,
        last_update=updater.last_results.get(channel.chat_key),
        **block.meta(),
    )
    if not block.text:
        return ""
    return PromptInjection(text=block.text, untrusted=True, kind="StoryState", preface=PREFACE, depth=depth)


async def variable_source(ctx: RoundVariableContext) -> Optional[template_lite.VariableProvider]:
    """世界书模板的变量来源：插件在这个频道生效、有模板、有本轮令牌时给出这一轮的状态视图"""
    channel = ctx.channel
    if channel is None or not ctx.one_time_code or not _effective(channel):
        return None
    try:
        state = await _load(
            code=ctx.one_time_code,
            channel=channel,
            config=ctx.config,
            trigger_uid=ctx.trigger_person_uid,
            preset_id=ctx.preset_id,
            dry_run=ctx.dry_run,
        )
    except Exception as error:  # noqa: BLE001 - 装载失败按没有变量算（取消照常往外抛）
        logger.warning(f"状态装载失败，这一轮不给变量: error={type(error).__name__}")
        return None
    if state is None:
        return None
    prompt_rounds.note_state(ctx.one_time_code, provider=True)
    return session.provider_of(state)


register_variable_source(plugin.key, variable_source)


@plugin.mount_on_round_finished()
async def on_round_finished(ctx: AgentCtx, outcome: str) -> None:
    """回合收尾：先提交这一轮顺手更新与兼容解析记下的（只在结局为 ok 时）；私聊里这一轮没有由它们提交过，
    再排一次分步更新；最后按自动发图的档位看要不要发状态图

    拿不到本轮令牌时（`ctx.one_time_code` 为空）不碰任何按令牌存的东西，也不提交缓冲；分步更新与发图
    不靠令牌，照常。
    """
    code = str(getattr(ctx, "one_time_code", "") or "")
    if code:
        session.rounds.pop(code)
    try:
        committed = await inline.commit_round(ctx, outcome)
    except Exception as error:  # noqa: BLE001 - 这一轮的顺手改动提交失败不影响别的
        logger.warning(f"提交这一轮的顺手改动失败: error={type(error).__name__}")
        committed = False
    if outcome != "ok":
        return
    channel = await _channel_of(ctx)
    if channel is None:
        return
    private = getattr(channel, "chat_type", None) == ChatType.PRIVATE
    if private and not committed:
        updater.schedule(channel.chat_key)
    try:
        counterpart = str(getattr(ctx, "from_platform_userid", "") or "") if private else ""
        await card.after_round(ctx, channel, outcome, counterpart=counterpart)
    except Exception as error:  # noqa: BLE001 - 发图是附加的
        logger.warning(f"排自动发状态图失败: error={type(error).__name__}")


@plugin.mount_on_user_message()
async def on_user_message(ctx: AgentCtx, message: ChatMessage) -> None:  # noqa: ARG001 - 只数条数
    channel = await _channel_of(ctx)
    if channel is None or getattr(channel, "chat_type", None) == ChatType.PRIVATE:
        return
    config = await channel.get_effective_config()
    cfg = updater.scoped_config(str(channel.instance_key or ""))
    threshold = int(cfg.GROUP_UPDATE_EVERY_N) or int(getattr(config, "MEMORY_CONSOLIDATION_MSG_THRESHOLD", 50) or 50)
    if updater.note_user_message(channel.chat_key, threshold):
        updater.schedule(channel.chat_key)


@plugin.mount_on_channel_reset()
async def on_channel_reset(ctx: AgentCtx) -> None:
    channel = await _channel_of(ctx)
    if channel is None:
        return
    cfg = updater.scoped_config(str(channel.instance_key or ""))
    if not cfg.RESET_ON_CHANNEL_RESET:
        return
    preset_id = await resolve_preset_id(channel)
    row = await store.get_channel(channel.chat_key, preset_id)
    if row is None:
        return
    await reset_channel_state(row, editor="", source="reset", keep=int(cfg.SNAPSHOT_KEEP))


async def reset_channel_state(
    row: DBStateChannel,
    *,
    editor: str,
    source: str,
    keep: int,
) -> bool:
    """把这一行频道状态清回它那个人设的模板初值（个人档案不动）；没有模板或版本冲突时返回 False

    按行操作：一个频道换过人设会有几行，管理员点的是哪一行就重置哪一行，不是频道当前人设的那一行。
    重置不在一轮里、拿不到宏上下文，初值里的宏保持原文。
    """
    template_row, template = await store.load_template(int(row.preset_id))
    if template is None or template_row is None:
        return False
    channel_part, _person = store.split_initial(template, dict(template_row.initial or {}))
    if not await store.commit_channel(row, channel_part, consecutive_failures=0, paused=False):
        return False
    await store.write_snapshot(
        target_kind="channel",
        target_id=int(row.id),
        chat_key=str(row.chat_key),
        adapter_key=str(row.adapter_key or ""),
        instance_key=str(row.instance_key or ""),
        source=source,
        result="applied",
        data=channel_part,
        editor=editor,
        keep=keep,
    )
    return True


__all__ = [
    "PREFACE",
    "on_channel_reset",
    "on_round_finished",
    "on_user_message",
    "reset_channel_state",
    "state_block",
    "variable_source",
]
