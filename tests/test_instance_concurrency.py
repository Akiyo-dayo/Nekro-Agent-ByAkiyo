"""实例级并发隔离与配额

改造前沙盒执行由一个全局 `asyncio.Semaphore` 把守，等待者是一条**全局 FIFO 队列**。
本文件钉死换成公平调度器之后的四条契约：

1. **公平轮转**：一个实例先堆了 N 个等待者，后来的实例仍然最多等「有排队的实例数」次派发
   就轮到，而不是排在那 N 个之后。这是"配额"解决不了、必须靠调度顺序解决的那一半；
2. **每实例并发上限**：一个实例最多同时占用配额允许的名额数，剩余容量立刻可供其他实例使用；
   上限为 0 时不额外限制，单实例部署的吞吐与改造前一致；
3. **配额耗尽是明确拒绝，不是静默丢弃**：排队超限与等待超时都抛
   `InstanceConcurrencyRejected` 并计入统计，调用链据此给出用户可见提示；
4. **名额不会漏**：等待者被取消后那一格必须还能派给别人。

排队顺序全部靠"手工驱动 release + `asyncio.sleep(0)` 让出一轮"来观察，不比较耗时、
不靠 sleep 竞速，结果是确定的。
"""

import asyncio
from typing import List

import pytest

from nekro_agent.core.config import config
from nekro_agent.services.instance_concurrency import (
    REJECT_REASON_QUEUE_FULL,
    REJECT_REASON_TIMEOUT,
    SHARED_INSTANCE_KEY,
    InstanceConcurrencyRejected,
    InstanceLimits,
    instance_key_of,
    resolve_instance_limits,
    scheduler,
)

INST_A = "inst_a"
INST_B = "inst_b"


@pytest.fixture(autouse=True)
def clean_scheduler():
    """每个用例都从干净的调度状态开始"""
    scheduler.reset_all()
    yield
    scheduler.reset_all()


def limits(*, max_concurrent: int = 0, queue_limit: int = 0, wait_timeout: float = 0.0) -> InstanceLimits:
    return InstanceLimits(max_concurrent=max_concurrent, queue_limit=queue_limit, wait_timeout=wait_timeout)


async def _hold(instance_key: str, instance_limits: InstanceLimits, order: List[str], label: str) -> None:
    """拿到名额后记一笔，然后一直持有到任务被取消

    名额由用例自己调 `scheduler.release()` 归还，这样"什么时候释放"完全由用例决定，
    派发顺序因此可以逐步观察。
    """
    await scheduler.acquire(instance_key, instance_limits)
    order.append(label)
    await asyncio.Event().wait()


async def _let_all_enqueue() -> None:
    """让所有已创建的任务各自跑到第一个挂起点

    `acquire` 的入队是同步发生在第一个 await 之前的，任务又按创建顺序启动，
    因此一次让步之后入队顺序就等于创建顺序。
    """
    await asyncio.sleep(0)


