"""日志与对外响应的脱敏工具

原本这套实现散落在 `services/deployment/client.py` 里，只服务于部署代理。多租户引入之后，
"凭据不得出现在任何日志或 API 错误响应里"变成了全局硬约束（定制版客户的后台会看到后端返回的
错误文本，上游报错里经常原样回显 `Authorization: Bearer sk-xxx`），因此抽到工具层复用。

三类需要遮蔽的东西：

- **凭据**：API Key / Bearer Token / `api_key=xxx` 形态的键值对
- **内网端点**：`http://127.0.0.1:8021/...` 这类暴露宿主拓扑的地址
- **宿主绝对路径**：`/home/xxx/...`、`C:\\Users\\xxx\\...`

`redact_text()` 三类全做，是对外文本的默认出口；需要单独复用某一类时用对应的窄函数。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from nekro_agent.schemas.tenant import CREDENTIAL_MASK

# 字典脱敏时整键丢弃的字段名（子串匹配）
HIDDEN_KEY_PARTS = ("token", "secret", "password", "api_key", "apikey", "authorization", "signature")
DROPPED_KEYS = {
    "data_dir",
    "compose_file",
    "env_file",
    "token_file",
    "token_file_path",
    "daemon_token",
    "health_url",
    "expose_port",
    "api_base",
    "socks_proxy",
    "backup_file",
}

_INTERNAL_ENDPOINT_RE = re.compile(
    r"(?i)\b(?:https?|socks5h?)://(?:127\.0\.0\.1|localhost|host\.docker\.internal|na-tools\.local)(?::\d+)?[^\s'\",)]*"
)
_UNIX_ABSOLUTE_PATH_RE = re.compile(r"(?<!:)/(?:home|root|srv|var|opt|etc|tmp|usr|mnt|data)/[^\s'\",)]*")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s'\",)]+")

# 凭据形态。分成三条而不是一条大正则，是为了各自能给出可读的替换标记
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}")
_SK_PREFIXED_RE = re.compile(r"\b(?:sk|xai|gsk|hf|ghp|glpat)-[A-Za-z0-9._\-]{8,}")
# Google API Key（Gemini 原生接口用，正规的是 AIza 加 35 位；放宽到 30 位以上，粘错一两位的也遮）。
# 前面不加 `\b`：转义后的 `\n` 会把下一段 Key 黏在字母 `n` 后面
_GOOGLE_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{30,}")
_KV_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|access[_-]?token|token|secret|password|authorization)"
    r"(\s*[=:]\s*)([\"']?)([^\s\"',&;]{4,})\3",
)

REDACTED_CREDENTIAL = "[redacted_credential]"
REDACTED_ENDPOINT = "[redacted_endpoint]"
REDACTED_PATH = "[redacted_path]"

# 短于该长度的"凭据"多半是占位符或空串，逐字替换反而会把正常文本打成马赛克
_MIN_LITERAL_SECRET_LEN = 6


def mask_credential(_value: str) -> str:
    """把不可见的凭据换成对外掩码

    无条件返回掩码串，空值也不例外：`schemas/tenant.ModelGroupInfo` 的契约写的是"恒为掩码串"，
    而"这个组到底配没配 Key"本身也是平台方的信息，不该顺着空串泄露给租户。
    """
    return CREDENTIAL_MASK


def is_credential_mask(value: str) -> bool:
    """判断前端回填的值是否只是掩码占位符

    写接口的"保持不变"哨兵：前端把读接口拿到的掩码原样提交回来时，必须保留原凭据，
    否则一次无意的保存就会把真实 Key 覆盖成 `********`。
    """
    return value == CREDENTIAL_MASK


def redact_credentials(value: str, literal_secrets: Iterable[str] = ()) -> str:
    """遮蔽文本中的凭据

    Args:
        value: 原始文本
        literal_secrets: 已知的明文凭据，逐字替换。上游报错未必是标准形态
            （例如只回显 Key 的后半段所在的整行），已知明文时直接按字面量兜底。
    """
    for secret in literal_secrets:
        if secret and len(secret) >= _MIN_LITERAL_SECRET_LEN:
            value = value.replace(secret, REDACTED_CREDENTIAL)
    value = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED_CREDENTIAL}", value)
    value = _SK_PREFIXED_RE.sub(REDACTED_CREDENTIAL, value)
    value = _GOOGLE_KEY_RE.sub(REDACTED_CREDENTIAL, value)
    return _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED_CREDENTIAL}", value)


def redact_internal_endpoints(value: str) -> str:
    """遮蔽内网端点与宿主绝对路径"""
    value = _INTERNAL_ENDPOINT_RE.sub(REDACTED_ENDPOINT, value)
    value = _UNIX_ABSOLUTE_PATH_RE.sub(REDACTED_PATH, value)
    return _WINDOWS_ABSOLUTE_PATH_RE.sub(REDACTED_PATH, value)


def redact_text(value: str, literal_secrets: Iterable[str] = ()) -> str:
    """对外文本的默认脱敏出口：凭据 + 内网端点 + 宿主路径"""
    return redact_internal_endpoints(redact_credentials(value, literal_secrets))


def redact_public_payload(value: Any, literal_secrets: Iterable[str] = ()) -> Any:
    """递归脱敏将要返回给前端的结构体

    命中 `DROPPED_KEYS` / `HIDDEN_KEY_PARTS` 的键整条丢弃而不是置为掩码：
    这些字段本身的存在就在泄露宿主形态，留个空壳没有意义。
    """
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():  # pyright: ignore[reportUnknownVariableType]
            key_text = str(key)
            lowered = key_text.lower()
            if lowered in DROPPED_KEYS or any(part in lowered for part in HIDDEN_KEY_PARTS):
                continue
            clean[key_text] = redact_public_payload(item, literal_secrets)
        return clean
    if isinstance(value, list):
        return [redact_public_payload(item, literal_secrets) for item in value]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, str):
        return redact_text(value, literal_secrets)
    return value
