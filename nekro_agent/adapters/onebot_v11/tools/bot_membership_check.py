"""主动核对 Bot 在不在群：只标记，不自动停用

## 为什么要主动核对

`tools/bot_membership.py` 只在协议端推来「Bot 被踢 / 主动退群」通知时才知道 Bot 离开了某个群。
现实里通知会漏：通知上线之前就被踢掉的群、实例换了账号以后已经不在的旧群、以及协议端重启
期间丢掉的通知，都不会留下任何痕迹。这些频道在后台看起来仍然正常，节日祝福之类的广播
照样推过去，Agent 也照样白跑一轮，最后在发送时被 QQ 拒绝。

所以这里定期（或由后台手动）向协议端要一次群列表（`get_group_list`），和本实例名下的群频道
逐个比对，把不在列表里的频道打上「Bot 不在群」标记。

## 为什么只标记，不停用

「群列表里没有」和「这个频道不该再用了」不是同一件事：协议端自己的群列表可能不完整，
人也可能只是临时把 Bot 移出去又拉回来。自动停用会把这两种情况都变成一次真实的状态变更，
而且停用之后还需要人记得回来启用。这里因此只写一个可撤回的标记，是否停用由人在后台决定
（批量停用走的也是手动停用的那条路径）。

## fail-safe：宁可漏标，不能误标

一轮核对里只要有任何一处看起来不可信，这一轮就**一个标记都不写**，也不清除旧标记：

- 调不通协议端、超时、返回结构不对、返回空列表；
- 本次群数比上一次骤减（超过一半），但只降一次时先不采信，连续两次才按它标记；
- 某个「不在群」的频道一小时内还有人来过消息 —— 那多半是协议端返回的列表不完整。

这些情况都只记一条 WARNING，把判断留给下一轮。

## 为什么按实例分开

`chat_key` 里带实例段，同一个群在账号 A 下不在、在账号 B 下还在时，只该标 A 那个实例的频道。
协议端群列表是按账号返回的，因此核对也按实例逐个进行，基线同样带账号：实例换了 QQ 号，
旧账号的群数不能拿来和新账号比。
"""

import asyncio
import time
from enum import StrEnum
from typing import Callable, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel

from nekro_agent.adapters.onebot_v11.core.bot import BotInstanceOfflineError, get_bot
from nekro_agent.adapters.onebot_v11.core.channel import is_numeric_channel_id, parse_numeric_target_id
from nekro_agent.adapters.onebot_v11.core.instance import lookup_cached_account_id, resolve_account_id
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import (
    BotMembershipSource,
    BotMembershipStatus,
    DBChatChannel,
)
from nekro_agent.services.broadcast_targets import has_inbound_message_since

logger = get_sub_logger("adapter.onebot_v11")

# 单次取群列表的最长等待时间。各协议端对 `get_group_list` 的响应都远快于此，
# 超过这个时间还没回来基本就是连接卡住或协议端在重启，继续等只会拖住后台请求。
GROUP_LIST_TIMEOUT_SECONDS: float = 60.0

# 「刚刚还有人说话」的保护窗口。在窗口内有人发言、协议端却没这个群，先不采信这一轮。
RECENT_INBOUND_GUARD_SECONDS: int = 3600

# 进程刚起来时先等一会儿：协议端刚连上时群列表可能还没加载全，
# 这时候核对最容易把「还没加载完」误判成「Bot 不在群」。
INITIAL_DELAY_SECONDS: int = 600

# 调度器每 5 分钟醒一次看配置、看到没到点。间隔本身由配置决定，不在启动时固化。
TICK_SECONDS: int = 300


class MembershipCheckOutcome(StrEnum):
    CHECKED = "checked"          # 核对完成，标记已按结果写入
    OFFLINE = "offline"          # 实例未绑定账号或协议端没连上，没有核对
    CALL_FAILED = "call_failed"  # 调 get_group_list 出错或超时
    EMPTY = "empty"              # 返回空列表
    MALFORMED = "malformed"      # 返回结构不对
    INCOMPLETE = "incomplete"    # 判定为不完整


