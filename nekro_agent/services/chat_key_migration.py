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
from tortoise.expressions import F, Q
from tortoise.functions import Count
from tortoise.transactions import in_transaction

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_affection import DBAffection, DBAffectionEvent
from nekro_agent.models.db_announcement import DBAnnouncementDelivery
from nekro_agent.models.db_channel_quota import DBChannelQuotaDaily, DBChannelQuotaPolicy
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_email import DBEmail
from nekro_agent.models.db_email_account import DBEmailAccount
from nekro_agent.models.db_email_outbox import DBEmailOutbox
from nekro_agent.models.db_email_send_log import DBEmailSendLog
from nekro_agent.models.db_email_user_pref import DBEmailUserPref
from nekro_agent.models.db_emotion_item import DBEmotionItem
from nekro_agent.models.db_emotion_set import DBEmotionSet
from nekro_agent.models.db_emotion_set_assignment import DBEmotionSetAssignment
from nekro_agent.models.db_exec_code import DBExecCode
from nekro_agent.models.db_mem_channel_summary import DBMemChannelSummary
from nekro_agent.models.db_mem_channel_summary_state import DBMemChannelSummaryState
from nekro_agent.models.db_mem_entity import DBMemEntity
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.models.db_mem_prompt_round import DBMemPromptRound
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.models.db_plugin_data import DBPluginData
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_recurring_timer_job import DBRecurringTimerJob
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_workspace_comm_log import DBWorkspaceCommLog
from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
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
    *,
    drop_plugin_settings: bool = False,
    drop_config_overrides: bool = False,
) -> Dict[str, int]:
    """把所有引用旧 chat_key 的记录迁移到新 chat_key，返回每张表更新的行数

    幂等：迁移完成后旧 chat_key 不再匹配任何行，重复调用只会得到全 0 的结果。

    所有表的改写在同一个事务内完成。调用方已处于事务中时（例如频道认领），这里会退化为
    savepoint，跟随外层事务一起提交或回滚。

    Args:
        old_chat_key: 升级前的频道标识
        new_chat_key: 升级后的频道标识
        instance_key: 认领该频道的账号实例标识，用于回填带实例维度的表
        drop_plugin_settings / drop_config_overrides: 跨租户迁移选了删除时为真——旧标识上的频道级
            插件开关 / 频道层配置覆盖**删掉**而不是搬到新标识（见 `instance_settings.transfer_session_settings`）

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
        counts["mem_entity_origin"] = await DBMemEntity.filter(origin_chat_key=old_chat_key).update(
            origin_chat_key=new_chat_key,
        )
        counts["mem_relation_origin"] = await DBMemRelation.filter(origin_chat_key=old_chat_key).update(
            origin_chat_key=new_chat_key,
        )
        # 摘要按（频道, 人设, 版本）唯一、暂停行按（频道, 人设）唯一。合并两行频道时新标识下
        # 可能已经有同一人设的摘要 / 暂停行：保留新标识那一份（幸存行上的是更晚的意图，与
        # 「只补空缺、不覆盖」同一口径），旧标识下同一人设的行删掉，否则整次改写撞唯一约束回滚。
        summary_presets = await _presets_under(DBMemChannelSummary, new_chat_key)
        if summary_presets:
            await DBMemChannelSummary.filter(chat_key=old_chat_key, preset_id__in=summary_presets).delete()
        counts["mem_channel_summary"] = await DBMemChannelSummary.filter(chat_key=old_chat_key).update(
            chat_key=new_chat_key,
            instance_key=instance_key,
        )
        state_presets = await _presets_under(DBMemChannelSummaryState, new_chat_key)
        if state_presets:
            await DBMemChannelSummaryState.filter(chat_key=old_chat_key, preset_id__in=state_presets).delete()
        counts["mem_channel_summary_state"] = await DBMemChannelSummaryState.filter(chat_key=old_chat_key).update(
            chat_key=new_chat_key,
            instance_key=instance_key,
        )
        counts["mem_prompt_round"] = await DBMemPromptRound.filter(chat_key=old_chat_key).update(
            chat_key=new_chat_key,
            instance_key=instance_key,
        )
        # 只有频道书带 `chat_key`（域书 / 个人书 / 人设书都没有它），这里改的是频道书，
        # 实例回填与摘要表同一口径。新标识下可能已经有同一个频道的书：书表没有唯一约束，
        # 两本都留着即可，不像摘要那样要先删掉撞上唯一键的那一份。
        counts["wb_book"] = await DBWbBook.filter(chat_key=old_chat_key).update(
            chat_key=new_chat_key,
            instance_key=instance_key,
        )
        counts["workspace_comm_log"] = await DBWorkspaceCommLog.filter(source_chat_key=old_chat_key).update(
            source_chat_key=new_chat_key,
        )
        # 表情行上记的「是哪个频道收的」只是展示与排查用（`collected_chat_key`），但换标识之后
        # 它若还指着旧标识，控制台就会显示一个已经查不到的频道。归属本身挂在套组上，不随它变。
        counts["emotion_item_collected"] = await DBEmotionItem.filter(collected_chat_key=old_chat_key).update(
            collected_chat_key=new_chat_key,
        )
        # 邮箱：实例自有账户上的「新邮件通知推到哪个频道」，以及用户邮件偏好里「最后一次设置
        # 发生在哪个频道」。两列都只是引用，换标识之后不改就会指向一个已经不存在的频道，
        # 表现为通知静默丢失、偏好页里的来源频道显示成一条查不到的记录。
        counts["email_account_notify"] = await DBEmailAccount.filter(notify_chat_key=old_chat_key).update(
            notify_chat_key=new_chat_key,
        )
        counts["email_user_pref_chat"] = await DBEmailUserPref.filter(set_in_chat_key=old_chat_key).update(
            set_in_chat_key=new_chat_key,
        )
        counts["channel_quota_daily"] = await _merge_channel_quota_daily(old_chat_key, new_chat_key, instance_key)
        # 频道层额度策略按（作用域, 适配器, 实例, 频道）唯一。新标识下已经有频道层策略时保留
        # 新的那一份（与上面摘要同一口径：幸存行上是更晚的意图），旧标识的删掉。
        if await DBChannelQuotaPolicy.filter(scope="channel", chat_key=new_chat_key).exists():
            await DBChannelQuotaPolicy.filter(scope="channel", chat_key=old_chat_key).delete()
        policy_updates: Dict[str, Any] = {"chat_key": new_chat_key}
        if instance_key:
            policy_updates["instance_key"] = instance_key
        counts["channel_quota_policy"] = await DBChannelQuotaPolicy.filter(
            scope="channel",
            chat_key=old_chat_key,
        ).update(**policy_updates)

    if counts.get("channel_quota_policy"):
        # 策略在进程里有整表缓存，改了库就要让它失效，否则判定还按旧标识找那一行
        from nekro_agent.services.channel_quota.policy import invalidate_policy_cache

        invalidate_policy_cache()
    if counts.get("email_account_notify"):
        # 适配器上「地址 → 归属」那张内存表里也记着通知频道（`InstanceMailOwner.notify_chat_key`）：
        # 不刷新的话，重启之前新邮件通知还推到旧标识上
        await _refresh_notify_owners(new_chat_key)
    counts["qdrant_paragraph_payload"] = await _sync_paragraph_vectors(paragraph_ids, instance_key, new_chat_key)
    counts["one_shot_timer"] = await _migrate_one_shot_timers(old_chat_key, new_chat_key)
    counts.update(
        await _transfer_session_settings(
            old_chat_key,
            new_chat_key,
            drop_plugin_settings=drop_plugin_settings,
            drop_config_overrides=drop_config_overrides,
        ),
    )
    return counts


async def _transfer_session_settings(
    old_chat_key: str,
    new_chat_key: str,
    *,
    drop_plugin_settings: bool,
    drop_config_overrides: bool,
) -> Dict[str, int]:
    """频道级插件开关（`sessions.yaml`）与频道层配置覆盖（`configs/channels/...`）跟到新标识上

    与一次性定时器同一个位置与限制：文件不参与数据库事务，外层事务之后若回滚，设置已经在新标识上。
    不搬的话换了标识的频道把开关与覆盖全丢了，而旧标识被别的频道重新用上时会直接继承它们。
    写不进去只记警告（库已经改了），设置留在旧标识上，按日志手工处理；新建实例前的兜底清扫会清掉
    带实例前缀的那些。跨租户迁移选了删除时（`drop_*`）不搬、删旧标识上的那一样。
    """
    from nekro_agent.services.instance_settings import transfer_session_settings

    try:
        return await transfer_session_settings(
            old_chat_key,
            new_chat_key,
            drop_plugin_settings=drop_plugin_settings,
            drop_config_overrides=drop_config_overrides,
        )
    except Exception as e:  # noqa: BLE001 - 库已改，设置文件写不进去只记警告
        logger.warning(f"换会话标识时处理频道级设置失败: {old_chat_key} -> {new_chat_key} | {type(e).__name__}")
        return {"plugin_session_toggle": 0, "config_channel_override": 0}


async def _refresh_notify_owners(new_chat_key: str) -> None:
    """通知频道改到 `new_chat_key` 的那些实例自有邮箱：刷新适配器内存里的归属项

    与实例改判之后的刷新同一个函数（`email_accounts.refresh_runtime_owners`），按账户的归属实例逐个刷。
    库已经改了：刷新失败只记警告（重启 email 适配器后按库表重建）。
    """
    from nekro_agent.services.email_accounts import refresh_runtime_owners

    owners = set(
        await DBEmailAccount.filter(notify_chat_key=new_chat_key).values_list("owner_adapter_key", "owner_instance_key"),
    )
    for owner_adapter_key, owner_instance_key in owners:
        try:
            await refresh_runtime_owners(owner_adapter_key, owner_instance_key)
        except Exception as e:  # noqa: BLE001 - 库已改，内存表刷新失败只记警告
            logger.warning(f"换会话标识后刷新邮箱归属表失败: {type(e).__name__}")


async def _merge_channel_quota_daily(old_chat_key: str, new_chat_key: str, instance_key: str) -> int:
    """把旧标识下的频道额度计数并到新标识上，返回处理的行数

    计数表按（频道, 日）唯一。合并两行重复频道时，新标识下可能已经有同一天的计数——
    那是同一个群那一天真实用掉的量，不能像摘要那样「保留新的、删旧的」，否则这一天的用量
    凭空少掉一半，暂停判定也跟着放宽。所以同一天的两行**相加**到新标识那一行（临时加额同样
    相加，告警档位取高的，暂停提示取已经发过的那个时刻），再删掉旧行；新标识下没有的那几天
    直接改标识。
    """
    old_rows = list(await DBChannelQuotaDaily.filter(chat_key=old_chat_key))
    if not old_rows:
        return 0
    existing = {
        row.day: row
        for row in await DBChannelQuotaDaily.filter(chat_key=new_chat_key, day__in=[row.day for row in old_rows])
    }
    for row in old_rows:
        target = existing.get(row.day)
        if target is None:
            updates: Dict[str, Any] = {"chat_key": new_chat_key}
            if instance_key:
                updates["instance_key"] = instance_key
            await DBChannelQuotaDaily.filter(id=row.id).update(**updates)
            continue
        await DBChannelQuotaDaily.filter(id=target.id).update(
            replies=F("replies") + int(row.replies or 0),
            tokens_all=F("tokens_all") + int(row.tokens_all or 0),
            tokens_reply=F("tokens_reply") + int(row.tokens_reply or 0),
            bonus=F("bonus") + int(row.bonus or 0),
            alerted_level=max(int(target.alerted_level or 0), int(row.alerted_level or 0)),
            pause_notice_sent_at=target.pause_notice_sent_at or row.pause_notice_sent_at,
        )
        await DBChannelQuotaDaily.filter(id=row.id).delete()
    return len(old_rows)


async def _presets_under(model: Any, chat_key: str) -> List[int]:
    """某个频道标识下已经有行的人设 ID（去重）。"""
    return sorted({int(value) for value in await model.filter(chat_key=chat_key).values_list("preset_id", flat=True)})


async def _sync_paragraph_vectors(
    paragraph_ids: List[int],
    instance_key: str,
    new_chat_key: str | None = None,
) -> int:
    """把改写后的 instance_key 与来源键同步到 Qdrant payload

    段落行改了而向量 payload 没改，记忆检索的实例过滤（`instance_scope`）会把这批记忆挡在
    召回之外，DB 与向量库的口径也会长期不一致。同步失败时不阻断认领流程，但必须留下可操作的告警。

    只改实例的迁移（`new_chat_key is None`）走原来的 `set_paragraph_instance_key`，来源键不动；
    认领频道才需要连 `origin_chat_key` 一起改。来源类型按数据库里的实际值分组写：迁移
    只换 chat_key，不该顺手把 `origin_chat_type` 覆盖成空串；存量行原本是空串的也保持空串。
    """
    if not paragraph_ids:
        return 0

    if new_chat_key is None:
        updated = await memory_qdrant_manager.set_paragraph_instance_key(paragraph_ids, instance_key)
        if not updated:
            logger.warning(
                f"记忆向量 payload 未能同步 instance_key，共 {len(paragraph_ids)} 条段落受影响，"
                f"请在 Qdrant 恢复后重建记忆向量库: instance_key={instance_key}",
            )
        return updated

    rows = await DBMemParagraph.filter(id__in=paragraph_ids).values("id", "origin_chat_type")
    groups: dict[str, List[int]] = {}
    for row in rows:
        groups.setdefault(str(row["origin_chat_type"] or ""), []).append(int(row["id"]))

    updated = 0
    for origin_chat_type, group_ids in groups.items():
        updated += await memory_qdrant_manager.set_paragraph_payload(
            group_ids,
            {
                "instance_key": instance_key,
                "origin_chat_key": new_chat_key,
                "origin_chat_type": origin_chat_type,
            },
        )
    if not updated:
        logger.warning(
            f"记忆向量 payload 未能同步 instance_key / origin_chat_key，共 {len(paragraph_ids)} 条段落受影响，"
            f"请在 Qdrant 恢复后重建记忆向量库: instance_key={instance_key}, chat_key={new_chat_key}",
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


async def absorb_channel_rows(
    survivor: DBChatChannel,
    donors: List[DBChatChannel],
    *,
    drop_plugin_settings: bool = False,
    drop_config_overrides: bool = False,
) -> ChannelAbsorbResult:
    """把若干旧频道行的绑定、`data` 与引用并入幸存行，并删掉旧行

    `drop_*`：跨租户迁移选了删除时，旧行标识上的频道级插件开关 / 频道层配置覆盖删掉、不并过去
    （见 `migrate_chat_key_references`）。

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
                drop_plugin_settings=drop_plugin_settings,
                drop_config_overrides=drop_config_overrides,
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
    # 源实例对上用户登记的服务器凭据行数。不进 `counts`：跨租户时这些行默认删除而不是改写，
    # 「将要改写的行数」与执行结果逐项对得上的约定在这一项上不成立，单列出来交给界面决定怎么展示
    ssh_credentials: int = 0
    # 源实例名下有设置的插件数、有生效覆盖的配置文件数（实例层 + 它的频道）。同样不进 `counts`：
    # 跨租户时默认删除而不是搬
    plugin_settings: int = 0
    config_overrides: int = 0


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
        # 表情：这个实例的收集库与指向它的 `instance` 分配，随实例一起改判。
        # 收集库按（适配器, 实例）归属，整实例迁移后它必须一起过去。
        "emotion_set": await DBEmotionSet.filter(
            kind="collect",
            owner_adapter_key=adapter_key,
            owner_instance_key=source_instance_key,
        ).count(),
        "emotion_item": await DBEmotionItem.filter(
            set__kind="collect",
            set__owner_adapter_key=adapter_key,
            set__owner_instance_key=source_instance_key,
        ).count(),
        "emotion_set_assignment": await DBEmotionSetAssignment.filter(
            target="instance",
            adapter_key=adapter_key,
            instance_key=source_instance_key,
        ).count(),
    }

    residues = {
        "affection": await DBAffection.filter(adapter_key=adapter_key, instance_key=source_instance_key).count(),
        "affection_event": await DBAffectionEvent.filter(
            adapter_key=adapter_key,
            instance_key=source_instance_key,
        ).count(),
        "announcement_delivery": await DBAnnouncementDelivery.filter(instance_key=source_instance_key).count(),
    }

    # 插件设置与配置覆盖：源实例名下（实例级 + 它的频道）有多少。不进 `counts`，理由同 SSH 凭据
    from nekro_agent.services.instance_settings import describe_counts

    settings = await describe_counts(adapter_key, source_instance_key, source_chat_keys)
    return InstanceMigrationPlan(
        adapter_key=adapter_key,
        source_instance_key=source_instance_key,
        target_instance_key=target_instance_key,
        channels=items,
        counts=counts,
        residues=residues,
        ssh_credentials=await DBWsSshCredential.filter(
            adapter_key=adapter_key,
            instance_key=source_instance_key,
            owner_kind="user",
        ).count(),
        plugin_settings=settings.plugin_settings,
        config_overrides=settings.config_overrides,
    )


