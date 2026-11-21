"""SSH 服务器凭据的字段校验

校验只回答"这行数据能不能存"：别名的形状、目标地址与登录名的边界、私钥看起来像不像
私钥文本、`host_keys` 的每一行能不能按 SSH 线格式解出一个已知类型。

私钥**不做深度解析**：真正的私钥格式（OpenSSH、PEM、带口令短语）由后续网关用 SSH 库
解析。这里只拦"明显不是私钥"的粘贴，免得把一段说明文字当成私钥存进去；再加两道上限：
KDF 成本（`private_key_cost_problem`）——带口令短语的私钥要跑一遍 KDF，轮数、迭代次数与
派生长度写在私钥文件里、由登记的人决定，不设上限就能让一次登录把 NA 卡住；文本长度与
钥匙位数（`private_key_size_problem`）——RSA 导入时的校验成本约随位数的三次方涨。两道都读
asyncssh 自己的解析结果（见函数说明）；真正解析的地方另有子进程、超时与按工作区的名额兜底
（`upstream`）。

所有文本字段都拒绝 NUL：PG 的文本列存不进去，报出来的是一个与字段无关的数据库错误。
"""

import base64
import hashlib
import re
from typing import Any, List, Tuple

from nekro_agent.schemas.errors import ValidationError
from nekro_agent.services.ssh_gateway.constants import (
    ALIAS_RESERVED_PREFIX,
    CREDENTIAL_SECRET_COLUMNS,
    FINGERPRINT_PREFIX,
    HOST_KEY_TYPES,
    is_valid_alias,
)

_WHITESPACE_RE = re.compile(r"\s")

MAX_HOST_LENGTH = 255
MAX_USERNAME_LENGTH = 64
MAX_OWNER_ACCOUNT_LENGTH = 128
MIN_PORT = 1
MAX_PORT = 65535

# 私钥 KDF 的上限：ssh-keygen 默认 16 轮，常见的「加强」写法是 -a 64 / -a 100
MAX_BCRYPT_ROUNDS = 128
# PKCS#8 的上限按「迭代次数 × 每次要算的块数」计（PBKDF2 的块数 = ⌈keyLength / hLen⌉，
# keyLength 同样写在文件里）；OpenSSL 默认 2048 次、一块
MAX_PBKDF_ITERATIONS = 1_000_000

# 私钥文本与钥匙本身的大小上限：RSA 的校验（导入时核对 p、q 等）成本约随位数的三次方涨，
# 8192 位就要好几秒、32k 位要几分钟。4096 位的 RSA 私钥（含口令短语加密）约 3.4k 字符
MAX_PRIVATE_KEY_CHARS = 8192
MAX_RSA_KEY_BITS = 4096
MAX_DSA_KEY_BITS = 3072

_OPENSSH_MAGIC = b"openssh-key-v1\x00"

# asyncssh `pbe.py` 认的算法（OID 点分串）与每次迭代要算的块数，逐项对照它的实现：
# - PBES1（`_pbkdf1`）：MD5 输出 16 字节、要 16 字节时会再递归一轮，算两遍；SHA1 一遍
# - PKCS#12（`_pbkdf_p12`）：密钥与 IV 各按 SHA1 的 20 字节一块算，3DES 是 2 + 1 块
# - PBES2：只有 PBKDF2；块数 = ⌈密钥长度 / PRF 输出长度⌉，密钥长度缺省取加密算法的密钥长度
_PBES1_BLOCKS = {"1.2.840.113549.1.5.3": 2, "1.2.840.113549.1.5.10": 1}
_PKCS12_BLOCKS = {
    "1.2.840.113549.1.12.1.1": 1,  # rc4-128
    "1.2.840.113549.1.12.1.2": 1,  # rc4-40
    "1.2.840.113549.1.12.1.3": 3,  # des3：密钥 24 字节两块 + IV 一块
    "1.2.840.113549.1.12.1.4": 2,  # des2：密钥 16 字节一块 + IV 一块
}
_OID_PBES2 = "1.2.840.113549.1.5.13"
_OID_PBKDF2 = "1.2.840.113549.1.5.12"
_PBES2_CIPHER_KEY_SIZE = {
    "2.16.840.1.101.3.4.1.2": 16,  # aes128-cbc
    "2.16.840.1.101.3.4.1.22": 24,  # aes192-cbc
    "2.16.840.1.101.3.4.1.42": 32,  # aes256-cbc
    "1.3.6.1.4.1.3029.1.2": 16,  # blowfish-cbc
    "1.2.840.113533.7.66.10": 16,  # cast128-cbc
    "1.3.14.3.2.7": 8,  # des-cbc
    "1.2.840.113549.3.7": 24,  # des3-cbc
}
_PBKDF2_PRF_OUTPUT = {
    "1.2.840.113549.2.7": 20,  # sha1（缺省）
    "1.2.840.113549.2.8": 28,  # sha224
    "1.2.840.113549.2.9": 32,  # sha256
    "1.2.840.113549.2.10": 48,  # sha384
    "1.2.840.113549.2.11": 64,  # sha512
}


