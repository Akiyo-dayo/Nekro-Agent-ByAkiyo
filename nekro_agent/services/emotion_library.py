"""表情库的服务层（核心提供零件，插件调用）

## 三种套组

- **预设套组**（`preset`）：平台方按分类整理，分给人设或实例；
- **存量（待整理）**（`legacy`）：老版本那一整块 JSON 导入进来的容器，默认只分给平台方实例，
  平台方整理完可以拆走并删掉它；
- **收集库**（`collect`）：每个实例一个，Bot 收集的表情都进这里，不需要分配。

一条表情只属于一个套组（`emotion_item.set`），归属因此永远只有一处答案来源。

## 可读 / 可写怎么算

一个会话的可读集合 = 本实例收集库 ∪ 分给当前人设的 ∪ 本实例按配置挑出来的（挑出来的还要与
「可用池」取交集）；可写集合**只有**本实例收集库。可用池 = 收集库 ∪ 分给所有实例的 ∪
（本实例归平台方时）分给平台方实例的 ∪ 分给本实例的。细节见 `resolve_emotion_scope`。

平台方提供的套组对租户实例**只读**：预设与存量套组只在控制台里改，会话里的增删改只能落到
收集库。平台方实例也一样，这条不因「是不是平台方」而放宽。

## 越权为什么按不存在回答

模型的输入是敌对的：它可以把任何 `emotion_id` 传进来。如果越权与真的查不到给出不同的文案，
模型就能把整个表情库枚举出来（「这个 ID 存在但不给你」本身就是信息）。所以不在可读集合里
一律回「不存在」，文案与插件原来那句逐字相同（`emotion_not_found_message`）。唯一的例外是
「在可读里但不可写」——那条要告诉模型「这是平台方提供的，只能用不能改」，否则它会反复重试。

## 文件为什么按内容哈希命名

存量是平铺目录、文件名带原名与描述，两个会话收同名图、同描述时后一张覆盖前一张；内容寻址
（`h/<sha256>.<扩展名>`）让文件名就是内容，同一张图只存一份，不同套组、不同实例的行指向
同一个文件，永远不互相覆盖。用 sha256 而不是 md5：md5 可以人为造碰撞，内容寻址下碰撞等于
让别人的行指向你造的图。删除按引用计数，别的行还指着同一个文件时不动它。

## 向量为什么带 `set_id`，搜索后还要按库表复核

向量集合是全局一个（所有套组共用一种嵌入），而可见范围是「按套组」算的，所以每个点的载荷里
必须带 `set_id`，搜索时先按 `set_id ∈ 可读套组` 过滤。但向量库与库表是两套存储：移动、删除、
存量导入都可能让载荷短暂落后于库表（写入失败只记 WARNING，靠下次回填与搜索复核兜底）。
因此搜索结果**必须**再按库表查一遍、确认这条表情仍在自己可读的套组里，对不上的丢掉——
过滤条件只在一处生效（库表），向量过滤只是把候选集缩小。

## 边界

本模块不 import 任何 `plugins.*`：插件调用它，反向依赖会让核心被插件拖住。向量嵌入由插件
生成后传进来，这里不调模型。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from collections.abc import Awaitable, Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from qdrant_client import models as qdrant_models
from tortoise.functions import Count

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv
from nekro_agent.core.vector_db import get_qdrant_client
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_emotion_item import DBEmotionItem
from nekro_agent.models.db_emotion_set import DBEmotionSet
from nekro_agent.models.db_emotion_set_assignment import DBEmotionSetAssignment
from nekro_agent.models.db_plugin_data import DBPluginData

logger = get_sub_logger("emotion_library")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

KIND_PRESET = "preset"
KIND_LEGACY = "legacy"
KIND_COLLECT = "collect"

TARGET_ALL_INSTANCES = "all_instances"
TARGET_PLATFORM_INSTANCES = "platform_instances"
TARGET_INSTANCE = "instance"
TARGET_PRESET = "preset"

# 只有这两类套组可以被分配：收集库属于单个实例，按定义不需要分配
ASSIGNABLE_KINDS: FrozenSet[str] = frozenset({KIND_PRESET, KIND_LEGACY})

ASSIGNMENT_TARGETS: FrozenSet[str] = frozenset(
    {TARGET_ALL_INSTANCES, TARGET_PLATFORM_INSTANCES, TARGET_INSTANCE, TARGET_PRESET},
)

COLLECT_SET_DEFAULT_NAME = "本实例收集"
LEGACY_SET_NAME = "存量（待整理）"
LEGACY_SET_CATEGORY = "未整理"

# 与插件 `get_vector_collection_name()` 的产物相同（插件键 = `作者.模块名`）。写死而不是
# 反查插件：服务层不能 import 插件，而集合名必须在插件还没加载时也能算出来。
EMOTION_VECTOR_COLLECTION = "KroMiose.emotion"

def default_emotion_files_dir() -> Path:
    """表情文件的默认目录：与插件现在用的目录一致，`DATA_DIR/plugin_data/<插件键>/emotions`

    单独成函数是为了让「默认值是不是这个目录」能被核对：测试固件会把 `EMOTION_FILES_DIR`
    整体改道到临时目录（删实例、清收集库都会删这下面的文件）。
    """
    return Path(OsEnv.DATA_DIR) / "plugin_data" / EMOTION_VECTOR_COLLECTION / "emotions"


EMOTION_FILES_DIR: Path = default_emotion_files_dir()

# 内容寻址的子目录：新收集与控制台上传的文件都落在这里
EMOTION_HASH_SUBDIR = "h"

PLATFORM_READONLY_MESSAGE = "这是平台方提供的表情，只能使用不能修改"

# 存量导入的标记键由插件在插件存储里管；这里只把名字固定下来，供 B2 的插件初始化引用
LEGACY_IMPORT_MARKER = "emotion_store_imported_v1"

# 单次 `set_payload` / `upsert` 提交多少个点。批量太大会撑爆请求体，太小会让导入慢成一条条写
VECTOR_BATCH_SIZE = 100

# 启动回填时，向量库里缺点的表情每次启动最多重新生成多少条嵌入（每条一次嵌入模型调用）。
# 少量缺点（几条从没写成的）一次补完；集合被清空过的部署不在启动时一口气打几百次模型，
# 剩下的由下次启动接着补，或者平台方在控制台跑 `emo_reindex -y` 一次重建
MISSING_POINT_REEMBED_LIMIT = 100

# `one_time_code` 缓存的容量与有效期：一轮里提示词可能渲染多次，重复解析要查三次主键点查
_SCOPE_CACHE_TTL_SECONDS = 60
_SCOPE_CACHE_MAX_ENTRIES = 512


def emotion_not_found_message(emotion_id: str) -> str:
    """「这条表情不存在」的文案

    与插件改造前那句逐字相同：越权与真的查不到必须同文，模型才分不出「没有」和「不是你的」。
    """
    return f"Error: Emotion with ID '{emotion_id}' not found"


# ---------------------------------------------------------------------------
# 会话归属与可读 / 可写集合
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmotionScope:
    """一个会话能读、能写的表情集合

    Attributes:
        resolved: 解析出了会话归属
        adapter_key: 归属实例的适配器标识；解析不了时为空串
        instance_key: 归属实例标识；解析不了或频道行没有实例段时为空串
        is_platform_instance: 实例行的 `tenant_id` 为空（或单租户部署）
        persona_id: 当前生效人设，0 = 内置人设 / 解析不了
        collect_set_id: 本实例收集库；没有实例（`instance_key` 为空）或解析不了时为 None
        readable_set_ids: 能读到内容的套组
        writable_set_ids: 能改能删的套组；只可能是 `{collect_set_id}` 或空
    """

    resolved: bool
    adapter_key: str
    instance_key: str
    is_platform_instance: bool
    persona_id: int
    collect_set_id: Optional[int]
    readable_set_ids: FrozenSet[int]
    writable_set_ids: FrozenSet[int]


# `one_time_code` -> (过期时刻, 可读 / 可写集合的输入摘要, 结果)。同一轮里多次解析只算一次；
# 配置参数变了就重算（缓存键里带上它们，免得一轮里改了配置还读到旧的）。
_scope_cache: Dict[str, Tuple[float, Tuple[Any, ...], EmotionScope]] = {}


def _cache_lookup(key: str, signature: Tuple[Any, ...]) -> Optional[EmotionScope]:
    entry = _scope_cache.get(key)
    if entry is None:
        return None
    expires_at, cached_signature, scope = entry
    if expires_at < time.monotonic() or cached_signature != signature:
        _scope_cache.pop(key, None)
        return None
    return scope


def _cache_store(key: str, signature: Tuple[Any, ...], scope: EmotionScope) -> None:
    """写入缓存；超过容量时清掉最旧的一半

    不做 LRU：这里只关心「一轮内复用」，跨轮命中没有价值。整批清掉一半比逐条淘汰便宜，
    也避免在提示词渲染的热路径上维护额外的链表。
    """
    if len(_scope_cache) >= _SCOPE_CACHE_MAX_ENTRIES:
        stale = sorted(_scope_cache.items(), key=lambda item: item[1][0])[: _SCOPE_CACHE_MAX_ENTRIES // 2]
        for stale_key, _ in stale:
            _scope_cache.pop(stale_key, None)
    _scope_cache[key] = (time.monotonic() + _SCOPE_CACHE_TTL_SECONDS, signature, scope)


def clear_scope_cache() -> None:
    """清空解析缓存（测试与配置变更时用）"""
    _scope_cache.clear()


def _normalize_set_rules(
    persona_set_rules: Sequence[Tuple[int, Sequence[int]]],
) -> Tuple[Tuple[int, Tuple[int, ...]], ...]:
    """把人设套组规则规整成可比较、可缓存的形状"""
    return tuple(
        (int(preset_id), tuple(int(set_id) for set_id in set_ids))
        for preset_id, set_ids in persona_set_rules
    )


def _empty_scope(*, adapter_key: str = "", instance_key: str = "", is_platform: bool = False) -> EmotionScope:
    """「什么都看不到」的那份结果"""
    return EmotionScope(
        resolved=False,
        adapter_key=adapter_key,
        instance_key=instance_key,
        is_platform_instance=is_platform,
        persona_id=0,
        collect_set_id=None,
        readable_set_ids=frozenset(),
        writable_set_ids=frozenset(),
    )


async def instance_pool_set_ids(adapter_key: str, instance_key: str, *, is_platform_instance: bool) -> Set[int]:
    """一个实例的「可用池」：收集库 ∪ 分给所有实例的 ∪（平台方实例时）分给平台方实例的 ∪ 分给它的

    收集库自己不需要分配，所以这里要把实例对的 `collect` 套组也算进来。
    """
    pool: Set[int] = set()
    collect = await DBEmotionSet.filter(
        kind=KIND_COLLECT,
        owner_adapter_key=adapter_key,
        owner_instance_key=instance_key,
    ).first()
    if collect is not None:
        pool.add(collect.id)

    targets = [TARGET_ALL_INSTANCES]
    if is_platform_instance:
        targets.append(TARGET_PLATFORM_INSTANCES)
    assigned = await DBEmotionSetAssignment.filter(target__in=targets).values_list("set_id", flat=True)
    pool.update(int(set_id) for set_id in assigned)

    if instance_key:
        per_instance = await DBEmotionSetAssignment.filter(
            target=TARGET_INSTANCE,
            adapter_key=adapter_key,
            instance_key=instance_key,
        ).values_list("set_id", flat=True)
        pool.update(int(set_id) for set_id in per_instance)
    return pool


async def persona_set_ids(persona_id: int) -> Set[int]:
    """分配给人设的套组；0（内置人设）不匹配任何分配"""
    if not persona_id:
        return set()
    assigned = await DBEmotionSetAssignment.filter(target=TARGET_PRESET, preset_id=persona_id).values_list(
        "set_id",
        flat=True,
    )
    return {int(set_id) for set_id in assigned}


async def _effective_persona_id(channel: Optional[DBChatChannel]) -> int:
    """当前生效人设的 ID；内置人设（库里没有那一行）为 0

    判据复用 `DBChatChannel.get_effective_preset_id()` 的回退链（频道 → 实例默认 → 系统默认 →
    内置），与记忆、好感的隔离键同一个来源。频道行缺失时按内置人设处理。
    """
    if channel is None:
        return 0
    try:
        return int(await channel.get_effective_preset_id())
    except Exception as e:  # 人设解析失败不该让整个表情库不可用，按内置人设处理
        logger.warning(f"解析当前人设失败，按内置人设处理: error={e}")
        return 0


async def _build_scope(
    owner_resolved: bool,
    adapter_key: str,
    instance_key: str,
    is_platform: bool,
    chat_key: str,
    *,
    enabled_sets: Sequence[int],
    persona_set_rules: Sequence[Tuple[int, Sequence[int]]],
) -> EmotionScope:
    """按归属与插件配置算出可读 / 可写集合

    - 解析不了、多租户部署：三个集合全空（fail-closed）；
    - 解析不了、单租户部署：按平台方，能读「分给所有实例」与「分给平台方实例」的，但没有
      本实例、没有收集库、没有可写集合；
    - 频道行没有实例段：同上（按平台方、没有收集库），但它解析得出归属。

    `chat_key` 只用来取那条频道行的人设，值来自 `resource_scope`（宿主签发），不是模型参数。
    """
    if not owner_resolved:
        if not is_platform:
            return _empty_scope()
        readable = await _platform_only_readable()
        return EmotionScope(
            resolved=False,
            adapter_key=adapter_key,
            instance_key=instance_key,
            is_platform_instance=True,
            persona_id=0,
            collect_set_id=None,
            readable_set_ids=frozenset(readable),
            writable_set_ids=frozenset(),
        )

    if not instance_key:
        readable = await _platform_only_readable()
        return EmotionScope(
            resolved=True,
            adapter_key=adapter_key,
            instance_key="",
            is_platform_instance=True,
            persona_id=0,
            collect_set_id=None,
            readable_set_ids=frozenset(readable),
            writable_set_ids=frozenset(),
        )

    collect = await get_or_create_collect_set(adapter_key, instance_key)
    collect_set_id = collect.id
    pool = await instance_pool_set_ids(adapter_key, instance_key, is_platform_instance=is_platform)

    persona_id = await _effective_persona_id(await _channel_for(chat_key, adapter_key, instance_key))
    persona_ids = await persona_set_ids(persona_id)

    # 本实例的挑选：人设规则优先于 ENABLED_SETS；两者都与可用池取交集，配置里写了池外的
    # ID 一律忽略——配置不能给自己授权。
    rules = _normalize_set_rules(persona_set_rules)
    rule_for_persona = next((set_ids for preset_id, set_ids in rules if persona_id and preset_id == persona_id), None)
    if rule_for_persona is not None:
        picked = set(rule_for_persona) & pool
    elif enabled_sets:
        picked = {int(set_id) for set_id in enabled_sets} & pool
    else:
        picked = set(pool)

    readable = set(picked) | persona_ids
    readable.add(collect_set_id)

    return EmotionScope(
        resolved=True,
        adapter_key=adapter_key,
        instance_key=instance_key,
        is_platform_instance=is_platform,
        persona_id=persona_id,
        collect_set_id=collect_set_id,
        readable_set_ids=frozenset(readable),
        writable_set_ids=frozenset({collect_set_id}),
    )


async def _platform_only_readable() -> Set[int]:
    """没有本实例时能读到的：分给所有实例的 ∪ 分给平台方实例的"""
    assigned = await DBEmotionSetAssignment.filter(
        target__in=[TARGET_ALL_INSTANCES, TARGET_PLATFORM_INSTANCES],
    ).values_list("set_id", flat=True)
    return {int(set_id) for set_id in assigned}


async def _channel_for(chat_key: str, adapter_key: str, instance_key: str) -> Optional[DBChatChannel]:
    """取用于解析人设的频道行

    优先用解析归属时那条确切的频道（`chat_key` 来自 `resource_scope`，是宿主签发的）；
    拿不到时退到「这个实例对下 ID 最小的那条」，只为拿到实例默认人设的回退结果——同一个实例
    下的频道在没有各自绑定人设时解析出的是同一个人设。
    """
    if chat_key:
        row = await DBChatChannel.filter(chat_key=chat_key).first()
        if row is not None:
            return row
    return (
        await DBChatChannel.filter(adapter_key=adapter_key, instance_key=instance_key)
        .order_by("id")
        .first()
    )


async def resolve_emotion_scope(
    ctx: Any,
    *,
    enabled_sets: Sequence[int],
    persona_set_rules: Sequence[Tuple[int, Sequence[int]]],
) -> EmotionScope:
    """按会话上下文解析可读 / 可写集合

    身份**只**从 `services/resource_scope.resolve_session_owner(ctx)` 取（它只认宿主签发的
    `from_chat_key`），不读模型传入的任何参数。

    `ctx` 上有非空的 `one_time_code` 时按它缓存 60 秒：同一轮里提示词渲染、多个方法调用都
    会问一次可读集合，每次重算要查人设回退链与几条分配查询。

    Args:
        ctx: 沙盒方法 / 提示词注入 / 命令收到的上下文
        enabled_sets: 插件配置 `ENABLED_SETS`，空表示可用池全部
        persona_set_rules: 插件配置 `PERSONA_SET_RULES`，每项 `(人设 ID, 套组 ID 列表)`

    Returns:
        解析结果；解析不了时的口径见 :class:`EmotionScope`
    """
    from nekro_agent.services.resource_scope import resolve_session_owner

    owner = await resolve_session_owner(ctx)

    cache_key = getattr(ctx, "one_time_code", "") or ""
    signature = (
        owner.resolved,
        owner.adapter_key,
        owner.instance_key,
        owner.platform,
        tuple(int(set_id) for set_id in enabled_sets),
        _normalize_set_rules(persona_set_rules),
    )
    if cache_key:
        cached = _cache_lookup(cache_key, signature)
        if cached is not None:
            return cached

    scope = await _build_scope(
        owner.resolved,
        owner.adapter_key,
        owner.instance_key,
        owner.platform,
        owner.chat_key,
        enabled_sets=enabled_sets,
        persona_set_rules=persona_set_rules,
    )
    if cache_key:
        _cache_store(cache_key, signature, scope)
    return scope


async def resolve_emotion_scope_for_chat(
    chat_key: str,
    *,
    enabled_sets: Sequence[int],
    persona_set_rules: Sequence[Tuple[int, Sequence[int]]],
) -> EmotionScope:
    """按频道标识解析可读 / 可写集合（命令入口用）

    身份走同模块的 `resolve_chat_owner(chat_key)`，与 `resolve_emotion_scope` 同一判据，
    只是入口不同：命令手上只有它所在频道的标识。
    """
    from nekro_agent.services.resource_scope import resolve_chat_owner

    owner = await resolve_chat_owner(chat_key)
    return await _build_scope(
        owner.resolved,
        owner.adapter_key,
        owner.instance_key,
        owner.platform,
        owner.chat_key,
        enabled_sets=enabled_sets,
        persona_set_rules=persona_set_rules,
    )


# ---------------------------------------------------------------------------
# 套组与分配
# ---------------------------------------------------------------------------

# 「先查后建」的进程内锁：每个实例对最多一个收集库。跨进程的兜底靠 `get_or_create` 的冲突捕获
# （`DBEmotionSet` 上没有唯一约束，理由见模型模块的文档）。
_collect_locks: Dict[Tuple[str, str], asyncio.Lock] = {}
_collect_locks_guard = asyncio.Lock()


async def _collect_lock(adapter_key: str, instance_key: str) -> asyncio.Lock:
    async with _collect_locks_guard:
        lock = _collect_locks.get((adapter_key, instance_key))
        if lock is None:
            lock = asyncio.Lock()
            _collect_locks[(adapter_key, instance_key)] = lock
        return lock


async def get_or_create_collect_set(adapter_key: str, instance_key: str) -> DBEmotionSet:
    """取这个实例对的收集库，没有就建一个

    锁里「先查后建」：两个会话同时第一次收集时不会各建一个收集库（那会让一半表情落进一个
    谁都看不到的库里）。名称固定为 `COLLECT_SET_DEFAULT_NAME`，归属列写实例对。
    """
    if not instance_key:
        raise ValueError("收集库必须属于一个实例")

    lock = await _collect_lock(adapter_key, instance_key)
    async with lock:
        existing = (
            await DBEmotionSet.filter(
                kind=KIND_COLLECT,
                owner_adapter_key=adapter_key,
                owner_instance_key=instance_key,
            )
            .order_by("id")
            .first()
        )
        if existing is not None:
            return existing
        return await DBEmotionSet.create(
            kind=KIND_COLLECT,
            name=COLLECT_SET_DEFAULT_NAME,
            category="",
            description="",
            owner_adapter_key=adapter_key,
            owner_instance_key=instance_key,
        )


async def ensure_legacy_set() -> DBEmotionSet:
    """取存量套组，没有就建一个并加一条「分给平台方实例」的分配

    存量 JSON 只记了来源路径，没记是哪个频道 / 实例收的，所以只能整体进一个平台方套组；
    默认只给平台方实例看，租户实例看不到这些来路不明的图。
    """
    existing = await DBEmotionSet.filter(kind=KIND_LEGACY).order_by("id").first()
    if existing is not None:
        return existing
    created = await DBEmotionSet.create(
        kind=KIND_LEGACY,
        name=LEGACY_SET_NAME,
        category=LEGACY_SET_CATEGORY,
        description="从旧版表情库导入，整理完可以拆进预设套组后删除",
        owner_adapter_key="",
        owner_instance_key="",
    )
    await add_assignment(created.id, target=TARGET_PLATFORM_INSTANCES)
    return created


async def create_preset_set(
    *,
    name: str,
    category: str = "",
    description: str = "",
    created_by: str = "",
) -> DBEmotionSet:
    """建一个平台方预设套组

    权限在调用方（控制台接口）判：服务层不判身份，只保证落下来的行形状正确。
    """
    return await DBEmotionSet.create(
        kind=KIND_PRESET,
        name=name.strip() or "未命名套组",
        category=category.strip(),
        description=description,
        owner_adapter_key="",
        owner_instance_key="",
        created_by=created_by,
    )


async def update_set(
    set_id: int,
    *,
    name: Optional[str] = None,
    category: Optional[str] = None,
    description: Optional[str] = None,
    sort: Optional[int] = None,
    updated_by: str = "",
) -> DBEmotionSet:
    """改套组的展示属性；`None` 表示不动这一项"""
    row = await DBEmotionSet.get_or_none(id=set_id)
    if row is None:
        raise ValueError(f"表情套组不存在: {set_id}")
    if name is not None:
        row.name = name.strip() or row.name
    if category is not None:
        row.category = category.strip()
    if description is not None:
        row.description = description
    if sort is not None:
        row.sort = int(sort)
    if updated_by:
        row.updated_by = updated_by
    await row.save()
    return row


async def delete_set(set_id: int) -> Dict[str, int]:
    """删一个预设 / 存量套组：删行（级联删分配与表情行）、删向量、按引用计数删文件

    收集库不能从这里删：它跟着实例走，删实例时由 `purge_instance_emotions` 处理；允许这里删
    会让「本实例收集库」在会话还在用的时候消失，之后又被自动建一个空的，表情行全部落进
    一个没人指向的套组。

    Returns:
        计数：`assignments`（随套组消失的分配）、`items`（表情行）、`vectors`（删掉的向量点）、
        `files`（真正删掉的文件）
    """
    row = await DBEmotionSet.get_or_none(id=set_id)
    if row is None:
        raise ValueError(f"表情套组不存在: {set_id}")
    if row.kind not in (KIND_PRESET, KIND_LEGACY):
        raise ValueError("只有预设套组与存量套组可以删除")

    assignments = await DBEmotionSetAssignment.filter(set_id=set_id).count()
    items = await DBEmotionItem.filter(set_id=set_id).all()
    counts = {
        "assignments": int(assignments),
        "items": len(items),
        "vectors": 0,
        "files": 0,
    }
    emotion_ids = [item.emotion_id for item in items]
    file_paths = {item.file_path for item in items if item.file_path}

    await DBEmotionSet.filter(id=set_id).delete()

    counts["vectors"] = await vector_delete(emotion_ids)
    counts["files"] = await _delete_files_if_unreferenced(file_paths)
    logger.info(
        f"删除表情套组: set_id={set_id}, kind={row.kind}, "
        f"items={counts['items']}, vectors={counts['vectors']}, files={counts['files']}",
    )
    return counts


def _validate_assignment_target(
    *,
    target: str,
    adapter_key: str,
    instance_key: str,
    preset_id: int,
) -> None:
    """分配目标的形状检查：目标必须在四个值里，`instance` 要带实例对，`preset` 要带人设 ID"""
    if target not in ASSIGNMENT_TARGETS:
        raise ValueError(f"未知的分配目标: {target}")
    if target == TARGET_INSTANCE and not instance_key:
        raise ValueError("分配给某个实例时必须带适配器标识与实例标识")
    if target == TARGET_PRESET and preset_id <= 0:
        raise ValueError("分配给人设时人设 ID 必须大于 0")


async def add_assignment(
    set_id: int,
    *,
    target: str,
    adapter_key: str = "",
    instance_key: str = "",
    preset_id: int = 0,
    created_by: str = "",
) -> DBEmotionSetAssignment:
    """加一条分配；重复添加返回已有那条

    `collect` 套组拒绝分配：它属于单个实例，能被分配等于把别人的收集库开给别的实例读。
    """
    row = await DBEmotionSet.get_or_none(id=set_id)
    if row is None:
        raise ValueError(f"表情套组不存在: {set_id}")
    if row.kind not in ASSIGNABLE_KINDS:
        raise ValueError("只有预设套组与存量套组可以分配")

    _validate_assignment_target(
        target=target,
        adapter_key=adapter_key,
        instance_key=instance_key,
        preset_id=preset_id,
    )
    if target != TARGET_INSTANCE:
        adapter_key, instance_key = "", ""
    if target != TARGET_PRESET:
        preset_id = 0

    existing = await DBEmotionSetAssignment.filter(
        set_id=set_id,
        target=target,
        adapter_key=adapter_key,
        instance_key=instance_key,
        preset_id=preset_id,
    ).first()
    if existing is not None:
        return existing

    try:
        return await DBEmotionSetAssignment.create(
            set_id=set_id,
            target=target,
            adapter_key=adapter_key,
            instance_key=instance_key,
            preset_id=preset_id,
            created_by=created_by,
        )
    except Exception as e:
        # 并发下两个请求都查不到、都去插，唯一约束会挡掉后一个；取回已经落下的那条
        logger.warning(f"写入套组分配冲突，取回已有记录: set_id={set_id}, target={target}, error={e}")
        existing = await DBEmotionSetAssignment.filter(
            set_id=set_id,
            target=target,
            adapter_key=adapter_key,
            instance_key=instance_key,
            preset_id=preset_id,
        ).first()
        if existing is None:
            raise
        return existing


async def remove_assignment(assignment_id: int) -> None:
    """删一条分配；不存在时什么都不做（控制台重复点删除不该报错）"""
    row = await DBEmotionSetAssignment.get_or_none(id=assignment_id)
    if row is not None:
        await row.delete()


async def list_sets_visible_to_instance(
    adapter_key: str,
    instance_key: str,
    *,
    is_platform_instance: bool,
) -> List[DBEmotionSet]:
    """控制台用：这个实例的会话能读到的全部套组（不含未分配的预设套组）"""
    pool = await instance_pool_set_ids(adapter_key, instance_key, is_platform_instance=is_platform_instance)
    if not pool:
        return []
    return await DBEmotionSet.filter(id__in=sorted(pool)).order_by("sort", "id")


# ---------------------------------------------------------------------------
# 表情行
# ---------------------------------------------------------------------------


def generate_emotion_id() -> str:
    """生成一个 8 位小写十六进制的表情 ID（与存量格式一致，向量点 ID 是 `int(id, 16)`）"""
    return secrets.token_hex(4)


async def allocate_emotion_id() -> str:
    """生成一个库里还没用过的表情 ID

    撞号概率极低（16^8），但唯一约束会把它变成一次失败写入；这里先查重，撞了重来，
    最多 5 次。极端并发下仍可能撞（查完到写入之间），`create_item` 会再兜一次。
    """
    for _ in range(5):
        candidate = generate_emotion_id()
        if not await DBEmotionItem.filter(emotion_id=candidate).exists():
            return candidate
    raise RuntimeError("生成表情 ID 连续撞号，放弃本次操作")


async def get_readable_item(scope: EmotionScope, emotion_id: str) -> Optional[DBEmotionItem]:
    """在可读集合里按 ID 取一条表情；不在可读里返回 None（调用方按不存在回答）"""
    if not scope.readable_set_ids or not emotion_id:
        return None
    return await DBEmotionItem.filter(emotion_id=emotion_id, set_id__in=sorted(scope.readable_set_ids)).first()


async def get_writable_item(scope: EmotionScope, emotion_id: str) -> Tuple[Optional[DBEmotionItem], Optional[str]]:
    """取一条可写的表情，并给出不可写时的原因

    Returns:
        可写 → `(item, None)`；在可读里但不可写 → `(None, PLATFORM_READONLY_MESSAGE)`；
        其余（包括不存在与越权）→ `(None, emotion_not_found_message(emotion_id))`
    """
    item = await get_readable_item(scope, emotion_id)
    if item is None:
        return None, emotion_not_found_message(emotion_id)
    if item.set_id not in scope.writable_set_ids:
        return None, PLATFORM_READONLY_MESSAGE
    return item, None


async def find_readable_by_sha256(scope: EmotionScope, sha256: str) -> Optional[DBEmotionItem]:
    """在可读集合里按内容哈希找同一张图

    只查可读集合：跨库命中不能拿去改别人的行（改造前正是这样把别人收集的表情改写了）。
    命中后调用方直接返回那条的 ID，不动它。
    """
    if not sha256 or not scope.readable_set_ids:
        return None
    return (
        await DBEmotionItem.filter(file_sha256=sha256, set_id__in=sorted(scope.readable_set_ids))
        .order_by("id")
        .first()
    )


async def create_item(
    *,
    set_id: int,
    description: str,
    tags: List[str],
    file_path: str,
    file_sha256: str,
    source_path: str = "",
    collected_chat_key: str = "",
) -> DBEmotionItem:
    """新建一条表情行

    只写库表、不碰向量与文件：嵌入要由插件生成（服务层不调模型），文件由调用方先落盘。
    `emotion_id` 唯一约束冲突（极端并发）时重新分配 ID 重试一次。
    """
    now = int(time.time())
    payload = {
        "set_id": set_id,
        "description": description,
        "tags": list(tags),
        "file_path": file_path,
        "file_sha256": file_sha256,
        "source_path": source_path,
        "collected_chat_key": collected_chat_key,
        "added_at": now,
        "updated_at": now,
    }
    for attempt in range(2):
        emotion_id = await allocate_emotion_id()
        try:
            return await DBEmotionItem.create(emotion_id=emotion_id, **payload)
        except Exception as e:
            if attempt == 0:
                logger.warning(f"表情 ID 写入冲突，重新分配后重试: emotion_id={emotion_id}, error={e}")
                continue
            raise
    raise RuntimeError("表情行写入失败")


async def update_item(
    item: DBEmotionItem,
    *,
    description: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> DBEmotionItem:
    """改描述 / 标签；`None` 表示不动这一项"""
    if description is not None:
        item.description = description
    if tags is not None:
        item.tags = list(tags)
    item.updated_at = int(time.time())
    await item.save()
    return item


async def _delete_files_if_unreferenced(file_paths: Iterable[str]) -> int:
    """按引用计数删文件：还有别的行指着同一个路径时不动它

    内容寻址之后「同一张图只存一份」是常态，删一行的文件会让别的套组里的同一条表情变成坏图。
    """
    deleted = 0
    for rel_path in {path for path in file_paths if path}:
        still_used = await DBEmotionItem.filter(file_path=rel_path).exists()
        if still_used:
            continue
        resolved = resolve_emotion_file(rel_path)
        if resolved is None:
            continue
        try:
            if resolved.is_file():
                resolved.unlink()
                deleted += 1
        except OSError as e:
            logger.warning(f"删除表情文件失败: path={rel_path}, error={e}")
    return deleted


async def delete_item(item: DBEmotionItem) -> None:
    """删一条表情：删行 → 删向量点 → 文件只在没有别的行引用同一路径时才删"""
    rel_path = item.file_path
    emotion_id = item.emotion_id
    await item.delete()
    await vector_delete([emotion_id])
    await _delete_files_if_unreferenced([rel_path])


async def move_items(emotion_ids: Sequence[str], target_set_id: int) -> int:
    """把一批表情移到另一个套组：改 `set` 外键，提交后批量改向量载荷里的 `set_id`

    向量同步失败只记 WARNING、不抛：库表是权威，搜索时的库表复核与下次启动的回填会补上。
    """
    ids = [emotion_id for emotion_id in emotion_ids if emotion_id]
    if not ids:
        return 0
    target = await DBEmotionSet.get_or_none(id=target_set_id)
    if target is None:
        raise ValueError(f"表情套组不存在: {target_set_id}")

    rows = await DBEmotionItem.filter(emotion_id__in=ids).all()
    moved: List[str] = []
    for row in rows:
        row.set_id = target_set_id
        row.updated_at = int(time.time())
        await row.save()
        moved.append(row.emotion_id)
    if moved:
        await vector_set_set_id(moved, target_set_id)
    return len(moved)


async def copy_items(emotion_ids: Sequence[str], target_set_id: int) -> List[str]:
    """把一批表情复制到另一个套组：新行、新 ID、同一个文件；向量复制源点的向量与载荷

    源点不存在（向量库落后于库表）时跳过并记 WARNING：新行照样建出来，只是暂时搜不到，
    下次回填 / 重建会补上。文件不复制——内容寻址下两份行指向同一个文件是设计的一部分。
    """
    ids = [emotion_id for emotion_id in emotion_ids if emotion_id]
    if not ids:
        return []
    target = await DBEmotionSet.get_or_none(id=target_set_id)
    if target is None:
        raise ValueError(f"表情套组不存在: {target_set_id}")

    sources = await DBEmotionItem.filter(emotion_id__in=ids).all()
    created: List[str] = []
    for source in sources:
        new_id = await allocate_emotion_id()
        now = int(time.time())
        try:
            row = await DBEmotionItem.create(
                emotion_id=new_id,
                set_id=target_set_id,
                description=source.description,
                tags=list(source.tags or []),
                source_path=source.source_path,
                file_path=source.file_path,
                file_sha256=source.file_sha256,
                collected_chat_key=source.collected_chat_key,
                added_at=now,
                updated_at=now,
            )
        except Exception as e:
            logger.warning(f"复制表情失败: source={source.emotion_id}, error={e}")
            continue
        created.append(row.emotion_id)
        await _copy_vector_point(source.emotion_id, row.emotion_id, target_set_id)
    return created


async def _copy_vector_point(source_id: str, new_id: str, set_id: int) -> None:
    """取源点的向量与载荷，换 ID 与 `set_id` 写一个新点；源点不存在就跳过"""
    client = await _qdrant()
    if client is None:
        return
    try:
        records = await client.retrieve(
            collection_name=EMOTION_VECTOR_COLLECTION,
            ids=[_point_id(source_id)],
            with_payload=True,
            with_vectors=True,
        )
    except Exception as e:
        logger.warning(f"读取源表情向量失败，跳过向量复制: source={source_id}, error={e}")
        return
    if not records:
        logger.warning(f"源表情没有向量点，跳过向量复制: source={source_id}")
        return
    record = records[0]
    vector = record.vector
    if vector is None:
        logger.warning(f"源表情向量为空，跳过向量复制: source={source_id}")
        return
    payload = dict(record.payload or {})
    payload["emotion_id"] = new_id
    payload["set_id"] = int(set_id)
    await _upsert_points([(new_id, vector, payload)])


async def recent_items(set_id: int, limit: int) -> List[DBEmotionItem]:
    """一个套组里最近加入的几条（按 `added_at` 倒序）"""
    if limit <= 0:
        return []
    return await DBEmotionItem.filter(set_id=set_id).order_by("-added_at", "-id").limit(limit)


async def count_items_by_set(set_ids: Iterable[int]) -> Dict[int, int]:
    """每个套组里有多少条表情；没查到的套组不出现在结果里（调用方用 `get` 取默认 0）"""
    ids = sorted({int(set_id) for set_id in set_ids})
    if not ids:
        return {}
    rows = (
        await DBEmotionItem.filter(set_id__in=ids)
        .annotate(cnt=Count("id"))
        .group_by("set_id")
        .values("set_id", "cnt")
    )
    return {int(row["set_id"]): int(row["cnt"]) for row in rows}


async def list_items(
    set_ids: Iterable[int],
    *,
    offset: int = 0,
    limit: int = 50,
    keyword: str = "",
) -> Tuple[int, List[DBEmotionItem]]:
    """分页列出套组里的表情，返回 `(总数, 当前页)`

    `keyword` 只在描述与标签里做包含匹配。标签是 JSON 列，各后端的 JSON 查询语法不同，
    所以这里把候选行取出来在 Python 里过一遍——控制台的分页规模在几百条量级，够用且行为一致。
    """
    ids = sorted({int(set_id) for set_id in set_ids})
    if not ids:
        return 0, []
    query = DBEmotionItem.filter(set_id__in=ids)
    needle = keyword.strip().lower()
    if not needle:
        total = await query.count()
        page = await query.order_by("-added_at", "-id").offset(max(0, offset)).limit(max(0, limit))
        return total, page

    rows = await query.order_by("-added_at", "-id")
    matched = [row for row in rows if _matches_keyword(row, needle)]
    total = len(matched)
    start = max(0, offset)
    return total, matched[start : start + max(0, limit)]


def _matches_keyword(row: DBEmotionItem, needle: str) -> bool:
    """描述或任一标签包含关键词（大小写不敏感）"""
    if needle in (row.description or "").lower():
        return True
    return any(needle in str(tag).lower() for tag in (row.tags or []))


# ---------------------------------------------------------------------------
# 文件：按内容哈希
# ---------------------------------------------------------------------------


def sha256_of_bytes(data: bytes) -> str:
    """一段内容的 sha256（十六进制小写）"""
    return hashlib.sha256(data).hexdigest()


def sha256_of_file(path: Path) -> str:
    """一个文件的 sha256；读不到时抛 `OSError`，由调用方决定怎么记"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def detect_image_extension(data: bytes) -> Optional[str]:
    """按文件头识别图片类型，返回不带点的扩展名；认不出返回 None

    不信任调用方给的文件名：扩展名决定浏览器怎么解释这份内容，认错了等于把一张图当别的类型
    发出去。这里只认几种表情图会用到的格式。
    """
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"BM"):
        return "bmp"
    return None


