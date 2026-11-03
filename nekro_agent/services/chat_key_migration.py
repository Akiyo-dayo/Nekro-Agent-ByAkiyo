"""chat_key 标识升级的级联迁移与重复频道行的合并修复

适配器多账号化后 `chat_key` 的构成发生了变化（例如 OneBot V11 从 `onebot_v11-group_123456`
变为 `onebot_v11-10001-group_123456`）。`chat_key` 被大量业务表当作外键字符串直接引用，
频道行被「惰性认领」升级到新标识后，这些引用必须同步改写，否则历史消息、执行记录、插件数据、
定时任务与记忆都会变成孤儿数据，表现为机器人升级后完全失忆。

本模块只负责改写引用，不负责决定「哪一行该被认领」——那是 `DBChatChannel.get_or_create` 的职责。

`merge_duplicate_chat_channels` 修的是另一件事：唯一约束补上之前，同一频道的并发首条消息会各自
走完「查不到 -> 新建」，把一个会话写成两行——一行留着人设/工作区绑定，另一行是空壳且恰好接管了流量。
它必须在 aerich 迁移**之前**跑：唯一约束的迁移撞上存量重复行会直接失败，服务连启动都启动不了。

`plan_instance_migration` / `migrate_instance_ownership` 是**显式的**「源实例 → 目标实例」归属迁移。
协议端在管理员建实例之前先连上时会被自动登记成 `ob{账号}`，历史频道认领给它；管理员随后新建
`inst1` 绑同一账号，流量就带着 `inst1` 进来，同一个群于是有两行 `instance_key` 不同的频道。
两行按隔离模型是**合法的两个会话**，所以自动合并绝不可取（那等于替用户决定这段历史归谁），
必须由平台方在总后台点一次、并且先看过预览再执行。
"""

import json
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from tortoise import Tortoise
from tortoise.expressions import Q
from tortoise.functions import Count
from tortoise.transactions import in_transaction

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_affection import DBAffection, DBAffectionEvent
from nekro_agent.models.db_announcement import DBAnnouncementDelivery
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_exec_code import DBExecCode
from nekro_agent.models.db_mem_entity import DBMemEntity
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.models.db_plugin_data import DBPluginData
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_recurring_timer_job import DBRecurringTimerJob
from nekro_agent.models.db_workspace_comm_log import DBWorkspaceCommLog
from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager

logger = get_sub_logger("chat_key_migration")

# 被归档的重复行会以快照形式落在幸存行的 `data` 里，方便事后核对合并是否搬全了字段
ARCHIVED_DUPLICATES_KEY = "archived_duplicate_channels"

# 合并时按「只补空缺、不覆盖」搬运的字段。幸存行上已有的值是更晚的、用户可见的配置，
# 覆盖它等于把用户在故障期间的重新设置又抹掉一次
RESTORABLE_FIELDS: Tuple[str, ...] = ("preset_id", "workspace_id", "channel_name", "channel_type")


