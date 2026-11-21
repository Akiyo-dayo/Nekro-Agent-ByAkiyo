"""用量流水的枚举与查询契约

放在 `schemas/` 而不是模型或服务里，理由与 `schemas/tenant.py:ModelGroupOwnership` 相同：
模型层（`models/db_llm_usage.py`）、服务层（`services/usage/*`）与
`services/agent/openai.py` 三边都要用它，而 `openai.py` 在模块级导入模型包会把整张
模型表（连带频道行那条 import 链）拖进 LLM 请求路径的导入图里。
枚举本身没有依赖，谁都可以引。
"""

from enum import Enum


class LLMUsageStatus(str, Enum):
    """一次上游请求的收尾形态

    只分到「账上要不要分开看」的粒度：

    - `SUCCESS`：拿到了可用正文
    - `FAILED`：抛异常收尾（网络错误、上游 4xx/5xx、空响应、内容拦截……）
    - `ABORTED`：流式生成器被消费方提前丢弃（例如 WebUI 关掉了那个页面）。
      它既不是成功也不是失败，但**已经产生了 token**，必须单列一档；
      混进 `FAILED` 会让"失败率"这个运营指标失真。
    """

    SUCCESS = "success"
    FAILED = "failed"
    ABORTED = "aborted"


class LLMUsageScene(str, Enum):
    """这次请求是替谁烧的

    分档依据是「运营看到某一档暴涨时会去动的那个开关不同」：对话主链路要看配额，
    好感/记忆是后台评估（可以关掉），插件生成与连通性测试是人点出来的。
    """

    CHAT = "chat"  # Agent 回合主链路
    # 向量化（`gen_openai_embeddings`）。**单列一档而不是并进调用它的那条链路**：
    # 它是唯一一条按「每条 query 一次请求」放大的花销（一次增强检索会打出 N 次），
    # 而运营看到它涨时要动的开关（记忆检索/知识库/表情包向量）与对话主链路完全不同。
    EMBEDDING = "embedding"
    HISTORY_PLAN = "history_plan"  # 上下文规划（`services/agent/templates/history.py`）
    AFFECTION_EVAL = "affection_eval"  # 好感度评估
    MEMORY_CONSOLIDATE = "memory_consolidate"  # 记忆整合
    VISION_DESCRIBE = "vision_describe"  # 图片描述（`plugins/builtin/view_image.py`）
    PLUGIN = "plugin"  # 插件自行发起
    PLUGIN_GENERATE = "plugin_generate"  # WebUI 插件代码生成
    MODEL_TEST = "model_test"  # 模型组连通性测试
    # 后台提示词试发：归属到实例与模型组、**不挂任何频道**，所以不占频道每日额度
    # （账本只在有 chat_key 时才累加按群计数）。回复只给发起的管理员看。
    PROMPT_TRIAL = "prompt_trial"
    COMMAND = "command"  # 聊天命令触发的调试请求
    # 没有任何调用方声明归属。**不是丢弃**：钱照样烧了，只是算不到人头上。
    # 它在平台方的用量页上是一个独立分档，涨起来就说明有一条链路没接上记账。
    UNATTRIBUTED = "unattributed"


__all__ = ["LLMUsageScene", "LLMUsageStatus"]
