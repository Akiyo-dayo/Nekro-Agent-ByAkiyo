import copy
import json
import logging
import re
import sys
import traceback as traceback_module
from asyncio import Queue
from collections import deque
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import (
    Any,
    AsyncGenerator,
    Dict,
    FrozenSet,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Set,
    Tuple,
)

from loguru import logger

from .config import config
from .os_env import (
    APP_LOG_DIR,
    FACTORY_DEFAULT_POSTGRES_PASSWORD,
    LEGACY_EXAMPLE_JWT_SECRETS,
    SECRET_PLACEHOLDER_PREFIX,
    OsEnv,
)

# 内存中保存最近的日志记录
log_records = deque(maxlen=1000)
# 订阅者队列
subscribers: List[Queue] = []
# 记录所有出现过的日志来源
log_sources: Set[str] = set()


def format_log_entry(record: Dict) -> Dict:
    """格式化日志条目"""
    # 记录日志来源
    log_sources.add(record["name"])

    extra = record.get("extra") or {}
    record_exception = record.get("exception")
    traceback_text: Optional[str] = None
    exception_type: Optional[str] = None
    exception_message: Optional[str] = None

    if record_exception:
        exception_type = getattr(record_exception.type, "__name__", None)
        exception_value = record_exception.value
        exception_message = str(exception_value) if exception_value is not None else None
        traceback_text = "".join(
            traceback_module.format_exception(
                record_exception.type,
                exception_value,
                record_exception.traceback,
            )
        ).strip() or None

    return {
        "timestamp": datetime.fromtimestamp(record["time"].timestamp()).strftime("%Y-%m-%d %H:%M:%S"),
        "level": record["level"].name,
        "message": record["message"],
        "source": record["name"],
        "subsystem": extra.get("subsystem"),
        "plugin_key": extra.get("plugin_key"),
        "function": record["function"],
        "line": record["line"],
        "exception_type": exception_type,
        "exception_message": exception_message,
        "traceback": traceback_text,
    }


class LogInterceptHandler:
    """日志拦截处理器"""

    async def __call__(self, message):
        """处理日志消息"""
        record = message.record
        log_entry = format_log_entry(record)
        log_records.append(log_entry)
        log_json = json.dumps(log_entry)
        for queue in subscribers:
            await queue.put(f"{log_json}\n\n")


# 捕获未处理的异常处理
def exception_handler(
    _type: BaseException,
    value: BaseException,
    traceback: TracebackType,  # noqa: ARG001
):
    try:
        raise value  # noqa: TRY301
    except Exception:
        logger.exception("Uncaught exception occurred")


sys.excepthook = exception_handler


# =============================================================================
# loguru 的 `diagnose`：默认关，开发机显式打开
# =============================================================================
#
# `diagnose=True` 时 loguru 会在 traceback 的每一帧下面把**那一行源码里出现的每个名字
# 连同取值**渲染出来。这是 loguru 文档自己点名"生产环境应关闭，会带出敏感数据"的选项。
#
# 为什么它不是"再加一层脱敏就能解决"的问题：那份诊断文本是 loguru 直接从帧对象里
# `repr()` 出来的，**不经过任何插值点**，所以本项目已有的两条脱敏判据
# （`services/agent/upstream_redaction.py` 的按值/按形态、`tools/redaction.py` 的正则）
# 一条都作用不到它。#39 只能绕开它——LLM 链路改用 stdlib `traceback.format_exception()`
# 自己渲染再整体脱敏。绕开是逐条的，而泄漏面是全进程的：
# 测试部署 `app.log` 里现存 50 处 `ConnectionParameters(user=…, password=…)`（数据库口令），
# 出处根本不在 LLM 链路，而是 tortoise/asyncpg 连库失败时的诊断行。
#
# 所以这里把开关做到 sink 上，一次性覆盖全进程。

_DIAGNOSE_TRUE_TOKENS = frozenset({"1", "true", "yes", "y", "on"})
_DIAGNOSE_FALSE_TOKENS = frozenset({"0", "false", "no", "n", "off"})


