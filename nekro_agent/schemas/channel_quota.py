"""频道每日额度的接口契约

平台方字段与租户字段**分成两组模型**，不是同一组加注释：租户永远写不到平台方字段，
而「请求模型里根本没有那个字段」比任何一层校验都可靠——多一层校验就多一处可能写错的地方。
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# 提示语长度上限。这句话会以 Bot 的口吻发进群里，太长的提示本身就是一次刷屏
MAX_PAUSE_MESSAGE_LENGTH = 200


def _normalize_pause_message(value: Optional[str]) -> Optional[str]:
    """去首尾空白；空串当「清除」（None），超长拒绝

    空串与 NULL 在库里是两个值，语义却应该只有一个：「没说」。允许空串存进去会让
    解析侧多一条「空串也算没设」的判据，而那是同一个判据的第二处实现。
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > MAX_PAUSE_MESSAGE_LENGTH:
        raise ValueError(f"暂停提示最多 {MAX_PAUSE_MESSAGE_LENGTH} 字")
    return text


class _PolicyFields(BaseModel):
    """平台方字段的公共部分（三层共用，NULL = 继承上一层）"""

    enabled: Optional[bool] = Field(default=None, description="是否启用频道额度；NULL = 继承")
    unit: Optional[Literal["replies", "tokens"]] = Field(default=None, description="额度单位；NULL = 继承")
    group_daily_limit: Optional[int] = Field(default=None, ge=0, description="群的每日额度；0 = 不限；NULL = 继承")
    private_daily_limit: Optional[int] = Field(
        default=None,
        ge=0,
        description="私聊的每日额度；0 = 不限；NULL = 继承",
    )
    over_limit_action: Optional[Literal["alert", "pause"]] = Field(
        default=None,
        description="超额动作；NULL = 继承",
    )
    pause_message: Optional[str] = Field(default=None, description=f"暂停提示，最多 {MAX_PAUSE_MESSAGE_LENGTH} 字")

    @field_validator("pause_message")
    @classmethod
    def _check_pause_message(cls, value: Optional[str]) -> Optional[str]:
        return _normalize_pause_message(value)


class GlobalQuotaPolicyUpdate(_PolicyFields):
    """全局策略的写入：全局是最后一层，开关、单位、超额动作不允许留空"""

    enabled: bool = Field(description="是否启用频道额度")
    unit: Literal["replies", "tokens"] = Field(description="额度单位")
    over_limit_action: Literal["alert", "pause"] = Field(description="超额动作")


class ScopedQuotaPolicyUpdate(_PolicyFields):
    """实例层与频道层策略的写入：每一列都可以留空表示「跟上一层」"""


class ChannelQuotaPolicyInfo(BaseModel):
    """一层策略的完整内容（读取用）"""

    scope: str
    adapter_key: str
    instance_key: str
    chat_key: str
    enabled: Optional[bool]
    unit: Optional[str]
    group_daily_limit: Optional[int]
    private_daily_limit: Optional[int]
    over_limit_action: Optional[str]
    pause_message: Optional[str]
    updated_by: str
    update_time: Optional[str]


class TenantInstanceQuotaUpdate(BaseModel):
    """租户对实例层「我的额度」的写入；NULL = 清除（回到平台方的额度）"""

    group_daily_limit: Optional[int] = Field(default=None, ge=1, description="群的每日额度；NULL = 清除")
    private_daily_limit: Optional[int] = Field(default=None, ge=1, description="私聊的每日额度；NULL = 清除")
    pause_message: Optional[str] = Field(default=None, description=f"暂停提示，最多 {MAX_PAUSE_MESSAGE_LENGTH} 字")

    @field_validator("pause_message")
    @classmethod
    def _check_pause_message(cls, value: Optional[str]) -> Optional[str]:
        return _normalize_pause_message(value)


class TenantChannelQuotaUpdate(BaseModel):
    """租户对频道层「我的额度」的写入

    只有一个 `daily_limit`：写进群列还是私聊列由这个频道自己的类型决定，
    让调用方自己选列等于把「频道是群还是私聊」这个事实交给他去猜。
    """

    daily_limit: Optional[int] = Field(default=None, ge=1, description="该频道的每日额度；NULL = 清除")
    pause_message: Optional[str] = Field(default=None, description=f"暂停提示，最多 {MAX_PAUSE_MESSAGE_LENGTH} 字")

    @field_validator("pause_message")
    @classmethod
    def _check_pause_message(cls, value: Optional[str]) -> Optional[str]:
        return _normalize_pause_message(value)


class TenantInstanceQuotaInfo(BaseModel):
    adapter_key: str
    instance_key: str
    unit: str
    group_daily_limit: Optional[int]
    private_daily_limit: Optional[int]
    pause_message: Optional[str]
    platform_group_daily_limit: int
    platform_private_daily_limit: int


class TenantChannelQuotaInfo(BaseModel):
    chat_key: str
    chat_type: str
    unit: str
    daily_limit: Optional[int]
    pause_message: Optional[str]
    platform_limit: int
    effective_limit: int


class ChannelQuotaDailyItem(BaseModel):
    chat_key: str
    channel_name: str
    chat_type: str
    adapter_key: str
    instance_key: str
    tenant_id: Optional[int]
    tenant_name: str
    unit: str
    used: int
    limit: int
    bonus: int
    remaining: Optional[int]
    percent: int
    state: str
    action: str


class ChannelQuotaDailyListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    day: str
    items: List[ChannelQuotaDailyItem]


class ChannelQuotaHistoryDay(BaseModel):
    day: str
    replies: int
    tokens_all: int
    tokens_reply: int
    bonus: int


class ChannelQuotaSceneUsage(BaseModel):
    scene: str
    token_total: int


class ChannelQuotaPolicySummary(BaseModel):
    unit: str
    platform_limit: int
    tenant_limit: Optional[int]
    effective_limit: int
    over_limit_action: str
    pause_message: str


class ChannelQuotaHistoryResponse(BaseModel):
    chat_key: str
    days: List[ChannelQuotaHistoryDay]
    by_scene: List[ChannelQuotaSceneUsage]
    policy: ChannelQuotaPolicySummary


class ChannelQuotaBonusRequest(BaseModel):
    amount: int = Field(gt=0, description="今天要加的额度，正整数")


class ChannelQuotaBonusResponse(BaseModel):
    chat_key: str
    day: str
    bonus: int
    status: ChannelQuotaDailyItem


class ChannelQuotaRecountResponse(BaseModel):
    """一次全量重算的结果：只报「哪一天、动了几个频道」

    不回传逐频道的数字：全量重算之后前端要做的是刷新列表，列表本身就是那一份数字，
    再回传一遍等于让两个来源各说一次，迟早不一致。
    """

    day: str
    channels: int
