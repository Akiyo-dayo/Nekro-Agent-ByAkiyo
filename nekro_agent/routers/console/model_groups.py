"""总后台模型组路由

凭据隔离是这组端点的核心：定制版客户能在自己的后台管理模型组，但平台方那份 API Key
永远只以掩码串出现。判定逻辑全部收敛在 `services/model_group_service.py`，本模块只做
请求解析与响应组装。

## 写接口的"保持不变"哨兵

读接口对不可见凭据返回 `CREDENTIAL_MASK`。前端把整个表单原样提交回来是常态，因此写接口约定：
`api_key` / `base_url` / `chat_proxy` 的值等于 `CREDENTIAL_MASK` 时保留原值不变，
不会把真实凭据覆盖成 `********`。

## 连通性测试

租户可以测试自己能用的组（含平台组），实现与脱敏全部在
`services/model_group_test.py`，本模块只负责身份解析与出参组装。

## 批量删除

逐个组走与单个删除相同的判定（`model_group_service.delete_groups`），逐个结算并列出没删成的组与原因。

## 删除前的引用提醒

删除确认框先查一次"这些组被哪些配置引用"（`services/model_group_refs.py`），只提醒不拦截。
作用域：租户只看到自己实例下的引用，且只对自己能删的组作答。
"""

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from nekro_agent.core.config import ModelConfigGroup
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_model_group import ModelGroupVisibility
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.console import ActionOkResponse
from nekro_agent.schemas.tenant import ModelGroupInfo, ModelGroupListResponse
from nekro_agent.services.model_group_refs import ModelGroupRefScope, find_model_group_references
from nekro_agent.services.model_group_service import (
    DeleteRefusal,
    build_model_group_info,
    can_see_credential,
    create_group,
    delete_group,
    delete_groups,
    get_editable_group,
    get_visible_group,
    list_visible_groups,
    resolve_console_identity,
    update_group,
)
from nekro_agent.services.model_group_test import (
    ModelTestErrorCode,
    prepare_model_group_test,
    run_model_group_test,
)
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role

logger = get_sub_logger("console_api")
router = APIRouter(tags=["Console"])


class ModelGroupPayload(BaseModel):
    """模型组的可写字段

    全部可选，未提交的字段保持原值。字段名是 `ModelConfigGroup` 同名字段的小写形态。
    """

    group_name: Optional[str] = Field(default=None, description="显示名称")
    chat_model: Optional[str] = Field(default=None, description="模型名称")
    chat_proxy: Optional[str] = Field(default=None, description="访问代理；等于掩码串时保持不变")
    base_url: Optional[str] = Field(default=None, description="接口地址；等于掩码串时保持不变")
    api_key: Optional[str] = Field(default=None, description="接口凭据；等于掩码串时保持不变")
    model_type: Optional[Literal["chat", "embedding", "draw"]] = Field(default=None, description="模型类型")
    enable_vision: Optional[bool] = Field(default=None, description="启用视觉功能")
    enable_cot: Optional[bool] = Field(default=None, description="启用外置思维链")
    token_input_rate: Optional[float] = Field(default=None, description="输入 Token 倍率")
    token_completion_rate: Optional[float] = Field(default=None, description="补全 Token 倍率")
    model_price_rate: Optional[float] = Field(default=None, description="模型价格倍率")
    temperature: Optional[float] = Field(default=None, description="温度值")
    top_p: Optional[float] = Field(default=None, description="Top P")
    top_k: Optional[int] = Field(default=None, description="Top K")
    presence_penalty: Optional[float] = Field(default=None, description="提示重复惩罚")
    frequency_penalty: Optional[float] = Field(default=None, description="补全重复惩罚")
    extra_body: Optional[str] = Field(default=None, description="额外参数 (JSON)")
    visibility: Optional[ModelGroupVisibility] = Field(
        default=None,
        description="平台组对租户的开放程度；仅平台方修改平台组时生效",
    )


class ModelGroupCreateRequest(ModelGroupPayload):
    """新建模型组；租户创建的自动归属该租户"""

    name: str = Field(..., description="模型组名，全局唯一，创建后不可改")


# 一次最多提交的组名数。界面一屏勾得到的远少于这个数，上限只是不让一次请求无界地连删
MAX_BATCH_DELETE = 200


class ModelGroupBatchDeleteRequest(BaseModel):
    """批量删除模型组"""

    names: List[str] = Field(..., min_length=1, max_length=MAX_BATCH_DELETE, description="要删除的模型组名")


