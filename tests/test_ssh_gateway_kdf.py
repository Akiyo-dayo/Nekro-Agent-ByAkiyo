"""私钥 KDF：成本检查与 asyncssh 走同一条解析路，KDF 在能杀掉的子进程里跑

## 这条测试钉的是什么

- 成本检查读的是 asyncssh 自己的解析结果。独立复核找到的两种绕过——标记行里多一个空格、
  正文里有一行带冒号——以及 `ENCRYPTED` 标记行多空格（PBKDF2 迭代次数不限），每一种都先用
  一把**低成本**的同格式私钥证明 asyncssh 真的认这种写法（能导入），再证明**高成本**的同格式
  私钥被检查拒掉；
- PKCS#8 的成本按「迭代次数 × 块数」算（PBKDF2 的 keyLength、PBES1、PKCS#12 各自的块数），
  `Proc-Type` 套 `ENCRYPTED PRIVATE KEY` 的两层加密直接拒（先证明 asyncssh 真的会解开它）；
- 文本长度与 RSA 位数有上限：读得到位数的格式在保存与连接前就拒，加密的格式在子进程解开
  之后、校验之前拒；
- 所有解析（带不带口令短语）都在子进程里跑：正常能解出钥匙；超时会杀掉子进程；同一把私钥
  同时只解析一次，调用方被取消不会取消它；失败的结果缓存一阵、不再重新解析；名额每个工作区
  一个，排在全局名额之前。

不跑真正的高成本 KDF：高成本的私钥只改轮数 / 迭代次数字段，用来喂成本检查。
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import asyncssh
import pytest
from asyncssh.asn1 import der_decode, der_encode
from asyncssh.packet import MPInt, String, UInt32

from nekro_agent.schemas.errors import ValidationError
from nekro_agent.schemas.ssh_gateway import PRIVATE_KEY_MAX_LENGTH
from nekro_agent.services.ssh_gateway import constants as C
from nekro_agent.services.ssh_gateway import upstream, validation
from nekro_agent.services.ssh_gateway.authority import TargetSpec
from nekro_agent.services.ssh_gateway.validation import (
    MAX_BCRYPT_ROUNDS,
    MAX_PBKDF_ITERATIONS,
    MAX_PRIVATE_KEY_CHARS,
    MAX_RSA_KEY_BITS,
    private_key_cost_problem,
    private_key_size_problem,
    validate_private_key,
)

PASSPHRASE = "phrase-" + "kdf7"
TARGET_IP = "9.9.9." + "9"


@pytest.fixture(autouse=True)
def fresh_upstream_state():
    upstream.reset_upstream_state()
    yield
    upstream.reset_upstream_state()


# ---------------------------------------------------------------------------
# 构造各种写法的私钥
# ---------------------------------------------------------------------------


def _pem_lines(text: str) -> tuple[str, list[str], str]:
    lines = text.strip().splitlines()
    return lines[0], lines[1:-1], lines[-1]


def _rewrap(header: str, body: bytes, footer: str, *, width: int = 70) -> str:
    encoded = base64.b64encode(body).decode("ascii")
    wrapped = "\n".join(encoded[i : i + width] for i in range(0, len(encoded), width))
    return f"{header}\n{wrapped}\n{footer}\n"


def _double_space(text: str) -> str:
    """标记行里 BEGIN / END 后面多一个空格：asyncssh 会先 strip 掉名字两边的空白"""
    return text.replace("-----BEGIN ", "-----BEGIN  ").replace("-----END ", "-----END  ")


def _openssh_key(rounds: int) -> str:
    """一把带口令短语的 OpenSSH 私钥；`rounds` 不是 16 时只改字段（不真的跑那么多轮）"""
    key = asyncssh.generate_private_key("ssh-ed25519")
    text = key.export_private_key("openssh", passphrase=PASSPHRASE, rounds=16).decode("ascii")
    if rounds == 16:
        return text
    header, lines, footer = _pem_lines(text)
    data = bytearray(base64.b64decode("".join(lines)))
    pos = len(b"openssh-key-v1\x00")
    for _ in range(2):  # 跳过 ciphername、kdfname
        length = int.from_bytes(data[pos : pos + 4], "big")
        pos += 4 + length
    pos += 4  # kdfoptions 的长度
    salt_length = int.from_bytes(data[pos : pos + 4], "big")
    pos += 4 + salt_length
    data[pos : pos + 4] = int(rounds).to_bytes(4, "big")
    return _rewrap(header, bytes(data), footer)


def _with_colon_line(text: str) -> str:
    """正文按 16 字一行重排，除第一行外每行中间插一个冒号

    asyncssh 的 `_parse_pem` 只把**开头**连续带冒号的行当 PEM 头部，之后的行整体交给
    `a2b_base64`，它会跳过冒号；旧的检查却把带冒号的行整行丢掉。轮数 / 迭代次数落在前
    一百来个字节里，按 16 字一行时一定在某个带冒号的行上。第一行不插，免得被当成头部。
    """
    header, lines, footer = _pem_lines(text)
    encoded = "".join(lines)
    narrow = [encoded[i : i + 16] for i in range(0, len(encoded), 16)]
    shaped = [narrow[0]] + [line[:8] + ":" + line[8:] if len(line) > 8 else line for line in narrow[1:]]
    return header + "\n" + "\n".join(shaped) + "\n" + footer + "\n"


def _replace_ints(value: Any, old: int, new: int) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return new if value == old else value
    if isinstance(value, tuple):
        return tuple(_replace_ints(item, old, new) for item in value)
    if isinstance(value, list):
        return [_replace_ints(item, old, new) for item in value]
    return value


def _pkcs8_key(
    iterations: int,
    *,
    cipher_name: str = "aes256-cbc",
    hash_name: str = "sha256",
    pbe_version: int = 2,
) -> str:
    """一把口令短语加密的 PKCS#8 私钥；迭代次数不是 2048 时只改字段"""
    key = asyncssh.generate_private_key("ssh-ed25519")
    text = key.export_private_key(
        "pkcs8-pem",
        passphrase=PASSPHRASE,
        cipher_name=cipher_name,
        hash_name=hash_name,
        pbe_version=pbe_version,
        rounds=2048,
    ).decode("ascii")
    if iterations == 2048:
        return text
    header, lines, footer = _pem_lines(text)
    decoded = der_decode(base64.b64decode("".join(lines)))
    algorithm, encrypted = decoded
    patched = (_replace_ints(algorithm, 2048, iterations), encrypted)
    return _rewrap(header, der_encode(patched), footer)


