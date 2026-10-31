import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel
from tortoise.queryset import QuerySet

from nekro_agent.core.core_utils import ConfigBase
from nekro_agent.models.db_plugin_data import DBPluginData
from nekro_agent.models.db_user import DBUser

# 跨路由复用三处已有的作用域设施，而不是在本模块另写一套频道/凭据/归属判定：
# - `chat_channel` 的频道守卫与可见频道查询集是频道归属的唯一出口；
# - `config._mask_config_items` / `config.restore_masked_credentials` 是租户视角凭据脱敏与
#   回写还原的既有口径（显式 `is_secret` 标记优先、字段名兜底，且都会递归进 dict/list），
#   插件配置与通用配置走同一套判定才不会一边掩一边漏；
# - `model_group_service` 的 `iter_model_group_ref_fields` / `assert_model_group_selectable`
#   是"这个字段引用了模型组"与"这个身份能不能选它"的唯一判定，通用配置写入闸门用的是同一对。
from nekro_agent.routers.chat_channel import assert_channel_accessible, visible_channels
from nekro_agent.routers.config import _mask_config_items, restore_masked_credentials
from nekro_agent.schemas.errors import NotFoundError, PluginLoadError, PluginNotFoundError, ValidationError
from nekro_agent.schemas.tenant import CREDENTIAL_MASK
from nekro_agent.services.model_group_service import (
    assert_model_group_selectable,
    iter_model_group_ref_fields,
    resolve_console_identity,
)
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.plugin.manager import (
    disable_plugin,
    enable_plugin,
    get_all_ext_meta_data,
    get_all_plugin_router_info,
    get_plugin_detail,
    get_plugin_instance_config,
    get_plugin_scope_states,
    save_plugin_instance_config,
    set_plugin_scope_override,
    update_plugin_activation_strategy,
)
from nekro_agent.services.plugin.scope import OverrideScope, VisibilitySource
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import assert_instance_accessible, get_tenant_scope
from nekro_agent.tools.redaction import is_credential_mask, redact_internal_endpoints

router = APIRouter(prefix="/plugins", tags=["Plugins"])

# 加载失败的插件在清单/详情里带 `filePath`（宿主机绝对路径）与 `stackTrace`（进程内堆栈）。
# 那是宿主的内部情报，不是插件本身的一部分；去掉之后租户仍有 `errorMessage` / `errorType`，
# 「这个插件挂了、挂在什么错上」一条不少。
_HOST_INTERNAL_PLUGIN_FIELDS = ("filePath", "stackTrace")

# 但只删这两个字段不够：加载失败条目的 `errorMessage` 就是异常文本本身，`description` 又是它的副本，
# 而 `SyntaxError: invalid syntax (C:\...\plugins\builtin\x.py, line 3)` 这类消息会把同一条宿主路径
# 原样带回来（实测）。这两项过一遍 `redact_internal_endpoints`：路径与内网端点换成占位符，
# 错误类型与描述文字原样保留，租户仍看得出「挂在什么错上」。
_PLUGIN_ERROR_TEXT_FIELDS = ("description", "errorMessage")


async def assert_plugin_scope_accessible(user: DBUser, instance_key: str, chat_key: str = "") -> None:
    """校验当前身份能否操作某个作用域下的插件设置

    平台方不受限；租户传入他人（或不存在）的 `instance_key` / `chat_key` 一律 404，
    两者对外不可区分。

    `chat_key` 必须单独校验：会话级覆盖落在 `chat_key` 这一层（见 `plugin/scope.py` 的
    三层优先级），只校验 `instance_key` 等于让租户拿自己的实例标识当通行证，
    去改别人频道里的插件可见性。
    """
    if get_tenant_scope(user) is None:
        return
    await assert_instance_accessible(user, instance_key)
    if chat_key:
        await assert_channel_accessible(user, chat_key)