class ModelGroupBatchDeleteFailure(BaseModel):
    """没删成的一个组"""

    name: str = Field(...)
    reason: DeleteRefusal = Field(
        ...,
        description=(
            "not_found：不存在或当前身份不可见；forbidden：可见但无权删除；default_group：默认模型组不可删除；"
            "error：删除时出错（详情见服务端日志）"
        ),
    )


class ModelGroupBatchDeleteResponse(BaseModel):
    """批量删除结果；逐个结算，`ok` 仅在全部删除成功时为 True"""

    ok: bool = Field(...)
    succeeded: List[str] = Field(default_factory=list, description="已删除的组名")
    failed: List[ModelGroupBatchDeleteFailure] = Field(default_factory=list)
    message: str = Field(default="")


class ModelGroupReferencesRequest(BaseModel):
    """查询模型组被哪些配置引用"""

    names: List[str] = Field(..., min_length=1, max_length=MAX_BATCH_DELETE, description="要查询的模型组名")


class ModelGroupReferenceInfo(BaseModel):
    """一处引用"""

    scope: ModelGroupRefScope = Field(..., description="引用所在的配置层")
    target: str = Field(
        default="",
        description="system 为空；adapter 为适配器标识；instance 为 `<adapter>/<instance>`；channel 为 chat_key；插件两层为插件键",
    )
    field: str = Field(..., description="配置字段名")
    title: str = Field(default="", description="配置字段标题")
    instance_key: str = Field(default="", description="仅 plugin_instance：插件在哪个实例上的配置")
    i18n_title: Optional[Dict[str, str]] = Field(default=None, description="字段标题的多语言版本")


class ModelGroupReferenceItem(BaseModel):
    """一个组的引用情况"""

    name: str = Field(...)
    total: int = Field(..., description="引用总数；references 可能被截断")
    references: List[ModelGroupReferenceInfo] = Field(default_factory=list)


class ModelGroupReferencesResponse(BaseModel):
    """只含当前身份可删除的组；其余组名不出现"""

    items: List[ModelGroupReferenceItem] = Field(default_factory=list)


class ModelGroupTestResult(BaseModel):
    """连通性测试结果；不回显任何凭据

    `message` 恒取自服务端预定义的有限文案集合（见 `model_group_test.NORMALIZED_MESSAGES`）。
    `response_text` / `error_detail` 只在调用方本就能看到该组凭据时才有值。
    """

    name: str = Field(...)
    success: bool = Field(default=False)
    error_code: ModelTestErrorCode = Field(default=ModelTestErrorCode.NONE, description="归一化结论")
    status_code: Optional[int] = Field(default=None, description="上游 HTTP 状态码；非 HTTP 失败为 None")
    latency_ms: int = Field(default=0)
    model_name: str = Field(default="")
    message: str = Field(default="", description="有限集合内的人类可读文案")
    response_text: str = Field(default="", description="上游回复；凭据不可见时恒为空")
    error_detail: str = Field(default="", description="上游原文（已脱敏）；凭据不可见时恒为空")


def _merge_payload(base: ModelConfigGroup, payload: ModelGroupPayload) -> ModelConfigGroup:
    """把提交的小写字段合并进 `ModelConfigGroup`

    只合并显式提交过的字段（`exclude_unset`），这样 `temperature: null` 表示"清空"，
    而完全不提交 `temperature` 表示"保持不变"，两者不会混淆。
    """
    data: Dict[str, Any] = base.model_dump()
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field == "visibility":
            continue
        data[field.upper()] = value
    return ModelConfigGroup.model_validate(data)


@router.get("/model-groups", summary="获取模型组列表")
@require_role(Role.Admin)
async def get_console_model_groups(
    _current_user: DBUser = Depends(get_current_active_user),
) -> ModelGroupListResponse:
    """获取当前身份可见的模型组

    租户看不到别的租户自建的组；平台组对租户可见但凭据恒为掩码。
    """
    identity = resolve_console_identity(_current_user)
    items = await list_visible_groups(identity)
    return ModelGroupListResponse(items=items, total=len(items))


