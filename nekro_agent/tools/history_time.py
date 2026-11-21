"""聊天记录里时间的渲染与解析

聊天记录每条消息前面都带着 ``[月-日 时:分:秒 id:发送者账号]``（见
`DBChatMessage.parse_chat_history_prompt`）。模型要把某条消息交给宿主撤回时，能照抄的
只有这个时间；宿主要把它还原成 `send_timestamp` 才能回库里按秒找那条记录。

**渲染与解析必须共用同一个时区、同一个格式**：两边只要有一处不同，模型照抄下来的时间
就会多算或少算几个小时，撤回工具永远找不到目标。所以两件事都收在这个模块里，
`format_history_time` 与 `parse_history_time` 互为逆运算。

年份不在字符串里（历史里刻意不写年份，省 token），解析时只能在「今年、上一年」两个候选里找。
「消息不可能来自将来」能排除一部分：算出来比现在晚一分钟以上的候选不成立，这样 1 月里撤
12 月的消息也能对上。但今年这个日子已经过去时，两个候选都在过去、分不出来
（历史穿越插件能翻出一年前的消息），所以 `history_time_candidates` 把两个都交出去，
由调用方回库看哪一个真有消息。

`send_timestamp` 是 NA 入库时刻（`int(time.time())`），不是 QQ 那边的发送时间；渲染与解析用的
都是它，所以不需要、也不能去换算 QQ 时间。
"""

import datetime
import re
import time
from typing import List, Optional

HISTORY_TIME_FORMAT = "%m-%d %H:%M:%S"

# 只认历史渲染出来的那一形态：两位月、两位日、两位时分秒。先 strip 再要求整串匹配，
# 所以首尾空白可以容忍，中间夹了换行或任何别的字符都不算
_HISTORY_TIME_PATTERN = re.compile(r"[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")

# 算出来的时间比现在晚这么多秒以上，就当它是将来的时间，退回上一年再猜
_FUTURE_TOLERANCE_SECONDS = 60

# 解析时最多往前找几年：今年与上一年。更早的消息按这串字找不到，模型要改用 msg_id
_YEARS_BACK = 2


def history_timezone() -> Optional[datetime.tzinfo]:
    """渲染与解析共用的时区；None 表示进程本地时区（与改造前的 datetime.fromtimestamp 完全一致）

    测试用 monkeypatch 把它换成固定时区，以模拟「服务器时区与渲染时区不一致」。
    生产路径返回 None，因此渲染结果与改造前逐字相同。
    """
    return None


def format_history_time(timestamp: int) -> str:
    """聊天记录里显示的时间"""
    return datetime.datetime.fromtimestamp(timestamp, history_timezone()).strftime(HISTORY_TIME_FORMAT)


def history_time_candidates(text: str, now: Optional[float] = None) -> List[int]:
    """模型照抄的时间可能对应的全部 send_timestamp，新的在前；解析不了返回空表

    历史里不写年份，同一个「月-日 时:分:秒」今年和去年都可能有：今年这个日子已经过去时，
    两个候选都在过去，光凭这串字分不出是哪一年。所以把两个都交给调用方，由它回库看哪一个
    真有消息（撤回插件两年都有时拒绝，让模型改用 `msg_id`）。

    Args:
        text: 形如 ``11-09 10:00:00`` 的时间（历史里方括号内的那一串）
        now: 判定「将来」用的当前时刻；缺省取 `time.time()`。测试注入固定值用

    Returns:
        候选时间戳（最多两个：今年、去年）；格式不对、年份凑不出来、或都在将来时为空
    """
    raw = text.strip()
    if _HISTORY_TIME_PATTERN.fullmatch(raw) is None:
        return []

    current = time.time() if now is None else now
    tz = history_timezone()
    try:
        current_year = datetime.datetime.fromtimestamp(current, tz).year
    except (OverflowError, OSError, ValueError):
        # 极端时间戳（超出平台可表示范围）当作解析不了，别让插件的撤回调用因此炸掉
        return []

    candidates: List[int] = []
    # 今年、上一年。像非闰年的 2 月 29 日这种，strptime 直接抛 ValueError，跳过这一年
    for offset in range(_YEARS_BACK):
        year = current_year - offset
        try:
            naive = datetime.datetime.strptime(f"{year:04d}-{raw}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        # 时区为 None 时走朴素 datetime 的 .timestamp()，与回库的本地时间戳同一口径；
        # 固定时区时补上 tzinfo 再算，避免把渲染时区当成服务器本地时区
        timestamp = naive.timestamp() if tz is None else naive.replace(tzinfo=tz).timestamp()
        if timestamp > current + _FUTURE_TOLERANCE_SECONDS:
            # 将来：跨年（1 月里看到 12 月的消息）时今年这个候选不成立
            continue
        candidates.append(int(timestamp))
    return candidates


def parse_history_time(text: str, now: Optional[float] = None) -> Optional[int]:
    """把模型照抄的时间解析回最近的那个 send_timestamp；解析不了返回 None

    只取 `history_time_candidates` 的第一个（最近的）。要区分「今年还是去年」的调用方
    （撤回插件）直接用 `history_time_candidates`。
    """
    candidates = history_time_candidates(text, now)
    return candidates[0] if candidates else None
