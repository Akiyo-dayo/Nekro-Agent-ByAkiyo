import datetime
from typing import Any, Dict, List, cast

import json5
from tortoise import fields
from tortoise.models import Model

from nekro_agent.adapters.interface.schemas.extra import PlatformMessageExt
from nekro_agent.core.config import CoreConfig
from nekro_agent.schemas.chat_message import (
    ChatMessageSegment,
    ChatMessageSegmentAt,
    ChatMessageSegmentFile,
    ChatMessageSegmentForward,
    ChatMessageSegmentImage,
    ChatMessageSegmentJsonCard,
    ChatMessageSegmentUntrusted,
    segments_from_list,
)
from nekro_agent.tools.common_util import limited_text_output
from nekro_agent.tools.path_convertor import convert_filename_to_sandbox_upload_path
from nekro_agent.tools.untrusted_content import (
    UntrustedToolResult,
    render_untrusted_result,
    sanitize_block_text,
    wrap_untrusted_block,
)

# 合并转发内容完全由发送方构造，渲染进提示词时的长度上限
MAX_FORWARD_PROMPT_LENGTH = 2048


class DBChatMessage(Model):
    """数据库聊天消息模型"""

    id = fields.IntField(pk=True, generated=True, description="ID")
    sender_id = fields.CharField(max_length=256, index=True, description="发送者 ID") # -1 表示 Bot 发送的消息
    sender_name = fields.CharField(max_length=128, index=True, description="发送者真实昵称")
    sender_nickname = fields.CharField(max_length=128, index=True, description="发送者显示昵称")
    is_tome = fields.IntField(description="是否与 Bot 相关")
    is_recalled = fields.BooleanField(description="是否为撤回消息")

    adapter_key = fields.CharField(max_length=64, index=True, description="适配器标识")
    # 收发该消息的账号实例标识，单实例适配器留空。详见 DBChatChannel.instance_key
    instance_key = fields.CharField(max_length=64, default="", index=True, description="适配器实例标识")
    message_id = fields.CharField(max_length=512, index=True, description="消息平台 ID")
    chat_key = fields.CharField(max_length=256, index=True, description="聊天频道唯一标识")
    chat_type = fields.CharField(max_length=32, index=True, description="聊天频道类型")
    platform_userid = fields.CharField(max_length=256, index=True, description="平台用户 ID")

    content_text = fields.TextField(description="消息内容文本")
    content_data = fields.TextField(description="消息内容数据 JSON")

    raw_cq_code = fields.TextField(description="原始 CQ 码")
    ext_data = fields.TextField(description="扩展数据")

    send_timestamp = fields.IntField(index=True, description="发送时间戳")
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "chat_message"

    def parse_chat_history_prompt(
        self,
        one_time_code: str,
        config: "CoreConfig",
        ref_mode: bool = False,
        travel_mode: bool = False,
    ) -> str:
        """解析聊天历史记录生成提示词

        Args:
            ref_mode: 是否带消息编号 / 引用前缀（被引用的消息，或开了 `AI_ALWAYS_INCLUDE_MSG_ID`）。
                只管前缀，**与令牌无关**：旧写法把它直接当 `travel_mode` 传下去，引用到的消息里的图片、
                文件、@ 与工具结果都被渲染成没有令牌的裸文。
            travel_mode: 漫游记录用的渲染（调用方没有本轮令牌，传空串），资源段不带令牌；
                不可信内容的边界在任何模式下都保留。
        """
        content = convert_raw_msg_data_json_to_msg_prompt(self.content_data, one_time_code, config, travel_mode)
        content = limited_text_output(content, config.AI_CONTEXT_LENGTH_PER_MESSAGE, placeholder="(content too long, omitted)")
        time_str = datetime.datetime.fromtimestamp(self.send_timestamp).strftime("%m-%d %H:%M:%S")

        # 消息引用前缀生成
        additional_info: str = f"msg_id:{self.message_id}" if ref_mode and self.message_id else ""
        ref_str: str = f"ref:{self.ext_data_obj.ref_msg_id}" if ref_mode and self.ext_data_obj.ref_msg_id else ""
        # tome 标记：仅在群聊中、非 Bot/系统消息、且明确指向当前 Bot 时输出
        # sender_id 以字符串形式存储，"-1" 表示 Bot/系统消息
        is_group_chat: bool = self.chat_type not in ("private", "c2c")
        is_bot_message: bool = str(self.sender_id) == "-1"
        tome_str: str = (
            "tome:true"
            if config.AI_INCLUDE_TOME_INDICATOR and is_group_chat and not is_bot_message and self.is_tome
            else ""
        )

        parts: List[str] = [p for p in [additional_info, ref_str, tome_str] if p]
        prefix_str: str = f"({', '.join(parts)})" if parts else ""

        return f'{prefix_str}[{time_str} id:{self.platform_userid}] "{self.sender_nickname}" 说: {content or self.content_text}'

    def parse_content_data(self) -> List[ChatMessageSegment]:
        """解析内容数据"""
        return segments_from_list(cast(List[Dict], json5.loads(self.content_data)))

    @property
    def is_system(self) -> bool:
        """是否为系统消息"""
        return self.sender_id == -1

    @property
    def ext_data_obj(self) -> PlatformMessageExt:
        """扩展数据"""
        if not self.ext_data or self.ext_data == "{}":
            return PlatformMessageExt()
        try:
            return PlatformMessageExt.model_validate(json5.loads(self.ext_data))
        except Exception:
            return PlatformMessageExt()


