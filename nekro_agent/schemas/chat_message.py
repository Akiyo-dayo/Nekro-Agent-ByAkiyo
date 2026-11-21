import time
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional

from pydantic import BaseModel, Field

from nekro_agent.core import logger
from nekro_agent.tools.common_util import (
    copy_to_upload_dir,
    download_file,
    download_file_from_base64,
    download_file_from_bytes,
)


class ChatType(Enum):
    PRIVATE = "private"
    GROUP = "group"
    UNKNOWN = "unknown"


# 工具结果行的身份。不是 "-1"（Bot，会占回复配额）也不是 SYSTEM（提示词要求模型信任它）
TOOL_RESULT_SENDER_ID = "-2"
TOOL_RESULT_SENDER_NAME = "UNTRUSTED_TOOL_RESULT"

# 同一账号由其他程序发出的消息（OneBot 的 `message_sent`，比如接在同一协议端上的另一个框架）。
# 不是 "-1"：那会占回复配额，Bot 自己的历史也会被它污染；也不是真人：不算好感，不进参与者名单。
# `platform_userid` 仍记 Bot 账号的号码，与 Bot 自己的消息同一个约定
SELF_SENT_SENDER_ID = "-3"
# 这个后缀跟着昵称走，WebUI、提示词、记忆整理读到的都是它，不用每处各写一遍判断
SELF_SENT_NICKNAME_SUFFIX = "（同账号其他程序）"
# `ext_data.self_sent_source` 的取值
SELF_SENT_SOURCE_EXTERNAL = "external"

# 只用字符串判断开关，核心与适配器都不 import 插件
VIDEO_ANALYSIS_PLUGIN_KEY = "Akiyo_dayo.video_analysis"
# 视频插件关着时跟在占位后面。固定文案：文件名由发送方决定，不能进提示词
VIDEO_UNAVAILABLE_NOTE = "（这里有一个视频，但视频分析功能没有开启，你看不到它的内容。如实告诉用户，不要说没看到视频）"


class ChatMessageSegmentType(Enum):
    TEXT = "text"
    IMAGE = "image"
    VOICE = "voice"
    VIDEO = "video"
    FILE = "file"
    REFERENCE = "reference"
    AT = "at"
    JSON_CARD = "json_card"
    FORWARD = "forward"
    POKE = "poke"
    UNTRUSTED = "untrusted"


class ChatMessageSegment(BaseModel):
    """聊天消息段基础文本"""

    type: ChatMessageSegmentType
    text: str

    class Config:
        use_enum_values = True


class ChatMessageSegmentAt(ChatMessageSegment):
    """聊天消息段 @"""

    target_platform_userid: str  # 被 @ 的人平台用户ID
    target_nickname: str  # 被 @ 的人原始昵称


