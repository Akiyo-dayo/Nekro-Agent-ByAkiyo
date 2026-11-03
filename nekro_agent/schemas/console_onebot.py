"""总后台：OneBot 协议端状态响应模型

只有只读状态：协议端自报的身份，以及本次连接以来 NA 发出多少条、收到多少条自身消息回传。
"""

from typing import Optional

from pydantic import BaseModel


class OnebotEndpointStatus(BaseModel):
    online: bool
    account_id: str = ""
    app_name: str = ""
    app_version: str = ""
    protocol_version: str = ""
    implementation: str = "unknown"
    self_report: str = "unknown"
    own_sends: int = 0
    echoes: int = 0
    connected_at: Optional[float] = None
    last_echo_at: Optional[float] = None
