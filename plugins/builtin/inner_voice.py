"""
# 心声 (Inner Voice)

让 Bot 偶尔在回完话之后，补一张"心声"卡片，说出它此刻的内心活动。

## 主要功能

- **搭主 Agent 的顺风车**: 心声不额外请求模型。触发时把 `inner_voice` 这个工具
  临时放进主 Agent 的工具箱，心声内容在主 Agent 那一次 LLM 请求里顺带产出。
- **回复之后才出现**: 沙盒方法只把内容缓存下来，真正发送推迟到整轮 Agent 任务结束、
  主回复确定已经送达之后，顺序不依赖模型把代码写成什么样。
- **随机触发**: 每条消息按概率布防，带冷却时间，不会连珠炮。
- **图片呈现**: 渲染成一张小卡片发出，而不是一段普通文本。

## 使用方法

默认关闭。在插件配置里打开 `ENABLE_INNER_VOICE` 后，Bot 会在群聊中按配置的概率
自发地冒出心声，不需要任何指令。

## 为什么是"临时放进工具箱"而不是常驻

工具箱里每多一个常驻工具，模型每一轮都要多读一遍、多分一份注意力。心声一天也用不了几次，
让它常驻是拿主对话的回复质量去换一个锦上添花的功能。这里改成按布防结果动态收集：
没布防的轮次，提示词里根本不存在这个方法，token 成本与干扰都是零；
布防的轮次才多出十来行方法说明。沙盒的调用白名单与提示词披露同源
（见 `services/sandbox/ext_caller.py`），因此未布防时模型即便凭记忆调用也会被拒。

## "回复之后"是怎么保证的

NekroAgent 在单轮 Agent 任务收尾时，会向进程内的系统事件总线广播一条
`agent_active / active=False`（`services/message_service.py` 的 `finally` 块）。
此时 `run_agent` 已经返回，模型这一轮要发的消息全部送达平台。本插件订阅这条总线，
收到属于本轮的结束事件之后才渲染发送，因此顺序不依赖模型把 `inner_voice(...)`
写在代码的第几行。这套机制在原版 NekroAgent 上就有，不需要给宿主打任何补丁。

**整个进程只维持一个订阅。** 总线的订阅位是全局稀缺资源（`_MAX_SUBSCRIBERS = 50`），
而且和 WebUI 的实时推送共用：每个打开后台的浏览器标签页占一位。若按频道各订阅一次，
几十个活跃群就能把订阅位吃光，还会把用户的后台实时推送一起挤掉。所以这里是
**一个长生命周期消费者 + 插件内部按 `chat_key` 分发**：无论多少实例、多少频道，永远只占 1 位。

## 设计约束

心声**绝不能拖累主对话**：布防判定是同步的（只做几次比较），缓存写入是一次字典赋值，
渲染与发送整体丢进后台任务，任何一步失败都只写日志。哪怕字体缺失、图片发送失败，
用户看到的也只是"这次没有心声"，而不是"Bot 不回话了"。
消费循环里也只做 `json.loads` + 一次字典查找，重活一律丢后台：总线对慢消费者的保护
是**直接把订阅者移除**，被移除方拿不到任何通知，只会永远等不到下一条事件。

## 与原版上游的兼容性

本插件可以直接装在原版 NekroAgent 上，缺失的能力逐项降级而不是让插件加载失败：

- 缺 `services.imagecard`（本 fork 新增的 Pillow 卡片渲染）时，心声退化为一条普通文本；
- 缺 `services.system_broadcast`（或它换了接口）时，退回到"宽限时间后发送"，
  顺序改由方法说明里的书写约定保证，而不再是结构性保证。

两种降级都会在日志里说明当前处于什么模式。
"""

import asyncio
import json
import random
import re
import time
from typing import Any, Coroutine, Dict, List, Optional, Set, Tuple, Union

from pydantic import Field