class ChatMessageSegmentFile(ChatMessageSegment):
    """聊天消息段文件"""

    file_name: str
    local_path: Optional[str] = None
    remote_url: Optional[str] = None

    @classmethod
    def get_segment_type(cls) -> ChatMessageSegmentType:
        return ChatMessageSegmentType.FILE

    @classmethod
    async def create_from_url(cls, url: str, from_chat_key: str, file_name: str = "", use_suffix: str = ""):
        """从 URL 创建文件消息段"""
        if url.startswith("data:"):
            return await cls.create_from_base64(url, from_chat_key, file_name, use_suffix)

        local_path, _file_name = await download_file(url, file_name=file_name, use_suffix=use_suffix, from_chat_key=from_chat_key)

        # 落盘函数会清洗文件名，消息段要存实际落盘名，否则渲染给模型的沙盒路径会指向不存在的文件
        return cls(
            type=cls.get_segment_type(),
            text=f"[{cls.get_segment_type().value.capitalize()}: {_file_name}]",
            file_name=_file_name,
            local_path=local_path,
            remote_url=url,
        )

    @classmethod
    async def create_form_local_path(cls, local_path: str, from_chat_key: str, file_name: str = "", use_suffix: str = ""):
        """从本地路径创建文件消息段"""
        local_path, _file_name = await copy_to_upload_dir(
            file_path=local_path,
            file_name=file_name,
            use_suffix=use_suffix,
            from_chat_key=from_chat_key,
        )

        return cls(
            type=cls.get_segment_type(),
            text=f"[{cls.get_segment_type().value.capitalize()}: {_file_name}]",
            file_name=_file_name,
            local_path=local_path,
        )

    @classmethod
    def create_unavailable_placeholder(cls, reason: str = "") -> ChatMessageSegment:
        """构造"资源取不到"的占位文本段

        资源级失败（下载被安全策略拒绝、404、超时、本地文件丢失）只应让**这一个资源**降级，
        不应让整条消息消失。降级成纯文本段而不是保留 Image/File 段，是因为下游会拿
        `local_path` / `remote_url` 去读盘、去让视觉模型抓图（见 `history.py` 的图片装配逻辑）；
        留一个指不到东西的资源段只会把失败推迟到更难排查的地方。

        占位文案刻意不带 URL、文件名等发送方可控内容：它会原样进入提示词
        （`convert_raw_msg_data_json_to_msg_prompt` 对纯文本段不做可信边界包裹），
        拼接任意外部字符串等于开一条提示词注入通道。

        Args:
            reason: 仅用于日志，不进入提示词
        """
        kind = cls.get_segment_type().value.capitalize()
        return ChatMessageSegment(
            type=ChatMessageSegmentType.TEXT,
            text=f"[{kind}: unavailable, failed to fetch]",
        )

    @classmethod
    async def create_from_url_or_placeholder(
        cls,
        url: str,
        from_chat_key: str,
        file_name: str = "",
        use_suffix: str = "",
    ) -> ChatMessageSegment:
        """从 URL 创建资源消息段；取不到时降级为占位文本段

        用于消息接收链路：一张图下不来不该让用户发的文字一起消失，模型也需要知道"这里原本有
        一张图但取不到"，而不是凭空少一段内容。需要感知失败的调用方（插件、出站发送）应继续用
        `create_from_url`。
        """
        try:
            return await cls.create_from_url(
                url=url,
                from_chat_key=from_chat_key,
                file_name=file_name,
                use_suffix=use_suffix,
            )
        except Exception as e:
            logger.warning(f"{cls.get_segment_type().value} 资源下载失败，降级为占位文本: {type(e).__name__}: {e}")
            return cls.create_unavailable_placeholder(reason=str(e))

    @classmethod
    async def create_from_local_path_or_placeholder(
        cls,
        local_path: str,
        from_chat_key: str,
        file_name: str = "",
        use_suffix: str = "",
    ) -> ChatMessageSegment:
        """从本地路径创建资源消息段；文件缺失/不可读时降级为占位文本段

        与 `create_from_url_or_placeholder` 同一契约：协议端给的本地路径经常因为容器挂载差异
        而不存在，这同样是资源级失败，不应连累整条消息。
        """
        try:
            return await cls.create_form_local_path(
                local_path=local_path,
                from_chat_key=from_chat_key,
                file_name=file_name,
                use_suffix=use_suffix,
            )
        except Exception as e:
            logger.warning(f"{cls.get_segment_type().value} 本地资源读取失败，降级为占位文本: {type(e).__name__}: {e}")
            return cls.create_unavailable_placeholder(reason=str(e))

    @classmethod
    async def create_from_bytes(cls, _bytes: bytes, from_chat_key: str, file_name: str = "", use_suffix: str = ""):
        """从字节数据创建文件消息段"""
        local_path, _file_name = await download_file_from_bytes(
            bytes_data=_bytes,
            file_name=file_name,
            use_suffix=use_suffix,
            from_chat_key=from_chat_key,
        )

        return cls(
            type=cls.get_segment_type(),
            text=f"[{cls.get_segment_type().value.capitalize()}: {_file_name}]",
            file_name=_file_name,
            local_path=local_path,
        )

    @classmethod
    async def create_from_base64(cls, base64_str: str, from_chat_key: str, file_name: str = "", use_suffix: str = ""):
        """从 Base64 数据创建文件消息段"""
        local_path, _file_name = await download_file_from_base64(
            base64_str=base64_str,
            file_name=file_name,
            use_suffix=use_suffix,
            from_chat_key=from_chat_key,
        )

        return cls(
            type=cls.get_segment_type(),
            text=f"[{cls.get_segment_type().value.capitalize()}: {_file_name}]",
            file_name=_file_name,
            local_path=local_path,
        )