async def _settle() -> None:
    """让被唤醒的等待者跑到下一个挂起点"""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class TestFairRotation:
    """公平轮转：突发实例不能把后来者顶到队尾"""

    async def test_late_instance_is_not_starved_by_a_burst(self, monkeypatch: pytest.MonkeyPatch):
        """A 先堆 5 个、B 随后来 1 个时，B 排在第 3 位而不是第 6 位

        全局 FIFO 信号量下的顺序是 a1,a2,a3,a4,a5,b1——B 要等 A 的 5 次执行全部跑完。
        轮转之后是 a1,a2,b1,...：A 被放行一次就排到轮转队尾，B 的队首请求随即轮到。
        """
        monkeypatch.setattr(config, "SANDBOX_MAX_CONCURRENT", 1)
        order: List[str] = []
        tasks = [asyncio.create_task(_hold(INST_A, limits(), order, f"a{i}")) for i in range(1, 6)]
        tasks.append(asyncio.create_task(_hold(INST_B, limits(), order, "b1")))
        await _let_all_enqueue()

        assert order == ["a1"], "容量为 1，只有第一个请求能立刻拿到名额"

        scheduler.release(INST_A)
        await _settle()
        assert order == ["a1", "a2"]

        scheduler.release(INST_A)
        await _settle()
        assert order == ["a1", "a2", "b1"], "轮到 B 了，而不是继续放行 A 堆着的 a3"

        scheduler.release(INST_B)
        await _settle()
        assert order == ["a1", "a2", "b1", "a3"]

        for task in tasks:
            task.cancel()

    async def test_rotation_alternates_between_two_backlogged_instances(self, monkeypatch: pytest.MonkeyPatch):
        """两个实例都有积压时严格交替，任何一方都不能连续独占"""
        monkeypatch.setattr(config, "SANDBOX_MAX_CONCURRENT", 1)
        order: List[str] = []
        tasks = [asyncio.create_task(_hold(INST_A, limits(), order, f"a{i}")) for i in range(1, 4)]
        tasks.extend(asyncio.create_task(_hold(INST_B, limits(), order, f"b{i}")) for i in range(1, 4))
        await _let_all_enqueue()

        holder = INST_A
        for _ in range(4):
            scheduler.release(holder)
            await _settle()
            holder = INST_A if order[-1].startswith("a") else INST_B

        assert order == ["a1", "a2", "b1", "a3", "b2"]

        for task in tasks:
            task.cancel()


class TestInstanceQuota:
    """每实例并发上限：收窄爆炸半径，但不牺牲单实例部署的吞吐"""

    async def test_cap_bounds_one_instance_and_leaves_room_for_others(self, monkeypatch: pytest.MonkeyPatch):
        """A 上限 2 时最多占 2 格，剩下的 2 格立刻归 B，即使 B 的请求是最后到的"""
        monkeypatch.setattr(config, "SANDBOX_MAX_CONCURRENT", 4)
        capped = limits(max_concurrent=2)
        order: List[str] = []
        tasks = [asyncio.create_task(_hold(INST_A, capped, order, f"a{i}")) for i in range(1, 5)]
        tasks.extend(asyncio.create_task(_hold(INST_B, capped, order, f"b{i}")) for i in range(1, 3))
        await _let_all_enqueue()

        assert order == ["a1", "a2", "b1", "b2"]
        stats = scheduler.stats()
        assert stats[INST_A].active == 2, "上限 2 生效，A 不能占满全局 4 格"
        assert stats[INST_B].active == 2
        assert stats[INST_A].queued == 2

        for task in tasks:
            task.cancel()

    async def test_zero_cap_keeps_single_instance_throughput(self, monkeypatch: pytest.MonkeyPatch):
        """上限 0 表示不额外限制：只有一个实例在跑时它仍能用满全局容量"""
        monkeypatch.setattr(config, "SANDBOX_MAX_CONCURRENT", 4)
        order: List[str] = []
        tasks = [asyncio.create_task(_hold(INST_A, limits(), order, f"a{i}")) for i in range(1, 5)]
        await _let_all_enqueue()

        assert order == ["a1", "a2", "a3", "a4"]
        assert scheduler.stats()[INST_A].active == 4

        for task in tasks:
            task.cancel()