from nekro_agent.api import core, i18n
from nekro_agent.api.plugin import ConfigBase, ExtraField, NekroPlugin, SandboxMethodType
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.schemas.chat_message import ChatMessage, ChatType
from nekro_agent.services.plugin.scope import plugin_effective_in_chat

# 正文落到卡片/消息之前的长度上限。模型偶尔会不受控地长篇大论，
# 心声这个体裁本身就该是一两句话，超出的部分对读者没有价值。
FALLBACK_MAX_BODY_LENGTH = 200

# --- 可选依赖：本 fork 提供，原版上游没有 ---------------------------------
try:
    from nekro_agent.services.imagecard import (
        fonts_available,
        prepare_body_text,
        render_inner_voice_card,
    )

    HAS_IMAGE_CARD = True
except ImportError:  # pragma: no cover - 取决于宿主是否为本 fork
    HAS_IMAGE_CARD = False

    # 原版上游也没有 tools.untrusted_content，这里给出等价的最小清洗：
    # 心声正文由模型产出，控制字符既会破坏卡片排版，也会污染纯文本消息
    _CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

    def fonts_available() -> bool:
        return False

    def prepare_body_text(raw: str) -> str:
        cleaned = _CONTROL_CHARS.sub("", raw.replace("\r\n", "\n").replace("\r", "\n")).strip()
        return cleaned[:FALLBACK_MAX_BODY_LENGTH] if len(cleaned) > FALLBACK_MAX_BODY_LENGTH else cleaned

    def render_inner_voice_card(body: str) -> bytes:
        raise RuntimeError(f"当前环境缺少 imagecard 渲染能力，无法渲染 {len(body)} 字的心声")


# --- 可选依赖：宿主的进程内系统事件总线 -------------------------------------
#
# `services.system_broadcast` 是 NekroAgent 的**内部模块**，不是对插件承诺的公开 API：
# 它现在的用途是给 WebUI 推 SSE，随时可能改名、改事件结构、或者换一套实现。
# 因此这里一律走「能力探测 + try/except」：探测不到就整体降级到宽限发送，
# 而不是让插件在 import 期直接炸掉、把整个插件仓的加载一起带崩。
try:
    from nekro_agent.services import system_broadcast

    HAS_EVENT_BUS = all(hasattr(system_broadcast, name) for name in ("subscribe_system_events", "unsubscribe_system_events"))
    _bus: Any = system_broadcast if HAS_EVENT_BUS else None
except ImportError:  # pragma: no cover - 取决于宿主版本
    HAS_EVENT_BUS = False
    _bus = None


plugin = NekroPlugin(
    name="心声插件",
    module_name="inner_voice",
    description="让 Bot 偶尔以图片卡片的形式说出此刻的内心活动",
    version="0.3.0",
    author="Akiyo_dayo",
    url="https://github.com/Akiyo-dayo/Nekro-Agent-ByAkiyo",
    i18n_name=i18n.i18n_text(zh_CN="心声插件", en_US="Inner Voice Plugin"),
    i18n_description=i18n.i18n_text(
        zh_CN="让 Bot 偶尔以图片卡片的形式说出此刻的内心活动",
        en_US="Occasionally lets the bot reveal its current inner monologue as an image card",
    ),
)