class MembershipCheckResult(BaseModel):
    adapter_key: str = "onebot_v11"
    instance_key: str
    outcome: MembershipCheckOutcome
    checked_at: int
    account_id: str = ""
    group_count: int = 0          # 协议端返回了几个群
    channel_count: int = 0        # 本实例参与比对的群频道数（数字群号的那些）
    not_in_group_count: int = 0   # 比对后不在群的频道数
    newly_marked: int = 0         # 本轮从「没有标记 / 在群」变成「不在群」的
    cleared: int = 0              # 本轮从「不在群」变成「在群」的
    skipped_non_numeric: int = 0  # 频道标识不是 group_<数字>、没法比对而跳过的
    message: str = ""             # 给人看的一句话（失败原因、判定不完整的理由）


# 每个实例一把锁：后台手动核对撞上定期核对时，后一次等前一次跑完再跑，
# 否则两轮会拿着各自的旧状态互相覆盖，计数也会重复。
_instance_locks: Dict[str, asyncio.Lock] = {}

# 每个 (实例, 账号) 上一次通过群列表解析的群数。带账号是为了在实例换绑后丢掉旧基线。
_group_count_baseline: Dict[Tuple[str, str], int] = {}


def reset_membership_check_state() -> None:
    """清空群数基线与实例锁（测试用）

    基线是进程内状态，测试之间不清会互相影响；锁在测试里也会被上一个事件循环绑住。
    """
    _instance_locks.clear()
    _group_count_baseline.clear()


def parse_group_list(raw: object) -> Optional[Set[int]]:
    """把协议端返回的群列表解析成群号集合

    结构不可信时整体返回 None，而不是只丢掉坏的那几条：一份列表里有坏元素，
    说明我们对协议端的返回格式理解错了，剩下的元素同样不可信。空列表返回空集合，
    由调用方判断成 `EMPTY`（空列表本身是合法结构，只是不能拿来标记）。
    """
    if not isinstance(raw, list):
        return None
    groups: Set[int] = set()
    for item in raw:
        if not isinstance(item, dict):
            return None
        if "group_id" not in item:
            return None
        value = item["group_id"]
        # bool 是 int 的子类，必须单独挡掉：True 会被 int() 当成 1
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            group_id = value
        elif isinstance(value, str) and value.isascii() and value.isdigit():
            group_id = int(value)
        else:
            return None
        if group_id <= 0:
            return None
        groups.add(group_id)
    return groups


def _make_result(
    instance_key: str,
    outcome: MembershipCheckOutcome,
    checked_at: int,
    *,
    account_id: str = "",
    message: str = "",
) -> MembershipCheckResult:
    return MembershipCheckResult(
        instance_key=instance_key,
        outcome=outcome,
        checked_at=checked_at,
        account_id=account_id,
        message=message,
    )


async def check_instance_membership(instance_key: str, *, now: Optional[int] = None) -> MembershipCheckResult:
    """核对一个实例名下的群频道；只有 `CHECKED` 时才会写库"""
    checked_at = int(time.time()) if now is None else now
    lock = _instance_locks.setdefault(instance_key, asyncio.Lock())
    async with lock:
        return await _check_instance_locked(instance_key, checked_at)