def _reject_nul(value: str, label: str) -> None:
    if "\x00" in value:
        raise ValidationError(reason=f"{label}不能包含 NUL 字符")


def _asn1_ints(value: Any) -> List[int]:
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, (tuple, list)):
        found: List[int] = []
        for item in value:
            found.extend(_asn1_ints(item))
        return found
    return []


_COST_TOO_HIGH_BCRYPT = f"私钥的口令短语加密轮数过高（上限 {MAX_BCRYPT_ROUNDS}）"
_COST_TOO_HIGH_PBKDF = "私钥的口令短语加密参数过高，请转成 OpenSSH 格式"
_COST_NESTED = "私钥套了两层加密（Proc-Type 加 ENCRYPTED PRIVATE KEY），请转成 OpenSSH 格式"
_COST_UNPARSEABLE = "私钥解析失败"
_SIZE_TOO_LONG = f"私钥文本太长（最多 {MAX_PRIVATE_KEY_CHARS} 个字符）"
_SIZE_RSA_TOO_BIG = f"私钥位数过高（RSA 最多 {MAX_RSA_KEY_BITS} 位）"
_SIZE_DSA_TOO_BIG = f"私钥位数过高（DSA 最多 {MAX_DSA_KEY_BITS} 位）"


def _oid(value: Any) -> str:
    return str(value) if type(value).__name__ == "ObjectIdentifier" else ""


def public_key_size_problem(public_data: bytes) -> str:
    """按 SSH 线格式的公钥读 RSA 模数 / DSA p 的位数，超限返回说明

    网关子进程里有一份同样判据的副本（子进程以 `-I` 启动，导入不了本模块）。
    """
    from asyncssh.packet import PacketDecodeError, SSHPacket

    try:
        packet = SSHPacket(bytes(public_data))
        name = packet.get_string()
        if name == b"ssh-rsa":
            packet.get_mpint()  # e
            if packet.get_mpint().bit_length() > MAX_RSA_KEY_BITS:
                return _SIZE_RSA_TOO_BIG
        elif name == b"ssh-dss":
            if packet.get_mpint().bit_length() > MAX_DSA_KEY_BITS:
                return _SIZE_DSA_TOO_BIG
    except (PacketDecodeError, ValueError, TypeError):
        return ""
    return ""


def _openssh_public_blob(body: bytes) -> bytes:
    """OpenSSH 私钥里第一把公钥（加不加密都是明文）；读不出返回空串"""
    from asyncssh.packet import PacketDecodeError, SSHPacket

    if not body.startswith(_OPENSSH_MAGIC):
        return b""
    try:
        packet = SSHPacket(body[len(_OPENSSH_MAGIC) :])
        packet.get_string()  # ciphername
        packet.get_string()  # kdfname
        packet.get_string()  # kdfoptions
        if packet.get_uint32() < 1:
            return b""
        return packet.get_string()
    except (PacketDecodeError, ValueError, TypeError):
        return b""


