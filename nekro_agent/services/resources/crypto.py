"""工作区资源敏感载荷的落库编码

## 读侧要认得的三种存量形态

1. `nkenc1:` 信封（`ENVELOPE`）：当前格式。整段 JSON 文本用
   `tools/credential_cipher` 的凭据主密钥加密，库里只有密文。
2. 明文 JSON（`PLAIN`）：改造前的格式，也是回滚模式下写出的格式。读侧照旧能读，
   但它意味着这一行还没被重写，启动时的整批升级会把它封成信封。
3. 旧 AES 载荷（`LEGACY_ENCRYPTED`）：更早的版本用 `NEKRO_JWT_SECRET_KEY` 派生
   AES 密钥加密的 `{nonce, ciphertext, tag}`。它要的是**另一把**密钥，
   只能通过显式的恢复接口（`recover_legacy_secret_payloads`）带着旧密钥恢复。

## 为什么复用 `credential_cipher` 那把主密钥

资源载荷与模型组 `api_key` 保护的是同一类东西（库 dump / 备份外流时不能读出的凭据）。
再引入一把资源专用密钥，等于给运维多一份必须单独备份、单独轮换、也更容易丢的材料；
而丢密钥的后果是所有工作区资源的 SSH 私钥、口令一起作废。复用同一把主密钥、
同一套 `nkenc1:` 信封格式，也让"信封格式是跨版本持久化契约"这条约束只有一份
（详见 `tools/credential_cipher.py` 模块文档），不会出现两代格式各自演化的局面。

## `stale` 的含义

`stale=True` 表示**这个存储值应该用当前主密钥重写一遍**，只有两种来源：

- 非空的 `PLAIN`：存量明文，从未加密过；
- 用历史密钥解开的 `ENVELOPE`：密钥搬过家（例如从数据目录文件搬进环境变量）。

`EMPTY`、用当前主密钥解开的 `ENVELOPE`、`LEGACY_ENCRYPTED` 都不算陈旧：
最后一种要的是旧密钥，硬重写只会把还可能救回来的值抹掉。

## 回滚模式

`RESOURCE_PAYLOAD_AT_REST=plain` 是给"换回不支持信封的旧版本"准备的临时开关：
此时写入退回明文 JSON，启动时 `resources_upgrade_at_rest()` 会把信封整批写回明文。
默认永远是 `encrypted`，并且没有可用密钥时**拒绝**保存敏感字段（宁可这次写入失败，
也不静默地把明文落库）。
"""

import base64
import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Dict

from Crypto.Cipher import AES

from nekro_agent.core.config import config
from nekro_agent.core.os_env import OsEnv
from nekro_agent.tools import credential_cipher


class SecretPayloadDecodeError(ValueError):
    """敏感资源载荷无法解码。"""


class SecretPayloadEncryptError(ValueError):
    """敏感资源载荷拒绝以明文保存（没有可用的静态加密密钥）。"""


# 文案同时出现在写侧异常、启动日志与健康检查的 `issues[].reason` 里；
# 它必须给出下一步动作（否则运维只知道"坏了"，不知道去恢复哪把密钥）
_ENCRYPT_UNAVAILABLE_MESSAGE = (
    "凭据静态加密不可用（没有可用密钥），拒绝以明文保存敏感字段。"
    "请配置 NEKRO_CREDENTIAL_SECRET_KEY 或让数据目录可写后重启"
)
_DECRYPT_UNAVAILABLE_MESSAGE = (
    "敏感字段是密文，但当前密钥解不开（密钥被换过或密钥文件丢了）。"
    "请恢复原来的 NEKRO_CREDENTIAL_SECRET_KEY 或数据目录里的 credential_secret.key"
)


class SecretPayloadFormat(str, Enum):
    EMPTY = "empty"
    PLAIN = "plain"
    ENVELOPE = "envelope"
    LEGACY_ENCRYPTED = "legacy_encrypted"
    INVALID = "invalid"


@dataclass(frozen=True)
class SecretPayloadDecodeResult:
    payload: Dict[str, str]
    format: SecretPayloadFormat
    # 这个存储值是否应该用当前主密钥重写一遍；口径见模块文档
    stale: bool = False


def _get_secret_key(secret: str | None = None) -> bytes:
    return hashlib.sha256((secret if secret is not None else OsEnv.JWT_SECRET_KEY).encode("utf-8")).digest()


