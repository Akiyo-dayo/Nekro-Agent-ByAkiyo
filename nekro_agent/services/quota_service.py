import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("quota_service")

# 每日回复配额对应的可覆盖配置字段，以及它在覆盖层里的启用开关
QUOTA_LIMIT_FIELD = "AI_CHAT_DAILY_REPLY_LIMIT"
QUOTA_LIMIT_TOGGLE = f"enable_{QUOTA_LIMIT_FIELD}"

# 平台方为单个实例设定的配额上限，存在 `DBAdapterInstance.metadata_json` 里。
# 不做成可覆盖配置项是**有意的**：租户管理员能写自己名下实例的覆盖层
# （`PUT /config/instance/...` 是 Role.Admin + 归属校验），上限一旦落在同一层，
# 租户就能自己把它改掉，计费边界形同虚设。元数据只有 Role.Super 的总后台接口能写。
METADATA_QUOTA_CEILING = "quota_daily_reply_max"


class QuotaService:
    """配额管理服务

    负责管理聊天频道的AI回复配额，包括：
    - 临时提升额度（当天有效，重启失效）
    - 每小时限额计算
    - 配额进度查询

    数据全部存储在内存中，不依赖数据库。
    """

    def __init__(self):
        # 内存存储结构: {chat_key: {"date": "2024-01-15", "boost": 10}}
        # 临时提升仅当天有效，通过date字段判断是否过期
        self._daily_boosts: Dict[str, Dict] = {}

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    def _today_str(self) -> str:
        """返回额度时区下的今天（临时加额与频道额度用同一个换天时刻）

        延迟导入：`channel_quota` 的计数模块会反过来读 `quota_service` 的加额，
        模块级导入会成环。
        """
        from nekro_agent.services.channel_quota.counter import quota_day

        return quota_day()

    def _current_hour(self) -> int:
        """返回当前小时 (0-23)"""
        return datetime.now().hour

    # ------------------------------------------------------------------
    # 临时提升 (boost) 相关方法
    # ------------------------------------------------------------------

    def get_boost(self, chat_key: str) -> int:
        """获取指定频道当日的临时提升额度

        临时提升仅当天有效：若记录的日期不是今天，则视为已过期，返回 0。

        Args:
            chat_key: 聊天频道标识（如 "group_123456"）

        Returns:
            int: 当日临时提升的配额数，0 表示无提升或已过期
        """
        info = self._daily_boosts.get(chat_key)
        if not info or info["date"] != self._today_str():
            return 0
        return info["boost"]

    def set_boost(self, chat_key: str, amount: int) -> None:
        """设置指定频道当日的临时提升额度（覆盖原有值）

        Args:
            chat_key: 聊天频道标识
            amount: 要设置的提升配额数，建议 >= 0
        """
        self._daily_boosts[chat_key] = {
            "date": self._today_str(),
            "boost": amount,
        }
        logger.debug(f"[{chat_key}] 临时提升已设置为 {amount}")

    def add_boost(self, chat_key: str, amount: int) -> int:
        """在现有临时提升基础上追加额度

        若当日尚无提升记录，则从 0 开始累加。

        Args:
            chat_key: 聊天频道标识
            amount: 本次要追加的配额数

        Returns:
            int: 追加后的临时提升总额度
        """
        current = self.get_boost(chat_key)
        new_total = current + amount
        self.set_boost(chat_key, new_total)
        logger.debug(f"[{chat_key}] 临时提升追加 {amount}，当前总提升 {new_total}")
        return new_total

    def clear_boost(self, chat_key: str) -> None:
        """清除指定频道的临时提升记录

        清除后 get_boost() 将返回 0，直到再次调用 set_boost/add_boost。

        Args:
            chat_key: 聊天频道标识
        """
        removed = self._daily_boosts.pop(chat_key, None)
        if removed is not None:
            logger.debug(f"[{chat_key}] 临时提升已清除（原值 {removed.get('boost', 0)}）")

    # ------------------------------------------------------------------
    # 配额计算方法
    # ------------------------------------------------------------------

    def calculate_hourly_quota(self, daily_limit: int) -> int:
        """根据每日限额计算每小时限额（向上取整）

        公式: max(1, ceil(daily_limit / 24))

        当 daily_limit <= 0 时返回 0（表示无限额或未启用）。

        Args:
            daily_limit: 每日限额数（含临时提升后的有效限额）

        Returns:
            int: 每小时限额（最少 1 条；daily_limit <= 0 时为 0）

        示例:
            - daily_limit=24  → 1 条/小时
            - daily_limit=50  → 3 条/小时  (ceil(50/24)=3)
            - daily_limit=100 → 5 条/小时  (ceil(100/24)=5)
            - daily_limit=150 → 7 条/小时  (ceil(150/24)=7)
        """
        if daily_limit <= 0:
            return 0
        return max(1, math.ceil(daily_limit / 24))

    def get_hourly_quota_progress(
        self,
        chat_key: str,
        current_hour_count: int,
        hourly_limit: int,
    ) -> Dict:
        """获取当前小时的配额使用进度

        Args:
            chat_key: 聊天频道标识（保留字段，便于未来扩展日志）
            current_hour_count: 本小时已回复的消息数
            hourly_limit: 小时限额

        Returns:
            Dict: 包含以下字段的进度字典
                - hour (int): 当前小时 (0-23)
                - current_count (int): 本小时已使用数
                - hourly_limit (int): 小时限额
                - remaining (int): 剩余可用额度（最小为 0）
                - exceeded (bool): 是否已超限
        """
        return {
            "hour": self._current_hour(),
            "current_count": current_hour_count,
            "hourly_limit": hourly_limit,
            "remaining": max(0, hourly_limit - current_hour_count),
            "exceeded": current_hour_count >= hourly_limit,
        }