def _with_key_length(text: str, iterations: int, key_length: int) -> str:
    """PBES2 / PBKDF2 参数里插进 keyLength（asyncssh 读它，派生长度由文件决定）"""
    header, lines, footer = _pem_lines(text)
    (oid, (kdf, enc)), encrypted = der_decode(base64.b64decode("".join(lines)))
    kdf_oid, kdf_fields = kdf
    salt, _count, *rest = kdf_fields
    patched = ((oid, ((kdf_oid, (salt, iterations, key_length, *rest)), enc)), encrypted)
    return _rewrap(header, der_encode(patched), footer)


# ---------------------------------------------------------------------------
# 成本检查与 asyncssh 认同一种写法
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "shape"),
    [
        ("标准写法", lambda text: text),
        ("标记行多一个空格", _double_space),
        ("正文里有一行带冒号", _with_colon_line),
        ("两样都有", lambda text: _with_colon_line(_double_space(text))),
    ],
)
def test_openssh_variants_are_read_like_asyncssh_reads_them(label: str, shape: Any) -> None:
    cheap = shape(_openssh_key(16))
    # asyncssh 认这种写法（真的跑 16 轮）
    asyncssh.import_private_key(cheap, PASSPHRASE)
    assert private_key_cost_problem(cheap) == "", label
    assert private_key_cost_problem(shape(_openssh_key(MAX_BCRYPT_ROUNDS))) == "", label
    assert "轮数过高" in private_key_cost_problem(shape(_openssh_key(16_777_216))), label


@pytest.mark.parametrize(
    ("label", "shape"),
    [
        ("标准写法", lambda text: text),
        ("标记行多一个空格", _double_space),
        ("正文里有一行带冒号", _with_colon_line),
    ],
)
def test_pkcs8_variants_are_read_like_asyncssh_reads_them(label: str, shape: Any) -> None:
    cheap = shape(_pkcs8_key(2048))
    asyncssh.import_private_key(cheap, PASSPHRASE)
    assert private_key_cost_problem(cheap) == "", label
    assert "加密参数过高" in private_key_cost_problem(shape(_pkcs8_key(MAX_PBKDF_ITERATIONS + 1))), label


def test_unencrypted_and_odd_inputs() -> None:
    key = asyncssh.generate_private_key("ssh-ed25519")
    plain = key.export_private_key("openssh").decode("ascii")
    assert private_key_cost_problem(plain) == ""
    assert private_key_cost_problem("") == ""
    assert private_key_cost_problem("just some words") == ""
    assert "ASCII" in private_key_cost_problem(plain + "密")
    assert "PuTTY" in private_key_cost_problem("PuTTY-User-Key-File-3: ssh-ed25519\n")