async def migrate_instance_ownership(
    adapter_key: str,
    source_instance_key: str,
    target_instance_key: str,
    *,
    delete_ssh_credentials: bool = False,
    delete_plugin_settings: bool = False,
    delete_config_overrides: bool = False,
    actor: str = "",
    ssh_delete_detail: str = "实例迁移（跨租户）",
) -> InstanceMigrationOutcome:
    """把源实例名下的会话与记忆改判给目标实例

    **破坏性且不可逆**，调用方必须先让用户看过 `plan_instance_migration` 的预览并二次确认。

    `delete_ssh_credentials` 为真时，源实例对上用户登记的服务器凭据**删除**而不是跟到目标实例
    （逐行留 `cred_delete` 审计，`actor` 记操作人，`ssh_delete_detail` 是审计里写的缘由）。跨租户迁移时由调用方默认打开，与租户划转
    同一条取舍：凭据是「能进别人家门」的东西，误跟过去比误删代价大。结果里删除数记在
    `ssh_credential_deleted`，`ssh_credential` 仍只记改写数。

    `delete_plugin_settings` / `delete_config_overrides` 同一条取舍（跨租户时调用方默认打开）：源实例名下的
    插件设置（含插件的 API Key）与核心配置的实例层 / 频道层覆盖在**搬任何频道之前**删掉，删不动就整次
    迁移不做（`ValidationError`）。选了删的那一样之后一路不搬：逐个频道换标识时删旧标识上的，扫尾不搬
    实例级的，全部搬完再对源实例删一遍——开头删过之后源租户还能往源标识上写，这些不能落到目标方手里。
    删插件设置时源实例标识在别的适配器下还有一行就整次拒绝（实例级设置两行共用一份）。
    删除数记在 `plugin_settings_deleted` / `config_overrides_deleted`。不删时它们跟着
    走：频道级的随逐个频道搬，实例级的在扫尾时搬；有频道迁移失败时实例级的留在源实例，重跑补齐。

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
    from nekro_agent.schemas.errors import ValidationError
    from nekro_agent.services import instance_settings

    if delete_plugin_settings and await instance_settings.shared_instance_level_conflict(
        adapter_key,
        source_instance_key,
        (),
    ):
        # 插件的实例级设置只按实例标识存：别的适配器下的同名实例与源实例共用一份，删了连它的一起删。
        # 与彻底删除、划转遇到跨适配器同名时同一个做法——在任何改动之前拒绝
        raise ValidationError(
            reason=(
                f"实例标识 '{source_instance_key}' 在别的适配器下还有一个实例，插件的实例级设置按实例标识存、"
                "两者共用一份；请先处理同名实例，或取消勾选「一并删除插件设置」"
            ),
        )

    # 源或目标实例只要有一方归租户，源实例名下的频道搬走就会让它们绑着的工作区归属来回变：
    # 先把这些工作区记进「曾经不归平台方」的标记，记不下来就拒绝这次迁移（与令牌曝光记录同一个失败方向）
    from nekro_agent.services.mcp.web_chat_workspace_taint import (
        instance_owner_tenant,
        taint_workspaces_bound_to_instance_keys,
    )

    source_tenant = await instance_owner_tenant(adapter_key, source_instance_key)
    target_tenant = await instance_owner_tenant(adapter_key, target_instance_key)
    if source_tenant is not None or target_tenant is not None:
        await taint_workspaces_bound_to_instance_keys(
            [source_instance_key],
            reason=f"频道迁移实例 {source_instance_key} -> {target_instance_key}",
            refuse_on_failure=True,
        )

    plan = await plan_instance_migration(adapter_key, source_instance_key, target_instance_key)

    moved: List[str] = []
    merged: List[str] = []
    failed: List[str] = []
    # 预览里出现过的数据源即便一行都没改也要留一个 0，否则「预览说 0、结果里干脆没这一项」
    # 会让调用方分不清"没改"和"没统计"
    counts: Dict[str, int] = dict.fromkeys(plan.counts, 0)

    source_chat_keys = [item.source_chat_key for item in plan.channels]
    if delete_plugin_settings or delete_config_overrides:
        # 跨租户默认删：在搬任何频道之前删。删不动就拒绝（400），一个频道都没动，库里什么都没改。
        # 之后一路不搬（逐个频道删旧标识上的、扫尾不搬实例级的），搬完再删一遍：开头这一遍与搬完之间
        # 源租户还能往源标识上写，那些不能落到目标方手里
        try:
            await instance_settings.drop_instance_settings(
                adapter_key,
                source_instance_key,
                source_chat_keys,
                plugin_settings=delete_plugin_settings,
                config_overrides=delete_config_overrides,
            )
        except Exception as e:
            logger.error(
                f"{ssh_delete_detail}前删除源实例的设置失败，没有迁移: {source_instance_key} -> {target_instance_key}, "
                f"{type(e).__name__}: {e}",
            )
            raise ValidationError(
                reason=f"源实例的插件设置或配置覆盖删不掉，迁移没有执行，请重试: {type(e).__name__}",
            ) from e
        counts["plugin_settings_deleted"] = plan.plugin_settings if delete_plugin_settings else 0
        counts["config_overrides_deleted"] = plan.config_overrides if delete_config_overrides else 0
        logger.warning(
            f"{ssh_delete_detail}时删除源实例的设置: {source_instance_key} -> {target_instance_key}, "
            f"插件 {counts['plugin_settings_deleted']} 个、配置覆盖 {counts['config_overrides_deleted']} 个",
        )

    for item in plan.channels:
        try:
            reference_counts = await _migrate_one_channel(
                adapter_key,
                item,
                target_instance_key,
                drop_plugin_settings=delete_plugin_settings,
                drop_config_overrides=delete_config_overrides,
            )
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

    for source_name, count in (
        await _migrate_instance_scoped_rows(
            adapter_key,
            source_instance_key,
            target_instance_key,
            delete_ssh_credentials=delete_ssh_credentials,
            actor=actor,
            ssh_delete_detail=ssh_delete_detail,
            move_instance_level_settings=not failed,
            move_plugin_settings=not delete_plugin_settings,
            move_config_overrides=not delete_config_overrides,
        )
    ).items():
        counts[source_name] = counts.get(source_name, 0) + count

    if delete_plugin_settings or delete_config_overrides:
        # 搬完之后对源实例再删一遍（见开头那一遍的注释）。库已经改了，这一遍删不动只记警告：
        # 什么都没搬到目标实例上，残留在源标识上，残留统计会报出来，建同名实例前还会再扫
        redropped = await instance_settings.drop_instance_settings_quietly(
            adapter_key,
            source_instance_key,
            source_chat_keys,
            plugin_settings=delete_plugin_settings,
            config_overrides=delete_config_overrides,
            reason=f"{ssh_delete_detail}搬完之后",
        )
        if any(redropped.values()):
            logger.warning(
                f"{ssh_delete_detail}搬运期间源实例上又被写入了设置，已删除: "
                f"{source_instance_key} -> {target_instance_key}, {redropped}",
            )

    if target_tenant is not None:
        # 搬完再补一遍目标实例：开头置位与搬运之间新绑到源实例频道上的工作区，随频道一起到了租户名下。
        # 库已经改了，这一遍记不下来不再拒绝，记录会置为待重建（判定一律不合格）
        try:
            await taint_workspaces_bound_to_instance_keys(
                [target_instance_key],
                reason=f"频道迁移实例 {source_instance_key} -> {target_instance_key} 后复核",
            )
        except Exception as e:  # noqa: BLE001 - 库已改，补记失败只记警告
            logger.warning(f"频道迁移后补记网页聊天 MCP 工作区标记失败: {e}")

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
    *,
    drop_plugin_settings: bool = False,
    drop_config_overrides: bool = False,
) -> Dict[str, int]:
    """把一个频道连同它的全部引用改判给目标实例，返回各数据源改写的行数

    `drop_*`：跨租户迁移选了删除——这个频道旧标识上的那一样删掉、不搬（见 `migrate_chat_key_references`）。
    """
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
            return (
                await absorb_channel_rows(
                    target_row,
                    [source_row],
                    drop_plugin_settings=drop_plugin_settings,
                    drop_config_overrides=drop_config_overrides,
                )
            ).reference_counts

        counts = await migrate_chat_key_references(
            source_row.chat_key,
            item.target_chat_key,
            instance_key=target_instance_key,
            drop_plugin_settings=drop_plugin_settings,
            drop_config_overrides=drop_config_overrides,
        )
        source_row.instance_key = target_instance_key
        source_row.chat_key = item.target_chat_key
        await source_row.save(update_fields=["instance_key", "chat_key", "update_time"])
        return counts


async def _migrate_instance_scoped_rows(
    adapter_key: str,
    source_instance_key: str,
    target_instance_key: str,
    *,
    delete_ssh_credentials: bool = False,
    actor: str = "",
    ssh_delete_detail: str = "实例迁移（跨租户）",
    move_instance_level_settings: bool = True,
    move_plugin_settings: bool = True,
    move_config_overrides: bool = True,
) -> Dict[str, int]:
    """改判只挂在实例上、不挂在任何频道上的数据

    记忆图谱（实体 / 关系）与 CC 任务产生的段落只有 `instance_key`，人设的归属列是
    `owner_instance_key`。它们不随某个频道走，但整个实例都改判了，它们必须一起过去——
    否则频道搬到新实例后，人设在新作用域下不可见（`routers/presets.py` 按归属过滤），
    记忆检索的实例过滤也会把这批记忆挡在召回之外。

    `move_instance_level_settings=False`（有频道迁移失败时）：插件的实例级设置与实例层配置覆盖
    留在源实例上——源实例名下还有频道在用它们，重跑补齐那一次再搬。
    `move_plugin_settings` / `move_config_overrides` 为假（跨租户迁移选了删除）的那一样一行都不搬，
    由调用方搬完之后从源实例上删掉。
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
        # 书按实例对归属。频道书已经在逐个频道的迁移里改过实例，这里接住域书、个人书、
        # 实例的人设书，以及频道行早已不在的频道书——整个实例都改判了，它名下的书必须
        # 一起过去，否则这些书在新实例的域里一条都选不中（等于静默失效）。
        #
        # 按（适配器, 实例）限定，不只看实例：同一个实例标识在另一个适配器下可以是另一行、
        # 归另一个租户（存量数据），只按实例改会把那边的书一并改判到目标实例上。
        book_owner = {"adapter_key": adapter_key, "instance_key": source_instance_key}
        # 频道行早已不在的频道书，`chat_key` 还带着源实例段：只改实例的话，同一个频道在
        # 目标实例下重新出现时标识是目标实例段，这本书永远选不中。按逐个频道迁移同一套
        # 格式（`build_instance_chat_key`）把实例段换掉；认不出格式的标识原样留着。
        source_prefix = f"{adapter_key}-{source_instance_key}-"
        orphan_channel_books = await DBWbBook.filter(
            **book_owner,
            scope_kind="channel",
            chat_key__startswith=source_prefix,
        ).values_list("id", "chat_key")
        for book_id, chat_key in orphan_channel_books:
            await DBWbBook.filter(id=book_id).update(
                chat_key=build_instance_chat_key(adapter_key, target_instance_key, str(chat_key)[len(source_prefix) :]),
            )
        counts["wb_book"] = await DBWbBook.filter(**book_owner).update(instance_key=target_instance_key)
        # SSH 服务器凭据按实例对归属：整个实例改判了，用户登记的行要么一起跟到目标实例，
        # 要么（跨租户时默认）删掉——绝不能留在源实例对上，否则源实例的租户还能看见一台
        # 已经不属于它的服务器。只认 user 行：平台方行的两个归属列恒为空，本来也匹配不上。
        # 删除与审计放在同一个事务里：任何一步失败整段回滚，凭据原样留在源实例（不会半删半跟）
        ssh_rows = await DBWsSshCredential.filter(
            adapter_key=adapter_key,
            instance_key=source_instance_key,
            owner_kind="user",
        ).all()
        ssh_credential_ids = [int(row.id) for row in ssh_rows]
        counts["ssh_credential"] = 0
        counts["ssh_credential_deleted"] = 0
        if ssh_rows and delete_ssh_credentials:
            from nekro_agent.services.ssh_gateway import constants as ssh_constants
            from nekro_agent.services.ssh_gateway.credentials import ssh_credential_service

            # 先写审计再删行：快照来自行本身，删完就没有可读的归属了
            for ssh_row in ssh_rows:
                await ssh_credential_service._write_audit(  # noqa: SLF001 - 与凭据服务共用同一份审计写法
                    ssh_row,
                    event=ssh_constants.EVENT_CRED_DELETE,
                    allowed=True,
                    actor=actor,
                    detail=ssh_delete_detail,
                )
            # 按审计过的那几行删：读与删之间新登记的行没有审计，宁可留在源实例上由残留计数报出来
            counts["ssh_credential_deleted"] = await DBWsSshCredential.filter(id__in=ssh_credential_ids).delete()
            logger.warning(
                f"{ssh_delete_detail}时删除用户登记的服务器凭据: adapter_key={adapter_key}, "
                f"{source_instance_key} -> {target_instance_key}, deleted={counts['ssh_credential_deleted']}",
            )
        elif ssh_rows:
            counts["ssh_credential"] = await DBWsSshCredential.filter(
                adapter_key=adapter_key,
                instance_key=source_instance_key,
                owner_kind="user",
            ).update(instance_key=target_instance_key)
        # 邮箱：账户、缓存行快照、用户偏好、节日邮件队列、发信记录都按实例对归属，整个实例
        # 改判时必须一起过去。**用户偏好**要绕开唯一键（`(适配器, 实例, 平台用户)`）：目标实例
        # 已经有同一个用户的偏好行时保留目标那份、删掉源那份——"并成一份"没有正确口径
        # （地址用哪个、退订标记算谁的），而留两行会直接撞唯一键把整次迁移弄失败。
        counts["email_account"] = await DBEmailAccount.filter(
            owner_adapter_key=adapter_key,
            owner_instance_key=source_instance_key,
        ).update(owner_instance_key=target_instance_key)
        counts["email_cache"] = await DBEmail.filter(
            owner_adapter_key=adapter_key,
            owner_instance_key=source_instance_key,
        ).update(owner_instance_key=target_instance_key)
        counts["email_outbox"] = await DBEmailOutbox.filter(
            adapter_key=adapter_key,
            instance_key=source_instance_key,
        ).update(instance_key=target_instance_key)
        counts["email_send_log"] = await DBEmailSendLog.filter(
            adapter_key=adapter_key,
            instance_key=source_instance_key,
        ).update(instance_key=target_instance_key)
        counts["email_user_pref"] = await _migrate_instance_user_prefs(
            adapter_key,
            source_instance_key,
            target_instance_key,
        )
    # 归属变了的凭据行：提交之后通知网关，按旧归属授权的连接断开、下一次按新归属重新判
    from nekro_agent.services.ssh_gateway import events as ssh_events

    for credential_id in ssh_credential_ids:
        await ssh_events.notify_credential_changed(int(credential_id))
    await _sync_paragraph_vectors(paragraph_ids, target_instance_key)
    counts.update(await _migrate_instance_emotions(adapter_key, source_instance_key, target_instance_key))
    # 平台方邮箱点名这个实例的发信授权改写：不跟着改的话，迁移之后目标实例拿不到那份
    # 授权（`listed` 名单里还是源实例的名字），而源实例的名字已经没人用了。
    counts["email_send_share"] = _rewrite_platform_share_instance(
        adapter_key,
        source_instance_key,
        target_instance_key,
    )
    # 适配器内存里的「地址 → 归属」表也要跟上（事务已提交）：不刷新的话，重启之前收到的新信
    # 快照会记成源实例，而源实例可能归另一个租户
    from nekro_agent.services.email_accounts import refresh_runtime_owners

    try:
        counts["email_runtime_owner"] = await refresh_runtime_owners(adapter_key, target_instance_key)
    except Exception as e:  # noqa: BLE001 - 库已改，内存表刷新失败只记警告（重启后按库表重建）
        logger.warning(f"实例归属迁移后刷新邮箱归属表失败: {type(e).__name__}")
        counts["email_runtime_owner"] = 0
    # 插件的实例级设置与核心配置的实例层覆盖按实例标识存：改到目标实例上，频道行早已不在的频道级
    # 开关与覆盖按前缀改（还留在源实例名下的频道除外——迁移失败的那些不是残留）。不跟着改的话目标
    # 实例丢了这些设置，而源实例名将来被重新认领时会读到它们（建实例前的兜底清扫会清，但那是兜底）。
    # 文件不参与事务；搬不动只记警告，残留统计里的 `plugin_settings` / `config_overrides` 会把它报出来。
    from nekro_agent.services.instance_settings import instance_chat_keys, move_instance_settings

    try:
        counts.update(
            await move_instance_settings(
                adapter_key,
                source_instance_key,
                target_instance_key,
                exclude_chat_keys=await instance_chat_keys(adapter_key, source_instance_key),
                instance_level=move_instance_level_settings,
                plugin_settings=move_plugin_settings,
                config_overrides=move_config_overrides,
            ),
        )
    except Exception as e:  # noqa: BLE001 - 库已改，设置搬不动只记警告
        logger.warning(f"实例归属迁移时搬插件设置与配置覆盖失败: {type(e).__name__}: {e}")
        counts["plugin_settings"] = 0
        counts["config_overrides"] = 0
    return counts