def store_bytes_by_hash(data: bytes, ext: str) -> Tuple[str, str]:
    """把一份内容写进 `EMOTION_FILES_DIR/h/<sha256>.<ext>`，返回 `(相对路径, sha256)`

    已经存在就不重写：同一张图只存一份，重复收集不产生第二份文件，也不改动已有文件的
    修改时间（控制台与备份按它判断变化）。`ext` 由调用方用 `detect_image_extension` 认出，
    这里只做小写与去点，空值退到 `bin`。
    """
    digest = sha256_of_bytes(data)
    suffix = (ext or "").strip().lower().lstrip(".") or "bin"
    rel_path = f"{EMOTION_HASH_SUBDIR}/{digest}.{suffix}"
    target = EMOTION_FILES_DIR / EMOTION_HASH_SUBDIR / f"{digest}.{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(data)
    return rel_path, digest


def resolve_emotion_file(rel_path: str) -> Optional[Path]:
    """把库表里的相对路径解析成插件表情目录下的绝对路径

    支持两种形态：存量平铺文件名（`xxx_ab12cd.png`）与内容寻址的 `h/<sha256>.<ext>`。
    存量 JSON 里可能是绝对路径（`emo_migrate` 修过一部分），这里只取文件名部分，与插件原来
    `resolve_emotion_file_path` 的口径一致。

    解析结果必须仍在 `EMOTION_FILES_DIR` 之内：`../` 之类的相对路径能把读取带到数据目录的
    任何地方，库表里的值来自历史数据与外部输入，不能信。
    """
    if not rel_path:
        return None
    raw = Path(rel_path)
    if raw.anchor or raw.drive:
        # 绝对路径只取文件名：存量里记的是迁移前的宿主路径，文件本身已经在表情目录下。
        # 用 `anchor` 而不是 `is_absolute()`：Windows 上 `/srv/...` 有根没有盘符，
        # `is_absolute()` 为假，但它同样是一个"从根开始"的路径，不能直接拼到目录后面。
        raw = Path(raw.name)
    root = EMOTION_FILES_DIR.resolve()
    candidate = (EMOTION_FILES_DIR / raw).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        logger.warning(f"表情文件路径越界，已拒绝: {rel_path}")
        return None
    return candidate


