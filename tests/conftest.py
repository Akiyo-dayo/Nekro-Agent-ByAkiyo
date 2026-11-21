"""全局测试固件。

## 为什么需要它

`services/memory/text_terms` 有两档 CJK 切分（词典档 `jieba` / 无词典的启发式档）。生产上
「词典还没加载完就先用启发式档、后台线程加载好了再切换」是**有意**的：加载是约 0.85s 的纯
CPU 过程，压在事件循环上等于单进程里的几十个实例一起停摆。

但同一条规则在测试里会变成不确定性：一条用例切出什么词，取决于那个后台线程有没有跑完，
于是同一份断言可能红也可能绿、还与用例执行顺序有关。所以这里在**任何用例开始之前**一次性
把词典加载好，让整个会话恒定跑在生产稳态（词典档）上。

jieba 缺席时（它是 `pyproject.toml` 里的正式依赖，正常不会）这个固件是空操作，依赖词典档的
用例由 `tests/test_memory_chinese_segmentation.py:require_jieba()` 自行 skip。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

# CI 的「Run Backend Tests」步骤在 pytest 写完报告后进程不退出时会强杀它，那之后 shell 拿到的退出码
# 是杀出来的、不再代表 pytest 自己的结论。这里让 pytest 在会话结束时把**自己的**退出状态落盘
# （路径由环境变量给，未设则什么都不做），判定步骤据此区分「正常收尾后挂住被杀」与「中途被中断」。
SESSION_STATUS_FILE_ENV = "NA_PYTEST_SESSION_STATUS_FILE"


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int | pytest.ExitCode) -> None:
    target = os.environ.get(SESSION_STATUS_FILE_ENV)
    if target:
        Path(target).write_text(str(int(exitstatus)), encoding="utf-8")


@pytest.fixture(scope="session", autouse=True)
def _warm_up_memory_tokenizer() -> None:
    """在收集之后、第一条用例之前把切词词典加载好，消除「哪一档生效」的时序依赖。"""
    from nekro_agent.services.memory.text_terms import warm_up_tokenizer

    warm_up_tokenizer()


@pytest.fixture(autouse=True)
def _fresh_rpc_decode_budget() -> None:
    """沙盒 RPC 的解码时间预算按容器键记账；各用例共用同几个容器键，上一条用例的欠账不该让下一条被拒"""
    from nekro_agent.services.rpc_service import decode_budget

    decode_budget.reset()


@pytest.fixture(autouse=True)
def _isolated_web_chat_workspace_taint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """网页聊天 MCP 的「曾经不归平台方」标记按用例隔离

    置位现在挂在归属推导上，任何用例推导出租户归属都会写这个文件；不隔离就会写进真实的
    `data/system`。模块级的内存集合跨用例残留，还会让下一条用例里同 id 的工作区被误判成被标记。
    所以落盘改道到临时目录，内存集合与待重建标志、重建锁一并重置；收尾取消没跑完的清理任务。
    """
    from nekro_agent.services.mcp import web_chat_workspace_taint as taint

    monkeypatch.setattr(taint, "taint_file", lambda: tmp_path / "web_chat_mcp_tainted_workspaces.json")
    monkeypatch.setattr(taint, "_memory_tainted", set())
    monkeypatch.setattr(taint, "_rebuild_pending", False)
    monkeypatch.setattr(taint, "_rebuild_lock", None)

    yield

    for task in list(taint._scrub_tasks):
        if not task.done():
            task.cancel()
    taint._scrub_tasks.clear()
