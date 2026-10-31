"""运维写操作的确认落地

运维 Bot 在 IM 里只发链接不执行，链接落到这里。这两个端点是整个运维通道
**唯一**真正改变系统状态的地方，因此它们的守卫比 IM 那一侧严格得多：

- `require_platform_owner()`：链接本身不是凭据。转发给别人、截图泄露，
  只要对方不是平台方账号就打不开。租户管理员被钉死在 `Role.Admin`
  （见 `services/user/tenant_perm.py`），永远不满足平台方判定。
- 预览与确认**分成两个端点**：预览不消费令牌，确认才消费。
  合成一个的话，人还没看清要做什么，令牌就已经被 GET 请求（预取、扫描器、
  聊天软件的链接预览）消耗掉了。

令牌用 POST 请求体传输而不是查询串：查询串会进访问日志与浏览器历史。
它出现在浏览器地址栏是深链形态决定的，但没必要再多一份副本。
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.console import ActionOkResponse
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.services.ops_bot.actions import execute_write_action, resolve_action
from nekro_agent.services.ops_bot.deeplink import (
    OpsDeepLinkError,
    OpsDeepLinkPayload,
    OpsDeepLinkReason,
    consume_ops_deeplink,
    peek_ops_deeplink,
)
from nekro_agent.services.user.tenant_perm import require_platform_owner

logger = get_sub_logger("ops_bot.console")

router = APIRouter(prefix="/ops", tags=["Console Ops"])

DEEPLINK_ERROR_MESSAGES = {
    OpsDeepLinkReason.MALFORMED: "确认链接格式不正确",
    OpsDeepLinkReason.BAD_SIGNATURE: "确认链接签名校验失败",
    OpsDeepLinkReason.EXPIRED: "确认链接已过期，请回到运维频道重新发起",
    OpsDeepLinkReason.ALREADY_USED: "确认链接已被使用或已失效，请回到运维频道重新发起",
}


class OpsActionTokenRequest(BaseModel):
    """携带确认令牌"""

    token: str = Field(..., description="运维 Bot 签发的一次性确认令牌")


class OpsActionPreview(BaseModel):
    """确认页需要展示的内容

    `summary` 是给人看的"这条链接点下去会发生什么"，来自 `OpsActionSpec.summary`。
    确认页必须把它显示出来——不显示就等于让人盲签。
    """

    action: str = Field(..., description="写操作标识")
    summary: str = Field(..., description="该操作的自然语言说明")
    target: str = Field(default="", description="操作目标")
    target_label: str = Field(default="", description="目标参数的名称")
    issuer_user_id: str = Field(..., description="在运维频道里发起该操作的账号")
    issuer_chat_key: str = Field(..., description="发起该操作的运维频道")
    expires_at: int = Field(..., description="过期时间（unix 秒）")


def _to_validation_error(error: OpsDeepLinkError) -> ValidationError:
    return ValidationError(reason=DEEPLINK_ERROR_MESSAGES[error.reason])


def _build_preview(payload: OpsDeepLinkPayload) -> OpsActionPreview:
    spec = resolve_action(payload.action)
    if spec is None:
        # 签名有效却指向未知操作：只可能发生在降级部署（新版签发、旧版落地）。
        # 宁可拒绝也不能猜一个操作执行。
        raise ValidationError(reason=f"确认链接指向了未知的运维操作: {payload.action}")
    return OpsActionPreview(
        action=payload.action,
        summary=spec.summary,
        target=payload.target,
        target_label=spec.target_label,
        issuer_user_id=payload.issuer_user_id,
        issuer_chat_key=payload.issuer_chat_key,
        expires_at=payload.exp,
    )


@router.post("/actions/preview", summary="预览运维确认链接")
async def preview_ops_action(
    body: OpsActionTokenRequest,
    _current_user: DBUser = Depends(require_platform_owner()),
) -> OpsActionPreview:
    """校验确认链接并返回它要执行的操作，**不消费**令牌"""
    try:
        payload = peek_ops_deeplink(body.token)
    except OpsDeepLinkError as error:
        raise _to_validation_error(error) from error
    return _build_preview(payload)


@router.post("/actions/confirm", summary="确认执行运维写操作")
async def confirm_ops_action(
    body: OpsActionTokenRequest,
    current_user: DBUser = Depends(require_platform_owner()),
) -> ActionOkResponse:
    """消费确认链接并执行对应的写操作"""
    try:
        payload = consume_ops_deeplink(body.token)
    except OpsDeepLinkError as error:
        raise _to_validation_error(error) from error

    preview = _build_preview(payload)
    logger.info(
        f"平台方确认运维写操作: user={current_user.username} action={payload.action} "
        f"target={payload.target or '-'} issuer={payload.issuer_user_id}",
    )
    message = await execute_write_action(payload.action, payload.target)
    return ActionOkResponse(ok=True, message=f"{preview.summary} —— {message}")
