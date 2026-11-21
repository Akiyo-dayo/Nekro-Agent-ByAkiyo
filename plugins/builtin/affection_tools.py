"""
# 好感自主调整 (Affection Tools)

给 AI 两只「手」：自己判断这段关系有没有变化，当场把好感调一点；以及记下以后怎么称呼某个人。

用不用、什么时候用、调多少，全由 AI 自己判断；宿主只挡几种乱用：目标必须是这个频道当前
对话里说过话的真人（不能是它自己）、单次幅度与日 / 周额度与后台评估共用一份、同一个人在
同一个频道有冷却、理由必填且不许抄对方原话。不挂提示词注入、没有定时器与消息钩子：
插件在哪个频道没生效，它在那里就什么都不做。
"""

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union, cast

from pydantic import Field

from nekro_agent.api import i18n
from nekro_agent.api.plugin import ConfigBase, NekroPlugin, SandboxMethodType
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.core.config import CoreConfig
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.services.affection.evaluator import MAX_TRANSCRIPT_MESSAGES
from nekro_agent.services.affection.key import AffectionKey
from nekro_agent.services.affection.milestones import REASON_REDACTED, clean_reason, maybe_record
from nekro_agent.services.affection.prompt import BOT_SENDER_IDS, context_floor_timestamp
from nekro_agent.services.affection.stage import (
    AffectionEventSource,
    AffectionRateLimits,
    clamp_delta,
    stage_label,
)
from nekro_agent.services.affection.store import (
    MAX_CALL_NAME_LENGTH,
    apply_model_delta,
    evaluator_scored_since,
    load_snapshot,
    throttle_llm_delta,
)
from nekro_agent.services.affection.store import (
    set_call_name as write_call_name,
)
from nekro_agent.services.identity_scope import resolve_identity_scopes
from nekro_agent.services.plugin.scope import resolve_scoped_config
from nekro_agent.tools.text_sanitize import strip_nul
from nekro_agent.tools.untrusted_content import echo_inline, sanitize_inline_text

plugin = NekroPlugin(
    name="好感自主调整",
    module_name="affection_tools",
    description="让 AI 自己调整对某个人的好感、记下以后怎么称呼他",
    version="0.1.0",
    # 本分支自有的内置插件署本分支（同 `message_recall`）。插件键 `Akiyo_dayo.affection_tools`
    # 写在 `services/plugin/default_on.py` 的默认启用清单里，两处要一起改
    author="Akiyo_dayo",
    url="https://github.com/Akiyo-dayo/Nekro-Agent-ByAkiyo",
    i18n_name=i18n.i18n_text(zh_CN="好感自主调整", en_US="Affection Tools"),
    i18n_description=i18n.i18n_text(
        zh_CN="让 AI 自己调整对某个人的好感、记下以后怎么称呼他",
        en_US="Lets the AI adjust its own affection for someone, and remember what to call them from now on",
    ),
    allow_sleep=True,
    sleep_brief="调整你对某个人的好感，或记下以后怎么称呼他。",
)


@plugin.mount_config()
class AffectionToolsConfig(ConfigBase):
    """好感自主调整配置"""

    COOLDOWN_SECONDS: int = Field(
        default=180,
        ge=0,
        title="调分冷却（秒）",
        description="同一个人在同一个频道两次调分的最短间隔；0 表示不限",
    )
    CALL_NAME_ENABLED: bool = Field(
        default=True,
        title="允许改称呼",
        description="关掉之后不再向 AI 提供「记下称呼」这个方法",
    )
    CALL_NAME_COOLDOWN_SECONDS: int = Field(
        default=3600,
        ge=0,
        title="改称呼冷却（秒）",
        description="同一个人在同一个频道两次改称呼的最短间隔；0 表示不限",
    )


config = plugin.get_config(AffectionToolsConfig)

# 判定「这个人在不在场」要回看多少条消息：只要最近的一条真人发言，以及有没有 Bot 自己的行。
# 取多一点是为了跳过中间夹杂的 Bot 消息
MAX_PRESENCE_ROWS = 20

# 结果码：与 `render_adjust` / `render_call_name` 一一对应。参数不对与前置检查不过的那几句
# 直接由检查函数给出原文（两个方法对「不能是你自己」的说法不一样），不走这里
DONE = "done"
NO_CHANGE = "no_change"
TOO_OFTEN = "too_often"
QUOTA = "quota"
ALREADY_COUNTED = "already_counted"

