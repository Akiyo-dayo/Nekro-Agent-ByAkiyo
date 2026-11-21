"""频道滚动摘要的读写

摘要只按「频道 + 人设」归属，不受工作区与跨频道召回开关影响：它描述的就是这个频道
自己的近况。每次沉淀把上一版摘要带给模型，让模型在旧版本上更新，而不是从零重写。

并发前提：调度器按频道排他，同一频道的两次沉淀不会同时发生，因此这里不额外加锁；
即使偶然出现并发写入，唯一约束会挡住重复版本号，而不会产生两行同版本的数据。
"""

from __future__ import annotations

from datetime import datetime, timezone

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_mem_channel_summary import DBMemChannelSummary
from nekro_agent.models.db_mem_channel_summary_state import DBMemChannelSummaryState
from nekro_agent.schemas.errors import ConflictError, ValidationError
from nekro_agent.schemas.memory_admin import SummaryListEntry
from nekro_agent.services.memory.instance_scope import AdminFilter, AdminMemoryScope
from nekro_agent.services.memory.scope_condition import MemTable
from nekro_agent.tools.untrusted_content import sanitize_block_text

logger = get_sub_logger("memory.channel_summary")

# 每个（频道, 人设）保留的版本数。多留几版是为了让写坏的摘要能被回看，而不是只剩一行。
SUMMARY_KEEP_VERSIONS = 5


class _Unset:
    """「调用方没传这个参数」的哨兵。

    不能用 ``None`` 表示没传：``None`` 在这里是一个有意义的值（「当时还没有摘要」），
    两者混淆会让自动保存在一个空频道上永远被当成版本冲突。
    """

    __slots__ = ()


UNSET = _Unset()


async def get_latest_summary(chat_key: str, preset_id: int) -> DBMemChannelSummary | None:
    """取这个频道当前人设下最新的一版摘要"""
    return await DBMemChannelSummary.filter(chat_key=chat_key, preset_id=preset_id).order_by("-version").first()


async def get_versions(chat_key: str, preset_id: int) -> list[DBMemChannelSummary]:
    """取这个频道当前人设下的全部保留版本，新到旧。"""
    return await DBMemChannelSummary.filter(chat_key=chat_key, preset_id=preset_id).order_by("-version")


async def list_summaries(
    scope: AdminMemoryScope,
    f: AdminFilter,
    *,
    page: int,
    page_size: int,
) -> tuple[int, list[SummaryListEntry]]:
    """按管理端范围列出摘要，每个（频道, 人设）一行。

    可见范围只经 ``scope.apply``；「最新版 / 版本数 / 是否暂停」要在内存里按组聚合，
    因为 Tortoise 的分组查询参数在不同数据库上的行为不一致，而每组最多只保留
    :data:`SUMMARY_KEEP_VERSIONS` 行，全部取回来再分页的代价可以接受。

    分页按每组最新版的创建时间倒序：一页里出现的是最近被更新过的那些频道摘要。
    """
    rows = await scope.apply(DBMemChannelSummary.all(), MemTable.SUMMARY, f).order_by("-create_time", "-id")
    grouped: dict[tuple[str, int], list[DBMemChannelSummary]] = {}
    for row in rows:
        grouped.setdefault((row.chat_key, row.preset_id), []).append(row)

    paused_pairs: set[tuple[str, int]] = set()
    chat_keys = {chat_key for chat_key, _ in grouped}
    if chat_keys:
        states = await DBMemChannelSummaryState.filter(chat_key__in=sorted(chat_keys), paused=True)
        paused_pairs = {(state.chat_key, state.preset_id) for state in states}

    entries: list[SummaryListEntry] = []
    for (chat_key, preset_id), versions in grouped.items():
        latest = max(versions, key=lambda item: item.version)
        entries.append(
            SummaryListEntry(
                chat_key=chat_key,
                preset_id=preset_id,
                adapter_key=latest.adapter_key,
                instance_key=latest.instance_key,
                id=latest.id,
                version=latest.version,
                content=latest.content,
                source=latest.source,
                create_time=latest.create_time,
                version_count=len(versions),
                paused=(chat_key, preset_id) in paused_pairs,
            ),
        )

    entries.sort(key=lambda entry: (_created_timestamp(entry), entry.id), reverse=True)
    total = len(entries)
    resolved_page = max(1, int(page))
    resolved_size = max(1, min(int(page_size), 100))
    start = (resolved_page - 1) * resolved_size
    return total, entries[start : start + resolved_size]


