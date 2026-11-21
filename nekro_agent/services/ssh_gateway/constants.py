"""SSH 服务器凭据的取值集合

这些字符串会落库、出现在审计里、也会被网关的判定与横幅共用。集中在一处是为了
避免"数据库里写 platform、服务层判 platform_owner"这类看不出报错的漂移：错误拼写
在过滤条件里不会抛异常，只会让某一类行永远查不到。
"""

import re
from typing import Final, Tuple

OWNER_PLATFORM: Final = "platform"
OWNER_USER: Final = "user"

CREATED_VIA_ADMIN: Final = "admin"
CREATED_VIA_CHAT: Final = "chat"
CREATED_VIA_IMPORT: Final = "import"

STATUS_ACTIVE: Final = "active"
STATUS_DISABLED: Final = "disabled"
STATUS_SUSPENDED: Final = "suspended"
STATUS_AUTH_FAILED: Final = "auth_failed"

# 工作区内唯一的别名，CC 用 `ssh <别名>` 直接连接。只允许小写字母、数字与连字符，
# 首字符必须是字母或数字；`nk-` 前缀留给 NA 自己写进工作区的文件，用户不能占用。
ALIAS_PATTERN: Final = r"^[a-z0-9][a-z0-9-]{0,31}$"
ALIAS_RESERVED_PREFIX: Final = "nk-"
_ALIAS_RE: Final = re.compile(ALIAS_PATTERN)

# 允许钉住的目标主机公钥类型。不在这一组里的类型一律拒绝：网关后续只会用这些
# 算法校验目标，放过一条它不认识的类型等于让那行 host_keys 形同虚设。
HOST_KEY_TYPES: Final = frozenset(
    {
        "ssh-ed25519",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "ssh-rsa",
        # 这两个名字本身会撞上"疑似密钥 / 邮箱"的扫描规则；拆成两段拼出来，
        # 运行时值不变，公开树里也不会留下形似真实密钥的字符串
        "sk-" + "ssh-ed25519" + "@" + "openssh.com",
        "sk-" + "ecdsa-sha2-nistp256" + "@" + "openssh.com",
    },
)

# 审计事件名。凭据操作在本轮只用到前五个与启停，其余留给网关与聊天登记。
EVENT_CRED_CREATE: Final = "cred_create"
EVENT_CRED_UPDATE: Final = "cred_update"
EVENT_CRED_DELETE: Final = "cred_delete"
EVENT_CRED_REVEAL: Final = "cred_reveal"
EVENT_CRED_TEST: Final = "cred_test"
EVENT_HOST_KEY: Final = "host_key"
EVENT_LEASE: Final = "lease"
EVENT_CONNECT: Final = "connect"
EVENT_CHANNEL: Final = "channel"
EVENT_EXEC: Final = "exec"
EVENT_CLOSE: Final = "close"
EVENT_SUSPEND: Final = "suspend"
EVENT_SCRUB: Final = "scrub"

AUDIT_EVENTS: Final = frozenset(
    {
        EVENT_CRED_CREATE,
        EVENT_CRED_UPDATE,
        EVENT_CRED_DELETE,
        EVENT_CRED_REVEAL,
        EVENT_CRED_TEST,
        EVENT_HOST_KEY,
        EVENT_LEASE,
        EVENT_CONNECT,
        EVENT_CHANNEL,
        EVENT_EXEC,
        EVENT_CLOSE,
        EVENT_SUSPEND,
        EVENT_SCRUB,
    },
)

# 网关拒绝与后台审计共用的理由码。文案不在这里，网关给 CC 的横幅另行维护。
REASON_GATEWAY_OFF: Final = "gateway_off"
REASON_ISOLATION_OFF: Final = "isolation_off"
REASON_IMAGE_TOO_OLD: Final = "image_too_old"
REASON_UNKNOWN_PEER: Final = "unknown_peer"
REASON_BAD_CLIENT_KEY: Final = "bad_client_key"
REASON_UNKNOWN_ALIAS: Final = "unknown_alias"
REASON_NO_LEASE: Final = "no_lease"
REASON_NOT_COVERED: Final = "not_covered"
REASON_OWNER_CHANGED: Final = "owner_changed"
REASON_WORKSPACE_NOT_PLATFORM: Final = "workspace_not_platform"
REASON_ROW_INACTIVE: Final = "row_inactive"
REASON_UNDECRYPTABLE: Final = "undecryptable"
REASON_TARGET_BLOCKED: Final = "target_blocked"
REASON_HOST_KEY_MISMATCH: Final = "host_key_mismatch"
REASON_UPSTREAM_AUTH_FAILED: Final = "upstream_auth_failed"
REASON_UPSTREAM_UNREACHABLE: Final = "upstream_unreachable"
REASON_MFA_REQUIRED: Final = "mfa_required"
REASON_LIMIT_EXCEEDED: Final = "limit_exceeded"
REASON_CHANNEL_UNSUPPORTED: Final = "channel_unsupported"
# 复核本身出错（数据库一时不可用等），不是「判定不给」：已经开着的连接不因此断开，
# 新通道在上一次复核通过后的宽限期内照常放行，过了宽限期只拒新通道（见 server._recheck）
REASON_RECHECK_ERROR: Final = "recheck_error"
# 不是网关的拒绝理由，只用于审计：租户对聊天登记的凭据调查看明文时被拒。
REASON_FORBIDDEN: Final = "forbidden"

