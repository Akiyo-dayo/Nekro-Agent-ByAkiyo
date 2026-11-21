"""网关主机钥匙与容器专用客户端钥匙

## 为什么主机钥匙文件损坏时不能覆盖

容器里的 `known_hosts` 钉的是这把公钥。文件读不出来时重新生成一把，等于让所有已经
写好的 `known_hosts` 立刻对不上：下一次连接会报主机密钥不符，而真正的坏事（磁盘坏了、
文件被截断、有人动过）被一次"自动修复"掩盖过去。所以只在文件**不存在**时生成；
存在但读不出就抛错，让监督任务把网关标成「不可用：主机钥匙读不出」，由平台方处理。

## 为什么用 OpenSSH 私钥格式

容器侧与 NA 侧都只认 OpenSSH 的线格式。`cryptography` 直接能写以 BEGIN OPENSSH 开头的
那一段文本，不引入命令行 `ssh-keygen`，也不依赖宿主工具。

## 权限

私钥权限尽量收到 0600。Windows 上 `chmod` 能改的东西有限（ACL 才是真正的权限），
所以失败只记 WARNING，不拦住网关启动——文件所在目录本来就只有 NA 自己能进。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Tuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv

logger = get_sub_logger("ssh_gateway.keys")

_HOST_KEY_DIR = "ssh_gateway"
_HOST_KEY_NAME = "host_ed25519"


class HostKeyUnavailableError(RuntimeError):
    """主机钥匙文件存在但读不出来；调用方据此把网关标成不可用"""


def gateway_host_key_path() -> Path:
    """网关主机钥匙的存放路径（不一定已经存在）"""
    return Path(OsEnv.DATA_DIR) / _HOST_KEY_DIR / _HOST_KEY_NAME


def _load_private_key(path: Path):
    """读一把不加密的 OpenSSH 私钥；任何读取/解析错误都转成 `HostKeyUnavailableError`"""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise HostKeyUnavailableError(f"读不出主机钥匙文件: {path}") from exc
    try:
        return serialization.load_ssh_private_key(data, password=None)
    except (ValueError, TypeError) as exc:
        raise HostKeyUnavailableError(f"主机钥匙文件损坏，拒绝覆盖: {path}") from exc


def load_or_create_host_key() -> Path:
    """返回主机钥匙路径；不存在就生成 ed25519

    Raises:
        HostKeyUnavailableError: 文件存在但读不出。**不会**覆盖它。
    """
    path = gateway_host_key_path()
    if path.exists():
        # 只为尽早发现损坏；真正的读取由调用方按需再做
        _load_private_key(path)
        return path

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HostKeyUnavailableError(f"建不了主机钥匙目录: {path.parent}") from exc

    private = Ed25519PrivateKey.generate()
    payload = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )

    # 先写临时文件再替换：写到一半被打断不会留下一个"存在但坏掉"的钥匙，
    # 那种状态在下次启动时会被判成损坏并一直不可用
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_bytes(payload)
        try:
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:  # noqa: PERF203 - Windows 上这是预期会失败的尽力而为
            logger.warning(f"主机钥匙权限没能收到 0600（已忽略，继续启动）: {exc}")
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise HostKeyUnavailableError(f"写不了主机钥匙文件: {path}") from exc
    return path


def gateway_host_public_key() -> str:
    """主机钥匙对应的 OpenSSH 公钥行，不含注释"""
    path = load_or_create_host_key()
    private = _load_private_key(path)
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )
    return public.decode("ascii").strip()


def generate_client_keypair() -> Tuple[str, str]:
    """生成一把网关专用客户端钥匙

    Returns:
        `(OpenSSH 私钥 PEM 文本, 公钥行)`。私钥不加密——它只开网关，且要能直接写进
        容器里的 `id_gateway`。
    """
    private = Ed25519PrivateKey.generate()
    private_text = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_text = private.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode("ascii").strip()
    return private_text, public_text


def public_key_line(text: str) -> str:
    """把带注释的公钥行规整成「类型 base64」两段

    格式不对时原样返回去掉首尾空白的内容：比较结果自然是不相等，不需要在这里抛错。
    """
    parts = str(text or "").strip().split()
    if len(parts) < 2:
        return str(text or "").strip()
    return f"{parts[0]} {parts[1]}"


__all__ = [
    "HostKeyUnavailableError",
    "gateway_host_key_path",
    "gateway_host_public_key",
    "generate_client_keypair",
    "load_or_create_host_key",
    "public_key_line",
]
