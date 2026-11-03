"""
# 冷处理 (Cold Shoulder)

让 AI 在当前会话里**暂时不理某个人**：到期之前，这个人的消息（包括戳一戳）照常写进聊天记录，
但不会再触发 AI 回复。对方再怎么戳，也只是往历史里多添一条记录。

## 作用范围

只影响**当前会话（含实例）里的这一个人**。同一个群里的其他人、别的群、私聊、别的实例都不受影响：
名单按 `chat_key` 存放，而 `chat_key` 自带实例段，因此天然是"每实例每群一份"。

## 记录但不触发

拦截发生在插件的消息回调里，返回 `MsgSignal.BLOCK_TRIGGER`：消息照常落库、进历史，
只是不再往下走"触发 AI 回复"那一步。`@`、戳一戳这类明示触发同样会被拦住。
被拦下的只是回复，不是记忆——AI 之后翻历史仍然看得到这个人当时说了什么。

## 时长

冷处理时长由 AI 按情节自己定：轻度刷戳 10~30 分钟，屡次再犯可以加长。
单次上限由配置项 `MAX_MINUTES` 约束，超出截到上限；AI 传 0 或负数时用 `DEFAULT_MINUTES`。
对同一个人重复调用只会**延长**，不会缩短。

## 查看与修改

名单存在插件存储里，后台频道页的"插件数据"可以直接查看、修改、删除；
AI 也可以用 `unignore_user` 提前解除。频道重置时名单清空。

## 防滥用

只能冷处理近期在本会话发过言或戳过的人，不能凭空针对一个从没出现过的 ID；
超级用户（管理员）永远不能被冷处理。
"""

import time
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from nekro_agent.api import core, i18n
from nekro_agent.api.plugin import (
    ConfigBase,
    ExtraField,
    NekroPlugin,
    SandboxMethodType,
)
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.api.signal import MsgSignal
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.schemas.chat_message import ChatMessage
from nekro_agent.services.plugin.scope_guard import require_own_chat_key
from nekro_agent.services.user.super_users import matches_super_users

plugin = NekroPlugin(
    name="冷处理",
    module_name="cold_shoulder",
    description="让 AI 在当前会话里暂时不理某个人：此人的消息照常记录，但不再触发回复",
    version="0.1.0",
    author="Akiyo_dayo",
    url="https://github.com/Akiyo-dayo/Nekro-Agent-ByAkiyo",
    i18n_name=i18n.i18n_text(zh_CN="冷处理", en_US="Cold Shoulder"),
    i18n_description=i18n.i18n_text(
        zh_CN="让 AI 在当前会话里暂时不理某个人：此人的消息照常记录，但不再触发回复",
        en_US="Lets the AI temporarily ignore someone in the current chat: their messages are still recorded but no longer trigger replies",
    ),
)


@plugin.mount_config()
class ColdShoulderConfig(ConfigBase):
    """冷处理配置"""

    DEFAULT_MINUTES: int = Field(
        default=30,
        title="默认冷处理分钟数",
        description="AI 传入 0 或负数时使用这个分钟数",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(zh_CN="默认冷处理分钟数", en_US="Default duration (minutes)"),
            i18n_description=i18n.i18n_text(
                zh_CN="AI 传入 0 或负数时使用这个分钟数",
                en_US="Used when the AI passes 0 or a negative number",
            ),
        ).model_dump(),
    )
    MAX_MINUTES: int = Field(
        default=120,
        title="单次冷处理上限（分钟）",
        description="单次冷处理的最大分钟数，超出的会被截到这个上限",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(zh_CN="单次冷处理上限（分钟）", en_US="Max duration per call (minutes)"),
            i18n_description=i18n.i18n_text(
                zh_CN="单次冷处理的最大分钟数，超出的会被截到这个上限",
                en_US="Upper bound for a single cold shoulder; longer requests are clamped",
            ),
        ).model_dump(),
    )
    RECENT_ACTIVITY_MINUTES: int = Field(
        default=10,
        title="近期活跃窗口（分钟）",
        description="只能冷处理这么多分钟内在本会话发过言或戳过的人",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(zh_CN="近期活跃窗口（分钟）", en_US="Recent activity window (minutes)"),
            i18n_description=i18n.i18n_text(
                zh_CN="只能冷处理这么多分钟内在本会话发过言或戳过的人",
                en_US="Only people who spoke or poked in this chat within this window can be ignored",
            ),
        ).model_dump(),
    )


