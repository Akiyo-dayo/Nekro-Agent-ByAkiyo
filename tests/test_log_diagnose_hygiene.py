"""loguru `diagnose` 开关与出厂默认凭据告警（§3 #39 残留②／待用户决策 8）。

## 这条缺陷是什么

`diagnose=True` 时 loguru 会在 traceback 的每一帧下面把**那一行源码里出现的每个名字连同
取值**渲染出来。它是 loguru 文档自己点名「生产环境应关闭，会带出敏感数据」的选项，
而 loguru 的默认值是 **True**，`core/logger.py` 从来没有显式给过这个键。

这不是理论风险：测试部署 `app.log` 里现存 **50 处** `ConnectionParameters(user=…, password=…)`，
是 tortoise/asyncpg 连库失败时被诊断行打出来的**数据库口令**，出处根本不在 LLM 链路。

## 为什么脱敏解决不了它

那份诊断文本是 loguru 直接从帧对象 `repr()` 出来的，**不经过任何插值点**，
所以 `services/agent/upstream_redaction.py`（按值 + 按形态）与 `tools/redaction.py`
的正则**一条都作用不到它**。#39 只能逐条绕开（改用 stdlib traceback 自己渲染再脱敏），
而泄漏面是全进程的。开关做在 sink 上才是一次覆盖全进程。

## 两个方向都要断言

「口令 0 命中」这一侧单独成立没有意义——把整条 traceback 吞掉同样满足它。
所以每一条「0 命中」都配一条「异常类型 / 异常消息 / 调用栈逐字还在」，
另有对照组（同一份代码在 `diagnose=True` 下跑一遍）证明这些 0 是**测得出差异**的（§2.11）。
"""

from __future__ import annotations

import ast
import importlib
import io
import logging
import logging.config
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest
from loguru import logger

from nekro_agent.core.logger import (
    ACCESS_LOG_LOGGER_NAMES,
    FACTORY_DEFAULT_CREDENTIAL_CHECKS,
    REDACTED_QUERY_VALUE,
    SENSITIVE_QUERY_KEY_PARTS,
    SensitiveQueryParamFilter,
    audit_factory_default_credentials,
    build_log_handlers,
    format_log_entry,
    install_sensitive_query_param_filter,
    is_sensitive_query_key,
    redact_sensitive_query_params,
    resolve_log_diagnose,
)
from nekro_agent.core.os_env import FACTORY_DEFAULT_POSTGRES_PASSWORD, OsEnv

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGGER_SOURCE = REPO_ROOT / "nekro_agent" / "core" / "logger.py"
OS_ENV_SOURCE = REPO_ROOT / "nekro_agent" / "core" / "os_env.py"

# `nekro_agent.core.__init__` 里 `logger` 这个名字指的是 loguru 的 Logger 实例，
# 直接 `from nekro_agent.core import logger` 拿到的不是子模块 —— 要看模块级状态得走 import_module
logger_module = importlib.import_module("nekro_agent.core.logger")

# 哨兵只出现在**局部变量**里，绝不出现在日志消息的 f-string 里 ——
# 否则「0 命中」会因为消息本身不含它而恒成立，与 diagnose 开关无关。
SENTINEL_PG_PASSWORD = "S3ntinel-Pg-Passw0rd-Zz"
SENTINEL_API_KEY = "sk-S3ntinel-Model-Group-Key-Zz"


# ============================================================
# 0. 复现真实泄漏形态的异常路径
# ============================================================


class _ConnectionParameters:
    """模仿 asyncpg 的 `ConnectionParameters`：它的 repr 里带 password

    测试部署 `app.log` 里那 50 处泄漏就是这个对象被诊断行渲染出来的。
    """

    def __init__(self, user: str, password: str) -> None:
        self.user = user
        self.password = password

    def __repr__(self) -> str:
        return f"ConnectionParameters(user={self.user!r}, password={self.password!r})"


def _connect(params: _ConnectionParameters) -> None:
    raise ConnectionRefusedError("[Errno 111] Connect call failed ('127.0.0.1', 5433)")


def _open_database_connection() -> None:
    """局部变量里带口令的真实形态：连接参数对象 + DSN 串"""
    params = _ConnectionParameters(user="nekro_agent", password=SENTINEL_PG_PASSWORD)
    dsn = f"postgres://{params.user}:{params.password}@127.0.0.1:5433/nekro_agent"  # noqa: F841
    _connect(params)


class _AllLLMRequestsFailedError(Exception):
    """#39 残留①（`services/message_service.py:283`）的形态：异常文案本身是固定串，
    泄漏只可能来自诊断行渲染的上游帧局部变量。"""


def _send_agent_request(api_key: str, messages: List[Dict[str, str]]) -> None:
    raise _AllLLMRequestsFailedError("所有 LLM 请求均失败")


def _run_agent() -> None:
    api_key = SENTINEL_API_KEY
    messages = [{"role": "user", "content": "你好"}]
    _send_agent_request(api_key, messages)


def _render_with_spec(spec: Dict[str, Any], raiser: Any, message: str) -> str:
    """用给定的 sink 配置跑一次异常并返回 loguru 真正写出去的文本

    只把 `sink` 换成内存缓冲，`format` / `backtrace` / `diagnose` 逐字沿用传进来的那一份 ——
    对照的必须是产品自己的配置，不是测试另写的一套（§2.9）。
    """
    buffer = io.StringIO()
    kwargs = {key: value for key, value in spec.items() if key not in {"sink", "rotation", "retention", "compression"}}
    kwargs["level"] = "DEBUG"
    kwargs["colorize"] = False
    handler_id = logger.add(buffer, **kwargs)
    try:
        try:
            raiser()
        except Exception:
            logger.exception(message)
    finally:
        logger.remove(handler_id)
    return buffer.getvalue()