def private_key_size_problem(private_key: str) -> str:
    """私钥文本长度与钥匙位数是否超限；返回问题说明，没问题返回空串

    - 文本超过 `MAX_PRIVATE_KEY_CHARS`：拒；
    - OpenSSH 格式：公钥部分不加密，直接读位数；
    - 其余格式：不带口令短语、跳过 RSA 校验地导入一次（只解码，开销有界）读位数；加密的
      PKCS#8 / 旧式 PEM 在这里解不开，留给网关子进程解密之后、校验之前再判。
    """
    text = str(private_key or "")
    if len(text) > MAX_PRIVATE_KEY_CHARS:
        return _SIZE_TOO_LONG
    try:
        data = text.encode("ascii")
    except UnicodeEncodeError:
        return ""  # 成本检查会给出 ASCII 的说明
    try:
        import asyncssh
        from asyncssh.public_key import _match_next
    except ImportError:  # pragma: no cover - 与成本检查同一个取舍
        return _COST_UNPARSEABLE
    try:
        fmt, key_info, _end = _match_next(data, b"PRIVATE KEY")
    except Exception:  # noqa: BLE001 - asyncssh 自己也解不了，交给它报
        return ""
    if fmt == "pem" and key_info[0] == b"OPENSSH":
        blob = _openssh_public_blob(key_info[2])
        return public_key_size_problem(blob) if blob else ""
    try:
        key = asyncssh.import_private_key(data, None, unsafe_skip_rsa_key_validation=True)
    except Exception:  # noqa: BLE001 - 加密的或坏的：前者留给子进程判，后者交给 asyncssh 报
        return ""
    return public_key_size_problem(key.public_data)


def _openssh_cost_problem(body: bytes) -> str:
    """OpenSSH 私钥：照 asyncssh `_decode_openssh_private` 的读法取出 cipher / kdf / 轮数"""
    from asyncssh.packet import PacketDecodeError, SSHPacket

    if not body.startswith(_OPENSSH_MAGIC):
        return ""  # asyncssh 同样不认，直接报错，不会跑 KDF
    try:
        packet = SSHPacket(body[len(_OPENSSH_MAGIC) :])
        cipher_name = packet.get_string()
        kdf_name = packet.get_string()
        kdf_data = packet.get_string()
        if cipher_name == b"none":
            return ""  # 没加密：asyncssh 不跑 KDF，kdf 字段写什么都无所谓
        if kdf_name != b"bcrypt":
            return "私钥使用了不支持的口令短语算法"
        kdf_packet = SSHPacket(kdf_data)
        kdf_packet.get_string()  # salt
        rounds = kdf_packet.get_uint32()
    except (PacketDecodeError, ValueError, TypeError):
        # 读法与 asyncssh 逐字相同：这里读不下去，asyncssh 也在跑 KDF 之前就报错
        return ""
    if rounds > MAX_BCRYPT_ROUNDS:
        return _COST_TOO_HIGH_BCRYPT
    return ""