# ---------------------------------------------------------------------------
# 向量
# ---------------------------------------------------------------------------


async def _qdrant() -> Any:
    """取 Qdrant 客户端；不可用时返回 None（调用方记 debug 后跳过）"""
    try:
        return await get_qdrant_client()
    except Exception as e:
        logger.warning(f"Qdrant 客户端初始化失败，跳过表情向量操作: error={e}")
        return None


def _point_id(emotion_id: str) -> int:
    """表情 ID 到向量点 ID：`int(emotion_id, 16)`，与改造前一致"""
    return int(emotion_id, 16)


def _emotion_payload(emotion_id: str, *, set_id: int, description: str, tags: List[str]) -> Dict[str, Any]:
    """一个表情点的载荷：`set_id` 是搜索过滤的依据，其余给结果展示用"""
    return {
        "emotion_id": emotion_id,
        "set_id": int(set_id),
        "description": description,
        "tags": list(tags),
    }


async def _upsert_points(points: List[Tuple[str, Any, Dict[str, Any]]]) -> bool:
    """批量写点；集合不存在或写入失败只记 WARNING（集合由插件初始化时建），返回是否写成"""
    if not points:
        return True
    client = await _qdrant()
    if client is None:
        return False
    try:
        await client.upsert(
            collection_name=EMOTION_VECTOR_COLLECTION,
            points=[
                qdrant_models.PointStruct(id=_point_id(emotion_id), vector=vector, payload=payload)
                for emotion_id, vector, payload in points
            ],
            wait=True,
        )
    except Exception as e:
        logger.warning(f"表情向量写入失败: count={len(points)}, error={e}")
        return False
    return True


