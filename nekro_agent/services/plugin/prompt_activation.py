from __future__ import annotations

from typing import Dict, List, Literal

from pydantic import BaseModel

from nekro_agent.core.config import CONFIG_PATH, config
from nekro_agent.models.db_plugin_data import DBPluginData
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services.config_service import ConfigService
from nekro_agent.services.plugin.base import NekroPlugin

_STORE_PLUGIN_KEY = "__prompt_activation__"
_STORE_KEY = "module_rounds"
DEFAULT_ACTIVE_ROUNDS = 3
MAX_ACTIVE_ROUNDS = 20
PluginActivationStrategy = Literal["auto", "allow_sleep", "forbid_sleep"]


class PluginActivationState(BaseModel):
    module_rounds: Dict[str, int] = {}


def get_plugin_activation_strategy(module_name: str) -> PluginActivationStrategy:
    strategy = (config.PLUGIN_ACTIVATION_STRATEGIES or {}).get(module_name, "auto")
    if strategy not in {"auto", "allow_sleep", "forbid_sleep"}:
        return "auto"
    return strategy


def set_plugin_activation_strategy(module_name: str, strategy: PluginActivationStrategy) -> None:
    strategies = dict(config.PLUGIN_ACTIVATION_STRATEGIES or {})
    if strategy == "auto":
        strategies.pop(module_name, None)
    else:
        strategies[module_name] = strategy
    config.PLUGIN_ACTIVATION_STRATEGIES = strategies
    ConfigService.save_config(config, CONFIG_PATH)


def plugin_supports_sleep(plugin: NekroPlugin) -> bool:
    return bool(plugin.sleep_brief.strip())


def plugin_strategy_is_protected(plugin: NekroPlugin) -> bool:
    return plugin.allow_sleep is False


def plugin_strategy_can_change(plugin: NekroPlugin) -> bool:
    return not plugin_strategy_is_protected(plugin)


def is_sleep_effective(plugin: NekroPlugin) -> bool:
    if plugin_strategy_is_protected(plugin):
        return False
    strategy = get_plugin_activation_strategy(plugin.module_name)
    if strategy == "forbid_sleep":
        return False
    if strategy == "allow_sleep":
        return plugin_supports_sleep(plugin)
    # auto 策略：必须明确声明 allow_sleep=True 且提供 sleep_brief，缺一不可
    # 老插件未声明 allow_sleep（为 None）一律视为不可休眠，采用保守策略
    return plugin.allow_sleep is True and plugin_supports_sleep(plugin)


async def get_activation_state(chat_key: str) -> PluginActivationState:
    record = await DBPluginData.filter(
        plugin_key=_STORE_PLUGIN_KEY,
        target_chat_key=chat_key,
        target_user_id="",
        data_key=_STORE_KEY,
    ).first()
    if not record or not record.data_value:
        return PluginActivationState()
    try:
        return PluginActivationState.model_validate_json(record.data_value)
    except Exception:
        return PluginActivationState()


async def save_activation_state(chat_key: str, state: PluginActivationState) -> None:
    value = state.model_dump_json()
    record = await DBPluginData.filter(
        plugin_key=_STORE_PLUGIN_KEY,
        target_chat_key=chat_key,
        target_user_id="",
        data_key=_STORE_KEY,
    ).first()
    if record:
        await DBPluginData.filter(id=record.id).update(data_value=value)
        return
    await DBPluginData.create(
        plugin_key=_STORE_PLUGIN_KEY,
        target_chat_key=chat_key,
        target_user_id="",
        data_key=_STORE_KEY,
        data_value=value,
    )


def normalize_rounds(rounds: int | None) -> int:
    if rounds is None:
        return DEFAULT_ACTIVE_ROUNDS
    return max(1, min(int(rounds), MAX_ACTIVE_ROUNDS))


async def activate_module_for_chat(chat_key: str, module_name: str, rounds: int | None = None) -> int:
    state = await get_activation_state(chat_key)
    final_rounds = normalize_rounds(rounds)
    state.module_rounds[module_name] = final_rounds
    await save_activation_state(chat_key, state)
    return final_rounds


async def extend_module_for_chat(chat_key: str, module_name: str, rounds: int | None = None) -> int:
    state = await get_activation_state(chat_key)
    extra_rounds = normalize_rounds(rounds)
    current = max(0, state.module_rounds.get(module_name, 0))
    state.module_rounds[module_name] = min(current + extra_rounds, MAX_ACTIVE_ROUNDS)
    await save_activation_state(chat_key, state)
    return state.module_rounds[module_name]


def build_plugin_activation_rules() -> str:
    return (
        "Plugin visibility may be scheduled.\n"
        "- Only rely on fully rendered plugin blocks as currently available capabilities.\n"
        "- Sleeping plugin blocks are summaries, not full capability details.\n"
        "- Sleeping does NOT mean unavailable. If a sleeping capability_brief matches the goal, "
        "activate it and use it instead of reimplementing that capability yourself."
    )


