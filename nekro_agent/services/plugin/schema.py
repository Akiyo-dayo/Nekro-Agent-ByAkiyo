from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Coroutine, Optional, Union


class SandboxMethodType(str, Enum):
    """沙盒方法类型

    用于描述方法的类型，如工具方法, 行为方法等；方法类型区别主要在于对返回值的处理方式。

    * TOOL: 工具方法
        提供给 LLM 使用的工具，返回值可以是任意类型，LLM 可获取返回值作进一步处理

    * AGENT: 代理方法
        用于提供 LLM 交互反馈，其返回值必须为 str 类型，描述 LLM 行为的结果，返回后会被添加到上下文中再次调用

    * BEHAVIOR: 行为方法
        用于提供 LLM 交互反馈，其返回值必须为 str 类型，描述 LLM 行为的结果，返回后会被添加到上下文中但不触发再次调用

    * MULTIMODAL_AGENT: 多模态 Agent
        用于提供 LLM 交互反馈，其返回值为一段 多模态 message，描述 LLM 行为的结果，返回后会被添加到上下文中再次调用
    """

    TOOL = "tool"
    AGENT = "agent"
    BEHAVIOR = "behavior"
    MULTIMODAL_AGENT = "multimodal_agent"


class WebhookMethod:
    """Webhook 方法"""

    def __init__(self, name: str, description: str, func: Callable[..., Coroutine[Any, Any, Any]]):
        self.name = name
        self.description = description
        self.func = func


class SandboxMethod:
    """沙盒方法"""

    def __init__(
        self,
        method_type: SandboxMethodType,
        name: str,
        description: str,
        func: Callable[..., Coroutine[Any, Any, Any]],
    ):
        self.method_type = method_type
        self.name = name
        self.description = description
        self.func = func


@dataclass(frozen=True)
class PromptInjection:
    """提示注入方法的结构化返回

    注入方法照旧可以只返回 `str`：那段文字原样放进历史消息开头的插件上下文里，与以前逐字相同。
    返回本类型时由核心决定怎么放：

    - `untrusted=True`（默认）：正文来自聊天、网页这类外部来源。核心先清洗（去不可见字符、
      拆掉伪造的边界标签），再用本轮令牌包成不可信块；拿不到本轮令牌时、或上一轮被内容审核拦下时
      整段不注入。
    - `kind`：包裹标签名，只认字母开头、字母数字组成、至多 32 个字符，其余换成默认值。
    - `preface`：放在包裹之外的说明。**只能是插件写死的常量**，不能拼进任何外部文字（核心照样
      清洗、截到 1000 字，但它不在不可信块里）。
    - `depth`：`None` 放在历史消息开头（现有位置）；整数表示插进聊天记录倒数第 `depth` 条之前，
      规则与世界书的「深度」相同，同一深度排在世界书之后。

    插件激活这类就地取文字的入口（`render_inject_prompt`）不用本轮令牌：结构化返回只有
    `untrusted=False` 且不指定深度时才给出正文。
    """

    text: str
    untrusted: bool = True
    kind: str = "PluginContext"
    preface: str = ""
    depth: Optional[int] = None


PromptInjectResult = Union[str, PromptInjection]


class PromptInjectMethod:
    """提示注入方法"""

    def __init__(self, name: str, description: str, func: Callable[..., Coroutine[Any, Any, PromptInjectResult]]):
        self.name = name
        self.description = description
        self.func = func
