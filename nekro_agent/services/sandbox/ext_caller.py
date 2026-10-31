from pathlib import Path
from typing import List, Optional

from nekro_agent.core import config
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services.plugin.collector import plugin_collector

from .credential_guard import issue_sandbox_call_grant

CODE_PREAMBLE = """
from api_caller import *

_ck = FROM_CHAT_KEY
"""

METHOD_REG_TEMPLATE = """
@__extension_method_proxy
def {method_name}(*args, **kwargs):
    pass
"""  #! 沙盒环境下不需要使用异步方式调用，因为实际执行是通过 RPC 调用的

# 本地实现的方法本轮不在作用域时，把名字从模块命名空间里删掉。
# `globals().pop` 而不是 `del`：模板将来若不再定义该函数，`del` 会在导入期抛 NameError 把整个
# `api_caller` 弄坏，而少一个方法本不该是致命错误。
LOCAL_ONLY_REVOKE_TEMPLATE = """
globals().pop("{method_name}", None)
"""

# 该方法的实现完全在容器内本地完成（见 ext_caller_code.dynamic_importer），
# 不经过 RPC，因此既不生成代理，也不进入回调白名单
LOCAL_ONLY_METHOD_NAMES = {"dynamic_importer"}

# 下发进容器的 `api_caller.py` 模板。**必须按 `__file__` 定位**：
# 早先这里写的是相对路径 `Path("nekro_agent/services/sandbox/ext_caller_code.py")`，
# 依赖进程的当前工作目录，从仓库根之外启动 NA 就是 `FileNotFoundError`，
# 而这条异常会被 `message_service` 的兜底 except 吞成一行日志——表现是「Bot 不理人」
_EXT_CALLER_CODE_PATH = Path(__file__).resolve().with_name("ext_caller_code.py")

# 容器侧回调的读超时下限：沙盒运行超时被配得极小时也要留出足够的一次往返
_MIN_RPC_READ_TIMEOUT_SECONDS = 10

# 容器连不上 NA 的回调端点时打印的标记，与 `ext_caller_code.RPC_UNREACHABLE_TAG` 逐字相同
# （模板是被当作文本下发的，没法 import，所以两处各写一份，由单测钉住一致性）。
# 宿主侧靠它把「这次执行失败是因为回调链路不通」认出来，见 `runner.diagnose_rpc_bridge_failure`
RPC_UNREACHABLE_TAG = "[NA-RPC-UNREACHABLE]"


def resolve_rpc_read_timeout() -> int:
    """算出下发给容器的回调读超时

    取沙盒运行超时本身：容器本来就活不过这个时长，所以这个值不会让任何一次**本来能成功**的
    调用失败，它纯粹是防「连上了但对端不回」的兜底。真正解决「回调不通」的是容器侧写死的
    连接超时（见 `ext_caller_code.RPC_CONNECT_TIMEOUT_SECONDS`）。

    Returns:
        读超时秒数
    """
    return max(int(config.SANDBOX_RUNNING_TIMEOUT or 0), _MIN_RPC_READ_TIMEOUT_SECONDS)


async def get_api_caller_code(container_key: str, from_chat_key: str, ctx: Optional[AgentCtx] = None) -> str:
    """生成下发进沙盒容器的 `api_caller.py` 源码

    这里同时为本轮运行签发 RPC 调用许可：容器拿到的回调令牌是**这个容器专属**的随机值，
    而不是全局 `RPC_SECRET_KEY`；许可里记录的方法白名单，就是下面真正写进容器的那份清单。

    白名单从「实际下发」这一动作派生，而不是另外维护一张表——后续有人新增插件沙盒方法时，
    方法要么被下发（自然进入白名单），要么没被下发（也就不该能被调用），
    不存在「忘了更新白名单」导致意外开口的可能。

    下发范围由 `get_all_sandbox_methods(ctx)` 按 `会话 > 实例 > 全局` 解析，与 `run_agent`
    渲染提示词用的是同一个 `resolve_active_plugins`。**ctx 必须传**：缺了它这份清单会退化
    成「进程内启用的全部方法」，等于把别的实例开启的插件也交给这个容器。

    Args:
        container_key: 沙盒容器键
        from_chat_key: 会话标识
        ctx: Agent 上下文，用于按作用域解析本轮真正可见的方法

    Returns:
        `api_caller.py` 的完整源码
    """
    methods = await plugin_collector.get_all_sandbox_methods(ctx)
    exposed_method_names: List[str] = [
        method.func.__name__ for method in methods if method.func.__name__ not in LOCAL_ONLY_METHOD_NAMES
    ]
    rpc_token = issue_sandbox_call_grant(
        container_key=container_key,
        from_chat_key=from_chat_key,
        method_names=exposed_method_names,
    )

    base_code = (
        _EXT_CALLER_CODE_PATH.read_text(encoding="utf-8")
        .replace("{CHAT_API}", config.SANDBOX_CHAT_API_URL)
        .replace("{CONTAINER_KEY}", container_key)
        .replace("{FROM_CHAT_KEY}", from_chat_key)
        .replace("{RPC_SECRET_KEY}", rpc_token)
        .replace("{RPC_READ_TIMEOUT_SECONDS}", str(resolve_rpc_read_timeout()))
    )

    for method_name in exposed_method_names:
        base_code += METHOD_REG_TEMPLATE.format(method_name=method_name)

    # 本地实现的方法（`dynamic_importer`）函数体直接写在模板里，不受上面那份清单约束。
    # 不摘掉的话「关掉这个插件」在模型侧毫无效果——它照样能在沙盒里装包，
    # 语义与其它插件不一致（审计时实测：该插件在所有作用域都关闭，容器里仍然可用）。
    # 摘的方式是从模块命名空间里删名字，于是调用它得到的是 `NameError`，
    # 与其它「本轮不可见」的方法**表现完全一致**，模型不需要学第二种失败形态。
    resolved_method_names = {method.func.__name__ for method in methods}
    for local_only_name in sorted(LOCAL_ONLY_METHOD_NAMES - resolved_method_names):
        base_code += LOCAL_ONLY_REVOKE_TEMPLATE.format(method_name=local_only_name)
    return base_code.strip()
