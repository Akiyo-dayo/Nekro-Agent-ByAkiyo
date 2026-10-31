import asyncio
import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from nekro_agent.core.cc_model_presets import cc_presets_store
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.schemas.cc_model_preset import (
    CCModelPresetCreate,
    CCModelPresetInfo,
    CCModelPresetListResponse,
    CCModelPresetUpdate,
)
from nekro_agent.schemas.errors import ConflictError, NotFoundError, OperationFailedError
from nekro_agent.schemas.tenant import CREDENTIAL_MASK
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import get_tenant_scope, require_platform_owner
from nekro_agent.services.workspace.manager import WorkspaceService

router = APIRouter(prefix="/cc-model-presets", tags=["CC Model Presets"])

# ==================== 为什么整个写侧是平台方专属 ====================
#
# CC 预设**没有归属维度**：`cc_presets_store` 是一份进程共享的 JSON 文件，没有
# `owner_tenant_id` 这一列，也没有任何"这条预设属于谁"的概念。所以不存在"租户改自己的
# 预设"这回事——每一次写入都是在改平台方的资产，作用于全部工作区。
#
# 三条具体理由，任一条单独成立即可：
#
# 1. **PATCH 是一条现成的凭据外带通道。**`auth_token` 等于 `CREDENTIAL_MASK` 时会被剔除、
#    保留库里的真实 Key（下面的哨兵处理），而 `base_url` 没有这层保护。租户提交
#    `base_url=自己的服务器` + `auth_token=********`，服务端就会带着平台方**真实的**
#    Claude Key 去打租户指定的地址——脱敏、掩码一条都拦不住，因为请求本身"合法"。
#    这与模型组 `/config/model-groups/{name}` 判成平台方专属是同一条理由。
# 2. **test / test-inline / fetch-models 让服务端代为发起出站请求**，而这一族**没有**
#    模型组那边的 `assert_outbound_urls_allowed` 公网校验——填 `http://127.0.0.1:8021/x`
#    就能拿 NA 自己的网络身份探内网。要给租户开放，先得补上等价的出站校验。
# 3. **create / delete 改的是全体共用的一份清单**：租户建的预设别的租户也看得见、能绑；
#    删除会让引用它的工作区（可能属于别的租户）失去配置来源。
#
# 用 `require_platform_owner()` 而不是 `@require_role(Role.Super)`：未启用多租户时全部账号
# 的 `(tenant_id, tenant_role)` 恒为 `(NULL, "platform_owner")`，这条依赖对它们恒放行，
# 现有 `Role.Admin` 管理员的行为逐字不变；换成 `Role.Super` 会把这些账号一并挡在外面。
#
# 读侧（`/list`、`/{preset_id}`）保持 `Role.Admin` + 按身份脱敏不变：租户要在工作区里
# 选用预设，得先看得见它的名字。


class CCModelPresetTestItem(BaseModel):
    preset_id: int
    preset_name: str
    model_name: str
    success: bool
    latency_ms: int
    used_model: str | None = None
    response_text: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    error_message: str | None = None


class CCModelPresetTestRequest(BaseModel):
    preset_ids: list[int]


class CCModelPresetTestResponse(BaseModel):
    items: list[CCModelPresetTestItem]


class CCModelPresetInlineTestRequest(BaseModel):
    base_url: str
    auth_token: str
    model: str
    api_timeout_ms: str | None = None


class CCModelPresetFetchModelsRequest(BaseModel):
    base_url: str
    auth_token: str


class CCModelPresetFetchModelsResponse(BaseModel):
    models: list[str]