class ChatMessageSegmentVideo(ChatMessageSegment):
    """OneBot 视频占位：没有落盘文件，也绝不存链接

    QQ 的视频链接带 `rkey`，有时效，不该进日志、聊天记录和提示词，所以 `text` 恒为
    `[Video: message:<编号>]`，要看内容时按编号向协议端现取。`video_name` 由发送方决定，
    只给 WebUI 显示，不进提示词。

    别的适配器的视频段带 `file_name`、是真落盘的文件，走 `ChatMessageSegmentFile`，与这里无关。
    """

    video_name: str = ""
    file_size: int = 0
    # 这段是引用回复带出来的：`text` 里的编号指向被引用的那条消息，不是这一行自己
    quoted: bool = False
    quoted_sender_id: str = ""

    @staticmethod
    def placeholder_text(message_id: str) -> str:
        return f"[Video: message:{message_id}]"


def merge_video_placeholders(content_text: str, segments: Iterable[ChatMessageSegment]) -> str:
    """把 OneBot 视频占位补进要落库的 `content_text`

    判定（伪造检查、命令、忽略前缀、关键词、禁词、喊名字）一律用不带占位的原文：占位里有“message”，
    用户正文里再出现“(id:”就会被伪造检查整条丢掉；占位还会被 `message`、`Video` 这类关键词与禁词命中。
    只在落库（以及随之广播、记忆整理）时补上，别处按 `content_text` 检索仍看得到视频。
    """
    missing = [
        seg.text for seg in segments if isinstance(seg, ChatMessageSegmentVideo) and seg.text and seg.text not in content_text
    ]
    return "".join([content_text, *missing]) if missing else content_text


class ChatMessageSegmentImage(ChatMessageSegmentFile):
    """聊天消息段图片"""

    @classmethod
    def get_segment_type(cls) -> ChatMessageSegmentType:
        return ChatMessageSegmentType.IMAGE


class ChatMessageSegmentForward(ChatMessageSegment):
    """合并转发消息段

    forward_content 结构:
    [
        {"sender": "昵称", "content": "文本内容(图片位置用[图片]占位)", "images": ["filename1.jpg"]},
        {"sender": "昵称", "content": "[合并转发消息]", "images": [], "forward_content": [...]},  # 嵌套
    ]
    """

    forward_content: List[Dict[str, Any]] = Field(default_factory=list)


class ChatMessageSegmentJsonCard(ChatMessageSegment):
    """聊天消息段 JSON卡片"""

    json_data: Dict[str, Any]  # 存储解析后的JSON卡片数据
    card_title: Optional[str] = None  # 卡片标题
    card_desc: Optional[str] = None  # 卡片描述
    card_icon: Optional[str] = None  # 卡片图标URL
    card_preview: Optional[str] = None  # 卡片预览图URL
    card_url: Optional[str] = None  # 卡片链接URL
    share_from_nick: Optional[str] = None  # 分享者昵称

    @classmethod
    def get_segment_type(cls) -> ChatMessageSegmentType:
        """获取消息段类型"""
        return ChatMessageSegmentType.JSON_CARD


class ChatMessageSegmentUntrusted(ChatMessageSegment):
    """沙盒方法交回来的不可信内容，`text` 是正文

    存的是不带令牌的原文，渲染提示词时才用当时的令牌包裹，与合并转发同一个做法。
    """

    kind: str
    summary: str = ""
    attrs: Dict[str, int] = Field(default_factory=dict)


