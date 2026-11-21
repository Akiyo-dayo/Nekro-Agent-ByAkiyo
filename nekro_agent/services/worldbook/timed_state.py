"""定时效果：sticky / cooldown / delay 的纯函数判定与进程内状态

三者的单位都是**这个频道的消息条数**（不分谁发的），与酒馆一致。设计稿原本打算用扫描
窗口里的条数来数，但窗口最多 50 条而 `sticky` / `cooldown` / `delay` 最大 200，数不
出来。因此本模块只接受调用方算好的两个数：

- `msgs_since_anchor`：锚点那条消息之后又来了几条（锚点 = 上一次「非 sticky 状态下
  命中并注入」那一轮时窗口里最新消息的 `id`）；
- `channel_count`：这个频道一共能数到几条消息（运行时用一条只取 `id`、`limit 201`
  的查询算出，饱和在 201——比最大 200 大一，够判「不足 n 条」）。

怎么数消息不是纯函数的事，判据才是。这样 ``timed_gate`` 可以在没有任何数据库的情况下
被边界用例逐个钉住（第 n 条仍是 sticky、第 n+1 条进 cooldown、cooldown 结束后 free）。

## 只在结局为 ok 的轮次提交

暂存区（`stage` → `commit` / `discard`）就是为了这件事：一轮里可能渲染多次提示词，
每次都会算出候选锚点；只有本轮真正成功（结局 `ok`）才把锚点写进存储。被拦、失败、
取消的轮次 discard，不开始 sticky、也不进 cooldown——与召回冷却的提交时机一致。
不这样做的话，一次被拦的轮次会让一条条目进入 cooldown，下一轮明明该注入却注不进去，
而明细里只会显示「冷却中」，没有任何线索指向那次被拦。
"""

from __future__ import annotations

from enum import StrEnum

from .matcher import HitReason
from .types import MatchEntry

# 进程内状态的上限。定时状态是「丢一条最多让一条 sticky 提前结束」的量级，不值得为它
# 建表；但也不能无限长，4096 个频道条目对绝大多数部署都够，超了丢最早的一半。
MAX_KEYS = 4096
MAX_STAGED_ROUNDS = 4096


class TimedGate(StrEnum):
    """条目在当前时刻处于哪一档"""

    FREE = "free"
    STICKY = "sticky"
    COOLDOWN = "cooldown"


def timed_gate(
    *,
    anchor_message_id: int | None,
    msgs_since_anchor: int | None,
    sticky: int,
    cooldown: int,
) -> TimedGate:
    """按锚点与「过了几条」判断当前档位

    边界刻意写成闭区间：`sticky = n` 表示「接下来 n 条消息之内」仍然保持，所以第 n 条
    还是 sticky，第 n + 1 条才进 cooldown。区间之间不能重叠也不能留缝：
    `(sticky, sticky + cooldown]` 是 cooldown 段，之后才 free。

    没有锚点（或调用方数不出条数）一律 free：这是「从没生效过」的正常状态，不是异常。
    """
    if anchor_message_id is None or msgs_since_anchor is None:
        return TimedGate.FREE
    if sticky > 0 and msgs_since_anchor <= sticky:
        return TimedGate.STICKY
    if cooldown > 0 and sticky < msgs_since_anchor <= sticky + cooldown:
        return TimedGate.COOLDOWN
    return TimedGate.FREE


def entry_gate(
    entry: MatchEntry,
    *,
    anchor_message_id: int | None,
    msgs_since_anchor: int | None,
) -> TimedGate:
    """按条目算档位；运行时一律走这里，不直接调 :func:`timed_gate`

    常驻条目的 `sticky` 在这里按 0 算：它本来就每轮都在，sticky 对它没有意义（设计
    取舍是「忽略」）。若照原值算，锚点之后的前 `sticky` 条会落在 sticky 档，cooldown
    就被往后推了 `sticky` 条——等于让一个「无意义」的字段悄悄改变了冷却的起点。
    """
    return timed_gate(
        anchor_message_id=anchor_message_id,
        msgs_since_anchor=msgs_since_anchor,
        sticky=0 if entry.constant else entry.sticky,
        cooldown=entry.cooldown,
    )


def delay_satisfied(delay: int, channel_count: int) -> bool:
    """频道的消息条数是否够 `delay` 条

    `delay <= 0` 表示不用这个效果，恒满足。不足时条目**不命中**（不是「延迟到下一轮」）：
    酒馆的语义也是「消息太少时先不出场」。
    """
    return delay <= 0 or channel_count >= delay


