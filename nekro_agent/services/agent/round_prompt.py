"""一轮提示词的组装：从生效配置到系统 / 示例 / 历史三段消息

## 为什么从 `run_agent` 抽出来

提示词预览与试发要按某个频道当前的布置渲染出**同一份**消息，但它们不跑一轮：不登记会话
令牌、不开运维回合、不进沙盒、不写明细。把「取配置 → 人设 → 插件 → 好感 → 世界书 →
运维说明 → 编译器 → 三段消息」原样搬到这里，真实一轮与预览走同一条路径，预览看到的东西
就不会与真发出去的慢慢分叉。

「真的要跑一轮」才做的事留在 `run_agent`：`register_session_token`、`open_ops_round`、
运行阶段广播、发送、迭代与沙盒。

## `dry_run` 的差别

只挡有副作用的那几步，其余照常：好感块只读渲染但不排评估；插件静态说明照常渲染、动态
注入换成一行占位；历史渲染不写运维守卫记录、不跑记忆召回；世界书照常求值但收尾按
`cancelled` 丢弃定时锚点并弹缓存；酒馆上下文同样弹掉；`prompt_rounds` 因为预览的令牌
从不 `begin`，所有 `note_*` 天然是空操作。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional

from nekro_agent.adapters.interface.schemas.platform import PlatformUser
from nekro_agent.core.config import CoreConfig, ModelConfigGroup
from nekro_agent.core.config import config as global_config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_chat_channel import DBChatChannel, DefaultPreset
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_rp_preset import DBRpPreset
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.chat_message import ChatMessage, ChatType
from nekro_agent.services.affection.evaluator import schedule_affection_evaluation
from nekro_agent.services.affection.prompt import render_affection_prompt
from nekro_agent.services.affection.suppression import is_narrative_suppressed
from nekro_agent.services.memory import prompt_rounds
from nekro_agent.services.model_group_service import enforce_channel_model_group_scope
from nekro_agent.services.ops_agent.provider import render_prompt_section as render_ops_prompt_section
from nekro_agent.services.plugin.call_priority import build_plugin_call_priority_rules
from nekro_agent.services.plugin.prompt_activation import build_plugin_activation_rules
from nekro_agent.services.plugin.scope import resolve_active_plugins
from nekro_agent.services.tavern import greeting as tavern_greeting
from nekro_agent.services.tavern import runtime as tavern_runtime
from nekro_agent.services.tavern.rp_layout import RpLayout, build_layout
from nekro_agent.services.tavern.rp_preset import segments_from_stored
from nekro_agent.services.worldbook import render as wb_render
from nekro_agent.services.worldbook import runtime as wb_runtime
from nekro_agent.services.worldbook import variables as wb_variables
from nekro_agent.services.worldbook.selection import Hit

from .creator import OpenAIChatMessage
from .model_capability import is_capability_marked
from .prefill import PREFILL_CAPABILITY, PrefillSpec, prefill_message
from .templates.compiler import PromptCompiler
from .templates.plugin import render_plugins_prompt

logger = get_sub_logger("agent_runtime")

# 模型组里会真的发预填的模式；与 `prefill._PREFILL_MODES` 同一个口径，这里只用来给预览
# 解释「为什么发 / 不发」，不在发送侧另起一套判据。
_PREFILL_MODES = frozenset({"assistant", "deepseek"})

# 这两个层是平台方写的。租户看预览时，只要某个槽位有一层来自这里，整段文字就换占位：
# 租户看不到平台方配置的槽位原文，但要知道「这里有一段、多少字」。
_PLATFORM_SLOT_LAYERS = frozenset({"system", "adapter"})

# 三个槽位在占位文字里的中文名。与设计稿的措辞一致，改这里等于改预览页显示的文字。
_SLOT_LABELS = {
    "post_history": "后置指令",
    "auxiliary": "辅助提示词",
    "prefill": "预填",
}


# 预览 / 试发时 Bot 不在线、取不到它自己的平台账号，提示词里那一处用这串占位。
OFFLINE_BOT_ID_PLACEHOLDER = "[Bot 当前不在线，平台账号未知]"


def _trigger_person_uid(trigger_message: Optional[ChatMessage]) -> str:
    """触发这一轮的人的平台账号；判不出（定时任务、预览）时为空串"""
    if trigger_message is None:
        return ""
    return str(getattr(trigger_message, "platform_userid", None) or getattr(trigger_message, "sender_id", "") or "")


async def _dry_run_self_info(ctx: AgentCtx, channel: DBChatChannel) -> tuple[PlatformUser, bool]:
    """预览 / 试发取 Bot 自己的信息；取不到时用占位，返回（信息, 是否用了占位）

    正式回合要靠这份信息告诉模型「你在平台上是谁」，取不到就该失败；但预览只是给管理员
    看提示词，实例掉线（协议端没连上）是常态，不能因此整页 500。这里兜住任何异常——
    适配器各自抛的类型不同，预览不该关心是哪一种——记 WARNING，换成占位继续组装。
    """
    try:
        return await ctx.adapter.get_self_info(channel.chat_key), False
    except Exception as exc:
        logger.warning(f"[round_prompt] {channel.chat_key} | 预览取不到 Bot 信息，改用占位: {type(exc).__name__}")
        return (
            PlatformUser(platform_name=channel.adapter_key, user_id=OFFLINE_BOT_ID_PLACEHOLDER, user_name=""),
            True,
        )


def _has_platform_layer(layers: Sequence[str]) -> bool:
    """这个槽位是否有任何一层来自平台方（全局 / 适配器）"""
    return any(layer in _PLATFORM_SLOT_LAYERS for layer in layers)


def _mask_slot_text(label: str, text: str) -> str:
    """平台方层槽位的占位文字：只报「有这段、多少字」，不带原文"""
    return f"[含平台方设置的{label}，共 {len(text)} 字]"



@dataclass
class RoundPrompt:
    """一轮（或一次预览）组装好的提示词与它用到的配置

    Attributes:
        messages: 系统 / 示例 / 历史三段消息，顺序就是发给模型的顺序。
        config: 频道的生效配置。
        preset: 本轮生效的人设；可能是内置默认人设（没有行 ID）。
        used_model_group: 本轮真正要用的模型组（试发时可能是调用方指定的组）。
        runtime_prompt: 适配器 + 插件的动态提示词；迭代时增量历史还要用它。
        prefill: 预填文字与发送状态；没有预填时是 `None`。
        tavern: 酒馆运行时上下文（人设正文、槽位、深度注入）。
        meta: 预览用的各段元数据；只有 `dry_run` 时填。
    """

    messages: list[OpenAIChatMessage]
    config: CoreConfig
    preset: DBPreset | DefaultPreset
    used_model_group: ModelConfigGroup
    runtime_prompt: str
    prefill: Optional[PrefillSpec]
    tavern: tavern_runtime.TavernRound
    meta: dict[str, Any]


async def build_round_prompt(
    *,
    chat_key: str,
    db_chat_channel: DBChatChannel,
    ctx: AgentCtx,
    one_time_code: str,
    trigger_message: Optional[ChatMessage],
    dry_run: bool = False,
    preset_override: Optional[DBPreset] = None,
    model_group_override: Optional[ModelConfigGroup] = None,
    extra_history_lines: Optional[list[str]] = None,
    hidden_book_ids: Optional[set[int]] = None,
    mask_platform_slots: bool = False,
    rp_preset_override: Optional[DBRpPreset] = None,
    lean_history: int = 0,
) -> RoundPrompt:
    """组装这一轮发给模型的提示词

    Args:
        chat_key: 频道键。
        db_chat_channel: 频道行。
        ctx: Agent 上下文；预览时调用方也要提供一个可用的上下文。
        one_time_code: 本轮一次性令牌（提示词里的边界标签与缓存键）。
        trigger_message: 触发这一轮的消息；取 `{{user}}` 用。
        dry_run: 预览 / 试发；见模块文档里的差别清单。
        preset_override: 预览时换一个人设看看；调用方已判过可见性。
        model_group_override: 试发时指定的模型组；只影响视觉能力判定、`ENABLE_COT`
            与返回值，不改配置里真实生效的组。
        extra_history_lines: 试发 / 预览时追加在聊天记录最后的那一句；格式由调用方负责。
        hidden_book_ids: 预览要遮正文的书 ID；这些书的条目正文换成固定占位，字数统计仍按原文。
        mask_platform_slots: 预览时把带平台方层的槽位文字换成「有这段、多少字」的占位。
            只允许在 `dry_run=True` 时打开：发给模型的永远是原文。
        rp_preset_override: 预览时指定一份角色扮演预设；调用方已判过可见性。仍要求
            酒馆运行时与高级提示词槽位都开着，否则不启用并在 `meta` 里给出原因。
        lean_history: 首次请求被上游按内容拦下后少带内容重发时传最近消息条数（>0）：
            不排好感评估、不发开场白、世界书只留常驻条目且不做语义检索、历史只带最后
            这么多条，返回的 `prefill` 置 None。0 表示正常一轮。

    Returns:
        组装结果；`dry_run` 时 `meta` 已填好。

    预览的所有临时状态都在这里收尾（世界书轮次缓存、酒馆上下文缓存）；真实一轮的收尾仍在
    `run_agent` 的 `finally` 里，与改造前一样。
    """
    if mask_platform_slots and not dry_run:
        # 这是接口层与组装层之间最后一道闸：真实一轮即使调用方传错了，也不能把占位发出去。
        raise ValueError("mask_platform_slots 只允许用于提示词预览")
    try:
        return await _build_round_prompt(
            chat_key=chat_key,
            db_chat_channel=db_chat_channel,
            ctx=ctx,
            one_time_code=one_time_code,
            trigger_message=trigger_message,
            dry_run=dry_run,
            preset_override=preset_override,
            model_group_override=model_group_override,
            extra_history_lines=extra_history_lines,
            hidden_book_ids=hidden_book_ids,
            mask_platform_slots=mask_platform_slots,
            rp_preset_override=rp_preset_override,
            lean_history=lean_history,
        )
    finally:
        if dry_run:
            # 预览不提交任何世界书定时状态：按 `cancelled` 丢掉本轮暂存锚点并弹缓存。
            wb_runtime.finish_round(one_time_code, "cancelled")
            tavern_runtime.discard_tavern_round(one_time_code)


async def _build_round_prompt(
    *,
    chat_key: str,
    db_chat_channel: DBChatChannel,
    ctx: AgentCtx,
    one_time_code: str,
    trigger_message: Optional[ChatMessage],
    dry_run: bool,
    preset_override: Optional[DBPreset],
    model_group_override: Optional[ModelConfigGroup],
    extra_history_lines: Optional[list[str]],
    hidden_book_ids: Optional[set[int]],
    mask_platform_slots: bool,
    rp_preset_override: Optional[DBRpPreset],
    lean_history: int,
) -> RoundPrompt:
    """实际组装；异常照原样抛给调用方，预览的缓存清理在 :func:`build_round_prompt` 里"""
    config = await db_chat_channel.get_effective_config()
    # 模型组归属的运行时兜底。接口层（`routers/config.py:_assert_tenant_may_write`）只管住
    # "从今往后"，而**存量覆盖文件里可能已经写着**越权的组名——补接口层挡不住已落盘的，
    # 而这一行下面立刻就要拿组名去取真实的 API Key。越界引用会被就地回落并打 WARNING，
    # 不抛异常：让 Bot 因为一条历史配置直接哑掉，对租户是不可诊断的故障。
    # 平台方实例与未启用多租户的部署在函数入口即返回，行为逐字不变。
    await enforce_channel_model_group_scope(
        config,
        instance_key=db_chat_channel.instance_key,
        adapter_key=db_chat_channel.adapter_key,
        chat_key=chat_key,
    )
    preset: DBPreset | DefaultPreset
    if preset_override is not None:
        preset = preset_override
    else:
        preset = await db_chat_channel.get_preset()

    # 酒馆运行时：人设正文、深度注入与三个槽位。闸关时它一次查询都不做，人设正文原样，
    # 所以下面所有步骤在闸关时与改造前逐字相同。
    tavern = await tavern_runtime.resolve_tavern_round(
        one_time_code,
        db_chat_channel,
        config,
        preset,
        trigger_message,
        dry_run=dry_run,
    )
    # 预览指定预设：仍要求酒馆运行时与高级提示词槽位都开着；不开时按没选处理，并在
    # `meta` 里说明原因，页面才能解释「为什么没有按这份预设渲染」。
    rp_preset_inactive_reason = ""
    if rp_preset_override is not None:
        if not tavern.enabled:
            rp_preset_inactive_reason = "tavern_off"
        elif tavern.slots is None or not tavern.slots.enabled:
            rp_preset_inactive_reason = "slots_off"
        else:
            tavern.rp_preset = rp_preset_override
        if rp_preset_inactive_reason:
            tavern.rp_preset = None
    elif dry_run and tavern.rp_preset is None and _configured_rp_preset_id(config) > 0:
        # 频道自己的配置里选了预设、这一轮却没用上：预览同样要说清原因，否则管理员只看到
        # 「选了但没效果」。正式一轮不记（那里靠运行时的 WARNING）。
        if not tavern.enabled:
            rp_preset_inactive_reason = "tavern_off"
        elif tavern.slots is None or not tavern.slots.enabled:
            rp_preset_inactive_reason = "slots_off"
        else:
            rp_preset_inactive_reason = "unavailable"
    # 开场白必须发在渲染历史之前：它进聊天记录之后，这一轮的历史里才看得到它，模型
    # 才能接着演。预览只判断与展开，不发也不写；闸关或解析失败时 `tavern.enabled` 为假，
    # 不碰任何记录。
    greeting_meta: Optional[dict[str, Any]] = None
    if dry_run:
        greeting_meta = await tavern_greeting.preview_greeting(
            channel=db_chat_channel,
            preset=preset,
            card=tavern.card,
            config=config,
            tavern=tavern,
        )
    elif tavern.enabled and lean_history <= 0:
        # 少带内容重发时开场白已经由第一次渲染记过，不再重复。
        await tavern_greeting.maybe_greet(
            channel=db_chat_channel,
            preset=preset,
            card=tavern.card,
            config=config,
            ctx=ctx,
            tavern=tavern,
            one_time_code=one_time_code,
        )

    # 宏展开函数与三个槽位的「打开后会发什么」要在遮罩之前取：遮罩会把本轮渲染的槽位文字
    # 换成占位，之后再也算不出真实字数。预览的 `slots_preview` 正需要那份真实文字与字数。
    tavern_expand = tavern_runtime.expander(one_time_code)
    # 只为量字数、生成明细与预览元数据再展开一次时用这个：不计入本轮宏统计、不记长度保护
    # 的 WARNING，否则同一段文字算两遍，没发出去的槽位文字还可能误报「触发长度保护」。
    tavern_measure = tavern_runtime.expander(one_time_code, record=False)
    slot_preview_texts = _slot_preview_texts(tavern, tavern_measure)
    if mask_platform_slots:
        _apply_platform_slot_mask(tavern, slot_preview_texts)

    adapter_dialog_examples = await ctx.adapter.set_dialog_example()
    adapter_jinja_env = await ctx.adapter.get_jinja_env()
    bot_offline = False
    if dry_run:
        self_info, bot_offline = await _dry_run_self_info(ctx, db_chat_channel)
    else:
        self_info = await ctx.adapter.get_self_info(chat_key)
    logger.debug(f"[round_prompt] {chat_key} | 适配器信息就绪，开始渲染插件 prompt")

    # 获取当前使用的模型组。试发允许指定一个组：只影响这一份提示词的视觉判定与返回值，
    # 频道配置里真实生效的组不动。
    used_model_group: ModelConfigGroup = (
        model_group_override if model_group_override is not None else config.MODEL_GROUPS[config.USE_MODEL_GROUP]
    )
    # 按 会话 > 实例 > 全局 解析本轮真正可见的插件集合；单实例部署下等价于全局开关
    active_plugins = await resolve_active_plugins(instance_key=db_chat_channel.instance_key, chat_key=chat_key)
    # 激活策略控制器本身也受作用域约束：某个实例把它关掉时，本轮就该退回全量披露模式
    activation_enabled = any(plugin.module_name == "plugin_activation" for plugin in active_plugins)

    rendered_plugins = await render_plugins_prompt(
        active_plugins,
        ctx,
        activation_enabled=activation_enabled,
        call_inject=not dry_run,
        one_time_code=one_time_code,
        # 上一轮被拦时这一轮不注入插件的不可信内容：判据与世界书的不可信条目抑制同一个。
        suppress_untrusted=is_narrative_suppressed(chat_key, global_config.AI_CHAT_CONTEXT_EXPIRE_SECONDS),
    )
    adapter_runtime_prompt = await ctx.adapter.render_runtime_prompt()
    runtime_prompts = [
        prompt for prompt in (adapter_runtime_prompt, rendered_plugins.runtime_prompt) if prompt.strip()
    ]
    runtime_prompt = "\n\n".join(runtime_prompts)
    logger.debug(f"[round_prompt] {chat_key} | 插件 prompt 渲染完成，开始构建 system prompt")

    # 好感度：读路径 fail-open（失败返回空串），写路径只登记后台任务后立刻返回。
    # 两者都不会让主对话因为好感度而变慢或失败，详见 services/affection/。
    affection_prompt = await render_affection_prompt(
        db_chat_channel=db_chat_channel,
        one_time_code=one_time_code,
        config=config,
    )
    # 预览不排评估：那会在后台真的写好感，而预览的承诺是「不留痕」。
    # 少带内容重发同样不排：第一次渲染已经排过一次。
    if not dry_run and lean_history <= 0:
        schedule_affection_evaluation(db_chat_channel, config)

    # 世界书：闸关时不查任何世界书表、也不查消息（`resolve_round` 的早退分支），
    # 因此提示词与改造前逐字相同。读路径 fail-open：求值失败只会让这一轮不注入。
    # 预览「换一个人设看看」时人设书按所选人设判，否则看到的是「新人设 + 旧人设的书」；
    # 频道摘要与好感块跟着人设的记忆分区走，预览不切换分区，页面上写明。
    # 世界书模板读变量的来源（由插件登记，例如角色状态）：没有登记时是空提供者，与没有变量时
    # 逐字相同；世界书关着时模板根本不求值，不问。取不到、出错、超时都回落成空提供者。
    template_variables = None
    if bool(getattr(config, "WORLDBOOK_ENABLED", False)):
        template_variables = await wb_variables.provider_for_round(
            wb_variables.RoundVariableContext(
                one_time_code=one_time_code,
                channel=db_chat_channel,
                chat_key=chat_key,
                preset_id=preset_override.id if preset_override is not None else None,
                is_private=getattr(db_chat_channel, "chat_type", None) == ChatType.PRIVATE,
                trigger_person_uid=_trigger_person_uid(trigger_message),
                config=config,
                dry_run=dry_run,
            ),
        )
    worldbook_round = await wb_runtime.resolve_round(
        one_time_code,
        db_chat_channel,
        config,
        preset_id_override=preset_override.id if preset_override is not None else None,
        variables=template_variables,
        constant_only=lean_history > 0,
    )
    prompt_rounds.note_worldbook(
        one_time_code,
        state=worldbook_round.state,
        dropped=worldbook_round.dropped,
        budget=worldbook_round.budget.as_dict() if worldbook_round.budget is not None else None,
        tokens=worldbook_round.tokens,
    )
    # 条目正文里的宏按当轮上下文展开；闸关时展开器是恒等函数，输出逐字不变。
    # `person_labels` 缺省为空（测试替身等没有这个字段的轮次结果按「不标」处理），
    # 给了才在个人书条目前加一行「关于谁」。
    worldbook_prompt = wb_render.render_block(
        worldbook_round.groups.after_persona,
        one_time_code,
        in_history=False,
        expand=tavern_expand,
        hidden_book_ids=hidden_book_ids,
        person_labels=getattr(worldbook_round, "person_labels", None),
    )
    if worldbook_prompt:
        prompt_rounds.note_worldbook_items(
            one_time_code,
            wb_render.rendered_items(
                worldbook_round.groups.after_persona,
                expand=tavern_measure,
                with_reason=True,
            ),
        )

    # 角色扮演预设：世界书块已经渲染好，按预设顺序把人设卡字段、预设段落与世界书
    # 排成一块替换人设节。没启用时 `rp_layout` 为 `None`，下面所有参数保持原形状。
    rp_layout: Optional[RpLayout] = None
    if (
        tavern.enabled
        and tavern.slots is not None
        and tavern.slots.enabled
        and tavern.rp_preset is not None
    ):
        rp_layout = build_layout(
            segments_from_stored(tavern.rp_preset.segments),
            fields=_rp_card_fields(tavern),
            worldbook_block=worldbook_prompt,
            post_history_slot=tavern.post_history,
            expand=tavern_expand,
        )
        prompt_rounds.note_tavern(
            one_time_code,
            rp_preset={
                "id": int(tavern.rp_preset.id),
                "name": tavern.rp_preset.name,
                "segments": len(rp_layout.used),
                "system_chars": len(rp_layout.system_block),
                "tail_chars": len(rp_layout.tail_text),
            },
        )

    # 运维工具说明：没有授权时是空串，模板里的 `{%- if %}` 不成立，渲染结果与改造前逐字一致
    ops_prompt = render_ops_prompt_section(ctx)

    prompt_compiler = PromptCompiler(
        affection_prompt=affection_prompt,
        worldbook_prompt=worldbook_prompt,
        auxiliary_prompt=tavern.auxiliary,
        ops_prompt=ops_prompt,
        platform_name=self_info.platform_name,
        bot_platform_id=self_info.user_id,
        chat_preset=tavern.persona_content,
        plugins_prompt=rendered_plugins.system_prompt,
        plugins_runtime_prompt=runtime_prompt,
        plugin_depth_blocks=rendered_plugins.depth_blocks,
        plugin_activation_rules=build_plugin_activation_rules() if activation_enabled else "",
        plugin_call_priority_rules=build_plugin_call_priority_rules(),
        enable_cot=used_model_group.ENABLE_COT,
        chat_key_rules="\n".join(f"- {r}" for r in db_chat_channel.adapter.chat_key_rules),
        enable_at=db_chat_channel.adapter.config.SESSION_ENABLE_AT,
        # 角色扮演频道（酒馆运行时开着且高级提示词槽位开着）去掉人设节的外层框。
        # 槽位关着时人设正文只是普通设定，保留原来的框。
        persona_roleplay=bool(tavern.enabled and tavern.slots is not None and tavern.slots.enabled),
        # 稳定前缀顺序：按频道生效配置决定世界书 / 好感块紧跟人设，还是挪到系统提示词末尾。
        stable_prefix_order=bool(getattr(config, "PROMPT_STABLE_PREFIX_ORDER", False)),
        # 预设块非空时替换人设节；世界书已经包含在块里，模板不再单独输出。
        rp_block=rp_layout.system_block if rp_layout is not None else "",
        rp_tail=rp_layout.tail_text if rp_layout is not None else None,
        rp_depth_blocks=list(rp_layout.depth_blocks) if rp_layout is not None else None,
    )

    messages = [prompt_compiler.render_system_message()]
    practice_messages = prompt_compiler.render_practice_messages(adapter_dialog_examples, adapter_jinja_env)
    messages.extend(practice_messages)
    # 每条消息来自哪一段，在拼的地方就记下：预览按它分组显示。不让预览从总条数反推
    # （「第一条系统、最后一条历史」），以后这里多拼一条或拆一条，标记也不会悄悄错位。
    message_sources = ["system", *(["example"] * len(practice_messages)), "history"]
    messages.append(
        await prompt_compiler.render_history_message(
            chat_key=chat_key,
            db_chat_channel=db_chat_channel,
            one_time_code=one_time_code,
            config=config,
            model_group=used_model_group,
            dry_run=dry_run,
            extra_history_lines=extra_history_lines,
            hidden_book_ids=hidden_book_ids,
            max_messages=lean_history if lean_history > 0 else None,
        ),
    )
    # 辅助提示词非空就一定进了系统提示词（模板只在空时跳过）；这里把明细从 pending 改成 sent。
    # 预览没有 begin 过令牌，`note_tavern` 是空操作。
    if tavern.auxiliary:
        prompt_rounds.note_tavern(one_time_code, auxiliary_state="sent")

    logger.debug(
        f"[round_prompt] {chat_key} | 历史记录渲染完成，发送 LLM 请求 (model={used_model_group.CHAT_MODEL})",
    )
    meta = (
        _build_meta(
            tavern,
            worldbook_round,
            used_model_group,
            tavern_measure,
            greeting_meta,
            mask_platform_slots=mask_platform_slots,
            slot_preview_texts=slot_preview_texts,
        )
        if dry_run
        else {}
    )
    if dry_run:
        # 页面据此提示「Bot 不在线，提示词里它的平台账号是占位」，免得管理员以为那就是真值
        meta["bot_offline"] = bot_offline
        meta["message_sources"] = message_sources
        if rp_layout is not None and tavern.rp_preset is not None:
            meta["rp_preset"] = {
                "id": int(tavern.rp_preset.id),
                "name": tavern.rp_preset.name,
                "used": [dict(item) for item in rp_layout.used],
                "prefill_text": rp_layout.prefill_text,
                "prefill_sent": False,
            }
        if rp_preset_inactive_reason:
            meta["rp_preset_inactive_reason"] = rp_preset_inactive_reason
    return RoundPrompt(
        messages=messages,
        config=config,
        preset=preset,
        used_model_group=used_model_group,
        runtime_prompt=runtime_prompt,
        prefill=None if lean_history > 0 else tavern.prefill,
        tavern=tavern,
        meta=meta,
    )


def _configured_rp_preset_id(config: Any) -> int:
    """频道生效配置里选的角色扮演预设编号；没选或值不成形时是 0"""
    try:
        return int(getattr(config, "PROMPT_RP_PRESET_ID", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _rp_card_fields(tavern: tavern_runtime.TavernRound) -> dict[str, str]:
    """预设排布要用的人设卡字段；没有卡时 `description` 用人设正文，其余为空

    `source_fields` 是 JSON 列，历史数据或手工改库可能不是 dict；任何一种形状不对都按
    空串处理，与运行时读卡片的其它地方同一方向。
    """
    fields = {"system": "", "description": "", "personality": "", "scenario": "", "mes_example": ""}
    card = tavern.card
    source = getattr(card, "source_fields", None) if card is not None else None
    if isinstance(source, dict):
        for key in fields:
            value = source.get(key)
            fields[key] = value if isinstance(value, str) else ""
    elif card is None:
        fields["description"] = tavern.persona_content
    return fields


def _slot_preview_texts(tavern: tavern_runtime.TavernRound, expand: Callable[[str], str]) -> dict[str, str]:
    """三个槽位「打开以后会发什么」的真实文字

    槽位开着时直接用本轮已经展开的文字；关着时按当轮宏上下文展开 `slots` 里的原文。
    `{{original}}` 已经在 `resolve_slots` 里串好，这里只负责其余宏与空值。
    """
    slots = tavern.slots
    if slots is None:
        return {"post_history": "", "auxiliary": "", "prefill": ""}
    if slots.enabled:
        return {
            "post_history": tavern.post_history,
            "auxiliary": tavern.auxiliary,
            "prefill": tavern.prefill.text if tavern.prefill is not None else "",
        }
    return {
        "post_history": expand(slots.post_history.text),
        "auxiliary": expand(slots.auxiliary.text),
        "prefill": expand(slots.prefill.text),
    }


def _apply_platform_slot_mask(tavern: tavern_runtime.TavernRound, real_texts: dict[str, str]) -> None:
    """把带平台方层的槽位文字就地换成占位

    就地改缓存里的那一个对象，是因为历史渲染从 `cached_tavern_round` 取后置指令；拷一份
    只改返回值的话，聊天记录里仍会漏出平台方原文。这个对象只属于本次预览，预览收尾时
    由 `build_round_prompt` 弹出，不会影响别的轮次。

    槽位关着时本轮不渲染它，这里不动 `tavern` 的字段；`slots_preview` 另外按占位显示。
    """
    slots = tavern.slots
    if slots is None or not slots.enabled:
        return
    if _has_platform_layer(slots.post_history.layers):
        tavern.post_history = _mask_slot_text(_SLOT_LABELS["post_history"], real_texts["post_history"])
    if _has_platform_layer(slots.auxiliary.layers):
        tavern.auxiliary = _mask_slot_text(_SLOT_LABELS["auxiliary"], real_texts["auxiliary"])
    if tavern.prefill is not None and _has_platform_layer(slots.prefill.layers):
        tavern.prefill.text = _mask_slot_text(_SLOT_LABELS["prefill"], real_texts["prefill"])


def _slot_meta(text: str, layers: list[str]) -> dict[str, Any]:
    """预览里的一个槽位：最终文字与贡献过文字的层"""
    return {"text": text, "layers": list(layers)}


def _prefill_meta(tavern: tavern_runtime.TavernRound, group: ModelConfigGroup) -> dict[str, Any]:
    """预览里预填的最终文字、会不会发、为什么

    `reason` 的取值与发送侧的状态名对齐：`ready` / `empty` / `gated`（槽位关着）/
    `model_group_off`（组上没开预填）/ `unsupported`（这个组刚被上游拒绝过预填）。
    """
    if tavern.prefill is None:
        slots = tavern.slots
        if slots is not None and not slots.enabled and slots.prefill.text:
            return {"text": "", "will_send": False, "reason": "gated"}
        return {"text": "", "will_send": False, "reason": "empty"}
    text = tavern.prefill.text
    if prefill_message(group, text) is not None:
        return {"text": text, "will_send": True, "reason": "ready"}
    if group.PREFILL_MODE not in _PREFILL_MODES:
        reason = "model_group_off"
    elif is_capability_marked(group, PREFILL_CAPABILITY):
        reason = "unsupported"
    else:
        reason = "unavailable"
    return {"text": text, "will_send": False, "reason": reason}


def _worldbook_meta_items(
    hits: Sequence[Hit],
    expand: Callable[[str], str],
    token_counts: Optional[Mapping[int, int]] = None,
) -> list[dict[str, Any]]:
    """把一组入选条目转成预览用的清单：只写引用、位置、信任、字数与 token 数，不写原文"""
    counts = token_counts or {}
    return [
        {
            "entry_id": entry_id,
            "book_id": book_id,
            "position": position,
            "trusted": trusted,
            "chars": chars,
            "tokens": counts.get(entry_id),
        }
        for entry_id, book_id, chars, position, trusted in wb_render.rendered_items(hits, expand=expand)
    ]


def _build_meta(
    tavern: tavern_runtime.TavernRound,
    worldbook_round: wb_runtime.RoundWorldbook,
    group: ModelConfigGroup,
    expand: Callable[[str], str],
    greeting: Optional[dict[str, Any]] = None,
    *,
    mask_platform_slots: bool = False,
    slot_preview_texts: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """预览的元数据：各段字数、槽位、深度注入、世界书三组、预填

    一律只放引用、层名、状态与字数；槽位与预填的文字是管理员自己写的，预览要在回复里
    显示原文（这是「打开后会发什么」的答案），但不落明细表。

    `slots_preview` 是「打开以后会发什么」那份：槽位关着时也显示原文，租户身份下带平台方
    层的槽位换成占位（与 `slots` 的遮罩同一个判据、同一个字数口径）。
    """
    slots = tavern.slots
    slot_enabled = bool(slots.enabled) if slots is not None else False
    if slot_preview_texts is None:
        slot_preview_texts = _slot_preview_texts(tavern, expand)
    preview = {
        name: _slot_meta(
            _mask_slot_text(_SLOT_LABELS[name], text)
            if mask_platform_slots and slots is not None and _has_platform_layer(getattr(slots, name).layers)
            else text,
            getattr(slots, name).layers if slots is not None else [],
        )
        for name, text in slot_preview_texts.items()
    }
    return {
        "persona_chars": len(tavern.persona_content),
        "slots": {
            "enabled": slot_enabled,
            "post_history": _slot_meta(
                tavern.post_history,
                slots.post_history.layers if slots is not None else [],
            ),
            "auxiliary": _slot_meta(
                tavern.auxiliary,
                slots.auxiliary.layers if slots is not None else [],
            ),
            "prefill": _slot_meta(
                tavern.prefill.text if tavern.prefill is not None else "",
                slots.prefill.layers if slots is not None else [],
            ),
        },
        "slots_preview": preview,
        "depth_prompt": {"text": tavern.depth_prompt, "depth": tavern.depth},
        "worldbook": {
            "after_persona": _worldbook_meta_items(
                worldbook_round.groups.after_persona,
                expand,
                worldbook_round.tokens,
            ),
            "before_history": _worldbook_meta_items(
                worldbook_round.groups.before_history,
                expand,
                worldbook_round.tokens,
            ),
            "at_depth": [
                {"depth": depth, **item}
                for depth, hits in sorted(worldbook_round.groups.at_depth.items())
                for item in _worldbook_meta_items(hits, expand, worldbook_round.tokens)
            ],
            # 这一轮的预算是怎么算出来的；闸关或求值失败时为 None
            "budget": worldbook_round.budget.as_dict() if worldbook_round.budget is not None else None,
        },
        "prefill": _prefill_meta(tavern, group),
        "greeting": greeting if greeting is not None else {"will_send": False, "text": ""},
    }