async def vector_upsert(
    emotion_id: str,
    vector: List[float],
    *,
    set_id: int,
    description: str,
    tags: List[str],
) -> None:
    """写入 / 覆盖一个表情的向量点（嵌入由插件生成后传进来）"""
    await _upsert_points(
        [(emotion_id, list(vector), _emotion_payload(emotion_id, set_id=set_id, description=description, tags=tags))],
    )


async def vector_delete(emotion_ids: Sequence[str]) -> int:
    """删一批向量点，返回请求删掉的个数

    集合不存在时按 0 处理、不抛：表情库可能在向量库还没建好时就被清理（例如删实例）。
    """
    ids = [emotion_id for emotion_id in emotion_ids if emotion_id]
    if not ids:
        return 0
    client = await _qdrant()
    if client is None:
        return 0
    try:
        await client.delete(
            collection_name=EMOTION_VECTOR_COLLECTION,
            points_selector=qdrant_models.PointIdsList(points=[_point_id(emotion_id) for emotion_id in ids]),
        )
    except Exception as e:
        logger.warning(f"删除表情向量失败: count={len(ids)}, error={e}")
        return 0
    return len(ids)


@dataclass(frozen=True)
class SetIdSyncResult:
    """一批 `set_id` 改写的结果

    Attributes:
        written: 实际改写了的点数
        missing: 向量库里**没有点**的表情 ID（库表有、向量库没有：嵌入从没写成，或集合被重建过）
        failed: 点存在、但改写仍然失败的个数（向量库抖动等）；下次启动的回填会再试
    """

    written: int
    missing: Tuple[str, ...] = ()
    failed: int = 0