# ============================================================
# 1. 开关判据：取值 + **理由**
# ============================================================


class TestResolveLogDiagnose:
    """兜底分支占了这个函数的大半，只断言 False 等于没断言（§6）"""

    @pytest.mark.parametrize(
        ("raw", "expected", "reason_fragment"),
        [
            (None, False, "未设置"),
            ("", False, "未设置"),
            ("   ", False, "未设置"),
            ("true", True, "显式开启"),
            ("True", True, "显式开启"),
            ("TRUE", True, "显式开启"),
            (" true ", True, "显式开启"),
            ("1", True, "显式开启"),
            ("yes", True, "显式开启"),
            ("y", True, "显式开启"),
            ("on", True, "显式开启"),
            ("false", False, "显式关闭"),
            ("False", False, "显式关闭"),
            ("0", False, "显式关闭"),
            ("no", False, "显式关闭"),
            ("n", False, "显式关闭"),
            ("off", False, "显式关闭"),
            ("maybe", False, "无法识别"),
            ("enabled", False, "无法识别"),
            ("2", False, "无法识别"),
        ],
    )
    def test_table(self, raw: Any, expected: bool, reason_fragment: str) -> None:
        enabled, reason = resolve_log_diagnose(raw)
        assert enabled is expected
        assert reason_fragment in reason

    def test_unrecognised_value_is_not_confused_with_unset(self) -> None:
        """两个兜底分支都回 False，但它们的理由必须能分开

        「写错了开关」与「压根没配」的运维动作不同：前者要去改那一行，后者是默认状态。
        """
        _, unset = resolve_log_diagnose(None)
        _, garbage = resolve_log_diagnose("ture")
        assert unset != garbage
        assert "ture" in garbage

    def test_default_source_is_read_at_call_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """缺省取 `OsEnv.LOG_DIAGNOSE`，且是**调用时**读

        import 期定死的话测试换不掉它，换台机器就在测另一件事（§2.9）。
        """
        monkeypatch.setattr(OsEnv, "LOG_DIAGNOSE", "true")
        assert resolve_log_diagnose()[0] is True
        monkeypatch.setattr(OsEnv, "LOG_DIAGNOSE", "false")
        assert resolve_log_diagnose()[0] is False


# ============================================================
# 2. 三个 sink 都要显式带上开关（漏一个 = 那个出口照样泄漏）
# ============================================================


class TestBuildLogHandlers:
    def test_every_sink_carries_diagnose_explicitly(self) -> None:
        """没写这个键就取 loguru 的默认值 True —— 「没写」与「写了 False」结果相反"""
        for diagnose in (True, False):
            handlers = build_log_handlers(diagnose)
            assert len(handlers) == 3
            for spec in handlers:
                assert "diagnose" in spec
                assert spec["diagnose"] is diagnose

    def test_backtrace_stays_on(self) -> None:
        """关掉 diagnose 不等于砍掉调用栈：扩展帧是关掉之后要保住的排障能力"""
        for spec in build_log_handlers(False):
            assert spec["backtrace"] is True

    def test_sink_set_is_unchanged(self) -> None:
        """三个出口：内存/SSE、stdout、app.log —— 改造不许悄悄少一个"""
        handlers = build_log_handlers(False)
        assert isinstance(handlers[0]["sink"], logger_module.LogInterceptHandler)
        assert handlers[1]["sink"] is sys.stdout
        assert Path(handlers[2]["sink"]).name == "app.log"

    def test_rotation_policy_unchanged(self) -> None:
        """`app.log` 的轮转策略是既有行为，本轮不动"""
        app_log = build_log_handlers(False)[2]
        assert app_log["rotation"] == "100 MB"
        assert app_log["retention"] == "10 days"
        assert app_log["compression"] == "zip"


# ============================================================
# 3. 接线：产品**真的**用了这套配置（§2.7 —— 零件全对、调用点没了会全绿）
# ============================================================


class TestTheSwitchIsActuallyWiredIn:
    def test_module_level_handlers_match_the_resolved_switch(self) -> None:
        """模块导入时装上去的那份配置必须与 `resolve_log_diagnose()` 一致"""
        expected, _ = resolve_log_diagnose()
        assert logger_module.LOG_DIAGNOSE_ENABLED is expected
        assert logger_module.log_handlers
        for spec in logger_module.log_handlers:
            assert spec["diagnose"] is expected

    def test_default_deployment_lands_on_the_safe_side(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """什么都不配的部署必须是关着的那一档"""
        monkeypatch.delenv("NEKRO_LOG_DIAGNOSE", raising=False)
        monkeypatch.setattr(OsEnv, "LOG_DIAGNOSE", "")
        assert resolve_log_diagnose()[0] is False

    def test_configure_call_site_exists(self) -> None:
        """AST 契约：`logger.configure(handlers=log_handlers)`，且 `log_handlers` 由
        `build_log_handlers(LOG_DIAGNOSE_ENABLED)` 得来。

        把 `build_log_handlers(...)` 改回内联字面量、或改成 `build_log_handlers(True)`，
        行为断言在「当前环境恰好是关着的」时未必红，这条直接红。
        """
        tree = ast.parse(LOGGER_SOURCE.read_text(encoding="utf-8"))

        assigned_from = {
            target.id: node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        # log_handlers = build_log_handlers(LOG_DIAGNOSE_ENABLED)
        handlers_expr = assigned_from.get("log_handlers")
        assert isinstance(handlers_expr, ast.Call)
        assert isinstance(handlers_expr.func, ast.Name)
        assert handlers_expr.func.id == "build_log_handlers"
        assert [ast.unparse(arg) for arg in handlers_expr.args] == ["LOG_DIAGNOSE_ENABLED"]

        # LOG_DIAGNOSE_ENABLED, LOG_DIAGNOSE_REASON = resolve_log_diagnose()
        tuple_sources = [
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Tuple)
            and [element.id for element in target.elts if isinstance(element, ast.Name)]
            == ["LOG_DIAGNOSE_ENABLED", "LOG_DIAGNOSE_REASON"]
        ]
        assert len(tuple_sources) == 1
        assert ast.unparse(tuple_sources[0]) == "resolve_log_diagnose()"

        configure_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "configure"
        ]
        assert len(configure_calls) == 1
        assert [(kw.arg, ast.unparse(kw.value)) for kw in configure_calls[0].keywords] == [
            ("handlers", "log_handlers"),
        ]


