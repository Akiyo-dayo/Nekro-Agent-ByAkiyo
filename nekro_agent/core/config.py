from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Literal, Optional, Set

from pydantic import Field, PrivateAttr

from nekro_agent.schemas.i18n import SupportedLang, i18n_text, set_system_lang

from .core_utils import ConfigBase, ExtraField
from .os_env import OsEnv

CONFIG_DIR = Path(OsEnv.DATA_DIR) / "configs"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = Path(OsEnv.DATA_DIR) / "configs" / "nekro-agent.yaml"
CHANNEL_CONFIG_DIR = CONFIG_DIR / "channels"
CHANNEL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)


class ModelConfigGroup(ConfigBase):
    """模型配置组"""

    GROUP_NAME: str = Field(default="", title="模型组名称")
    CHAT_MODEL: str = Field(default="", title="聊天模型名称")
    CHAT_PROXY: str = Field(default="", title="聊天模型访问代理")
    BASE_URL: str = Field(default="", title="聊天模型 API 地址")
    API_KEY: str = Field(
        default="",
        title="聊天模型 API 密钥",
        json_schema_extra=ExtraField(is_secret=True).model_dump(),
    )
    MODEL_TYPE: Literal["chat", "embedding", "draw"] = Field(
        default="chat",
        title="模型类型",
        description="模型的用途类型，可以是聊天(chat)、向量嵌入(embedding)或绘图(draw)",
    )
    ENABLE_VISION: bool = Field(
        default=False,
        title="启用视觉功能",
        description="是否启用视觉能力，如果模型不支持请关闭此选项",
    )
    ENABLE_COT: bool = Field(
        default=False,
        title="启用外置思维链",
        description="启用后会强制 AI 在回答前输出思考过程，可以在一定程度提高回复质量，如果模型原生支持思维链请关闭此选项",
    )
    TOKEN_INPUT_RATE: float = Field(default=1.0, title="输入 Token 倍率")
    TOKEN_COMPLETION_RATE: float = Field(default=1.0, title="补全 Token 倍率")
    MODEL_PRICE_RATE: float = Field(default=1.0, title="模型价格倍率")
    TEMPERATURE: Optional[float] = Field(default=None, title="温度值")
    TOP_P: Optional[float] = Field(default=None, title="Top P")
    TOP_K: Optional[int] = Field(default=None, title="Top K")
    PRESENCE_PENALTY: Optional[float] = Field(default=None, title="提示重复惩罚")
    FREQUENCY_PENALTY: Optional[float] = Field(default=None, title="补全重复惩罚")
    EXTRA_BODY: Optional[str] = Field(default=None, title="额外参数 (JSON)")


# 配置文件里 `MODEL_GROUPS.*.API_KEY` 的占位符
#
# `refresh_runtime_view()` 会把运行时视图 dump 回 `<DATA_DIR>/configs/nekro-agent.yaml`，
# 改造前那份 YAML 里的 Key 是**明文**——整目录打包的备份照样带出全部 Key。现在 dump 侧
# 把非空 Key 一律写成这个占位符，明文与密文都不落盘；数据库仍是凭据的唯一事实来源。
#
# 语义（导入/更新路径共用的契约）：识别到占位符 = **这次没有提供新的 Key**——
# 绝不把它当凭据发给上游，也绝不拿它覆盖库里的密文（见 `models/db_model_group.py` 的
# `import_config_group` 与 `services/model_group_service.py` 的 `update_group`）。
# 取 `nkmask:1` 与密文信封前缀（`nkenc1:`，`tools/credential_cipher.py`）同族但可区分，
# 且不是任何真实上游的 Key 形态；真有人的 Key 恰好逐字等于它，也只能怪这个形态太挑。
MODEL_GROUP_API_KEY_PLACEHOLDER = "nkmask:1"


def is_model_group_api_key_placeholder(value: str) -> bool:
    """这个值是不是配置文件里的 `API_KEY` 占位符"""
    return value == MODEL_GROUP_API_KEY_PLACEHOLDER