def _to_info(item, *, credential_visible: bool = True) -> CCModelPresetInfo:
    """把预设行转成响应对象

    `auth_token` 与 `config_json.env.ANTHROPIC_AUTH_TOKEN` 是**平台方的 Claude 凭据原文**。
    这两个读端点此前既没有 `@require_role`，也不脱敏——任何登录账号（包括只读的租户普通用户）
    直接就能取走。`credential_visible=False` 时 `auth_token` 回落成掩码哨兵、`config_json` 置空
    （后者整份是要写进容器 `settings.json` 的环境变量，掩掉单个字段没有意义，也没有部分可用的语义）。

    掩码用的是 `CREDENTIAL_MASK`，与模型组那套一致：写回时该哨兵表示"保持不变"。
    """
    return CCModelPresetInfo(
        id=item.id,
        name=item.name,
        description=item.description,
        base_url=item.base_url,
        auth_token=item.auth_token if credential_visible else (CREDENTIAL_MASK if item.auth_token else ""),
        api_timeout_ms=item.api_timeout_ms,
        model_type=item.model_type,
        preset_model=item.preset_model,
        anthropic_model=item.anthropic_model,
        small_fast_model=item.small_fast_model,
        default_sonnet=item.default_sonnet,
        default_opus=item.default_opus,
        default_haiku=item.default_haiku,
        extra_env=item.extra_env,
        is_default=item.is_default,
        create_time=item.create_time,
        update_time=item.update_time,
        config_json=item.to_config_json() if credential_visible else None,
    )


def _credential_visible(user: DBUser) -> bool:
    """只有平台方能看到 CC 预设里的凭据原文（未启用多租户时恒为平台方）"""
    return get_tenant_scope(user) is None


@router.get("/list", response_model=CCModelPresetListResponse)
@require_role(Role.Admin)
async def list_presets(_current_user: DBUser = Depends(get_current_active_user)):
    items = cc_presets_store.list_all()
    visible = _credential_visible(_current_user)
    return CCModelPresetListResponse(
        total=len(items),
        items=[_to_info(p, credential_visible=visible) for p in items],
    )


@router.post("/test-inline", response_model=CCModelPresetTestItem)
@require_role(Role.Admin)
async def test_preset_inline(
    body: CCModelPresetInlineTestRequest,
    _current_user: DBUser = Depends(get_current_active_user),
    _owner: DBUser = Depends(require_platform_owner()),
) -> CCModelPresetTestItem:
    """使用临时参数直接测试 Anthropic 模型（不需要先保存预设）。

    平台方专属，理由见模块顶部第 2 条：`base_url` 完全由调用方给定，服务端照它发请求。
    """
    from nekro_agent.services.agent.anthropic import test_anthropic_messages

    if not body.base_url.strip():
        return CCModelPresetTestItem(
            preset_id=0, preset_name="inline-test", model_name=body.model,
            success=False, latency_ms=0, error_message="Base URL 不能为空",
        )
    if not body.auth_token.strip():
        return CCModelPresetTestItem(
            preset_id=0, preset_name="inline-test", model_name=body.model,
            success=False, latency_ms=0, error_message="Auth Token 不能为空",
        )
    if not body.model.strip():
        return CCModelPresetTestItem(
            preset_id=0, preset_name="inline-test", model_name="",
            success=False, latency_ms=0, error_message="模型名称不能为空",
        )

    started = time.perf_counter()
    try:
        result = await test_anthropic_messages(
            base_url=body.base_url,
            auth_token=body.auth_token,
            model=body.model.strip(),
            api_timeout_ms=body.api_timeout_ms,
        )
        latency_ms = max(1, int((time.perf_counter() - started) * 1000))
        return CCModelPresetTestItem(
            preset_id=0, preset_name="inline-test", model_name=body.model.strip(),
            success=True, latency_ms=latency_ms,
            used_model=result.model, response_text=result.response_text,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        )
    except Exception as e:
        latency_ms = max(1, int((time.perf_counter() - started) * 1000))
        return CCModelPresetTestItem(
            preset_id=0, preset_name="inline-test", model_name=body.model.strip(),
            success=False, latency_ms=latency_ms, error_message=str(e),
        )