@plugin.mount_config()
class InnerVoiceConfig(ConfigBase):
    """心声配置"""

    ENABLE_INNER_VOICE: bool = Field(
        default=False,
        title="启用心声",
        description="关闭时完全不触发，也不会往提示词里放任何东西",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(zh_CN="启用心声", en_US="Enable inner voice"),
            i18n_description=i18n.i18n_text(
                zh_CN="关闭时完全不触发，也不会往提示词里放任何东西",
                en_US="When off, nothing is triggered and nothing is added to the prompt",
            ),
        ).model_dump(),
    )
    TRIGGER_PROBABILITY: float = Field(
        default=0.03,
        title="单条消息的触发概率",
        description="取值 0~1。0.03 表示大约每 33 条消息布防一次，实际频率还受冷却时间与模型意愿限制",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(zh_CN="单条消息的触发概率", en_US="Trigger probability per message"),
            i18n_description=i18n.i18n_text(
                zh_CN="取值 0~1，实际频率还受冷却时间与模型意愿限制",
                en_US="Between 0 and 1; the cooldown and the model's own choice further limit the real frequency",
            ),
        ).model_dump(),
    )
    MIN_INTERVAL_SECONDS: int = Field(
        default=3600,
        title="同一会话的最小间隔（秒）",
        description="心声贵在偶尔出现，间隔太短会变成刷屏",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(zh_CN="同一会话的最小间隔（秒）", en_US="Minimum interval per session (seconds)"),
            i18n_description=i18n.i18n_text(
                zh_CN="心声贵在偶尔出现，间隔太短会变成刷屏",
                en_US="Inner voice works because it is rare; a short interval turns it into spam",
            ),
        ).model_dump(),
    )
    ENABLE_IN_PRIVATE_CHAT: bool = Field(
        default=False,
        title="在私聊中也触发",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(zh_CN="在私聊中也触发", en_US="Also trigger in private chats"),
        ).model_dump(),
    )


config = plugin.get_config(InnerVoiceConfig)

# 布防之后多久仍未等到回合结束事件就作废。
# 布防发生在"收到消息"时，而 Agent 未必会被这条消息触发（没提到 Bot、被概率拦下等），
# 没有这道保险，一次布防会一直挂着，几小时后突然在一段毫不相干的对话里冒出来。
ARM_TTL_SECONDS = 600.0

# 消费循环空转多久做一次自检（清理过期布防 + 确认订阅还在）
HEARTBEAT_SECONDS = 30.0

# 拿不到订阅位时的重试间隔。订阅位被 WebUI 标签页占满是暂时状态，用户关几个页面就腾出来了
SUBSCRIBE_RETRY_SECONDS = 60.0

# 降级模式下的宽限时间：拿不到回合结束信号时，只能靠等一小会儿
# 让模型写在 `inner_voice(...)` 之后的发送语句有机会先落地
FALLBACK_GRACE_SECONDS = 2.0

# chat_key -> 布防时刻（毫秒）。有值表示本会话当前允许模型产出心声
_armed: Dict[str, int] = {}
# chat_key -> (心声原文, 产出这段心声的 Agent 上下文)，等待本轮收尾后结算
_pending: Dict[str, Tuple[str, AgentCtx]] = {}
# chat_key -> 上次真正发出心声的时间戳
_last_sent_at: Dict[str, float] = {}
# 后台任务的强引用。asyncio 只持弱引用，不留一份的话任务可能在跑完之前就被回收
_background_tasks: Set["asyncio.Task[None]"] = set()

# 全进程唯一的总线消费者与它占用的那一个订阅位
_listener_task: Optional["asyncio.Task[None]"] = None
_listener_queue: Optional["asyncio.Queue[str]"] = None
# 订阅位是否处于"申请不到"的状态。只在状态翻转时告警，
# 否则每 SUBSCRIBE_RETRY_SECONDS 重试一次就刷一条 WARNING，反而把日志淹了
_slot_unavailable = False


def reset_state() -> None:
    """清空布防、待发与冷却状态，仅供测试使用"""
    global _slot_unavailable  # noqa: PLW0603 - 全进程单例
    _armed.clear()
    _pending.clear()
    _last_sent_at.clear()
    _slot_unavailable = False


def should_trigger(
    *,
    enabled: bool,
    probability: float,
    min_interval: float,
    allow_private: bool,
    is_group: bool,
    roll: float,
    now: float,
    last_sent_at: Optional[float],
    in_flight: bool,
) -> bool:
    """判断这条消息要不要为本会话布防心声

    做成不读任何全局状态的纯函数：这五道闸门（总开关 / 私聊 / 在途去重 / 概率 / 冷却）
    合起来决定了用户实际感受到的频率，是这个插件唯一值得反复确认的逻辑，
    而构造一个消息事件再观察"有没有发出图片"根本没法稳定断言。

    Args:
        roll: 调用方掷出的 [0, 1) 随机数
        now: 当前时间戳
        last_sent_at: 本会话上次发送心声的时间戳，从未发过传 None
        in_flight: 本会话是否已有一次心声在途（已布防尚未结算，或已产出尚未发送）
    """
    if not enabled:
        return False
    if not is_group and not allow_private:
        return False
    if in_flight:
        return False
    if roll >= probability:
        return False
    return last_sent_at is None or (now - last_sent_at) >= min_interval