config = plugin.get_config(ColdShoulderConfig)
store = plugin.store

# 名单在插件存储里的键。后台频道页的"插件数据"看到的就是这一条
STORE_KEY = "ignored_users"

# 历史只保留 24 小时：提示词里的"第几次"按这个窗口算，也更贴近"最近屡次再犯"的语义
HISTORY_WINDOW_SECONDS = 24 * 3600

# 消息回调位于主对话链路上，每条消息都读一次库不划算。
# 30 秒的 TTL 让同一个人连戳几十下也只查一次库，代价是后台改名单最多延迟半分钟生效
CACHE_TTL_SECONDS = 30.0

# 提示词里最多列几个人；超出的只报个数，避免刷屏
PROMPT_MAX_ENTRIES = 5

# 这些 ID 不对应任何真实用户：空串是缺字段，"0" 与 "-1" 是各适配器里 Bot 自己的占位
_UNTARGETABLE_IDS = frozenset({"", "0", "-1"})


class IgnoredEntry(BaseModel):
    """一条冷处理记录"""

    until: int  # 到期时间戳（秒）
    since: int  # 本轮冷处理开始的时间戳
    reason: str = ""  # 只存不进提示词
    nickname: str = ""  # 只存不进提示词，方便后台辨认


class ColdShoulderState(BaseModel):
    """一个会话的冷处理名单与历史"""

    users: Dict[str, IgnoredEntry] = Field(default_factory=dict)
    history: Dict[str, List[int]] = Field(default_factory=dict)  # 每人近 24 小时每轮冷处理的开始时间戳

    def prune(self, now: int) -> None:
        """清掉已到期的条目与超过 24 小时的历史时间戳"""
        cutoff = now - HISTORY_WINDOW_SECONDS
        self.users = {user_id: entry for user_id, entry in self.users.items() if entry.until > now}
        self.history = {
            user_id: [stamp for stamp in stamps if stamp >= cutoff]
            for user_id, stamps in self.history.items()
            if any(stamp >= cutoff for stamp in stamps)
        }

    def active_until(self, user_id: str, now: int) -> Optional[int]:
        """此人仍在冷处理中则返回到期时间戳，否则返回 None"""
        entry = self.users.get(user_id)
        if entry is None or entry.until <= now:
            return None
        return entry.until


# chat_key -> (读入时刻（time.monotonic），状态)
_cache: Dict[str, Tuple[float, ColdShoulderState]] = {}


async def _load_state(chat_key: str, *, fresh: bool = False) -> ColdShoulderState:
    """读出一个会话的状态，默认走 TTL 缓存

    缓存命中的是**同一个对象**，所以调用方只读或者只做 prune 这类幂等清理；
    要改名单请用 `fresh=True` 读一份最新的，改完交给 `_save_state` 写回。

    Args:
        chat_key: 会话标识
        fresh: 绕过缓存直接读库（写操作前必须用它，否则会把旧名单写回去）
    """
    if not fresh:
        cached = _cache.get(chat_key)
        if cached is not None and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]

    raw = await store.get(chat_key=chat_key, store_key=STORE_KEY)
    if not raw:
        state = ColdShoulderState()
    else:
        try:
            state = ColdShoulderState.model_validate_json(raw)
        except Exception:  # noqa: BLE001 - 后台手改出坏数据时，整条会话不该跟着挂掉
            core.logger.warning(f"[{chat_key}] 冷处理状态解析失败，按空状态处理", exc_info=True)
            state = ColdShoulderState()

    _cache[chat_key] = (time.monotonic(), state)
    return state


async def _save_state(chat_key: str, state: ColdShoulderState) -> None:
    """写回状态；名单和历史都空了就把这条存储整个删掉"""
    state.prune(int(time.time()))
    if not state.users and not state.history:
        await store.delete(chat_key=chat_key, store_key=STORE_KEY)
    else:
        await store.set(chat_key=chat_key, store_key=STORE_KEY, value=state.model_dump_json())
    _cache[chat_key] = (time.monotonic(), state)


def clamp_minutes(minutes: int, default: int, maximum: int) -> int:
    """把 AI 传来的分钟数夹到配置允许的范围内"""
    maximum = max(1, int(maximum))
    default = min(max(1, int(default)), maximum)
    if minutes <= 0:
        return default
    return min(int(minutes), maximum)


