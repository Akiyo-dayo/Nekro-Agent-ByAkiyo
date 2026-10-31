"""用量归属上下文：一次 LLM 请求「算在谁头上」的**唯一**载体

## 为什么是 contextvar 而不是给每个函数加参数

拿到 usage 的地方全在 `services/agent/openai.py` 里（三处），而**声明归属**的地方散在十几个
调用方：Agent 回合、上下文规划、好感评估、记忆整合、图片描述、插件生成、连通性测试、聊天命令。
给 `gen_openai_chat_response` 加十几个入参、再让每个调用方逐个透传，等于把「记账」这件事
摊进每一个调用点的签名里——只要有一个新调用点忘了传，它烧的钱就悄悄不见了，而且没有任何信号。

contextvar 反过来：**不声明也会记账**，只是落进 `LLMUsageScene.UNATTRIBUTED` 这一档。
钱一分不少，只是算不到人头上；而那一档在平台方的用量页上是独立可见的，涨起来就说明
有一条链路没接上。这比"静默丢失"好在它是**可发现**的。

contextvar 的传播语义正好合用：`asyncio.create_task` 会复制当前上下文，所以回合里派生的
子任务自动继承归属；而它不会跨进程/跨请求泄漏到别人身上。

## 与 `services/identity_scope.py` 的关系

归属的三维（域 / 人 / 人设）**不在这里解析**，这里只搬运解析结果或原始入参：

- 调用方手上已经有 `IdentityScope` 时用 :meth:`LLMUsageContext.with_identity` 填进来；
- 只有 `chat_key` 时什么都不填，由写侧（`ledger._resolve_attribution`）去查频道行、
  再调 `identity_scope.resolve_memory_domain`。**写侧在后台任务里跑，不占主链路。**

无论哪条路径，域的字符串编码都只由 `ScopeDomain.domain_id` 产生，本模块不拼域。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Iterator, Mapping, Optional

from nekro_agent.core.config import ModelConfigGroup
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.schemas.usage import LLMUsageScene

logger = get_sub_logger("llm_usage")


@dataclass(frozen=True)
class LLMUsageContext:
    """一次（或一组）LLM 请求的归属声明

    **不可变**：每一次尝试都 `replace` 出一份新的，而不是就地改。同一轮里三次重试的
    `attempt_index` 必须互不相同，就地改会让并发的另一条链路读到别人的尝试序号。
    """

    scene: LLMUsageScene = LLMUsageScene.UNATTRIBUTED

    # ==== 归属 ====
    # `adapter_key` 与 `instance_key` 要么**成对**声明、要么都不声明：写侧的分支是
    # `if not adapter_key and context.chat_key`，只填适配器会跳过频道行查询而实例段仍是空串，
    # 账当场归不到实例、也就归不到租户。只有 `chat_key` 时两个都留空，由写侧补全。
    adapter_key: str = ""
    instance_key: str = ""
    chat_key: str = ""
    domain_id: str = ""
    person_uid: str = ""
    preset_id: int = 0
    # 显式租户归属。`None` = 由实例反查（对话主链路走这条）。
    #
    # 存在的理由是**有些花销根本没有实例**：租户管理员在总后台点一次「模型组连通性测试」
    # 打的是真上游、烧的是真钱，可那一跳既没有会话也没有实例，按实例反查只能得到 NULL，
    # 于是它既不出现在这个租户自己的账单里（`records()` 按 `tenant_id` 过滤），
    # 也在平台方页面上混进 `unattributed` 那一档。声明出来才算得清。
    #
    # 实例反查得到的结果**优先**：主链路的归属永远以频道/实例这条链为准，本字段不参与竞争。
    tenant_id: Optional[int] = None

    # ==== 用哪个模型组（倍率随组走，是**当时**的快照） ====
    model_group_name: str = ""
    token_input_rate: float = 1.0
    token_completion_rate: float = 1.0
    model_price_rate: float = 1.0

    # ==== 这是本轮的第几次尝试 ====
    attempt_index: int = 1
    attempt_total: int = 1
    is_fallback: bool = False

    def with_model_group(self, name: str, group: Optional[ModelConfigGroup]) -> "LLMUsageContext":
        """填入模型组名与它的三个计费倍率

        倍率在这里就取走、随流水落库，不是查询时再去 join 模型组表：
        运维哪天调一次倍率，join 的写法会让**历史账单集体改写**。
        """
        if group is None:
            return replace(self, model_group_name=name)
        return replace(
            self,
            model_group_name=name,
            token_input_rate=group.TOKEN_INPUT_RATE,
            token_completion_rate=group.TOKEN_COMPLETION_RATE,
            model_price_rate=group.MODEL_PRICE_RATE,
        )

    def with_identity(self, scope: object) -> "LLMUsageContext":
        """从一个 `identity_scope.IdentityScope` 填入域 / 人 / 人设

        ⚠️ **当前全仓零调用方**（round-23 复核核对过），因此 `person_uid` 这一列恒为空串。
        这不是漏接线：一次 Agent 回合的 prompt 装的是整个频道的上下文，把这笔钱记在
        "最后说话的那个人"头上是**错的归因**。按人计费要先有产品口径
        （PROJECT_STATE §8 第 20 条），本方法留着是为了那一天到来时不用再造一遍。
        在它有调用方之前，**不要在读侧假装 `person_uid` 有值**。

        入参故意打成 `object`：本模块被 `services/agent/openai.py` 在模块级导入，
        而 `identity_scope` 会拖进实例组与别名两张表的模型。用鸭子类型读三个属性即可，
        判据仍然只有 `identity_scope` 那一份（这里一个字段都不重新计算）。
        """
        return replace(
            self,
            domain_id=str(getattr(scope, "domain_id", "") or ""),
            person_uid=str(getattr(scope, "person_uid", "") or ""),
            preset_id=int(getattr(scope, "preset_id", 0) or 0),
        )

    def for_attempt(self, *, index: int, total: int, is_fallback: bool = False) -> "LLMUsageContext":
        """派生出「本轮第 index 次尝试」的上下文"""
        return replace(self, attempt_index=index, attempt_total=total, is_fallback=is_fallback)


# 未声明归属时的取值。**不是 None**：读侧因此永远拿得到一个可用的上下文，
# 不需要在每个记账点写一次 `if ctx is None`（那正是"同一个判据的第 N 处实现"的温床）。
UNATTRIBUTED_CONTEXT = LLMUsageContext()

_current_usage_context: ContextVar[LLMUsageContext] = ContextVar(
    "llm_usage_context",
    default=UNATTRIBUTED_CONTEXT,
)


def current_usage_context() -> LLMUsageContext:
    """读出当前生效的归属声明；没有声明时返回 :data:`UNATTRIBUTED_CONTEXT`"""
    return _current_usage_context.get()


@contextmanager
def usage_scope(context: LLMUsageContext) -> Iterator[LLMUsageContext]:
    """在此块内声明归属

    用法::

        with usage_scope(LLMUsageContext(scene=LLMUsageScene.CHAT, chat_key=chat_key)):
            await gen_openai_chat_response(...)

    **必须用 token 还原而不是设回旧值**：`ContextVar.set` 在同一个上下文里嵌套时，
    设回"我进来时读到的那个值"会把外层的声明写成内层的形态（外层若从未 set 过，
    读到的是默认值，还原后外层就永久变成了未声明）。

    ## `reset` 会跨上下文，而那**不是**假想情况

    本块跨过一个 `yield` 时（`plugin/generator.py:generate_plugin_code_stream` 就是这个形态：
    `with usage_scope(...): async for chunk in ...: yield chunk`），退出这一步可能发生在
    **另一个 Task 的上下文里**——异步生成器没有自己的上下文（PEP 568 至今未落地），它的每一步
    都跑在当次驱动它的那个任务里；而消费方提前放弃生成器时，收尾是由 asyncio 的
    asyncgen finalizer 用 `loop.create_task(agen.aclose())` 驱动的，那是一个**新拷贝的**上下文。
    此时 `ContextVar.reset(token)` 抛 `ValueError: <Token …> was created in a different Context`，
    并在 GeneratorExit 处理途中变成一条 `an error occurred during closing of asynchronous
    generator` —— 实测可复现（WebUI 的插件代码生成页面被关掉就走这条路）。

    这里咽掉那一条并只打 debug：**记账是观测，它的收尾动作不该在用户关掉一个页面时刷 ERROR。**
    咽掉的代价只是"那个上下文里的声明没被还原"，而那个上下文本来就是收尾任务自己的一次性拷贝，
    随任务一起消失。归属本身不依赖这次还原——见 `openai.gen_openai_chat_stream` 在**入口**
    就把上下文快照下来的那一段。
    """
    token = _current_usage_context.set(context)
    try:
        yield context
    finally:
        try:
            _current_usage_context.reset(token)
        except ValueError:
            logger.debug("用量归属声明在另一个上下文里收尾，跳过还原（异步生成器被外部 finalizer 关闭）")


def resolve_model_group_name(
    model_groups: Mapping[str, ModelConfigGroup],
    group: Optional[ModelConfigGroup],
) -> str:
    """把一个 `ModelConfigGroup` 反查成它在运行时视图里的**字典键**

    账要按"哪个模型组"算，而 `ModelConfigGroup.GROUP_NAME` 是**显示名**、可以重复也可以为空；
    真正的身份是 `config.MODEL_GROUPS` 的键（`DBModelGroup.name` 全局唯一，
    `USE_MODEL_GROUP` 等配置项也是按这个键引用的）。

    按**对象同一性**反查：`resolve_primary_model_group` / `resolve_fallback_model_group`
    返回的就是字典里的那个对象。同一性对不上（调用方自己构造了一份）时退回显示名，
    退不出来就给空串——记一行"组名不详"的账，好过让记账本身抛异常打掉一次回复。
    """
    if group is None:
        return ""
    for name, candidate in model_groups.items():
        if candidate is group:
            return name
    return group.GROUP_NAME or ""


__all__ = [
    "UNATTRIBUTED_CONTEXT",
    "LLMUsageContext",
    "LLMUsageScene",
    "current_usage_context",
    "resolve_model_group_name",
    "usage_scope",
]