async def migrate_chat_key_references(
    old_chat_key: str,
    new_chat_key: str,
    instance_key: str = "",
) -> Dict[str, int]:
    """把所有引用旧 chat_key 的记录迁移到新 chat_key，返回每张表更新的行数

    幂等：迁移完成后旧 chat_key 不再匹配任何行，重复调用只会得到全 0 的结果。

    所有表的改写在同一个事务内完成。调用方已处于事务中时（例如频道认领），这里会退化为
    savepoint，跟随外层事务一起提交或回滚。

    Args:
        old_chat_key: 升级前的频道标识
        new_chat_key: 升级后的频道标识
        instance_key: 认领该频道的账号实例标识，用于回填带实例维度的表

    Returns:
        以数据源为键的更新行数统计。`qdrant_paragraph_payload` 表示向量库中同步了
        instance_key 的点数量，`one_shot_timer` 表示改写的一次性定时器数量。
    """
    if not old_chat_key or old_chat_key == new_chat_key:
        return {}

    # 段落主键需要在改写前取出，之后要按这批 ID 同步 Qdrant payload
    paragraph_ids: List[int] = list(
        await DBMemParagraph.filter(origin_chat_key=old_chat_key).values_list("id", flat=True),
    )

    counts: Dict[str, int] = {}
    async with in_transaction():
        counts["chat_message"] = await DBChatMessage.filter(chat_key=old_chat_key).update(
            chat_key=new_chat_key,
            instance_key=instance_key,
        )
        counts["exec_code"] = await DBExecCode.filter(chat_key=old_chat_key).update(chat_key=new_chat_key)
        counts["plugin_data"] = await DBPluginData.filter(target_chat_key=old_chat_key).update(
            target_chat_key=new_chat_key,
        )
        counts["recurring_timer_job"] = await DBRecurringTimerJob.filter(chat_key=old_chat_key).update(
            chat_key=new_chat_key,
        )
        counts["mem_paragraph"] = await DBMemParagraph.filter(origin_chat_key=old_chat_key).update(
            origin_chat_key=new_chat_key,
            instance_key=instance_key,
        )
        counts["mem_episode"] = await DBMemEpisode.filter(origin_chat_key=old_chat_key).update(
            origin_chat_key=new_chat_key,
            instance_key=instance_key,
        )
        counts["workspace_comm_log"] = await DBWorkspaceCommLog.filter(source_chat_key=old_chat_key).update(
            source_chat_key=new_chat_key,
        )

    counts["qdrant_paragraph_payload"] = await _sync_paragraph_vectors(paragraph_ids, instance_key)
    counts["one_shot_timer"] = await _migrate_one_shot_timers(old_chat_key, new_chat_key)
    return counts


async def _sync_paragraph_vectors(paragraph_ids: List[int], instance_key: str) -> int:
    """把回填后的 instance_key 同步到 Qdrant payload

    段落行改了 instance_key 而向量 payload 没改，记忆检索的实例过滤（`instance_scope`）会把这批
    记忆挡在召回之外，DB 与向量库的口径也会长期不一致。同步失败时不阻断认领流程，但必须留下
    可操作的告警。
    """
    if not paragraph_ids or not instance_key:
        return 0

    updated = await memory_qdrant_manager.set_paragraph_instance_key(paragraph_ids, instance_key)
    if not updated:
        logger.warning(
            f"记忆向量 payload 未能同步 instance_key，共 {len(paragraph_ids)} 条段落受影响，"
            f"请在 Qdrant 恢复后重建记忆向量库: instance_key={instance_key}",
        )
    return updated


async def _migrate_one_shot_timers(old_chat_key: str, new_chat_key: str) -> int:
    """改写一次性定时器持有的 chat_key

    一次性定时器同时存在于 `TimerService` 的内存字典与 `one_shot_timers.json` 里，
    两者都以 chat_key 为键，必须交给服务自身改写后重新落盘。
    """
    # 延迟导入：timer_service 依赖 message_service，模块级导入会与模型层形成导入环
    from nekro_agent.services.timer.timer_service import timer_service

    return await timer_service.migrate_chat_key(old_chat_key, new_chat_key)


class ChannelMergeResult(NamedTuple):
    """一个频道唯一性三元组上的合并结果"""

    adapter_key: str
    instance_key: str
    channel_id: str
    survivor_id: int
    archived_ids: List[int]
    restored_fields: List[str]


async def merge_duplicate_chat_channels() -> List[ChannelMergeResult]:
    """把重复的频道行合并成一行，返回每个三元组的合并结果

    幂等：合并后每个三元组只剩一行，重复调用返回空列表。表结构还没升到带 `instance_key`
    的版本时直接跳过——那种库根本不可能存在本类重复行。
    """
    if not await _chat_channel_schema_ready():
        return []

    seen: Dict[Tuple[str, str, str], int] = {}
    for row in await DBChatChannel.all().values("adapter_key", "instance_key", "channel_id"):
        identity = (str(row["adapter_key"]), str(row["instance_key"]), str(row["channel_id"]))
        seen[identity] = seen.get(identity, 0) + 1

    results: List[ChannelMergeResult] = []
    for identity, count in seen.items():
        if count < 2:
            continue
        result = await merge_channel_duplicates(*identity)
        if result is not None:
            results.append(result)
    if results:
        logger.warning(f"已合并 {len(results)} 个重复频道: {results}")
    return results