def resolve_log_diagnose(raw: Optional[str] = None) -> Tuple[bool, str]:
    """决定 loguru 的 `diagnose` 开关，并给出**理由**

    理由是返回值的一部分而不是可有可无的日志文案：这个函数有一个"认不出的取值就当关"
    的兜底分支，只断言"结果是 False"的话任何输入都满足它，那一侧的信息量为零
    （§6：断言兜底结果时必须一并断言兜底的理由）。

    Args:
        raw: `NEKRO_LOG_DIAGNOSE` 的原始取值；缺省时读 `OsEnv.LOG_DIAGNOSE`
            （调用时读、不在 import 期定死，测试才能 monkeypatch，见 §2.9）

    Returns:
        `(是否开启, 一句话理由)`
    """
    value = OsEnv.LOG_DIAGNOSE if raw is None else raw
    token = (value or "").strip().lower()

    if not token:
        return False, "NEKRO_LOG_DIAGNOSE 未设置，按默认关闭"
    if token in _DIAGNOSE_TRUE_TOKENS:
        return True, f"NEKRO_LOG_DIAGNOSE={value.strip()!r} 显式开启"
    if token in _DIAGNOSE_FALSE_TOKENS:
        return False, f"NEKRO_LOG_DIAGNOSE={value.strip()!r} 显式关闭"
    # 认不出的取值一律当关：写错开关的人想要的是"开"，但猜错的代价不对称——
    # 猜成开 = 口令进日志，猜成关 = 少几行局部变量。
    return False, f"NEKRO_LOG_DIAGNOSE={value.strip()!r} 无法识别，按默认关闭"


_LOG_FORMAT = (
    "<g>{time:MM-DD HH:mm:ss}</g> "
    "[<lvl>{level}</lvl>] "
    "<c><u>{name}</u></c> | "
    "<c>{function}:{line}</c>| "
    "{message}"
)


def build_log_handlers(diagnose: bool) -> List[Dict[str, Any]]:
    """构造三个 sink 的配置

    `diagnose` 必须逐个 sink 显式给出：`logger.configure(handlers=[...])` 会把每一项
    原样交给 `logger.add()`，没写这个键就取 loguru 的默认值 **True**。
    `backtrace` 同样显式写出 True —— 它管的是"扩展调用栈"，是关掉 `diagnose` 之后
    仍要保住的排障能力，显式写出来才能在测试里钉住"没有把整条 traceback 一起吞掉"。

    Args:
        diagnose: 是否让 loguru 在 traceback 里渲染局部变量

    Returns:
        `logger.configure(handlers=...)` 用的三项配置：内存/SSE、stdout、`app.log`
    """
    return [
        {
            "sink": LogInterceptHandler(),
            "format": _LOG_FORMAT,
            "level": config.APP_LOG_LEVEL,
            "backtrace": True,
            "diagnose": diagnose,
        },
        {
            "sink": sys.stdout,
            "format": _LOG_FORMAT,
            "level": config.APP_LOG_LEVEL,
            "backtrace": True,
            "diagnose": diagnose,
        },
        {
            "sink": Path(APP_LOG_DIR) / "app.log",
            "format": _LOG_FORMAT,
            "level": config.APP_LOG_LEVEL,
            "backtrace": True,
            "diagnose": diagnose,
            "rotation": "100 MB",
            "retention": "10 days",
            "compression": "zip",
        },
    ]


