from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Generic, List, Optional, Tuple, Type, TypeVar, cast

from fastapi import APIRouter
from jinja2 import Environment
from nonebot import logger
from pydantic import BaseModel, Field

from nekro_agent.core.core_utils import ConfigBase, ExtraField

if TYPE_CHECKING:
    from nekro_agent.schemas.agent_message import AgentMessageSegment
    from nekro_agent.services.command.manager import UserPermissionSource
    from nekro_agent.services.command.schemas import CommandResponse
from nekro_agent.core import config
from nekro_agent.core.os_env import OsEnv
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.schemas.i18n import i18n_text
from nekro_agent.services.agent.templates.base import PromptTemplate, register_template
from nekro_agent.services.command.base import CommandPermission

from .schemas.platform import (
    PlatformChannel,
    PlatformMessage,
    PlatformSendRequest,
    PlatformSendResponse,
    PlatformUser,
)


class AdapterMetadata(BaseModel):
    """适配器元数据"""

    name: str
    description: str
    version: str = "1.0.0"
    author: str = ""
    homepage: str = ""
    tags: List[str] = []
class BaseAdapterConfig(ConfigBase):
    """适配器配置基类"""

    ENABLED: bool = Field(
        default=False,
        title="启用适配器",
        description="关闭后该适配器不会在启动时加载，修改后需要重启应用生效",
        json_schema_extra=ExtraField(
            is_need_restart=True,
            i18n_category=i18n_text(zh_CN="基础设置", en_US="Basic Settings"),
            i18n_title=i18n_text(zh_CN="启用适配器", en_US="Enable Adapter"),
            i18n_description=i18n_text(
                zh_CN="关闭后该适配器不会在启动时加载，修改后需要重启应用生效",
                en_US="When disabled, this adapter will not be loaded on startup. Restart the application after changes.",
            ),
        ).model_dump(),
    )

    SESSION_ENABLE_AT: bool = Field(
        default=True,
        title="启用 @用户 功能",
        description="关闭后 AI 发送的 @用户 消息将被解析为纯文本用户名，避免反复打扰用户",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="交互", en_US="Interaction"),
            i18n_title=i18n_text(zh_CN="启用 @用户 功能", en_US="Enable @User Mention"),
            i18n_description=i18n_text(
                zh_CN="关闭后 AI 发送的 @用户 消息将被解析为纯文本用户名，避免反复打扰用户",
                en_US="When disabled, @user mentions sent by AI will be converted to plain text usernames to avoid repeatedly disturbing users.",
            ),
        ).model_dump(),
    )
    SESSION_PROCESSING_WITH_EMOJI: bool = Field(
        default=True,
        title="显示处理中表情反馈",
        description="当 AI 开始处理消息时，对应消息会显示处理中表情反馈，需要适配器支持此能力",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="交互", en_US="Interaction"),
            i18n_title=i18n_text(zh_CN="显示处理中表情反馈", en_US="Show Processing Emoji Feedback"),
            i18n_description=i18n_text(
                zh_CN="当 AI 开始处理消息时，对应消息会显示处理中表情反馈，需要适配器支持此能力",
                en_US="When AI starts processing a message, a processing emoji reaction will be shown on the corresponding message. This requires adapter support.",
            ),
        ).model_dump(),
    )
    # 命令系统配置
    COMMAND_PREFIX: str = Field(
        default="/",
        title="命令前缀",
        description="触发命令的前缀字符，如 / 或 !",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="命令", en_US="Commands"),
            i18n_title=i18n_text(zh_CN="命令前缀", en_US="Command Prefix"),
            i18n_description=i18n_text(
                zh_CN="触发命令的前缀字符，如 / 或 !",
                en_US="Prefix character used to trigger commands, such as / or !.",
            ),
        ).model_dump(),
    )
    COMMAND_ENABLED: bool = Field(
        default=True,
        title="启用命令系统",
        description="关闭后该适配器不再识别和处理命令",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="命令", en_US="Commands"),
            i18n_title=i18n_text(zh_CN="启用命令系统", en_US="Enable Command System"),
            i18n_description=i18n_text(
                zh_CN="关闭后该适配器不再识别和处理命令",
                en_US="When disabled, this adapter will stop recognizing and processing commands.",
            ),
        ).model_dump(),
    )
    COMMAND_UNAUTHORIZED_OUTPUT: bool = Field(
        default=True,
        title="权限不足提示",
        description="权限不足时是否向用户输出提示信息",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="命令", en_US="Commands"),
            i18n_title=i18n_text(zh_CN="权限不足提示", en_US="Show Permission Denied Notice"),
            i18n_description=i18n_text(
                zh_CN="权限不足时是否向用户输出提示信息",
                en_US="Whether to output a notice to users when permissions are insufficient.",
            ),
        ).model_dump(),
    )
    COMMAND_ENHANCED_OUTPUT: bool = Field(
        default=False,
        title="命令增强输出",
        description="启用后，较长的命令输出将使用平台特性进行优化展示（如合并转发、卡片等）",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="命令", en_US="Commands"),
            i18n_title=i18n_text(zh_CN="命令增强输出", en_US="Enhanced Command Output"),
            i18n_description=i18n_text(
                zh_CN="启用后，较长的命令输出将使用平台特性进行优化展示（如合并转发、卡片等）",
                en_US="When enabled, longer command outputs will use platform-specific optimized display methods such as forward messages or cards.",
            ),
        ).model_dump(),
    )
    COMMAND_ENHANCED_OUTPUT_MIN_LENGTH: int = Field(
        default=200,
        title="增强输出触发字数",
        description="命令输出超过此字数时触发增强输出（需启用命令增强输出）",
        json_schema_extra=ExtraField(
            i18n_category=i18n_text(zh_CN="命令", en_US="Commands"),
            i18n_title=i18n_text(zh_CN="增强输出触发字数", en_US="Enhanced Output Threshold"),
            i18n_description=i18n_text(
                zh_CN="命令输出超过此字数时触发增强输出（需启用命令增强输出）",
                en_US="Enhanced output will be triggered when command output exceeds this length. Requires enhanced command output to be enabled.",
            ),
        ).model_dump(),
    )


