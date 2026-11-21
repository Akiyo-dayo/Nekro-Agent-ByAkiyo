"""总后台实例健康状态与告警配置路由

本路由自带 `/health` 前缀，由 `routers/console/__init__.py` 追加 `/console` 前缀后
对外暴露为 `/api/console/health/*`。

## 为什么这一族对租户开放，但必须带作用域

健康快照与状态流水是「我的 Bot 现在怎么了」的唯一权威回答，租户看不到它就只剩一个红点。
但同一份数据里也带着**别的租户**的实例标识、显示名与协议端错误细节，所以整族按
`Role.Admin` + 实例作用域过滤：

- `/instances`、`/events` 只回该身份名下的实例（平台方不受限）；
- `/events?instance_key=` 填了别人的实例标识时结果为空 —— 作用域与筛选参数是**与**关系，
  筛选只能收窄不能扩宽；
- `/test-notification` 会真实触发外发邮件 / Webhook，因此租户必须指明是给**哪个实例**试，
  由 `assert_instance_accessible` 校验归属；不带实例是"全局通道"，那是平台方视角，
  租户没有这一档（与 `resolve_instance_scope` 对空实例的判法同源）。而且租户那一次试发
  **只走他自己实例层上配的那条 Webhook**：邮件收件人与运维频道都是平台方的，
  让租户能拿标题正文任意的一条消息去敲它们，等于凭空造出一个进平台方收件端的投递口。
"""

from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.services import embedding_health
from nekro_agent.services.console.instance_health import (
    InstanceHealthEventListResponse,
    InstanceHealthListResponse,
    instance_health_service,
)
from nekro_agent.services.notification import NotificationLevel, dispatch_notification
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import (
    assert_instance_accessible,
    get_tenant_scope,
)

router = APIRouter(prefix="/health", tags=["Console Health"])


def _scope_tenant_id(user: DBUser) -> Optional[int]:
    """该身份的数据作用域；平台方为 None，语义是"不受限"

    作用域刻意按**租户归属列**下推，而不是先取一份 `accessible_instance_keys()` 再按
    `instance_key__in` 过滤：`instance_key` 的唯一性只到 `(adapter_key, instance_key)`，
    按取值过滤会把另一个适配器下同名的那一行一并放进来（见 `list_health` 的注释）。
    """
    return get_tenant_scope(user)


class TestNotificationRequest(BaseModel):
    """测试通知请求"""

    title: str = Field(default="Nekro Agent 通知测试", description="通知标题", max_length=128)
    body: str = Field(default="这是一条测试通知，收到即表示通知渠道配置正确。", description="通知正文", max_length=2000)
    level: NotificationLevel = Field(default="info", description="通知级别")
    adapter_key: str = Field(default="", description="按实例试发时的适配器标识")
    instance_key: str = Field(default="", description="按实例试发时的实例标识；租户身份必填")


class TestNotificationResponse(BaseModel):
    """测试通知结果

    `results` 为各渠道的投递结果；邮件渠道未启用或未配置发信账号时返回 False。
    """

    results: Dict[str, bool] = Field(default_factory=dict, description="渠道名 -> 是否投递成功")
    any_succeeded: bool = Field(default=False, description="是否至少有一个渠道投递成功")


@router.get("/instances", summary="获取各实例健康状态与掉线时长")
@require_role(Role.Admin)
async def get_instance_health(
    _current_user: DBUser = Depends(get_current_active_user),
) -> InstanceHealthListResponse:
    """获取可见实例的健康快照

    Returns:
        实例健康状态列表，含掉线起始时间、已掉线时长与该实例上生效的告警策略参数
    """
    return await instance_health_service.list_health(scope_tenant_id=_scope_tenant_id(_current_user))


