"""文本清洗：剔除 NUL（`\\x00`）

PostgreSQL 的 text / varchar / jsonb 都不收 NUL，asyncpg 会抛
`CharacterNotInRepertoireError: invalid byte sequence for encoding "UTF8": 0x00`，
一条带 NUL 的消息会让整条写库失败、整条消息丢掉。SQLite 收 NUL，所以默认跑 SQLite
的用例发现不了这类数据。协议端的群名片、昵称、正文都由发送方任意构造，是最容易
带进 NUL 的地方。

只剔除 NUL，其他字符一个都不动：`\\t`、`\\n`、`\\x01`、emoji、看起来像乱码的
`ǿĀ` 都是用户数据，改它们等于篡改内容，也会让「库里存的」与「广播出去的」不一致。

刻意不 import tortoise / 配置模块：落库监听器（`models/nul_guard.py`）与各服务层都
要用它，保持成一个无副作用、可轻量导入的纯函数模块。
"""

from typing import TypeVar

T = TypeVar("T")

# 单独提出来，避免各处重复写字面量与转义形式
_NUL = "\x00"


def strip_nul(value: T) -> T:
    """递归剔除字符串里的 NUL，其余内容逐字不变

    - `str`：去掉所有 `\\x00`
    - `dict`：新 dict，**键和值**都递归处理（NUL 出现在键上同样会让 jsonb 写库失败）
    - `list` / `tuple`：同类型的新容器，元素递归处理
    - 其他类型（int、None、bytes、pydantic 对象等）原样返回：bytes 不是文本列的内容，
      而且 NUL 在二进制里是合法字节，动它反而会破坏数据

    返回新容器而不是就地修改，是因为入参往往同时被广播、插件回调等其他路径持有，
    就地改会让它们看到的那份数据被悄悄换掉。
    """
    if isinstance(value, str):
        return value.replace(_NUL, "")  # type: ignore[return-value]
    if isinstance(value, dict):
        return {strip_nul(key): strip_nul(item) for key, item in value.items()}  # type: ignore[return-value]
    if isinstance(value, list):
        return [strip_nul(item) for item in value]  # type: ignore[return-value]
    if isinstance(value, tuple):
        return tuple(strip_nul(item) for item in value)  # type: ignore[return-value]
    return value
