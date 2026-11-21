"""LLM 用量流水表

## 为什么需要它

上线前唯一的配额是**回复条数**（`AI_CHAT_DAILY_REPLY_LIMIT` + `services/quota_service.py`），
而一次超长上下文回复和一次「好的」在计费上差两个数量级——条数配额挡不住。
token 数此前**只在单次请求里存在**（`services/agent/openai.py` 读了 `usage.*` 用完即弃），
`DBModelGroup.token_input_rate` / `token_completion_rate` 两列有值但全仓没有任何消费点。

共用版 Bot 烧的是平台方的 Key。算不出每个客户烧了多少，就做不了按量计费、
超支时无法止损、出账单时没有凭据。本表就是那本账。

## 一行 = 一次**上游请求**，不是一轮对话

一轮对话可能发出 1~N 次请求：重试、fallback 换组、剥图重试、以及回合内的多次迭代。
每一次都真金白银，因此每一次各记一行，用 `attempt_index` / `is_fallback` / `status`
把它们区分开。想看「一轮对话花了多少」就按 `chat_key` + 时间窗聚合。

**失败的那几次也记。** 上游 2xx 但正文为空的那一类是**已经计过费**的
（实测有中转站在 `completion_tokens=0` 而 `reasoning_tokens=1116` 时照样计费，
见 `openai.EmptyLLMResponseError` 的 docstring）；网络层失败拿不到 usage，
两个 token 列会是 0，但那一行仍然要在——「这个实例今天失败了 400 次」本身就是运营信号。

## 归属维度复用 `services/identity_scope.py`

| 维度 | 列 | 来源 |
|---|---|---|
| 实例 | `adapter_key` + `instance_key` | 计费单元。**始终是真实实例**，不因为它被编进域而清空 |
| 域 | `domain_id` | `ScopeDomain.domain_id`（`g:<组ID>` / `i:<适配器>:<实例>`），字符串编码的唯一产生者就是它 |
| 租户 | `tenant_id` | 写入时按实例解析出的**快照**，NULL = 平台方自己或无法归属 |
| 人 | `person_uid` | `identity_scope.resolve_person_uid` 的输出；空串 = 本次请求与具体的人无关 |
| 人设 | `preset_id` | 0 = 无人设档 |

⚠️ **`person_uid` 目前没有任何生产者**（`LLMUsageContext.with_identity` 全仓零调用方）。
它不是被忘掉的，而是**计费单元不是「人」**：一次 Agent 回合的 prompt
装的是整个频道的上下文，把这笔钱记在"最后说话的那个人"头上是错的归因，不是缺失的归因。
按人归因没有站得住的口径，所以这一列
先留着不填，而**不要**在读侧假装它有值。`preset_id` 只有记忆整合那一档在填（那条链路的
隔离键本来就带人设），其余场景恒 0，同理。

出账口径因此是「**哪个租户 / 哪个实例**烧了多少」，不是「哪个人」。

这里**不用** `ScopeDomain.as_columns()`：那套三列编码在属于实例组时会把
`adapter_key` / `instance_key` 写成空串（对好感表是对的，域才是好感的键），
而用量的计费单元恰恰是实例本身，清空等于把账算不回去。域另用字符串编码放在
`domain_id` 一列，两者不冲突也不重复。

### `tenant_id` 是快照，不随实例改归属而回溯

实例从租户 A 划归 B 之后，A 名下已产生的流水仍然是 A 的账（钱确实是那时候烧的），
B 只看得到划归之后的行。这是计费语义要求的，不是遗漏。

## 费用是**当时的**倍率算出来的，一并落库

`token_input_rate` / `token_completion_rate` / `model_price_rate` 三列是请求发生时那个
模型组的取值快照。只存 token 数、查询时再去 join 模型组的话，运维哪天调一次倍率，
**历史账单会集体改写**——那是出账凭据最不该有的性质。

## 不落任何上游原文

`error_type` 只存异常类名，`finish_reason` 只存归一化后、且属于固定集合的取值。
上游报错体里回显凭据是常见形态（见 `services/agent/upstream_redaction.py`），
而本表对租户可读——把原文写进来等于绕过所有 HTTP 层掩码的一次直投。
"""

from tortoise import fields
from tortoise.models import Model