# ============================================================
# 4. 端到端：口令 0 命中，而异常类型 / 消息 / 调用栈照常保留
# ============================================================


def _db_case() -> Tuple[str, str]:
    return SENTINEL_PG_PASSWORD, "数据库连接失败"


class TestDiagnoseOffStopsTheLeak:
    """两个方向都断言：只看「0 命中」的话，把整条 traceback 吞掉也能满分"""

    @pytest.mark.parametrize("handler_index", [0, 1, 2])
    def test_password_gone_from_every_product_sink(self, handler_index: int) -> None:
        text = _render_with_spec(build_log_handlers(False)[handler_index], _open_database_connection, "数据库连接失败")
        assert SENTINEL_PG_PASSWORD not in text

    def test_diagnostics_survive(self) -> None:
        """关掉 diagnose 之后仍然拿得到：异常类型、异常消息、完整调用栈、出事的那一帧"""
        text = _render_with_spec(build_log_handlers(False)[2], _open_database_connection, "数据库连接失败")
        assert SENTINEL_PG_PASSWORD not in text
        assert "ConnectionRefusedError" in text  # 异常类型
        assert "Connect call failed ('127.0.0.1', 5433)" in text  # 异常消息
        assert "Traceback (most recent call last)" in text  # 调用栈
        assert "_open_database_connection" in text  # 中间帧
        assert "_connect" in text  # 抛出点所在函数
        assert "数据库连接失败" in text  # 调用方自己的文案

    def test_contrast_group_really_leaks(self) -> None:
        """对照组：同一份代码在 `diagnose=True` 下必须泄漏

        没有这一条的话，上面那个 0 可能只是「这条输入下两种实现无法区分」（§2.11）。
        """
        text = _render_with_spec(build_log_handlers(True)[2], _open_database_connection, "数据库连接失败")
        assert SENTINEL_PG_PASSWORD in text
        assert "ConnectionParameters(user='nekro_agent'" in text

    def test_llm_path_residual_is_closed(self) -> None:
        """#39 残留①：异常文案是固定串，上游帧的 `api_key` / 整份 prompt 靠诊断行泄漏"""
        spec_off, spec_on = build_log_handlers(False)[2], build_log_handlers(True)[2]
        off = _render_with_spec(spec_off, _run_agent, "执行失败: 所有 LLM 请求均失败")
        on = _render_with_spec(spec_on, _run_agent, "执行失败: 所有 LLM 请求均失败")

        assert SENTINEL_API_KEY in on  # 对照组：确实会漏
        assert "你好" in on  # 连整份 prompt 一起漏
        assert SENTINEL_API_KEY not in off
        assert "你好" not in off
        # 反方向：排障信息一个不少
        assert "_AllLLMRequestsFailedError" in off
        assert "所有 LLM 请求均失败" in off
        assert "Traceback (most recent call last)" in off
        assert "_run_agent" in off
        assert "_send_agent_request" in off


# ============================================================
# 5. 内存日志 / SSE / 下载这条通道：它渲染的是 stdlib traceback，本来就不带局部变量
# ============================================================


class TestStructuredLogEntryHasNoLocals:
    """`format_log_entry` 是 `/logs`、`/logs/stream`、`/logs/download` 三个出口的共同来源"""

    def test_entry_keeps_diagnostics_without_locals(self) -> None:
        captured: List[Dict[str, Any]] = []
        handler_id = logger.add(
            lambda message: captured.append(format_log_entry(dict(message.record))),
            level="DEBUG",
            diagnose=True,  # 故意开着：证明这条通道不受 diagnose 影响
            format="{message}",
        )
        try:
            try:
                _open_database_connection()
            except Exception:
                logger.exception("数据库连接失败")
        finally:
            logger.remove(handler_id)

        assert captured
        entry = captured[-1]
        assert entry["exception_type"] == "ConnectionRefusedError"
        assert entry["exception_message"] == "[Errno 111] Connect call failed ('127.0.0.1', 5433)"
        assert entry["traceback"] is not None
        assert "Traceback (most recent call last)" in entry["traceback"]
        assert "_open_database_connection" in entry["traceback"]
        assert SENTINEL_PG_PASSWORD not in entry["traceback"]
        assert SENTINEL_PG_PASSWORD not in entry["message"]


# ============================================================
# 6. 出厂默认凭据的启动期告警
# ============================================================


