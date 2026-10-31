"""插件生成器

负责生成插件代码和模板。
"""

from typing import AsyncGenerator, Optional

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.services.agent.creator import OpenAIChatMessage
from nekro_agent.services.agent.openai import (
    gen_openai_chat_response,
    gen_openai_chat_stream,
)
from nekro_agent.services.agent.templates.generator import (
    ApplySystemPrompt,
    ApplyUserPrompt,
    GeneratorSystemPrompt,
    GeneratorUserPrompt,
)
from nekro_agent.services.usage import LLMUsageContext, LLMUsageScene, usage_scope

logger = get_sub_logger("plugin_system")


def _usage_context(model_group: str) -> LLMUsageContext:
    """本模块三处调用共用的归属声明

    这条链路没有会话也没有租户（WebUI 里由平台方点出来的代码生成），归属只到"模型组"这一层，
    但它**照样烧钱**，而且是最容易被忽略的一档：一次长上下文的插件生成比一整天的群聊还贵。
    """
    return LLMUsageContext(scene=LLMUsageScene.PLUGIN_GENERATE).with_model_group(
        model_group,
        config.MODEL_GROUPS.get(model_group),
    )


def _build_model_params(model_group: str) -> dict:
    """构建模型参数

    Args:
        model_group: 模型组名称

    Returns:
        dict: 模型参数字典
    """
    model_config = config.MODEL_GROUPS[model_group]
    return {
        "model": model_config.CHAT_MODEL,
        "temperature": model_config.TEMPERATURE,
        "top_p": model_config.TOP_P,
        "top_k": model_config.TOP_K,
        "frequency_penalty": model_config.FREQUENCY_PENALTY,
        "presence_penalty": model_config.PRESENCE_PENALTY,
        "extra_body": model_config.EXTRA_BODY,
        "base_url": model_config.BASE_URL,
        "api_key": model_config.API_KEY,
    }


def generate_plugin_template(name: str, description: str) -> str:
    """生成插件模板

    Args:
        name (str): 插件名称
        description (str): 插件描述

    Returns:
        str: 插件模板代码
    """
    return f'''from typing import Optional

from nekro_agent.api.schemas import AgentCtx
from nekro_agent.services.plugin.base import NekroPlugin, ConfigBase, SandboxMethodType
from pydantic import Field

# 创建插件实例
plugin = NekroPlugin(
    name="{name}",
    module_name="{name.lower()}",
    description="{description}",
    version="0.1.0",
    author="喵喵小助手",
    url="https://github.com/your-username/{name.lower()}",
)

# 示例：添加配置类
@plugin.mount_config()
class {name.capitalize()}Config(ConfigBase):
    """插件配置"""
    EXAMPLE_CONFIG: str = Field(
        default="默认值",
        title="示例配置",
        description="这是一个示例配置项",
    )

# 获取配置实例
config = plugin.get_config({name.capitalize()}Config)

@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="示例方法",
    description="这是一个示例工具方法",
)
async def example_method(_ctx: AgentCtx, param1: str, param2: int) -> str:
    """示例方法描述
    
    Args:
        param1: 第一个参数描述
        param2: 第二个参数描述
        
    Returns:
        str: 返回结果描述
        
    Example:
        example_method("测试", 123)
    """
    # 在这里实现你的功能
    return f"你输入的参数是: {{param1}}, {{param2}}"

@plugin.mount_cleanup_method()
async def clean_up():
    """清理插件资源"""
    # 如有必要，在此实现清理资源的逻辑
    pass
'''


async def generate_plugin_code(file_path: str, prompt: str, current_code: Optional[str] = None) -> str:
    """生成插件代码

    Args:
        file_path (str): 文件路径
        prompt (str): 提示词
        current_code (Optional[str], optional): 当前代码. Defaults to None.

    Returns:
        str: 生成的代码
    """
    assert file_path
    # 构建提示词
    messages = [
        OpenAIChatMessage.from_template("system", GeneratorSystemPrompt()),
        OpenAIChatMessage.from_template("user", GeneratorUserPrompt(prompt=prompt, current_code=current_code)),
    ]

    # 获取模型配置并调用 LLM 生成代码
    params = _build_model_params(config.PLUGIN_GENERATE_MODEL_GROUP)
    with usage_scope(_usage_context(config.PLUGIN_GENERATE_MODEL_GROUP)):
        response = await gen_openai_chat_response(
            messages=messages,
            stream_mode=False,
            **params,
        )

    return _clean_code_format(response.response_content)