from nekro_agent.schemas.usage import LLMUsageScene, LLMUsageStatus


class DBLLMUsageRecord(Model):
    """一次上游 LLM 请求的用量与费用"""

    id = fields.BigIntField(pk=True, generated=True, description="ID")

    # ==== 归属（见模块文档；三维全部来自 services/identity_scope.py） ====
    adapter_key = fields.CharField(max_length=64, default="", index=True, description="适配器标识")
    instance_key = fields.CharField(max_length=64, default="", index=True, description="实例标识；空串表示无法归属到实例")
    domain_id = fields.CharField(
        max_length=160,
        default="",
        index=True,
        description="记忆域的字符串编码（ScopeDomain.domain_id）；空串表示未解析",
    )
    tenant_id = fields.IntField(null=True, index=True, description="归属租户ID快照；NULL = 平台方或无法归属")
    chat_key = fields.CharField(max_length=256, default="", index=True, description="会话标识；空串表示与会话无关")
    person_uid = fields.CharField(max_length=256, default="", description="人的标识；空串表示与具体的人无关")
    preset_id = fields.IntField(default=0, description="人设标识；0 表示无人设档")

    # ==== 用哪个模型组 / 哪个模型 ====
    model_group_name = fields.CharField(max_length=128, default="", index=True, description="模型组名（运行时视图的字典键）")
    model_name = fields.CharField(max_length=256, default="", index=True, description="实际使用的模型名")
    stream_mode = fields.BooleanField(default=False, description="是否流式请求")

    # ==== 烧了多少 ====
    token_input = fields.IntField(default=0, description="输入（prompt）token 数")
    token_input_cached = fields.IntField(
        default=0,
        description="输入里命中上游缓存的 token 数（上游不回时为 0）",
    )
    token_output = fields.IntField(default=0, description="输出（completion）token 数")
    token_total = fields.IntField(default=0, description="上游给出的总 token 数")

    # ==== 折算多少钱（倍率是**当时的**快照，见模块文档） ====
    token_input_rate = fields.FloatField(default=1.0, description="计费时使用的输入 Token 倍率")
    token_completion_rate = fields.FloatField(default=1.0, description="计费时使用的补全 Token 倍率")
    model_price_rate = fields.FloatField(default=1.0, description="计费时使用的模型价格倍率")
    cost = fields.FloatField(default=0.0, description="折算费用")

    # ==== 什么形态（失败/重试/fallback 要能分开看） ====
    scene = fields.CharField(max_length=32, default=LLMUsageScene.UNATTRIBUTED.value, index=True, description="调用场景")
    status = fields.CharField(max_length=16, default=LLMUsageStatus.SUCCESS.value, index=True, description="收尾形态")
    # 失败归类**只用异常类名**，不另存一份 `LLMFailureKind`：
    # `classify_llm_failure` 的判据里有「本次请求是否真的带了图」这一维，而那是调用方
    # （`send_agent_request`）才知道的东西，记账点在 `openai.py` 里够不到。硬要塞进来
    # 就得让 openai.py 反过来 import `model_capability`，而后者正 import 着 openai
    # ——一个纯粹为了多一列而造出来的循环依赖。异常类名本身已经把最关键的那几支分开了
    # （`LLMContentBlockedError` / `LLMMalformedToolCallError` / `EmptyLLMResponseError`
    # / 上游的 `APIStatusError` / `TimeoutError`），够运营判断「这一档为什么涨」。
    error_type = fields.CharField(max_length=128, default="", description="异常类名；**不含任何上游原文**")
    finish_reason = fields.CharField(max_length=64, default="", description="归一化后的上游收尾原因，取自固定集合")
    attempt_index = fields.IntField(default=1, description="本轮里的第几次尝试，从 1 开始")
    attempt_total = fields.IntField(default=1, description="本轮计划的尝试总数")
    is_fallback = fields.BooleanField(default=False, index=True, description="本次是否用的备用模型组")

    generation_time_ms = fields.IntField(default=0, description="本次请求耗时（毫秒）")

    create_time = fields.DatetimeField(auto_now_add=True, index=True, description="创建时间")

    class Meta:  # type: ignore
        table = "llm_usage_record"


__all__ = ["DBLLMUsageRecord", "LLMUsageScene", "LLMUsageStatus"]