def resolve_is_group(chat_type: Union[ChatType, str, None], channel_type: Optional[str]) -> Optional[bool]:
    """判定这次事件属于群聊还是私聊

    按 事件自带类型 -> 上下文频道类型 的顺序取第一个能认出来的值。

    Returns:
        群聊 True，私聊 False；两者都认不出时返回 None，调用方据此整体跳过

    分成三态而不是"非群即私"，是因为第三种情况真实存在且必须区别对待：
    系统事件与部分通知类事件的类型就是 `UNKNOWN`。把它当私聊处理的话，
    一旦用户打开了"私聊也触发"，心声就会挂到一个没有会话语境的事件上。

    入参刻意同时接受枚举与字符串：`ChatMessage` 声明 `chat_type: ChatType`，
    但它的 `Config.use_enum_values = True` 会让实例上真正存着的是 `"group"` 这样的裸字符串，
    而 `AgentCtx.channel_type` 本来就是字符串。只按枚举比较会全程静默地判成"认不出"。
    """
    for raw in (chat_type, channel_type):
        value = raw.value if isinstance(raw, ChatType) else raw
        if value == ChatType.GROUP.value:
            return True
        if value == ChatType.PRIVATE.value:
            return False
    return None


def is_round_finished_event(event: Dict[str, Any], chat_key: str, armed_at_ms: int) -> bool:
    """这条广播事件是不是"本轮 Agent 任务已经收尾"

    NekroAgent 在单轮 Agent 任务的 `finally` 里广播一条 `agent_active / active=False`
    （见 `services/message_service.py`），此时 `run_agent` 已经返回，模型这一轮
    要发的消息全部送达平台。插件把它当作"回复之后"的判定依据。

    `started_at` 是该轮任务的开始时间（毫秒）。布防发生在 `on_user_message`，
    一定早于本轮任务开始，因此凡是 `started_at` 早于布防时刻的结束事件，
    都属于布防之前就已经在跑的上一轮，必须放过去继续等。
    """
    return (
        event.get("type") == "agent_active"
        and event.get("active") is False
        and event.get("chat_key") == chat_key
        and int(event.get("started_at") or 0) >= armed_at_ms
    )


def match_finished_round(event: Dict[str, Any]) -> Optional[str]:
    """在一条总线事件里认出它结算的是哪个布防中的会话

    单订阅的代价就是所有频道的事件都会流经这里，所以先按 `chat_key` 做一次
    O(1) 字典查找再判断，不遍历布防表。

    Returns:
        该结算的 chat_key；这条事件与心声无关时返回 None
    """
    chat_key = event.get("chat_key")
    if not isinstance(chat_key, str) or not chat_key:
        return None
    armed_at_ms = _armed.get(chat_key)
    if armed_at_ms is None:
        return None
    return chat_key if is_round_finished_event(event, chat_key, armed_at_ms) else None


def handle_raw_event(raw: str) -> Optional[str]:
    """消费一条总线原始事件

    这是消费循环里唯一做的事，必须够快：一次 `json.loads`、一次字典查找，
    真正的渲染与发送丢后台。总线对慢消费者的处理是把订阅者直接移除，
    在这里多做一点重活，代价就是整个心声功能失效。

    Returns:
        本条事件触发结算的 chat_key，没触发则为 None（返回值供测试断言）
    """
    try:
        event = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(event, dict):
        return None
    chat_key = match_finished_round(event)
    if chat_key is None:
        return None
    spawn_background(settle_round(chat_key))
    return chat_key