# 全局单例，供其他模块直接 import 使用
quota_service = QuotaService()


# ======================================================================
# 实例级配额管理
#
# `AI_CHAT_DAILY_REPLY_LIMIT` 本来就是 overridable 字段，能按实例设，但此前只有聊天命令
# `/quota_set` 能改，而它写的是**频道层**。几十个实例时既没有总览也没有批量入口。
# 下面这组函数把"按实例读写配额 + 平台方上限"做成服务，供总后台路由与租户写入护栏共用。
# ======================================================================


def is_quota_within_ceiling(limit: int, ceiling: Optional[int]) -> bool:
    """判断一个每日回复上限是否落在平台方设定的上限之内

    两个边界值都要按语义处理，不能只比大小：

    - `ceiling is None`：平台方没给这个实例设上限，任何取值都放行；
    - `limit <= 0`：配额字段里 0 表示**不限制**，在有上限时它是最大的越界，不是最小的取值。
    """
    if ceiling is None:
        return True
    if limit <= 0:
        return False
    return limit <= ceiling


def load_quota_ceiling(instance: Any) -> Optional[int]:
    """读取实例的平台方配额上限；未设置或数据损坏时返回 None（不设上限）"""
    from nekro_agent.services.console.instance_manager import load_metadata

    raw = load_metadata(instance).get(METADATA_QUOTA_CEILING)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if raw > 0 else None


async def save_quota_ceiling(instance: Any, ceiling: Optional[int]) -> None:
    """写入实例的平台方配额上限；`None` 或非正数表示取消上限

    读改写整份元数据而不是整体覆盖：容器信息、默认人设等都住在同一个字段里。
    """
    from nekro_agent.services.console.instance_manager import dump_metadata, load_metadata

    metadata = load_metadata(instance)
    if ceiling is None or ceiling <= 0:
        metadata.pop(METADATA_QUOTA_CEILING, None)
    else:
        metadata[METADATA_QUOTA_CEILING] = int(ceiling)
    instance.metadata_json = dump_metadata(metadata)
    await instance.save(update_fields=["metadata_json", "update_time"])


