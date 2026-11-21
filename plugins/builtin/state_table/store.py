"""状态的读写：模板、频道状态、个人档案、快照

- 频道状态按 `(chat_key, preset_id)`，个人档案按好感记录的键（`AffectionKey.as_columns()`）；
- 提交用乐观锁：`version` 变了（管理员刚改过）就不覆盖，返回 False；
- 每次提交与每次更新尝试都写一行快照（没提交的不带整棵树），按目标裁剪保留份数；
- 日志里不打状态值。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Optional

from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_state_channel import DBStateChannel
from nekro_agent.models.db_state_person import DBStatePerson
from nekro_agent.models.db_state_snapshot import DBStateSnapshot
from nekro_agent.models.db_state_template import DBStateTemplate

from . import tree as state_tree
from .schema import Template, TemplateError

logger = get_sub_logger("state_table.store")

# 没提交的更新记录（解析失败、全部被拒、冲突……）每个目标保留这么多行。
KEEP_RECORDS = 50
# 审计行（管理员改、回滚、重置）每个目标保留这么多行，与更新记录分开算。
KEEP_AUDIT = 200
AUDIT_SOURCES = ("admin", "rollback", "reset")


def split_initial(template: Template, initial: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """初值树 → `(频道部分, 个人部分)`：个人范围的顶层键归个人档案"""
    roots = set(template.person_roots())
    channel = {key: value for key, value in initial.items() if key not in roots}
    person = {key: value for key, value in initial.items() if key in roots}
    return state_tree.copy_tree(channel), state_tree.copy_tree(person)


async def load_template(preset_id: int) -> tuple[Optional[DBStateTemplate], Optional[Template]]:
    row = await DBStateTemplate.get_or_none(preset_id=preset_id)
    if row is None:
        return None, None
    try:
        return row, Template.from_json(row.field_defs)
    except TemplateError as error:
        # 原因里可能带卡片作者起的字段名：日志只记类别
        logger.warning(f"状态模板不合法，按没有模板处理: preset_id={preset_id}, 类别={type(error).__name__}")
        return row, None


async def save_template(
    preset_id: int,
    template: Template,
    initial: Mapping[str, Any],
    *,
    source: str,
    source_entry_id: Optional[int] = None,
    editor: str = "",
    expected_version: Optional[int] = None,
) -> Optional[DBStateTemplate]:
    """新建或覆盖一个人设的模板（版本加一）

    `expected_version` 给了就按版本提交：已有的行版本对不上、或者以为没有（0）却已经有了，都返回 None、
    什么都不写。不给（自动推导）时：已经有了就不覆盖，返回已有的那一行。
    """
    row = await DBStateTemplate.get_or_none(preset_id=preset_id)
    if row is None:
        if expected_version not in (None, 0):
            return None
        try:
            return await DBStateTemplate.create(
                preset_id=preset_id,
                field_defs=template.to_json(),
                initial=state_tree.copy_tree(dict(initial)),
                source=source,
                source_entry_id=source_entry_id,
                edited_by=editor,
            )
        except IntegrityError:
            if expected_version is not None:
                return None
            return await DBStateTemplate.get(preset_id=preset_id)
    if expected_version is None:
        return row
    updated = await DBStateTemplate.filter(id=row.id, version=expected_version).update(
        field_defs=template.to_json(),
        initial=state_tree.copy_tree(dict(initial)),
        source=source,
        source_entry_id=source_entry_id,
        version=expected_version + 1,
        edited_by=editor,
    )
    if not updated:
        return None
    await row.refresh_from_db()
    return row


async def get_channel(chat_key: str, preset_id: int) -> Optional[DBStateChannel]:
    return await DBStateChannel.get_or_none(chat_key=chat_key, preset_id=preset_id)


async def create_channel(
    *,
    chat_key: str,
    preset_id: int,
    adapter_key: str,
    instance_key: str,
    template_version: int,
    data: Mapping[str, Any],
    last_message_db_id: int = 0,
) -> DBStateChannel:
    """建频道状态；并发建行撞唯一键时取已有的那一行"""
    try:
        return await DBStateChannel.create(
            chat_key=chat_key,
            preset_id=preset_id,
            adapter_key=adapter_key,
            instance_key=instance_key,
            template_version=template_version,
            data=state_tree.copy_tree(dict(data)),
            last_message_db_id=last_message_db_id,
        )
    except IntegrityError:
        return await DBStateChannel.get(chat_key=chat_key, preset_id=preset_id)


async def commit_channel(row: DBStateChannel, data: Mapping[str, Any], **fields: Any) -> bool:
    """按版本提交；版本对不上（别处刚改过）返回 False，什么都不写"""
    updated = await DBStateChannel.filter(id=row.id, version=row.version).update(
        data=state_tree.copy_tree(dict(data)),
        version=int(row.version) + 1,
        **fields,
    )
    if not updated:
        return False
    row.data = state_tree.copy_tree(dict(data))
    row.version = int(row.version) + 1
    for key, value in fields.items():
        setattr(row, key, value)
    return True


async def update_channel_fields(row: DBStateChannel, **fields: Any) -> None:
    """只改计数类的列（不动状态树、不加版本）"""
    if not fields:
        return
    await DBStateChannel.filter(id=row.id).update(**fields)
    for key, value in fields.items():
        setattr(row, key, value)


async def rows_by_person(model: Any, keys: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """`{人的标识: 键列}` → 按键列取 `model` 的行（没有的不在结果里）

    键列里除了 `platform_userid` 都相同的人合成一次查询（`platform_userid__in`），不逐人查。
    """
    groups: dict[tuple[tuple[str, Any], ...], dict[str, list[str]]] = {}
    for uid, columns in keys.items():
        common = tuple(sorted((name, value) for name, value in columns.items() if name != "platform_userid"))
        groups.setdefault(common, {}).setdefault(str(columns.get("platform_userid", "")), []).append(uid)
    found: dict[str, Any] = {}
    for common, by_platform_uid in groups.items():
        rows = await model.filter(**dict(common), platform_userid__in=list(by_platform_uid))
        for row in rows:
            for uid in by_platform_uid.get(str(row.platform_userid), []):
                found[uid] = row
    return found


async def load_people(keys: Mapping[str, Mapping[str, Any]]) -> dict[str, DBStatePerson]:
    """`{人的标识: 键列}` → 已有的个人档案（没有的不在结果里）"""
    return await rows_by_person(DBStatePerson, keys)


async def ensure_person(columns: Mapping[str, Any], initial: Mapping[str, Any]) -> DBStatePerson:
    row = await DBStatePerson.get_or_none(**columns)
    if row is not None:
        return row
    try:
        # 包一层（在提交的事务里就是保存点）：撞了唯一键只退回这一句，外面的事务还能接着用
        async with in_transaction():
            return await DBStatePerson.create(**columns, data=state_tree.copy_tree(dict(initial)))
    except IntegrityError:
        return await DBStatePerson.get(**columns)


async def commit_person(row: DBStatePerson, data: Mapping[str, Any]) -> bool:
    updated = await DBStatePerson.filter(id=row.id, version=row.version).update(
        data=state_tree.copy_tree(dict(data)),
        version=int(row.version) + 1,
    )
    if not updated:
        return False
    row.data = state_tree.copy_tree(dict(data))
    row.version = int(row.version) + 1
    return True


async def write_snapshot(
    *,
    target_kind: str,
    target_id: int,
    chat_key: str,
    adapter_key: str,
    instance_key: str,
    source: str,
    result: str,
    data: Optional[Mapping[str, Any]] = None,
    applied: Sequence[Mapping[str, str]] = (),
    rejected: Sequence[Mapping[str, str]] = (),
    model_group: str = "",
    editor: str = "",
    keep: int = 20,
) -> DBStateSnapshot:
    """写一行快照或更新记录，再按目标裁剪：带树的留 `keep` 份，不带树的留 :data:`KEEP_RECORDS` 行"""
    row = await DBStateSnapshot.create(
        target_kind=target_kind,
        target_id=target_id,
        chat_key=chat_key,
        adapter_key=adapter_key,
        instance_key=instance_key,
        source=source,
        result=result,
        data=state_tree.copy_tree(dict(data)) if data is not None else None,
        ops_applied=[{"op": item.get("op", ""), "path": item.get("path", "")} for item in applied],
        ops_rejected=[
            {"op": item.get("op", ""), "path": item.get("path", ""), "reason": item.get("reason", "")}
            for item in rejected
        ],
        model_group=model_group,
        editor=editor,
    )
    await _prune(target_kind, target_id, keep=max(1, keep))
    return row


async def _prune(target_kind: str, target_id: int, *, keep: int) -> None:
    """按目标裁剪：审计行（管理员改、回滚、重置）单独留 :data:`KEEP_AUDIT` 行，不被更新记录挤掉；
    其余带树的留 `keep` 份、不带树的留 :data:`KEEP_RECORDS` 行"""
    # 每个目标的行数很少（几百行以内）：取出编号在这里切，不用 OFFSET（SQLite 不接受没有 LIMIT 的 OFFSET）。
    base = DBStateSnapshot.filter(target_kind=target_kind, target_id=target_id)
    audit = await base.filter(source__in=AUDIT_SOURCES).order_by("-id").values_list("id", flat=True)
    others = base.exclude(source__in=AUDIT_SOURCES)
    with_data = await others.filter(data__isnull=False).order_by("-id").values_list("id", flat=True)
    without = await others.filter(data__isnull=True).order_by("-id").values_list("id", flat=True)
    stale = [*list(audit)[KEEP_AUDIT:], *list(with_data)[keep:], *list(without)[KEEP_RECORDS:]]
    if stale:
        await DBStateSnapshot.filter(id__in=stale).delete()


def now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "AUDIT_SOURCES",
    "KEEP_AUDIT",
    "KEEP_RECORDS",
    "rows_by_person",
    "commit_channel",
    "commit_person",
    "create_channel",
    "ensure_person",
    "get_channel",
    "load_people",
    "load_template",
    "now",
    "save_template",
    "split_initial",
    "update_channel_fields",
    "write_snapshot",
]