class CoreConfig(ConfigBase):
    """核心配置"""

    SYSTEM_LANG: Literal["zh-CN", "en-US"] = Field(
        default="zh-CN",
        title="系统语言",
        description="系统默认语言，影响命令响应等文本的语言",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="基础设置",
                en_US="Basic Settings",
            ),
            i18n_title=i18n_text(
                zh_CN="系统语言",
                en_US="System Language",
            ),
            i18n_description=i18n_text(
                zh_CN="系统默认语言，影响命令响应等文本的语言 (zh-CN / en-US)",
                en_US="Default system language for command responses and other text (zh-CN / en-US)",
            ),
        ).model_dump(),
    )
    ENABLE_UI_ONBOARDING_GUIDE: bool = Field(
        default=True,
        title="启用界面上手引导",
        description="启用后，管理员打开 WebUI 时会自动进入界面上手引导；跳过或完成引导后会自动关闭。",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="其他",
                en_US="Other",
            ),
            i18n_title=i18n_text(
                zh_CN="启用界面上手引导",
                en_US="Enable UI Onboarding Guide",
            ),
            i18n_description=i18n_text(
                zh_CN="启用后，管理员打开 WebUI 时会自动进入界面上手引导；跳过或完成引导后会自动关闭，可稍后在此重新启用。",
                en_US="When enabled, administrators will automatically enter the UI onboarding guide when opening WebUI. It turns off after the guide is skipped or completed and can be re-enabled here later.",
            ),
        ).model_dump(),
    )

    """Nekro Cloud 云服务配置"""
    ENABLE_NEKRO_CLOUD: bool = Field(
        default=True,
        title="启用 NekroAI 云服务",
        description=(
            "是否启用 NekroAI 云服务，启用后可使用 NekroAI 提供的云服务共享能力，同时会收集并上报一些应用使用统计信息。"
            "敏感数据将经过不可逆摘要处理后仅用于统计分析，收集过程实现逻辑均公开开源，不包含任何具体用户/聊天/频道/代码执行等隐私信息！"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="基础设置",
                en_US="Basic Settings",
            ),
            i18n_title=i18n_text(
                zh_CN="启用 NekroAI 云服务",
                en_US="Enable NekroAI Cloud Service",
            ),
            i18n_description=i18n_text(
                zh_CN="启用后可使用 NekroAI 提供的云服务共享能力，同时会收集并上报一些应用使用统计信息。敏感数据将经过不可逆摘要处理后仅用于统计分析。",
                en_US="When enabled, you can use cloud services provided by NekroAI. Some usage statistics will be collected and reported. Sensitive data will be processed with irreversible hashing for statistical analysis only.",
            ),
        ).model_dump(),
    )
    NEKRO_CLOUD_API_KEY: str = Field(
        default="",
        title="NekroAI 云服务 API Key",
        description="NekroAI 云服务 API Key，可前往 <a href='https://cloud.nekro.ai/me'>NekroAI 社区</a> 获取",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="基础设置",
                en_US="Basic Settings",
            ),
            is_secret=True,
            placeholder="nk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            i18n_title=i18n_text(
                zh_CN="NekroAI 云服务 API Key",
                en_US="NekroAI Cloud Service API Key",
            ),
            i18n_description=i18n_text(
                zh_CN="NekroAI 云服务 API Key，可前往 <a href='https://cloud.nekro.ai/me' target='_blank' rel='noopener noreferrer'>NekroAI 社区</a> 获取",
                en_US="NekroAI Cloud Service API Key, get it from <a href='https://cloud.nekro.ai/me' target='_blank' rel='noopener noreferrer'>NekroAI Community</a>",
            ),
        ).model_dump(),
    )
    ENSURE_SFW_CONTENT: bool = Field(
        default=True,
        json_schema_extra=ExtraField(is_hidden=True).model_dump(),
    )

    """应用配置"""
    APP_LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        title="应用日志级别",
        description="应用日志级别，需要重启应用后生效",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="基础设置",
                en_US="Basic Settings",
            ),
            i18n_title=i18n_text(
                zh_CN="应用日志级别",
                en_US="Application Log Level",
            ),
            i18n_description=i18n_text(
                zh_CN="应用日志级别，需要重启应用后生效",
                en_US="Application log level, requires restart to take effect",
            ),
        ).model_dump(),
    )
    SUPER_USERS: List[str] = Field(
        default=[],
        title="管理员列表",
        description=(
            "此处指定的管理员用户可在聊天里使用超级用户指令。纯号码为 OneBot V11 的 QQ 号；"
            "其他平台写成「适配器标识:用户ID」，例如 telegram:123456。只在平台方可信的来源生效："
            "租户名下的实例、待认领实例、发送者 ID 可由调用方填写的适配器（SSE、邮件等）里不生效。只有超级管理员可以修改"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="基础设置",
                en_US="Basic Settings",
            ),
            i18n_title=i18n_text(
                zh_CN="管理员列表",
                en_US="Administrator List",
            ),
            i18n_description=i18n_text(
                zh_CN=(
                    "此处指定的管理员用户可在聊天里使用超级用户指令。纯号码为 OneBot V11 的 QQ 号；"
                    "其他平台写成「适配器标识:用户ID」，例如 telegram:123456。只在平台方可信的来源生效："
                    "租户名下的实例、待认领实例、发送者 ID 可由调用方填写的适配器（SSE、邮件等）里不生效。只有超级管理员可以修改"
                ),
                en_US=(
                    "Users listed here can use super user commands in chat. A plain number is a OneBot V11 QQ number; "
                    "for other platforms write adapter_key:user_id, e.g. telegram:123456. Only honoured from sources the "
                    "platform can vouch for: not in tenant-owned instances, unclaimed instances, or adapters whose sender "
                    "IDs are supplied by the caller (SSE, email, ...). Only super admins can change it"
                ),
            ),
        ).model_dump(),
    )
    COMMAND_ENABLED: bool = Field(
        default=True,
        title="全局命令开关",
        description="全局关闭后所有适配器不再处理命令",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="基础设置",
                en_US="Basic Settings",
            ),
            i18n_title=i18n_text(
                zh_CN="全局命令开关",
                en_US="Global Command Switch",
            ),
            i18n_description=i18n_text(
                zh_CN="全局关闭后所有适配器不再处理命令",
                en_US="When disabled, all adapters will stop processing commands",
            ),
        ).model_dump(),
    )
    COMMAND_MATCH_ALLOW_HYPHEN_FOR_UNDERSCORE: bool = Field(
        default=True,
        title="命令匹配允许连字符代替下划线",
        description="启用后，命令名匹配时会将连字符视为下划线，输入 cc-help 时可匹配 cc_help",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="基础设置",
                en_US="Basic Settings",
            ),
            i18n_title=i18n_text(
                zh_CN="命令匹配允许连字符代替下划线",
                en_US="Allow Hyphen Instead of Underscore in Command Matching",
            ),
            i18n_description=i18n_text(
                zh_CN="启用后，命令名匹配时会将连字符视为下划线，输入 cc-help 时可匹配 cc_help",
                en_US="When enabled, command matching treats hyphens as underscores, so cc-help can match cc_help",
            ),
        ).model_dump(),
    )
    DEBUG_IN_CHAT: bool = Field(
        default=False,
        title="聊天调试模式",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="基础设置",
                en_US="Basic Settings",
            ),
            i18n_title=i18n_text(
                zh_CN="聊天调试模式",
                en_US="Chat Debug Mode",
            ),
        ).model_dump(),
    )
    SAVE_PROMPTS_LOG: bool = Field(
        default=False,
        title="保存聊天提示词生成日志",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="基础设置",
                en_US="Basic Settings",
            ),
            i18n_title=i18n_text(
                zh_CN="保存聊天提示词生成日志",
                en_US="Save Chat Prompt Generation Log",
            ),
        ).model_dump(),
    )
    MAX_UPLOAD_SIZE_MB: int = Field(
        default=10,
        title="上传文件大小限制 (MB)",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="基础设置",
                en_US="Basic Settings",
            ),
            i18n_title=i18n_text(
                zh_CN="上传文件大小限制 (MB)",
                en_US="Upload File Size Limit (MB)",
            ),
        ).model_dump(),
    )
    ENABLE_COMMAND_UNAUTHORIZED_OUTPUT: bool = Field(
        default=False,
        title="启用未授权命令反馈",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="基础设置",
                en_US="Basic Settings",
            ),
            i18n_title=i18n_text(
                zh_CN="启用未授权命令反馈",
                en_US="Enable Unauthorized Command Feedback",
            ),
        ).model_dump(),
    )
    DEFAULT_PROXY: str = Field(
        default="",
        title="系统级默认代理地址",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="代理配置",
                en_US="Proxy Configuration",
            ),
            placeholder="例: http://127.0.0.1:7890",
            i18n_title=i18n_text(
                zh_CN="系统级默认代理地址",
                en_US="System Default Proxy URL",
            ),
            i18n_description=i18n_text(
                zh_CN="系统级功能使用的默认代理地址，不影响模型组代理；需包含协议头，例如 http:// 或 socks5://",
                en_US="Default proxy URL used by system-level features, without affecting model-group proxies; must include a scheme such as http:// or socks5://",
            ),
        ).model_dump(),
    )
    DEFAULT_PROXY_USERNAME: str = Field(
        default="",
        title="系统级代理用户名",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="代理配置",
                en_US="Proxy Configuration",
            ),
            i18n_title=i18n_text(
                zh_CN="系统级代理用户名",
                en_US="System Proxy Username",
            ),
            i18n_description=i18n_text(
                zh_CN="可选。系统级代理认证用户名，仅对接入系统级代理管理的功能生效",
                en_US="Optional. Username for system-level proxy authentication, effective only for features using the system proxy manager",
            ),
        ).model_dump(),
    )
    DEFAULT_PROXY_PASSWORD: str = Field(
        default="",
        title="系统级代理密码",
        json_schema_extra=ExtraField(
            is_secret=True,
            i18n_category=i18n_text(
                zh_CN="代理配置",
                en_US="Proxy Configuration",
            ),
            i18n_title=i18n_text(
                zh_CN="系统级代理密码",
                en_US="System Proxy Password",
            ),
            i18n_description=i18n_text(
                zh_CN="可选。系统级代理认证密码，仅对接入系统级代理管理的功能生效",
                en_US="Optional. Password for system-level proxy authentication, effective only for features using the system proxy manager",
            ),
        ).model_dump(),
    )
    MEMORY_ENABLE_SYSTEM: bool = Field(
        default=False,
        title="启用记忆系统",
        description="控制记忆系统的自动运行主链路。关闭后将停止自动沉淀、自动检索注入、CC 记忆握手、自动恢复与后台维护；已存在的记忆数据不会被删除，只读观测与少数显式手动维护功能仍可使用",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="启用记忆系统", en_US="Enable Memory System"),
            i18n_description=i18n_text(
                zh_CN="控制记忆系统的自动运行主链路。关闭后将停止自动沉淀、自动检索注入、CC 记忆握手、自动恢复与后台维护；已存在的记忆数据不会被删除，只读观测与少数显式手动维护功能仍可使用",
                en_US="Controls the automatic memory pipeline. When disabled, automatic consolidation, retrieval injection, CC memory handshake, automatic recovery, and background maintenance stop; existing memory data is kept, while read-only inspection and a few explicit manual maintenance actions remain available",
            ),
        ).model_dump(),
    )
    MEMORY_CONTEXT_MAX_LENGTH: int = Field(
        default=1200,
        title="记忆上下文最大长度",
        description="记忆系统注入到 Agent 提示词中的最大字符长度。值越大，Agent 可参考的记忆越多，但也会占用更多上下文窗口",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="记忆上下文最大长度", en_US="Memory Context Max Length"),
            i18n_description=i18n_text(
                zh_CN="记忆系统注入到 Agent 提示词中的最大字符长度。值越大，Agent 可参考的记忆越多，但也会占用更多上下文窗口",
                en_US="Maximum character length of memory context injected into the Agent prompt. Larger values allow more memory context but consume more prompt window",
            ),
        ).model_dump(),
    )
    MEMORY_ENABLE_ENHANCED_RETRIEVAL: bool = Field(
        default=False,
        title="启用增强记忆检索",
        description="启用后会先使用专门的模型根据最近上下文生成结构化记忆检索条件，再执行记忆检索；模型不可用或输出异常时会自动回退到规则检索",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="启用增强记忆检索", en_US="Enable Enhanced Memory Retrieval"),
            i18n_description=i18n_text(
                zh_CN="启用后会先使用专门的模型根据最近上下文生成结构化记忆检索条件，再执行记忆检索；模型不可用或输出异常时会自动回退到规则检索",
                en_US="When enabled, a dedicated model first generates a structured memory retrieval plan from recent context before retrieval. It automatically falls back to rule-based retrieval when the model is unavailable or returns invalid output",
            ),
        ).model_dump(),
    )
    MEMORY_ENHANCED_RETRIEVAL_MODEL_GROUP: str = Field(
        default="",
        title="增强记忆检索模型组",
        description="用于增强记忆检索规划的聊天模型组。留空时回退到默认聊天模型组",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            ref_model_groups=True,
            model_type="chat",
            i18n_title=i18n_text(zh_CN="增强记忆检索模型组", en_US="Enhanced Memory Retrieval Model Group"),
            i18n_description=i18n_text(
                zh_CN="用于增强记忆检索规划的聊天模型组。留空时回退到默认聊天模型组",
                en_US="Chat model group used for enhanced memory retrieval planning. Falls back to the default chat model group when empty",
            ),
        ).model_dump(),
    )
    MEMORY_ENHANCED_RETRIEVAL_TIMEOUT_SECONDS: float = Field(
        default=30.0,
        title="增强记忆检索规划超时（秒）",
        description="增强记忆检索的规划模型调用超时时间。超时后本轮自动回退到规则检索，不影响正常回复；填 0 表示不限制",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="增强记忆检索规划超时（秒）", en_US="Enhanced Memory Retrieval Planning Timeout (s)"),
            i18n_description=i18n_text(
                zh_CN="增强记忆检索的规划模型调用超时时间。超时后本轮自动回退到规则检索，不影响正常回复；填 0 表示不限制",
                en_US="Timeout for the enhanced memory retrieval planning model call. On timeout this round falls back to rule-based retrieval without affecting the reply; 0 means no limit",
            ),
        ).model_dump(),
    )
    MEMORY_CONSOLIDATION_MODEL_GROUP: str = Field(
        default="default",
        title="情景沉淀模型组",
        description="用于情景记忆沉淀提取的模型组名称。留空时回退到默认聊天模型组",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            ref_model_groups=True,
            model_type="chat",
            i18n_title=i18n_text(zh_CN="情景沉淀模型组", en_US="Memory Consolidation Model Group"),
            i18n_description=i18n_text(
                zh_CN="用于情景记忆沉淀提取的模型组名称。留空时回退到默认聊天模型组",
                en_US="Model group for episodic memory consolidation. Falls back to the default chat model group when empty",
            ),
        ).model_dump(),
    )
    MEMORY_CONSOLIDATION_FALLBACK_MODEL_GROUP: str = Field(
        default="",
        title="情景沉淀备用模型组",
        description="当情景记忆沉淀在最后一次解析重试时，切换使用的备用模型组。留空时回退到情景沉淀主模型组",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            ref_model_groups=True,
            model_type="chat",
            i18n_title=i18n_text(zh_CN="情景沉淀备用模型组", en_US="Memory Consolidation Fallback Model Group"),
            i18n_description=i18n_text(
                zh_CN="当情景记忆沉淀在最后一次解析重试时，切换使用的备用模型组。留空时回退到情景沉淀主模型组",
                en_US="Fallback model group used on the final parse retry of episodic memory consolidation. Falls back to the primary consolidation model group when empty",
            ),
            placeholder="例: api-nekro-pro-2.5",
        ).model_dump(),
    )
    MEMORY_EMBEDDING_MODEL_GROUP: str = Field(
        default="text-embedding",
        title="记忆 Embedding 模型组",
        description="用于记忆系统向量化的 embedding 模型组。应选择 MODEL_TYPE 为 embedding 的模型组，并与向量维度配置保持一致",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            ref_model_groups=True,
            model_type="embedding",
            i18n_title=i18n_text(zh_CN="记忆 Embedding 模型组", en_US="Memory Embedding Model Group"),
            i18n_description=i18n_text(
                zh_CN="用于记忆系统向量化的 embedding 模型组。应选择 MODEL_TYPE 为 embedding 的模型组，并与向量维度配置保持一致",
                en_US="Embedding model group used by the memory system for vectorization. It should use a model group with MODEL_TYPE set to embedding and match the configured vector dimension",
            ),
            placeholder="例: text-embedding",
        ).model_dump(),
    )
    MEMORY_EMBEDDING_DIMENSION: int = Field(
        default=1024,
        title="记忆 Embedding 维度",
        description="记忆系统向量化使用的维度，需要与所选 embedding 模型和 Qdrant 索引保持一致",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="记忆系统",
                en_US="Memory System",
            ),
            i18n_title=i18n_text(
                zh_CN="记忆 Embedding 维度",
                en_US="Memory Embedding Dimension",
            ),
            i18n_description=i18n_text(
                zh_CN="记忆系统向量化使用的维度，需要与所选 embedding 模型和 Qdrant 索引保持一致",
                en_US="Embedding dimension used by memory system, must match the selected embedding model and Qdrant index",
            ),
        ).model_dump(),
    )
    KB_EMBEDDING_MODEL_GROUP: str = Field(
        default="text-embedding",
        title="知识库 Embedding 模型组",
        description="用于知识库向量化与检索的 embedding 模型组。必须选择 MODEL_TYPE 为 embedding 的模型组，并与知识库向量维度配置保持一致",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="知识库", en_US="Knowledge Base"),
            ref_model_groups=True,
            model_type="embedding",
            required=True,
            i18n_title=i18n_text(zh_CN="知识库 Embedding 模型组", en_US="Knowledge Base Embedding Model Group"),
            i18n_description=i18n_text(
                zh_CN="用于知识库向量化与检索的 embedding 模型组。必须选择 MODEL_TYPE 为 embedding 的模型组，并与知识库向量维度配置保持一致",
                en_US="Embedding model group used for knowledge base vectorization and retrieval. It must use a model group with MODEL_TYPE set to embedding and match the configured knowledge base vector dimension",
            ),
            placeholder="例: text-embedding",
        ).model_dump(),
    )
    KB_EMBEDDING_DIMENSION: int = Field(
        default=1024,
        title="知识库 Embedding 维度",
        description="知识库向量化使用的维度，需要与所选 embedding 模型和 Qdrant 索引保持一致",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="知识库", en_US="Knowledge Base"),
            i18n_title=i18n_text(zh_CN="知识库 Embedding 维度", en_US="Knowledge Base Embedding Dimension"),
            i18n_description=i18n_text(
                zh_CN="知识库向量化使用的维度，需要与所选 embedding 模型和 Qdrant 索引保持一致",
                en_US="Embedding dimension used by the knowledge base, which must match the selected embedding model and Qdrant index",
            ),
        ).model_dump(),
    )
    # 默认 2（上游 3）：工作区文档与全局素材各一个信号量，实际并发翻倍；
    # 每个索引任务在切换前要把整份文档的向量留在内存里，小内存部署按 2 × 2 估算峰值
    KB_INDEX_CONCURRENCY: int = Field(
        default=2,
        title="知识库索引并发数",
        description="同时进行的知识库索引任务数（工作区文档与全局资产各按此值限制）。设为 1 时逐个串行索引，调大可加快批量索引，但会增加 Embedding 服务与 Qdrant 负载。修改后需重启服务生效",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="知识库", en_US="Knowledge Base"),
            i18n_title=i18n_text(zh_CN="知识库索引并发数", en_US="Knowledge Base Index Concurrency"),
            i18n_description=i18n_text(
                zh_CN="同时进行的知识库索引任务数（工作区文档与全局资产各按此值限制）。设为 1 时逐个串行索引，调大可加快批量索引，但会增加 Embedding 服务与 Qdrant 负载。修改后需重启服务生效",
                en_US="Number of concurrent knowledge base indexing tasks (workspace documents and global assets are each limited by this value). Set to 1 for fully serial indexing; higher values speed up batch indexing at the cost of heavier Embedding service and Qdrant load. Restart required after changing",
            ),
            placeholder="建议 1~8",
        ).model_dump(),
    )
    KB_ZIP_MAX_UPLOAD_SIZE_MB: int = Field(
        default=100,
        title="知识库 Zip 单包大小上限 (MB)",
        description="知识库 zip 批量导入时单个压缩包的大小上限（MB），超限拒绝上传。修改后需重启服务生效",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="知识库", en_US="Knowledge Base"),
            i18n_title=i18n_text(zh_CN="知识库 Zip 单包大小上限 (MB)", en_US="Knowledge Base Zip Upload Size Limit (MB)"),
            i18n_description=i18n_text(
                zh_CN="知识库 zip 批量导入时单个压缩包的大小上限（MB），超限拒绝上传。修改后需重启服务生效",
                en_US="Size limit (MB) for a single zip archive when batch-importing into the knowledge base; uploads exceeding it are rejected. Restart required after changing",
            ),
            placeholder="建议 50~500",
        ).model_dump(),
    )
    KB_ZIP_MAX_EXTRACT_SIZE_MB: int = Field(
        default=500,
        title="知识库 Zip 解压总大小上限 (MB)",
        description="知识库 zip 批量导入时解压后文件总大小上限（MB），用于防止 zip 炸弹。修改后需重启服务生效",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="知识库", en_US="Knowledge Base"),
            i18n_title=i18n_text(zh_CN="知识库 Zip 解压总大小上限 (MB)", en_US="Knowledge Base Zip Extract Size Limit (MB)"),
            i18n_description=i18n_text(
                zh_CN="知识库 zip 批量导入时解压后文件总大小上限（MB），用于防止 zip 炸弹。修改后需重启服务生效",
                en_US="Total extracted size limit (MB) for knowledge base zip import, used to guard against zip bombs. Restart required after changing",
            ),
            placeholder="建议 100~2000",
        ).model_dump(),
    )
    KB_ZIP_MAX_FILES: int = Field(
        default=500,
        title="知识库 Zip 文件数上限",
        description="知识库 zip 批量导入时单个压缩包允许的最大文件条目数。修改后需重启服务生效",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="知识库", en_US="Knowledge Base"),
            i18n_title=i18n_text(zh_CN="知识库 Zip 文件数上限", en_US="Knowledge Base Zip File Count Limit"),
            i18n_description=i18n_text(
                zh_CN="知识库 zip 批量导入时单个压缩包允许的最大文件条目数。修改后需重启服务生效",
                en_US="Maximum number of file entries allowed in a single zip archive for knowledge base import. Restart required after changing",
            ),
            placeholder="建议 100~2000",
        ).model_dump(),
    )
    KB_BATCH_MAX_SIZE: int = Field(
        default=1000,
        title="知识库批量操作单请求上限",
        description="知识库批量删除/解绑/重建索引的单请求 ID 数量上限，超限拒绝。修改后需重启服务生效",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="知识库", en_US="Knowledge Base"),
            i18n_title=i18n_text(zh_CN="知识库批量操作单请求上限", en_US="Knowledge Base Batch Operation Size Limit"),
            i18n_description=i18n_text(
                zh_CN="知识库批量删除/解绑/重建索引的单请求 ID 数量上限，超限拒绝。修改后需重启服务生效",
                en_US="Maximum number of IDs per batch request for knowledge base delete/unbind/reindex; exceeding it is rejected. Restart required after changing",
            ),
            placeholder="建议 100~5000",
        ).model_dump(),
    )
    KB_BATCH_CONCURRENCY: int = Field(
        default=8,
        title="知识库批量操作并发数",
        description="知识库批量删除/解绑/重建索引时服务端并发处理数。修改后需重启服务生效",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="知识库", en_US="Knowledge Base"),
            i18n_title=i18n_text(zh_CN="知识库批量操作并发数", en_US="Knowledge Base Batch Operation Concurrency"),
            i18n_description=i18n_text(
                zh_CN="知识库批量删除/解绑/重建索引时服务端并发处理数。修改后需重启服务生效",
                en_US="Server-side concurrency for knowledge base batch delete/unbind/reindex. Restart required after changing",
            ),
            placeholder="建议 1~32",
        ).model_dump(),
    )
    MEMORY_CONSOLIDATION_FORCE_JSON_OUTPUT: bool = Field(
        default=True,
        title="强制沉淀模型输出 JSON",
        description="为记忆沉淀请求显式附加 JSON 输出约束，降低解析失败概率。关闭后仅适用于兼容性调试场景",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="强制沉淀模型输出 JSON", en_US="Force JSON Consolidation Output"),
            i18n_description=i18n_text(
                zh_CN="为记忆沉淀请求显式附加 JSON 输出约束，降低解析失败概率",
                en_US="Explicitly request JSON output for memory consolidation to reduce parse failures",
            ),
        ).model_dump(),
    )
    MEMORY_LLM_MAX_RETRIES: int = Field(
        default=3,
        title="记忆 LLM 调用最大重试次数",
        description="记忆沉淀调用模型接口时的最大重试次数。用于处理网络抖动、上游超时或模型临时不可用",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="LLM 最大重试次数", en_US="LLM Max Retries"),
            i18n_description=i18n_text(
                zh_CN="记忆沉淀调用模型接口时的最大重试次数。用于处理网络抖动、上游超时或模型临时不可用",
                en_US="Maximum retry count for model calls during memory consolidation, used for transient network issues, upstream timeouts, or temporary model unavailability",
            ),
        ).model_dump(),
    )
    MEMORY_CONSOLIDATION_PARSE_MAX_RETRIES: int = Field(
        default=2,
        title="沉淀解析最大重试次数",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="沉淀解析最大重试次数", en_US="Consolidation Parse Max Retries"),
            i18n_description=i18n_text(
                zh_CN="当 LLM 返回内容无法解析为记忆 JSON 时，重新请求的最大次数",
                en_US="Maximum number of re-requests when LLM output cannot be parsed into memory JSON",
            ),
        ).model_dump(),
    )
    MEMORY_RETRIEVAL_DEFAULT_LIMIT: int = Field(
        default=10,
        title="默认检索数量",
        description="单次记忆检索默认返回的候选数量上限。过大可能增加噪声和上下文占用，过小可能遗漏相关记忆",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="默认检索数量", en_US="Default Retrieval Limit"),
            i18n_description=i18n_text(
                zh_CN="单次记忆检索默认返回的候选数量上限。过大可能增加噪声和上下文占用，过小可能遗漏相关记忆",
                en_US="Default maximum number of candidates returned by a memory retrieval. Too large may add noise and prompt cost, too small may miss relevant memories",
            ),
        ).model_dump(),
    )
    MEMORY_RETRIEVAL_MIN_SIMILARITY: float = Field(
        default=0.5,
        title="最低相似度",
        description="向量检索的最低相似度阈值。提高该值会减少低相关记忆，降低该值会提高召回率但可能带来噪声",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="最低相似度", en_US="Minimum Similarity"),
            i18n_description=i18n_text(
                zh_CN="向量检索的最低相似度阈值。提高该值会减少低相关记忆，降低该值会提高召回率但可能带来噪声",
                en_US="Minimum similarity threshold for vector retrieval. Higher values reduce weak matches; lower values improve recall but may increase noise",
            ),
        ).model_dump(),
    )
    MEMORY_RETRIEVAL_EPISODIC_BOOST: float = Field(
        default=1.2,
        title="情景记忆加权",
        description="检索排序时对情景记忆的额外权重系数。提高后更偏向近期经历和历史事件",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="情景记忆加权", en_US="Episodic Boost"),
            i18n_description=i18n_text(
                zh_CN="检索排序时对情景记忆的额外权重系数。提高后更偏向近期经历和历史事件",
                en_US="Extra weighting factor applied to episodic memories during retrieval ranking. Higher values bias toward experiences and events",
            ),
        ).model_dump(),
    )
    MEMORY_RETRIEVAL_RECENT_BOOST_HOURS: int = Field(
        default=24,
        title="近期记忆加权窗口",
        description="在该时间窗口内的记忆会被视为近期记忆，可额外获得近期加权",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="近期记忆加权窗口", en_US="Recent Boost Hours"),
            i18n_description=i18n_text(
                zh_CN="在该时间窗口内的记忆会被视为近期记忆，可额外获得近期加权",
                en_US="Memories within this time window are treated as recent and can receive an additional ranking boost",
            ),
        ).model_dump(),
    )
    MEMORY_RETRIEVAL_RECENT_BOOST_FACTOR: float = Field(
        default=1.1,
        title="近期记忆加权倍率",
        description="用于提升近期记忆排序分数的倍率，通常与近期记忆加权窗口配合使用",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="近期记忆加权倍率", en_US="Recent Boost Factor"),
            i18n_description=i18n_text(
                zh_CN="用于提升近期记忆排序分数的倍率，通常与近期记忆加权窗口配合使用",
                en_US="Multiplier used to boost the ranking score of recent memories, usually paired with the recent boost window",
            ),
        ).model_dump(),
    )
    MEMORY_RETRIEVAL_RELATION_BOOST: float = Field(
        default=1.05,
        title="关系记忆加权",
        description="关系检索结果合并回段落候选时的额外加权系数。提高后更容易命中实体关系相关记忆",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="关系记忆加权", en_US="Relation Boost"),
            i18n_description=i18n_text(
                zh_CN="关系检索结果合并回段落候选时的额外加权系数。提高后更容易命中实体关系相关记忆",
                en_US="Extra weighting applied when relation-based matches are merged back into paragraph candidates. Higher values favor entity-relation memories",
            ),
        ).model_dump(),
    )
    MEMORY_RETRIEVAL_RELATION_MATCH_LIMIT: int = Field(
        default=20,
        title="关系匹配实体上限",
        description="单次关系补召回时允许参与匹配的实体数量上限，用于限制关系扩展成本",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="关系匹配实体上限", en_US="Relation Match Limit"),
            i18n_description=i18n_text(
                zh_CN="单次关系补召回时允许参与匹配的实体数量上限，用于限制关系扩展成本",
                en_US="Maximum number of entities considered during relation-based recall expansion, used to cap relation matching cost",
            ),
        ).model_dump(),
    )
    MEMORY_CONSOLIDATION_BATCH_SIZE: int = Field(
        default=50,
        title="单次沉淀消息数",
        description="每次情景记忆沉淀最多读取的消息数量。较大值有利于形成完整片段，但会增加单批处理耗时",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="单次沉淀消息数", en_US="Consolidation Batch Size"),
            i18n_description=i18n_text(
                zh_CN="每次情景记忆沉淀最多读取的消息数量。较大值有利于形成完整片段，但会增加单批处理耗时",
                en_US="Maximum number of messages read in one episodic consolidation batch. Larger values help form fuller context but increase batch latency",
            ),
        ).model_dump(),
    )
    MEMORY_CONSOLIDATION_MIN_CONTENT_LENGTH: int = Field(
        default=10,
        title="最小沉淀内容长度",
        description="当合并后的消息内容低于该长度时，跳过本次情景记忆沉淀，避免产生价值过低的记忆",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="最小沉淀内容长度", en_US="Minimum Consolidation Content Length"),
            i18n_description=i18n_text(
                zh_CN="当合并后的消息内容低于该长度时，跳过本次情景记忆沉淀，避免产生价值过低的记忆",
                en_US="Skip episodic consolidation when the merged message content is shorter than this length to avoid very low-value memories",
            ),
        ).model_dump(),
    )
    MEMORY_CONSOLIDATION_MAX_SUMMARY_LENGTH: int = Field(
        default=200,
        title="沉淀摘要最大长度",
        description="情景记忆摘要字段的最大长度，用于控制数据库展示和检索摘要的体积",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="沉淀摘要最大长度", en_US="Consolidation Summary Max Length"),
            i18n_description=i18n_text(
                zh_CN="情景记忆摘要字段的最大长度，用于控制数据库展示和检索摘要的体积",
                en_US="Maximum length of the episodic memory summary field, used to control display and retrieval summary size",
            ),
        ).model_dump(),
    )
    MEMORY_CONSOLIDATION_MSG_THRESHOLD: int = Field(
        default=50,
        title="沉淀消息阈值",
        description="调度器判断是否触发自动情景沉淀时使用的消息数量阈值",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="沉淀消息阈值", en_US="Consolidation Message Threshold"),
            i18n_description=i18n_text(
                zh_CN="调度器判断是否触发自动情景沉淀时使用的消息数量阈值",
                en_US="Message-count threshold used by the scheduler to decide when automatic episodic consolidation should be triggered",
            ),
        ).model_dump(),
    )
    MEMORY_CONSOLIDATION_TIME_THRESHOLD_HOURS: float = Field(
        default=2.0,
        title="沉淀时间阈值",
        description="距离上次沉淀超过该时长后，即使消息数不高，也可能触发自动沉淀",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="沉淀时间阈值", en_US="Consolidation Time Threshold"),
            i18n_description=i18n_text(
                zh_CN="距离上次沉淀超过该时长后，即使消息数不高，也可能触发自动沉淀",
                en_US="If this duration has passed since the last consolidation, automatic consolidation may still be triggered even with a low message count",
            ),
        ).model_dump(),
    )
    MEMORY_CONSOLIDATION_MIN_INTERVAL_SECONDS: int = Field(
        default=300,
        title="最小沉淀间隔",
        description="两次自动情景沉淀之间的最小间隔，用于避免高活跃频道被频繁打断",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="最小沉淀间隔", en_US="Minimum Consolidation Interval"),
            i18n_description=i18n_text(
                zh_CN="两次自动情景沉淀之间的最小间隔，用于避免高活跃频道被频繁打断",
                en_US="Minimum interval between automatic episodic consolidations, used to avoid over-triggering on highly active channels",
            ),
        ).model_dump(),
    )
    MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS: float = Field(
        default=0.2,
        title="沉淀批次冷却间隔（秒）",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="沉淀批次冷却间隔（秒）", en_US="Consolidation Batch Cooldown Seconds"),
            i18n_description=i18n_text(
                zh_CN="单个沉淀批次结束后的短暂让步时间，用于降低对其他接口响应的影响",
                en_US="Short cooldown after each consolidation batch to reduce impact on other API responses",
            ),
        ).model_dump(),
    )
    MEMORY_EPISODIC_HALF_LIFE_SECONDS: int = Field(
        default=86400,
        title="情景记忆半衰期",
        description="对话沉淀出的情景记忆的默认半衰期秒数。有效权重低于清理阈值后记忆会被停用并删除向量，因此该值决定了「完全没被再次提及的记忆能存活多久」",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="情景记忆半衰期", en_US="Episodic Half-Life"),
            i18n_description=i18n_text(
                zh_CN="对话沉淀出的情景记忆的默认半衰期秒数。仅对新沉淀的记忆生效，已有记忆保持原值",
                en_US="Default half-life in seconds for episodic memories consolidated from conversations. Only affects newly consolidated memories",
            ),
        ).model_dump(),
    )
    MEMORY_RETRIEVAL_REINFORCE_BOOST: float = Field(
        default=0.3,
        title="检索命中强化增量",
        description="记忆被检索命中时增加的基础权重，并重置衰减计时。设为 0 表示关闭强化，此时记忆只会随时间单向衰减直至被清理",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="检索命中强化增量", en_US="Retrieval Reinforcement Boost"),
            i18n_description=i18n_text(
                zh_CN="记忆被检索命中时增加的基础权重并重置衰减计时，让常被想起的记忆留存更久。设为 0 关闭强化",
                en_US="Base weight added when a memory is hit by retrieval, also resetting its decay timer so frequently recalled memories persist longer. Set to 0 to disable",
            ),
        ).model_dump(),
    )
    MEMORY_RELATION_HALF_LIFE_SECONDS: int = Field(
        default=86400,
        title="关系半衰期",
        description="新创建关系记忆的默认半衰期秒数。值越小，关系有效权重衰减越快",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="关系半衰期", en_US="Relation Half-Life"),
            i18n_description=i18n_text(
                zh_CN="新创建关系记忆的默认半衰期秒数。值越小，关系有效权重衰减越快",
                en_US="Default half-life in seconds for newly created relation memories. Smaller values decay relation weight faster",
            ),
        ).model_dump(),
    )
    MEMORY_SEMANTIC_HALF_LIFE_DAYS: int = Field(
        default=30,
        title="语义记忆半衰期天数",
        description="CC 语义记忆的默认半衰期天数。较长的值更适合沉淀稳定可复用的经验结论",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="语义记忆半衰期天数", en_US="Semantic Half-Life Days"),
            i18n_description=i18n_text(
                zh_CN="CC 语义记忆的默认半衰期天数。较长的值更适合沉淀稳定可复用的经验结论",
                en_US="Default half-life in days for CC semantic memories. Larger values fit stable and reusable knowledge better",
            ),
        ).model_dump(),
    )
    MEMORY_SEMANTIC_MIN_RESULT_LENGTH: int = Field(
        default=60,
        title="语义沉淀最小结果长度",
        description="CC 任务结果短于该长度时不进行语义记忆沉淀，避免将过短回复误记为长期知识",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="语义沉淀最小结果长度", en_US="Semantic Minimum Result Length"),
            i18n_description=i18n_text(
                zh_CN="CC 任务结果短于该长度时不进行语义记忆沉淀，避免将过短回复误记为长期知识",
                en_US="Do not persist CC semantic memory when the task result is shorter than this length, to avoid storing very short replies as durable knowledge",
            ),
        ).model_dump(),
    )
    MEMORY_SEMANTIC_MAX_TASK_LENGTH: int = Field(
        default=300,
        title="语义沉淀任务摘要长度",
        description="CC 任务描述在写入语义记忆前保留的最大长度，用于控制摘要和正文体积",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="语义沉淀任务摘要长度", en_US="Semantic Task Max Length"),
            i18n_description=i18n_text(
                zh_CN="CC 任务描述在写入语义记忆前保留的最大长度，用于控制摘要和正文体积",
                en_US="Maximum preserved length of the CC task description before writing semantic memory, used to control summary and content size",
            ),
        ).model_dump(),
    )
    MEMORY_SEMANTIC_MAX_RESULT_LENGTH: int = Field(
        default=4000,
        title="语义沉淀结果最大长度",
        description="CC 任务结果写入语义记忆时保留的最大长度，用于限制超长产出对存储和检索的影响",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="语义沉淀结果最大长度", en_US="Semantic Result Max Length"),
            i18n_description=i18n_text(
                zh_CN="CC 任务结果写入语义记忆时保留的最大长度，用于限制超长产出对存储和检索的影响",
                en_US="Maximum preserved length of CC task results when writing semantic memory, used to cap storage and retrieval impact of very long outputs",
            ),
        ).model_dump(),
    )
    MEMORY_SEMANTIC_MAX_SUMMARY_LENGTH: int = Field(
        default=120,
        title="语义记忆摘要最大长度",
        description="CC 语义记忆摘要字段的最大长度，主要影响列表展示和简短检索摘要",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="语义记忆摘要最大长度", en_US="Semantic Summary Max Length"),
            i18n_description=i18n_text(
                zh_CN="CC 语义记忆摘要字段的最大长度，主要影响列表展示和简短检索摘要",
                en_US="Maximum length of the CC semantic memory summary field, mainly affecting list display and short retrieval summaries",
            ),
        ).model_dump(),
    )
    MEMORY_SCHEDULER_MAX_CONCURRENT: int = Field(
        default=3,
        title="最大并发沉淀数",
        description="后台调度器同时允许执行的最大沉淀任务数。增大可提高吞吐，但也会增加资源占用",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="最大并发沉淀数", en_US="Max Concurrent Consolidations"),
            i18n_description=i18n_text(
                zh_CN="后台调度器同时允许执行的最大沉淀任务数。增大可提高吞吐，但也会增加资源占用",
                en_US="Maximum number of consolidation tasks the background scheduler may run concurrently. Larger values improve throughput but increase resource usage",
            ),
        ).model_dump(),
    )
    MEMORY_SCHEDULER_STAGGER_DELAY_SECONDS: int = Field(
        default=5,
        title="错峰调度延迟",
        description="调度器为不同工作区任务增加的基础错峰延迟，用于降低多个沉淀任务同时抢占资源的概率",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="错峰调度延迟", en_US="Stagger Delay"),
            i18n_description=i18n_text(
                zh_CN="调度器为不同工作区任务增加的基础错峰延迟，用于降低多个沉淀任务同时抢占资源的概率",
                en_US="Base stagger delay added by the scheduler across workspace tasks to reduce simultaneous resource contention",
            ),
        ).model_dump(),
    )
    MEMORY_REBUILD_LOOKBACK_DAYS: int = Field(
        default=30,
        title="记忆重建回溯天数",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="记忆重建回溯天数", en_US="Memory Rebuild Lookback Days"),
            i18n_description=i18n_text(
                zh_CN="重建工作区记忆时，默认只从当前时间往前指定天数内的聊天和委托记录开始回放；设为 0 表示不限制",
                en_US="When rebuilding workspace memory, only replay chat and delegation records within the specified number of days from now by default; set to 0 for unlimited",
            ),
        ).model_dump(),
    )
    MEMORY_PRUNE_ENABLED: bool = Field(
        default=True,
        title="启用自动记忆清理",
        description="控制后台是否定期清理低价值记忆。关闭后不会自动 prune，但手动清理仍可使用",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="启用自动记忆清理", en_US="Enable Automatic Memory Pruning"),
            i18n_description=i18n_text(
                zh_CN="控制后台是否定期清理低价值记忆。关闭后不会自动 prune，但手动清理仍可使用",
                en_US="Controls whether low-value memories are pruned periodically in the background. When disabled, automatic pruning stops but manual pruning remains available",
            ),
        ).model_dump(),
    )
    MEMORY_PRUNE_INTERVAL_HOURS: int = Field(
        default=6,
        title="自动清理间隔（小时）",
        description="后台自动记忆清理的执行间隔，仅在启用自动记忆清理时生效",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="自动清理间隔（小时）", en_US="Automatic Prune Interval (Hours)"),
            i18n_description=i18n_text(
                zh_CN="后台自动记忆清理的执行间隔，仅在启用自动记忆清理时生效",
                en_US="Execution interval for background automatic memory pruning, only effective when automatic pruning is enabled",
            ),
        ).model_dump(),
    )
    MEMORY_PRUNE_PARAGRAPH_THRESHOLD: float = Field(
        default=0.05,
        title="段落清理阈值",
        description="段落记忆有效权重低于该阈值时可被自动清理。提高该值会更激进地淘汰低价值段落",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="段落清理阈值", en_US="Paragraph Prune Threshold"),
            i18n_description=i18n_text(
                zh_CN="段落记忆有效权重低于该阈值时可被自动清理。提高该值会更激进地淘汰低价值段落",
                en_US="Paragraph memories with effective weight below this threshold may be pruned automatically. Higher values prune low-value paragraphs more aggressively",
            ),
        ).model_dump(),
    )
    MEMORY_PRUNE_RELATION_THRESHOLD: float = Field(
        default=0.03,
        title="关系清理阈值",
        description="关系记忆有效权重低于该阈值时可被自动清理。提高该值会更激进地淘汰低价值关系",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="关系清理阈值", en_US="Relation Prune Threshold"),
            i18n_description=i18n_text(
                zh_CN="关系记忆有效权重低于该阈值时可被自动清理。提高该值会更激进地淘汰低价值关系",
                en_US="Relation memories with effective weight below this threshold may be pruned automatically. Higher values prune low-value relations more aggressively",
            ),
        ).model_dump(),
    )
    MEMORY_EPISODE_ENABLED: bool = Field(
        default=True,
        title="启用 Episode 聚合",
        description="控制是否启用 episodic paragraph 到 Episode 事件的聚合能力。关闭后不再自动或手动形成 Episode",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="启用 Episode 聚合", en_US="Enable Episode Aggregation"),
            i18n_description=i18n_text(
                zh_CN="控制是否启用 episodic paragraph 到 Episode 事件的聚合能力。关闭后不再自动或手动形成 Episode",
                en_US="Controls whether episodic paragraphs can be aggregated into Episode events. When disabled, Episode creation stops for both automatic and manual flows",
            ),
        ).model_dump(),
    )
    MEMORY_EPISODE_MIN_PARAGRAPHS: int = Field(
        default=3,
        title="Episode 最小段落数",
        description="形成一个 Episode 至少需要的情景段落数量。值越大，Episode 事件会更保守",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="Episode 最小段落数", en_US="Episode Minimum Paragraphs"),
            i18n_description=i18n_text(
                zh_CN="形成一个 Episode 至少需要的情景段落数量。值越大，Episode 事件会更保守",
                en_US="Minimum number of episodic paragraphs required to form an Episode. Larger values make Episode creation more conservative",
            ),
        ).model_dump(),
    )
    MEMORY_EPISODE_TIME_GAP_MINUTES: int = Field(
        default=30,
        title="Episode 时间间隔阈值",
        description="聚合 Episode 时，相邻段落允许的最大时间间隔。超过该间隔会被视为不同事件",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="Episode 时间间隔阈值", en_US="Episode Time Gap Minutes"),
            i18n_description=i18n_text(
                zh_CN="聚合 Episode 时，相邻段落允许的最大时间间隔。超过该间隔会被视为不同事件",
                en_US="Maximum allowed time gap between adjacent paragraphs when aggregating Episodes. Larger gaps are treated as different events",
            ),
        ).model_dump(),
    )
    MEMORY_EPISODE_AUTO_CONSOLIDATE: bool = Field(
        default=True,
        title="自动 Episode 聚合",
        description="控制情景记忆沉淀后是否顺带自动尝试 Episode 聚合。关闭后仅可通过手动入口触发",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="自动 Episode 聚合", en_US="Auto Episode Consolidation"),
            i18n_description=i18n_text(
                zh_CN="控制情景记忆沉淀后是否顺带自动尝试 Episode 聚合。关闭后仅可通过手动入口触发",
                en_US="Controls whether Episode aggregation is attempted automatically after episodic consolidation. When disabled, it can only be triggered manually",
            ),
        ).model_dump(),
    )
    MEMORY_EPISODE_SCAN_LIMIT: int = Field(
        default=200,
        title="Episode 聚合扫描上限",
        description="单次 Episode 聚合扫描的最大候选段落数，用于限制聚合成本",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="Episode 聚合扫描上限", en_US="Episode Scan Limit"),
            i18n_description=i18n_text(
                zh_CN="单次 Episode 聚合扫描的最大候选段落数，用于限制聚合成本",
                en_US="Maximum number of candidate paragraphs scanned in one Episode aggregation run, used to cap aggregation cost",
            ),
        ).model_dump(),
    )
    MEMORY_STARTUP_RECOVERY_ENABLED: bool = Field(
        default=False,
        title="启动时恢复历史沉淀任务",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="启动时恢复历史沉淀任务", en_US="Recover Historical Consolidations On Startup"),
            i18n_description=i18n_text(
                zh_CN="启用后会在启动时扫描工作区历史未沉淀消息并自动补跑，可能显著占用系统资源",
                en_US="When enabled, scans and resumes historical unconsolidated messages on startup, which may significantly consume system resources",
            ),
        ).model_dump(),
    )
    MEMORY_STARTUP_RECOVERY_MAX_TASKS: int = Field(
        default=1,
        title="启动恢复最大任务数",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="启动恢复最大任务数", en_US="Startup Recovery Max Tasks"),
            i18n_description=i18n_text(
                zh_CN="限制启动时自动恢复的沉淀任务数，0 表示不限制",
                en_US="Limit the number of consolidation tasks automatically recovered on startup, 0 means unlimited",
            ),
        ).model_dump(),
    )
    MEMORY_INSTANCE_SHARING_GROUPS: Dict[str, List[str]] = Field(
        default_factory=dict,
        title="记忆实例共享组",
        description="配置哪些实例之间共享记忆。键为自定义组名，值为该组内共享记忆的实例标识列表（在总后台的实例列表里可以看到）。默认空表示所有实例的记忆互相隔离；未被任何组覆盖的实例只能检索自己的记忆",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="记忆实例共享组", en_US="Memory Instance Sharing Groups"),
            i18n_description=i18n_text(
                zh_CN="配置哪些实例之间共享记忆。键为自定义组名，值为该组内共享记忆的实例标识（如 OneBot 协议端的 QQ 号）列表。默认空表示所有实例的记忆互相隔离；未被任何组覆盖的实例只能检索自己的记忆",
                en_US="Configures which instances share memories. Each key is a custom group name and each value lists the instance keys (e.g. the QQ number of a OneBot protocol client) sharing memories within that group. Empty by default, meaning every instance is fully isolated; an instance not covered by any group can only retrieve its own memories",
            ),
        ).model_dump(),
    )
    MEMORY_LOG_RETENTION_DAYS: int = Field(
        default=30,
        title="强化日志保留天数",
        description="记忆强化日志在数据库中的默认保留天数。当前主要影响 reinforcement log 清理，不影响手动导出的其他故障日志文件",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="强化日志保留天数", en_US="Reinforcement Log Retention Days"),
            i18n_description=i18n_text(
                zh_CN="记忆强化日志在数据库中的默认保留天数。当前主要影响 reinforcement log 清理，不影响手动导出的其他故障日志文件",
                en_US="Default retention days for memory reinforcement logs in the database. Currently affects reinforcement-log cleanup only, not other exported failure log files",
            ),
        ).model_dump(),
    )

    """OpenAI API 配置"""
    MODEL_GROUPS: Dict[str, ModelConfigGroup] = Field(
        default={
            "default": ModelConfigGroup(
                CHAT_MODEL="gemini-2.5-flash",
                CHAT_PROXY="",
                BASE_URL="https://api.nekro.ai/v1",
                API_KEY="",
                MODEL_TYPE="chat",
                ENABLE_VISION=True,
                ENABLE_COT=False,
            ),
            "default-draw": ModelConfigGroup(
                CHAT_MODEL="Kolors",
                CHAT_PROXY="",
                BASE_URL="https://api.nekro.ai/v1",
                API_KEY="",
                MODEL_TYPE="draw",
                ENABLE_VISION=False,
                ENABLE_COT=False,
            ),
            "default-draw-chat": ModelConfigGroup(
                CHAT_MODEL="gemini-2.5-flash-image-preview",
                CHAT_PROXY="",
                BASE_URL="https://api.nekro.ai/v1",
                API_KEY="",
                MODEL_TYPE="draw",
                ENABLE_VISION=False,
                ENABLE_COT=False,
            ),
            "text-embedding": ModelConfigGroup(
                CHAT_MODEL="text-embedding-v3",
                CHAT_PROXY="",
                BASE_URL="https://api.nekro.ai/v1",
                API_KEY="",
                MODEL_TYPE="embedding",
                ENABLE_VISION=False,
                ENABLE_COT=False,
            ),
        },
        title="模型组配置",
        json_schema_extra=ExtraField(is_hidden=True).model_dump(),
    )
    USE_MODEL_GROUP: str = Field(
        default="default",
        title="使用的主模型组",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="模型配置",
                en_US="Model Configuration",
            ),
            ref_model_groups=True,
            required=True,
            model_type="chat",
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="使用的主模型组",
                en_US="Primary Model Group",
            ),
            i18n_description=i18n_text(
                zh_CN="主要使用的模型组，可在 '模型组' 选项卡配置",
                en_US="Primary model group to use, can be configured in 'Model Groups' tab",
            ),
        ).model_dump(),
        description="主要使用的模型组，可在 `模型组` 选项卡配置",
    )
    DEBUG_MIGRATION_MODEL_GROUP: str = Field(
        default="default",
        title="调试/Agent 迁移模型组",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="模型配置",
                en_US="Model Configuration",
            ),
            ref_model_groups=True,
            model_type="chat",
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="调试/Agent 迁移模型组",
                en_US="Debug/Agent Migration Model Group",
            ),
            i18n_description=i18n_text(
                zh_CN="主模型组编写的代码执行出错或产生 Agent 反馈时，迭代调用时使用的模型组",
                en_US="Model group used for iterative calls when code execution fails or Agent feedback is generated",
            ),
        ).model_dump(),
        description="主模型组编写的代码执行出错或产生 Agent 反馈时，迭代调用时使用的模型组",
    )
    FALLBACK_MODEL_GROUP: str = Field(
        default="default",
        title="备用模型组",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="模型配置",
                en_US="Model Configuration",
            ),
            ref_model_groups=True,
            model_type="chat",
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="备用模型组",
                en_US="Fallback Model Group",
            ),
            i18n_description=i18n_text(
                zh_CN="当主模型组不可用时，使用备用模型组",
                en_US="Model group used when primary model group is unavailable",
            ),
        ).model_dump(),
        description="当主模型组不可用时, 使用备用模型组",
    )

    """聊天配置"""
    AI_CHAT_DEFAULT_PRESET_ID: Optional[int] = Field(
        default=None,
        title="默认人设",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            ref_presets=True,
            ref_presets_no_default=True,
            i18n_title=i18n_text(
                zh_CN="默认人设",
                en_US="Default Preset",
            ),
            i18n_description=i18n_text(
                zh_CN="为空时使用系统内置默认人设",
                en_US="Uses built-in default preset if empty",
            ),
        ).model_dump(),
    )
    AI_CHAT_PRESET_NAME: str = Field(
        default="",
        title="默认聊天人设名 (已弃用)",
        json_schema_extra=ExtraField(
            is_hidden=True,
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            i18n_title=i18n_text(
                zh_CN="默认聊天人设名 (已弃用)",
                en_US="Default Chat Preset Name (Deprecated)",
            ),
            i18n_description=i18n_text(
                zh_CN="已弃用，请使用「默认人设 ID」代替",
                en_US="Deprecated, please use 'Default Preset ID' instead",
            ),
        ).model_dump(),
    )
    AI_CHAT_PRESET_SETTING: str = Field(
        default="",
        title="默认聊天人设详情 (已弃用)",
        json_schema_extra=ExtraField(
            is_hidden=True,
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            is_textarea=True,
            i18n_title=i18n_text(
                zh_CN="默认聊天人设详情 (已弃用)",
                en_US="Default Chat Preset Details (Deprecated)",
            ),
            i18n_description=i18n_text(
                zh_CN="已弃用，请使用「默认人设 ID」代替",
                en_US="Deprecated, please use 'Default Preset ID' instead",
            ),
        ).model_dump(),
    )
    AI_CHAT_CONTEXT_EXPIRE_SECONDS: int = Field(
        default=60 * 30,
        title="对话上下文过期时间 (秒)",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="对话上下文过期时间 (秒)",
                en_US="Context Expiration Time (seconds)",
            ),
            i18n_description=i18n_text(
                zh_CN="超出该时间范围的消息不会被 AI 回复时参考",
                en_US="Messages older than this will not be referenced by AI",
            ),
        ).model_dump(),
        description="超出该时间范围的消息不会被 AI 回复时参考",
    )
    AI_CHAT_CONTEXT_MAX_LENGTH: int = Field(
        default=32,
        title="对话上下文最大条数",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="对话上下文最大条数",
                en_US="Max Context Message Count",
            ),
            i18n_description=i18n_text(
                zh_CN="AI 对话上下文最大条数，超出该条数会自动截断",
                en_US="Maximum number of context messages, exceeding this will be truncated",
            ),
        ).model_dump(),
        description="AI 对话上下文最大条数, 超出该条数会自动截断",
    )
    AI_SCRIPT_MAX_RETRY_TIMES: int = Field(
        default=3,
        title="代码执行调试 / Agent 迭代最大次数",
        description="执行代码过程出错或者产生 Agent 反馈时，进行迭代调用允许的最大次数，增大该值可能略微增加调试成功概率，过大会造成响应时间增加、Token 消耗增加等",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="模型配置",
                en_US="Model Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="代码执行调试 / Agent 迭代最大次数",
                en_US="Max Code Debug / Agent Iteration Times",
            ),
            i18n_description=i18n_text(
                zh_CN="执行代码过程出错或产生 Agent 反馈时，进行迭代调用允许的最大次数",
                en_US="Maximum iterations when code execution fails or Agent feedback is generated",
            ),
        ).model_dump(),
    )
    AI_CHAT_LLM_API_MAX_RETRIES: int = Field(
        default=3,
        title="模型 API 调用重试次数",
        description="模型组调用失败后重试次数，重试的最后一次将使用备用模型组",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="模型配置",
                en_US="Model Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="模型 API 调用重试次数",
                en_US="Model API Retry Count",
            ),
            i18n_description=i18n_text(
                zh_CN="模型组调用失败后重试次数，重试的最后一次将使用备用模型组",
                en_US="Retry count after model call failure, last retry will use fallback model group",
            ),
        ).model_dump(),
    )
    AI_DEBOUNCE_WAIT_SECONDS: float = Field(
        default=0.9,
        title="防抖等待时长 (秒)",
        description="收到触发消息时延迟指定时长再开始回复流程，防抖等待时长中继续收到的消息只会触发最后一条",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="防抖等待时长 (秒)",
                en_US="Debounce Wait Time (seconds)",
            ),
            i18n_description=i18n_text(
                zh_CN="收到触发消息时延迟指定时长再开始回复流程，防抖等待时长中继续收到的消息只会触发最后一条",
                en_US="Delay before starting reply process, only last message received during debounce will trigger",
            ),
        ).model_dump(),
    )
    AI_GENERATE_TIMEOUT: int = Field(
        default=180,
        title="AI 对话内容生成超时时间 (秒)",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="模型配置",
                en_US="Model Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="AI 对话内容生成超时时间 (秒)",
                en_US="AI Response Generation Timeout (seconds)",
            ),
            i18n_description=i18n_text(
                zh_CN="AI 大模型生成响应结果的最大等待时间，超过该时间会自动停止生成并报错",
                en_US="Maximum wait time for AI response generation, will stop and error if exceeded",
            ),
        ).model_dump(),
        description="AI 大模型生成响应结果的最大等待时间，超过该时间会自动停止生成并报错",
    )
    AI_IGNORED_PREFIXES: List[str] = Field(
        default=["#", "＃", "[Debug]", "[Opt Output]", "≡NA≡"],
        title="忽略的消息前缀",
        description="带有这些前缀的消息不会被参考或者触发",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            sub_item_name="前缀",
            i18n_title=i18n_text(
                zh_CN="忽略的消息前缀",
                en_US="Ignored Message Prefixes",
            ),
            i18n_description=i18n_text(
                zh_CN="带有这些前缀的消息不会被参考或者触发",
                en_US="Messages with these prefixes will not be referenced or triggered",
            ),
        ).model_dump(),
    )
    AI_COMMAND_OUTPUT_PREFIX: str = Field(
        default="≡NA≡:",
        title="命令输出前缀",
        description="命令输出前缀，用于标识命令输出",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="命令输出前缀",
                en_US="Command Output Prefix",
            ),
            i18n_description=i18n_text(
                zh_CN="命令输出前缀，用于标识命令输出",
                en_US="Prefix for command output identification",
            ),
        ).model_dump(),
    )
    ONEBOT_RECORD_SELF_SENT: bool = Field(
        default=True,
        title="记录同账号其他程序发出的消息",
        description=(
            "OneBot V11：把账号自身发出、但不是 NA 发的消息（如接在同一协议端上的其他框架）记入聊天记录和 AI 上下文。"
            "这些消息只记录，绝不触发回复。需要协议端开启「上报自身消息」（reportSelfMessage）：内置 NapCat 重启容器后自动开启；"
            "远程的 NapCat、SnowLuma 默认关闭，要在协议端手动打开（SnowLuma 保存即生效，不用重启）。实例详情页会显示协议端有没有在上报"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="记录同账号其他程序发出的消息",
                en_US="Record Messages Sent by Other Programs on This Account",
            ),
            i18n_description=i18n_text(
                zh_CN=(
                    "OneBot V11：把账号自身发出、但不是 NA 发的消息（如接在同一协议端上的其他框架）记入聊天记录和 AI 上下文。"
                    "这些消息只记录，绝不触发回复。需要协议端开启「上报自身消息」（reportSelfMessage）：内置 NapCat 重启容器后自动开启；"
                    "远程的 NapCat、SnowLuma 默认关闭，要在协议端手动打开（SnowLuma 保存即生效，不用重启）。实例详情页会显示协议端有没有在上报"
                ),
                en_US=(
                    "OneBot V11: record messages this account sent that did not come from NA (such as another framework on the same "
                    "protocol endpoint) in the chat history and AI context. They are recorded only and never trigger a reply. "
                    "The endpoint must have \"Report Self Messages\" (reportSelfMessage) enabled: the built-in NapCat turns it on after "
                    "the container restarts; remote NapCat and SnowLuma have it off by default and must be enabled on the endpoint "
                    "(SnowLuma applies it on save without a restart). The instance detail page shows whether the endpoint reports them"
                ),
            ),
        ).model_dump(),
    )
    AI_CHAT_DAILY_REPLY_LIMIT: int = Field(
        default=0,
        title="每日回复数量限制",
        description="每个频道每日 AI 回复数量上限，0 表示不限制",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="回复配额",
                en_US="Reply Quota",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="每日回复数量限制",
                en_US="Daily Reply Limit",
            ),
            i18n_description=i18n_text(
                zh_CN="每个频道每日 AI 回复数量上限，0 表示不限制",
                en_US="Maximum AI replies per channel per day, 0 = unlimited",
            ),
        ).model_dump(),
    )
    AI_CHAT_ENABLE_HOURLY_LIMIT: bool = Field(
        default=False,
        title="启用每小时限额",
        description="启用后将根据每日限额自动计算每小时回复上限，使回复更均匀分布",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="回复配额",
                en_US="Reply Quota",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="启用每小时限额",
                en_US="Enable Hourly Limit",
            ),
            i18n_description=i18n_text(
                zh_CN="启用后将根据每日限额自动计算每小时回复上限，使回复更均匀分布",
                en_US="Auto-calculate hourly reply limit from daily limit for even distribution",
            ),
        ).model_dump(),
    )
    AI_CHAT_QUOTA_WHITELIST_USERS: List[str] = Field(
        default=[],
        title="配额白名单用户",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="回复配额", en_US="Reply Quota"),
            i18n_title=i18n_text(zh_CN="配额白名单用户", en_US="Quota Whitelist Users"),
            i18n_description=i18n_text(
                zh_CN=(
                    "列表中的用户发送的消息不受每日/每小时配额限制。格式与管理员列表相同：纯号码为 OneBot V11 的 QQ 号，"
                    "其他平台写成「适配器标识:用户ID」，例如 telegram:123456；同样只在平台方可信的来源生效"
                    "（租户名下的实例、待认领实例、SSE / 邮件等发送者 ID 可由调用方填写的适配器里不生效）"
                ),
                en_US=(
                    "Messages from users in this list bypass daily/hourly quota limits. Same format as the "
                    "administrator list: a bare number is a OneBot V11 QQ number, other platforms use "
                    "'adapter_key:user_id' (e.g. telegram:123456). Only honored from platform-trusted sources "
                    "(not in tenant instances, unclaimed instances, or adapters whose sender ID is caller-supplied such as SSE / email)"
                ),
            ),
        ).model_dump(),
    )
    AI_CHAT_QUOTA_SUPER_USERS_EXEMPT: bool = Field(
        default=True,
        title="管理员不受配额限制",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="回复配额", en_US="Reply Quota"),
            i18n_title=i18n_text(zh_CN="管理员不受配额限制", en_US="Super Users Exempt from Quota"),
            i18n_description=i18n_text(
                zh_CN="启用后，SUPER_USERS 中的管理员发送的消息不受配额限制",
                en_US="When enabled, messages from SUPER_USERS bypass quota limits",
            ),
        ).model_dump(),
    )
    AI_CHAT_RANDOM_REPLY_PROBABILITY: float = Field(
        default=0.0,
        title="随机回复概率",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="随机回复概率",
                en_US="Random Reply Probability",
            ),
            i18n_description=i18n_text(
                zh_CN="随机回复概率，任意消息触发 AI 回复的概率，0.0 表示不启用，1.0 表示必定触发",
                en_US="Probability of AI replying to any message, 0.0 = disabled, 1.0 = always",
            ),
        ).model_dump(),
        description="随机回复概率，任意消息触发 AI 回复的概率，0.0 表示不启用，1.0 表示必定触发",
    )
    AI_CHAT_TRIGGER_REGEX: List[str] = Field(
        default=[],
        title="触发正则表达式",
        description="触发正则表达式，当消息匹配到正则表达式时，会触发 AI 回复",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            sub_item_name="表达式",
            i18n_title=i18n_text(
                zh_CN="触发正则表达式",
                en_US="Trigger Regex Patterns",
            ),
            i18n_description=i18n_text(
                zh_CN="当消息匹配到这些正则表达式时，会触发 AI 回复",
                en_US="AI will reply when message matches these regex patterns",
            ),
        ).model_dump(),
    )
    AI_CHAT_IGNORE_REGEX: List[str] = Field(
        default=[],
        title="忽略正则表达式",
        description="忽略正则表达式，当消息匹配到正则表达式时，不会触发 AI 回复",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            sub_item_name="表达式",
            i18n_title=i18n_text(
                zh_CN="忽略正则表达式",
                en_US="Ignore Regex Patterns",
            ),
            i18n_description=i18n_text(
                zh_CN="当消息匹配到这些正则表达式时，不会触发 AI 回复",
                en_US="AI will not reply when message matches these regex patterns",
            ),
        ).model_dump(),
    )
    AI_RESPONSE_PRE_DROP_REGEX: List[str] = Field(
        default=[],
        title="AI 响应预处理丢弃正则表达式",
        description="使用正则表达式匹配 AI 预响应结果，丢弃匹配到的内容段再执行后续思维链解析、代码解析等内容",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            sub_item_name="表达式",
            i18n_title=i18n_text(
                zh_CN="AI 响应预处理丢弃正则表达式",
                en_US="AI Response Pre-Drop Regex",
            ),
            i18n_description=i18n_text(
                zh_CN="匹配 AI 预响应结果，丢弃匹配到的内容段再执行后续解析",
                en_US="Match and drop content from AI pre-response before further parsing",
            ),
        ).model_dump(),
    )
    AI_CONTEXT_LENGTH_PER_MESSAGE: int = Field(
        default=768,
        title="单条消息最大长度 (字符)",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="单条消息最大长度 (字符)",
                en_US="Max Length Per Message (characters)",
            ),
            i18n_description=i18n_text(
                zh_CN="聊天上下文单条消息最大长度，超出该长度会自动截取并缩略显示",
                en_US="Maximum length per message in context, exceeding will be truncated",
            ),
        ).model_dump(),
        description="聊天上下文单条消息最大长度，超出该长度会自动截取并缩略显示",
    )
    AI_CONTEXT_LENGTH_PER_SESSION: int = Field(
        default=5120,
        title="聊天上下文最大长度 (字符)",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="聊天上下文最大长度 (字符)",
                en_US="Max Context Length (characters)",
            ),
            i18n_description=i18n_text(
                zh_CN="聊天历史记录上下文最大长度，超出该长度会自动截断",
                en_US="Maximum context length for chat history, exceeding will be truncated",
            ),
        ).model_dump(),
        description="聊天历史记录上下文最大长度，超出该长度会自动截断",
    )
    AI_VISION_IMAGE_LIMIT: int = Field(
        default=5,
        title="视觉参考图片数量限制",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            i18n_title=i18n_text(
                zh_CN="视觉参考图片数量限制",
                en_US="Vision Image Count Limit",
            ),
        ).model_dump(),
    )
    AI_VISION_IMAGE_SIZE_LIMIT_KB: int = Field(
        default=1024,
        title="视觉图片大小限制 (KB)",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="视觉图片大小限制 (KB)",
                en_US="Vision Image Size Limit (KB)",
            ),
            i18n_description=i18n_text(
                zh_CN="每次传递的图片大小限制，超出此大小的图片会被自动压缩到限制内传递",
                en_US="Image size limit per transmission, exceeding images will be compressed",
            ),
        ).model_dump(),
        description="每次传递的图片大小限制，超出此大小的图片会被自动压缩到限制内传递",
    )
    AI_SYSTEM_NOTIFY_WINDOW_SIZE: int = Field(
        default=10,
        title="聊天上下文系统消息通知窗口大小",
        description="聊天上下文系统消息通知窗口大小，超出该大小的系统消息不会被参考",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="聊天上下文系统消息通知窗口大小",
                en_US="System Message Notification Window Size",
            ),
            i18n_description=i18n_text(
                zh_CN="聊天上下文系统消息通知窗口大小，超出该大小的系统消息不会被参考",
                en_US="System message notification window size, messages outside will not be referenced",
            ),
        ).model_dump(),
    )
    AI_SYSTEM_NOTIFY_LIMIT: int = Field(
        default=3,
        title="聊天上下文系统消息通知条数限制",
        description="聊天上下文系统消息通知条数限制，超出该条数不会被参考",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="聊天上下文系统消息通知条数限制",
                en_US="System Message Notification Count Limit",
            ),
            i18n_description=i18n_text(
                zh_CN="聊天上下文系统消息通知条数限制，超出该条数不会被参考",
                en_US="System message notification count limit, exceeding will not be referenced",
            ),
        ).model_dump(),
    )
    AI_ALWAYS_INCLUDE_MSG_ID: bool = Field(
        default=False,
        title="始终呈现所有消息的 ID",
        description="启用后上下文中将始终呈现所有消息的 ID，这将占用额外的上下文长度，但允许 AI 在回复时更灵活地引用消息或使用插件对特定消息进行处理",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="始终呈现所有消息的 ID",
                en_US="Always Include Message IDs",
            ),
            i18n_description=i18n_text(
                zh_CN="启用后上下文中将始终呈现所有消息的 ID，允许 AI 更灵活地引用消息",
                en_US="When enabled, all message IDs will be included in context for flexible referencing",
            ),
        ).model_dump(),
    )
    AI_INCLUDE_TOME_INDICATOR: bool = Field(
        default=False,
        title="在历史消息中标注消息指向标记",
        description="启用后，群聊历史中每条人类消息会附加 tome:true/false 标记，表示该消息是否通过固定规则（如 @ 提及）判断为直接指向当前 Bot。AI 会将其作为辅助参考，不会盲信该标记，仍会结合上下文综合判断。",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="在历史消息中标注消息指向标记",
                en_US="Include Message Addressee Indicator",
            ),
            i18n_description=i18n_text(
                zh_CN="启用后群聊历史中每条人类消息会附加 tome:true/false 标记，辅助 AI 识别消息归属，避免多 Bot 场景下错误接话",
                en_US="When enabled, each human message in group chat history will include a tome:true/false indicator to help AI identify message ownership in multi-bot scenarios",
            ),
        ).model_dump(),
    )
    AI_SHOW_REMOTE_URL: bool = Field(
        default=False,
        title="显示远程资源 URL",
        description="启用后若资源远程 URL 可用，将在上下文中追加提供远程 URL 供 AI 参考使用",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="显示远程资源 URL",
                en_US="Show Remote Resource URL",
            ),
            i18n_description=i18n_text(
                zh_CN="启用后若资源远程 URL 可用，将在上下文中追加提供远程 URL 供 AI 参考使用",
                en_US="When enabled, if the resource remote URL is available, it will be appended to the context for AI to reference",
            ),
        ).model_dump(),
    )
    AI_REQUEST_STREAM_MODE: bool = Field(
        default=False,
        title="启用流式请求",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="模型配置",
                en_US="Model Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="启用流式请求",
                en_US="Enable Stream Mode",
            ),
            i18n_description=i18n_text(
                zh_CN="启用后 AI 会以流式请求方式返回响应，可能解决某些 LLM 请求异常的问题",
                en_US="AI will use streaming mode for responses, may fix some LLM request issues",
            ),
        ).model_dump(),
        description="启用后 AI 会以流式请求方式返回响应，再合并解析，这可能解决某些 LLM 请求异常的问题，但是会丢失准确的 Token 统计信息",
    )
    AI_STREAM_FIRST_TOKEN_TIMEOUT: int = Field(
        default=60,
        title="流式首 Token 超时 (秒)",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="模型配置",
                en_US="Model Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="流式首 Token 超时 (秒)",
                en_US="Stream First Token Timeout (seconds)",
            ),
            i18n_description=i18n_text(
                zh_CN="仅在启用流式请求时生效；若在指定时间内未收到首个有效流式片段，则判定本次请求失败并进入后续重试",
                en_US="Only effective when stream mode is enabled; if no first valid stream chunk is received within this time, the request fails and enters subsequent retries",
            ),
        ).model_dump(),
        description="仅在启用流式请求时生效。若供应商在指定时间内未返回首个有效流式片段（空块不计入），则立即判定本次请求失败并进入后续重试，用于减少长时间无响应等待。",
    )

    """聊天设置"""
    SESSION_GROUP_ACTIVE_DEFAULT: bool = Field(
        default=True,
        title="新群聊默认启用聊天",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            i18n_title=i18n_text(
                zh_CN="新群聊默认启用聊天",
                en_US="Enable Chat for New Groups by Default",
            ),
        ).model_dump(),
    )
    SESSION_PRIVATE_ACTIVE_DEFAULT: bool = Field(
        default=True,
        title="新私聊默认启用聊天",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            i18n_title=i18n_text(
                zh_CN="新私聊默认启用聊天",
                en_US="Enable Chat for New Private Chats by Default",
            ),
        ).model_dump(),
    )
    SESSION_ENABLE_FAILED_LLM_FEEDBACK: bool = Field(
        default=True,
        title="启用本轮失败反馈",
        # 这个开关的覆盖面比名字大：它现在管的是「这一轮没能给出结果」的**全部**四条出口
        # （LLM 全部重试失败 / 迭代次数耗尽 / 兜底超时 / 其它异常），不只是 LLM 调用本身。
        # 字段名保持不变——改名会让所有存量覆盖配置失效。
        description="启用后，本轮对话没能给出结果时会在聊天里发一条提示（LLM 调用失败、迭代次数耗尽、处理超时、其它异常）",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="启用本轮失败反馈",
                en_US="Enable Round Failure Feedback",
            ),
            i18n_description=i18n_text(
                zh_CN="启用后，本轮对话没能给出结果时会在聊天里发一条提示（LLM 调用失败、迭代次数耗尽、处理超时、其它异常）",
                en_US="Send a chat notice whenever a round fails to produce a result: LLM call failure, iteration limit reached, processing timeout, or any other error",
            ),
        ).model_dump(),
    )
    SESSION_FAILED_FEEDBACK_COOLDOWN_SECONDS: float = Field(
        default=180.0,
        title="失败反馈冷却 (秒)",
        description="同一会话两条「本轮失败」提示之间的最小秒数，避免故障持续时把提示本身刷成新的洪水；0 表示每次都提示",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="失败反馈冷却 (秒)",
                en_US="Failure Feedback Cooldown (seconds)",
            ),
            i18n_description=i18n_text(
                zh_CN="同一会话两条「本轮失败」提示之间的最小秒数，避免故障持续时把提示本身刷成新的洪水；0 表示每次都提示",
                en_US="Minimum seconds between two round-failure notices in the same session, so the notice itself does not become a new flood. 0 notifies every time",
            ),
        ).model_dump(),
    )

    SESSION_STALL_REPEAT_THRESHOLD: int = Field(
        default=2,
        title="无进展提前收尾阈值",
        # 默认值必须与 `services/agent/run_agent.py:STALL_REPEAT_THRESHOLD` 逐字一致
        # （tests 里有一条用例把两处钉在一起）。做成配置项的理由：这条判据一旦在线上误伤，
        # 改造前唯一的止血手段是改代码重部署，与本仓「行为变化要能被运维关掉」的惯例不符。
        description="同一个错误签名连续出现多少次就判定「再重试也没有进展」并提前结束本轮；0 表示关闭该判据（回退到烧满迭代次数的旧行为），小于 2 的正数会被抬到 2",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="无进展提前收尾阈值",
                en_US="Stalled Round Early-Exit Threshold",
            ),
            i18n_description=i18n_text(
                zh_CN="同一个错误签名连续出现多少次就判定「再重试也没有进展」并提前结束本轮；0 表示关闭该判据（回退到烧满迭代次数的旧行为），小于 2 的正数会被抬到 2",
                en_US="How many times the same error signature may repeat in a row before the round is judged to be making no progress and ends early. 0 disables the check (falling back to burning every iteration); positive values below 2 are raised to 2",
            ),
        ).model_dump(),
    )
    SESSION_CODE_MISSING_MAX_ATTEMPTS: int = Field(
        default=2,
        title="无可执行代码提前收尾次数",
        # 默认值必须与 `services/agent/run_agent.py:CODE_MISSING_MAX_ATTEMPTS` 逐字一致。
        # 关掉的只是**提前收尾**：「没有可执行代码就不进沙盒」是安全闸门不是启发式，
        # 任何取值下都照常生效，否则关掉阈值等于把整段自然语言重新送进容器执行。
        description="模型连续多少次回复里都没有可执行代码块就提前结束本轮；0 表示关闭该判据（只受迭代次数上限约束），小于 2 的正数会被抬到 2。无论取值多少，没有代码的回复都不会被送进沙盒执行",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="无可执行代码提前收尾次数",
                en_US="Missing-Code Early-Exit Attempts",
            ),
            i18n_description=i18n_text(
                zh_CN="模型连续多少次回复里都没有可执行代码块就提前结束本轮；0 表示关闭该判据（只受迭代次数上限约束），小于 2 的正数会被抬到 2。无论取值多少，没有代码的回复都不会被送进沙盒执行",
                en_US="How many consecutive replies without an executable code block end the round early. 0 disables the check (only the iteration limit applies); positive values below 2 are raised to 2. Replies without code are never executed in the sandbox regardless of this value",
            ),
        ).model_dump(),
    )

    """公告配置"""
    ANNOUNCEMENT_MIN_INTERVAL_SECONDS: float = Field(
        default=1.0,
        title="公告发送最小间隔 (秒)",
        description="同一适配器实例内两条公告之间的最小随机等待时长，不同实例之间互不影响",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="公告",
                en_US="Announcement",
            ),
            i18n_title=i18n_text(
                zh_CN="公告发送最小间隔 (秒)",
                en_US="Announcement Min Interval (seconds)",
            ),
            i18n_description=i18n_text(
                zh_CN="同一适配器实例内两条公告之间的最小随机等待时长，不同实例之间互不影响",
                en_US="Minimum random delay between two announcements within the same adapter instance; different instances are independent",
            ),
        ).model_dump(),
    )
    ANNOUNCEMENT_MAX_INTERVAL_SECONDS: float = Field(
        default=5.0,
        title="公告发送最大间隔 (秒)",
        description="同一适配器实例内两条公告之间的最大随机等待时长，小于最小间隔时按最小间隔处理",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="公告",
                en_US="Announcement",
            ),
            i18n_title=i18n_text(
                zh_CN="公告发送最大间隔 (秒)",
                en_US="Announcement Max Interval (seconds)",
            ),
            i18n_description=i18n_text(
                zh_CN="同一适配器实例内两条公告之间的最大随机等待时长，小于最小间隔时按最小间隔处理",
                en_US="Maximum random delay between two announcements within the same adapter instance; clamped to the minimum interval when smaller",
            ),
        ).model_dump(),
    )
    ANNOUNCEMENT_FAILURE_BREAK_RATIO: float = Field(
        default=0.5,
        title="公告失败率熔断阈值",
        description="已投递样本足够时失败率超过该比例将中止整个公告任务，避免账号被持续风控；设为 1 表示不熔断",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="公告",
                en_US="Announcement",
            ),
            i18n_title=i18n_text(
                zh_CN="公告失败率熔断阈值",
                en_US="Announcement Failure Break Ratio",
            ),
            i18n_description=i18n_text(
                zh_CN="已投递样本足够时失败率超过该比例将中止整个公告任务，避免账号被持续风控；设为 1 表示不熔断",
                en_US="Abort the whole announcement task once the failure ratio exceeds this value (with enough samples); set to 1 to disable the breaker",
            ),
        ).model_dump(),
    )
    ANNOUNCEMENT_MAX_CHANNELS_PER_TASK: int = Field(
        default=500,
        title="单次公告最大会话数",
        description="单个公告任务允许覆盖的会话数量上限，超过时拒绝提交",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="公告",
                en_US="Announcement",
            ),
            i18n_title=i18n_text(
                zh_CN="单次公告最大会话数",
                en_US="Max Channels Per Announcement",
            ),
            i18n_description=i18n_text(
                zh_CN="单个公告任务允许覆盖的会话数量上限，超过时拒绝提交",
                en_US="Maximum number of chat channels a single announcement task may cover; submission is rejected beyond this limit",
            ),
        ).model_dump(),
    )

    """沙盒配置"""
    SANDBOX_IMAGE_NAME: str = Field(
        default="ghcr.io/akiyo-dayo/nekro-agent-byakiyo-sandbox",
        title="沙盒镜像名称",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="沙盒配置",
                en_US="Sandbox Configuration",
            ),
            i18n_title=i18n_text(
                zh_CN="沙盒镜像名称",
                en_US="Sandbox Image Name",
            ),
        ).model_dump(),
    )
    SANDBOX_RUNNING_TIMEOUT: int = Field(
        default=120,
        title="沙盒超时时间 (秒)",
        description="每个沙盒容器最长运行时间，超过该时间沙盒容器会被强制停止",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="沙盒配置",
                en_US="Sandbox Configuration",
            ),
            i18n_title=i18n_text(
                zh_CN="沙盒超时时间 (秒)",
                en_US="Sandbox Timeout (seconds)",
            ),
            i18n_description=i18n_text(
                zh_CN="每个沙盒容器最长运行时间，超过该时间沙盒容器会被强制停止",
                en_US="Maximum runtime for each sandbox container, will be forcefully stopped if exceeded",
            ),
        ).model_dump(),
    )
    SANDBOX_MAX_CONCURRENT: int = Field(
        default=4,
        title="最大并发沙盒数",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="沙盒配置",
                en_US="Sandbox Configuration",
            ),
            i18n_title=i18n_text(
                zh_CN="最大并发沙盒数",
                en_US="Max Concurrent Sandboxes",
            ),
        ).model_dump(),
    )
    # 实例级并发配额：本组**有意标 overridable**，与插件故障隔离那组相反。
    # 插件在进程里只加载一份、被所有实例共用，隔离状态因此天然是进程级的；而"这个实例
    # 最多能同时占几个沙盒名额"天然是按实例的资源分配，正是四层配置的实例层要表达的东西。
    # 只有全局容量 SANDBOX_MAX_CONCURRENT 保持不可覆盖——它是整个进程的物理容量，
    # 让某一个实例的覆盖值决定全进程能跑多少沙盒是说不通的。
    SANDBOX_INSTANCE_MAX_CONCURRENT: int = Field(
        default=0,
        title="单实例最大并发沙盒数",
        description="单个实例最多同时占用的沙盒名额，0 表示不额外限制（仅受全局上限与公平轮转约束）",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_category=i18n_text(zh_CN="沙盒配置", en_US="Sandbox Configuration"),
            i18n_title=i18n_text(zh_CN="单实例最大并发沙盒数", en_US="Per-Instance Max Concurrent Sandboxes"),
            i18n_description=i18n_text(
                zh_CN="单个实例最多同时占用的沙盒名额，0 表示不额外限制（仅受全局上限与公平轮转约束）",
                en_US="Maximum sandbox slots one instance may hold at once; 0 means no extra cap (global limit and fair rotation still apply)",
            ),
        ).model_dump(),
    )
    SANDBOX_INSTANCE_QUEUE_LIMIT: int = Field(
        default=64,
        title="单实例沙盒排队上限",
        description="单个实例最多有多少次执行在排队等待名额，超出的请求会被明确拒绝而不是无限堆积；0 表示不限制队列长度",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_category=i18n_text(zh_CN="沙盒配置", en_US="Sandbox Configuration"),
            i18n_title=i18n_text(zh_CN="单实例沙盒排队上限", en_US="Per-Instance Sandbox Queue Limit"),
            i18n_description=i18n_text(
                zh_CN="单个实例最多有多少次执行在排队等待名额，超出的请求会被明确拒绝而不是无限堆积；0 表示不限制队列长度",
                en_US="How many executions of one instance may wait for a slot; anything beyond is rejected explicitly instead of piling up. 0 means unlimited queue",
            ),
        ).model_dump(),
    )
    SANDBOX_INSTANCE_QUEUE_TIMEOUT_SECONDS: float = Field(
        default=180.0,
        title="单实例沙盒排队超时",
        description="一次执行最多等待多久名额，超时按拒绝处理并给出可见提示；0 表示无限等待",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_category=i18n_text(zh_CN="沙盒配置", en_US="Sandbox Configuration"),
            i18n_title=i18n_text(zh_CN="单实例沙盒排队超时", en_US="Per-Instance Sandbox Queue Timeout"),
            i18n_description=i18n_text(
                zh_CN="一次执行最多等待多久名额，超时按拒绝处理并给出可见提示；0 表示无限等待",
                en_US="How long one execution may wait for a slot; on timeout it is rejected with a visible notice. 0 means wait forever",
            ),
        ).model_dump(),
    )
    SANDBOX_INSTANCE_REJECT_NOTICE_COOLDOWN_SECONDS: float = Field(
        default=300.0,
        title="并发拒绝提示冷却",
        description="同一实例两次「排队已满」聊天提示之间的最小秒数，避免过载时把提示本身刷成新的洪水；0 表示每次都提示",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="沙盒配置", en_US="Sandbox Configuration"),
            i18n_title=i18n_text(zh_CN="并发拒绝提示冷却", en_US="Concurrency Reject Notice Cooldown"),
            i18n_description=i18n_text(
                zh_CN="同一实例两次「排队已满」聊天提示之间的最小秒数，避免过载时把提示本身刷成新的洪水；0 表示每次都提示",
                en_US="Minimum seconds between two 'queue full' chat notices for the same instance, so the notice itself does not become a new flood. 0 notifies every time",
            ),
        ).model_dump(),
    )
    # 默认值首次启动就会落盘；之后部署端口变了，由启动钩子里的
    # `services/sandbox/network_guard.reconcile_sandbox_chat_api_url` 纠正（它按这个形态逐字匹配，改形态要同步那边）
    SANDBOX_CHAT_API_URL: str = Field(
        default=f"http://host.docker.internal:{OsEnv.EXPOSE_PORT}/api",
        title="沙盒访问 Nekro API 地址",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="沙盒配置",
                en_US="Sandbox Configuration",
            ),
            i18n_title=i18n_text(
                zh_CN="沙盒访问 Nekro API 地址",
                en_US="Sandbox Nekro API URL",
            ),
        ).model_dump(),
    )
    # 默认取 best_effort 而非 strict 是经过权衡的，不是随手定的：
    # 改造前沙盒网络本来就是全开放的，best_effort 相对现状是净改善，且不会让任何现有部署
    # 一升级就因为「宿主没有 iptables 权限 / 用的是 nftables / 宿主不是 Linux」而整个沙盒功能瘫痪。
    # 想要强保证的部署应当显式设为 strict——那是一个需要运维确认宿主环境后才做的决定。
    SANDBOX_NETWORK_ISOLATION: Literal["best_effort", "strict", "off"] = Field(
        default="best_effort",
        title="沙盒网络隔离",
        description=(
            "沙盒容器执行的是模型生成的代码，默认可以直连宿主内网。开启后沙盒会被放到专用网络上，"
            "并由宿主的包过滤规则切断到 RFC1918 私网、链路本地（含云厂商元数据服务 169.254.169.254）"
            "与回环地址的访问，公网出网与 NA 自身的回调端点不受影响。"
            "best_effort：装不上规则时告警并继续；strict：装不上则拒绝启动沙盒；off：完全不介入。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="沙盒配置",
                en_US="Sandbox Configuration",
            ),
            i18n_title=i18n_text(
                zh_CN="沙盒网络隔离",
                en_US="Sandbox Network Isolation",
            ),
            i18n_description=i18n_text(
                zh_CN=(
                    "沙盒容器执行的是模型生成的代码，默认可以直连宿主内网。开启后沙盒会被放到专用网络上，"
                    "并由宿主的包过滤规则切断到 RFC1918 私网、链路本地（含云厂商元数据服务 169.254.169.254）"
                    "与回环地址的访问，公网出网与 NA 自身的回调端点不受影响。"
                    "best_effort：装不上规则时告警并继续；strict：装不上则拒绝启动沙盒；off：完全不介入。"
                ),
                en_US=(
                    "Sandbox containers execute model-generated code and can reach the host's internal network "
                    "by default. When enabled, sandboxes run on a dedicated network and host packet filter rules "
                    "cut off access to RFC1918 private ranges, link-local (including the cloud metadata service at "
                    "169.254.169.254) and loopback, while public egress and NA's own callback endpoint keep working. "
                    "best_effort: warn and continue if rules cannot be installed; strict: refuse to start sandboxes; "
                    "off: do not interfere at all."
                ),
            ),
        ).model_dump(),
    )
    SANDBOX_ONEBOT_SERVER_MOUNT_DIR: str = Field(
        default="/app/nekro_agent_data",
        title="协议端挂载 NA 数据目录",
        description="该目录用于 NA 向 OneBot 协议端上传资源文件时，指定文件访问的路径使用，请确保协议端能通过该目录访问到 NA 的应用数据，如果协议端运行在 Docker 容器中则需要将此目录挂载到容器中对应位置",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="沙盒配置",
                en_US="Sandbox Configuration",
            ),
            i18n_title=i18n_text(
                zh_CN="协议端挂载 NA 数据目录",
                en_US="OneBot Server NA Data Mount Directory",
            ),
            i18n_description=i18n_text(
                zh_CN="NA 向 OneBot 协议端上传资源文件时使用的路径，确保协议端能访问",
                en_US="Path for NA to upload resources to OneBot server, ensure server can access",
            ),
        ).model_dump(),
    )

    """CC Workspace 配置"""
    CC_SANDBOX_IMAGE: str = Field(
        default="kromiose/nekro-cc-sandbox",
        title="CC 沙盒镜像名称",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="沙盒配置",
                en_US="Sandbox Configuration",
            ),
            i18n_title=i18n_text(zh_CN="CC 沙盒镜像名称", en_US="CC Sandbox Image Name"),
        ).model_dump(),
    )
    CC_SANDBOX_IMAGE_TAG: str = Field(
        default="latest",
        title="CC 沙盒镜像标签",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="沙盒配置",
                en_US="Sandbox Configuration",
            ),
            i18n_title=i18n_text(zh_CN="CC 沙盒镜像标签", en_US="CC Sandbox Image Tag"),
        ).model_dump(),
    )
    CC_SANDBOX_INTERNAL_PORT: int = Field(
        default=7021,
        title="CC 沙盒容器内部端口",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="沙盒配置",
                en_US="Sandbox Configuration",
            ),
            i18n_title=i18n_text(zh_CN="CC 沙盒容器内部端口", en_US="CC Sandbox Internal Port"),
        ).model_dump(),
    )
    CC_SANDBOX_PORT_RANGE_START: int = Field(
        default=40000,
        title="CC 沙盒宿主机端口段起始",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="沙盒配置",
                en_US="Sandbox Configuration",
            ),
            i18n_title=i18n_text(zh_CN="CC 沙盒端口段起始", en_US="CC Sandbox Port Range Start"),
        ).model_dump(),
    )
    CC_SANDBOX_PORT_RANGE_END: int = Field(
        default=49999,
        title="CC 沙盒宿主机端口段结束",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="沙盒配置",
                en_US="Sandbox Configuration",
            ),
            i18n_title=i18n_text(zh_CN="CC 沙盒端口段结束", en_US="CC Sandbox Port Range End"),
        ).model_dump(),
    )
    # 与 SANDBOX_NETWORK_ISOLATION 分开而不是复用同一个开关，是因为两者的正当出网需求不同：
    # 沙盒是一次性代码执行环境，工作区是长期开发环境，更可能被部署方指向内网的 git 服务器或
    # 私有制品库——那正是本防护会切断的地址。绑在一个开关上会逼运维为了让工作区连内网 registry
    # 而把沙盒的防护一起关掉。默认同样取 best_effort，理由见 SANDBOX_NETWORK_ISOLATION。
    CC_WORKSPACE_NETWORK_ISOLATION: Literal["best_effort", "strict", "off"] = Field(
        default="best_effort",
        title="CC 工作区网络隔离",
        description=(
            "CC 工作区容器执行的同样是模型生成的代码，且默认与 NA 及其数据库、向量库、协议端共处一个网络。"
            "开启后工作区会被放到专用网络上（NA 自身一并接入以保持容器名寻址可用），"
            "并由宿主的包过滤规则切断到 RFC1918 私网、链路本地（含云厂商元数据服务 169.254.169.254）"
            "与宿主自身的访问，公网出网与 DNS 不受影响。"
            "best_effort：装不上规则时告警并继续；strict：装不上则拒绝启动工作区容器；off：完全不介入。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="沙盒配置",
                en_US="Sandbox Configuration",
            ),
            i18n_title=i18n_text(
                zh_CN="CC 工作区网络隔离",
                en_US="CC Workspace Network Isolation",
            ),
            i18n_description=i18n_text(
                zh_CN=(
                    "CC 工作区容器执行的同样是模型生成的代码，且默认与 NA 及其数据库、向量库、协议端共处一个网络。"
                    "开启后工作区会被放到专用网络上（NA 自身一并接入以保持容器名寻址可用），"
                    "并由宿主的包过滤规则切断到 RFC1918 私网、链路本地（含云厂商元数据服务 169.254.169.254）"
                    "与宿主自身的访问，公网出网与 DNS 不受影响。"
                    "best_effort：装不上规则时告警并继续；strict：装不上则拒绝启动工作区容器；off：完全不介入。"
                ),
                en_US=(
                    "CC workspace containers also execute model-generated code, and by default share a network with "
                    "NA and its database, vector store and protocol side-cars. When enabled, workspaces run on a "
                    "dedicated network (NA joins it too so container-name addressing keeps working) and host packet "
                    "filter rules cut off access to RFC1918 private ranges, link-local (including the cloud metadata "
                    "service at 169.254.169.254) and the host itself, while public egress and DNS keep working. "
                    "best_effort: warn and continue if rules cannot be installed; strict: refuse to start workspace "
                    "containers; off: do not interfere at all."
                ),
            ),
        ).model_dump(),
    )
    # 沙盒守卫与工作区守卫共用这一项：它补的是两者同一个覆盖不到的角落——云上 1:1 NAT 拓扑里
    # 宿主对外的公网 IP 并不落在宿主任何本地网卡上，容器发往它的包会绕经云网关发夹回来，
    # 既不匹配 INPUT 上按沙盒/工作区网桥收窄的默认拒绝，也不匹配转发链上的私有段黑名单。
    # 之所以要运维显式声明而不是自动探测：宿主本机拿不到自己的公网 IP，只能问外部回显服务，
    # 而回显给出的是出向 IP、未必等于入向 IP，还可能有多个、可能随时被重新绑定——
    # 猜错就等于往一台无关的互联网主机下 DROP，比缺口本身更糟。默认留空即维持现有行为。
    NETWORK_ISOLATION_EXTRA_BLOCKED_CIDRS: List[str] = Field(
        default=[],
        title="容器隔离额外切断的目的地",
        description=(
            "沙盒与 CC 工作区容器额外要切断的目的地址/网段（IPv4 与 IPv6 均可，单个地址会按 /32 或 /128 处理）。"
            "典型用途是填入宿主对外的公网 IP：云上 1:1 NAT 部署里该地址不在宿主本机网卡上，"
            "容器访问它的包会绕经云网关发夹回来，现有规则匹配不到。留空则不追加任何规则。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="沙盒配置",
                en_US="Sandbox Configuration",
            ),
            sub_item_name="地址或网段",
            i18n_title=i18n_text(
                zh_CN="容器隔离额外切断的目的地",
                en_US="Extra Blocked Destinations For Container Isolation",
            ),
            i18n_description=i18n_text(
                zh_CN=(
                    "沙盒与 CC 工作区容器额外要切断的目的地址/网段（IPv4 与 IPv6 均可，"
                    "单个地址会按 /32 或 /128 处理）。典型用途是填入宿主对外的公网 IP："
                    "云上 1:1 NAT 部署里该地址不在宿主本机网卡上，容器访问它的包会绕经云网关发夹回来，"
                    "现有规则匹配不到。留空则不追加任何规则。"
                ),
                en_US=(
                    "Extra destination addresses/CIDRs to cut off for sandbox and CC workspace containers "
                    "(IPv4 and IPv6; a bare address is treated as /32 or /128). The typical use is the host's "
                    "own public IP: under cloud 1:1 NAT that address lives on no local interface, so container "
                    "traffic to it hairpins through the cloud gateway and matches none of the existing rules. "
                    "Leave empty to add no extra rules."
                ),
            ),
        ).model_dump(),
    )
    CC_SANDBOX_STARTUP_TIMEOUT: int = Field(
        default=120,
        title="CC 沙盒启动超时（秒）",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="沙盒配置",
                en_US="Sandbox Configuration",
            ),
            i18n_title=i18n_text(zh_CN="CC 沙盒启动超时（秒）", en_US="CC Sandbox Startup Timeout (s)"),
        ).model_dump(),
    )

    """邮件通知配置"""
    MAIL_ENABLED: bool = Field(
        default=False,
        title="启用运行状态邮件通知 (已弃用)",
        description="已迁移到 Email 适配器配置中，旧值仍会作为兼容回退保留",
        json_schema_extra=ExtraField(
            is_hidden=True,
            i18n_category=i18n_text(
                zh_CN="邮件通知",
                en_US="Email Notification",
            ),
            i18n_title=i18n_text(
                zh_CN="启用运行状态邮件通知 (已弃用)",
                en_US="Enable Runtime Status Email Notification (Deprecated)",
            ),
            i18n_description=i18n_text(
                zh_CN="已迁移到 Email 适配器配置中，旧值仍会作为兼容回退保留",
                en_US="Migrated to the Email adapter settings. Old values are still kept as a backward-compatible fallback.",
            ),
        ).model_dump(),
    )
    MAIL_USERNAME: str = Field(
        default="",
        title="邮件通知账号 (已弃用)",
        json_schema_extra=ExtraField(
            is_hidden=True,
            is_secret=True,
            i18n_category=i18n_text(
                zh_CN="邮件通知",
                en_US="Email Notification",
            ),
            i18n_title=i18n_text(
                zh_CN="邮件通知账号 (已弃用)",
                en_US="Email Notification Account (Deprecated)",
            ),
            i18n_description=i18n_text(
                zh_CN="已迁移到 Email 适配器配置中，旧值仍会作为兼容回退保留",
                en_US="Migrated to the Email adapter settings. Old values are still kept as a backward-compatible fallback.",
            ),
        ).model_dump(),
        description="已迁移到 Email 适配器配置中，旧值仍会作为兼容回退保留",
    )
    MAIL_PASSWORD: str = Field(
        default="",
        title="邮件通知密码/授权码 (已弃用)",
        json_schema_extra=ExtraField(
            is_hidden=True,
            is_secret=True,
            i18n_category=i18n_text(
                zh_CN="邮件通知",
                en_US="Email Notification",
            ),
            i18n_title=i18n_text(
                zh_CN="邮件通知密码/授权码 (已弃用)",
                en_US="Email Password/Authorization Code (Deprecated)",
            ),
            i18n_description=i18n_text(
                zh_CN="已迁移到 Email 适配器配置中，旧值仍会作为兼容回退保留",
                en_US="Migrated to the Email adapter settings. Old values are still kept as a backward-compatible fallback.",
            ),
        ).model_dump(),
        description="已迁移到 Email 适配器配置中，旧值仍会作为兼容回退保留",
    )
    MAIL_TARGET: List[str] = Field(
        default=[],
        title="邮件通知目标 (已弃用)",
        json_schema_extra=ExtraField(
            is_hidden=True,
            i18n_category=i18n_text(
                zh_CN="邮件通知",
                en_US="Email Notification",
            ),
            sub_item_name="目标邮箱",
            i18n_title=i18n_text(
                zh_CN="邮件通知目标 (已弃用)",
                en_US="Email Notification Targets (Deprecated)",
            ),
            i18n_description=i18n_text(
                zh_CN="已迁移到 Email 适配器配置中，旧值仍会作为兼容回退保留",
                en_US="Migrated to the Email adapter settings. Old values are still kept as a backward-compatible fallback.",
            ),
        ).model_dump(),
    )
    MAIL_HOSTNAME: str = Field(
        default="smtp.qq.com",
        title="邮件通知 SMTP 服务器 (已弃用)",
        description="已迁移到 Email 适配器配置中，旧值仍会作为兼容回退保留",
        json_schema_extra=ExtraField(
            is_hidden=True,
            i18n_category=i18n_text(
                zh_CN="邮件通知",
                en_US="Email Notification",
            ),
            i18n_title=i18n_text(
                zh_CN="邮件通知 SMTP 服务器 (已弃用)",
                en_US="Email SMTP Server (Deprecated)",
            ),
            i18n_description=i18n_text(
                zh_CN="已迁移到 Email 适配器配置中，旧值仍会作为兼容回退保留",
                en_US="Migrated to the Email adapter settings. Old values are still kept as a backward-compatible fallback.",
            ),
        ).model_dump(),
    )
    MAIL_PORT: int = Field(
        default=587,
        title="邮件通知 SMTP 端口 (已弃用)",
        description="已迁移到 Email 适配器配置中，旧值仍会作为兼容回退保留",
        json_schema_extra=ExtraField(
            is_hidden=True,
            i18n_category=i18n_text(
                zh_CN="邮件通知",
                en_US="Email Notification",
            ),
            i18n_title=i18n_text(
                zh_CN="邮件通知 SMTP 端口 (已弃用)",
                en_US="Email SMTP Port (Deprecated)",
            ),
            i18n_description=i18n_text(
                zh_CN="已迁移到 Email 适配器配置中，旧值仍会作为兼容回退保留",
                en_US="Migrated to the Email adapter settings. Old values are still kept as a backward-compatible fallback.",
            ),
        ).model_dump(),
    )
    MAIL_STARTTLS: bool = Field(
        default=True,
        title="邮件通知启用 TLS 加密 (已弃用)",
        json_schema_extra=ExtraField(
            is_hidden=True,
            i18n_category=i18n_text(
                zh_CN="邮件通知",
                en_US="Email Notification",
            ),
            i18n_title=i18n_text(
                zh_CN="邮件通知启用 TLS 加密 (已弃用)",
                en_US="Enable TLS Encryption for Email (Deprecated)",
            ),
            i18n_description=i18n_text(
                zh_CN="已迁移到 Email 适配器配置中，旧值仍会作为兼容回退保留",
                en_US="Migrated to the Email adapter settings. Old values are still kept as a backward-compatible fallback.",
            ),
        ).model_dump(),
    )

    """实例监控配置"""
    INSTANCE_HEALTH_CHECK_INTERVAL_SECONDS: int = Field(
        default=60,
        title="实例探活间隔（秒）",
        description="后台主动探活循环的执行周期，用于发现协议端 WS 连接尚存但对端已失效的假在线",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="实例监控",
                en_US="Instance Monitoring",
            ),
            i18n_title=i18n_text(
                zh_CN="实例探活间隔（秒）",
                en_US="Instance Health Check Interval (s)",
            ),
            i18n_description=i18n_text(
                zh_CN="后台主动探活循环的执行周期，用于发现协议端 WS 连接尚存但对端已失效的假在线",
                en_US="Interval of the background probe loop, used to detect stale connections where the socket is alive but the peer is dead.",
            ),
        ).model_dump(),
    )
    INSTANCE_OFFLINE_ALERT_DELAY_SECONDS: int = Field(
        default=60,
        title="掉线告警延迟（秒）",
        description="实例需连续离线超过该时长才会真正发出告警，用于抑制网络抖动造成的告警刷屏。按实例生效：频道层的覆盖不参与告警解析",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_category=i18n_text(
                zh_CN="实例监控",
                en_US="Instance Monitoring",
            ),
            i18n_title=i18n_text(
                zh_CN="掉线告警延迟（秒）",
                en_US="Offline Alert Delay (s)",
            ),
            i18n_description=i18n_text(
                zh_CN="实例需连续离线超过该时长才会真正发出告警，用于抑制网络抖动造成的告警刷屏。按实例生效：频道层的覆盖不参与告警解析",
                en_US="An instance must stay offline for at least this long before an alert is sent, suppressing alert storms caused by network jitter. Resolved per instance: a channel-layer override does not affect alerting.",
            ),
        ).model_dump(),
    )
    INSTANCE_ALERT_WEBHOOK_URL: str = Field(
        default="",
        title="实例告警 Webhook 地址",
        description="留空表示不启用。告警会以 JSON 形式 POST 到该地址；仅允许 http/https 且必须指向公网地址。按实例生效：频道层的覆盖不参与告警解析",
        json_schema_extra=ExtraField(
            # 按实例覆盖：定制版客户的 Bot 掉线，告警要能发到**他自己**的收件端，
            # 而不是只落到平台方那一个邮箱。
            #
            # 三个标记缺一不可：
            # - `is_secret`：Slack / 飞书 / 钉钉的 webhook **URL 本身就是凭据**。不标的话
            #   租户打开自己实例的作用域配置页，`_with_inherited_override_values` 会把平台方
            #   全局那条地址当作"继承值"原样显示出来。
            # - `scope_owned_secret`：标了 secret 之后 `_assert_tenant_may_write` 会连租户写
            #   **自己实例层**都一起拒掉，功能当场死掉。这一项的凭据归属跟着作用域走：
            #   写在自己实例上的地址就是自己的（按归属分支，不能一刀切掩码）。
            # - `is_outbound_url`：租户填的地址等于让 NA 代为发起出站请求，写入时强制公网。
            overridable=True,
            is_secret=True,
            scope_owned_secret=True,
            is_outbound_url=True,
            placeholder="https://example.com/webhook",
            i18n_category=i18n_text(
                zh_CN="实例监控",
                en_US="Instance Monitoring",
            ),
            i18n_title=i18n_text(
                zh_CN="实例告警 Webhook 地址",
                en_US="Instance Alert Webhook URL",
            ),
            i18n_description=i18n_text(
                zh_CN="留空表示不启用。告警会以 JSON 形式 POST 到该地址；仅允许 http/https 且必须指向公网地址。按实例生效：频道层的覆盖不参与告警解析",
                en_US="Leave empty to disable. Alerts are POSTed as JSON to this URL; only http/https targets resolving to public addresses are allowed. Resolved per instance: a channel-layer override does not affect alerting.",
            ),
        ).model_dump(),
    )
    INSTANCE_ALERT_QUIET_HOURS: str = Field(
        default="",
        title="实例告警静默时段",
        description='形如 "23:00-07:00"，该时段内不发送告警通知（跨零点自动识别）。留空表示不启用。按实例生效：频道层的覆盖不参与告警解析',
        json_schema_extra=ExtraField(
            overridable=True,
            placeholder="23:00-07:00",
            i18n_category=i18n_text(
                zh_CN="实例监控",
                en_US="Instance Monitoring",
            ),
            i18n_title=i18n_text(
                zh_CN="实例告警静默时段",
                en_US="Instance Alert Quiet Hours",
            ),
            i18n_description=i18n_text(
                zh_CN='形如 "23:00-07:00"，该时段内不发送告警通知（跨零点自动识别）。留空表示不启用。按实例生效：频道层的覆盖不参与告警解析',
                en_US='Format "23:00-07:00". No alert is sent during this window (overnight ranges are detected automatically). Leave empty to disable. Resolved per instance: a channel-layer override does not affect alerting.',
            ),
        ).model_dump(),
    )

    """运维 Bot 配置

    本组配置**全部不标记 `overridable`**，因此它们不会进入
    `core/overridable_config.py` 生成的覆盖模型，适配器/实例/频道三层都无法覆盖。
    这是有意为之：一旦允许频道级覆盖，租户就能在自己的频道里把运维通道打开，
    等于绕过总后台的权限体系。运维 Bot 只认全局配置。
    """
    OPS_BOT_ENABLED: bool = Field(
        default=False,
        title="启用运维 Bot",
        description="启用后，运维人员可在指定频道里查询总后台状态；写操作仍需回到后台确认",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="运维 Bot", en_US="Ops Bot"),
            i18n_title=i18n_text(zh_CN="启用运维 Bot", en_US="Enable Ops Bot"),
            i18n_description=i18n_text(
                zh_CN="启用后，运维人员可在指定频道里查询总后台状态；写操作仍需回到后台确认",
                en_US="When enabled, operators can query console status from a designated channel. Write operations still require confirmation in the console.",
            ),
        ).model_dump(),
    )
    OPS_BOT_ADAPTER_KEY: str = Field(
        default="onebot_v11",
        title="运维频道所属适配器",
        description="运维频道所在的适配器标识，需与运维实例实际使用的适配器一致",
        json_schema_extra=ExtraField(
            placeholder="onebot_v11",
            i18n_category=i18n_text(zh_CN="运维 Bot", en_US="Ops Bot"),
            i18n_title=i18n_text(zh_CN="运维频道所属适配器", en_US="Ops Channel Adapter"),
            i18n_description=i18n_text(
                zh_CN="运维频道所在的适配器标识，需与运维实例实际使用的适配器一致",
                en_US="Adapter key of the ops channel; must match the adapter the ops instance actually runs on.",
            ),
        ).model_dump(),
    )
    OPS_BOT_INSTANCE_KEY: str = Field(
        default="",
        title="运维实例标识",
        description="承担运维通道的实例标识。它只是通道，不因此获得任何额外权限",
        json_schema_extra=ExtraField(
            placeholder="inst1",
            i18n_category=i18n_text(zh_CN="运维 Bot", en_US="Ops Bot"),
            i18n_title=i18n_text(zh_CN="运维实例标识", en_US="Ops Instance Key"),
            i18n_description=i18n_text(
                zh_CN="承担运维通道的实例标识。它只是通道，不因此获得任何额外权限",
                en_US="Instance that carries the ops channel. It is only a transport and gains no privileges by itself.",
            ),
        ).model_dump(),
    )
    OPS_BOT_CHANNEL_ID: str = Field(
        default="",
        title="运维频道标识",
        description='形如 "group_123456" 或 "private_10001"，只有该频道内的指令会被受理',
        json_schema_extra=ExtraField(
            placeholder="group_123456",
            i18n_category=i18n_text(zh_CN="运维 Bot", en_US="Ops Bot"),
            i18n_title=i18n_text(zh_CN="运维频道标识", en_US="Ops Channel ID"),
            i18n_description=i18n_text(
                zh_CN='形如 "group_123456" 或 "private_10001"，只有该频道内的指令会被受理',
                en_US='Format "group_123456" or "private_10001". Only commands sent in this channel are accepted.',
            ),
        ).model_dump(),
    )
    OPS_BOT_OPERATORS: List[str] = Field(
        default=[],
        title="运维人员白名单",
        description="填写账号（QQ 号）。权限主体是人不是实例：不在名单内的人即使在运维频道里也无权使用",
        json_schema_extra=ExtraField(
            sub_item_name="账号",
            i18n_category=i18n_text(zh_CN="运维 Bot", en_US="Ops Bot"),
            i18n_title=i18n_text(zh_CN="运维人员白名单", en_US="Operator Allowlist"),
            i18n_description=i18n_text(
                zh_CN="填写账号（QQ 号）。权限主体是人不是实例：不在名单内的人即使在运维频道里也无权使用",
                en_US="Account IDs (QQ numbers). The subject of authorization is the person, not the instance: anyone outside this list is rejected even inside the ops channel.",
            ),
        ).model_dump(),
    )
    OPS_BOT_DEEPLINK_TTL_SECONDS: int = Field(
        default=300,
        title="确认深链有效期（秒）",
        description="写操作确认链接的有效期，取值会被夹到 30~3600 秒",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="运维 Bot", en_US="Ops Bot"),
            i18n_title=i18n_text(zh_CN="确认深链有效期（秒）", en_US="Confirmation Link TTL (s)"),
            i18n_description=i18n_text(
                zh_CN="写操作确认链接的有效期，取值会被夹到 30~3600 秒",
                en_US="TTL of the write-operation confirmation link; clamped to 30-3600 seconds.",
            ),
        ).model_dump(),
    )
    OPS_BOT_CONSOLE_BASE_URL: str = Field(
        default="",
        title="总后台访问地址",
        description="用于拼出确认链接，如 https://console.example.com。留空则只给出相对路径",
        json_schema_extra=ExtraField(
            placeholder="https://console.example.com",
            i18n_category=i18n_text(zh_CN="运维 Bot", en_US="Ops Bot"),
            i18n_title=i18n_text(zh_CN="总后台访问地址", en_US="Console Base URL"),
            i18n_description=i18n_text(
                zh_CN="用于拼出确认链接，如 https://console.example.com。留空则只给出相对路径",
                en_US="Used to build the confirmation link, e.g. https://console.example.com. Leave empty to emit a relative path only.",
            ),
        ).model_dump(),
    )
    OPS_BOT_ALERT_ENABLED: bool = Field(
        default=False,
        title="掉线告警推送到运维频道",
        description="启用后，实例掉线告警会作为第三条通道推送到运维频道（邮件 / Webhook 之外）",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="运维 Bot", en_US="Ops Bot"),
            i18n_title=i18n_text(zh_CN="掉线告警推送到运维频道", en_US="Push Alerts to Ops Channel"),
            i18n_description=i18n_text(
                zh_CN="启用后，实例掉线告警会作为第三条通道推送到运维频道（邮件 / Webhook 之外）",
                en_US="When enabled, instance offline alerts are also pushed to the ops channel as a third notification channel besides email and webhook.",
            ),
        ).model_dump(),
    )

    """插件配置"""
    PLUGIN_GENERATE_MODEL_GROUP: str = Field(
        default="default",
        title="插件代码生成模型组",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="模型配置",
                en_US="Model Configuration",
            ),
            ref_model_groups=True,
            model_type="chat",
            i18n_title=i18n_text(
                zh_CN="插件代码生成模型组",
                en_US="Plugin Code Generation Model Group",
            ),
            i18n_description=i18n_text(
                zh_CN="用于生成插件代码的模型组，建议使用上下文长、逻辑推理能力强的模型",
                en_US="Model group for plugin code generation, recommend long-context models with strong reasoning",
            ),
        ).model_dump(),
        description="用于生成插件代码的模型组，建议使用上下文长，逻辑推理能力强的模型",
    )
    PLUGIN_APPLY_MODEL_GROUP: str = Field(
        default="default",
        title="插件代码应用模型组",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="模型配置",
                en_US="Model Configuration",
            ),
            ref_model_groups=True,
            model_type="chat",
            i18n_title=i18n_text(
                zh_CN="插件代码应用模型组",
                en_US="Plugin Code Application Model Group",
            ),
            i18n_description=i18n_text(
                zh_CN="用于应用插件代码的模型组，建议使用上下文长、响应速度快的模型",
                en_US="Model group for plugin code application, recommend long-context and fast models",
            ),
        ).model_dump(),
        description="用于应用插件代码的模型组，建议使用上下文长，响应速度快的模型",
    )
    PLUGIN_UPDATE_USE_PROXY: bool = Field(
        default=False,
        title="更新/克隆插件时使用代理",
        description="是否在克隆或更新插件 Git 仓库时使用系统级默认代理",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="代理配置",
                en_US="Proxy Configuration",
            ),
            i18n_title=i18n_text(
                zh_CN="更新/克隆插件时使用代理",
                en_US="Use Proxy for Plugin Update/Clone",
            ),
            i18n_description=i18n_text(
                zh_CN="是否在克隆或更新插件 Git 仓库时使用系统级默认代理",
                en_US="Whether to use the system default proxy when cloning or updating plugin Git repositories",
            ),
        ).model_dump(),
    )
    DYNAMIC_PLUGIN_INSTALL_USE_PROXY: bool = Field(
        default=False,
        title="动态安装插件依赖时使用代理",
        description="是否在动态安装插件依赖时使用系统级默认代理",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="代理配置",
                en_US="Proxy Configuration",
            ),
            i18n_title=i18n_text(
                zh_CN="动态安装插件依赖时使用代理",
                en_US="Use Proxy for Dynamic Plugin Installation",
            ),
            i18n_description=i18n_text(
                zh_CN="是否在动态安装插件依赖时使用系统级默认代理",
                en_US="Whether to use the system default proxy when dynamically installing plugin dependencies",
            ),
        ).model_dump(),
    )
    DYNAMIC_PLUGIN_INSTALL_MIRROR: Literal[
        "https://pypi.tuna.tsinghua.edu.cn/simple",
        "https://mirrors.aliyun.com/pypi/simple",
        "https://mirrors.cloud.tencent.com/pypi/simple",
        "https://repo.huaweicloud.com/repository/pypi/simple",
        "https://pypi.org/simple",
    ] = Field(
        default="https://pypi.tuna.tsinghua.edu.cn/simple",
        title="动态插件依赖安装镜像源",
        description="动态安装插件依赖时使用的 PyPI 镜像源地址",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="插件配置",
                en_US="Plugin Configuration",
            ),
            i18n_title=i18n_text(
                zh_CN="动态插件依赖安装镜像源",
                en_US="Dynamic Plugin Dependency Installation Mirror",
            ),
            i18n_description=i18n_text(
                zh_CN="动态安装插件依赖时使用的 PyPI 镜像源地址",
                en_US="PyPI mirror for dynamically installing plugin dependencies",
            ),
        ).model_dump(),
    )

    """Postgresql 配置"""
    POSTGRES_HOST: str = Field(
        default="127.0.0.1",
        title="数据库主机",
        json_schema_extra=ExtraField(is_hidden=True).model_dump(),
    )
    POSTGRES_PORT: int = Field(
        default=5432,
        title="数据库端口",
        json_schema_extra=ExtraField(is_hidden=True).model_dump(),
    )
    POSTGRES_USER: str = Field(
        default="db_username",
        title="数据库用户名",
        json_schema_extra=ExtraField(is_hidden=True).model_dump(),
    )
    POSTGRES_PASSWORD: str = Field(
        default="db_password",
        title="数据库密码",
        json_schema_extra=ExtraField(is_hidden=True, is_secret=True).model_dump(),
    )
    POSTGRES_DATABASE: str = Field(
        default="nekro_agent",
        title="数据库名称",
        json_schema_extra=ExtraField(is_hidden=True).model_dump(),
    )

    """Qdrant 配置"""
    QDRANT_URL: str = Field(
        default="http://127.0.0.1:6333",
        title="Qdrant 地址",
        json_schema_extra=ExtraField(is_hidden=True).model_dump(),
    )
    QDRANT_API_KEY: str = Field(
        default="",
        title="Qdrant API Key",
        json_schema_extra=ExtraField(is_hidden=True, is_secret=True).model_dump(),
    )

    """其他功能"""
    ENABLE_FESTIVAL_REMINDER: bool = Field(
        default=True,
        title="启用节日祝福提醒 (已弃用)",
        description="已弃用，请使用节日祝福插件的配置代替",
        json_schema_extra=ExtraField(
            is_hidden=True,
            i18n_category=i18n_text(
                zh_CN="基础设置",
                en_US="Basic Settings",
            ),
            i18n_title=i18n_text(
                zh_CN="启用节日祝福提醒 (已弃用)",
                en_US="Enable Festival Greeting Reminder (Deprecated)",
            ),
            i18n_description=i18n_text(
                zh_CN="已弃用，请使用节日祝福插件的配置代替",
                en_US="Deprecated, please use the Festival Greeting plugin settings instead",
            ),
        ).model_dump(),
    )
    ENABLE_ADVANCED_COMMAND: bool = Field(
        default=False,
        title="启用高级管理命令",
        description="启用后可以执行包含危险操作的管理员高级命令，请谨慎使用",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="基础设置",
                en_US="Basic Settings",
            ),
            i18n_title=i18n_text(
                zh_CN="启用高级管理命令",
                en_US="Enable Advanced Admin Commands",
            ),
            i18n_description=i18n_text(
                zh_CN="启用后可以执行包含危险操作的管理员高级命令，请谨慎使用",
                en_US="Enable advanced admin commands with dangerous operations, use with caution",
            ),
        ).model_dump(),
    )
    # 上游无条件开启。本分支默认关：机器人归租户，群主 / 群管理员是租户的客户，
    # 不该不经租户同意就能关掉租户的机器人。租户可以在自己实例或频道的覆盖配置里打开
    COMMAND_GROUP_ADMIN_AS_CHANNEL_ADMIN: bool = Field(
        default=False,
        title="群主与群管理员自动成为频道管理员",
        description="开启后，群主和群管理员在该群里自动拥有频道管理员命令权限（reset、stop、na_on、na_off、na_observe 等，只能操作本群）。可按实例或频道单独开启",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_category=i18n_text(
                zh_CN="基础设置",
                en_US="Basic Settings",
            ),
            i18n_title=i18n_text(
                zh_CN="群主与群管理员自动成为频道管理员",
                en_US="Group Owners and Admins Become Channel Admins",
            ),
            i18n_description=i18n_text(
                zh_CN="开启后，群主和群管理员在该群里自动拥有频道管理员命令权限（reset、stop、na_on、na_off、na_observe 等，只能操作本群）。可按实例或频道单独开启",
                en_US="When enabled, group owners and admins automatically get channel admin command permission in that group (reset, stop, na_on, na_off, na_observe and similar, limited to that group). Can be enabled per instance or channel",
            ),
        ).model_dump(),
    )
    OPENAI_CLIENT_USER_AGENT: str = Field(
        default="nekro-agent",
        title="OpenAI Client User Agent",
        description="OpenAI Client User Agent",
        json_schema_extra=ExtraField(is_hidden=True).model_dump(),
    )

    """好感度系统"""
    AFFECTION_ENABLE_SYSTEM: bool = Field(
        default=True,
        title="启用好感度系统",
        description="好感度按 (实例, 用户) 隔离，会影响 Bot 对不同人的态度。关闭后既不注入提示词也不再评估，已有数据保留",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_category=i18n_text(zh_CN="好感度系统", en_US="Affection System"),
            i18n_title=i18n_text(zh_CN="启用好感度系统", en_US="Enable Affection System"),
            i18n_description=i18n_text(
                zh_CN="好感度按 (实例, 用户) 隔离，会影响 Bot 对不同人的态度。关闭后既不注入提示词也不再评估，已有数据保留",
                en_US="Affection is isolated per (instance, user) and shapes how the bot treats each person. Disabling stops both prompt injection and evaluation; existing data is kept",
            ),
        ).model_dump(),
    )
    AFFECTION_ENABLE_PROMPT_INJECTION: bool = Field(
        default=True,
        title="好感度影响提示词",
        description="关闭后仍会记录好感度变化，但不再注入提示词，Bot 的表现不受好感度影响",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_category=i18n_text(zh_CN="好感度系统", en_US="Affection System"),
            i18n_title=i18n_text(zh_CN="好感度影响提示词", en_US="Inject Affection Into Prompt"),
            i18n_description=i18n_text(
                zh_CN="关闭后仍会记录好感度变化，但不再注入提示词，Bot 的表现不受好感度影响",
                en_US="When disabled, affection changes are still recorded but no longer injected into the prompt, so the bot's behaviour is unaffected",
            ),
        ).model_dump(),
    )
    AFFECTION_ENABLE_REPLY_GATE: bool = Field(
        default=True,
        title="好感度影响回复意愿",
        description="好感度低的人，Bot 在非明示触发（随机搭话、关键词命中）下更少搭理；@、私聊、喊名字等直接点名永远照走。提防阶段减半、厌恶阶段降到一成",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_category=i18n_text(zh_CN="好感度系统", en_US="Affection System"),
            i18n_title=i18n_text(zh_CN="好感度影响回复意愿", en_US="Affection Gates Reply Eagerness"),
            i18n_description=i18n_text(
                zh_CN="好感度低的人，Bot 在非明示触发（随机搭话、关键词命中）下更少搭理；@、私聊、喊名字等直接点名永远照走。提防阶段减半、厌恶阶段降到一成",
                en_US="Low-affection users get fewer replies on discretionary triggers (random chat, keyword hits); explicit summons (@, DM, name call) always go through. Wary stage halves it, hostile drops it to 10%",
            ),
        ).model_dump(),
    )
    AFFECTION_MODEL_GROUP: str = Field(
        default="",
        title="好感度评估模型组",
        description="生成好感度状态描述使用的模型组，留空则使用主模型组。该调用不在主对话链路上，可以选用更便宜的模型",
        json_schema_extra=ExtraField(
            overridable=True,
            ref_model_groups=True,
            model_type="chat",
            i18n_category=i18n_text(zh_CN="好感度系统", en_US="Affection System"),
            i18n_title=i18n_text(zh_CN="好感度评估模型组", en_US="Affection Evaluation Model Group"),
            i18n_description=i18n_text(
                zh_CN="生成好感度状态描述使用的模型组，留空则使用主模型组。该调用不在主对话链路上，可以选用更便宜的模型",
                en_US="Model group used to write affection state descriptions. Empty falls back to the primary model group. This call is off the main chat path, so a cheaper model is fine",
            ),
        ).model_dump(),
    )
    AFFECTION_PROMPT_MAX_USERS: int = Field(
        default=5,
        title="好感度注入人数上限",
        description="单轮提示词中最多注入几个人的好感度状态，按最近发言时间取。人多的群里调大会显著增加上下文占用",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_category=i18n_text(zh_CN="好感度系统", en_US="Affection System"),
            i18n_title=i18n_text(zh_CN="好感度注入人数上限", en_US="Max Users Injected"),
            i18n_description=i18n_text(
                zh_CN="单轮提示词中最多注入几个人的好感度状态，按最近发言时间取。人多的群里调大会显著增加上下文占用",
                en_US="Maximum number of people whose affection state is injected per turn, taken by most recent activity. Raising it in busy groups noticeably increases context usage",
            ),
        ).model_dump(),
    )
    AFFECTION_MAX_DELTA_PER_ROUND: int = Field(
        default=5,
        title="单轮好感度变化上限",
        description="单次评估中好感度变化量的绝对值上限，用于防止模型被诱导给出极端数值。系统内另有不可配置的硬上限兜底",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_category=i18n_text(zh_CN="好感度系统", en_US="Affection System"),
            i18n_title=i18n_text(zh_CN="单轮好感度变化上限", en_US="Max Affection Delta Per Round"),
            i18n_description=i18n_text(
                zh_CN="单次评估中好感度变化量的绝对值上限，用于防止模型被诱导给出极端数值。系统内另有不可配置的硬上限兜底",
                en_US="Absolute cap on the affection change produced by a single evaluation, guarding against a model coaxed into extreme values. A non-configurable hard cap backs it up",
            ),
        ).model_dump(),
    )
    AFFECTION_DAILY_GAIN_LIMIT: int = Field(
        default=30,
        title="好感度单日涨幅上限",
        description="同一个人在最近 24 小时内由自动评估累计能涨的上限，按流水表滚动统计。单轮上限拦不住每隔几分钟刷一轮，这一条才拦得住；设为 0 表示不限制",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_category=i18n_text(zh_CN="好感度系统", en_US="Affection System"),
            i18n_title=i18n_text(zh_CN="好感度单日涨幅上限", en_US="Daily Affection Gain Limit"),
            i18n_description=i18n_text(
                zh_CN="同一个人在最近 24 小时内由自动评估累计能涨的上限，按流水表滚动统计。单轮上限拦不住每隔几分钟刷一轮，这一条才拦得住；设为 0 表示不限制",
                en_US="Cap on how much affection one person can gain from automatic evaluation within a rolling 24 hours, computed from the event ledger. The per-round cap cannot stop someone triggering an evaluation every few minutes; this one can. 0 disables the limit",
            ),
        ).model_dump(),
    )
    AFFECTION_DAILY_DROP_LIMIT: int = Field(
        default=45,
        title="好感度单日跌幅上限",
        description="同一个人在最近 24 小时内由自动评估累计能跌的上限（绝对值）。默认比涨幅上限宽——信任崩塌本就比建立快，但仍要有底，免得一次刷屏辱骂把老朋友一天打到谷底；设为 0 表示不限制",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_category=i18n_text(zh_CN="好感度系统", en_US="Affection System"),
            i18n_title=i18n_text(zh_CN="好感度单日跌幅上限", en_US="Daily Affection Drop Limit"),
            i18n_description=i18n_text(
                zh_CN="同一个人在最近 24 小时内由自动评估累计能跌的上限（绝对值）。默认比涨幅上限宽——信任崩塌本就比建立快，但仍要有底，免得一次刷屏辱骂把老朋友一天打到谷底；设为 0 表示不限制",
                en_US="Cap on how much affection one person can lose from automatic evaluation within a rolling 24 hours (absolute value). Deliberately looser than the gain limit — trust collapses faster than it builds — but still bounded, so a single abusive spree cannot bottom out an old friend in a day. 0 disables the limit",
            ),
        ).model_dump(),
    )
    AFFECTION_WEEKLY_GAIN_LIMIT: int = Field(
        default=100,
        title="好感度单周涨幅上限",
        description="同一个人在最近 7 天内由自动评估累计能涨的上限，按流水表滚动统计。它决定“从亲近走到交心”最快要花多久；设为 0 表示不限制",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_category=i18n_text(zh_CN="好感度系统", en_US="Affection System"),
            i18n_title=i18n_text(zh_CN="好感度单周涨幅上限", en_US="Weekly Affection Gain Limit"),
            i18n_description=i18n_text(
                zh_CN="同一个人在最近 7 天内由自动评估累计能涨的上限，按流水表滚动统计。它决定“从亲近走到交心”最快要花多久；设为 0 表示不限制",
                en_US="Cap on how much affection one person can gain from automatic evaluation within a rolling 7 days, computed from the event ledger. It sets the fastest possible path from 'close' to 'trusted'. 0 disables the limit",
            ),
        ).model_dump(),
    )
    AFFECTION_ENABLE_DIMINISHING_RETURNS: bool = Field(
        default=True,
        title="好感度递减收益",
        description="分数越高，同样一次互动带来的涨幅越小：陌生到眼熟几乎全速，亲近到交心要慢慢磨。关闭后涨幅只受单轮上限与窗口额度约束，关系会显得升得很快",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_category=i18n_text(zh_CN="好感度系统", en_US="Affection System"),
            i18n_title=i18n_text(zh_CN="好感度递减收益", en_US="Diminishing Affection Returns"),
            i18n_description=i18n_text(
                zh_CN="分数越高，同样一次互动带来的涨幅越小：陌生到眼熟几乎全速，亲近到交心要慢慢磨。关闭后涨幅只受单轮上限与窗口额度约束，关系会显得升得很快",
                en_US="The higher the score, the less the same interaction moves it: near full speed from stranger to acquainted, a slow grind from close to trusted. Disabling leaves only the per-round cap and window budget, making relationships escalate noticeably faster",
            ),
        ).model_dump(),
    )
    AFFECTION_UPDATE_MIN_INTERVAL_SECONDS: int = Field(
        default=180,
        title="好感度评估最小间隔",
        description="同一频道两次好感度评估之间的最小秒数。评估会调用一次 LLM，间隔太小会在活跃群里明显增加开销",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_category=i18n_text(zh_CN="好感度系统", en_US="Affection System"),
            i18n_title=i18n_text(zh_CN="好感度评估最小间隔", en_US="Min Affection Evaluation Interval"),
            i18n_description=i18n_text(
                zh_CN="同一频道两次好感度评估之间的最小秒数。评估会调用一次 LLM，间隔太小会在活跃群里明显增加开销",
                en_US="Minimum seconds between two affection evaluations in the same channel. Each evaluation costs one LLM call, so a short interval is expensive in busy groups",
            ),
        ).model_dump(),
    )
    AFFECTION_CACHE_TTL_SECONDS: int = Field(
        default=60,
        title="好感度缓存有效期",
        description="好感度快照的进程内缓存秒数。每条消息都要读好感度，缓存过短会让主链路频繁打库；设为 0 表示不缓存",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="好感度系统", en_US="Affection System"),
            i18n_title=i18n_text(zh_CN="好感度缓存有效期", en_US="Affection Cache TTL"),
            i18n_description=i18n_text(
                zh_CN="好感度快照的进程内缓存秒数。每条消息都要读好感度，缓存过短会让主链路频繁打库；设为 0 表示不缓存",
                en_US="In-process cache lifetime for affection snapshots. Affection is read on every message, so a short TTL means frequent database hits on the main path; 0 disables caching",
            ),
        ).model_dump(),
    )

    """插件配置"""
    PLUGIN_ENABLED: List[str] = Field(
        default=["KroMiose.basic", "KroMiose.plugin_activation"],
        title="启用插件",
        description="启用插件 key 列表",
        json_schema_extra=ExtraField(is_hidden=True).model_dump(),
    )
    PLUGIN_ACTIVATION_STRATEGIES: Dict[str, Literal["auto", "allow_sleep", "forbid_sleep"]] = Field(
        default={},
        title="插件激活策略覆盖",
        description="按插件 module_name 覆盖插件提示词激活策略",
        json_schema_extra=ExtraField(is_hidden=True).model_dump(),
    )
    PLUGIN_CALL_PRIORITIES: Dict[str, Literal["high", "medium", "low"]] = Field(
        default={},
        title="插件调用优先级覆盖",
        description="按插件 module_name 覆盖大模型在同类能力间的调用优先级",
        json_schema_extra=ExtraField(is_hidden=True).model_dump(),
    )

    # 插件故障隔离：本组配置**有意不标 overridable**。
    # 一个进程内每个插件只加载一份、被所有实例共用，隔离状态因此天然是进程级的；
    # 若允许按实例覆盖，等于让某一个实例的阈值决定其余所有实例还能不能用这个插件。
    # 它们仍属于四层配置体系的全局层，在系统配置里可读写。
    PLUGIN_CALL_TIMEOUT_SECONDS: float = Field(
        default=30.0,
        title="插件回调超时",
        description="消息 / 提示词等每轮都会走的插件回调的超时秒数，超时按失败处理并跳过本次。插件正当地调 LLM、下载文件都在这个量级以内，设为 0 表示不限时（不推荐：卡住的回调会无限挂起该条链路）",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="插件故障隔离", en_US="Plugin Fault Isolation"),
            i18n_title=i18n_text(zh_CN="插件回调超时", en_US="Plugin Callback Timeout"),
            i18n_description=i18n_text(
                zh_CN="消息 / 提示词等每轮都会走的插件回调的超时秒数，超时按失败处理并跳过本次。设为 0 表示不限时（不推荐：卡住的回调会无限挂起该条链路）",
                en_US="Timeout in seconds for per-turn plugin callbacks such as message and prompt hooks. A timeout counts as a failure and the call is skipped. 0 disables the timeout (not recommended: a stuck callback hangs that chain forever)",
            ),
        ).model_dump(),
    )
    PLUGIN_ISOLATION_FAILURE_THRESHOLD: int = Field(
        default=3,
        title="插件自动隔离阈值",
        description="同一插件的受护栏回调连续失败多少次后自动隔离。成功一次即清零，因此偶发失败不会攒够数量。设为 0 表示不自动隔离，只告警",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="插件故障隔离", en_US="Plugin Fault Isolation"),
            i18n_title=i18n_text(zh_CN="插件自动隔离阈值", en_US="Plugin Quarantine Threshold"),
            i18n_description=i18n_text(
                zh_CN="同一插件的受护栏回调连续失败多少次后自动隔离。成功一次即清零，因此偶发失败不会攒够数量。设为 0 表示不自动隔离，只告警",
                en_US="How many consecutive failures of a plugin's guarded callbacks trigger automatic quarantine. One success resets the counter, so sporadic failures never accumulate. 0 disables quarantine and only warns",
            ),
        ).model_dump(),
    )
    PLUGIN_ISOLATION_COOLDOWN_SECONDS: float = Field(
        default=600.0,
        title="插件隔离冷却时长",
        description="自动隔离的持续秒数，到期后自动恢复调用。修好插件并重载或重新启用可立即恢复，不必等满冷却期",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="插件故障隔离", en_US="Plugin Fault Isolation"),
            i18n_title=i18n_text(zh_CN="插件隔离冷却时长", en_US="Plugin Quarantine Cooldown"),
            i18n_description=i18n_text(
                zh_CN="自动隔离的持续秒数，到期后自动恢复调用。修好插件并重载或重新启用可立即恢复，不必等满冷却期",
                en_US="How long an automatic quarantine lasts before calls resume. Reloading or re-enabling a fixed plugin restores it immediately without waiting out the cooldown",
            ),
        ).model_dump(),
    )
    PLUGIN_BLOCKING_WARN_SECONDS: float = Field(
        default=1.0,
        title="插件阻塞告警阈值",
        description="单次插件回调在事件循环线程上消耗的 CPU 时间超过该秒数即告警。只按 CPU 时间计，正当等待 LLM / 网络不会触发。仅告警，不计入失败也不触发隔离；设为 0 关闭检测",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="插件故障隔离", en_US="Plugin Fault Isolation"),
            i18n_title=i18n_text(zh_CN="插件阻塞告警阈值", en_US="Plugin Blocking Warning Threshold"),
            i18n_description=i18n_text(
                zh_CN="单次插件回调在事件循环线程上消耗的 CPU 时间超过该秒数即告警。只按 CPU 时间计，正当等待 LLM / 网络不会触发。仅告警，不计入失败也不触发隔离；设为 0 关闭检测",
                en_US="Warn when a single plugin callback burns more than this many seconds of CPU on the event-loop thread. Measured as CPU time only, so legitimate waits on LLMs or the network never trigger it. Warning only: it never counts as a failure or triggers quarantine. 0 disables the check",
            ),
        ).model_dump(),
    )
    PLUGIN_LIFECYCLE_TIMEOUT_SECONDS: float = Field(
        default=120.0,
        title="插件生命周期超时",
        description="插件初始化 / 清理 / 启停回调的超时秒数。这些回调卡住会分别拖住进程启动、进程关闭和插件开关操作。设为 0 表示不限时",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="插件故障隔离", en_US="Plugin Fault Isolation"),
            i18n_title=i18n_text(zh_CN="插件生命周期超时", en_US="Plugin Lifecycle Timeout"),
            i18n_description=i18n_text(
                zh_CN="插件初始化 / 清理 / 启停回调的超时秒数。这些回调卡住会分别拖住进程启动、进程关闭和插件开关操作。设为 0 表示不限时",
                en_US="Timeout in seconds for plugin init, cleanup and enable/disable callbacks. A stuck callback there stalls process startup, shutdown, or the plugin toggle respectively. 0 disables the timeout",
            ),
        ).model_dump(),
    )

    def get_model_group_info(self, model_name: str) -> ModelConfigGroup:
        try:
            return self.MODEL_GROUPS[model_name]
        except KeyError as e:
            raise KeyError(f"模型组 '{model_name}' 不存在，请确认配置正确") from e

    def require_advanced_command(self) -> bool:
        if not self.ENABLE_ADVANCED_COMMAND:
            raise PermissionError("高级管理命令未启用，请在配置文件中启用")
        return True

    # ==== 模型组 `API_KEY` 的「落盘形态」记账 ====
    #
    # 内存里 `API_KEY` 只有两种可用形态：明文 / 空串；而 YAML 里有三种：明文（首次部署手写）/
    # 占位符 `nkmask:1`（Key 在数据库里）/ 空串（这个组没配 Key）。`load_config` 把占位符归一成
    # 空串（bootstrap 之前的窗口 fail-closed），于是**归一之后「有 Key」与「没配 Key」在内存里
    # 看起来一样**——不记下来的话，导入期那次原样写回就把全部占位符写成了空串（旧实现的缺陷：
    # 每次重启 YAML 里的 `nkmask:1` 都变 `API_KEY: ''`，而且 `import_config_group` 的占位符
    # 告警在生产上永远走不到）。这里记下「加载时 YAML 里是占位符的组名」。
    #
    # 生命周期：加载时记，`bootstrap_model_groups()` 数据库接管之后 `release_...()` 清掉——
    # 从那一刻起落盘形态只由视图里的真值决定（有 Key → 占位符，没 Key → 空串）。
    _placeholder_model_groups: Set[str] = PrivateAttr(default_factory=set)
    # 本次 dump 是否把非空 Key 掩码成占位符；只由 `dump_config` 在调用期间临时设置
    _mask_model_group_secrets: bool = PrivateAttr(default=True)
    # 「库里解不开、YAML 原值尚未被库接管」的组：组名 -> 该组 YAML 里原本的 `API_KEY`（明文或占位符）。
    # 启动时因这类行存在而不落盘（保住 YAML 里可能是唯一副本的明文），但那只保护启动那一趟：之后任何一次别的
    # 配置落盘，内存视图里这些组的 Key 都是 fail-closed 的空串，会把 YAML 明文写成 `''`（#61 残留，A4）。
    # 落盘时对「内存里 Key 仍为空」的这些组原样写回记下的值。生命周期：每次 `bootstrap_model_groups()` 重新记；
    # 视图重建时按真值修剪（见 `prune_held_model_groups`）。
    _held_model_group_yaml_values: Dict[str, str] = PrivateAttr(default_factory=dict)

    def hold_model_group_yaml_values(self, held: Dict[str, str]) -> None:
        """整体替换「落盘时对这些组原样写回 YAML 原值」的记账；传空字典即清空"""
        self._held_model_group_yaml_values = dict(held)

    def held_model_group_names(self) -> FrozenSet[str]:
        return frozenset(self._held_model_group_yaml_values)

    def prune_held_model_groups(self) -> None:
        """视图重建后修剪记账：只留「还在视图里、且 Key 仍为空」的组

        组被删了、或库里的 Key 已可用 / 被后台显式换了新 Key（视图里 Key 非空），库已经接管，
        那份旧的 YAML 原值不该再被钉着写回（否则同名重建的空 Key 组会冒出旧明文）。
        """
        self._held_model_group_yaml_values = {
            name: value
            for name, value in self._held_model_group_yaml_values.items()
            if name in self.MODEL_GROUPS and not self.MODEL_GROUPS[name].API_KEY
        }

    def placeholder_model_group_names(self) -> FrozenSet[str]:
        """加载时 YAML 里 `API_KEY` 是占位符、尚未被数据库接管的组名（快照）"""
        return frozenset(self._placeholder_model_groups)

    def release_model_group_placeholders(self) -> None:
        """数据库已接管模型组：不再按「加载时是占位符」写回

        由 `services/model_group_service.bootstrap_model_groups()` 在导入与告警都做完之后调用。
        此后某个组的 Key 若是空的（比如拿占位符 YAML 配全新库恢复、Key 确实没了），
        YAML 里就如实写空串，而不是继续声称「Key 在数据库里」。
        """
        self._placeholder_model_groups.clear()

    @classmethod
    def load_config(cls, file_path: Optional[Path] = None, auto_register: bool = True):
        """加载配置文件"""
        config = super().load_config(file_path=file_path, auto_register=auto_register)
        # 配置文件里的 `API_KEY` 现在是占位符（密钥只存数据库，#61）：读进来时归一成空串。
        # 真实明文由启动后的 `refresh_runtime_view()` 按数据库重建；归一保证的是
        # 「bootstrap 之前 / 重建之前」这段窗口里，任何读取点拿到的都是 fail-closed 的空串，
        # 而不是一串会被原样发往上游鉴权的占位文本。手写的明文 Key（首次部署）不受影响。
        # 归一的同时**记下是哪些组**（见上面 `_placeholder_model_groups` 的说明）。
        placeholder_groups: Set[str] = set()
        for name, group in config.MODEL_GROUPS.items():
            if is_model_group_api_key_placeholder(group.API_KEY):
                group.API_KEY = ""
                placeholder_groups.add(name)
        config._placeholder_model_groups = placeholder_groups  # noqa: SLF001
        config.load_config_to_env()
        return config

    def _dump_data(self) -> Dict[str, Any]:
        """落盘数据：在**序列化结果**上处理模型组 `API_KEY`，不碰运行中的 `self.MODEL_GROUPS`

        每个组按下表写 `API_KEY`（「掩码」= 本次 dump 带 `mask_model_group_secrets=True`）：

        | 内存里的 Key | 加载时是占位符 | 写成 |
        |---|---|---|
        | 非空 | —— | 掩码 → `nkmask:1`；不掩码 → 原样 |
        | 空，且库里解不开、被 `hold_model_group_yaml_values` 记下 | —— | 记下的 YAML 原值（明文或占位符，A4） |
        | 空 | 是（尚未被数据库接管） | `nkmask:1` |
        | 空 | 否 | 空串（「没配 Key」是可读的合法状态，不是需要遮蔽的秘密） |

        这样「有 Key」与「没配 Key」在文件里永远可区分，重启也不会把占位符退化成空串。
        `model_dump()` 返回的是新建的嵌套字典，改它不会碰到内存里的 `ModelConfigGroup` 对象。
        """
        data = super()._dump_data()
        serialized_groups = data.get("MODEL_GROUPS")
        if not isinstance(serialized_groups, dict):
            return data
        for name, group in self.MODEL_GROUPS.items():
            entry = serialized_groups.get(name)
            if not isinstance(entry, dict):
                continue
            if group.API_KEY:
                if self._mask_model_group_secrets:
                    entry["API_KEY"] = MODEL_GROUP_API_KEY_PLACEHOLDER
            elif name in self._held_model_group_yaml_values:
                # 库里解不开、YAML 原值（明文或占位符）尚未被库接管：原样写回，不抹成空串（A4）
                entry["API_KEY"] = self._held_model_group_yaml_values[name]
            elif name in self._placeholder_model_groups:
                entry["API_KEY"] = MODEL_GROUP_API_KEY_PLACEHOLDER
        return data

    def dump_config(self, file_path: Optional[Path] = None, *, mask_model_group_secrets: bool = True) -> None:
        """落盘配置文件；默认把 `MODEL_GROUPS.*.API_KEY` 写成占位符（#61）

        掩码只发生在**序列化输出**上（`_dump_data` 钩子）：内存里的 `MODEL_GROUPS` 自始至终
        是同一个对象、里头是可用的明文，几十个同步读取点零改动，dump 期间也不存在「运行中的
        视图被临时换成另一份」的窗口（旧实现靠换进去再换回来）。

        只支持 YAML：钩子只作用于 YAML 序列化，允许 `.json` 就等于给明文开了一扇没人看的门。

        Args:
            file_path: 目标路径，缺省用本实例的默认路径
            mask_model_group_secrets: 是否掩码非空 Key。唯一的 False 调用点是模块导入期那次
                初始 dump（见文件底部）：那一刻数据库还没接管模型组，YAML 是手写凭据的
                **唯一载体**，先抹成占位符、随后 bootstrap 之前进程崩溃，首次部署的
                Key 就永久丢了。占位符组不受这个开关影响（它们在 False 时同样原样写回）。
        """
        target_path = file_path or self.get_config_file_path()
        if target_path is not None and target_path.suffix == ".json":
            raise ValueError("CoreConfig 只支持 YAML 落盘：模型组 API_KEY 的掩码只作用于 YAML 序列化")
        previous = self._mask_model_group_secrets
        self._mask_model_group_secrets = mask_model_group_secrets
        try:
            super().dump_config(file_path)
        finally:
            self._mask_model_group_secrets = previous


