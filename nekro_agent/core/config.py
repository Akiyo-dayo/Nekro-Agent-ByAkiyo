from pathlib import Path
from typing import Any, ClassVar, Dict, FrozenSet, List, Literal, Optional, Set, Type

from pydantic import Field, PrivateAttr, field_validator

from nekro_agent.schemas.i18n import SupportedLang, i18n_text, set_system_lang
from nekro_agent.services.tavern.assemble import DEFAULT_CARD_TEMPLATE

from .core_utils import ConfigBase, ExtraField
from .os_env import OsEnv

CONFIG_DIR = Path(OsEnv.DATA_DIR) / "configs"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = Path(OsEnv.DATA_DIR) / "configs" / "nekro-agent.yaml"
CHANNEL_CONFIG_DIR = CONFIG_DIR / "channels"
CHANNEL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

# 模型组上「上下文长度」与「分词系数」的上限：挡的是手误（多打几个 0），不是模型的真实能力
MODEL_GROUP_CONTEXT_LENGTH_MAX = 10_000_000
MODEL_GROUP_TOKEN_FACTOR_MAX = 10.0


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
    PREFILL_MODE: Literal["off", "assistant", "deepseek"] = Field(
        default="off",
        title="预填方式",
        description="只有声明支持预填的服务才设成非 off；DeepSeek 前缀续写要把接口地址设成它的 beta 地址",
    )
    API_FORMAT: Literal["openai", "gemini"] = Field(
        default="openai",
        title="接口格式",
        description=(
            "聊天请求用的接口格式。OpenAI 兼容（默认）适用于绝大多数服务与中转站；"
            "Gemini 原生直接调用 generateContent，可以拿到被拦的具体原因、用生成配置控制思考预算。"
            "选 Gemini 原生时接口地址填到版本号为止，例如 https://generativelanguage.googleapis.com/v1beta；"
            "思考预算、安全阈值等原生参数写在额外请求体的 generationConfig / safetySettings 里。"
            "只对聊天模型生效。"
        ),
    )
    CONTEXT_LENGTH: int = Field(
        default=0,
        ge=0,
        le=MODEL_GROUP_CONTEXT_LENGTH_MAX,
        title="上下文长度（token）",
        description=(
            "这个模型的上下文窗口。世界书每轮预算按它的百分比算；0 表示未填，按 32768 估。"
            "照实填：填大了世界书每轮会注入更多、费用跟着涨"
        ),
    )
    TOKEN_COUNT_FACTOR: float = Field(
        default=0.0,
        ge=0.0,
        le=MODEL_GROUP_TOKEN_FACTOR_MAX,
        title="分词系数",
        description=(
            "这个模型的分词器相对 o200k_base 多数还是少数（例如 1.29 表示同一段文字它数出的 token 多 29%）。"
            "0 表示按模型名自动取（DeepSeek 0.80、Gemini 0.89、Qwen 0.90、Claude 1.29 等）"
        ),
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


# SSH 值守实例名单摘空时留下的占位：空名单的意思是「所有平台方实例」，摘掉最后一个实例不能反而
# 放宽，所以留这一项表示「一个都不给」。它不含冒号，永远匹配不上任何 `适配器:实例`。
SSH_GATEWAY_DUTY_INSTANCES_CLOSED = "!none"


def normalize_duty_instances(value: Any) -> List[str]:
    """规整值守实例名单：去空白、去空项、去重；有真实条目时去掉占位（不让两者并存）"""
    items: List[str] = []
    for raw in value or []:
        item = str(raw).strip()
        if item and item not in items:
            items.append(item)
    if any(item != SSH_GATEWAY_DUTY_INSTANCES_CLOSED for item in items):
        items = [item for item in items if item != SSH_GATEWAY_DUTY_INSTANCES_CLOSED]
    return items


# 角色状态「兼容解析」的出厂档位（私聊默认可用、群里要管理员显式打开）。以后要改成默认关，只改这一处。
STATE_COMPAT_PARSE_DEFAULT: Literal["private", "all", "off"] = "private"


class CoreConfig(ConfigBase):
    """核心配置"""

    # 导入时加载完立刻整份写回（本文件末尾那次 dump）：修正过非法值就先备份原文件，
    # 免得被去掉的原值（以及文件里手写的凭据）随写回一起消失
    _backup_original_on_repair: ClassVar[bool] = True

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
    # 平台方专属：清理对全部租户的聊天图片同时生效，登记在 `routers/config.py` 的
    # `PLATFORM_ONLY_SYSTEM_CONFIG_KEYS` 里（租户读不到也写不了）。不标 `overridable`，
    # 实例 / 频道覆盖层里没有这一项；清理任务只读全局配置（`services/upload_retention.py`）。
    UPLOADS_IMAGE_RETENTION_DAYS: int = Field(
        default=0,
        title="上传图片保留天数",
        description=(
            "聊天上传目录里的图片保留多少天，0 表示不清理（默认）。打开后每天清一次修改时间早于这个天数的图片"
            "（jpg / png / gif / webp 等），视频、文档等其它类型不动，邮件附件不动；"
            "清掉的图片在控制台聊天记录、网页会话里会显示不出来，沙盒里也读不到。只有平台方能改"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="基础设置",
                en_US="Basic Settings",
            ),
            i18n_title=i18n_text(
                zh_CN="上传图片保留天数",
                en_US="Uploaded Image Retention (Days)",
            ),
            i18n_description=i18n_text(
                zh_CN=(
                    "聊天上传目录里的图片保留多少天，0 表示不清理（默认）。打开后每天清一次修改时间早于这个天数的图片"
                    "（jpg / png / gif / webp 等），视频、文档等其它类型不动，邮件附件不动；"
                    "清掉的图片在控制台聊天记录、网页会话里会显示不出来，沙盒里也读不到。只有平台方能改"
                ),
                en_US=(
                    "How many days to keep images in the chat upload directory; 0 disables cleanup (default). "
                    "When enabled, images (jpg / png / gif / webp, etc.) last modified more than this many days ago "
                    "are deleted once a day. Videos, documents and other file types are left alone, and so are email "
                    "attachments. Deleted images no longer show in console chat history or web chat sessions, and the "
                    "sandbox can no longer read them. Platform operators only"
                ),
            ),
        ).model_dump(),
    )
    # 平台方专属总闸，登记在 `routers/config.py` 的 `PLATFORM_ONLY_SYSTEM_CONFIG_KEYS` 里。
    # 关掉之后实例自有邮箱既不入池也不轮询，后台管理页只剩只读：给平台方留一个能一键收回
    # 这项能力的开关，而不必逐个实例去删账户。
    EMAIL_INSTANCE_ACCOUNTS_ENABLED: bool = Field(
        default=True,
        title="实例自有邮箱",
        description=(
            "关掉时实例自有邮箱不轮询、不能用（读写都不给它账户），管理页只读；"
            "平台方提供的邮箱不受影响。只有平台方能改"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="邮件",
                en_US="Email",
            ),
            i18n_title=i18n_text(
                zh_CN="实例自有邮箱",
                en_US="Instance-Owned Mailboxes",
            ),
            i18n_description=i18n_text(
                zh_CN=(
                    "关掉时实例自有邮箱不轮询、不能用（读写都不给它账户），管理页只读；"
                    "平台方提供的邮箱不受影响。只有平台方能改"
                ),
                en_US=(
                    "When disabled, instance-owned mailboxes are not polled and cannot be used (no accounts are "
                    "handed out for reading or sending), and the admin page is read-only. Platform-provided "
                    "mailboxes are unaffected. Platform operators only"
                ),
            ),
        ).model_dump(),
    )
    # 平台方专属：连接数的上限，不是发信上限；登记在 `routers/config.py` 的
    # `PLATFORM_ONLY_SYSTEM_CONFIG_KEYS` 里（租户读不到也写不了），租户按实例看自己的用量。
    # 0 表示一个实例自有邮箱都不许建（等于关掉这个能力）。
    EMAIL_MAX_ACCOUNTS_PER_INSTANCE: int = Field(
        default=3,
        ge=0,
        le=20,
        title="每个实例的邮箱账户上限",
        description=(
            "每个实例最多能填几个自己的邮箱账户（默认 3，0 ~ 20）。这是连接数上限，不是发信上限；"
            "平台方提供的邮箱不算在内。只有平台方能改"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="邮件",
                en_US="Email",
            ),
            i18n_title=i18n_text(
                zh_CN="每个实例的邮箱账户上限",
                en_US="Mailbox Accounts Per Instance",
            ),
            i18n_description=i18n_text(
                zh_CN=(
                    "每个实例最多能填几个自己的邮箱账户（默认 3，0 ~ 20）。这是连接数上限，不是发信上限；"
                    "平台方提供的邮箱不算在内。只有平台方能改"
                ),
                en_US=(
                    "How many mailbox accounts an instance may add (default 3, range 0-20). This caps connections, "
                    "not how many emails can be sent; platform-provided mailboxes are not counted. Platform operators only"
                ),
            ),
        ).model_dump(),
    )
    EMAIL_OUTBOX_INTERVAL_SECONDS: int = Field(
        default=5,
        ge=1,
        le=600,
        title="节日邮件发信间隔（秒）",
        description=(
            "节日邮件队列里同一个发件邮箱两封之间的最小间隔（默认 5 秒，1 ~ 600）。"
            "这是防被收件方当成垃圾邮件的节奏，不是数量上限；不同发件邮箱各走各的。只有平台方能改"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="邮件",
                en_US="Email",
            ),
            i18n_title=i18n_text(
                zh_CN="节日邮件发信间隔（秒）",
                en_US="Festival Mail Interval (Seconds)",
            ),
            i18n_description=i18n_text(
                zh_CN=(
                    "节日邮件队列里同一个发件邮箱两封之间的最小间隔（默认 5 秒，1 ~ 600）。"
                    "这是防被收件方当成垃圾邮件的节奏，不是数量上限；不同发件邮箱各走各的。只有平台方能改"
                ),
                en_US=(
                    "Minimum gap between two festival emails from the same sender mailbox (default 5 seconds, "
                    "range 1-600). This is a pacing setting to avoid being treated as spam, not a volume cap; "
                    "different sender mailboxes are paced independently. Platform operators only"
                ),
            ),
        ).model_dump(),
    )
    EMAIL_FESTIVAL_MAIL_ENABLED: bool = Field(
        default=True,
        title="节日祝福邮件",
        description=(
            "开着时节日到点会给「和本实例开过私聊的用户」与「在群里要过节日邮件的用户」发祝福邮件，"
            "要求邮箱工具插件与节日祝福插件都在实例上启用、本实例有能用的发件邮箱；"
            "关掉时只推群消息，已入队的作废。只有平台方能改"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="邮件",
                en_US="Email",
            ),
            i18n_title=i18n_text(
                zh_CN="节日祝福邮件",
                en_US="Festival Greeting Mail",
            ),
            i18n_description=i18n_text(
                zh_CN=(
                    "开着时节日到点会给「和本实例开过私聊的用户」与「在群里要过节日邮件的用户」发祝福邮件，"
                    "要求邮箱工具插件与节日祝福插件都在实例上启用、本实例有能用的发件邮箱；"
                    "关掉时只推群消息，已入队的作废。只有平台方能改"
                ),
                en_US=(
                    "When enabled, festival greetings are also emailed to users who have had a private chat with "
                    "this instance and to users who asked for them in a group. Both the email utilities plugin and "
                    "the festival greeting plugin must be enabled on the instance, and the instance needs a usable "
                    "sender mailbox. When disabled, only group messages are pushed and queued mail is voided. "
                    "Platform operators only"
                ),
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
    MEMORY_DOMAIN_SCOPE_ENABLED: bool = Field(
        default=False,
        title="启用记忆域分区",
        description="长期记忆改按（域, 人设）分区、不再依赖工作区。关闭时与改造前完全相同。打开后频道还要「启用聊天记忆」生效才沉淀与注入。运行中切换不必重启；有记忆重建正在进行时请等它结束再切换",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="启用记忆域分区", en_US="Enable Memory Domain Partition"),
            i18n_description=i18n_text(
                zh_CN="长期记忆改按（域, 人设）分区、不再依赖工作区。关闭时与改造前完全相同。打开后频道还要「启用聊天记忆」生效才沉淀与注入。运行中切换不必重启；有记忆重建正在进行时请等它结束再切换",
                en_US="Long-term memories are partitioned by (domain, preset) instead of requiring a workspace. When disabled, behavior is identical to before this change. When enabled, a channel also needs \"Enable Chat Memory\" in effect to consolidate and inject memories. It can be switched at runtime without a restart; if a memory rebuild is running, wait for it to finish before switching",
            ),
        ).model_dump(),
    )
    MEMORY_ENABLE_FOR_CHAT: bool = Field(
        default=False,
        title="启用聊天记忆",
        description=(
            "是否沉淀与注入聊天记忆。可在全局、实例、频道三层设置，下层覆盖上层，"
            "最终按每个频道生效的值判断。需同时打开记忆总开关；"
            "只在『记忆按域分区』打开时生效（关着时仍按旧规则：频道绑了工作区才有记忆）。"
            "租户管理员只能关、不能开。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            overridable=True,
            tenant_may_only_lower=True,
            i18n_title=i18n_text(zh_CN="启用聊天记忆", en_US="Enable Chat Memory"),
            i18n_description=i18n_text(
                zh_CN=(
                    "是否沉淀与注入聊天记忆。可在全局、实例、频道三层设置，下层覆盖上层，"
                    "最终按每个频道生效的值判断。需同时打开记忆总开关；"
                    "只在『记忆按域分区』打开时生效（关着时仍按旧规则：频道绑了工作区才有记忆）。"
                    "租户管理员只能关、不能开。"
                ),
                en_US=(
                    "Whether chat memories are consolidated and injected. It can be set at the global, "
                    "instance and channel levels, each overriding the one above, and is decided by the value "
                    "in effect for each channel. The memory master switch must also be on. Takes effect only "
                    "when memory domain partitioning is enabled; while it is off the old rule still applies "
                    "(a channel needs a bound workspace to have memories). Tenant administrators can only "
                    "turn it off, never on."
                ),
            ),
        ).model_dump(),
    )
    MEMORY_RECALL_GROUP_TO_GROUP: Literal["off", "related", "all"] = Field(
        default="off",
        title="群聊记忆跨群召回",
        description=(
            "群聊记忆的跨群召回档位：关＝群里的记忆只在本群；"
            "相关＝只在这条记忆涉及的人都是成员的群里召回；全部＝域内所有群。"
            "只在记忆按域分区打开时生效；全局默认关。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            overridable=True,
            i18n_title=i18n_text(zh_CN="群聊记忆跨群召回", en_US="Group Memory Recall to Groups"),
            i18n_description=i18n_text(
                zh_CN=(
                    "群聊记忆的跨群召回档位：关＝群里的记忆只在本群；"
                    "相关＝只在这条记忆涉及的人都是成员的群里召回；全部＝域内所有群。"
                    "只在记忆按域分区打开时生效；全局默认关。"
                ),
                en_US=(
                    "How group memories are recalled in other groups: off = only in the origin group; "
                    "related = only in groups where every person mentioned by the memory is a member; "
                    "all = every group in the domain. Takes effect only when memory domain partitioning "
                    "is enabled; off by default globally."
                ),
            ),
        ).model_dump(),
    )
    MEMORY_RECALL_GROUP_TO_PRIVATE: Literal["off", "related", "all"] = Field(
        default="off",
        title="群聊记忆进入私聊",
        description=(
            "群聊记忆进入私聊的档位：关＝群记忆不进私聊；"
            "相关＝只在涉及的人本人的私聊里召回；全部＝域内所有私聊。"
            "只在记忆按域分区打开时生效；全局默认关。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            overridable=True,
            i18n_title=i18n_text(zh_CN="群聊记忆进入私聊", en_US="Group Memory Recall to Private Chats"),
            i18n_description=i18n_text(
                zh_CN=(
                    "群聊记忆进入私聊的档位：关＝群记忆不进私聊；"
                    "相关＝只在涉及的人本人的私聊里召回；全部＝域内所有私聊。"
                    "只在记忆按域分区打开时生效；全局默认关。"
                ),
                en_US=(
                    "How group memories are recalled in private chats: off = never; "
                    "related = only in the private chat of a person mentioned by the memory; "
                    "all = every private chat in the domain. Takes effect only when memory domain "
                    "partitioning is enabled; off by default globally."
                ),
            ),
        ).model_dump(),
    )
    MEMORY_RECALL_PRIVATE_TO_GROUP: Literal["off", "related", "all"] = Field(
        default="off",
        title="私聊记忆进入群聊",
        description=(
            "私聊记忆进入群聊的档位：关＝私聊记忆不进群；"
            "相关＝只在私聊对象本人是成员的群里召回；全部＝域内所有群。"
            "私聊记忆永远不会进别人的私聊。只在记忆按域分区打开时生效；全局默认关。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            overridable=True,
            i18n_title=i18n_text(zh_CN="私聊记忆进入群聊", en_US="Private Memory Recall to Groups"),
            i18n_description=i18n_text(
                zh_CN=(
                    "私聊记忆进入群聊的档位：关＝私聊记忆不进群；"
                    "相关＝只在私聊对象本人是成员的群里召回；全部＝域内所有群。"
                    "私聊记忆永远不会进别人的私聊。只在记忆按域分区打开时生效；全局默认关。"
                ),
                en_US=(
                    "How private-chat memories are recalled in groups: off = never; "
                    "related = only in groups where the private-chat counterpart is a member; "
                    "all = every group in the domain. Private memories never enter someone else's "
                    "private chat. Takes effect only when memory domain partitioning is enabled; "
                    "off by default globally."
                ),
            ),
        ).model_dump(),
    )
    MEMORY_MEMBER_ACTIVE_DAYS: int = Field(
        default=30,
        ge=1,
        title="群成员活跃天数",
        description=(
            "「相关」档判定群成员的天数：最近这么多天在这个群里发过言的人算成员。"
            "只在记忆按域分区打开时生效。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="群成员活跃天数", en_US="Active Member Days"),
            i18n_description=i18n_text(
                zh_CN=(
                    "「相关」档判定群成员的天数：最近这么多天在这个群里发过言的人算成员。"
                    "只在记忆按域分区打开时生效。"
                ),
                en_US=(
                    "Number of days used to decide group membership for the related recall mode: "
                    "anyone who spoke in the group within this many days counts as a member. "
                    "Takes effect only when memory domain partitioning is enabled."
                ),
            ),
        ).model_dump(),
    )
    MEMORY_OTHER_BOT_ACCOUNTS: List[str] = Field(
        default=[],
        title="群里其他 Bot 的账号",
        description=(
            "这些账号在群里说的话按「别的 Bot 的台词」处理：不当成任何真人的事实，也不为它们记人物信息。"
            "同一个部署里别的实例的号会自动认出来，不用填；这里填第三方 Bot 的平台账号（QQ 号一类），"
            "可以按频道单独设。"
        ),
        json_schema_extra=ExtraField(
            sub_item_name="账号",
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            overridable=True,
            i18n_title=i18n_text(zh_CN="群里其他 Bot 的账号", en_US="Other bots' accounts"),
            i18n_description=i18n_text(
                zh_CN=(
                    "这些账号在群里说的话按「别的 Bot 的台词」处理：不当成任何真人的事实，也不为它们记人物信息。"
                    "同一个部署里别的实例的号会自动认出来，不用填；这里填第三方 Bot 的平台账号（QQ 号一类），"
                    "可以按频道单独设。"
                ),
                en_US=(
                    "Messages from these accounts in groups are treated as another bot's lines: they are not taken "
                    "as facts about any real person, and no person profile is recorded for them. Accounts of other "
                    "instances in the same deployment are detected automatically and need not be listed; put "
                    "third-party bot platform accounts (such as QQ numbers) here. Can be set per channel."
                ),
            ),
        ).model_dump(),
    )
    MEMORY_CONTEXT_MAX_ITEMS: int = Field(
        default=8,
        ge=1,
        title="记忆上下文最大条数",
        description="记忆系统注入到 Agent 提示词中的最大条数。只在记忆按域分区打开时生效；值越大，Agent 可参考的记忆越多",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            overridable=True,
            i18n_title=i18n_text(zh_CN="记忆上下文最大条数", en_US="Memory Context Max Items"),
            i18n_description=i18n_text(
                zh_CN="记忆系统注入到 Agent 提示词中的最大条数。只在记忆按域分区打开时生效；值越大，Agent 可参考的记忆越多",
                en_US=(
                    "Maximum number of memory items injected into the Agent prompt. Only takes effect when "
                    "memory domain partitioning is enabled; larger values let the Agent reference more memories"
                ),
            ),
        ).model_dump(),
    )
    MEMORY_CONTEXT_MAX_LENGTH: int = Field(
        default=1200,
        title="记忆上下文最大长度",
        description="记忆系统注入到 Agent 提示词中的最大字符长度。值越大，Agent 可参考的记忆越多，但也会占用更多上下文窗口",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            overridable=True,
            i18n_title=i18n_text(zh_CN="记忆上下文最大长度", en_US="Memory Context Max Length"),
            i18n_description=i18n_text(
                zh_CN="记忆系统注入到 Agent 提示词中的最大字符长度。值越大，Agent 可参考的记忆越多，但也会占用更多上下文窗口",
                en_US="Maximum character length of memory context injected into the Agent prompt. Larger values allow more memory context but consume more prompt window",
            ),
        ).model_dump(),
    )
    MEMORY_CHANNEL_SUMMARY_ENABLED: bool = Field(
        default=True,
        title="频道滚动摘要",
        description=(
            "可在全局、实例、频道三层设置，下层覆盖上层。只在记忆按域分区打开、且频道的「启用聊天记忆」"
            "生效时起作用。关闭后不再更新也不再注入频道摘要，已经保存的历史版本保留"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            overridable=True,
            i18n_title=i18n_text(zh_CN="频道滚动摘要", en_US="Channel Rolling Summary"),
            i18n_description=i18n_text(
                zh_CN=(
                    "可在全局、实例、频道三层设置，下层覆盖上层。只在记忆按域分区打开、且频道的「启用聊天记忆」"
                    "生效时起作用。关闭后不再更新也不再注入频道摘要，已经保存的历史版本保留"
                ),
                en_US=(
                    "Can be set at the global, instance and channel levels, each overriding the one above. "
                    "Takes effect only when memory domain partitioning is enabled and \"Enable Chat Memory\" "
                    "is in effect for the channel. When disabled, the channel summary is no longer "
                    "updated or injected; previously saved versions are kept"
                ),
            ),
        ).model_dump(),
    )
    MEMORY_CHANNEL_SUMMARY_MAX_CHARS: int = Field(
        default=400,
        ge=100,
        le=2000,
        title="频道摘要最大长度",
        description="频道滚动摘要的最大字符数，超出部分在写入时截断。只影响摘要，不影响记忆段落",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="频道摘要最大长度", en_US="Channel Summary Max Length"),
            i18n_description=i18n_text(
                zh_CN="频道滚动摘要的最大字符数，超出部分在写入时截断。只影响摘要，不影响记忆段落",
                en_US=(
                    "Maximum character length of the rolling channel summary; longer content is "
                    "truncated when written. Applies only to summaries, not memory paragraphs"
                ),
            ),
        ).model_dump(),
    )
    MEMORY_RECALL_COOLDOWN_ROUNDS: int = Field(
        default=3,
        ge=0,
        title="记忆召回冷却轮数",
        description=(
            "同一段记忆被自动注入后，接下来这么多轮的自动注入里跳过它，第 N+1 轮恢复。"
            "0 表示不冷却。只在记忆按域分区打开时生效；"
            "memory_tools 主动搜索不受冷却限制。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            overridable=True,
            i18n_title=i18n_text(zh_CN="记忆召回冷却轮数", en_US="Memory Recall Cooldown Rounds"),
            i18n_description=i18n_text(
                zh_CN=(
                    "同一段记忆被自动注入后，接下来这么多轮的自动注入里跳过它，第 N+1 轮恢复。"
                    "0 表示不冷却。只在记忆按域分区打开时生效；"
                    "memory_tools 主动搜索不受冷却限制。"
                ),
                en_US=(
                    "After a memory is injected automatically, skip it in automatic injection for this "
                    "many following rounds and include it again from round N+1. 0 disables the cooldown. "
                    "Takes effect only when memory domain partitioning is enabled; explicit searches "
                    "through memory_tools are never cooled down."
                ),
            ),
        ).model_dump(),
    )
    MEMORY_RECALL_COOLDOWN_BYPASS_SIMILARITY: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        title="记忆召回冷却绕过相似度",
        description=(
            "本轮检索相似度达到这个值时视为「直接问到它」，即使它在冷却期内也照常注入。"
            "只在记忆按域分区打开时生效；本项不可覆盖。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(
                zh_CN="记忆召回冷却绕过相似度",
                en_US="Memory Recall Cooldown Bypass Similarity",
            ),
            i18n_description=i18n_text(
                zh_CN=(
                    "本轮检索相似度达到这个值时视为「直接问到它」，即使它在冷却期内也照常注入。"
                    "只在记忆按域分区打开时生效；本项不可覆盖。"
                ),
                en_US=(
                    "When this round's retrieval similarity reaches this value, the memory counts as "
                    "directly asked for and is injected even during its cooldown. Takes effect only when "
                    "memory domain partitioning is enabled; this item is not overridable."
                ),
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
    MEMORY_RECALL_JUDGE_MODEL_GROUP: str = Field(
        default="",
        title="记忆筛选模型组",
        description=(
            "填了以后，每轮自动想起的记忆在放进提示词之前，先让这个模型组对照最近几句对话看一遍，"
            "只留和眼下话题有关的；留空 = 不筛，按相似度取。每轮多一次模型调用，建议填便宜、快的模型。"
            "只在聊天记忆（域记忆）开着时生效。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            ref_model_groups=True,
            model_type="chat",
            i18n_title=i18n_text(zh_CN="记忆筛选模型组", en_US="Memory recall filter model group"),
            i18n_description=i18n_text(
                zh_CN=(
                    "填了以后，每轮自动想起的记忆在放进提示词之前，先让这个模型组对照最近几句对话看一遍，"
                    "只留和眼下话题有关的；留空 = 不筛，按相似度取。每轮多一次模型调用，建议填便宜、快的模型。"
                    "只在聊天记忆（域记忆）开着时生效。"
                ),
                en_US=(
                    "When set, memories automatically recalled each round are first checked against the recent "
                    "conversation by this model group before entering the prompt, and only those related to the "
                    "current topic are kept; empty = no filtering, keep the similarity ranking. This adds one model "
                    "call per round, so a cheap and fast model group is recommended. It takes effect only when chat "
                    "memory (domain memory) is enabled."
                ),
            ),
        ).model_dump(),
    )
    MEMORY_CONSOLIDATION_MODEL_GROUP: str = Field(
        default="default",
        title="情景沉淀模型组",
        description="用于情景记忆沉淀提取的模型组名称。留空时回退到默认聊天模型组",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            overridable=True,
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
        description=(
            "当情景沉淀主模型组因鉴权、限流、超时或服务端错误整轮失败时，"
            "改用备用模型组把这一批完整重试一轮。留空时回退到情景沉淀主模型组"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            overridable=True,
            ref_model_groups=True,
            model_type="chat",
            i18n_title=i18n_text(zh_CN="情景沉淀备用模型组", en_US="Memory Consolidation Fallback Model Group"),
            i18n_description=i18n_text(
                zh_CN=(
                    "当情景沉淀主模型组因鉴权、限流、超时或服务端错误整轮失败时，"
                    "改用备用模型组把这一批完整重试一轮。留空时回退到情景沉淀主模型组"
                ),
                en_US=(
                    "When the primary consolidation model group fails for a whole round because of "
                    "authentication, rate limiting, timeout, or a server error, the fallback group "
                    "retries the batch once in full. Falls back to the primary group when empty"
                ),
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
    MEMORY_CONSOLIDATION_DEDUP_SIMILARITY: float = Field(
        default=0.92,
        ge=0.0,
        le=1.0,
        title="沉淀近重复相似度",
        description="新记忆与同一频道近两周内、涉及同一批人的已有记忆相似度达到这个值，就当作同一件事："
        "两段都保留，较笼统的一段放进隔离区（原因：近重复），后台可以放回。"
        "相似度在 0.84 到这个值之间的，交给沉淀模型判断是不是同一件事。0 = 不查。",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="沉淀近重复相似度", en_US="Consolidation Near-Duplicate Similarity"),
            i18n_description=i18n_text(
                zh_CN="新记忆与同一频道近两周内、涉及同一批人的已有记忆相似度达到这个值，就当作同一件事："
                "两段都保留，较笼统的一段放进隔离区（原因：近重复），后台可以放回。"
                "相似度在 0.84 到这个值之间的，交给沉淀模型判断是不是同一件事。0 = 不查。",
                en_US=(
                    "When a new memory's similarity to an existing memory from the same channel within the last two "
                    "weeks and involving the same people reaches this value, they count as the same matter: both are "
                    "kept, the more general one is moved to quarantine (reason: near-duplicate) and can be restored "
                    "from the admin page. Between 0.84 and this value, the consolidation model decides whether they "
                    "are the same matter. 0 disables the check."
                ),
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
        description="新创建关系记忆的默认半衰期秒数。值越小，关系有效权重衰减越快。沉淀出的关系不短于来源段落的半衰期（偏好、事实、决定抽出的关系按语义半衰期保留）",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="关系半衰期", en_US="Relation Half-Life"),
            i18n_description=i18n_text(
                zh_CN="新创建关系记忆的默认半衰期秒数。值越小，关系有效权重衰减越快。沉淀出的关系不短于来源段落的半衰期（偏好、事实、决定抽出的关系按语义半衰期保留）",
                en_US=(
                    "Default half-life in seconds for newly created relation memories. Smaller values decay relation weight "
                    "faster. Consolidated relations never decay faster than the paragraph they come from (relations from "
                    "preferences, facts and decisions keep the semantic half-life)"
                ),
            ),
        ).model_dump(),
    )
    MEMORY_SEMANTIC_HALF_LIFE_DAYS: int = Field(
        default=30,
        ge=1,
        le=3650,
        title="语义记忆半衰期天数",
        description="沉淀出的偏好、事实、决定（关于人的稳定信息）与 CC 语义记忆的半衰期天数（1 ~ 3650）。其余沉淀记忆按情景半衰期衰减",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="语义记忆半衰期天数", en_US="Semantic Half-Life Days"),
            i18n_description=i18n_text(
                zh_CN="沉淀出的偏好、事实、决定（关于人的稳定信息）与 CC 语义记忆的半衰期天数（1 ~ 3650）。其余沉淀记忆按情景半衰期衰减",
                en_US=(
                    "Half-life in days for consolidated preferences, facts and decisions (stable information about people) "
                    "and for CC semantic memories (1 to 3650). Other consolidated memories decay with the episodic half-life"
                ),
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
        description="调度器两次派发沉淀之间的最短间隔（秒），把多个频道的沉淀错开开跑；不随排队的频道数放大",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="错峰调度延迟", en_US="Stagger Delay"),
            i18n_description=i18n_text(
                zh_CN="调度器两次派发沉淀之间的最短间隔（秒），把多个频道的沉淀错开开跑；不随排队的频道数放大",
                en_US="Minimum gap in seconds between two consolidation dispatches, so channels start staggered; it does not grow with the number of queued channels",
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
                zh_CN="启用后会在启动时扫描历史未沉淀消息，积压达到沉淀阈值的频道都自动补跑，可能显著占用系统资源。关闭时只兜底积压超过一批的频道，让换版重启后的积压能自己消化完",
                en_US="When enabled, scans historical unconsolidated messages on startup and resumes every chat whose backlog reaches the consolidation threshold, which may significantly consume system resources. When disabled, only chats with more than one batch of backlog are resumed, so backlogs left by a restart still drain on their own",
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
    MEMORY_PROMPT_ROUNDS_KEEP: int = Field(
        default=20,
        ge=0,
        title="提示词明细保留轮数",
        description="每个频道保留最近多少轮提示词明细；0 表示不记录。本项不可覆盖",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="提示词明细保留轮数", en_US="Prompt Detail Rounds To Keep"),
            i18n_description=i18n_text(
                zh_CN="每个频道保留最近多少轮提示词明细；0 表示不记录。本项不可覆盖",
                en_US=(
                    "How many recent prompt-detail rounds to keep per channel; 0 means do not record. "
                    "This item is not overridable"
                ),
            ),
        ).model_dump(),
    )
    MEMORY_PROMPT_ROUNDS_MAX_AGE_DAYS: int = Field(
        default=30,
        ge=1,
        title="提示词明细最长保留天数",
        description="提示词明细的最长保留天数，维护任务按它清理超龄记录。本项不可覆盖",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="提示词明细最长保留天数", en_US="Prompt Detail Max Age Days"),
            i18n_description=i18n_text(
                zh_CN="提示词明细的最长保留天数，维护任务按它清理超龄记录。本项不可覆盖",
                en_US=(
                    "Maximum age in days for prompt-detail rounds; the maintenance task removes older rows. "
                    "This item is not overridable"
                ),
            ),
        ).model_dump(),
    )
    MEMORY_AUTO_QUARANTINE_BLOCKED_ROUNDS: int = Field(
        default=3,
        ge=0,
        title="被拦轮次自动隔离阈值",
        description="同一条记忆在多少个被拦轮次里被注入就自动隔离；0 表示只计数、不隔离。本项不可覆盖",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="记忆系统", en_US="Memory System"),
            i18n_title=i18n_text(zh_CN="被拦轮次自动隔离阈值", en_US="Auto Quarantine Blocked Rounds"),
            i18n_description=i18n_text(
                zh_CN="同一条记忆在多少个被拦轮次里被注入就自动隔离；0 表示只计数、不隔离。本项不可覆盖",
                en_US=(
                    "A memory is quarantined automatically after it has been injected in this many blocked rounds; "
                    "0 means count only, never quarantine. This item is not overridable"
                ),
            ),
        ).model_dump(),
    )

    WORLDBOOK_ENABLED: bool = Field(
        default=False,
        title="启用世界书",
        description=(
            "是否注入世界书条目。可在全局、实例、频道三层设置，下层覆盖上层，最终按每个频道生效的值判断。"
            "关着时运行时不查世界书、提示词与以前逐字相同；管理页照常可用（可以先写好再打开）。"
            "平台方与租户管理员都能为自己的实例 / 频道打开或关闭。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="世界书", en_US="Worldbook"),
            overridable=True,
            i18n_title=i18n_text(zh_CN="启用世界书", en_US="Enable Worldbook"),
            i18n_description=i18n_text(
                zh_CN=(
                    "是否注入世界书条目。可在全局、实例、频道三层设置，下层覆盖上层，最终按每个频道生效的值判断。"
                    "关着时运行时不查世界书、提示词与以前逐字相同；管理页照常可用（可以先写好再打开）。"
                    "平台方与租户管理员都能为自己的实例 / 频道打开或关闭。"
                ),
                en_US=(
                    "Whether worldbook entries are injected. It can be set at the global, instance and "
                    "channel levels, each overriding the one above, and is decided by the value in effect for "
                    "each channel. While it is off the runtime never queries worldbook tables and the prompts "
                    "stay byte-for-byte identical to before; the admin pages keep working, so entries can be "
                    "prepared before turning it on. Both the platform and tenant administrators may turn it on "
                    "or off for their own instances or channels."
                ),
            ),
        ).model_dump(),
    )
    WORLDBOOK_VECTOR_ENABLED: bool = Field(
        default=True,
        title="世界书语义触发",
        description="关掉后这个范围内的世界书只按关键词与常驻匹配；条目和书上的语义设置保留",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="世界书", en_US="Worldbook"),
            overridable=True,
            i18n_title=i18n_text(zh_CN="世界书语义触发", en_US="Worldbook Semantic Activation"),
            i18n_description=i18n_text(
                zh_CN="关掉后这个范围内的世界书只按关键词与常驻匹配；条目和书上的语义设置保留",
                en_US=(
                    "When off, worldbook entries in this scope match only by keyword or constant; "
                    "the semantic settings on entries and books are kept."
                ),
            ),
        ).model_dump(),
    )
    WORLDBOOK_BUDGET_PERCENT: int = Field(
        default=25,
        ge=1,
        le=100,
        title="世界书预算：占上下文的百分比",
        description=(
            "世界书每轮最多占「上下文长度 − 回复预留 4096」的百分之几（与 SillyTavern 的 Context % 同义）。"
            "上下文长度取这一轮主模型组上填的值，没填按 32768 算。"
            "可在全局、实例、频道三层设置，下层覆盖上层；租户管理员只能往下调。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="世界书", en_US="Worldbook"),
            overridable=True,
            tenant_may_only_lower=True,
            slider=True,
            i18n_title=i18n_text(zh_CN="世界书预算：占上下文的百分比", en_US="Worldbook Budget: Context %"),
            i18n_description=i18n_text(
                zh_CN=(
                    "世界书每轮最多占「上下文长度 − 回复预留 4096」的百分之几（与 SillyTavern 的 Context % 同义）。"
                    "上下文长度取这一轮主模型组上填的值，没填按 32768 算。"
                    "可在全局、实例、频道三层设置，下层覆盖上层；租户管理员只能往下调。"
                ),
                en_US=(
                    'The share of "context length minus a 4096-token reply reserve" that worldbook entries may '
                    "use per round, like SillyTavern's Context %. The context length is the one set on this "
                    "round's main model group, or 32768 when it is not set. It can be set at the global, instance "
                    "and channel levels, each overriding the one above; tenant administrators may only lower it."
                ),
            ),
        ).model_dump(),
    )
    WORLDBOOK_BUDGET_CAP_TOKENS: int = Field(
        default=0,
        ge=0,
        le=65536,
        title="世界书预算：token 上限",
        description=(
            "世界书每轮 token 数的上限（与 SillyTavern 的 Budget Cap 同义）。预算取「按百分比算出的数」与这个上限中"
            "较小的一个；0 表示不另设上限、只按百分比算，但每轮预算无论如何不超过 65536。可在全局、实例、频道三层设置，"
            "下层覆盖上层；平台方可以在全局给一个上限，免得填了大上下文的模型组把费用一下子放大；"
            "租户管理员只能往下调，上一层设了上限时不能改成 0。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="世界书", en_US="Worldbook"),
            overridable=True,
            tenant_may_only_lower=True,
            zero_means_unlimited=True,
            slider=True,
            i18n_title=i18n_text(zh_CN="世界书预算：token 上限", en_US="Worldbook Budget: Token Cap"),
            i18n_description=i18n_text(
                zh_CN=(
                    "世界书每轮 token 数的上限（与 SillyTavern 的 Budget Cap 同义）。预算取「按百分比算出的数」与这个上限中"
                    "较小的一个；0 表示不另设上限、只按百分比算，但每轮预算无论如何不超过 65536。可在全局、实例、频道三层设置，"
                    "下层覆盖上层；平台方可以在全局给一个上限，免得填了大上下文的模型组把费用一下子放大；"
                    "租户管理员只能往下调，上一层设了上限时不能改成 0。"
                ),
                en_US=(
                    "A cap on worldbook tokens per round, like SillyTavern's Budget Cap. The budget is the smaller "
                    "of the percentage-based figure and this cap; 0 means no extra cap, so only the percentage "
                    "applies, but the budget never exceeds 65536 tokens per round. It can be set at the global, "
                    "instance and channel levels, each overriding the one above. The platform may set a global cap "
                    "so that a model group with a large context does not multiply the cost at once; tenant "
                    "administrators may only lower it and cannot set it to 0 while the level above has a cap."
                ),
            ),
        ).model_dump(),
    )
    WORLDBOOK_LOOKUP_MAX_TOKENS: int = Field(
        default=3000,
        ge=200,
        le=20000,
        title="世界书查询：每次带回的 token 上限",
        description=(
            "模型每次查询世界书最多带回多少 token。可在全局、实例、频道三层设置，下层覆盖上层；"
            "租户管理员只能往下调。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="世界书", en_US="Worldbook"),
            overridable=True,
            tenant_may_only_lower=True,
            slider=True,
            i18n_title=i18n_text(
                zh_CN="世界书查询：每次带回的 token 上限",
                en_US="Worldbook Lookup: Tokens per Call",
            ),
            i18n_description=i18n_text(
                zh_CN=(
                    "模型每次查询世界书最多带回多少 token。可在全局、实例、频道三层设置，下层覆盖上层；"
                    "租户管理员只能往下调。"
                ),
                en_US=(
                    "The most tokens a single worldbook lookup may bring back. It can be set at the global, "
                    "instance and channel levels, each overriding the one above; tenant administrators may "
                    "only lower it."
                ),
            ),
        ).model_dump(),
    )
    WORLDBOOK_LOOKUP_MAX_CALLS_PER_ROUND: int = Field(
        default=4,
        ge=0,
        le=20,
        title="世界书查询：每轮次数上限",
        description=(
            "模型每轮最多可以查几次世界书（搜索与按编号读取共用）。0 表示不让模型查。"
            "可在全局、实例、频道三层设置，下层覆盖上层；租户管理员只能往下调。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="世界书", en_US="Worldbook"),
            overridable=True,
            tenant_may_only_lower=True,
            slider=True,
            i18n_title=i18n_text(
                zh_CN="世界书查询：每轮次数上限",
                en_US="Worldbook Lookup: Calls per Round",
            ),
            i18n_description=i18n_text(
                zh_CN=(
                    "模型每轮最多可以查几次世界书（搜索与按编号读取共用）。0 表示不让模型查。"
                    "可在全局、实例、频道三层设置，下层覆盖上层；租户管理员只能往下调。"
                ),
                en_US=(
                    "How many worldbook lookups the model may make per round; search and read by id share "
                    "this count. 0 means the model cannot look anything up. It can be set at the global, "
                    "instance and channel levels, each overriding the one above; tenant administrators may "
                    "only lower it."
                ),
            ),
        ).model_dump(),
    )
    # 角色状态（可选内置插件 `state_table`）的两项要能按频道覆盖，插件配置只有实例级，所以放在这里。
    # 插件关着时这两项不起作用。0 表示「不注入状态块」，与世界书上限的「不设上限」意思相反，所以
    # 不标 `zero_means_unlimited`：租户往下调到 0 就是在自己的实例或频道里关掉状态块。
    STATE_PROMPT_MAX_TOKENS: int = Field(
        default=1200,
        ge=0,
        le=8192,
        title="角色状态：状态块 token 上限",
        description=(
            "角色状态插件每轮注入的状态块最多多少 token（约 1.25 字一个 token），单独计、不占世界书预算；"
            "超了按字段的优先级整组截掉。0 表示不注入状态块（状态照常更新）。可在全局、实例、频道三层设置，"
            "租户管理员只能往下调。插件没开时不起作用。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="角色状态", en_US="Character State"),
            overridable=True,
            tenant_may_only_lower=True,
            slider=True,
            i18n_title=i18n_text(zh_CN="角色状态：状态块 token 上限", en_US="Character State: Block Token Cap"),
            i18n_description=i18n_text(
                zh_CN=(
                    "角色状态插件每轮注入的状态块最多多少 token（约 1.25 字一个 token），单独计、不占世界书预算；"
                    "超了按字段的优先级整组截掉。0 表示不注入状态块（状态照常更新）。可在全局、实例、频道三层设置，"
                    "租户管理员只能往下调。插件没开时不起作用。"
                ),
                en_US=(
                    "The most tokens the character state plugin injects as its state block each round. It is "
                    "counted on its own and does not use the worldbook budget; when over, lower-priority field "
                    "groups are cut as a whole. 0 means no state block is injected (the state is still updated). "
                    "It can be set at the global, instance and channel levels; tenant administrators may only "
                    "lower it. It has no effect while the plugin is off."
                ),
            ),
        ).model_dump(),
    )
    STATE_PRESENT_WINDOW_MINUTES: int = Field(
        default=30,
        ge=1,
        le=1440,
        title="角色状态：在场的人（分钟）",
        description=(
            "状态块只写最近这么多分钟内在频道里说过话的人的个人档案，由系统按发言时间算，不让模型判断。"
            "可在全局、实例、频道三层设置。插件没开时不起作用。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="角色状态", en_US="Character State"),
            overridable=True,
            i18n_title=i18n_text(zh_CN="角色状态：在场的人（分钟）", en_US="Character State: Present Window (Minutes)"),
            i18n_description=i18n_text(
                zh_CN=(
                    "状态块只写最近这么多分钟内在频道里说过话的人的个人档案，由系统按发言时间算，不让模型判断。"
                    "可在全局、实例、频道三层设置。插件没开时不起作用。"
                ),
                en_US=(
                    "The state block only includes the profiles of people who spoke in the channel within the "
                    "last this many minutes; the system decides this from message times, not the model. It can "
                    "be set at the global, instance and channel levels. It has no effect while the plugin is off."
                ),
            ),
        ).model_dump(),
    )
    STATE_COMPAT_PARSE: Literal["private", "all", "off"] = Field(
        default=STATE_COMPAT_PARSE_DEFAULT,
        title="角色状态：解析回复里的变量更新块",
        description=(
            "酒馆卡要求模型在回复里写变量更新块（`<UpdateVariable>` 一类）时，发送前剥掉的块要不要用来更新状态。"
            "私聊＝只在私聊里用；全部＝群聊也用（群里只认频道状态的路径，个人档案的路径一律不收）；关＝只剥不用。"
            "块里的改动和其它更新一样过系统的校验，被拦或失败的轮次不用。可在全局、实例、频道三层设置。插件没开时不起作用。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="角色状态", en_US="Character State"),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="角色状态：解析回复里的变量更新块", en_US="Character State: Parse Variable Blocks in Replies"
            ),
            i18n_description=i18n_text(
                zh_CN=(
                    "酒馆卡要求模型在回复里写变量更新块（`<UpdateVariable>` 一类）时，发送前剥掉的块要不要用来更新状态。"
                    "私聊＝只在私聊里用；全部＝群聊也用（群里只认频道状态的路径，个人档案的路径一律不收）；关＝只剥不用。"
                    "块里的改动和其它更新一样过系统的校验，被拦或失败的轮次不用。可在全局、实例、频道三层设置。插件没开时不起作用。"
                ),
                en_US=(
                    "When a tavern card asks the model to write variable update blocks (such as `<UpdateVariable>`) "
                    "in its replies, whether the blocks stripped before sending are used to update the state. "
                    "private = only in private chats; all = group chats too (in groups only channel state paths are "
                    "accepted, never profile paths); off = strip only. The changes go through the same system "
                    "validation as other updates, and rounds that were blocked or failed are not used. It can be set "
                    "at the global, instance and channel levels. It has no effect while the plugin is off."
                ),
            ),
        ).model_dump(),
    )
    STATE_IMAGE_SEND_MODE: Literal["command", "every_reply", "every_n"] = Field(
        default="command",
        title="角色状态：状态图什么时候发",
        description=(
            "只在命令时＝群友发「/状态 图」才发；每次回复后＝每轮回复成功后发一张；每 N 轮＝每隔「状态图间隔」轮发一张。"
            "自动发的图在群里只画频道状态里不私密的部分，不进聊天记录。平台方可以设间隔的下限，"
            "「每次回复后」也受这个下限约束。可在全局、实例、频道三层设置。插件没开时不起作用。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="角色状态", en_US="Character State"),
            overridable=True,
            i18n_title=i18n_text(zh_CN="角色状态：状态图什么时候发", en_US="Character State: When to Send the Image"),
            i18n_description=i18n_text(
                zh_CN=(
                    "只在命令时＝群友发「/状态 图」才发；每次回复后＝每轮回复成功后发一张；每 N 轮＝每隔「状态图间隔」轮发一张。"
                    "自动发的图在群里只画频道状态里不私密的部分，不进聊天记录。平台方可以设间隔的下限，"
                    "「每次回复后」也受这个下限约束。可在全局、实例、频道三层设置。插件没开时不起作用。"
                ),
                en_US=(
                    "command = only when someone sends `/state image`; every_reply = one image after every "
                    "successful reply; every_n = one image every 'image interval' rounds. Images sent "
                    "automatically show only the non-private part of the channel state in groups and are not "
                    "recorded in the chat history. The platform may set a minimum interval, which also applies to "
                    "every_reply. It can be set at the global, instance and channel levels. It has no effect while "
                    "the plugin is off."
                ),
            ),
        ).model_dump(),
    )
    STATE_IMAGE_EVERY_N: int = Field(
        default=10,
        ge=1,
        le=1000,
        title="角色状态：状态图间隔（轮）",
        description=(
            "「每 N 轮」时隔多少轮发一张，只数回复成功的轮次；实际间隔不小于平台方设的下限。"
            "可在全局、实例、频道三层设置。插件没开时不起作用。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="角色状态", en_US="Character State"),
            overridable=True,
            i18n_title=i18n_text(zh_CN="角色状态：状态图间隔（轮）", en_US="Character State: Image Interval (Rounds)"),
            i18n_description=i18n_text(
                zh_CN=(
                    "「每 N 轮」时隔多少轮发一张，只数回复成功的轮次；实际间隔不小于平台方设的下限。"
                    "可在全局、实例、频道三层设置。插件没开时不起作用。"
                ),
                en_US=(
                    "With every_n, how many rounds between images; only successful rounds count, and the actual "
                    "interval is never below the platform minimum. It can be set at the global, instance and "
                    "channel levels. It has no effect while the plugin is off."
                ),
            ),
        ).model_dump(),
    )
    STATE_IMAGE_MIN_N: int = Field(
        default=1,
        ge=1,
        le=1000,
        title="角色状态：状态图间隔下限（轮）",
        description=(
            "自动发状态图时，间隔至少这么多轮（防刷屏）；「每次回复后」按 1 轮算，也会被拉到这个下限。"
            "只有平台方能设，不能按实例或频道覆盖。插件没开时不起作用。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="角色状态", en_US="Character State"),
            i18n_title=i18n_text(
                zh_CN="角色状态：状态图间隔下限（轮）", en_US="Character State: Minimum Image Interval (Rounds)"
            ),
            i18n_description=i18n_text(
                zh_CN=(
                    "自动发状态图时，间隔至少这么多轮（防刷屏）；「每次回复后」按 1 轮算，也会被拉到这个下限。"
                    "只有平台方能设，不能按实例或频道覆盖。插件没开时不起作用。"
                ),
                en_US=(
                    "When images are sent automatically, at least this many rounds pass between two images (to "
                    "avoid flooding); every_reply counts as 1 and is raised to this minimum too. Only the platform "
                    "can set it; it cannot be overridden per instance or channel. It has no effect while the "
                    "plugin is off."
                ),
            ),
        ).model_dump(),
    )
    # 已废弃：0.4.7 起世界书预算按 token 计（`WORLDBOOK_BUDGET_PERCENT` 与 `WORLDBOOK_BUDGET_CAP_TOKENS`）。启动时迁移一次（见
    # `services/worldbook/budget_migration.py`），之后运行时不再读它；保留字段只为让旧配置文件照常加载，
    # 0.4.8 删除。仍保留覆盖与「租户只能调低」两个标记：旧的覆盖文件照常读得进来，迁移前也不会被放宽。
    WORLDBOOK_MAX_CHARS: int = Field(
        default=3000,
        ge=0,
        le=20000,
        title="（已废弃）世界书每轮正文字数上限",
        description="0.4.7 起改为按 token 计的两项（占上下文的百分比、token 上限），启动时已自动迁移；本项不再生效，0.4.8 删除。",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="世界书", en_US="Worldbook"),
            is_hidden=True,
            overridable=True,
            tenant_may_only_lower=True,
            i18n_title=i18n_text(
                zh_CN="（已废弃）世界书每轮正文字数上限",
                en_US="(Deprecated) Worldbook Max Chars per Round",
            ),
            i18n_description=i18n_text(
                zh_CN="0.4.7 起改为按 token 计的两项（占上下文的百分比、token 上限），启动时已自动迁移；本项不再生效，0.4.8 删除。",
                en_US=(
                    "Replaced in 0.4.7 by the two token-based items (context % and token cap) and migrated at "
                    "startup; this item no longer has any effect and will be removed in 0.4.8."
                ),
            ),
        ).model_dump(),
    )
    WORLDBOOK_BUDGET_MIGRATED: bool = Field(
        default=False,
        title="世界书预算已迁移到按 token 计",
        description="启动时把旧的字数上限迁移到 token 预算以后置为 true，之后不再迁移。由系统维护，不要手改",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="世界书", en_US="Worldbook"),
            is_hidden=True,
        ).model_dump(),
    )
    WORLDBOOK_VECTOR_BACKFILLED: bool = Field(
        default=False,
        title="世界书向量激活标记已回填",
        description="启动时把旧条目 ext 里的 vectorized 回填到列上以后置为 true，之后不再回填。由系统维护，不要手改",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="世界书", en_US="Worldbook"),
            is_hidden=True,
        ).model_dump(),
    )
    WORLDBOOK_BUDGET_MIGRATION_NOTICE: List[str] = Field(
        default_factory=list,
        title="世界书预算迁移记录",
        description="迁移逐层做了什么；非空时世界书配置页顶部显示一次性提示，平台方点「知道了」后清空。由系统维护",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="世界书", en_US="Worldbook"),
            is_hidden=True,
        ).model_dump(),
    )
    WORLDBOOK_MAX_ENTRIES_PER_ROUND: int = Field(
        default=32,
        ge=1,
        le=200,
        title="世界书每轮条数上限",
        description="每轮最多注入多少条世界书条目。本项不可覆盖",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="世界书", en_US="Worldbook"),
            i18n_title=i18n_text(zh_CN="世界书每轮条数上限", en_US="Worldbook Max Entries per Round"),
            i18n_description=i18n_text(
                zh_CN="每轮最多注入多少条世界书条目。本项不可覆盖",
                en_US="How many worldbook entries may be injected per round at most. This item is not overridable",
            ),
        ).model_dump(),
    )
    WORLDBOOK_AUTO_QUARANTINE_BLOCKED_ROUNDS: int = Field(
        default=3,
        ge=0,
        title="世界书被拦轮次自动隔离阈值",
        description=(
            "同一条世界书条目在多少个被拦轮次里被注入就自动隔离；0 表示只计数、不隔离。"
            "常驻条目只计数、不自动隔离；一轮注入的关键词条目超过 5 条时，这一轮也只计数。本项不可覆盖"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="世界书", en_US="Worldbook"),
            i18n_title=i18n_text(zh_CN="世界书被拦轮次自动隔离阈值", en_US="Worldbook Auto Quarantine Blocked Rounds"),
            i18n_description=i18n_text(
                zh_CN=(
                    "同一条世界书条目在多少个被拦轮次里被注入就自动隔离；0 表示只计数、不隔离。"
                    "常驻条目只计数、不自动隔离；一轮注入的关键词条目超过 5 条时，这一轮也只计数。本项不可覆盖"
                ),
                en_US=(
                    "A worldbook entry is quarantined automatically after it has been injected in this many "
                    "blocked rounds; 0 means count only, never quarantine. Constant entries are only counted, "
                    "never auto-quarantined, and a round that injected more than 5 keyword entries is only "
                    "counted too. This item is not overridable"
                ),
            ),
        ).model_dump(),
    )

    TAVERN_CARD_TEMPLATE: str = Field(
        default=DEFAULT_CARD_TEMPLATE,
        title="酒馆人设卡拼装模板",
        description=(
            "导入酒馆卡时按这个模板把描述、性格、场景、示例对话、系统提示词拼成人设正文；"
            "一段里引用的卡片字段都为空时整段去掉；其余 {{…}} 原样保留、运行时展开；"
            "改模板不影响已导入的人设，要在人设的「酒馆扩展」里点重新拼装。"
        ),
        json_schema_extra=ExtraField(
            is_textarea=True,
            i18n_category=i18n_text(zh_CN="酒馆兼容", en_US="Tavern Compatibility"),
            i18n_title=i18n_text(zh_CN="酒馆人设卡拼装模板", en_US="Tavern Card Assembly Template"),
            i18n_description=i18n_text(
                zh_CN=(
                    "导入酒馆卡时按这个模板把描述、性格、场景、示例对话、系统提示词拼成人设正文；"
                    "一段里引用的卡片字段都为空时整段去掉；其余 {{…}} 原样保留、运行时展开；"
                    "改模板不影响已导入的人设，要在人设的「酒馆扩展」里点重新拼装。"
                ),
                en_US=(
                    "When importing a tavern card, assemble the description, personality, scenario, example dialogue "
                    "and system prompt into the persona text with this template. A paragraph is dropped when every card "
                    "field it references is empty. Other {{…}} placeholders are left as-is and expanded at runtime. "
                    "Changing the template does not affect imported personas; click reassemble on the persona's "
                    "Tavern Extension page to apply it."
                ),
            ),
        ).model_dump(),
    )

    # 酒馆人设卡运行时（宏、深度注入、开场白、输出清理、高级槽位）的总闸。默认开：
    # 没有卡片数据的人设与不含宏的世界书条目开着闸也逐字不变，出问题时一键关回改造前的行为。
    TAVERN_COMPAT_ENABLED: bool = Field(
        default=True,
        title="启用酒馆人设卡运行时",
        description=(
            "酒馆人设卡运行时的回滚闸。关掉时运行时不读人设的酒馆扩展、不展开宏、不发开场白、"
            "不做输出清理、不发高级槽位，提示词与以前逐字相同；导入导出与扩展页照常可用。本项不可覆盖"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="酒馆兼容", en_US="Tavern Compatibility"),
            i18n_title=i18n_text(zh_CN="启用酒馆人设卡运行时", en_US="Enable Tavern Persona Runtime"),
            i18n_description=i18n_text(
                zh_CN=(
                    "酒馆人设卡运行时的回滚闸。关掉时运行时不读人设的酒馆扩展、不展开宏、不发开场白、"
                    "不做输出清理、不发高级槽位，提示词与以前逐字相同；导入导出与扩展页照常可用。本项不可覆盖"
                ),
                en_US=(
                    "Rollback gate for the tavern persona runtime. When it is off the runtime does not read the "
                    "persona's tavern extension, does not expand macros, sends no greeting, applies no output "
                    "cleanup and sends no advanced slot; the prompts stay byte-for-byte identical to before. "
                    "Import/export and the extension page keep working. This item is not overridable"
                ),
            ),
        ).model_dump(),
    )
    # 已废弃：开场白自 0.4.7 起不再发进聊天，本项不再被运行时读取；保留字段只为让旧配置文件与覆盖文件
    # 照常加载，下一个版本删除。导入、编辑、导出照常保存开场白。
    PERSONA_GREETING_ENABLED: bool = Field(
        default=False,
        title="发送人设开场白（已废弃）",
        description="已废弃：开场白不再发进聊天，本项不起作用，下一个版本删除。导入、编辑、导出照常保存开场白",
        json_schema_extra=ExtraField(
            is_hidden=True,
            overridable=True,
            i18n_category=i18n_text(zh_CN="酒馆兼容", en_US="Tavern Compatibility"),
            i18n_title=i18n_text(zh_CN="发送人设开场白（已废弃）", en_US="Send Persona Greeting (Deprecated)"),
            i18n_description=i18n_text(
                zh_CN="已废弃：开场白不再发进聊天，本项不起作用，下一个版本删除。导入、编辑、导出照常保存开场白",
                en_US=(
                    "Deprecated: greetings are no longer sent to the chat, so this item has no effect and will be "
                    "removed in the next version. Importing, editing and exporting still keep greetings."
                ),
            ),
        ).model_dump(),
    )
    TAVERN_TRIAL_DAILY_LIMIT: int = Field(
        default=100,
        ge=0,
        le=100000,
        title="每实例每天试发次数上限",
        description=(
            "每个实例每天最多试发几次提示词；0 表示不限。用的是管理员自己的 Key，"
            "这个上限只为挡住误操作与脚本刷，不为省钱。本项不可覆盖"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="酒馆兼容", en_US="Tavern Compatibility"),
            i18n_title=i18n_text(zh_CN="每实例每天试发次数上限", en_US="Daily Prompt Trial Limit per Instance"),
            i18n_description=i18n_text(
                zh_CN=(
                    "每个实例每天最多试发几次提示词；0 表示不限。用的是管理员自己的 Key，"
                    "这个上限只为挡住误操作与脚本刷，不为省钱。本项不可覆盖"
                ),
                en_US=(
                    "How many prompt trials an instance may run per day; 0 means unlimited. Trials use the "
                    "administrator's own key, so the limit only guards against mistakes and scripted abuse, not "
                    "cost. This item is not overridable"
                ),
            ),
        ).model_dump(),
    )

    # 三个高级提示词槽位出厂一律留空：NA 不内置任何破限文字，内容由管理员自己填。
    # 标了审计，是因为这几个字段装的可能是敏感文字，改动要能追溯到人、又不能把原文再存一份。
    PROMPT_ADVANCED_SLOTS_ENABLED: bool = Field(
        default=False,
        title="启用高级提示词槽位",
        description=(
            "打开后，这个实例会把后置指令、辅助提示词与预填发给模型。内容及其后果、对应 Key 的合规由管理员负责。"
            "平台方与租户管理员都能为自己的实例或频道开关"
        ),
        json_schema_extra=ExtraField(
            overridable=True,
            audit_changes=True,
            enable_confirm=i18n_text(
                zh_CN="打开后，这个实例会把后置指令、辅助提示词与预填发给模型。内容及其后果、对应 Key 的合规由管理员负责。",
                en_US=(
                    "Once enabled, this instance sends post-history instructions, auxiliary instructions and prefill "
                    "to the model. The administrator is responsible for the content, its consequences and the "
                    "compliance of the corresponding key."
                ),
            ),
            i18n_category=i18n_text(zh_CN="高级提示词槽位", en_US="Advanced Prompt Slots"),
            i18n_title=i18n_text(zh_CN="启用高级提示词槽位", en_US="Enable Advanced Prompt Slots"),
            i18n_description=i18n_text(
                zh_CN=(
                    "打开后，这个实例会把后置指令、辅助提示词与预填发给模型。内容及其后果、对应 Key 的合规由管理员负责。"
                    "平台方与租户管理员都能为自己的实例或频道开关"
                ),
                en_US=(
                    "Once enabled, this instance sends post-history instructions, auxiliary instructions and prefill "
                    "to the model. The administrator is responsible for the content, its consequences and the "
                    "compliance of the corresponding key. Both the platform and tenant administrators may turn it on "
                    "or off for their own instances or channels."
                ),
            ),
        ).model_dump(),
    )
    PROMPT_STABLE_PREFIX_ORDER: bool = Field(
        default=False,
        title="提示词稳定前缀顺序",
        description=(
            "开启后，世界书「人设之后」组的本轮命中与好感块从人设之后挪到系统提示词末尾（插件说明与运维说明之后），"
            "它们前面的部分更容易命中上游的提示词缓存。人设里的酒馆宏（随触发人变化的 {{user}}、{{time}}、{{random}} 等）、"
            "插件说明里的激活轮数提示、运维说明的有无仍会让前缀变化。会改变提示词里各段的先后，建议先在部分频道试用。"
        ),
        json_schema_extra=ExtraField(
            overridable=True,
            audit_changes=True,
            i18n_category=i18n_text(zh_CN="高级提示词槽位", en_US="Advanced Prompt Slots"),
            i18n_title=i18n_text(zh_CN="提示词稳定前缀顺序", en_US="Stable Prompt Prefix Order"),
            i18n_description=i18n_text(
                zh_CN=(
                    "开启后，世界书「人设之后」组的本轮命中与好感块从人设之后挪到系统提示词末尾（插件说明与运维说明之后），"
                    "它们前面的部分更容易命中上游的提示词缓存。人设里的酒馆宏（随触发人变化的 {{user}}、{{time}}、{{random}} 等）、"
                    "插件说明里的激活轮数提示、运维说明的有无仍会让前缀变化。会改变提示词里各段的先后，建议先在部分频道试用。"
                ),
                en_US=(
                    "When enabled, this round's world info hits in the after-persona group and the affection block move "
                    "from right after the persona to the end of the system prompt (after the plugin and ops instructions), "
                    "so the part before them is more likely to hit the upstream prompt cache. Tavern macros in the persona "
                    "(such as {{user}}, which follows whoever triggered the round, {{time}} and {{random}}), plugin "
                    "activation hints and whether the ops section is present still change the prefix. This changes the "
                    "order of the sections; try it in a few channels first."
                ),
            ),
        ).model_dump(),
    )
    PROMPT_RP_PRESET_ID: int = Field(
        default=0,
        title="角色扮演预设",
        description=(
            "选一份导入的酒馆预设；只在「启用高级提示词槽位」打开、且人设走酒馆兼容运行时的频道生效。0 = 不用"
        ),
        json_schema_extra=ExtraField(
            overridable=True,
            audit_changes=True,
            ref_rp_presets=True,
            i18n_category=i18n_text(zh_CN="高级提示词槽位", en_US="Advanced Prompt Slots"),
            i18n_title=i18n_text(zh_CN="角色扮演预设", en_US="Roleplay Preset"),
            i18n_description=i18n_text(
                zh_CN="选一份导入的酒馆预设；只在「启用高级提示词槽位」打开、且人设走酒馆兼容运行时的频道生效。0 = 不用",
                en_US=(
                    "Pick an imported tavern preset. It only applies when advanced prompt slots are enabled and the "
                    "persona runs through the tavern-compatible runtime. 0 means not used."
                ),
            ),
        ).model_dump(),
    )
    PROMPT_POST_HISTORY: str = Field(
        default="",
        title="后置指令",
        description=(
            "放在聊天记录之后发给模型；支持 {{original}} 保留上一层的结果；"
            "只在高级提示词槽位打开时发送"
        ),
        json_schema_extra=ExtraField(
            overridable=True,
            is_textarea=True,
            audit_changes=True,
            i18n_category=i18n_text(zh_CN="高级提示词槽位", en_US="Advanced Prompt Slots"),
            i18n_title=i18n_text(zh_CN="后置指令", en_US="Post-History Instructions"),
            i18n_description=i18n_text(
                zh_CN=(
                    "放在聊天记录之后发给模型；支持 {{original}} 保留上一层的结果；"
                    "只在高级提示词槽位打开时发送"
                ),
                en_US=(
                    "Sent after the chat history; {{original}} keeps the value from the layer below; only sent when "
                    "advanced prompt slots are enabled."
                ),
            ),
        ).model_dump(),
    )
    PROMPT_AUXILIARY: str = Field(
        default="",
        title="辅助提示词",
        description=(
            "放在系统提示词里人设之后；支持 {{original}} 保留上一层的结果；"
            "只在高级提示词槽位打开时发送"
        ),
        json_schema_extra=ExtraField(
            overridable=True,
            is_textarea=True,
            audit_changes=True,
            i18n_category=i18n_text(zh_CN="高级提示词槽位", en_US="Advanced Prompt Slots"),
            i18n_title=i18n_text(zh_CN="辅助提示词", en_US="Auxiliary Instructions"),
            i18n_description=i18n_text(
                zh_CN=(
                    "放在系统提示词里人设之后；支持 {{original}} 保留上一层的结果；"
                    "只在高级提示词槽位打开时发送"
                ),
                en_US=(
                    "Placed in the system prompt after the persona text; {{original}} keeps the value from the layer "
                    "below; only sent when advanced prompt slots are enabled."
                ),
            ),
        ).model_dump(),
    )
    PROMPT_PREFILL: str = Field(
        default="",
        title="预填",
        description=(
            "作为回答的开头发给模型；支持 {{original}} 保留上一层的结果；"
            "只对模型组「预填方式」不是关的组生效；全局层通常留空，按实例填"
        ),
        json_schema_extra=ExtraField(
            overridable=True,
            is_textarea=True,
            audit_changes=True,
            i18n_category=i18n_text(zh_CN="高级提示词槽位", en_US="Advanced Prompt Slots"),
            i18n_title=i18n_text(zh_CN="预填", en_US="Assistant Prefill"),
            i18n_description=i18n_text(
                zh_CN=(
                    "作为回答的开头发给模型；支持 {{original}} 保留上一层的结果；"
                    "只对模型组「预填方式」不是关的组生效；全局层通常留空，按实例填"
                ),
                en_US=(
                    "Sent to the model as the beginning of the answer; {{original}} keeps the value from the layer "
                    "below; only applies to model groups whose prefill mode is not off; the global layer is usually "
                    "left empty and the value is filled per instance."
                ),
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
    OUTPUT_FILTER_ENABLE: bool = Field(
        default=False,
        title="启用输出过滤",
        description=(
            "开启后 Bot 发出的文字先过词表和正则，命中的按下面的方式替换。"
            "覆盖范围：Bot 回复、命令输出、插件经框架接口发送的消息；"
            "OneBot V11 下插件直接调用发送类接口（send_msg / 合并转发等）的也会过滤。"
            "过滤不到：插件绕过框架直接连协议端（自己发 HTTP / WebSocket）发出的消息，"
            "以及非 OneBot 适配器上插件直接调平台 SDK 发出的消息。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="输出过滤", en_US="Output Filter"),
            overridable=True,
            i18n_title=i18n_text(zh_CN="启用输出过滤", en_US="Enable Output Filter"),
            i18n_description=i18n_text(
                zh_CN=(
                    "开启后 Bot 发出的文字先过词表和正则，命中的按下面的方式替换。"
                    "覆盖范围：Bot 回复、命令输出、插件经框架接口发送的消息；"
                    "OneBot V11 下插件直接调用发送类接口（send_msg / 合并转发等）的也会过滤。"
                    "过滤不到：插件绕过框架直接连协议端（自己发 HTTP / WebSocket）发出的消息，"
                    "以及非 OneBot 适配器上插件直接调平台 SDK 发出的消息。"
                ),
                en_US=(
                    "When enabled, text sent by the bot is matched against the word and regex lists before "
                    "sending, and hits are replaced as configured below. Covers bot replies, command output, "
                    "and messages plugins send through the framework; on OneBot V11 it also covers plugins "
                    "calling send APIs directly (send_msg, forward messages, etc.). It cannot cover messages "
                    "sent by plugins that bypass the framework and talk to the protocol endpoint directly "
                    "(their own HTTP / WebSocket), nor messages sent by plugins through a platform SDK on "
                    "non-OneBot adapters."
                ),
            ),
        ).model_dump(),
    )
    OUTPUT_FILTER_MODE: Literal["fragment", "whole"] = Field(
        default="fragment",
        title="过滤方式",
        description="fragment：只替换命中的片段；whole：整条消息换成替换文字",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="输出过滤", en_US="Output Filter"),
            overridable=True,
            i18n_title=i18n_text(zh_CN="过滤方式", en_US="Filter Mode"),
            i18n_description=i18n_text(
                zh_CN="fragment：只替换命中的片段；whole：整条消息换成替换文字",
                en_US="fragment: replace only the matched fragments; whole: replace the entire message with the replacement text",
            ),
        ).model_dump(),
    )
    OUTPUT_FILTER_REPLACEMENT: str = Field(
        default="**Filter**",
        title="替换文字",
        description="命中片段或整条消息要换成的文字；留空按 **Filter** 处理",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="输出过滤", en_US="Output Filter"),
            overridable=True,
            i18n_title=i18n_text(zh_CN="替换文字", en_US="Replacement Text"),
            i18n_description=i18n_text(
                zh_CN="命中片段或整条消息要换成的文字；留空按 **Filter** 处理",
                en_US="Text used to replace matched fragments or the whole message; empty means **Filter**",
            ),
        ).model_dump(),
    )
    OUTPUT_FILTER_WORDS: str = Field(
        default="",
        title="过滤词",
        description=(
            "一行一个；空行和以 # 开头的行忽略；也可以整段粘贴一个 JSON 字符串数组。"
            "匹配时不分全半角、大小写，忽略插在字之间的空白和零宽字符。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="输出过滤", en_US="Output Filter"),
            overridable=True,
            is_textarea=True,
            i18n_title=i18n_text(zh_CN="过滤词", en_US="Filter Words"),
            i18n_description=i18n_text(
                zh_CN=(
                    "一行一个；空行和以 # 开头的行忽略；也可以整段粘贴一个 JSON 字符串数组。"
                    "匹配时不分全半角、大小写，忽略插在字之间的空白和零宽字符。"
                ),
                en_US=(
                    "One per line; blank lines and lines starting with # are ignored. A JSON string array "
                    "pasted as a whole is also accepted. Matching is case-insensitive, normalizes full-width "
                    "and half-width forms, and ignores whitespace and zero-width characters inserted between "
                    "characters."
                ),
            ),
        ).model_dump(),
    )
    OUTPUT_FILTER_REGEX: str = Field(
        default="",
        title="过滤正则",
        description=(
            "一行一条；空行和以 # 开头的行忽略。正则匹配的是归一化之后的文字（全角转半角、转小写、"
            "去掉空白与零宽字符），所以不要在正则里写空白。太复杂的正则会被拒绝，原因见「命中统计」。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="输出过滤", en_US="Output Filter"),
            overridable=True,
            is_textarea=True,
            i18n_title=i18n_text(zh_CN="过滤正则", en_US="Filter Regex"),
            i18n_description=i18n_text(
                zh_CN=(
                    "一行一条；空行和以 # 开头的行忽略。正则匹配的是归一化之后的文字（全角转半角、转小写、"
                    "去掉空白与零宽字符），所以不要在正则里写空白。太复杂的正则会被拒绝，原因见「命中统计」。"
                ),
                en_US=(
                    "One per line; blank lines and lines starting with # are ignored. Regexes run against "
                    "normalized text (full-width folded to half-width, lowercased, whitespace and zero-width "
                    "characters removed), so do not write whitespace in them. Overly complex regexes are "
                    "rejected; see Hit Statistics for the reason."
                ),
            ),
        ).model_dump(),
    )
    OUTPUT_FILTER_BEFORE_MEMORY: bool = Field(
        default=False,
        title="记忆沉淀前过滤",
        description=(
            "打开后（且输出过滤本身开着），长期记忆沉淀写入的段落、摘要、实体与关系先过一遍过滤："
            "段落与摘要里命中的片段换成替换文字，命中的实体与关系直接丢弃。"
            "只影响打开之后新写的记忆，不改已存的；不影响注入。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="输出过滤", en_US="Output Filter"),
            overridable=True,
            i18n_title=i18n_text(zh_CN="记忆沉淀前过滤", en_US="Filter Before Memory"),
            i18n_description=i18n_text(
                zh_CN=(
                    "打开后（且输出过滤本身开着），长期记忆沉淀写入的段落、摘要、实体与关系先过一遍过滤："
                    "段落与摘要里命中的片段换成替换文字，命中的实体与关系直接丢弃。"
                    "只影响打开之后新写的记忆，不改已存的；不影响注入。"
                ),
                en_US=(
                    "When enabled (and output filtering itself is on), paragraphs, summaries, entities and "
                    "relations written by long-term memory consolidation are filtered first: matched fragments "
                    "in paragraphs and summaries become the replacement text, and matched entities and relations "
                    "are dropped. Only affects memories written after enabling; existing memories are unchanged, "
                    "and injection is not filtered."
                ),
            ),
        ).model_dump(),
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
        title="每日回复上限（自限）",
        description=(
            "每个频道每天最多回复多少个回合（一轮发几条消息都只算一次），0 表示不限制。"
            "按额度时区的零点换天。这是在平台方额度以内自己再调低的那一档"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="额度",
                en_US="Quota",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="每日回复上限（自限）",
                en_US="Daily Reply Limit (Self Limit)",
            ),
            i18n_description=i18n_text(
                zh_CN=(
                    "每个频道每天最多回复多少个回合（一轮发几条消息都只算一次），0 表示不限制。"
                    "按额度时区的零点换天。这是在平台方额度以内自己再调低的那一档"
                ),
                en_US=(
                    "Maximum reply rounds per channel per day (a round counts once no matter how many "
                    "messages it sends), 0 = unlimited. The day resets at midnight in the quota timezone. "
                    "This is the lower self-imposed ceiling inside the platform quota"
                ),
            ),
        ).model_dump(),
    )
    AI_CHAT_ENABLE_HOURLY_LIMIT: bool = Field(
        default=False,
        title="启用每小时限额",
        description="启用后将根据每日限额自动计算每小时回复回合上限，使回复更均匀分布（一轮回复拆成几条消息也只算一次）",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="额度",
                en_US="Quota",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="启用每小时限额",
                en_US="Enable Hourly Limit",
            ),
            i18n_description=i18n_text(
                zh_CN="启用后将根据每日限额自动计算每小时回复回合上限，使回复更均匀分布（一轮回复拆成几条消息也只算一次）",
                en_US=(
                    "Auto-calculate the hourly reply-round limit from the daily limit for even distribution "
                    "(a round counts once no matter how many messages it sends)"
                ),
            ),
        ).model_dump(),
    )
    AI_CHAT_QUOTA_WHITELIST_USERS: List[str] = Field(
        default=[],
        title="配额白名单用户",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="额度", en_US="Quota"),
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
            i18n_category=i18n_text(zh_CN="额度", en_US="Quota"),
            i18n_title=i18n_text(zh_CN="管理员不受配额限制", en_US="Super Users Exempt from Quota"),
            i18n_description=i18n_text(
                zh_CN="启用后，SUPER_USERS 中的管理员发送的消息不受配额限制",
                en_US="When enabled, messages from SUPER_USERS bypass quota limits",
            ),
        ).model_dump(),
    )
    # 频道每日额度只在全局出现的三项。策略本身（开关、单位、额度、超额动作、提示语）
    # 存在 `channel_quota_policy` 表里，理由见 `models/db_channel_quota.py` 的模块文档：
    # 平台方的承诺不能落在租户可写的覆盖层上。下面三项不标 `overridable`，同理。
    CHANNEL_QUOTA_TIMEZONE: str = Field(
        default="Asia/Shanghai",
        title="频道额度日界时区",
        description=(
            "频道每日额度按这个时区的零点换天，留空或时区名非法时回落 Asia/Shanghai。"
            "回复上限（自限）与临时加额也按它换天"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="额度", en_US="Quota"),
            i18n_title=i18n_text(zh_CN="频道额度日界时区", en_US="Channel Quota Day Boundary Timezone"),
            i18n_description=i18n_text(
                zh_CN=(
                    "频道每日额度按这个时区的零点换天，留空或时区名非法时回落 Asia/Shanghai。"
                    "回复上限（自限）与临时加额也按它换天"
                ),
                en_US=(
                    "Channel daily quota resets at midnight in this timezone; falls back to Asia/Shanghai. "
                    "The reply limit (self limit) and temporary boosts reset with it too"
                ),
            ),
        ).model_dump(),
    )
    CHANNEL_QUOTA_TOKEN_SCOPE: str = Field(
        default="all",
        title="频道额度 token 计入范围",
        description=(
            "all = 账本里记在该频道的全部调用都计入额度；"
            "reply_only = 只计回复主链路与上下文规划（chat、history_plan），"
            "其余调用照样记账、照样在后台按场景显示，但不计入额度、暂停时也不停"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="额度", en_US="Quota"),
            i18n_title=i18n_text(zh_CN="频道额度 token 计入范围", en_US="Channel Quota Token Scope"),
            i18n_description=i18n_text(
                zh_CN=(
                    "all = 账本里记在该频道的全部调用都计入额度；"
                    "reply_only = 只计回复主链路与上下文规划（chat、history_plan），"
                    "其余调用照样记账、照样在后台按场景显示，但不计入额度、暂停时也不停"
                ),
                en_US=(
                    "all = every call booked to this channel counts toward the quota; "
                    "reply_only = only the reply pipeline and history planning (chat, history_plan) count. "
                    "Other calls are still booked and shown by scene, but do not count and keep running while paused"
                ),
            ),
        ).model_dump(),
    )
    CHANNEL_QUOTA_ALERT_PERCENTS: List[int] = Field(
        default=[80, 100],
        title="频道额度告警阈值",
        description="用量达到这些百分比时各告警一次（当天去重）",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="额度", en_US="Quota"),
            i18n_title=i18n_text(zh_CN="频道额度告警阈值", en_US="Channel Quota Alert Thresholds"),
            i18n_description=i18n_text(
                zh_CN="用量达到这些百分比时各告警一次（当天去重）",
                en_US="Send one alert each time usage reaches these percentages (deduplicated per day)",
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
                zh_CN=(
                    "启用后 AI 会以流式请求方式返回响应，可能解决某些 LLM 请求异常的问题。"
                    "Token 统计依赖供应商支持 stream_options.include_usage：支持时与非流式一样准确；"
                    "不支持时该次记为 0，按 Token 计的频道额度与用量费用会少算"
                ),
                en_US=(
                    "AI will use streaming mode for responses, may fix some LLM request issues. "
                    "Token counts depend on the provider supporting stream_options.include_usage: "
                    "as accurate as non-stream mode if it does; recorded as 0 if it does not, "
                    "so token-based chat quotas and usage costs are undercounted"
                ),
            ),
        ).model_dump(),
        description=(
            "启用后 AI 会以流式请求方式返回响应，再合并解析，这可能解决某些 LLM 请求异常的问题。"
            "Token 统计依赖供应商支持 stream_options.include_usage：支持时与非流式一样准确；"
            "不支持时该次记为 0，按 Token 计的频道额度与用量费用会少算"
        ),
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
                zh_CN=(
                    "仅在启用流式请求时生效；从发出请求起算（包括等待响应头的时间），"
                    "若在指定时间内一个流式数据块都没收到（任何数据块都算，包括空块），"
                    "则判定本次请求失败并进入后续重试"
                ),
                en_US=(
                    "Only effective when stream mode is enabled; counted from when the request is sent "
                    "(including the wait for response headers). If no stream chunk at all arrives within "
                    "this time (any chunk counts, empty ones included), the request fails and enters subsequent retries"
                ),
            ),
        ).model_dump(),
        description=(
            "仅在启用流式请求时生效。从发出请求起算（包括等待响应头的时间），"
            "若供应商在指定时间内一个流式数据块都没返回（任何数据块都算，包括空块），"
            "则立即判定本次请求失败并进入后续重试，用于减少长时间无响应等待。"
        ),
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

    AI_CHAT_BLOCKED_RETRY_MESSAGES: int = Field(
        default=6,
        ge=0,
        le=32,
        title="被拦后重发带几条消息",
        description="一轮的第一次请求被模型上游按内容拦下时，只带最近这么多条消息、不带记忆 / 摘要 / 好感 / 关键词世界书再发一次；0 = 不重发，直接提示被拦",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="聊天配置",
                en_US="Chat Configuration",
            ),
            overridable=True,
            i18n_title=i18n_text(
                zh_CN="被拦后重发带几条消息",
                en_US="Messages to Keep on a Blocked Retry",
            ),
            i18n_description=i18n_text(
                zh_CN="一轮的第一次请求被模型上游按内容拦下时，只带最近这么多条消息、不带记忆 / 摘要 / 好感 / 关键词世界书再发一次；0 = 不重发，直接提示被拦",
                en_US="When the first request of a round is blocked by the model provider for content, resend once with only the most recent messages and without memory, summaries, affection, or keyword-matched world info. 0 disables the retry and reports the block directly",
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
        description="该目录用于 NA 向 OneBot 协议端上传资源文件时，指定文件访问的路径使用。NA 交给协议端发送的文件都放在数据目录的 uploads 下：协议端运行在 Docker 容器中时，把 NA 数据目录下的 uploads 只读挂载到容器里本目录下的 uploads 即可，不要把整个数据目录挂给它",
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
                zh_CN="NA 向 OneBot 协议端上传资源文件时使用的路径；协议端只需能读到其中的 uploads（只读挂载即可）",
                en_US="Path for NA to upload resources to OneBot server; the server only needs read access to its uploads subdirectory",
            ),
        ).model_dump(),
    )

    """CC Workspace 配置"""
    # 默认用本 fork 自建的 cc-sandbox 镜像（公开 fork Akiyo-dayo/nekro-cc-sandbox）：Claude Code、
    # Node 与浏览器组件都钉了版本，tini 当 1 号进程；ak4 起强制取消后的下一条消息不再空返回，
    # `.claude.json` 也留在挂载的 ~/.claude 里。标签固定不变、从不发 latest；
    # 回滚就改回上一版 `0.1.14-ak3-cc2.1.311`（或上游的 `kromiose/nekro-cc-sandbox` / `0.1.14`）并重建容器
    CC_SANDBOX_IMAGE: str = Field(
        default="ghcr.io/akiyo-dayo/nekro-cc-sandbox",
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
        default="0.1.14-ak4-cc2.1.311",
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
    RESOURCE_PAYLOAD_AT_REST: Literal["encrypted", "plain"] = Field(
        default="encrypted",
        title="工作区资源敏感字段落库形态",
        description=(
            "工作区资源敏感字段的落库形态。默认 encrypted（密文）。"
            "只有在要换回不支持加密的旧版本之前才改成 plain 并重启一次："
            "启动时会把密文整批写回明文 JSON。"
        ),
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(
                zh_CN="沙盒配置",
                en_US="Sandbox Configuration",
            ),
            i18n_title=i18n_text(
                zh_CN="工作区资源敏感字段落库形态",
                en_US="Workspace Resource Secret Payload Storage Format",
            ),
            i18n_description=i18n_text(
                zh_CN=(
                    "工作区资源敏感字段的落库形态。默认 encrypted（密文）。"
                    "只有在要换回不支持加密的旧版本之前才改成 plain 并重启一次："
                    "启动时会把密文整批写回明文 JSON。"
                ),
                en_US=(
                    "Storage format for workspace resource secrets. Defaults to encrypted. "
                    "Only switch to plain and restart once before rolling back to an older "
                    "version that cannot read envelopes: startup rewrites all ciphertext "
                    "back to plaintext JSON."
                ),
            ),
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

    """运维工具配置

    与 `OPS_BOT_*` 同理，本组配置**全部不标记 `overridable`**：一旦允许频道级覆盖，
    租户就能在自己的频道里把运维工具打开，等于绕过总后台的权限体系。
    运维工具只认全局配置，且只在平台方实例的运维群 / 私聊里生效。
    """
    OPS_AGENT_TOOLS_ENABLED: bool = Field(
        default=False,
        title="启用运维工具",
        description="启用后，运维人员在平台方实例的运维群或私聊里可以让 Bot 调用只读运维工具；关闭时模型完全看不到这些工具",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="运维工具", en_US="Ops Tools"),
            i18n_title=i18n_text(zh_CN="启用运维工具", en_US="Enable Ops Tools"),
            i18n_description=i18n_text(
                zh_CN="启用后，运维人员在平台方实例的运维群或私聊里可以让 Bot 调用只读运维工具；关闭时模型完全看不到这些工具",
                en_US="When enabled, operators can let the bot call read-only ops tools in an ops group or private chat on a platform-owned instance. When disabled the model never sees these tools at all.",
            ),
        ).model_dump(),
    )
    OPS_AGENT_OPERATORS: List[str] = Field(
        default=[],
        title="运维工具人员白名单",
        description="填写账号（QQ 号），格式与超级用户名单相同。留空时沿用运维 Bot 的人员白名单，避免已配置的部署重配",
        json_schema_extra=ExtraField(
            sub_item_name="账号",
            i18n_category=i18n_text(zh_CN="运维工具", en_US="Ops Tools"),
            i18n_title=i18n_text(zh_CN="运维工具人员白名单", en_US="Ops Tool Operator Allowlist"),
            i18n_description=i18n_text(
                zh_CN="填写账号（QQ 号），格式与超级用户名单相同。留空时沿用运维 Bot 的人员白名单，避免已配置的部署重配",
                en_US="Account IDs (QQ numbers) in the same format as the super user list. When empty, the ops bot operator allowlist is reused so existing deployments need no reconfiguration.",
            ),
        ).model_dump(),
    )

    """CC 工作区 SSH 网关配置

    凭据本身不进容器，由 NA 内置的网关替 CC 登录服务器。默认关：网关的监听、
    提示词与授权记账都不参与，存量 SSH 资源照旧注入。
    """
    SSH_GATEWAY_ENABLED: bool = Field(
        default=False,
        title="启用 SSH 网关",
        description="SSH 网关总开关：让 CC 工作区经 NA 连服务器，凭据不进容器；后台打开或关掉 5 秒内生效，不用重启。默认关",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="CC 工作区", en_US="CC Workspace"),
            i18n_title=i18n_text(zh_CN="启用 SSH 网关", en_US="Enable SSH Gateway"),
            i18n_description=i18n_text(
                zh_CN="SSH 网关总开关：让 CC 工作区经 NA 连服务器，凭据不进容器；后台打开或关掉 5 秒内生效，不用重启。默认关",
                en_US=(
                    "Master switch for the SSH gateway: let CC workspaces reach servers through NA "
                    "without putting credentials inside the container. Changes take effect within "
                    "5 seconds without a restart. Off by default."
                ),
            ),
        ).model_dump(),
    )
    SSH_GATEWAY_OWNER_ACCOUNTS: List[str] = Field(
        default=[],
        title="平台方服务器所有者名单",
        description="能用平台方服务器的所有者本人，格式同超级用户名单；空 = 谁都不能用",
        json_schema_extra=ExtraField(
            sub_item_name="账号",
            i18n_category=i18n_text(zh_CN="CC 工作区", en_US="CC Workspace"),
            i18n_title=i18n_text(zh_CN="平台方服务器所有者名单", en_US="Platform Server Owner Allowlist"),
            i18n_description=i18n_text(
                zh_CN="能用平台方服务器的所有者本人，格式同超级用户名单；空 = 谁都不能用",
                en_US=(
                    "Platform owners who may use platform-owned servers, in the same format as the "
                    "super user list. Empty means nobody can use them."
                ),
            ),
        ).model_dump(),
    )
    SSH_GATEWAY_USER_ACCOUNTS: List[str] = Field(
        default=[],
        title="用户服务器白名单",
        description="能用自己服务器、能在聊天里登记服务器的用户白名单，格式同超级用户名单；空 = 用户服务器功能关",
        json_schema_extra=ExtraField(
            sub_item_name="账号",
            i18n_category=i18n_text(zh_CN="CC 工作区", en_US="CC Workspace"),
            i18n_title=i18n_text(zh_CN="用户服务器白名单", en_US="User Server Allowlist"),
            i18n_description=i18n_text(
                zh_CN="能用自己服务器、能在聊天里登记服务器的用户白名单，格式同超级用户名单；空 = 用户服务器功能关",
                en_US=(
                    "Users who may use their own servers and register servers in chat, in the same "
                    "format as the super user list. Empty disables the user-server feature."
                ),
            ),
        ).model_dump(),
    )
    SSH_GATEWAY_LEASE_MAX_SECONDS: int = Field(
        default=7200,
        title="SSH 授权最长秒数",
        description="一次 CC 任务对服务器的授权最长多久（秒）",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="CC 工作区", en_US="CC Workspace"),
            i18n_title=i18n_text(zh_CN="SSH 授权最长秒数", en_US="SSH Lease Max Seconds"),
            i18n_description=i18n_text(
                zh_CN="一次 CC 任务对服务器的授权最长多久（秒）",
                en_US="How long one CC task may keep its server authorization, in seconds.",
            ),
        ).model_dump(),
    )
    SSH_GATEWAY_AUDIT_RETENTION_DAYS: int = Field(
        default=180,
        title="SSH 审计保留天数",
        description="SSH 审计记录保留天数；0 = 不清理",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="CC 工作区", en_US="CC Workspace"),
            i18n_title=i18n_text(zh_CN="SSH 审计保留天数", en_US="SSH Audit Retention Days"),
            i18n_description=i18n_text(
                zh_CN="SSH 审计记录保留天数；0 = 不清理",
                en_US="How many days to keep SSH audit records; 0 disables cleanup.",
            ),
        ).model_dump(),
    )
    SSH_GATEWAY_PORT: int = Field(
        default=2222,
        title="SSH 网关端口",
        description="网关在工作区网桥上的监听端口",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="CC 工作区", en_US="CC Workspace"),
            i18n_title=i18n_text(zh_CN="SSH 网关端口", en_US="SSH Gateway Port"),
            i18n_description=i18n_text(
                zh_CN="网关在工作区网桥上的监听端口",
                en_US="The port the gateway listens on inside workspace bridge networks.",
            ),
        ).model_dump(),
    )
    SSH_GATEWAY_MAX_CONNECTIONS: int = Field(
        default=32,
        title="SSH 网关连接数上限",
        description="所有工作区同时在用的容器侧连接数上限",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="CC 工作区", en_US="CC Workspace"),
            i18n_title=i18n_text(zh_CN="SSH 网关连接数上限", en_US="SSH Gateway Max Connections"),
            i18n_description=i18n_text(
                zh_CN="所有工作区同时在用的容器侧连接数上限",
                en_US="Maximum number of concurrent container-side connections across all workspaces.",
            ),
        ).model_dump(),
    )
    SSH_GATEWAY_MAX_CONNECTIONS_PER_WORKSPACE: int = Field(
        default=8,
        title="每工作区 SSH 连接数上限",
        description="同一个工作区同时在用的容器侧连接数上限",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="CC 工作区", en_US="CC Workspace"),
            i18n_title=i18n_text(zh_CN="每工作区 SSH 连接数上限", en_US="SSH Gateway Max Connections per Workspace"),
            i18n_description=i18n_text(
                zh_CN="同一个工作区同时在用的容器侧连接数上限",
                en_US="Maximum number of concurrent container-side connections for one workspace.",
            ),
        ).model_dump(),
    )
    SSH_GATEWAY_MAX_CHANNELS: int = Field(
        default=10,
        title="每连接 SSH 通道数上限",
        description="每个容器侧连接最多同时开几个通道",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="CC 工作区", en_US="CC Workspace"),
            i18n_title=i18n_text(zh_CN="每连接 SSH 通道数上限", en_US="SSH Gateway Max Channels per Connection"),
            i18n_description=i18n_text(
                zh_CN="每个容器侧连接最多同时开几个通道",
                en_US="Maximum number of concurrent channels on one container-side connection.",
            ),
        ).model_dump(),
    )
    SSH_GATEWAY_MAX_CHANNELS_TOTAL: int = Field(
        default=64,
        title="全局 SSH 通道数上限",
        description="所有连接合计的通道数上限，决定网关内存上界",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="CC 工作区", en_US="CC Workspace"),
            i18n_title=i18n_text(zh_CN="全局 SSH 通道数上限", en_US="SSH Gateway Max Total Channels"),
            i18n_description=i18n_text(
                zh_CN="所有连接合计的通道数上限，决定网关内存上界",
                en_US="Maximum number of channels across all connections; this bounds gateway memory use.",
            ),
        ).model_dump(),
    )
    SSH_GATEWAY_MAX_CHANNELS_PER_WORKSPACE: int = Field(
        default=16,
        title="每工作区 SSH 通道数上限",
        description="同一个工作区所有连接合计同时开着的通道数上限，免得一个容器占满全局名额；0 表示不限",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="CC 工作区", en_US="CC Workspace"),
            i18n_title=i18n_text(zh_CN="每工作区 SSH 通道数上限", en_US="SSH Gateway Max Channels per Workspace"),
            i18n_description=i18n_text(
                zh_CN="同一个工作区所有连接合计同时开着的通道数上限，免得一个容器占满全局名额；0 表示不限",
                en_US=(
                    "Maximum number of channels open at once across all connections of one workspace, "
                    "so that one container cannot take every slot of the global limit; 0 means no limit."
                ),
            ),
        ).model_dump(),
    )
    SSH_GATEWAY_MAX_BYTES_PER_SEC: int = Field(
        default=16777216,
        title="SSH 网关带宽上限",
        description="全局带宽上限（字节/秒），两个方向分别算；0 = 不限",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="CC 工作区", en_US="CC Workspace"),
            i18n_title=i18n_text(zh_CN="SSH 网关带宽上限", en_US="SSH Gateway Bandwidth Limit"),
            i18n_description=i18n_text(
                zh_CN="全局带宽上限（字节/秒），两个方向分别算；0 = 不限",
                en_US="Global bandwidth limit in bytes per second, applied separately per direction; 0 means unlimited.",
            ),
        ).model_dump(),
    )
    SSH_GATEWAY_UPSTREAM_IDLE_SECONDS: int = Field(
        default=60,
        title="SSH 上游空闲关闭秒数",
        description="复用的上游连接空闲多久后关闭（秒）",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="CC 工作区", en_US="CC Workspace"),
            i18n_title=i18n_text(zh_CN="SSH 上游空闲关闭秒数", en_US="SSH Upstream Idle Seconds"),
            i18n_description=i18n_text(
                zh_CN="复用的上游连接空闲多久后关闭（秒）",
                en_US="How many seconds a reused upstream connection may stay idle before it is closed.",
            ),
        ).model_dump(),
    )
    SSH_GATEWAY_AUDIT_INPUT_BYTES: int = Field(
        default=65536,
        title="SSH 审计输入记录字节数",
        description="每个通道记录容器到服务器方向的前多少字节输入；0 = 不记",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="CC 工作区", en_US="CC Workspace"),
            i18n_title=i18n_text(zh_CN="SSH 审计输入记录字节数", en_US="SSH Audit Input Bytes"),
            i18n_description=i18n_text(
                zh_CN="每个通道记录容器到服务器方向的前多少字节输入；0 = 不记",
                en_US="How many leading container-to-server bytes to record per channel; 0 disables recording.",
            ),
        ).model_dump(),
    )
    SSH_GATEWAY_LOOP_LAG_MS: int = Field(
        default=200,
        title="SSH 网关让路阈值",
        description="事件循环延迟超过多少毫秒时网关暂停转发让路",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="CC 工作区", en_US="CC Workspace"),
            i18n_title=i18n_text(zh_CN="SSH 网关让路阈值", en_US="SSH Gateway Loop Lag Threshold"),
            i18n_description=i18n_text(
                zh_CN="事件循环延迟超过多少毫秒时网关暂停转发让路",
                en_US="Event-loop lag in milliseconds above which the gateway pauses forwarding to yield.",
            ),
        ).model_dump(),
    )
    SSH_GATEWAY_ALLOW_WITHOUT_ISOLATION: bool = Field(
        default=False,
        title="允许隔离未生效时使用网关",
        description="只给开发机：工作区网络隔离没生效时也提供网关。默认关",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="CC 工作区", en_US="CC Workspace"),
            i18n_title=i18n_text(zh_CN="允许隔离未生效时使用网关", en_US="Allow Gateway Without Isolation"),
            i18n_description=i18n_text(
                zh_CN="只给开发机：工作区网络隔离没生效时也提供网关。默认关",
                en_US="Development machines only: serve the gateway even when workspace network isolation is off. Off by default.",
            ),
        ).model_dump(),
    )
    # 值守模式：平台方服务器可以标成「值守」，平台方实例上有人报故障就能让 Bot 自己连上去处理。四项默认值都不改变现有行为；
    # 与上面各项一样按前缀 `SSH_GATEWAY_` 只给平台方读写（`routers/config.py`）
    SSH_GATEWAY_DEFAULT_DUTY_MODE: str = Field(
        default="strict",
        title="平台方服务器默认模式",
        description="服务器上没单独设模式时取这个：strict（严格，只认所有者本人）或 duty（值守）。默认 strict",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="CC 工作区", en_US="CC Workspace"),
            i18n_title=i18n_text(zh_CN="平台方服务器默认模式", en_US="Default Platform Server Mode"),
            i18n_description=i18n_text(
                zh_CN=(
                    "服务器上没单独设模式时取这个：strict（严格，只认所有者本人叫醒的一轮）或 duty（值守：平台方实例上"
                    "有人报故障，Bot 就能自己连这台服务器去查、去修）。其它取值按 strict。默认 strict"
                ),
                en_US=(
                    "Used when a server has no mode of its own: strict (only rounds woken by the owners) or duty "
                    "(anyone who reaches the bot on a platform instance can have it connect to fix things). Any other "
                    "value counts as strict. Default strict."
                ),
            ),
        ).model_dump(),
    )
    SSH_GATEWAY_DUTY_INSTANCES: List[str] = Field(
        default=[],
        title="能触发值守的平台方实例",
        description="写成 适配器:实例；空 = 所有平台方实例。只能收窄：租户实例写进来也不生效",
        json_schema_extra=ExtraField(
            sub_item_name="实例",
            i18n_category=i18n_text(zh_CN="CC 工作区", en_US="CC Workspace"),
            i18n_title=i18n_text(zh_CN="能触发值守的平台方实例", en_US="Instances That May Trigger Duty Mode"),
            i18n_description=i18n_text(
                zh_CN=(
                    "哪些平台方实例的频道能让值守服务器被连，条目写成 适配器:实例（例如 onebot_v11:inst1）；"
                    "空 = 所有平台方实例。只能收窄：租户实例写进来也不生效。不含冒号的条目永远匹配不上"
                    "（删掉、划走名单里最后一个实例时会留下 !none，表示一个都不给；别把它当垃圾删掉，"
                    "删了就成了所有平台方实例。加上真实条目时它会自动去掉）"
                ),
                en_US=(
                    "Which platform instances' chats may make duty-mode servers usable, as adapter:instance "
                    "(e.g. onebot_v11:inst1). Empty means every platform instance. This only narrows: tenant "
                    "instances listed here still do not count. Entries without a colon never match. When the last "
                    "listed instance is deleted or transferred, the list becomes !none, meaning no instance at all; "
                    "deleting !none would allow every platform instance. Adding a real entry removes it automatically."
                ),
            ),
        ).model_dump(),
    )
    SSH_GATEWAY_DUTY_REPORT_CHAT_KEYS: List[str] = Field(
        default=[],
        title="值守摘要发到哪些会话",
        description="值守用过服务器后的摘要发到这些会话；空 = 私聊发给所有者名单里同一适配器的账号",
        json_schema_extra=ExtraField(
            sub_item_name="会话标识",
            i18n_category=i18n_text(zh_CN="CC 工作区", en_US="CC Workspace"),
            i18n_title=i18n_text(zh_CN="值守摘要发到哪些会话", en_US="Duty Report Chats"),
            i18n_description=i18n_text(
                zh_CN="值守用过服务器后的摘要发到这些会话；空 = 私聊发给所有者名单里同一适配器的账号",
                en_US=(
                    "Chats that receive the report after a duty-mode server was used. Empty sends it privately "
                    "to the owner accounts on the same adapter."
                ),
            ),
        ).model_dump(),
    )
    SSH_GATEWAY_RUNBOOK_IN_PROMPT_CHARS: int = Field(
        default=8000,
        title="任务正文里运维手册的总字数",
        description="拍到授权时随任务正文带的运维手册合计最多多少字；0 = 不带",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="CC 工作区", en_US="CC Workspace"),
            i18n_title=i18n_text(zh_CN="任务正文里运维手册的总字数", en_US="Runbook Characters in Task Text"),
            i18n_description=i18n_text(
                zh_CN="拍到授权时随任务正文带的运维手册合计最多多少字，超了按服务器均分截断；0 = 不带",
                en_US=(
                    "Maximum total characters of runbooks attached to the task text when servers are authorized; "
                    "split evenly across servers when over. 0 attaches none."
                ),
            ),
        ).model_dump(),
    )

    @field_validator("SSH_GATEWAY_DUTY_INSTANCES", mode="after")
    @classmethod
    def _normalize_duty_instances(cls, value: List[str]) -> List[str]:
        """加载配置时规整值守实例名单（写入时由 `normalize_config_value` 规整）"""
        return normalize_duty_instances(value)

    @classmethod
    def normalize_config_value(cls, key: str, value: Any) -> Any:
        """后台写入一项之前的规整（`ConfigService.parse_config_value` 调）；没有规整规则的原样返回

        值守实例名单：平台方往里加实例时自动去掉占位 `!none`，不让它和真实条目并存。
        """
        if key == "SSH_GATEWAY_DUTY_INSTANCES":
            return normalize_duty_instances(value)
        return value

    OPS_AGENT_GROUPS: List[str] = Field(
        default=[],
        title="运维工具群白名单",
        description='填写完整的会话标识，如 "onebot_v11-inst1-group_123456"。留空时沿用运维 Bot 的运维频道（仅当该频道配置完整）',
        json_schema_extra=ExtraField(
            sub_item_name="会话标识",
            i18n_category=i18n_text(zh_CN="运维工具", en_US="Ops Tools"),
            i18n_title=i18n_text(zh_CN="运维工具群白名单", en_US="Ops Tool Group Allowlist"),
            i18n_description=i18n_text(
                zh_CN='填写完整的会话标识，如 "onebot_v11-inst1-group_123456"。留空时沿用运维 Bot 的运维频道（仅当该频道配置完整）',
                en_US='Full chat keys such as "onebot_v11-inst1-group_123456". When empty, the ops bot channel is reused, but only if that channel configuration is complete.',
            ),
        ).model_dump(),
    )
    OPS_AGENT_PRIVATE_ENABLED: bool = Field(
        default=True,
        title="允许私聊使用运维工具",
        description="私聊里只认白名单中的人；关闭后运维工具仅在运维群里生效",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="运维工具", en_US="Ops Tools"),
            i18n_title=i18n_text(zh_CN="允许私聊使用运维工具", en_US="Allow Ops Tools in Private Chats"),
            i18n_description=i18n_text(
                zh_CN="私聊里只认白名单中的人；关闭后运维工具仅在运维群里生效",
                en_US="Private chats are accepted only for allowlisted people. When disabled, ops tools work in ops groups only.",
            ),
        ).model_dump(),
    )
    OPS_AGENT_WRITE_LIMIT_PER_ROUND: int = Field(
        default=5,
        title="单轮写操作上限",
        description="一个回合内允许执行的写操作次数，超出后本轮剩余的写调用一律拒绝",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="运维工具", en_US="Ops Tools"),
            i18n_title=i18n_text(zh_CN="单轮写操作上限", en_US="Write Operations per Round"),
            i18n_description=i18n_text(
                zh_CN="一个回合内允许执行的写操作次数，超出后本轮剩余的写调用一律拒绝",
                en_US="How many write operations one round may execute. Further write calls in that round are refused.",
            ),
        ).model_dump(),
    )
    OPS_AGENT_WRITE_LIMIT_PER_HOUR: int = Field(
        default=30,
        title="每小时写操作上限",
        description="同一个人一小时内允许执行的写操作次数，防止一句话里被诱导做大批量改动",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="运维工具", en_US="Ops Tools"),
            i18n_title=i18n_text(zh_CN="每小时写操作上限", en_US="Write Operations per Hour"),
            i18n_description=i18n_text(
                zh_CN="同一个人一小时内允许执行的写操作次数，防止一句话里被诱导做大批量改动",
                en_US="How many write operations one person may execute within an hour, so a single sentence cannot be steered into a bulk change.",
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

    AFFECTION_MILESTONE_ENABLED: bool = Field(
        default=True,
        title="关系里程碑",
        description=(
            "关系阶段跨过门槛时自动记一条长期记忆，并在好感信息里带上最近一条。可在全局、实例、频道三层设置，"
            "下层覆盖上层；只在频道的「启用聊天记忆」也生效时写入"
        ),
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_category=i18n_text(zh_CN="好感度系统", en_US="Affection System"),
            i18n_title=i18n_text(zh_CN="关系里程碑", en_US="Relationship Milestones"),
            i18n_description=i18n_text(
                zh_CN=(
                    "关系阶段跨过门槛时自动记一条长期记忆，并在好感信息里带上最近一条。可在全局、实例、频道三层设置，"
                    "下层覆盖上层；只在频道的「启用聊天记忆」也生效时写入"
                ),
                en_US=(
                    "Write a long-term memory when the relationship crosses a stage threshold, and carry the latest one "
                    "in the affection block. It can be set at the global, instance and channel levels, each overriding "
                    "the one above; milestones are only written where \"Enable Chat Memory\" is also in effect"
                ),
            ),
        ).model_dump(),
    )
    AFFECTION_MILESTONE_COOLDOWN_HOURS: int = Field(
        default=24,
        ge=1,
        title="里程碑最短间隔（小时）",
        description="同一个人在同一人设下两条关系里程碑之间至少隔多久",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_category=i18n_text(zh_CN="好感度系统", en_US="Affection System"),
            i18n_title=i18n_text(zh_CN="里程碑最短间隔（小时）", en_US="Milestone Cooldown (Hours)"),
            i18n_description=i18n_text(
                zh_CN="同一个人在同一人设下两条关系里程碑之间至少隔多久",
                en_US="Minimum hours between two relationship milestones for the same person under the same preset",
            ),
        ).model_dump(),
    )
    AFFECTION_ABSENCE_HINT_DAYS: int = Field(
        default=7,
        ge=0,
        title="久别提示（天）",
        description=(
            "一个人隔了这么多天再出现时，好感信息里带上多久没见。按最近一次有好感记录的互动算"
            "（后台评估或模型自己调分时更新），不是最近一次说话；0 表示不带"
        ),
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_category=i18n_text(zh_CN="好感度系统", en_US="Affection System"),
            i18n_title=i18n_text(zh_CN="久别提示（天）", en_US="Absence Hint (Days)"),
            i18n_description=i18n_text(
                zh_CN=(
                    "一个人隔了这么多天再出现时，好感信息里带上多久没见。按最近一次有好感记录的互动算"
                    "（后台评估或模型自己调分时更新），不是最近一次说话；0 表示不带"
                ),
                en_US=(
                    "When someone shows up again after this many days, the affection block says how long it has "
                    "been. It counts from the last interaction that left an affection record (updated by a "
                    "background evaluation or by the model adjusting the score itself), not from the last time "
                    "they spoke; 0 turns the line off"
                ),
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
    # 配置文件是整份落盘的，改上面 `PLUGIN_ENABLED` 的默认值只对全新安装有效。要「默认启用」的新内置插件
    # 在启动时由 `services/plugin/default_on.py` 加进 `PLUGIN_ENABLED` 一次，并记在这里：
    # 平台方之后关掉它，不会在下次启动时又被打开
    PLUGIN_DEFAULT_ON_APPLIED: List[str] = Field(
        default=[],
        title="已按默认启用过的插件",
        description="新内置插件「默认启用」只生效一次的记录；平台方之后关掉的插件不会被再次打开",
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


def check_tenant_limit_markers(model_cls: Type[ConfigBase]) -> None:
    """「租户只能调低」与「0 表示不设上限」两个标记的导入期校验；不合规就抛 `TypeError`

    写入侧对「租户只能调低」的字段按类型分两支比较前后两档生效值：bool 比的是「开 / 关」，
    int 比的是数值大小。标记一旦落到别的类型上（字符串、字面量枚举……），比较会失去意义、
    判据会静默失效，因此在这里直接让模块导入失败，而不是等运行时才发现。

    「0 表示不设上限」只在「租户只能调低」的判据里有意义（把 0 当作无穷大）；标在别处等于没标，
    而且一定是漏标了另一个：整数上限项没有「只能调低」，租户写 0 就放开了。
    """
    for field_name, field in model_cls.model_fields.items():
        extra = field.json_schema_extra
        if not isinstance(extra, dict):
            continue
        if extra.get("tenant_may_only_lower") and field.annotation not in (bool, int):
            raise TypeError(f"配置项 {field_name} 标了 tenant_may_only_lower，但类型既不是 bool 也不是 int")
        if extra.get("zero_means_unlimited"):
            if field.annotation is not int:
                raise TypeError(f"配置项 {field_name} 标了 zero_means_unlimited，但类型不是 int")
            if not extra.get("tenant_may_only_lower"):
                raise TypeError(f"配置项 {field_name} 标了 zero_means_unlimited，但没有同时标 tenant_may_only_lower")


check_tenant_limit_markers(CoreConfig)


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
