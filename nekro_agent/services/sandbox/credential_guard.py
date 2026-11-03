"""沙盒 RPC 边界的凭据守卫

## 威胁模型

沙盒里跑的是模型生成的代码，而模型的输入（聊天内容、外部资料）由租户完全控制。
因此必须假设：**沙盒里的代码是敌对的，它会主动尝试把平台方的模型 API Key 带出去**。

平台方运营共用版 Bot，同时给客户提供定制版实例。客户能左右自己实例里的 AI，
但永远不能拿到平台方 `MODEL_GROUPS[*].API_KEY`、数据库口令、JWT 密钥这些凭据。
沙盒 -> 服务端只有 `/ext/rpc_exec` 这一条回调通道，所以这条通道就是唯一的收口点。

## 本模块提供两道正交的防线

### 1. 调用许可（`SandboxCallGrant`）——入口白名单

历史实现里，沙盒只要持有全局 `RPC_SECRET_KEY` 就能：

- 调用**任意**已加载插件的沙盒方法，包括当前会话根本没有启用的插件；
- 把 `from_chat_key` 换成别的频道，以别人的身份执行方法。

改为：每次生成 `api_caller.py` 时为该容器签发一份一次性许可，内容是
「这个容器专属的随机令牌 + 它绑定的 `from_chat_key` + 本轮真正写进容器的方法名集合」。
RPC 入口只认许可，于是

- 令牌不再是全局共享的，泄漏范围收敛到单个容器、单轮运行；
- 方法白名单 = 实际下发给 AI 的那份清单，**新增插件方法不会意外扩大攻击面**，
  因为白名单是从下发动作本身派生的，不需要谁记得去维护一张名单；
- 换 `chat_key` 越权被绑定关系直接挡掉。

许可按沙盒运行超时自动过期，避免旧容器的令牌长期有效，也避免映射随频道数单向增长。

### 2. 出口脱敏（`redact_sandbox_payload`）——返回值与异常兜底

`services/rpc_service.execute_rpc_method` 会把插件抛出的异常 `str(e)` 原样回传，
沙盒再 `print` 出来喂给模型。实测存在的泄漏形态：

- 绘图插件把 `?key={API_KEY}` 拼进 URL，httpx 的 `HTTPStatusError` 文案含完整 URL；
- 各类插件 `raise Exception(f"...: {e}")` 把上游 `BASE_URL`、代理串带回去。

逐个插件打补丁是打不完的（插件还能由客户自行安装），所以在 RPC 出口做统一拦截：
把当前所有已注册配置里判定为凭据的取值收集起来，凡是要发回沙盒的字符串命中即替换。
这是兜底而非唯一防线——它拦的是「凭据原文」，不负责阻止插件泄漏别的东西。
"""

from __future__ import annotations

import re
import secrets
import time
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from pydantic import BaseModel

from nekro_agent.core import config
from nekro_agent.core.core_utils import ConfigBase, ConfigManager
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv, resolve_deployment_onebot_access_token
from nekro_agent.tools.redaction import REDACTED_CREDENTIAL

logger = get_sub_logger("sandbox_guard")


# =============================================================================
# 1. 调用许可
# =============================================================================


class SandboxCallGrant(BaseModel):
    """某个沙盒容器在本轮运行中被授予的 RPC 调用许可

    Attributes:
        token: 该容器专属的回调令牌，随 `api_caller.py` 下发进容器
        from_chat_key: 签发时绑定的会话标识，RPC 请求必须原样带回
        method_names: 本轮实际写进容器的沙盒方法名集合
        expire_at: 过期时间戳（秒）
        callback_count: 本轮运行里到达 `/ext/rpc_exec` 的回调次数。**统计的是「包进来了」，
            不是「鉴权过了」**：401 也算，因为它同样证明沙盒 -> NA 的网络路径是通的。
            用途见 `services/sandbox/runner.py`——「容器超时 + 全程零回调」是「回调链路不通」
            的判据，用来把这类故障与「模型写了个死循环」区分开
    """

    token: str
    from_chat_key: str
    method_names: FrozenSet[str]
    expire_at: float
    callback_count: int = 0