@router.post("/fetch-models", response_model=CCModelPresetFetchModelsResponse)
@require_role(Role.Admin)
async def fetch_cc_models(
    body: CCModelPresetFetchModelsRequest,
    _current_user: DBUser = Depends(get_current_active_user),
    _owner: DBUser = Depends(require_platform_owner()),
) -> CCModelPresetFetchModelsResponse:
    """通过 Anthropic API 拉取可用模型列表。

    平台方专属，理由同 `test-inline`：`base_url` 由调用方给定，服务端照它发请求。
    """
    from nekro_agent.services.agent.anthropic import list_anthropic_models

    try:
        models = await list_anthropic_models(
            base_url=body.base_url,
            auth_token=body.auth_token,
        )
    except Exception as e:
        raise OperationFailedError(operation="拉取 Anthropic 模型列表", detail=str(e)) from e

    return CCModelPresetFetchModelsResponse(models=models)


@router.post("/test", response_model=CCModelPresetTestResponse)
@require_role(Role.Admin)
async def test_presets(
    body: CCModelPresetTestRequest,
    _current_user: DBUser = Depends(get_current_active_user),
    _owner: DBUser = Depends(require_platform_owner()),
):
    """用库里存着的凭据真实打一次 Anthropic 接口（平台方专属）

    这条本身不接受调用方给的地址，但它带着平台方的 Key 打的是**库里那个** `base_url`——
    与 PATCH 组合起来就是完整的外带链路，而且失败时 `error_message` 直接是 `str(e)`，
    上游异常里常带请求上下文。既然写侧整体归平台方，这条没有单独放开的理由。
    """
    from nekro_agent.services.agent.anthropic import resolve_cc_test_model, test_anthropic_messages

    async def run_single(preset_id: int) -> CCModelPresetTestItem:
        preset = cc_presets_store.get_by_id(preset_id)
        if not preset:
            return CCModelPresetTestItem(
                preset_id=preset_id,
                preset_name="",
                model_name="",
                success=False,
                latency_ms=0,
                error_message=f"CC 模型组不存在: {preset_id}",
            )

        model_name = resolve_cc_test_model(
            model_type=preset.model_type,
            preset_model=preset.preset_model,
            anthropic_model=preset.anthropic_model,
            small_fast_model=preset.small_fast_model,
            default_sonnet=preset.default_sonnet,
            default_opus=preset.default_opus,
            default_haiku=preset.default_haiku,
        )
        if not preset.base_url.strip():
            return CCModelPresetTestItem(
                preset_id=preset.id,
                preset_name=preset.name,
                model_name=model_name,
                success=False,
                latency_ms=0,
                error_message="Base URL 不能为空",
            )
        if not preset.auth_token.strip():
            return CCModelPresetTestItem(
                preset_id=preset.id,
                preset_name=preset.name,
                model_name=model_name,
                success=False,
                latency_ms=0,
                error_message="Auth Token 不能为空",
            )
        if not model_name:
            return CCModelPresetTestItem(
                preset_id=preset.id,
                preset_name=preset.name,
                model_name="",
                success=False,
                latency_ms=0,
                error_message="缺少可用于测试的模型名称",
            )

        started = time.perf_counter()
        try:
            result = await test_anthropic_messages(
                base_url=preset.base_url,
                auth_token=preset.auth_token,
                model=model_name,
                api_timeout_ms=preset.api_timeout_ms,
            )
            latency_ms = max(1, int((time.perf_counter() - started) * 1000))
            return CCModelPresetTestItem(
                preset_id=preset.id,
                preset_name=preset.name,
                model_name=model_name,
                success=True,
                latency_ms=latency_ms,
                used_model=result.model,
                response_text=result.response_text,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
        except Exception as e:
            latency_ms = max(1, int((time.perf_counter() - started) * 1000))
            return CCModelPresetTestItem(
                preset_id=preset.id,
                preset_name=preset.name,
                model_name=model_name,
                success=False,
                latency_ms=latency_ms,
                error_message=str(e),
            )

    items = await asyncio.gather(*(run_single(preset_id) for preset_id in body.preset_ids))
    return CCModelPresetTestResponse(items=items)


@router.post("", response_model=CCModelPresetInfo)
@require_role(Role.Admin)
async def create_preset(
    body: CCModelPresetCreate,
    _current_user: DBUser = Depends(get_current_active_user),
    _owner: DBUser = Depends(require_platform_owner()),
):
    """新建 CC 预设（平台方专属，理由见模块顶部第 3 条）"""
    if cc_presets_store.name_exists(body.name):
        raise ConflictError(resource=f"CC 模型预设 '{body.name}'")
    item = cc_presets_store.create(**body.model_dump())
    return _to_info(item)


@router.get("/{preset_id}", response_model=CCModelPresetInfo)
@require_role(Role.Admin)
async def get_preset(preset_id: int, _current_user: DBUser = Depends(get_current_active_user)):
    item = cc_presets_store.get_by_id(preset_id)
    if not item:
        raise NotFoundError(resource=f"CC 模型预设 {preset_id}")
    return _to_info(item, credential_visible=_credential_visible(_current_user))


@router.patch("/{preset_id}", response_model=CCModelPresetInfo)
@require_role(Role.Admin)
async def update_preset(
    preset_id: int,
    body: CCModelPresetUpdate,
    _current_user: DBUser = Depends(get_current_active_user),
    _owner: DBUser = Depends(require_platform_owner()),
):
    """更新 CC 预设（平台方专属，理由见模块顶部第 1 条：`base_url` 可写 + 掩码哨兵保留真 Key）"""
    item = cc_presets_store.get_by_id(preset_id)
    if not item:
        raise NotFoundError(resource=f"CC 模型预设 {preset_id}")
    data = body.model_dump(exclude_none=True)
    # 掩码哨兵表示"保持不变"：读端点对租户返回的是 `********`，原样提交回来不能被当成新凭据存进去，
    # 否则一次无关字段的编辑就会把平台方的 Claude Key 覆盖成掩码串。与模型组那套语义一致。
    if data.get("auth_token") == CREDENTIAL_MASK:
        data.pop("auth_token")
    if "name" in data and data["name"] != item.name:
        if cc_presets_store.name_exists(data["name"], exclude_id=preset_id):
            raise ConflictError(resource=f"CC 模型预设 '{data['name']}'")
    updated = cc_presets_store.update(preset_id, **data)
    if updated is None:
        raise OperationFailedError(operation="更新 CC 模型预设", detail=f"预设 {preset_id} 更新失败")
    await WorkspaceService.sync_workspace_settings_for_preset(updated.id, updated)
    return _to_info(updated, credential_visible=_credential_visible(_current_user))


@router.delete("/{preset_id}")
@require_role(Role.Admin)
async def delete_preset(
    preset_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
    _owner: DBUser = Depends(require_platform_owner()),
):
    """删除 CC 预设（平台方专属，理由见模块顶部第 3 条）"""
    bound_workspaces = []
    for workspace in await DBWorkspace.all():
        raw_preset_id = (workspace.metadata or {}).get("cc_model_preset_id")
        try:
            if raw_preset_id is not None and int(raw_preset_id) == preset_id:
                bound_workspaces.append(workspace.name)
        except (TypeError, ValueError):
            continue

    if bound_workspaces:
        raise ConflictError(
            resource=f"CC 模型组 {preset_id}",
            detail=f"仍被工作区引用: {', '.join(bound_workspaces[:10])}",
            data={"workspaces": bound_workspaces},
        )

    result = cc_presets_store.delete(preset_id)
    if result == "not_found":
        raise NotFoundError(resource=f"CC 模型预设 {preset_id}")
    if result == "protected":
        raise OperationFailedError(operation="删除 CC 模型预设", detail="默认预设不可删除")
    return {"ok": True}