def _is_not_found(error: BaseException) -> bool:
    """Qdrant 回 404（点或集合不存在）"""
    return getattr(error, "status_code", None) == 404


async def _existing_point_ids(client: Any, point_ids: List[int]) -> Optional[set]:
    """这批点里哪些真的在向量库里；查询本身失败返回 None（调用方退到逐条写）

    `retrieve` 对不存在的 ID 不报错、只是不返回，正好拿来先筛一遍。
    """
    try:
        records = await client.retrieve(
            collection_name=EMOTION_VECTOR_COLLECTION,
            ids=point_ids,
            with_payload=False,
            with_vectors=False,
        )
    except Exception as e:
        logger.warning(f"查询表情向量点是否存在失败，改为逐条写 set_id: count={len(point_ids)}, error={e}")
        return None
    found = set()
    for record in records:
        try:
            found.add(int(record.id))
        except (TypeError, ValueError):
            continue
    return found


async def _set_payload_points(client: Any, point_ids: List[int], set_id: int) -> None:
    await client.set_payload(
        collection_name=EMOTION_VECTOR_COLLECTION,
        payload={"set_id": int(set_id)},
        points=point_ids,
        wait=True,
    )


async def _set_payload_one_by_one(
    client: Any,
    point_ids: List[int],
    by_point: Dict[int, str],
    set_id: int,
    known_missing: Sequence[str],
) -> SetIdSyncResult:
    """逐个点写 `set_id`：404 记进 missing，别的失败计数，谁都不拖累别人"""
    written = 0
    failed = 0
    missing = list(known_missing)
    for point_id in point_ids:
        try:
            await _set_payload_points(client, [point_id], set_id)
        except Exception as e:
            if _is_not_found(e):
                missing.append(by_point[point_id])
            else:
                failed += 1
                logger.debug(f"表情向量 set_id 逐条写入失败: emotion_id={by_point[point_id]}, error={e}")
            continue
        written += 1
    if failed:
        logger.warning(f"表情向量 set_id 有 {failed} 个点写入失败（下次启动的回填会再试）")
    return SetIdSyncResult(written=written, missing=tuple(missing), failed=failed)


