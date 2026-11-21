
"""通用消息服务

这个服务负责处理跨平台的消息发送逻辑，包含所有与具体协议端无关的通用处理。
协议端特定的逻辑通过 adapter.forward_message 接口委托给各自的适配器实现。
"""

import asyncio
from pathlib import Path
from typing import List, Optional, Union

from nekro_agent.adapters.interface.base import BaseAdapter
from nekro_agent.adapters.interface.schemas.platform import (
    PlatformSendRequest,
    PlatformSendResponse,
    PlatformSendSegment,
    PlatformSendSegmentType,
)
from nekro_agent.adapters.utils import adapter_utils
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.schemas.agent_message import (
    AgentMessageSegment,
    AgentMessageSegmentType,
)
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.schemas.errors import InstanceDisabledError
from nekro_agent.services.agent.resolver import fix_raw_response
from nekro_agent.services.output_filter.engine import filter_text
from nekro_agent.services.output_filter.service import (
    filter_send_segments,
    outbound_filtered,
)
from nekro_agent.services.tavern import variable_blocks
from nekro_agent.services.tavern.cleanup_runtime import (
    apply_cleanup,
    builtin_strip_enabled,
    extract_variable_blocks,
    is_card_preset_chat,
    mask_reasoning_regions,
    needs_card_mode,
    rules_for_chat,
    strip_variable_markup,
)
from nekro_agent.services.tavern.html_text import display_html_to_text, has_display_html
from nekro_agent.tools.common_util import download_file
from nekro_agent.tools.message_id import normalize_ref_msg_id
from nekro_agent.tools.path_convertor import (
    convert_to_host_path,
    is_url_path,
)
from nekro_agent.tools.sandbox_shared_files import host_file_is_regular, snapshot_for_handoff

logger = get_sub_logger("message_pipeline")


def _display_to_text(chat_key: str, text: str) -> str:
    """展示标签转纯文字；出任何错都原样返回（与卡片规则「异常返回原文」同一个口径），日志不带正文"""
    try:
        return display_html_to_text(text)
    except Exception as error:  # noqa: BLE001 - 转纯文字是附加步骤，不能让这条消息发不出去
        logger.warning(f"展示标签转纯文字失败，原文照发: chat_key={chat_key}, error={type(error).__name__}")
        return text