async def merge_channel_duplicates(
    adapter_key: str,
    instance_key: str,
    channel_id: str,
) -> Optional[ChannelMergeResult]:
    """把某个频道唯一性三元组下的多行合并成一行

    幸存行选「当前在接管流量的那一行」：先要 `is_active`，同级再取 id 最小的。故障现场里空壳行
    往往正是激活的那一行，用户看到的机器人行为由它决定，因此必须让它继续存在、把绑定补回它身上，
    而不是反过来复活一行被停用的旧行。

    字段搬运只补空缺：幸存行上已有的值可能是用户在故障期间重新设置的，属于更晚的意图。

    Returns:
        合并结果；该三元组本来就只有一行时返回 None
    """
    async with in_transaction():
        rows = (
            await DBChatChannel.filter(adapter_key=adapter_key, instance_key=instance_key, channel_id=channel_id)
            .order_by("id")
            .select_for_update()
        )
        if len(rows) < 2:
            return None

        survivor = min(rows, key=lambda row: (not row.is_active, row.id))
        donors = [row for row in rows if row.id != survivor.id]
        absorbed = await absorb_channel_rows(survivor, donors)

    return ChannelMergeResult(
        adapter_key=adapter_key,
        instance_key=instance_key,
        channel_id=channel_id,
        survivor_id=survivor.id,
        archived_ids=[donor.id for donor in donors],
        restored_fields=absorbed.restored_fields,
    )


class ChannelAbsorbResult(NamedTuple):
    """把若干旧频道行并进一行之后的结果"""

    restored_fields: List[str]
    reference_counts: Dict[str, int]


async def absorb_channel_rows(survivor: DBChatChannel, donors: List[DBChatChannel]) -> ChannelAbsorbResult:
    """把若干旧频道行的绑定、`data` 与引用并入幸存行，并删掉旧行

    调用方负责开事务、选出幸存行；本函数只负责「怎么并」，因此同城重复行合并与跨实例归属迁移
    共用同一套搬运规则，不会出现两处逐渐分叉的实现。

    字段搬运只补空缺：幸存行上已有的值属于更晚的用户意图，覆盖它等于把用户后来的设置抹掉一次。
    被删掉的旧行以快照形式落在幸存行的 `data[ARCHIVED_DUPLICATES_KEY]` 里，事后可核对。
    """
    survivor_data = _load_channel_data(survivor)
    archived = list(survivor_data.get(ARCHIVED_DUPLICATES_KEY) or [])
    restored_fields: List[str] = []
    reference_counts: Dict[str, int] = {}

    for donor in donors:
        for field in RESTORABLE_FIELDS:
            if getattr(survivor, field, None) in (None, "") and getattr(donor, field, None) not in (None, ""):
                setattr(survivor, field, getattr(donor, field))
                restored_fields.append(field)
        for key, value in _load_channel_data(donor).items():
            if key != ARCHIVED_DUPLICATES_KEY:
                survivor_data.setdefault(key, value)
        archived.append(
            {
                "id": donor.id,
                "chat_key": donor.chat_key,
                "instance_key": donor.instance_key,
                "is_active": donor.is_active,
                "preset_id": donor.preset_id,
                "workspace_id": donor.workspace_id,
                "channel_name": donor.channel_name,
                "channel_type": donor.channel_type,
                "create_time": donor.create_time.isoformat() if donor.create_time else None,
            },
        )
        # 同城重复行之间的 chat_key 通常完全一致（并发新建用的是同一个标识），此时引用无需改写。
        # 跨实例迁移则一定不同。不一致时必须先把引用搬到幸存行上，否则删行会把历史消息与记忆变成孤儿数据
        if donor.chat_key != survivor.chat_key:
            migrated = await migrate_chat_key_references(
                donor.chat_key,
                survivor.chat_key,
                instance_key=survivor.instance_key,
            )
            for source_name, count in migrated.items():
                reference_counts[source_name] = reference_counts.get(source_name, 0) + count
            logger.info(f"合并频道行时改写引用: {donor.chat_key} -> {survivor.chat_key}, 明细={migrated}")

    survivor_data[ARCHIVED_DUPLICATES_KEY] = archived
    survivor.data = json.dumps(survivor_data, ensure_ascii=False)
    await survivor.save(update_fields=[*RESTORABLE_FIELDS, "data", "update_time"])
    await DBChatChannel.filter(id__in=[donor.id for donor in donors]).delete()

    return ChannelAbsorbResult(restored_fields=restored_fields, reference_counts=reference_counts)