# container_key -> 许可。同一频道的容器键是稳定的，新一轮运行会直接覆盖旧值
_grants: Dict[str, SandboxCallGrant] = {}

# 许可有效期相对沙盒运行超时的余量（秒）：容器最长活到 SANDBOX_RUNNING_TIMEOUT，
# 余量覆盖启动、拉起与回调往返的时间偏差
_GRANT_EXPIRE_MARGIN_SECONDS = 120


def issue_sandbox_call_grant(container_key: str, from_chat_key: str, method_names: Iterable[str]) -> str:
    """为一个沙盒容器签发本轮运行的调用许可

    Args:
        container_key: 沙盒容器键，回调时原样带回
        from_chat_key: 该容器绑定的会话标识
        method_names: 本轮写进容器的沙盒方法名

    Returns:
        该容器专属的回调令牌
    """
    now = time.time()
    _prune_expired_grants(now)

    token = secrets.token_urlsafe(32)
    _grants[container_key] = SandboxCallGrant(
        token=token,
        from_chat_key=from_chat_key,
        method_names=frozenset(method_names),
        expire_at=now + max(config.SANDBOX_RUNNING_TIMEOUT, 0) + _GRANT_EXPIRE_MARGIN_SECONDS,
    )
    return token


def resolve_sandbox_call_grant(container_key: str, token: str, from_chat_key: str) -> Optional[SandboxCallGrant]:
    """校验回调身份并取出对应许可

    三个要素必须同时对上：容器键存在且未过期、令牌一致、会话标识一致。
    令牌比较使用常量时间比较，避免按字节爆破。

    Args:
        container_key: 请求携带的容器键
        token: 请求携带的回调令牌
        from_chat_key: 请求携带的会话标识

    Returns:
        校验通过的许可；任一要素不匹配时返回 None
    """
    grant = _grants.get(container_key)
    if not grant:
        return None
    if grant.expire_at <= time.time():
        _grants.pop(container_key, None)
        return None
    if not secrets.compare_digest(grant.token, token):
        return None
    if grant.from_chat_key != from_chat_key:
        return None
    return grant


def note_sandbox_callback(container_key: str) -> None:
    """记下「这个容器的一次回调到达了 NA」

    在鉴权**之前**调用，与请求是否合法无关：这个计数唯一的用途是判定沙盒 -> NA 的网络路径
    通不通，而一个被 401 挡下的请求同样证明包送到了。

    Args:
        container_key: 请求携带的容器键
    """
    grant = _grants.get(container_key)
    if grant is not None:
        grant.callback_count += 1


def sandbox_callback_count(container_key: str) -> int:
    """本轮运行里该容器已经发起过多少次回调

    Args:
        container_key: 沙盒容器键

    Returns:
        回调次数；许可不存在（未签发或已过期回收）时为 0
    """
    grant = _grants.get(container_key)
    return grant.callback_count if grant else 0


def revoke_sandbox_call_grant(container_key: str) -> None:
    """撤销指定容器的调用许可"""
    _grants.pop(container_key, None)


def _prune_expired_grants(now: float) -> None:
    for key in [key for key, grant in _grants.items() if grant.expire_at <= now]:
        _grants.pop(key, None)


# =============================================================================
# 2. 出口脱敏
# =============================================================================

# 与 WebUI / API 出口共用同一个掩码标记，便于排查时一眼认出是哪层拦下的
REDACTED_PLACEHOLDER = REDACTED_CREDENTIAL

