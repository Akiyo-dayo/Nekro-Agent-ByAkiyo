"""外部文本的安全回显

`tools.py`（读工具）、`writes.py`（写操作的目标标签）与 `receipt.py`（回执）都要把
宿主里的名字与标识拼进给人或模型看的文本。放在这里而不是留在 `tools.py`：
`writes` 与 `tools` 会互相引用，共用的包装留在一个两边都不依赖的模块里才不会有环。

名字与标识都来自平台或租户：名字可能带换行、不可见方向符与引号，标识可能被拿去做
下一次调用的参数。因此统一过 `echo_inline`，只是长度上限不同。
"""

from __future__ import annotations

from nekro_agent.tools.untrusted_content import echo_inline

# 结果里单个外部名字最多保留的字符数：够认出是哪一个，不够塞进一段指令
MAX_NAME_CHARS = 32
# 标识的上限：会话标识由适配器、实例标识、频道号拼成，常见长度在 30 到 60 之间
MAX_IDENT_CHARS = 128


def name(value: object) -> str:
    """外部名字 -> 可安全回显的单行短串"""
    return echo_inline(value, MAX_NAME_CHARS)


def ident(value: object) -> str:
    """标识（实例 / 适配器 / 会话标识）-> 可安全回显的单行串

    标识要能原样拿去调下一个工具，按名字的 32 字截断会把 `onebot_v11-xxx-group_xxx`
    这类会话标识截坏；但它们部分来自平台（群号）与租户（实例标识），照样要过 `echo_inline`。
    """
    return echo_inline(value, MAX_IDENT_CHARS)


__all__ = ["MAX_IDENT_CHARS", "MAX_NAME_CHARS", "ident", "name"]
