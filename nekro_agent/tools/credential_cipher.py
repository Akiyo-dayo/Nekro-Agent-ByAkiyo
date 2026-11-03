"""凭据的静态加密（encryption at rest）

模型组的 `api_key` 此前是明文落库（`db_model_group.api_key = TextField()`）。一次库泄露
或一份备份就等于所有 Key 全裸，凭据隔离也就无从谈起，所以这一列必须在库里是密文。

## 威胁模型：这挡什么、不挡什么（**不要把它说成是万能的**）

| 场景 | 挡得住吗 |
|---|---|
| `pg_dump` / 数据库备份文件外流 | ✅ 那一列是密文，单看它推不出 Key |
| 只读拿到数据库连接（SQL 注入、只给了只读账号的运维） | ✅ 同上 |
| 有人从数据库以外的地方读到了密钥 | ❌ 见下 |
| **宿主被拿下** | ❌ **挡不住。** 密钥就在同一台机器上（环境变量，或数据目录里的密钥文件）， |
| | 能在这台机器上以 NA 的身份执行代码的人同样能解密。 |

⚠️ 磁盘上还有同一份 Key 的**第二份载体**：`services/model_group_service.refresh_runtime_view()`
会把运行时视图 dump 回配置文件（`<数据目录>/configs/nekro-agent.yaml`）。自 #61 修复后，
那份 YAML 里的 `API_KEY` 不再含明文：有 Key 的组是占位符 `nkmask:1`、没配 Key 的组是空串
（`core/config.py`），**明文与密文都不落盘**，
整目录打包的备份不再带出 Key。代价是 YAML 从此**不是凭据的恢复来源**：密钥材料丢了，
唯一的恢复路径是把密钥材料找回来（环境变量或 `credential_secret.key` 的备份），
处置见 `docs/operations.md` §1.5 与 §2.1。

⚠️ 前车之鉴：`db_workspace_resource.secret_payload_encrypted`
这个字段名里写着 encrypted，存的却是明文 JSON。**不要再造一个名字骗人的东西**：
本模块存进去的每一个值都带 `nkenc1:` 前缀，密文自己说明自己是密文；
没有前缀的值就是明文，读的时候按明文处理，不假装。

## 密钥从哪来（按优先级）

1. **环境变量 `NEKRO_CREDENTIAL_SECRET_KEY`** —— 推荐形态。密钥不落在数据目录里，
   备份整个数据目录也带不走它。
2. **数据目录里的密钥文件** `<DATA_DIR>/credential_secret.key` —— 环境变量没配时
   **自动生成**一把（0600），并打一条 WARNING 说明发生了什么。

第 2 档是刻意选的默认值，理由写在这里以免下一轮有人"顺手改成拒绝启动"：

- **拒绝启动**会让每一套既有部署（上游标准 Docker 部署、单实例裸机部署）在升级后
  当场起不来，与"未启用多租户的部署、平台方行为逐字不变"这条硬约束直接冲突；
- **降级明文 + 告警**会让"什么都不配的部署"永远裸奔 —— 而那正是绝大多数部署的形态，
  告警在 app.log 里躺三个月没人看；
- 自动生成文件密钥让"什么都不配的部署"**落在安全的那一侧**（库 dump 仍然读不出 Key），
  同时一个字都不用改就能升级；想要更强的姿态就配环境变量，那时文件密钥自动降级为
  只用于解密的历史密钥（见下）。

只有当密钥文件**既读不出也写不了**（只读文件系统、权限不对）时才落到第三档：
明文写入 + 每次 ERROR。此时行为与改造前逐字相同，但不会静默。

## 密钥轮换 / 从文件搬到环境变量

解密时按 `[主密钥, *历史密钥]` 依次尝试；写入永远只用主密钥。
配了环境变量之后，数据目录里那把旧密钥自动成为历史密钥，于是：

- 存量密文照常解得开（不会因为搬了一次密钥就全部作废）；
- `services/model_group_service.upgrade_credentials_at_rest()` 在启动时把"不是用主密钥
  加密的"那些行整批重新加密，一次重启完成轮换。

主密钥换成一把与任何历史密钥都对不上的值时，那些行会**解不开**：此时返回空串并打 ERROR，
**不会**把密文当成 Key 发给上游（那只会在上游侧留下一次带密文的鉴权失败）。

⚠️ 解不开的行**不会被无脑抹掉**。启动时的整批升级（`upgrade_credentials_at_rest()`）
跳过它们并在日志里逐个点名；自 #64 修复后，`DBModelGroup.save()` 上还有一道回查守卫：
解不开的行在任何一次普通保存里也原样保留 `api_key` 原列（后台改个名字、走一遍
"掩码=保持不变"都不再毁掉密文），落点与语义见 `models/db_model_group.py` 模块文档。
仍会覆盖它的只剩"显式填入新 Key"——那本来就是一次覆盖指令。

## 信封格式是**跨版本的持久化契约**

`ENVELOPE_PREFIX`、`_KDF_SALT`、`_KDF_CONTEXT`、`_AAD`、`_NONCE_LEN`/`_TAG_LEN` 与
`nonce|tag|ct` 这个排列，决定的不是"本进程怎么加密"，而是"**别人库里已经躺着的那一串**
还解不解得出来"。改它们里的任何一个都不是重命名，是一次数据迁移：

- 改 `ENVELOPE_PREFIX` → 存量密文不再被认成信封，落进"存量明文"分支被**原样返回**，
  于是一串 `nkenc1:...` 会被当成 API Key 发给上游（正是上面承诺不会发生的那件事）；
- 改 KDF 参数或 AAD → 同一把主密钥派生出不同的 AES 密钥，存量凭据当场全灭。

这两类改动**不会**被"本进程加密→本进程解密"的 round-trip 用例发现（换套参数照样自洽），
所以 `tests/test_model_group_credential_at_rest.py::TestStoredFormatIsAContractWithDataAlreadyOnDisk`
钉了一条离线金标准向量。真要换算法：加一个 `nkenc2:` 前缀、读侧同时支持两代、
再补一条新的金标准向量，不要改这一代的常量。
"""

