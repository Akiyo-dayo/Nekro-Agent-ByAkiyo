"""清理协议端上报的展示名（昵称、群名片、群名）

QQ 的「彩色 / 特效群名片」在协议端那侧不是普通字符串：协议端会把名片的原始字节
（protobuf 编码）直接当成 ``card`` 字符串上报，里面夹着 0x0e、0x12、0x0a、0x09、0x07、
0x1a、0x10、0x11 这类控制字符，结尾固定是一个 NUL（0x00）。另一个协议端（NapCat）
遇到同样的名片时会把里面的文字片段拼成一段正常文字再上报。

实测把前一种上报的 ``card`` 里 U+0000–U+001F 与 U+007F 全部去掉，得到的字符串与后一种
完全一致。所以不解析 protobuf，只在 OneBot 入口把这些控制字符去掉。

只去掉上面这两段码位，其他字符一律保留：``$``、``"``、``ǿ``、``Ā`` 都是名片文字里正常
出现的字符，去掉会改变当事人看到的名字。末尾再 ``strip()`` 一次首尾空白——控制字符被去掉
之后，原本夹在中间的空格很容易落到首尾。

这里的清理是**入口级**的：入库前的 NUL 兜底（:mod:`nekro_agent.models.nul_guard`）只去掉 NUL，
保的是「写库不失败」；本模块去掉全部控制字符，保的是「落库、进提示词、写日志的名字本身就是干净的」，
并且和 NapCat 上报的名字一致。
"""

import re
from typing import Optional

# U+0000–U+001F 与 U+007F。用转义写，避免源码里出现真实控制字符
_CONTROL_CHARS_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


def clean_display_name(value: Optional[str]) -> str:
    """去掉展示名里的 C0 控制字符与 DEL，再去掉首尾空白

    Args:
        value: 协议端上报的昵称 / 群名片 / 群名；可以是 ``None``

    Returns:
        清理后的名字。``None`` 与非字符串按空串处理；全是控制字符时同样得到 ``""``，
        调用方可以据此走 ``or`` 回退链
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        return ""
    return _CONTROL_CHARS_PATTERN.sub("", value).strip()
