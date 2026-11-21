from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from pydantic import BaseModel

from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services.plugin.base import NekroPlugin
from nekro_agent.services.plugin.call_priority import PluginCallPriority, get_plugin_call_priority
from nekro_agent.services.plugin.injection import render_injection
from nekro_agent.services.plugin.prompt_activation import (
    build_prompt_disclosure_view,
    get_plugin_brief,
)

from .base import PromptTemplate, env, register_template


@register_template("plugin.j2", "plugin_prompt")
class PluginPrompt(PromptTemplate):
    plugin_name: str
    module_name: str
    state: str
    call_priority: PluginCallPriority = "auto"
    rounds_left: Optional[int] = None
    activation_hint: str = ""
    plugin_brief: str = ""
    plugin_injected_prompt: str = ""
    plugin_method_prompt: str = ""


class PluginPromptRenderUnit(BaseModel):
    plugin_name: str
    module_name: str
    state: str
    call_priority: PluginCallPriority = "auto"
    rounds_left: Optional[int] = None
    activation_hint: str = ""
    plugin_brief: str = ""
    plugin_injected_prompt: str = ""
    plugin_method_prompt: str = ""


@dataclass
class RenderedPluginPrompts:
    system_prompt: str
    runtime_prompt: str
    # 要求插进聊天记录某个深度的插件注入：`(深度, 渲染好的整块文字)`，按插件顺序。
    depth_blocks: List[Tuple[int, str]] = field(default_factory=list)


async def _render_plugin_prompt(unit: PluginPromptRenderUnit) -> str:
    return PluginPrompt(
        plugin_name=unit.plugin_name,
        module_name=unit.module_name,
        call_priority=unit.call_priority,
        state=unit.state,
        rounds_left=unit.rounds_left,
        activation_hint=unit.activation_hint,
        plugin_brief=unit.plugin_brief,
        plugin_injected_prompt=unit.plugin_injected_prompt,
        plugin_method_prompt=unit.plugin_method_prompt,
    ).render(env)


def _render_plugin_runtime_prompt(units: List[PluginPromptRenderUnit]) -> str:
    sections: List[str] = []
    for unit in units:
        if not unit.plugin_injected_prompt.strip():
            continue
        sections.append(
            "\n".join(
                [
                    f'<plugin_runtime_context name="{unit.plugin_name}" module_name="{unit.module_name}">',
                    unit.plugin_injected_prompt,
                    "</plugin_runtime_context>",
                ],
            ),
        )
    return "\n\n".join(sections)


def _sort_plugin_units_for_cache(units: List[PluginPromptRenderUnit]) -> List[PluginPromptRenderUnit]:
    # Static-only plugin docs first; plugins with runtime injected context later.
    return sorted(
        units,
        key=lambda unit: (
            bool(unit.plugin_injected_prompt.strip()),
            unit.state != "always_awake",
            unit.module_name,
        ),
    )


def _inject_plugin_names(units: List[PluginPromptRenderUnit], plugins: List[NekroPlugin]) -> List[str]:
    """这一轮会真正调用 `render_inject_prompt` 的插件名

    `render_inject_prompt` 是插件自己的代码，预览时不能调用（不保证没有副作用）。
    占位里只列名字，管理员据此知道「真跑这一轮时这里会多出哪些插件的内容」。
    状态不是 `active` / `always_awake` 的插件本来就不会被注入，不列。
    """
    plugin_map = {plugin.module_name: plugin for plugin in plugins}
    names: List[str] = []
    for unit in units:
        if unit.state not in ("active", "always_awake"):
            continue
        plugin = plugin_map.get(unit.module_name)
        if plugin is not None and plugin.prompt_inject_method is not None:
            names.append(plugin.name)
    return names


def _dry_run_runtime_prompt(names: List[str]) -> str:
    """预览时替代插件动态注入的占位；没有插件可注入时是空串（与真实一轮一致）"""
    if not names:
        return ""
    return f"[插件动态注入：预览不执行（{'、'.join(names)}）]"


async def render_plugin_prompt_unit(unit: PluginPromptRenderUnit) -> str:
    return await _render_plugin_prompt(unit)


async def _raw_injection(plugin: Any, ctx: AgentCtx) -> Any:
    """取插件注入方法的原始返回值

    插件类自己重写了 `render_inject_prompt`（或者是只实现了它的替身对象）时照旧调它、拿它的
    字符串：重写的逻辑不能被新入口绕过。其余走 `render_prompt_injection`，拿结构化返回。
    """
    overridden = getattr(type(plugin), "render_inject_prompt", None) is not NekroPlugin.render_inject_prompt
    render_raw = getattr(plugin, "render_prompt_injection", None)
    if overridden or render_raw is None:
        return await plugin.render_inject_prompt(ctx)
    return await render_raw(ctx)


