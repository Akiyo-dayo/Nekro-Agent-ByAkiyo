from .base import PromptTemplate, register_template


@register_template("policy_kernel.j2", "policy_kernel_prompt")
class PolicyKernelPrompt(PromptTemplate):
    pass


@register_template("runtime_contract.j2", "runtime_contract_prompt")
class RuntimeContractPrompt(PromptTemplate):
    platform_name: str
    bot_platform_id: str
    enable_cot: bool
    chat_key_rules: str
    enable_at: bool
    plugin_activation_rules: str
    plugin_call_priority_rules: str


@register_template("persona.j2", "persona_prompt")
class PersonaPrompt(PromptTemplate):
    chat_preset: str


@register_template("system.j2", "system_prompt")
class SystemPrompt(PromptTemplate):
    stable_static: str
    channel_static: str
    runtime_dynamic: str
    # 好感度状态块，紧跟人设之后：它调的是人设的语气强度，属于同一层语义。
    # 内容由 LLM 生成，已在 `services/affection/prompt.py` 里清洗并包上会话令牌边界。
    affection_prompt: str
    plugins_prompt: str