def build_instance_chat_key(adapter_key: str, instance_key: str, channel_id: str) -> str:
    """按实例段拼出会话标识

    与 `adapters/interface/base.py` 的 `BaseAdapter.build_chat_key` 是同一套格式，
    刻意不走适配器对象：归属迁移常发生在源实例已被停用、甚至适配器整体加载失败的时候，
    `adapter_utils.get_adapter()` 在那种情况下直接抛 `AdapterUnavailableError`，
    会把一个纯数据库操作变成对运行时状态的依赖。两者一致由测试钉住。
    """
    if not instance_key:
        return f"{adapter_key}-{channel_id}"
    return f"{adapter_key}-{instance_key}-{channel_id}"


class InstanceMigrationChannel(NamedTuple):
    """一个频道在归属迁移里的去向"""

    channel_id: str
    channel_name: str
    source_chat_key: str
    target_chat_key: str
    # 目标实例下已有同 `channel_id` 的频道行，本频道会被合并进去而不是整行搬过去
    conflict: bool
    message_count: int


class InstanceMigrationPlan(NamedTuple):
    """归属迁移的预览

    `counts` 是「将要改写的行数」，与执行结果的 `counts` 用完全相同的口径统计，
    因此预览数字与实际落库数字可以逐项对上（有回归测试钉住这条）。
    """

    adapter_key: str
    source_instance_key: str
    target_instance_key: str
    channels: List[InstanceMigrationChannel]
    counts: Dict[str, int]
    # 留在源实例、本操作**不搬**的数据，连同原因一起交给界面显式展示
    residues: Dict[str, int]


class InstanceMigrationOutcome(NamedTuple):
    """归属迁移的执行结果

    频道逐个结算：中途某个频道失败时，已完成的频道保持完整迁移状态，未开始的原样留在源实例，
    调用方据此知道「哪些搬了、哪些没搬」，重跑一次即可补齐剩下的。
    """

    adapter_key: str
    source_instance_key: str
    target_instance_key: str
    moved_channels: List[str]
    merged_channels: List[str]
    failed_channels: List[str]
    counts: Dict[str, int]
    # 执行完之后仍然挂在源实例名下的行数，正常情况全为 0
    residual: Dict[str, int]


# 归属迁移**不搬**的数据，及其理由。它们会被原样列进预览，避免管理员事后才发现少了东西。
INSTANCE_MIGRATION_RESIDUE_REASONS: Dict[str, str] = {
    "affection": "好感度按 (适配器, 实例, 平台用户) 唯一，同一个用户在两个实例下各有一行；"
    "合并两份分数怎么算没有唯一正确的口径，不由本操作替用户决定",
    "affection_event": "好感度变化流水随好感度一起留在源实例",
    "announcement_delivery": "公告投递流水是「当时发给了谁」的既成事实，改写它等于篡改审计记录",
}


