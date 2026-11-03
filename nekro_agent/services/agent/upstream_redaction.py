"""LLM 上游报错原文的脱敏出口

## 要防的是什么

上游 400/401/5xx 的报错体**由上游决定内容**，而供应商回显凭据是常见形态：
`Incorrect API key provided: sk-…`、把 `?key=` 拼进 URL 的 4xx、
反向代理把整条 `Authorization` 头抄回来的 5xx。这段文本此前一路原样流到四个出口：

| 出口 | 谁能看到 |
| --- | --- |
| `logger.exception` -> `app.log` / 内存日志环 / `/logs` API / 运维 Bot 推送 | 平台方 + 备份 |
| `prompts_error/*.json` 的 `error.message` | 平台方 + 备份 + `/deployment/backup` 打包 |
| `AgentRuntimeStatusEvent.error_summary` -> SSE | **租户**（`/events` 是 `Role.Admin`） |
| `DBExecCode.extra_data.llm_retry_errors` | 平台方 + 数据库备份 |

第三条是真正的 HTTP 层泄漏：租户的会话跑在**平台方的**模型组上，上游一旦回显平台 Key，
这条 SSE 就把它直接推到租户后台，绕过所有既有掩码。

## 与 #37 的判据差别：两条都要有

`nekro_log_hygiene` 脱的是**已知配置项的取值**——键名可枚举，按值精确替换即可。
这里脱的是**任意上游返回的文本**，里面的凭据未必是我们认识的那一条
（中转站回显的可能是它自己上游的 Key、也可能是被改写过的头），所以：

- **按值**：`credential_guard.collect_platform_secrets()`——活配置树 + `OsEnv` 的权威取值，
  抓「上游把我们的 Key 原样抄回来」。这一条不会误伤正常文本。
- **按形态**：`tools/redaction.redact_credentials()` 的三条正则（`Bearer/Basic`、
  `sk-`/`ghp-` 前缀族、`api_key=` 键值对），抓「回显的凭据我们不认识」。

只有按值 = 换一家中转站就漏；只有按形态 = 自建上游的非标准 Key 形态就漏。
两条是并集，不是二选一。

## 为什么只脱凭据，不连内网端点与宿主路径一起脱

`tools/redaction.redact_text()` 还会把 `/opt/...`、`http://127.0.0.1:8021` 换成占位符，
那是**对外 API 响应**（`model_group_test`）的口径。这条链路的文本主要落在平台方自己的
`app.log` 与错误日志文件里，而路径与 endpoint 恰恰是排障要看的东西——
traceback 的每一行 `File "/opt/..."` 都被打成 `[redacted_path]` 之后，
「把整个报错吞了」和「脱敏」就没区别了。#39 的硬约束是**凭据**，判据到此为止。

同一段文本在四个出口用**同一条**判据，不按「落在哪里」分叉（判据分叉等于其中一处悄悄失效）。

## `format_redacted_traceback` 为什么必须存在

`logger.exception()` 不只渲染 f-string，还会渲染一份 loguru 的诊断 traceback，
而 loguru 的 `diagnose` 默认开着：**它会把每一行源码里出现的名字连同取值一起打出来**。
实测（本机复现 + 测试部署 `app.log` 现存证据）：

- 复现：一次带 `sk-…` 的 401，哨兵在 sink 里出现 **4 次**——f-string 1 次、
  诊断行 2 次（局部变量取值）、异常尾行 1 次。只脱 f-string 等于只堵住 1/4。
- 现存：`app.log` 里 50 处 `ConnectionParameters(user=…, password=…)` ——
  那是 tortoise/asyncpg 连库失败时被 loguru 诊断行打出来的**数据库口令**。

所以这条链路不再用 `logger.exception`，改成 `traceback.format_exception()` 自己渲染
（stdlib 的实现**不含局部变量**）再整体脱敏，然后用 `logger.error` 打出去。
代价是丢掉 loguru 的诊断行与配色，换来的是「局部变量里的 `api_key`/整份 prompt
不会进日志文件」。
"""

from __future__ import annotations

import traceback
from typing import FrozenSet, Iterable, Optional

from nekro_agent.services.sandbox.credential_guard import collect_platform_secrets
from nekro_agent.tools.redaction import redact_credentials


def collect_llm_secret_values(*extra_secrets: Optional[str]) -> FrozenSet[str]:
    """本次 LLM 调用要按字面量遮蔽的凭据集合

    `collect_platform_secrets()` 取的是**全局**配置树，而本次请求真正用的模型组可能来自
    会话/实例覆盖层（`core/overridable_config.py`），它的 `API_KEY` 未必在全局树里。
    调用方因此要把这次实际用上的凭据一并传进来——漏掉它就等于「谁的 Key 泄漏了就漏谁的」。

    Args:
        *extra_secrets: 本次请求实际使用的凭据（模型组 `API_KEY`、代理串等），允许为 None/空串

    Returns:
        凭据原文集合（并集）
    """
    values = set(collect_platform_secrets())
    values.update(secret for secret in extra_secrets if secret)
    return frozenset(values)


def redact_upstream_text(text: str, secret_values: Optional[Iterable[str]] = None) -> str:
    """遮蔽上游报错文本里的凭据（按值 + 按形态，见模块 docstring）

    Args:
        text: 上游原文（`str(exception)`、响应体片段、渲染好的 traceback 等）
        secret_values: 字面量凭据集合；省略时现场调 `collect_llm_secret_values()`

    Returns:
        脱敏后的文本。**非凭据部分逐字保留**——状态码、错误类型、供应商文案都还在，
        否则排障就废了
    """
    values = collect_llm_secret_values() if secret_values is None else secret_values
    return redact_credentials(text, literal_secrets=values)


def format_redacted_traceback(exc: BaseException, secret_values: Optional[Iterable[str]] = None) -> str:
    """把异常渲染成**不含局部变量**的 traceback 文本并脱敏

    用 stdlib 的 `traceback.format_exception` 而不是 `logger.exception`：后者会走 loguru 的
    `diagnose`，把每一行源码里出现的名字连同取值一起打出来（`messages` 的整份 prompt、
    `api_key`、`gen_kwargs` 全在内），而那份诊断文本不经过任何 f-string，脱不掉。

    Args:
        exc: 异常对象
        secret_values: 字面量凭据集合；省略时现场收集

    Returns:
        脱敏后的多行 traceback 文本（含异常链 `__cause__` / `__context__`）
    """
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return redact_upstream_text(rendered.rstrip(), secret_values)


__all__ = [
    "collect_llm_secret_values",
    "format_redacted_traceback",
    "redact_upstream_text",
]