from __future__ import annotations

import base64
import os
import secrets
import stat
from pathlib import Path
from typing import Literal, NamedTuple, Optional, Tuple

from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import HKDF

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv

logger = get_sub_logger("credential_cipher")

# 密文信封前缀。存量明文 Key 不可能以它开头，因此"这个值是不是密文"永远是结构判定，
# 不需要另外加一个 `is_encrypted` 布尔列（那种列一旦与实际内容不一致就是灾难）
ENVELOPE_PREFIX = "nkenc1:"

# 密钥文件名。放在数据目录下：那是每套部署唯一不共用的目录（见 `os_env.derive_deployment_scope`）
KEY_FILE_NAME = "credential_secret.key"

_KDF_SALT = b"nekro-agent/credential-cipher/v1"
_KDF_CONTEXT = b"model-group-credentials"
# GCM 的附加认证数据：把密文绑定到"本项目的凭据列"这个用途上，
# 换个用途的密文即使用同一把密钥也验不过
_AAD = b"nekro-agent:credential-at-rest:v1"

_NONCE_LEN = 12
_TAG_LEN = 16

KeySource = Literal["env", "file", "none"]

# 解密结果的来源分档。`upgrade_credentials_at_rest()` 靠它决定哪些行需要重写
PlaintextSource = Literal["empty", "plaintext", "primary", "fallback", "undecryptable"]

# 需要用主密钥重写一遍的两档。`needs_reencrypt()` 与
# `services/model_group_service.upgrade_credentials_at_rest()` **共用这一份**，
# 免得"哪些算陈旧"出现第二套判定（`undecryptable` 有意不在其中，理由见 `needs_reencrypt`）
REENCRYPT_SOURCES: Tuple[PlaintextSource, ...] = ("plaintext", "fallback")


class CipherStatus(NamedTuple):
    """当前进程的加密可用状态，供启动日志与测试断言使用"""

    source: KeySource
    # 密钥文件是不是本进程刚生成的（只在 source == "file" 时有意义）
    generated: bool
    # 可用于解密的历史密钥数量
    fallback_count: int

    @property
    def available(self) -> bool:
        """能不能加密。为假时写入退化为明文"""
        return self.source != "none"

    def describe(self) -> str:
        """一句话说明当前处在哪一档，直接进启动日志"""
        if self.source == "env":
            return f"环境变量 NEKRO_CREDENTIAL_SECRET_KEY（历史密钥 {self.fallback_count} 把）"
        if self.source == "file":
            hint = "本次自动生成" if self.generated else "沿用已有"
            return f"数据目录密钥文件 {KEY_FILE_NAME}（{hint}）"
        return "无可用密钥"


