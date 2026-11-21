import json
import time
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Optional, Union

from pydantic import BaseModel, ValidationError
from tortoise import fields
from tortoise.exceptions import IntegrityError
from tortoise.models import Model
from tortoise.signals import pre_save
from tortoise.timezone import now as db_now
from tortoise.transactions import in_transaction

from nekro_agent.adapters.utils import adapter_utils
from nekro_agent.core import config
from nekro_agent.core.logger import logger
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.nul_guard import strip_nul_before_save
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services.config_resolver import config_resolver
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.tools.text_sanitize import strip_nul

if TYPE_CHECKING:
    from nekro_agent.adapters.interface.base import BaseAdapter
    from nekro_agent.core.config import CoreConfig


# 实例元数据里存放"该实例默认人设 ID"的键，供人设回退链使用
INSTANCE_METADATA_DEFAULT_PRESET_ID = "default_preset_id"


async def get_instance_default_preset(adapter_key: str, instance_key: str) -> Optional[DBPreset]:
    """按 `(adapter_key, instance_key)` 取实例配置的默认人设

    从 `DBChatChannel._get_instance_default_preset` 抽出来放在模块级：节日邮件的正文按
    「实例 → 系统默认 → 内置」选人设，它手上没有频道（一个实例可能有很多频道，也可能一个都没有），
    只有实例对。两处共用同一段读元数据的逻辑，回退链才不会各自漂移。

    Returns:
        实例默认人设；未配置实例、未设默认人设或人设已删除时返回 None
    """
    if not adapter_key or not instance_key:
        return None
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance

    instance = await DBAdapterInstance.get_or_none(adapter_key=adapter_key, instance_key=instance_key)
    if instance is None or not instance.metadata_json:
        return None
    try:
        metadata = json.loads(instance.metadata_json)
    except json.JSONDecodeError:
        return None
    preset_id = metadata.get(INSTANCE_METADATA_DEFAULT_PRESET_ID) if isinstance(metadata, dict) else None
    if not isinstance(preset_id, int):
        return None
    return await DBPreset.get_or_none(id=preset_id)


class ChannelStatus(StrEnum):
    """频道状态枚举"""

    ACTIVE = "active"
    OBSERVE = "observe"
    DISABLED = "disabled"


# 频道 `data` 里记「这个频道是被系统自动停用的」的键。和管理员手动停用的区别只靠它：
# 有这个标记的停用，条件消失后系统会自己恢复；手动停用永远不会被自动恢复
CHANNEL_DATA_AUTO_DISABLE_KEY = "auto_disable"


class ChannelAutoDisableReason(StrEnum):
    """频道被系统自动停用的原因"""

    BOT_LEFT_GROUP = "bot_left_group"  # Bot 被移出群或自己退群


class ChannelAutoDisableInfo(BaseModel):
    """自动停用标记的内容

    `previous_status` 是恢复时要回到的状态：启用还是旁观。手动停用的频道不会被打上这个标记，
    所以恢复逻辑只看得到系统自己停掉的那些。
    """

    reason: ChannelAutoDisableReason
    detail: str = ""
    disabled_at: int  # Unix 秒
    previous_status: ChannelStatus = ChannelStatus.ACTIVE


# 频道 `data` 里记「最近一次确认 Bot 在不在这个群」的键。只是标记：不会因此停用或启用频道
CHANNEL_DATA_BOT_MEMBERSHIP_KEY = "bot_membership"


class BotMembershipStatus(StrEnum):
    IN_GROUP = "in_group"
    NOT_IN_GROUP = "not_in_group"


class BotMembershipSource(StrEnum):
    CHECK = "check"      # 定期核对或后台手动核对：拿协议端的群列表比对出来的
    INBOUND = "inbound"  # 这个群又来了入站消息、或收到 Bot 自己入群的通知


class BotMembershipInfo(BaseModel):
    status: BotMembershipStatus
    checked_at: int  # Unix 秒
    source: BotMembershipSource
    account_id: str = ""  # 核对时实例绑定的账号；入站来源时留空


