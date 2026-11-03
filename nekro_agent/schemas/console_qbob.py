"""总后台：附带 qbob（QQ 官方机器人 → OneBot v11）的请求与响应模型

设计见 `docs/qbob-integration.md`。这里的响应**一律不含** AppSecret、OneBot 访问令牌、
qbob 服务账号口令：读状态的接口对租户管理员开放，凭据不能顺着它流出去。
"""

from typing import Any, ClassVar, Literal, Optional

from pydantic import BaseModel, Field

from nekro_agent.schemas.errors import AppError
from nekro_agent.schemas.i18n import I18nDict, i18n_text


class QbobOperationError(AppError):
    """调用附带的 qbob 失败（连不上、服务账号不可用、qbob 拒绝了请求）

    502：失败在上游 qbob，不在 NA 自身。`reason` 是给人看的说明，**不含任何凭据**。
    """

    http_status: ClassVar[int] = 502
    i18n_message: ClassVar[I18nDict] = i18n_text(
        zh_CN="{operation}失败：{reason}",
        en_US="{operation} failed: {reason}",
    )

    def __init__(self, operation: str, reason: str, **kwargs: Any):
        super().__init__(operation=operation, reason=reason, **kwargs)


class QbobServiceStatus(BaseModel):
    """本部署附带的 qbob 服务是否可用"""

    configured: bool = Field(default=False, description="是否配置了 NEKRO_QBOB_ADMIN_URL")
    reachable: bool = Field(default=False, description="qbob 管理面是否连得上")
    service_account_ready: bool = Field(default=False, description="NA 的 qbob 服务账号是否可用")
    version: str = Field(default="", description="qbob 版本")
    qbob_ref: str = Field(default="", description="本机镜像所用的上游提交（NEKRO_QBOB_REF），用于界面标注许可与来源")
    bot_count: int = Field(default=0, description="qbob 里正在运行的 bot 数（含非 NA 托管的；停用的不计）")
    message: str = Field(default="", description="不可用时的说明")


class QbobServiceCredentialRequest(BaseModel):
    """手工提供 NA 访问 qbob 用的账号（初始口令文件已不在时）"""

    username: str = Field(..., min_length=1, max_length=128, description="qbob 管理台账号（需 admin 角色）")
    password: str = Field(..., min_length=8, max_length=1024, description="口令；只写不读")


class QbobBotInfo(BaseModel):
    """实例接入的 qbob bot 的状态"""

    configured: bool = Field(default=False, description="本部署是否附带了 qbob")
    attached: bool = Field(default=False, description="本实例是否已接入 qbob 里的某个 bot")
    appid: str = Field(default="", description="QQ 机器人 AppID")
    name: str = Field(default="", description="bot 名称")
    self_id: str = Field(default="", description="qbob 给这个 bot 的 15 位 OneBot self_id")
    found: bool = Field(default=False, description="qbob 里是否还有这个 bot")
    running: bool = Field(default=False, description="bot 是否在运行")
    enabled: bool = Field(default=False, description="bot 是否启用")
    qq_connected: bool = Field(default=False, description="与 QQ 开放平台的连接")
    onebot_connected: bool = Field(default=False, description="反向 WS 是否连上了本 NA")
    endpoint_ok: bool = Field(default=False, description="反向 WS 端点是否与 NA 期望的一致")
    markdown_enabled: bool = Field(default=False, description="是否启用 markdown（群聊真 @ 需要）")
    report_self_message: bool = Field(default=False, description="是否上报 bot 自己发的消息")
    last_error: str = Field(default="", description="qbob 记录的最近错误")
    message: str = Field(default="", description="不可用或需要处理时的说明")


class QbobAttachRequest(BaseModel):
    """手填凭据接入"""

    appid: str = Field(..., min_length=1, max_length=32, description="QQ 机器人 AppID")
    secret: str = Field(..., min_length=1, max_length=256, description="AppSecret；只写不读，不落 NA 数据库")


class QbobQrStartResponse(BaseModel):
    """扫码接入：二维码"""

    task_id: str = Field(..., description="绑定任务号，轮询时带上")
    connect_url: str = Field(..., description="二维码内容（手机 QQ 扫码打开的地址）")
    qr_svg: str = Field(default="", description="qbob 生成的二维码 SVG")


class QbobQrPollResponse(BaseModel):
    """扫码接入：轮询结果"""

    status: Literal["pending", "expired", "attached", "conflict", "failed"] = Field(
        ...,
        description="轮询状态；conflict / failed 时 message 说明原因，之后再轮询同一任务得到同样的结果",
    )
    message: str = Field(default="", description="说明")
    bot: Optional[QbobBotInfo] = Field(default=None, description="接入成功时的 bot 状态")


class QbobBotOptionsRequest(BaseModel):
    """自助运维：bot 的几项开关（不传的字段不改）"""

    enabled: Optional[bool] = Field(default=None)
    markdown_enabled: Optional[bool] = Field(default=None)
    report_self_message: Optional[bool] = Field(default=None)
