"""好感度核心服务

好感度按 **(域, 人, 人设)** 隔离——三维都由 `services/identity_scope.py` 解析，
键由 :class:`~.key.AffectionKey` 承载，**本包内外都不许自己拼键**。
隔离键不含会话：同一个人在群里和私聊里是同一份（但换了人设就是另一份）。

分两层：

- **数值层**（:mod:`.stage`）—— 分数、阶段、门槛判定。它是内部实现，不是产品的面孔。
- **叙述层**（:mod:`.evaluator`）—— 由 LLM 按当前人设口吻写出的状态与变化描述，
  这才是注入提示词、呈现给用户的那一层。

主对话链路只碰两个入口：

- :func:`~.prompt.render_affection_prompt` —— 读，永不调用 LLM，任何失败都退化成空串；
- :func:`~.evaluator.schedule_affection_evaluation` —— 写，登记一个后台任务后立刻返回。

两者都保证不会让主对话因为好感度而失败或变慢。

后台还有一条不在对话链路上的入口：:mod:`.merge`——把两个平台账号认成同一个人
（换号不能丢），以及把它拆开。
"""

from .key import AffectionKey
from .merge import MergeResult, merge_accounts, unmerge_account
from .prompt import affection_score_of, render_affection_prompt
from .stage import (
    AFFECTION_MAX_SCORE,
    AFFECTION_MIN_SCORE,
    AffectionEventSource,
    AffectionRateLimits,
    AffectionStage,
    AffectionWindowUsage,
    all_stages,
    clamp_delta,
    clamp_score,
    limit_delta,
    meets_stage,
    scale_gain,
    stage_label,
    stage_of,
    stage_tone,
)

__all__ = [
    "AFFECTION_MAX_SCORE",
    "AFFECTION_MIN_SCORE",
    "AffectionEventSource",
    "AffectionKey",
    "AffectionRateLimits",
    "AffectionStage",
    "AffectionWindowUsage",
    "MergeResult",
    "affection_score_of",
    "all_stages",
    "clamp_delta",
    "clamp_score",
    "limit_delta",
    "meets_stage",
    "merge_accounts",
    "render_affection_prompt",
    "scale_gain",
    "stage_label",
    "stage_of",
    "stage_tone",
    "unmerge_account",
]
