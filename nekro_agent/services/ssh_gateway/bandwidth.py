"""网关两个方向的带宽令牌桶

## 为什么是令牌桶，不是"每 N 毫秒放行一批"

桥接层每次从一侧读到一块数据就想立刻写给另一侧。限速要能回答一个非阻塞的问题：
"现在这一块能不能走？不能的话还要等多久？" 令牌桶正好给出这个答案：`try_take` 拿得到
返回 0，拿不到返回需要等待的秒数，调用方据此暂停读、`call_later` 之后再来。这样既不
阻塞事件循环，也不丢数据——TCP 与 SSH 的窗口会把两头压住。

桶容量 = 1 秒的速率。速率从配置读，配置改了在下一个 `try_take` 生效；0 表示不限速，
每次立刻放行。两个方向（`up` / `down`）各一个桶，互不影响，与设计口径"两个方向分别算"
一致。

## 单块大于桶容量时

一个包最大 32 KB，默认速率 16 MiB/s，正常不会遇到。若配置把速率调得比单块还小，按
"桶满时放行这一块"处理：返回 0 并把桶清零。否则一次请求永远等不到足够令牌，转发会
永久停住。
"""

from __future__ import annotations

import time
from typing import Callable, Dict

from nekro_agent.core.config import config

_clock: Callable[[], float] = time.monotonic

DIRECTIONS = ("up", "down")


class _Bucket:
    """一个方向的令牌桶"""

    __slots__ = ("rate", "tokens", "updated_at")

    def __init__(self) -> None:
        self.rate = 0
        self.tokens = 0.0
        self.updated_at = 0.0

    def reset(self, rate: int, now: float) -> None:
        self.rate = rate
        self.tokens = float(rate)
        self.updated_at = now

    def refill(self, now: float) -> None:
        elapsed = max(0.0, now - self.updated_at)
        self.updated_at = now
        if self.rate <= 0:
            return
        self.tokens = min(float(self.rate), self.tokens + elapsed * float(self.rate))


_buckets: Dict[str, _Bucket] = {name: _Bucket() for name in DIRECTIONS}


def current_rate() -> int:
    """当前配置的速率（字节/秒）；负值按 0 处理"""
    try:
        return max(0, int(config.SSH_GATEWAY_MAX_BYTES_PER_SEC))
    except (TypeError, ValueError):
        return 0


def try_take(direction: str, n: int) -> float:
    """尝试从某个方向取 `n` 个字节的额度

    Returns:
        0 表示现在就能走；大于 0 表示还需要等多少秒。不阻塞、不消耗令牌（拿不到时）。
    """
    if direction not in _buckets:
        raise ValueError(f"未知的带宽方向: {direction}")
    amount = int(n)
    if amount <= 0:
        return 0.0

    rate = current_rate()
    if rate <= 0:
        return 0.0

    now = _clock()
    bucket = _buckets[direction]
    if bucket.rate != rate:
        # 配置变了：按新速率重置，避免用旧速率攒下的令牌多放行一批
        bucket.reset(rate, now)
    else:
        bucket.refill(now)

    if amount > bucket.rate:
        # 单块比一整秒的额度还大：桶满就放行这一块，否则等桶满
        if bucket.tokens >= float(bucket.rate):
            bucket.tokens = 0.0
            return 0.0
        return (float(bucket.rate) - bucket.tokens) / float(bucket.rate)

    if bucket.tokens >= float(amount):
        bucket.tokens -= float(amount)
        return 0.0
    return (float(amount) - bucket.tokens) / float(bucket.rate)


def reset_bandwidth() -> None:
    """清空两个桶（仅供测试）"""
    for name in DIRECTIONS:
        _buckets[name] = _Bucket()


__all__ = ["DIRECTIONS", "current_rate", "reset_bandwidth", "try_take"]
