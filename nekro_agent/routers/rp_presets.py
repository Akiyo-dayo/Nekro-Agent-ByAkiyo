"""角色扮演预设（酒馆对话补全预设）的导入与管理接口

鉴权与作用域照 `routers/presets.py`：`Role.Admin`，`resolve_instance_scope` 把请求里的
`instance_key` 解析成当前身份可用的作用域。可见范围是「全局 + 本实例」；可写范围是
平台方的全部，或租户自己实例的那一份（全局预设对租户只读）。

上传只收 JSON，大小上限 2 MiB；解析是纯函数（`services/tavern/rp_preset.py`），
`/inspect` 只解析不落库。导入、改开关、删除都记审计，只存名字、段数与段落摘要的
前 12 位，不存正文。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, File, Form, Query, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from tortoise.expressions import Q
from tortoise.queryset import QuerySet

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_preset import GLOBAL_PRESET_OWNER
from nekro_agent.models.db_rp_preset import DBRpPreset
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import NotFoundError, PermissionDeniedError, ValidationError
from nekro_agent.services.tavern import audit
from nekro_agent.services.tavern.preset_refs import (
    MAX_REFERENCES_PER_PRESET,
    RpPresetReferencesResponse,
    find_rp_preset_references,
)
from nekro_agent.services.tavern.rp_preset import (
    MAX_FILE_BYTES,
    RpPresetRejected,
    RpSegment,
    parse_preset,
    segment_as_dict,
    segments_from_stored,
)
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import assert_instance_accessible, get_tenant_scope

from .presets import resolve_instance_scope

router = APIRouter(prefix="/rp-presets", tags=["Roleplay Presets"])

logger = get_sub_logger("rp_presets")

# 单段摘要用于审计；不含正文。段落列表按固定键序序列化，同一个开关状态得到同一个摘要。
_AUDIT_SHA_CHARS = 12


class RpPresetSegmentBrief(BaseModel):
    """列表 / 检查接口里的段落：不带正文，只给字数与排布信息"""

    identifier: str
    name: str
    role: str
    marker: bool
    position: str
    depth: int
    injection_order: int
    enabled: bool
    chars: int


class RpPresetSegmentDetail(RpPresetSegmentBrief):
    """详情接口里的段落：带正文，供前端显示"""

    content: str
    forbid_overrides: bool = False


class RpPresetInspectResponse(BaseModel):
    name: str
    segments: list[RpPresetSegmentBrief] = Field(default_factory=list)
    notes: list[dict[str, Any]] = Field(default_factory=list)
    regex_count: int = 0


class RpPresetSummary(BaseModel):
    id: int
    name: str
    owner_instance_key: str
    segment_count: int
    enabled_count: int
    read_only: bool
    update_time: str


class RpPresetListResponse(BaseModel):
    total: int
    items: list[RpPresetSummary] = Field(default_factory=list)


class RpPresetDetail(RpPresetSummary):
    segments: list[RpPresetSegmentDetail] = Field(default_factory=list)
    notes: list[dict[str, Any]] = Field(default_factory=list)
    regex_count: int = 0


class RpPresetUpdateRequest(BaseModel):
    """只改名字与开关；正文与顺序不在这一版的可编辑范围内"""

    name: Optional[str] = None
    enabled: dict[str, bool] = Field(default_factory=dict)


class RpPresetDeleteResponse(BaseModel):
    ok: bool = True
    # 删除前的引用情况：真被某一层配置引用过就是 True。只是如实汇报，不拦删除
    # （运行时会把它当成没选，见 `services/tavern/preset_refs.py`）。
    referenced: bool = False


# ---------------------------------------------------------------------------
# 可见 / 可写范围
# ---------------------------------------------------------------------------


def visible_rp_presets(instance_key: str) -> QuerySet[DBRpPreset]:
    """按实例作用域筛出可见预设：全局视图看全部，实例视图看全局 + 自己"""
    if not instance_key:
        return DBRpPreset.all()
    return DBRpPreset.filter(Q(owner_instance_key=GLOBAL_PRESET_OWNER) | Q(owner_instance_key=instance_key))


def writable_rp_presets(instance_key: str) -> QuerySet[DBRpPreset]:
    """按实例作用域筛出可写预设：实例视图只有自己那份；全局视图平台方写全部"""
    if not instance_key:
        return DBRpPreset.all()
    return DBRpPreset.filter(owner_instance_key=instance_key)


async def get_visible_rp_preset(preset_id: int, instance_key: str) -> DBRpPreset:
    preset = await visible_rp_presets(instance_key).filter(id=preset_id).first()
    if preset is None:
        raise NotFoundError(resource="角色扮演预设")
    return preset


async def get_writable_rp_preset(preset_id: int, instance_key: str) -> DBRpPreset:
    preset = await get_visible_rp_preset(preset_id, instance_key)
    if instance_key and preset.owner_instance_key != instance_key:
        raise PermissionDeniedError(detail=f"全局角色扮演预设 {preset_id} 对实例 {instance_key} 只读")
    return preset


async def _require_scope_instance(user: DBUser, scope: str) -> str:
    """非空作用域必须是当前身份够得着的实例；平台方也要求存在

    返回这个实例所在的适配器标识（没有作用域时是空串）：实例标识只在适配器内唯一，
    后面要按实例取数的地方得带上它，才不会碰到别的适配器下的同名实例。
    """
    if not scope:
        return ""
    instance = await assert_instance_accessible(user, scope)
    return str(instance.adapter_key)


def _read_only(user: DBUser, scope: str, row: DBRpPreset) -> bool:
    if get_tenant_scope(user) is None:
        return False
    return row.owner_instance_key != scope


def _segment_count(segments: Any) -> tuple[int, int]:
    if not isinstance(segments, list):
        return 0, 0
    items = [item for item in segments if isinstance(item, dict)]
    return len(items), sum(1 for item in items if bool(item.get("enabled")))


def _summary(row: DBRpPreset, user: DBUser, scope: str) -> RpPresetSummary:
    total, enabled = _segment_count(row.segments)
    return RpPresetSummary(
        id=int(row.id),
        name=row.name,
        owner_instance_key=row.owner_instance_key,
        segment_count=total,
        enabled_count=enabled,
        read_only=_read_only(user, scope, row),
        update_time=row.update_time.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _brief(segment: RpSegment) -> RpPresetSegmentBrief:
    return RpPresetSegmentBrief(
        identifier=segment.identifier,
        name=segment.name,
        role=segment.role,
        marker=segment.marker,
        position=segment.position,
        depth=segment.depth,
        injection_order=segment.injection_order,
        enabled=segment.enabled,
        chars=0 if segment.marker else len(segment.content),
    )


def _detail(row: DBRpPreset, user: DBUser, scope: str) -> RpPresetDetail:
    segments = segments_from_stored(row.segments)
    return RpPresetDetail(
        **_summary(row, user, scope).model_dump(),
        segments=[
            RpPresetSegmentDetail(
                **_brief(segment).model_dump(),
                content=segment.content,
                forbid_overrides=segment.forbid_overrides,
            )
            for segment in segments
        ],
        notes=row.notes if isinstance(row.notes, list) else [],
        regex_count=len(row.regex_rules) if isinstance(row.regex_rules, list) else 0,
    )


def _audit_summary(name: str, segments: Any) -> dict[str, Any]:
    """审计摘要：名字、段数、规范化段落的 sha 前 12 位；不含任何正文"""
    payload = json.dumps(segments, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_AUDIT_SHA_CHARS]
    return {
        "name": name[:128],
        "segments": len(segments) if isinstance(segments, list) else 0,
        "sha": digest,
    }


async def _read_upload(file: UploadFile) -> bytes:
    """按 2 MiB 上限多读一个字节判断超限，不把超大文件整个读进内存"""
    data = await file.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValidationError(reason=f"预设文件超过 {MAX_FILE_BYTES // (1024 * 1024)} MiB")
    return data


def _parse_upload(data: bytes) -> tuple[dict[str, Any], list[RpSegment], list[dict[str, Any]], list[dict[str, str]]]:
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValidationError(reason="不是有效的 JSON 文件") from None
    if not isinstance(raw, dict):
        raise ValidationError(reason="这不是对话补全预设（顶层不是对象）")
    try:
        segments, rules, notes = parse_preset(raw)
    except RpPresetRejected as error:
        raise ValidationError(reason=str(error)) from error
    return raw, segments, rules, notes


def _default_name(filename: Optional[str]) -> str:
    """文件名去扩展名；去掉首尾空白并截到列宽"""
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return name.strip()[:128] or "未命名预设"


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


@router.get("", summary="列出角色扮演预设", response_model=RpPresetListResponse)
@require_role(Role.Admin)
async def list_rp_presets(
    instance_key: str = Query("", description="实例作用域标识；留空表示平台方全局视图"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> RpPresetListResponse:
    scope = await resolve_instance_scope(_current_user, instance_key)
    await _require_scope_instance(_current_user, scope)
    rows = await visible_rp_presets(scope).order_by("-update_time", "-id")
    items = [_summary(row, _current_user, scope) for row in rows]
    return RpPresetListResponse(total=len(items), items=items)


@router.post("/inspect", summary="检查一份预设（不落库）", response_model=RpPresetInspectResponse)
@require_role(Role.Admin)
async def inspect_rp_preset_endpoint(
    file: UploadFile = File(..., description="酒馆对话补全预设 JSON"),
    instance_key: str = Form("", description="实例作用域标识"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> RpPresetInspectResponse:
    scope = await resolve_instance_scope(_current_user, instance_key)
    await _require_scope_instance(_current_user, scope)
    _raw, segments, _rules, notes = _parse_upload(await _read_upload(file))
    return RpPresetInspectResponse(
        name=_default_name(file.filename),
        segments=[_brief(segment) for segment in segments],
        notes=notes,
        regex_count=len(_rules),
    )


@router.post("/import", summary="导入一份预设", response_model=RpPresetDetail)
@require_role(Role.Admin)
async def import_rp_preset_endpoint(
    file: UploadFile = File(..., description="酒馆对话补全预设 JSON"),
    instance_key: str = Form("", description="实例作用域标识"),
    name: Optional[str] = Form(None, description="显示名；留空取文件名"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> RpPresetDetail:
    scope = await resolve_instance_scope(_current_user, instance_key)
    await _require_scope_instance(_current_user, scope)
    raw, segments, rules, notes = _parse_upload(await _read_upload(file))
    display_name = (name or "").strip()[:128] or _default_name(file.filename)
    row = await DBRpPreset.create(
        name=display_name,
        owner_instance_key=scope,
        segments=[segment_as_dict(segment) for segment in segments],
        raw=raw,
        regex_rules=rules,
        notes=notes,
        created_by=_current_user.username,
        updated_by=_current_user.username,
    )
    await audit.record_change(
        user=_current_user,
        layer="rp_preset",
        field="rp_preset.import",
        old="",
        new=_audit_summary(display_name, row.segments),
        instance_key=scope,
    )
    return _detail(row, _current_user, scope)


@router.get("/{preset_id}", summary="角色扮演预设详情", response_model=RpPresetDetail)
@require_role(Role.Admin)
async def get_rp_preset_endpoint(
    preset_id: int,
    instance_key: str = Query("", description="实例作用域标识"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> RpPresetDetail:
    scope = await resolve_instance_scope(_current_user, instance_key)
    await _require_scope_instance(_current_user, scope)
    row = await get_visible_rp_preset(preset_id, scope)
    return _detail(row, _current_user, scope)


@router.put("/{preset_id}", summary="改名字或段落开关", response_model=RpPresetDetail)
@require_role(Role.Admin)
async def update_rp_preset_endpoint(
    preset_id: int,
    body: RpPresetUpdateRequest = Body(...),
    instance_key: str = Query("", description="实例作用域标识"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> RpPresetDetail:
    scope = await resolve_instance_scope(_current_user, instance_key)
    await _require_scope_instance(_current_user, scope)
    row = await get_writable_rp_preset(preset_id, scope)
    before = _audit_summary(row.name, row.segments)

    if body.name is not None:
        display_name = body.name.strip()[:128]
        if not display_name:
            raise ValidationError(reason="预设名不能为空")
        row.name = display_name
    if body.enabled:
        segments = row.segments if isinstance(row.segments, list) else []
        updated: list[Any] = []
        for item in segments:
            if isinstance(item, dict) and item.get("identifier") in body.enabled:
                item = {**item, "enabled": bool(body.enabled[str(item.get("identifier"))])}
            updated.append(item)
        row.segments = updated
    row.updated_by = _current_user.username
    await row.save()
    await audit.record_change(
        user=_current_user,
        layer="rp_preset",
        field="rp_preset.toggle",
        old=before,
        new=_audit_summary(row.name, row.segments),
        instance_key=scope,
    )
    return _detail(row, _current_user, scope)


@router.get(
    "/{preset_id}/references",
    summary="这个预设被哪些配置层引用",
    response_model=RpPresetReferencesResponse,
)
@require_role(Role.Admin)
async def get_rp_preset_references_endpoint(
    preset_id: int,
    instance_key: str = Query("", description="实例作用域标识；留空表示平台方全局视图"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> RpPresetReferencesResponse:
    """权限与可见性同详情接口：看不见的预设按 404 处理，看不到「有没有这个 id」

    作用域非空时只看这个实例的实例层与它名下频道的频道层（平台层与别的实例不算）；
    全局视图扫全部四层。`items` 最多 `MAX_REFERENCES_PER_PRESET` 条，`total` 是真实总数。
    """
    scope = await resolve_instance_scope(_current_user, instance_key)
    adapter_key = await _require_scope_instance(_current_user, scope)
    await get_visible_rp_preset(preset_id, scope)
    items = await find_rp_preset_references(preset_id, only_instance_key=scope, only_adapter_key=adapter_key)
    return RpPresetReferencesResponse(
        total=len(items),
        items=items[:MAX_REFERENCES_PER_PRESET],
    )


@router.delete("/{preset_id}", summary="删除角色扮演预设", response_model=RpPresetDeleteResponse)
@require_role(Role.Admin)
async def delete_rp_preset_endpoint(
    preset_id: int,
    instance_key: str = Query("", description="实例作用域标识"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> RpPresetDeleteResponse:
    scope = await resolve_instance_scope(_current_user, instance_key)
    adapter_key = await _require_scope_instance(_current_user, scope)
    row = await get_writable_rp_preset(preset_id, scope)
    # 删前扫一遍引用：结果只用于提示操作者，不拦删除。查不出来不阻断删除——这时如实
    # 报「没查到引用」比把一次管理动作卡死在一个只读查询上更合适，WARNING 里留着原因。
    try:
        referenced = bool(
            await find_rp_preset_references(preset_id, only_instance_key=scope, only_adapter_key=adapter_key),
        )
    except Exception as exc:  # noqa: BLE001 - 引用普查失败不该让删除失败
        logger.warning(f"删除角色扮演预设前扫描引用失败，按未引用处理: preset_id={preset_id} | {type(exc).__name__}")
        referenced = False
    await audit.record_change(
        user=_current_user,
        layer="rp_preset",
        field="rp_preset.delete",
        old=_audit_summary(row.name, row.segments),
        new="",
        instance_key=scope,
    )
    await row.delete()
    # 真被引用过也照删：运行时会把它当成没选，只是日志里报一条去重 WARNING。
    return RpPresetDeleteResponse(ok=True, referenced=referenced)


@router.get("/{preset_id}/export", summary="导出原文件")
@require_role(Role.Admin)
async def export_rp_preset_endpoint(
    preset_id: int,
    instance_key: str = Query("", description="实例作用域标识"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> JSONResponse:
    scope = await resolve_instance_scope(_current_user, instance_key)
    await _require_scope_instance(_current_user, scope)
    row = await get_visible_rp_preset(preset_id, scope)
    filename = (row.name or "rp-preset").replace('"', "").replace("\\", "_").replace("/", "_")
    # 头字段只能是 latin-1；中文名走 RFC 5987，另给一个纯 ASCII 的回退名。
    disposition = f"attachment; filename=\"rp-preset.json\"; filename*=UTF-8''{quote(filename + '.json')}"
    return JSONResponse(
        content=row.raw if isinstance(row.raw, dict) else {},
        headers={"Content-Disposition": disposition},
    )


__all__ = [
    "get_visible_rp_preset",
    "get_writable_rp_preset",
    "router",
    "visible_rp_presets",
    "writable_rp_presets",
]
