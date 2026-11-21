"""好感度的数值层：取值边界、阶段划分、门槛判定与涨跌速率规则

本模块**不碰数据库、不碰 LLM、不碰配置以外的任何全局状态**，因此它的每一条规则都可以被
直接测试，也可以被后台、提示词、插件三方共用同一份判定，不会出现"后台显示亲近、
提示词里却当陌生人"这种两处各算一遍的偏差。速率规则也留在这里：需要查历史流水的
那一半（"最近一天已经涨了多少"）在 :mod:`.store`，算出用量之后怎么裁剪则是纯数值。

数值是内部实现：它决定门槛与排序，但不是产品的面孔。呈现给用户和模型的是
`DBAffection.state_text` 那一层由 LLM 生成的描述。

**隔离键不在这里**：它从 `(适配器, 实例, 平台用户)` 变成 `(域, 人, 人设)` 之后需要查实例组
与别名表，留在本模块就会把模型层拖进这个纯函数模块。见 :mod:`.key`。

## 为什么单轮上限不够

单轮上限只管一次评估。评估的最小间隔默认 180 秒，一天能触发几百轮，凑够满级绰绰有余。
于是这里叠了两层互相独立的约束：

- **递减收益**（:func:`scale_gain`）—— 分数越高，同样一次互动带来的涨幅越小。
  "从陌生到眼熟"几乎全速，"从亲近到交心"要慢慢磨。这比一刀切的上限自然，
  也更像真实关系：认识很快，交心很慢。
- **滚动窗口预算**（:func:`limit_delta`）—— 一天/一周内累计能涨多少是有限的。
  这一层才是真正堵死刷分的那道墙，递减收益只负责让曲线好看。

两层都**只作用于模型写的分**（后台评估与主模型自主调整）。人工调整与重置是运营手段，走各自的入口，不经过这里。
"""

from __future__ import annotations

from enum import StrEnum
from typing import NamedTuple

# 好感度取值边界。上界大于下界的绝对值：好感可以慢慢积累得很高，恶感则不该无限下探
# （否则一次刷屏辱骂就能把一个老朋友永久打进深渊，再也爬不回来）。
AFFECTION_MIN_SCORE = -100
AFFECTION_MAX_SCORE = 200

# 单轮变化量的硬上限。真正生效的上限取配置值与本常量的较小者：
# delta 由 LLM 产出，属于用户可诱导的量，必须有一个不依赖配置的兜底封顶。
AFFECTION_HARD_MAX_DELTA = 20

# 速率约束的滚动窗口长度。窗口是**滚动**的：以当下为终点回看固定时长，
# 而不是按自然日/自然周切分——后者会在边界上开一个免费的双倍额度窗口
# （23:59 刷一轮、00:01 再刷一轮，就拿到了两天的量）。
AFFECTION_DAILY_WINDOW_SECONDS = 24 * 60 * 60
AFFECTION_WEEKLY_WINDOW_SECONDS = 7 * 24 * 60 * 60


class AffectionStage(StrEnum):
    """好感度阶段

    取值同时作为数据库 `stage` 列与接口字段的值，重命名等于破坏存量数据，不要改。
    """

    HOSTILE = "hostile"
    WARY = "wary"
    STRANGER = "stranger"
    ACQUAINTED = "acquainted"
    FRIENDLY = "friendly"
    CLOSE = "close"
    TRUSTED = "trusted"


class _StageSpec(NamedTuple):
    stage: AffectionStage
    # 进入该阶段所需的最低分数
    min_score: int
    label: str
    # 给模型看的语气指引。只描述"该用什么态度",不描述"该说什么话"
    tone: str