# 这里刻意**只做明文字面量替换**，不套用 `tools/redaction.redact_credentials` 的形态正则。
# 形态正则（`sk-xxx`、`password: xxx`）服务的是「给管理员看的日志与 API 响应」，
# 那里宁可多遮一点；而沙盒方法的返回值里流动的是用户自己的内容——
# 用户让 AI 记一条「我家 wifi 密码是 xxx」的笔记，再读回来时不该变成掩码。
# 平台凭据的集合是从活的配置树里现取的，本身就是权威且完整的，不需要靠正则去猜。
#
# 短取值不参与匹配：配置里随手填的 "1234"、"test" 之类会把正常文本打成马赛克，
# 而任何真实凭据都远长于这个阈值
_MIN_SECRET_LENGTH = 12

# 字段名判定：`is_secret` 元数据只覆盖 WebUI 需要打码的字段，
# 像 `ModelConfigGroup.API_KEY` 这种嵌套模型里的凭据并没有标记，必须按名字兜住
_SECRET_FIELD_NAME_PATTERN = re.compile(
    r"(API_?KEY|ACCESS_?KEY|SECRET|PASSWORD|PASSWD|TOKEN|CREDENTIAL|PRIVATE_?KEY|AUTH_?KEY)",
    re.IGNORECASE,
)

# 配置树与返回值都可能自引用，限定深度避免病态结构打爆栈
_MAX_WALK_DEPTH = 8

# 进程级凭据：这些不在配置树里，但同样绝不能出现在发回沙盒的内容中。
#
# ⚠️ **这是一份手工清单，`OsEnv` 上每新增一个凭据类字段都必须同时登记进来**，
# 漏登记不会有任何运行时信号（脱敏静默少遮一个值）。看门人是
# `tests/test_sandbox_credential_isolation.py::TestEveryOsEnvSecretIsRegistered`：
# 它按 `_SECRET_FIELD_NAME_PATTERN` 扫 `OsEnv` 的全部字符串字段，少一个就红。
# `CREDENTIAL_SECRET_KEY` 就曾漏登记，是被那条用例补回来的。
_OS_ENV_SECRET_ATTRS: Tuple[str, ...] = (
    "POSTGRES_PASSWORD",
    "QDRANT_API_KEY",
    "JWT_SECRET_KEY",
    "JWT_REFRESH_SECRET_KEY",
    "SUPER_ACCESS_KEY",
    "RPC_SECRET_KEY",
    "WEBHOOK_SECRET_KEY",
    "ADMIN_PASSWORD",
    # 模型组 `api_key` 的静态加密主密钥（`tools/credential_cipher.py`）。
    # 它比这张表里任何一项都值钱：拿到它 + 一份库 dump = 全部租户与平台方的 API Key。
    # 走环境变量那一档（`.env*` / compose 里配了 NEKRO_CREDENTIAL_SECRET_KEY）时它就是
    # 一个进程级字符串，与 JWT 密钥同类，必须一并纳入出口脱敏
    "CREDENTIAL_SECRET_KEY",
)