def _normalize_payload(payload: object) -> Dict[str, str]:
    if not isinstance(payload, dict):
        raise SecretPayloadDecodeError("敏感资源载荷必须是 JSON 对象")
    return {str(k): str(v) for k, v in payload.items()}


def _is_legacy_encrypted_payload(data: object) -> bool:
    return isinstance(data, dict) and {"nonce", "ciphertext", "tag"}.issubset(data.keys())


def _decrypt_legacy_payload(data: dict, *, secret: str | None = None) -> Dict[str, str]:
    cipher = AES.new(
        _get_secret_key(secret),
        AES.MODE_GCM,
        nonce=base64.b64decode(data["nonce"]),
    )
    plaintext = cipher.decrypt_and_verify(
        base64.b64decode(data["ciphertext"]),
        base64.b64decode(data["tag"]),
    )
    return _normalize_payload(json.loads(plaintext.decode("utf-8")))


def encrypt_secret_payload(payload: Dict[str, str]) -> str:
    """把敏感字段值编码成落库形态

    `plain` 是明确的回滚模式（只应该为了换回旧版本临时使用一次）；`encrypted` 下
    **没有可用密钥就抛错**，不学模型组那套"降级明文 + ERROR"——模型组的 Key 还有
    "用户重新填一次"这条恢复路径，而工作区资源里的 SSH 私钥、root 口令未必有第二份。
    """
    if not payload:
        return ""
    normalized = _normalize_payload(payload)
    if config.RESOURCE_PAYLOAD_AT_REST == "plain":
        return json.dumps(normalized, ensure_ascii=False)
    if not credential_cipher.credential_cipher_status().available:
        raise SecretPayloadEncryptError(_ENCRYPT_UNAVAILABLE_MESSAGE)
    value = credential_cipher.encrypt_credential(json.dumps(normalized, ensure_ascii=False))
    # 防御：一旦上游退化成"没有密钥就原样返回"，这里宁可让这次写入失败，
    # 也不能把明文当成密文静默落库（字段名写着 encrypted，不能第二次名不副实）
    if not credential_cipher.is_encrypted_envelope(value):
        raise SecretPayloadEncryptError(_ENCRYPT_UNAVAILABLE_MESSAGE)
    return value


def decrypt_secret_payload(value: str, *, legacy_secret: str | None = None) -> Dict[str, str]:
    return decode_secret_payload(value, legacy_secret=legacy_secret).payload


def decode_secret_payload(value: str, *, legacy_secret: str | None = None) -> SecretPayloadDecodeResult:
    if not value.strip():
        return SecretPayloadDecodeResult(payload={}, format=SecretPayloadFormat.EMPTY)
    # 结构判定而不是"以前缀开头就算"：前缀之后解不出 nonce + tag 的值仍按存量明文处理
    if credential_cipher.is_encrypted_envelope(value):
        plaintext, source = credential_cipher.decrypt_credential_with_source(value)
        if source == "undecryptable":
            raise SecretPayloadDecodeError(_DECRYPT_UNAVAILABLE_MESSAGE)
        try:
            data = json.loads(plaintext)
            return SecretPayloadDecodeResult(
                payload=_normalize_payload(data),
                format=SecretPayloadFormat.ENVELOPE,
                stale=source == "fallback",
            )
        except SecretPayloadDecodeError:
            raise
        except Exception as e:
            raise SecretPayloadDecodeError("敏感资源载荷解密后不是合法的 JSON 对象") from e
    # 这一句的 `json.JSONDecodeError` 故意留在 try 之外：调用方（详情 / 更新）靠
    # `ValueError` 兜住坏载荷，异常类型变了会让那些端点直接 500
    data = json.loads(value)
    if _is_legacy_encrypted_payload(data):
        try:
            return SecretPayloadDecodeResult(
                payload=_decrypt_legacy_payload(data, secret=legacy_secret),
                format=SecretPayloadFormat.LEGACY_ENCRYPTED,
            )
        except Exception as e:
            raise SecretPayloadDecodeError(
                "旧版加密敏感字段无法解密，请提供创建资源时使用的旧 NEKRO_JWT_SECRET_KEY"
            ) from e
    try:
        return SecretPayloadDecodeResult(
            payload=_normalize_payload(data),
            format=SecretPayloadFormat.PLAIN,
            # 能走到这里说明留存值非空，且不是信封 / 旧 AES：就是需要升级的存量明文
            stale=True,
        )
    except SecretPayloadDecodeError:
        raise
    except Exception as e:
        raise SecretPayloadDecodeError("敏感资源载荷格式无效") from e