async def sync_set_id_batch(emotion_ids: Sequence[str], set_id: int) -> SetIdSyncResult:
    """把一批表情的向量载荷 `set_id` 改成给定套组，**跳过向量库里没有的点**

    为什么要先筛：Qdrant 的 `set_payload` 按 ID 列表写时，**批里只要有一个点不存在，整批就回
    404、一个都不写**。存量里总有几条从没写成嵌入的表情（或集合被重建过），于是同一批里剩下
    的点永远拿不到 `set_id`，按套组过滤的搜索就再也搜不到它们——每次启动都卡在同一批上。

    做法：先 `retrieve` 这批 ID（不存在的不报错、只是不返回），只对存在的写；`retrieve` 本身
    失败时退到整批写，整批写再失败就逐条写、把 404 的记进 missing。
    """
    ids = [emotion_id for emotion_id in emotion_ids if emotion_id]
    if not ids:
        return SetIdSyncResult(written=0)
    client = await _qdrant()
    if client is None:
        return SetIdSyncResult(written=0, failed=len(ids))

    by_point: Dict[int, str] = {}
    for emotion_id in ids:
        by_point[_point_id(emotion_id)] = emotion_id
    point_ids = list(by_point)

    existing = await _existing_point_ids(client, point_ids)
    if existing is not None:
        present = [point_id for point_id in point_ids if point_id in existing]
        missing = tuple(by_point[point_id] for point_id in point_ids if point_id not in existing)
        if not present:
            return SetIdSyncResult(written=0, missing=missing)
        try:
            await _set_payload_points(client, present, set_id)
        except Exception as e:
            # 查完到写之间点又被删了，或者向量库抖了一下：退到逐条写
            logger.warning(f"表情向量 set_id 批量写入失败，改为逐条写: count={len(present)}, error={e}")
            return await _set_payload_one_by_one(client, present, by_point, set_id, missing)
        return SetIdSyncResult(written=len(present), missing=missing)

    try:
        await _set_payload_points(client, point_ids, set_id)
    except Exception as e:
        logger.warning(f"表情向量 set_id 整批写入失败，改为逐条写: count={len(point_ids)}, error={e}")
        return await _set_payload_one_by_one(client, point_ids, by_point, set_id, ())
    return SetIdSyncResult(written=len(point_ids))


async def vector_set_set_id(emotion_ids: Sequence[str], set_id: int) -> int:
    """批量改向量载荷里的 `set_id`（套组移动 / 存量导入回填用），返回实际改写的个数

    只覆盖单个载荷字段，向量本身不动，因此不需要重新算嵌入。向量库里没有点的表情跳过
    （见 `sync_set_id_batch`）；失败只记 WARNING：库表是权威，搜索的库表复核会兜住，
    启动回填会补上——缺点的那些也由启动回填重新生成嵌入。
    """
    result = await sync_set_id_batch(emotion_ids, set_id)
    if result.missing:
        logger.info(
            f"表情向量 set_id 同步：{len(result.missing)} 个表情在向量库里没有点，已跳过（启动回填会补上）",
        )
    return result.written


async def vector_search(
    query_vector: List[float],
    *,
    set_ids: Collection[int],
    limit: int,
) -> List[Tuple[str, float]]:
    """按 `set_id ∈ set_ids` 过滤的向量搜索，返回 `[(emotion_id, score)]`

    载荷过滤只用来缩小候选集；调用方**必须**再按库表复核一遍（见模块文档）。
    `set_ids` 为空时直接返回：没有可读套组就没有可搜的东西，不必打扰向量库。
    """
    if not set_ids or limit <= 0:
        return []
    client = await _qdrant()
    if client is None:
        return []
    try:
        hits = await client.search(
            collection_name=EMOTION_VECTOR_COLLECTION,
            query_vector=list(query_vector),
            query_filter=qdrant_models.Filter(
                must=[
                    qdrant_models.FieldCondition(
                        key="set_id",
                        match=qdrant_models.MatchAny(any=sorted({int(set_id) for set_id in set_ids})),
                    ),
                ],
            ),
            limit=max(1, int(limit)),
            with_payload=True,
        )
    except Exception as e:
        logger.warning(f"表情向量搜索失败: set_count={len(set_ids)}, error={e}")
        return []

    results: List[Tuple[str, float]] = []
    for hit in hits:
        payload = hit.payload or {}
        emotion_id = payload.get("emotion_id")
        if not isinstance(emotion_id, str) or not emotion_id:
            # 载荷缺 emotion_id 的点认不出来是哪条表情，宁可丢掉也不猜
            continue
        results.append((emotion_id, float(hit.score)))
    return results


async def resync_vector_set_ids(
    *,
    embed_missing: Optional[Callable[[DBEmotionItem], Awaitable[Optional[List[float]]]]] = None,
    reembed_limit: int = MISSING_POINT_REEMBED_LIMIT,
) -> Dict[str, int]:
    """按库表把每个表情向量点的 `set_id` 载荷重写一遍（启动回填用）

    为什么需要回填：改造前的向量点里没有 `set_id`，而搜索按 `set_id` 过滤——没有这个字段的
    点会**一条都搜不到**。导入时只改了「新插入的那批」，改造前就存在的点得靠这里扫一遍。

    为什么是后台任务：要逐套组读库表、按批问向量库，几万条的表情库能跑几十秒；卡在插件加载
    里等于整个后端起不来。回填是幂等的（`set_payload` 只覆盖一个字段），跑两遍没有副作用，
    中途被打断也只是下次启动再来一次。

    **库表有、向量库没有的点**（嵌入从没写成，或集合被重建过）：`set_payload` 补不了它们。
    嵌入要调模型，核心不知道怎么算，所以由插件传 `embed_missing` 进来（文件已经不在的返回 None，
    与 `emo_reindex` 同口径）；每次启动最多补 `reembed_limit` 条，免得集合被清空过的部署在
    启动时一口气打几百次嵌入模型。补不完的、没传回调的，WARNING 里写明数量与去控制台跑
    `emo_reindex -y`，下次启动也会接着补。

    Returns:
        计数：`sets`（处理了几个套组）、`points`（改写了几个点）、`batches`（提交了几批）、
        `missing`（向量库里没有点的表情数）、`reembedded`（其中重新生成并写回的个数）、
        `failed`（点存在但改写失败的个数）
    """
    counts = {"sets": 0, "points": 0, "batches": 0, "missing": 0, "reembedded": 0, "failed": 0}
    client = await _qdrant()
    if client is None:
        return counts
    try:
        collections = await client.get_collections()
    except Exception as e:
        logger.warning(f"获取向量集合列表失败，跳过表情 set_id 回填: error={e}")
        return counts
    if EMOTION_VECTOR_COLLECTION not in {collection.name for collection in collections.collections}:
        # 集合还没建（首次启动）或已被删：没有点可回填，也不用在这里建一个
        return counts

    missing_ids: List[str] = []
    rows = await DBEmotionSet.all().order_by("id")
    for row in rows:
        set_id = int(row.id)
        emotion_ids = list(
            await DBEmotionItem.filter(set_id=set_id).order_by("id").values_list("emotion_id", flat=True),
        )
        if not emotion_ids:
            continue
        counts["sets"] += 1
        for start in range(0, len(emotion_ids), VECTOR_BATCH_SIZE):
            batch = emotion_ids[start : start + VECTOR_BATCH_SIZE]
            result = await sync_set_id_batch(batch, set_id)
            counts["points"] += result.written
            counts["failed"] += result.failed
            missing_ids.extend(result.missing)
            counts["batches"] += 1

    counts["missing"] = len(missing_ids)
    if missing_ids and embed_missing is not None:
        counts["reembedded"] = await _reembed_missing_points(missing_ids[: max(0, int(reembed_limit))], embed_missing)

    if counts["batches"]:
        logger.info(
            f"表情向量 set_id 回填完成: sets={counts['sets']}, points={counts['points']}, "
            f"batches={counts['batches']}, missing={counts['missing']}, reembedded={counts['reembedded']}",
        )
    remaining = counts["missing"] - counts["reembedded"]
    if remaining > 0:
        logger.warning(
            f"表情向量库里还缺 {remaining} 个点（库表里有这些表情，按套组搜索搜不到它们）。"
            f"每次启动会自动补最多 {int(reembed_limit)} 个（文件已丢失的补不了）；"
            f"想一次补齐，请在控制台运行 emo_reindex -y 重建表情索引",
        )
    return counts