class TestRejection:
    """配额耗尽必须是明确结论，不能变成静默丢消息"""

    async def test_queue_limit_rejects_instead_of_piling_up(self, monkeypatch: pytest.MonkeyPatch):
        """排队超过上限时抛异常并计数，而不是无限堆积"""
        monkeypatch.setattr(config, "SANDBOX_MAX_CONCURRENT", 1)
        bounded = limits(queue_limit=2)
        order: List[str] = []
        tasks = [asyncio.create_task(_hold(INST_A, bounded, order, f"a{i}")) for i in range(1, 4)]
        await _let_all_enqueue()

        with pytest.raises(InstanceConcurrencyRejected) as excinfo:
            await scheduler.acquire(INST_A, bounded)

        assert excinfo.value.reason == REJECT_REASON_QUEUE_FULL
        assert excinfo.value.instance_key == INST_A
        stats = scheduler.stats()[INST_A]
        assert stats.rejected_queue_full == 1
        assert stats.queued == 2, "被拒的那次不会挤进队列"

        for task in tasks:
            task.cancel()

    async def test_wait_timeout_rejects_with_timeout_reason(self, monkeypatch: pytest.MonkeyPatch):
        """名额一直不释放时，等待超时按拒绝处理

        结论是确定的：唯一的名额被一个不归还的持有者占着，排队者除了超时没有别的出路。
        """
        monkeypatch.setattr(config, "SANDBOX_MAX_CONCURRENT", 1)
        await scheduler.acquire(INST_A, limits())

        with pytest.raises(InstanceConcurrencyRejected) as excinfo:
            await scheduler.acquire(INST_A, limits(wait_timeout=0.05))

        assert excinfo.value.reason == REJECT_REASON_TIMEOUT
        stats = scheduler.stats()[INST_A]
        assert stats.rejected_timeout == 1
        assert stats.queued == 0, "超时的等待者必须从队列里摘掉"
        assert stats.active == 1, "超时不影响已经在跑的那一个"

    async def test_rejection_notice_is_rate_limited_per_instance(self, monkeypatch: pytest.MonkeyPatch):
        """拒绝提示按实例做冷却，过载时不会把提示本身刷成新的洪水"""
        monkeypatch.setattr(config, "SANDBOX_INSTANCE_REJECT_NOTICE_COOLDOWN_SECONDS", 300.0)

        assert scheduler.should_notify_rejection(INST_A, now=1000.0) is True
        assert scheduler.should_notify_rejection(INST_A, now=1200.0) is False
        assert scheduler.should_notify_rejection(INST_B, now=1200.0) is True, "冷却是按实例算的"
        assert scheduler.should_notify_rejection(INST_A, now=1301.0) is True


class TestSlotAccounting:
    """名额账必须是平的：既不能漏，也不能凭空多出来"""

    async def test_cancelled_waiter_does_not_swallow_a_slot(self, monkeypatch: pytest.MonkeyPatch):
        """排队者被取消后，释放出来的名额要能派给下一个实例"""
        monkeypatch.setattr(config, "SANDBOX_MAX_CONCURRENT", 1)
        order: List[str] = []
        holder = asyncio.create_task(_hold(INST_A, limits(), order, "a1"))
        doomed = asyncio.create_task(_hold(INST_A, limits(), order, "a2"))
        latecomer = asyncio.create_task(_hold(INST_B, limits(), order, "b1"))
        await _let_all_enqueue()

        doomed.cancel()
        await _settle()

        scheduler.release(INST_A)
        await _settle()
        assert order == ["a1", "b1"], "被取消的 a2 不能吃掉这一格"
        assert scheduler.stats()[INST_B].active == 1

        holder.cancel()
        latecomer.cancel()

    async def test_release_without_holding_is_ignored(self):
        """没拿到名额就调 release 不能把全局计数减成负数"""
        scheduler.release(INST_A)
        assert scheduler.capacity_snapshot()["active"] == 0


class TestInstanceKeyBucket:
    """会话标识到并发桶的归并"""

    @pytest.mark.parametrize(
        ("chat_key", "expected"),
        [
            ("onebot_v11-inst1-group_10001", "inst1"),
            ("onebot_v11-ob1000000003-private_42", "ob1000000003"),
            ("group_10001", SHARED_INSTANCE_KEY),
            ("", SHARED_INSTANCE_KEY),
            ("onebot_v11-..-group_1", SHARED_INSTANCE_KEY),
        ],
    )
    def test_instance_key_of(self, chat_key: str, expected: str):
        assert instance_key_of(chat_key) == expected