class TestFactoryDefaultCredentialAudit:
    def test_judgement_is_not_forked(self) -> None:
        """判据与被判值必须共用同一个字面量（§2.10：只改一侧 = 守卫从此永不成立）"""
        postgres = next(c for c in FACTORY_DEFAULT_CREDENTIAL_CHECKS if c.env_key == "NEKRO_POSTGRES_PASSWORD")
        assert postgres.factory_default == FACTORY_DEFAULT_POSTGRES_PASSWORD
        assert FACTORY_DEFAULT_POSTGRES_PASSWORD == "nekro_agent"

    def test_os_env_default_is_the_same_constant(self) -> None:
        """被判值那一侧也要钉住

        只断言「常量 == 判据」是不够的：把 `OsEnv.POSTGRES_PASSWORD` 的 default 改回一个
        字面量之后，两边就是两份判据了，而这条守卫会**静默地**永不成立（§2.10）。
        用 AST 而不是 `OsEnv.POSTGRES_PASSWORD == 常量`：后者在设了
        `NEKRO_POSTGRES_PASSWORD` 的机器上验的是另一件事（§2.13 环境封闭性）。
        """
        tree = ast.parse(OS_ENV_SOURCE.read_text(encoding="utf-8"))
        os_env_class = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "OsEnv"
        )
        declarations = [
            node.value
            for node in os_env_class.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "POSTGRES_PASSWORD"
            and node.value is not None
        ]
        assert len(declarations) == 1
        call = declarations[0]
        assert isinstance(call, ast.Call)
        defaults = [ast.unparse(kw.value) for kw in call.keywords if kw.arg == "default"]
        assert defaults == ["FACTORY_DEFAULT_POSTGRES_PASSWORD"]

    def test_compose_default_is_the_same_literal(self) -> None:
        """判据的**第三份副本**：compose 形态下生效的出厂默认值写在 `docker-compose.yml` 里

        `environment:` 是一张显式白名单，`NEKRO_POSTGRES_PASSWORD=${POSTGRES_PASSWORD:-<默认>}`
        里那个 `<默认>` 才是 compose 部署真正跑的值。它与 `FACTORY_DEFAULT_POSTGRES_PASSWORD`
        分叉的话，「口令还是出厂默认」这条告警会在**最常见的部署形态上恰好不响**
        （§2.10：判据与被判值只改一侧 = 守卫从此永不成立）。
        """
        compose = (REPO_ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8")
        matches = re.findall(r"NEKRO_POSTGRES_PASSWORD=\$\{POSTGRES_PASSWORD:-([^}]*)\}", compose)
        assert len(matches) == 1, "compose 里 NEKRO_POSTGRES_PASSWORD 的写法变了，这条判据要跟着改"
        assert matches[0] == FACTORY_DEFAULT_POSTGRES_PASSWORD

    @pytest.mark.parametrize(
        ("value", "should_alert"),
        [
            ("nekro_agent", True),  # 出厂默认
            (None, True),  # 没设 = 生效的正是出厂默认
            ("nekro_agent2", False),
            ("Nekro_Agent", False),  # 大小写不同就是另一个口令，不能模糊匹配
            ("a-real-random-password", False),
            ("", False),  # 空口令是另一个问题（连不上库），不由这条判据管
        ],
    )
    def test_table(self, value: Any, should_alert: bool) -> None:
        values: Dict[str, str] = {} if value is None else {"NEKRO_POSTGRES_PASSWORD": value}
        alerts = audit_factory_default_credentials(values)
        assert bool(alerts) is should_alert

    def test_alert_text_is_actionable_and_leaks_nothing_new(self) -> None:
        alerts = audit_factory_default_credentials({"NEKRO_POSTGRES_PASSWORD": FACTORY_DEFAULT_POSTGRES_PASSWORD})
        assert len(alerts) == 1
        alert = alerts[0]
        assert "NEKRO_POSTGRES_PASSWORD" in alert
        assert "docs/operations.md" in alert
        # 告警只说键名与后果，不复述取值（这里取值恰好是公开常量，但规矩要一致）
        assert alert.count(FACTORY_DEFAULT_POSTGRES_PASSWORD) == 0

    def test_default_source_is_oS_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """不传参时取 `OsEnv` 上已经解析好的**生效值**"""
        monkeypatch.setattr(OsEnv, "POSTGRES_PASSWORD", FACTORY_DEFAULT_POSTGRES_PASSWORD)
        assert audit_factory_default_credentials()
        monkeypatch.setattr(OsEnv, "POSTGRES_PASSWORD", "changed-for-real")
        assert audit_factory_default_credentials() == []

    def test_audit_call_site_exists(self) -> None:
        """AST 契约：模块级真的把告警打出去了（§2.7：零件对、调用点没了会全绿）"""
        tree = ast.parse(LOGGER_SOURCE.read_text(encoding="utf-8"))
        call_sites = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "audit_factory_default_credentials"
        ]
        assert len(call_sites) == 1
        loops = [
            node
            for node in tree.body
            if isinstance(node, ast.For) and "audit_factory_default_credentials" in ast.unparse(node.iter)
        ]
        assert len(loops) == 1
        assert "logger.warning" in ast.unparse(loops[0])


# ============================================================
# 7. 鉴权链路：URL 上的令牌不许被 DEBUG 日志原样打出来
# ============================================================