async def resolve_instance_daily_limit(adapter_key: str, instance_key: str) -> Tuple[int, str, int]:
    """解析某个实例当前生效的每日回复上限

    Returns:
        (生效值, 来源层 `instance`/`adapter`/`system`, 实例层被清除后会继承到的值)
    """
    from nekro_agent.core.overridable_config import OverridableConfig
    from nekro_agent.services.config_resolver import ConfigLayer, config_resolver
    from nekro_agent.services.config_service import UnifiedConfigService, instance_override_config_key

    config_key = instance_override_config_key(adapter_key, instance_key)
    inherited_values = await config_resolver.inherited_values(config_key) or {}
    inherited = int(inherited_values.get(QUOTA_LIMIT_FIELD, 0) or 0)

    overrides = UnifiedConfigService._get_config_instance(config_key)  # noqa: SLF001
    if isinstance(overrides, OverridableConfig) and getattr(overrides, QUOTA_LIMIT_TOGGLE, False):
        return int(getattr(overrides, QUOTA_LIMIT_FIELD)), ConfigLayer.INSTANCE.value, inherited

    # 继承值来自适配器层还是全局层，靠"适配器层是否启用了该字段"区分
    from nekro_agent.core.config import config as system_config
    from nekro_agent.services.config_service import ADAPTER_OVERRIDE_KEY_PREFIX

    adapter_overrides = UnifiedConfigService._get_config_instance(f"{ADAPTER_OVERRIDE_KEY_PREFIX}{adapter_key}")  # noqa: SLF001
    if isinstance(adapter_overrides, OverridableConfig) and getattr(adapter_overrides, QUOTA_LIMIT_TOGGLE, False):
        return inherited, ConfigLayer.ADAPTER.value, int(getattr(system_config, QUOTA_LIMIT_FIELD))
    return inherited, ConfigLayer.SYSTEM.value, inherited


def set_instance_daily_limit(adapter_key: str, instance_key: str, limit: Optional[int]) -> None:
    """写入实例层的每日回复上限；`limit is None` 表示取消覆盖、回落到上层继承值

    Raises:
        ValueError: 配置写入失败（配置键非法、落盘失败等）
    """
    from nekro_agent.services.config_service import UnifiedConfigService, instance_override_config_key

    config_key = instance_override_config_key(adapter_key, instance_key)
    updates = {QUOTA_LIMIT_TOGGLE: "false"} if limit is None else {QUOTA_LIMIT_TOGGLE: "true", QUOTA_LIMIT_FIELD: str(int(limit))}

    success, message = UnifiedConfigService.batch_update_config(config_key, updates)
    if not success:
        raise ValueError(f"写入实例配额失败: {message}")
    success, message = UnifiedConfigService.save_config(config_key)
    if not success:
        raise ValueError(f"保存实例配额失败: {message}")


async def collect_instance_quota_usage(instance_keys: List[str], limits: Dict[str, int]) -> Dict[str, Dict[str, int]]:
    """聚合各实例今日的配额消耗

    配额判定发生在**频道**粒度（`AI_CHAT_DAILY_REPLY_LIMIT` 是每频道每日上限），所以
    只给一个实例总数会误导运维：真正需要看的是"这个实例有几个频道已经撞到上限了"。
    这里按 (实例, 频道) 聚合一次，再在内存里折算成实例视角的指标。

    数据来自频道额度计数表 `DBChannelQuotaDaily` 的 `replies`（回复回合数），日期键与
    判定共用 `CHANNEL_QUOTA_TIMEZONE`；不再回头数 `DBChatMessage` 的 Bot 消息条数。

    Args:
        instance_keys: 需要统计的实例标识
        limits: 各实例当前生效的每日上限，用于算出撞线频道数（<=0 表示不限制）

    Returns:
        instance_key -> {replies_today, active_channels, peak_channel_replies, channels_at_limit}
    """
    from nekro_agent.models.db_channel_quota import DBChannelQuotaDaily
    from nekro_agent.services.channel_quota.counter import quota_day

    usage: Dict[str, Dict[str, int]] = {
        key: {"replies_today": 0, "active_channels": 0, "peak_channel_replies": 0, "channels_at_limit": 0}
        for key in instance_keys
    }
    if not instance_keys:
        return usage

    rows = await DBChannelQuotaDaily.filter(
        instance_key__in=instance_keys,
        day=quota_day(),
        replies__gt=0,
    ).values("instance_key", "chat_key", "replies")

    for row in rows:
        entry = usage.get(str(row["instance_key"]))
        if entry is None:
            continue
        count = int(row["replies"] or 0)
        entry["replies_today"] += count
        entry["active_channels"] += 1
        entry["peak_channel_replies"] = max(entry["peak_channel_replies"], count)
        limit = limits.get(str(row["instance_key"]), 0)
        if limit > 0 and count >= limit:
            entry["channels_at_limit"] += 1

    return usage


