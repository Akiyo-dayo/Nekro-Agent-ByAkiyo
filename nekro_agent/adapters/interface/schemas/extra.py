from pydantic import BaseModel


class PlatformMessageExt(BaseModel):
    """平台消息扩展数据"""

    ref_chat_key: str = ""  # 引用聊天频道唯一标识
    ref_msg_id: str = ""  # 引用消息的平台消息 ID
    ref_sender_id: str = ""  # 引用消息的发送者平台 ID
    self_sent_source: str = ""  # 非空表示这条是同账号由其他程序发出的，见 SELF_SENT_SOURCE_EXTERNAL