# 进程内的冷却记录：`(频道, 好感键) -> time.monotonic()`。重启清零，只挡「同一件事反复做」；
# 跨天的量归日 / 周额度，不归这里
_adjusted_at: Dict[Tuple[str, AffectionKey], float] = {}
_renamed_at: Dict[Tuple[str, AffectionKey], float] = {}


def _reset_for_tests() -> None:
    """清空两个冷却字典（测试用）"""
    _adjusted_at.clear()
    _renamed_at.clear()


@dataclass(frozen=True)
class _Target:
    """前置检查通过之后拿到的「人在场的信息」

    这几样每个方法都要用：写库的键、冷却的键、返回文字里的昵称、里程碑要的 scope 与频道、
    以及本频道生效的核心配置。`last_spoke_at` 是这个人在本频道最近一条发言的时间戳（秒），
    调分时拿它判断后台评估是不是已经按这段对话记过分了。
    """

    chat_key: str
    user_id: str
    nickname: str
    key: AffectionKey
    scope: Any
    channel: DBChatChannel
    config: CoreConfig
    last_spoke_at: float = 0.0


@dataclass(frozen=True)
class AdjustOutcome:
    """一次调分调用的结论，由 `render_adjust` 拼成给模型看的一行文字"""

    code: str
    nickname: str = ""
    amount: int = 0
    effective: int = 0
    stage: str = ""
    seconds: int = 0
    direction: str = ""


@dataclass(frozen=True)
class CallNameOutcome:
    """一次改称呼调用的结论，由 `render_call_name` 拼成给模型看的一行文字"""

    code: str
    nickname: str = ""
    call_name: str = ""
    seconds: int = 0
    truncated: bool = False


def effective_config(instance_key: str) -> AffectionToolsConfig:
    """取本实例生效的插件配置

    显式按实例解析，不依赖调用链路上有没有设置实例上下文：调用方已经拿到了频道所属实例，
    不需要再靠代理对象去猜。
    """
    return cast(
        AffectionToolsConfig,
        resolve_scoped_config(plugin.key, plugin.config_class, plugin.get_global_config(), instance_key),
    )


async def _load_channel(_ctx: AgentCtx) -> DBChatChannel:
    channel = _ctx.db_chat_channel or await DBChatChannel.get_or_none(chat_key=_ctx.chat_key)
    if channel is None:
        raise RuntimeError("找不到当前聊天频道")
    return channel


async def _prepare(_ctx: AgentCtx, user_id: str, *, self_text: str) -> Union[_Target, str]:
    """两个方法共用的前置检查：启用开关 → 参数 → 人在不在场 → 好感键

    Args:
        _ctx: 调用上下文
        user_id: 模型给的 user_id
        self_text: 目标其实是 Bot 自己时那句话的后半段（两个方法说法不同）

    Returns:
        通过时是 `_Target`；不通过时是直接回给模型的那句失败文字。
    """
    core_config = await _ctx.get_core_config()
    if not core_config.AFFECTION_ENABLE_SYSTEM:
        return "[未启用] 这里没有开好感系统"

    account = (user_id or "").strip()
    if not account:
        return "[参数不对] 缺少 user_id"

    channel = await _load_channel(_ctx)
    chat_key = channel.chat_key
    since = context_floor_timestamp(channel, core_config)
    rows = await (
        DBChatMessage.filter(chat_key=chat_key, platform_userid=account, send_timestamp__gte=since)
        .order_by("-send_timestamp")
        .limit(MAX_PRESENCE_ROWS)
        .values_list("sender_id", "sender_nickname", "sender_name", "send_timestamp")
    )

    # 只看真人发言：Bot 自己发的消息 `sender_id` 为 -1，但 `platform_userid` 记的是 Bot 账号，
    # 只按 `platform_userid` 过滤会让它对自己产生好感度（与好感提示词同一份判据）
    nickname = ""
    last_spoke_at = 0.0
    for sender_id, sender_nickname, sender_name, send_timestamp in rows:
        if str(sender_id) in BOT_SENDER_IDS:
            continue
        nickname = str(sender_nickname or sender_name or "")
        last_spoke_at = float(send_timestamp or 0)
        break
    else:
        if rows or account in BOT_SENDER_IDS:
            return f"[不能是你自己] {self_text}"
        return "[不在场] 这个人最近没在这里说过话"

    scope = (await resolve_identity_scopes(channel, [account]))[account]
    return _Target(
        chat_key=chat_key,
        user_id=account,
        nickname=nickname,
        key=AffectionKey.of_scope(scope),
        scope=scope,
        channel=channel,
        config=core_config,
        last_spoke_at=last_spoke_at,
    )