# 按 min_score 从高到低排列，判定时取第一个够得着的
_STAGE_SPECS: tuple[_StageSpec, ...] = (
    _StageSpec(AffectionStage.TRUSTED, 100, "交心", "可以主动分享自己的想法和小情绪，愿意为对方多花心思"),
    _StageSpec(AffectionStage.CLOSE, 60, "亲近", "语气亲昵放松，会开玩笑、会撒娇，不必端着"),
    _StageSpec(AffectionStage.FRIENDLY, 30, "熟络", "自然熟稔，愿意多聊几句，偶尔调侃"),
    _StageSpec(AffectionStage.ACQUAINTED, 10, "眼熟", "记得对方，态度友好但仍有分寸"),
    _StageSpec(AffectionStage.STRANGER, 0, "陌生", "礼貌、客气，保持正常社交距离"),
    _StageSpec(AffectionStage.WARY, -30, "提防", "态度冷淡简短，不主动亲近，但仍然回应"),
    _StageSpec(AffectionStage.HOSTILE, AFFECTION_MIN_SCORE, "厌恶", "明显疏远，回应克制而敷衍，不配合对方的引导"),
)

# 阶段的高低次序，供门槛判定使用（越靠后越亲近）
_STAGE_ORDER: tuple[AffectionStage, ...] = tuple(spec.stage for spec in reversed(_STAGE_SPECS))


class _GainBand(NamedTuple):
    # 进入该档位所需的最低分数
    min_score: int
    # 该档位保留多少比例的涨幅（百分比）
    percent: int


# 递减收益的档位，阈值与 `_STAGE_SPECS` 对齐，按 min_score 从高到低排列。
# 负分段不打折：一个被讨厌的人愿意回头示好，那份改善不该被"你分低"这条理由稀释。
_GAIN_BANDS: tuple[_GainBand, ...] = (
    _GainBand(100, 25),
    _GainBand(60, 40),
    _GainBand(30, 60),
    _GainBand(10, 80),
    _GainBand(AFFECTION_MIN_SCORE, 100),
)

# 回复意愿的档位：「低好感降回复概率」的乘数表（百分比），阈值与 `_STAGE_SPECS` 对齐。
# 只作用于非明示触发（随机搭话 / 关键词命中）；@、私聊、喊名字、trigger_agent /
# FORCE_TRIGGER 这类直接召唤永远放行——软门管的是环境里的自发参与，直接点名是功能性 UX。
# 分数 ≥ 0 不打折：陌生人是正常社交，早期互动不该被「你分低」这条理由稀释；
# 恶感段也保留非零尾巴——软门是「少搭理」，不是「永久沉默」（硬沉默走 is_prevent_trigger）。
_REPLY_EAGERNESS_BANDS: tuple[_GainBand, ...] = (
    _GainBand(0, 100),
    _GainBand(-30, 50),
    _GainBand(AFFECTION_MIN_SCORE, 10),
)


class AffectionEventSource(StrEnum):
    """好感度变化来源

    速率约束只认模型写的分（`LLM` / `TOOL`）：其余几种都是管理员在后台修数据或系统在维护一次账号合并，
    既不该被"今天已经涨够了"挡住，也不该占用或退还用户的窗口额度。

    `MERGE` / `UNMERGE` 是账号合并留下的补偿事件，见 :mod:`.merge`：
    合并时按"好感取高的"补上差额，撤销时按流水里记着的那一份原样扣回。
    单独成两个来源而不是复用 `MANUAL`，是因为撤销必须能**精确地**找回自己当初补了多少——
    混在人工调整里就再也分不出来了。

    `DOMAIN_MERGE` 是实例入域并入存量好感留下的折叠补偿，见
    `services/identity_admin.py:merge_self_domain_into_group`。

    `TOOL` 是主模型在对话里通过「好感自主调整」插件写下的流水：调分，以及改称呼那条
    `delta = 0` 的流水。它与 `LLM` 一样是模型自己写的字，只是入口从后台评估换成了
    主模型当场表态。

    下面三种都是 `delta=0` 的「只动描述」事件，分数与阶段一律不变：

    - `BLOCKED_CLEAR`：评估请求被上游内容审核拦下，去掉旧描述重试成功之后，系统清掉了
      重试里没给出新描述的那些人的旧描述——它们就在被拦的那份提示词里，有嫌疑。
      见 :mod:`.evaluator`。
    - `NARRATIVE_CLEAR` / `NARRATIVE_EDIT`：管理员在后台只清印象、编辑印象。
      事件里只写宿主固定文案，不抄新旧文字。

    `RESET` / `NARRATIVE_CLEAR` / `NARRATIVE_EDIT` 合称**后台写入**（:data:`ADMIN_WRITE_SOURCES`）：
    评估只读最近一次后台写入之后的对话，进行中的评估遇上它要作废，否则管理员刚清掉的内容
    会被旧对话或那一轮评估原样写回来。
    """

    LLM = "llm"
    TOOL = "tool"
    MANUAL = "manual"
    RESET = "reset"
    MERGE = "merge"
    UNMERGE = "unmerge"
    DOMAIN_MERGE = "domain_merge"
    BLOCKED_CLEAR = "blocked_clear"
    NARRATIVE_CLEAR = "narrative_clear"
    NARRATIVE_EDIT = "narrative_edit"


