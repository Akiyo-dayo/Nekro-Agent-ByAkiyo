import asyncio
import json
import re
from typing import Any, Dict, List, Literal, Optional, Type, get_args

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from nekro_agent.core.config import CoreConfig, ModelConfigGroup, config
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_model_group import DBModelGroup
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import (
    ConfigInvalidError,
    ConfigNotFoundError,
    DefaultModelGroupDeleteError,
    ModelGroupNotFoundError,
    NotFoundError,
    OperationFailedError,
    PermissionDeniedError,
    ValidationError,
)
from nekro_agent.schemas.tenant import CREDENTIAL_MASK, CurrentIdentity
from nekro_agent.services.config_resolver import EffectiveConfigTrace, config_resolver
from nekro_agent.services.config_service import (
    CHANNEL_OVERRIDE_KEY_PREFIX,
    INSTANCE_OVERRIDE_KEY_PREFIX,
    INSTANCE_OVERRIDE_KEY_SEP,
    UnifiedConfigService,
    instance_override_config_key,
)
from nekro_agent.services.model_group_service import (
    assert_model_group_selectable,
    assert_outbound_urls_allowed,
    can_see_credential,
    can_view_group,
    delete_platform_group,
    get_visible_group,
    is_model_group_ref_item,
    mask_config_group,
    resolve_console_identity,
    upsert_platform_group,
)
from nekro_agent.services.model_group_test import (
    ModelGroupTestOutcome,
    ModelTestErrorCode,
    build_failure_outcome,
    prepare_model_group_test,
    run_model_group_test,
)
from nekro_agent.services.quota_service import assert_tenant_quota_within_ceiling
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import (
    accessible_instance_keys,
    assert_instance_accessible,
    get_tenant_scope,
    require_platform_owner,
)
from nekro_agent.tools.common_util import get_app_version
from nekro_agent.tools.redaction import is_credential_mask, redact_text


class BatchUpdateConfig(BaseModel):
    """批量更新配置请求体"""

    configs: Dict[str, str]


class ActionResponse(BaseModel):
    """简单操作响应"""

    ok: bool = True


class UiOnboardingGuideStatus(BaseModel):
    """界面上手引导状态"""

    should_show: bool


class ConfigInfo(BaseModel):
    """配置基本信息"""

    config_key: str
    config_class: str
    config_file_path: Optional[str] = None
    config_type: str
    field_count: int


class ConfigItem(BaseModel):
    """配置项信息"""

    model_config = ConfigDict(extra="allow")

    key: str
    value: Any
    title: str
    description: Optional[str] = None
    placeholder: Optional[str] = None
    type: str
    is_complex: Optional[bool] = None
    element_type: Optional[str] = None
    key_type: Optional[str] = None
    value_type: Optional[str] = None
    field_schema: Optional[Dict[str, Any]] = None
    enum: Optional[List[str]] = None
    is_secret: Optional[bool] = None
    is_textarea: Optional[bool] = None
    ref_model_groups: Optional[bool] = None
    ref_presets: Optional[bool] = None
    ref_presets_multiple: Optional[bool] = None
    is_hidden: Optional[bool] = None
    required: Optional[bool] = None
    model_type: Optional[str] = None
    sub_item_name: Optional[str] = None
    enable_toggle: Optional[str] = None
    overridable: Optional[bool] = None
    is_need_restart: Optional[bool] = None
    i18n_title: Optional[Dict[str, str]] = None
    i18n_description: Optional[Dict[str, str]] = None
    i18n_category: Optional[Dict[str, str]] = None


class ModelTypeOption(BaseModel):
    """模型类型描述"""

    value: str
    label: str
    description: Optional[str] = None
    color: Optional[str] = None
    icon: Optional[str] = None


class FetchModelsRequest(BaseModel):
    """服务端拉取模型列表请求。"""

    base_url: str
    api_key: str
    proxy_url: Optional[str] = None


class FetchModelsResponse(BaseModel):
    """服务端拉取模型列表响应。"""

    models: List[str]


class ModelGroupTestItem(BaseModel):
    """基础模型组检测结果。

    `error_code` / `message` 是归一化结论，取值范围见 `services/model_group_test.py`；
    `response_text` / `error_message` 里的上游原文只在调用方本就能看到该组凭据时才有值，
    否则 `error_message` 同样回落成有限集合内的归一化文案。
    """

    group_name: str
    model_name: str
    success: bool
    latency_ms: int
    error_code: ModelTestErrorCode = ModelTestErrorCode.NONE
    status_code: Optional[int] = None
    message: str = ""
    used_model: Optional[str] = None
    response_text: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    error_message: Optional[str] = None


class ModelGroupTestRequest(BaseModel):
    """基础模型组检测请求。"""

    group_names: List[str]


class ModelGroupInlineTestRequest(BaseModel):
    """基础模型组 inline 检测请求（直接传入配置，不依赖已保存数据）。"""

    group_name: str
    chat_model: str
    base_url: str
    api_key: str
    model_type: str
    chat_proxy: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    extra_body: Optional[str] = None


class ModelGroupTestResponse(BaseModel):
    """基础模型组检测响应。"""

    items: List[ModelGroupTestItem]


router = APIRouter(prefix="/config", tags=["Config"])


# ==================== 凭据脱敏 ====================
#
# 定制版客户能访问自己的后台，但永远不能拿到平台方的模型 API Key。
# 只在前端把输入框渲染成密码框是不够的——租户手里有登录态，可以直接调这些接口，
# 因此脱敏必须发生在接口层，且要能穿透 `MODEL_GROUPS` 这类嵌套结构。

# 字段名里出现这些片段即按凭据处理。
# `ExtraField(is_secret=True)` 是权威标记，但适配器与插件的配置分散在各自目录下，
# 漏标一处就等于泄露一份平台方凭据，所以再叠一层按名兜底。
# 只收录确凿的凭据词：裸 `KEY` 不在其中，否则 `CHAT_KEY` / `INSTANCE_KEY` 这类业务标识会被误伤；
# `TOKEN` 排除掉计量/超时语义的用法（`TOKEN_INPUT_RATE`、`FIRST_TOKEN_TIMEOUT` 等）。
_CREDENTIAL_NAME_PATTERN = re.compile(
    r"SECRET|PASSWORD|PASSWD|CREDENTIAL|SESSDATA|COOKIE"
    r"|API_?KEY|APP_?KEY|ACCESS_?KEY|AUTH_?KEY|PRIVATE_?KEY|AES_?KEY|SIGN_?KEY"
    r"|TOKEN(?!_?(RATE|TIMEOUT|INPUT|OUTPUT|COMPLETION|COUNT|LIMIT|USAGE|EXPIRE|NUM|SIZE))",
    re.IGNORECASE,
)