class _KeyRing(NamedTuple):
    """主密钥 + 历史密钥"""

    primary: Optional[bytes]
    fallbacks: Tuple[bytes, ...]
    status: CipherStatus


_key_ring: Optional[_KeyRing] = None


def reset_credential_cipher_cache() -> None:
    """丢掉进程内缓存的密钥

    密钥来源全部从 `OsEnv` 与数据目录在**调用时**读取
    （由环境派生的值不要在 import 期定死），测试改完 `OsEnv.DATA_DIR` /
    `OsEnv.CREDENTIAL_SECRET_KEY` 之后调一次这个函数即可。
    """
    global _key_ring  # noqa: PLW0603
    _key_ring = None


def _derive_key(material: str) -> bytes:
    """把任意长度的密钥材料派生成 32 字节 AES 密钥

    用 HKDF 而不是直接 sha256：密钥材料是运维手填的字符串，长度与熵都不可控，
    HKDF 的 extract 阶段正是为这种情形准备的。
    """
    derived = HKDF(
        master=material.encode("utf-8"),
        key_len=32,
        salt=_KDF_SALT,
        hashmod=SHA256,
        context=_KDF_CONTEXT,
    )
    # pycryptodome 的 HKDF 在 num_keys=1 时返回 bytes，多把时返回 tuple；这里恒为单把
    return derived if isinstance(derived, bytes) else derived[0]


def _key_file_path() -> Path:
    """密钥文件的绝对路径（数据目录在调用时读，不缓存）"""
    return Path(OsEnv.DATA_DIR).expanduser() / KEY_FILE_NAME


def _key_file_mode(path: Path) -> Optional[int]:
    """密钥文件的 POSIX 权限位（`S_IMODE`）；非 POSIX 或读不到时返回 None

    Windows 上 `st_mode` 只反映只读位、恒显示 0666，拿它判「过宽」是永远在误报，
    所以那里不检查（真正的 ACL 要用 `icacls`，见 `model_group_service` 对手填 Key 备份的处理）。
    """
    if os.name != "posix":
        return None
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def _warn_if_key_file_permissions_too_open(path: Path) -> None:
    """密钥文件的组 / 其他用户有任何权限位就告警（POSIX）

    这个文件是模型组凭据的**主密钥**：同机任何能读它的账户都能解开库里全部模型组密文，
    凭据隔离就形同虚设。应用自己建它时是 0600，会被放开通常是老版本
    安装脚本的 `chmod -R 777`（现已改成排除它并纠正回 0600，见 `docker/install.sh`）。
    判据是「组 / 其他有任何位」而不猜哪一位危险。只告警、不自动改：权限是运维的现场，
    应用不替他收（也可能根本不是属主），与出厂默认口令告警同一口径。
    """
    mode = _key_file_mode(path)
    if mode is None or mode & 0o077 == 0:
        return
    logger.warning(
        f"凭据密钥文件权限过宽（{path}，当前 {mode:04o}）：同机其他账户可读它就能解开库里全部模型组密文。"
        f"请收紧：chmod 600 {path}。"
        "（如果它是被安装脚本放开的：现行的 install 脚本已排除它并会纠正回 0600，重跑一遍即可。）",
    )


def _read_key_file(path: Path) -> Optional[str]:
    """读出密钥文件的内容；不存在或读不了都返回 None

    读到内容时顺带检查一次权限（`_load_key_ring` 的结果按进程缓存，所以每个进程至多告警一次）。
    """
    try:
        if not path.is_file():
            return None
        material = path.read_text(encoding="utf-8").strip()
    except OSError as e:
        logger.error(f"凭据密钥文件读取失败（{path}）：{e}")
        return None
    _warn_if_key_file_permissions_too_open(path)
    return material or None