@router.post("/model-groups", summary="新建模型组")
@require_role(Role.Admin)
async def create_console_model_group(
    body: ModelGroupCreateRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ModelGroupInfo:
    """新建模型组

    租户创建的自动归属该租户，且其 `base_url` / `chat_proxy` 必须指向公网。
    """
    identity = resolve_console_identity(_current_user)
    group = _merge_payload(ModelConfigGroup(), body)
    # 不提交 `visibility` 时把 None 原样传下去，由 `create_group` 用
    # `models/db_model_group.DEFAULT_VISIBILITY`（private）兜底。
    # 这里曾经写着 `body.visibility or ModelGroupVisibility.SHARED` —— 接口层自带一个
    # 与字段默认相反的默认值，是这类默认值最容易失守的地方
    row = await create_group(identity, body.name, group, body.visibility)
    return build_model_group_info(row, identity)


@router.patch("/model-groups/{name}", summary="更新模型组")
@require_role(Role.Admin)
async def update_console_model_group(
    name: str,
    body: ModelGroupPayload,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ModelGroupInfo:
    """更新模型组；平台组对租户只读"""
    identity = resolve_console_identity(_current_user)
    row = await get_editable_group(name, identity)
    updated = await update_group(identity, row, _merge_payload(row.to_config_group(), body), body.visibility)
    return build_model_group_info(updated, identity)


@router.delete("/model-groups/{name}", summary="删除模型组")
@require_role(Role.Admin)
async def delete_console_model_group(
    name: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    """删除模型组；平台组对租户只读"""
    identity = resolve_console_identity(_current_user)
    row = await get_editable_group(name, identity)
    await delete_group(row)
    return ActionOkResponse(ok=True, message=f"模型组 {name} 已删除")


@router.post("/model-groups/actions/references", summary="查询模型组被哪些配置引用")
@require_role(Role.Admin)
async def get_console_model_group_references(
    body: ModelGroupReferencesRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ModelGroupReferencesResponse:
    """删除确认框用：列出这些组被哪些配置项引用

    只对当前身份可删除的组作答；租户只看到自己实例下的引用（实例 / 频道覆盖与插件实例配置）。
    """
    identity = resolve_console_identity(_current_user)
    reports = await find_model_group_references(body.names, identity)
    return ModelGroupReferencesResponse(
        items=[
            ModelGroupReferenceItem(
                name=report.name,
                total=report.total,
                references=[ModelGroupReferenceInfo(**reference._asdict()) for reference in report.references],
            )
            for report in reports
        ],
    )


@router.post("/model-groups/actions/batch-delete", summary="批量删除模型组")
@require_role(Role.Admin)
async def batch_delete_console_model_groups(
    body: ModelGroupBatchDeleteRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ModelGroupBatchDeleteResponse:
    """批量删除模型组

    每个组的判定与单个删除相同：租户只能删自己租户的组，平台组对租户只读，
    不可见的组按不存在处理，默认模型组不可删除。
    """
    identity = resolve_console_identity(_current_user)
    outcome = await delete_groups(body.names, identity)
    failed = [ModelGroupBatchDeleteFailure(name=name, reason=reason) for name, reason in outcome.refused]
    deleted_count = len(outcome.deleted)
    return ModelGroupBatchDeleteResponse(
        ok=not failed,
        succeeded=outcome.deleted,
        failed=failed,
        message=f"已删除 {deleted_count} 个，失败 {len(failed)} 个" if failed else f"已删除 {deleted_count} 个模型组",
    )


@router.post("/model-groups/{name}/test", summary="模型组连通性测试")
@require_role(Role.Admin)
async def test_console_model_group(
    name: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ModelGroupTestResult:
    """用真实请求测试模型组连通性

    租户可以测试自己能用的组（含平台组），但整个链路不回显凭据：请求由后端发起，
    租户只拿到 `model_group_test` 归一化后的结论，上游原文不参与组装。
    """
    identity = resolve_console_identity(_current_user)
    row = await get_visible_group(name, identity)
    group = row.to_config_group()
    # 凭据可见性由 `can_see_credential` 单点判定，不在这里另算一遍
    credential_visible = can_see_credential(row, identity)

    await prepare_model_group_test(identity, group, credential_visible=credential_visible)
    # `name` 是身份键（`DBModelGroup.name`），不是 `group.GROUP_NAME` 那个显示名——
    # 账上的模型组名只认前一个命名空间。`identity.tenant_id` 让这笔支出落到点它的那个租户头上
    outcome = await run_model_group_test(
        group,
        credential_visible=credential_visible,
        group_name=name,
        tenant_id=identity.tenant_id,
    )

    return ModelGroupTestResult(
        name=name,
        success=outcome.ok,
        error_code=outcome.error_code,
        status_code=outcome.status_code,
        latency_ms=outcome.latency_ms,
        model_name=outcome.model_name,
        message=outcome.message,
        response_text=outcome.response_text,
        error_detail=outcome.detail,
    )