def get_plugin_brief(plugin: NekroPlugin) -> str:
    """插件的能力摘要

    这段文字是模型在"该用哪个能力"这一步唯一能扫到的路由依据，因此它对**所有**披露状态
    都要给出，而不只在插件休眠时给。`sleep_brief` 通常写得比 `description` 更具体，优先取它；
    第三方插件没写 `sleep_brief` 时回落到 `description`，两者都空则不渲染该段。
    """
    return plugin.sleep_brief or plugin.description


async def render_plugin_prompt_for_agent(plugin: NekroPlugin, ctx: AgentCtx, rounds_left: int | None = None) -> str:
    from nekro_agent.services.agent.templates.plugin import (
        PluginPromptRenderUnit,
        render_plugin_prompt_unit,
    )

    return await render_plugin_prompt_unit(
        PluginPromptRenderUnit(
            plugin_name=plugin.name,
            module_name=plugin.module_name,
            state="active",
            rounds_left=rounds_left,
            plugin_brief=get_plugin_brief(plugin),
            activation_hint=(
                f"Last visible run. Call extend_plugin_activation('{plugin.module_name}') now if you still need it next run."
                if rounds_left == 1
                else ""
            ),
            plugin_injected_prompt=await plugin.render_inject_prompt(ctx),
            plugin_method_prompt=await plugin.render_sandbox_methods_prompt(ctx),
        ),
    )


async def build_prompt_disclosure_view(plugins: List[NekroPlugin], ctx: AgentCtx) -> List[BaseModel]:
    from nekro_agent.services.agent.templates.plugin import PluginPromptRenderUnit

    state = await get_activation_state(ctx.chat_key)
    rounds_snapshot = {module_name: rounds for module_name, rounds in state.module_rounds.items() if rounds > 0}
    consumed = False
    units: List[PluginPromptRenderUnit] = []
    valid_sleepable_modules: set[str] = set()

    for plugin in plugins:
        if len(plugin.support_adapter) != 0 and ctx.adapter_key not in plugin.support_adapter:
            continue

        if is_sleep_effective(plugin):
            valid_sleepable_modules.add(plugin.module_name)

        brief = get_plugin_brief(plugin)
        is_sleeping = is_sleep_effective(plugin) and rounds_snapshot.get(plugin.module_name, 0) <= 0
        if is_sleep_effective(plugin):
            if is_sleeping:
                units.append(
                    PluginPromptRenderUnit(
                        plugin_name=plugin.name,
                        module_name=plugin.module_name,
                        state="sleeping",
                        rounds_left=0,
                        plugin_brief=brief,
                    ),
                )
                continue

            units.append(
                PluginPromptRenderUnit(
                    plugin_name=plugin.name,
                    module_name=plugin.module_name,
                    state="active",
                    rounds_left=rounds_snapshot.get(plugin.module_name, 0),
                    plugin_brief=brief,
                    activation_hint=(
                        f"Last visible run. Call extend_plugin_activation('{plugin.module_name}') now if you still need it next run."
                        if rounds_snapshot.get(plugin.module_name, 0) == 1
                        else ""
                    ),
                    plugin_method_prompt=await plugin.render_sandbox_methods_prompt(ctx),
                ),
            )
            consumed = True
            continue

        method_prompt = await plugin.render_sandbox_methods_prompt(ctx)
        if not method_prompt.strip():
            # 常驻插件本轮一个方法都没披露时**整块不渲染**。
            #
            # 运行时契约要求模型「先读 brief 判断该用哪个能力，再读 predefined_methods 判断怎么调」。
            # 只给 brief 不给方法，模型只能猜一个名字，而沙盒里那个名字不存在 —— 直接 NameError 撞墙。
            # 这不是假设：审计时 `inner_voice` 在未布防的实例上正是这个形态，探针照着 brief 调就撞了。
            #
            # 方法为空是插件自己的 `collect_methods` 决定的（按需披露：只在布防的轮次交出方法），
            # 也就是说「本轮不提供这个能力」是插件的意图，那就连声明一起收掉，
            # 而不是留一句够不着的广告。插件想让模型知道它存在，就该在这一轮交出方法。
            continue

        units.append(
            PluginPromptRenderUnit(
                plugin_name=plugin.name,
                module_name=plugin.module_name,
                state="always_awake",
                plugin_brief=brief,
                plugin_method_prompt=method_prompt,
            ),
        )

    cleaned_snapshot = {
        module_name: rounds
        for module_name, rounds in rounds_snapshot.items()
        if module_name in valid_sleepable_modules
    }
    if consumed:
        next_state = PluginActivationState(
            module_rounds={
                module_name: rounds - 1
                for module_name, rounds in cleaned_snapshot.items()
                if rounds - 1 > 0
            },
        )
        await save_activation_state(ctx.chat_key, next_state)
    elif state.module_rounds != cleaned_snapshot:
        await save_activation_state(ctx.chat_key, PluginActivationState(module_rounds=cleaned_snapshot))

    return units