class TestUrlTokenIsNotLogged:
    """本轮扫泄漏面时发现的第二处：`services/user/deps.py`

    走 URL 传令牌的是那几个 SSE 端点（EventSource 设不了请求头）：
    `/logs/stream`（`Role.Super`）、`/events`、命令输出流、NapCat 日志流。
    把它打进日志 = 平台方的**活令牌**逐字进 `app.log` / stdout / `/logs/download`。
    只在 `APP_LOG_LEVEL=DEBUG` 时触发（测试部署当前是 INFO，实测 0 命中），
    但 `.env.example` 里 `LOG_LEVEL=DEBUG` 是默认写着的。
    """

    SENTINEL_TOKEN = "eyJhbGciOiJIUzI1NiJ9.S3ntinelUrlToken.Zz"

    async def _drive(self, raw_query: bytes) -> str:
        from starlette.requests import HTTPConnection

        from nekro_agent.schemas.errors import UnauthorizedError
        from nekro_agent.services.user.deps import get_current_user

        captured: List[Any] = []
        handler_id = logger.add(captured.append, level="DEBUG", format="{message}", colorize=False)
        try:
            connection = HTTPConnection({"type": "http", "query_string": raw_query, "headers": []})
            # 精确到 `UnauthorizedError`：写成 `Exception` 等于不断言，连"函数改名了"都能通过
            with pytest.raises(UnauthorizedError):
                await get_current_user(connection)
        finally:
            logger.remove(handler_id)
        return "".join(str(message) for message in captured)

    async def test_raw_token_never_reaches_the_sink(self) -> None:
        text = await self._drive(f"token={self.SENTINEL_TOKEN}".encode())
        assert self.SENTINEL_TOKEN not in text
        # 反方向：这条路径**确实被走到了**，而且排障需要的信息还在
        assert "Token from URL" in text
        assert f"len={len(self.SENTINEL_TOKEN)}" in text
        assert "bearer_prefix=False" in text

    async def test_bearer_prefixed_token_never_reaches_the_sink(self) -> None:
        text = await self._drive(f"token=Bearer {self.SENTINEL_TOKEN}".encode())
        assert self.SENTINEL_TOKEN not in text
        assert "bearer_prefix=True" in text


# ============================================================
# 8. 随仓库发布的示例配置不许把它打开
# ============================================================


class TestShippedExampleIsSafe:
    """`.env.example` 会被逐字复制成 `.env.dev`（文件头第 9 行 `cp .env.example .env.dev`），
    所以它里面的取值等于「什么都不想的开发者拿到的默认值」。

    ⚠️ 它**不是** prod 的模板：裸机 prod 读的是自己写的 `.env.prod`（仓库里那份只有
    DRIVER/HOST/PORT/ONEBOT_ACCESS_TOKEN 四行、会被 `dockerfile:89` 打进镜像），
    而 compose 形态压根不读 env 文件——`docker-compose.yml` 的 `environment:` 是一张
    **显式白名单**，没登记的键传不进容器。所以 `NEKRO_LOG_DIAGNOSE` 在 compose 上
    结构上恒为关；要在那里打开必须先往白名单里加一行（判据见 PRELAUNCH_CHECKLIST §4.2.1）。
    """

    def test_env_example_declares_it_off(self) -> None:
        lines = [
            line.strip()
            for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("NEKRO_LOG_DIAGNOSE=")
        ]
        assert lines == ["NEKRO_LOG_DIAGNOSE=false"]
        assert resolve_log_diagnose(lines[0].split("=", 1)[1])[0] is False


# ============================================================
# 9. 同一份凭据的**第二个出口**：uvicorn 的访问日志
# ============================================================
#
# `diagnose` 只覆盖「异常 traceback 里的局部变量」，§7 只覆盖「本项目自己那条 DEBUG」。
# 而 uvicorn 每处理完一个请求就打一行访问日志，路径取的是
# `get_path_with_query_string(scope)` —— **带完整 query string**，且是 **INFO 级、无条件打**。
# 走 URL 传的两类凭据（`?token=<JWT>`、`?sig=<取件签名>`）因此会逐字进
# `app.log` / stdout / 内存日志（`/logs`、`/logs/stream`、`/logs/download` 三个出口都读它）。
#
# 这一节按 uvicorn 真实的调用形态驱动，并且**两个 logger 名都要驱动**：
# HTTP 走 `uvicorn.access`，WebSocket 的 `[accepted]` / `403` 行走 `uvicorn.error`。

# 只出现在 URL 里，不出现在任何断言用的固定串里
SENTINEL_URL_JWT = "eyJhbGciOiJIUzI1NiJ9.S3ntinelAccessLogToken.Zz"
SENTINEL_UPLOAD_SIG = "v1.1786000000." + "d3adbeef" * 8

# nonebot/drivers/fastapi.py 里逐字的那一份（uvicorn 启动时会拿它跑 dictConfig）
NONEBOT_UVICORN_LOGGING_CONFIG: Dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"default": {"class": "nonebot.log.LoguruHandler"}},
    "loggers": {
        "uvicorn.error": {"handlers": ["default"], "level": "INFO"},
        "uvicorn.access": {"handlers": ["default"], "level": "INFO"},
    },
}

# uvicorn/protocols/http/h11_impl.py:480 与 protocols/websockets/websockets_impl.py:270
# 逐字的调用形态：(logger 名, 格式串, args)
UVICORN_HTTP_CALL = (
    "uvicorn.access",
    '%s - "%s %s HTTP/%s" %d',
    ("203.0.113.7:51234", "GET", f"/api/emails/12/attachment/a.png?token={SENTINEL_URL_JWT}", "1.1", 200),
)
UVICORN_WS_CALL = (
    "uvicorn.error",
    '%s - "WebSocket %s" [accepted]',
    ("203.0.113.7:51235", f"/api/workspaces/3/sandbox/terminal?token={SENTINEL_URL_JWT}"),
)


@pytest.fixture
def _isolated_uvicorn_loggers() -> Iterator[None]:
    """快照并还原两个 uvicorn logger 的 logging 状态

    这一节要跑 `dictConfig`、要临时摘掉过滤器（对照组），都是**进程全局**的改动。
    不还原的话后面的用例会在一个被改过的 logging 配置上跑。
    """
    saved = [
        (
            name,
            logging.getLogger(name).filters[:],
            logging.getLogger(name).handlers[:],
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
        )
        for name in ACCESS_LOG_LOGGER_NAMES
    ]
    try:
        yield
    finally:
        for name, filters, handlers, level, propagate in saved:
            std_logger = logging.getLogger(name)
            std_logger.filters = filters
            std_logger.handlers = handlers
            std_logger.level = level
            std_logger.propagate = propagate