def _instance_level_plugins(instance_key: str) -> List[str]:
    """实例级启停与实例级配置涉及的插件（残留统计用）"""
    from nekro_agent.services.plugin.scope_lifecycle import instance_level_plugins

    return instance_level_plugins(instance_key)


def _instance_config_override_residual(adapter_key: str, instance_key: str) -> int:
    """实例层配置覆盖是否还有生效项（残留统计用）"""
    from nekro_agent.services.config_override_lifecycle import count_active_overrides

    return count_active_overrides(adapter_key, instance_key)


async def _migrate_instance_user_prefs(
    adapter_key: str,
    source_instance_key: str,
    target_instance_key: str,
) -> int:
    """把用户邮件偏好从源实例改判到目标实例，返回改动行数

    目标实例已有同一个用户的偏好行时**保留目标、删源**：两行的地址、订阅与退订标记可能
    完全不同，合并没有正确口径；而直接改写会撞 `(适配器, 实例, 平台用户)` 唯一键。

    这里只返回「真的改判过去的行数」：被删掉的源行也算处理过（它已经不在源实例名下了），
    但因为数据没有过去，单独记一个删除数没有意义，所以一并计入返回值。
    """
    source_rows = await DBEmailUserPref.filter(
        adapter_key=adapter_key,
        instance_key=source_instance_key,
    ).all()
    if not source_rows:
        return 0
    target_userids = set(
        await DBEmailUserPref.filter(
            adapter_key=adapter_key,
            instance_key=target_instance_key,
        ).values_list("platform_userid", flat=True),
    )
    migrated = 0
    for row in source_rows:
        if row.platform_userid in target_userids:
            await row.delete()
            continue
        row.instance_key = target_instance_key
        await row.save(update_fields=["instance_key", "update_time"])
        migrated += 1
    return migrated