# 设置配置键和文件路径
CoreConfig.set_config_key("system")
CoreConfig.set_config_file_path(CONFIG_PATH)

try:
    config = CoreConfig.load_config()
    config_schema = config.model_json_schema()
except Exception as e:
    print(f"Nekro Agent 配置文件加载失败: {e} | 请检查配置文件是否符合语法要求")
    print("应用将退出...")
    exit(1)

# 导入期这次 dump 有意**不做掩码**：此刻数据库尚未接管模型组，YAML 还是手写凭据的唯一
# 载体，先抹成占位符、随后 bootstrap 之前崩溃 = 首次部署的 Key 永久丢失。
# 占位符组与空 Key 组在这次 dump 里原样写回（`CoreConfig._dump_data` 的表格）。
# 掩码从 `bootstrap_model_groups()` 完成数据库接管那一刻开始：只要没有解不开的行，它**每次
# 启动都落盘一次**，所以升级后首次启动 YAML 里的明文就被掩码。
# `tests/test_model_group_yaml_persistence.py` 的 `start_process` 照这一句模拟导入期，并有
# AST 用例钉住它——改这一句要同步改那边。
config.dump_config(mask_model_group_secrets=False)
set_system_lang(SupportedLang(config.SYSTEM_LANG))


def save_config():
    """保存配置"""
    global config
    config.dump_config()
    set_system_lang(SupportedLang(config.SYSTEM_LANG))