class TestLimitResolution:
    """配额走四层配置，且改配置后不需要重启"""

    @pytest.fixture(autouse=True)
    def clean_override_cache(self):
        """清掉覆盖配置的全局缓存

        `UnifiedConfigService._get_config_instance` 命中 `ConfigManager` 就直接返回，
        不会再看文件。部署环境里 `data/` 下本来就可能存着实例覆盖文件，一旦被前一个用例
        读进缓存，后面把 `DATA_DIR` 指到临时目录也读不到新写的覆盖——本地空目录看不出来，
        服务器上必然失败。
        """
        from nekro_agent.core.core_utils import ConfigManager
        from nekro_agent.services.config_resolver import config_resolver
        from nekro_agent.services.config_service import instance_override_config_key

        keys = [instance_override_config_key("onebot_v11", name) for name in ("inst1", "inst2")]
        keys.append("adapter_override_onebot_v11")
        for key in keys:
            ConfigManager.unregister_config(key)
        config_resolver.bump_version()
        yield
        for key in keys:
            ConfigManager.unregister_config(key)
        config_resolver.bump_version()

    def test_reads_global_layer_and_refreshes_on_config_bump(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        """无覆盖时取全局值；配置版本号自增后缓存失效，新值立刻生效"""
        from nekro_agent.core.os_env import OsEnv
        from nekro_agent.services.config_resolver import config_resolver

        # 指到空目录，保证读到的确实是全局层，而不是部署环境里已有的某份覆盖文件
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path))
        monkeypatch.setattr(config, "SANDBOX_INSTANCE_MAX_CONCURRENT", 3)
        monkeypatch.setattr(config, "SANDBOX_INSTANCE_QUEUE_LIMIT", 7)
        monkeypatch.setattr(config, "SANDBOX_INSTANCE_QUEUE_TIMEOUT_SECONDS", 12.0)
        config_resolver.bump_version()

        resolved = resolve_instance_limits("onebot_v11", "inst1")
        assert resolved == InstanceLimits(max_concurrent=3, queue_limit=7, wait_timeout=12.0)

        monkeypatch.setattr(config, "SANDBOX_INSTANCE_MAX_CONCURRENT", 5)
        assert resolve_instance_limits("onebot_v11", "inst1").max_concurrent == 3, "同一版本号内走缓存"

        config_resolver.bump_version()
        assert resolve_instance_limits("onebot_v11", "inst1").max_concurrent == 5

    def test_instance_layer_overrides_global(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        """实例层覆盖优先于全局层，且只影响写了覆盖的那个实例"""
        from nekro_agent.core.os_env import OsEnv
        from nekro_agent.services.config_resolver import config_resolver
        from nekro_agent.services.config_service import instance_override_config_path

        monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path))
        monkeypatch.setattr(config, "SANDBOX_INSTANCE_MAX_CONCURRENT", 1)

        override_path = instance_override_config_path("onebot_v11", "inst1")
        override_path.parent.mkdir(parents=True, exist_ok=True)
        override_path.write_text(
            "enable_SANDBOX_INSTANCE_MAX_CONCURRENT: true\nSANDBOX_INSTANCE_MAX_CONCURRENT: 9\n",
            encoding="utf-8",
        )
        config_resolver.bump_version()

        assert resolve_instance_limits("onebot_v11", "inst1").max_concurrent == 9
        assert resolve_instance_limits("onebot_v11", "inst2").max_concurrent == 1, "没写覆盖的实例不受影响"


async def test_scheduler_snapshot_reports_global_pressure(monkeypatch: pytest.MonkeyPatch):
    """总后台读到的全局容量/占用/排队要和调度器的真实状态一致"""
    monkeypatch.setattr(config, "SANDBOX_MAX_CONCURRENT", 2)
    order: List[str] = []
    tasks: List[asyncio.Task] = [asyncio.create_task(_hold(INST_A, limits(), order, f"a{i}")) for i in range(1, 4)]
    tasks.append(asyncio.create_task(_hold(INST_B, limits(), order, "b1")))
    await _let_all_enqueue()

    assert scheduler.capacity_snapshot() == {"capacity": 2, "active": 2, "queued": 2}

    for task in tasks:
        task.cancel()