async def plan_instance_migration(
    adapter_key: str,
    source_instance_key: str,
    target_instance_key: str,
) -> InstanceMigrationPlan:
    """算出「源实例 → 目标实例」归属迁移会改动什么，不写任何数据

    Args:
        adapter_key: 两个实例共同所属的适配器标识
        source_instance_key: 交出历史的实例
        target_instance_key: 接收历史的实例

    Returns:
        预览。`channels` 为空表示源实例名下已经没有频道，此时执行迁移是一次空操作。
    """
    channels = (
        await DBChatChannel.filter(adapter_key=adapter_key, instance_key=source_instance_key).order_by("id").all()
    )
    target_channel_ids = set(
        await DBChatChannel.filter(adapter_key=adapter_key, instance_key=target_instance_key).values_list(
            "channel_id",
            flat=True,
        ),
    )

    source_chat_keys = [channel.chat_key for channel in channels]
    # 一条分组查询拿全部频道的消息数：预览要给出逐频道明细，按频道逐个 count 会在几十个会话的
    # 实例上退化成几十次往返
    message_counts: Dict[str, int] = {
        str(row["chat_key"]): int(row["message_count"] or 0)
        for row in await DBChatMessage.filter(chat_key__in=source_chat_keys)
        .annotate(message_count=Count("id"))
        .group_by("chat_key")
        .values("chat_key", "message_count")
    }

    items = [
        InstanceMigrationChannel(
            channel_id=channel.channel_id,
            channel_name=channel.channel_name or "",
            source_chat_key=channel.chat_key,
            target_chat_key=build_instance_chat_key(adapter_key, target_instance_key, channel.channel_id),
            conflict=channel.channel_id in target_channel_ids,
            message_count=message_counts.get(channel.chat_key, 0),
        )
        for channel in channels
    ]

    counts = {
        "chat_channel": len(channels),
        "chat_message": await DBChatMessage.filter(chat_key__in=source_chat_keys).count(),
        "exec_code": await DBExecCode.filter(chat_key__in=source_chat_keys).count(),
        "plugin_data": await DBPluginData.filter(target_chat_key__in=source_chat_keys).count(),
        "recurring_timer_job": await DBRecurringTimerJob.filter(chat_key__in=source_chat_keys).count(),
        "workspace_comm_log": await DBWorkspaceCommLog.filter(source_chat_key__in=source_chat_keys).count(),
        # 记忆段落/情节既可能挂在频道上（聊天记忆），也可能只挂在实例上（CC 任务记忆），
        # 两条来源都归源实例所有，因此按并集统计——执行时也正是这两步的并集
        "mem_paragraph": await DBMemParagraph.filter(
            Q(instance_key=source_instance_key) | Q(origin_chat_key__in=source_chat_keys),
        ).count(),
        "mem_episode": await DBMemEpisode.filter(
            Q(instance_key=source_instance_key) | Q(origin_chat_key__in=source_chat_keys),
        ).count(),
        "mem_entity": await DBMemEntity.filter(instance_key=source_instance_key).count(),
        "mem_relation": await DBMemRelation.filter(instance_key=source_instance_key).count(),
        "preset": await DBPreset.filter(owner_instance_key=source_instance_key).count(),
    }

    residues = {
        "affection": await DBAffection.filter(adapter_key=adapter_key, instance_key=source_instance_key).count(),
        "affection_event": await DBAffectionEvent.filter(
            adapter_key=adapter_key,
            instance_key=source_instance_key,
        ).count(),
        "announcement_delivery": await DBAnnouncementDelivery.filter(instance_key=source_instance_key).count(),
    }

    return InstanceMigrationPlan(
        adapter_key=adapter_key,
        source_instance_key=source_instance_key,
        target_instance_key=target_instance_key,
        channels=items,
        counts=counts,
        residues=residues,
    )