def collect_platform_secrets() -> FrozenSet[str]:
    """收集当前进程内所有应视为平台凭据的取值

    覆盖面来自三处：

    - `OsEnv` 上的部署级密钥（数据库口令、JWT、RPC/Webhook 密钥等）；
    - `ConfigManager` 里注册的**全部**配置实例——核心配置、各适配器配置、各插件配置。
      逐个字段判断是否为凭据：显式标了 `is_secret`，或字段名命中凭据命名模式。
      嵌套模型（`CoreConfig.MODEL_GROUPS` 下的 `ModelConfigGroup`）会递归下钻，
      所以新增模型组、新增插件配置都自动纳入，无需在此登记；
    - 本进程里已经解析过的实例级插件配置（`plugin_scope._scoped_config_cache` 里值不为 None
      的合并结果）。这是兜底：实例覆盖不注册进 `ConfigManager`，默认参数的调用方看不到它。
      指定了实例的出口应当用 `collect_instance_plugin_secrets()` 精确取，而不是依赖这里
      「碰巧解析过」。

    不做缓存：一次遍历只是几百次字段访问，而缓存会让「刚在 WebUI 改完密钥」
    出现一段不受保护的窗口期。

    Returns:
        凭据原文集合
    """
    secret_values: Set[str] = set()

    for attr in _OS_ENV_SECRET_ATTRS:
        _collect_string(getattr(OsEnv, attr, None), secret_values)
    # 走解析函数而不是模块常量：裸机部署上 `os.environ` 里根本没有这个键
    # （`run_bot.py` 只把 `NEKRO_` 前缀的键导进去），常量恒为空串，
    # 于是部署级令牌在裸机上从来没被纳入过脱敏范围。见 os_env 里那个函数的 docstring。
    _collect_string(resolve_deployment_onebot_access_token(), secret_values)

    for config_key in ConfigManager.get_all_config_keys():
        instance = ConfigManager.get_config(config_key)
        if instance is None:
            continue
        try:
            _walk_config(instance, is_secret_field=False, depth=0, out=secret_values)
        except Exception as e:  # 单个插件配置异常不应让整条防线失效
            logger.warning(f"收集配置 `{config_key}` 的凭据字段失败，该配置未纳入脱敏范围: {e}")

    from nekro_agent.services.plugin import scope as plugin_scope

    # 先拷一份再遍历：遍历期间可能有别的协程解析出新实例配置并写入缓存
    for cached_config in list(plugin_scope._scoped_config_cache.values()):  # noqa: SLF001
        if cached_config is None:
            continue
        try:
            _walk_config(cached_config, is_secret_field=False, depth=0, out=secret_values)
        except Exception as e:
            logger.warning(f"收集实例级插件配置的凭据字段失败，该配置未纳入脱敏范围: {e}")

    return frozenset(secret_values)


def collect_instance_plugin_secrets(instance_key: str) -> FrozenSet[str]:
    """收集某个实例下所有已加载插件的生效配置里的凭据

    实例级覆盖不注册进 `ConfigManager`，所以 `collect_platform_secrets()` 看不到它；
    沙盒方法按实例读到的值必须在这里单独收一遍，否则会原样回到沙盒。
    空标识表示单实例 / 无实例，此时读到的就是全局配置，已由 `collect_platform_secrets()` 覆盖。

    Args:
        instance_key: 频道所属实例标识

    Returns:
        凭据原文集合
    """
    if not instance_key:
        return frozenset()

    # 延迟导入：插件收集器会拉起整套插件加载链路，模块级导入会成环
    from nekro_agent.services.plugin.collector import plugin_collector
    from nekro_agent.services.plugin.scope import resolve_scoped_config

    secret_values: Set[str] = set()
    for plugin in list(plugin_collector.loaded_plugins.values()):
        config_cls = plugin.config_class
        if config_cls is ConfigBase:
            # 没注册配置类的插件没有可读的凭据字段，读全局配置反而会创建一份空配置
            continue
        try:
            scoped_config = resolve_scoped_config(plugin.key, config_cls, plugin.get_global_config(), instance_key)
            _walk_config(scoped_config, is_secret_field=False, depth=0, out=secret_values)
        except Exception as e:  # 单个插件异常不应让整条防线失效
            logger.warning(f"收集插件 `{plugin.key}` 在实例 `{instance_key}` 下的凭据字段失败，该插件未纳入脱敏范围: {e}")

    return frozenset(secret_values)


def _collect_string(value: Any, out: Set[str]) -> None:
    if isinstance(value, str) and len(value) >= _MIN_SECRET_LENGTH:
        out.add(value)