def _create_key_file(path: Path) -> Optional[str]:
    """生成一把新密钥并写入密钥文件

    用 `O_EXCL` 建文件：两个进程同时启动时只有一个能建成，另一个回头去读，
    不会出现"各写各的密钥、后写的把先写的覆盖掉"。
    Windows 上 `mode` 参数被忽略，权限收紧只在 POSIX 上生效 —— 这一条写在这里，
    免得下一轮有人拿 Windows 上的 `stat` 结果当成缺陷。

    建不出来时返回 None（调用方据此落到"明文 + 每次 ERROR"那一档），**不抛异常**：
    一个凭据加密功能不该让整套部署起不来。
    """
    material = secrets.token_urlsafe(48)
    # 建目录与建文件分成两个 try 是有意的：`mkdir` 在"父路径是个文件"时，
    # Windows 抛 FileExistsError、POSIX 抛 NotADirectoryError。和下面 `O_EXCL` 的
    # FileExistsError（那才是"另一个进程刚建好"）混在一个 try 里，前者会被当成后者，
    # 于是一个真实的"密钥建不出来"被静默读成 None，连 ERROR 都不打。
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error(
            f"凭据密钥文件的目录创建失败（{path.parent}）：{e}。"
            "本次启动模型组凭据将**以明文写入数据库**——请配置环境变量 "
            "NEKRO_CREDENTIAL_SECRET_KEY，或让数据目录可写。",
        )
        return None
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # 两个进程同时启动，另一个先建好了：回头去读它那把，不要各写各的
        return _read_key_file(path)
    except OSError as e:
        logger.error(
            f"凭据密钥文件创建失败（{path}）：{e}。"
            "本次启动模型组凭据将**以明文写入数据库**——请配置环境变量 "
            "NEKRO_CREDENTIAL_SECRET_KEY，或让数据目录可写。",
        )
        return None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(material)
    except OSError as e:  # pragma: no cover - 建得出却写不进去只在磁盘满/被拔时发生
        logger.error(f"凭据密钥文件写入失败（{path}）：{e}")
        return None
    logger.warning(
        f"未配置 NEKRO_CREDENTIAL_SECRET_KEY，已在数据目录自动生成一把凭据加密密钥（{path}，权限 0600）。"
        "它保护的是「数据库被 dump / 备份外流」，**挡不住宿主被拿下**（密钥就在同一台机器上）。"
        "⚠️ 这个文件丢了 = 库里所有模型组 API Key 都解不开（需要在后台重新填一次）；"
        "备份数据目录时请一并备份，或改配环境变量把密钥挪出数据目录。",
    )
    return material


def _load_key_ring() -> _KeyRing:
    """解析出本进程的主密钥与历史密钥"""
    env_material = (OsEnv.CREDENTIAL_SECRET_KEY or "").strip()
    key_path = _key_file_path()

    if env_material:
        # 配了环境变量：数据目录里那把（如果有）降级为只用于解密的历史密钥，
        # 于是"先跑起来再把密钥挪进环境变量"这条常见路径不会让存量密文全部作废
        file_material = _read_key_file(key_path)
        fallbacks = (
            (_derive_key(file_material),) if file_material and file_material != env_material else ()
        )
        return _KeyRing(
            primary=_derive_key(env_material),
            fallbacks=fallbacks,
            status=CipherStatus(source="env", generated=False, fallback_count=len(fallbacks)),
        )

    existing = _read_key_file(key_path)
    if existing:
        return _KeyRing(
            primary=_derive_key(existing),
            fallbacks=(),
            status=CipherStatus(source="file", generated=False, fallback_count=0),
        )

    created = _create_key_file(key_path)
    if created:
        return _KeyRing(
            primary=_derive_key(created),
            fallbacks=(),
            status=CipherStatus(source="file", generated=True, fallback_count=0),
        )
    return _KeyRing(
        primary=None,
        fallbacks=(),
        status=CipherStatus(source="none", generated=False, fallback_count=0),
    )


def _key_ring_cached() -> _KeyRing:
    global _key_ring  # noqa: PLW0603
    if _key_ring is None:
        _key_ring = _load_key_ring()
    return _key_ring


def credential_cipher_status() -> CipherStatus:
    """当前的密钥来源与可用性（会触发一次密钥解析）"""
    return _key_ring_cached().status


def is_encrypted_envelope(value: str) -> bool:
    """这个存储值是不是本模块产出的密文信封

    **结构判定**，不是"以前缀开头"就算：前缀之后必须是能解出 base64 且长度足够容纳
    nonce 与 tag 的字节串。这样一把恰好以 `nkenc1:` 开头的明文 Key 会被判成明文
    （走存量兼容路径原样返回），而不是被当成坏掉的密文丢掉。
    """
    if not value.startswith(ENVELOPE_PREFIX):
        return False
    return _decode_envelope(value) is not None


