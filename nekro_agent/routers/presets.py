import base64
import json
from collections import Counter
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, File, Query, UploadFile
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from tortoise.expressions import Q
from tortoise.queryset import QuerySet

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import INSTANCE_METADATA_DEFAULT_PRESET_ID
from nekro_agent.models.db_preset import GLOBAL_PRESET_OWNER, DBPreset
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import (
    CloudServiceError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from nekro_agent.services.identity_admin import PRESET_SWITCH_REVERSIBLE_NOTE, preview_preset_switch
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import (
    assert_instance_accessible,
    get_tenant_scope,
    require_platform_owner,
)
from nekro_agent.systems.cloud.api.preset import (
    create_preset as cloud_create_preset,
)
from nekro_agent.systems.cloud.api.preset import (
    delete_preset as cloud_delete_preset,
)
from nekro_agent.systems.cloud.api.preset import (
    get_preset,
    list_user_presets,
)
from nekro_agent.systems.cloud.api.preset import (
    update_preset as cloud_update_preset,
)
from nekro_agent.systems.cloud.exceptions import NekroCloudDisabled
from nekro_agent.systems.cloud.schemas.preset import PresetCreate, PresetUpdate
from nekro_agent.tools.image_utils import process_image_data_url
from nekro_agent.tools.telemetry_util import generate_instance_id

router = APIRouter(prefix="/presets", tags=["Presets"])

logger = get_sub_logger("presets")

# 所有作用域参数共用的说明，避免每个端点重复一遍语义
INSTANCE_SCOPE_DESC = "实例作用域标识；留空表示平台方全局视图（可见并可管理全部人设）"


class TagInfo(BaseModel):
    tag: str
    count: int


class PresetSummary(BaseModel):
    id: int
    remote_id: str | None
    on_shared: bool
    owner_instance_key: str
    is_global: bool
    name: str
    title: str
    avatar: str
    description: str
    tags: str
    author: str
    is_remote: bool
    create_time: str
    update_time: str


class PresetDetail(PresetSummary):
    content: str


class PresetListResponse(BaseModel):
    total: int
    items: List[PresetSummary]


class ActionResponse(BaseModel):
    ok: bool = True


class ShareResponse(BaseModel):
    remote_id: str


class RefreshStatusResponse(BaseModel):
    updated_count: int
    total_cloud_presets: int


class AvatarUploadResponse(BaseModel):
    avatar: str


class InstanceDefaultPresetResponse(BaseModel):
    instance_key: str
    preset_id: Optional[int] = None
    preset: Optional[PresetSummary] = None


class InstanceDefaultPresetUpdate(BaseModel):
    preset_id: Optional[int] = None
    # 换人设 = 把频道切到另一份好感与记忆，必须先看过影响面。理由与
    # `POST /chat-channel/{chat_key}/preset` 那一处逐字相同，见该端点的 docstring。
    confirmed: bool = False


class InstanceDefaultPresetImpact(BaseModel):
    """改实例默认人设的影响面

    这是人设回退链的**第二层**：频道自己没绑人设时说话的就是它。改掉它，
    该实例下所有"没绑人设的频道"同时换人——影响面比逐个频道改大得多，
    却长期只是一个下拉框，这里补上与频道那一处同形的二次确认。
    """

    scope_label: str
    domain_id: str
    current_preset_id: int
    current_preset_name: str
    target_preset_id: int
    target_preset_name: str
    is_noop: bool
    affected_people: int
    returning_people: int
    affected_channels: int
    domain_memory_paragraphs: int
    memory_scope_note: str
    reversible_note: str


def _preset_summary(preset: DBPreset) -> PresetSummary:
    return PresetSummary(
        id=preset.id,
        remote_id=preset.remote_id,
        on_shared=preset.on_shared,
        owner_instance_key=preset.owner_instance_key,
        is_global=preset.owner_instance_key == GLOBAL_PRESET_OWNER,
        name=preset.name,
        title=preset.title or preset.name,
        avatar=preset.avatar,
        description=preset.description,
        tags=preset.tags,
        author=preset.author,
        is_remote=preset.remote_id is not None,
        create_time=preset.create_time.strftime("%Y-%m-%d %H:%M:%S"),
        update_time=preset.update_time.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _preset_detail(preset: DBPreset) -> PresetDetail:
    base = _preset_summary(preset)
    return PresetDetail(**base.model_dump(), content=preset.content)


def visible_presets(instance_key: str) -> QuerySet[DBPreset]:
    """按实例作用域筛出可见人设

    人设库全局共享，实例内新建的人设独享隔离，因此：
    - 全局视图（`instance_key` 为空）——平台方视角，可见全部人设；
    - 实例视图——只可见全局共享人设与该实例自己的私有人设。

    Args:
        instance_key: 实例作用域标识，空串表示全局视图

    Returns:
        已套上可见性条件的查询集
    """
    if not instance_key:
        return DBPreset.all()
    return DBPreset.filter(Q(owner_instance_key=GLOBAL_PRESET_OWNER) | Q(owner_instance_key=instance_key))


def writable_presets(instance_key: str) -> QuerySet[DBPreset]:
    """按实例作用域筛出可写人设

    全局共享人设由平台方维护，对实例只读：否则一个子实例的编辑/删除会波及所有实例。
    实例视图下只有该实例自己创建或下载的人设可写。

    Args:
        instance_key: 实例作用域标识，空串表示全局视图

    Returns:
        已套上可写性条件的查询集
    """
    if not instance_key:
        return DBPreset.all()
    return DBPreset.filter(owner_instance_key=instance_key)


async def get_visible_preset(preset_id: int, instance_key: str) -> DBPreset:
    """取一条对该实例可见的人设，越权（他实例私有人设）一律按不存在处理"""
    preset = await visible_presets(instance_key).filter(id=preset_id).first()
    if not preset:
        raise NotFoundError(resource="人设")
    return preset


async def get_writable_preset(preset_id: int, instance_key: str) -> DBPreset:
    """取一条该实例有权修改的人设

    他实例的私有人设按 404 处理（不暴露其存在性），全局共享人设按 403 处理（可见但只读）。
    """
    preset = await get_visible_preset(preset_id, instance_key)
    if instance_key and preset.owner_instance_key != instance_key:
        raise PermissionDeniedError(detail=f"全局共享人设 {preset_id} 对实例 {instance_key} 只读")
    return preset


async def resolve_instance_scope(user: DBUser, instance_key: str) -> str:
    """把请求里的实例作用域参数解析成当前身份实际可用的作用域

    平台方原样透传，空串仍然是"全局视图"，未启用多租户时行为与改造前完全一致。
    租户则必须显式指定自己名下的实例：空串对他们不能再退化成全局视图，
    否则等于让租户看见并改动全部人设；传他人的 `instance_key` 一律 404。
    """
    scope = instance_key.strip()
    if get_tenant_scope(user) is None:
        return scope
    if not scope:
        raise ValidationError(reason="租户身份必须指定实例作用域")
    await assert_instance_accessible(user, scope)
    return scope


async def _require_instance(instance_key: str) -> DBAdapterInstance:
    """按实例标识取实例行，不存在时抛 404

    与总后台一致，`instance_key` 在本项目里被当作全局唯一标识使用
    （数据库层的唯一约束是 `(adapter_key, instance_key)`，是它之上更宽的一层）。
    """
    if not instance_key:
        raise ValidationError(reason="实例标识不能为空")
    instance = await DBAdapterInstance.filter(instance_key=instance_key).first()
    if instance is None:
        raise NotFoundError(resource=f"实例 '{instance_key}'")
    return instance


def _load_instance_metadata(instance: DBAdapterInstance) -> Dict[str, Any]:
    """读取实例元数据；损坏或非对象的内容按空字典处理，避免单条脏数据打挂人设设置"""
    if not instance.metadata_json:
        return {}
    try:
        parsed = json.loads(instance.metadata_json)
    except (ValueError, TypeError):
        logger.warning(f"实例元数据解析失败，按空处理: instance_key={instance.instance_key}")
        return {}
    return parsed if isinstance(parsed, dict) else {}


@router.get("/tags", summary="获取所有可用的人设标签", response_model=List[TagInfo])
@require_role(Role.Admin)
async def get_all_preset_tags(
    instance_key: str = Query("", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> List[TagInfo]:
    all_presets = await visible_presets(await resolve_instance_scope(_current_user, instance_key)).values("tags")

    tag_counts = Counter()
    for preset in all_presets:
        tags_str = preset.get("tags")
        if tags_str:
            tags = [tag.strip() for tag in tags_str.split(",") if tag.strip()]
            tag_counts.update(tags)

    sorted_tags = sorted(tag_counts.items(), key=lambda item: (-item[1], item[0]))

    return [TagInfo(tag=tag, count=count) for tag, count in sorted_tags]


@router.get("/list", summary="获取人设列表", response_model=PresetListResponse)
@require_role(Role.Admin)
async def get_preset_list(
    page: int = 1,
    page_size: int = 20,
    search: Optional[str] = None,
    tag: Optional[str] = None,
    tags: Optional[str] = None,
    remote_only: Optional[bool] = None,
    instance_key: str = Query("", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> PresetListResponse:
    query = visible_presets(await resolve_instance_scope(_current_user, instance_key))

    if search:
        query = query.filter(Q(name__contains=search) | Q(title__contains=search) | Q(description__contains=search))

    selected_tags: List[str] = []
    if tags:
        selected_tags.extend([t.strip() for t in tags.split(",") if t.strip()])
    if tag:
        selected_tags.append(tag.strip())

    if selected_tags:
        for t in selected_tags:
            query = query.filter(tags__contains=t)
    if remote_only is not None:
        query = query.filter(remote_id__not_isnull=True) if remote_only else query.filter(remote_id__isnull=True)

    total = await query.count()

    query = query.offset((page - 1) * page_size).limit(page_size)
    query = query.order_by("-remote_id", "-update_time")

    presets = await query

    return PresetListResponse(total=total, items=[_preset_summary(p) for p in presets])


@router.get("/instance/{instance_key}/default", summary="获取实例默认人设", response_model=InstanceDefaultPresetResponse)
@require_role(Role.Admin)
async def get_instance_default_preset(
    instance_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> InstanceDefaultPresetResponse:
    """读取某实例的默认人设

    默认人设 ID 存在 `DBAdapterInstance.metadata_json` 里，由人设回退链
    （频道 → 实例默认 → 系统默认 → 内置）的第二层消费。已被删除或已不再可见的
    人设视同未设置，与回退链的运行时行为保持一致。
    """
    scope = await resolve_instance_scope(_current_user, instance_key)
    instance = await _require_instance(scope)
    preset_id = _load_instance_metadata(instance).get(INSTANCE_METADATA_DEFAULT_PRESET_ID)
    if not isinstance(preset_id, int):
        return InstanceDefaultPresetResponse(instance_key=scope)

    preset = await visible_presets(scope).filter(id=preset_id).first()
    if preset is None:
        logger.warning(f"实例 {scope} 的默认人设 id={preset_id} 已不存在或不再可见，按未设置返回")
        return InstanceDefaultPresetResponse(instance_key=scope)

    return InstanceDefaultPresetResponse(instance_key=scope, preset_id=preset.id, preset=_preset_summary(preset))


async def _effective_preset_id_without_instance_layer(adapter_key: str, preset_id: Optional[int]) -> int:
    """「频道没绑人设、实例默认是 `preset_id`」时实际说话的那个人设

    判据**复用 `DBChatChannel.get_effective_preset_id`**，不在这里另写一条回退链：
    只要它哪天改了（比如多插一层），这里会自动跟上，而抄一份就会分叉——
    分叉的后果是"预览说影响 3 人、实际换掉的是另一份"。

    探针的 `instance_key` 故意留空：`_get_instance_default_preset` 对空实例标识直接返回
    None，于是那一层被跳过，正好由 `preset_id` 这个参数**代替**它。剩下的
    「系统默认 → 内置」两层照常走真代码。探针只在内存里存在，绝不落库。
    """
    from nekro_agent.models.db_chat_channel import DBChatChannel

    probe = DBChatChannel(adapter_key=adapter_key, instance_key="", preset_id=preset_id)
    return await probe.get_effective_preset_id()


async def _instance_default_preset_impact(
    instance: DBAdapterInstance,
    target_preset_id: Optional[int],
) -> InstanceDefaultPresetImpact:
    """算出"改这个实例的默认人设会影响多少人、多少频道"

    受影响的频道 = 该实例下**自己没有有效人设**的那些（它们才落到实例默认这一层）；
    绑了自己人设的频道不受影响，把它们算进去会把影响面说得比实际大。
    """
    from nekro_agent.models.db_chat_channel import DBChatChannel

    current_raw = _load_instance_metadata(instance).get(INSTANCE_METADATA_DEFAULT_PRESET_ID)
    current_preset_id = await _effective_preset_id_without_instance_layer(
        instance.adapter_key,
        current_raw if isinstance(current_raw, int) else None,
    )
    target_resolved = await _effective_preset_id_without_instance_layer(instance.adapter_key, target_preset_id)

    channel_preset_ids = await DBChatChannel.filter(
        adapter_key=instance.adapter_key,
        instance_key=instance.instance_key,
    ).values_list("preset_id", flat=True)
    wanted = sorted({int(value) for value in channel_preset_ids if isinstance(value, int) and value > 0})
    live = set(await DBPreset.filter(id__in=wanted).values_list("id", flat=True)) if wanted else set()
    affected_channels = sum(
        1 for value in channel_preset_ids if not isinstance(value, int) or value <= 0 or value not in live
    )

    impact = await preview_preset_switch(
        adapter_key=instance.adapter_key,
        instance_key=instance.instance_key,
        scope_label=instance.display_name or instance.instance_key,
        current_preset_id=current_preset_id,
        target_preset_id=target_resolved,
        affected_channels=affected_channels,
    )
    return InstanceDefaultPresetImpact(
        scope_label=impact.scope_label,
        domain_id=impact.domain_id,
        current_preset_id=impact.current_preset_id,
        current_preset_name=impact.current_preset_name,
        target_preset_id=impact.target_preset_id,
        target_preset_name=impact.target_preset_name,
        is_noop=impact.is_noop,
        affected_people=impact.affected_people,
        returning_people=impact.returning_people,
        affected_channels=impact.affected_channels,
        domain_memory_paragraphs=impact.domain_memory_paragraphs,
        memory_scope_note=impact.memory_scope_note,
        reversible_note=PRESET_SWITCH_REVERSIBLE_NOTE,
    )


@router.get(
    "/instance/{instance_key}/default/impact",
    summary="预览改实例默认人设的影响面",
    response_model=InstanceDefaultPresetImpact,
)
@require_role(Role.Admin)
async def get_instance_default_preset_impact(
    instance_key: str,
    preset_id: Optional[int] = Query(default=None, description="目标默认人设；留空表示清除"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> InstanceDefaultPresetImpact:
    """二次确认弹窗要显示的那段内容"""
    scope = await resolve_instance_scope(_current_user, instance_key)
    instance = await _require_instance(scope)
    return await _instance_default_preset_impact(instance, preset_id)


@router.put("/instance/{instance_key}/default", summary="设置实例默认人设", response_model=ActionResponse)
@require_role(Role.Admin)
async def set_instance_default_preset(
    instance_key: str,
    body: InstanceDefaultPresetUpdate,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """设置或清除某实例的默认人设

    `preset_id` 传 null 表示清除。设置的人设必须对该实例可见（全局共享人设或它自己的
    私有人设），否则拒绝，避免把别的实例的私有人设挂成本实例默认。

    **真的换了生效人设时必须带 `confirmed=true`**：这一层管的是"频道没绑人设时说话的是谁"，
    改掉它等于一次性把该实例下所有未绑人设的频道切到另一份好感与记忆上。
    闸门与 `POST /chat-channel/{chat_key}/preset` 同形，理由见那里。
    """
    scope = await resolve_instance_scope(_current_user, instance_key)
    instance = await _require_instance(scope)
    metadata = _load_instance_metadata(instance)

    if body.preset_id is None:
        metadata.pop(INSTANCE_METADATA_DEFAULT_PRESET_ID, None)
    else:
        preset = await visible_presets(scope).filter(id=body.preset_id).first()
        if preset is None:
            raise NotFoundError(resource="人设")
        metadata[INSTANCE_METADATA_DEFAULT_PRESET_ID] = preset.id

    impact = await _instance_default_preset_impact(instance, body.preset_id)
    if not impact.is_noop and not body.confirmed:
        raise ValidationError(
            reason=(
                f"改实例默认人设会把 {impact.affected_channels} 个未绑人设的频道切到另一份好感与记忆："
                f"当前「{impact.current_preset_name}」下有 {impact.affected_people} 人的好感记录，"
                f"切到「{impact.target_preset_name}」后这些人看起来会归零"
                f"（目标人设下已有 {impact.returning_people} 人的旧记录）。"
                f"{PRESET_SWITCH_REVERSIBLE_NOTE} 确认后请带 confirmed=true 重试。"
            ),
        )

    instance.metadata_json = json.dumps(metadata, ensure_ascii=False)
    await instance.save(update_fields=["metadata_json", "update_time"])
    logger.info(f"实例 {scope} 默认人设更新为: {body.preset_id}")
    if not impact.is_noop:
        logger.warning(
            f"实例默认人设已更换: instance={scope}, {impact.current_preset_id} -> {impact.target_preset_id}, "
            f"affected_people={impact.affected_people}, affected_channels={impact.affected_channels}, "
            f"operator={_current_user.username}",
        )

    return ActionResponse(ok=True)


@router.get("/{preset_id}", summary="获取人设详情", response_model=PresetDetail)
@require_role(Role.Admin)
async def get_preset_detail(
    preset_id: int,
    instance_key: str = Query("", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> PresetDetail:
    preset = await get_visible_preset(preset_id, await resolve_instance_scope(_current_user, instance_key))

    return _preset_detail(preset)


@router.get("/by-remote/{remote_id}", summary="通过远程ID获取本地人设详情", response_model=PresetDetail)
@require_role(Role.Admin)
async def get_preset_detail_by_remote_id(
    remote_id: str,
    instance_key: str = Query("", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> PresetDetail:
    scope = await resolve_instance_scope(_current_user, instance_key)
    # 同一个云端人设可能同时存在全局副本与实例私有副本，本作用域自己的那份优先
    preset = await DBPreset.filter(remote_id=remote_id, owner_instance_key=scope).first()
    if preset is None:
        preset = await visible_presets(scope).filter(remote_id=remote_id).first()
    if not preset:
        raise NotFoundError(resource="人设")

    return _preset_detail(preset)


@router.post("", summary="创建人设", response_model=ActionResponse)
@require_role(Role.Admin)
async def create_preset(
    name: str = Body(...),
    title: str = Body(None),
    avatar: str = Body(...),
    content: str = Body(...),
    description: str = Body(""),
    tags: str = Body(""),
    author: str = Body(""),
    instance_key: str = Body("", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """创建人设

    带上 `instance_key` 即在该实例作用域下新建，人设归该实例独享；留空则由平台方创建为全局共享人设。
    """
    scope = await resolve_instance_scope(_current_user, instance_key)
    if scope:
        # 归属写错会让人设对所有视图都不可见（只剩平台方全局视图看得到），提前挡掉
        await _require_instance(scope)

    await DBPreset.create(
        name=name,
        title=title or name,
        avatar=avatar,
        content=content,
        description=description,
        tags=tags,
        author=author or _current_user.username,
        on_shared=False,
        owner_instance_key=scope,
    )

    return ActionResponse(ok=True)


@router.put("/{preset_id}", summary="更新人设", response_model=ActionResponse)
@require_role(Role.Admin)
async def update_preset(
    preset_id: int,
    name: str = Body(...),
    title: str = Body(None),
    avatar: str = Body(...),
    content: str = Body(...),
    description: str = Body(""),
    tags: str = Body(""),
    author: str = Body(""),
    remove_remote: bool = Body(False),
    instance_key: str = Body("", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """更新人设

    归属（`owner_instance_key`）不可通过本接口改动，避免实例把私有人设"提权"成全局共享。
    """
    preset = await get_writable_preset(preset_id, await resolve_instance_scope(_current_user, instance_key))

    preset.name = name
    preset.title = title or name
    preset.avatar = avatar
    preset.content = content
    preset.description = description
    preset.tags = tags
    preset.author = author or _current_user.username

    if remove_remote and preset.remote_id and not preset.on_shared:
        preset.remote_id = ""
        preset.on_shared = False

    await preset.save()

    return ActionResponse(ok=True)


@router.delete("/{preset_id}", summary="删除人设", response_model=ActionResponse)
@require_role(Role.Admin)
async def delete_preset(
    preset_id: int,
    instance_key: str = Query("", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """删除人设

    ## 这是「换人设」的**第四个**入口，而且是唯一一个不带主语的

    人设是隔离键 `(域, 人, 人设)` 的一维。删掉一个正在被使用的人设，所有绑着它的频道
    下一句话就落到回退链的下一档（实例默认 → 系统默认 → 内置）——后果与运营手动换人设
    **逐字相同**：那些频道的好感当场看起来归零。区别只在于这里没有一个"目标人设"可选，
    所以它长得不像换人设，也就长期没有任何提示。

    另外两个后台入口（`POST /chat-channel/{chat_key}/preset`、
    `PUT /presets/instance/{key}/default`）挂的是"显示影响人数 + 二次确认"；
    这一处**不加 `confirmed` 参数**：删除本来就有自己的确认弹窗，再叠一层等于让人
    学会一路点确认，而那正是二次确认失效的方式。这里做的是与 `AgentCtx.set_preset`
    同一种兜底——**必然留痕**：把"多少个频道会换人格、多少行好感会变成孤儿"写进 WARNING。
    孤儿数据不级联删（人设可能是误删），在「身份与域 → 孤儿数据」里可见、可手动清理。

    计数放在 `delete()` 之前只是稳妥，不是正确性依赖：`DBChatChannel.preset_id` 与
    `DBAffection.preset_id` 都是**普通整数列而不是外键**，删人设行不会连带改它们，
    所以前后两种写法此刻数出来的是同一个值。（写在这里是因为"删完再数会恒为 0"是一个
    很自然、但在这张 schema 上**不成立**的推断——真按它去写一条"证明顺序有意义"的用例，
    那条用例会恒绿。）哪天真给这两列加上外键与级联，顺序就立刻变成正确性依赖。
    """
    from nekro_agent.models.db_affection import DBAffection
    from nekro_agent.models.db_chat_channel import DBChatChannel

    preset = await get_writable_preset(preset_id, await resolve_instance_scope(_current_user, instance_key))

    bound_channels = await DBChatChannel.filter(preset_id=preset.id).count()
    orphaned_rows = await DBAffection.filter(preset_id=preset.id).count()

    await preset.delete()
    if bound_channels or orphaned_rows:
        logger.warning(
            f"删除了正在使用的人设: id={preset.id}, name={preset.name}, "
            f"{bound_channels} 个频道会落到回退链的下一档人设（那些频道的好感当场看起来归零），"
            f"{orphaned_rows} 行好感变成孤儿数据（不级联删，「身份与域 → 孤儿数据」可见可清理）；"
            f"{PRESET_SWITCH_REVERSIBLE_NOTE} operator={_current_user.username}",
        )
    return ActionResponse(ok=True)


@router.post("/{preset_id}/sync", summary="同步云端人设", response_model=ActionResponse)
@require_role(Role.Admin)
async def sync_preset(
    preset_id: int,
    instance_key: str = Query("", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    preset = await get_writable_preset(preset_id, await resolve_instance_scope(_current_user, instance_key))

    if not preset.remote_id:
        raise ValidationError(reason="此人设不是云端人设")

    response = await get_preset(preset.remote_id)
    if not response.success or not response.data:
        raise CloudServiceError(reason=str(response.message))

    preset.name = response.data.name
    preset.title = response.data.title
    preset.avatar = response.data.avatar
    preset.content = response.data.content
    preset.description = response.data.description
    preset.tags = response.data.tags
    preset.author = response.data.author
    await preset.save()

    return ActionResponse(ok=True)


@router.post("/upload-avatar", summary="上传头像", response_model=AvatarUploadResponse)
@require_role(Role.Admin)
async def upload_avatar(
    file: UploadFile = File(...),
    _current_user: DBUser = Depends(get_current_active_user),
) -> AvatarUploadResponse:
    contents = await file.read()

    base64_encoded = base64.b64encode(contents).decode("utf-8")

    mime_type = file.content_type or "image/jpeg"

    data_url = f"data:{mime_type};base64,{base64_encoded}"

    processed_data_url = await process_image_data_url(data_url)

    return AvatarUploadResponse(avatar=processed_data_url)


@router.post("/{preset_id}/share", summary="共享人设到云端", response_model=ShareResponse)
@require_role(Role.Admin)
async def share_preset(
    preset_id: int,
    is_sfw: bool = True,
    instance_key: str = Query("", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> ShareResponse:
    """共享人设到云端

    实例私有人设允许共享（内容本就属于该实例自己）；共享只是把内容推到云端，
    不改变本地归属，共享后它对其他实例仍然不可见。
    """
    preset = await get_writable_preset(preset_id, await resolve_instance_scope(_current_user, instance_key))

    if preset.on_shared and preset.remote_id:
        raise ValidationError(reason="此人设已经共享到云端")

    if not preset.name:
        raise ValidationError(reason="人设名称不能为空")
    if not preset.content:
        raise ValidationError(reason="人设内容不能为空")
    if not preset.avatar:
        raise ValidationError(reason="人设头像不能为空")
    if not preset.description:
        raise ValidationError(reason="人设描述不能为空，请先编辑人设添加描述")

    instance_id = generate_instance_id()

    try:
        preset_data = PresetCreate(
            name=preset.name,
            title=preset.title or preset.name,
            avatar=preset.avatar,
            content=preset.content,
            description=preset.description,
            tags=preset.tags,
            author=preset.author,
            ext_data=preset.ext_data or "",
            is_sfw=is_sfw,
            instance_id=instance_id,
        )
    except PydanticValidationError as e:
        raise ValidationError(reason=str(e)) from e

    response = await cloud_create_preset(preset_data)

    if not response.success or not response.data:
        raise CloudServiceError(reason=str(response.message))

    preset.remote_id = response.data.id
    preset.on_shared = True
    await preset.save()

    return ShareResponse(remote_id=response.data.id)


@router.post("/{preset_id}/unshare", summary="撤回共享人设", response_model=ActionResponse)
@require_role(Role.Admin)
async def unshare_preset(
    preset_id: int,
    instance_key: str = Query("", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    preset = await get_writable_preset(preset_id, await resolve_instance_scope(_current_user, instance_key))

    if not preset.on_shared or not preset.remote_id:
        raise ValidationError(reason="此人设未共享到云端")

    instance_id = generate_instance_id()
    remote_id = str(preset.remote_id) if preset.remote_id else ""
    response = await cloud_delete_preset(remote_id, instance_id)

    preset.on_shared = False

    if response.success:
        preset.remote_id = ""

    await preset.save()

    if not response.success:
        raise CloudServiceError(reason=str(response.message))

    return ActionResponse(ok=True)


@router.post("/{preset_id}/sync-to-cloud", summary="同步人设到云端", response_model=ActionResponse)
@require_role(Role.Admin)
async def sync_to_cloud(
    preset_id: int,
    is_sfw: bool = True,
    instance_key: str = Query("", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    preset = await get_writable_preset(preset_id, await resolve_instance_scope(_current_user, instance_key))

    if not preset.on_shared or not preset.remote_id:
        raise ValidationError(reason="此人设未共享到云端")

    if not preset.name:
        raise ValidationError(reason="人设名称不能为空")
    if not preset.content:
        raise ValidationError(reason="人设内容不能为空")
    if not preset.avatar:
        raise ValidationError(reason="人设头像不能为空")
    if not preset.description:
        raise ValidationError(reason="人设描述不能为空，请先编辑人设添加描述")

    instance_id = generate_instance_id()
    try:
        preset_data = PresetUpdate(
            name=preset.name,
            title=preset.title or preset.name,
            avatar=preset.avatar,
            content=preset.content,
            description=preset.description,
            tags=preset.tags,
            author=preset.author,
            ext_data=preset.ext_data or "",
            is_sfw=is_sfw,
            instance_id=instance_id,
        )
    except PydanticValidationError as e:
        raise ValidationError(reason=str(e)) from e

    remote_id = str(preset.remote_id) if preset.remote_id else ""

    response = await cloud_update_preset(remote_id, preset_data)

    if not response.success:
        raise CloudServiceError(reason=str(response.message))

    return ActionResponse(ok=True)


@router.post("/refresh-shared-status", summary="刷新人设共享状态", response_model=RefreshStatusResponse)
@require_role(Role.Admin)
async def refresh_shared_status(
    instance_key: str = Query("", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
    _platform: DBUser = Depends(require_platform_owner()),
) -> RefreshStatusResponse:
    """刷新人设共享状态

    只回写当前作用域可写的人设：实例视图下不会去改全局共享人设的共享状态。

    这一条是平台方专属（`Role.Admin` + `require_platform_owner()`，未启用多租户的部署
    行为逐字不变），有两条理由：

    1. 它按 `list_user_presets()` —— **部署级社区账号**的发布清单 —— 回写 `on_shared`，
       于是"平台方一共发布了几条"会随 `total_cloud_presets` 漏给调用方；
    2. 更要紧的是它是 `on_shared` 的第二个写入口。`cloud/presets_market.py:download_preset`
       已经不再让实例作用域下的副本继承 `on_shared`（否则租户能拿平台方自己发布的人设
       走 `sync-to-cloud` / `unshare` 改写或下架社区里的公开发布）；这里若还留给租户，
       它只要下载完再刷一次，`remote_id ∈ 平台方发布清单` 就会把 `on_shared` 掰回
       `True`，那条接管链原样复活。两处必须一起收，缺一个都不成立。
    """
    scope = await resolve_instance_scope(_current_user, instance_key)
    try:
        response = await list_user_presets()

        if not response.success:
            raise CloudServiceError(reason=f"获取云端人设列表失败: {response.error}")

        if not response.data or not response.data.items:
            await writable_presets(scope).filter(on_shared=True).update(on_shared=False)
            return RefreshStatusResponse(updated_count=0, total_cloud_presets=0)

        cloud_preset_ids = [item.id for item in response.data.items]
        local_presets = await writable_presets(scope).filter(remote_id__not_isnull=True).all()

        updated_count = 0
        for preset in local_presets:
            if not preset.remote_id:
                continue

            is_in_cloud = preset.remote_id in cloud_preset_ids

            if preset.on_shared != is_in_cloud:
                preset.on_shared = is_in_cloud
                await preset.save()
                updated_count += 1

        return RefreshStatusResponse(
            updated_count=updated_count,
            total_cloud_presets=len(cloud_preset_ids),
        )
    except NekroCloudDisabled:
        raise CloudServiceError(reason="Nekro Cloud 未启用") from None
