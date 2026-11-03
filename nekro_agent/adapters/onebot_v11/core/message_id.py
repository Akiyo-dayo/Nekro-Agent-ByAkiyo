"""OneBot 消息编号的解析规则

NapCat、SnowLuma 的 `message_id` 是有符号 int32 哈希，约一半是负数，不能只认非负纯数字。

只收规范写法：记录里按 `str(message_id)` 存、按字符串查，非规范写法（`"007"`、`"-0"`、`"+5"`、
带空白的）转成整数后再写回去是另一个字符串，查不到同一条记录。

上限放到有符号 64 位而不是 int32：别的实现（如 qbob）不保证编号落在 int32 内。
"""

import re
from typing import Optional

_MIN_MESSAGE_ID = -(2**63)
_MAX_MESSAGE_ID = 2**63 - 1
_CANONICAL_ID = re.compile(r"-?[0-9]{1,19}")


def parse_message_id(raw: object) -> Optional[int]:
    """把消息编号的任意输入解析成整数；不是规范编号时返回 None

    `bool` 要单独挡掉：Python 里 `True` 也是 `int`。任何输入都不抛异常。
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if _MIN_MESSAGE_ID <= raw <= _MAX_MESSAGE_ID else None
    if not isinstance(raw, str):
        return None
    if not raw.isascii() or _CANONICAL_ID.fullmatch(raw) is None:
        return None
    try:
        value = int(raw)
    except ValueError:  # 正则已经限定了写法，这里只是兜底
        return None
    if not _MIN_MESSAGE_ID <= value <= _MAX_MESSAGE_ID:
        return None
    # 只收规范写法：落库与查库都按 `str(message_id)` 的字符串形式
    return value if str(value) == raw else None
