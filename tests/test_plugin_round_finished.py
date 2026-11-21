"""插件的回合收尾回调

每轮对话收尾时按结局（ok / blocked / failed / cancelled）回调一次；只排后台任务，不等结果；
插件抛错或卡住不影响那一轮，也不拖慢别的插件；只对在这个频道生效的插件调用。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from nekro_agent.core.config import config
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services.agent import run_agent as run_agent_module
from nekro_agent.services.agent.model_capability import LLMFailureKind
from nekro_agent.services.plugin import collector as collector_module
from nekro_agent.services.plugin.base import NekroPlugin
from nekro_agent.services.plugin.collector import plugin_collector

CHAT_KEY = "onebot_v11-ob31001-group_31"


def _plugin(module_name: str) -> NekroPlugin:
    return NekroPlugin(
        name=f"探针 {module_name}",
        module_name=module_name,
        description="回合收尾用例",
        version="0.1.0",
        author="Probe",
        url="",
    )


async def _drain() -> None:
    while collector_module._ROUND_FINISHED_TASKS:  # noqa: SLF001
        await asyncio.gather(*list(collector_module._ROUND_FINISHED_TASKS), return_exceptions=True)  # noqa: SLF001


@pytest.fixture
def plugins(monkeypatch: pytest.MonkeyPatch) -> dict[str, NekroPlugin]:
    loaded: dict[str, NekroPlugin] = {}
    monkeypatch.setattr(plugin_collector, "loaded_plugins", loaded)

    async def _instance(_chat_key: str) -> str:
        return "ob31001"

    monkeypatch.setattr(collector_module, "resolve_chat_plugin_instance", _instance)
    monkeypatch.setattr(collector_module, "plugin_effective_with_instance", lambda key, _i, _c: not key.endswith("off"))
    return loaded


class TestScheduleRoundFinished:
    async def test_each_effective_plugin_gets_the_outcome(self, plugins: dict[str, NekroPlugin]) -> None:
        seen: list[tuple[str, str, str]] = []
        for name in ("p_one", "p_two"):
            plugin = _plugin(name)

            @plugin.mount_on_round_finished()
            async def _done(ctx: AgentCtx, outcome: str, _name: str = name) -> None:
                seen.append((_name, ctx.from_chat_key or "", outcome))

            plugins[plugin.key] = plugin

        plugin_collector.schedule_round_finished(AgentCtx(from_chat_key=CHAT_KEY), "ok")
        await _drain()

        assert sorted(seen) == [("p_one", CHAT_KEY, "ok"), ("p_two", CHAT_KEY, "ok")]

    async def test_returns_before_the_callback_runs(self, plugins: dict[str, NekroPlugin]) -> None:
        release = asyncio.Event()
        finished: list[str] = []
        plugin = _plugin("p_slow")

        @plugin.mount_on_round_finished()
        async def _done(_ctx: AgentCtx, outcome: str) -> None:
            await release.wait()
            finished.append(outcome)

        plugins[plugin.key] = plugin

        plugin_collector.schedule_round_finished(AgentCtx(from_chat_key=CHAT_KEY), "blocked")
        assert finished == []
        release.set()
        await _drain()

        assert finished == ["blocked"]

    async def test_plugin_off_in_this_channel_is_skipped(self, plugins: dict[str, NekroPlugin]) -> None:
        seen: list[str] = []
        plugin = _plugin("p_off")

        @plugin.mount_on_round_finished()
        async def _done(_ctx: AgentCtx, outcome: str) -> None:
            seen.append(outcome)

        plugins[plugin.key] = plugin

        plugin_collector.schedule_round_finished(AgentCtx(from_chat_key=CHAT_KEY), "ok")
        await _drain()

        assert seen == []

    async def test_raising_or_hanging_plugin_does_not_stop_the_others(
        self,
        plugins: dict[str, NekroPlugin],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "PLUGIN_CALL_TIMEOUT_SECONDS", 0.2)
        seen: list[str] = []

        broken = _plugin("p_broken")

        @broken.mount_on_round_finished()
        async def _broken(_ctx: AgentCtx, _outcome: str) -> None:
            raise RuntimeError("插件自己的错")

        hanging = _plugin("p_hanging")

        @hanging.mount_on_round_finished()
        async def _hanging(_ctx: AgentCtx, _outcome: str) -> None:
            await asyncio.sleep(30)

        good = _plugin("p_good")

        @good.mount_on_round_finished()
        async def _good(_ctx: AgentCtx, outcome: str) -> None:
            seen.append(outcome)

        for plugin in (broken, hanging, good):
            plugins[plugin.key] = plugin

        plugin_collector.schedule_round_finished(AgentCtx(from_chat_key=CHAT_KEY), "failed")
        await asyncio.wait_for(_drain(), timeout=5)

        assert seen == ["failed"]

    async def test_no_hooked_plugin_creates_no_task(self, plugins: dict[str, NekroPlugin]) -> None:
        plugins["Probe.p_plain"] = _plugin("p_plain")

        plugin_collector.schedule_round_finished(AgentCtx(from_chat_key=CHAT_KEY), "ok")

        assert not collector_module._ROUND_FINISHED_TASKS  # noqa: SLF001

    async def test_bad_ctx_is_swallowed(self, plugins: dict[str, NekroPlugin]) -> None:
        plugin = _plugin("p_any")

        @plugin.mount_on_round_finished()
        async def _done(_ctx: AgentCtx, _outcome: str) -> None:
            return None

        plugins[plugin.key] = plugin

        plugin_collector.schedule_round_finished(object(), "ok")  # pyright: ignore[reportArgumentType]
        await _drain()

    async def test_failures_are_isolated_on_their_own_key(
        self,
        plugins: dict[str, NekroPlugin],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services.plugin import isolation

        isolation.reset_all()
        monkeypatch.setattr(config, "PLUGIN_ISOLATION_FAILURE_THRESHOLD", 1)
        plugin = _plugin("p_iso")
        injected: list[str] = []

        @plugin.mount_on_round_finished()
        async def _done(_ctx: AgentCtx, _outcome: str) -> None:
            raise RuntimeError("收尾回调坏了")

        @plugin.mount_prompt_inject_method("probe")
        async def _inject(_ctx: Any) -> str:
            injected.append("called")
            return "注入照常"

        plugins[plugin.key] = plugin
        try:
            plugin_collector.schedule_round_finished(AgentCtx(from_chat_key=CHAT_KEY), "ok")
            await _drain()

            assert isolation.get_quarantine(f"{plugin.key}#on_round_finished") is not None
            assert isolation.get_quarantine(plugin.key) is None
            assert await plugin.render_inject_prompt(AgentCtx(from_chat_key=CHAT_KEY)) == "注入照常"

            assert isolation.release_quarantine(plugin.key, reason="用例") is True
            assert isolation.get_quarantine(f"{plugin.key}#on_round_finished") is None
        finally:
            isolation.reset_all()

    async def test_unlimited_global_timeout_still_caps_the_hook(
        self,
        plugins: dict[str, NekroPlugin],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "PLUGIN_CALL_TIMEOUT_SECONDS", 0)
        monkeypatch.setattr(collector_module, "ROUND_FINISHED_MAX_SECONDS", 0.1)
        plugin = _plugin("p_forever")

        @plugin.mount_on_round_finished()
        async def _forever(_ctx: AgentCtx, _outcome: str) -> None:
            await asyncio.sleep(30)

        plugins[plugin.key] = plugin

        plugin_collector.schedule_round_finished(AgentCtx(from_chat_key=CHAT_KEY), "ok")
        await asyncio.wait_for(_drain(), timeout=5)

    def test_reset_methods_clears_the_hook(self) -> None:
        plugin = _plugin("p_reset")

        @plugin.mount_on_round_finished()
        async def _done(_ctx: AgentCtx, _outcome: str) -> None:
            return None

        plugin.reset_methods()

        assert plugin.on_round_finished_method is None


class TestRunAgentSchedulesTheHook:
    async def _run(self, monkeypatch: pytest.MonkeyPatch, error: BaseException | None) -> list[str]:
        scheduled: list[str] = []

        async def fake_round(**_kwargs: Any) -> None:
            if error is not None:
                raise error

        async def no_db(*_args: Any, **_kwargs: Any) -> Any:
            return None

        monkeypatch.setattr(run_agent_module, "_run_agent_in_instance_scope", fake_round)
        monkeypatch.setattr(run_agent_module, "_channel_message_marker", no_db)
        monkeypatch.setattr(run_agent_module, "_record_round_reply", no_db)
        monkeypatch.setattr(
            run_agent_module.plugin_collector,
            "schedule_round_finished",
            lambda _ctx, outcome: scheduled.append(outcome),
        )
        ctx = SimpleNamespace(db_chat_channel=SimpleNamespace(chat_key=CHAT_KEY, instance_key="ob31001"))

        if error is None:
            await run_agent_module.run_agent(CHAT_KEY, ctx=ctx)  # type: ignore[arg-type]
        else:
            with pytest.raises(type(error)):
                await run_agent_module.run_agent(CHAT_KEY, ctx=ctx)  # type: ignore[arg-type]
        return scheduled

    async def test_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert await self._run(monkeypatch, None) == ["ok"]

    async def test_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        error = run_agent_module.AllLLMRequestsFailedError(kinds=[LLMFailureKind.CONTENT_BLOCKED])

        assert await self._run(monkeypatch, error) == ["blocked"]

    async def test_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert await self._run(monkeypatch, RuntimeError("别处的错")) == ["failed"]

    async def test_cancelled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert await self._run(monkeypatch, asyncio.CancelledError()) == ["cancelled"]