# =============================================================================
# URL 上的凭据：uvicorn 的访问日志会把整条 query string 原样写进这三个 sink
# =============================================================================
#
# `diagnose` 管的是「异常 traceback 里的局部变量」，管不到这一条：uvicorn 每处理完一个
# 请求就打一行访问日志，里面的路径是 `get_path_with_query_string(scope)` ——
# **带完整 query string**。而本项目有两类凭据就走 query string：
#
# - `?token=<JWT>`：`services/user/deps.py:get_current_user` 支持从 URL 取令牌，
#   浏览器侧凡是设不了请求头的场景都用它（`services/api/emails.ts:withAuthToken`
#   的附件/预览 URL、`/workspaces/{id}/sandbox/terminal` 的终端 WebSocket）。
#   拿到就等于免登录，`/logs/stream` 这一档还是 `Role.Super`。
# - `?sig=<HMAC>`：`tools/upload_access.py` 的取件签名，覆盖「某个频道的上传目录 +
#   过期时间」，是**跨租户**的能力凭据（测试部署 `app.log` 里现存 1415 条）。
#
# 它是 **INFO 级、无条件打**的，不像 `deps.py` 那条 DEBUG 只在开发机上出现；
# 三个 sink（内存/SSE、stdout、`app.log`）一个都跑不掉，`/logs/download` 会把它交出去，
# `app.log` 会随备份/工单外发。
#
# 修在 **stdlib logging 的 logger 上**而不是 loguru 的 sink 上，有两个理由：
# ① 这样连「nonebot 把 uvicorn 的 logger 桥到 loguru」这条路径都不依赖 ——
#    `--reload` 模式下 uvicorn 用的是自己那份 LOGGING_CONFIG（stdout），照样被遮；
# ② `logging.config.dictConfig()`（uvicorn 启动时会跑一次）**只清 handlers、不清 filters**，
#    所以这里在 import 期装上的过滤器活得过 uvicorn 的配置动作（有用例钉住这一条）。

# 判据：query 参数名**按整词**匹配，命中就把**取值**换成掩码。
# 旧写法是子串（`auth` 命中 author / authority，`sig` 命中 design / assign / signal，
# `token` 命中 max_tokens），于是访问日志里这些无辜参数的取值被白白遮掉，排障价值丢了；
# 整词匹配把参数名切成词（按 `_` `-` 等分隔符和 camelCase 的大小写边界）再逐词比对，
# 另有一份“连写”名单收 `accesstoken` / `apikey` 这类没有分隔符的常见写法。
# **与 `tools/redaction.py:HIDDEN_KEY_PARTS` 是同一份判据的镜像**——那边管「字典/文本对外
# 输出前脱敏」，这边管「访问日志」，判据分叉就等于其中一处悄悄失效。
# 不 import 那个模块是因为 `core/logger.py` 是全项目最早被导入的模块之一，
# 而 `tools.redaction` → `schemas.tenant` → `core.config` 这条链会把 import 顺序钉死；
# 由 `tests/test_log_diagnose_hygiene.py` 断言这边覆盖得住那边的每一项。
#
# 判据仍然**偏向多遮**（与 `nekro_log_hygiene.build_env_secret_shield` 同一条理由）：
# 多遮一个非凭据，代价是访问日志里少看见一个取值（键名与整条请求行都还在）；
# 漏遮一个凭据，代价是它进了每一份被打包、被 scp、被贴进工单的日志。
# 但**不收 `key` 这个裸词**：`chat_key` / `instance_key` / `container_key` 是访问日志里
# 用来对上号的主要维度，遮掉它们等于把这行日志的价值清零，而它们本身不是凭据。
SENSITIVE_QUERY_KEY_WORDS: FrozenSet[str] = frozenset(
    {
        "token",
        "secret",
        "password",
        "passwd",
        "pwd",
        "authorization",
        "auth",
        "credential",
        "credentials",
        "signature",
        "sig",
        "rkey",  # QQ 多媒体链接的取件凭据，NoneBot 的事件日志会带出原始链接
        "apikey",
    },
)
# 相邻若干个词连写后的名字（`api_key` -> `apikey`），以及没有分隔符的常见连写
SENSITIVE_QUERY_KEY_COMPOUNDS: FrozenSet[str] = frozenset(
    {
        "apikey",
        "accesskey",
        "secretkey",
        "privatekey",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "authtoken",
        "apitoken",
        "sessiontoken",
        "clientsecret",
        "tenantaccesstoken",
    },
)
_KEY_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_WORD_SEPARATOR = re.compile(r"[^a-z0-9]+")
_KEY_MAX_COMPOUND_WORDS = 3

