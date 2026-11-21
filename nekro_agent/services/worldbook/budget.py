"""世界书每轮预算：按 token 计，照 SillyTavern 的「占上下文的百分比」与「token 上限」

```
base   = max(0, 上下文长度 − 回复预留)
budget = round(百分比 / 100 × base)
上限 > 0 时 budget = min(budget, 上限)
budget = min(budget, 65536)                 # 硬上限：上限填 0 也不会超过它
budget = max(budget, 1)                     # 与 ST 的 `|| 1` 一致
```

- 上限 0 的意思是「不另设上限，按百分比算」，但预算始终不超过 :data:`HARD_CAP_TOKENS`：
  模型组的上下文长度最多能填一千万，25% 就是每轮两百多万 token，一轮的费用没有边。

- 上下文长度取这一轮**主模型组**（频道生效配置里的 `USE_MODEL_GROUP`）上填的 `CONTEXT_LENGTH`；
  没填（0）按 :data:`DEFAULT_CONTEXT_LENGTH` 算。不按模型名猜：中转站的模型名千奇百怪，猜成
  一百万的上下文，25% 就是每轮 26 万 token。
- 回复预留固定 :data:`OUTPUT_RESERVE_TOKENS`：ST 用界面上的「最大回复长度」，NA 的主对话
  请求不设 `max_tokens`，没有对应的数。
- 备用模型组不另算：预算在发请求之前就定了，换组重试不重算；备用组上下文更小时，
  结果里带上它，明细据此写一句提示。

运行时、触发测试、提示词预览、导入报告都调 :func:`resolve_budget`，同一份口径。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

from nekro_agent.core.config import config as global_config

from . import tokens

OUTPUT_RESERVE_TOKENS = 4096
DEFAULT_CONTEXT_LENGTH = 32768
PERCENT_MIN = 1
PERCENT_MAX = 100
DEFAULT_PERCENT = 25
CAP_MAX = 65536
# 每轮预算的硬上限：不管百分比、上限怎么填，都不超过它（与上限能填的最大值相同）
HARD_CAP_TOKENS = CAP_MAX
# 上下文长度在这以内时，减去回复预留后不剩什么，预算只有 1 token
SMALL_CONTEXT_LENGTH = OUTPUT_RESERVE_TOKENS


@dataclass(frozen=True)
class WorldbookBudget:
    """一轮的世界书预算，以及它是怎么算出来的（明细、测试页、预览照原样显示）

    Attributes:
        model_group: 这一轮主模型组的名字。
        context_length: 用来算预算的上下文长度（组上没填时是 :data:`DEFAULT_CONTEXT_LENGTH`）。
        context_length_set: 组上是否填了上下文长度。
        output_reserve: 回复预留。
        percent: 占上下文的百分比（1 ~ 100）。
        cap_tokens: token 上限；0 表示不另设上限（按百分比算，仍不超过 :data:`HARD_CAP_TOKENS`）。
        base: `max(0, context_length − output_reserve)`。
        budget: 最终预算（token）。
        factor: 这一轮的分词系数。
        factor_source: 系数从哪来，见 :class:`tokens.TokenFactor`。
        factor_family: 按模型名匹配到的家族。
        estimator: 计数方式 `tiktoken` / `chars`。
        fallback_model_group: 备用模型组的名字（与主组相同或没配时为空）。
        fallback_context_length: 备用组的上下文长度比主组小时记下它，否则为 0。
    """

    model_group: str
    context_length: int
    context_length_set: bool
    output_reserve: int
    percent: int
    cap_tokens: int
    base: int
    budget: int
    factor: float
    factor_source: str
    factor_family: str
    estimator: str
    fallback_model_group: str = ""
    fallback_context_length: int = 0

    def as_dict(self) -> dict[str, Any]:
        """连同「预算约合多少中文字」一起，给明细、测试页、预览原样显示"""
        return {**asdict(self), "approx_chars": tokens.approx_chars(self.budget, self.factor)}

    def count(self, text: str) -> int:
        """按这一轮的系数数一段正文"""
        return tokens.count_tokens(text, self.factor)


def compute_budget(context_length: int, percent: int, cap_tokens: int) -> tuple[int, int]:
    """按 ST 的公式算 `(base, budget)`；参数先夹进合法区间"""
    percent = max(PERCENT_MIN, min(PERCENT_MAX, int(percent)))
    cap_tokens = max(0, min(CAP_MAX, int(cap_tokens)))
    base = max(0, int(context_length) - OUTPUT_RESERVE_TOKENS)
    budget = round(percent / 100 * base)
    if cap_tokens > 0:
        budget = min(budget, cap_tokens)
    return base, max(min(budget, HARD_CAP_TOKENS), 1)


def ignore_budget_cap(budget: WorldbookBudget) -> int:
    """「忽略预算」条目每轮合计最多多少 token

    这类条目不受这一轮预算拦，但不能没有边：合计不超过 token 上限（这个频道生效的
    `WORLDBOOK_BUDGET_CAP_TOKENS`，租户只能往下调），上限为 0 时不超过 :data:`HARD_CAP_TOKENS`。
    超出的那几条不再豁免，按普通条目判（放不下就记 `global_tokens`）。
    """
    return budget.cap_tokens if budget.cap_tokens > 0 else HARD_CAP_TOKENS


def is_small_context(context_length: int) -> bool:
    """填了上下文长度、但小到减去回复预留后不剩什么（世界书预算只有 1 token）"""
    return 0 < int(context_length) <= SMALL_CONTEXT_LENGTH


def _int_setting(source: Any, name: str, default: int) -> int:
    try:
        return int(getattr(source, name, default))
    except (TypeError, ValueError):
        return default


def _group(name: str) -> Optional[Any]:
    """运行时视图里的模型组；找不到为 None（预算照样能算，按默认上下文与系数）"""
    groups = getattr(global_config, "MODEL_GROUPS", None) or {}
    return groups.get(name) if name else None


def _context_length_of(group: Optional[Any]) -> int:
    try:
        return max(0, int(getattr(group, "CONTEXT_LENGTH", 0) or 0)) if group is not None else 0
    except (TypeError, ValueError):
        return 0


def resolve_budget(effective_config: Any) -> WorldbookBudget:
    """按一份生效配置（频道 / 实例 / 全局）算这一轮的世界书预算

    只读内存里的运行时视图，不查库、不抛：读不到的项按默认值，预算总能算出来。
    计数方式按「此刻编码是否已加载」报；异步入口应先 `await tokens.ensure_loaded_async()`。
    """
    group_name = str(getattr(effective_config, "USE_MODEL_GROUP", "") or "")
    group = _group(group_name)
    raw_context = _context_length_of(group)
    context_length = raw_context if raw_context > 0 else DEFAULT_CONTEXT_LENGTH
    percent = _int_setting(effective_config, "WORLDBOOK_BUDGET_PERCENT", DEFAULT_PERCENT)
    cap_tokens = _int_setting(effective_config, "WORLDBOOK_BUDGET_CAP_TOKENS", 0)
    base, budget = compute_budget(context_length, percent, cap_tokens)
    factor = tokens.factor_for_group(group)

    fallback_name = str(getattr(effective_config, "FALLBACK_MODEL_GROUP", "") or "")
    fallback_context = 0
    if fallback_name and fallback_name != group_name:
        raw_fallback = _context_length_of(_group(fallback_name))
        effective_fallback = raw_fallback if raw_fallback > 0 else DEFAULT_CONTEXT_LENGTH
        if effective_fallback < context_length:
            fallback_context = effective_fallback

    return WorldbookBudget(
        model_group=group_name,
        context_length=context_length,
        context_length_set=raw_context > 0,
        output_reserve=OUTPUT_RESERVE_TOKENS,
        percent=max(PERCENT_MIN, min(PERCENT_MAX, percent)),
        cap_tokens=max(0, min(CAP_MAX, cap_tokens)),
        base=base,
        budget=budget,
        factor=factor.value,
        factor_source=factor.source,
        factor_family=factor.family,
        estimator=tokens.estimator(),
        fallback_model_group=fallback_name if fallback_context else "",
        fallback_context_length=fallback_context,
    )


__all__ = [
    "CAP_MAX",
    "DEFAULT_CONTEXT_LENGTH",
    "DEFAULT_PERCENT",
    "HARD_CAP_TOKENS",
    "OUTPUT_RESERVE_TOKENS",
    "SMALL_CONTEXT_LENGTH",
    "WorldbookBudget",
    "compute_budget",
    "is_small_context",
    "resolve_budget",
]