def _decode_envelope(value: str) -> Optional[bytes]:
    """把信封解成原始字节；结构不对返回 None"""
    payload = value[len(ENVELOPE_PREFIX) :]
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding)
    except (ValueError, TypeError):
        return None
    if len(raw) < _NONCE_LEN + _TAG_LEN:
        return None
    return raw


def encrypt_credential(plain: str) -> str:
    """把明文凭据封成密文信封

    空串原样返回：没有东西要保护，而"空 Key"是个有意义的常见状态
    （很多模型组本来就不填 Key），让它在库里保持可读的空串比封成一坨密文更好排障。
    没有可用密钥时原样返回明文并打 ERROR —— 静默降级才是最坏的那一档。
    """
    if not plain:
        return ""
    ring = _key_ring_cached()
    if ring.primary is None:
        logger.error(
            "凭据静态加密不可用（无可用密钥），本次写入为**明文**。"
            "请配置 NEKRO_CREDENTIAL_SECRET_KEY 或让数据目录可写后重启。",
        )
        return plain
    nonce = os.urandom(_NONCE_LEN)
    cipher = AES.new(ring.primary, AES.MODE_GCM, nonce=nonce)
    cipher.update(_AAD)
    ciphertext, tag = cipher.encrypt_and_digest(plain.encode("utf-8"))
    payload = base64.urlsafe_b64encode(nonce + tag + ciphertext).decode("ascii").rstrip("=")
    return f"{ENVELOPE_PREFIX}{payload}"


def _try_decrypt(raw: bytes, key: bytes) -> Optional[str]:
    """用一把密钥尝试解密；验不过返回 None"""
    nonce = raw[:_NONCE_LEN]
    tag = raw[_NONCE_LEN : _NONCE_LEN + _TAG_LEN]
    body = raw[_NONCE_LEN + _TAG_LEN :]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(_AAD)
    try:
        return cipher.decrypt_and_verify(body, tag).decode("utf-8")
    except (ValueError, KeyError, UnicodeDecodeError):
        return None


def decrypt_credential_with_source(stored: str) -> Tuple[str, PlaintextSource]:
    """解密并同时说明"这个值原本是怎么存的"

    分档存在的意义是让"需要重新加密的行"可枚举（存量明文、以及用历史密钥加密的行），
    见 `services/model_group_service.upgrade_credentials_at_rest()`。
    """
    if not stored:
        return "", "empty"
    raw = _decode_envelope(stored) if stored.startswith(ENVELOPE_PREFIX) else None
    if raw is None:
        # 存量明文：读时兼容，原样返回。写侧会在下一次保存时自动升级成密文
        return stored, "plaintext"

    ring = _key_ring_cached()
    if ring.primary is not None:
        plain = _try_decrypt(raw, ring.primary)
        if plain is not None:
            return plain, "primary"
    for key in ring.fallbacks:
        plain = _try_decrypt(raw, key)
        if plain is not None:
            return plain, "fallback"

    logger.error(
        "模型组凭据解密失败：库里是密文，但当前密钥解不开它（密钥被换过、或密钥文件丢了）。"
        "本次按空凭据处理——**不会**把密文当成 API Key 发给上游。"
        "请恢复原密钥，或在后台重新填写该模型组的 API Key。",
    )
    return "", "undecryptable"


def decrypt_credential(stored: str) -> str:
    """把存储值还原成明文凭据"""
    return decrypt_credential_with_source(stored)[0]


def classify_stored_credential(stored: str) -> PlaintextSource:
    """库里那一列**当前是什么状态**（只回答分档，不回传明文）

    存在的意义是让调用方一趟拿全两个答案：哪些行要重写、哪些行解不开。
    分两次问会让"解不开"那条 ERROR 被重复打一遍，也会给这份判定开出第二个入口。
    """
    return decrypt_credential_with_source(stored)[1]


def needs_reencrypt(stored: str) -> bool:
    """这个存储值是否需要用主密钥重新写一遍

    两种情形（`REENCRYPT_SOURCES`）：存量明文（从未加密过）、
    以及用历史密钥加密的密文（密钥搬过家）。

    解不开的密文**不算**——重写它只会把一个还有可能被原密钥救回来的值彻底抹掉。
    普通保存路径上另有一道回查守卫兜着（`DBModelGroup.save()`），
    完整口径见模块文档顶部那段说明。
    """
    if not stored:
        return False
    return classify_stored_credential(stored) in REENCRYPT_SOURCES