# 与 `tools/redaction.py:REDACTED_CREDENTIAL` 逐字相同（同上，由测试钉住）
REDACTED_QUERY_VALUE = "[redacted_credential]"

# `?k=v` / `&k=v`。取值到 `&`、空白、引号为止 —— 访问日志的形态是
# `1.2.3.4:5678 - "GET /path?token=xxx HTTP/1.1" 200`，取值后面跟的就是空格。
_QUERY_PARAM_RE = re.compile(r"(?P<sep>[?&])(?P<key>[^?&=\s\"']+)=(?P<value>[^&\s\"']*)")

# uvicorn 打访问日志用的 logger 名。**两个都要**：
# HTTP 请求走 `uvicorn.access`（`protocols/http/h11_impl.py`），
# 而 **WebSocket 的 `[accepted]` / `403` 行走 `uvicorn.error`**
# （`protocols/websockets/websockets_impl.py` 里 `self.logger = getLogger("uvicorn.error")`）——
# 只装 `uvicorn.access` 的话，`?token=` 的那条终端 WebSocket 恰好是漏掉的那一个。
ACCESS_LOG_LOGGER_NAMES: Tuple[str, ...] = ("uvicorn.access", "uvicorn.error")


def _key_words(key: str) -> List[str]:
    spaced = _KEY_CAMEL_BOUNDARY.sub("_", key).lower()
    return [word for word in _KEY_WORD_SEPARATOR.split(spaced) if word]


def is_sensitive_query_key(key: str) -> bool:
    """query 参数名是否应视为凭据（大小写不敏感，按整词匹配，见上方说明）"""
    words = _key_words(key)
    if any(word in SENSITIVE_QUERY_KEY_WORDS for word in words):
        return True
    for start in range(len(words)):
        for stop in range(start + 1, min(len(words), start + _KEY_MAX_COMPOUND_WORDS) + 1):
            if "".join(words[start:stop]) in SENSITIVE_QUERY_KEY_COMPOUNDS:
                return True
    return False


def redact_sensitive_query_params(text: str) -> str:
    """把文本里 `?token=…` / `&sig=…` 这类取值换成掩码

    只动取值，不动参数名、路径、方法、状态码、客户端地址 —— 访问日志的排障价值全在那些字段上。

    Args:
        text: 任意文本，通常是一条访问日志的请求行

    Returns:
        取值已遮蔽的文本；没有敏感参数时原样返回
    """

    def _mask(match: "re.Match[str]") -> str:
        key = match.group("key")
        if not is_sensitive_query_key(key):
            return match.group(0)
        return f"{match.group('sep')}{key}={REDACTED_QUERY_VALUE}"

    return _QUERY_PARAM_RE.sub(_mask, text)