async def accessible_plugin_data(user: DBUser, plugin_key: str) -> QuerySet[DBPluginData]:
    """按当前身份的租户作用域筛出可读写的插件数据行

    平台方不受限（查询集不加任何条件，与改造前完全一致）。租户只看得到
    `target_chat_key` 落在自己实例名下频道的行：`target_chat_key` 为空的行是插件的
    **全局存储**，不挂在任何频道上、因而不属于任何租户，对租户一律不可见。
    """
    query = DBPluginData.filter(plugin_key=plugin_key)
    if get_tenant_scope(user) is None:
        return query
    return query.filter(target_chat_key__in=await (await visible_channels(user)).values_list("chat_key", flat=True))


def visible_plugin_meta(user: DBUser, meta: Dict[str, Any]) -> Dict[str, Any]:
    """按当前身份裁剪插件元数据里的宿主内部字段

    插件清单是全体共用的一份（与技能库同理），没有「每租户一份」这回事，
    所以只裁字段、不裁条目；平台方拿到的对象逐字不变。
    """
    if get_tenant_scope(user) is None:
        return meta
    visible = {key: value for key, value in meta.items() if key not in _HOST_INTERNAL_PLUGIN_FIELDS}
    if not meta.get("loadFailed"):
        # 正常加载的插件没有异常文本，描述是插件作者写的介绍，不该进脱敏
        return visible
    return {
        key: redact_internal_endpoints(value) if key in _PLUGIN_ERROR_TEXT_FIELDS and isinstance(value, str) else value
        for key, value in visible.items()
    }


async def restore_masked_plugin_config(plugin_key: str, instance_key: str, configs: Dict[str, str]) -> Dict[str, str]:
    """把租户提交值里的掩码位置还原成该实例当前的真实取值

    读侧的 `_mask_config_items` 是**递归**的：它不只掩标量 `is_secret` 字段，还会按键名掩掉
    dict/list 里嵌着的凭据（`{"openai_api_key": "..."}`、`EXTRA_TOKENS: [...]` 都命中）。
    因此只判「整值等于掩码」接不住复杂字段 —— 租户把读到的表单原样保存一次，实例覆盖里就会
    落下一个字面量 `********`，把这份配置配坏（实测：`PROVIDERS` 的嵌套 Key 被替换成星号）。
    还原口径与 `config.py:_sanitize_config_updates` 一致。
    """
    if not any(CREDENTIAL_MASK in value for value in configs.values()):
        return configs

    current = {str(item["key"]): item["value"] for item in await get_plugin_instance_config(plugin_key, instance_key) or []}

    restored: Dict[str, str] = {}
    for key, raw_value in configs.items():
        if is_credential_mask(raw_value):
            # 整值就是掩码：该项从本次更新里剔除，保持不变
            continue
        if CREDENTIAL_MASK not in raw_value:
            restored[key] = raw_value
            continue
        try:
            parsed = json.loads(raw_value)
        except ValueError:
            restored[key] = raw_value
            continue
        if not isinstance(parsed, (dict, list)):
            # 标量字段的传输格式是原始字符串而非 JSON（见 `ConfigService.set_config_value`：
            # str 字段直接 setattr 原串），回写 json.dumps 会给它多套一层引号
            restored[key] = raw_value
            continue
        restored[key] = json.dumps(restore_masked_credentials(parsed, current.get(key)), ensure_ascii=False)
    return restored