async def _enrich_units_with_inject(
    units: List[PluginPromptRenderUnit],
    plugins: List[NekroPlugin],
    ctx: AgentCtx,
    states: Optional[set] = None,
    *,
    one_time_code: str = "",
    suppress_untrusted: bool = False,
) -> Tuple[List[PluginPromptRenderUnit], List[Tuple[int, str]]]:
    """按需为指定 state 的 unit 取插件注入，仅用于构建 runtime_prompt 与深度块。

    返回 `str` 的注入方法与以前逐字相同（放进 unit 的 `plugin_injected_prompt`）。结构化返回按
    本轮令牌包裹（`services/plugin/injection.py`）；带深度的不进 runtime_prompt，单独收进深度块，
    由历史渲染插进聊天记录。

    Args:
        units: 原始 unit 列表（inject 字段为空）
        plugins: 插件列表，用于查找 inject 方法
        ctx: Agent 上下文
        states: 需要渲染 inject 的 state 集合，None 表示所有 state
        one_time_code: 本轮令牌；空串时不可信的结构化注入一律不注入
        suppress_untrusted: 上一轮被内容审核拦下时为真，这一轮不可信的结构化注入一律不注入
    """
    plugin_map = {p.module_name: p for p in plugins}
    result: List[PluginPromptRenderUnit] = []
    depth_blocks: List[Tuple[int, str]] = []
    for unit in units:
        if states is None or unit.state in states:
            plugin = plugin_map.get(unit.module_name)
            if plugin:
                rendered = render_injection(
                    await _raw_injection(plugin, ctx),
                    one_time_code=one_time_code,
                    module_name=unit.module_name,
                    suppress_untrusted=suppress_untrusted,
                )
                if rendered.depth is not None:
                    if rendered.text.strip():
                        depth_blocks.append((rendered.depth, rendered.text))
                    result.append(unit)
                    continue
                result.append(unit.model_copy(update={"plugin_injected_prompt": rendered.text}))
                continue
        result.append(unit)
    return result, depth_blocks


async def render_plugins_prompt_legacy(
    plugins: List[NekroPlugin],
    ctx: AgentCtx,
    *,
    call_inject: bool = True,
    one_time_code: str = "",
    suppress_untrusted: bool = False,
) -> RenderedPluginPrompts:
    units: List[PluginPromptRenderUnit] = []
    for plugin in plugins:
        if len(plugin.support_adapter) != 0 and ctx.adapter_key not in plugin.support_adapter:
            continue

        units.append(
            PluginPromptRenderUnit(
                plugin_name=plugin.name,
                module_name=plugin.module_name,
                call_priority=get_plugin_call_priority(plugin.module_name),
                state="always_awake",
                plugin_brief=get_plugin_brief(plugin),
                plugin_method_prompt=await plugin.render_sandbox_methods_prompt(ctx),
            ),
        )
    units = _sort_plugin_units_for_cache(units)
    depth_blocks: List[Tuple[int, str]] = []
    if call_inject:
        enriched, depth_blocks = await _enrich_units_with_inject(
            units,
            plugins,
            ctx,
            one_time_code=one_time_code,
            suppress_untrusted=suppress_untrusted,
        )
        runtime_prompt = _render_plugin_runtime_prompt(enriched)
    else:
        runtime_prompt = _dry_run_runtime_prompt(_inject_plugin_names(units, plugins))
    return RenderedPluginPrompts(
        system_prompt="\n\n".join([await _render_plugin_prompt(unit) for unit in units]),
        runtime_prompt=runtime_prompt,
        depth_blocks=depth_blocks,
    )


async def render_plugins_prompt(
    plugins: List[NekroPlugin],
    ctx: AgentCtx,
    activation_enabled: bool = True,
    *,
    call_inject: bool = True,
    one_time_code: str = "",
    suppress_untrusted: bool = False,
) -> RenderedPluginPrompts:
    """渲染插件提示词

    Args:
        plugins: 本轮**已按作用域解析过**的插件集合。调用方应传入
            `scope.resolve_active_plugins()` 的结果（会话 > 实例 > 全局），
            而不是进程内全部启用的插件，否则实例级的插件分控不会体现在提示词里。
        ctx: Agent 上下文
        activation_enabled: 是否启用激活策略披露模式
        call_inject: 是否调用插件的 `render_inject_prompt`。预览 / 试发传 `False`：
            那是插件自己的代码、不保证没有副作用，换成一行列出插件名的占位。
        one_time_code: 本轮令牌，用来包裹插件的不可信注入；空串时那类注入不注入。
        suppress_untrusted: 上一轮被内容审核拦下时为真（与世界书不可信条目的抑制同一个判据），
            这一轮插件的不可信注入一律不注入。
    """
    if not activation_enabled:
        return await render_plugins_prompt_legacy(
            plugins,
            ctx,
            call_inject=call_inject,
            one_time_code=one_time_code,
            suppress_untrusted=suppress_untrusted,
        )

    units = await build_prompt_disclosure_view(plugins, ctx)
    units = _sort_plugin_units_for_cache(units)
    depth_blocks: List[Tuple[int, str]] = []
    if call_inject:
        enriched, depth_blocks = await _enrich_units_with_inject(
            units,
            plugins,
            ctx,
            states={"active", "always_awake"},
            one_time_code=one_time_code,
            suppress_untrusted=suppress_untrusted,
        )
        runtime_prompt = _render_plugin_runtime_prompt(enriched)
    else:
        runtime_prompt = _dry_run_runtime_prompt(_inject_plugin_names(units, plugins))
    return RenderedPluginPrompts(
        system_prompt="\n\n".join([await _render_plugin_prompt(unit) for unit in units]),
        runtime_prompt=runtime_prompt,
        depth_blocks=depth_blocks,
    )