class SensitiveQueryParamFilter(logging.Filter):
    """stdlib logging 过滤器：在记录离开 logger 之前遮掉 URL 上的凭据

    装在 **logger** 上而不是 handler 上：handler 会被 `dictConfig` 换掉，filter 不会。

    只改 `record.args` 里的字符串项，`record.msg` 仅在**没有** args 时才动 ——
    uvicorn 传进来的 `msg` 是 `'%s - "%s %s HTTP/%s" %d'` 这种格式串，
    在有 args 的情况下动它会把 `%` 占位符改坏，把一条日志变成一次 logging 内部异常。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and args:
            record.args = tuple(redact_sensitive_query_params(item) if isinstance(item, str) else item for item in args)
        elif isinstance(args, dict) and args:
            record.args = {
                key: (redact_sensitive_query_params(value) if isinstance(value, str) else value)
                for key, value in args.items()
            }
        elif isinstance(record.msg, str):
            record.msg = redact_sensitive_query_params(record.msg)
        return True


def _text_of(exc: BaseException) -> str:
    try:
        return str(exc)
    except Exception:  # noqa: BLE001 - 日志路径上，坏掉的 __str__ 不能再炸一次
        return "<unprintable exception>"


def _clone_with_message(exc: BaseException, message: str) -> BaseException:
    """造一个“文本已脱敏”的异常副本；原异常不动

    先试 `copy.copy` + 改 `args`（保留原类型与属性）；构造参数和 `args` 对不上的异常复制不了，
    或者复制出来文本仍不对（自定义了 `__str__`），就退到一个同名同模块的占位类，保证日志里的类型名不变。
    """
    try:
        clone = copy.copy(exc)
        clone.args = tuple(redact_sensitive_query_params(arg) if isinstance(arg, str) else arg for arg in exc.args)
        if _text_of(clone) == message:
            return clone
    except Exception:  # noqa: BLE001, S110 - 复制失败走下面的兜底
        pass
    stand_in = type(type(exc).__name__, (Exception,), {"__module__": type(exc).__module__, "__qualname__": type(exc).__qualname__})
    return stand_in(message)


def redact_exception_for_log(exc: BaseException, _seen: Optional[Dict[int, BaseException]] = None) -> BaseException:
    """返回一个可以安全交给日志格式化的异常：文本里的 URL 凭据已遮，`__cause__` / `__context__` 链同样处理

    `traceback.format_exception` 会把异常消息和整条链上每个异常的消息原样打出来，而 httpx 一类的异常
    消息里就是完整链接（含 `rkey`）。没有任何需要遮的内容时原样返回同一个对象，有才造副本；
    **绝不就地改原异常**——调用方捕获后重新抛出、处理时拿到的必须还是原样。
    """
    seen = {} if _seen is None else _seen
    if id(exc) in seen:
        return seen[id(exc)]
    seen[id(exc)] = exc  # 先占位，防止 __context__ 成环

    text = _text_of(exc)
    redacted = redact_sensitive_query_params(text)
    cause = exc.__cause__
    context = exc.__context__
    new_cause = redact_exception_for_log(cause, seen) if cause is not None else None
    new_context = redact_exception_for_log(context, seen) if context is not None else None
    members = list(exc.exceptions) if isinstance(exc, BaseExceptionGroup) else []
    new_members = [redact_exception_for_log(member, seen) for member in members]

    unchanged = (
        redacted == text
        and new_cause is cause
        and new_context is context
        and all(new is old for new, old in zip(new_members, members))
    )
    if unchanged:
        return exc

    twin: BaseException
    if isinstance(exc, BaseExceptionGroup):
        try:
            twin = type(exc)(redact_sensitive_query_params(exc.message), new_members)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - 自定义了构造参数的异常组，退到 derive（消息原样）
            twin = exc.derive(new_members)  # type: ignore[arg-type]
    else:
        twin = _clone_with_message(exc, redacted)
    twin.__cause__ = new_cause
    twin.__context__ = new_context
    twin.__suppress_context__ = exc.__suppress_context__
    twin.__traceback__ = exc.__traceback__
    seen[id(exc)] = twin
    return twin


def scrub_sensitive_query_params(record: Any) -> None:
    """loguru 的 patcher：第三方经 loguru 打的日志也要遮

    上面的过滤器只管 stdlib 那条路。NoneBot 的事件日志走 loguru，内容是原始消息，
    QQ 的多媒体链接连同 `rkey` 都在里面。

    异常也要处理：sink 把 traceback 格式化出来的时候会带上异常消息与整条异常链，
    而 httpx 一类的异常消息里就是完整链接。这里把 `record["exception"]` 换成已脱敏的副本，
    三个 sink（内存/SSE、stdout、`app.log`）拿到的都是它。
    """
    record["message"] = redact_sensitive_query_params(record["message"])
    exception = record.get("exception")
    if exception is not None and exception.value is not None:
        value = redact_exception_for_log(exception.value)
        if value is not exception.value:
            record["exception"] = type(exception)(type(value), value, exception.traceback)


def install_sensitive_query_param_filter(logger_names: Tuple[str, ...] = ACCESS_LOG_LOGGER_NAMES) -> List[str]:
    """给访问日志的 logger 装上遮蔽过滤器

    幂等：已经装过就跳过（模块被重复 import、或测试反复调用时不会叠加）。

    Args:
        logger_names: 要装的 stdlib logger 名

    Returns:
        本次真正装上去的 logger 名
    """
    installed: List[str] = []
    for name in logger_names:
        std_logger = logging.getLogger(name)
        if any(isinstance(existing, SensitiveQueryParamFilter) for existing in std_logger.filters):
            continue
        std_logger.addFilter(SensitiveQueryParamFilter())
        installed.append(name)
    return installed


# =============================================================================
# 出厂默认凭据的启动期告警
# =============================================================================
#
# `NEKRO_POSTGRES_PASSWORD` 的出厂默认值逐字等于包名 `nekro_agent`，于是"扫一遍磁盘上
# 有没有口令泄漏"这件事对它**在结构上不成立**（测试部署上 1729 个文件命中，全部是正常文本）。
# 换句话说：这一条一旦没换，泄漏了也不会有任何告警——扫描器沉默不是因为安全，
# 是因为它分不出来。启动时响亮地说一次，是这条唯一可行的信号。
#
# 有意**不**拒绝启动：这不是一个能由代码替运维决定的事（开发机、CI、一次性 lab 都合法），
# 拒绝启动会把一条提醒变成一次生产事故。


class FactoryDefaultCheck(NamedTuple):
    """一条"这个凭据还是出厂默认值吗"的判据"""

    env_key: str
    factory_default: str
    alert: str


FACTORY_DEFAULT_CREDENTIAL_CHECKS: Tuple[FactoryDefaultCheck, ...] = (
    FactoryDefaultCheck(
        env_key="NEKRO_POSTGRES_PASSWORD",
        factory_default=FACTORY_DEFAULT_POSTGRES_PASSWORD,
        alert=(
            "⚠️ 数据库口令仍是出厂默认值（NEKRO_POSTGRES_PASSWORD）。"
            "它逐字等于包名，任何「扫一遍日志/备份里有没有口令」的工具都无法把它与正常文本区分开——"
            "也就是说它一旦泄漏，你不会从任何告警里知道。"
            "上线前必须改掉，见 docs/operations.md §1.3"
        ),
    ),
)


# -----------------------------------------------------------------------------
# JWT 两把签名密钥与 ADMIN 口令（#40 / docs/operations.md §1.1 §1.2，round-28）
# -----------------------------------------------------------------------------
#
# 与上面 Postgres 那条的**缺键语义相反，且是有意的**：
#
# - Postgres 没设 = 生效的正是出厂默认 → 告警；
# - JWT 没设（或为空）= `OsEnv` 兜底每进程随机 → 安全（代价只是重启后全员重登）→ 不告警；
# - ADMIN 口令没设 = admin 登录不可用，不是弱口令 → 不告警。
#
# 只在「设了、而且是示例值 / 占位符 / 过短」时告警；两把设成同一个值也告警（refresh 没有独立吊销能力）。
# 同样有意**不拒绝启动**：拒绝启动会把一条提醒变成一次生产事故（沿用 round-16 口径）。
# 告警只说键名与后果，不复述取值。

# docs/operations.md §1.1 的判据：长度 >= 32
JWT_SECRET_MIN_LENGTH = 32
# 安装脚本生成的是 16 位字母数字随机口令（约 95 位熵），不能被告警；
# 12 位以下的口令（含出厂示例 `admin`、某现网的 11 位）才算弱。docs/operations.md 建议的 >= 20 是上线判据，不是告警线。
ADMIN_PASSWORD_MIN_LENGTH = 12

_JWT_SECRET_KEYS: Tuple[str, str] = ("NEKRO_JWT_SECRET_KEY", "NEKRO_JWT_REFRESH_SECRET_KEY")
_ADMIN_PASSWORD_KEY = "NEKRO_ADMIN_PASSWORD"

_JWT_WEAK_ALERT = (
    "⚠️ JWT 签名密钥（{key}）是仓库示例值 / 占位符，或长度不足 " + str(JWT_SECRET_MIN_LENGTH) + " 位。"
    "拿到这把密钥的人可以直接自签任意账号（含 admin）的令牌，登录口令不再构成边界。"
    "上线前必须换掉，但换之前先保留旧密钥（存到只有你能读的地方）："
    "旧版 AES 加密的工作区资源载荷是用旧 NEKRO_JWT_SECRET_KEY 加密的，要靠旧密钥才能恢复（资源页「使用旧密钥恢复」），旧密钥丢了就恢复不了。"
    "然后换成 `openssl rand -hex 32` 生成的随机值（换后全部已签发令牌失效，要在维护窗口做），"
    "见 docs/operations.md §1.1"
)
_JWT_SAME_ALERT = (
    "⚠️ NEKRO_JWT_SECRET_KEY 与 NEKRO_JWT_REFRESH_SECRET_KEY 是同一个值：refresh 令牌失去独立吊销能力。"
    "两把必须各自随机生成，见 docs/operations.md §1.1"
)
_ADMIN_WEAK_ALERT = (
    "⚠️ 后台 admin 口令（NEKRO_ADMIN_PASSWORD）是示例占位符，或长度不足 " + str(ADMIN_PASSWORD_MIN_LENGTH) + " 位。"
    "它就是 admin 账号（Role.Super，含宿主级容器操作）的登录口令，没有第二个真相来源；"
    "上线前必须换成随机生成的强口令（`openssl rand -base64 24`），见 docs/operations.md §1.2"
)


def _is_placeholder(value: str) -> bool:
    return value.strip().upper().startswith(SECRET_PLACEHOLDER_PREFIX)


def _is_weak_jwt_secret(value: str) -> bool:
    return value in LEGACY_EXAMPLE_JWT_SECRETS or _is_placeholder(value) or len(value) < JWT_SECRET_MIN_LENGTH


def _is_weak_admin_password(value: str) -> bool:
    return _is_placeholder(value) or len(value) < ADMIN_PASSWORD_MIN_LENGTH


def _audit_weak_signing_secrets(effective: Mapping[str, str]) -> List[str]:
    """JWT 两把 + ADMIN 口令的告警文案。缺键 / 空串 / 纯空白一律跳过（见上面的缺键语义）"""
    alerts: List[str] = []
    jwt_values = {key: effective.get(key, "") for key in _JWT_SECRET_KEYS}
    for key, value in jwt_values.items():
        if value.strip() and _is_weak_jwt_secret(value):
            alerts.append(_JWT_WEAK_ALERT.format(key=key))
    access, refresh = jwt_values.values()
    if access.strip() and access == refresh:
        alerts.append(_JWT_SAME_ALERT)
    admin = effective.get(_ADMIN_PASSWORD_KEY, "")
    if admin.strip() and _is_weak_admin_password(admin):
        alerts.append(_ADMIN_WEAK_ALERT)
    return alerts


def audit_factory_default_credentials(values: Optional[Mapping[str, str]] = None) -> List[str]:
    """检查凭据是否仍是出厂默认值 / 示例值 / 弱值，返回要打出去的告警文案

    Args:
        values: `{环境变量名: 当前生效值}`。缺省时取 `OsEnv` 上已经解析好的生效值。
            **Postgres 口令缺键按"仍是默认值"处理**——环境变量没设时生效的正是那个默认值，
            把"没设"判成"安全"会让这条告警在最常见的部署形态上恰好不响。
            **JWT 两把与 ADMIN 口令缺键 / 空串则跳过**（没设 = 随机兜底 / 登录不可用，见上方注释）。

    Returns:
        命中的告警文案；全部已改则为空列表
    """
    effective: Mapping[str, str] = (
        {
            "NEKRO_POSTGRES_PASSWORD": OsEnv.POSTGRES_PASSWORD,
            _JWT_SECRET_KEYS[0]: OsEnv.JWT_SECRET_KEY,
            _JWT_SECRET_KEYS[1]: OsEnv.JWT_REFRESH_SECRET_KEY,
            _ADMIN_PASSWORD_KEY: OsEnv.ADMIN_PASSWORD,
        }
        if values is None
        else values
    )
    alerts = [
        check.alert
        for check in FACTORY_DEFAULT_CREDENTIAL_CHECKS
        if effective.get(check.env_key, check.factory_default) == check.factory_default
    ]
    return [*alerts, *_audit_weak_signing_secrets(effective)]


# 立即配置日志处理器
Path(APP_LOG_DIR).mkdir(parents=True, exist_ok=True)

LOG_DIAGNOSE_ENABLED, LOG_DIAGNOSE_REASON = resolve_log_diagnose()
log_handlers = build_log_handlers(LOG_DIAGNOSE_ENABLED)

logger.configure(handlers=log_handlers, patcher=scrub_sensitive_query_params)  # type: ignore

# 访问日志的 URL 凭据遮蔽：必须在 `nonebot.run()`（uvicorn 起服务、跑 dictConfig）之前装上。
# 本模块由插件加载阶段导入，早于 run，满足这个顺序。
install_sensitive_query_param_filter()

# 两侧都打一句：关着的时候要能在启动日志里**看见它确实关着**（上线前清单要 grep 这一行），
# 开着的时候要响亮到没人会以为那是默认状态。
if LOG_DIAGNOSE_ENABLED:
    logger.warning(
        f"loguru diagnose 已开启（{LOG_DIAGNOSE_REASON}）："
        "异常日志会逐帧渲染局部变量，其中可能包含数据库口令 / API Key / 请求体原文，"
        "且这些内容**不经过任何脱敏**。仅限开发环境，生产部署请置空或设为 false。",
    )
else:
    logger.info(f"loguru diagnose 已关闭（{LOG_DIAGNOSE_REASON}）：异常类型/消息/调用栈照常记录，不渲染局部变量")

for _factory_default_alert in audit_factory_default_credentials():
    logger.warning(_factory_default_alert)


async def get_log_records(
    page: int = 1,
    page_size: int = 100,
    source: Optional[str] = None,
    count_only: bool = False,
    subsystem: Optional[str] = None,
    plugin_key: Optional[str] = None,
) -> List[Dict] | int:
    """获取历史日志记录，默认返回最新的100条日志

    Args:
        page: 页码，从1开始
        page_size: 每页记录数
        source: 日志来源过滤
        count_only: 是否只返回计数
        subsystem: 子系统过滤（来自 logger.bind(subsystem=...)）
        plugin_key: 插件过滤（来自 logger.bind(plugin_key=...)）

    Returns:
        返回指定页的日志记录，按时间从新到旧排序
    """
    # 1. 转换 deque 到列表并过滤（此时是从旧到新的顺序）
    filtered_logs = [
        log
        for log in log_records
        if (not source or log["source"] == source) and (not subsystem or log.get("subsystem") == subsystem)
        and (not plugin_key or log.get("plugin_key") == plugin_key)
    ]
    # 如果只需要计数
    if count_only:
        return len(filtered_logs)
    # 2. 反转列表使其变成从新到旧的顺序
    filtered_logs = filtered_logs[::-1]

    # 分页（此时分页基于从新到旧排序的列表）
    start = (page - 1) * page_size
    end = start + page_size if page_size > 0 else None
    return filtered_logs[start:end][::-1]


async def get_log_sources() -> List[str]:
    """获取所有日志来源"""
    return sorted(log_sources)


async def subscribe_logs() -> AsyncGenerator[str, None]:
    """订阅日志流"""
    queue: Queue = Queue()
    subscribers.append(queue)
    try:
        while True:
            message = await queue.get()
            yield message
    finally:
        subscribers.remove(queue)


def get_sub_logger(subsystem: str, log_name: Optional[str] = None, **extra: Any):
    """获取带子系统标记的 logger（用于前端过滤）。

    注意：
    - 不会分流到独立日志文件（避免产生过多碎片日志）
    - 仅通过 record.extra 打标，供内存日志/接口过滤
    """
    subsystem = str(subsystem).strip()
    if not subsystem:
        return logger
    name = (log_name or subsystem).strip() or subsystem
    bound = logger.bind(subsystem=subsystem, **extra)

    def patch_record(record: Any) -> None:
        record["name"] = name

    return bound.patch(patch_record)