async def _reembed_missing_points(
    emotion_ids: Sequence[str],
    embed_missing: Callable[[DBEmotionItem], Awaitable[Optional[List[float]]]],
) -> int:
    """给向量库里缺点的表情重新生成嵌入并写回（载荷照库表，`set_id` 是行上当前的），返回写成的个数"""
    written = 0
    for emotion_id in emotion_ids:
        item = await DBEmotionItem.get_or_none(emotion_id=emotion_id)
        if item is None:
            continue
        try:
            vector = await embed_missing(item)
        except Exception as e:
            logger.warning(f"补表情向量点时生成嵌入失败: emotion_id={emotion_id}, error={e}")
            continue
        if not vector:
            continue
        payload = _emotion_payload(
            item.emotion_id,
            set_id=int(item.set_id),
            description=item.description or "",
            tags=list(item.tags or []),
        )
        if await _upsert_points([(item.emotion_id, list(vector), payload)]):
            written += 1
    return written


# ---------------------------------------------------------------------------
# 存量导入
# ---------------------------------------------------------------------------


def _json_int(value: Any) -> int:
    """把存量 JSON 里的时间字段读成 int 秒；读不出按 0（调用方语义是「没有」）"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _legacy_file_name(raw: Any) -> str:
    """存量 `file_path` 只留文件名部分（可能是绝对路径），与插件原来那个解析函数同口径"""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    return Path(raw.strip()).name


async def import_legacy_store(raw_json: Optional[str]) -> Dict[str, int]:
    """把旧版那一整块 JSON 导入存量套组

    调用方（插件初始化）从插件存储里读出那一行 JSON 传进来；本函数不读插件存储、不改那一行，
    旧 JSON 因此原样留着，回滚时还能用。

    逻辑：`ensure_legacy_set()`（没有就建，并加一条「分给平台方实例」的分配）；JSON 里每个
    `emotion_id`，表里没有就插一行进存量套组，已有的不动；文件在就补算哈希，不在留空；
    最后对这批**新插入**的 ID 按批改写向量载荷的 `set_id`。

    幂等：重复调用不重复插入（已有的跳过）。JSON 解析失败记 WARNING 返回全 0，不抛——
    一份坏掉的旧数据不该挡住插件启动。

    Returns:
        计数：`total`（JSON 里的条数）、`inserted`、`existing`、`missing_files`、`vector_batches`
    """
    counts = {"total": 0, "inserted": 0, "existing": 0, "missing_files": 0, "vector_batches": 0}
    if not raw_json:
        return counts
    try:
        payload = json.loads(raw_json)
    except (TypeError, ValueError) as e:
        logger.warning(f"存量表情 JSON 解析失败，跳过导入: error={e}")
        return counts
    if not isinstance(payload, dict):
        logger.warning("存量表情 JSON 不是对象，跳过导入")
        return counts

    emotions = payload.get("emotions")
    if not isinstance(emotions, dict):
        emotions = {}
    counts["total"] = len(emotions)
    if not emotions:
        return counts

    legacy = await ensure_legacy_set()
    inserted_ids: List[str] = []
    for raw_id, meta in emotions.items():
        emotion_id = str(raw_id)
        if not emotion_id:
            continue
        if await DBEmotionItem.filter(emotion_id=emotion_id).exists():
            counts["existing"] += 1
            continue
        info = meta if isinstance(meta, dict) else {}
        rel_path = _legacy_file_name(info.get("file_path"))
        file_sha = ""
        if rel_path:
            resolved = resolve_emotion_file(rel_path)
            if resolved is not None and resolved.is_file():
                try:
                    file_sha = sha256_of_file(resolved)
                except OSError as e:
                    logger.warning(f"读取存量表情文件失败: path={rel_path}, error={e}")
            else:
                counts["missing_files"] += 1
        else:
            counts["missing_files"] += 1

        tags = info.get("tags")
        await DBEmotionItem.create(
            emotion_id=emotion_id,
            set_id=legacy.id,
            description=str(info.get("description") or ""),
            tags=list(tags) if isinstance(tags, list) else [],
            source_path=str(info.get("source_path") or ""),
            file_path=rel_path,
            file_sha256=file_sha,
            collected_chat_key="",
            added_at=_json_int(info.get("added_time")),
            updated_at=_json_int(info.get("last_updated")),
        )
        counts["inserted"] += 1
        inserted_ids.append(emotion_id)

    for start in range(0, len(inserted_ids), VECTOR_BATCH_SIZE):
        batch = inserted_ids[start : start + VECTOR_BATCH_SIZE]
        await vector_set_set_id(batch, legacy.id)
        counts["vector_batches"] += 1

    logger.info(
        f"存量表情导入完成: total={counts['total']}, inserted={counts['inserted']}, "
        f"existing={counts['existing']}, missing_files={counts['missing_files']}, "
        f"vector_batches={counts['vector_batches']}",
    )
    return counts


# ---------------------------------------------------------------------------
# 生命周期：实例彻底删除 / 归属迁移 / 划转
# ---------------------------------------------------------------------------

# 白板插件把每块板的只读令牌存在 `plugin_data` 里。核心不能 import 插件，所以键名算法在这里
# 写一份同样的实现，并用例钉住两边一致（`plugins/builtin/whiteboard.py` 的
# `_view_token_store_key`）。
WHITEBOARD_PLUGIN_KEY = "nekro.whiteboard"
WHITEBOARD_VIEW_TOKEN_PREFIX = "view_token:"


def whiteboard_view_token_store_key(adapter_key: str, instance_key: str) -> str:
    """白板只读令牌在插件存储里的键：前缀 + 实例对哈希的前 32 个十六进制字符"""
    digest = hashlib.sha256(f"{adapter_key or ''}\x00{instance_key or ''}".encode("utf-8")).hexdigest()[:32]
    return f"{WHITEBOARD_VIEW_TOKEN_PREFIX}{digest}"


async def _delete_whiteboard_token(adapter_key: str, instance_key: str) -> int:
    """删掉这个实例对的白板只读令牌行，返回删掉的行数

    实例没了，凭旧链接还能看那块板就等于留了一个匿名读口子；令牌是按实例对存的，所以删行
    而不是清值。
    """
    store_key = whiteboard_view_token_store_key(adapter_key, instance_key)
    deleted = await DBPluginData.filter(
        plugin_key=WHITEBOARD_PLUGIN_KEY,
        target_chat_key="",
        target_user_id="",
        data_key=store_key,
    ).delete()
    return int(deleted or 0)


async def revoke_whiteboard_view_token(adapter_key: str, instance_key: str) -> int:
    """实例换了主人（划转）时作废它那块白板的只读链接，返回删掉的行数

    链接是发给前一个主人的：不作废的话，前租户手里的链接能接着看新主人的板。删行之后新主人要用时
    插件会现生成一枚新的；已经连着的浏览器由插件在复核授权时断开（`whiteboard._still_authorized`）。
    """
    return await _delete_whiteboard_token(adapter_key, instance_key)


async def _move_whiteboard_token(adapter_key: str, instance_key: str, target_adapter_key: str, target_instance_key: str) -> int:
    """把只读令牌行改到目标实例对；目标已有令牌就删掉源行（不覆盖已有链接）。返回真正搬过去的行数"""
    source_key = whiteboard_view_token_store_key(adapter_key, instance_key)
    target_key = whiteboard_view_token_store_key(target_adapter_key, target_instance_key)
    if source_key == target_key:
        return 0
    rows = await DBPluginData.filter(
        plugin_key=WHITEBOARD_PLUGIN_KEY,
        target_chat_key="",
        target_user_id="",
        data_key=source_key,
    ).all()
    if not rows:
        return 0
    target_exists = await DBPluginData.filter(
        plugin_key=WHITEBOARD_PLUGIN_KEY,
        target_chat_key="",
        target_user_id="",
        data_key=target_key,
    ).exists()
    if target_exists:
        await DBPluginData.filter(
            plugin_key=WHITEBOARD_PLUGIN_KEY,
            target_chat_key="",
            target_user_id="",
            data_key=source_key,
        ).delete()
        return 0
    for row in rows:
        row.data_key = target_key
        await row.save()
    return len(rows)


async def _clear_collect_set(set_id: int) -> Dict[str, int]:
    """清空一个收集库：删行、删向量、按引用计数删文件，套组本身留着

    「划转时一并清空收集的表情」要的正是这个形状：收集库跟着实例走，实例还在，库也还在，
    只是空了；下次会话进来还是同一个套组 ID，配置里引用的 ID 不会失效。
    """
    items = await DBEmotionItem.filter(set_id=set_id).all()
    counts = {"items": len(items), "vectors": 0, "files": 0}
    if not items:
        return counts
    emotion_ids = [item.emotion_id for item in items]
    file_paths = {item.file_path for item in items if item.file_path}
    await DBEmotionItem.filter(set_id=set_id).delete()
    counts["vectors"] = await vector_delete(emotion_ids)
    counts["files"] = await _delete_files_if_unreferenced(file_paths)
    return counts


async def clear_collect_set(adapter_key: str, instance_key: str) -> Dict[str, int]:
    """清空某个实例的收集库（收集库不存在时返回全 0，不顺手建一个）"""
    collect = await DBEmotionSet.filter(
        kind=KIND_COLLECT,
        owner_adapter_key=adapter_key,
        owner_instance_key=instance_key,
    ).first()
    if collect is None:
        return {"sets": 0, "items": 0, "vectors": 0, "files": 0}
    counts = await _clear_collect_set(collect.id)
    return {"sets": 1, **counts}


async def purge_instance_emotions(
    adapter_key: str,
    instance_key: str,
    *,
    emotion_ids: Optional[Sequence[str]] = None,
    file_paths: Optional[Iterable[str]] = None,
) -> Dict[str, int]:
    """彻底删除一个实例的表情数据

    删这个实例对的收集库（级联删表情行与指向它的分配）、指向这个实例对的 `instance` 分配、
    向量点、文件（按引用计数），外加这块板的白板只读令牌。

    向量与文件在数据库事务之后处理：Qdrant 与文件系统都不参与事务，放进事务里失败会连带
    回滚已经正确的关系型删除（与同文件里记忆向量的做法一致）。调用方负责在事务提交后调用
    本函数。

    Args:
        adapter_key: 实例的适配器标识
        instance_key: 实例标识
        emotion_ids: 调用方已经在事务里删掉表情行时，把这些行的 `emotion_id` 传进来。
            行没了就查不出要删哪些向量点，所以要么由本函数自己查（默认），要么由调用方在
            删行之前取好传进来。
        file_paths: 同上，删行之前取好的 `file_path` 集合。

    Returns:
        计数：`sets` / `items` / `assignments` / `vectors` / `files` / `whiteboard_tokens`；
        传入了 `emotion_ids` / `file_paths` 时 `sets` 与 `items` 为 0（行是调用方删的）
    """
    counts = {"sets": 0, "items": 0, "assignments": 0, "vectors": 0, "files": 0, "whiteboard_tokens": 0}

    pre_collected = emotion_ids is not None
    pending_ids: List[str] = [str(emotion_id) for emotion_id in (emotion_ids or []) if emotion_id]
    pending_files: set[str] = {path for path in (file_paths or []) if path}

    collect = await DBEmotionSet.filter(
        kind=KIND_COLLECT,
        owner_adapter_key=adapter_key,
        owner_instance_key=instance_key,
    ).first()
    if collect is not None:
        if not pre_collected:
            items = await DBEmotionItem.filter(set_id=collect.id).all()
            counts["items"] = len(items)
            pending_ids = [item.emotion_id for item in items]
            pending_files = {item.file_path for item in items if item.file_path}
        counts["sets"] = 1

    # 指向这个实例对的分配：`instance` 目标才带实例对，其余目标不受影响
    assignment_filter = DBEmotionSetAssignment.filter(
        target=TARGET_INSTANCE,
        adapter_key=adapter_key,
        instance_key=instance_key,
    )
    counts["assignments"] = int(await assignment_filter.count())

    if collect is not None:
        # 删套组：表情行与指向它的分配随外键级联删除
        await DBEmotionSet.filter(id=collect.id).delete()
    await assignment_filter.delete()

    counts["vectors"] = await vector_delete(pending_ids)
    counts["files"] = await _delete_files_if_unreferenced(pending_files)
    counts["whiteboard_tokens"] = await _delete_whiteboard_token(adapter_key, instance_key)

    logger.info(
        f"彻底删除实例的表情数据: adapter={adapter_key}, instance={instance_key}, "
        f"sets={counts['sets']}, items={counts['items']}, assignments={counts['assignments']}, "
        f"vectors={counts['vectors']}, files={counts['files']}",
    )
    return counts


async def collect_instance_emotion_refs(adapter_key: str, instance_key: str) -> Tuple[List[str], List[str]]:
    """取这个实例收集库里所有表情的 ID 与文件路径（调用方要在删行**之前**调用）

    `purge_instance` 在事务里删行、提交后再删向量与文件；行删掉之后就查不出该删哪些点了，
    所以引用必须在事务里（删行之前）先取好。
    """
    collect = await DBEmotionSet.filter(
        kind=KIND_COLLECT,
        owner_adapter_key=adapter_key,
        owner_instance_key=instance_key,
    ).first()
    if collect is None:
        return [], []
    rows = await DBEmotionItem.filter(set_id=collect.id).values_list("emotion_id", "file_path")
    ids = [str(emotion_id) for emotion_id, _ in rows]
    files = [str(file_path) for _, file_path in rows if file_path]
    return ids, files


async def migrate_instance_emotions(
    adapter_key: str,
    instance_key: str,
    target_adapter_key: str,
    target_instance_key: str,
) -> Dict[str, int]:
    """把收集库与分配改到目标实例对（换实例对时用）

    目标已经有收集库时把源库的表情行移进去（`move_items`，向量载荷同步改），再删源套组：
    一个实例对只能有一个收集库，留两个会让「本实例收集」的语义分叉。

    Returns:
        计数：`sets`（改归属或合并掉的套组）、`assignments`、`moved_items`、`whiteboard_tokens`
    """
    counts = {"sets": 0, "assignments": 0, "moved_items": 0, "whiteboard_tokens": 0}
    if adapter_key == target_adapter_key and instance_key == target_instance_key:
        return counts

    source = await DBEmotionSet.filter(
        kind=KIND_COLLECT,
        owner_adapter_key=adapter_key,
        owner_instance_key=instance_key,
    ).first()
    if source is not None:
        target = await DBEmotionSet.filter(
            kind=KIND_COLLECT,
            owner_adapter_key=target_adapter_key,
            owner_instance_key=target_instance_key,
        ).first()
        if target is None:
            source.owner_adapter_key = target_adapter_key
            source.owner_instance_key = target_instance_key
            await source.save()
            counts["sets"] += 1
        else:
            moved_ids = list(await DBEmotionItem.filter(set_id=source.id).values_list("emotion_id", flat=True))
            counts["moved_items"] = await move_items([str(emotion_id) for emotion_id in moved_ids], target.id)
            await DBEmotionSet.filter(id=source.id).delete()
            counts["sets"] += 1

    counts["assignments"] = int(
        await DBEmotionSetAssignment.filter(
            target=TARGET_INSTANCE,
            adapter_key=adapter_key,
            instance_key=instance_key,
        ).update(adapter_key=target_adapter_key, instance_key=target_instance_key),
    )

    counts["whiteboard_tokens"] = await _move_whiteboard_token(adapter_key, instance_key, target_adapter_key, target_instance_key)
    logger.info(
        f"表情收集库随实例迁移: {adapter_key}/{instance_key} -> {target_adapter_key}/{target_instance_key}, "
        f"sets={counts['sets']}, assignments={counts['assignments']}, moved_items={counts['moved_items']}",
    )
    return counts


async def count_instance_emotions(adapter_key: str, instance_key: str) -> Dict[str, int]:
    """统计一个实例对的表情规模（划转预览与实例彻底删除确认框用）"""
    collect = await DBEmotionSet.filter(
        kind=KIND_COLLECT,
        owner_adapter_key=adapter_key,
        owner_instance_key=instance_key,
    ).first()
    items = await DBEmotionItem.filter(set_id=collect.id).count() if collect is not None else 0
    assignments = await DBEmotionSetAssignment.filter(
        target=TARGET_INSTANCE,
        adapter_key=adapter_key,
        instance_key=instance_key,
    ).count()
    return {"sets": 1 if collect is not None else 0, "items": int(items), "assignments": int(assignments)}


async def revoke_instance_assignments(adapter_key: str, instance_key: str) -> int:
    """取消平台方指向这个实例对的 `instance` 分配（划转时每次都做）

    划转之后这个实例换了主人，平台方原本「给这个实例」的授权不该自动跟过去——那是给前一个
    租户的。`all_instances` / `platform_instances` 的分配照旧生效。
    """
    deleted = await DBEmotionSetAssignment.filter(
        target=TARGET_INSTANCE,
        adapter_key=adapter_key,
        instance_key=instance_key,
    ).delete()
    return int(deleted or 0)
