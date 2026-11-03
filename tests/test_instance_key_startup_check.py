"""跨适配器重名实例标识的启动自检

数据库的唯一约束只到 `(adapter_key, instance_key)`，而人设、插件开关、记忆、协议端容器名
等数据只按 `instance_key` 存取。升级前留下的同名实例会共用这些数据；分属两个租户时就是
跨租户读写。这里钉住启动自检的三件事：只报告重名、只读、以及真的挂在启动钩子上。
"""

import ast
from pathlib import Path
from typing import List

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.services.console.instance_manager import warn_cross_adapter_instance_key_collisions

ONEBOT = "onebot_v11"
WECHAT = "wechat_ilink_multi"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def captured_warnings():
    """把本次用例里打出来的 WARNING 收进一个列表

    两个坑都踩过了，记在这里：

    1. `get_sub_logger` 回的是 **loguru** 的 logger，不走 stdlib 的 `logging`，
       `caplog` 一条都收不到 —— 用它写的第一版是恒绿的；
    2. `get_sub_logger` 每次调用回的是 `logger.bind(...)` 产生的**新对象**，
       所以 monkeypatch 掉 `nekro_agent.core.logger.logger.warning` 拦不住在函数体内
       现取 logger 的调用方。

    因此这里挂一个真正的 loguru sink：不替换任何被测代码，只在日志的出口接一根线，
    用例收尾时摘掉。
    """
    from nekro_agent.core.logger import logger

    captured: List[str] = []
    sink_id = logger.add(lambda message: captured.append(str(message)), level="WARNING", format="{message}")
    try:
        yield captured
    finally:
        logger.remove(sink_id)


class TestCollisionsAreReported:
    async def test_one_warning_per_colliding_instance_key(self, db, captured_warnings: List[str]) -> None:  # noqa: ARG002
        await DBAdapterInstance.create(adapter_key=ONEBOT, instance_key="inst1", provider=ONEBOT)
        # 软删墓碑也要算：它仍然占着这个标识，数据仍然按标识存取
        await DBAdapterInstance.create(adapter_key=WECHAT, instance_key="inst1", status="deleted", provider="ilink")
        await DBAdapterInstance.create(adapter_key=ONEBOT, instance_key="solo", provider=ONEBOT)
        await DBAdapterInstance.create(adapter_key=WECHAT, instance_key="solo2", provider="ilink")
        # 空标识是单实例适配器的常态，不能算重名
        await DBAdapterInstance.create(adapter_key=ONEBOT, instance_key="", provider=ONEBOT)
        await DBAdapterInstance.create(adapter_key="email", instance_key="", provider="email")

        collisions = await warn_cross_adapter_instance_key_collisions()

        assert collisions == {"inst1": [ONEBOT, WECHAT]}
        assert len(captured_warnings) == 1
        assert "inst1" in captured_warnings[0]
        assert ONEBOT in captured_warnings[0]
        assert WECHAT in captured_warnings[0]

    async def test_the_check_does_not_touch_any_row(self, db) -> None:  # noqa: ARG002
        await DBAdapterInstance.create(adapter_key=ONEBOT, instance_key="inst1", provider=ONEBOT, status="online")
        await DBAdapterInstance.create(adapter_key=WECHAT, instance_key="inst1", provider="ilink", status="offline")

        before = await DBAdapterInstance.all().order_by("id").values()
        await warn_cross_adapter_instance_key_collisions()
        after = await DBAdapterInstance.all().order_by("id").values()

        assert after == before

    async def test_no_collision_is_silent(self, db, captured_warnings: List[str]) -> None:  # noqa: ARG002
        await DBAdapterInstance.create(adapter_key=ONEBOT, instance_key="solo", provider=ONEBOT)
        await DBAdapterInstance.create(adapter_key=WECHAT, instance_key="solo2", provider="ilink")
        await DBAdapterInstance.create(adapter_key=ONEBOT, instance_key="", provider=ONEBOT)
        await DBAdapterInstance.create(adapter_key="email", instance_key="", provider="email")

        collisions = await warn_cross_adapter_instance_key_collisions()

        assert collisions == {}
        assert captured_warnings == []


def _await_calls(tree: ast.AST, name: str) -> List[ast.Await]:
    """找出对某个名字的 await 调用"""
    calls: List[ast.Await] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if (isinstance(func, ast.Name) and func.id == name) or (
            isinstance(func, ast.Attribute) and func.attr == name
        ):
            calls.append(node)
    return calls


class TestStartupHookIsWired:
    def test_hook_runs_inside_a_try_after_migrations_and_before_sandbox_cleanup(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "nekro_agent" / "__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        hook_calls = _await_calls(tree, "warn_cross_adapter_instance_key_collisions")
        assert len(hook_calls) == 1
        hook = hook_calls[0]
        hook_position = (hook.lineno, hook.col_offset)

        migrated = _await_calls(tree, "run_db_migrations")
        assert migrated
        assert hook_position > max((node.lineno, node.col_offset) for node in migrated)

        cleanup_position = min(
            (node.lineno, node.col_offset)
            for node in ast.walk(tree)
            if (isinstance(node, ast.Name) and node.id == "cleanup_sandbox_containers")
            or (isinstance(node, ast.Attribute) and node.attr == "cleanup_sandbox_containers")
        )
        assert hook_position < cleanup_position

        try_bodies = [
            statement
            for try_node in ast.walk(tree)
            if isinstance(try_node, ast.Try)
            for statement in try_node.body
        ]
        assert any(hook is inner for statement in try_bodies for inner in ast.walk(statement))