async def _check_instance_locked(instance_key: str, checked_at: int) -> MembershipCheckResult:
    account_id = lookup_cached_account_id(instance_key) or await resolve_account_id(instance_key)
    if not account_id:
        return _make_result(
            instance_key,
            MembershipCheckOutcome.OFFLINE,
            checked_at,
            message="实例未绑定账号",
        )

    try:
        bot = get_bot(account_id)
    except BotInstanceOfflineError:
        return _make_result(
            instance_key,
            MembershipCheckOutcome.OFFLINE,
            checked_at,
            account_id=account_id,
            message="协议端未连接",
        )

    try:
        # 不带任何参数：各协议端对 `get_group_list` 额外参数的支持不一致，
        # 传了反而可能被拒。
        raw = await asyncio.wait_for(bot.call_api("get_group_list"), timeout=GROUP_LIST_TIMEOUT_SECONDS)
    except Exception as e:
        # 超时异常的 str() 是空串，后台只会看到一句没有原因的「失败」，所以退回异常类名
        reason = str(e) or type(e).__name__
        logger.warning(
            f"核对 Bot 在群失败：实例 {instance_key}（账号 {account_id}）取群列表出错: {reason}",
        )
        return _make_result(
            instance_key,
            MembershipCheckOutcome.CALL_FAILED,
            checked_at,
            account_id=account_id,
            message=reason,
        )

    groups = parse_group_list(raw)
    if groups is None:
        logger.warning(f"核对 Bot 在群失败：实例 {instance_key}（账号 {account_id}）返回的群列表结构不对")
        return _make_result(
            instance_key,
            MembershipCheckOutcome.MALFORMED,
            checked_at,
            account_id=account_id,
            message="群列表结构不对",
        )

    if not groups:
        logger.warning(
            f"核对 Bot 在群跳过：实例 {instance_key}（账号 {account_id}）返回了空的群列表，为免误标，本轮不改任何标记",
        )
        return _make_result(
            instance_key,
            MembershipCheckOutcome.EMPTY,
            checked_at,
            account_id=account_id,
            message="群列表为空",
        )

    channels = await DBChatChannel.filter(
        adapter_key="onebot_v11",
        instance_key=instance_key,
        channel_id__startswith="group_",
    ).all()

    numeric_channels: List[Tuple[DBChatChannel, int]] = []
    skipped_non_numeric = 0
    for channel in channels:
        if not is_numeric_channel_id(channel.channel_id):
            skipped_non_numeric += 1
            logger.debug(f"核对 Bot 在群跳过频道 {channel.chat_key}：频道标识不是 group_<数字>")
            continue
        numeric_channels.append((channel, parse_numeric_target_id(channel.channel_id)))

    not_in_group = [(channel, group_id) for channel, group_id in numeric_channels if group_id not in groups]
    channel_count = len(numeric_channels)
    not_in_group_count = len(not_in_group)

    # 群数骤减要先看上一次：只降一次可能是协议端还没加载全，连续两次才是真的。
    # 这一次的群数无论如何都写进基线，否则「连续两次」永远等不到第二次。
    baseline_key = (instance_key, account_id)
    previous_count = _group_count_baseline.get(baseline_key)
    _group_count_baseline[baseline_key] = len(groups)
    if previous_count is not None and previous_count >= 4 and len(groups) * 2 < previous_count:
        message = (
            f"群数从上次的 {previous_count} 个降到 {len(groups)} 个，降幅超过一半，先不采信；"
            "下一次核对仍是这个数就按它标记"
        )
        logger.warning(f"核对 Bot 在群判定不完整：实例 {instance_key}（账号 {account_id}）{message}")
        return _make_result(
            instance_key,
            MembershipCheckOutcome.INCOMPLETE,
            checked_at,
            account_id=account_id,
            message=message,
        )

    # 刚刚还有人来过消息、协议端却说不在这群，比「列表不完整」更可能是协议端出了问题。
    since_timestamp = checked_at - RECENT_INBOUND_GUARD_SECONDS
    for channel, _group_id in not_in_group:
        if await has_inbound_message_since(channel.chat_key, since_timestamp):
            message = f"频道 {channel.chat_key} 一小时内还有人说话，却不在协议端返回的群列表里"
            logger.warning(f"核对 Bot 在群判定不完整：实例 {instance_key}（账号 {account_id}）{message}")
            return _make_result(
                instance_key,
                MembershipCheckOutcome.INCOMPLETE,
                checked_at,
                account_id=account_id,
                message=message,
            )

    newly_marked = 0
    cleared = 0
    not_in_group_count = 0
    for channel, group_id in numeric_channels:
        # 重新取一次行：从上面读到现在可能已经过去几秒，别人可能刚改过自定义名称、
        # 自动停用标记或启停状态。拿着旧 `data` 写回去会把这些改动覆盖掉。
        row = await DBChatChannel.filter(id=channel.id).first()
        if row is None:
            continue
        status = BotMembershipStatus.IN_GROUP if group_id in groups else BotMembershipStatus.NOT_IN_GROUP
        previous = await row.record_bot_membership(
            status,
            BotMembershipSource.CHECK,
            account_id=account_id,
            now=checked_at,
        )
        if status is BotMembershipStatus.NOT_IN_GROUP:
            not_in_group_count += 1
            if previous is not BotMembershipStatus.NOT_IN_GROUP:
                newly_marked += 1
        elif previous is BotMembershipStatus.NOT_IN_GROUP:
            cleared += 1

    logger.info(
        f"核对 Bot 在群完成：实例 {instance_key}（账号 {account_id}）协议端返回 {len(groups)} 个群，"
        f"比对 {channel_count} 个群频道，不在群 {not_in_group_count} 个，"
        f"新标 {newly_marked} 个，清除 {cleared} 个，跳过非数字频道 {skipped_non_numeric} 个",
    )
    return MembershipCheckResult(
        instance_key=instance_key,
        outcome=MembershipCheckOutcome.CHECKED,
        checked_at=checked_at,
        account_id=account_id,
        group_count=len(groups),
        channel_count=channel_count,
        not_in_group_count=not_in_group_count,
        newly_marked=newly_marked,
        cleared=cleared,
        skipped_non_numeric=skipped_non_numeric,
    )


