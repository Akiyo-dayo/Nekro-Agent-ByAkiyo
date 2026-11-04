"""总后台实例管理业务逻辑

## 寻址方式

Console 的 API 只用 `instance_key` 定位实例（见 `nekro_agent/schemas/console.py` 的三层模型），
不携带 `adapter_key`。因此本模块把 `instance_key` 当作**全局**唯一标识：创建时跨适配器校验冲突，
查询时直接按 `instance_key` 命中。数据库层的 `unique_together` 仍是
`(adapter_key, instance_key)`，本模块的全局校验是它之上更严格的一层约束。

## 为什么 `instance_key` 不可变

它编码在该实例所有 `chat_key` 里（见 `adapters/onebot_v11/core/channel.py`），
同时也是 `chat_channel` / `chat_message` / `mem_*` 的分区列。改动它等于让该实例的
全部会话、消息与记忆一次性变成孤儿数据，因此 PATCH 不接受该字段，创建后永久固定。

## 账号绑定的唯一性

一个实例 = 一个 Bot 账号，反向也必须成立。`provider_account_id` 是入站消息定位实例的唯一依据
（`adapters/onebot_v11/core/instance.py` 的 `resolve_instance_key`），两个实例绑同一个账号时
这条查询就没有正确答案。创建与更新因此拒绝把一个已被占用的账号绑给第二个实例，
判据统一在 `DBAdapterInstance.find_account_binding_conflict`。

这条唯一性目前**只有代码层没有数据库约束**（原因见 `models/db_adapter_instance.py` 的模块文档），
所以还需要 `unbind_duplicate_account_bindings` 在启动期收拾存量与残余竞争窗口留下的重复绑定。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import secrets
from datetime import datetime
from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple

import aiodocker
from qdrant_client import models as qdrant_models
from tortoise import Tortoise
from tortoise.functions import Count, Max
from tortoise.timezone import now as db_now
from tortoise.transactions import in_transaction

from nekro_agent.adapters.onebot_v11.core.channel import is_valid_instance_key
from nekro_agent.adapters.onebot_v11.core.instance import clear_instance_cache
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.vector_db import get_qdrant_client
from nekro_agent.models.db_adapter_instance import TOMBSTONE_STATUS, DBAdapterInstance
from nekro_agent.models.db_adapter_instance_event import DBAdapterInstanceEvent
from nekro_agent.models.db_adapter_instance_session import DBAdapterInstanceSession
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_exec_code import DBExecCode
from nekro_agent.models.db_instance_group import DBInstanceDomainBinding
from nekro_agent.models.db_mem_entity import DBMemEntity
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.models.db_plugin_data import DBPluginData
from nekro_agent.models.db_recurring_timer_job import DBRecurringTimerJob
from nekro_agent.schemas.console import (
    ConsoleOverview,
    ContainerInfo,
    ContainerState,
    InstanceCreateRequest,
    InstanceInfo,
    InstanceListResponse,
    InstanceOption,
    InstanceProvider,
    InstanceStats,
    InstanceStatus,
    InstanceUpdateRequest,
)
from nekro_agent.schemas.errors import AppError, ConflictError, NotFoundError, ValidationError
from nekro_agent.schemas.i18n import SupportedLang
from nekro_agent.services.adapter_instance_service import adapter_instance_service
from nekro_agent.services.chat_key_migration import InstanceMigrationOutcome, migrate_instance_ownership
from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager
from nekro_agent.services.user.token_exposure import record_account_landing_on_tenant, record_instances_exposure

logger = get_sub_logger("console.instance")

# 内置 qbob 实例接入的 AppID 存在元数据里（qbob_manager 也用它）；这里只在实例概要里带出
# 这一个非敏感字段，前端删除确认框据此判断「bot 还接着、但账号可能已被清空」
METADATA_QBOB_APPID = "qbob_appid"

DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# `DBAdapterInstance.status` 的原始取值。它是自由字符串（历史遗留），
# 这里集中声明本模块会主动写入的几个，避免字面量散落各处。
STATUS_OFFLINE = "offline"
STATUS_CONNECTING = "connecting"
STATUS_DISABLED = "disabled"
STATUS_ARCHIVED = "archived"
STATUS_UNCLAIMED = "unclaimed"
# 「实例建好了，但从来没绑过账号」。与 offline 分开是本轮修复的核心：
# 一个从未绑定账号的实例不可能有连接，把它显示成"离线"等于告诉管理员"你的 Bot 掉线了"，
# 而真正的下一步是去认领一个已经连进来的协议端、或手工填上账号。
STATUS_UNBOUND = "unbound"
# `AdapterInstanceService.soft_delete_instance` 留下的墓碑状态。归档（archived）是
# 用户可见的只读保留态，墓碑则是旧软删流程的产物，不应出现在总后台列表里。
# 与模型层同一个取值：账号占用判定也要排除墓碑，两处一旦漂移就会出现「列表里看不见、
# 却挡着账号不让改绑」的死路。
STATUS_TOMBSTONE = TOMBSTONE_STATUS

_STATUS_MAP: Dict[str, InstanceStatus] = {
    "online": InstanceStatus.ONLINE,
    "connected": InstanceStatus.ONLINE,
    STATUS_CONNECTING: InstanceStatus.CONNECTING,
    STATUS_OFFLINE: InstanceStatus.OFFLINE,
    "login_required": InstanceStatus.LOGIN_REQUIRED,
    "error": InstanceStatus.ERROR,
    STATUS_UNCLAIMED: InstanceStatus.UNCLAIMED,
    STATUS_UNBOUND: InstanceStatus.UNBOUND,
    STATUS_DISABLED: InstanceStatus.DISABLED,
    STATUS_ARCHIVED: InstanceStatus.ARCHIVED,
    # 旧软删墓碑对外等价于归档
    STATUS_TOMBSTONE: InstanceStatus.ARCHIVED,
}

# 未绑定账号时会被改写成 `unbound` 的原始状态。
#
# 这几个取值描述的都是「此刻这条连接怎么样」，而没有账号就没有连接，它们对该实例毫无意义。
# **刻意不含** `login_required` 与 `wechat_ilink_multi` 的 `binding` / `session_expired` /
# `renewing`：那几个同样发生在「还没有账号」的阶段，但各自带着更精确的下一步动作
# （去扫码 / 去重新绑定），改写成 unbound 是信息损失。也不含 `disabled` / `archived` /
# 墓碑：那是管理员的意图，优先级高于连接事实。
UNBOUND_REWRITABLE_STATUSES: frozenset[str] = frozenset(
    {"pending", "online", "connected", STATUS_CONNECTING, STATUS_OFFLINE, "error"},
)

# 「绑定关系一变就要重算」的状态集合，比上面多一个 `unbound` 本身：刚绑上账号的实例必须能从
# `unbound` 回到 `offline`，否则它会永远停在"未绑定"上、永远不进掉线监测。
# 存量改判用的是上面那个不含 `unbound` 的集合，否则每次改判都会给已经正确的行再写一条
# from==to 的审计事件。
IDLE_SWAPPABLE_STATUSES: frozenset[str] = UNBOUND_REWRITABLE_STATUSES | {STATUS_UNBOUND}

_PROVIDER_MAP: Dict[str, InstanceProvider] = {
    InstanceProvider.BUILTIN_NAPCAT.value: InstanceProvider.BUILTIN_NAPCAT,
    InstanceProvider.REMOTE_ONEBOT.value: InstanceProvider.REMOTE_ONEBOT,
    InstanceProvider.NATIVE.value: InstanceProvider.NATIVE,
    InstanceProvider.BUILTIN_QBOB.value: InstanceProvider.BUILTIN_QBOB,
    # 协议端自行连入时由 onebot_v11 适配器写入的取值
    "onebot_v11": InstanceProvider.REMOTE_ONEBOT,
}

# 元数据里由本模块与容器/健康监测模块共享的键
METADATA_ACCESS_TOKEN = "access_token"
METADATA_OFFLINE_SINCE = "offline_since"
# 存量修复解绑重复账号时留下的审计痕迹
METADATA_UNBOUND_ACCOUNT_CONFLICT = "unbound_account_conflict"


def map_status(raw_status: str) -> InstanceStatus:
    """把数据库里的自由状态字符串映射为对外枚举，无法识别时回落 OFFLINE"""
    return _STATUS_MAP.get(raw_status.strip().lower(), InstanceStatus.OFFLINE)


def map_provider(raw_provider: str) -> InstanceProvider:
    """把数据库里的自由 provider 字符串映射为对外枚举，无法识别时回落远程协议端"""
    return _PROVIDER_MAP.get(raw_provider.strip().lower(), InstanceProvider.REMOTE_ONEBOT)


def account_binding_required(raw_provider: str) -> bool:
    """该协议端形态是否靠「账号绑定」才拿得到连接

    只有这类实例才谈得上「未绑定账号」：OneBot 系（内置 NapCat / 外部协议端）与
    `wechat_ilink_multi` 这样的多账号适配器，连接是按 `provider_account_id` 定位的
    （见 `adapters/onebot_v11/core/instance.py` 的 `resolve_account_id`）。

    **原生适配器（`native`）不算**：email / minecraft / sse 这类整个适配器只有一条连接，
    连接从适配器自身的配置来，`provider_account_id` 对它们本来就恒为空。把它们判成
    「未绑定账号」会给出一句完全不适用的引导，还会把它们从掉线监测里摘出去——
    那是行为回退，不是修复。
    """
    return map_provider(raw_provider) is not InstanceProvider.NATIVE


def resolve_instance_status(raw_status: str, provider: str, provider_account_id: str) -> InstanceStatus:
    """解析对外状态，并把「从未绑定账号」从「掉线」里分出来

    数据库里的状态由 `create_instance` / `update_instance` / 启动期的
    `normalize_unbound_instance_status()` 负责写成 `unbound`，本函数是**读侧的兜底**：
    库里仍是 `offline` 的存量行、以及别的模块直接改状态留下的行，在界面上也要立刻显示对，
    而不是等下一次重启。两条路径同一判据（账号为空 + 原始状态可改写 + 形态需要账号绑定）。
    """
    mapped = map_status(raw_status)
    if provider_account_id.strip():
        # 绑着账号却挂着 unbound 是一行自相矛盾的数据（适配器自己的绑定流程直接写账号列时会出现），
        # 对外按"绑好了、还没连上"呈现
        return InstanceStatus.OFFLINE if mapped is InstanceStatus.UNBOUND else mapped
    if not account_binding_required(provider):
        return mapped
    if raw_status.strip().lower() not in UNBOUND_REWRITABLE_STATUSES:
        return mapped
    return InstanceStatus.UNBOUND


def idle_status_for(instance: DBAdapterInstance) -> str:
    """实例「当前没有连接」时该落到的状态

    绑了账号是 `offline`（连接断了，去看协议端），没绑账号是 `unbound`（还没接上，去认领）。
    创建、启用、改绑账号三条路径共用它，避免三处各写一遍判据后逐渐分叉。
    """
    if instance.provider_account_id.strip() or not account_binding_required(instance.provider):
        return STATUS_OFFLINE
    return STATUS_UNBOUND


def load_metadata(instance: DBAdapterInstance) -> Dict[str, Any]:
    """读取实例元数据；损坏或非对象的内容按空字典处理，避免单条脏数据打挂整个列表"""
    if not instance.metadata_json:
        return {}
    try:
        parsed = json.loads(instance.metadata_json)
    except (ValueError, TypeError):
        logger.warning(f"实例元数据解析失败，按空处理: instance_key={instance.instance_key}")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def dump_metadata(metadata: Dict[str, Any]) -> str:
    return json.dumps(metadata, ensure_ascii=False)


async def collect_instance_stats(instance_keys: List[str]) -> Dict[str, InstanceStats]:
    """一次性聚合出所有实例的运行统计

    固定三条聚合查询（会话数 / 今日消息数 / 最近活跃时间），与实例数量无关，
    避免列表接口按实例逐个查询退化成 N+1。

    Args:
        instance_keys: 需要统计的实例标识列表

    Returns:
        instance_key -> InstanceStats，未产生任何数据的实例也会有零值条目
    """
    stats: Dict[str, InstanceStats] = {key: InstanceStats() for key in instance_keys}
    if not instance_keys:
        return stats

    today_start = db_now().replace(hour=0, minute=0, second=0, microsecond=0)

    channel_rows, message_rows, active_rows = await asyncio.gather(
        DBChatChannel.filter(instance_key__in=instance_keys)
        .annotate(channel_count=Count("id"))
        .group_by("instance_key")
        .values("instance_key", "channel_count"),
        DBChatMessage.filter(instance_key__in=instance_keys, create_time__gte=today_start)
        .annotate(message_count=Count("id"))
        .group_by("instance_key")
        .values("instance_key", "message_count"),
        DBChatMessage.filter(instance_key__in=instance_keys)
        .annotate(last_active=Max("create_time"))
        .group_by("instance_key")
        .values("instance_key", "last_active"),
    )

    for row in channel_rows:
        target = stats.get(str(row["instance_key"]))
        if target is not None:
            target.channel_count = int(row["channel_count"] or 0)

    for row in message_rows:
        target = stats.get(str(row["instance_key"]))
        if target is not None:
            target.message_count_today = int(row["message_count"] or 0)

    for row in active_rows:
        target = stats.get(str(row["instance_key"]))
        last_active = row["last_active"]
        if target is not None and isinstance(last_active, datetime):
            target.last_active_at = last_active.strftime(DATETIME_FORMAT)

    return stats


async def resolve_container_info(instance: DBAdapterInstance) -> Optional[ContainerInfo]:
    """查询实例背后的协议端容器信息

    容器托管由并行开发的 `services/console/container_manager` 提供，尚未落地时按"无容器"处理，
    使实例 CRUD 与容器能力可以彼此独立地上线。仅内置 NapCat 形态才有容器，
    远程协议端不必为此付出一次 Docker 往返。
    """
    if map_provider(instance.provider) is not InstanceProvider.BUILTIN_NAPCAT:
        return None

    try:
        from nekro_agent.services.console.container_manager import get_container_info
    except ImportError:
        return None

    result: Any = get_container_info(instance)
    if inspect.isawaitable(result):
        result = await result
    return result if isinstance(result, ContainerInfo) else None


async def build_instance_info(instance: DBAdapterInstance, stats: InstanceStats) -> InstanceInfo:
    """把实例行组装为对外契约对象"""
    metadata = load_metadata(instance)
    offline_since = metadata.get(METADATA_OFFLINE_SINCE)

    return InstanceInfo(
        instance_key=instance.instance_key,
        display_name=instance.display_name,
        adapter_key=instance.adapter_key,
        provider=map_provider(instance.provider),
        provider_account_id=instance.provider_account_id,
        status=resolve_instance_status(instance.status, instance.provider, instance.provider_account_id),
        enabled=instance.enabled,
        is_default=instance.is_default,
        # 必须原样下发库里的归属。这里一度硬编码成 None（多租户尚未落地时的占位），
        # 结果是「划归租户后回到界面又显示未归属」：绑定确实写进了库，但列表接口把它抹平了，
        # 前端据此算出的勾选集合恒为空，再点一次保存就会按整表覆盖语义把已有归属全部解绑。
        tenant_id=instance.tenant_id,
        container=await resolve_container_info(instance),
        stats=stats,
        last_error=instance.last_error,
        offline_since=offline_since if isinstance(offline_since, str) else None,
        qbob_appid=str(metadata.get(METADATA_QBOB_APPID) or ""),
    )


async def list_instances() -> InstanceListResponse:
    """列出所有实例（不含旧软删墓碑）"""
    instances = await DBAdapterInstance.exclude(status=STATUS_TOMBSTONE).order_by("id")
    stats = await collect_instance_stats([instance.instance_key for instance in instances])

    items = await asyncio.gather(
        *[build_instance_info(instance, stats.get(instance.instance_key, InstanceStats())) for instance in instances],
    )
    return InstanceListResponse(items=list(items), total=len(items))


async def list_instance_options() -> List[InstanceOption]:
    """列出实例作用域切换器的候选实例（不含旧软删墓碑）

    与 `list_instances()` 的差别只有一处、但正是它存在的理由：**只取六列，不查容器、不做统计**。
    `list_instances()` 每个 `builtin_napcat` 实例要新建一个 Docker 客户端做一次 inspect，
    再加三条跨 `chat_message` 全表的聚合；切换器这两样都用不到，却因为共用同一个接口
    在每次打开后台时把它们全付一遍。

    排序与 `list_instances()` 一致（按 `id`，即创建顺序），呈现顺序由前端按「当前 / 常用 /
    需处理 / 名称」重排，后端只保证稳定与确定。
    """
    rows = (
        await DBAdapterInstance.exclude(status=STATUS_TOMBSTONE)
        .order_by("id")
        .values("instance_key", "display_name", "adapter_key", "provider", "provider_account_id", "status", "enabled")
    )
    return [
        InstanceOption(
            instance_key=str(row["instance_key"]),
            display_name=str(row["display_name"] or ""),
            adapter_key=str(row["adapter_key"]),
            provider_account_id=str(row["provider_account_id"] or ""),
            # 与 `build_instance_info` 同一判据：切换器里也要能看出「这个实例根本没绑账号」，
            # 否则同一个实例在列表页显示"未绑定账号"、在切换器里显示"离线"
            status=resolve_instance_status(
                str(row["status"] or ""),
                str(row["provider"] or ""),
                str(row["provider_account_id"] or ""),
            ),
            enabled=bool(row["enabled"]),
        )
        for row in rows
    ]


async def get_instance(instance_key: str) -> DBAdapterInstance:
    """按实例标识取实例行，不存在时抛 404"""
    instance = await DBAdapterInstance.filter(instance_key=instance_key).first()
    if instance is None:
        raise NotFoundError(resource=f"实例 '{instance_key}'")
    return instance


async def describe_instance(instance: DBAdapterInstance) -> InstanceInfo:
    """为单个实例组装详情"""
    stats = await collect_instance_stats([instance.instance_key])
    return await build_instance_info(instance, stats.get(instance.instance_key, InstanceStats()))


async def generate_instance_key() -> str:
    """生成一个未被占用的实例标识，形如 inst1 / inst2"""
    taken = set(await DBAdapterInstance.all().values_list("instance_key", flat=True))
    index = 1
    while True:
        candidate = f"inst{index}"
        if candidate not in taken and is_valid_instance_key(candidate):
            return candidate
        index += 1


async def assert_account_unbound(adapter_key: str, account_id: str, exclude_id: Optional[int] = None) -> None:
    """账号已被同一适配器下的其他实例绑定时拒绝，并指出该去认领谁

    协议端在管理员建实例之前先连上是常态，此时它已被自动登记成一个 `unclaimed` 实例并绑好账号
    （见 `resolve_instance_key`）。管理员接着新建实例又填同一个账号，就会得到两行绑同一账号的实例，
    入站路由随即失去唯一答案。**未认领实例照样算占用**——它正是这条路径最常见的占用方，
    放行它等于把这个缺陷原样保留；认领流程本身不受影响，认领改的是既有那一行的状态，不新增行。
    """
    holder = await DBAdapterInstance.find_account_binding_conflict(adapter_key, account_id, exclude_id=exclude_id)
    if holder is None:
        return
    hint = "请改为认领该实例" if map_status(holder.status) is InstanceStatus.UNCLAIMED else "请先为该实例解绑账号"
    raise ConflictError(resource=f"账号 {account_id}（已绑定到实例 '{holder.instance_key}'，{hint}）")


async def create_instance(body: InstanceCreateRequest) -> DBAdapterInstance:
    """新建实例

    `instance_key` 留空时自动生成；显式传入时必须满足 slug 约束且全局未被占用。

    ## 协议端 access token

    留空则自动生成一枚，写入元数据；`container_manager.resolve_access_token` 据此
    下发到协议端配置，**握手时也按它校验**（`adapters/onebot_v11/core/access_token.py`）。
    改造前只生成、不校验，两头对不上，见那个模块的文档。

    ⚠️ 生成在**这里**、而不是在自动登记那条路径上（`adapters/onebot_v11/core/instance.py`
    的 `_register_unclaimed_instance`），是有意的不对称：这里建的实例的协议端配置由 NA
    下发（内置 NapCat）或由平台方从总后台抄走（远程协议端），令牌推得下去；
    而自动登记的实例是用户自己在别处架好、已经连上来的协议端，NA 碰不到它的配置——
    给它凭空生成一枚令牌，下一次重连就会被自己拒掉。那条路径刻意不写这个键，
    于是 `resolve_access_token` 退回部署级令牌，行为与改造前逐字相同。
    """
    adapter_key = body.adapter_key.strip()
    if not adapter_key:
        raise ValidationError(reason="适配器标识不能为空")

    instance_key = body.instance_key.strip().lower()
    if instance_key:
        if not is_valid_instance_key(instance_key):
            raise ValidationError(reason="实例标识仅允许小写字母、数字与下划线，且长度不超过 32")
        if await DBAdapterInstance.filter(instance_key=instance_key).exists():
            raise ConflictError(resource=f"实例 '{instance_key}'")
    else:
        instance_key = await generate_instance_key()

    account_id = body.provider_account_id.strip()
    await assert_account_unbound(adapter_key, account_id)

    # 令牌现在是真的会被校验的凭据，因此调用方传进来的值要按凭据对待：
    # 去掉首尾空白（抄粘时最常见的污染，且它会让"抄过去的"与"存下来的"不等长而静默握手失败），
    # 空白串等同于留空、自动生成一枚。
    metadata: Dict[str, Any] = {
        METADATA_ACCESS_TOKEN: body.access_token.strip() or secrets.token_urlsafe(24),
    }

    instance = await DBAdapterInstance.create(
        adapter_key=adapter_key,
        instance_key=instance_key,
        display_name=body.display_name.strip() or instance_key,
        provider=body.provider.value,
        provider_account_id=account_id,
        # 没填账号的新实例落 `unbound` 而不是 `offline`：它从来没上线过，"离线"是个假消息，
        # 而这条假消息正是「我建了实例、连了 napcat，它却一直说离线」的来源
        status=STATUS_UNBOUND if not account_id and account_binding_required(body.provider.value) else STATUS_OFFLINE,
        enabled=True,
        metadata_json=dump_metadata(metadata),
    )
    logger.info(f"创建实例: adapter_key={adapter_key}, instance_key={instance_key}, provider={body.provider.value}")

    if instance.provider_account_id:
        # 账号绑定关系变了就必须让路由缓存失效，否则消息会继续按旧映射投递
        clear_instance_cache()
    return instance


async def update_instance(instance: DBAdapterInstance, body: InstanceUpdateRequest) -> DBAdapterInstance:
    """更新实例的可变属性

    `instance_key` 与 `adapter_key` 不在可变集合里（见模块文档）。

    只写本次改了的列：`instance` 是请求开头读出的，整行写回会把这之间别处改的列（租户改划、连接写的状态）冲回旧值。
    """
    edited: List[str] = []
    if body.display_name is not None:
        instance.display_name = body.display_name.strip()
        edited.append("display_name")

    account_changed = False
    if body.provider_account_id is not None:
        normalized = body.provider_account_id.strip()
        account_changed = normalized != instance.provider_account_id
        if (
            account_changed
            and map_provider(instance.provider) is InstanceProvider.BUILTIN_QBOB
            and str(load_metadata(instance).get(METADATA_QBOB_APPID) or "")
        ):
            # qbob 实例的账号由接入流程按 bot 的 self_id 写入：从这里改 / 清会让 bot 重连时
            # 落到「未认领实例」，必须走协议端页的解绑，让 qbob 那边的 bot 一起删掉
            # ConflictError 的文案是「{resource}已存在」，所以按「主体（说明）」的写法
            raise ConflictError(resource="本实例在 qbob 里接入的 bot（请先在协议端页解绑 qbob，再改账号）")
        if account_changed:
            await assert_account_unbound(instance.adapter_key, normalized, exclude_id=instance.id)
            # 改绑到租户实例上：这个账号的协议端此前出示过的令牌（比如它待认领时凭的部署级令牌）此刻在租户手里
            await record_account_landing_on_tenant(instance, normalized, reason=f"账号改绑到租户实例 {instance.instance_key}")
        instance.provider_account_id = normalized
        edited.append("provider_account_id")

    if body.is_default is not None:
        instance.is_default = body.is_default
        edited.append("is_default")
        if body.is_default:
            # 默认实例在同一适配器下唯一
            await DBAdapterInstance.filter(adapter_key=instance.adapter_key).exclude(id=instance.id).update(
                is_default=False,
            )

    if edited:
        await instance.save(update_fields=[*edited, "update_time"])
    if body.enabled is not None and body.enabled != instance.enabled:
        instance = await set_instance_enabled(instance, enabled=body.enabled)

    if account_changed:
        clear_instance_cache()
        logger.info(f"实例账号绑定变更，已清空路由缓存: instance_key={instance.instance_key}")
        # 绑定关系一变，「有没有连接可谈」这件事就变了：刚解绑的实例不该继续显示成掉线并被探活告警，
        # 刚绑上账号的实例也不该停在 unbound。落库而不是只靠读侧推导，掉线监测才看得见。
        instance = await sync_account_binding_status(instance)

    return instance


async def sync_account_binding_status(instance: DBAdapterInstance) -> DBAdapterInstance:
    """账号绑定变化后把状态对齐到 `unbound` / `offline`

    只改「描述连接现状」的那几个状态（`UNBOUND_REWRITABLE_STATUSES`）。管理员的停用/归档意图、
    以及适配器自己写的 `login_required` / `binding` 这类更精确的诊断一律不动。

    上面的判断看的是调用方手里的对象，只决定「要不要写」；写入走 `set_runtime_status` 的条件更新，
    读出之后实例若被停用 / 归档 / 软删（内置 NapCat 回写账号时由容器管理自动调用，窗口在别人手里），这次就不写。
    """
    desired = idle_status_for(instance)
    if instance.status == desired or instance.status.strip().lower() not in IDLE_SWAPPABLE_STATUSES:
        return instance
    message = "实例已解绑账号，暂无可用连接" if desired == STATUS_UNBOUND else "实例已绑定账号，等待协议端连接"
    return await adapter_instance_service.set_runtime_status(instance, desired, message=message)


async def set_instance_enabled(instance: DBAdapterInstance, enabled: bool) -> DBAdapterInstance:
    """启用 / 停用实例

    停用统一落到 `disabled` 状态；启用只在实例处于停用、归档或墓碑态时把状态拉回 `offline`，
    其余情况保留当前状态，交由掉线监测按真实连接情况刷新，避免把一个在线实例误标成离线。

    ## 这两个字段在哪里被执行

    `enabled` 曾经**只是后台的一个标记**：入站链路唯一的闸门是租户维度的，没有实例判据，
    于是停用之后 Bot 照常收消息、照常跑 Agent、照常回复。现在三处闸门共用
    `DBAdapterInstance.is_instance_active`：

    | 位置 | 行为 |
    |---|---|
    | `adapters/interface/collector.py` | 入站消息静默丢弃（早于建频道行、早于命令识别） |
    | `services/message_service.py::push_human_message` / `push_system_message` | 通知事件、定时任务、沙盒回执这些绕开 collector 的入站一律丢弃；同时挡住消息落库、插件挂载点，以及配额用尽时那条直调 `forward_message` 的通报 |
    | `services/message_service.py::schedule_agent_task` | 不调度 Agent（挡住直接调进来的定时任务） |
    | `services/chat/universal_chat_service.py::send_agent_message` | 出站显式失败（`InstanceDisabledError`） |

    ## 为什么是"连着但不理"而不是断连

    停用不动 WebSocket 连接。断连会把"启用后恢复"的控制权交给协议端的重连策略——
    NA 只能拒绝连接，无法要求对方回来；远端 OneBot 不重试时启用就变成一句空话。
    而且断连只对 OneBot 这类长连接适配器成立，webhook / 轮询形态的适配器根本没有可断的连接，
    "停用"会因适配器而异。判据留在应用层则对所有适配器同构，且**立即**生效。

    ## 热生效

    判据每次现查数据库、不带缓存，因此本函数返回即生效，不需要重启进程。
    也**不需要** `clear_instance_cache()`：那个缓存装的是账号与实例的绑定关系，启停不改绑定。
    """
    if enabled and instance.status == STATUS_TOMBSTONE and instance.tenant_id is not None:
        # 复活租户名下的软删实例：建档完成后的启动扫描不看软删行，换过的部署级令牌可能就是在它躺着时换的，
        # 复活之后它此刻生效的令牌又到了租户手里。先记后改，记不下来就拒绝，什么都不动
        record_instances_exposure([instance], reason="复活租户实例", refuse_on_failure=True)
    instance.enabled = enabled
    if not enabled:
        return await adapter_instance_service.set_status(
            instance,
            STATUS_DISABLED,
            message="实例已被总后台停用",
            extra_fields=["enabled"],
        )

    if instance.status in (STATUS_DISABLED, STATUS_ARCHIVED, STATUS_TOMBSTONE):
        # 墓碑复活是一条隐蔽的**账号写入路径**：墓碑不算账号占用（见模型层文档），躺着的这段
        # 时间里账号可能已被别的实例合法绑走，直接复活就写出两行绑同一账号的活实例。
        # `set_status` 里有同一道守卫兜住所有调用点，这里**提前**调一次只为了顺序：
        # `idle_status_for` 读的是账号列，先复活后解绑会把一个刚被解绑的实例落成 `offline`，
        # 掉线监测随即为一个不可能有连接的实例告警 —— 那正是 `unbound` 当初要消灭的误导。
        #
        # **判据必须与 `set_status` 里那道守卫逐字相同，只认墓碑。** 停用与归档从来没有停止
        # 算账号占用（`find_account_binding_conflict` 只排除墓碑），所以它们身上出现冲突只可能是
        # 脏数据，而脏数据该由谁让位是 `unbind_duplicate_account_bindings` 的判据说了算——它保留
        # **最早那行**，也就是 `get_by_bound_account` 此刻正在路由到的那一行。放宽到这两个状态时
        # 实测的后果正相反：一行 `disabled` 的实例本来是账号的路由归属方，管理员点一下「启用」
        # 就把它自己的账号解掉、入站消息静默改判给另一行——启用一个实例反而让它彻底收不到消息。
        released = (
            await adapter_instance_service.release_account_if_taken(instance)
            if instance.status == STATUS_TOMBSTONE
            else ""
        )
        was_tombstone = instance.status == STATUS_TOMBSTONE
        # 没绑账号的实例启用后落 `unbound`：它启用与否都不可能有连接，落 offline 只会
        # 让掉线监测立刻开始为它计时并告警
        instance = await adapter_instance_service.set_status(
            instance,
            idle_status_for(instance),
            message=released or "实例已被总后台启用",
            extra_fields=["enabled"],
        )
        if released:
            # `set_status` 只为 error/deleted 保留 message，而"我启用的实例怎么没了账号"
            # 必须在总后台看得见——这条说明是唯一的现场线索
            instance.last_error = released
            await instance.save(update_fields=["last_error", "update_time"])
        if was_tombstone:
            # 复活等于把账号重新绑回这一行（上面可能刚解掉一个陈旧绑定），而它**不一定**清缓存：
            # `clear_instance_cache()` 只在改绑那一支被调。在线连接要按新绑定复核一次
            from nekro_agent.adapters.onebot_v11.core.access_token import schedule_live_connection_revalidation

            schedule_live_connection_revalidation()
        return instance

    await instance.save(update_fields=["enabled", "update_time"])
    return instance


async def claim_instance(instance: DBAdapterInstance, display_name: str) -> DBAdapterInstance:
    """认领一个协议端自行连入、尚未在总后台登记的实例（就地认领，标识不变）"""
    if map_status(instance.status) is not InstanceStatus.UNCLAIMED:
        raise ValidationError(reason="仅未认领状态的实例可以认领")

    name = display_name.strip()
    if not name:
        raise ValidationError(reason="显示名称不能为空")

    instance.display_name = name
    instance.enabled = True
    # 认领只是把实例登记到总后台，此刻的真实连接状态未知，交由掉线监测刷新
    claimed = await adapter_instance_service.set_status(
        instance,
        STATUS_CONNECTING,
        message=f"实例已被认领: {name}",
        extra_fields=["display_name", "enabled"],
    )
    # 账号没换行，但从「未认领」变成了「已认领」：在线连接的复核只判已认领实例，这一刻起它也归复核管。
    # 握手出示的令牌不是这个实例此刻应出示的那枚（它没有自己的令牌，就是部署级令牌）的连接会被断开——
    # 认领者以为是自己的协议端，其实可能是凭别家待绑实例的令牌抢先连上来的那条
    from nekro_agent.adapters.onebot_v11.core.access_token import schedule_live_connection_revalidation

    schedule_live_connection_revalidation()
    return claimed


# ==================== 认领流程：未认领协议端的两条出路 ====================


class InstanceAdoptOutcome(NamedTuple):
    """把一个未认领实例交给某个目标实例之后的结果

    `migration` 为 None 表示这次认领没有搬运任何数据（就地认领）。
    `account_moved` 为 False 且 `migration.failed_channels` 非空时是**可重试的半成品**：
    已搬走的频道留在目标实例，账号仍在源实例上，重跑同一次操作即可补齐剩下的。
    """

    target: DBAdapterInstance
    source_instance_key: str
    migration: Optional[InstanceMigrationOutcome]
    account_moved: bool
    source_archived: bool


async def assert_account_can_move_to(source: DBAdapterInstance, target: DBAdapterInstance) -> None:
    """校验源实例的账号能不能过户给目标实例

    单独抽出来是为了让「认领为新实例」能在**建行之前**就查一遍：否则账号冲突会在新实例已经
    建好之后才抛出来，留下一个谁都不认识的空实例。判据与 `assert_account_unbound` 同源
    （`find_account_binding_conflict`），只是这里要额外把源实例排除在外——它正是账号当前的持有者。
    """
    account = source.provider_account_id.strip()
    if not account:
        return
    if target.provider_account_id.strip() not in ("", account):
        raise ConflictError(
            resource=(
                f"实例 '{target.instance_key}'（已绑定账号 {target.provider_account_id}，"
                "一个实例只能绑一个账号，请先为它解绑）"
            ),
        )
    if target.provider_account_id.strip() == account:
        return
    # 除源实例与目标实例之外还有第三行占着这个账号：先修那个，否则搬完照样路由不出唯一答案
    holder = await DBAdapterInstance.find_account_binding_conflict(
        target.adapter_key,
        account,
        exclude_id=source.id,
    )
    if holder is not None and holder.id != target.id:
        raise ConflictError(resource=f"账号 {account}（已绑定到实例 '{holder.instance_key}'，请先为该实例解绑账号）")


def _warn_if_adoption_changes_the_expected_token(
    source: DBAdapterInstance,
    target: DBAdapterInstance,
    account: str,
) -> None:
    """账号过户会**换掉这个协议端握手时该出示的令牌**，这件事必须留下一条现场记录

    协议端接入令牌是按实例校验的（`adapters/onebot_v11/core/access_token.py`），期望值由
    账号所属的实例决定。而未认领实例按定义没有自己的令牌——它一直是靠部署级令牌（或压根
    不带令牌）连进来的。把它并进一个**有自己令牌**的实例，账号一过户，同一条连接握手时
    出示的令牌就不再是这个实例要求的那枚，**旧连接会被当场断开**（`clear_instance_cache`
    触发的那次复核）：远程协议端必须把目标实例的令牌抄进自己的配置才能重连，
    不抄就只会看到"认领完成之后 Bot 就掉线了"，而界面上这一步显示的是成功。

    内置 NapCat 目标不用人管（`resync_network_config` 会把令牌下发进那份配置）；
    远程协议端 NA 碰不到它的配置文件，只能由人收尾，两条出路见
    `docs/operations.md §1.4`。这里只负责让它在日志里可见——改变认领语义
    （拒绝、或顺手清掉目标实例的令牌）都是在替用户做一个更坏的决定。

    **一条提示不该有能力打断认领**：读元数据出任何岔子都咽掉。
    """
    if map_provider(target.provider) is InstanceProvider.BUILTIN_NAPCAT:
        return
    try:
        # 延迟导入：`container_manager` 反过来会导入本模块（见 `get_container_info` 那处同款注释）
        from nekro_agent.services.console.container_manager import read_container_metadata

        has_own_token = bool(read_container_metadata(target).access_token)
    except Exception as e:  # noqa: BLE001 - 见 docstring
        logger.warning(f"读取实例 {target.instance_key} 的接入令牌失败，无法判断认领是否会换掉期望令牌: {e}")
        return
    if not has_own_token:
        return
    logger.warning(
        f"认领后该协议端要出示的令牌变了: 账号 {account} 从 {source.instance_key} 过户到 "
        f"{target.instance_key}，而 {target.instance_key} 有自己的接入令牌。"
        "旧连接已断开：远程协议端要把该实例的令牌抄进它自己的配置才能重连"
        "（另一条出路是清空该实例的 access_token，退回部署级令牌）——见 docs/operations.md §1.4",
    )


async def adopt_unclaimed_instance(
    source: DBAdapterInstance,
    target: DBAdapterInstance,
) -> InstanceAdoptOutcome:
    """把未认领实例的账号与历史一并交给一个既有实例

    这是「有协议端连上来但没人认领」的第二条出路（第一条是
    `claim_unclaimed_as_new_instance`，把它认领成一个自己命名的新实例）。两条出路走的是
    同一段代码，差别只在目标实例是既有的还是当场新建的。

    ## 三步，顺序不能换

    1. **搬历史** —— 直接复用 `chat_key_migration.migrate_instance_ownership()`：
       它已经有预览、幂等、逐频道事务与按频道的失败隔离，重写一套只会多一份会分叉的实现。
    2. **移账号** —— `provider_account_id` 从源实例挪到目标实例。必须在搬完之后：
       账号唯一性由 `find_account_binding_conflict` 把关，先绑目标会撞上"账号已被源实例占用"。
    3. **归档源实例** —— 数据已经不在它名下了，但保留行本身（而不是删除）才留得下审计痕迹，
       也符合项目「删除默认归档」的既定约定。

    ## 一个频道搬失败就整体停在第 1 步

    此时账号还在源实例上、源实例也没归档，界面上看到的仍是「有一个未认领的协议端」，
    管理员重跑一次即可补齐——而不是留下一个「账号已经过户、历史却少了一半」的状态：
    后者没有任何入口能修回来。
    """
    if map_status(source.status) is not InstanceStatus.UNCLAIMED:
        raise ValidationError(reason="仅未认领状态的实例可以被认领")
    if source.id == target.id:
        raise ValidationError(reason="源实例与目标实例不能是同一个")
    if source.adapter_key != target.adapter_key:
        raise ValidationError(reason="只能在同一适配器下的实例之间认领")
    if target.status in (STATUS_TOMBSTONE, STATUS_ARCHIVED):
        raise ValidationError(reason="不能把协议端并入一个已归档的实例")
    if not target.enabled or target.status == STATUS_DISABLED:
        # 停用的实例是**收发闸门关着**的实例（`DBAdapterInstance.is_instance_active`）。
        # 把一个正在收消息的协议端并进去，等于：账号过户到一个不派发消息的实例、历史跟着搬走、
        # 源实例被归档——这条 Bot 会当场彻底静默，而界面上不会有任何一处说它坏了。
        # 本流程存在的理由就是「我的实例收不到消息」，绝不能自己再造一个同样的现场。
        raise ValidationError(reason=f"实例 '{target.instance_key}' 已被停用，请先启用它再认领协议端")

    account = source.provider_account_id.strip()
    await assert_account_can_move_to(source, target)
    if account and target.provider_account_id.strip() != account:
        # 并进租户实例：源实例是待认领行、没有自己的令牌，它的协议端凭的是部署级令牌，而这个协议端现在归租户了。
        # 在搬历史之前先记，记不下来就拒绝，什么都不动
        await record_account_landing_on_tenant(
            target, account, reason=f"待认领协议端并入租户实例 {target.instance_key}", refuse_on_failure=True,
        )

    migration = await migrate_instance_ownership(source.adapter_key, source.instance_key, target.instance_key)
    if migration.failed_channels:
        logger.warning(
            f"认领时有会话迁移失败，账号未过户、源实例未归档，可重跑补齐: "
            f"{source.instance_key} -> {target.instance_key}, 失败={migration.failed_channels}",
        )
        return InstanceAdoptOutcome(
            target=target,
            source_instance_key=source.instance_key,
            migration=migration,
            account_moved=False,
            source_archived=False,
        )

    account_moved = False
    if account and target.provider_account_id.strip() != account:
        async with in_transaction():
            source.provider_account_id = ""
            await source.save(update_fields=["provider_account_id", "update_time"])
            target.provider_account_id = account
            await target.save(update_fields=["provider_account_id", "update_time"])
        account_moved = True
        # 入站路由缓存装的正是「账号 -> 实例」，不清空的话下一条消息还会投给源实例。
        # 同时排一次在线连接复核：握手出示的令牌与目标实例要求的不同时，旧连接会被断开
        clear_instance_cache()
        _warn_if_adoption_changes_the_expected_token(source, target, account)
        # 出示的令牌与目标实例要求的不同 → 旧连接已被上面的复核断开（要按新绑定重连）；
        # 相同则不断开，但超级用户命令仍要重连一次才在该实例上生效（#98 的规则，不变）
        logger.warning(
            f"账号 {account} 已并入 {target.instance_key}：握手出示的令牌与目标实例要求的不同时旧连接会被断开、"
            "要按新绑定重连；相同则连接保留，但超级用户命令仍要重连一次才在该实例上生效",
        )

    if target.enabled:
        # 连接此刻还挂在协议端上，真实状态交给掉线监测的下一轮刷新，与就地认领同一套处理
        target = await adapter_instance_service.set_status(
            target,
            STATUS_CONNECTING,
            message=f"已认领协议端 {account or source.instance_key}（并入自 {source.instance_key}）",
        )

    await archive_instance(source)
    logger.info(
        f"认领完成: {source.instance_key} -> {target.instance_key}, 账号={account or '无'}, "
        f"搬迁 {len(migration.moved_channels)} 个会话、合并 {len(migration.merged_channels)} 个会话",
    )
    return InstanceAdoptOutcome(
        target=target,
        source_instance_key=source.instance_key,
        migration=migration,
        account_moved=account_moved,
        source_archived=True,
    )


async def claim_unclaimed_as_new_instance(
    source: DBAdapterInstance,
    display_name: str,
    new_instance_key: str,
) -> InstanceAdoptOutcome:
    """把未认领实例认领成一个由管理员命名的新实例

    ## 为什么不是"改一下 instance_key"

    `instance_key` 编码在该实例全部 `chat_key` 里，同时是 `chat_channel` / `chat_message` /
    `mem_*` 的分区列（见本模块的模块文档）。就地改这一列等于让这些数据一次性变成孤儿。
    所以这里把「改名」表达成它本来的样子：**建一个用管理员想要的标识的实例，
    再把协议端连同历史整体交给它**，搬运走既有的归属迁移，失败隔离与幂等全部继承下来。

    留空 `new_instance_key`（或与协议端自动登记的标识相同）时退化为就地认领，
    行为与改造前逐字一致。
    """
    if map_status(source.status) is not InstanceStatus.UNCLAIMED:
        raise ValidationError(reason="仅未认领状态的实例可以认领")

    name = display_name.strip() or source.display_name.strip() or source.instance_key
    slug = new_instance_key.strip().lower()
    if not slug or slug == source.instance_key:
        claimed = await claim_instance(source, name)
        return InstanceAdoptOutcome(
            target=claimed,
            source_instance_key=source.instance_key,
            migration=None,
            account_moved=False,
            source_archived=False,
        )

    if not is_valid_instance_key(slug):
        raise ValidationError(reason="实例标识仅允许小写字母、数字与下划线，且长度不超过 32")
    if await DBAdapterInstance.filter(instance_key=slug).exists():
        raise ConflictError(resource=f"实例 '{slug}'")
    # 账号冲突要在**建行之前**查出来，否则新实例已经落库、认领却抛异常，
    # 留下一个谁都不认识的空实例。新实例还不存在，所以这里用的是既有的「账号有没有被别人占着」
    # 那一支（排除源实例自己——它正是当前的持有者）
    await assert_account_unbound(source.adapter_key, source.provider_account_id.strip(), exclude_id=source.id)

    target = await DBAdapterInstance.create(
        adapter_key=source.adapter_key,
        instance_key=slug,
        display_name=name,
        provider=source.provider,
        # 账号在 `adopt_unclaimed_instance` 的第 2 步才过户：先绑会撞上账号唯一性校验
        provider_account_id="",
        status=STATUS_UNBOUND,
        enabled=True,
        metadata_json=source.metadata_json,
        tenant_id=source.tenant_id,
    )
    logger.info(f"认领为新实例: {source.instance_key} -> {slug}（{name}）")
    return await adopt_unclaimed_instance(source, target)


async def normalize_unbound_instance_status() -> List[str]:
    """把存量里「从未绑定账号却挂着 offline」的实例改判为 `unbound`，返回被改写的实例标识

    升级前建的空壳实例（管理员先建实例、还没接协议端）在库里一律是 `offline`。不改判的话
    掉线监测每一轮都会把它们当成"掉线的实例"重新计时，达到阈值就发一条
    「实例掉线告警」——而它们从来没上线过，这条告警从第一次起就是错的。

    判据与读侧 `resolve_instance_status()` 完全相同，因此这次改判**不改变界面上已经显示的
    任何内容**，只是把结论落进库里让掉线监测也看得见。幂等：改完再跑返回空列表。

    跑在掉线监测启动之前（`InstanceHealthService.start()`），表结构还没升级完时直接跳过。
    """
    if not await _adapter_instance_schema_ready():
        return []

    rewritten: List[str] = []
    # 自动改判只动启用中的实例；写入仍走条件更新（`set_runtime_status`），候选读出之后被停用的也不会被改
    candidates = await DBAdapterInstance.filter(
        provider_account_id="",
        status__in=sorted(UNBOUND_REWRITABLE_STATUSES),
        enabled=True,
    ).order_by("id")
    for instance in candidates:
        if not account_binding_required(instance.provider):
            continue
        await adapter_instance_service.set_runtime_status(
            instance,
            STATUS_UNBOUND,
            message="实例从未绑定账号，状态由离线改判为未绑定",
        )
        rewritten.append(instance.instance_key)

    if rewritten:
        logger.info(f"已把 {len(rewritten)} 个从未绑定账号的实例改判为 unbound: {rewritten}")
    return rewritten


class UnclaimedProtocolEnd(NamedTuple):
    """一个已经连进来、但还没人认领的协议端"""

    instance: DBAdapterInstance
    stats: InstanceStats


async def list_unclaimed_protocol_ends() -> List[UnclaimedProtocolEnd]:
    """列出全部未认领实例，连同它们已经攒下的会话与消息量

    会话数是这份清单里最要紧的一列：它直接回答「我在频道管理里找不到的那些会话，是不是
    都挂在这个没人认领的实例底下」。
    """
    instances = await DBAdapterInstance.filter(status=STATUS_UNCLAIMED).order_by("id")
    stats = await collect_instance_stats([instance.instance_key for instance in instances])
    return [
        UnclaimedProtocolEnd(instance=instance, stats=stats.get(instance.instance_key, InstanceStats()))
        for instance in instances
    ]


async def list_adopt_targets(adapter_keys: Set[str]) -> List[DBAdapterInstance]:
    """列出可以作为「并入」目标的既有实例

    排除未认领实例本身、归档与墓碑：把一个协议端并进另一个未认领实例毫无意义，
    并进已归档的实例则是把活的流量交给一个只读保留的壳。

    **同样排除已停用的实例**（`enabled=False` 或 `status=disabled`）。停用的实例收发闸门是
    关着的，把一个正在收消息的协议端并进去会让这条 Bot 当场彻底静默，且界面上没有一处会说它坏了
    —— 判据与 `adopt_unclaimed_instance` 里的拦截逐字对应：清单里不出现，接口上也不放行。
    """
    if not adapter_keys:
        return []
    return (
        await DBAdapterInstance.filter(adapter_key__in=sorted(adapter_keys), enabled=True)
        .exclude(status__in=[STATUS_UNCLAIMED, STATUS_ARCHIVED, STATUS_TOMBSTONE, STATUS_DISABLED])
        .order_by("id")
    )


class AccountBindingRepair(NamedTuple):
    """一个「适配器 + 账号」上的重复绑定修复结果"""

    adapter_key: str
    provider_account_id: str
    kept_instance_key: str
    unbound_instance_keys: List[str]


async def unbind_duplicate_account_bindings() -> List[AccountBindingRepair]:
    """把存量里「一个账号绑多个实例」的多余绑定解掉，返回每个账号的修复结果

    升级前建的实例可能已经处于这种状态（创建侧此前完全不校验账号占用），而这正是入站消息
    路由不出唯一实例的成因。修复挂在启动期、且**排在 aerich 迁移之前**，位置与
    `merge_duplicate_chat_channels` 相同：这是存量修复的既定档位，也是将来补上账号唯一索引时
    必须的顺序——唯一索引的迁移撞上存量重复行会直接失败，服务连启动都启动不了。

    **刻意不合并实例**。重复频道行是同一个会话被写成两行，合并才是还原真相；而两个实例按隔离模型
    是两个合法的实例，各自可能已经有会话、人设与记忆，替用户决定谁并进谁就是替他丢数据——
    那件事有专门的、要先看预览再二次确认的入口（`chat_key_migration.migrate_instance_ownership`）。
    这里只做能可逆的最小动作：**保留一行绑定，把其余实例的账号字段清空**。被解绑的实例仍在，
    数据分毫未动，在总后台里表现为「未绑定账号」，管理员可以改绑别的账号，也可以走归属迁移合并。

    保留哪一行与 `DBAdapterInstance.get_by_bound_account` 同一判据（排除软删墓碑后最早那行），
    因此修复不改变入站消息此刻实际投递到的实例，只是把歧义消掉。**墓碑必须排除**：
    软删不清空账号字段（`soft_delete_instance` 只改状态），而墓碑的 id 通常比接手的新实例小，
    按"最早那行"会把账号判给一个已删除的实例、反过来解绑那个正在收发消息的——
    这条路径今天就走得通（`adapters/wechat_ilink_multi/routers.py` 的删除接口留下墓碑，
    它自己的账号占用校验也正是排除墓碑的）。

    幂等：修复后每个账号只剩一行绑定，重复调用返回空列表。
    """
    if not await _adapter_instance_schema_ready():
        return []

    seen: Dict[Tuple[str, str], int] = {}
    for row in (
        await DBAdapterInstance.exclude(provider_account_id="")
        .exclude(status=STATUS_TOMBSTONE)
        .values("adapter_key", "provider_account_id")
    ):
        identity = (str(row["adapter_key"]), str(row["provider_account_id"]))
        seen[identity] = seen.get(identity, 0) + 1

    repairs: List[AccountBindingRepair] = []
    for (adapter_key, account_id), count in seen.items():
        if count < 2:
            continue
        async with in_transaction():
            rows = (
                await DBAdapterInstance.filter(adapter_key=adapter_key, provider_account_id=account_id)
                .exclude(status=STATUS_TOMBSTONE)
                .order_by("id")
                .select_for_update()
            )
            if len(rows) < 2:
                continue
            keeper, losers = rows[0], rows[1:]
            for loser in losers:
                metadata = load_metadata(loser)
                metadata[METADATA_UNBOUND_ACCOUNT_CONFLICT] = {
                    "provider_account_id": account_id,
                    "kept_by": keeper.instance_key,
                    "unbound_at": db_now().strftime(DATETIME_FORMAT),
                }
                loser.provider_account_id = ""
                loser.metadata_json = dump_metadata(metadata)
                loser.last_error = (
                    f"账号 {account_id} 同时被多个实例绑定，已解绑本实例；"
                    f"该账号现归属实例 '{keeper.instance_key}'"
                )
                await loser.save(update_fields=["provider_account_id", "metadata_json", "last_error", "update_time"])
        repairs.append(
            AccountBindingRepair(
                adapter_key=adapter_key,
                provider_account_id=account_id,
                kept_instance_key=keeper.instance_key,
                unbound_instance_keys=[loser.instance_key for loser in losers],
            ),
        )

    if repairs:
        clear_instance_cache()
        logger.warning(f"已解除 {len(repairs)} 个账号上的重复实例绑定: {repairs}")
    return repairs


async def warn_cross_adapter_instance_key_collisions() -> Dict[str, List[str]]:
    """启动期只读自检：找出被多个适配器同时使用的 `instance_key`，逐个打 WARNING

    数据库的唯一约束只到 `(adapter_key, instance_key)`，而人设、插件开关、记忆、协议端容器名
    等数据只按 `instance_key` 存取，同名的几个实例会共用同一份；它们分属不同租户时就是跨租户读写。
    现在的创建路径都会拒绝跨适配器重名，这里只是把升级前留下的存量指给管理员看。

    **只读，不改任何数据**：哪个实例该改名、数据该归谁，只有管理员说得清。
    空标识（单实例适配器）不算重名。

    Returns:
        `instance_key` -> 使用它的适配器标识（按字母序）；没有重名时为空字典
    """
    adapters_by_key: Dict[str, Set[str]] = {}
    for adapter_key, instance_key in await DBAdapterInstance.all().values_list("adapter_key", "instance_key"):
        key = str(instance_key)
        if not key:
            continue
        adapters_by_key.setdefault(key, set()).add(str(adapter_key))

    collisions: Dict[str, List[str]] = {}
    for instance_key in sorted(adapters_by_key):
        adapter_keys = sorted(adapters_by_key[instance_key])
        if len(adapter_keys) < 2:
            continue
        collisions[instance_key] = adapter_keys
        logger.warning(
            f"实例标识 '{instance_key}' 同时被多个适配器使用：{', '.join(adapter_keys)}。"
            "人设、插件开关、记忆、协议端容器名等数据只按实例标识存取，这几个实例会共用同一份；"
            "若它们分属不同租户，就是跨租户读写。请为其中一个改用新的实例标识重建，并核对上述数据的归属。"
            "本检查只读，不改任何数据。",
        )
    return collisions


async def _adapter_instance_schema_ready() -> bool:
    """判断 `adapter_instance` 表是否已经具备当前模型的全部列

    修复跑在 aerich 迁移之前，此时表结构可能还停在升级前的版本（甚至整张表还不存在），
    ORM 的整行查询会直接报错。缺列即跳过：那种库还没有本模块要修的字段。
    与 `chat_key_migration` 里频道侧的同名判断是同一套做法。
    """
    conn = Tortoise.get_connection("default")
    if conn.capabilities.dialect == "sqlite":
        rows = await conn.execute_query_dict("PRAGMA table_info('adapter_instance')")
        existing = {str(row["name"]) for row in rows}
    else:
        rows = await conn.execute_query_dict(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'adapter_instance'",
        )
        existing = {str(row["column_name"]) for row in rows}

    missing = set(DBAdapterInstance._meta.db_fields) - existing  # noqa: SLF001
    if missing:
        logger.info(f"adapter_instance 表结构尚未升级完成，跳过重复账号绑定修复: 缺少列 {sorted(missing)}")
        return False
    return True


async def archive_instance(instance: DBAdapterInstance) -> DBAdapterInstance:
    """归档实例：停用并置为只读保留，数据全部保留可查"""
    instance.enabled = False
    logger.info(f"归档实例: instance_key={instance.instance_key}, adapter_key={instance.adapter_key}")
    return await adapter_instance_service.set_status(
        instance,
        STATUS_ARCHIVED,
        message="实例已归档",
        extra_fields=["enabled"],
    )


async def purge_instance(instance: DBAdapterInstance) -> Dict[str, int]:
    """彻底删除实例及其全部关联数据

    统计与删除同处一个事务，且**先落日志再删**：即便后续某步失败整体回滚，
    日志里仍留有这次操作原本要抹掉的数据规模，便于事后核对。

    分区依据：`chat_channel` / `chat_message` / `instance_domain_binding` 同时带
    `adapter_key` 与 `instance_key`，按这一对精确匹配；`mem_*` 只有 `instance_key`，
    因此存在跨适配器同名实例时整个操作直接拒绝（见函数开头的冲突检查）；
    `exec_code` / `plugin_data` / `recurring_timer_job` 只有 `chat_key`，按
    `{adapter_key}-{instance_key}-` 前缀匹配（`instance_key` 不含 `-`，前缀不会误伤 `inst1` vs `inst10`）。
    定时任务必须一并清除，否则实例没了任务还会继续触发并消耗额度。

    向量库里的段落向量在事务提交后单独清理：Qdrant 不参与数据库事务，
    放在事务内失败会连带回滚已经正确的关系型删除。

    **内置 NapCat 容器与数据目录同理，也在提交后单独回收**（见
    `_reclaim_purged_instance_container`）。此前这一步整个不存在：十二张表加向量库全清了，
    宿主上那个容器带着 `restart: unless-stopped` 继续跑，而库里已经没有任何一行指得到它
    —— `container_manager` 里每个能停 / 删容器的函数都以 `DBAdapterInstance` 为入口。
    测试部署上实测漏出两个，空转 19 天，合计 1.2 GB 内存 + 5.7 GB 磁盘。
    """
    instance_key = instance.instance_key
    chat_key_prefix = f"{instance.adapter_key}-{instance_key}-"

    # 记忆与向量只按 instance_key 存：另一个适配器下的同名实例会被连带删掉，
    # 而那一行实例本身还在，变成一个没有数据的空壳。宁可拒绝也不做不可逆的误删。
    duplicate = await DBAdapterInstance.filter(instance_key=instance_key).exclude(id=instance.id).first()
    if duplicate is not None:
        raise ConflictError(
            # 文案是「{resource}已存在」：按「主体（说明）」的写法
            resource=(
                f"另一个适配器下的同名实例 '{instance_key}'（记忆和向量只按实例标识存，"
                "彻底删除会连带删掉它的数据；请先归档本实例，或先处理那个同名实例）"
            ),
        )

    # 容器形态必须在**删行之前**读：删完之后库里已经没有 provider 可查了
    had_builtin_container = instance.provider.strip().lower() == InstanceProvider.BUILTIN_NAPCAT.value

    async with in_transaction():
        counts: Dict[str, int] = {
            "chat_channel": await DBChatChannel.filter(adapter_key=instance.adapter_key, instance_key=instance_key).count(),
            "chat_message": await DBChatMessage.filter(adapter_key=instance.adapter_key, instance_key=instance_key).count(),
            "exec_code": await DBExecCode.filter(chat_key__startswith=chat_key_prefix).count(),
            "plugin_data": await DBPluginData.filter(target_chat_key__startswith=chat_key_prefix).count(),
            "recurring_timer_job": await DBRecurringTimerJob.filter(chat_key__startswith=chat_key_prefix).count(),
            "mem_paragraph": await DBMemParagraph.filter(instance_key=instance_key).count(),
            "mem_episode": await DBMemEpisode.filter(instance_key=instance_key).count(),
            "mem_entity": await DBMemEntity.filter(instance_key=instance_key).count(),
            "mem_relation": await DBMemRelation.filter(instance_key=instance_key).count(),
            "adapter_instance_event": await DBAdapterInstanceEvent.filter(instance_id=instance.id).count(),
            "adapter_instance_session": await DBAdapterInstanceSession.filter(instance_id=instance.id).count(),
            "instance_domain_binding": await DBInstanceDomainBinding.filter(
                adapter_key=instance.adapter_key, instance_key=instance_key,
            ).count(),
        }
        logger.warning(
            f"彻底删除实例 {instance_key}（adapter_key={instance.adapter_key}），级联删除统计: {counts}",
        )

        await DBRecurringTimerJob.filter(chat_key__startswith=chat_key_prefix).delete()
        await DBPluginData.filter(target_chat_key__startswith=chat_key_prefix).delete()
        await DBExecCode.filter(chat_key__startswith=chat_key_prefix).delete()
        await DBMemRelation.filter(instance_key=instance_key).delete()
        await DBMemEntity.filter(instance_key=instance_key).delete()
        await DBMemEpisode.filter(instance_key=instance_key).delete()
        await DBMemParagraph.filter(instance_key=instance_key).delete()
        await DBChatMessage.filter(adapter_key=instance.adapter_key, instance_key=instance_key).delete()
        await DBChatChannel.filter(adapter_key=instance.adapter_key, instance_key=instance_key).delete()
        await DBAdapterInstanceSession.filter(instance_id=instance.id).delete()
        await DBAdapterInstanceEvent.filter(instance_id=instance.id).delete()
        # 域归属必须一起删掉：`instance_key` 是可以被重新认领的 slug（见 `claim_unclaimed_instance`），
        # 残留的绑定行会让**同名的新实例**（可能属于另一个租户）直接继承被删实例的记忆/好感域。
        # 好感行不在本清单里、也不该在（它挂在域上而不是实例上），因此那份数据会被新实例原样读到。
        await DBInstanceDomainBinding.filter(adapter_key=instance.adapter_key, instance_key=instance_key).delete()
        await DBAdapterInstance.filter(id=instance.id).delete()

    clear_instance_cache()
    await purge_instance_vectors(instance_key)
    await _reclaim_purged_instance_container(instance_key, had_builtin_container=had_builtin_container)
    return counts


async def _reclaim_purged_instance_container(instance_key: str, *, had_builtin_container: bool) -> None:
    """彻底删除实例之后回收它的内置 NapCat 容器与数据目录

    ## 为什么在事务提交之后

    与 `purge_instance_vectors` 同一个理由：Docker 不参与数据库事务，放进事务里失败会
    连带回滚已经正确的关系型删除。代价是「行删了、容器没删掉」这个中间态存在——
    它现在**是可见且可行动的**：失败会打 WARNING，而且总后台
    `/console/napcat-orphans` 会把它列成 `orphaned` 等着回收。

    ## 为什么判据取删除**之前**的 provider

    删完之后库里已经没有 provider 可读。而且这个判据必须窄：
    只回收**本实例自己那个内置容器**。实例是别的形态（协议端在外部机器上）时，
    宿主上那个同名容器是不是残留、能不能删，是另一个判断——交给孤儿盘点那条路，
    它有完整的判据表和二次确认，不该由「删实例」顺手替人做决定。

    ## 为什么连数据目录一起删

    `delete_container()` 有意保留 `napcat_data/{key}/`（「重建容器后无需重新扫码」），
    那是**删容器**的语义。这里是**彻底删除实例**——消息、记忆、向量全都清了，
    单独留下 QQ 登录态既不一致，也正好是那 5.7 GB 磁盘泄漏的来源。
    彻底删除本来就要回填实例标识二次确认（`routers/console/instances.py`），
    不可逆这件事调用方已经知情。

    ## 判定与执行都不在这里实现

    直接复用 `napcat_orphans` 那一份：行已经删掉了，所以它给出的判定天然是
    `ORPHANED`。**不要在这里另写一遍**——本项目已经五次栽在「同一个判据的第二处实现」上。
    宿主上本来就没有这个容器时它抛 `NapcatOrphanRefused`，那是正常路径（绝大多数实例
    从来没建过内置容器），按 debug 记一笔即可。
    """
    if not had_builtin_container:
        return

    # 延迟导入：`napcat_orphans` -> `container_manager` -> 本模块，模块级导入会成环
    from nekro_agent.services.console.napcat_orphans import (
        NapcatOrphanRefused,
        execute_napcat_orphan_cleanup,
    )

    try:
        outcome = await execute_napcat_orphan_cleanup(instance_key, remove_data_dir=True)
    except NapcatOrphanRefused as e:
        logger.debug(f"实例 {instance_key} 没有可回收的内置 NapCat 残留: {e}")
        return
    except Exception as e:  # noqa: BLE001 — Docker 侧任何异常都不该让「实例已删除」这个既成事实变成 500
        logger.warning(
            f"实例 {instance_key} 的内置 NapCat 容器回收失败: {e}。"
            f"数据库侧已经删除完毕，宿主上的残留可在总后台「NapCat 残留回收」里处理",
        )
        return

    logger.info(
        f"已回收实例 {instance_key} 的内置 NapCat 残留: "
        f"container_removed={outcome.container_removed} data_dir_removed={outcome.data_dir_removed} "
        f"freed_bytes={outcome.freed_bytes}",
    )
    if outcome.failures:
        logger.warning(
            f"实例 {instance_key} 的内置 NapCat 残留未清干净: {'；'.join(outcome.failures)}。"
            f"可在总后台「NapCat 残留回收」里重试",
        )


async def purge_instance_vectors(instance_key: str) -> None:
    """清除实例在记忆向量库里的残留段落向量

    关系型记录删掉但向量还在，这些段落仍会被记忆检索命中，等于删除没生效。
    向量库不可用时只记录告警：数据库侧的删除已经提交，不应因为外部组件失联而回滚。
    """
    client = await get_qdrant_client()
    if client is None:
        logger.warning(f"Qdrant 客户端不可用，实例向量未清理: instance_key={instance_key}")
        return

    try:
        await client.delete(
            collection_name=memory_qdrant_manager.collection_name,
            points_selector=qdrant_models.FilterSelector(
                filter=qdrant_models.Filter(
                    must=[
                        qdrant_models.FieldCondition(
                            key="instance_key",
                            match=qdrant_models.MatchValue(value=instance_key),
                        ),
                    ],
                ),
            ),
        )
    except Exception as e:
        logger.warning(f"实例向量清理失败: instance_key={instance_key}, error={e}")
    else:
        logger.info(f"实例向量清理完成: instance_key={instance_key}")


# ==================== 接入令牌更换 ====================


class AccessTokenRotation(NamedTuple):
    """一次换令牌的结果

    `delivery` 是新令牌送没送到协议端：`restarted`（内置 NapCat 已重启）/ `config_written`
    （容器还没建，配置已重新下发）/ `rewired`（qbob 已重新接线）/ `not_attached`（qbob 里还没有
    bot）/ `manual`（远程协议端，要人抄过去）/ `failed`（送达失败，`delivery_error` 说明原因）。
    """

    token: str
    disconnected: int
    delivery: str
    delivery_error: str


def _delivery_error_text(error: Exception) -> str:
    """送达失败时给界面看的那句话：`AppError` 取它的对外文案，其余取 `str`

    只取文案、不碰异常里的任何请求/响应内容，令牌因此不会被带进响应、事件与日志。
    """
    if isinstance(error, AppError):
        return error.get_message(SupportedLang.ZH_CN)
    return str(error)


def _redact_tokens(text: str, *tokens: str) -> str:
    """把文本里出现的令牌换成掩码

    异常文本是**第三方**给的（Docker、qbob、httpx），谁也无法保证它某天不会把请求体
    原样带出来。这里是最后一道闸：日志、事件与响应都只经过它处理过的文本。
    """
    for token in tokens:
        if token:
            text = text.replace(token, "***")
    return text


async def rotate_access_token(instance: DBAdapterInstance, *, operator: str) -> AccessTokenRotation:
    """更换实例自己的接入令牌，并把它送到协议端

    ## 为什么只由服务端生成

    令牌是握手凭据，不是可调参数：允许调用方指定就等于允许把一枚已知的字符串装进实例，
    而「换令牌」的全部意义是作废旧的。这里与 `create_instance` 用同一个生成器
    （`secrets.token_urlsafe(24)`），长度与熵一致。

    ## 为什么不回滚

    第 4 步（送达）失败时**不**把旧令牌写回去：旧令牌此刻已经不被接受了，回滚只会把一枚
    正要作废的令牌救回来。失败原因进响应，由界面提示手动重启容器 / 在协议端页重新接线。

    ## 为什么拒绝未认领实例

    未认领实例的协议端凭部署级令牌（或压根不带令牌）连着，凭空给它一枚令牌，它下一次握手
    就被拒——那正是 `create_instance` 的注释里说的「自动登记那条路径刻意不写这个键」。
    要换先认领，认领之后它才有「自己的令牌」这回事。

    ## 可信来源（#98）怎么自动恢复

    曝光集合按令牌指纹记。新令牌的指纹不在里面，其余条件照旧（平台方、已认领、非空令牌），
    于是这个实例的超级用户来源判定自动恢复可信；旧令牌的指纹留在集合里，凭它握手的连接
    也不再被认可（`connection_accepted_by` 比的就是「出示的」与「此刻期望的」）。
    **租户实例不记曝光**：租户实例本来就一律不可信，收回时会记下它当时生效的令牌
    （那时已经是新令牌），启动扫描也会扫到，这里再记一次只会让日志多一条无用的告警。

    ## 顺序

    0. 同一实例的更换按实例排队（进程内锁），锁里再按库里当前值取旧令牌；
    1. 行锁下把新令牌合并写进元数据（`container_manager.write_access_token`）；
    2. 按 `id` 回读一行，后面全用这一行：写元数据只刷新了调用方那份的 `metadata_json`，
       而状态、provider 这些列可能是几十秒前的快照；
    3. `revalidate_live_connections()` 断开凭旧令牌握手、账号绑在已认领实例上的连接（#108）；
       账号还没绑定 / 绑在未认领实例上的连接不在这里断，它们的消息本来就由未认领闸门挡着；
    4. 按形态送达（失败不回滚）；内置 NapCat 在实例停用 / 归档、或容器被停着时只重写配置，不替管理员把容器拉起来；
    5. 记一条实例事件与一条 WARNING 日志，两者都**不写令牌**；租户实例的事件租户看得见，不写平台方操作人与原始报错。
    """
    if instance.adapter_key != "onebot_v11":
        raise ValidationError(reason="只有 OneBot V11 实例有接入令牌")
    if map_status(instance.status) is InstanceStatus.UNCLAIMED:
        raise ValidationError(reason="未认领实例没有自己的接入令牌，请先认领")

    # 同一实例的两次更换排队：交错时后一次先写完库，前一次再拿自己那枚去下发配置，
    # 库里与协议端配置里就是两枚不同的令牌，前一次的响应也给出一枚已经作废的令牌
    async with _rotation_locks.setdefault(instance.id, asyncio.Lock()):
        return await _rotate_access_token_locked(instance, operator=operator)


# 实例 id -> 换令牌的锁（进程内；NA 是单进程部署）
_rotation_locks: Dict[int, asyncio.Lock] = {}

# 这几种容器状态表示「被停着」：Docker 的 restart 会把已停止的容器启动起来，换令牌不该替管理员做这个决定
_STOPPED_CONTAINER_STATES = frozenset({ContainerState.EXITED, ContainerState.CREATED, ContainerState.PAUSED})


async def _napcat_should_stay_down(instance: DBAdapterInstance) -> bool:
    """内置 NapCat 换完令牌是只写配置，还是重启容器：停用 / 归档的实例与被停掉的容器只写配置"""
    if not instance.enabled or instance.status in (STATUS_DISABLED, STATUS_ARCHIVED):
        return True
    from nekro_agent.services.console import container_manager

    # 读不到（Docker 不可达、容器不存在）时按 `absent` 回答，交给下面的重启去报真实原因
    info = await container_manager.get_container_info(instance)
    return info is not None and info.state in _STOPPED_CONTAINER_STATES


async def _rotate_access_token_locked(instance: DBAdapterInstance, *, operator: str) -> AccessTokenRotation:
    token = secrets.token_urlsafe(24)
    # 延迟导入：`container_manager` 反过来会导入本模块（见 `get_container_info` 那处同款注释）
    from nekro_agent.services.console import container_manager, qbob_manager

    # 在锁里按库里当前值取：调用方手里那份可能是上一次更换之前读的
    current = await DBAdapterInstance.filter(id=instance.id).first()
    if current is None:
        raise NotFoundError(resource=f"实例 '{instance.instance_key}'")
    # 换之前实际生效的那一枚（没有自己的令牌时就是部署级令牌）只用来给异常文本脱敏，绝不写进日志 / 事件 / 响应
    previous_token = container_manager.resolve_access_token(current)
    await container_manager.write_access_token(current, token)
    fresh = await DBAdapterInstance.filter(id=instance.id).first()
    if fresh is None:
        # 写令牌与回读之间被彻底删掉了：实例已经不在，按「不存在」报错，不假装换成功
        raise NotFoundError(resource=f"实例 '{instance.instance_key}'")

    from nekro_agent.adapters.onebot_v11.core.access_token import revalidate_live_connections

    disconnected = await revalidate_live_connections()

    delivery = "manual"
    delivery_error = ""
    if container_manager._is_builtin_napcat(fresh):  # 与容器侧同一个判据，别在这里另写一份 provider 比较
        try:
            if await _napcat_should_stay_down(fresh):
                await container_manager.resync_network_config(fresh)
                delivery = "config_written"
            else:
                try:
                    await container_manager.restart_container(fresh)
                except NotFoundError:
                    # 容器还没建：配置先落到数据目录，下次建容器时用的就是新令牌
                    await container_manager.resync_network_config(fresh)
                    delivery = "config_written"
                else:
                    delivery = "restarted"
        except Exception as e:  # noqa: BLE001 - 送达失败不回滚，见 docstring
            delivery = "failed"
            delivery_error = _redact_tokens(_delivery_error_text(e), token, previous_token)
            logger.warning(
                f"实例 {fresh.instance_key} 的新接入令牌没能送达协议端（{delivery}），需要人工重启容器: {delivery_error}",
            )
    elif map_provider(fresh.provider) is InstanceProvider.BUILTIN_QBOB:
        try:
            if qbob_manager.attached_appid(fresh):
                await qbob_manager.rewire(fresh)
                delivery = "rewired"
            else:
                delivery = "not_attached"
        except Exception as e:  # noqa: BLE001 - 同上
            delivery = "failed"
            delivery_error = _redact_tokens(_delivery_error_text(e), token, previous_token)
            logger.warning(
                f"实例 {fresh.instance_key} 的新接入令牌没能写回 qbob，需要在协议端页重新接线: {delivery_error}",
            )

    if fresh.tenant_id is None:
        message = f"{operator} 更换了接入令牌，断开 {disconnected} 条旧连接，送达结果: {delivery}"
        if delivery_error:
            message += f"（{delivery_error}）"
    else:
        # 租户在自己实例的健康事件里看得到这一条：不写平台方操作人，也不写 Docker / qbob 的原始报错（日志里有）
        message = f"平台方更换了接入令牌，断开 {disconnected} 条旧连接，送达结果: {delivery}"
    await adapter_instance_service.record_event(
        instance=fresh,
        event_type="access_token_rotated",
        status_from=fresh.status,
        status_to=fresh.status,
        message=message,
    )
    logger.warning(
        f"实例 {fresh.instance_key} 的接入令牌已更换: 操作人={operator}, 断开旧连接={disconnected}, 送达={delivery}"
        + (f", 原因={delivery_error}" if delivery_error else ""),
    )
    return AccessTokenRotation(token=token, disconnected=disconnected, delivery=delivery, delivery_error=delivery_error)


async def probe_docker_available() -> bool:
    """探测宿主机 Docker 是否可用，决定总后台能否托管内置协议端容器"""
    try:
        docker = aiodocker.Docker()
    except Exception as e:
        logger.debug(f"Docker 客户端不可用: {e}")
        return False

    try:
        await docker.version()
    except Exception as e:
        logger.debug(f"Docker 版本探测失败: {e}")
        return False
    else:
        return True
    finally:
        await docker.close()


async def build_overview() -> ConsoleOverview:
    """组装总后台首页概览"""
    today_start = db_now().replace(hour=0, minute=0, second=0, microsecond=0)

    instances, channel_total, message_count_today, docker_available = await asyncio.gather(
        DBAdapterInstance.exclude(status=STATUS_TOMBSTONE),
        DBChatChannel.all().count(),
        DBChatMessage.filter(create_time__gte=today_start).count(),
        probe_docker_available(),
    )

    statuses = [
        resolve_instance_status(instance.status, instance.provider, instance.provider_account_id)
        for instance in instances
    ]
    return ConsoleOverview(
        instance_total=len(statuses),
        instance_online=sum(1 for status in statuses if status is InstanceStatus.ONLINE),
        instance_offline=sum(1 for status in statuses if status is InstanceStatus.OFFLINE),
        instance_unbound=sum(1 for status in statuses if status is InstanceStatus.UNBOUND),
        instance_unclaimed=sum(1 for status in statuses if status is InstanceStatus.UNCLAIMED),
        channel_total=channel_total,
        message_count_today=message_count_today,
        docker_available=docker_available,
    )