def reload_config():
    """重新加载配置文件

    读 YAML 得到的新实例**只当字段来源，不注册**（`auto_register=False`）：`ConfigManager["system"]`
    必须始终是下面被就地更新的这个活对象。YAML 里的模型组 `API_KEY` 是占位符（#61），新实例里没有明文；
    若让它顶掉注册表项，`credential_guard.collect_platform_secrets()` 就收不到平台 Key，
    沙盒 RPC 返回值与 cc_relay 的出口脱敏在下次重启之前都不再遮它们。
    `UnifiedConfigService.reload_config` 对 CoreConfig 也走本函数。
    """
    global config

    new_config = CoreConfig.load_config(auto_register=False)
    # 更新配置字段
    for field_name in CoreConfig.model_fields:
        if field_name == "MODEL_GROUPS":
            # 运行时视图归数据库所有，只能由 `refresh_runtime_view()` 重建；配置文件里的
            # `API_KEY` 现在是占位符（#61），照抄会把占位串灌进视图、被当成 Key 发往上游。
            # 手改 YAML 增删模型组的入口是重启（`bootstrap_model_groups()` 导入数据库）。
            continue
        setattr(config, field_name, getattr(new_config, field_name))
    set_system_lang(SupportedLang(config.SYSTEM_LANG))

    # 活对象的字段整体换了，按版本号缓存的频道有效配置（`DBChatChannel._effective_config`）必须作废，
    # 否则 `conf_reload` 显示成功、实际仍用重载前合并出来的旧值。
    # 放在这个共同路径本身：聊天命令与 `UnifiedConfigService.reload_config("system")` 都经过这里，
    # 不靠各调用方各写一遍。延迟导入：`config_resolver` 反过来依赖本模块的 `CoreConfig`。
    from nekro_agent.services.config_resolver import config_resolver

    config_resolver.bump_version()