class ChatMessageSegmentPoke(ChatMessageSegment):
    """戳一戳消息段"""

    action_img_url: str = ""
    poke_style: str = "戳一戳"
    poke_style_suffix: str = ""
    target_id: str = ""


def segment_from_dict(data: Dict) -> ChatMessageSegment:
    """根据字典数据创建聊天消息段"""
    segment_type = ChatMessageSegmentType(data["type"])
    if segment_type == ChatMessageSegmentType.TEXT:
        return ChatMessageSegment.model_validate(data)
    if segment_type == ChatMessageSegmentType.IMAGE:
        return ChatMessageSegmentImage.model_validate(data)
    # OneBot 的视频段只有占位，没有落盘文件
    if segment_type == ChatMessageSegmentType.VIDEO and not data.get("file_name"):
        return ChatMessageSegmentVideo.model_validate(data)
    if segment_type in {
        ChatMessageSegmentType.FILE,
        ChatMessageSegmentType.VOICE,
        ChatMessageSegmentType.VIDEO,
    }:
        return ChatMessageSegmentFile.model_validate(data)
    if segment_type == ChatMessageSegmentType.AT:
        return ChatMessageSegmentAt.model_validate(data)
    if segment_type == ChatMessageSegmentType.JSON_CARD:
        return ChatMessageSegmentJsonCard.model_validate(data)
    if segment_type == ChatMessageSegmentType.FORWARD:
        return ChatMessageSegmentForward.model_validate(data)
    if segment_type == ChatMessageSegmentType.POKE:
        return ChatMessageSegmentPoke.model_validate(data)
    if segment_type == ChatMessageSegmentType.UNTRUSTED:
        return ChatMessageSegmentUntrusted.model_validate(data)
    raise ValueError(f"Unsupported segment type: {segment_type}")


def segments_from_list(data: List[Dict]) -> List[ChatMessageSegment]:
    """根据列表数据创建聊天消息段列表"""
    return [segment_from_dict(item) for item in data]


class ChatMessage(BaseModel):
    """聊天消息"""

    message_id: str  # 消息的平台 ID
    sender_id: str  # 发送者人平台 id
    sender_name: str  # 发送者原始昵称
    sender_nickname: str  # 发送者频道显示昵称
    adapter_key: str  # 适配器标识
    instance_key: str = ""  # 收发该消息的账号实例标识，单实例适配器留空
    platform_userid: Optional[str]  # 发送者平台用户ID
    is_tome: Optional[int] = 0  # 是否与 Bot 相关消息
    is_recalled: Optional[bool] = False  # 是否为撤回消息

    chat_key: str  # 聊天频道唯一标识
    chat_type: ChatType  # 聊天频道类型
    content_text: str  # 聊天内容文本
    content_data: List[ChatMessageSegment]  # 聊天内容数据

    raw_cq_code: str  # 原始 CQ 码
    ext_data: Dict[str, Any]  # 扩展数据

    send_timestamp: int  # 发送时间戳

    class Config:
        use_enum_values = True

    @classmethod
    def create_empty(cls, chat_key: str) -> "ChatMessage":
        """创建空消息"""
        return cls(
            message_id="",
            sender_id="",
            sender_name="",
            sender_nickname="",
            adapter_key="",
            platform_userid="",
            is_tome=0,
            is_recalled=False,
            chat_key=chat_key,
            chat_type=ChatType.UNKNOWN,
            content_text="",
            content_data=[],
            raw_cq_code="",
            ext_data={},
            send_timestamp=int(time.time()),
        )

    def is_empty(self) -> bool:
        """判断消息是否为空"""
        return (
            not self.message_id
            and not self.sender_id
            and not self.sender_name
            and not self.sender_nickname
            and not self.adapter_key
            and not self.platform_userid
            and not self.content_text
            and not self.content_data
            and not self.raw_cq_code
        )
