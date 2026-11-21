"""隐私矩阵：三个跨频道开关的每个档位 × 每个方向，逐格断言。

矩阵的形状：

- 行：三个开关各自「关 / 相关 / 全部」；
- 列：私聊 → 群、群 → 群、群 → 私聊、私聊 → 别人的私聊、跨租户。

后两列在**任何**开关组合下都不通，因此额外跑 27 种组合。每格都用真实检索出口
`retrieve_memories`（SQLite / 真 PG + 本地 Qdrant）与 `evaluate` 交叉核对。
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from _memory_privacy_harness import (
    I1,
    SEARCH_LIMIT,
    U151,
    UXW,
    Harness,
    MemoryEnv,
    SwitchState,
)
from _memory_privacy_harness import memory_env as _shared_memory_env

memory_env = _shared_memory_env

MODES = ("default", "override")
SWITCHES = ("group_to_group", "group_to_private", "private_to_group")
LEVELS = ("off", "related", "all")


class World:
    def __init__(self) -> None:
        self.gA: Any = None
        self.gB: Any = None
        self.p151: Any = None
        self.pxw: Any = None
        self.g3: Any = None
        self.p3: Any = None
        self.m1: Any = None
        self.m3: Any = None


async def _seed_world(harness: Harness) -> World:
    world = World()
    world.gA = await harness.create_channel("gA", channel_type="group")
    world.gB = await harness.create_channel("gB", channel_type="group")
    world.p151 = await harness.create_channel("p151", channel_type="private")
    world.pxw = await harness.create_channel("pxw", channel_type="private")
    world.g3 = await harness.create_channel("g3", instance_key="i3", channel_type="group")
    world.p3 = await harness.create_channel("p3", instance_key="i3", channel_type="private")

    # 151 最近 5 天在群 B 发过言：G→G / P→G 的相关档因此成立。
    await harness.create_message("gB", account=U151, seconds_ago=5 * 86400, index=1)
    await harness.create_message("p151", account=U151, seconds_ago=3600, index=1, chat_type="private")
    await harness.create_message("pxw", account=UXW, seconds_ago=3600, index=1, chat_type="private")
    await harness.create_message("p3", account=U151, seconds_ago=3600, index=1, instance_key="i3", chat_type="private")

    world.m1 = await harness.create_paragraph(
        content="151 中午吃了麦当劳",
        origin_chat_key="gA",
        origin_chat_type="group",
        persons=(U151,),
    )
    world.m3 = await harness.create_paragraph(
        content="151 私聊里说晚上要加班",
        origin_chat_key="p151",
        origin_chat_type="private",
        persons=(U151,),
    )
    return world


def _state(**fields: str) -> SwitchState:
    return SwitchState(
        group_to_group=fields.get("group_to_group", "off"),
        group_to_private=fields.get("group_to_private", "off"),
        private_to_group=fields.get("private_to_group", "off"),
    )


def _configure(harness: Harness, mode: str, state: SwitchState) -> None:
    """按「全局默认」或「实例覆盖」布置生效值。

    覆盖模式刻意让全局与实例取不同值，这样才能证明生效值来自实例覆盖而不是全局：
    关档用「全局全部 + 实例关」，相关 / 全部分别用「全局关 + 实例相关 / 全部」。
    """
    harness.reset_overrides()
    if mode == "default":
        harness.set_global(state)
        return

    tested = {name: getattr(state, name) for name in SWITCHES if getattr(state, name) != "off"}
    global_state = _state()
    if not tested:
        # 关档：全局先放到最高档，再由实例覆盖压回关闭。
        global_state = _state(**{name: "all" for name in SWITCHES})
    harness.set_global(global_state)
    harness.set_instance(I1, state)


def _state_for(switch_name: str, level: str) -> SwitchState:
    return _state(**{switch_name: level})


class TestPrivacyMatrix:
    @pytest.mark.parametrize("mode", MODES)
    async def test_switch_levels_by_direction(self, memory_env: MemoryEnv, mode: str) -> None:
        harness = memory_env.harness
        world = await _seed_world(harness)

        for switch_name in SWITCHES:
            for level in LEVELS:
                state = _state_for(switch_name, level)
                _configure(harness, mode, state)

                # 私聊 → 群：由 P→G 控制。
                ids = await harness.retrieve_ids(world.gB)
                assert (world.m3.paragraph_id in ids) is (state.private_to_group in {"related", "all"})
                assert ids == await harness.expected_ids(world.gB)

                # 群 → 群：由 G→G 控制。
                ids = await harness.retrieve_ids(world.gB)
                assert (world.m1.paragraph_id in ids) is (state.group_to_group in {"related", "all"})
                assert ids == await harness.expected_ids(world.gB)

                # 群 → 私聊：由 G→P 控制。
                ids = await harness.retrieve_ids(world.p151)
                assert (world.m1.paragraph_id in ids) is (state.group_to_private in {"related", "all"})
                assert ids == await harness.expected_ids(world.p151)

                # 私聊 → 别人的私聊：任何档位都不通。
                ids = await harness.retrieve_ids(world.pxw)
                assert world.m3.paragraph_id not in ids
                assert ids == await harness.expected_ids(world.pxw)

                # 跨租户：任何档位都不通。
                for cross_channel in (world.g3, world.p3):
                    ids = await harness.retrieve_ids(cross_channel)
                    assert world.m1.paragraph_id not in ids
                    assert world.m3.paragraph_id not in ids
                    assert ids == await harness.expected_ids(cross_channel)

                # `memory_tools` 的主动搜索与自动检索必须给同一个范围。
                tool_ids = await harness.tool_search_ids(world.gB)
                assert tool_ids == await harness.retrieve_ids(world.gB)

    @pytest.mark.parametrize("mode", MODES)
    async def test_last_two_columns_never_open_under_any_combination(
        self,
        memory_env: MemoryEnv,
        mode: str,
    ) -> None:
        harness = memory_env.harness
        world = await _seed_world(harness)

        for levels in itertools.product(LEVELS, repeat=3):
            state = SwitchState(
                group_to_group=levels[0],
                group_to_private=levels[1],
                private_to_group=levels[2],
            )
            _configure(harness, mode, state)

            other_private = await harness.retrieve_ids(world.pxw)
            assert world.m3.paragraph_id not in other_private, f"私聊记忆进了别人的私聊: {levels}"

            for cross_channel in (world.g3, world.p3):
                cross = await harness.retrieve_ids(cross_channel)
                assert world.m1.paragraph_id not in cross, f"跨租户召回了群记忆: {levels}"
                assert world.m3.paragraph_id not in cross, f"跨租户召回了私聊记忆: {levels}"

            assert other_private == await harness.expected_ids(world.pxw)

    async def test_search_limit_is_large_enough(self) -> None:
        """矩阵里只有两条段落；防止以后加行时被默认条数截断成假通过。"""
        assert SEARCH_LIMIT >= 10