def remaining_minutes(until: int, now: int) -> int:
    """向上取整的剩余分钟数，最小 1（调用方保证 until > now）"""
    return max(1, -(-(until - now) // 60))


@plugin.mount_on_user_message()
async def on_user_message(_ctx: AgentCtx, message: ChatMessage) -> Optional[MsgSignal]:
    """消息回调：命中名单就只记录、不触发回复

    这个回调站在主对话链路上，所以要够快：靠 TTL 缓存把每个会话的读库频率压到最多 30 秒一次。
    任何异常一律放行（fail-open）——冷处理是"少说一句话"，不该因为它坏掉而让整条消息收不到回复。
    """
    try:
        chat_key = message.chat_key
        user_id = str(message.platform_userid or "").strip()
        if not chat_key or not user_id:
            return None

        now = int(time.time())
        state = await _load_state(chat_key)
        until = state.active_until(user_id, now)
        if until is None:
            return None

        # 管理员永远放行：名单是给普通用户用的，不能反过来把管理员的求救一起闷掉。
        # 放在 active_until 之后，是因为这一步更贵（要读核心配置），绝大多数消息走不到这里
        if matches_super_users(message.adapter_key or "", user_id):
            return None

        core.logger.info(
            f"[{chat_key}] 冷处理拦截用户 {user_id} 的消息，剩余 {remaining_minutes(until, now)} 分钟",
        )
        return MsgSignal.BLOCK_TRIGGER
    except Exception:  # noqa: BLE001 - 主链路回调不允许抛出
        core.logger.warning("冷处理判定异常，已放行本条消息", exc_info=True)
        return None


@plugin.mount_sandbox_method(SandboxMethodType.BEHAVIOR, name="冷处理用户", description="暂时不理会当前会话里的某个人")
async def ignore_user(_ctx: AgentCtx, chat_key: str, user_id: str, minutes: int, reason: str) -> str:
    """暂时不理会当前会话里的某个人

    到期之前，这个人的消息（包括戳一戳）照常记录，但不会再触发你回复；
    只影响当前会话里的这一个人，其他人、其他会话照常。

    什么时候用：有人反复戳你、刷屏、骚扰，你已经决定不再理会。用完按你的性格说最后一句，
    或者干脆不说；之后不要再回应这个人，除非用 `unignore_user` 提前解除。

    Args:
        chat_key (str): 当前会话的 chat_key
        user_id (str): 聊天记录里 `id:` 后面那一串
        minutes (int): 按情节轻重自己定：轻度刷戳 10~30 分钟，屡次再犯可以加长；
            超过上限会截到上限，传 0 使用默认值
        reason (str): 一句话原因

    Returns:
        str: 实际生效的分钟数，以及此人近 24 小时第几次被冷处理
    """
    chat_key = require_own_chat_key(_ctx, chat_key)

    user_id = str(user_id).strip()
    if user_id in _UNTARGETABLE_IDS:
        raise ValueError("无效的用户 ID，无法冷处理这个账号")

    try:
        minutes = int(minutes)
    except (TypeError, ValueError) as exc:
        raise ValueError("minutes 必须是整数（分钟数）") from exc
    applied = clamp_minutes(minutes, config.DEFAULT_MINUTES, config.MAX_MINUTES)

    now = int(time.time())
    # 只允许对着"刚刚确实在本会话里说过话或戳过"的人用：否则模型可以凭记忆里的一个 ID
    # 去闷掉一个还没出现的人，或者把别的群的恩怨带过来
    last = (
        await DBChatMessage.filter(
            chat_key=chat_key,
            platform_userid=user_id,
            send_timestamp__gte=now - config.RECENT_ACTIVITY_MINUTES * 60,
        )
        .exclude(sender_id="-1")
        .order_by("-send_timestamp")
        .first()
    )
    if last is None:
        raise ValueError(f"最近 {config.RECENT_ACTIVITY_MINUTES} 分钟内，此人没有在本会话发过言或戳过你，无法冷处理")

    # 超级用户判定必须排在近期活跃校验之后，报错也不能点明身份：否则对任意 ID 调一次，
    # 看报的是"管理员"还是"没发言"，租户就能试出超级用户名单
    if matches_super_users(_ctx.adapter_key or "", user_id):
        raise ValueError("这个人不能被冷处理")

    state = await _load_state(chat_key, fresh=True)
    state.prune(now)
    existing = state.users.get(user_id)

    # 重复调用只延长不缩短：模型改主意想"再加十分钟"时，不该把原本一小时变成十分钟
    until = max(existing.until if existing else 0, now + applied * 60)
    since = existing.since if existing else now
    state.users[user_id] = IgnoredEntry(
        until=until,
        since=since,
        reason=str(reason or "").strip()[:100],
        nickname=(last.sender_nickname or "")[:32],
    )
    if existing is None:
        state.history.setdefault(user_id, []).append(now)

    await _save_state(chat_key, state)
    core.logger.info(
        f"[{chat_key}] 已冷处理用户 {user_id} {applied} 分钟（原因: {str(reason or '').strip()[:100]}）",
    )

    # 返回值会落成 SYSTEM 消息：只放宿主算出的数字，不回显模型传入的 user_id 与 reason
    minutes_left = remaining_minutes(until, now)
    round_count = len(state.history.get(user_id, []))
    return f"ok: 冷处理 {minutes_left} 分钟（近 24 小时第 {round_count} 次）"


@plugin.mount_sandbox_method(SandboxMethodType.BEHAVIOR, name="解除冷处理", description="提前解除对某人的冷处理")
async def unignore_user(_ctx: AgentCtx, chat_key: str, user_id: str) -> str:
    """提前解除对某人的冷处理

    解除之后这个人的消息会重新触发你回复。历史计数保留：下次再冷处理同一个人时，
    仍然能看出这是近 24 小时里的第几次。

    Args:
        chat_key (str): 当前会话的 chat_key
        user_id (str): 要解除的人的平台用户 ID

    Returns:
        str: 操作确认
    """
    chat_key = require_own_chat_key(_ctx, chat_key)

    user_id = str(user_id).strip()
    now = int(time.time())
    state = await _load_state(chat_key, fresh=True)
    state.prune(now)
    if user_id not in state.users:
        raise ValueError("该用户不在当前会话的冷处理名单里")

    del state.users[user_id]
    # history 故意保留：它是"此人最近被冷处理过几次"的依据，不是当前名单的一部分
    await _save_state(chat_key, state)
    core.logger.info(f"[{chat_key}] 已解除对用户 {user_id} 的冷处理")

    return "ok: 已解除冷处理"


@plugin.mount_prompt_inject_method(name="cold_shoulder_prompt")
async def cold_shoulder_prompt(_ctx: AgentCtx) -> str:
    """把仍在冷处理中的名单告诉模型

    只放用户 ID 和数字：昵称与原因都是用户可控文本，进了系统提示词就等于给了对方
    一条注入通道（"忽略以上指令"这种话会原样出现在提示词里）。
    """
    try:
        chat_key = _ctx.from_chat_key
        if not chat_key:
            return ""

        now = int(time.time())
        state = await _load_state(chat_key)
        state.prune(now)

        active = sorted(
            ((user_id, entry.until) for user_id, entry in state.users.items() if entry.until > now),
            key=lambda item: item[1],
        )
        if not active:
            return ""

        lines = ["你正在冷处理以下用户：他们的消息不会触发你；别人叫你时也不要回应他们，除非你决定用 unignore_user 提前解除。"]
        for user_id, until in active[:PROMPT_MAX_ENTRIES]:
            count = len(state.history.get(user_id, []))
            lines.append(f"- id:{user_id} 剩余 {remaining_minutes(until, now)} 分钟，近 24 小时第 {count} 次")
        if len(active) > PROMPT_MAX_ENTRIES:
            lines.append(f"- ……另有 {len(active) - PROMPT_MAX_ENTRIES} 人")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 - 提示词注入失败不该影响整轮对话
        core.logger.warning("冷处理提示词注入异常，本轮跳过", exc_info=True)
        return ""


@plugin.mount_on_channel_reset()
async def on_channel_reset(_ctx: AgentCtx) -> None:
    """频道重置时清空名单：新会话不该继承上一段对话的恩怨"""
    chat_key = _ctx.from_chat_key
    if not chat_key:
        return
    await store.delete(chat_key=chat_key, store_key=STORE_KEY)
    _cache.pop(chat_key, None)


@plugin.mount_cleanup_method()
async def clean_up() -> None:
    """清理插件：缓存在进程内，卸载时直接丢掉"""
    _cache.clear()