async def migrate_instance_ownership(
    adapter_key: str,
    source_instance_key: str,
    target_instance_key: str,
) -> InstanceMigrationOutcome:
    """把源实例名下的会话与记忆改判给目标实例

    **破坏性且不可逆**，调用方必须先让用户看过 `plan_instance_migration` 的预览并二次确认。

    冲突（源与目标在同一个 `channel_id` 上各有一行）时**目标实例那一行幸存**，源行的绑定按
    「只补空缺、不覆盖」并进去后删除并归档快照。这与同城重复行合并的「取活跃者」不同，理由是
    两种场景已知的信息不同：重复行是同一个会话被并发写成两份，谁在接管流量只能靠 `is_active` 猜；
    跨实例迁移则**确定**目标行就是当前在收消息的那一行（消息带着目标实例的 `instance_key` 进来），
    而且它的 `instance_key` / `chat_key` 已经是迁移后应有的取值——让它幸存，需要改写的行最少，
    中途失败能丢的东西也最少。

    幂等：迁移完成后源实例名下不再有频道，重复执行得到全 0 的结果。

    每个频道一个事务，实例级的记忆/人设扫尾另起一个事务。一个频道失败不回滚已完成的频道，
    因为「搬了一半」在本操作里是可恢复状态（重跑即可补齐），而把已完成的搬运回退成半旧半新才是更坏的状态。
    """
    plan = await plan_instance_migration(adapter_key, source_instance_key, target_instance_key)

    moved: List[str] = []
    merged: List[str] = []
    failed: List[str] = []
    # 预览里出现过的数据源即便一行都没改也要留一个 0，否则「预览说 0、结果里干脆没这一项」
    # 会让调用方分不清"没改"和"没统计"
    counts: Dict[str, int] = dict.fromkeys(plan.counts, 0)

    for item in plan.channels:
        try:
            reference_counts = await _migrate_one_channel(adapter_key, item, target_instance_key)
        except Exception as e:
            logger.exception(
                f"迁移频道失败，该频道保持在源实例名下: {source_instance_key} -> {target_instance_key}, "
                f"channel_id={item.channel_id}, error={e}",
            )
            failed.append(item.channel_id)
            continue

        (merged if item.conflict else moved).append(item.channel_id)
        counts["chat_channel"] += 1
        for source_name, count in reference_counts.items():
            counts[source_name] = counts.get(source_name, 0) + count

    for source_name, count in (await _migrate_instance_scoped_rows(source_instance_key, target_instance_key)).items():
        counts[source_name] = counts.get(source_name, 0) + count

    residual = await _count_instance_residual(adapter_key, source_instance_key)
    logger.warning(
        f"实例归属迁移完成: {source_instance_key} -> {target_instance_key}, "
        f"搬迁 {len(moved)} 个频道、合并 {len(merged)} 个频道、失败 {len(failed)} 个，"
        f"明细={counts}，源实例残留={residual}",
    )
    return InstanceMigrationOutcome(
        adapter_key=adapter_key,
        source_instance_key=source_instance_key,
        target_instance_key=target_instance_key,
        moved_channels=moved,
        merged_channels=merged,
        failed_channels=failed,
        counts=counts,
        residual=residual,
    )


async def _migrate_one_channel(
    adapter_key: str,
    item: InstanceMigrationChannel,
    target_instance_key: str,
) -> Dict[str, int]:
    """把一个频道连同它的全部引用改判给目标实例，返回各数据源改写的行数"""
    async with in_transaction():
        source_row = (
            await DBChatChannel.filter(adapter_key=adapter_key, chat_key=item.source_chat_key)
            .order_by("id")
            .select_for_update()
            .first()
        )
        if source_row is None:
            # 预览之后被别处删掉了；不是错误，重跑时它本来也不会再出现
            logger.info(f"频道行在迁移前已消失，跳过: chat_key={item.source_chat_key}")
            return {}

        target_row = (
            await DBChatChannel.filter(
                adapter_key=adapter_key,
                instance_key=target_instance_key,
                channel_id=item.channel_id,
            )
            .order_by("id")
            .select_for_update()
            .first()
        )
        if target_row is not None:
            return (await absorb_channel_rows(target_row, [source_row])).reference_counts

        counts = await migrate_chat_key_references(
            source_row.chat_key,
            item.target_chat_key,
            instance_key=target_instance_key,
        )
        source_row.instance_key = target_instance_key
        source_row.chat_key = item.target_chat_key
        await source_row.save(update_fields=["instance_key", "chat_key", "update_time"])
        return counts