# 后台写入：这几种事件的 `create_time` 是评估取对话的下界，也是进行中评估的作废判据。
# 人工调分（`MANUAL`）不在里面：它不碰描述，旧对话写回不了它改过的东西。
ADMIN_WRITE_SOURCES: frozenset[AffectionEventSource] = frozenset(
    {
        AffectionEventSource.RESET,
        AffectionEventSource.NARRATIVE_CLEAR,
        AffectionEventSource.NARRATIVE_EDIT,
    },
)

# 模型写的分：只有这些来源会触发关系里程碑。
# `LLM`（后台评估）与 `TOOL`（主模型自己调分）都是模型写的分，共用一份日 / 周额度，
# 也都可能跨过门槛产生里程碑。
# 人工调分、重置、账号合并与撤销、入域并入、清印象 / 编辑印象都是运营动作，不是关系里
# 发生的事——把它们记成里程碑，后台时间线里就会出现「管理员改了分数，于是关系升温了」
# 这种假叙事。想给人工动作留痕用世界书的「记一笔」。
MODEL_SOURCES: frozenset[AffectionEventSource] = frozenset({AffectionEventSource.LLM, AffectionEventSource.TOOL})


class AffectionRateLimits(NamedTuple):
    """速率约束的配置快照

    三个上限都是"滚动窗口内累计变化量的绝对值",非正值表示该项不限制。
    跌的日上限比涨的宽：现实里信任崩塌确实比建立快，但仍然要有上限——
    否则一次刷屏辱骂就能把一个老朋友一天之内打到谷底。
    """

    daily_gain: int
    daily_drop: int
    weekly_gain: int
    diminishing: bool = True


class AffectionWindowUsage(NamedTuple):
    """滚动窗口内已经用掉的额度

    三项都是非负数：`daily_drop` 记的是跌幅的绝对值，不是负数。
    """

    daily_gain: int
    daily_drop: int
    weekly_gain: int

    @classmethod
    def unused(cls) -> "AffectionWindowUsage":
        """一点额度都没用过；也是统计查不出来时的兜底值"""
        return cls(0, 0, 0)


def clamp_score(score: int) -> int:
    """把分数收进合法区间"""
    return max(AFFECTION_MIN_SCORE, min(AFFECTION_MAX_SCORE, score))


def clamp_delta(delta: int, configured_max: int) -> int:
    """把单轮变化量收进合法区间

    Args:
        delta: LLM 给出的原始变化量
        configured_max: 配置的单轮上限；非正值视为未配置，回落到硬上限

    Returns:
        收敛后的变化量，绝对值不超过 `AFFECTION_HARD_MAX_DELTA`
    """
    limit = AFFECTION_HARD_MAX_DELTA if configured_max <= 0 else min(configured_max, AFFECTION_HARD_MAX_DELTA)
    return max(-limit, min(limit, delta))


