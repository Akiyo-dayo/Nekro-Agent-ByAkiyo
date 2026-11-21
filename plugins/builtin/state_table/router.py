"""角色状态的管理接口（后台「角色状态」页用）

每个端点都挂 `plugin_route_scope`：身份、管理员角色、实例归属、插件在这个实例上生效。租户必须带
`instance_key`（与 `adapter_key`），只能看、改自己实例的频道状态与个人档案；平台方也要指定实例。

- 模板跟人设走：全局人设（`owner_instance_key` 为空）的模板只有平台方能改，租户只读；实例自己的人设，
  这个实例的管理员能改。
- 个人档案：自成一域的档案归那个实例；属于实例组的档案不属于任何一个实例，只有平台方能看能改（列表里
  平台方另外看得到这个实例所在的那个组的档案）。
- 改状态、改档案、回滚、重置、恢复更新、改模板都写一行快照（`source=admin` / `rollback` / `reset`，带操作者），
  兼作审计。改动要带读到时的版本号，版本变了返回 409，不覆盖别处刚做的修改。
- 状态值（含私密字段）只在详情里按作用域给管理员看；列表、审计里不带值。
"""

from __future__ import annotations

import math
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import Response
from tortoise.expressions import Q, Subquery

from nekro_agent.core.config import config as global_config
from nekro_agent.models.db_affection import DBAffection
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_instance_group import DBInstanceDomainBinding
from nekro_agent.models.db_preset import GLOBAL_PRESET_OWNER, DBPreset
from nekro_agent.models.db_state_channel import DBStateChannel
from nekro_agent.models.db_state_person import DBStatePerson
from nekro_agent.models.db_state_snapshot import DBStateSnapshot
from nekro_agent.models.db_state_template import DBStateTemplate
from nekro_agent.schemas.errors import (
    ConcurrentWriteConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from nekro_agent.services.plugin.route_auth import PluginRouteScope, plugin_route_scope

from . import builtin_templates, card, store, view
from . import tree as state_tree
from .initvar import MAX_DEPTH, MAX_NODES
from .plugin import plugin
from .schema import Template, TemplateError
from .session import derive_from_book, initvar_entries

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

_scope = plugin_route_scope(plugin.key)


def _editor(scope: PluginRouteScope) -> str:
    return str(getattr(scope.user, "username", "") or "")[:64]


def _check_tree(data: Any) -> dict[str, Any]:
    """管理员提交的整棵树：必须是对象、只含 JSON 形状、节点数与深度有上限、序列化后不超过 64 KB"""
    if not isinstance(data, dict):
        raise ValidationError(reason="状态必须是一个对象")
    stack: list[tuple[Any, int]] = [(data, 1)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise ValidationError(reason="状态太大或嵌套太深")
        if isinstance(node, dict):
            stack.extend((value, depth + 1) for value in node.values())
        elif isinstance(node, list):
            stack.extend((value, depth + 1) for value in node)
        elif isinstance(node, float) and not math.isfinite(node):
            # PG 的 jsonb 不收 NaN / Infinity，进来了也写不进去
            raise ValidationError(reason="状态里的数字必须是有限的")
        elif not (node is None or isinstance(node, (str, int, float, bool))):
            raise ValidationError(reason="状态里只能有文字、数字、真假值、列表与对象")
    if state_tree.tree_bytes(data) > state_tree.MAX_TREE_BYTES:
        raise ValidationError(reason="状态超过 64 KB")
    return state_tree.copy_tree(data)


async def _channel_row(scope: PluginRouteScope, state_id: int) -> DBStateChannel:
    row = await DBStateChannel.get_or_none(id=state_id, adapter_key=scope.adapter_key, instance_key=scope.instance_key)
    if row is None:
        raise NotFoundError(resource="频道状态")
    return row


async def _person_row(scope: PluginRouteScope, person_id: int) -> DBStatePerson:
    row = await DBStatePerson.get_or_none(id=person_id)
    if row is None:
        raise NotFoundError(resource="个人档案")
    own = row.adapter_key == scope.adapter_key and row.instance_key == scope.instance_key and not row.domain_group_id
    if not own and not (scope.is_platform and row.domain_group_id):
        # 实例组域的档案不属于任何一个实例：只有平台方能动
        raise NotFoundError(resource="个人档案")
    return row


async def _preset_for(scope: PluginRouteScope, preset_id: int, *, write: bool) -> DBPreset:
    preset = await DBPreset.get_or_none(id=preset_id)
    if preset is None:
        raise NotFoundError(resource="人设")
    owner = str(preset.owner_instance_key or GLOBAL_PRESET_OWNER)
    if owner == GLOBAL_PRESET_OWNER:
        if write and not scope.is_platform:
            raise PermissionDeniedError
        return preset
    if owner != scope.instance_key and not scope.is_platform:
        raise NotFoundError(resource="人设")
    return preset


def _summary(row: DBStateChannel) -> dict[str, Any]:
    return {
        "id": row.id,
        "chat_key": row.chat_key,
        "preset_id": row.preset_id,
        "version": row.version,
        "paused": row.paused,
        "consecutive_failures": row.consecutive_failures,
        "last_update_at": row.last_update_at.isoformat() if row.last_update_at else None,
        "update_time": row.update_time.isoformat() if row.update_time else None,
    }


def _snapshot_summary(row: DBStateSnapshot) -> dict[str, Any]:
    return {
        "id": row.id,
        "source": row.source,
        "result": row.result,
        "has_data": row.data is not None,
        "applied": row.ops_applied,
        "rejected": row.ops_rejected,
        "model_group": row.model_group,
        "editor": row.editor,
        "create_time": row.create_time.isoformat() if row.create_time else None,
    }


def _keep(scope: PluginRouteScope) -> int:
    from .updater import scoped_config

    return int(scoped_config(scope.instance_key).SNAPSHOT_KEEP)


async def _audit(
    *,
    kind: str,
    target_id: int,
    scope: PluginRouteScope,
    source: str,
    data: Optional[dict[str, Any]],
    chat_key: str = "",
    ops: Optional[list[dict[str, str]]] = None,
    domain: Optional[tuple[str, str]] = None,
) -> None:
    """写一行审计；`domain` 是目标自己的 `(adapter_key, instance_key)`（个人档案属于实例组时是空串）"""
    adapter_key, instance_key = domain if domain is not None else (scope.adapter_key, scope.instance_key)
    await store.write_snapshot(
        target_kind=kind,
        target_id=target_id,
        chat_key=chat_key,
        adapter_key=adapter_key,
        instance_key=instance_key,
        source=source,
        result="applied",
        data=data,
        applied=ops or [],
        editor=_editor(scope),
        keep=_keep(scope),
    )


def _changed_keys(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, str]]:
    keys = sorted(set(before) | set(after))
    return [{"op": "admin", "path": f"/{key}"} for key in keys if before.get(key) != after.get(key)]


def _paging(page: int, size: int) -> tuple[int, int]:
    """(偏移, 条数)；页码从 1 起，每页 1 ~ :data:`MAX_PAGE_SIZE` 条"""
    size = min(max(int(size), 1), MAX_PAGE_SIZE)
    return (max(int(page), 1) - 1) * size, size


async def _group_domain(scope: PluginRouteScope) -> int:
    """这个实例所在的好感域实例组（没有绑定时为 0）"""
    binding = await DBInstanceDomainBinding.get_or_none(adapter_key=scope.adapter_key, instance_key=scope.instance_key)
    if binding is None:
        return 0
    group = binding.affection_group_id if binding.affection_group_id is not None else binding.memory_group_id
    return int(group or 0)


def _person_summary(row: DBStatePerson, nickname: str = "") -> dict[str, Any]:
    return {
        "id": row.id,
        "preset_id": row.preset_id,
        "platform_userid": row.platform_userid,
        "nickname": nickname,
        "domain_group_id": row.domain_group_id or 0,
        "version": row.version,
        "update_time": row.update_time.isoformat() if row.update_time else None,
    }


def _audit_summary(row: DBStateSnapshot) -> dict[str, Any]:
    return {
        "id": row.id,
        "target_kind": row.target_kind,
        "target_id": row.target_id,
        "chat_key": row.chat_key,
        "source": row.source,
        "ops": [
            {"op": str(item.get("op", ""))[:16], "path": str(item.get("path", ""))[:200]}
            for item in (row.ops_applied or [])
            if isinstance(item, dict)
        ][:20],
        "editor": row.editor,
        "create_time": row.create_time.isoformat() if row.create_time else None,
    }


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/setup-status")
    async def setup_status(scope: PluginRouteScope = Depends(_scope)) -> dict[str, Any]:
        """这个实例的启用进度（只读）：可见人设里已有模板的个数、频道状态行、个人档案行与更新用的模型组"""
        from .updater import scoped_config

        visible_presets = DBPreset.filter(owner_instance_key__in=[scope.instance_key, GLOBAL_PRESET_OWNER])
        template_count = await DBStateTemplate.filter(preset_id__in=Subquery(visible_presets.values("id"))).count()
        channel_count = await DBStateChannel.filter(
            adapter_key=scope.adapter_key, instance_key=scope.instance_key
        ).count()
        own = Q(adapter_key=scope.adapter_key, instance_key=scope.instance_key, domain_group_id=0)
        group = await _group_domain(scope) if scope.is_platform else 0
        person_count = await DBStatePerson.filter(own | Q(domain_group_id=group) if group else own).count()

        cfg = scoped_config(scope.instance_key)
        configured = str(cfg.UPDATE_MODEL_GROUP or "")
        effective = configured.strip() or str(getattr(global_config, "USE_MODEL_GROUP", "") or "").strip()
        return {
            "template_count": template_count,
            "channel_count": channel_count,
            "person_count": person_count,
            "update_model_group": configured,
            "update_model_group_effective": effective,
            "update_uses_fallback": not configured.strip(),
            "inline_private_update": bool(cfg.INLINE_UPDATE_ENABLED),
        }

    @router.get("/channels")
    async def list_channels(
        preset_id: Optional[int] = Query(None),
        paused: Optional[bool] = Query(None),
        q: str = Query("", max_length=100),
        page: int = Query(1, ge=1),
        size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        scope: PluginRouteScope = Depends(_scope),
    ) -> dict[str, Any]:
        """这个实例的频道状态列表（不带值）：频道名、人设名、最近一次更新的来源与结果"""
        query = DBStateChannel.filter(adapter_key=scope.adapter_key, instance_key=scope.instance_key)
        if preset_id is not None:
            query = query.filter(preset_id=preset_id)
        if paused is not None:
            query = query.filter(paused=paused)
        if q.strip():
            named = await DBChatChannel.filter(
                adapter_key=scope.adapter_key, instance_key=scope.instance_key, channel_name__icontains=q.strip()
            ).values_list("chat_key", flat=True)
            query = query.filter(Q(chat_key__icontains=q.strip()) | Q(chat_key__in=list(named)))
        offset, limit = _paging(page, size)
        total = await query.count()
        rows = await query.order_by("-update_time", "-id").offset(offset).limit(limit)
        chat_keys = [row.chat_key for row in rows]
        names = dict(
            await DBChatChannel.filter(chat_key__in=chat_keys).values_list("chat_key", "channel_name")  # type: ignore[arg-type]
        )
        presets = dict(
            await DBPreset.filter(id__in=list({row.preset_id for row in rows})).values_list("id", "name")  # type: ignore[arg-type]
        )
        latest: dict[int, DBStateSnapshot] = {}
        if rows:
            records = (
                await DBStateSnapshot.filter(target_kind="channel", target_id__in=[row.id for row in rows])
                .exclude(source__in=store.AUDIT_SOURCES)
                .order_by("-id")
                .limit(len(rows) * 20)
            )
            for record in records:
                latest.setdefault(int(record.target_id), record)
        items = []
        for row in rows:
            record = latest.get(int(row.id))
            items.append(
                {
                    **_summary(row),
                    "channel_name": str(names.get(row.chat_key) or ""),
                    "preset_name": str(presets.get(row.preset_id) or ""),
                    "last_source": record.source if record is not None else None,
                    "last_result": record.result if record is not None else None,
                }
            )
        return {"items": items, "total": total, "page": page, "size": limit}

    @router.get("/channels/{state_id}/image")
    async def channel_image(state_id: int, scope: PluginRouteScope = Depends(_scope)) -> Response:
        """状态图预览：画的是频道里会看到的那一份（群聊不带私密字段与任何人的档案）

        与频道里的「/状态 图」共用同一个冷却和画图位。
        """
        row = await _channel_row(scope, state_id)
        channel = await DBChatChannel.filter(chat_key=row.chat_key).order_by("id").first()
        if channel is None:
            raise NotFoundError(resource="频道")
        data = await view.build_card_for_row(channel, row, requester_uid="", include_requester=False)
        if data is None:
            raise NotFoundError(resource="状态模板")
        left = card.cooldown_left(row.chat_key, "image", card.cooldown_seconds(scope.instance_key, "image"))
        if left:
            raise ValidationError(reason=f"这个频道刚画过状态图，{left} 秒后再试")
        image = await card.render(data)
        if image is None:
            raise ValidationError(reason="现在画不了状态图（环境里没有中文字体，或者画图的人太多）")
        return Response(content=image, media_type="image/png", headers={"Cache-Control": "no-store"})

    @router.get("/channels/{state_id}")
    async def channel_detail(state_id: int, scope: PluginRouteScope = Depends(_scope)) -> dict[str, Any]:
        row = await _channel_row(scope, state_id)
        snapshots = await DBStateSnapshot.filter(target_kind="channel", target_id=row.id).order_by("-id").limit(70)
        return {**_summary(row), "data": row.data, "snapshots": [_snapshot_summary(item) for item in snapshots]}

    @router.put("/channels/{state_id}")
    async def edit_channel(
        state_id: int,
        data: Any = Body(..., embed=True),
        version: int = Body(..., embed=True),
        scope: PluginRouteScope = Depends(_scope),
    ) -> dict[str, Any]:
        row = await _channel_row(scope, state_id)
        tree = _check_tree(data)
        if row.version != version:
            raise ConcurrentWriteConflictError(operation="修改频道状态")
        before = dict(row.data or {})
        if not await store.commit_channel(row, tree):
            raise ConcurrentWriteConflictError(operation="修改频道状态")
        await _audit(
            kind="channel",
            target_id=row.id,
            scope=scope,
            source="admin",
            data=tree,
            chat_key=row.chat_key,
            ops=_changed_keys(before, tree),
        )
        return _summary(row)

    @router.post("/channels/{state_id}/reset")
    async def reset_channel(state_id: int, scope: PluginRouteScope = Depends(_scope)) -> dict[str, Any]:
        from .main import reset_channel_state

        row = await _channel_row(scope, state_id)
        # 重置的是这一行（它自己的人设），不是频道当前人设的那一行
        if not await reset_channel_state(row, editor=_editor(scope), source="reset", keep=_keep(scope)):
            raise NotFoundError(resource="频道状态或模板")
        return _summary(await _channel_row(scope, state_id))

    @router.post("/channels/{state_id}/resume")
    async def resume_channel(state_id: int, scope: PluginRouteScope = Depends(_scope)) -> dict[str, Any]:
        row = await _channel_row(scope, state_id)
        await store.update_channel_fields(row, paused=False, consecutive_failures=0)
        await _audit(
            kind="channel",
            target_id=row.id,
            scope=scope,
            source="admin",
            data=None,
            chat_key=row.chat_key,
            ops=[{"op": "resume", "path": ""}],
        )
        return _summary(row)

    @router.get("/channels/{state_id}/snapshots/{snapshot_id}")
    async def snapshot_detail(
        state_id: int, snapshot_id: int, scope: PluginRouteScope = Depends(_scope)
    ) -> dict[str, Any]:
        row = await _channel_row(scope, state_id)
        snapshot = await DBStateSnapshot.get_or_none(id=snapshot_id, target_kind="channel", target_id=row.id)
        if snapshot is None:
            raise NotFoundError(resource="快照")
        return {**_snapshot_summary(snapshot), "data": snapshot.data}

    @router.post("/channels/{state_id}/rollback/{snapshot_id}")
    async def rollback_channel(
        state_id: int,
        snapshot_id: int,
        version: int = Body(..., embed=True),
        scope: PluginRouteScope = Depends(_scope),
    ) -> dict[str, Any]:
        row = await _channel_row(scope, state_id)
        snapshot = await DBStateSnapshot.get_or_none(id=snapshot_id, target_kind="channel", target_id=row.id)
        if snapshot is None or snapshot.data is None:
            raise NotFoundError(resource="可回滚的快照")
        if row.version != version:
            raise ConcurrentWriteConflictError(operation="回滚频道状态")
        tree = _check_tree(snapshot.data)
        if not await store.commit_channel(row, tree):
            raise ConcurrentWriteConflictError(operation="回滚频道状态")
        await _audit(
            kind="channel",
            target_id=row.id,
            scope=scope,
            source="rollback",
            data=tree,
            chat_key=row.chat_key,
            ops=[{"op": "rollback", "path": f"#{snapshot_id}"}],
        )
        return _summary(row)

    @router.get("/people")
    async def list_people(
        preset_id: Optional[int] = Query(None),
        uid: str = Query("", max_length=256),
        page: int = Query(1, ge=1),
        size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        scope: PluginRouteScope = Depends(_scope),
    ) -> dict[str, Any]:
        """个人档案列表（不带值）：这个实例自成一域的档案；平台方另外看得到这个实例所在实例组的档案"""
        own = Q(adapter_key=scope.adapter_key, instance_key=scope.instance_key, domain_group_id=0)
        group = await _group_domain(scope) if scope.is_platform else 0
        query = DBStatePerson.filter(own | Q(domain_group_id=group) if group else own)
        if preset_id is not None:
            query = query.filter(preset_id=preset_id)
        if uid.strip():
            query = query.filter(platform_userid=uid.strip())
        offset, limit = _paging(page, size)
        total = await query.count()
        rows = await query.order_by("-update_time", "-id").offset(offset).limit(limit)
        keys = {
            str(row.id): {
                "adapter_key": row.adapter_key,
                "instance_key": row.instance_key,
                "domain_group_id": row.domain_group_id,
                "platform_userid": row.platform_userid,
                "preset_id": row.preset_id,
            }
            for row in rows
        }
        nicknames = {
            key: str(record.nickname or "") for key, record in (await store.rows_by_person(DBAffection, keys)).items()
        }
        return {
            "items": [_person_summary(row, nicknames.get(str(row.id), "")) for row in rows],
            "total": total,
            "page": page,
            "size": limit,
        }

    @router.get("/people/lookup")
    async def lookup_person(
        preset_id: int = Query(...),
        uid: str = Query(..., min_length=1, max_length=256),
        domain_group_id: int = Query(0, ge=0),
        scope: PluginRouteScope = Depends(_scope),
    ) -> dict[str, Any]:
        """按好感的键找档案（好感详情跳过来时用）；找不到或不归这个作用域都是 404"""
        if domain_group_id:
            row = await DBStatePerson.get_or_none(
                domain_group_id=domain_group_id, platform_userid=uid, preset_id=preset_id
            )
        else:
            row = await DBStatePerson.get_or_none(
                adapter_key=scope.adapter_key,
                instance_key=scope.instance_key,
                domain_group_id=0,
                platform_userid=uid,
                preset_id=preset_id,
            )
        if row is None:
            raise NotFoundError(resource="个人档案")
        checked = await _person_row(scope, int(row.id))
        return {"id": checked.id}

    @router.get("/people/{person_id}")
    async def person_detail(person_id: int, scope: PluginRouteScope = Depends(_scope)) -> dict[str, Any]:
        row = await _person_row(scope, person_id)
        snapshots = await DBStateSnapshot.filter(target_kind="person", target_id=row.id).order_by("-id").limit(70)
        return {
            **_person_summary(row),
            "data": row.data,
            "snapshots": [_snapshot_summary(item) for item in snapshots],
        }

    @router.put("/people/{person_id}")
    async def edit_person(
        person_id: int,
        data: Any = Body(..., embed=True),
        version: int = Body(..., embed=True),
        scope: PluginRouteScope = Depends(_scope),
    ) -> dict[str, Any]:
        row = await _person_row(scope, person_id)
        tree = _check_tree(data)
        if row.version != version:
            raise ConcurrentWriteConflictError(operation="修改个人档案")
        before = dict(row.data or {})
        if not await store.commit_person(row, tree):
            raise ConcurrentWriteConflictError(operation="修改个人档案")
        await _audit(
            kind="person",
            target_id=row.id,
            scope=scope,
            source="admin",
            data=tree,
            ops=_changed_keys(before, tree),
            domain=(str(row.adapter_key or ""), str(row.instance_key or "")),
        )
        return {"id": row.id, "version": row.version}

    @router.get("/people/{person_id}/snapshots/{snapshot_id}")
    async def person_snapshot_detail(
        person_id: int, snapshot_id: int, scope: PluginRouteScope = Depends(_scope)
    ) -> dict[str, Any]:
        row = await _person_row(scope, person_id)
        snapshot = await DBStateSnapshot.get_or_none(id=snapshot_id, target_kind="person", target_id=row.id)
        if snapshot is None:
            raise NotFoundError(resource="快照")
        return {**_snapshot_summary(snapshot), "data": snapshot.data}

    @router.post("/people/{person_id}/rollback/{snapshot_id}")
    async def rollback_person(
        person_id: int,
        snapshot_id: int,
        version: int = Body(..., embed=True),
        scope: PluginRouteScope = Depends(_scope),
    ) -> dict[str, Any]:
        row = await _person_row(scope, person_id)
        snapshot = await DBStateSnapshot.get_or_none(id=snapshot_id, target_kind="person", target_id=row.id)
        if snapshot is None or snapshot.data is None:
            raise NotFoundError(resource="可回滚的快照")
        if row.version != version:
            raise ConcurrentWriteConflictError(operation="回滚个人档案")
        tree = _check_tree(snapshot.data)
        if not await store.commit_person(row, tree):
            raise ConcurrentWriteConflictError(operation="回滚个人档案")
        await _audit(
            kind="person",
            target_id=row.id,
            scope=scope,
            source="rollback",
            data=tree,
            ops=[{"op": "rollback", "path": f"#{snapshot_id}"}],
            domain=(str(row.adapter_key or ""), str(row.instance_key or "")),
        )
        return {"id": row.id, "version": row.version}

    @router.post("/people/{person_id}/reset")
    async def reset_person(person_id: int, scope: PluginRouteScope = Depends(_scope)) -> dict[str, Any]:
        row = await _person_row(scope, person_id)
        template_row, template = await store.load_template(int(row.preset_id))
        if template is None or template_row is None:
            raise NotFoundError(resource="状态模板")
        _channel_part, person_part = store.split_initial(template, dict(template_row.initial or {}))
        if not await store.commit_person(row, person_part):
            raise ConcurrentWriteConflictError(operation="重置个人档案")
        await _audit(
            kind="person",
            target_id=row.id,
            scope=scope,
            source="reset",
            data=person_part,
            ops=[{"op": "reset", "path": ""}],
            domain=(str(row.adapter_key or ""), str(row.instance_key or "")),
        )
        return {"id": row.id, "version": row.version}

    @router.get("/templates")
    async def list_templates(
        q: str = Query("", max_length=100), scope: PluginRouteScope = Depends(_scope)
    ) -> dict[str, Any]:
        """这个实例能用的人设（自己的与全局的），各带有没有模板、来源、版本、能不能改"""
        query = DBPreset.filter(owner_instance_key__in=[scope.instance_key, GLOBAL_PRESET_OWNER])
        if q.strip():
            query = query.filter(name__icontains=q.strip())
        presets = await query.order_by("id").limit(500)
        rows = {
            row.preset_id: row for row in await DBStateTemplate.filter(preset_id__in=[preset.id for preset in presets])
        }
        items = []
        for preset in presets:
            row = rows.get(preset.id)
            owner = str(preset.owner_instance_key or GLOBAL_PRESET_OWNER)
            fields = row.field_defs.get("fields", []) if row is not None and isinstance(row.field_defs, dict) else []
            items.append(
                {
                    "preset_id": preset.id,
                    "preset_name": preset.name,
                    "global": owner == GLOBAL_PRESET_OWNER,
                    "writable": scope.is_platform or owner == scope.instance_key,
                    "exists": row is not None,
                    "source": row.source if row is not None else None,
                    "version": row.version if row is not None else 0,
                    "fields": len(fields) if isinstance(fields, list) else 0,
                }
            )
        return {"items": items}

    @router.get("/templates/{preset_id}/initvar-entries")
    async def template_initvar_entries(preset_id: int, scope: PluginRouteScope = Depends(_scope)) -> dict[str, Any]:
        """这个人设的书里启用的 `[initvar]` 条目（编号与标题，不带正文），给「按书重推」选用哪一条"""
        preset = await _preset_for(scope, preset_id, write=False)
        entries = await initvar_entries(preset.id)
        return {
            "items": [{"entry_id": entry_id, "title": str(comment)[:200]} for entry_id, comment, _content in entries]
        }

    @router.post("/templates/{preset_id}/builtin")
    async def template_from_builtin(
        preset_id: int,
        kinds: list[str] = Body(..., embed=True),
        version: int = Body(..., embed=True),
        scope: PluginRouteScope = Depends(_scope),
    ) -> dict[str, Any]:
        """套用现成模板（`scene` 场景、`inventory` 物品栏）：与已有模板合并，同名的顶层字段保留原来的"""
        preset = await _preset_for(scope, preset_id, write=True)
        unknown = [kind for kind in kinds if kind not in builtin_templates.BUILTINS]
        if not kinds or unknown:
            raise ValidationError(reason=f"现成模板只有 {', '.join(builtin_templates.BUILTINS)}")
        existing_row, existing = await store.load_template(preset.id)
        if existing_row is not None and existing is None:
            raise ValidationError(reason="现有模板读不出来，先修好或重推再套用")
        merged = builtin_templates.merge(existing, dict(existing_row.initial or {}) if existing_row else {}, kinds)
        if not merged.added:
            return {
                "preset_id": preset.id,
                "version": existing_row.version if existing_row else 0,
                "added": [],
                "skipped": list(merged.skipped),
            }
        row = await store.save_template(
            preset.id,
            merged.template,
            merged.initial,
            source="manual" if existing_row is not None else "builtin",
            editor=_editor(scope),
            expected_version=version,
        )
        if row is None:
            raise ConcurrentWriteConflictError(operation="套用现成模板")
        await _audit(
            kind="template",
            target_id=row.id,
            scope=scope,
            source="admin",
            data=None,
            ops=[{"op": "template", "path": f"builtin:{kind}"} for kind in merged.added],
        )
        return {
            "preset_id": preset.id,
            "version": row.version,
            "added": list(merged.added),
            "skipped": list(merged.skipped),
        }

    @router.get("/audit")
    async def list_audit(
        kind: Optional[str] = Query(None, pattern="^(channel|person|template)$"),
        source: Optional[str] = Query(None, pattern="^(admin|rollback|reset)$"),
        page: int = Query(1, ge=1),
        size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        scope: PluginRouteScope = Depends(_scope),
    ) -> dict[str, Any]:
        """这个实例的审计行（管理员改、回滚、重置、改模板）：谁、什么时候、改了哪几个路径；不带值

        平台方另外看得到这个实例所在实例组的个人档案的审计行（那些行不属于任何一个实例，实例为空串）。
        """
        visible = Q(adapter_key=scope.adapter_key, instance_key=scope.instance_key)
        group = await _group_domain(scope) if scope.is_platform else 0
        if group:
            visible |= Q(
                target_kind="person",
                adapter_key="",
                instance_key="",
                target_id__in=Subquery(DBStatePerson.filter(domain_group_id=group).values("id")),
            )
        query = DBStateSnapshot.filter(visible, source__in=store.AUDIT_SOURCES)
        if kind:
            query = query.filter(target_kind=kind)
        if source:
            query = query.filter(source=source)
        offset, limit = _paging(page, size)
        total = await query.count()
        rows = await query.order_by("-id").offset(offset).limit(limit)
        return {"items": [_audit_summary(row) for row in rows], "total": total, "page": page, "size": limit}

    @router.get("/templates/{preset_id}")
    async def template_detail(preset_id: int, scope: PluginRouteScope = Depends(_scope)) -> dict[str, Any]:
        preset = await _preset_for(scope, preset_id, write=False)
        row, _template = await store.load_template(preset.id)
        writable = scope.is_platform or str(preset.owner_instance_key or GLOBAL_PRESET_OWNER) == scope.instance_key
        if row is None:
            return {"preset_id": preset.id, "exists": False, "writable": writable}
        return {
            "preset_id": preset.id,
            "exists": True,
            "writable": writable,
            "field_defs": row.field_defs,
            "initial": row.initial,
            "source": row.source,
            "version": row.version,
            "edited_by": row.edited_by,
        }

    @router.put("/templates/{preset_id}")
    async def edit_template(
        preset_id: int,
        field_defs: Any = Body(..., embed=True),
        initial: Any = Body(..., embed=True),
        version: int = Body(..., embed=True),
        scope: PluginRouteScope = Depends(_scope),
    ) -> dict[str, Any]:
        """改模板：带读到时的版本号（还没有模板时是 0），版本变了返回 409"""
        preset = await _preset_for(scope, preset_id, write=True)
        try:
            template = Template.from_json(field_defs)
        except TemplateError as error:
            raise ValidationError(reason=str(error)) from error
        tree = _check_tree(initial)
        row = await store.save_template(
            preset.id, template, tree, source="manual", editor=_editor(scope), expected_version=version
        )
        if row is None:
            raise ConcurrentWriteConflictError(operation="修改状态模板")
        await _audit(
            kind="template",
            target_id=row.id,
            scope=scope,
            source="admin",
            data=None,
            ops=[{"op": "template", "path": ""}],
        )
        return {"preset_id": preset.id, "version": row.version}

    @router.post("/templates/{preset_id}/from-initvar")
    async def template_from_initvar(
        preset_id: int,
        version: int = Body(..., embed=True),
        entry_id: Optional[int] = Body(None, embed=True),
        scope: PluginRouteScope = Depends(_scope),
    ) -> dict[str, Any]:
        """按书里的 `[initvar]` 重新推导模板（`entry_id` 给了就只用那一条）：私密词只用平台层面的词表
        （模板可能被别的实例共用）"""
        from .updater import platform_words

        preset = await _preset_for(scope, preset_id, write=True)
        derived = await derive_from_book(preset.id, private_words=platform_words(), entry_id=entry_id)
        if derived is None:
            raise NotFoundError(resource="可用的 [initvar] 条目")
        template, initial, source_entry = derived
        row = await store.save_template(
            preset.id,
            template,
            initial,
            source="initvar",
            source_entry_id=source_entry,
            editor=_editor(scope),
            expected_version=version,
        )
        if row is None:
            raise ConcurrentWriteConflictError(operation="重新推导状态模板")
        await _audit(
            kind="template",
            target_id=row.id,
            scope=scope,
            source="admin",
            data=None,
            ops=[{"op": "template", "path": "initvar"}],
        )
        return {"preset_id": preset.id, "version": row.version, "fields": len(template.fields)}

    return router


plugin.mount_router()(create_router)


__all__ = ["create_router"]