# 定义配置类型变量，约束为 BaseAdapterConfig 子类
TConfig = TypeVar("TConfig", bound=BaseAdapterConfig)
T = TypeVar("T", bound="BaseAdapter")


class BaseAdapter(ABC, Generic[TConfig]):
    """适配器基类"""

    _router: APIRouter  # 实例变量的类型注解
    _Configs: Type[TConfig]
    _config: TConfig

    def __init__(self, config_cls: Type[TConfig] = BaseAdapterConfig):
        self._Configs = config_cls
        self._adapter_config_path = Path(OsEnv.DATA_DIR) / "configs" / self.key / "config.yaml"
        self._config = self.get_config(self._Configs)

        # 注册配置到统一配置系统
        from nekro_agent.core.core_utils import ConfigManager

        ConfigManager.register_config(f"adapter_{self.key}", self._config)

    @property
    @abstractmethod
    def key(self) -> str:
        """适配器唯一标识"""
        raise NotImplementedError

    @property
    @abstractmethod
    def metadata(self) -> AdapterMetadata:
        """适配器元数据"""
        raise NotImplementedError

    def get_adapter_router(self) -> APIRouter:
        """获取适配器路由"""
        return APIRouter()

    @property
    def router(self) -> APIRouter:
        """获取适配器路由"""
        if hasattr(self, "_router"):
            return self._router
        self._router = self.get_adapter_router()
        return self._router

    def get_config(self, config_cls: Type[TConfig]) -> TConfig:
        """获取适配器配置"""
        if not hasattr(self, "_config"):
            self._config = self._Configs.load_config(file_path=self._adapter_config_path)
            self._config.dump_config(self._adapter_config_path)
        return cast(config_cls, self._config)

    @property
    def config(self) -> TConfig:
        """获取适配器配置"""
        return self.get_config(self._Configs)

    @property
    def config_path(self) -> Path:
        """获取适配器配置路径"""
        return self._adapter_config_path

    def cast(self, adapter_type: Type[T]) -> T:
        """转换适配器类型"""
        return cast(adapter_type, self)

    @property
    def chat_key_rules(self) -> List[str]:
        return [
            "Group chat: `platform-group_123456` (where 123456 is the group number)",
            "Private chat: `platform-private_123456` (where 123456 is the user's QQ number)",
        ]

    @property
    def record_command_input(self) -> bool:
        """是否在命令消息被命令系统消费前，将用户输入原文写入聊天历史。"""
        return False

    def get_docs_path(self) -> Path:
        """获取适配器文档路径"""
        return Path(__file__).parent.parent / self.key / "README.md"

    @abstractmethod
    async def init(self) -> None:
        """初始化适配器"""
        raise NotImplementedError

    @abstractmethod
    async def cleanup(self) -> None:
        """清理适配器"""
        raise NotImplementedError

    async def set_dialog_example(self) -> Optional[List[PromptTemplate]]:
        """自定义对话示例"""
        return None

    async def get_jinja_env(self) -> Optional[Environment]:
        """返回jinja模板"""
        return None

    async def render_runtime_prompt(self) -> str:
        """渲染适配器级运行时提示词"""
        return ""

    @abstractmethod
    async def forward_message(self, request: PlatformSendRequest) -> PlatformSendResponse:
        """推送消息到协议端

        Args:
            request: 协议端发送请求，包含已经预处理好的消息数据

        Returns:
            PlatformSendResponse: 发送结果
        """
        raise NotImplementedError

    @abstractmethod
    async def get_self_info(self, chat_key: Optional[str] = None) -> PlatformUser:
        """获取自身信息

        Args:
            chat_key: 可选的会话标识，用于多账号适配器定位具体实例；单实例适配器可忽略该参数

        Returns:
            PlatformUser: 自身信息
        """
        raise NotImplementedError

    async def probe_instance(self, instance_key: str) -> str:  # noqa: ARG002
        """对指定实例做一次存活探测

        与 `get_self_info(chat_key=...)` 的差别只有一处：探测跑在后台巡检里，手上有的是
        **实例**而不是频道。因此这里是一条按实例定位连接的独立入口，而不是给
        `get_self_info` 再加一个「二选一」的定位参数，也不是拿实例标识拼一个合成 chat_key
        ——合成 chat_key 的频道段是编出来的，它会顺着 `DBChatChannel.get_channel()`
        这类按会话取行的逻辑扩散出一个并不存在的频道，还会以它为名落进上传目录与沙盒挂载路径。

        默认实现按「整个适配器只有一条连接」处理，探测哪个实例都是同一个答案，与改造前
        `get_self_info()` 完全一致。**多账号适配器必须覆写本方法**，否则会拿别的账号的连接
        回答本实例的问题：连接数不为 1 时不是全员误判离线，就是被在线的那个账号顶替成在线。

        Args:
            instance_key: 目标实例标识

        Returns:
            str: 探测到的自身账号标识，仅用于日志

        Raises:
            Exception: 该实例的连接不可用。任何异常都会被健康监测判定为离线，
                因此实现方**不得**在定位不到连接时返回一个占位结果。
        """
        return (await self.get_self_info()).user_id

    @abstractmethod
    async def get_user_info(self, user_id: str, channel_id: str) -> PlatformUser:
        """获取用户(或者群聊用户)信息"""
        raise NotImplementedError

    @abstractmethod
    async def get_channel_info(self, channel_id: str) -> PlatformChannel:
        """获取频道信息"""
        raise NotImplementedError

    def get_default_channel_status(self, channel_type: ChatType) -> str:
        if channel_type == ChatType.GROUP:
            return "active" if config.SESSION_GROUP_ACTIVE_DEFAULT else "disabled"
        if channel_type == ChatType.PRIVATE:
            return "active" if config.SESSION_PRIVATE_ACTIVE_DEFAULT else "disabled"
        return "active"

    @property
    def supports_webui_send(self) -> bool:
        """是否支持从 WebUI 发送消息（默认不支持，适配器可按需覆盖）"""
        return False

    # 下面三个方法的签名与上游 NekroAgent 2.4.x 逐字一致，框架也只按这个形状调用它们：
    # 照上游写的子类覆写不能因为本分支多传一个参数而报错。框架额外需要的东西
    # （会话标识、按频道的授权）在 `collector` 里补，不往这里加参数。

    async def get_user_command_permission(
        self,
        platform_user: PlatformUser,
        platform_channel: PlatformChannel,
        platform_message: PlatformMessage,
    ) -> CommandPermission:
        """获取平台用户在命令系统中的权限等级。

        默认使用命令管理器中的持久化授权；适配器可覆盖此方法接入平台侧管理员、群主等权限。
        这里返回的 `SUPER_USER` 不会让人成为超级用户——超级用户只认 `SUPER_USERS`，
        `collector` 会把它压成 `ADVANCED`。

        与上游的差别：平台来源（`source="platform"`）的记录在这里不算数。它是某个群里群角色的
        快照，只有适配器自己在**本条消息所在的群**实时识别才作数；照上游写法覆写成
        「是群管理就给 ADVANCED，否则 `super()`」的适配器，若这里认快照，在租户 A 的群里
        当过管理的人到了租户 B 的频道也是频道管理员。
        """
        del platform_channel, platform_message

        from nekro_agent.services.command.manager import USER_PERMISSION_SOURCE_PLATFORM, command_manager

        record = command_manager.get_user_permission_record(self.key, platform_user.user_id)
        if record.source == USER_PERMISSION_SOURCE_PLATFORM:
            return CommandPermission.USER
        return record.permission

    async def set_user_command_permission(
        self,
        platform_userid: str,
        permission: CommandPermission | str,
        *,
        source: "UserPermissionSource" = "manual",
        channel_id: str | None = None,
    ) -> CommandPermission:
        """设置平台用户在命令系统中的权限等级（全局一份，对该适配器的所有频道生效）。"""
        from nekro_agent.services.command.manager import command_manager

        normalized_permission = (
            permission if isinstance(permission, CommandPermission) else CommandPermission(permission)
        )
        await command_manager.set_user_permission(
            self.key,
            platform_userid,
            normalized_permission,
            source=source,
            channel_id=channel_id,
        )
        return normalized_permission

    async def reset_user_command_permission(self, platform_userid: str) -> None:
        """重置平台用户命令权限，回退为普通用户。"""
        from nekro_agent.services.command.manager import command_manager

        await command_manager.reset_user_permission(self.key, platform_userid)

    async def set_message_reaction(self, message_id: str, status: bool = True, chat_key: str = "") -> bool:  # noqa: ARG002
        """设置消息反应（可选实现）

        Args:
            message_id (str): 消息ID
            status (bool): True为设置反应，False为取消反应
            chat_key (str): 会话标识，多账号适配器用它定位归属实例；单实例适配器可忽略

        Returns:
            bool: 是否成功设置
        """
        # 默认实现：不支持消息反应功能
        return False

    # region 辅助方法

    def build_chat_key(self, channel_id: str, instance_key: str = "") -> str:
        """构建聊天标识

        `channel_id` 始终保持干净形态（如 `group_123456`），实例维度只体现在 chat_key 上，
        否则插件里大量既有的 `channel_id.split("_")` 会静默取到错误的频道类型与目标 ID。

        Args:
            channel_id: 频道标识
            instance_key: 适配器实例标识，多账号适配器用它区分同一频道下的不同账号；单实例适配器留空

        Returns:
            str: 聊天标识
        """
        if instance_key:
            return f"{self.key}-{instance_key}-{channel_id}"
        return f"{self.key}-{channel_id}"

    def parse_chat_key(self, chat_key: str) -> Tuple[str, str]:
        """解析聊天标识

        基类无法判断中间段是实例标识还是频道标识的一部分，因此这里保持最朴素的一次切分。
        带实例段的适配器必须覆写本方法剥掉实例段，保证返回的 channel_id 与 `PlatformChannel.channel_id`
        完全一致——第三方插件通过 `_ctx.adapter.parse_chat_key()` 取到的就是这个值。

        Args:
            chat_key: 聊天标识

        Returns:
            Tuple[str, str]: (adapter_key, channel_id)
        """
        parts = chat_key.split("-", 1)

        if len(parts) != 2:
            raise ValueError(f"无效的聊天标识: {chat_key}")

        adapter_key = parts[0]
        channel_id = parts[1]

        return adapter_key, channel_id

    def parse_channel_id(self, chat_key: str) -> str:
        """解析聊天标识中的频道ID"""
        return self.parse_chat_key(chat_key)[1]

    # endregion

    # region 命令系统

    def detect_command(self, text: str) -> Optional[Tuple[str, str]]:
        """检测文本是否为命令，返回 (command_name, raw_args) 或 None"""
        prefix = self.config.COMMAND_PREFIX
        if not text.startswith(prefix):
            return None
        content = text[len(prefix):]
        parts = content.split(None, 1)
        if not parts:
            return None
        return parts[0], parts[1] if len(parts) > 1 else ""

    async def execute_command(
        self,
        chat_key: str,
        user_id: str,
        username: str,
        command_name: str,
        raw_args: str,
        is_super_user: bool = False,
        is_advanced_user: bool = False,
    ) -> Optional[List["CommandResponse"]]:
        """执行命令并消费流式输出 - 自动检查适配器级开关"""
        from nekro_agent.services.command.registry import command_registry
        from nekro_agent.services.command.output import (
            materialize_command_response,
        )
        from nekro_agent.services.command.schemas import (
            CommandExecutionContext,
            CommandRequest,
            CommandResponse,
            CommandResponseStatus,
        )
        from nekro_agent.services.command.wait_manager import wait_manager

        if not self.config.COMMAND_ENABLED:
            return None

        # 用户发送新命令时自动取消挂起的 wait
        wait_manager.cancel(chat_key, user_id)

        from nekro_agent.core.config import config
        from nekro_agent.schemas.i18n import SupportedLang

        context = CommandExecutionContext(
            user_id=user_id,
            chat_key=chat_key,
            username=username,
            adapter_key=self.key,
            is_super_user=is_super_user,
            is_advanced_user=is_advanced_user,
            lang=SupportedLang(config.SYSTEM_LANG),
        )
        request = CommandRequest(context=context, command_name=command_name, raw_args=raw_args)

        from nekro_agent.services.command_output_broadcaster import command_output_broadcaster

        responses: List[CommandResponse] = []
        async for raw_response in command_registry.execute(request):
            response = await materialize_command_response(chat_key, raw_response)
            responses.append(response)
            if response.status == CommandResponseStatus.PROCESSING:
                await self._send_command_response(chat_key, response)
            elif response.status == CommandResponseStatus.WAITING:
                await self._handle_command_wait(chat_key, user_id, response)
            elif response.status in (CommandResponseStatus.SUCCESS, CommandResponseStatus.ERROR):
                await self._send_command_response(chat_key, response)
            elif response.status == CommandResponseStatus.UNAUTHORIZED:
                if self.config.COMMAND_UNAUTHORIZED_OUTPUT:
                    await self._send_command_message(chat_key, response.message)

            # 广播到 WebUI（所有状态均广播，供管理员监控）
            await command_output_broadcaster.publish(
                chat_key=chat_key,
                command_name=command_name,
                status=response.status.value,
                message=response.message,
                output_segments=response.output_segments,
            )
        return responses

    async def try_handle_wait_input(
        self,
        chat_key: str,
        user_id: str,
        username: str,
        text: str,
        is_super_user: bool = False,
        is_advanced_user: bool = False,
    ) -> bool:
        """检查并处理挂起的 wait 交互

        当用户发送非命令消息时调用此方法，检查是否有匹配的 WaitSession。
        若存在，将用户输入路由到 callback_cmd，并返回 True 表示已消费。

        Returns:
            True 表示消息已被 wait 消费，调用方应终止后续处理
        """
        from nekro_agent.services.command.wait_manager import wait_manager

        session = wait_manager.try_consume(chat_key, user_id)
        if not session:
            return False

        # 将 context_data 中的内容作为额外参数拼接到 raw_args
        extra_args = " ".join(f'{k}:"{v}"' for k, v in session.context_data.items()) if session.context_data else ""
        raw_args = f"{text} {extra_args}".strip() if extra_args else text

        await self.execute_command(
            chat_key=chat_key,
            user_id=user_id,
            username=username,
            command_name=session.callback_cmd,
            raw_args=raw_args,
            is_super_user=is_super_user,
            is_advanced_user=is_advanced_user,
        )
        return True

    async def _send_command_message(self, chat_key: str, message: str) -> None:
        """发送命令输出消息到频道

        当启用增强输出且消息长度超过阈值时，尝试使用平台特性发送；
        若平台不支持或发送失败，回退为普通文本。
        """
        if (
            self.config.COMMAND_ENHANCED_OUTPUT
            and len(message) >= self.config.COMMAND_ENHANCED_OUTPUT_MIN_LENGTH
            and await self._try_send_enhanced_command_message(chat_key, message)
        ):
            return

        from nekro_agent.services.chat.universal_chat_service import universal_chat_service

        await universal_chat_service.send_operation_message(
            chat_key=chat_key,
            message=message,
        )

    async def _send_command_response(
        self,
        chat_key: str,
        response: "CommandResponse",
    ) -> None:
        """发送命令响应到频道。"""
        from nekro_agent.services.command.output import (
            build_command_output_messages,
            build_command_output_platform_segments,
        )
        if not response.output_segments:
            await self._send_command_plain_if_any(chat_key, response)
            return

        enhanced_messages = await build_command_output_messages(response, chat_key)
        if enhanced_messages and self.config.COMMAND_ENHANCED_OUTPUT:
            sent = await self._try_send_enhanced_command_response(
                chat_key,
                response,
                enhanced_messages,
            )
            if sent:
                return

        platform_segments = await build_command_output_platform_segments(response, chat_key)
        if platform_segments:
            plt_response = await self.forward_message(
                PlatformSendRequest(chat_key=chat_key, segments=platform_segments),
            )
            if not plt_response.success:
                raise ValueError(f"适配器发送消息失败，错误: {plt_response.error_message}")
            return

        await self._send_command_plain_if_any(chat_key, response)

    async def _send_command_plain_if_any(
        self,
        chat_key: str,
        response: "CommandResponse",
    ) -> None:
        """存在纯文本摘要时发送普通命令消息。"""
        if response.message:
            await self._send_command_message(chat_key, response.message)

    async def _try_send_enhanced_command_message(self, chat_key: str, message: str) -> bool:  # noqa: ARG002
        """尝试以平台增强格式发送命令消息

        子类可重写此方法，利用平台特性（合并转发、卡片等）优化长消息展示。
        返回 True 表示发送成功，False 表示不支持或失败（将回退为普通文本）。
        """
        return False

    async def _try_send_enhanced_command_response(  # noqa: ARG002
        self,
        chat_key: str,
        response: "CommandResponse",
        messages: list["AgentMessageSegment"],
    ) -> bool:
        """尝试以平台增强格式发送富媒体命令响应。

        默认不处理，回退到普通命令发送链路。
        """
        return False

    async def _handle_command_wait(self, chat_key: str, user_id: str, response: "CommandResponse") -> None:
        """处理 wait 状态（发送提示消息并注册 WaitSession）"""
        from nekro_agent.services.command.wait_manager import wait_manager

        # 发送 wait 提示消息（含可选项）
        wait_msg = response.message
        if response.wait_options:
            options_str = " / ".join(response.wait_options)
            wait_msg = f"{wait_msg}\n可选: {options_str}"
        await self._send_command_message(chat_key, wait_msg)

        if response.callback_cmd:
            await wait_manager.create_session(
                chat_key=chat_key,
                user_id=user_id,
                callback_cmd=response.callback_cmd,
                context_data=response.context_data,
                timeout=response.wait_timeout or 60.0,
                on_timeout_message=response.on_timeout_message or "操作超时，已取消",
            )

    @property
    def supports_command_completion(self) -> bool:
        """是否支持命令补全（子类可覆盖）"""
        return False

    async def sync_commands(self, chat_key: Optional[str] = None) -> None:
        """同步命令到平台（子类可覆盖实现 Slash Commands 等）"""

    # endregion