class UniversalChatService:
    """通用消息服务 - 处理跨平台的消息发送逻辑"""

    def __init__(self):
        pass

    async def send_operation_message(self, chat_key: str, message: str):
        """发送操作消息（通用方法）

        Args:
            chat_key (str): 聊天频道标识
            message (str): 操作消息内容
        """

        from nekro_agent.services.config_resolver import config_resolver

        adapter = await adapter_utils.get_adapter_for_chat(chat_key)
        effective_config = await config_resolver.get_effective_config(chat_key)
        await self.send_agent_message(
            chat_key=chat_key,
            messages=f"{effective_config.AI_COMMAND_OUTPUT_PREFIX} {message}".strip(),
            adapter=adapter,
        )

    async def send_agent_message(
        self,
        chat_key: str,
        messages: Union[List[AgentMessageSegment], str],
        adapter: BaseAdapter,
        ctx: Optional[AgentCtx] = None,
        file_mode: bool = False,
        record: bool = False,
        ref_msg_id: Optional[str] = None,
    ):
        """发送机器人消息

        Args:
            chat_key (str): 聊天的唯一标识
            messages (Union[List[AgentMessageSegment], str]): 机器人消息
            adapter (BaseAdapter): 适配器实例
            ctx (Optional[AgentCtx], optional): 机器人上下文. Defaults to None.
            file_mode (bool, optional): 是否为文件发送模式（影响文件类型的消息段生成）. Defaults to False.
            record (bool, optional): 是否记录聊天记录. Defaults to False.

        Raises:
            ValueError: 聊天类型错误
        """
        await self._assert_instance_active(chat_key)

        if isinstance(messages, str):
            messages = [AgentMessageSegment(type=AgentMessageSegmentType.TEXT, content=messages)]

        # 预处理消息段
        processed_segments = await self._preprocess_messages(messages, chat_key, ctx, file_mode)

        # 酒馆卡片的清理规则只作用于 AI 回复（带 `ctx` 的发送）与开场白：后台手动发、
        # 公告、命令输出都不带 `ctx`，不清理。位置必须在输出过滤之前，过滤看到的是
        # 清理后的最终文字。
        # 有变量块接收方登记、这次发送带着本轮令牌时，清理顺带取出会被剥掉的更新块（见 `variable_blocks`）；
        # 没登记或拿不到令牌时不取（块要按令牌归到这一轮，没有令牌交出去也没人取得走）。
        blocks: Optional[List[str]] = (
            [] if ctx is not None and variable_blocks.has_sinks() and getattr(ctx, "one_time_code", "") else None
        )
        if ctx is not None:
            processed_segments, messages = await self._apply_cleanup(
                chat_key, processed_segments, messages, blocks_out=blocks
            )

        # 出站过滤放在预处理之后：此时文字已经是最终要发出去的那一版
        before_filter = processed_segments
        processed_segments, messages = await self._apply_output_filter(chat_key, processed_segments, messages)
        if blocks and processed_segments is not before_filter:
            # 被输出过滤改过的这次发送，里面的变量块不交
            blocks = None

        # 检查是否有有效的消息段
        if not processed_segments:
            if blocks and ctx is not None:
                # 剥完只剩变量块：没有文字可发，块照样交（要不要用由接收方按这一轮的结局决定）
                await self._deliver_blocks(ctx, chat_key, blocks)
            logger.warning("Empty Message, skip sending")
            if config.DEBUG_IN_CHAT:
                # 发送调试消息
                debug_request = PlatformSendRequest(
                    chat_key=chat_key,
                    segments=[PlatformSendSegment(type=PlatformSendSegmentType.TEXT, content="[Debug] 无消息回复")],
                )
                with outbound_filtered():
                    await adapter.forward_message(debug_request)
            return

        # 构建协议端发送请求
        ref_msg_id = normalize_ref_msg_id(ref_msg_id)
        send_request = PlatformSendRequest(
            chat_key=chat_key,
            segments=processed_segments,
            ref_msg_id=ref_msg_id,
        )

        # 发送消息
        try:
            with outbound_filtered():
                plt_response: PlatformSendResponse = await adapter.forward_message(send_request)
        except Exception as e:
            logger.exception(f"发送消息失败: {e}")
            if config.DEBUG_IN_CHAT:
                debug_request = PlatformSendRequest(
                    chat_key=chat_key,
                    segments=[
                        PlatformSendSegment(
                            type=PlatformSendSegmentType.TEXT,
                            content="[Debug] 发送消息失败，请检查协议端状态",
                        ),
                    ],
                )
                with outbound_filtered():
                    await adapter.forward_message(debug_request)
            else:
                raise

        if not plt_response.success:
            logger.error(f"适配器发送消息失败，错误: {plt_response.error_message}")
            raise ValueError(f"适配器发送消息失败，错误: {plt_response.error_message}")

        if blocks and ctx is not None:
            await self._deliver_blocks(ctx, chat_key, blocks)

        # 记录聊天记录
        # 适配器已自行写入历史（`recorded=True`，如网页适配器的 forward_message）时不再重复写一条
        if record and not plt_response.recorded:
            from nekro_agent.services.message_service import message_service

            try:
                await message_service.push_bot_message(
                    chat_key,
                    messages,
                    plt_response,
                    ref_msg_id=ref_msg_id,
                    normalize_at_markup=False,
                )
            except TypeError as exc:
                if "normalize_at_markup" not in str(exc):
                    raise
                logger.warning("push_bot_message 运行时钩子不兼容 normalize_at_markup，已使用旧签名重试")
                await message_service.push_bot_message(
                    chat_key,
                    messages,
                    plt_response,
                    ref_msg_id=ref_msg_id,
                )

    async def _deliver_blocks(self, ctx: AgentCtx, chat_key: str, blocks: List[str]) -> None:
        """把这次发送取到的变量块交给接收方；任何失败都不影响这次发送（日志不带原文）

        带上这个频道的实例与是不是私聊，接收方据此判断自己在这里生效没有、要不要收（只在取到块时多查这一次）。
        """
        try:
            channel = getattr(ctx, "db_chat_channel", None)
            if channel is None or getattr(channel, "chat_key", None) != chat_key:
                channel = await DBChatChannel.filter(chat_key=chat_key).order_by("id").first()
            if channel is None:
                return
            payload = variable_blocks.VariableBlocks(
                one_time_code=str(ctx.one_time_code or ""),
                from_chat_key=str(ctx.from_chat_key or ctx.chat_key or ""),
                chat_key=chat_key,
                blocks=tuple(blocks),
                instance_key=str(channel.instance_key or ""),
                is_private=channel.chat_type == ChatType.PRIVATE,
            )
            await variable_blocks.deliver(payload)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            logger.warning(f"交变量块失败: chat_key={chat_key}, error={type(error).__name__}")

    async def _assert_instance_active(self, chat_key: str) -> None:
        """出站总闸：该会话所属实例被停用时拒绝发送

        这里是所有主动出站的必经之路——WebUI 手动发消息、公告群发、运维 Bot 告警推送、
        定时任务与插件/沙盒的 `api.message.send_*`、以及命令输出，全都收敛到
        `send_agent_message`。放在这一层而不是各个调用点，是为了让「停用」对**将来新增的**
        出站路径也默认成立。

        频道行查不到时放行：那种 chat_key 本来就发不出去，下游会给出更准确的报错，
        在这里抢先报「实例已停用」只会误导排查。
        """
        if await DBAdapterInstance.is_chat_key_active(chat_key):
            return
        channel = await DBChatChannel.filter(chat_key=chat_key).order_by("id").first()
        instance_key = channel.instance_key if channel else ""
        logger.warning(f"实例或其所属租户已停用，拒绝出站发送: chat_key={chat_key}, instance_key={instance_key}")
        raise InstanceDisabledError(instance_key=instance_key or chat_key)

    async def _preprocess_messages(
        self,
        messages: List[AgentMessageSegment],
        chat_key: str,
        ctx: Optional[AgentCtx],
        file_mode: bool,
    ) -> List[PlatformSendSegment]:
        """预处理消息段，将 AgentMessageSegment 转换为 PlatformSendSegment

        Args:
            messages: 原始的 Agent 消息段列表
            chat_key: 聊天频道标识
            ctx: 上下文
            file_mode: 是否为文件模式（用于决定 FILE 类型文件生成 FILE 还是 IMAGE 消息段）

        Returns:
            List[PlatformSendSegment]: 预处理后的平台消息段列表
        """
        processed_segments: List[PlatformSendSegment] = []

        for agent_message in messages:
            content = agent_message.content
            agent_message_type = AgentMessageSegmentType(agent_message.type)

            if agent_message_type == AgentMessageSegmentType.TEXT:
                # 处理文本消息 - 只做基本的修复，不解析@
                content = fix_raw_response(content)
                agent_message.content = content

                if content.strip():
                    processed_segments.append(PlatformSendSegment(type=PlatformSendSegmentType.TEXT, content=content))

                logger.info(f"Sending agent message: {content}")

            elif agent_message_type in (
                AgentMessageSegmentType.IMAGE,
                AgentMessageSegmentType.FILE,
            ):
                segment_type = (
                    PlatformSendSegmentType.IMAGE
                    if agent_message_type == AgentMessageSegmentType.IMAGE
                    else (PlatformSendSegmentType.FILE if file_mode else PlatformSendSegmentType.IMAGE)
                )
                processed_segment = await self._preprocess_file_segment(
                    agent_message=agent_message,
                    chat_key=chat_key,
                    ctx=ctx,
                    content=content,
                    segment_type=segment_type,
                )
                processed_segments.extend(processed_segment)
            else:
                raise ValueError(f"Invalid agent message type: {agent_message.type}")

        return processed_segments

    async def _apply_cleanup(
        self,
        chat_key: str,
        processed_segments: List[PlatformSendSegment],
        messages: List[AgentMessageSegment],
        blocks_out: Optional[List[str]] = None,
    ) -> tuple[List[PlatformSendSegment], List[AgentMessageSegment]]:
        """按频道生效人设的卡片规则清理出站文字，并同步改写聊天记录

        只在带 `ctx` 的发送里调用。发送段与要记进聊天记录的那份必须是同一份清理后的
        文字：只改一边会让历史里留着一条从未发出去的原话。文本段的对应关系按预处理时
        的先后顺序配对；`_preprocess_messages` 只会给非空文本生成发送段，空文本在配对时
        跳过。清理后为空的文本段去掉，全去掉时沿用现有的「Empty Message」路径。

        先做内置的变量标记剥离（`<UpdateVariable>` 之类，不依赖卡片规则，跟着酒馆兼容的
        回滚闸），再跑卡片规则，最后对酒馆卡人设把展示用的 HTML 标签转成纯文字
        （`tavern.html_text`）：顺序见 `cleanup_runtime.strip_variable_markup`。

        `blocks_out` 不为空时（有变量块接收方登记），在剥离之前把会被剥掉的更新块原文取出来放进去：
        只对酒馆兼容开着、并且生效人设来自酒馆卡的频道取，取不出来不影响清理与发送。
        """
        rules = await rules_for_chat(chat_key)
        strip = builtin_strip_enabled()
        if rules is None and not strip:
            return processed_segments, messages
        # 「是不是从酒馆卡导入的人设」只在普通剥离之后还剩变量标签、或者清理之后还有展示用的 HTML
        # 标签时才需要知道，一次发送最多查一次。有清理规则不代表是卡片人设：任何人设都能在扩展页里加规则。
        card_preset: list[bool] = []

        async def is_card() -> bool:
            if not card_preset:
                card_preset.append(await is_card_preset_chat(chat_key))
            return card_preset[0]

        async def collect(text: str) -> None:
            # 先在内存里取（没有变量标签时立刻返回），取到了才查一次「是不是卡片人设」。思考与分析区域先在整条
            # 消息上遮掉（与 MVU 一样）：包在思考里的块不算数。只影响取块，发出去的文字照常剥离。
            if blocks_out is None:
                return
            try:
                found = extract_variable_blocks(mask_reasoning_regions(text), card_mode=True)
                if found and await is_card():
                    blocks_out.extend(found)
            except Exception as error:  # noqa: BLE001 - 取块是附加步骤，不能让这条消息发不出去
                logger.warning(f"取变量更新块失败，这次不交: chat_key={chat_key}, error={type(error).__name__}")

        async def clean(text: str, *, take_blocks: bool = False) -> str:
            result = text
            if strip:
                if take_blocks and blocks_out is not None:
                    await collect(text)
                result = strip_variable_markup(text)
                if needs_card_mode(result) and await is_card():
                    result = strip_variable_markup(text, card_mode=True)
            if rules is not None:
                result = await apply_cleanup(chat_key, result, rules)
            if strip and has_display_html(result) and await is_card():
                result = _display_to_text(chat_key, result)
            return result

        cleaned_by_index: dict[int, str] = {}
        pending: List[tuple[str, str]] = []
        for index, message in enumerate(messages):
            if AgentMessageSegmentType(message.type) != AgentMessageSegmentType.TEXT:
                continue
            cleaned = await clean(message.content, take_blocks=True)
            cleaned_by_index[index] = cleaned
            pending.append((message.content, cleaned))

        pending_pos = 0
        cleaned_segments: List[PlatformSendSegment] = []
        for segment in processed_segments:
            if PlatformSendSegmentType(segment.type) != PlatformSendSegmentType.TEXT:
                cleaned_segments.append(segment)
                continue
            # 空白文本不生成发送段，配对时跳过它们，否则会错位到下一段文字。
            while pending_pos < len(pending) and not pending[pending_pos][0].strip():
                pending_pos += 1
            if pending_pos < len(pending) and pending[pending_pos][0] == segment.content:
                cleaned = pending[pending_pos][1]
                pending_pos += 1
            else:
                # 例如无效文件路径生成的文本段，它不是某条文本消息的发送段。
                cleaned = await clean(segment.content)
            if cleaned.strip():
                cleaned_segments.append(
                    segment.model_copy(update={"content": cleaned}) if cleaned != segment.content else segment,
                )

        cleaned_messages: List[AgentMessageSegment] = []
        for index, message in enumerate(messages):
            if AgentMessageSegmentType(message.type) != AgentMessageSegmentType.TEXT:
                cleaned_messages.append(message)
                continue
            cleaned = cleaned_by_index[index]
            if not cleaned.strip():
                continue
            cleaned_messages.append(
                message.model_copy(update={"content": cleaned}) if cleaned != message.content else message,
            )
        return cleaned_segments, cleaned_messages

    async def _apply_output_filter(
        self,
        chat_key: str,
        processed_segments: List[PlatformSendSegment],
        messages: List[AgentMessageSegment],
    ) -> tuple[List[PlatformSendSegment], List[AgentMessageSegment]]:
        """对出站消息执行输出过滤，并同步改写要记进聊天记录的文本

        聊天记录必须与实际发出去的文字一致：只改发送段、不改 `messages` 会让历史里留着
        一条从未发出去的原话。fragment 模式下两边各自跑一次同一个引擎（不重复计数），
        whole 模式则把历史也换成那一条替换文字。
        """
        filtered_segments, settings, hits = await filter_send_segments(chat_key, processed_segments)
        if filtered_segments is processed_segments:
            return processed_segments, messages
        if settings is None:
            return filtered_segments, messages
        if settings.mode == "whole" or hits.get("error:engine"):
            return filtered_segments, [
                AgentMessageSegment(type=AgentMessageSegmentType.TEXT, content=settings.replacement),
            ]

        filtered_messages: List[AgentMessageSegment] = []
        for message in messages:
            if AgentMessageSegmentType(message.type) == AgentMessageSegmentType.TEXT:
                filtered_text = filter_text(
                    message.content,
                    settings.rules,
                    settings.replacement,
                    protect_at_markup=True,
                ).text
                filtered_messages.append(message.model_copy(update={"content": filtered_text}))
            else:
                filtered_messages.append(message)
        return filtered_segments, filtered_messages

    async def _preprocess_file_segment(
        self,
        agent_message: AgentMessageSegment,
        chat_key: str,
        ctx: Optional[AgentCtx],
        content: str,
        segment_type: PlatformSendSegmentType,
    ) -> List[PlatformSendSegment]:
        """预处理图片/文件类消息段。"""
        try:
            if is_url_path(content):
                file_path, _ = await download_file(content, from_chat_key=chat_key)
                processed_file_path = file_path
                agent_message.content = str(file_path)
            else:
                host_path = convert_to_host_path(
                    Path(content),
                    chat_key=chat_key,
                    container_key=ctx.container_key if ctx else None,
                )

                # 沙盒共享目录里的文件：不把原路径交给适配器（判过归属之后沙盒还能把它换成链接），
                # 先逐级不跟随链接地复制一份到上传目录，交出去的是副本
                if host_path and await asyncio.to_thread(host_file_is_regular, host_path):
                    handoff_path = await asyncio.to_thread(snapshot_for_handoff, host_path, chat_key)
                    processed_file_path = str(handoff_path)
                    agent_message.content = str(handoff_path)
                else:
                    logger.warning(f"Invalid or non-existent file path: {content}")
                    return [
                        PlatformSendSegment(
                            type=PlatformSendSegmentType.TEXT,
                            content=f"Invalid file path: {content}",
                        ),
                    ]

            logger.info(f"Sending agent file: {processed_file_path}")
            return [PlatformSendSegment(type=segment_type, file_path=processed_file_path)]
        except ValueError as e:
            logger.error(f"Path conversion error: {e}")
            return [PlatformSendSegment(type=PlatformSendSegmentType.TEXT, content=str(e))]


# 全局实例
universal_chat_service = UniversalChatService()
