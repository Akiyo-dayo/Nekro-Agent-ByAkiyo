"""重启系统：「不支持」与「失败」必须是两件事

## 这条测试要防的是什么

`POST /api/restart` 原来只有 Docker 一条路走得通，其余部署一律 500「重启系统失败」。
用户的原话是「仪表盘重启系统报错 重启系统失败」—— 症状是报错，实际是这种部署形态
**没有能把进程拉回来的监管者**。两件事混在一个 500 里，管理员只能去日志里找一个不存在的 bug。

所以这里钉三组东西：

1. **判据本身**（`parse_systemd_unit` / `evaluate_systemd_unit`）——
   判错的代价不对称：把能重启的判成不支持只是让人手动重启一次；
   把起不来的判成支持就是服务永久下线。所以判据保守，且必须逐条可测。
2. **路由的三条分支**各自的出口形态：Docker 沿用旧路径、systemd 安排优雅退出、
   其余抛 `DeploymentCapabilityError` 且带机器可读的 `reason_code`。
3. **capability 与真正的重启共用同一次判据** —— 否则会出现「按钮可点、接口拒绝」
   或者更糟的「按钮禁用、接口其实能用」。

`cgroup` 那几行不是编的：`session-3017.scope` 那份就是测试服务器上
`/proc/<bot pid>/cgroup` 的真实内容（裸机 `setsid nohup .venv/bin/bot` 起来的进程）。
"""

import asyncio
import json
import signal
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from nekro_agent.routers import restart as restart_router
from nekro_agent.schemas.errors import DeploymentCapabilityError, OperationFailedError
from nekro_agent.schemas.i18n import SupportedLang
from nekro_agent.services.user.role import Role
from nekro_agent.tools import process_supervisor
from nekro_agent.tools.process_supervisor import (
    RestartCapability,
    RestartMode,
    evaluate_systemd_unit,
    parse_systemctl_show,
    parse_systemd_unit,
    probe_restart_capability,
)

SUPER_USER = SimpleNamespace(perm_level=Role.Super, username="admin")

# 裸机部署（测试服务器实测）：SSH 会话的 scope，systemd 只是收养了它，不管它的生死
BARE_METAL_CGROUP = "0::/user.slice/user-0.slice/session-3017.scope\n"
# 同一台机器上跑在 systemd 服务里时的形态
SERVICE_CGROUP_V2 = "0::/system.slice/nekro-agent.service\n"
SERVICE_CGROUP_V1 = (
    "12:pids:/system.slice/nekro-agent.service\n"
    "1:name=systemd:/system.slice/nekro-agent.service\n"
)
DOCKER_CGROUP = "0::/system.slice/docker-4f1c2b9a.scope\n"


class TestParseSystemdUnit:
    """从 `/proc/self/cgroup` 认出所属的 systemd 服务"""

    @pytest.mark.parametrize(
        ("label", "cgroup", "expected"),
        [
            ("裸机 SSH 会话 scope", BARE_METAL_CGROUP, None),
            ("cgroup v2 服务", SERVICE_CGROUP_V2, "nekro-agent.service"),
            ("cgroup v1 服务", SERVICE_CGROUP_V1, "nekro-agent.service"),
            ("Docker 容器 scope", DOCKER_CGROUP, None),
            ("嵌套在服务下的子 cgroup", "0::/system.slice/na.service/extra\n", "na.service"),
            ("空文件", "", None),
            ("只有根 cgroup", "0::/\n", None),
        ],
    )
    def test_only_service_units_count(self, label: str, cgroup: str, expected: Optional[str]) -> None:
        """只有 `*.service` 算数

        `session-*.scope` 与 `docker-*.scope` 都不是服务：进程退出后没有任何人会把它拉起来。
        认错这一条 = 判成「支持重启」= 服务永久下线。
        """
        assert parse_systemd_unit(cgroup) == expected, label


class TestParseSystemctlShow:
    def test_reads_key_value_pairs(self) -> None:
        assert parse_systemctl_show("Restart=always\nMainPID=2113088\n") == {
            "Restart": "always",
            "MainPID": "2113088",
        }

    def test_ignores_lines_without_separator(self) -> None:
        """`systemctl` 偶尔会往 stdout 写非 KEY=VALUE 的行，不能让它污染判据"""
        assert parse_systemctl_show("Restart=always\nFailed to get properties\n") == {"Restart": "always"}