# ---------------------------------------------------------------------------
# PKCS#8 的成本按「迭代次数 × 块数」算
# ---------------------------------------------------------------------------


def test_pbkdf2_key_length_multiplies_the_cost() -> None:
    """keyLength 由文件决定：两个各两万的值单看都在上限内，乘起来要跑将近一分钟"""
    cheap = _pkcs8_key(2048)
    assert private_key_cost_problem(_with_key_length(cheap, 2048, 32)) == ""
    assert "加密参数过高" in private_key_cost_problem(_with_key_length(cheap, 20_000, 20_000))


def test_pbkdf2_blocks_follow_the_prf_output_size() -> None:
    """SHA1 每块 20 字节，AES-256 的 32 字节要两块：迭代次数在上限内，工作量超了"""
    cheap = _pkcs8_key(2048, hash_name="sha1")
    asyncssh.import_private_key(cheap, PASSPHRASE)
    assert private_key_cost_problem(_pkcs8_key(400_000, hash_name="sha1")) == ""
    assert "加密参数过高" in private_key_cost_problem(_pkcs8_key(600_000, hash_name="sha1"))


@pytest.mark.parametrize(
    ("cipher_name", "hash_name", "allowed", "rejected"),
    [
        ("des-cbc", "md5", 400_000, 600_000),  # PBES1：MD5 要递归算两遍
        ("des3-cbc", "sha1", 300_000, 400_000),  # PKCS#12：密钥两块 + IV 一块
    ],
)
def test_pbes1_and_pkcs12_count_their_blocks(cipher_name: str, hash_name: str, allowed: int, rejected: int) -> None:
    cheap = _pkcs8_key(2048, cipher_name=cipher_name, hash_name=hash_name, pbe_version=1)
    asyncssh.import_private_key(cheap, PASSPHRASE)
    assert (
        private_key_cost_problem(_pkcs8_key(allowed, cipher_name=cipher_name, hash_name=hash_name, pbe_version=1)) == ""
    )
    assert "加密参数过高" in private_key_cost_problem(
        _pkcs8_key(rejected, cipher_name=cipher_name, hash_name=hash_name, pbe_version=1),
    )


def _proc_type_nested(text: str) -> str:
    """把一把 `ENCRYPTED PRIVATE KEY` 的 DER 再按旧式 PEM 头（单轮 MD5）加密一层"""
    from asyncssh.pbe import pkcs1_encrypt

    header, lines, footer = _pem_lines(text)
    dek_name, iv, encrypted = pkcs1_encrypt(base64.b64decode("".join(lines)), "aes128-cbc", PASSPHRASE)
    headers = f"Proc-Type: 4,ENCRYPTED\nDEK-Info: {dek_name.decode('ascii')},{iv.hex().upper()}\n"
    return _rewrap(header + "\n" + headers, encrypted, footer)


def test_proc_type_wrapped_pkcs8_is_rejected() -> None:
    """外层是密文，检查读不到里面的迭代次数；asyncssh 却会先解开外层再跑里面的 KDF"""
    cheap = _proc_type_nested(_pkcs8_key(2048))
    asyncssh.import_private_key(cheap, PASSPHRASE)  # 证明 asyncssh 真的认这种写法
    assert "两层加密" in private_key_cost_problem(cheap)
    with pytest.raises(ValidationError):
        validate_private_key(cheap)


# ---------------------------------------------------------------------------
# 文本长度与钥匙位数
# ---------------------------------------------------------------------------


def _openssh_with_public_blob(blob: bytes) -> str:
    """一个只有外壳的 OpenSSH 私钥：公钥部分是给定的线格式，私钥部分随便填"""
    body = (
        b"openssh-key-v1\x00"
        + String(b"none")
        + String(b"none")
        + String(b"")
        + UInt32(1)
        + String(blob)
        + String(b"\x00" * 16)
    )
    return _rewrap("-----BEGIN OPENSSH " + "PRIVATE KEY-----", body, "-----END OPENSSH " + "PRIVATE KEY-----")


def test_schema_and_validation_share_the_length_limit() -> None:
    assert PRIVATE_KEY_MAX_LENGTH == MAX_PRIVATE_KEY_CHARS