async def generate_plugin_code_stream(
    file_path: str,
    prompt: str,
    current_code: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """流式生成插件代码 - 简化版

    Args:
        file_path (str): 文件路径
        prompt (str): 提示词
        current_code (Optional[str], optional): 当前代码. Defaults to None.

    Yields:
        str: 生成的代码片段
    """
    # 构建提示词
    messages = [
        OpenAIChatMessage.from_template("system", GeneratorSystemPrompt()),
        OpenAIChatMessage.from_template("user", GeneratorUserPrompt(prompt=prompt, current_code=current_code)),
    ]

    logger.info(f"开始为 {file_path} 流式生成代码，提示词: {prompt[:100]}...")

    # 获取模型配置
    params = _build_model_params(config.PLUGIN_GENERATE_MODEL_GROUP)

    # 使用OpenAI模块中的流式生成器
    try:
        # 传递list[OpenAIChatMessage]作为Union[OpenAIChatMessage, Dict]类型的参数是安全的
        # mypy不能正确识别这种兼容性，但实际上这是有效的
        #
        # `usage_scope` 必须裹住**整个消费循环**而不只是那次 create：调用
        # `gen_openai_chat_stream(...)` 只是造出生成器对象，它的函数体（连同那句
        # 「入口快照归属」）要到第一次 `__anext__` 才跑，而那一下发生在下面这个
        # `async for` 里。只裹住创建那一行，快照取到的就是 UNATTRIBUTED。
        #
        # ⚠️ 反过来，这个 with 块**跨过了一个 yield**，退出时可能落在另一个任务的上下文里
        # （消费方提前放弃时由 asyncio 的 asyncgen finalizer 关它）。`usage_scope` 已经
        # 咽掉那一支的 `ContextVar.reset` ValueError，归属本身不依赖这次还原 —— 见
        # `services/usage/context.py:usage_scope` 与 `openai.gen_openai_chat_stream` 的注释。
        with usage_scope(_usage_context(config.PLUGIN_GENERATE_MODEL_GROUP)):
            async for chunk in gen_openai_chat_stream(
                messages=messages,  # type: ignore
                **params,
            ):
                yield chunk
    except Exception as e:
        logger.error(f"插件代码流式生成失败: {e}")
        # 不重新抛出异常，而是让生成器结束


async def apply_plugin_code(file_path: str, prompt: str, current_code: str) -> str:
    """应用生成的代码

    Args:
        file_path (str): 文件路径
        prompt (str): 提示词
        current_code (str): 当前代码

    Returns:
        str: 处理后的代码
    """
    assert file_path
    messages = [
        OpenAIChatMessage.from_template("system", ApplySystemPrompt()),
        OpenAIChatMessage.from_template("user", ApplyUserPrompt(current_code=current_code, prompt=prompt)),
    ]

    # 获取模型配置
    params = _build_model_params(config.PLUGIN_APPLY_MODEL_GROUP)

    with usage_scope(_usage_context(config.PLUGIN_APPLY_MODEL_GROUP)):
        response = await gen_openai_chat_response(
            messages=messages,
            stream_mode=False,
            **params,
        )

    return _clean_code_format(response.response_content)


def _clean_code_format(code: str) -> str:
    """清理代码格式，移除 Markdown 代码块标记

    Args:
        code (str): 原始代码文本

    Returns:
        str: 清理后的代码
    """
    code = code.strip()
    # 移除开头的 ```python 或 ``` 标记
    if code.lower().startswith("```python"):
        code = code[10:]
    elif code.startswith("```"):
        code = code[3:]
    # 移除结尾的 ``` 标记
    if code.endswith("\n```"):
        code = code[:-4]
    elif code.endswith("```"):
        code = code[:-3]

    return code.strip() + "\n"
