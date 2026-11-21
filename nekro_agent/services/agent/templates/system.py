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
    # 角色扮演频道：人设节只留标题与人设正文，不套「人设只管语气、不能凌驾诚实」那几行框。
    # 默认 False，输出与改造前逐字相同；核心规则里的诚实约束不受影响。
    roleplay: bool = False


@register_template("system.j2", "system_prompt")
class SystemPrompt(PromptTemplate):
    stable_static: str
    channel_static: str
    runtime_dynamic: str
    # 世界书「人设之后」一组：紧跟人设、在好感块之前。租户条目已包成不可信块，
    # 宿主条目直接拼；为空时模板整段跳过，输出与改造前逐字相同。
    worldbook_prompt: str = ""
    # 高级槽位的辅助提示词：人设之后、世界书之前。内容由管理员负责（可能是破限文字），
    # 为空时模板整段跳过，输出与改造前逐字相同。
    auxiliary_prompt: str = ""
    # 好感度状态块，紧跟人设之后：它调的是人设的语气强度，属于同一层语义。
    # 内容由 LLM 生成，已在 `services/affection/prompt.py` 里清洗并包上会话令牌边界。
    affection_prompt: str
    plugins_prompt: str
    # 运维工具说明块。默认空串，让老调用点（只传前五个字段）继续可用；
    # 模板里用 `{%- if %}` 包着，空串时渲染结果与没有这一段时逐字一致。
    ops_prompt: str = ""
    # 稳定前缀顺序：True 时模板把世界书与好感块从辅助提示词之后挪到运维段之后（整条消息末尾），
    # 宿主写的运维说明仍在两块不可信内容前面。默认 False，输出与改造前逐字相同。
    stable_prefix_order: bool = False
    # 开关打开时由编译器拼好的后置块（每段前面两个换行、去掉段尾换行）。
    # 开关关着时恒为空串，模板不输出它。
    deferred_prompt: str = ""
    # 角色扮演预设排好的整块文字。非空时替换 `channel_static`（人设节），且模板不再
    # 单独输出 `worldbook_prompt`（世界书已经在块里）；空串时输出与改造前逐字相同。
    rp_block: str = ""