async def assert_tenant_may_select_model_groups(user: DBUser, plugin_key: str, configs: Dict[str, str]) -> None:
    """插件配置里引用模型组的字段，租户只能填自己看得见的组

    这是 `config.py:_assert_tenant_may_write` 那扇门的**第二个出口**。通用配置那一侧
    （`instance_override_*` / `channel_config_*`）已经按 `ref_model_groups` 标记把住了写入，
    但插件配置根本不走那条路由：本端点直接落 `save_instance_config_overrides()`，
    此前一次归属判定都没有。而插件里的这类字段与 `USE_MODEL_GROUP` 是同一个形状 ——
    `view_image.VISION_MODEL` / `draw.USE_DRAW_MODEL_GROUP` / `emotion.EMBEDDING_MODEL` /
    `email_utils.DEFAULT_SUMMARY_MODEL_GROUP` 运行时都拿去
    `core_config.MODEL_GROUPS[<名字>]` 取真实 Key。租户在**自己的实例**上把它改成平台方的
    private 组，此后每一次画图/看图/嵌入都走平台方那把 Key：作用域守卫是对的（写的确实是
    他自己的实例），被绕过的是 `visibility=private` 这条授权与计费边界。

    判定不另写一套：字段由 `iter_model_group_ref_fields()` 按标记枚举（插件用
    `ExtraField(ref_model_groups=True)` 或等价的裸 dict 声明，两种都认），
    归属由 `assert_model_group_selectable()` 判（内部即 `can_view_group`），
    不可见与不存在同样 404。

    调用点在 `restore_masked_plugin_config()` **之后**：整值是掩码的项在那一步就被剔除了
    （"保持不变"，一个字都不落盘），因此到这里的每一个取值都是本次真要写下去的值。

    **身份判定不在这里做第二遍**（形态与 `config.py:_assert_tenant_may_write` 一致）：
    调用点已经在 `get_tenant_scope(...) is not None` 这一支里，而 `assert_model_group_selectable`
    内部对平台方本就直接返回。再加一次 `is None: return` 是一条任何变异都杀不掉的死分支——
    看着像边界、其实不是，而不可证伪的"防御"只会让下一个人以为这里已经守住了。
    平台方与未启用多租户的部署因此行为逐字不变。
    """
    plugin = plugin_collector.get_plugin(plugin_key)
    if plugin is None or plugin.config_class is ConfigBase:
        return
    ref_field_names = {ref.name for ref in iter_model_group_ref_fields(plugin.config_class)}
    if not ref_field_names:
        return
    identity = resolve_console_identity(user)
    for item_key, raw_value in configs.items():
        if item_key in ref_field_names:
            await assert_model_group_selectable(raw_value, identity)


class TogglePluginRequest(BaseModel):
    """切换插件状态请求体"""

    enabled: bool


class PluginScopeOverrideRequest(BaseModel):
    """设置插件作用域启停覆盖请求体"""

    enabled: Optional[bool] = None
    """`true`/`false` 为显式覆盖，`null` 表示清除覆盖、回落上一层"""


class PluginScopeStatus(BaseModel):
    """插件在某作用域下的生效状态"""

    plugin_key: str
    name: str
    enabled: bool
    source: VisibilitySource
    """生效值来自哪一层：session > instance > global"""
    inherited_enabled: bool
    """未被本层覆盖时会取到的上层状态"""
    instance_override: Optional[bool] = None
    session_override: Optional[bool] = None
    loaded: bool
    """插件在本进程内是否已加载（进程级状态，与作用域可见性正交）"""


class PluginScopeResponse(BaseModel):
    """插件作用域状态列表"""

    instance_key: str
    chat_key: str = ""
    items: List[PluginScopeStatus]


class SavePluginConfigRequest(BaseModel):
    configs: Dict[str, str]


class BatchUpdateConfig(BaseModel):
    """批量更新配置请求体"""

    configs: Dict[str, str]


class PluginActivationStrategyRequest(BaseModel):
    strategy: str


class ActionResponse(BaseModel):
    ok: bool = True


class PluginDocsResponse(BaseModel):
    docs: str | None
    exists: bool


class PluginDataResponse(BaseModel):
    """插件数据响应模型"""

    id: int
    plugin_key: str
    target_chat_key: str
    target_user_id: str
    data_key: str
    data_value: str
    create_time: str
    update_time: str

    @classmethod
    def from_orm(cls, obj: DBPluginData) -> "PluginDataResponse":
        return cls(
            id=obj.id,
            plugin_key=obj.plugin_key,
            target_chat_key=obj.target_chat_key,
            target_user_id=obj.target_user_id,
            data_key=obj.data_key,
            data_value=obj.data_value,
            create_time=obj.create_time.isoformat(),
            update_time=obj.update_time.isoformat(),
        )


