
#!/usr/bin/env python3

"""
WeChatPad 实时消息处理器
基于WebSocket实现实时消息接收和处理
"""

from __future__ import annotations

import asyncio
import json
import traceback
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable

import websockets

from nekro_agent.adapters.interface.collector import collect_message
from nekro_agent.adapters.interface.schemas.extra import PlatformMessageExt
from nekro_agent.adapters.interface.schemas.platform import (
    PlatformChannel,
    PlatformMessage,
    PlatformUser,
)
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.schemas.chat_message import (
    ChatMessageSegment,
    ChatMessageSegmentImage,
    ChatMessageSegmentType,
    ChatType,
)

from .config import WeChatPadConfig

logger = get_sub_logger("adapter.wechatpad")
if TYPE_CHECKING:
    from .adapter import WeChatPadAdapter


class MessageType(Enum):
    """消息类型枚举"""

    TEXT = 1  # 文本消息
    IMAGE = 3  # 图片消息
    VOICE = 34  # 语音消息
    VIDEO = 43  # 视频消息
    EMOJI = 47  # 表情消息
    LOCATION = 48  # 位置消息
    LINK = 49  # 链接消息
    FILE = 6  # 文件消息
    SYSTEM = 10000  # 系统消息


@dataclass
class WeChatMessage:
    """微信消息数据结构"""

    msg_id: int
    from_user: str
    to_user: str
    msg_type: int
    content: str
    status: int
    create_time: int
    msg_source: str
    push_content: str
    new_msg_id: int
    img_buffer: str = ""  # base64编码的图片/语音数据
    img_status: int = 1  # 图片状态

    @property
    def message_type(self) -> MessageType:
        """获取消息类型枚举"""
        try:
            return MessageType(self.msg_type)
        except ValueError:
            return MessageType.TEXT  # 默认为文本消息

    def _parse_xml_content(self, content: str) -> dict:
        """解析XML格式的消息内容，提取媒体URL"""
        try:
            root = ET.fromstring(content)

            # 检查是否是图片消息
            img_elem = root.find(".//img")
            if img_elem is not None:
                # 尝试获取图片URL
                img_url = (
                    img_elem.get("src")
                    or img_elem.get("cdnurl")
                    or img_elem.get("aeskey")
                )
                if img_url:
                    return {"type": "image", "url": img_url, "data": content}

            # 检查是否是语音消息
            voicemsg_elem = root.find(".//voicemsg")
            if voicemsg_elem is not None:
                voice_url = voicemsg_elem.get("voiceurl") or voicemsg_elem.get(
                    "clientmsgid"
                )
                voice_length = voicemsg_elem.get("voicelength", "0")
                if voice_url:
                    return {
                        "type": "voice",
                        "url": voice_url,
                        "length": voice_length,
                        "data": content,
                    }

            # 检查是否是表情消息
            emoji_elem = root.find(".//emoji")
            if emoji_elem is not None:
                emoji_url = emoji_elem.get("cdnurl") or emoji_elem.get("encrypturl")
                emoji_md5 = emoji_elem.get("md5")
                if emoji_url:
                    return {
                        "type": "emoji",
                        "url": emoji_url,
                        "md5": emoji_md5,
                        "data": content,
                    }

            # 检查是否是文件消息
            appmsg_elem = root.find(".//appmsg")
            if appmsg_elem is not None:
                title_elem = appmsg_elem.find("title")
                url_elem = appmsg_elem.find("url")
                if title_elem is not None and url_elem is not None:
                    return {
                        "type": "file",
                        "title": title_elem.text,
                        "url": url_elem.text,
                        "data": content,
                    }

            # 默认返回原始XML内容
            return {"type": "xml", "data": content}

        except ET.ParseError as e:
            logger.warning(f"XML解析失败: {e}")
            return {"type": "text", "data": content}

    @property
    def create_datetime(self) -> datetime:
        """获取创建时间的datetime对象"""
        return datetime.fromtimestamp(self.create_time)

    @property
    def is_group_message(self) -> bool:
        """判断是否为群消息"""
        return "@chatroom" in self.to_user or "@chatroom" in self.from_user

    @property
    def sender_name(self) -> str:
        """从push_content中提取发送者名称"""
        if " : " in self.push_content:
            return self.push_content.split(" : ")[0]
        return self.from_user

    @property
    def actual_sender_wxid(self) -> str:
        """获取实际发送者的wxid（处理群消息中的真实发送者）"""
        if self.is_group_message and ":" in self.content:
            # 群消息格式: "wxid_xxx:\n消息内容"
            lines = self.content.split("\n", 1)
            if len(lines) > 0 and lines[0].endswith(":"):
                return lines[0][:-1]  # 移除末尾的冒号
        return self.from_user

    @property
    def actual_content(self) -> str:
        """获取实际消息内容（处理群消息格式）"""
        if self.is_group_message and ":" in self.content:
            # 群消息格式: "wxid_xxx:\n消息内容"
            lines = self.content.split("\n", 1)
            if len(lines) > 1 and lines[0].endswith(":"):
                return lines[1]  # 返回实际消息内容
        return self.content

    @property
    def channel_id(self) -> str:
        """获取频道ID（群聊ID或私聊对方ID）"""
        if self.is_group_message:
            # 群消息：使用to_user作为群ID
            return self.to_user
        # 私聊：使用实际发送者ID
        return self.actual_sender_wxid

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式"""
        return {
            "msg_id": self.msg_id,
            "from_user": self.from_user,
            "to_user": self.to_user,
            "msg_type": self.msg_type,
            "message_type": self.message_type.name,
            "content": self.content,
            "status": self.status,
            "create_time": self.create_time,
            "create_datetime": self.create_datetime.isoformat(),
            "msg_source": self.msg_source,
            "push_content": self.push_content,
            "new_msg_id": self.new_msg_id,
            "is_group_message": self.is_group_message,
            "sender_name": self.sender_name,
        }


class MessageHandler:
    """消息处理器基类"""

    async def handle_message(self, message: WeChatMessage) -> bool:
        """
        处理消息
        返回True表示消息已处理，False表示继续传递给下一个处理器
        """
        raise NotImplementedError


class TextMessageHandler(MessageHandler):
    """文本消息处理器"""

    def __init__(self, keywords: list[str] = None, callback: Callable = None):
        self.keywords = keywords or []
        self.callback = callback

    async def handle_message(self, message: WeChatMessage) -> bool:
        if message.message_type != MessageType.TEXT:
            return False

        # 关键词过滤
        if self.keywords and not any(
            keyword in message.content for keyword in self.keywords
        ):
            return False

        logger.info(f"处理文本消息: {message.sender_name} -> {message.content}")

        if self.callback:
            await self.callback(message)

        return True


class GroupMessageHandler(MessageHandler):
    """群消息处理器"""

    def __init__(self, group_ids: list[str] = None, callback: Callable = None):
        self.group_ids = group_ids or []
        self.callback = callback

    async def handle_message(self, message: WeChatMessage) -> bool:
        if not message.is_group_message:
            return False

        # 群ID过滤
        if self.group_ids and not any(
            group_id in message.to_user or group_id in message.from_user
            for group_id in self.group_ids
        ):
            return False

        logger.info(
            f"处理群消息: {message.sender_name} 在群中发送 -> {message.content}"
        )

        if self.callback:
            await self.callback(message)

        return True


class WeChatRealtimeProcessor:
    """微信实时消息处理器"""

    def __init__(
        self, config: WeChatPadConfig, adapter: WeChatPadAdapter | None = None
    ):
        self.config = config
        self.adapter = adapter  # 适配器引用，用于消息转发
        self.handlers: list[MessageHandler] = []
        self.is_running = False
        self.websocket: websockets.WebSocketServerProtocol | None = None
        self.start_time: datetime | None = None
        self.message_count = 0

        # 群名缓存，避免重复API调用
        self._group_name_cache: dict[str, str] = {}

    def add_handler(self, handler: MessageHandler):
        """添加消息处理器"""
        self.handlers.append(handler)
        logger.info(f"添加消息处理器: {handler.__class__.__name__}")

    def remove_handler(self, handler: MessageHandler):
        """移除消息处理器"""
        if handler in self.handlers:
            self.handlers.remove(handler)
            logger.info(f"移除消息处理器: {handler.__class__.__name__}")

    def _build_websocket_url(self) -> str:
        """构建WebSocket URL"""
        base_url = self.config.WECHATPAD_API_URL
        ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url += "/ws/GetSyncMsg"

        # 添加认证参数
        params = {"key": self.config.WECHATPAD_AUTH_KEY}
        ws_url += "?" + urllib.parse.urlencode(params)

        return ws_url

    def _parse_message(self, raw_data: str) -> WeChatMessage | None:
        """解析原始消息数据"""
        try:
            data = json.loads(raw_data)

            # 提取消息字段
            msg_id = data.get("msg_id", 0)
            from_user = data.get("from_user_name", {}).get("str", "")
            to_user = data.get("to_user_name", {}).get("str", "")
            msg_type = data.get("msg_type", 1)
            content = data.get("content", {}).get("str", "")
            status = data.get("status", 0)
            create_time = data.get("create_time", 0)
            msg_source = data.get("msg_source", "")
            push_content = data.get("push_content", "")
            new_msg_id = data.get("new_msg_id", 0)

            # 处理img_buf字段（图片和语音数据）
            img_buf = data.get("img_buf", {})
            img_status = data.get("img_status", 1)
            img_buffer = img_buf.get("buffer", "") if img_buf.get("len", 0) > 0 else ""

            return WeChatMessage(
                msg_id=msg_id,
                from_user=from_user,
                to_user=to_user,
                msg_type=msg_type,
                content=content,
                status=status,
                create_time=create_time,
                msg_source=msg_source,
                push_content=push_content,
                new_msg_id=new_msg_id,
                img_buffer=img_buffer,
                img_status=img_status,
            )

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.error(f"解析消息失败: {e}, 原始数据: {raw_data}")
            return None

    @staticmethod
    def _is_authored_text(message: WeChatMessage) -> bool:
        """这条消息的文字是不是发送者本人打的

        只有 `msg_type` 恰好是 1（文本）才算。其余类型——引用（appmsg 里的 refermsg）、合并转发
        的聊天记录、链接、名片、系统消息，以及 `message_type` 认不出、退回成 TEXT 的类型——
        存进消息段的都是原始 XML，里面可能是别人说的话；按 `ext_data.forwarded` 标上，
        依赖「本人说过」的判据（例如聊天登记服务器的来源证明）不采用它们。
        """
        return int(message.msg_type) == MessageType.TEXT.value

    async def _process_message(self, message: WeChatMessage):
        """处理单条消息"""
        self.message_count += 1

        logger.info(
            f"🔄 处理消息 #{self.message_count}: {message.from_user} -> {message.content[:50]}..."
        )

        # 1. 转发消息到nekro-agent核心系统
        if self.adapter:
            try:
                logger.info("🚀 开始转发消息到nekro-agent核心系统...")
                await self._forward_to_nekro_agent(message)
                logger.info("✅ 消息转发完成")
            except Exception as e:
                logger.error(f"❌ 转发消息到nekro-agent失败: {e}")

        # 2. 依次调用自定义处理器
        for handler in self.handlers:
            try:
                handled = await handler.handle_message(message)
                if handled:
                    logger.debug(f"消息被处理器 {handler.__class__.__name__} 处理")
                    break
            except Exception as e:
                logger.error(f"处理器 {handler.__class__.__name__} 处理消息时出错: {e}")

    async def _forward_to_nekro_agent(self, message: WeChatMessage):
        """转发消息到nekro-agent核心系统"""
        if not self.adapter:
            return

        # 获取自身信息用于判断 is_self
        try:
            self_info = await self.adapter.get_self_info()
            bot_wxid = self_info.user_id
        except Exception as e:
            logger.warning(f"获取自身信息失败，无法判断 is_self: {e}")
            bot_wxid = ""

        # 判断是否为自己发送的消息
        is_self = message.actual_sender_wxid == bot_wxid

        # 如果是自己发送的消息，跳过处理
        if is_self:
            logger.debug(f"跳过自己发送的消息: {message.actual_content[:50]}...")
            return

        # 判断是群聊还是私聊
        is_group = message.is_group_message
        channel_id = message.channel_id
        chat_type = ChatType.GROUP if is_group else ChatType.PRIVATE

        # 判断是否为 @ 消息（仅在群聊中有意义）
        is_tome = False
        if is_group and bot_wxid:
            # 检查消息中是否包含 @ 机器人的内容
            is_tome = (
                f"@{bot_wxid}" in message.actual_content
                or message.actual_content.startswith("@")
            )
        elif not is_group:
            # 私聊消息默认为 @ 机器人
            is_tome = True

        # 构造平台频道信息
        platform_channel = PlatformChannel(
            channel_id=channel_id,
            channel_name=await self._get_group_name(channel_id) if is_group else "",  #
            channel_type=chat_type,
        )

        # 构造平台用户信息
        platform_user = PlatformUser(
            user_id=message.actual_sender_wxid,
            user_name=message.sender_name,
            platform_name="微信",
            user_avatar="",  # 暂时为空，后续可通过 API 获取
        )

        # 构造消息段（根据消息类型处理）
        content_segments = []
        if message.message_type == MessageType.TEXT:
            content_segments.append(
                ChatMessageSegment(
                    type=ChatMessageSegmentType.TEXT,
                    text=message.actual_content,
                )
            )
        elif message.message_type == MessageType.IMAGE:
            # 处理图片消息，参考OneBot V11模式
            logger.debug(f"处理图片消息: {message.actual_content[:100]}...")

            # 优先使用 img_buffer 中的 base64 数据
            if message.img_buffer and message.img_status == 2:
                try:
                    # img_buffer 中包含 base64 编码的图片数据
                    base64_data = f"data:image/jpeg;base64,{message.img_buffer}"
                    logger.debug("使用img_buffer中base64数据创建图片消息段")

                    image_segment = await ChatMessageSegmentImage.create_from_base64(
                        base64_data,
                        from_chat_key=channel_id,
                        file_name="image.jpg",
                    )
                    content_segments.append(image_segment)
                    logger.info("✅ 使用base64数据创建图片消息段成功")
                except Exception as e:
                    logger.warning(f"使用base64数据创建图片消息段失败: {e}")
                    # 回退到XML解析模式
                    await self._handle_image_from_xml_or_api(
                        message, content_segments, channel_id
                    )
            else:
                # 回退到XML解析模式，尝试使用API下载高清图片
                await self._handle_image_from_xml_or_api(
                    message, content_segments, channel_id
                )
        elif message.message_type == MessageType.VOICE:
            # 处理语音消息，参考OneBot V11模式
            logger.debug(f"处理语音消息: {message.actual_content[:100]}...")

            # 优先使用 img_buffer 中的语音数据（如果有）
            if message.img_buffer and message.img_status == 2:
                try:
                    # TODO: 未来可以实现语音文件处理，类似OneBot的文件上传
                    # voice_segment = await ChatMessageSegmentFile.create_from_base64(...)
                    # content_segments.append(voice_segment)

                    # 目前仍然解析XML获取时长信息
                    parsed_xml = self._parse_xml_content(message.actual_content)
                    if parsed_xml["type"] == "voice" and parsed_xml.get("length"):
                        voice_length_ms = int(parsed_xml["length"])
                        voice_length_sec = voice_length_ms / 1000.0
                        content_segments.append(
                            ChatMessageSegment(
                                type=ChatMessageSegmentType.TEXT,
                                text=f"[语音] {voice_length_sec:.1f}秒 (有数据)",
                            )
                        )
                        logger.info(f"✅ 语音消息包含数据: {voice_length_sec:.1f}秒")
                    else:
                        content_segments.append(
                            ChatMessageSegment(
                                type=ChatMessageSegmentType.TEXT,
                                text="[语音] (有数据)",
                            )
                        )
                except Exception as e:
                    logger.warning(f"处理语音数据失败: {e}")
                    # 回退到XML解析模式
                    await self._handle_voice_from_xml_or_api(
                        message, content_segments, channel_id
                    )
            else:
                # 回退到XML解析模式，尝试使用API下载语音文件
                await self._handle_voice_from_xml_or_api(
                    message, content_segments, channel_id
                )
        elif message.message_type == MessageType.EMOJI:
            # 处理表情消息，参考OneBot V11模式
            logger.debug(f"处理表情消息: {message.actual_content[:100]}...")
            parsed_xml = self._parse_xml_content(message.actual_content)

            if parsed_xml["type"] == "emoji" and parsed_xml.get("url"):
                try:
                    # 表情可以作为图片处理，使用CDN URL
                    emoji_url = parsed_xml["url"]
                    logger.debug(f"尝试使用表情URL创建图片消息段: {emoji_url[:50]}...")

                    # 表情通常是GIF或PNG格式
                    suffix = ".gif"  # 表情通常是GIF
                    emoji_segment = await ChatMessageSegmentImage.create_from_url(
                        url=emoji_url,
                        from_chat_key=channel_id,
                        file_name=f"emoji{suffix}",
                        use_suffix=suffix,
                    )
                    content_segments.append(emoji_segment)
                    logger.info("✅ 表情消息段创建成功")
                except Exception as e:
                    logger.warning(f"创建表情消息段失败: {e}")
                    content_segments.append(
                        ChatMessageSegment(
                            type=ChatMessageSegmentType.TEXT,
                            text="[表情]",
                        )
                    )
            else:
                # 回退到文本模式
                content_segments.append(
                    ChatMessageSegment(
                        type=ChatMessageSegmentType.TEXT,
                        text="[表情]",
                    )
                )
        else:
            # 其他类型消息，使用原始内容
            content_segments.append(
                ChatMessageSegment(
                    type=ChatMessageSegmentType.TEXT,
                    text=message.actual_content,
                )
            )

        # 构造平台消息
        platform_message = PlatformMessage(
            message_id=str(message.msg_id),
            sender_id=message.actual_sender_wxid,
            sender_name=message.sender_name,
            sender_nickname=message.sender_name,
            content_data=content_segments,
            content_text=message.actual_content,
            is_tome=is_tome,
            timestamp=message.create_time,
            is_self=is_self,
            ext_data=PlatformMessageExt(forwarded=not self._is_authored_text(message)),
        )

        # 提交消息到收集器
        await collect_message(
            self.adapter, platform_channel, platform_user, platform_message
        )

        logger.info(
            f"实时消息已转发: [{channel_id}] {message.sender_name}: {message.actual_content}"
        )

    async def start(self):
        """开始实时消息处理（带自动重连）"""
        if self.is_running:
            logger.warning("实时消息处理器已在运行")
            return

        self.is_running = True
        self.start_time = datetime.now()
        self.message_count = 0

        # 启动WebSocket连接重试机制
        await self._connect_websocket_with_retry()

        logger.info("🛑 实时消息处理器已完全停止")

    async def _connect_websocket_with_retry(self):
        """WebSocket连接重试机制"""
        ws_url = self._build_websocket_url()
        logger.info(f"🔗 准备连接WebSocket: {ws_url}")

        while self.is_running:
            try:
                await self._connect_and_listen_once(ws_url)
            except Exception as e:
                if not self.is_running:
                    break

                logger.error(f"❌ WebSocket连接失败: {type(e).__name__}: {e}")
                logger.info("⏰ 5秒后尝试重连...")

                # 固定5秒重连间隔
                await asyncio.sleep(5)

    async def _connect_and_listen_once(self, ws_url: str):
        """单次WebSocket连接和监听（超时重连策略）"""
        # 确保先关闭任何现有连接
        if self.websocket:
            try:
                await self.websocket.close()
                logger.info("关闭了现有的WebSocket连接")
            except Exception as e:
                logger.warning(f"关闭现有连接时出错: {e}")
            finally:
                self.websocket = None

        logger.info(f"🔄 正在尝试连接到: {ws_url}")

        # 使用简单的websockets.connect，不设置ping参数
        async with websockets.connect(ws_url) as websocket:
            self.websocket = websocket
            logger.info("✅ WebSocket连接成功！")
            logger.info(f"📊 连接状态: {websocket.state}")
            logger.info(f"🌐 远程地址: {websocket.remote_address}")
            logger.info("🔄 开始接收消息...")

            # 超时重连策略
            wait_time = 120  # 2分钟超时

            while self.is_running:
                try:
                    # 使用超时等待消息
                    raw_message = await asyncio.wait_for(
                        websocket.recv(),
                        timeout=wait_time,
                    )

                    logger.info(f"📨 收到原始消息: {raw_message[:200]}...")

                    # 异步处理消息，不阻塞接收循环
                    asyncio.create_task(self._handle_message_async(raw_message))

                except asyncio.TimeoutError:
                    logger.debug(f"WebSocket 连接空闲超过 {wait_time} 秒，主动重连")
                    break  # 超时后重连
                except websockets.exceptions.ConnectionClosed as e:
                    logger.warning(f"🔌 WebSocket连接已关闭: {e}")
                    break  # 连接关闭，重连
                except Exception as e:
                    logger.error(f"❌ 处理WebSocket消息时发生错误: {e}")
                    break  # 其他错误，重连

            self.websocket = None

    async def _handle_message_async(self, raw_message: str):
        """异步处理单条消息（避免阻塞WebSocket接收）"""
        try:
            # 解析消息
            message = self._parse_message(raw_message)
            if message:
                logger.info(
                    f"✅ 消息解析成功: {message.from_user} -> {message.content[:50]}..."
                )
                # 处理消息
                await self._process_message(message)
            else:
                logger.warning(f"❌ 无法解析消息: {raw_message}")
        except Exception as e:
            logger.error(f"❌ 异步处理消息失败: {e}")

            logger.error(f"堆栈跟踪: {traceback.format_exc()}")

    async def stop(self):
        """停止实时消息处理"""
        if not self.is_running:
            return

        self.is_running = False
        if self.websocket:
            await self.websocket.close()

        logger.info("正在停止实时消息处理器...")

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        if self.start_time:
            running_time = datetime.now() - self.start_time
            messages_per_minute = self.message_count / max(
                running_time.total_seconds() / 60, 1
            )
        else:
            running_time = None
            messages_per_minute = 0

        return {
            "is_running": self.is_running,
            "message_count": self.message_count,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "running_time_seconds": running_time.total_seconds() if running_time else 0,
            "messages_per_minute": round(messages_per_minute, 2),
            "handlers_count": len(self.handlers),
        }

    async def _get_group_name(self, group_id: str) -> str:
        """获取群聊名称，使用缓存机制"""
        # 检查缓存
        if group_id in self._group_name_cache:
            return self._group_name_cache[group_id]

        try:
            # 使用GetChatRoomInfo API获取群聊详细信息
            response = await self.adapter.http_client.get_chatroom_info(group_id)

            if response:
                # 根据API文档，群名应该在返回的数据中
                # 需要根据实际API响应结构调整字段名
                group_name = (
                    response.get("ChatRoomName")
                    or response.get("chatroomName")
                    or response.get("name")
                    or group_id  # 如果都没有，使用群ID作为备选
                )

                # 缓存结果
                self._group_name_cache[group_id] = group_name
                logger.debug(f"获取群名成功: {group_id} -> {group_name}")
                return group_name
            logger.warning(f"获取群聊信息返回空数据: {group_id}")
            return group_id

        except Exception as e:
            logger.warning(f"获取群名失败: {group_id}, 错误: {e}")
            # 缓存失败结果，避免重复尝试
            self._group_name_cache[group_id] = group_id
            return group_id

    async def _handle_image_from_xml_or_api(
        self, message: WeChatMessage, content_segments: list, channel_id: str
    ):
        """处理图片消息：从 XML 解析或使用 API 下载"""
        parsed_xml = self._parse_xml_content(message.actual_content)

        if parsed_xml["type"] == "image":
            # 尝试使用 API 下载高清图片
            if hasattr(message, "msg_id") and message.msg_id and self.adapter:
                try:
                    logger.debug(f"尝试使用API下载高清图片: msg_id={message.msg_id}")

                    # 使用 WeChatPad API 下载高清图片
                    img_data = await self.adapter.http_client.get_msg_big_img(
                        msg_id=message.msg_id,
                        from_user=message.sender_wxid,
                        to_user=message.receiver_wxid,
                        total_len=parsed_xml.get("length", 0) or 100000,  # 默认大小
                    )

                    if img_data and "Data" in img_data:
                        # 将下载的数据转换为 base64
                        import base64

                        base64_data = f"data:image/jpeg;base64,{base64.b64encode(img_data['Data']).decode()}"

                        image_segment = (
                            await ChatMessageSegmentImage.create_from_base64(
                                base64_data,
                                from_chat_key=channel_id,
                                file_name="image.jpg",
                            )
                        )
                        content_segments.append(image_segment)
                        logger.info("✅ 使用API下载高清图片成功")
                        return

                except Exception as e:
                    logger.warning(f"API下载高清图片失败: {e}")

            # 回退到 URL 模式
            if parsed_xml.get("url"):
                try:
                    suffix = ".jpg"  # 微信图片通常是jpg
                    image_segment = await ChatMessageSegmentImage.create_from_url(
                        url=parsed_xml["url"],
                        from_chat_key=channel_id,
                        file_name=f"image{suffix}",
                        use_suffix=suffix,
                    )
                    content_segments.append(image_segment)
                    logger.info("✅ 从 URL 创建图片消息段成功")
                    return
                except Exception as e:
                    logger.warning(f"从 URL 创建图片消息段失败: {e}")

        # 最后的回退方案
        content_segments.append(
            ChatMessageSegment(
                type=ChatMessageSegmentType.TEXT,
                text="[图片]",
            )
        )

    async def _handle_voice_from_xml_or_api(
        self, message: WeChatMessage, content_segments: list, channel_id: str
    ):
        """处理语音消息：从 XML 解析或使用 API 下载"""
        parsed_xml = self._parse_xml_content(message.actual_content)

        if parsed_xml["type"] == "voice":
            # 尝试使用 API 下载语音文件
            if (
                hasattr(message, "new_msg_id")
                and message.new_msg_id
                and parsed_xml.get("url")
                and self.adapter
            ):
                try:
                    logger.debug(
                        f"尝试使用API下载语音文件: new_msg_id={message.new_msg_id}"
                    )

                    # 使用 WeChatPad API 下载语音文件
                    voice_data = await self.adapter.http_client.get_msg_voice(
                        new_msg_id=message.new_msg_id,
                        to_user=message.receiver_wxid,
                        bufid=parsed_xml.get("url", ""),
                        length=parsed_xml.get("length", 0) or 1000,
                    )

                    if voice_data and "Data" in voice_data:
                        # TODO: 将来可以实现语音文件处理，类似OneBot的文件上传
                        # voice_segment = await ChatMessageSegmentFile.create_from_bytes(...)
                        # content_segments.append(voice_segment)

                        # 目前仍然显示文本信息
                        voice_length_ms = int(parsed_xml.get("length", 0))
                        voice_length_sec = voice_length_ms / 1000.0
                        content_segments.append(
                            ChatMessageSegment(
                                type=ChatMessageSegmentType.TEXT,
                                text=f"[语音] {voice_length_sec:.1f}秒 (已下载)",
                            )
                        )
                        logger.info("✅ 使用API下载语音文件成功")
                        return

                except Exception as e:
                    logger.warning(f"API下载语音文件失败: {e}")

            # 回退到显示时长信息
            if parsed_xml.get("length"):
                try:
                    voice_length_ms = int(parsed_xml["length"])
                    voice_length_sec = voice_length_ms / 1000.0
                    content_segments.append(
                        ChatMessageSegment(
                            type=ChatMessageSegmentType.TEXT,
                            text=f"[语音] {voice_length_sec:.1f}秒",
                        )
                    )
                    logger.debug(f"语音消息: {voice_length_sec:.1f}秒")
                    return
                except (ValueError, TypeError):
                    pass

        # 最后的回退方案
        content_segments.append(
            ChatMessageSegment(
                type=ChatMessageSegmentType.TEXT,
                text="[语音]",
            )
        )


# 便捷函数
async def create_simple_text_handler(keywords: list[str], response_callback: Callable):
    """创建简单的文本消息处理器"""

    async def handle_text_message(message: WeChatMessage):
        await response_callback(message)

    return TextMessageHandler(keywords=keywords, callback=handle_text_message)


async def create_echo_handler():
    """创建回显处理器（用于调试）"""

    async def echo_message(message: WeChatMessage):
        print(f"[{message.create_datetime}] {message.sender_name}: {message.content}")

    return TextMessageHandler(callback=echo_message)