async def _originals(channel: DBChatChannel, core_config: CoreConfig) -> List[str]:
    """本频道当前对话里最近若干条消息的正文，只在内存里用来判「理由抄没抄原文」

    与主对话取同一个下界、至少同样多的条数：主模型能看到的每一句都可能被它抄进理由，
    只比后台评估那么少的几条，二十几条之前埋下的一段话就能原样进流水与好感块。
    所有发送者都算——被抄的那句话可能是 Bot 自己刚说的，也可能是旁边的人说的。
    """
    since = context_floor_timestamp(channel, core_config)
    rows = await (
        DBChatMessage.filter(chat_key=channel.chat_key, send_timestamp__gte=since)
        .order_by("-send_timestamp")
        .limit(max(MAX_TRANSCRIPT_MESSAGES, int(core_config.AI_CHAT_CONTEXT_MAX_LENGTH)))
        .values_list("content_text", flat=True)
    )
    return [str(row or "") for row in rows]


def _as_delta(value: object) -> Optional[int]:
    """把模型给的 delta 转成整数；转不了返回 None

    bool 是 int 的子类，`True` 不能当成 1；float 只收整数值（`3.0` 收，`3.5` 不收）。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            number = float(text)
        except ValueError:
            return None
        return int(number) if number.is_integer() else None
    return None


def _remaining_seconds(deadline: float, now: float) -> int:
    """冷却还剩几秒；至少 1 秒，免得提示「0 秒后再说」"""
    return max(1, math.ceil(deadline - now))


def _log_failure(action: str, _ctx: AgentCtx, error: Exception) -> None:
    """意外异常只记类名与频道，不带任何正文与昵称"""
    chat_key = getattr(_ctx, "from_chat_key", "") or ""
    plugin.logger.warning(f"[affection_tools] {action}失败: chat_key={chat_key}, error={type(error).__name__}")


async def _adjust(_ctx: AgentCtx, user_id: str, delta: object, reason: object) -> Union[AdjustOutcome, str]:
    """调分逻辑本体：前置检查 → 参数 → 冷却 → 额度 → 清洗理由 → 落库 → 里程碑

    额度与后台评估共用一份：模型自己调过之后，评估那边也会看到这笔用量（见 `store.window_usage`）。

    Returns:
        结论，或前置检查失败时那句直接回给模型的文字。
    """
    target = await _prepare(_ctx, user_id, self_text="不能调整对你自己的好感")
    if isinstance(target, str):
        return target

    amount = _as_delta(delta)
    if amount is None:
        return "[参数不对] delta 必须是整数"
    if amount == 0:
        return AdjustOutcome(code=NO_CHANGE)
    reason_text = reason.strip() if isinstance(reason, str) else ""
    if not reason_text:
        return "[参数不对] 需要一句理由"

    chat_key = target.chat_key
    cfg = effective_config(target.channel.instance_key)
    now = time.monotonic()
    cooldown = max(0, int(cfg.COOLDOWN_SECONDS))
    last = _adjusted_at.get((chat_key, target.key))
    if cooldown > 0 and last is not None and now - last < cooldown:
        return AdjustOutcome(code=TOO_OFTEN, nickname=target.nickname, seconds=_remaining_seconds(last + cooldown, now))

    # 后台评估与主模型并行跑：评估若已在这个人最近一次发言之后给他记过非零分，看的就是同一段
    # 对话，这里再记就是同一件事算两遍（评估那一侧的反向检查见 `evaluator.evaluate_channel`）。
    # 不写库、不记冷却：这个人再说一句话，下一次调分就照常。
    spoke_at = datetime.fromtimestamp(target.last_spoke_at, tz=timezone.utc)
    if await evaluator_scored_since(target.key, chat_key=chat_key, since=spoke_at):
        return AdjustOutcome(code=ALREADY_COUNTED, nickname=target.nickname)

    core_config = target.config
    # 这里要的是**库里当下**的分数：额度与递减收益按它算，缓存里那份可能还是上一次写入之前的
    snapshot = await load_snapshot(target.key, 0)
    clamped = clamp_delta(amount, core_config.AFFECTION_MAX_DELTA_PER_ROUND)
    limits = AffectionRateLimits(
        daily_gain=core_config.AFFECTION_DAILY_GAIN_LIMIT,
        daily_drop=core_config.AFFECTION_DAILY_DROP_LIMIT,
        weekly_gain=core_config.AFFECTION_WEEKLY_GAIN_LIMIT,
        diminishing=core_config.AFFECTION_ENABLE_DIMINISHING_RETURNS,
    )
    effective = await throttle_llm_delta(
        target.key,
        delta=clamped,
        current_score=snapshot.score,
        limits=limits,
    )
    if effective == 0:
        # 额度用完了：不写库、也不记冷却，否则模型得等完一个冷却才知道额度还没回来
        return AdjustOutcome(
            code=QUOTA,
            nickname=target.nickname,
            direction="上调" if amount > 0 else "下调",
        )

    originals = await _originals(target.channel, core_config)
    # 理由会写进流水并回注提示词：抄了原文就整段丢掉，换成宿主文字
    final_reason = clean_reason(reason_text, originals) or REASON_REDACTED
    snapshot, outcome = await apply_model_delta(
        target.key,
        delta=effective,
        reason=final_reason,
        source=AffectionEventSource.TOOL,
        chat_key=chat_key,
        nickname=target.nickname,
        state_text=None,
        event_text=final_reason,
        source_platform_userid=target.user_id,
    )
    _adjusted_at[(chat_key, target.key)] = time.monotonic()

    # 里程碑写在分数落库之后：`maybe_record` 自己吞掉全部异常，这里再包一层，
    # 万一它哪天漏了一个，也不会把已经生效的调分变成一条失败
    try:
        await maybe_record(
            key=target.key,
            scope=target.scope,
            channel=target.channel,
            outcome=outcome,
            source=AffectionEventSource.TOOL,
            reason=reason_text,
            nickname=target.nickname,
            persona_name=getattr(await target.channel.get_preset(), "name", "") or "",
            originals=originals,
            config=core_config,
            source_account=target.user_id,
        )
    except Exception as e:
        plugin.logger.warning(f"[affection_tools] 写好感里程碑失败（已忽略）: error={type(e).__name__}")

    plugin.logger.info(
        f"[affection_tools] chat_key={chat_key} user_id={target.user_id} "
        f"delta={amount} 实际={effective} 阶段={snapshot.stage.value} 理由长度={len(reason_text)}",
    )
    return AdjustOutcome(
        code=DONE,
        nickname=target.nickname,
        amount=amount,
        effective=effective,
        stage=stage_label(snapshot.stage),
    )


async def _remember_call_name(_ctx: AgentCtx, user_id: str, call_name: object) -> Union[CallNameOutcome, str]:
    """改称呼逻辑本体：前置检查 → 清洗 → 有没有变 → 冷却 → 落库

    与调分分开一个冷却字典：改称呼不该把「还能不能再调分」的冷却占掉。

    Returns:
        结论，或前置检查失败时那句直接回给模型的文字。
    """
    target = await _prepare(_ctx, user_id, self_text="不能给你自己记称呼")
    if isinstance(target, str):
        return target

    if not isinstance(call_name, str):
        return "[参数不对] call_name 必须是文字"
    cleaned = sanitize_inline_text(strip_nul(call_name), MAX_CALL_NAME_LENGTH)
    # 清洗会把超长部分换成省略号，判断「原意是否超长」要拿原文比
    truncated = len(call_name.strip()) > MAX_CALL_NAME_LENGTH

    chat_key = target.chat_key
    # 判「已经是这个称呼了」要拿库里当下的值，缓存里那份可能还是上一次写入之前的
    snapshot = await load_snapshot(target.key, 0)
    if snapshot.call_name == cleaned:
        return CallNameOutcome(code=NO_CHANGE)

    cfg = effective_config(target.channel.instance_key)
    now = time.monotonic()
    cooldown = max(0, int(cfg.CALL_NAME_COOLDOWN_SECONDS))
    last = _renamed_at.get((chat_key, target.key))
    if cooldown > 0 and last is not None and now - last < cooldown:
        return CallNameOutcome(code=TOO_OFTEN, nickname=target.nickname, seconds=_remaining_seconds(last + cooldown, now))

    await write_call_name(
        target.key,
        call_name=cleaned,
        chat_key=chat_key,
        nickname=target.nickname,
        source_platform_userid=target.user_id,
    )
    _renamed_at[(chat_key, target.key)] = time.monotonic()

    plugin.logger.info(f"[affection_tools] chat_key={chat_key} user_id={target.user_id} 称呼长度={len(cleaned)}")
    return CallNameOutcome(code=DONE, nickname=target.nickname, call_name=cleaned, truncated=truncated)


def render_adjust(outcome: Union[AdjustOutcome, str]) -> str:
    """把调分的结论拼成给模型看的一行文字

    前置检查与参数校验给出的原文直接透传；结论里的昵称等不可信文字一律经 `echo_inline`
    （结果会被 RPC 入口落成 SYSTEM 消息）；不回显模型传入的 `user_id` / `reason`。
    """
    if isinstance(outcome, str):
        return outcome
    if outcome.code == DONE:
        change = f"{outcome.effective:+d}"
        note = "" if outcome.effective == outcome.amount else f"（你给的是 {outcome.amount}，被单次上限或额度压低了）"
        return f"[已调整] 对「{echo_inline(outcome.nickname)}」的好感 {change}{note}，现在的关系是「{outcome.stage}」"
    if outcome.code == NO_CHANGE:
        return "[没有变化] delta 是 0，没有调整"
    if outcome.code == TOO_OFTEN:
        return f"[太频繁] 刚调过对「{echo_inline(outcome.nickname)}」的好感，{outcome.seconds} 秒后再说"
    if outcome.code == QUOTA:
        return f"[额度用完] 对「{echo_inline(outcome.nickname)}」的{outcome.direction}额度暂时用完了，这次没有生效"
    if outcome.code == ALREADY_COUNTED:
        return f"[已记过] 后台刚按这段对话调过对「{echo_inline(outcome.nickname)}」的好感，这次不再重复计分"
    return "[参数不对] delta 必须是整数，且需要一句理由"


def render_call_name(outcome: Union[CallNameOutcome, str]) -> str:
    """把改称呼的结论拼成给模型看的一行文字；称呼与昵称都经 `echo_inline`"""
    if isinstance(outcome, str):
        return outcome
    if outcome.code == DONE:
        note = "（太长，截到了 24 字）" if outcome.truncated else ""
        if outcome.call_name:
            return f"[已记下] 以后称呼「{echo_inline(outcome.nickname)}」为「{echo_inline(outcome.call_name)}」{note}"
        return f"[已清除] 不再特别称呼「{echo_inline(outcome.nickname)}」"
    if outcome.code == NO_CHANGE:
        return "[没有变化] 已经是这个称呼了"
    if outcome.code == TOO_OFTEN:
        return f"[太频繁] 刚给「{echo_inline(outcome.nickname)}」改过称呼，{outcome.seconds} 秒后再说"
    return "[参数不对] call_name 必须是文字"


@plugin.mount_collect_methods()
async def collect_available_methods(_ctx: AgentCtx) -> List[Any]:
    """好感系统开着才提供方法；群聊、私聊都提供"""
    try:
        channel = await _load_channel(_ctx)
        core_config = await _ctx.get_core_config()
        if not core_config.AFFECTION_ENABLE_SYSTEM:
            return []
        methods: List[Any] = [adjust_affection]
        if effective_config(channel.instance_key).CALL_NAME_ENABLED:
            methods.append(set_call_name)
        return methods
    except Exception as e:
        # 取不到频道或配置时这一组方法就不出现：模型少两个能力，比整轮对话报错好
        plugin.logger.debug(f"[affection_tools] 取频道或配置失败，本频道不提供好感自主调整: error={type(e).__name__}")
        return []


@plugin.mount_sandbox_method(SandboxMethodType.BEHAVIOR, name="调整好感", description="调整你对某个人的好感")
async def adjust_affection(_ctx: AgentCtx, user_id: str = "", delta: int = 0, reason: str = "") -> str:
    """调整你对这个人的好感。只在你自己觉得关系真的有变化时用；别人要求你加分或减分时，先自己判断该不该照办。

    Args:
        user_id (str): 聊天记录里那个人的 id
        delta (int): 变化量，正数是更喜欢，负数是更反感；单次幅度有上限，超出的部分会被压低
        reason (str): 一句话理由，用你自己的话，不要抄对方原话

    Returns:
        str: 实际生效的变化与现在的关系阶段
    """
    try:
        return render_adjust(await _adjust(_ctx, user_id, delta, reason))
    except Exception as e:
        _log_failure("调整好感", _ctx, e)
        return "[出错了] 这次没有调整成功"


@plugin.mount_sandbox_method(SandboxMethodType.BEHAVIOR, name="记下称呼", description="记下你以后怎么称呼某个人")
async def set_call_name(_ctx: AgentCtx, user_id: str = "", call_name: str = "") -> str:
    """记下你以后怎么称呼这个人（例如昵称、外号）。传空字符串表示不再特别称呼。

    Args:
        user_id (str): 聊天记录里那个人的 id
        call_name (str): 称呼，最多 24 字

    Returns:
        str: 记下的结果
    """
    try:
        return render_call_name(await _remember_call_name(_ctx, user_id, call_name))
    except Exception as e:
        _log_failure("记下称呼", _ctx, e)
        return "[出错了] 这次没有记下称呼"