def _is_credential_field_name(field_name: str) -> bool:
    """字段名是否看起来是一份凭据"""
    return bool(_CREDENTIAL_NAME_PATTERN.search(field_name))


def _field_holds_credential(model_class: Type[BaseModel], field_name: str) -> bool:
    """嵌套模型的某个字段是否承载凭据（显式标记优先，字段名兜底）"""
    field = model_class.model_fields.get(field_name)
    if field is not None and isinstance(field.json_schema_extra, dict) and field.json_schema_extra.get("is_secret"):
        return True
    return _is_credential_field_name(field_name)


def mask_credentials(value: Any, *, is_credential: bool = False) -> Any:
    """递归地把配置值里的凭据替换为掩码串

    空串保持为空串：租户需要能分辨"这一项还没配"和"这一项已配但看不到"，
    并且写回空串不会被误判成掩码占位。

    Args:
        value: 待脱敏的配置值，可以是标量、列表、字典或 Pydantic 模型
        is_credential: 该值自身是否已被判定为凭据（由外层的字段名/标记决定）

    Returns:
        与入参同构的副本；Pydantic 模型保持原类型，不会退化成字典
    """
    if isinstance(value, str):
        return CREDENTIAL_MASK if is_credential and value else value
    if isinstance(value, BaseModel):
        model_class = type(value)
        return value.model_copy(
            update={
                name: mask_credentials(
                    getattr(value, name, None),
                    is_credential=_field_holds_credential(model_class, name),
                )
                for name in model_class.model_fields
            },
        )
    if isinstance(value, dict):
        return {
            key: mask_credentials(
                item,
                is_credential=is_credential or (isinstance(key, str) and _is_credential_field_name(key)),
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [mask_credentials(item, is_credential=is_credential) for item in value]
    return value


def holds_credential(value: Any, *, is_credential: bool = False) -> bool:
    """配置值的结构里是否存在凭据位置（与取值是否为空无关）"""
    if isinstance(value, str):
        return is_credential
    if isinstance(value, BaseModel):
        model_class = type(value)
        return any(
            holds_credential(getattr(value, name, None), is_credential=_field_holds_credential(model_class, name))
            for name in model_class.model_fields
        )
    if isinstance(value, dict):
        return any(
            holds_credential(item, is_credential=is_credential or (isinstance(key, str) and _is_credential_field_name(key)))
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(holds_credential(item, is_credential=is_credential) for item in value)
    return False


def restore_masked_credentials(new_value: Any, current_value: Any) -> Any:
    """把提交值里等于掩码串的位置还原成当前真实值

    契约见 `schemas/tenant.py`：`CREDENTIAL_MASK` 表示"保持不变"。读接口给租户返回的是掩码，
    整份配置原样提交回来时绝不能把真实凭据覆盖成一串星号。
    """
    if isinstance(new_value, str):
        if new_value != CREDENTIAL_MASK:
            return new_value
        return current_value if isinstance(current_value, str) else new_value
    if isinstance(new_value, dict):
        return {
            key: restore_masked_credentials(item, _current_child_value(current_value, str(key))) for key, item in new_value.items()
        }
    if isinstance(new_value, list):
        current_list = current_value if isinstance(current_value, list) else []
        return [
            restore_masked_credentials(item, current_list[index] if index < len(current_list) else None)
            for index, item in enumerate(new_value)
        ]
    return new_value


def _current_child_value(current_value: Any, key: str) -> Any:
    """从当前值里取出对应子节点，取不到时返回 None"""
    if isinstance(current_value, BaseModel):
        return getattr(current_value, key, None)
    if isinstance(current_value, dict):
        return current_value.get(key)
    return None


def _is_tenant_view(user: DBUser) -> bool:
    """当前身份是否受租户作用域限制

    平台方恒为 False，读写路径都不会进入任何过滤分支，未启用多租户时行为与改造前完全一致。
    """
    return get_tenant_scope(user) is not None


# 系统配置里承载整张模型组表的那一项。
# 它的取值是 `Dict[str, ModelConfigGroup]` —— **全部租户的全部模型组**，没有任何归属信息。
# `mask_credentials` 只按字段名判凭据，因此它挡得住 `API_KEY`，挡不住组名、`BASE_URL`、
# `CHAT_PROXY`，而这三样正是 `/config/model-groups` 改成按归属过滤要挡的东西。
# 凡是会把 `CoreConfig` 整份导出给租户的路径，都必须把这一项换成按归属裁剪过的视图。
MODEL_GROUPS_CONFIG_ITEM_KEY = "MODEL_GROUPS"


async def _visible_model_groups(user: DBUser) -> Dict[str, ModelConfigGroup]:
    """当前身份可见的模型组运行时视图

    事实来源是 `DBModelGroup`（`config.MODEL_GROUPS` 是一份不带归属信息的运行时视图），
    可见性与凭据掩码都委托给 `model_group_service`，与 `/console/model-groups` 同一套判定。

    平台方与未启用多租户的部署直接拿运行时视图本体，行为逐字不变。
    """
    if not _is_tenant_view(user):
        return config.MODEL_GROUPS

    identity = resolve_console_identity(user)
    rows = await DBModelGroup.all().order_by("id")
    return {row.name: mask_config_group(row, identity) for row in rows if can_view_group(row, identity)}


def _mask_config_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """对配置项列表做租户视角脱敏，并把兜底判定出的凭据字段如实标成 `is_secret`"""
    masked_items: List[Dict[str, Any]] = []
    for item in items:
        is_credential = bool(item.get("is_secret")) or _is_credential_field_name(str(item.get("key", "")))
        masked = dict(item)
        masked["value"] = mask_credentials(item.get("value"), is_credential=is_credential)
        if is_credential:
            masked["is_secret"] = True
        masked_items.append(masked)
    return masked_items


async def _with_inherited_override_values(config_key: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """覆盖开关关着的行，值列改为显示当前继承到的有效值

    覆盖关闭时该层存的值不参与解析，把它当成"当前值"显示会误导用户：历史上创建覆盖文件
    会把当时的默认值全量冻结进去，源码默认值改过之后这份快照就成了陈旧数据
    （实测有频道文件存着 `AFFECTION_ENABLE_SYSTEM: false`，而源码默认值早已是 true）。
    改为显示上层此刻真正生效的值之后，界面所示即勾上开关后会钉住的值。

    非覆盖层配置（系统配置、适配器自身配置、插件配置）不受影响：
    `config_resolver.inherited_values` 对它们返回 None，整个函数退化为恒等变换。
    """
    inherited = await config_resolver.inherited_values(config_key)
    if inherited is None:
        return items

    toggle_states = {str(item.get("key")): bool(item.get("value")) for item in items}
    resolved: List[Dict[str, Any]] = []
    for item in items:
        toggle = item.get("enable_toggle")
        item_key = str(item.get("key"))
        if toggle and not toggle_states.get(str(toggle), False) and item_key in inherited:
            resolved.append({**item, "value": inherited[item_key]})
        else:
            resolved.append(item)
    return resolved


def _current_config_value(config_key: str, item_key: str) -> Any:
    """读取某个配置项当前的真实值，配置项不存在时返回 None"""
    current_item = UnifiedConfigService.get_config_item(config_key, item_key)
    return current_item.get("value") if current_item else None


# 平台方专属的系统配置前缀：这些项决定"谁能操作总后台"，不是业务参数。
# 目前只有运维 Bot 一组——它的白名单一旦可写，租户管理员把自己的账号填进去
# 就拿到了平台方的运维通道，而通道背后是宿主级的实例与容器操作。
PLATFORM_ONLY_SYSTEM_CONFIG_PREFIXES = ("OPS_BOT_",)


def _is_platform_only_config(config_key: str, item_key: str) -> bool:
    """该配置项是否只允许平台方修改"""
    return config_key == "system" and item_key.startswith(PLATFORM_ONLY_SYSTEM_CONFIG_PREFIXES)


# 覆盖层的配置键前缀。写在这些层上的取值只对该作用域生效，因此"这份值归谁"
# 与"这个作用域归谁"是同一件事 —— 见 `_is_scope_owned_write`。
_OVERRIDE_LAYER_KEY_PREFIXES = (INSTANCE_OVERRIDE_KEY_PREFIX, CHANNEL_OVERRIDE_KEY_PREFIX)


def _is_scope_owned_write(config_key: str, item: Optional[Dict[str, Any]]) -> bool:
    """这次写入是不是"把自己的凭据写在自己的作用域上"

    两条判据缺一不可：

    1. 配置项显式标了 `ExtraField.scope_owned_secret`（目前只有实例告警 Webhook 地址）。
       不做成"所有 secret 在覆盖层都放行"：可覆盖的字段里还有别的敏感项，
       一刀切等于顺手把平台方凭据的写入口一起打开。
    2. 目标是**覆盖层**（实例 / 频道）。`system` / `adapter_*` 是跨租户共享作用域，
       写进去等于替所有租户做决定 —— 它们本来就被 `_assert_config_key_accessible`
       在更早的地方 403 掉了，这里再钉一次是因为本函数是三条写路径的共同收口。

    归属（"这个实例/频道是不是他的"）由调用点的 `_assert_config_key_accessible` /
    `assert_instance_accessible` 负责，本函数不重复判定。
    """
    if item is None or not item.get("scope_owned_secret"):
        return False
    return config_key.startswith(_OVERRIDE_LAYER_KEY_PREFIXES)


async def _assert_tenant_outbound_url_allowed(
    identity: CurrentIdentity,
    config_key: str,
    item_key: str,
    item: Optional[Dict[str, Any]],
    raw_value: str,
) -> None:
    """标了 `ExtraField.is_outbound_url` 的配置项，租户填的地址必须先过公网校验

    这类取值会让 NA 服务器**代为发起出站请求**（实例告警 Webhook 就是），不校验的话
    `http://127.0.0.1:8021/api/...` 就能以 NA 自己的网络身份打内网。判据复用模型组那条
    `assert_outbound_urls_allowed`，不另写一套；平台方在它内部直接放行，
    自建部署把告警发到内网收件器的用法不受影响。

    发送前还有第二道（`services/notification/channels.py`）：这里挡的是"填错/填坏"，
    那里挡的是"写入之后才变成内网"（DNS rebinding、放行开关后来才被打开）。
    """
    if item is None or not item.get("is_outbound_url"):
        return
    value = raw_value.strip()
    if not value or is_credential_mask(value):
        # 空串是"停用"，掩码是"保持不变"（`_sanitize_config_updates` 会把它整项剔除），
        # 两者都不会产生新的出站目标
        return
    await assert_outbound_urls_allowed(identity, value)


async def _assert_tenant_may_write(user: DBUser, config_key: str, configs: Dict[str, str]) -> None:
    """租户身份不得写入承载凭据、决定平台方权限边界、或引用了别人资源的配置项

    只做读侧脱敏是不够的：租户可以把平台模型组的 `BASE_URL` 改到自己的服务器上，
    真实 API Key 会随下一次请求发过去，等于绕过掩码把凭据取走。因此承载凭据的字段
    对租户整体只读——提交值与当前值任意一侧存在凭据位置都拒绝。

    权限边界类配置（`PLATFORM_ONLY_SYSTEM_CONFIG_PREFIXES`）不含凭据，
    但它决定的是"谁能操作总后台"，同样必须挡住，否则租户管理员可以自助提权。

    每日回复配额是**计费边界**，同样在这里把守：总后台的配额接口拦得再严，只要通用
    配置写入还开着，租户就能从配置页把自己的配额改上去。三条写路径都汇到本函数，
    因此这是唯一需要下手的地方。

    **凭据这条判据有一个按归属分支的例外**（`_is_scope_owned_write`）：实例告警 Webhook
    地址本身就是凭据（Slack / 飞书那条 URL 就是口令），所以它必须按凭据脱敏，否则租户打开
    自己实例的配置页就能从"继承值"里读到平台方那条全局地址；但写在**自己实例层**上的那份
    是他自己的，一刀切拒绝等于让"租户给自己的实例配自己的告警收件端"这件事根本做不成。

    **会让服务端代为出站的地址还要再过一道 SSRF 校验**（`_assert_tenant_outbound_url_allowed`）。

    **引用模型组的配置项是同一条边界上的第三扇门。** `USE_MODEL_GROUP` 这类字段以前在这里
    只被当成普通字符串：它既不承载凭据、也不带平台方前缀、更不是配额，于是三条判据一条都
    不命中。可它决定的是每一轮对话花谁的 Key —— 租户写一次平台 private 组名就绕过了
    授权与计费边界。判定按 `ref_model_groups` 标记驱动（`is_model_group_ref_item`），
    不硬编码字段名；归属判定复用 `assert_model_group_selectable`（内部即 `can_view_group`）。
    """
    await assert_tenant_quota_within_ceiling(config_key, configs)
    identity = resolve_console_identity(user)

    for item_key, raw_value in configs.items():
        if _is_platform_only_config(config_key, item_key):
            raise PermissionDeniedError(detail=f"配置项 {config_key}.{item_key} 属于平台方权限边界，租户身份不可修改")

        current_item = UnifiedConfigService.get_config_item(config_key, item_key)

        # 凭据归属跟着作用域走的那一类：写在自己实例/频道层上的值就是他自己的，
        # 读侧仍然按凭据脱敏（否则会把平台方全局那条地址当"继承值"显示出去）
        if not _is_scope_owned_write(config_key, current_item):
            is_credential = bool(current_item.get("is_secret")) if current_item else False
            is_credential = is_credential or _is_credential_field_name(item_key)

            current_value = current_item.get("value") if current_item else None
            submitted_value: Any = None
            try:
                submitted_value = json.loads(raw_value)
            except ValueError:
                submitted_value = raw_value

            if holds_credential(current_value, is_credential=is_credential) or holds_credential(
                submitted_value,
                is_credential=is_credential,
            ):
                raise PermissionDeniedError(detail=f"配置项 {config_key}.{item_key} 承载凭据，租户身份不可修改")

        # 取值恰好是掩码串时这一项会被 `_sanitize_config_updates` 整个剔除（"保持不变"），
        # 一个字都不会落盘，所以不该把它当成一次模型组引用去校验。
        if raw_value != CREDENTIAL_MASK and is_model_group_ref_item(current_item):
            # 用原始字符串而不是 `submitted_value`：这些字段都是 str，
            # `set_config_value` 落盘的就是原始字符串（`json.loads` 只会把 `"null"`
            # 这类取值悄悄变形，判定与被判值的形态必须一致）。
            await assert_model_group_selectable(raw_value, identity)

        # 出站地址校验放在权限判定**之后**：它要做一次 DNS 解析，
        # 让一个注定被拒的写入先去解析调用方给的域名，本身就是一次被白嫖的出站探测。
        await _assert_tenant_outbound_url_allowed(identity, config_key, item_key, current_item, raw_value)


def _sanitize_config_updates(config_key: str, configs: Dict[str, str]) -> Dict[str, str]:
    """写入前处理掩码占位

    - 整个值就是掩码：该项从本次更新里剔除，保持不变；
    - 复杂字段（JSON 字符串）里嵌着掩码：逐个位置还原成当前真实值。
    """
    sanitized: Dict[str, str] = {}
    for item_key, raw_value in configs.items():
        if raw_value == CREDENTIAL_MASK:
            continue
        if CREDENTIAL_MASK not in raw_value:
            sanitized[item_key] = raw_value
            continue
        try:
            parsed = json.loads(raw_value)
        except ValueError:
            # 不是 JSON 的普通字符串，掩码只是恰好出现在文本里，原样写入
            sanitized[item_key] = raw_value
            continue
        restored = restore_masked_credentials(parsed, _current_config_value(config_key, item_key))
        sanitized[item_key] = json.dumps(restored, ensure_ascii=False)
    return sanitized


async def _assert_config_key_accessible(user: DBUser, config_key: str, *, for_write: bool = True) -> None:
    """校验当前身份能否操作某个 `config_key` 所指的作用域

    `config_key` 不是一个不透明标识：它可以编码出实例作用域
    (`instance_override_{adapter}:{instance_key}`) 与频道作用域 (`channel_config_{chat_key}`)。
    `/set/{config_key}/{item_key}` 与 `/batch/{config_key}` 从路径原样收下它，此前只过
    `_assert_tenant_may_write`（那只管凭据、平台方边界与配额天花板，**不管归属**），
    于是租户管理员可以拼一个别的租户的实例标识去写别人的配置。
    兄弟端点 `PUT /config/instance/...` 一直有 `assert_instance_accessible`，是这两条漏了。

    读侧（`/info`、`/list`、`/get`）走同一个函数而不是另写一套判据：`config_key` 的
    形态解析一旦出现两份实现，两份就会各自漂移，而这里判错一次就是一次跨租户读写。
    `for_write` 只影响**跨租户共享作用域**（`system` / `adapter_*` / `adapter_override_*` /
    `plugin_*`）的结论：

    - 写入是提权 —— 它对所有租户同时生效，因此 403（权限边界，不是资源存在性）；
    - 读取是租户配置页赖以工作的正常路径 —— 凭据由 `_mask_config_items` 脱敏、
      平台方边界项由 `_is_platform_only_config` 滤掉，因此放行。

    实例/频道作用域两侧结论完全一致：不属于自己一律 404。读侧同样必须挡在
    `UnifiedConfigService` 之前 —— `_get_config_instance()` 对覆盖类配置键会
    `dump_config()` 落盘，光是"读"别人的实例配置就会在磁盘上凭空造出一份覆盖文件。

    平台方与未启用多租户的部署直接放行，行为与改造前逐字一致。
    """
    if not _is_tenant_view(user):
        return
    if config_key.startswith(INSTANCE_OVERRIDE_KEY_PREFIX):
        # 分隔符必须与 `instance_override_config_key()` 生成时用的一致（`INSTANCE_OVERRIDE_KEY_SEP`，
        # 即 `__`）。这里若按 `:` 切，真实键 `instance_override_onebot_v11__inst1` 会切不出
        # instance_key，租户连自己的实例都会被判成"不存在"——安全上是 fail-closed，
        # 功能上却把这条合法路径整个堵死。adapter_key 不含双下划线，故从左首个分隔符切分即可还原两段。
        adapter_key, _, instance_key = config_key[len(INSTANCE_OVERRIDE_KEY_PREFIX) :].partition(
            INSTANCE_OVERRIDE_KEY_SEP,
        )
        if not adapter_key or not instance_key:
            raise NotFoundError(resource=f"配置 '{config_key}'")
        await assert_instance_accessible(user, instance_key)
        return
    if config_key.startswith(CHANNEL_OVERRIDE_KEY_PREFIX):
        await _assert_chat_key_accessible(user, config_key[len(CHANNEL_OVERRIDE_KEY_PREFIX) :])
        return
    # 其余（system / adapter_ / adapter_override_ / plugin_）都是**跨租户共享**的作用域：
    # 它们对所有租户同时生效，租户身份一律不得写入。这是权限边界而非资源存在性，故 403。
    if for_write:
        raise PermissionDeniedError(detail=f"配置 '{config_key}' 作用于全部租户，租户身份只能修改自己实例或频道的配置")


async def _assert_chat_key_accessible(user: DBUser, chat_key: str) -> None:
    """校验当前身份能否访问某个频道所属的实例

    频道不存在、或属于别的租户时都抛 404，两者对外不可区分。
    """
    if not _is_tenant_view(user):
        return
    channel = await DBChatChannel.filter(chat_key=chat_key).first()
    if channel is None:
        raise NotFoundError(resource=f"频道 '{chat_key}'")
    await assert_instance_accessible(user, channel.instance_key)


def _raise_config_error(
    *,
    config_key: str,
    item_key: Optional[str],
    error_msg: Optional[str],
    operation: str,
) -> None:
    if not error_msg:
        raise OperationFailedError(operation=operation)

    if "配置实例不存在" in error_msg:
        raise ConfigNotFoundError(key=config_key)

    if "配置项不存在" in error_msg:
        missing_key = item_key
        missing_prefix = "配置项不存在:"
        if missing_key is None and missing_prefix in error_msg:
            missing_key = error_msg.split(missing_prefix, maxsplit=1)[1].strip()
        key = f"{config_key}.{missing_key}" if missing_key else config_key
        raise ConfigNotFoundError(key=key)

    if "无效" in error_msg or "invalid" in error_msg.lower():
        key = item_key or config_key
        raise ConfigInvalidError(key=key, reason=error_msg)

    raise OperationFailedError(operation=operation)


@router.get("/keys", summary="获取所有配置键列表", response_model=List[str])
@require_role(Role.Admin)
async def get_config_keys(_current_user: DBUser = Depends(get_current_active_user)) -> List[str]:
    """获取所有已注册的配置键列表（租户身份下作用域类配置键按归属裁剪）

    这张清单**不是**编译期常量：`UnifiedConfigService._get_config_instance()` 会把动态
    加载到的配置登记进 `ConfigManager`，所以每有一个频道来消息、或平台方在总后台看一眼
    别人的实例配置，`channel_config_{chat_key}` / `instance_override_{adapter}__{instance_key}`
    就会长进这张表。实测（进程内靶场，平台方先读一次 rival 的配置）：租户随后拿到的清单里
    出现了 `channel_config_onebot_v11-rival1-group_20001` 与
    `instance_override_onebot_v11__rival1`。

    逐条读现在已经 404，但泄露出去的 `instance_key` / `chat_key` 本身就是别处端点的入参，
    等于把别的租户的实例与群变成可枚举的——与 `assert_instance_accessible` 坚持用 404
    而不是 403 要挡的是同一件事。
    """
    keys = UnifiedConfigService.get_all_config_keys()
    if not _is_tenant_view(_current_user):
        return keys

    accessible = set(await accessible_instance_keys(_current_user))
    channel_keys = [key[len(CHANNEL_OVERRIDE_KEY_PREFIX) :] for key in keys if key.startswith(CHANNEL_OVERRIDE_KEY_PREFIX)]
    accessible_chat_keys = set(
        await DBChatChannel.filter(chat_key__in=channel_keys, instance_key__in=accessible).values_list(
            "chat_key",
            flat=True,
        ),
    )

    visible: List[str] = []
    for key in keys:
        if key.startswith(INSTANCE_OVERRIDE_KEY_PREFIX):
            if key[len(INSTANCE_OVERRIDE_KEY_PREFIX) :].partition(INSTANCE_OVERRIDE_KEY_SEP)[2] in accessible:
                visible.append(key)
        elif key.startswith(CHANNEL_OVERRIDE_KEY_PREFIX):
            if key[len(CHANNEL_OVERRIDE_KEY_PREFIX) :] in accessible_chat_keys:
                visible.append(key)
        else:
            visible.append(key)
    return visible


@router.get("/ui-onboarding-guide/status", summary="获取界面上手引导状态", response_model=UiOnboardingGuideStatus)
@require_role(Role.Admin)
async def get_ui_onboarding_guide_status(
    _current_user: DBUser = Depends(get_current_active_user),
) -> UiOnboardingGuideStatus:
    """由后端判断当前是否需要展示界面上手引导"""
    return UiOnboardingGuideStatus(should_show=config.ENABLE_UI_ONBOARDING_GUIDE)


@router.get("/info/{config_key}", summary="获取配置基本信息", response_model=ConfigInfo)
@require_role(Role.Admin)
async def get_config_info(
    config_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ConfigInfo:
    """获取指定配置的基本信息

    租户身份下 `config_key` 必须指向自己的作用域：响应里的 `config_file_path` 会拼出
    实例/频道标识，而取实例本身就会把覆盖文件建出来，两者都不该对别的租户开放。
    """
    await _assert_config_key_accessible(_current_user, config_key, for_write=False)

    config_info = UnifiedConfigService.get_config_info(config_key)
    if not config_info:
        raise ConfigNotFoundError(key=config_key)
    return ConfigInfo(**config_info)


@router.get("/list/{config_key}", summary="获取指定配置的配置列表", response_model=List[ConfigItem])
@require_role(Role.Admin)
async def get_config_list(
    config_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> List[ConfigItem]:
    """获取指定配置的配置列表（租户身份下凭据一律为掩码，且只能读自己的作用域）"""
    await _assert_config_key_accessible(_current_user, config_key, for_write=False)

    try:
        config_list = UnifiedConfigService.get_config_list(config_key)
    except ValueError:
        raise ConfigNotFoundError(key=config_key) from None
    config_list = await _with_inherited_override_values(config_key, config_list)
    if _is_tenant_view(_current_user):
        # 平台方权限边界类配置对租户整体不可见：运维频道与运维人员名单本身
        # 就是伪造运维指令所需的全部情报，读得到就等于把攻击面画好递过去
        config_list = _mask_config_items(
            [item for item in config_list if not _is_platform_only_config(config_key, str(item.get("key", "")))],
        )
        if config_key == "system":
            # 理由见 `MODEL_GROUPS_CONFIG_ITEM_KEY`：这条读接口是绕开
            # `/config/model-groups` 归属过滤的一条等价路径，同一份数据不能两个口径
            visible_groups = await _visible_model_groups(_current_user)
            config_list = [
                {**item, "value": visible_groups} if item.get("key") == MODEL_GROUPS_CONFIG_ITEM_KEY else item
                for item in config_list
            ]
    return [ConfigItem(**item) for item in config_list]


@router.get("/get/{config_key}/{item_key}", summary="获取配置项值", response_model=ConfigItem)
@require_role(Role.Admin)
async def get_config_item(
    config_key: str,
    item_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ConfigItem:
    """获取指定配置的配置项值（租户身份下凭据一律为掩码，且只能读自己的作用域）"""
    await _assert_config_key_accessible(_current_user, config_key, for_write=False)

    config_item = UnifiedConfigService.get_config_item(config_key, item_key)
    if not config_item:
        raise ConfigNotFoundError(key=f"{config_key}.{item_key}")
    if _is_tenant_view(_current_user):
        if _is_platform_only_config(config_key, item_key):
            # 以"不存在"掩盖存在性，与 `assert_instance_accessible` 用 404 而非 403 是同一个理由
            raise ConfigNotFoundError(key=f"{config_key}.{item_key}")
        config_item = _mask_config_items([config_item])[0]
        if config_key == "system" and item_key == MODEL_GROUPS_CONFIG_ITEM_KEY:
            # 单项读是 `/config/list` 的等价路径，两条必须给同一个视图
            config_item = {**config_item, "value": await _visible_model_groups(_current_user)}
    return ConfigItem(**config_item)


@router.post("/set/{config_key}/{item_key}", summary="设置配置项值", response_model=ActionResponse)
@require_role(Role.Admin)
async def set_config_value(
    config_key: str,
    item_key: str,
    value: str = Query(..., description="配置项值"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """设置指定配置的配置项值

    值等于 `CREDENTIAL_MASK` 表示"保持不变"，直接跳过写入，避免把真实凭据覆盖成掩码串。
    """
    if _is_tenant_view(_current_user):
        await _assert_config_key_accessible(_current_user, config_key)
        await _assert_tenant_may_write(_current_user, config_key, {item_key: value})

    sanitized = _sanitize_config_updates(config_key, {item_key: value})
    if item_key not in sanitized:
        return ActionResponse(ok=True)

    success, error_msg = UnifiedConfigService.set_config_value(config_key, item_key, sanitized[item_key])
    if not success:
        _raise_config_error(
            config_key=config_key,
            item_key=item_key,
            error_msg=error_msg,
            operation="设置配置",
        )
    return ActionResponse(ok=True)


@router.post("/batch/{config_key}", summary="批量更新配置", response_model=ActionResponse)
@require_role(Role.Admin)
async def batch_update_config(
    config_key: str,
    body: BatchUpdateConfig,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """批量更新指定配置

    取值等于 `CREDENTIAL_MASK` 的字段（含嵌套结构里的位置）视为"保持不变"。
    """
    if _is_tenant_view(_current_user):
        await _assert_config_key_accessible(_current_user, config_key)
        await _assert_tenant_may_write(_current_user, config_key, body.configs)

    success, error_msg = UnifiedConfigService.batch_update_config(config_key, _sanitize_config_updates(config_key, body.configs))
    if not success:
        _raise_config_error(
            config_key=config_key,
            item_key=None,
            error_msg=error_msg,
            operation="批量更新配置",
        )

    success, error_msg = UnifiedConfigService.save_config(config_key)
    if not success:
        _raise_config_error(
            config_key=config_key,
            item_key=None,
            error_msg=error_msg,
            operation="保存配置",
        )

    # 系统配置保存后同步全局语言
    if config_key == "system":
        from nekro_agent.schemas.i18n import SupportedLang, set_system_lang

        set_system_lang(SupportedLang(config.SYSTEM_LANG))

    return ActionResponse(ok=True)


@router.post("/save/{config_key}", summary="保存配置", response_model=ActionResponse)
@require_role(Role.Admin)
async def save_config_api(
    config_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """保存指定配置到文件

    落盘与 `/batch/{config_key}` 同属写侧，判据也用同一条：跨租户共享作用域对租户 403，
    别人的实例/频道 404。前端在实例作用域下走 `PUT /config/instance/...`（写入即落盘），
    不会经过这里。
    """
    await _assert_config_key_accessible(_current_user, config_key)

    success, error_msg = UnifiedConfigService.save_config(config_key)
    if not success:
        _raise_config_error(
            config_key=config_key,
            item_key=None,
            error_msg=error_msg,
            operation="保存配置",
        )
    return ActionResponse(ok=True)


@router.post("/reload/{config_key}", summary="重置配置", response_model=ActionResponse)
@require_role(Role.Admin)
async def reload_config_api(
    config_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """重置指定配置

    "重置"是把磁盘上的配置重新读回内存并重新灌进 env，对 `system` 这类共享作用域来说
    等于替所有租户回滚一次配置，因此与写入用同一条判据。
    """
    await _assert_config_key_accessible(_current_user, config_key)

    success, error_msg = UnifiedConfigService.reload_config(config_key)
    if not success:
        _raise_config_error(
            config_key=config_key,
            item_key=None,
            error_msg=error_msg,
            operation="重置配置",
        )
    return ActionResponse(ok=True)


# ==================== 实例级配置API ====================


def _instance_override_config_key(adapter_key: str, instance_key: str) -> str:
    """校验并构造实例级覆盖配置的配置键

    两段标识都会拼进配置文件路径，非法字符必须在入口处拒绝。
    """
    from nekro_agent.adapters import ADAPTER_REGISTRY

    if adapter_key not in ADAPTER_REGISTRY:
        raise NotFoundError(resource=f"适配器 {adapter_key}")
    try:
        return instance_override_config_key(adapter_key, instance_key)
    except ValueError as e:
        raise ValidationError(reason=str(e)) from e


@router.get("/instance/{adapter_key}/{instance_key}", summary="获取实例级覆盖配置", response_model=List[ConfigItem])
@require_role(Role.Admin)
async def get_instance_config(
    adapter_key: str,
    instance_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> List[ConfigItem]:
    """获取指定适配器实例的覆盖配置列表

    租户身份只能读自己名下的实例（他人实例一律 404），且凭据一律为掩码。
    """
    if _is_tenant_view(_current_user):
        await assert_instance_accessible(_current_user, instance_key)

    config_key = _instance_override_config_key(adapter_key, instance_key)
    try:
        config_list = UnifiedConfigService.get_config_list(config_key)
    except ValueError:
        raise ConfigNotFoundError(key=config_key) from None
    config_list = await _with_inherited_override_values(config_key, config_list)
    if _is_tenant_view(_current_user):
        config_list = _mask_config_items(config_list)
    return [ConfigItem(**item) for item in config_list]


@router.put("/instance/{adapter_key}/{instance_key}", summary="写入实例级覆盖配置", response_model=ActionResponse)
@require_role(Role.Admin)
async def update_instance_config(
    adapter_key: str,
    instance_key: str,
    body: BatchUpdateConfig,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """批量写入并保存指定适配器实例的覆盖配置

    租户身份只能写自己名下的实例（他人实例一律 404），且不得改动承载凭据的字段。
    """
    config_key = _instance_override_config_key(adapter_key, instance_key)

    if _is_tenant_view(_current_user):
        await assert_instance_accessible(_current_user, instance_key)
        await _assert_tenant_may_write(_current_user, config_key, body.configs)

    success, error_msg = UnifiedConfigService.batch_update_config(config_key, _sanitize_config_updates(config_key, body.configs))
    if not success:
        _raise_config_error(
            config_key=config_key,
            item_key=None,
            error_msg=error_msg,
            operation="更新实例配置",
        )

    success, error_msg = UnifiedConfigService.save_config(config_key)
    if not success:
        _raise_config_error(
            config_key=config_key,
            item_key=None,
            error_msg=error_msg,
            operation="保存实例配置",
        )

    return ActionResponse(ok=True)


@router.get("/effective/{chat_key}", summary="获取频道有效配置溯源", response_model=EffectiveConfigTrace)
@require_role(Role.Admin)
async def get_effective_config_trace(
    chat_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> EffectiveConfigTrace:
    """获取频道有效配置，并逐字段给出最终值来自 全局/适配器/实例/频道 中的哪一层

    租户身份只能溯源自己实例下的频道（他人频道一律 404），且凭据一律为掩码。
    """
    await _assert_chat_key_accessible(_current_user, chat_key)

    trace = await config_resolver.explain_effective_config(chat_key)
    if not _is_tenant_view(_current_user):
        return trace

    visible_groups = await _visible_model_groups(_current_user)
    for item in trace.items:
        if item.key == MODEL_GROUPS_CONFIG_ITEM_KEY:
            # 溯源逐字段遍历整个 `CoreConfig`，模型组这一项同样会把全部租户的组带出来；
            # 理由见 `MODEL_GROUPS_CONFIG_ITEM_KEY`。溯源按 JSON 模式导出，这里保持同一形态。
            # 该字段不可覆盖，`layer_values` 恒为 None，没有逐层原值要处理。
            item.value = {name: group.model_dump(mode="json") for name, group in visible_groups.items()}
            continue
        is_credential = _field_holds_credential(CoreConfig, item.key)
        item.value = mask_credentials(item.value, is_credential=is_credential)
        if item.layer_values is not None:
            # 逐层原值同样要脱敏，否则掩码只挡住了最终值，上一层的真实凭据仍然可见
            item.layer_values = {
                layer: mask_credentials(layer_value, is_credential=is_credential) for layer, layer_value in item.layer_values.items()
            }
    return trace


# ==================== 模型组管理API ====================


@router.get("/model-groups", summary="获取模型组列表", response_model=Dict[str, ModelConfigGroup])
@require_role(Role.Admin)
async def get_model_groups(_current_user: DBUser = Depends(get_current_active_user)) -> Dict[str, ModelConfigGroup]:
    """获取所有模型组配置（本端点是配置项里模型组下拉的数据源）

    租户身份下按归属过滤：**`config.MODEL_GROUPS` 是一份不带归属信息的运行时视图**，
    照它返回等于把别的租户自建组的组名与模型名一并列出来（凭据虽被掩码，组名本身
    就是客户情报）。判定与掩码都收在 `_visible_model_groups` 里，`/config/list/system`、
    `/config/get/system/MODEL_GROUPS`、`/config/effective/{chat_key}` 这三条会把整份
    `CoreConfig` 导出给租户的路径共用它——否则本端点收敛了，那三条照样把同一份数据漏出去。

    平台方与未启用多租户的部署仍直接返回运行时视图，行为逐字不变。
    """
    return await _visible_model_groups(_current_user)


@router.post("/model-groups/{group_name}", summary="更新模型组", response_model=ActionResponse)
@require_role(Role.Admin)
async def update_model_group(
    group_name: str,
    model_config: ModelConfigGroup,
    _current_user: DBUser = Depends(get_current_active_user),
    _owner: DBUser = Depends(require_platform_owner()),
) -> ActionResponse:
    """更新或添加模型组配置

    本端点是平台方专属：它按组名整体覆盖、不看归属，租户改动模型组的 `BASE_URL`
    就能让平台凭据流向自己的服务器，掩码挡不住这条路径。租户自建模型组走
    `/console/model-groups`（那条路径按 `can_edit_group` 判定，平台组对租户只读）。
    提交的 `API_KEY` 等于 `CREDENTIAL_MASK` 时保持原值不变。

    平台方专属用的是 `require_platform_owner()` 而不是 `Role.Super`：未启用多租户时
    全部账号都是平台方，`Role.Admin` 的后台管理员照旧能改模型组，行为逐字不变；
    换成 `Role.Super` 会把这些既有账号一并挡在外面。
    """
    existing = config.MODEL_GROUPS.get(group_name)
    if existing is not None:
        model_config = ModelConfigGroup.model_validate(restore_masked_credentials(model_config.model_dump(), existing))

    # 模型组的事实来源是 `DBModelGroup`，`config.MODEL_GROUPS` 只是它的运行时视图。
    # 直接改视图的话，重启时视图按数据库重建，这次修改会被静默回滚。
    await upsert_platform_group(group_name, model_config)
    return ActionResponse(ok=True)


@router.delete("/model-groups/{group_name}", summary="删除模型组", response_model=ActionResponse)
@require_role(Role.Admin)
async def delete_model_group(
    group_name: str,
    _current_user: DBUser = Depends(get_current_active_user),
    _owner: DBUser = Depends(require_platform_owner()),
) -> ActionResponse:
    """删除模型组配置（平台方专属，理由同更新端点）"""
    if group_name not in config.MODEL_GROUPS:
        raise ModelGroupNotFoundError(name=group_name)
    if group_name == "default":
        raise DefaultModelGroupDeleteError

    # 同步删除事实来源，理由见更新端点
    await delete_platform_group(group_name)
    return ActionResponse(ok=True)


@router.get("/model-types", summary="获取支持的模型类型列表", response_model=List[ModelTypeOption])
@require_role(Role.Admin)
async def get_model_types(_current_user: DBUser = Depends(get_current_active_user)) -> List[ModelTypeOption]:
    """获取支持的模型类型列表"""
    model_types = get_args(ModelConfigGroup.model_fields["MODEL_TYPE"].annotation)

    type_info: Dict[str, Dict[str, str]] = {
        "chat": {
            "label": "聊天",
            "description": "用于对话的模型",
            "color": "primary",
            "icon": "Chat",
        },
        "embedding": {
            "label": "嵌入",
            "description": "用于将文本转换为向量的模型",
            "color": "secondary",
            "icon": "Code",
        },
        "draw": {
            "label": "绘图",
            "description": "用于生成图像的模型",
            "color": "warning",
            "icon": "Brush",
        },
    }

    return [
        ModelTypeOption(
            value=type_name,
            label=type_info.get(type_name, {}).get("label") or type_name,
            description=type_info.get(type_name, {}).get("description") or "",
            color=type_info.get(type_name, {}).get("color") or "default",
            icon=type_info.get(type_name, {}).get("icon") or "EmojiObjects",
        )
        for type_name in model_types
    ]


@router.post("/model-groups/actions/fetch-models", summary="通过服务端拉取可用模型列表", response_model=FetchModelsResponse)
@require_role(Role.Admin)
async def fetch_model_groups_models(
    body: FetchModelsRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> FetchModelsResponse:
    """通过后端真实出口拉取 OpenAI 兼容模型列表。

    本端点由后端代替浏览器发起请求，因此租户提交的地址等价于"让 NA 服务器替我打这个地址"，
    必须先过公网校验，否则填 `http://127.0.0.1:8021/x` 就能拿 NA 自己的身份探内网。
    """
    from nekro_agent.services.agent.openai import list_openai_models

    await assert_outbound_urls_allowed(
        resolve_console_identity(_current_user),
        body.base_url,
        body.proxy_url or "",
    )

    try:
        models = await list_openai_models(
            base_url=body.base_url,
            api_key=body.api_key,
            proxy_url=body.proxy_url,
        )
    except Exception as e:
        # 上游报错常把整段请求头回显出来，直接透传等于把刚提交的凭据再送回前端与日志
        raise OperationFailedError(
            operation="拉取模型列表",
            detail=redact_text(str(e), literal_secrets=(body.api_key,)),
        ) from e

    return FetchModelsResponse(models=models)


def _to_test_item(group_name: str, outcome: ModelGroupTestOutcome) -> ModelGroupTestItem:
    """把归一化结论转成前端既有的 `ModelGroupTestItem` 形态

    `error_message` 在凭据可见时优先给（已脱敏的）上游原文，方便调用方自己排障；
    凭据不可见时回落成归一化文案——`ModelGroupTestOutcome.detail` 此时恒为空，
    因此这里不会把上游文本漏出去。
    """
    return ModelGroupTestItem(
        group_name=group_name,
        model_name=outcome.model_name,
        success=outcome.ok,
        latency_ms=outcome.latency_ms,
        error_code=outcome.error_code,
        status_code=outcome.status_code,
        message=outcome.message,
        used_model=outcome.model_name if outcome.ok else None,
        response_text=outcome.response_text or None,
        input_tokens=outcome.input_tokens,
        output_tokens=outcome.output_tokens,
        error_message=None if outcome.ok else (outcome.detail or outcome.message),
    )


@router.post("/model-groups/actions/test", summary="测试基础模型组", response_model=ModelGroupTestResponse)
@require_role(Role.Admin)
async def test_model_groups(
    body: ModelGroupTestRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ModelGroupTestResponse:
    """通过真实请求测试基础模型组，聊天模型发送消息，嵌入模型调用 embeddings 接口。

    可见性与凭据可见性都按当前身份判定：查询走 `get_visible_group`（事实来源是数据库，
    不是 `config.MODEL_GROUPS` 那份无归属信息的运行时视图），租户看不见的组按不存在处理；
    平台组对租户只回归一化结论。发起与脱敏见 `services/model_group_test.py`。
    """
    identity = resolve_console_identity(_current_user)

    async def run_single(group_name: str) -> ModelGroupTestItem:
        try:
            row = await get_visible_group(group_name, identity)
        except ModelGroupNotFoundError:
            return _to_test_item(group_name, build_failure_outcome(ModelTestErrorCode.GROUP_NOT_FOUND))

        group = row.to_config_group()
        credential_visible = can_see_credential(row, identity)
        await prepare_model_group_test(identity, group, credential_visible=credential_visible)
        return _to_test_item(
            group_name,
            # `group_name` 是身份键；显示名落账会让同一个组在用量页上裂成两行
            await run_model_group_test(
                group,
                credential_visible=credential_visible,
                group_name=group_name,
                tenant_id=identity.tenant_id,
            ),
        )

    unique_group_names = list(dict.fromkeys(body.group_names))
    items = await asyncio.gather(*(run_single(group_name) for group_name in unique_group_names))
    return ModelGroupTestResponse(items=list(items))


@router.post("/model-groups/actions/test-inline", summary="inline 测试模型配置（不依赖已保存数据）", response_model=ModelGroupTestItem)
@require_role(Role.Admin)
async def test_model_group_inline(
    body: ModelGroupInlineTestRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ModelGroupTestItem:
    """使用请求体中直接传入的配置执行测试，测试编辑中未保存的配置时使用。

    ## "保持不变"哨兵只对自己的凭据生效

    表单里的凭据/地址可能是读接口给出的掩码串，此时回落到已保存的真实值，
    否则用户一打开编辑框点测试就会拿一串星号去请求上游。

    但这个回落**只在调用方本就能看到该组凭据时**才做。否则就存在一条完整的凭据外带通道：
    租户提交 `group_name=平台组`、`api_key=********`、`base_url=自己的服务器`，
    服务端把平台方的真实 Key 填进 Authorization 头发到租户指定的地址——掩码、脱敏、
    出站公网校验没有任何一条能拦住它，因为请求本身"合法"。因此这里直接拒绝，
    租户要测平台组请走 `/config/model-groups/actions/test`（那条路径不接受任何调用方参数）。

    经过上面的收口，能进入实际请求的凭据一定属于调用方自己，所以按凭据可见处理，
    上游报错原文（仍过脱敏）可以回给他。
    """
    identity = resolve_console_identity(_current_user)

    # 事实来源是数据库：`config.MODEL_GROUPS` 是不带归属信息的运行时视图，凭它判不出该不该回落
    saved_row = await DBModelGroup.get_or_none(name=body.group_name)
    saved_credential_visible = saved_row is not None and can_see_credential(saved_row, identity)

    resolved_base_url = body.base_url
    resolved_api_key = body.api_key
    resolved_proxy = body.chat_proxy or ""
    if saved_row is not None and saved_credential_visible:
        saved = saved_row.to_config_group()
        if is_credential_mask(resolved_base_url):
            resolved_base_url = saved.BASE_URL
        if is_credential_mask(resolved_api_key):
            resolved_api_key = saved.API_KEY
        if is_credential_mask(resolved_proxy):
            resolved_proxy = saved.CHAT_PROXY
    elif any(is_credential_mask(value) for value in (resolved_base_url, resolved_api_key, resolved_proxy)):
        raise PermissionDeniedError(detail="该模型组的凭据不属于当前身份，不能用于 inline 测试")

    if body.model_type not in ("chat", "embedding"):
        return _to_test_item(
            body.group_name,
            build_failure_outcome(ModelTestErrorCode.UNSUPPORTED_MODEL_TYPE, model_name=body.chat_model),
        )
    model_type: Literal["chat", "embedding"] = "embedding" if body.model_type == "embedding" else "chat"

    group = ModelConfigGroup(
        GROUP_NAME=body.group_name,
        CHAT_MODEL=body.chat_model,
        CHAT_PROXY=resolved_proxy,
        BASE_URL=resolved_base_url,
        API_KEY=resolved_api_key,
        MODEL_TYPE=model_type,
        TEMPERATURE=body.temperature,
        TOP_P=body.top_p,
        TOP_K=body.top_k,
        PRESENCE_PENALTY=body.presence_penalty,
        FREQUENCY_PENALTY=body.frequency_penalty,
        EXTRA_BODY=body.extra_body,
    )

    await prepare_model_group_test(identity, group, credential_visible=True)
    return _to_test_item(
        body.group_name,
        # inline 测试的组未必已经保存，但 `body.group_name` 走的仍是身份键那个命名空间
        # （上面刚用它 `DBModelGroup.get_or_none(name=...)` 反查过）
        await run_model_group_test(
            group,
            credential_visible=True,
            group_name=body.group_name,
            tenant_id=identity.tenant_id,
        ),
    )


@router.get("/version", summary="获取应用版本", response_model=str)
@require_role(Role.User)
async def get_version(_current_user: DBUser = Depends(get_current_active_user)) -> str:
    """获取应用版本"""
    return get_app_version()