class DefaultPreset(BaseModel):
    """默认人设"""

    name: str
    content: str


class DBChatChannel(Model):
    """数据库聊天频道模型"""

    id = fields.IntField(pk=True, generated=True, description="ID")
    is_active = fields.BooleanField(default=True, description="是否激活")
    observe_mode = fields.BooleanField(default=False, description="旁观模式")
    preset_id = fields.IntField(default=None, null=True, description="人设 ID")
    data = fields.TextField(description="频道数据")

    adapter_key = fields.CharField(max_length=64, index=True, description="适配器标识")
    # 同一适配器下的账号实例标识（创建时分配的稳定 slug，账号号码只是它的绑定属性）。
    # 单实例适配器留空。实例维度只体现在 chat_key 上，channel_id 保持干净形态，
    # 因此本字段是频道唯一性三元组的一部分，同时供记忆系统、插件直接查询，无需解析 chat_key 字符串。
    instance_key = fields.CharField(max_length=64, default="", index=True, description="适配器实例标识")
    channel_id = fields.CharField(max_length=256, index=True, description="频道 ID")
    channel_name = fields.CharField(max_length=64, null=True, description="频道名称")
    channel_type = fields.CharField(max_length=32, null=True, description="频道类型")

    chat_key = fields.CharField(max_length=256, index=True, description="全局聊天频道唯一标识")
    conversation_start_time = fields.DatetimeField(auto_now_add=True, description="对话起始时间")
    workspace_id = fields.IntField(null=True, description="关联工作区 ID")

    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    _effective_config: Optional["CoreConfig"] = None
    # 缓存 _effective_config 时的配置版本号，用于感知别处写入覆盖配置后的失效
    _effective_config_version: int = -1

    class Meta:  # type: ignore
        table = "chat_channel"
        # 频道唯一性三元组。没有这条数据库约束时，两条几乎同时到达的消息会各自走完
        # 「查不到 -> 新建」的竞争窗口，把同一个会话写成两行：一行留着人设/工作区绑定，
        # 另一行是空壳，而 `get_or_none` 之后会对整个频道抛 MultipleObjectsReturned。
        unique_together = (("adapter_key", "instance_key", "channel_id"),)

    @classmethod
    def _get_default_channel_status(cls, adapter_key: str, channel_type: ChatType) -> ChannelStatus:
        try:
            adapter = adapter_utils.get_adapter(adapter_key)
            return ChannelStatus(adapter.get_default_channel_status(channel_type))
        except Exception as e:
            logger.debug(f"获取适配器默认频道状态失败，使用全局配置: {adapter_key}, {e!s}")

        if channel_type == ChatType.GROUP and config.SESSION_GROUP_ACTIVE_DEFAULT:
            return ChannelStatus.ACTIVE
        if channel_type == ChatType.PRIVATE and config.SESSION_PRIVATE_ACTIVE_DEFAULT:
            return ChannelStatus.ACTIVE
        return ChannelStatus.DISABLED

    @classmethod
    async def get_by_identity(
        cls,
        adapter_key: str,
        instance_key: str,
        channel_id: str,
    ) -> Optional["DBChatChannel"]:
        """按频道唯一性三元组取回频道行

        刻意不用 `get_or_none`：它在匹配到多行时抛 MultipleObjectsReturned，而唯一约束是本次
        修复才补上的，存量库里仍可能残留重复行。此时整条入站链路直接崩掉，比"确定性地取最早那行"
        糟糕得多——最早那行正是携带人设/工作区绑定的原始行。
        """
        return await cls.filter(adapter_key=adapter_key, instance_key=instance_key, channel_id=channel_id).order_by(
            "id",
        ).first()

    @classmethod
    async def get_or_create(
        cls,
        adapter_key: str,
        channel_id: str,
        channel_type: ChatType,
        channel_name: str = "",
        chat_key: str | None = None,
        instance_key: str = "",
    ) -> "DBChatChannel":
        """获取或创建聊天频道

        唯一性由 (adapter_key, instance_key, channel_id) 三元组保证：`channel_id` 保持干净形态
        （如 `group_123456`），同一个群在不同账号下靠 instance_key 区分成不同的频道行，
        而不是共用同一行导致上下文混流。

        多账号化之前写入的频道行 channel_id 与现在完全一致，只是 instance_key 为空。这类"未认领"
        的历史行会在对应实例第一次有消息进来时被就地认领，避免升级后历史会话变成孤儿数据。

        同一频道的两条消息几乎总是并发处理（NoneBot 每个事件一个任务），因此"查不到就新建"
        存在真实的竞争窗口。这里用两道闸门收口：认领失败后必须重查（赢家刚把历史行改成本实例的行），
        新建撞上唯一约束后也必须重查。两者都拿不到才是真的异常。
        """
        # 下面既要拿它跟已有行的 channel_name 比，也要写进库；先清洗才能保证
        # 「比较用的」与「写进去的」是同一个值（否则每次同步都会判成"名字变了"再写一遍）
        channel_name = strip_nul(channel_name)
        resolved_chat_key = chat_key if chat_key is not None else f"{adapter_key}-{channel_id}"
        channel = await cls.get_by_identity(adapter_key, instance_key, channel_id)
        if not channel and instance_key:
            channel = await cls._claim_unclaimed_channel(
                adapter_key=adapter_key,
                channel_id=channel_id,
                chat_key=resolved_chat_key,
                instance_key=instance_key,
            )
            if not channel:
                # 认领竞争的败者：谓词重新求值时历史行已带上赢家的 instance_key，
                # 此刻本实例的频道行其实已经存在，直接新建就会写出重复行
                channel = await cls.get_by_identity(adapter_key, instance_key, channel_id)
        if not channel:
            default_status = cls._get_default_channel_status(adapter_key=adapter_key, channel_type=channel_type)
            try:
                channel = await cls.create(
                    adapter_key=adapter_key,
                    instance_key=instance_key,
                    channel_id=channel_id,
                    channel_type=channel_type.value,
                    channel_name=channel_name,
                    chat_key=resolved_chat_key,
                    is_active=default_status != ChannelStatus.DISABLED,
                    observe_mode=default_status == ChannelStatus.OBSERVE,
                    data=json.dumps({}),
                )
            except IntegrityError:
                channel = await cls.get_by_identity(adapter_key, instance_key, channel_id)
                if not channel:
                    raise
        else:
            if channel_name and channel.channel_name != channel_name:
                logger.info(f"更新频道名称: {channel.channel_name} -> {channel_name}")
                channel.channel_name = channel_name
                await channel.save()
            if channel_type and channel.channel_type != channel_type.value:
                logger.info(f"更新频道类型: {channel.channel_type} -> {channel_type.value}")
                channel.channel_type = channel_type.value
                await channel.save()
        return channel

    @classmethod
    async def _claim_unclaimed_channel(
        cls,
        adapter_key: str,
        channel_id: str,
        chat_key: str,
        instance_key: str,
    ) -> Optional["DBChatChannel"]:
        """把尚未归属任何实例的历史频道行认领给当前实例（惰性认领）

        多账号化之前的行 channel_id 与现在完全一致，只是 instance_key 为空、chat_key 不带实例段。
        频道第一次有消息进来时才触发，因此只会触碰真正在用的频道，天然幂等且可随时中断：
        认领完成后该行已带 instance_key，后续查询直接命中主查询，不会再次进入本流程。

        instance_key 非空的行说明已被别的实例认领过，谓词天然把它们排除在外，
        调用方会走新建流程，避免两个账号互相抢夺同一份历史数据。

        引用迁移与频道行改写放在同一个事务里，任一步失败都整体回滚：频道行仍是未认领状态，
        下一条消息会重新走完整认领流程，不会留下「频道已改名但引用还指向旧 chat_key」的孤儿状态。

        Returns:
            认领成功的频道行；没有可认领的历史记录时返回 None
        """
        from nekro_agent.services.chat_key_migration import migrate_chat_key_references

        # 行锁保证并发消息只会有一个实例认领成功：后到者重新求值谓词时该行 instance_key 已非空，取不到记录
        async with in_transaction():
            unclaimed_channel = (
                await cls.filter(adapter_key=adapter_key, instance_key="", channel_id=channel_id)
                .order_by("id")
                .select_for_update()
                .first()
            )
            if not unclaimed_channel:
                return None

            old_chat_key = unclaimed_channel.chat_key
            logger.info(
                f"认领历史频道记录: {old_chat_key} -> {chat_key} (adapter_key={adapter_key}, instance_key={instance_key})",
            )
            migrated = await migrate_chat_key_references(old_chat_key, chat_key, instance_key=instance_key)

            # 历史频道可能已经绑着工作区：认领给租户实例后，那个工作区的归属就从平台方变成了租户。
            # 认领是自动路径，置位失败不拦认领，只记警告（记录不可用期间所有工作区令牌都不生效）
            if unclaimed_channel.workspace_id is not None:
                try:
                    from nekro_agent.services.mcp.web_chat_workspace_taint import (
                        instance_owner_tenant,
                        taint_workspaces,
                    )

                    if await instance_owner_tenant(adapter_key, instance_key) is not None:
                        taint_workspaces(
                            [unclaimed_channel.workspace_id],
                            reason=f"历史频道认领到租户实例 {instance_key}",
                        )
                except Exception as e:  # noqa: BLE001 - 置位失败不影响认领
                    logger.warning(f"认领历史频道 {old_chat_key} 时置位工作区标记失败: {e}")

            unclaimed_channel.chat_key = chat_key
            unclaimed_channel.instance_key = instance_key
            await unclaimed_channel.save(update_fields=["chat_key", "instance_key", "update_time"])

        logger.info(f"历史频道引用迁移完成: {old_chat_key} -> {chat_key}, 明细={migrated}")
        return unclaimed_channel

    @classmethod
    async def get_channel(cls, chat_key: str) -> "DBChatChannel":
        """获取聊天频道"""
        assert chat_key, "获取聊天频道失败，chat_key 为空"
        # 显式定序：存量库里可能还残留同 chat_key 的重复行，不定序时不同请求会随机落到不同行，
        # 表现为"人设时有时无"。最早那行是携带绑定的原始行，取它最稳
        channel = await cls.filter(chat_key=chat_key).order_by("id").first()
        if not channel:
            raise ValueError(f"聊天频道不存在: {chat_key}")
        return channel

    async def sync_channel_name(self):
        """同步频道名称"""
        try:
            self.channel_name = await self.get_channel_name()
        except Exception as e:
            logger.error(f"同步频道名称失败: {e!s}")
        else:
            await self.save()

    async def get_channel_name(self) -> str:
        """获取频道名称"""

        adapter = adapter_utils.get_adapter(self.adapter_key)
        return (await adapter.get_channel_info(self.channel_id)).channel_name

    def get_custom_channel_name(self) -> Optional[str]:
        try:
            data = json.loads(self.data or "{}")
        except Exception:
            return None
        value = data.get("custom_channel_name")
        return value if isinstance(value, str) and value else None

    async def set_custom_channel_name(self, name: Optional[str]) -> None:
        try:
            data = json.loads(self.data or "{}")
        except Exception:
            data = {}

        custom_name = name.strip() if name else ""
        if custom_name:
            data["custom_channel_name"] = custom_name
        else:
            data.pop("custom_channel_name", None)

        self.data = json.dumps(data, ensure_ascii=False)
        await self.save(update_fields=["data", "update_time"])

    async def reset_channel(self):
        """重置聊天频道"""
        from nekro_agent.schemas.agent_ctx import AgentCtx

        # 该列随后会被当作 `create_time__gte` 的比较值去筛消息（见 routers/chat_channel.py），
        # 必须与 ORM 写 `create_time` 时用的时区表示一致，否则边界含义取决于底层驱动
        self.conversation_start_time = db_now()  # 重置对话起始时间
        # `/reset` 之后下一次被叫到要重发开场白：把记录写成 `-1` 而不是删键。删掉的话，
        # 有过 Bot 消息的频道会被当成升级前的老频道，只记不发。读改写 `data` 保住其它键
        # （自定义频道名、自动停用标记），随下面这一次整行 `save()` 一起写库。
        from nekro_agent.services.tavern.greeting import GREETED_KEY, RESET_MARK

        data = self._load_data()
        data[GREETED_KEY] = RESET_MARK
        self.data = json.dumps(data, ensure_ascii=False)
        await self.save()

        # 执行重置回调
        await plugin_collector.chat_channel_on_reset(await AgentCtx.create_by_chat_key(chat_key=self.chat_key))

    def _load_data(self) -> dict[str, Any]:
        """把 `data` 解析成字典；坏 JSON 或不是对象时返回空字典，不让脏数据打断调用链"""
        try:
            data = json.loads(self.data or "{}")
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get_auto_disable_info(self) -> Optional[ChannelAutoDisableInfo]:
        """读取自动停用标记；没有标记或标记内容不合法时返回 None"""
        raw = self._load_data().get(CHANNEL_DATA_AUTO_DISABLE_KEY)
        if not isinstance(raw, dict):
            return None
        try:
            return ChannelAutoDisableInfo.model_validate(raw)
        except ValidationError:
            return None

    async def auto_disable(self, reason: ChannelAutoDisableReason, detail: str = "") -> bool:
        """系统按确定性原因自动停用频道

        只停此刻**启用着**（含旁观）的频道。已经停用的（不论手动还是自动）直接返回 False、
        什么都不写——手动停用的频道不会被打上标记，也就永远不会被自动恢复。
        人的意图优先：后台 / 聊天命令的启停会清掉这个标记，之后不再属于「自动停用」。

        Returns:
            是否真的把频道停用了
        """
        if not self.is_active:
            return False

        data = self._load_data()
        info = ChannelAutoDisableInfo(
            reason=reason,
            detail=detail[:200],
            disabled_at=int(time.time()),
            previous_status=self.channel_status,
        )
        data[CHANNEL_DATA_AUTO_DISABLE_KEY] = info.model_dump(mode="json")
        self.data = json.dumps(data, ensure_ascii=False)
        self.is_active = False
        self.observe_mode = False
        await self.save(update_fields=["is_active", "observe_mode", "data", "update_time"])
        return True

    async def restore_auto_disabled(self, reason: ChannelAutoDisableReason) -> bool:
        """按自动停用标记恢复频道；只恢复标记原因匹配的那些

        `previous_status` 决定回到启用还是旁观。频道此刻已经启用（标记残留）时只清标记并返回 False，
        不把管理员的启用状态改回旧值。

        Returns:
            是否真的从停用恢复成启用 / 旁观
        """
        info = self.get_auto_disable_info()
        if info is None or info.reason != reason:
            return False

        data = self._load_data()
        data.pop(CHANNEL_DATA_AUTO_DISABLE_KEY, None)
        self.data = json.dumps(data, ensure_ascii=False)
        if self.is_active:
            await self.save(update_fields=["data", "update_time"])
            return False

        self.is_active = True
        self.observe_mode = info.previous_status == ChannelStatus.OBSERVE
        await self.save(update_fields=["is_active", "observe_mode", "data", "update_time"])
        return True

    def _clear_auto_disable_marker(self) -> None:
        """从内存里的 `data` 去掉自动停用标记，随调用方原来的 `save()` 一起写库

        后台接口和聊天命令（`/na_on` 之类）都是人的明确操作：手动启用或手动停用之后，
        这个频道就不再属于「自动停用」，条件消失时也不该被系统改回旧状态。
        """
        data = self._load_data()
        if CHANNEL_DATA_AUTO_DISABLE_KEY not in data:
            return
        data.pop(CHANNEL_DATA_AUTO_DISABLE_KEY, None)
        self.data = json.dumps(data, ensure_ascii=False)

    def get_bot_membership_info(self) -> Optional[BotMembershipInfo]:
        """读取「Bot 在不在群」标记；没有标记或标记内容不合法时返回 None"""
        raw = self._load_data().get(CHANNEL_DATA_BOT_MEMBERSHIP_KEY)
        if not isinstance(raw, dict):
            return None
        try:
            return BotMembershipInfo.model_validate(raw)
        except ValidationError:
            return None

    def is_bot_marked_not_in_group(self) -> bool:
        """最近一次结论是不是「Bot 不在这个群里」

        只读标记、不查协议端：宿主替插件做的推送（节日祝福、定时提醒、插件后台推送）
        在开回合、发消息之前用它挡掉注定被 QQ 拒绝的频道。标记缺失或内容不合法时按「在群」
        处理——标记只在有确切证据时才写，读不出来不能当成证据。
        """
        info = self.get_bot_membership_info()
        return info is not None and info.status is BotMembershipStatus.NOT_IN_GROUP

    async def record_bot_membership(
        self,
        status: BotMembershipStatus,
        source: BotMembershipSource,
        *,
        account_id: str = "",
        now: Optional[int] = None,
    ) -> Optional[BotMembershipStatus]:
        """记录一次「Bot 在不在群」的结论，返回写入前的状态（没有记录时为 None）

        调用方据返回值统计「新标」与「清除」。

        只写 `data` 一列，**刻意不把 `update_time` 放进 `update_fields`**：核对每轮都会重写
        所有群频道的标记，如果顺手刷新 `update_time`，频道目录（按它排序）就会每 6 小时整体
        跳一次，把真正的活跃时间淹掉。这个标记不是活跃信号。

        不动 `is_active` / `observe_mode`，也不碰 `auto_disable`：核对只做标记，
        停用与启用始终由人决定。
        """
        previous = self.get_bot_membership_info()
        info = BotMembershipInfo(
            status=status,
            checked_at=now if now is not None else int(time.time()),
            source=source,
            account_id=account_id,
        )
        data = self._load_data()
        data[CHANNEL_DATA_BOT_MEMBERSHIP_KEY] = info.model_dump(mode="json")
        self.data = json.dumps(data, ensure_ascii=False)
        await self.save(update_fields=["data"])
        return previous.status if previous is not None else None

    async def clear_bot_not_in_group(self, source: BotMembershipSource = BotMembershipSource.INBOUND) -> bool:
        """入站事件证明 Bot 还在群里：把「Bot 不在群」标记改回「在群」

        只有当前标记是 `NOT_IN_GROUP` 时才写库并返回 True。它在每条入站群消息上都会被调用，
        所以没有标记、标记已经是 `IN_GROUP`、或 `data` 坏掉时必须零写库。

        `source` 由调用方给：入站消息与 Bot 自己入群的通知都算「入站证据」，
        与定期核对的结论分开记录，后台能看出这个结论是怎么来的。
        """
        info = self.get_bot_membership_info()
        if info is None or info.status is not BotMembershipStatus.NOT_IN_GROUP:
            return False
        data = self._load_data()
        data[CHANNEL_DATA_BOT_MEMBERSHIP_KEY] = BotMembershipInfo(
            status=BotMembershipStatus.IN_GROUP,
            checked_at=int(time.time()),
            source=source,
        ).model_dump(mode="json")
        self.data = json.dumps(data, ensure_ascii=False)
        await self.save(update_fields=["data"])
        return True

    async def set_active(self, is_active: bool):
        """设置频道是否激活

        这是人的明确操作，保存前清掉自动停用标记：手动启用或手动停用之后，
        这个频道就不再属于「自动停用」，条件消失时系统也不该再改回来。
        """
        self.is_active = is_active
        if not is_active:
            self.observe_mode = False
        self._clear_auto_disable_marker()
        await self.save()

    @property
    def channel_status(self) -> ChannelStatus:
        """获取频道状态"""
        if not self.is_active:
            return ChannelStatus.DISABLED
        if self.observe_mode:
            return ChannelStatus.OBSERVE
        return ChannelStatus.ACTIVE

    async def set_channel_status(self, status: ChannelStatus | str):
        """设置频道状态

        这是人的明确操作，保存前清掉自动停用标记：手动启用或手动停用之后，
        这个频道就不再属于「自动停用」，条件消失时系统也不该再改回来。

        Args:
            status: ChannelStatus 枚举值或字符串
        """
        status = ChannelStatus(status)
        if status == ChannelStatus.ACTIVE:
            self.is_active = True
            self.observe_mode = False
        elif status == ChannelStatus.OBSERVE:
            self.is_active = True
            self.observe_mode = True
        elif status == ChannelStatus.DISABLED:
            self.is_active = False
            self.observe_mode = False
        else:
            raise ValueError(f"无效的频道状态: {status}")
        self._clear_auto_disable_marker()
        await self.save()

    @property
    def chat_type(self) -> ChatType:
        """获取聊天频道类型"""
        try:
            return ChatType(self.channel_type)
        except ValueError as e:
            logger.error(f"获取聊天频道类型失败: {e!s}")
            return ChatType.UNKNOWN

    async def get_preset(self) -> Union[DBPreset, DefaultPreset]:
        """获取人设

        回退链：频道 → 所属实例的默认人设 → 系统默认人设 → 内置人设。
        实例层让不同实例可以有各自的默认人格，而不必给每个频道单独设置。
        """
        # 先尝试频道自身的 preset_id
        preset = await DBPreset.get_or_none(id=self.preset_id)
        if preset:
            return preset
        # 再尝试所属实例的默认人设
        instance_preset = await self._get_instance_default_preset()
        if instance_preset:
            return instance_preset
        # 再尝试系统默认人设 ID
        if config.AI_CHAT_DEFAULT_PRESET_ID is not None:
            default_preset = await DBPreset.get_or_none(id=config.AI_CHAT_DEFAULT_PRESET_ID)
            if default_preset:
                return default_preset
        # 最终回退到内置默认人设
        from nekro_agent.services.preset_service import DEFAULT_PRESET_CONTENT, DEFAULT_PRESET_NAME

        return DefaultPreset(name=DEFAULT_PRESET_NAME, content=DEFAULT_PRESET_CONTENT)

    async def get_effective_preset_id(self) -> int:
        """获取本频道**当前生效**的人设标识，供隔离键的"人设"维使用

        Returns:
            生效人设的行 ID；回落到内置默认人设（库里没有这一行）时返回
            `identity_scope.PRESET_NONE`（0，"无人设档"）。

        判据刻意**复用 :meth:`get_preset` 的那条回退链**（频道 → 实例默认 → 系统默认 → 内置），
        而不是直接读 `self.preset_id`：

        - 「换了个人设就不会记得以前的恩怨情仇」说的是**实际在说话的那个人格**。
          频道没绑人设、但实例配了默认人设时，用户面对的就是那个人格，
          它与显式绑同一个人设的频道应当是同一份好感与记忆。
        - 只读 `self.preset_id` 会让"把实例默认人设显式绑到频道上"这个**纯运维动作**
          把该频道的数据整体切走一份，而运营完全没有换人设的意图。

        代价是本方法比读一个字段贵（最多三次主键点查），因此热路径上应当每轮只调一次、
        把结果传给解析器（`identity_scope.resolve_identity_scope(..., preset_id=...)`）。

        人设行被删除时回退链会继续往下走，于是新数据落到下一档人设上，
        **挂在被删人设下的旧数据原地保留成孤儿**（后台可见、可手动清理，不级联删）——
        这正是"人设可能是误删"要的语义。
        """
        preset = await self.get_preset()
        preset_id = getattr(preset, "id", None)
        return preset_id if isinstance(preset_id, int) and preset_id > 0 else 0

    @property
    def adapter(self) -> "BaseAdapter":
        """获取适配器"""
        return adapter_utils.get_adapter(self.adapter_key)

    async def _get_instance_default_preset(self) -> Optional[DBPreset]:
        """获取所属实例配置的默认人设

        读元数据那一段已提到模块级的 `get_instance_default_preset`（节日邮件也要用，
        它手上只有实例对、没有频道）。这里只负责「本频道没有实例段」那一档。

        Returns:
            实例默认人设；未配置实例、未设默认人设或人设已删除时返回 None
        """
        return await get_instance_default_preset(self.adapter_key, self.instance_key)

    async def get_effective_config(self) -> "CoreConfig":
        """获取本频道的有效配置（四层合并：会话 > 实例 > 适配器 > 全局）

        缓存带版本号：`ConfigResolver` 在任何一层覆盖配置被写入时自增版本号，
        版本不一致即丢弃重算。没有这个机制的话，`_effective_config` 是无 TTL 的实例属性缓存，
        改完配置后同一对象生命周期内会一直读到旧值，表现为"改了配置不生效"。

        显式传入 `instance_key` 可省掉解析器内部的反查查询——本对象已经持有该字段。
        """
        version = config_resolver.current_version()
        if self._effective_config is None or self._effective_config_version != version:
            self._effective_config = await config_resolver.get_effective_config(
                self.chat_key,
                instance_key=self.instance_key,
            )
            self._effective_config_version = version
        return self._effective_config

    def invalidate_effective_config(self) -> None:
        """使本对象缓存的有效配置立即失效

        版本号机制已覆盖"别处写入配置"的场景；本方法用于调用方明确知道需要重算的时刻。
        """
        self._effective_config = None

    async def set_preset(self, preset_id: Optional[int] = None) -> str:
        """设置聊天频道人设

        Args:
            preset_id: 人设ID，传入None则使用默认人设

        Returns:
            str: 设置成功的消息，包含当前人设名称

        Raises:
            ValueError: 目标人设对本频道所属实例不可见
        """
        # 设置人设ID，None需要作为null处理
        if preset_id is None or preset_id == -1:
            self.preset_id = None  # type: ignore  # 在数据库模型中允许为null
        else:
            # 可见性校验放在模型层而不是路由层：设置人设有 WebUI、聊天命令等多个入口，
            # 逐个入口加校验迟早会漏。人设归属规则见 DBPreset.owner_instance_key
            preset = await DBPreset.get_or_none(id=preset_id)
            if preset is not None and self.instance_key and preset.owner_instance_key:
                if preset.owner_instance_key != self.instance_key:
                    raise ValueError(f"人设 {preset_id} 属于其他实例，无法在本频道使用")
            self.preset_id = preset_id
        await self.save()

        # 获取人设信息
        preset = await self.get_preset()
        preset_name = preset.name if hasattr(preset, "name") else "默认人设"

        return f"设置成功，当前人设: {preset_name}"


# channel_name / chat_key 等文本列在 PG 上不收 NUL；写入前统一剔除
pre_save(DBChatChannel)(strip_nul_before_save)


async def is_chat_key_marked_bot_not_in_group(chat_key: str) -> bool:
    """按 `chat_key` 查频道行，判它是不是被标成了「Bot 不在群」

    给手上只有 `chat_key` 的推送入口用（调度回合、插件推送）。判据只有
    :meth:`DBChatChannel.is_bot_marked_not_in_group` 一处。

    频道行不存在、查库出错一律按「在群」返回 False：这道判断只是为了少撞一次注定失败的发送，
    它自己出错时不能把正常频道的推送也挡掉。
    """
    if not chat_key:
        return False
    try:
        channel = await DBChatChannel.filter(chat_key=chat_key).order_by("id").first()
    except Exception as e:  # noqa: BLE001 - 判定失败一律放行，不能让这道判断自己变成故障点
        logger.warning(f"读取「Bot 在不在群」标记失败，按在群处理: chat_key={chat_key} | {e}")
        return False
    return channel is not None and channel.is_bot_marked_not_in_group()