def _walk_config(value: Any, is_secret_field: bool, depth: int, out: Set[str]) -> None:
    """遍历配置树，把凭据字段的取值收进 out

    Args:
        value: 当前节点
        is_secret_field: 当前节点是否处于一个被判定为凭据的字段之下
        depth: 当前深度
        out: 收集结果
    """
    if depth > _MAX_WALK_DEPTH:
        return

    if isinstance(value, ConfigBase):
        model_fields = type(value).model_fields
        for field_name, field_value in value:
            field_info = model_fields.get(field_name)
            extra = getattr(field_info, "json_schema_extra", None) if field_info is not None else None
            marked_secret = bool(isinstance(extra, dict) and extra.get("is_secret"))
            name_matched = bool(_SECRET_FIELD_NAME_PATTERN.search(field_name))
            _walk_config(field_value, marked_secret or name_matched, depth + 1, out)
        return

    if isinstance(value, dict):
        for sub_value in value.values():
            _walk_config(sub_value, is_secret_field, depth + 1, out)
        return

    if isinstance(value, (list, tuple, set, frozenset)):
        for sub_value in value:
            _walk_config(sub_value, is_secret_field, depth + 1, out)
        return

    if is_secret_field:
        _collect_string(value, out)


def redact_sandbox_text(text: str, secret_values: Optional[FrozenSet[str]] = None) -> str:
    """把文本中出现的平台凭据替换为占位符

    Args:
        text: 原始文本
        secret_values: 凭据集合，省略时现场收集

    Returns:
        脱敏后的文本
    """
    values = collect_platform_secrets() if secret_values is None else secret_values
    redacted = text
    for secret in values:
        if secret in redacted:
            redacted = redacted.replace(secret, REDACTED_PLACEHOLDER)
    return redacted


def redact_sandbox_payload(value: Any, secret_values: Optional[FrozenSet[str]] = None) -> Any:
    """对即将发回沙盒的返回值做递归脱敏

    容器类型按结构重建，字符串逐个替换。无法结构化处理的自定义对象走 `repr` 探测：
    只要 `repr` 里出现凭据就整体替换成占位符——正常插件的返回值不该带着平台凭据，
    宁可让这一次调用失去返回值，也不能把密钥送进模型上下文。

    Args:
        value: 待发回沙盒的返回值
        secret_values: 凭据集合，省略时现场收集

    Returns:
        脱敏后的返回值
    """
    values = collect_platform_secrets() if secret_values is None else secret_values
    if not values:
        return value
    return _redact_value(value, values, depth=0)


def _redact_value(value: Any, secret_values: FrozenSet[str], depth: int) -> Any:
    if depth > _MAX_WALK_DEPTH:
        return REDACTED_PLACEHOLDER

    if value is None or isinstance(value, (bool, int, float, bytes, bytearray)):
        return value

    if isinstance(value, str):
        redacted = redact_sandbox_text(value, secret_values)
        if redacted != value:
            logger.error("沙盒 RPC 返回值中检出平台凭据，已拦截并替换为占位符")
        return redacted

    if isinstance(value, dict):
        return {
            _redact_value(key, secret_values, depth + 1): _redact_value(sub_value, secret_values, depth + 1)
            for key, sub_value in value.items()
        }

    if isinstance(value, (list, tuple, set, frozenset)):
        redacted_items: List[Any] = [_redact_value(item, secret_values, depth + 1) for item in value]
        try:
            # 保持容器类型不变，避免脱敏顺带改变插件返回值的形状
            return type(value)(redacted_items)
        except TypeError:
            # namedtuple 之类不接受单个可迭代对象的子类，退回基础类型
            return redacted_items if isinstance(value, list) else tuple(redacted_items)

    if _contains_secret_repr(value, secret_values):
        logger.error(f"沙盒 RPC 返回对象 `{type(value).__name__}` 中检出平台凭据，已整体拦截")
        return REDACTED_PLACEHOLDER
    return value


def _contains_secret_repr(value: Any, secret_values: FrozenSet[str]) -> bool:
    """用 repr 兜底探测自定义对象里的凭据

    repr 可能抛异常或极长；异常时保守判定为「含凭据」，让调用方拦下来。
    """
    try:
        text = repr(value)
    except Exception:
        return True
    return any(secret in text for secret in secret_values)