def convert_raw_msg_data_json_to_msg_prompt(json_data: str, one_time_code: str, config: "CoreConfig", travel_mode: bool = False) -> str:
    """将数据库保存的原始消息数据 JSON 转换为提示词字符串

    Args:
        json_data (str): 数据库保存的原始消息数据 JSON

    Returns:
        str: 提示词字符串
    """

    prompt_str = ""

    for seg in segments_from_list(cast(List[Dict[str, Any]], json5.loads(json_data))):
        if isinstance(seg, ChatMessageSegmentImage):
            remote_url_seg = f' (remote_url:"{seg.remote_url}")' if seg.remote_url and config.AI_SHOW_REMOTE_URL else ""
            prompt_str += (
                f"<Image:{convert_filename_to_sandbox_upload_path(seg.file_name)}{remote_url_seg}>"
                if travel_mode
                else f"<{one_time_code} | Image:{convert_filename_to_sandbox_upload_path(seg.file_name)}{remote_url_seg}>"
            )
        elif isinstance(seg, ChatMessageSegmentFile):
            remote_url_seg = f' (remote_url:"{seg.remote_url}")' if seg.remote_url and config.AI_SHOW_REMOTE_URL else ""
            prompt_str += (
                f"<File:{convert_filename_to_sandbox_upload_path(seg.file_name)}{remote_url_seg}>"
                if travel_mode
                else f"<{one_time_code} | File:{convert_filename_to_sandbox_upload_path(seg.file_name)}{remote_url_seg}>"
            )
        elif isinstance(seg, ChatMessageSegmentAt):
            prompt_str += (
                f"<At:[@id:{seg.target_platform_userid};nickname:{seg.target_nickname}@]>"
                if travel_mode
                else f"<{one_time_code} | At:[@id:{seg.target_platform_userid};nickname:{seg.target_nickname}@]>"
            )
        elif isinstance(seg, ChatMessageSegmentJsonCard):
            # JSON卡片消息：直接使用格式化后的文本，已经包含所有关键信息
            prompt_str += (
                f"{seg.text}"
                if travel_mode
                else f"<{one_time_code} | {seg.text}>"
            )
        elif isinstance(seg, ChatMessageSegmentForward):
            # 合并转发的昵称与正文均由发送方任意构造，与真实群聊消息格式同构。
            # 必须包裹在带会话令牌的边界内并标注为不可信，否则可伪造他人发言。
            forward_body = sanitize_block_text(seg.text, MAX_FORWARD_PROMPT_LENGTH)
            prompt_str += (
                forward_body
                if travel_mode
                else wrap_untrusted_block(
                    one_time_code,
                    "Forward",
                    forward_body,
                    nodes=len(seg.forward_content),
                )
            )
        elif isinstance(seg, ChatMessageSegmentUntrusted):
            result = UntrustedToolResult(kind=seg.kind, body=seg.text, summary=seg.summary, attrs=seg.attrs)
            # 工具结果的正文来自外部（模型看过视频之后写的话、抓来的网页），任何渲染模式下都要有不可信边界；
            # 漫游模式拿不到本轮令牌，边界里的令牌是空的，但 `trust:untrusted` 标注仍在
            prompt_str += render_untrusted_result(one_time_code, result)
        elif isinstance(seg, ChatMessageSegment):
            prompt_str += seg.text

    return prompt_str