async def check_all_instances() -> List[MembershipCheckResult]:
    """核对所有启用的 OneBot V11 实例；一个实例出错不影响其余实例"""
    instances = await DBAdapterInstance.filter(adapter_key="onebot_v11", enabled=True).exclude(
        status__in=["deleted", "archived", "disabled"],
    )

    results: List[MembershipCheckResult] = []
    for instance in instances:
        if not instance.instance_key:
            continue
        try:
            result = await check_instance_membership(instance.instance_key)
        except Exception:
            logger.exception(f"核对 Bot 在群时实例 {instance.instance_key} 出现未预期异常，跳过")
            continue
        results.append(result)
        if result.outcome is MembershipCheckOutcome.OFFLINE:
            # 实例不在线是常态，不该每 6 小时刷一屏
            logger.debug(f"核对 Bot 在群跳过：实例 {instance.instance_key} 不在线或未绑定账号")

    counts: Dict[str, int] = {}
    for result in results:
        counts[result.outcome.value] = counts.get(result.outcome.value, 0) + 1
    summary = "，".join(f"{outcome} {count} 个" for outcome, count in sorted(counts.items())) or "无实例"
    logger.info(f"核对 Bot 在群整轮完成：共核对 {len(results)} 个实例（{summary}）")
    return results


def is_check_due(
    *,
    now: float,
    started_at: float,
    last_run_at: Optional[float],
    interval_hours: int,
    initial_delay: float = INITIAL_DELAY_SECONDS,
) -> bool:
    """现在该不该跑一轮核对（纯函数，便于测试）

    间隔每次现读配置，所以改了间隔不用重启；`interval_hours <= 0` 表示关闭定期核对，
    后台仍可手动核对。
    """
    if interval_hours <= 0:
        return False
    if last_run_at is None:
        return now - started_at >= initial_delay
    return now - last_run_at >= interval_hours * 3600


class BotMembershipScheduler:
    """定期核对 Bot 在群的后台循环

    结构照 `services/console/instance_health.py`：串行地「睡一个 tick → 判断 → 跑一轮」，
    卡住的一轮只会把下一轮往后推，不会并发跑两轮。
    """

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._started_at: float = 0.0
        self._last_run_at: Optional[float] = None
        self._get_interval_hours: Callable[[], int] = lambda: 0
        self._tick_seconds: float = TICK_SECONDS

    def start(self, get_interval_hours: Callable[[], int]) -> None:
        if self._running:
            return
        self._running = True
        self._started_at = time.monotonic()
        self._get_interval_hours = get_interval_hours
        self._task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._tick_seconds)
                if not self._running:
                    return
                try:
                    interval_hours = int(self._get_interval_hours())
                except Exception as e:
                    logger.warning(f"读取核对 Bot 在群间隔配置失败，本轮按关闭处理: {e}")
                    interval_hours = 0
                interval_hours = max(0, interval_hours)
                if is_check_due(
                    now=time.monotonic(),
                    started_at=self._started_at,
                    last_run_at=self._last_run_at,
                    interval_hours=interval_hours,
                    initial_delay=INITIAL_DELAY_SECONDS,
                ):
                    try:
                        await check_all_instances()
                    finally:
                        # 整轮出错（比如查实例表时数据库抖了一下）也算跑过：不然下一个 tick 立刻重试，
                        # 数据库没恢复时每 5 分钟刷一次异常堆栈
                        self._last_run_at = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"核对 Bot 在群后台循环出现异常，继续下一轮: {e}")

    async def stop(self) -> None:
        self._running = False
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


# 全局单例：适配器 init 时启动，cleanup 时停掉
bot_membership_scheduler = BotMembershipScheduler()