def apply_timed(
    entry: MatchEntry,
    hit: HitReason | None,
    gate: TimedGate,
    channel_count: int,
) -> tuple[HitReason | None, bool]:
    """把定时效果叠加到关键词命中结果上

    Returns:
        （最终命中的原因, 本轮是否要设新锚点）。

    规则：

    - `delay` 不满足 → 不命中，也不设锚点；
    - `sticky` 档 → 非常驻条目命中（原因 `sticky`），**不**设新锚点：sticky 期间关键词
      再命中不刷新锚点，否则一条一直被提到的条目会永远 sticky 下去，cooldown 永远不开始；
    - `cooldown` 档 → 不命中；
    - `free` 档 → 只有原本命中才算命中；条目设了 sticky 或 cooldown 才需要新锚点；
    - 常驻条目的 `sticky` 被忽略（它本来就每轮都在），因此也不为它写 sticky 锚点；
      `cooldown` 与 `delay` 照样生效：常驻条目也会占预算，让它无限期占着上下文没有好处。
      档位应由 :func:`entry_gate` 算出（它对常驻条目按 sticky = 0 算）；万一传进来的
      仍是 sticky 档，常驻条目照常命中但**不**设新锚点——每轮都刷新锚点会让它永远
      进不了 cooldown。
    """
    if not delay_satisfied(entry.delay, channel_count):
        return None, False

    if gate is TimedGate.STICKY:
        if entry.constant:
            return hit, False
        return HitReason(kind="sticky"), False
    if gate is TimedGate.COOLDOWN:
        return None, False

    if hit is None:
        return None, False
    wants_anchor = entry.cooldown > 0 or (entry.sticky > 0 and not entry.constant)
    return hit, wants_anchor


class TimedStateStore:
    """（频道, 条目）到锚点消息 `id` 的进程内映射，带按轮次的暂存区

    暂存区与正式存储分开：一轮里渲染多次提示词，每次都 `stage` 一次（以最后一次为准），
    只有结局 `ok` 时才 `commit`。把「算出来的锚点」直接写进正式存储就没法回退了。
    """

    def __init__(self, *, max_keys: int = MAX_KEYS, max_rounds: int = MAX_STAGED_ROUNDS) -> None:
        self._max_keys = max_keys
        self._max_rounds = max_rounds
        self._state: dict[tuple[str, int], int] = {}
        self._staged: dict[str, dict[tuple[str, int], int]] = {}

    def get(self, chat_key: str, entry_id: int) -> int | None:
        """取锚点消息 `id`；没有就是 `None`"""
        return self._state.get((chat_key, entry_id))

    def stage(self, round_code: str, chat_key: str, entry_id: int, anchor_id: int) -> None:
        """暂存这一轮算出的锚点；同一轮同一（频道, 条目）以最后一次为准"""
        if round_code not in self._staged and len(self._staged) >= self._max_rounds:
            self._drop_oldest_rounds()
        self._staged.setdefault(round_code, {})[(chat_key, entry_id)] = anchor_id

    def commit(self, round_code: str) -> None:
        """把这一轮暂存的锚点写进正式存储

        写之前先按上限腾地方，而不是写完再检查：写完再删会把刚写进去的那条也删掉
        （它正是「最新」的那几条之一），表现为「提交了但下一轮查不到」。
        """
        staged = self._staged.pop(round_code, None)
        if not staged:
            return
        for key, anchor_id in staged.items():
            # 先删再插：字典按插入顺序记「新旧」，原地更新会让刚刷新的锚点仍排在最前，
            # 下一次腾地方时被当成最旧的那批丢掉。
            self._state.pop(key, None)
            if len(self._state) >= self._max_keys:
                self._drop_oldest_keys()
            self._state[key] = anchor_id

    def discard(self, round_code: str) -> None:
        """丢弃这一轮的暂存（被拦、失败、取消的轮次）"""
        self._staged.pop(round_code, None)

    def clear(self) -> None:
        """清空全部状态；给用例与「重启后重来」的显式路径用"""
        self._state.clear()
        self._staged.clear()

    def anchor_keys(self) -> list[tuple[str, int]]:
        """当前有锚点的（频道, 条目），按插入顺序；给用例看上限行为"""
        return list(self._state)

    def _drop_oldest_keys(self) -> None:
        for key in list(self._state)[: max(1, len(self._state) // 2)]:
            self._state.pop(key, None)

    def _drop_oldest_rounds(self) -> None:
        for round_code in list(self._staged)[: max(1, len(self._staged) // 2)]:
            self._staged.pop(round_code, None)


# 运行时单例。轮次收尾（结局确定之后）在 `finish` 里 commit / discard，本模块只有存储与用例。
timed_state_store = TimedStateStore()