async def _migrate_instance_scoped_rows(source_instance_key: str, target_instance_key: str) -> Dict[str, int]:
    """改判只挂在实例上、不挂在任何频道上的数据

    记忆图谱（实体 / 关系）与 CC 任务产生的段落只有 `instance_key`，人设的归属列是
    `owner_instance_key`。它们不随某个频道走，但整个实例都改判了，它们必须一起过去——
    否则频道搬到新实例后，人设在新作用域下不可见（`routers/presets.py` 按归属过滤），
    记忆检索的实例过滤也会把这批记忆挡在召回之外。
    """
    counts: Dict[str, int] = {}
    paragraph_ids: List[int] = list(
        await DBMemParagraph.filter(instance_key=source_instance_key).values_list("id", flat=True),
    )
    async with in_transaction():
        counts["mem_paragraph"] = await DBMemParagraph.filter(instance_key=source_instance_key).update(
            instance_key=target_instance_key,
        )
        counts["mem_episode"] = await DBMemEpisode.filter(instance_key=source_instance_key).update(
            instance_key=target_instance_key,
        )
        counts["mem_entity"] = await DBMemEntity.filter(instance_key=source_instance_key).update(
            instance_key=target_instance_key,
        )
        counts["mem_relation"] = await DBMemRelation.filter(instance_key=source_instance_key).update(
            instance_key=target_instance_key,
        )
        counts["preset"] = await DBPreset.filter(owner_instance_key=source_instance_key).update(
            owner_instance_key=target_instance_key,
        )
    await _sync_paragraph_vectors(paragraph_ids, target_instance_key)
    return counts


async def _count_instance_residual(adapter_key: str, source_instance_key: str) -> Dict[str, int]:
    """数一遍迁移之后仍然挂在源实例名下的行

    正常情况全是 0。非 0 意味着这些行在迁移前就已经与频道表对不上（例如频道行被删过而消息还在），
    本操作按频道驱动，够不着它们，必须显式报出来而不是让「源实例干净」变成一句没验证过的话。
    """
    chat_key_prefix = f"{adapter_key}-{source_instance_key}-"
    return {
        "chat_channel": await DBChatChannel.filter(adapter_key=adapter_key, instance_key=source_instance_key).count(),
        "chat_message": await DBChatMessage.filter(adapter_key=adapter_key, instance_key=source_instance_key).count(),
        "exec_code": await DBExecCode.filter(chat_key__startswith=chat_key_prefix).count(),
        "plugin_data": await DBPluginData.filter(target_chat_key__startswith=chat_key_prefix).count(),
        "recurring_timer_job": await DBRecurringTimerJob.filter(chat_key__startswith=chat_key_prefix).count(),
        "workspace_comm_log": await DBWorkspaceCommLog.filter(source_chat_key__startswith=chat_key_prefix).count(),
        "mem_paragraph": await DBMemParagraph.filter(instance_key=source_instance_key).count(),
        "mem_episode": await DBMemEpisode.filter(instance_key=source_instance_key).count(),
        "mem_entity": await DBMemEntity.filter(instance_key=source_instance_key).count(),
        "mem_relation": await DBMemRelation.filter(instance_key=source_instance_key).count(),
        "preset": await DBPreset.filter(owner_instance_key=source_instance_key).count(),
    }


def _load_channel_data(channel: DBChatChannel) -> Dict[str, Any]:
    """读出频道行的 `data` JSON，坏数据按空字典处理"""
    try:
        data = json.loads(channel.data or "{}")
    except (TypeError, ValueError):
        logger.warning(f"频道 data 字段不是合法 JSON，按空处理: id={channel.id}")
        return {}
    return data if isinstance(data, dict) else {}


async def _chat_channel_schema_ready() -> bool:
    """判断 `chat_channel` 表是否已经具备当前模型的全部列

    合并必须跑在 aerich 迁移之前（唯一约束的迁移会被存量重复行卡住），此时表结构可能还停在
    升级前的版本，ORM 的整行查询会直接报缺列。缺列即跳过：那种库还没引入 instance_key 维度，
    也就不可能产生本类重复行。
    """
    conn = Tortoise.get_connection("default")
    if conn.capabilities.dialect == "sqlite":
        rows = await conn.execute_query_dict("PRAGMA table_info('chat_channel')")
        existing = {str(row["name"]) for row in rows}
    else:
        rows = await conn.execute_query_dict(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'chat_channel'",
        )
        existing = {str(row["column_name"]) for row in rows}

    missing = set(DBChatChannel._meta.db_fields) - existing  # noqa: SLF001
    if missing:
        logger.info(f"chat_channel 表结构尚未升级完成，跳过重复频道合并: 缺少列 {sorted(missing)}")
        return False
    return True