async def save_summary(
    *,
    chat_key: str,
    adapter_key: str,
    instance_key: str,
    preset_id: int,
    content: str,
    last_message_db_id: int,
    source: str = "auto",
    source_version: int | None = None,
    edited_by: str = "",
    base_version: int | None | _Unset = UNSET,
) -> DBMemChannelSummary | None:
    """写入一版摘要；内容与最新一版完全相同时不写，返回 ``None``

    传了 ``base_version`` 时先比对当前最新版号：不是它说明这期间有人手动编辑或回滚过，
    这次自动结果基于旧内容生成，写进去会把手动修改盖掉，因此丢弃并返回 ``None``。

    同频道并发写的概率很低（调度器按频道排他），这里不加锁。写入后只保留最近
    :data:`SUMMARY_KEEP_VERSIONS` 版，更旧的行删掉。
    """
    latest = await get_latest_summary(chat_key, preset_id)
    if not isinstance(base_version, _Unset):
        current_version = latest.version if latest is not None else None
        if current_version != base_version:
            logger.info(
                f"摘要在这期间被手动改过，丢弃这次自动结果: chat_key={chat_key}, "
                f"preset_id={preset_id}, base_version={base_version}, current_version={current_version}",
            )
            return None
    if latest is not None and latest.content == content:
        return None

    version = latest.version + 1 if latest is not None else 1
    summary = await DBMemChannelSummary.create(
        chat_key=chat_key,
        adapter_key=adapter_key,
        instance_key=instance_key,
        preset_id=preset_id,
        version=version,
        content=content,
        last_message_db_id=last_message_db_id,
        source=source,
        source_version=source_version,
        edited_by=edited_by,
    )
    await _trim_versions(chat_key, preset_id)
    return summary


async def edit_summary(
    *,
    chat_key: str,
    preset_id: int,
    adapter_key: str,
    instance_key: str,
    content: str,
    base_version: int,
    editor: str,
) -> DBMemChannelSummary:
    """手动编辑摘要，存成新的一版。

    正文与自动保存走同一道清洗与截断，避免手动路径成为绕开字数上限的入口；
    清洗后为空说明用户只提交了不可见字符，按参数错误处理。
    """
    from nekro_agent.core.config import config

    cleaned = sanitize_block_text(content, int(config.MEMORY_CHANNEL_SUMMARY_MAX_CHARS))
    if not cleaned:
        raise ValidationError(reason="摘要内容清洗后为空")

    latest = await get_latest_summary(chat_key, preset_id)
    current_version = latest.version if latest is not None else None
    if current_version != base_version:
        raise ConflictError(
            resource=f"摘要版本（当前 {current_version}，提交基于 {base_version}，请重新载入）",
            detail=f"chat_key={chat_key}, preset_id={preset_id}, base_version={base_version}, current={current_version}",
        )
    if latest is not None and latest.content == cleaned:
        return latest

    version = latest.version + 1 if latest is not None else 1
    summary = await DBMemChannelSummary.create(
        chat_key=chat_key,
        adapter_key=adapter_key,
        instance_key=instance_key,
        preset_id=preset_id,
        version=version,
        content=cleaned,
        last_message_db_id=latest.last_message_db_id if latest is not None else 0,
        source="manual",
        source_version=None,
        edited_by=editor,
    )
    await _trim_versions(chat_key, preset_id)
    return summary