async def settle_round(chat_key: str) -> None:
    """结算一个会话本轮的心声

    到这里模型生成的代码已经跑完，它要发的消息都已经送达平台，
    所以此刻发出的卡片一定排在主回复之后——顺序由结算时机保证，
    而不是指望模型把 `inner_voice(...)` 写在最后一行。

    无论模型有没有产出心声，本轮的布防都在这里解除：模型选择不说，
    就不该让这次布防一直挂着、在后面每一轮里反复披露这个方法。

    分发这一环也要按频道判一次：布防之后到回合结束之间，租户可能已经把这个频道
    上的心声插件关掉；此时连渲染都不必做，直接清掉状态收工。真正的发送前
    （`render_and_send`）还会再判一次，防止渲染期间被关掉。
    """
    _armed.pop(chat_key, None)
    entry = _pending.pop(chat_key, None)
    if entry is None:
        return
    if not await plugin_effective_in_chat(plugin.key, chat_key):
        core.logger.debug(f"[{chat_key}] 心声插件在该频道未生效，跳过本轮结算")
        return
    body, ctx = entry
    # 先记冷却再发：渲染或发送失败时也不该立刻又攒一次
    _last_sent_at[chat_key] = time.time()
    await render_and_send(ctx, body)


def sweep_expired(now_ms: Optional[int] = None) -> List[str]:
    """清掉过了 TTL 还没等到结束事件的布防

    两种情况会落到这里：布防之后 Agent 压根没被触发（没提到 Bot、被概率拦下），
    以及订阅在积压中被总线移除、结束事件整个丢了。前者是常态，后者靠自愈重订阅补上，
    但**那一轮的事件已经追不回来了**——如果不清，这个会话的 `in_flight` 会永远为真，
    此后再也不会布防第二次，心声等于对这个群永久失效。

    Returns:
        被清掉的 chat_key 列表
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    ttl_ms = int(ARM_TTL_SECONDS * 1000)
    expired = [chat_key for chat_key, armed_at_ms in _armed.items() if now_ms - armed_at_ms > ttl_ms]
    for chat_key in expired:
        _armed.pop(chat_key, None)
        if _pending.pop(chat_key, None) is not None:
            core.logger.warning(f"[{chat_key}] 心声超过 {ARM_TTL_SECONDS:.0f}s 未等到回合结束事件，已丢弃")
    return expired


def is_armed(chat_key: str) -> bool:
    """本会话当前是否处于布防状态（并顺手清理过期布防）"""
    armed_at_ms = _armed.get(chat_key)
    if armed_at_ms is None:
        return False
    if time.time() * 1000 - armed_at_ms > ARM_TTL_SECONDS * 1000:
        sweep_expired()
        return False
    return True


def subscription_alive(queue: "asyncio.Queue[str]") -> bool:
    """这个订阅是否还挂在总线的订阅表上

    `_subscribers` 是 `system_broadcast` 的内部实现细节，读它属于能力探测而非依赖：
    总线在订阅者队列积压超过 `_MAX_QUEUE_SIZE` 时会**静默移除订阅者**，
    被移除方拿不到任何通知，只会永远等不到下一条事件。没有这道探测，
    心声会在某一次积压之后永久失效。

    探测不到（上游改名或换实现）时一律返回 True：宁可少一次自愈，
    也不要因为读不到内部结构就无限重订阅、反过来把订阅位耗光。
    """
    subscribers = getattr(_bus, "_subscribers", None)  # noqa: SLF001 - 见上，这是能力探测
    if not isinstance(subscribers, list):
        return True
    return any(item is queue for item in subscribers)


def acquire_subscription() -> Optional["asyncio.Queue[str]"]:
    """申请那唯一的一个订阅位"""
    global _slot_unavailable  # noqa: PLW0603 - 全进程单例
    if _bus is None:
        return None
    try:
        queue = _bus.subscribe_system_events()
    except Exception as exc:  # noqa: BLE001 - 内部模块，接口变了就降级
        core.logger.warning(f"心声：订阅系统事件总线失败，本次降级为宽限发送: {exc}")
        return None
    if queue is None:
        if not _slot_unavailable:
            _slot_unavailable = True
            core.logger.warning(
                f"心声：系统事件总线订阅位已满（与 WebUI 实时推送共用全局上限），"
                f"暂时降级为宽限发送，每 {SUBSCRIBE_RETRY_SECONDS:.0f}s 重试一次",
            )
        return None
    _slot_unavailable = False
    # 把占用情况写进日志：这条 INFO 每占用一个订阅位就出现一次，
    # 正常运行整个进程只该出现一次（重订阅自愈时会再出现一次）
    subscribers = getattr(_bus, "_subscribers", None)  # noqa: SLF001 - 见 subscription_alive
    total = len(subscribers) if isinstance(subscribers, list) else -1
    core.logger.info(f"心声：已接入系统事件总线（全进程共用一个订阅位，总线当前订阅数 {total}）")
    return queue


def release_subscription() -> None:
    """归还订阅位"""
    global _listener_queue  # noqa: PLW0603 - 全进程单例
    queue, _listener_queue = _listener_queue, None
    if queue is None or _bus is None:
        return
    try:
        _bus.unsubscribe_system_events(queue)
    except Exception as exc:  # noqa: BLE001
        core.logger.debug(f"心声：归还事件总线订阅失败: {exc}")


def heartbeat() -> None:
    """消费循环空转时的自检：清理过期布防 + 确认订阅还在

    自愈的关键一步。订阅被总线移除时没有任何通知，只能靠"很久没收到事件"这个信号
    回头去看自己还在不在订阅表里；在的话说明只是真的没事件，不在就立刻重订阅。
    """
    global _listener_queue  # noqa: PLW0603 - 全进程单例
    sweep_expired()
    if _listener_queue is not None and not subscription_alive(_listener_queue):
        core.logger.warning("心声：订阅已被事件总线移除（消费积压或宿主重置），正在重新订阅")
        _listener_queue = acquire_subscription()


async def listen_forever() -> None:
    """全进程唯一的总线消费循环

    只有这一个协程碰订阅位。它跑起来之后就不再退出（除非插件卸载被取消），
    所以订阅数恒定为 1，与频道数、实例数都无关。
    """
    global _listener_queue  # noqa: PLW0603 - 全进程单例
    try:
        while True:
            if _listener_queue is None:
                _listener_queue = acquire_subscription()
                if _listener_queue is None:
                    await asyncio.sleep(SUBSCRIBE_RETRY_SECONDS)
                    continue
            try:
                raw = await asyncio.wait_for(_listener_queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                heartbeat()
                continue
            try:
                handle_raw_event(raw)
            except Exception as exc:  # noqa: BLE001 - 一条坏事件不能让消费者退出
                core.logger.warning(f"心声：处理总线事件失败，已跳过: {exc}")
    except asyncio.CancelledError:
        raise
    finally:
        release_subscription()


def ensure_listener() -> bool:
    """确保消费者已经在跑，并返回当前是否具备"等回合结束再发"的能力

    幂等：反复调用只会有一个消费者、一个订阅位。第一次的订阅刻意在这里同步完成，
    免得"第一次布防时消费者还没被调度起来"这个窗口漏掉本轮的结束事件。
    """
    global _listener_task, _listener_queue  # noqa: PLW0603 - 全进程单例
    if _bus is None:
        return False
    if _listener_queue is None:
        _listener_queue = acquire_subscription()
    if _listener_task is None or _listener_task.done():
        _listener_task = asyncio.create_task(listen_forever())
    return _listener_queue is not None


@plugin.mount_on_user_message()
async def on_user_message(_ctx: AgentCtx, _message: ChatMessage) -> None:
    """消息回调：只做布防判定

    这个回调位于主对话链路上，返回得越快越好，所以这里只有几次比较和一次字典赋值，
    不碰数据库、不碰模型。返回 None 表示不干预正常流程。

    群聊/私聊只看事件自带的 `chat_type` 与上下文里的 `channel_type`，
    刻意不去解引用 `_ctx.db_chat_channel`：那是个声明为可选、且在不少调用路径上
    确实为 None 的字段，为了一个二选一的判断去读它，等于给主链路埋一颗 NoneType 地雷。
    """
    try:
        chat_key = _ctx.from_chat_key
        if not chat_key:
            return None

        # 事件订阅仍是全进程一份，但布防必须按频道判：这个频道所在作用域关掉了心声插件时，
        # 不能因为它恰好还活着就继续给这个群布防。判不出来（多租户部署缺频道行）按不生效。
        if not await plugin_effective_in_chat(plugin.key, chat_key):
            core.logger.debug(f"[{chat_key}] 心声插件在该频道未生效，跳过布防")
            return None

        is_group = resolve_is_group(_message.chat_type, _ctx.channel_type)
        if is_group is None:
            core.logger.debug("心声：事件缺少可判定的频道类型，跳过布防")
            return None

        if not should_trigger(
            enabled=config.ENABLE_INNER_VOICE,
            probability=config.TRIGGER_PROBABILITY,
            min_interval=config.MIN_INTERVAL_SECONDS,
            allow_private=config.ENABLE_IN_PRIVATE_CHAT,
            is_group=is_group,
            roll=random.random(),  # noqa: S311 - 触发概率不是安全用途
            now=time.time(),
            last_sent_at=_last_sent_at.get(chat_key),
            # 用 is_armed 而不是 `chat_key in _armed`：它带 TTL 过期。
            # 心跳里的清扫只在消费循环空转时才跑，繁忙实例上可能长期轮不到，
            # 那时一条丢失结束事件的陈旧布防会把这个会话的心声永久卡死在"在途"。
            in_flight=is_armed(chat_key) or chat_key in _pending,
        ):
            return None

        _armed[chat_key] = int(time.time() * 1000)
        # 订阅是全进程一次性的，这里只是"确保它已经起来了"，不会随频道数增长
        deferred = ensure_listener()
        core.logger.debug(
            f"[{chat_key}] 心声已布防，本轮将向模型披露 inner_voice 方法"
            f"（{'等回合结束事件' if deferred else '降级为宽限发送'}）",
        )
    except Exception as exc:  # noqa: BLE001 - 主链路回调不允许抛出
        core.logger.warning(f"心声布防判定异常，已忽略: {exc}")
    return None


@plugin.mount_collect_methods()
async def collect_available_methods(_ctx: AgentCtx) -> List[object]:
    """只在布防的轮次里把 inner_voice 交给模型"""
    chat_key = _ctx.from_chat_key
    if not chat_key or not is_armed(chat_key):
        return []
    return [inner_voice]


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="记录心声",
    description="记录此刻的内心活动，稍后以卡片形式发出",
)
async def inner_voice(_ctx: AgentCtx, content: str) -> str:
    """记录你此刻的一句心声

    心声是你自己在心里嘀咕的话，不说给任何人听，也不是对刚才聊天内容的总结或播报。
    它会在你本轮回复全部发送完毕之后，作为一张独立的小卡片补发出去。

    这是一个**可选**能力：只有当你此刻确实有点想法、情绪或小心思时才调用它；
    没有就不要调用，硬凑一句反而奇怪。整轮最多调用一次，重复调用以最后一次为准。

    Args:
        content (str): 心声正文。第一人称，完全贴合你的人设口吻，一到两句、不超过 60 字。
            可以是对刚才某件小事的碎碎念、突然冒出的念头、一点小情绪。
            不要写成日志、播报、总结或旁白，不要加引号或任何前缀标签。

    Example:
        ```python
        send_msg_text(_ck, "好啦好啦，这就帮你查。")
        inner_voice("他每次都这样，明明自己也会查……算了，谁让我今天心情好呢。")
        ```
    """
    chat_key = _ctx.from_chat_key
    if not chat_key:
        return "inner voice skipped: no chat context"

    body = prepare_body_text(content or "")
    if not body:
        return "inner voice skipped: empty content"

    _pending[chat_key] = (body, _ctx)
    if _listener_queue is not None:
        return "inner voice recorded, it will be shown after your reply"

    # 降级路径：拿不到回合结束信号（宿主没有事件总线，或订阅位被占满），
    # 只能靠宽限时间尽量排在主回复之后。顺序退化为书写约定而非结构性保证。
    _armed.pop(chat_key, None)
    spawn_background(settle_after_grace(chat_key))
    return "inner voice recorded, it will be shown shortly"


async def settle_after_grace(chat_key: str) -> None:
    """降级结算：等一小段宽限时间后再发"""
    await asyncio.sleep(FALLBACK_GRACE_SECONDS)
    entry = _pending.pop(chat_key, None)
    if entry is None:
        return
    body, ctx = entry
    _last_sent_at[chat_key] = time.time()
    await render_and_send(ctx, body)


async def render_and_send(ctx: AgentCtx, body: str) -> None:
    """渲染并发送心声

    整体包在一个 try 里：心声是锦上添花的功能，任何环节失败都只写日志。
    此时主回复早已送达，这里再怎么失败，用户也只是少看到一张卡片。
    """
    chat_key = ctx.from_chat_key or ""
    try:
        if HAS_IMAGE_CARD and fonts_available():
            # Pillow 作画是纯 CPU 工作，实测一张心声卡片要 50ms+。单实例时无所谓，
            # 一个进程跑几十个实例时它会把这段时间里**所有实例**的消息处理一起卡住，
            # 因此必须挪出事件循环（Pillow 的 C 扩展会释放 GIL，to_thread 是有效的）。
            image = await asyncio.to_thread(render_inner_voice_card, body)
            sandbox_path = await ctx.fs.mixed_forward_file(image, file_name=f"inner_voice_{int(time.time())}.png")
            # 走插件推送方法：布防时开着、发送时被关掉的频道，在发送这一刻再判一次。
            if await plugin.send_image(chat_key, sandbox_path, ctx):
                core.logger.info(f"[{chat_key}] 已发送心声卡片")
            else:
                core.logger.debug(f"[{chat_key}] 心声插件在该频道未生效，卡片未发送")
            return
        # 降级：缺渲染能力或缺中文字体时，心声照样要能发出来，只是变成一条普通文本
        if await plugin.send_text(chat_key, body, ctx):
            core.logger.info(f"[{chat_key}] 已发送心声（文本降级）")
        else:
            core.logger.debug(f"[{chat_key}] 心声插件在该频道未生效，文本未发送")
    except Exception as exc:  # noqa: BLE001 - 后台任务不允许把异常抛回事件循环
        core.logger.warning(f"[{chat_key}] 发送心声失败，本次跳过: {exc}")


def spawn_background(coro: Coroutine[Any, Any, None]) -> "asyncio.Task[None]":
    """把协程丢进后台并持有强引用"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


@plugin.mount_init_method()
async def init_plugin() -> None:
    """启动时把当前的降级情况讲清楚，免得用户对着"怎么发的是文字"猜半天"""
    if not HAS_IMAGE_CARD:
        core.logger.warning("心声插件：未检测到 services.imagecard，心声将以纯文本形式发送")
    if not HAS_EVENT_BUS:
        core.logger.warning(
            f"心声插件：宿主没有可用的系统事件总线，无法确知主回复何时发完，"
            f"心声将在模型调用 {FALLBACK_GRACE_SECONDS:.0f}s 后发出",
        )
    # 订阅推迟到第一次布防：功能没开或长期没触发时，不该白占一个全局订阅位


@plugin.mount_cleanup_method()
async def clean_up() -> None:
    """清理插件"""
    global _listener_task  # noqa: PLW0603 - 全进程单例
    if _listener_task is not None:
        _listener_task.cancel()
        _listener_task = None
    release_subscription()
    for task in list(_background_tasks):
        task.cancel()
    _background_tasks.clear()
    reset_state()
