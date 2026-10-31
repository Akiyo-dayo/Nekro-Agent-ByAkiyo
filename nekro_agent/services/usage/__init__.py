"""LLM 用量计量

- `context`：一次请求「算在谁头上」的声明（contextvar）
- `ledger`：算钱与落库（后台任务，不占主链路）
- `query`：读侧的作用域与聚合，供 `routers/console/usage.py` 使用

**本轮只做账本与视图，不做扣费。** 扣费策略（按量计费还是包月、超支停机还是降级、
预警阈值给谁）是产品决策，见 `PROJECT_STATE.md` §8。
"""

from .context import (
    UNATTRIBUTED_CONTEXT,
    LLMUsageContext,
    current_usage_context,
    resolve_model_group_name,
    usage_scope,
)
from .ledger import (
    LLMUsageScene,
    LLMUsageStatus,
    compute_token_cost,
    flush_llm_usage,
    record_llm_usage,
    reset_usage_recorder,
    usage_recorder_stats,
)

__all__ = [
    "UNATTRIBUTED_CONTEXT",
    "LLMUsageContext",
    "LLMUsageScene",
    "LLMUsageStatus",
    "compute_token_cost",
    "current_usage_context",
    "flush_llm_usage",
    "record_llm_usage",
    "reset_usage_recorder",
    "resolve_model_group_name",
    "usage_recorder_stats",
    "usage_scope",
]
