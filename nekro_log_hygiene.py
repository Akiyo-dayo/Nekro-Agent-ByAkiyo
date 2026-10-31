"""启动期凭据卫生：不让 NoneBot 的 `Loaded Config` 把凭据打进日志文件。

## 这条泄漏是怎么来的

`nonebot.init()` 在建 driver 之前会把整份配置 dump 进一条 DEBUG 日志
（`nonebot/__init__.py`：`logger.opt(colors=True).debug(f"Loaded Config: {model_dump(config)}")`）。
NoneBot 的 `Config` 是 `extra="allow"` 的 pydantic 模型，**env 文件里的每一个键**都会作为
额外字段进入这份 dump（只认 env 文件，不认纯 os.environ 的键，见
`nonebot/config.py:DotEnvSettingsSource.__call__` 末尾那段 "remain user custom config"）。

于是 `.env.dev` 里的 `NEKRO_ADMIN_PASSWORD` / `NEKRO_JWT_SECRET_KEY` /
`NEKRO_JWT_REFRESH_SECRET_KEY` / `NEKRO_QDRANT_API_KEY` 全部以**明文**出现在
`LOG_LEVEL=DEBUG` 的启动日志里。实测（测试部署）：6 份历轮启动日志各命中 1 行，
就是那一条 1062 字节的 `Loaded Config`。JWT 密钥比后台口令更严重——拿到它可以直接签
一张 `Role.Super` 的票，根本不需要登录。

## 修法：pydantic 的 `SecretStr`，不是对日志文本做字符串替换

在 `nonebot.init()` **之前**把凭据类的 env 键以 `SecretStr` 作为初始化 kwargs 传进去
（`InitSettingsSource` 的优先级高于 env 文件，见 `BaseSettings._settings_build_values`
里的 `deep_update(env_settings(), init_settings())`），dump 出来就是
`'nekro_admin_password': SecretStr('**********')`。

选 `SecretStr` 而不是"事后从日志行里正则抠掉密码"的理由：后者是黑名单，必然漏——
它既不知道有哪些自定义键，也没法区分"口令"与"恰好长得像口令的正常文本"。
`SecretStr` 是**类型层**的标记，遮蔽发生在值被渲染成文本之前。

## 为什么还要 `restore_shielded_values()`

掩码只为挡住那一条启动日志。`driver.config` 上的这些值是**真的会被读**的：
`nonebot-adapter-onebot` 的 `Adapter.__init__` 会 `Config(**driver.config.model_dump())`，
其 `onebot_access_token` 字段声明是 `Optional[str]`，喂 `SecretStr` 进去要么校验失败、
要么让 `_check_access_token` 的字符串比较**恒不相等**（表现是所有协议端连接被 403，
而日志里只有一句 "Check access token failed"）。所以 `nonebot.init()` 一返回、
`register_adapter()` 之前必须原样还原成明文。

## 放在仓库根目录而不是 `nekro_agent/` 包里的原因

这段代码必须在 `nonebot.init()` **之前**跑，而 `import nekro_agent.<任何子模块>` 会先执行
`nekro_agent/__init__.py`，它一路导到 `nekro_agent/core/config.py` 的模块级
`CoreConfig.load_config()` → `load_config_to_env()`，那里要一个**已经初始化好的** driver。
提前导入的后果不是报错而是静默降级：`load_to_nonebot_env` 那批配置从此再也进不了
nonebot（模块已在 `sys.modules` 里，插件加载阶段不会再执行一遍）。

所以本模块只依赖 stdlib + pydantic + python-dotenv + `nonebot.config`（后者只是读一下
`Config` 上声明过的字段名，import 它不触发任何初始化），与 `run_bot.py` 同级。

## 这个模块**没有**覆盖的另外两条路（凭据进日志一共有三条，缺一不可）

**① loguru 的 `diagnose`。** `SecretStr` 遮的是「已知配置项被渲染成文本」这一条路。
它挡不住 `diagnose`——后者在异常 traceback 里逐帧渲染**局部变量**，那份文本由 loguru
直接从帧对象 `repr()` 出来，既不经过 kwargs、也不经过任何插值点，所以本模块与
`tools/redaction.py`、`services/agent/upstream_redaction.py` 三处判据**一条都作用不到它**。
开关在 `nekro_agent/core/logger.py`：`NEKRO_LOG_DIAGNOSE`（默认关）。

**② uvicorn 的访问日志。** 它每处理完一个请求打一行，路径取的是
`get_path_with_query_string(scope)`——**带完整 query string**。本项目有两类凭据走 URL
（浏览器设不了请求头的场景）：`?token=<JWT>`（`services/user/deps.py` 支持从 URL 取令牌）
与 `?sig=<HMAC>`（`tools/upload_access.py` 的跨租户取件签名）。这一条**与配置项无关、
与异常无关**，是 INFO 级无条件打的，前两道判据都碰不到它。
遮蔽在 `nekro_agent/core/logger.py:install_sensitive_query_param_filter`
（装在 stdlib logging 的 `uvicorn.access` / `uvicorn.error` 两个 logger 上）。

三者的关系是「启动那一瞬 / 出异常那一刻 / 每一个请求」，覆盖面互不重叠——
别以为遮了 init kwargs 就万无一失。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple, Union

from dotenv import dotenv_values
from pydantic import SecretStr

# 凭据类字段名的判据。**与 `nekro_agent/services/sandbox/credential_guard.py` 的
# `_SECRET_FIELD_NAME_PATTERN` 是同一份判据**——那边管"别把凭据发回沙盒"，这边管
# "别把凭据打进日志"，两处漏判的后果同源，判据分叉就等于其中一处悄悄失效。
# 本模块不能 import 那个包（见模块 docstring 的最后一节），因此以镜像的方式重复一次，
# 并由 `tests/test_log_credential_hygiene.py` 断言两条正则逐字相等。
SECRET_ENV_KEY_PATTERN = re.compile(
    r"(API_?KEY|ACCESS_?KEY|SECRET|PASSWORD|PASSWD|TOKEN|CREDENTIAL|PRIVATE_?KEY|AUTH_?KEY)",
    re.IGNORECASE,
)

# NoneBot 的嵌套键分隔符。带它的键会被 `_explode_env_vars` 摊成嵌套 dict，
# 形态与"一个键一个值"不同，本模块不接管（保持原行为，宁可不遮也不改错）。
_NESTED_DELIMITER = "__"

# `nonebot.config.Env.environment` 的默认值：`--env=` 没给时 NoneBot 读 `.env.prod`
_DEFAULT_ENVIRONMENT = "prod"


def is_secret_env_key(key: str) -> bool:
    """判断一个 env 键名是否应视为凭据

    Args:
        key: env 键名，大小写不敏感

    Returns:
        命中凭据命名模式即为 True
    """
    return bool(SECRET_ENV_KEY_PATTERN.search(key))


def resolve_env_files(env_file: Union[str, os.PathLike, None]) -> Tuple[Path, ...]:
    """复刻 `nonebot.init()` 解析 env 文件的规则

    `nonebot.init(_env_file=X)` 实际读的是 `(".env", X)` 两份，X 缺省时是
    `.env.{ENVIRONMENT}`，而 `ENVIRONMENT` 自身又先从 os.environ、再从 `.env` 里取
    （`nonebot/config.py:Env`）。这里必须与它一致：漏读一份文件 = 那份里的凭据不受保护。

    Args:
        env_file: `run_bot.py` 解析出的 `--env=` 目标文件；未指定时为 None

    Returns:
        按 NoneBot 的读取顺序排列的 env 文件路径（不判断是否存在）
    """
    base = Path(".env")
    if env_file is not None:
        return (base, Path(env_file))

    environment = os.environ.get("ENVIRONMENT")
    if not environment and base.is_file():
        environment = (dotenv_values(base, encoding="utf-8") or {}).get("ENVIRONMENT") or None
    return (base, Path(f".env.{environment or _DEFAULT_ENVIRONMENT}"))


def _read_env_file_keys(env_files: Sequence[Union[str, os.PathLike]]) -> Dict[str, str]:
    """读出 env 文件里声明过的键（小写），值取 NoneBot 最终会用的那个

    NoneBot 合并的是 `{**env_file_vars, **os.environ}`：同名时**环境变量优先**。
    只有出现在 env 文件里的键才会成为额外字段，所以纯 os.environ 的键不在这里出现——
    否则我们会凭空给 `driver.config` 加出一个原本不存在的属性。
    """
    file_keys: Dict[str, str] = {}
    for path in env_files:
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            continue
        for key, value in (dotenv_values(candidate, encoding="utf-8") or {}).items():
            file_keys[key.lower()] = value or ""

    env_overrides = {key.lower(): value for key, value in os.environ.items()}
    return {key: env_overrides.get(key, value) for key, value in file_keys.items()}


def coerce_like_nonebot(raw: str) -> Any:
    """把 env 原始字符串还原成 NoneBot 会存进 `Config` 的那个值

    NoneBot 对额外字段会先尝试 `json.loads(value.strip())`，失败才保留**未 strip 的**原串
    （`nonebot/config.py` 末尾 "remain user custom config" 那段）。还原时必须逐字照做，
    否则一个纯数字的口令会从 `12345678`（int）变成 `"12345678"`（str），
    行为在肉眼看不见的地方分叉。
    """
    stripped = raw.strip()
    if not stripped:
        return raw
    try:
        return json.loads(stripped)
    except ValueError:
        return raw


def nonebot_declared_field_names() -> frozenset:
    """NoneBot `Config` 上**声明过**的字段名

    这些字段有具体类型（`port: int`、`host: IPvAnyAddress`……），塞 `SecretStr` 进去
    会在 `nonebot.init()` 里直接抛 pydantic 校验错。目前没有一个声明字段命中凭据命名模式，
    这条排除纯粹是给未来的 NoneBot 版本留的保险——出问题时它是启动即崩，
    而这里少遮一个非凭据字段没有任何代价。

    只 import `nonebot.config`，不触发任何初始化。
    """
    from nonebot.compat import model_fields
    from nonebot.config import Config as NoneBotConfig

    return frozenset(field.name.lower() for field in model_fields(NoneBotConfig))


def build_env_secret_shield(
    env_files: Sequence[Union[str, os.PathLike]],
    exclude_keys: Sequence[str] = (),
) -> Dict[str, SecretStr]:
    """为 env 文件里的凭据键生成 `nonebot.init()` 的掩码 kwargs

    判据有意**偏向多遮**：`ACCESS_TOKEN_EXPIRE_DAYS` 这种含 `TOKEN` 却只是个天数的键
    也会被遮住（实测 `.env.dev` 上就有一个）。两种错法的代价不对称——多遮一个非凭据，
    代价是那条 DEBUG 日志里少看见一个数字（键名仍在，取值仍在 `.env` 文件里，
    `describe_shield()` 还会把被遮的键名列出来）；漏遮一个凭据，代价是它进了每一份
    被打包、被 scp、被贴进工单的日志。

    Args:
        env_files: `resolve_env_files()` 的结果
        exclude_keys: 调用方自己也要传给 `nonebot.init()` 的键，避免重复关键字参数

    Returns:
        `{小写键名: SecretStr(明文)}`；空值键不纳入（没有可保护的东西，
        而遮成 `SecretStr('')` 只会让日志更难读）
    """
    excluded = {key.lower() for key in exclude_keys} | nonebot_declared_field_names()
    shield: Dict[str, SecretStr] = {}
    for key, value in _read_env_file_keys(env_files).items():
        if key in excluded or _NESTED_DELIMITER in key:
            continue
        if not value or not value.strip():
            continue
        if not is_secret_env_key(key):
            continue
        shield[key] = SecretStr(value)
    return shield


def restore_shielded_values(config: Any, shield: Mapping[str, SecretStr]) -> List[str]:
    """把掩码值换回明文，写回 `driver.config`

    必须在 `register_adapter()` 之前调用：适配器会 `Config(**driver.config.model_dump())`
    重建自己的配置模型，那些字段声明的是 `str`，拿到 `SecretStr` 不是校验失败就是
    比较恒不相等（见模块 docstring）。

    用 `setattr` 直接写属性是本仓库既有的做法
    （`nekro_agent/core/core_utils.py:load_config_to_env` 同样这么写回 nonebot 配置）。

    Args:
        config: `nonebot.get_driver().config`
        shield: `build_env_secret_shield()` 的返回值

    Returns:
        实际还原了的键名（已排序），供调用方断言/记录
    """
    restored: List[str] = []
    for key, secret in shield.items():
        setattr(config, key, coerce_like_nonebot(secret.get_secret_value()))
        restored.append(key)
    return sorted(restored)


def describe_shield(shield: Mapping[str, SecretStr]) -> str:
    """给启动日志用的一句话说明：只报键名，不报取值"""
    if not shield:
        return "无凭据类配置项需要遮蔽"
    return f"启动日志中已遮蔽 {len(shield)} 个凭据类配置项: {', '.join(sorted(shield))}"


def secret_env_keys(env_files: Sequence[Union[str, os.PathLike]]) -> List[str]:
    """列出 env 文件里被判定为凭据的键名（排序后），供排查与测试使用"""
    return sorted(build_env_secret_shield(env_files))


__all__ = [
    "SECRET_ENV_KEY_PATTERN",
    "build_env_secret_shield",
    "coerce_like_nonebot",
    "describe_shield",
    "is_secret_env_key",
    "nonebot_declared_field_names",
    "resolve_env_files",
    "restore_shielded_values",
    "secret_env_keys",
]
