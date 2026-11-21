"""SSH 凭据的静态加密

## 为什么直接拒绝没有密钥的写入

模型组在没有可用密钥时会退化成明文并打 ERROR，因为它还有"用户重新填一次"这条恢复
路径。SSH 服务器凭据不一样：私钥与 root 口令未必有第二份，一次静默的明文落库可能
就是永久泄露。因此 `encrypt_secret` 在 `credential_cipher` 不可用时抛错，建、改两条
路径都把这次写入失败掉，库里不会多出一行明文。

## 为什么启动时整批重写

存量行可能是加密上线之前的明文，也可能是用历史密钥写下的信封。两者读得出来，但只
兼容不升级的话，明文会永远裸着；历史密钥也不能一直留着。启动时扫一遍三个 `_enc`
列，把 `REENCRYPT_SOURCES` 里的两档用当前主密钥重写。解不开的行**跳过并点名**：
重写等于把一个还可能被原密钥救回来的值抹掉，而启动日志是运维唯一能拿到"具体是哪几行"
的地方。
"""

from typing import Dict, List, Tuple

from tortoise import Tortoise

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
from nekro_agent.tools import credential_cipher

logger = get_sub_logger("ssh_gateway.secrets")

_ENCRYPT_UNAVAILABLE_MESSAGE = (
    "凭据静态加密不可用（没有可用密钥），拒绝保存服务器凭据。"
    "请配置 NEKRO_CREDENTIAL_SECRET_KEY 或让数据目录可写后重启"
)


class SshSecretEncryptError(ValueError):
    """没有可用密钥，拒绝把服务器凭据写成明文"""


def encrypt_secret(plain: str) -> str:
    """把明文凭据封成 `nkenc1:` 信封；空串原样返回

    Raises:
        SshSecretEncryptError: 当前进程没有可用密钥，或上游退化成"原样返回明文"。
    """
    if not plain:
        return ""
    if not credential_cipher.credential_cipher_status().available:
        raise SshSecretEncryptError(_ENCRYPT_UNAVAILABLE_MESSAGE)
    value = credential_cipher.encrypt_credential(plain)
    # 防御：上游一旦退化成"没密钥就原样返回"，这里宁可让写入失败，也不能让字段名
    # 写着 encrypted、库里却是明文（资源载荷那条路径已经踩过一次这个坑）
    if not credential_cipher.is_encrypted_envelope(value):
        raise SshSecretEncryptError(_ENCRYPT_UNAVAILABLE_MESSAGE)
    return value


def decrypt_secret(stored: str) -> Tuple[str, str]:
    """还原明文并给出分档（empty / plaintext / primary / fallback / undecryptable）

    调用方要自己判断 `undecryptable`：这一层不抛错，是为了让"读一行做健康检查"这种
    场景可以一次拿全分档，而不是被第一个坏列打断。
    """
    return credential_cipher.decrypt_credential_with_source(stored)


def secret_is_undecryptable(stored: str) -> bool:
    """这一列当前是否解不开（空串不算）"""
    return bool(stored) and credential_cipher.classify_stored_credential(stored) == "undecryptable"


async def ssh_credentials_upgrade_at_rest() -> Tuple[List[int], List[int]]:
    """把三个 `_enc` 列里需要重加密的行整批重写

    Returns:
        `(被重写的行 id, 解不开的行 id)`。解不开的行整行跳过：只要三列里有一列
        解不开，就不动这一行，避免"顺手把另外两列重写、留下一半新一半旧"的状态。

    没有可用密钥时只扫不写，但仍然返回解不开的行，供启动日志点名。直接读原始列而
    不是 ORM 属性：ORM 属性会解密，从它身上看不出库里存的是明文还是信封。

    幂等：第二遍没有任何行落在 `REENCRYPT_SOURCES` 里。写库用 `filter(id=...).update(...)`，
    不改 `update_time`——重加密不是一次用户编辑。
    """
    rewrite_allowed = credential_cipher.credential_cipher_status().available
    conn = Tortoise.get_connection("default")
    rows = await conn.execute_query_dict(
        'SELECT "id", "alias", "password_enc", "private_key_enc", "passphrase_enc" FROM "ws_ssh_credential"',
    )

    rewritten: List[int] = []
    unreadable: List[int] = []
    for row in rows:
        row_id = int(row["id"])
        updates: Dict[str, str] = {}
        row_broken = False
        for column in ("password_enc", "private_key_enc", "passphrase_enc"):
            stored = row.get(column)
            if not isinstance(stored, str) or not stored:
                continue
            source = credential_cipher.classify_stored_credential(stored)
            if source == "undecryptable":
                row_broken = True
                break
            if source in credential_cipher.REENCRYPT_SOURCES:
                if not rewrite_allowed:
                    # 没有密钥时不尝试加密：那只会让整趟扫描在第一行就抛错，
                    # 而这次启动的职责本应是"只扫不写"，把解不开的行照样点名
                    continue
                plain, _ = credential_cipher.decrypt_credential_with_source(stored)
                updates[column] = encrypt_secret(plain)
        if row_broken:
            unreadable.append(row_id)
            continue
        if not rewrite_allowed or not updates:
            continue
        await DBWsSshCredential.filter(id=row_id).update(**updates)
        rewritten.append(row_id)

    if unreadable:
        # 一条 ERROR 里逐个点名：运维拿到"有一行解不开"没法行动，而 id 与别名就在手上
        names = ", ".join(f"{row_id}({_alias_of(rows, row_id)})" for row_id in unreadable)
        logger.error(
            f"以下 SSH 服务器凭据当前密钥解不开，本次启动**不会**重写它们: {names}。"
            "请先把原来的 NEKRO_CREDENTIAL_SECRET_KEY（或数据目录里那把密钥文件）放回去再重启；"
            "在这些行上保存其他字段不会覆盖原密文，但**重新填写凭据仍会覆盖**——想找回原值就先恢复密钥。",
        )

    if not rewrite_allowed:
        logger.error(
            "SSH 服务器凭据的静态加密不可用（没有可用密钥），本次启动只扫描、不重写任何行。"
            "请配置 NEKRO_CREDENTIAL_SECRET_KEY 或让数据目录可写后重启。",
        )

    return rewritten, unreadable


def _alias_of(rows: List[dict], row_id: int) -> str:
    for row in rows:
        if int(row["id"]) == row_id:
            return str(row.get("alias") or "")
    return ""


__all__ = [
    "SshSecretEncryptError",
    "decrypt_secret",
    "encrypt_secret",
    "secret_is_undecryptable",
    "ssh_credentials_upgrade_at_rest",
]