def _pbes2_work(params: Any) -> int:
    """照 asyncssh `_pbes2` / `_pbes2_pbkdf2` 的读法算 PBKDF2 的工作量；它在跑之前就会报错的写法返回 0"""
    if not (
        isinstance(params, tuple)
        and len(params) == 2
        and isinstance(params[0], tuple)
        and len(params[0]) >= 1
        and isinstance(params[1], tuple)
        and len(params[1]) >= 1
    ):
        return 0
    if _oid(params[0][0]) != _OID_PBKDF2:
        return 0
    default_key_size = _PBES2_CIPHER_KEY_SIZE.get(_oid(params[1][0]))
    if default_key_size is None:
        return 0
    kdf_params = params[0][1:]
    if len(kdf_params) != 1 or not isinstance(kdf_params[0], tuple) or len(kdf_params[0]) < 2:
        return 0
    fields = list(kdf_params[0])
    if not isinstance(fields[0], bytes) or not isinstance(fields[1], int):
        return 0
    count = int(fields[1])
    rest = fields[2:]
    key_size = int(rest.pop(0)) if rest and isinstance(rest[0], int) else default_key_size
    output_size = 20
    if rest:
        prf = rest[0]
        if not (isinstance(prf, tuple) and len(prf) == 2):
            return 0
        known = _PBKDF2_PRF_OUTPUT.get(_oid(prf[0]))
        if known is None:
            return 0
        output_size = known
    if count <= 0 or key_size <= 0:
        return 0
    return count * -(-key_size // output_size)


def _pkcs8_work(algorithm: Any) -> int:
    """`EncryptedPrivateKeyInfo` 的算法部分要算多少块哈希（照 asyncssh `pkcs8_decrypt` 分派）"""
    if not (isinstance(algorithm, tuple) and len(algorithm) == 2):
        return 0
    oid, params = _oid(algorithm[0]), algorithm[1]
    if oid == _OID_PBES2:
        return _pbes2_work(params)
    blocks = _PBES1_BLOCKS.get(oid) or _PKCS12_BLOCKS.get(oid)
    if blocks is None:
        return 0
    if not (isinstance(params, tuple) and len(params) == 2 and isinstance(params[1], int)):
        return 0
    return max(int(params[1]), 0) * blocks


def _pkcs8_cost_problem(decoded: Any) -> str:
    """加密的 PKCS#8（`EncryptedPrivateKeyInfo`）：按 asyncssh 真正要算的工作量判

    工作量 = 迭代次数 × 每次迭代要算的块数（PBKDF2 是 ⌈keyLength / hLen⌉，keyLength 由文件
    决定：两个各两万的值就要跑将近一分钟）。另留一道兜底：算法参数里任何整数超过上限也拒。
    形状不像 `EncryptedPrivateKeyInfo` 的（未加密的 PKCS#8 / PKCS#1）不跑 KDF，不判。
    """
    if not (isinstance(decoded, tuple) and len(decoded) == 2 and isinstance(decoded[1], bytes)):
        return ""
    if _pkcs8_work(decoded[0]) > MAX_PBKDF_ITERATIONS:
        return _COST_TOO_HIGH_PBKDF
    if any(value > MAX_PBKDF_ITERATIONS for value in _asn1_ints(decoded[0])):
        return _COST_TOO_HIGH_PBKDF
    return ""


def private_key_cost_problem(private_key: str) -> str:
    """私钥里由登记者决定的 KDF 成本是否超限；返回问题说明，没问题返回空串

    **必须与 asyncssh 自己的解析逐字同一条路**：自己写一份正则 / base64 解码，就会与 asyncssh
    在边角写法上分叉（标记行多一个空格、正文里有一行带冒号），检查读到一个无害的值，
    asyncssh 却照常解出真正的轮数去跑 KDF。所以这里直接调 asyncssh 的
    `public_key._match_next`（`import_private_key` → `_decode_private` 用的正是它，且只解
    第一把钥匙），在它给出的结果上判：

    - `pem` 且名字是 `OPENSSH`：按 `_decode_openssh_private` 的读法取 cipher / kdf / 轮数；
    - `pem` 且名字是 `ENCRYPTED`：`der_decode` 之后按 `EncryptedPrivateKeyInfo` 判算法参数；
    - `der`（文本以 `0` 开头时 asyncssh 会先试 DER）：同上；
    - 其余（未加密的 PKCS#1 / PKCS#8、旧式 `Proc-Type: 4,ENCRYPTED` 的单轮 MD5）没有可调成本。

    asyncssh 只在给了口令短语时才跑 KDF；这里不看口令短语，统一按「会跑」判。
    解析不下去的（格式坏了）返回空串：读法与 asyncssh 逐字相同，它在跑 KDF 之前就会报错，
    那不是成本问题，交给它报（保存时仍只看标记，不做深度解析）。
    PuTTY 格式不收：v3 用 argon2，内存参数同样写在文件里。
    这里按需导入 asyncssh：总开关关着时，保存凭据也要过这道检查。
    """
    text = str(private_key or "")
    if "PuTTY-User-Key-File" in text:
        return "不支持 PuTTY 格式的私钥，请转成 OpenSSH 格式"
    try:
        data = text.encode("ascii")
    except UnicodeEncodeError:
        return "私钥只能包含 ASCII 字符"  # asyncssh 同样拒收，提前给一句人话
    try:
        from asyncssh.asn1 import der_decode
        from asyncssh.public_key import _match_next
    except ImportError:  # pragma: no cover - asyncssh 改了内部结构：宁可拒收，也不能放过不受控的 KDF
        return _COST_UNPARSEABLE
    try:
        fmt, key_info, _end = _match_next(data, b"PRIVATE KEY")
    except Exception:  # noqa: BLE001 - asyncssh 自己也会在这里报错、不会跑 KDF；不是成本问题，交给它报
        return ""
    if fmt == "der":
        return _pkcs8_cost_problem(key_info[0])
    if fmt != "pem":
        return ""
    pem_name, headers, body = key_info
    if pem_name == b"OPENSSH":
        return _openssh_cost_problem(body)
    if pem_name == b"ENCRYPTED" and headers.get(b"Proc-Type") == b"4,ENCRYPTED":
        # asyncssh 先按旧式 PEM 头（单轮 MD5）解开外层，再跑里面的 PKCS#8 KDF；外层是密文，
        # 这里读不到里面的参数。没有正常工具会这样写，直接拒
        return _COST_NESTED
    if pem_name == b"ENCRYPTED":
        try:
            decoded = der_decode(body)
        except Exception:  # noqa: BLE001 - 解不开的 DER asyncssh 也只会报错，不跑 KDF
            return ""
        return _pkcs8_cost_problem(decoded)
    return ""


def validate_alias(alias: str) -> str:
    """别名必须是工作区内唯一、可安全写进 ssh 配置的短标识"""
    if not is_valid_alias(alias):
        if alias.startswith(ALIAS_RESERVED_PREFIX):
            raise ValidationError(reason="别名不能以 nk- 开头，这个前缀留给 NA 自己写进工作区的文件")
        raise ValidationError(reason="别名只能由小写字母、数字与连字符组成，且以字母或数字开头，最长 32 个字符")
    return alias


def validate_host(host: str) -> str:
    """目标地址或域名：非空、不超长、不含空白"""
    value = host.strip()
    if not value:
        raise ValidationError(reason="主机不能为空")
    _reject_nul(value, "主机")
    if len(value) > MAX_HOST_LENGTH:
        raise ValidationError(reason=f"主机最长 {MAX_HOST_LENGTH} 个字符")
    if _WHITESPACE_RE.search(value):
        raise ValidationError(reason="主机不能包含空白字符")
    return value


def validate_port(port: int) -> int:
    """SSH 端口范围"""
    if isinstance(port, bool) or not isinstance(port, int):
        raise ValidationError(reason="端口必须是整数")
    if not MIN_PORT <= port <= MAX_PORT:
        raise ValidationError(reason=f"端口必须在 {MIN_PORT} 到 {MAX_PORT} 之间")
    return port


def validate_username(username: str) -> str:
    """目标服务器上的登录名：非空、不超长、不含空白与 `@`"""
    value = username.strip()
    if not value:
        raise ValidationError(reason="登录用户名不能为空")
    _reject_nul(value, "登录用户名")
    if len(value) > MAX_USERNAME_LENGTH:
        raise ValidationError(reason=f"登录用户名最长 {MAX_USERNAME_LENGTH} 个字符")
    if _WHITESPACE_RE.search(value):
        raise ValidationError(reason="登录用户名不能包含空白字符")
    if "@" in value:
        raise ValidationError(reason="登录用户名不能包含 @；请把它填在单独的用户名字段里")
    return value


def validate_owner_account(owner_account: str) -> str:
    """凭据主人账号：去首尾空白、最长 128"""
    value = owner_account.strip()
    _reject_nul(value, "凭据主人账号")
    if len(value) > MAX_OWNER_ACCOUNT_LENGTH:
        raise ValidationError(reason=f"凭据主人账号最长 {MAX_OWNER_ACCOUNT_LENGTH} 个字符")
    return value


def validate_private_key(private_key: str) -> str:
    """非空时必须看得出是私钥文本

    只看两段标记，不解析算法与口令短语：那是网关用 SSH 库做的事，在这里猜错只会
    把合法的旧格式挡在门外。另有两道上限：KDF 成本（`private_key_cost_problem`）与文本长度、
    钥匙位数（`private_key_size_problem`）。
    """
    value = private_key.strip()
    if len(value) > MAX_PRIVATE_KEY_CHARS:
        raise ValidationError(reason=_SIZE_TOO_LONG)
    if value and not ("-----BEGIN" in value and "PRIVATE KEY-----" in value):
        raise ValidationError(reason="私钥看起来不是私钥文本（缺少 BEGIN / PRIVATE KEY 标记）")
    if value:
        _reject_nul(value, "私钥")
        problem = private_key_cost_problem(value) or private_key_size_problem(value)
        if problem:
            raise ValidationError(reason=problem)
    return value


def _decode_host_key(line: str, line_no: int) -> Tuple[str, str, bytes]:
    """把一行主机公钥拆成 `(类型, base64, 线路字节)`，不合法时报第几行"""
    text = line.strip()
    tokens = text.split()
    if len(tokens) < 2:
        raise ValidationError(reason=f"主机公钥第 {line_no} 行格式不对，至少要写类型与 base64")
    if tokens[0] in HOST_KEY_TYPES:
        key_type, encoded = tokens[0], tokens[1]
    else:
        # known_hosts 与 ssh-keyscan 的输出：主机（或逗号分隔的多个主机）在类型前面
        if len(tokens) < 3:
            raise ValidationError(reason=f"主机公钥第 {line_no} 行格式不对，known_hosts 行应为「主机 类型 base64」")
        key_type, encoded = tokens[1], tokens[2]

    if key_type not in HOST_KEY_TYPES:
        raise ValidationError(reason=f"主机公钥第 {line_no} 行的类型 {key_type!r} 不受支持")

    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        blob = base64.b64decode(padded, validate=True)
    except Exception as exc:  # noqa: BLE001 - 任何解码异常都归为"base64 解不开"
        raise ValidationError(reason=f"主机公钥第 {line_no} 行的 base64 解不开") from exc
    if len(blob) < 4:
        raise ValidationError(reason=f"主机公钥第 {line_no} 行的内容太短，不是 SSH 公钥线格式")

    declared_length = int.from_bytes(blob[:4], "big")
    if declared_length <= 0 or declared_length > len(blob) - 4:
        raise ValidationError(reason=f"主机公钥第 {line_no} 行的 SSH 线格式长度不对")
    try:
        wire_type = blob[4 : 4 + declared_length].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValidationError(reason=f"主机公钥第 {line_no} 行的类型名不是 ASCII") from exc
    if wire_type != key_type:
        raise ValidationError(
            reason=f"主机公钥第 {line_no} 行写的类型是 {key_type!r}，但线格式里的类型是 {wire_type!r}",
        )
    return key_type, encoded, blob


def parse_host_keys(text: str) -> List[str]:
    """解析粘贴进来的主机公钥，返回去重后的 `"类型 base64"` 列表

    接受三种写法：`known_hosts` 行（`主机 类型 base64 [注释]`）、`ssh-keyscan` 的输出
    （与 known_hosts 同形）以及只有 `类型 base64` 的两段。空行与 `#` 开头的注释行忽略。

    Raises:
        ValidationError: 任意一行不合法；文案里带行号，用户才知道改哪一行。
    """
    entries: List[str] = []
    seen: set[Tuple[str, bytes]] = set()
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key_type, encoded, blob = _decode_host_key(line, line_no)
        marker = (key_type, blob)
        if marker in seen:
            continue
        seen.add(marker)
        entries.append(f"{key_type} {encoded}")
    return entries


def host_key_fingerprint(entry: str) -> str:
    """算 `ssh-keygen -lf` 那种 SHA256 指纹

    入参既可以是 `"类型 base64"`，也可以是整行 known_hosts；两种都走同一套校验，
    免得"列表里的指纹"与"粘贴时的校验"各算一套。
    """
    parsed = parse_host_keys(entry)
    if not parsed:
        raise ValidationError(reason="空的主机公钥没有指纹")
    _, encoded = parsed[0].split(" ", 1)
    padded = encoded + "=" * (-len(encoded) % 4)
    blob = base64.b64decode(padded, validate=True)
    digest = hashlib.sha256(blob).digest()
    encoded_digest = base64.b64encode(digest).decode("ascii").rstrip("=")
    return f"{FINGERPRINT_PREFIX}{encoded_digest}"


def credential_secret_values(password: str, private_key: str, passphrase: str) -> dict[str, str]:
    """把三个凭据字段收成"列名 -> 明文"，供服务层判断至少填了一样"""
    return dict(zip(CREDENTIAL_SECRET_COLUMNS, (password, private_key, passphrase)))


__all__ = [
    "MAX_BCRYPT_ROUNDS",
    "MAX_DSA_KEY_BITS",
    "MAX_HOST_LENGTH",
    "MAX_OWNER_ACCOUNT_LENGTH",
    "MAX_PBKDF_ITERATIONS",
    "MAX_PORT",
    "MAX_PRIVATE_KEY_CHARS",
    "MAX_RSA_KEY_BITS",
    "MAX_USERNAME_LENGTH",
    "MIN_PORT",
    "credential_secret_values",
    "host_key_fingerprint",
    "parse_host_keys",
    "private_key_cost_problem",
    "private_key_size_problem",
    "public_key_size_problem",
    "validate_alias",
    "validate_host",
    "validate_owner_account",
    "validate_port",
    "validate_private_key",
    "validate_username",
]
