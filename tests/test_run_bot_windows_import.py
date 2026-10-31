"""`run_bot.py` 在 Windows 上 import 即崩（裸机部署验收发现，round-27 第二轮）

`run_bot.py` 顶层的卡死诊断在 Windows 上不成立：`faulthandler.register` 与 `signal.SIGUSR1` / `SIGUSR2`
都只在 POSIX 上存在，原先无条件调用，`bot` 入口一 import 就 AttributeError。诊断只是辅助手段，
平台不支持就跳过，不能挡住启动。

脚本顶层会解析命令行、初始化 nonebot，没法整份 import。这里取「到解析命令行参数之前」的那一段原样执行，
并把当前解释器的 `faulthandler` / `signal` 改造成 Windows 的样子（去掉那几个名字）——Linux 上也能复现，
不依赖在 Windows 上跑。
"""

import faulthandler
import signal
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

RUN_BOT = Path(__file__).parent.parent / "run_bot.py"
CUT_MARKER = "# 解析命令行参数"


def _diagnostics_prefix() -> str:
    source = RUN_BOT.read_text(encoding="utf-8")
    assert CUT_MARKER in source, "run_bot.py 的结构变了：找不到「解析命令行参数」分界，请同步这条用例"
    return source[: source.index(CUT_MARKER)]


def _run_prefix() -> Dict[str, Any]:
    namespace: Dict[str, Any] = {"__name__": "run_bot_diagnostics_probe"}
    exec(compile(_diagnostics_prefix(), str(RUN_BOT), "exec"), namespace)  # noqa: S102 - 取自本仓库自己的脚本
    return namespace


def _pretend_to_be_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(faulthandler, "register", raising=False)
    monkeypatch.delattr(signal, "SIGUSR1", raising=False)
    monkeypatch.delattr(signal, "SIGUSR2", raising=False)


class TestDiagnosticsAreOptionalOnPlatformsWithoutThem:
    def test_the_script_prefix_runs_where_the_posix_names_do_not_exist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _pretend_to_be_windows(monkeypatch)
        assert not hasattr(faulthandler, "register") and not hasattr(signal, "SIGUSR1")  # 前置对照：真的像 Windows

        namespace = _run_prefix()

        assert callable(namespace["_dump_asyncio_tasks"]), "卡死诊断的处理函数仍应定义（只是不注册）"

    def test_each_missing_name_alone_is_survived(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """三个名字各自缺席的组合都不该崩（不同平台 / 解释器缺的不一定是同一批）"""
        # 其余名字一律用无害的替身，避免在 POSIX 上真的给测试进程装信号处理器
        def present(missing: str) -> None:
            monkeypatch.undo()
            monkeypatch.setattr(signal, "signal", lambda *a, **k: None)
            monkeypatch.setattr(signal, "SIGUSR1", 10, raising=False)
            monkeypatch.setattr(signal, "SIGUSR2", 12, raising=False)
            monkeypatch.setattr(faulthandler, "register", lambda *a, **k: None, raising=False)
            if missing == "register":
                monkeypatch.delattr(faulthandler, "register")
            else:
                monkeypatch.delattr(signal, missing)

        for missing in ("register", "SIGUSR1", "SIGUSR2"):
            present(missing)
            _run_prefix()

    def test_control_the_diagnostics_are_still_registered_when_the_platform_has_them(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """反向对照：支持的平台上照常注册（别让「保护」变成把功能整个删掉）；用记录器代替真实注册"""
        registered: List[Tuple[Any, ...]] = []
        handlers: List[Tuple[Any, Any]] = []
        monkeypatch.setattr(faulthandler, "register", lambda *a, **k: registered.append(a), raising=False)
        monkeypatch.setattr(signal, "SIGUSR1", 10, raising=False)
        monkeypatch.setattr(signal, "SIGUSR2", 12, raising=False)
        monkeypatch.setattr(signal, "signal", lambda num, handler: handlers.append((num, handler)))

        _run_prefix()

        assert registered and registered[0][0] == 10, "SIGUSR1 的 faulthandler 没有注册"
        assert [num for num, _ in handlers] == [12], "SIGUSR2 的协程栈 dump 没有注册"

    def test_no_unguarded_posix_only_call_is_left_in_the_prefix(self) -> None:
        """静态兜底：前缀里凡是碰 SIGUSR* / faulthandler.register 的语句都在 hasattr 守卫之下"""
        import ast

        tree = ast.parse(_diagnostics_prefix())
        offenders = []
        # 只看模块顶层语句：函数体在 import 时不执行；`if hasattr(...)` 守卫下的语句算受保护
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Import, ast.ImportFrom)):
                continue
            if isinstance(node, ast.If) and "hasattr" in ast.unparse(node.test):
                continue
            text = ast.unparse(node)
            if "SIGUSR" in text or "faulthandler.register" in text:
                offenders.append((node.lineno, text))
        assert not offenders, f"这些顶层语句在非 POSIX 平台上会 AttributeError，需要 hasattr 守卫: {offenders}"