def _rewrite_platform_share_instance(adapter_key: str, source_instance_key: str, target_instance_key: str) -> int:
    """把平台方邮箱 `SEND_SHARE_INSTANCES` 里点名源实例的那一项改写成目标实例

    与删除实例时「摘掉这一项」是一对：实例改判之后，平台方原本授权给它的那份发信能力
    应该跟着到新实例上，而不是留在一个没人用的 slug 上。`all_instances` 的账户不受影响
    （它本来就对所有实例开放）。判据与写回在服务层，email 适配器没加载时也照改。
    """
    from nekro_agent.services.email_accounts import rewrite_platform_share_instances

    return rewrite_platform_share_instances(
        f"{adapter_key}:{source_instance_key}",
        f"{adapter_key}:{target_instance_key}",
    )


async def _migrate_instance_emotions(
    adapter_key: str,
    source_instance_key: str,
    target_instance_key: str,
) -> Dict[str, int]:
    """把收集库与 `instance` 分配改判给目标实例

    收集库按（适配器, 实例）归属：目标实例已经有收集库时把源库的表情行移进去再删源套组，
    一个实例对只能有一个收集库（留两个会让「本实例收集」的语义分叉）。表情行的移动会顺带
    改写向量载荷里的 `set_id`，所以放在上面那个事务之外——向量库不参与事务。

    白板只读令牌是按实例对哈希存的键：实例改判之后旧键指向一个已经不存在的实例对，
    凭旧链接还能看那块板。键的算法与白板插件一致（服务层写了同样一份，用例钉住）。
    """
    from nekro_agent.services import emotion_library

    migrated = await emotion_library.migrate_instance_emotions(
        adapter_key,
        source_instance_key,
        adapter_key,
        target_instance_key,
    )
    return {
        "emotion_set": migrated.get("sets", 0),
        "emotion_item": migrated.get("moved_items", 0),
        "emotion_set_assignment": migrated.get("assignments", 0),
    }


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
        "mem_channel_summary": await DBMemChannelSummary.filter(
            adapter_key=adapter_key,
            instance_key=source_instance_key,
        ).count(),
        "preset": await DBPreset.filter(owner_instance_key=source_instance_key).count(),
        # 书按（适配器, 实例）归属：这里数的是「迁移之后还有没有书挂在源实例名下」。
        # 正常情况是 0；非 0 说明这本书的归属在迁移前就与频道表对不上，必须报出来。
        "wb_book": await DBWbBook.filter(adapter_key=adapter_key, instance_key=source_instance_key).count(),
        # 用户登记的 SSH 服务器凭据：迁移之后还挂在源实例名下就是残留，必须报出来
        "ssh_credential": await DBWsSshCredential.filter(
            adapter_key=adapter_key,
            instance_key=source_instance_key,
            owner_kind="user",
        ).count(),
        # 表情：迁移之后还有没有收集库 / `instance` 分配挂在源实例名下。正常是 0；
        # 非 0 说明这一对（适配器, 实例）的归属在迁移前就与实例表对不上。
        "emotion_set": await DBEmotionSet.filter(
            kind="collect",
            owner_adapter_key=adapter_key,
            owner_instance_key=source_instance_key,
        ).count(),
        "emotion_set_assignment": await DBEmotionSetAssignment.filter(
            target="instance",
            adapter_key=adapter_key,
            instance_key=source_instance_key,
        ).count(),
        # 邮箱：迁移之后还挂在源实例名下的账户、缓存行、用户偏好、节日队列与发信记录。
        # 正常是 0；非 0 说明这些行的归属在迁移前就与实例表对不上。
        "email_account": await DBEmailAccount.filter(
            owner_adapter_key=adapter_key,
            owner_instance_key=source_instance_key,
        ).count(),
        "email_cache": await DBEmail.filter(
            owner_adapter_key=adapter_key,
            owner_instance_key=source_instance_key,
        ).count(),
        "email_user_pref": await DBEmailUserPref.filter(
            adapter_key=adapter_key,
            instance_key=source_instance_key,
        ).count(),
        "email_outbox": await DBEmailOutbox.filter(
            adapter_key=adapter_key,
            instance_key=source_instance_key,
        ).count(),
        "email_send_log": await DBEmailSendLog.filter(
            adapter_key=adapter_key,
            instance_key=source_instance_key,
        ).count(),
        # 插件：迁移之后源实例名下还有实例级启停或实例级配置的插件数，正常是 0
        "plugin_settings": len(_instance_level_plugins(source_instance_key)),
        # 核心配置：迁移之后源实例的实例层覆盖是否还有生效项（0 / 1），正常是 0
        "config_overrides": _instance_config_override_residual(adapter_key, source_instance_key),
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
