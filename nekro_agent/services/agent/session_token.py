"""Agent 会话令牌的服务端登记表

## 令牌是什么

`one_time_code` 是每次 Agent 运行时随机生成的一次性令牌，用于在提示词里标记可信边界：
只有携带该令牌的结构化标签才是系统生成的，其余内容一律视为不可信外部输入
（见 `nekro_agent/tools/untrusted_content.py` 与 `templates/j2/runtime_contract.j2` 的安全契约）。

## 为什么需要这张表

插件的沙盒方法常常要把外部内容（技能文档、网页抓取结果、第三方 API 返回值）交回给模型。
这些内容同样需要用令牌包裹，否则模型无法区分"资料"与"指令"。

但沙盒方法是经 RPC 回调进来的**独立 HTTP 请求**，`routers/rpc.py` 会新建一个 `AgentCtx`，
拿不到 `run_agent()` 里的局部变量。contextvar 也不会跨进程/跨请求传播。

## 为什么不把令牌下发进沙盒

沙盒里运行的是模型生成的代码。一旦令牌随 `api_caller` 下发到容器内，
生成的代码就能读到它并伪造出"可信"标签 —— 那比完全没有令牌更危险，
因为模型会**信任**这些伪造标签。

因此令牌只保留在服务端，按 `container_key` 存查：沙盒回调时带回的 `container_key`
本来就是路由参数，服务端据此还原令牌，全程不经过容器。
"""

from typing import Dict, Optional

from nekro_agent.tools.untrusted_content import UntrustedToolResult, render_untrusted_result

# container_key -> one_time_code。同一频道的容器键是稳定的，新一轮运行会直接覆盖旧值
_session_tokens: Dict[str, str] = {}
# container_key -> 等着拼进本轮模型上下文的不可信结果。沙盒只拿到占位，正文留在这里
_held_results: Dict[str, UntrustedToolResult] = {}


def register_session_token(container_key: str, one_time_code: str) -> None:
    """登记本轮运行的会话令牌

    Args:
        container_key: 沙盒容器键，沙盒回调时会原样带回
        one_time_code: 本轮运行的一次性令牌
    """
    if container_key and one_time_code:
        _session_tokens[container_key] = one_time_code
        # 上一轮沙盒死在半路时结果没人取走，不能带到这一轮
        _held_results.pop(container_key, None)


def get_session_token(container_key: str) -> Optional[str]:
    """取出该容器对应的会话令牌

    Args:
        container_key: 沙盒容器键

    Returns:
        令牌；未登记（例如非 Agent 触发的调用）时返回 None
    """
    return _session_tokens.get(container_key) if container_key else None


def clear_session_token(container_key: str) -> None:
    """清除登记，避免映射随频道数单向增长"""
    _session_tokens.pop(container_key, None)


def hold_untrusted_result(container_key: str, result: UntrustedToolResult) -> None:
    """暂存沙盒方法的不可信结果，等 Agent 循环取走"""
    _held_results[container_key] = result


def attach_untrusted_result(sandbox_output: str, container_key: str, one_time_code: str) -> str:
    """把暂存的结果用本轮令牌包好，接在沙盒输出后面

    历史渲染有单条长度上限，这一轮要看完整内容只能走这里。只交付一次。
    """
    result = _held_results.pop(container_key, None)
    if result is None:
        return sandbox_output
    return f"{sandbox_output}\n{render_untrusted_result(one_time_code, result)}"