class TestEvaluateSystemdUnit:
    """三条判据缺一不可，理由见 `process_supervisor` 模块 docstring"""

    @pytest.mark.parametrize(
        ("label", "properties", "pid", "expected_mode", "expected_reason"),
        [
            (
                "Restart=always 且我们就是主进程",
                {"Restart": "always", "MainPID": "4242"},
                4242,
                RestartMode.SYSTEMD,
                "",
            ),
            (
                "Restart=on-failure：优雅退出后不会被拉起来",
                {"Restart": "on-failure", "MainPID": "4242"},
                4242,
                RestartMode.UNSUPPORTED,
                "systemd_restart_policy",
            ),
            (
                "Restart=no",
                {"Restart": "no", "MainPID": "4242"},
                4242,
                RestartMode.UNSUPPORTED,
                "systemd_restart_policy",
            ),
            (
                "Restart 读不到",
                {"MainPID": "4242"},
                4242,
                RestartMode.UNSUPPORTED,
                "systemd_restart_policy",
            ),
            (
                "我们是 wrapper 起的子进程，自己退出未必等于服务退出",
                {"Restart": "always", "MainPID": "4200"},
                4242,
                RestartMode.UNSUPPORTED,
                "systemd_not_main_process",
            ),
            (
                "MainPID 读不到",
                {"Restart": "always"},
                4242,
                RestartMode.UNSUPPORTED,
                "systemd_not_main_process",
            ),
        ],
    )
    def test_capability_table(
        self,
        label: str,
        properties: dict[str, str],
        pid: int,
        expected_mode: RestartMode,
        expected_reason: str,
    ) -> None:
        capability = evaluate_systemd_unit("nekro-agent.service", properties, pid)
        assert capability.mode is expected_mode, label
        assert capability.reason_code == expected_reason, label
        assert capability.supported is (expected_mode is RestartMode.SYSTEMD), label
        assert capability.unit == "nekro-agent.service", label

    def test_unsupported_details_are_actionable(self) -> None:
        """不支持时的文案必须说清「怎么办」，否则又退回成一句无处下手的报错"""
        policy = evaluate_systemd_unit("na.service", {"Restart": "on-failure", "MainPID": "1"}, 1)
        assert "on-failure" in policy.detail
        assert "always" in policy.detail