@router.get("/events", summary="获取实例状态变更事件流水")
@require_role(Role.Admin)
async def get_instance_health_events(
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=200, description="每页条数"),
    adapter_key: str = Query(default="", description="按适配器标识筛选"),
    instance_key: str = Query(default="", description="按实例标识筛选"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> InstanceHealthEventListResponse:
    """分页获取实例状态变更审计流水

    Returns:
        按时间倒序的状态变更流水（仅限该身份可见的实例）
    """
    return await instance_health_service.list_events(
        page=page,
        page_size=page_size,
        adapter_key=adapter_key,
        instance_key=instance_key,
        scope_tenant_id=_scope_tenant_id(_current_user),
    )


class EmbeddingHealthItem(BaseModel):
    """一个模型组的嵌入（向量化）健康状态

    只有类别、HTTP 状态码与异常类名，没有错误原文（原文里常见回显的 Key）。时间是 UTC ISO 串，
    没发生过为 null。状态只在进程内存里，重启后从头记。
    """

    model_group: str = Field(description="模型组名")
    owner: str = Field(description="platform / tenant / unknown（库里查不到了，只有平台方看得到）")
    uses: List[str] = Field(default_factory=list, description="当前配置里用它的地方：memory / kb")
    state: str = Field(description="ok（最近一次成功）/ unstable（在失败、还没到长期失败）/ failing（长期失败）")
    consecutive_failures: int = Field(default=0, description="这一串连续失败的次数")
    error_kind: str = Field(default="", description="auth / quota / rejected / server / network；没在失败时为空")
    status_code: Optional[int] = Field(default=None, description="最近一次失败的 HTTP 状态码")
    error_type: str = Field(default="", description="最近一次失败的异常类名")
    streak_started_at: Optional[str] = Field(default=None, description="这一串失败的第一次")
    failing_since: Optional[str] = Field(default=None, description="判为长期失败的时刻")
    last_failure_at: Optional[str] = Field(default=None, description="最近一次失败")
    last_success_at: Optional[str] = Field(default=None, description="最近一次成功")
    input_rejections: int = Field(
        default=0,
        description="跟输入有关的拒绝（400 / 413 / 422）本次启动以来的次数；不计入失败串，不触发长期失败",
    )
    last_input_rejection_at: Optional[str] = Field(default=None, description="最近一次跟输入有关的拒绝")
    last_input_status: Optional[int] = Field(default=None, description="最近一次跟输入有关的拒绝的 HTTP 状态码")


class EmbeddingHealthResponse(BaseModel):
    """嵌入健康状态列表（只含本次启动以来调用过的模型组）"""

    items: List[EmbeddingHealthItem] = Field(default_factory=list)
    missing_vectors: Optional[int] = Field(
        default=None,
        description="有效、正文不是空白、却没有向量的记忆段数（全平台，最多缓存一分钟）；只给平台方，"
        "租户恒为 null；没配记忆嵌入模型组时也为 null",
    )


@router.get("/embedding", summary="获取嵌入（向量化）调用的健康状态")
@require_role(Role.Admin)
async def get_embedding_health(
    _current_user: DBUser = Depends(get_current_active_user),
) -> EmbeddingHealthResponse:
    """记忆、知识库用的嵌入模型组此刻是否在长期失败

    平台方看全部模型组与全平台缺向量的记忆段数；租户只看归自己的模型组（平台方的组、别的租户的组
    都不给），缺向量的段数不给（那是全平台的数）。判据与提醒见 `services/embedding_health.py`。
    """
    scope_tenant_id = _scope_tenant_id(_current_user)
    items = await embedding_health.list_health(scope_tenant_id=scope_tenant_id)
    missing = await embedding_health.count_missing_vectors() if scope_tenant_id is None else None
    return EmbeddingHealthResponse(items=[EmbeddingHealthItem(**item) for item in items], missing_vectors=missing)


@router.post("/test-notification", summary="发送测试通知")
@require_role(Role.Admin)
async def send_test_notification(
    body: TestNotificationRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> TestNotificationResponse:
    """向已启用的通知渠道发送一条测试通知，用于验证渠道配置

    给了 `instance_key` 就按该实例上生效的告警配置试发（租户借此验证自己配的 Webhook
    到底通不通 —— 一个静默失效的告警地址与没配是同一回事，而这正是 B7 要消灭的形态）。
    不给实例是平台方的全局通道视角，租户没有这一档。

    ## 租户那一次试发只走他自己那条通道

    渠道列表里除了 Webhook 还有**邮件**与**运维频道**，这两条的收件端都取自全局配置，
    是平台方的。整份列表照发的话，租户手上就多出一个「把标题正文任意的一条消息投递到
    平台方邮箱与运维群」的原语（标题 128 字、正文 2000 字，还带着 NA 自己的发信身份），
    而且发多少次都行；连"平台方到底配没配全局 Webhook"都能从返回的渠道名里读出来。
    因此租户身份走 `scoped_only`：只发他自己实例层上配的那条地址，一条都没配就是空结果 ——
    那本来就是他要验证的答案。真实掉线告警不受影响（`instance_health._notify` 不传这个开关），
    平台方原本收到的每一条一条不少。

    Returns:
        各渠道的投递结果
    """
    scoped = get_tenant_scope(_current_user) is not None
    adapter_key = body.adapter_key
    if body.instance_key:
        instance_key = body.instance_key
        # 成对交给守卫：同一个租户在两个适配器下各有一个同名实例时，只按标识取回的是
        # id 较小的那一行，配第二个实例会被误判成 400。带上适配器才能钉死本次要试发的那一行。
        try:
            instance = await assert_instance_accessible(_current_user, instance_key, adapter_key=body.adapter_key)
        except NotFoundError:
            # 填了适配器却对不上，是调用方自己搞错了：能看见这个实例就按 400 说清楚，
            # 真正的越权 / 不存在继续走 404，不暴露存在性。
            if not adapter_key:
                raise
            await assert_instance_accessible(_current_user, instance_key)
            raise ValidationError(reason=f"适配器标识 '{adapter_key}' 与实例 '{instance_key}' 不匹配") from None
        adapter_key = instance.adapter_key
    elif scoped:
        raise ValidationError(reason="请指定要试发的实例")

    results = await dispatch_notification(
        title=body.title,
        body=body.body,
        level=body.level,
        adapter_key=adapter_key,
        instance_key=body.instance_key,
        scoped_only=scoped,
    )
    return TestNotificationResponse(results=results, any_succeeded=any(results.values()))