# 这三个响应形状此前与实际返回值对不上：`debug_routes()` / `verify_plugin_routes()` 返回的是
# 路径字符串列表，`get_all_plugin_router_info()` 返回的是一个汇总字典，而模型写的是 `list[dict]`。
# `router-info` 因此**每次调用都是 500**（实测），另两条则是路由表非空时才炸。
class PluginRoutesResponse(BaseModel):
    plugin_routes: list[str]
    debug_completed: bool


class PluginRouteVerifyResponse(BaseModel):
    plugin_key: str
    found_routes: list[str]
    routes_count: int


@router.get("/list", summary="获取插件列表", response_model=List[dict])
@require_role(Role.Admin)
async def get_plugins(
    _current_user: DBUser = Depends(get_current_active_user),
) -> List[dict]:
    """获取插件列表

    插件目录全体共用一份，条目不按租户裁剪；只有加载失败条目里的宿主路径与堆栈会被去掉。
    各插件在**某个实例**下的生效状态请走 `GET /plugins/scope/{instance_key}`。
    """
    return [visible_plugin_meta(_current_user, meta) for meta in await get_all_ext_meta_data()]


@router.get("/detail/{plugin_id}", summary="获取插件详情", response_model=dict)
@require_role(Role.Admin)
async def get_plugin_detail_handler(
    plugin_id: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> dict:
    """获取插件详情

    返回的是插件自身的元数据（方法、webhook、路由形状），不含任何配置取值。
    """
    plugin = await get_plugin_detail(plugin_id)
    if not plugin:
        raise PluginNotFoundError(plugin_id=plugin_id)
    return visible_plugin_meta(_current_user, plugin)


# 全局启停写的是 `CoreConfig.PLUGIN_ENABLED`，并在本进程内加载/卸载插件与它的自定义路由 ——
# 一个进程跑着所有租户的实例，租户点一下就能把别人正在用的插件关掉。
# 租户要调整自己的可见范围走 `PUT /plugins/scope/{instance_key}/{plugin_key}`（实例/会话层覆盖），
# 那条路径不动全局开关。因此本端点是平台方专属。
@router.post("/toggle/{plugin_id}", summary="启用/禁用插件（全局）", response_model=ActionResponse)
@require_role(Role.Super)
async def toggle_plugin(
    plugin_id: str,
    body: TogglePluginRequest = Body(...),
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """启用/禁用插件"""
    module_name = plugin_id.split(".")[-1]
    failed_plugin = plugin_collector.get_failed_plugin_by_module_name(module_name)
    if failed_plugin:
        raise PluginLoadError(plugin_id=plugin_id, detail=failed_plugin.error_message)

    if body.enabled:
        success = await enable_plugin(plugin_id)
    else:
        success = await disable_plugin(plugin_id)

    if not success:
        raise PluginNotFoundError(plugin_id=plugin_id)
    return ActionResponse(ok=True)


@router.get("/scope/{instance_key}", summary="获取插件在指定作用域下的生效状态", response_model=PluginScopeResponse)
@require_role(Role.Admin)
async def get_plugin_scope(
    instance_key: str,
    chat_key: str = Query("", description="会话标识，传入后额外叠加会话级覆盖"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginScopeResponse:
    """获取各插件在该实例（可选叠加会话）下的生效状态与来源层级

    `source` 指出生效值来自哪一层：`session` > `instance` > `global`。
    """
    await assert_plugin_scope_accessible(_current_user, instance_key, chat_key)

    states = await get_plugin_scope_states(instance_key, chat_key)
    items: List[PluginScopeStatus] = []
    for state in states:
        plugin = plugin_collector.get_plugin(state.plugin_key)
        items.append(
            PluginScopeStatus(
                plugin_key=state.plugin_key,
                name=plugin.name if plugin else state.plugin_key,
                enabled=state.enabled,
                source=state.source,
                # 本层覆盖被清除后会回落到的状态：会话层看实例层，实例层看全局
                inherited_enabled=(
                    state.instance_override if state.source == "session" and state.instance_override is not None else state.global_enabled
                ),
                instance_override=state.instance_override,
                session_override=state.session_override,
                loaded=bool(plugin and plugin.is_enabled),
            ),
        )
    return PluginScopeResponse(instance_key=instance_key, chat_key=chat_key, items=items)


@router.put("/scope/{instance_key}/{plugin_key}", summary="设置插件的作用域启停覆盖", response_model=ActionResponse)
@require_role(Role.Admin)
async def set_plugin_scope(
    instance_key: str,
    plugin_key: str,
    body: PluginScopeOverrideRequest = Body(...),
    chat_key: str = Query("", description="传入后改为写会话级覆盖，否则写实例级覆盖"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """设置或清除插件的作用域启停覆盖

    `enabled` 为 `null` 时清除该层覆盖，使其回落到上一层（会话 -> 实例 -> 全局）。
    """
    await assert_plugin_scope_accessible(_current_user, instance_key, chat_key)

    scope = OverrideScope.SESSION if chat_key else OverrideScope.INSTANCE
    scope_key = chat_key or instance_key
    if not scope_key:
        raise ValidationError(reason="作用域标识不能为空")

    success, error = await set_plugin_scope_override(scope, scope_key, plugin_key, body.enabled)
    if not success:
        raise ValidationError(reason=error or "插件作用域覆盖设置失败")
    return ActionResponse(ok=True)


# 同一处理函数挂两条路径：
# - `/{plugin_key}/config/{instance_key}` 是对外约定的形状；
# - `/config/{plugin_key}/{instance_key}` 是无冲突的等价别名。
#   插件的自定义路由挂载在 `/plugins/{plugin_key}` 下、且是在核心路由之后动态 include 的，
#   前一种形状理论上会抢先匹配掉某个插件自己的 `/config/{...}` 路由；插件键必含 `.`，
#   字面量 `config` 与之不可能重名，因此别名形状可以零风险共存，遇到冲突时改用它即可。
@router.get("/config/{plugin_key}/{instance_key}", summary="获取插件的实例级配置", response_model=List[dict])
@router.get("/{plugin_key}/config/{instance_key}", summary="获取插件的实例级配置", response_model=List[dict])
@require_role(Role.Admin)
async def get_plugin_instance_config_handler(
    plugin_key: str,
    instance_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> List[Dict[str, Any]]:
    """获取插件在指定实例下生效的配置

    未被实例覆盖的字段返回全局值，配置项上的 `overridden` 标记指出该字段是否为实例级覆盖。
    租户身份下凭据一律为掩码。
    """
    await assert_plugin_scope_accessible(_current_user, instance_key)

    configs = await get_plugin_instance_config(plugin_key, instance_key)
    if configs is None:
        raise PluginNotFoundError(plugin_id=plugin_key)
    if get_tenant_scope(_current_user) is not None:
        # 这里解析出来的是「全局值 + 本实例覆盖」，**未被覆盖的凭据字段回落的正是平台方
        # 全局配好的真实 Key**（`google_search` 的 API_KEY/CX、`github` 的 TOKEN 都是
        # `is_secret=True` 的字段）。作用域守卫只挡得住「读别人的实例」，挡不住
        # 「从自己的实例里读平台方的 Key」，所以读侧必须再脱敏一层。
        configs = _mask_config_items(configs)
    return configs


@router.put("/config/{plugin_key}/{instance_key}", summary="写入插件的实例级配置覆盖", response_model=ActionResponse)
@router.put("/{plugin_key}/config/{instance_key}", summary="写入插件的实例级配置覆盖", response_model=ActionResponse)
@require_role(Role.Admin)
async def save_plugin_instance_config_handler(
    plugin_key: str,
    instance_key: str,
    body: SavePluginConfigRequest = Body(...),
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """写入插件的实例级配置覆盖

    只有与全局取值不同的字段会落盘；把某字段改回与全局一致即等于撤销覆盖、继续跟随全局。

    引用模型组的字段还要过一道归属判定（`assert_tenant_may_select_model_groups`）：
    作用域守卫只保证"写的是自己的实例"，保证不了"填的是自己看得见的组"。
    """
    await assert_plugin_scope_accessible(_current_user, instance_key)

    configs = body.configs
    if get_tenant_scope(_current_user) is not None:
        # 读侧回给租户的凭据是掩码串，原样提交回来必须当作「保持不变」而不是一个新取值，
        # 否则一次无意的保存就会给该实例落一条 `********` 的覆盖，把插件配坏。
        # 掩码可能整值命中，也可能嵌在复杂字段里，两种都要还原。
        configs = await restore_masked_plugin_config(plugin_key, instance_key, configs)
        await assert_tenant_may_select_model_groups(_current_user, plugin_key, configs)

    success, error = await save_plugin_instance_config(plugin_key, instance_key, configs)
    if not success:
        raise ValidationError(reason=error or "实例级插件配置保存失败")
    return ActionResponse(ok=True)


# 激活策略决定插件在本进程里是常驻还是可休眠，是**全进程一份**的设置，
# 与全局启停同一个道理：租户改它会改到所有人头上。
@router.post("/activation-strategy/{plugin_id}", summary="更新插件激活策略（全局）", response_model=ActionResponse)
@require_role(Role.Super)
async def update_plugin_activation_strategy_handler(
    plugin_id: str,
    body: PluginActivationStrategyRequest = Body(...),
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    if body.strategy not in {"auto", "allow_sleep", "forbid_sleep"}:
        raise ValidationError(reason="无效的插件激活策略")
    success, error = await update_plugin_activation_strategy(plugin_id, body.strategy)  # type: ignore[arg-type]
    if not success:
        raise ValidationError(reason=error or "插件激活策略更新失败")
    return ActionResponse(ok=True)


@router.get("/docs/{plugin_id}", summary="获取插件文档", response_model=PluginDocsResponse)
@require_role(Role.Admin)
async def get_plugin_docs(
    plugin_id: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginDocsResponse:
    """获取插件文档"""
    plugin = plugin_collector.get_plugin(plugin_id)
    if not plugin:
        raise PluginNotFoundError(plugin_id=plugin_id)
    docs = await plugin.get_docs()
    return PluginDocsResponse(docs=docs, exists=docs is not None)


# 重载会重新 import 插件模块并在进程内替换它，等于让一份服务端可信代码重新执行，
# 且影响所有实例；与插件编辑器（`routers/plugin_editor.py`）同属写/跑服务端代码这一档。
@router.post("/reload", summary="重载插件", response_model=ActionResponse)
@require_role(Role.Super)
async def reload_plugins(module_name: str, _current_user: DBUser = Depends(get_current_active_user)) -> ActionResponse:
    """重载插件"""
    await plugin_collector.reload_plugin_by_module_name(module_name)
    return ActionResponse(ok=True)


@router.get("/data/{plugin_id}", summary="获取插件数据列表", response_model=List[PluginDataResponse])
@require_role(Role.Admin)
async def get_plugin_data(
    plugin_id: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> List[PluginDataResponse]:
    """获取插件数据列表（租户只看得到自己实例名下频道的数据）"""
    data = await (await accessible_plugin_data(_current_user, plugin_id)).all()
    return [PluginDataResponse.from_orm(item) for item in data]


@router.delete("/data/{plugin_id}/{data_id}", summary="删除插件数据", response_model=ActionResponse)
@require_role(Role.Admin)
async def delete_plugin_data(
    plugin_id: str,
    data_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """删除插件数据（租户够不到的行与不存在的行同样 404）"""
    data = await (await accessible_plugin_data(_current_user, plugin_id)).filter(id=data_id).first()
    if not data:
        raise NotFoundError(resource="插件数据")
    await data.delete()
    return ActionResponse(ok=True)


@router.delete("/data/{plugin_id}", summary="重置插件数据", response_model=ActionResponse)
@require_role(Role.Admin)
async def reset_plugin_data(
    plugin_id: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """重置插件数据

    与列表口径一致：平台方清空该插件的全部数据，租户只清空自己实例名下频道的那些行。
    """
    await (await accessible_plugin_data(_current_user, plugin_id)).delete()
    return ActionResponse(ok=True)


# 云端插件的安装/更新会把 git 仓库里的任意 Python 拉进服务端执行，与插件编辑器同属
# 「写入服务端可信代码」，因此同样要求 Role.Super（平台方专属），租户管理员够不到。
@router.delete("/package/{module_name}", summary="删除云端插件", response_model=ActionResponse)
@require_role(Role.Super)
async def remove_package(
    module_name: str,
    clear_data: bool = Query(False, description="是否删除插件在数据库中的存储数据"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """删除云端插件"""
    if clear_data:
        await plugin_collector.remove_package(module_name, clear_config=True)
        await plugin_collector.clear_plugin_store_data_by_module_name(module_name)
    else:
        await plugin_collector.remove_package(module_name, clear_config=False)
    return ActionResponse(ok=True)


@router.post("/package/update/{module_name}", summary="更新云端插件", response_model=ActionResponse)
@require_role(Role.Super)
async def update_package(
    module_name: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """更新云端插件"""
    await plugin_collector.update_package(module_name, auto_reload=True)
    return ActionResponse(ok=True)


# 以下四条是部署级的路由自省与运维动作，一律平台方专属：
# 前三条吐的是**整个进程**的路由表（含其它租户在用的插件挂了哪些路径），
# `refresh-routes` 则一次性重建全部插件路由，影响所有实例。前端不调用其中任何一条。
@router.get("/router-info", summary="获取所有插件路由信息", response_model=dict)
@require_role(Role.Super)
async def get_plugin_router_info(
    _current_user: DBUser = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """获取所有插件的路由信息"""
    return await get_all_plugin_router_info()


@router.post("/refresh-routes", summary="刷新插件路由", response_model=ActionResponse)
@require_role(Role.Super)
async def refresh_plugin_routes(
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """刷新所有插件路由，更新OpenAPI文档"""
    from nekro_agent.services.plugin.router_manager import plugin_router_manager

    plugin_router_manager.refresh_all_plugin_routes()
    return ActionResponse(ok=True)


@router.get("/debug-routes", summary="调试路由信息", response_model=PluginRoutesResponse)
@require_role(Role.Super)
async def debug_routes(_current_user: DBUser = Depends(get_current_active_user)) -> PluginRoutesResponse:
    """调试当前应用的所有路由信息（平台方专用）"""
    from nekro_agent.services.plugin.router_manager import plugin_router_manager

    plugin_routes = plugin_router_manager.debug_routes()
    return PluginRoutesResponse(plugin_routes=plugin_routes, debug_completed=True)


@router.get("/verify-plugin-routes/{plugin_key}", summary="验证插件路由", response_model=PluginRouteVerifyResponse)
@require_role(Role.Super)
async def verify_plugin_routes_endpoint(
    plugin_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginRouteVerifyResponse:
    """验证指定插件的路由是否正确挂载（平台方专用）"""
    from nekro_agent.services.plugin.router_manager import plugin_router_manager

    found_routes = plugin_router_manager.verify_plugin_routes(plugin_key)

    return PluginRouteVerifyResponse(
        plugin_key=plugin_key,
        found_routes=found_routes,
        routes_count=len(found_routes),
    )