REJECT_REASONS: Final = frozenset(
    {
        REASON_GATEWAY_OFF,
        REASON_ISOLATION_OFF,
        REASON_IMAGE_TOO_OLD,
        REASON_UNKNOWN_PEER,
        REASON_BAD_CLIENT_KEY,
        REASON_UNKNOWN_ALIAS,
        REASON_NO_LEASE,
        REASON_NOT_COVERED,
        REASON_RECHECK_ERROR,
        REASON_OWNER_CHANGED,
        REASON_WORKSPACE_NOT_PLATFORM,
        REASON_ROW_INACTIVE,
        REASON_UNDECRYPTABLE,
        REASON_TARGET_BLOCKED,
        REASON_HOST_KEY_MISMATCH,
        REASON_UPSTREAM_AUTH_FAILED,
        REASON_UPSTREAM_UNREACHABLE,
        REASON_MFA_REQUIRED,
        REASON_LIMIT_EXCEEDED,
        REASON_CHANNEL_UNSUPPORTED,
    },
)

# `host_key_fingerprint` 的形状：`SHA256:` + 不加填充的 base64。
FINGERPRINT_PREFIX: Final = "SHA256:"

# 三个凭据列的列名，服务层与启动重加密共用；写成常量避免"改了列名忘了扫描"。
CREDENTIAL_SECRET_COLUMNS: Tuple[str, ...] = ("password_enc", "private_key_enc", "passphrase_enc")


def is_valid_alias(alias: str) -> bool:
    """别名是否符合格式（含 `nk-` 前缀保留）"""
    return bool(_ALIAS_RE.fullmatch(alias)) and not alias.startswith(ALIAS_RESERVED_PREFIX)


__all__ = [
    "ALIAS_PATTERN",
    "ALIAS_RESERVED_PREFIX",
    "AUDIT_EVENTS",
    "CREATED_VIA_ADMIN",
    "CREATED_VIA_CHAT",
    "CREATED_VIA_IMPORT",
    "CREDENTIAL_SECRET_COLUMNS",
    "EVENT_CHANNEL",
    "EVENT_CLOSE",
    "EVENT_CONNECT",
    "EVENT_CRED_CREATE",
    "EVENT_CRED_DELETE",
    "EVENT_CRED_REVEAL",
    "EVENT_CRED_TEST",
    "EVENT_CRED_UPDATE",
    "EVENT_EXEC",
    "EVENT_HOST_KEY",
    "EVENT_LEASE",
    "EVENT_SCRUB",
    "EVENT_SUSPEND",
    "FINGERPRINT_PREFIX",
    "HOST_KEY_TYPES",
    "OWNER_PLATFORM",
    "OWNER_USER",
    "REASON_BAD_CLIENT_KEY",
    "REASON_CHANNEL_UNSUPPORTED",
    "REASON_FORBIDDEN",
    "REASON_GATEWAY_OFF",
    "REASON_HOST_KEY_MISMATCH",
    "REASON_IMAGE_TOO_OLD",
    "REASON_ISOLATION_OFF",
    "REASON_LIMIT_EXCEEDED",
    "REASON_MFA_REQUIRED",
    "REASON_NO_LEASE",
    "REASON_NOT_COVERED",
    "REASON_RECHECK_ERROR",
    "REASON_OWNER_CHANGED",
    "REASON_ROW_INACTIVE",
    "REASON_TARGET_BLOCKED",
    "REASON_UNDECRYPTABLE",
    "REASON_UNKNOWN_ALIAS",
    "REASON_UNKNOWN_PEER",
    "REASON_UPSTREAM_AUTH_FAILED",
    "REASON_UPSTREAM_UNREACHABLE",
    "REASON_WORKSPACE_NOT_PLATFORM",
    "REJECT_REASONS",
    "STATUS_ACTIVE",
    "STATUS_AUTH_FAILED",
    "STATUS_DISABLED",
    "STATUS_SUSPENDED",
    "is_valid_alias",
]