async def assert_tenant_quota_within_ceiling(config_key: str, submitted: Dict[str, str]) -> None:
    """租户写入覆盖配置时，不得让实例的每日回复上限突破平台方设定的上限

    这是**计费边界**，因此判定的是"这次写入之后实际生效的值"，而不只是提交上来的数字：
    只校验提交值的话，租户把 `enable_` 关掉就能让配额回落到全局层的"不限制"，
    照样绕过上限。频道层同样要管——它的优先级比实例层还高。

    平台方身份不走这里（调用方只在租户视角下调用），未设上限的实例直接放行，
    因此未启用多租户的部署完全不受影响。

    Raises:
        PermissionDeniedError: 写入后的生效值会突破平台方上限
    """
    if QUOTA_LIMIT_FIELD not in submitted and QUOTA_LIMIT_TOGGLE not in submitted:
        return

    adapter_key, instance_key = await _instance_of_override(config_key)
    if not instance_key:
        return

    from nekro_agent.models.db_adapter_instance import DBAdapterInstance
    from nekro_agent.schemas.errors import PermissionDeniedError

    # 上限挂在实例行上，必须取到被写的那一行：另一个适配器下的同名实例有它自己的上限
    instance = await DBAdapterInstance.filter(adapter_key=adapter_key, instance_key=instance_key).order_by("id").first()
    ceiling = load_quota_ceiling(instance) if instance is not None else None
    if ceiling is None:
        return

    resulting = await _resulting_daily_limit(config_key, submitted)
    if not is_quota_within_ceiling(resulting, ceiling):
        logger.warning(
            f"租户试图把实例 {instance_key} 的每日回复上限改为 "
            f"{'不限制' if resulting <= 0 else resulting}，超过平台方上限 {ceiling}，已拒绝",
        )
        raise PermissionDeniedError(
            detail=f"每日回复上限不得超过平台方为实例 {instance_key} 设定的 {ceiling}（0 表示不限制，同样不被允许）",
        )


async def _instance_of_override(config_key: str) -> Tuple[str, str]:
    """从覆盖层配置键反查它影响的实例 `(adapter_key, instance_key)`；不是覆盖层配置时返回两个空串"""
    from nekro_agent.services.config_service import (
        CHANNEL_OVERRIDE_KEY_PREFIX,
        INSTANCE_OVERRIDE_KEY_PREFIX,
        INSTANCE_OVERRIDE_KEY_SEP,
    )

    if config_key.startswith(INSTANCE_OVERRIDE_KEY_PREFIX):
        adapter_key, _, instance_key = config_key[len(INSTANCE_OVERRIDE_KEY_PREFIX) :].partition(
            INSTANCE_OVERRIDE_KEY_SEP,
        )
        return adapter_key, instance_key
    if config_key.startswith(CHANNEL_OVERRIDE_KEY_PREFIX):
        from nekro_agent.models.db_chat_channel import DBChatChannel

        chat_key = config_key[len(CHANNEL_OVERRIDE_KEY_PREFIX) :]
        channel = await DBChatChannel.filter(chat_key=chat_key).first()
        return (channel.adapter_key, channel.instance_key) if channel is not None else ("", "")
    return "", ""


async def _resulting_daily_limit(config_key: str, submitted: Dict[str, str]) -> int:
    """算出这次写入之后该覆盖层实际生效的每日回复上限

    提交里可能只带开关或只带数值，缺的那一半沿用该层当前已存的取值；开关最终为关时，
    生效的是上层的继承值。
    """
    from nekro_agent.services.config_resolver import config_resolver
    from nekro_agent.services.config_service import UnifiedConfigService

    def _current(item_key: str) -> Any:
        item = UnifiedConfigService.get_config_item(config_key, item_key)
        return item.get("value") if item else None

    if QUOTA_LIMIT_TOGGLE in submitted:
        enabled = submitted[QUOTA_LIMIT_TOGGLE].strip().lower() in ("true", "1", "yes")
    else:
        enabled = bool(_current(QUOTA_LIMIT_TOGGLE))

    if not enabled:
        inherited = await config_resolver.inherited_values(config_key) or {}
        return int(inherited.get(QUOTA_LIMIT_FIELD, 0) or 0)

    if QUOTA_LIMIT_FIELD in submitted:
        try:
            return int(str(submitted[QUOTA_LIMIT_FIELD]).strip())
        except ValueError:
            return 0
    try:
        return int(_current(QUOTA_LIMIT_FIELD) or 0)
    except (TypeError, ValueError):
        return 0