async def rollback_summary(
    *,
    chat_key: str,
    preset_id: int,
    to_version: int | None,
    editor: str,
) -> DBMemChannelSummary:
    """把旧版内容写成新的一版；默认回滚到上一版。

    回滚本身也产生新版本，因此历史只往前长，回滚之后还能再回滚回来。
    """
    versions = await get_versions(chat_key, preset_id)
    if to_version is None:
        if len(versions) < 2:
            raise ValidationError(reason="没有可回滚的上一版摘要")
        target = versions[1]
    else:
        target = next((row for row in versions if row.version == to_version), None)
        if target is None:
            raise ValidationError(reason=f"要回滚到的摘要版本不存在: {to_version}")

    latest = versions[0] if versions else None
    new_version = latest.version + 1 if latest is not None else 1
    # 新版本沿用最新一版的归属：它是当前频道最近一次写入时记下的实例对；目标旧版可能
    # 属于迁移前的实例。内容取自目标版，归属取最新版。
    attribution = latest if latest is not None else target
    summary = await DBMemChannelSummary.create(
        chat_key=chat_key,
        adapter_key=attribution.adapter_key,
        instance_key=attribution.instance_key,
        preset_id=preset_id,
        version=new_version,
        content=target.content,
        last_message_db_id=latest.last_message_db_id if latest is not None else 0,
        source="rollback",
        source_version=target.version,
        edited_by=editor,
    )
    await _trim_versions(chat_key, preset_id)
    return summary


async def set_paused(
    *,
    chat_key: str,
    preset_id: int,
    adapter_key: str,
    instance_key: str,
    paused: bool,
    editor: str,
) -> DBMemChannelSummaryState:
    """打开或关闭某个（频道, 人设）的自动更新。没有行就建，有就改。"""
    state = await DBMemChannelSummaryState.get_or_none(chat_key=chat_key, preset_id=preset_id)
    paused_at = datetime.now(timezone.utc) if paused else None
    paused_by = editor if paused else ""
    if state is None:
        return await DBMemChannelSummaryState.create(
            chat_key=chat_key,
            preset_id=preset_id,
            adapter_key=adapter_key,
            instance_key=instance_key,
            paused=paused,
            paused_by=paused_by,
            paused_at=paused_at,
        )
    state.adapter_key = adapter_key
    state.instance_key = instance_key
    state.paused = paused
    state.paused_by = paused_by
    state.paused_at = paused_at
    await state.save()
    return state


async def is_paused(chat_key: str, preset_id: int) -> bool:
    """这个（频道, 人设）是否暂停了自动更新；没有状态行就是没暂停。"""
    state = await get_summary_state(chat_key, preset_id)
    return state is not None and state.paused


async def get_summary_state(chat_key: str, preset_id: int) -> DBMemChannelSummaryState | None:
    """取暂停状态行；没有行表示从未暂停过。

    管理端要显示「谁在什么时候暂停的」，因此除了 :func:`is_paused` 的布尔值，还需要
    整行。读取入口收在这里，调用方不必自己拼 `chat_key` / `preset_id` 条件。
    """
    return await DBMemChannelSummaryState.get_or_none(chat_key=chat_key, preset_id=preset_id)


def _created_timestamp(entry: SummaryListEntry) -> float:
    """分页排序用的时间戳；`None` 排到最后。

    不用 `datetime` 直接比较：SQLite 取回的时间可能不带时区，和带时区的值比较会抛
    ``TypeError``，而这里只需要一个稳定的先后顺序。
    """
    return entry.create_time.timestamp() if entry.create_time is not None else 0.0


async def _trim_versions(chat_key: str, preset_id: int) -> None:
    """只保留最近 :data:`SUMMARY_KEEP_VERSIONS` 版，更旧的行删掉。"""
    kept_versions = list(
        await DBMemChannelSummary.filter(chat_key=chat_key, preset_id=preset_id)
        .order_by("-version")
        .limit(SUMMARY_KEEP_VERSIONS)
        .values_list("version", flat=True),
    )
    if len(kept_versions) >= SUMMARY_KEEP_VERSIONS:
        oldest_kept = int(kept_versions[-1])
        await DBMemChannelSummary.filter(
            chat_key=chat_key,
            preset_id=preset_id,
            version__lt=oldest_kept,
        ).delete()