def _emit_through_loguru(calls: List[Tuple[str, str, Tuple[Any, ...]]]) -> str:
    """按 uvicorn 的调用形态打日志，返回产品 sink 真正收到的文本

    sink 用 `build_log_handlers()` 那一份（只把落点换成内存缓冲），
    走的是 nonebot 的 `LoguruHandler` 桥 —— 与生产同一条路。
    """
    buffer = io.StringIO()
    spec = build_log_handlers(False)[2]
    kwargs = {key: value for key, value in spec.items() if key not in {"sink", "rotation", "retention", "compression"}}
    kwargs["level"] = "INFO"
    kwargs["colorize"] = False
    handler_id = logger.add(buffer, **kwargs)
    try:
        for name, message, args in calls:
            logging.getLogger(name).info(message, *args)
    finally:
        logger.remove(handler_id)
    return buffer.getvalue()


class TestSensitiveQueryParamRedaction:
    """判据本身：只遮凭据类参数的**取值**，别的一个字不动"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # —— 该遮的
            ("/api/logs/stream?token=abc123def", f"/api/logs/stream?token={REDACTED_QUERY_VALUE}"),
            ("/x?sig=v1.123.deadbeef", f"/x?sig={REDACTED_QUERY_VALUE}"),
            ("/x?Token=ABC", f"/x?Token={REDACTED_QUERY_VALUE}"),  # 大小写不敏感
            ("/x?access_token=abc", f"/x?access_token={REDACTED_QUERY_VALUE}"),
            ("/x?api_key=abc", f"/x?api_key={REDACTED_QUERY_VALUE}"),
            ("/x?password=abc", f"/x?password={REDACTED_QUERY_VALUE}"),
            ("/x?signature=abc", f"/x?signature={REDACTED_QUERY_VALUE}"),
            ("/x?authorization=Bearer%20abc", f"/x?authorization={REDACTED_QUERY_VALUE}"),
            # 第二个及以后的参数（只看 `?` 会漏掉 `&` 后面的）
            ("/x?page=2&token=abc", f"/x?page=2&token={REDACTED_QUERY_VALUE}"),
            (
                "/x?token=abc&sig=def",
                f"/x?token={REDACTED_QUERY_VALUE}&sig={REDACTED_QUERY_VALUE}",
            ),
            # —— 不该遮的：访问日志的排障维度全在这些参数上
            ("/api/ext/rpc_exec?container_key=sandbox_x", "/api/ext/rpc_exec?container_key=sandbox_x"),
            (
                "/x?from_chat_key=onebot_v11-ob10001-group_1&page_size=20",
                "/x?from_chat_key=onebot_v11-ob10001-group_1&page_size=20",
            ),
            ("/x?instance_key=ob10001", "/x?instance_key=ob10001"),
            ("/api/logs/stream", "/api/logs/stream"),  # 没有 query string
            ("", ""),
        ],
    )
    def test_table(self, raw: str, expected: str) -> None:
        assert redact_sensitive_query_params(raw) == expected

    def test_request_line_keeps_everything_but_the_value(self) -> None:
        """整条访问日志的形态：只有取值变了"""
        line = f'203.0.113.7:51234 - "GET /api/emails/1/a.png?token={SENTINEL_URL_JWT} HTTP/1.1" 200'
        masked = redact_sensitive_query_params(line)
        assert SENTINEL_URL_JWT not in masked
        assert masked == f'203.0.113.7:51234 - "GET /api/emails/1/a.png?token={REDACTED_QUERY_VALUE} HTTP/1.1" 200'

    def test_bare_key_suffix_is_not_treated_as_secret(self) -> None:
        """`chat_key` / `container_key` 必须留着：遮了它们这行日志就没有对号入座的维度了"""
        assert is_sensitive_query_key("chat_key") is False
        assert is_sensitive_query_key("container_key") is False
        assert is_sensitive_query_key("instance_key") is False
        assert is_sensitive_query_key("api_key") is True

    def test_judgement_is_not_forked_with_tools_redaction(self) -> None:
        """与 `tools/redaction.py` 是同一份判据的镜像 —— 分叉 = 其中一处悄悄失效

        那边加了新的凭据键名而这边没跟上时，这条直接红。
        """
        from nekro_agent.tools.redaction import HIDDEN_KEY_PARTS, REDACTED_CREDENTIAL

        assert REDACTED_QUERY_VALUE == REDACTED_CREDENTIAL
        for part in HIDDEN_KEY_PARTS:
            assert is_sensitive_query_key(part), f"{part} 在 tools/redaction 里算凭据，这里却不算"
        # 反方向：这边额外收的几项是有意为之，不是笔误
        assert set(HIDDEN_KEY_PARTS) <= {part for part in SENSITIVE_QUERY_KEY_PARTS} | {"api_key", "apikey"}
        assert is_sensitive_query_key("sig") is True  # `tools/upload_access.py` 的取件签名


class TestSensitiveQueryParamFilterMechanics:
    def test_filter_rewrites_args_and_keeps_format_intact(self) -> None:
        """改的是 args 不是 msg：动了 msg 会把 `%` 占位符改坏，一条日志变成一次 logging 异常"""
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "%s %s HTTP/%s" %d',
            args=("1.2.3.4:5", "GET", f"/x?token={SENTINEL_URL_JWT}", "1.1", 200),
            exc_info=None,
        )
        assert SensitiveQueryParamFilter().filter(record) is True
        assert record.msg == '%s - "%s %s HTTP/%s" %d'  # 格式串原样
        rendered = record.getMessage()  # 不许抛 TypeError
        assert SENTINEL_URL_JWT not in rendered
        assert rendered == f'1.2.3.4:5 - "GET /x?token={REDACTED_QUERY_VALUE} HTTP/1.1" 200'

    def test_filter_handles_preformatted_message(self) -> None:
        """没有 args 时才动 msg（有的调用方直接传拼好的串）"""
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=f'1.2.3.4:5 - "GET /x?token={SENTINEL_URL_JWT} HTTP/1.1" 200',
            args=(),
            exc_info=None,
        )
        assert SensitiveQueryParamFilter().filter(record) is True
        assert SENTINEL_URL_JWT not in record.getMessage()

    def test_install_is_idempotent(self, _isolated_uvicorn_loggers: None) -> None:
        """模块级已经装过一次；再调不应该叠加第二个"""
        assert install_sensitive_query_param_filter() == []
        for name in ACCESS_LOG_LOGGER_NAMES:
            matched = [f for f in logging.getLogger(name).filters if isinstance(f, SensitiveQueryParamFilter)]
            assert len(matched) == 1


class TestAccessLogFilterIsActuallyWiredIn:
    """§2.7：零件全对、调用点没了 —— 测试会全绿"""

    def test_module_import_installed_it_on_both_loggers(self) -> None:
        """`uvicorn.access` 管 HTTP，`uvicorn.error` 管 WebSocket 的 access 行 —— 少一个就漏一半"""
        assert set(ACCESS_LOG_LOGGER_NAMES) == {"uvicorn.access", "uvicorn.error"}
        for name in ACCESS_LOG_LOGGER_NAMES:
            assert any(isinstance(f, SensitiveQueryParamFilter) for f in logging.getLogger(name).filters), name

    def test_install_call_site_exists(self) -> None:
        """AST 契约：模块级真的调了安装函数（把那一行删掉，行为用例可能仍绿——因为进程里已经装过）"""
        tree = ast.parse(LOGGER_SOURCE.read_text(encoding="utf-8"))
        module_level_calls = [
            node
            for node in tree.body
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "install_sensitive_query_param_filter"
        ]
        assert len(module_level_calls) == 1

    def test_filter_survives_uvicorn_dictconfig(self, _isolated_uvicorn_loggers: None) -> None:
        """uvicorn 启动时会跑一次 `dictConfig`，它**清 handlers 但不清 filters**

        这是「装在 logger 上而不是 handler 上」的全部理由；哪天这条前提不成立，这里先红。
        """
        logging.config.dictConfig(NONEBOT_UVICORN_LOGGING_CONFIG)
        for name in ACCESS_LOG_LOGGER_NAMES:
            assert any(isinstance(f, SensitiveQueryParamFilter) for f in logging.getLogger(name).filters), name


class TestUvicornAccessLogNeverCarriesUrlCredentials:
    """端到端：按 uvicorn 的真实调用形态打日志，看产品 sink 收到什么"""

    def test_http_and_websocket_access_lines_are_masked(self, _isolated_uvicorn_loggers: None) -> None:
        logging.config.dictConfig(NONEBOT_UVICORN_LOGGING_CONFIG)
        text = _emit_through_loguru([UVICORN_HTTP_CALL, UVICORN_WS_CALL])

        assert SENTINEL_URL_JWT not in text
        # 反方向：这两条访问日志**确实被打出来了**，而且排障字段一个不少
        assert text.count(REDACTED_QUERY_VALUE) == 2
        assert '"GET /api/emails/12/attachment/a.png?token=' in text
        assert '"WebSocket /api/workspaces/3/sandbox/terminal?token=' in text
        assert "203.0.113.7:51234" in text  # 客户端地址
        assert 'HTTP/1.1" 200' in text  # 状态码
        assert "[accepted]" in text

    def test_upload_signature_is_masked(self, _isolated_uvicorn_loggers: None) -> None:
        """`?sig=` 是**跨租户**的取件凭据（`tools/upload_access.py`），与 `?token=` 同一条出路"""
        logging.config.dictConfig(NONEBOT_UVICORN_LOGGING_CONFIG)
        text = _emit_through_loguru(
            [
                (
                    "uvicorn.access",
                    '%s - "%s %s HTTP/%s" %d',
                    (
                        "203.0.113.7:51236",
                        "GET",
                        f"/api/common/uploads/onebot_v11-ob10001-group_1/a.png?sig={SENTINEL_UPLOAD_SIG}",
                        "1.1",
                        200,
                    ),
                ),
            ],
        )
        assert SENTINEL_UPLOAD_SIG not in text
        assert "/api/common/uploads/onebot_v11-ob10001-group_1/a.png?sig=" in text

    def test_operational_query_params_survive(self, _isolated_uvicorn_loggers: None) -> None:
        """遮蔽不许把访问日志变成一堆掩码：非凭据参数逐字保留"""
        logging.config.dictConfig(NONEBOT_UVICORN_LOGGING_CONFIG)
        text = _emit_through_loguru(
            [
                (
                    "uvicorn.access",
                    '%s - "%s %s HTTP/%s" %d',
                    (
                        "172.19.0.2:36424",
                        "POST",
                        "/api/ext/rpc_exec?container_key=sandbox_x&from_chat_key=onebot_v11-ob10001-group_1",
                        "1.1",
                        200,
                    ),
                ),
            ],
        )
        assert "container_key=sandbox_x" in text
        assert "from_chat_key=onebot_v11-ob10001-group_1" in text
        assert REDACTED_QUERY_VALUE not in text

    def test_contrast_group_without_the_filter_really_leaks(self, _isolated_uvicorn_loggers: None) -> None:
        """摘掉过滤器 = 修复不存在。没有这一条，上面那些 0 可能只是「这条输入下测不出差异」（§2.11）"""
        logging.config.dictConfig(NONEBOT_UVICORN_LOGGING_CONFIG)
        for name in ACCESS_LOG_LOGGER_NAMES:
            std_logger = logging.getLogger(name)
            std_logger.filters = [f for f in std_logger.filters if not isinstance(f, SensitiveQueryParamFilter)]

        text = _emit_through_loguru([UVICORN_HTTP_CALL, UVICORN_WS_CALL])
        assert text.count(SENTINEL_URL_JWT) == 2


# ============================================================
# 10. 同一类形态的第三个出口：**出站** URL 上的凭据
# ============================================================


class TestOutboundUrlCredentialIsNotLogged:
    """`adapters/wechatpad/http_client.py`：鉴权 key 走 **query parameter**

    这条与 §9 是同一种形态、反方向（出站）。一次上游 4xx 就会把
    `?key=<WECHATPAD_AUTH_KEY>` 写进 `app.log` —— 而且**三处**都带它：
    httpx 的 `HTTPStatusError` 消息里含 `for url '<完整 URL>'`、`e.request.url` 本身、
    以及上游回显的响应体。`diagnose` 开关管不到它（它是 message 本身，不是诊断行）。

    用真的 `WeChatPadClient._request` + `httpx.MockTransport` 驱动：
    被测的那段 except 逻辑一行没换掉，只把网络换成内存。
    """

    SENTINEL_AUTH_KEY = "S3ntinel-WeChatPad-AuthKey-Zz"

    async def _drive(self) -> str:
        import httpx

        from nekro_agent.adapters.wechatpad.config import WeChatPadConfig
        from nekro_agent.adapters.wechatpad.http_client import WeChatPadClient

        config = WeChatPadConfig()
        config.WECHATPAD_AUTH_KEY = self.SENTINEL_AUTH_KEY
        config.WECHATPAD_API_URL = "http://pad.local"
        client = WeChatPadClient(config)

        def _handler(request: "httpx.Request") -> "httpx.Response":
            # 上游把收到的 key 抄回报错体里 —— 这是 #39 实测过的真实形态
            return httpx.Response(401, text=f'{{"Code":401,"Text":"invalid key: {self.SENTINEL_AUTH_KEY}"}}')

        client._http_client = httpx.AsyncClient(  # noqa: SLF001
            base_url="http://pad.local",
            transport=httpx.MockTransport(_handler),
        )

        captured: List[Any] = []
        handler_id = logger.add(captured.append, level="DEBUG", format="{message}", colorize=False)
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await client._request("GET", "/login/GetLoginStatus")  # noqa: SLF001
        finally:
            logger.remove(handler_id)
        return "".join(str(message) for message in captured)

    async def test_auth_key_never_reaches_the_sink(self) -> None:
        text = await self._drive()
        assert self.SENTINEL_AUTH_KEY not in text
        # 反方向：这条报错**确实打出来了**，排障信息一个不少
        assert "HTTP error occurred" in text
        assert "401" in text
        assert "/login/GetLoginStatus" in text
        assert "key=" in text  # 参数名还在，只有取值被换掉
        assert "[redacted_credential]" in text


class TestLarkLogBridgeIsRedacted:
    """第四个出口：`adapters/feishu/client.py` 把 lark-oapi 的 stdlib 日志**桥进** loguru

    `lark_oapi/core/http/transport.py` 的 DEBUG 行里 `headers: {...}` 是整份请求头，
    含 `Authorization: Bearer <tenant_access_token>`；而 `_setup_lark_logging()` 特意把
    这个 logger 设成 DEBUG 再接到项目 logger 上。与 §7 的 `deps.py` 同一档
    （`APP_LOG_LEVEL=DEBUG` 时是现状）。

    直接驱动 `_LarkLogHandler.emit`，不 mock 被测逻辑。
    """

    SENTINEL_LARK_TOKEN = "t-g1S3ntinelLarkTenantAccessTokenZz"

    def _drive(self, message: str) -> str:
        from nekro_agent.adapters.feishu.client import _LarkLogHandler  # noqa: PLC2701

        record = logging.LogRecord(
            name="Lark",
            level=logging.DEBUG,
            pathname=__file__,
            lineno=1,
            msg=message,
            args=(),
            exc_info=None,
        )
        captured: List[Any] = []
        handler_id = logger.add(captured.append, level="DEBUG", format="{message}", colorize=False)
        try:
            _LarkLogHandler().emit(record)
        finally:
            logger.remove(handler_id)
        return "".join(str(item) for item in captured)

    def test_tenant_access_token_is_masked(self) -> None:
        # lark_oapi/core/http/transport.py:39 的真实形态
        text = self._drive(
            "POST https://open.feishu.cn/open-apis/im/v1/messages 200, "
            f'headers: {{"Authorization": "Bearer {self.SENTINEL_LARK_TOKEN}"}}, '
            'params: {}, body: {"msg_type": "text"}',
        )
        assert self.SENTINEL_LARK_TOKEN not in text
        # 反方向：这条日志**确实转发了**，排障信息还在
        assert "[Lark]" in text
        assert "/open-apis/im/v1/messages" in text
        assert "200" in text
        assert "[redacted_credential]" in text

    def test_ping_filter_still_works(self) -> None:
        """既有行为不许改：ping/pong 心跳仍然被丢掉"""
        assert self._drive("ping") == ""
        assert self._drive("PONG received") == ""