class TestProbeRestartCapability:
    async def test_docker_deployment_keeps_the_old_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Docker 一侧不看 cgroup：容器停了由 daemon 负责拉起来"""
        monkeypatch.setattr(process_supervisor.OsEnv, "RUN_IN_DOCKER", True)
        capability = await probe_restart_capability()
        assert capability.mode is RestartMode.DOCKER
        assert capability.supported is True

    async def test_bare_metal_is_unsupported_not_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """测试服务器的真实形态：裸机 `uv run bot`，没有任何监管者"""
        monkeypatch.setattr(process_supervisor.OsEnv, "RUN_IN_DOCKER", False)
        monkeypatch.setattr(process_supervisor.sys, "platform", "linux")
        monkeypatch.setattr(process_supervisor, "_read_cgroup", lambda: BARE_METAL_CGROUP)
        capability = await probe_restart_capability()
        assert capability.mode is RestartMode.UNSUPPORTED
        assert capability.reason_code == "no_supervisor"
        assert capability.unit is None

    async def test_cgroup_unreadable_is_unsupported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(process_supervisor.OsEnv, "RUN_IN_DOCKER", False)
        monkeypatch.setattr(process_supervisor.sys, "platform", "linux")
        monkeypatch.setattr(process_supervisor, "_read_cgroup", lambda: None)
        capability = await probe_restart_capability()
        assert capability.reason_code == "no_supervisor"

    async def test_non_linux_is_unsupported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(process_supervisor.OsEnv, "RUN_IN_DOCKER", False)
        monkeypatch.setattr(process_supervisor.sys, "platform", "win32")
        capability = await probe_restart_capability()
        assert capability.reason_code == "not_posix"

    async def test_systemctl_failure_is_unsupported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """查不到重启策略 = 不能确认退出后会回来 = 按不支持处理"""
        monkeypatch.setattr(process_supervisor.OsEnv, "RUN_IN_DOCKER", False)
        monkeypatch.setattr(process_supervisor.sys, "platform", "linux")
        monkeypatch.setattr(process_supervisor, "_read_cgroup", lambda: SERVICE_CGROUP_V2)

        async def _no_properties(unit: str) -> None:
            assert unit == "nekro-agent.service"
            return None

        monkeypatch.setattr(process_supervisor, "_read_systemctl_properties", _no_properties)
        capability = await probe_restart_capability()
        assert capability.reason_code == "systemd_query_failed"
        assert capability.unit == "nekro-agent.service"

    async def test_supervised_service_is_supported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(process_supervisor.OsEnv, "RUN_IN_DOCKER", False)
        monkeypatch.setattr(process_supervisor.sys, "platform", "linux")
        monkeypatch.setattr(process_supervisor, "_read_cgroup", lambda: SERVICE_CGROUP_V2)
        monkeypatch.setattr(process_supervisor.os, "getpid", lambda: 4242)

        async def _properties(unit: str) -> dict[str, str]:
            return {"Restart": "always", "MainPID": "4242"}

        monkeypatch.setattr(process_supervisor, "_read_systemctl_properties", _properties)
        capability = await probe_restart_capability()
        assert capability.mode is RestartMode.SYSTEMD
        assert capability.supported is True


def _patch_probe(monkeypatch: pytest.MonkeyPatch, capability: RestartCapability) -> None:
    async def _probe() -> RestartCapability:
        return capability

    monkeypatch.setattr(restart_router, "probe_restart_capability", _probe)


class TestRestartRoute:
    async def test_unsupported_deployment_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """不支持的部署不能再回「重启系统失败」

        这是用户实际撞上的那一条：文案必须让人知道「不是 bug，是这种部署做不到」，
        并且不能是 5xx —— `app_error_handler` 对 >=500 会打一整条 `logger.exception`，
        正好把管理员引去翻日志。
        """
        _patch_probe(
            monkeypatch,
            RestartCapability(
                mode=RestartMode.UNSUPPORTED,
                supported=False,
                reason_code="no_supervisor",
                detail="当前是裸机部署，进程没有任何监管者",
            ),
        )
        with pytest.raises(DeploymentCapabilityError) as excinfo:
            await restart_router.restart_system(_current_user=SUPER_USER)

        error = excinfo.value
        assert error.http_status == 403
        assert 400 <= error.http_status < 500
        assert error.data == {"reason_code": "no_supervisor", "mode": "unsupported", "unit": None}
        message = error.get_message(SupportedLang.ZH_CN)
        assert "不支持" in message
        assert "监管者" in message

    async def test_systemd_deployment_schedules_graceful_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scheduled: list[bool] = []
        _patch_probe(
            monkeypatch,
            RestartCapability(mode=RestartMode.SYSTEMD, supported=True, unit="nekro-agent.service"),
        )
        monkeypatch.setattr(restart_router, "schedule_self_restart", lambda: scheduled.append(True))

        response = await restart_router.restart_system(_current_user=SUPER_USER)

        assert response.ok is True
        assert scheduled == [True]

    async def test_docker_deployment_calls_restart_self(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Docker 一侧逐字不变：仍旧只调 `restart_self()`，成功即 ok"""
        calls: list[bool] = []

        async def _restart_self() -> bool:
            calls.append(True)
            return True

        _patch_probe(monkeypatch, RestartCapability(mode=RestartMode.DOCKER, supported=True))
        monkeypatch.setattr(restart_router, "restart_self", _restart_self)

        response = await restart_router.restart_system(_current_user=SUPER_USER)

        assert response.ok is True
        assert calls == [True]

    async def test_docker_restart_failure_is_still_a_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Docker 下真的失败了仍然是 `OperationFailedError` —— 那才是「失败」"""

        async def _restart_self() -> bool:
            return False

        _patch_probe(monkeypatch, RestartCapability(mode=RestartMode.DOCKER, supported=True))
        monkeypatch.setattr(restart_router, "restart_self", _restart_self)

        with pytest.raises(OperationFailedError):
            await restart_router.restart_system(_current_user=SUPER_USER)

    async def test_capability_endpoint_mirrors_the_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """前端据此禁用按钮，所以它必须与真正重启走的是同一次判据"""
        _patch_probe(
            monkeypatch,
            RestartCapability(
                mode=RestartMode.UNSUPPORTED,
                supported=False,
                reason_code="systemd_restart_policy",
                detail="所属 systemd 服务的 Restart 策略是 on-failure",
                unit="nekro-agent.service",
            ),
        )
        response = await restart_router.get_restart_capability(_current_user=SUPER_USER)

        assert response.supported is False
        assert response.mode == "unsupported"
        assert response.reason_code == "systemd_restart_policy"
        assert response.unit == "nekro-agent.service"
        assert response.detail

    async def test_capability_and_restart_read_the_same_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """同一次探测下两个端点的结论必须一致：能力查询说不支持，重启就必须拒绝"""
        probes: list[str] = []

        async def _probe() -> RestartCapability:
            probes.append("called")
            return RestartCapability(
                mode=RestartMode.UNSUPPORTED,
                supported=False,
                reason_code="no_supervisor",
                detail="裸机部署，没有监管者",
            )

        monkeypatch.setattr(restart_router, "probe_restart_capability", _probe)

        capability = await restart_router.get_restart_capability(_current_user=SUPER_USER)
        with pytest.raises(DeploymentCapabilityError):
            await restart_router.restart_system(_current_user=SUPER_USER)

        assert capability.supported is False
        assert probes == ["called", "called"]


class TestErrorCarriesTheSameFactsAsCapability:
    """`POST` 的报错体必须和 `GET /capability` 说一样的话

    前端两条路共用同一套 `reason_code -> 文案` 的翻译（`hooks/useRestartCapability.ts`），
    systemd 那两个原因还要说出是哪个 unit。报错体少带一个字段，前端就只能回落到后端的
    中文 `detail`，en-US 用户拿到一堵中文墙。
    """

    async def test_systemd_reason_reports_the_unit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_probe(
            monkeypatch,
            RestartCapability(
                mode=RestartMode.UNSUPPORTED,
                supported=False,
                reason_code="systemd_restart_policy",
                detail="所属 systemd 服务的 Restart 策略是 on-failure",
                unit="na.service",
            ),
        )
        with pytest.raises(DeploymentCapabilityError) as excinfo:
            await restart_router.restart_system(_current_user=SUPER_USER)

        assert excinfo.value.data == {
            "reason_code": "systemd_restart_policy",
            "mode": "unsupported",
            "unit": "na.service",
        }


class TestEveryReasonCodeIsTranslatable:
    """后端能吐出的每一个 `reason_code`，前端两种语言都得有对应词条

    这不是形式主义：`detail` 只有中文（它就是「前端翻不到时的兜底」），少一个键不会报错、
    不会有任何信号，只会静默回落成中文。改造首版的 5 个 `reason_code` 里只有 3 个进了前端词条，
    缺的恰好是 `systemd_restart_policy` —— 「systemd 服务已经配好了、只差 Restart=always」
    是最可能出现、也最可操作的那一种部署。
    """

    LOCALES = ("zh-CN", "en-US")

    @staticmethod
    def _unsupported_texts(locale: str) -> dict[str, str]:
        path = (
            Path(__file__).resolve().parent.parent
            / "frontend"
            / "src"
            / "locales"
            / locale
            / "common.json"
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload["restartCapability"]["unsupported"]

    @pytest.mark.parametrize("locale", LOCALES)
    def test_all_backend_reason_codes_have_a_translation(self, locale: str) -> None:
        texts = self._unsupported_texts(locale)
        missing = sorted(set(process_supervisor._UNSUPPORTED_DETAILS) - set(texts))  # noqa: SLF001
        assert missing == [], f"{locale} 缺少 reason_code 文案: {missing}"

    @pytest.mark.parametrize("locale", LOCALES)
    def test_has_a_last_resort_entry(self, locale: str) -> None:
        """后端将来新增一个 reason_code 时，前端至少不会显示一个原始键名"""
        assert self._unsupported_texts(locale)["unknown"]

    def test_the_two_locales_cover_the_same_codes(self) -> None:
        assert set(self._unsupported_texts("zh-CN")) == set(self._unsupported_texts("en-US"))


class TestScheduleSelfRestart:
    async def test_it_signals_the_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """真的发了 SIGTERM 才算安排了重启 —— 「接口回了 ok 但什么也没发生」是最坏的结局"""
        signals: list[tuple[int, int]] = []
        monkeypatch.setattr(process_supervisor.os, "getpid", lambda: 4242)
        monkeypatch.setattr(
            process_supervisor.os,
            "kill",
            lambda pid, sig: signals.append((pid, sig)),
        )

        await process_supervisor.schedule_self_restart(delay=0)

        assert signals == [(4242, signal.SIGTERM)]

    async def test_a_failed_exit_is_logged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """退不出去就必须留下痕迹，否则和被 GC 掉一样无声"""
        errors: list[str] = []

        def _boom(pid: int, sig: int) -> None:
            raise PermissionError("not permitted")

        monkeypatch.setattr(process_supervisor.os, "kill", _boom)
        monkeypatch.setattr(process_supervisor.logger, "error", lambda message: errors.append(str(message)))

        task = process_supervisor.schedule_self_restart(delay=0)
        with pytest.raises(PermissionError):
            await task
        # 回调在任务完成后由事件循环派发，让出一次即可
        await asyncio.sleep(0)

        assert errors and "不会重启" in errors[0]


class TestTenantsCannotRestart:
    """整个进程承载所有租户的实例，重启是宿主级动作 —— 租户永远够不到 `Role.Super`"""

    @pytest.mark.parametrize("role", [Role.Guest, Role.User, Role.Admin])
    async def test_below_super_is_rejected(self, role: Role, monkeypatch: pytest.MonkeyPatch) -> None:
        from fastapi import HTTPException

        _patch_probe(monkeypatch, RestartCapability(mode=RestartMode.DOCKER, supported=True))
        for endpoint in (restart_router.restart_system, restart_router.get_restart_capability):
            with pytest.raises(HTTPException) as excinfo:
                await endpoint(_current_user=SimpleNamespace(perm_level=role))
            assert excinfo.value.status_code == 403
