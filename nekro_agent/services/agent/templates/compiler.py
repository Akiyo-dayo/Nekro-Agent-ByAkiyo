from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

from jinja2 import Environment

from nekro_agent.core.config import CoreConfig, ModelConfigGroup
from nekro_agent.models.db_chat_channel import DBChatChannel

from ..creator import OpenAIChatMessage
from .base import env as default_env
from .history import HistoryFirstStart, render_history_data
from .practice import (
    BasePracticePrompt_question,
    BasePracticePrompt_response,
    PracticePrompt_question_1,
    PracticePrompt_question_2,
    PracticePrompt_response_1,
    PracticePrompt_response_2,
)
from .system import PersonaPrompt, PolicyKernelPrompt, RuntimeContractPrompt, SystemPrompt

EXAMPLE_SESSION_TOKEN = "SESSION_TOKEN"


@dataclass
class PromptSegments:
    stable_static: str
    channel_static: str
    runtime_dynamic: str


class PromptCompiler:
    def __init__(
        self,
        *,
        platform_name: str,
        bot_platform_id: str,
        chat_preset: str,
        plugins_prompt: str,
        plugins_runtime_prompt: str,
        plugin_activation_rules: str,
        plugin_call_priority_rules: str,
        enable_cot: bool,
        chat_key_rules: str,
        enable_at: bool,
        affection_prompt: str = "",
        worldbook_prompt: str = "",
        auxiliary_prompt: str = "",
        ops_prompt: str = "",
        plugin_depth_blocks: Optional[List[Tuple[int, str]]] = None,
        persona_roleplay: bool = False,
        stable_prefix_order: bool = False,
        rp_block: str = "",
        rp_tail: Optional[str] = None,
        rp_depth_blocks: Optional[List[Tuple[int, str]]] = None,
    ):
        self.affection_prompt = affection_prompt
        # 插件要求插进聊天记录某个深度的块；只在本轮第一次渲染历史时插（见 `render_history_data`）。
        self.plugin_depth_blocks: List[Tuple[int, str]] = list(plugin_depth_blocks or [])
        # 角色扮演预设排好的系统块。非空时替换人设节，世界书已经包含在块里，模板不再单独输出。
        # 默认空串，别处的构造点不传，输出与改造前逐字相同。
        self.rp_block = rp_block
        # 预设排在聊天记录之后的文字；`None` 表示这一轮没有启用预设，历史按原逻辑拼后置指令。
        self.rp_tail = rp_tail
        # 预设要求在聊天记录某个深度插入的块；与插件块并进同一处深度注入。
        self.rp_depth_blocks: List[Tuple[int, str]] = list(rp_depth_blocks or [])
        self.worldbook_prompt = worldbook_prompt
        self.auxiliary_prompt = auxiliary_prompt
        self.ops_prompt = ops_prompt
        # 角色扮演频道的人设节不套外层框；默认 False，别处的构造点不传，输出与改造前逐字相同。
        self.persona_roleplay = persona_roleplay
        # 稳定前缀顺序：默认 False，别处的构造点不传，输出与改造前逐字相同。
        self.stable_prefix_order = stable_prefix_order
        self.platform_name = platform_name
        self.bot_platform_id = bot_platform_id
        self.chat_preset = chat_preset
        self.plugins_prompt = plugins_prompt
        self.plugins_runtime_prompt = plugins_runtime_prompt
        self.plugin_activation_rules = plugin_activation_rules
        self.plugin_call_priority_rules = plugin_call_priority_rules
        self.enable_cot = enable_cot
        self.chat_key_rules = chat_key_rules
        self.enable_at = enable_at

    def compile_segments(self) -> PromptSegments:
        return PromptSegments(
            stable_static=PolicyKernelPrompt().render(default_env),
            channel_static=PersonaPrompt(
                chat_preset=self.chat_preset,
                roleplay=self.persona_roleplay,
            ).render(default_env),
            runtime_dynamic=RuntimeContractPrompt(
                platform_name=self.platform_name,
                bot_platform_id=self.bot_platform_id,
                enable_cot=self.enable_cot,
                chat_key_rules=self.chat_key_rules,
                enable_at=self.enable_at,
                plugin_activation_rules=self.plugin_activation_rules,
                plugin_call_priority_rules=self.plugin_call_priority_rules,
            ).render(default_env),
        )

    def render_system_message(self, stable_prefix_order: Optional[bool] = None) -> OpenAIChatMessage:
        """渲染系统消息

        `stable_prefix_order` 不传时用构造时那份（`round_prompt` 按频道生效配置传进来）；
        显式传 `True` / `False` 可以覆盖它。开关打开时，世界书与好感块由编译器拼成
        `deferred_prompt` 挪到运维段之后、整条消息的末尾：宿主写的运维说明留在两块不可信
        内容的前面，不会落到它们的标题底下。模板其余部分与「两块都为空」时逐字相同，
        两块都为空时与开关关着时逐字相同。
        """
        segments = self.compile_segments()
        effective_stable_prefix_order = (
            self.stable_prefix_order if stable_prefix_order is None else stable_prefix_order
        )
        deferred_prompt = ""
        if effective_stable_prefix_order:
            # 每块前面一个空行；块正文末尾的换行去掉（块本身已经渲染好，原样搬，不重新包装）。
            # 预设块启用时世界书留在预设块里，不跟着好感块挪到末尾。
            deferred_prompt = "".join(
                "\n\n" + block.rstrip("\n")
                for block in (("" if self.rp_block else self.worldbook_prompt), self.affection_prompt)
                if block
            )
        return OpenAIChatMessage.from_template(
            "system",
            SystemPrompt(
                stable_static=segments.stable_static,
                channel_static=segments.channel_static,
                runtime_dynamic=segments.runtime_dynamic,
                worldbook_prompt=self.worldbook_prompt,
                auxiliary_prompt=self.auxiliary_prompt,
                affection_prompt=self.affection_prompt,
                plugins_prompt=self.plugins_prompt,
                ops_prompt=self.ops_prompt,
                stable_prefix_order=effective_stable_prefix_order,
                deferred_prompt=deferred_prompt,
                rp_block=self.rp_block,
            ),
            default_env,
        )

    def render_practice_messages(
        self,
        adapter_dialog_examples: Optional[List[object]],
        adapter_jinja_env: Optional[Environment],
    ) -> List[OpenAIChatMessage]:
        messages: List[OpenAIChatMessage] = []

        if adapter_dialog_examples and adapter_jinja_env:
            for prompt_template in adapter_dialog_examples:
                role = None
                if isinstance(prompt_template, BasePracticePrompt_question):
                    role = "user"
                    prompt_template.one_time_code = EXAMPLE_SESSION_TOKEN
                elif isinstance(prompt_template, BasePracticePrompt_response):
                    role = "assistant"
                    prompt_template.one_time_code = EXAMPLE_SESSION_TOKEN
                    prompt_template.enable_cot = self.enable_cot
                    prompt_template.enable_at = self.enable_at
                else:
                    raise TypeError(f"未知消息类型模板: {prompt_template}")
                messages.append(OpenAIChatMessage.from_template(role, prompt_template, adapter_jinja_env))
            return messages

        return [
            OpenAIChatMessage.from_template("user", PracticePrompt_question_1(one_time_code=EXAMPLE_SESSION_TOKEN), default_env),
            OpenAIChatMessage.from_template(
                "assistant",
                PracticePrompt_response_1(
                    one_time_code=EXAMPLE_SESSION_TOKEN,
                    enable_cot=self.enable_cot,
                    enable_at=self.enable_at,
                ),
                default_env,
            ),
            OpenAIChatMessage.from_template("user", PracticePrompt_question_2(one_time_code=EXAMPLE_SESSION_TOKEN), default_env),
            OpenAIChatMessage.from_template(
                "assistant",
                PracticePrompt_response_2(
                    one_time_code=EXAMPLE_SESSION_TOKEN,
                    enable_cot=self.enable_cot,
                    enable_at=self.enable_at,
                ),
                default_env,
            ),
        ]

    async def render_history_message(
        self,
        *,
        chat_key: str,
        db_chat_channel: DBChatChannel,
        one_time_code: str,
        config: CoreConfig,
        model_group: ModelConfigGroup,
        dry_run: bool = False,
        extra_history_lines: Optional[List[str]] = None,
        hidden_book_ids: Optional[Set[int]] = None,
        max_messages: Optional[int] = None,
    ) -> OpenAIChatMessage:
        return OpenAIChatMessage.from_template("user", HistoryFirstStart(enable_cot=self.enable_cot), default_env).extend(
            await render_history_data(
                chat_key=chat_key,
                db_chat_channel=db_chat_channel,
                one_time_code=one_time_code,
                plugin_injected_prompt=self.plugins_runtime_prompt,
                plugin_depth_blocks=self.plugin_depth_blocks,
                model_group=model_group,
                config=config,
                dry_run=dry_run,
                extra_history_lines=extra_history_lines,
                hidden_book_ids=hidden_book_ids,
                rp_tail=self.rp_tail,
                rp_depth_blocks=self.rp_depth_blocks,
                max_messages=max_messages,
            ),
        )