def test_overlong_private_key_text_is_rejected() -> None:
    plain = asyncssh.generate_private_key("ssh-ed25519").export_private_key("openssh").decode("ascii")
    padded = plain + "#" * (MAX_PRIVATE_KEY_CHARS + 1)
    assert "太长" in private_key_size_problem(padded)
    with pytest.raises(ValidationError):
        validate_private_key("-----BEGIN OPENSSH " + "PRIVATE KEY-----\n" + "A" * MAX_PRIVATE_KEY_CHARS)


def test_rsa_bits_are_read_from_the_openssh_public_part() -> None:
    """OpenSSH 格式的公钥部分不加密：不用解密就能读位数（加不加口令短语都一样）"""
    huge_modulus = (1 << (MAX_RSA_KEY_BITS + 512)) - 1
    too_big = _openssh_with_public_blob(String(b"ssh-rsa") + MPInt(65537) + MPInt(huge_modulus))
    assert "位数过高" in private_key_size_problem(too_big)
    fine = _openssh_with_public_blob(String(b"ssh-rsa") + MPInt(65537) + MPInt((1 << MAX_RSA_KEY_BITS) - 1))
    assert private_key_size_problem(fine) == ""
    real = asyncssh.generate_private_key("ssh-rsa", key_size=2048)
    assert private_key_size_problem(real.export_private_key("openssh", passphrase=PASSPHRASE).decode("ascii")) == ""


def test_rsa_bits_of_unencrypted_pkcs1_are_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """其余不加密的格式跳过校验地导入一次读位数（把上限调低，免得现场生成大钥匙）"""
    pkcs1 = asyncssh.generate_private_key("ssh-rsa", key_size=2048).export_private_key("pkcs1-pem").decode("ascii")
    assert private_key_size_problem(pkcs1) == ""
    monkeypatch.setattr(validation, "MAX_RSA_KEY_BITS", 1024)
    assert "位数过高" in private_key_size_problem(pkcs1)


# ---------------------------------------------------------------------------
# 解析在哪里跑、名额什么时候还
# ---------------------------------------------------------------------------


def _target(private_key: str, passphrase: str, *, workspace_id: int = 1) -> TargetSpec:
    return TargetSpec(
        credential_id=0,
        workspace_id=workspace_id,
        alias="web1",
        owner_kind=C.OWNER_USER,
        host=TARGET_IP,
        connect_address=TARGET_IP,
        port=22,
        username="root",
        password="",
        private_key=private_key,
        passphrase=passphrase,
        host_keys=[],
    )


def _free_slots() -> int:
    return int(upstream._semaphore()._value)  # noqa: SLF001


async def test_passphrase_key_is_decoded_in_a_subprocess() -> None:
    text = _openssh_key(16)
    expected = asyncssh.import_private_key(text, PASSPHRASE)
    key = await upstream._import_key(_target(text, PASSPHRASE))  # noqa: SLF001
    assert key is not None
    assert key.export_public_key("openssh") == expected.export_public_key("openssh")


_SLEEPER = "import sys, time\nsys.stdin.read()\ntime.sleep(60)\n"


def _spy_on_workers(monkeypatch: pytest.MonkeyPatch) -> list:
    started: list = []
    original = upstream._spawn_key_worker  # noqa: SLF001

    def spy() -> Any:
        proc = original()
        started.append(proc)
        return proc

    monkeypatch.setattr(upstream, "_spawn_key_worker", spy)
    return started


async def test_unencrypted_key_is_also_decoded_in_a_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """不带口令短语也进子进程：RSA 校验同样由文件决定成本，线程停不下来"""
    started = _spy_on_workers(monkeypatch)
    real = asyncssh.generate_private_key("ssh-rsa", key_size=2048)
    key = await upstream._import_key(_target(real.export_private_key("pkcs1-pem").decode("ascii"), ""))  # noqa: SLF001
    assert key is not None
    assert key.export_public_key("openssh") == real.export_public_key("openssh")
    assert len(started) == 1


async def test_wrong_passphrase_is_a_plain_parse_failure_and_is_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    started = _spy_on_workers(monkeypatch)
    target = _target(_openssh_key(16), "wrong-" + "phrase")
    for _ in range(2):
        with pytest.raises(upstream.UpstreamError) as exc_info:
            await upstream._import_key(target)  # noqa: SLF001
        assert exc_info.value.reason == C.REASON_UPSTREAM_AUTH_FAILED
        assert "解析失败" in exc_info.value.message
    assert len(started) == 1, "失败的结果要缓存一阵：重连不能每次都重新解析"