def scale_gain(delta: int, current_score: int) -> int:
    """按当前分数对涨幅做递减收益

    只作用于正的变化量：跌不打折，这是"信任崩塌比建立快"的那一半不对称。

    正的变化量**至少保留 1 点**。打折打到 0 会让高分段彻底停止增长，"交心"就成了
    一个永远差最后一步的数字；真正的封顶交给窗口预算，而不是靠取整把人卡死。

    Args:
        delta: 已经过 `clamp_delta` 收敛的变化量
        current_score: 本次变化之前的分数

    Returns:
        打折后的变化量，绝对值不超过入参
    """
    if delta <= 0:
        return delta
    bounded = clamp_score(current_score)
    for band in _GAIN_BANDS:
        if bounded >= band.min_score:
            return max(1, delta * band.percent // 100)
    return delta


def limit_delta(
    delta: int,
    *,
    current_score: int,
    usage: AffectionWindowUsage,
    limits: AffectionRateLimits,
) -> int:
    """把一次自动评估的变化量收进递减收益与滚动窗口预算之内

    纯函数：窗口用量由调用方（:mod:`.store`）查流水算好后传进来，本模块不碰数据库。

    Args:
        delta: 已经过 `clamp_delta` 收敛的变化量
        current_score: 本次变化之前的分数，决定递减收益的档位
        usage: 滚动窗口内已经用掉的额度
        limits: 速率约束配置

    Returns:
        实际可以落库的变化量；额度耗尽时为 0，符号与入参一致
    """
    if delta > 0:
        gain = scale_gain(delta, current_score) if limits.diminishing else delta
        return min(
            gain,
            _remaining(limits.daily_gain, usage.daily_gain),
            _remaining(limits.weekly_gain, usage.weekly_gain),
        )
    if delta < 0:
        return max(delta, -_remaining(limits.daily_drop, usage.daily_drop))
    return 0


def _remaining(limit: int, used: int) -> int:
    """滚动窗口内还剩多少额度

    非正的上限视为未配置，回落到单轮硬上限而不是无穷大：传进来的变化量本来就已经被
    `clamp_delta` 收敛过，再取一次 min 不改变结果，却省掉了一个哨兵值。
    """
    if limit <= 0:
        return AFFECTION_HARD_MAX_DELTA
    return max(0, limit - used)


def stage_of(score: int) -> AffectionStage:
    """由分数派生阶段"""
    bounded = clamp_score(score)
    for spec in _STAGE_SPECS:
        if bounded >= spec.min_score:
            return spec.stage
    return AffectionStage.HOSTILE


def stage_label(stage: AffectionStage) -> str:
    """阶段的中文名"""
    return _spec_of(stage).label


def stage_tone(stage: AffectionStage) -> str:
    """阶段对应的语气指引"""
    return _spec_of(stage).tone


def stage_rank(stage: AffectionStage) -> int:
    """阶段在高低次序中的位次，越大越亲近"""
    return _STAGE_ORDER.index(stage)


def meets_stage(score: int, required: AffectionStage) -> bool:
    """门槛判定：当前分数是否达到了要求的阶段

    插件与业务逻辑判断"够不够亲近才给做某件事"时统一走这里，不要各自比较裸分数——
    阈值一旦调整，散落各处的魔数会立刻失真。
    """
    return stage_rank(stage_of(score)) >= stage_rank(required)


def reply_eagerness(score: int) -> int:
    """好感度对**非明示触发**的回复意愿，返回 0-100 的百分比乘数

    只表示「Bot 有多愿意主动搭话」，不表示「会不会回答被点名的问题」——
    明示召唤（@、私聊、喊名字、trigger_agent、FORCE_TRIGGER）不经过这里。
    档位规则见 `_REPLY_EAGERNESS_BANDS` 的表头注释。
    """
    bounded = clamp_score(score)
    for band in _REPLY_EAGERNESS_BANDS:
        if bounded >= band.min_score:
            return band.percent
    return 100


def all_stages() -> tuple[AffectionStage, ...]:
    """全部阶段，按由低到高排列，供后台筛选器使用"""
    return _STAGE_ORDER


def _spec_of(stage: AffectionStage) -> _StageSpec:
    for spec in _STAGE_SPECS:
        if spec.stage is stage:
            return spec
    raise ValueError(f"未知好感度阶段: {stage}")