async def test_key_too_large_after_decrypting_is_rejected_before_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """加密的格式只有子进程解开以后才读得到位数（把上限调低，免得现场生成大钥匙）"""
    monkeypatch.setattr(upstream, "MAX_RSA_KEY_BITS", 1024)
    real = asyncssh.generate_private_key("ssh-rsa", key_size=2048)
    text = real.export_private_key(
        "pkcs8-pem", passphrase=PASSPHRASE, cipher_name="aes256-cbc", hash_name="sha256", pbe_version=2, rounds=2048
    ).decode("ascii")
    with pytest.raises(upstream.UpstreamError) as exc_info:
        await upstream._import_key(_target(text, PASSPHRASE))  # noqa: SLF001
    assert "位数过高" in exc_info.value.message


async def test_subprocess_is_killed_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upstream, "_KEY_WORKER_SCRIPT", _SLEEPER)
    monkeypatch.setattr(upstream, "KEY_IMPORT_TIMEOUT_SECONDS", 1.0)
    started = _spy_on_workers(monkeypatch)
    with pytest.raises(upstream.UpstreamError) as exc_info:
        await upstream._import_key(_target(_openssh_key(16), PASSPHRASE))  # noqa: SLF001
    assert "超时" in exc_info.value.message
    assert started and started[0].poll() is not None, "超时之后子进程必须已经退出"
    assert _free_slots() == upstream.KEY_IMPORT_CONCURRENCY


async def _wait_until(predicate: Any, *, seconds: float = 10.0) -> None:
    for _ in range(int(seconds / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)


async def test_same_key_is_decoded_once_and_a_cancelled_caller_does_not_cancel_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同时来的连接等同一次解析；第一个调用方被取消，第二个照样拿到结果，名额等子进程退出才还"""
    monkeypatch.setattr(upstream, "_KEY_WORKER_SCRIPT", _SLEEPER)
    monkeypatch.setattr(upstream, "KEY_IMPORT_TIMEOUT_SECONDS", 1.5)
    started = _spy_on_workers(monkeypatch)
    target = _target(_openssh_key(16), PASSPHRASE)
    first = asyncio.ensure_future(upstream._import_key(target))  # noqa: SLF001
    await _wait_until(lambda: bool(started))
    assert started, "子进程没有起来"
    second = asyncio.ensure_future(upstream._import_key(target))  # noqa: SLF001
    await asyncio.sleep(0.2)
    assert len(started) == 1, "同一把私钥不能再起第二个子进程"

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert started[0].poll() is None, "调用方被取消不该杀掉共用的解析"
    with pytest.raises(upstream.UpstreamError) as exc_info:
        await second
    assert "超时" in exc_info.value.message
    assert started[0].poll() is not None
    await _wait_until(lambda: _free_slots() == upstream.KEY_IMPORT_CONCURRENCY)
    assert _free_slots() == upstream.KEY_IMPORT_CONCURRENCY
    assert len(started) == 1


async def test_one_workspace_queues_behind_itself_not_behind_others(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一个工作区的第二把私钥要等第一把；别的工作区照常用剩下的全局名额"""
    monkeypatch.setattr(upstream, "_KEY_WORKER_SCRIPT", _SLEEPER)
    monkeypatch.setattr(upstream, "KEY_IMPORT_TIMEOUT_SECONDS", 1.5)
    started = _spy_on_workers(monkeypatch)
    calls = [
        asyncio.ensure_future(upstream._import_key(_target(_openssh_key(16), PASSPHRASE, workspace_id=1))),  # noqa: SLF001
        asyncio.ensure_future(upstream._import_key(_target(_openssh_key(16), PASSPHRASE, workspace_id=1))),  # noqa: SLF001
    ]
    await _wait_until(lambda: bool(started))
    await asyncio.sleep(0.3)
    assert len(started) == 1, "同一个工作区同时只解析一把"
    assert _free_slots() == upstream.KEY_IMPORT_CONCURRENCY - 1, "全局名额还剩一个，给别的工作区"

    calls.append(
        asyncio.ensure_future(upstream._import_key(_target(_openssh_key(16), PASSPHRASE, workspace_id=2))),  # noqa: SLF001
    )
    await _wait_until(lambda: len(started) == 2)
    assert len(started) == 2, "别的工作区不该排在这个工作区后面"

    results = await asyncio.gather(*calls, return_exceptions=True)
    assert all(isinstance(result, upstream.UpstreamError) for result in results)
    assert len(started) == 3
    assert all(proc.poll() is not None for proc in started)
    await _wait_until(lambda: _free_slots() == upstream.KEY_IMPORT_CONCURRENCY)
    assert _free_slots() == upstream.KEY_IMPORT_CONCURRENCY
