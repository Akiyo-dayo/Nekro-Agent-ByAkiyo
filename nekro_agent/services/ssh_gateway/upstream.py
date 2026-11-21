"""上游 SSH 连接：登录、主机钥匙、复用池

## 连接只连 `TargetSpec.connect_address`

授权层已经把「用户行按当前解析结果查过的地址」放进 `connect_address`；平台方行则是
登记的主机。这里不会再解析一次域名，也不读 `~/.ssh/config`、`~/.ssh/known_hosts`、
agent、X11 证书这些本机 SSH 设置：网关登录的目标与凭据完全由数据库里的这一行决定，
本机碰巧配过什么都不能偷偷参与。

## 主机钥匙

- 行上已有钥匙：只认这些（`known_hosts=[]` + `validate_host_public_key` 回调）。
  演示的钥匙不在其中就拒绝，理由 `host_key_mismatch`；不改行状态，后台可以接受新指纹。
- 行上还是空：接受这次出示的钥匙，登录成功后再写回行上（`pin_host_key`）。登录失败
  什么都不记，避免把中间人的钥匙钉下来。

## 登录失败为什么要分三种

- 认证被拒：目标服务器已经明确说"这组凭据不行"。把行改成 `auth_failed`，之后的
  授权复核会直接拒绝，不再反复打目标服务器（否则容易被 fail2ban 封掉 NA 的出口）。
- 二次验证：服务器要验证码/多轮挑战，网关没有地方问人，只能拒绝并让后台知道。
- 网络/超时：对方可能只是暂时不可达。按凭据记 10 秒退避，期间不再重试。

## 复用池

一次容器连接上经常连续开好几个通道（`ssh alias cmd1`、`cmd2`……）。每个通道都重新
登录既慢又会在目标服务器上留下多份会话，所以按 `(lease_id, credential_id)` 复用同一个
上游连接；连接上同时开着的通道达到上限、或者连接已经断了，才新建。复用省掉的只是
登录，不省审计：每个通道仍然单独记 `channel`/`exec`/`close`。

每个通道经 `acquire(..., reserve=True)` 在锁里占一个名额、关闭时还；新登录的连接一进池
就开始算空闲，没有通道用它，60 秒后自己关掉。登录期间租约结束或凭据变更的，登完立刻
关掉、不进池，免得一条已经失去授权的上游带着明文凭据留在内存里。

## 私钥解析不能卡住事件循环

带口令短语的私钥要跑一遍 KDF，而轮数（bcrypt）、迭代次数与派生长度（PKCS#8）写在私钥
文件里、由登记的人决定；不带口令短语的也有 RSA 校验（核对 p、q 等），成本约随位数的三次方
涨，8192 位要好几秒。所以：

- 解析前先设上限：KDF 工作量、文本长度、能读到的钥匙位数（读的是 asyncssh 自己的解析结果，
  不另写一套，见 `validation.private_key_cost_problem` / `private_key_size_problem`）；
- **所有**解析都放进子进程、限时 20 秒，超时就杀掉。子进程里先跳过校验地解出来、核位数
  （加密的格式只有解开以后才读得到），超限就不往下做；不超限再在不加密的导出上做一遍完整
  校验，导出交回父进程；
- 名额两层：每个工作区同时一个，全局同时两个。一个租户反复登记 / 测试高成本私钥，排队的是
  它自己的工作区，占不满全局名额；
- 同一把私钥（按「私钥 + 口令短语」的哈希）同时只解析一次，后来的连接等同一个结果；调用方
  被取消不会取消这次解析（它有界，结果还要给别的连接用）。成功的结果缓存 10 分钟，失败的
  （解析失败、超时、位数超限）缓存 60 秒，期间重连不再重新解析。

PuTTY 格式（v3 用 argon2，内存参数同样由文件决定）不接受。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import asyncssh

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.services.ssh_gateway import constants as C
from nekro_agent.services.ssh_gateway import row_state
from nekro_agent.services.ssh_gateway.authority import TargetSpec
from nekro_agent.services.ssh_gateway.validation import (
    MAX_BCRYPT_ROUNDS,
    MAX_DSA_KEY_BITS,
    MAX_PBKDF_ITERATIONS,
    MAX_RSA_KEY_BITS,
    private_key_cost_problem,
    private_key_size_problem,
)

logger = get_sub_logger("ssh_gateway.upstream")

CONNECT_TIMEOUT_SECONDS = 15.0
LOGIN_TIMEOUT_SECONDS = 30.0
KEEPALIVE_INTERVAL_SECONDS = 30.0
WINDOW_SIZE = 512 * 1024
MAX_CHANNELS_PER_UPSTREAM = 8
UNREACHABLE_BACKOFF_SECONDS = 10.0

KEY_IMPORT_CONCURRENCY = 2
# 每个工作区同时最多这么多个解析（在全局名额之前排）
KEY_IMPORT_PER_WORKSPACE = 1
# 子进程里的解析最多跑这么久；上限内的 bcrypt 128 轮、4096 位 RSA 校验在普通机器上都不到 1 秒
KEY_IMPORT_TIMEOUT_SECONDS = 20.0
KEY_CACHE_TTL_SECONDS = 600.0
KEY_FAILURE_TTL_SECONDS = 60.0
KEY_CACHE_MAX_ENTRIES = 64

_key_semaphore: Optional[asyncio.Semaphore] = None
_key_semaphore_loop: Optional[asyncio.AbstractEventLoop] = None
# workspace_id -> (所在事件循环, 名额)
_workspace_semaphores: Dict[int, Tuple[asyncio.AbstractEventLoop, asyncio.Semaphore]] = {}
# sha256(私钥 + NUL + 口令短语) -> (过期时刻, 解析好的钥匙)
_key_cache: Dict[str, Tuple[float, Any]] = {}
# 同上的哈希 -> (过期时刻, 给调用方的说明)：解析失败、超时、位数超限
_key_failures: Dict[str, Tuple[float, str]] = {}
# 同上的哈希 -> 正在进行的解析
_key_inflight: Dict[str, "asyncio.Future[Any]"] = {}

# 测试注入单调时钟，验证 10 秒退避而不真等
_clock: Callable[[], float] = time.monotonic

# credential_id -> 退避截止时刻。只放进程内存：重启后立刻重试一次是合理的
_unreachable_until: Dict[int, float] = {}

# 密码/验证码挑战里能当作"要密码"的提示词。小写后匹配，中文单独列
_PASSWORD_PROMPT_WORDS = ("password", "passcode", "密码")


class UpstreamError(Exception):
    """上游登录/连接失败；`reason` 是给授权层和横幅用的理由码"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = str(reason)
        self.message = str(message)


def _monotonic() -> float:
    return float(_clock())


def _backoff_remaining(credential_id: int) -> float:
    until = _unreachable_until.get(int(credential_id))
    if until is None:
        return 0.0
    remaining = until - _monotonic()
    if remaining <= 0:
        _unreachable_until.pop(int(credential_id), None)
        return 0.0
    return remaining


def _backoff(credential_id: int) -> None:
    _unreachable_until[int(credential_id)] = _monotonic() + UNREACHABLE_BACKOFF_SECONDS


def reset_upstream_state() -> None:
    """测试清空退避表、私钥缓存、正在进行的解析、名额与时钟"""
    global _clock, _key_semaphore, _key_semaphore_loop
    _unreachable_until.clear()
    _key_cache.clear()
    _key_failures.clear()
    _key_inflight.clear()
    _workspace_semaphores.clear()
    _key_semaphore = None
    _key_semaphore_loop = None
    _clock = time.monotonic


def _parse_pinned_keys(entries: List[str]) -> List[asyncssh.SSHKey]:
    pinned: List[asyncssh.SSHKey] = []
    for entry in entries:
        try:
            pinned.append(asyncssh.import_public_key(str(entry)))
        except (asyncssh.KeyImportError, ValueError) as exc:
            raise UpstreamError(C.REASON_HOST_KEY_MISMATCH, "登记的主机钥匙解析不了") from exc
    return pinned


def _fingerprint_of(key: asyncssh.SSHKey) -> str:
    return str(key.get_fingerprint("sha256"))


def _entry_of(key: asyncssh.SSHKey) -> str:
    exported = key.export_public_key("openssh")
    if isinstance(exported, bytes):
        return exported.decode("ascii", "replace").strip()
    return str(exported).strip()


class _UpstreamClient(asyncssh.SSHClient):
    """只认钉住的主机钥匙；键盘交互只接受"单个密码提示\""""

    def __init__(self, pinned: List[asyncssh.SSHKey], password: str) -> None:
        super().__init__()
        self._pinned = pinned
        self._password = password
        self._kbdint_tried = False
        self.mfa_required = False

    def validate_host_public_key(self, host: str, addr: str, port: int, key: asyncssh.SSHKey) -> bool:
        """`known_hosts=[]` 时 asyncssh 会走到这里；空钉住表表示首次接受"""
        if not self._pinned:
            return True
        return any(key == pinned for pinned in self._pinned)

    def kbdint_auth_requested(self) -> Optional[str]:
        if not self._password or self._kbdint_tried:
            return None
        self._kbdint_tried = True
        # 空串让服务器挑子方法；真正的筛选在 challenge 里做
        return ""

    def kbdint_challenge_received(
        self,
        name: str,
        instructions: str,
        lang: str,
        prompts: List[Tuple[str, bool]],
    ) -> Optional[List[str]]:
        """只答"恰好一个、看起来像密码"的挑战；其余都按二次验证处理"""
        if not prompts:
            return []
        if len(prompts) != 1:
            self.mfa_required = True
            return None
        prompt = str(prompts[0][0] or "").lower()
        if any(word in prompt for word in _PASSWORD_PROMPT_WORDS):
            return [self._password]
        self.mfa_required = True
        return None


def check_private_key_cost(private_key: str) -> None:
    """解析之前先设上限：KDF 成本、文本长度、能读到的钥匙位数（判据在 `validation`）

    存量的行可能早于这些上限保存，所以连接前再判一次，而不只靠保存时的校验。
    """
    problem = private_key_cost_problem(private_key) or private_key_size_problem(private_key)
    if problem:
        raise UpstreamError(C.REASON_UPSTREAM_AUTH_FAILED, problem)


def _key_cache_key(private_key: str, passphrase: str) -> str:
    digest = hashlib.sha256()
    digest.update(private_key.encode("utf-8", "surrogatepass"))
    digest.update(b"\x00")
    digest.update(passphrase.encode("utf-8", "surrogatepass"))
    return digest.hexdigest()


def _semaphore() -> asyncio.Semaphore:
    global _key_semaphore, _key_semaphore_loop
    loop = asyncio.get_running_loop()
    if _key_semaphore is None or _key_semaphore_loop is not loop:
        _key_semaphore = asyncio.Semaphore(KEY_IMPORT_CONCURRENCY)
        _key_semaphore_loop = loop
    return _key_semaphore


def _workspace_semaphore(workspace_id: int) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    entry = _workspace_semaphores.get(int(workspace_id))
    if entry is None or entry[0] is not loop:
        entry = (loop, asyncio.Semaphore(KEY_IMPORT_PER_WORKSPACE))
        _workspace_semaphores[int(workspace_id)] = entry
    return entry[1]


class _KeyWorkerFailed(Exception):
    """子进程里解析失败（格式不对、口令短语不对）"""


class _KeyTooLarge(Exception):
    """子进程解开之后发现钥匙位数超限（加密的格式在父进程里读不到位数）"""


# 子进程里跑的解析：私钥与口令短语经 stdin 传入（不进命令行，`ps` 看不到）。先跳过 RSA 校验地
# 解出来（KDF 在这里，成本已经在父进程设过上限），核位数；不超限再在**不加密的导出**上导入一遍
# 做完整校验（不再跑 KDF），把这份导出写回 stdout，父进程跳过校验地导入。
# 位数判据与 `validation.public_key_size_problem` 相同：子进程以 `-I` 启动，导入不了本项目的模块。
_KEY_WORKER_SCRIPT = (
    "import json, sys\n"
    "import asyncssh\n"
    "from asyncssh.packet import SSHPacket\n"
    "payload = json.loads(sys.stdin.read())\n"
    "def too_large(blob):\n"
    "    packet = SSHPacket(blob)\n"
    "    name = packet.get_string()\n"
    "    if name == b'ssh-rsa':\n"
    "        packet.get_mpint()\n"
    "        return packet.get_mpint().bit_length() > payload['max_rsa_bits']\n"
    "    if name == b'ssh-dss':\n"
    "        return packet.get_mpint().bit_length() > payload['max_dsa_bits']\n"
    "    return False\n"
    "try:\n"
    "    key = asyncssh.import_private_key(\n"
    "        payload['key'], payload['passphrase'] or None, unsafe_skip_rsa_key_validation=True\n"
    "    )\n"
    "    if too_large(key.public_data):\n"
    "        sys.stdout.buffer.write(b'BIG\\n')\n"
    "        raise SystemExit(0)\n"
    "    exported = key.export_private_key('openssh')\n"
    "    asyncssh.import_private_key(exported, None)\n"
    "except Exception:\n"
    "    sys.stdout.buffer.write(b'ERR\\n')\n"
    "    raise SystemExit(0)\n"
    "if isinstance(exported, str):\n"
    "    exported = exported.encode('ascii')\n"
    # 写字节而不是文本：Windows 上文本模式的 stdout 会把换行换成 \r\n
    "sys.stdout.buffer.write(b'OK\\n' + exported)\n"
)


def _spawn_key_worker() -> "subprocess.Popen[bytes]":
    return subprocess.Popen(  # noqa: S603 - 固定的解释器与脚本，不拼任何外部输入
        [sys.executable, "-I", "-c", _KEY_WORKER_SCRIPT],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


def _kill_quietly(proc: "subprocess.Popen[bytes]") -> None:
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def _exchange_with_worker(proc: "subprocess.Popen[bytes]", payload: bytes) -> bytes:
    """在线程里：写入、读回、限时；超时就杀掉并收尸，再抛 `TimeoutError`"""
    try:
        stdout, _stderr = proc.communicate(payload, timeout=KEY_IMPORT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        _kill_quietly(proc)
        proc.communicate()
        raise asyncio.TimeoutError() from exc
    except BaseException:
        _kill_quietly(proc)
        proc.wait()
        raise
    return stdout or b""


async def _import_in_subprocess(private_key: str, passphrase: str) -> asyncssh.SSHKey:
    """在能杀掉的子进程里解析（KDF 与 RSA 校验都在这里），超时就杀进程

    线程停不下来：asyncssh 在断连、登录超时时会取消认证任务，取消只放开了等待的协程，
    线程照样把 KDF 或 RSA 校验跑完，重连一次就多一个停不掉的线程，默认线程池满了以后 DNS
    解析与建连全卡住。子进程可以杀；杀完一定 `wait()` 收尸，再交还名额。
    """
    # 用 `subprocess.Popen` 而不是 `asyncio.create_subprocess_exec`：后者在 Windows 的
    # SelectorEventLoop 上不可用；Popen 在任何事件循环下都能杀。收发放在线程里，线程会在
    # 子进程退出（正常结束或被杀）时返回，所以它同样有界
    proc = _spawn_key_worker()
    payload = json.dumps(
        {
            "key": private_key,
            "passphrase": passphrase,
            "max_rsa_bits": MAX_RSA_KEY_BITS,
            "max_dsa_bits": MAX_DSA_KEY_BITS,
        },
    ).encode("utf-8")
    exchange = asyncio.ensure_future(asyncio.to_thread(_exchange_with_worker, proc, payload))
    try:
        stdout = await asyncio.shield(exchange)
    except BaseException:
        # 超时或被取消：杀掉子进程，等收发线程看到它退出、收完尸，名额才算还回去
        _kill_quietly(proc)
        await asyncio.wait({exchange})
        raise
    text = stdout.decode("ascii", "replace")
    if text.startswith("BIG\n"):
        raise _KeyTooLarge()
    if not text.startswith("OK\n"):
        raise _KeyWorkerFailed()
    # 子进程已经在这份不加密的导出上做过完整校验；这里跳过 RSA 一致性校验免得在本进程里重复算
    return asyncssh.import_private_key(text[3:], None, unsafe_skip_rsa_key_validation=True)


async def _run_key_import(private_key: str, passphrase: str, workspace_id: int) -> asyncssh.SSHKey:
    """先占工作区的名额、再占全局名额，占到子进程真正退出为止"""
    async with _workspace_semaphore(workspace_id):
        async with _semaphore():
            return await _import_in_subprocess(private_key, passphrase)


def _retrieve_quietly(task: "asyncio.Future[Any]") -> None:
    if not task.cancelled():
        task.exception()


def _remember(store: Dict[str, Tuple[float, Any]], cache_key: str, ttl: float, value: Any) -> None:
    now = _monotonic()
    for stale in [item for item, (expires, _value) in store.items() if expires <= now]:
        store.pop(stale, None)
    while len(store) >= KEY_CACHE_MAX_ENTRIES:
        store.pop(next(iter(store)), None)
    store[cache_key] = (now + ttl, value)


async def _import_once(cache_key: str, private_key: str, passphrase: str, workspace_id: int) -> asyncssh.SSHKey:
    """真正解析一次：成功进缓存，失败记 60 秒，都翻成 `UpstreamError`"""
    try:
        key = await _run_key_import(private_key, passphrase, workspace_id)
    except asyncio.TimeoutError as exc:
        message = "私钥解析超时"
        _remember(_key_failures, cache_key, KEY_FAILURE_TTL_SECONDS, message)
        raise UpstreamError(C.REASON_UPSTREAM_AUTH_FAILED, message) from exc
    except _KeyTooLarge as exc:
        message = f"私钥位数过高（RSA 最多 {MAX_RSA_KEY_BITS} 位、DSA 最多 {MAX_DSA_KEY_BITS} 位）"
        _remember(_key_failures, cache_key, KEY_FAILURE_TTL_SECONDS, message)
        raise UpstreamError(C.REASON_UPSTREAM_AUTH_FAILED, message) from exc
    except (_KeyWorkerFailed, asyncssh.KeyImportError, asyncssh.KeyEncryptionError, ValueError) as exc:
        message = "私钥解析失败"
        _remember(_key_failures, cache_key, KEY_FAILURE_TTL_SECONDS, message)
        raise UpstreamError(C.REASON_UPSTREAM_AUTH_FAILED, message) from exc
    _remember(_key_cache, cache_key, KEY_CACHE_TTL_SECONDS, key)
    return key


def _forget_inflight(cache_key: str, future: "asyncio.Future[Any]") -> None:
    if _key_inflight.get(cache_key) is future:
        _key_inflight.pop(cache_key, None)


async def _import_key(target: TargetSpec) -> Optional[asyncssh.SSHKey]:
    """解析私钥；口令短语只用于解密，绝不出现在错误文案里

    先查缓存（成功 10 分钟、失败 60 秒），再设上限（读 asyncssh 自己的解析结果），然后在
    子进程里解析（见模块说明）。同一把私钥同时只解析一次：后来的连接等同一个结果。
    """
    if not target.private_key:
        return None
    private_key = str(target.private_key)
    passphrase = str(target.passphrase or "")
    cache_key = _key_cache_key(private_key, passphrase)
    now = _monotonic()
    cached = _key_cache.get(cache_key)
    if cached is not None and cached[0] > now:
        return cached[1]
    failed = _key_failures.get(cache_key)
    if failed is not None and failed[0] > now:
        raise UpstreamError(C.REASON_UPSTREAM_AUTH_FAILED, str(failed[1]))

    check_private_key_cost(private_key)
    loop = asyncio.get_running_loop()
    worker = _key_inflight.get(cache_key)
    if worker is None or worker.done() or worker.get_loop() is not loop:
        # 解析放在单独的任务里，调用方（asyncssh 的认证任务）被取消不会取消它：它有界，
        # 名额占到子进程退出为止，结果还要交给同时在等的其它连接
        worker = asyncio.ensure_future(_import_once(cache_key, private_key, passphrase, int(target.workspace_id)))
        worker.add_done_callback(_retrieve_quietly)
        worker.add_done_callback(lambda done, key=cache_key: _forget_inflight(key, done))
        _key_inflight[cache_key] = worker
    return await asyncio.shield(worker)


async def _connect_socket(address: str, port: int) -> socket.socket:
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _blocking_connect, address, int(port)),
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise UpstreamError(C.REASON_UPSTREAM_UNREACHABLE, "连不上这台服务器") from exc


def _blocking_connect(address: str, port: int) -> socket.socket:
    sock = socket.create_connection((address, port), timeout=CONNECT_TIMEOUT_SECONDS)
    sock.setblocking(False)
    return sock


async def _pin_presented_key(conn: asyncssh.SSHClientConnection, credential_id: int) -> None:
    """首次连接成功后把服务器出示的钥匙写到行上；写不进去也不影响这次登录"""
    key = conn.get_server_host_key()
    if key is None:
        return
    try:
        entry = _entry_of(key)
        pinned = await row_state.pin_host_key(credential_id, entry, actor="gateway")
        if pinned:
            logger.info(f"SSH 凭据 {credential_id} 首次记下目标主机钥匙：{_fingerprint_of(key)}")
    except Exception as exc:  # noqa: BLE001 - 钉钥匙失败不该把一条已经建立好的连接关掉
        logger.warning(f"SSH 凭据 {credential_id} 记下目标主机钥匙失败: {type(exc).__name__}")


async def open_upstream(target: TargetSpec) -> asyncssh.SSHClientConnection:
    """登录一次并返回连接；不创建任何会话

    失败时按理由码抛 `UpstreamError`，调用方只负责把它翻成横幅与审计。
    """
    return await _open_upstream(target, record_row_state=True)


async def open_upstream_for_probe(target: TargetSpec) -> asyncssh.SSHClientConnection:
    """聊天登记用的一次性登录校验：只认证，不写任何行状态与退避表

    用一个临时的 `credential_id` 走同一份登录逻辑，但不碰凭据行、不记退避，失败后
    可以立刻重试。调用方负责在成功后关闭连接。
    """
    return await _open_upstream(target, record_row_state=False)


async def _open_upstream(
    target: TargetSpec,
    *,
    record_row_state: bool,
) -> asyncssh.SSHClientConnection:
    credential_id = int(target.credential_id)
    if record_row_state:
        remaining = _backoff_remaining(credential_id)
        if remaining > 0:
            raise UpstreamError(C.REASON_UPSTREAM_UNREACHABLE, f"最近连不上，{int(remaining) + 1} 秒后再试")

    pinned = _parse_pinned_keys(target.host_keys)
    try:
        key = await _import_key(target)
    except UpstreamError as exc:
        if record_row_state:
            await _mark_auth_failed(credential_id, exc.message)
        raise
    client = _UpstreamClient(pinned, target.password)
    try:
        sock = await _connect_socket(target.connect_address, int(target.port))
    except UpstreamError as exc:
        # 连接阶段就失败：和 `asyncssh.connect` 里的网络错误同一种处理，记退避与最近错误
        if record_row_state:
            _backoff(credential_id)
            await _set_last_error(credential_id, "连不上这台服务器")
        raise UpstreamError(C.REASON_UPSTREAM_UNREACHABLE, "连不上这台服务器") from exc

    kwargs: Dict[str, object] = {
        "sock": sock,
        "username": target.username,
        "client_factory": lambda: client,
        # 显式清空所有本机 SSH 设置来源
        "known_hosts": [],
        "config": None,
        # 没有私钥时必须传 None：传空列表 asyncssh 会去读本机 ~/.ssh 下的默认私钥
        "client_keys": [key] if key is not None else None,
        "agent_path": None,
        "agent_identities": None,
        "pkcs11_provider": None,
        "password": target.password or None,
        "password_auth": bool(target.password),
        "kbdint_auth": bool(target.password),
        "preferred_auth": ["publickey", "password", "keyboard-interactive"]
        if key is not None
        else (["password", "keyboard-interactive"] if target.password else []),
        "encoding": None,
        "window": WINDOW_SIZE,
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        "login_timeout": LOGIN_TIMEOUT_SECONDS,
        "keepalive_interval": KEEPALIVE_INTERVAL_SECONDS,
        "keepalive_count_max": 3,
        "gss_auth": False,
        "gss_kex": False,
        "gss_host": None,
        "x11_forwarding": False,
        "agent_forwarding": False,
    }

    try:
        conn = await asyncssh.connect(**kwargs)  # type: ignore[arg-type]
    except asyncssh.HostKeyNotVerifiable as exc:
        raise UpstreamError(C.REASON_HOST_KEY_MISMATCH, "服务器的主机密钥和登记的不一致，已拒绝，请核对") from exc
    except asyncssh.PermissionDenied as exc:
        if client.mfa_required:
            if record_row_state:
                await _mark_mfa(credential_id)
            raise UpstreamError(C.REASON_MFA_REQUIRED, "服务器要求二次验证，网关不支持") from exc
        if record_row_state:
            await _mark_auth_failed(credential_id, "服务器拒绝了这组凭据")
        raise UpstreamError(C.REASON_UPSTREAM_AUTH_FAILED, "服务器拒绝了这组凭据") from exc
    except (OSError, asyncio.TimeoutError, asyncssh.ConnectionLost, asyncssh.Error) as exc:
        if client.mfa_required:
            if record_row_state:
                await _mark_mfa(credential_id)
            raise UpstreamError(C.REASON_MFA_REQUIRED, "服务器要求二次验证，网关不支持") from exc
        if record_row_state:
            _backoff(credential_id)
            await _set_last_error(credential_id, "连不上这台服务器")
        raise UpstreamError(C.REASON_UPSTREAM_UNREACHABLE, "连不上这台服务器") from exc

    # 下面几步都要等库；这期间被取消（容器断开、登录超时）时连接既没交出去也没进池，
    # 必须在这里关掉，否则它带着明文凭据与 keepalive 一直活着
    try:
        if record_row_state and not pinned:
            await _pin_presented_key(conn, credential_id)
        if record_row_state:
            try:
                await row_state.touch_last_used(credential_id)
            except Exception as exc:  # noqa: BLE001 - 登录已经成功，时间戳写不进去不该回收连接
                logger.warning(f"SSH 凭据 {credential_id} 记录最近使用时间失败: {type(exc).__name__}")
    except BaseException:
        conn.abort()
        raise
    return conn


async def _mark_auth_failed(credential_id: int, message: str) -> None:
    try:
        await row_state.mark_auth_failed(credential_id, message)
    except Exception as exc:  # noqa: BLE001 - 行状态写不进去也要把拒绝理由返回给调用方
        logger.warning(f"SSH 凭据 {credential_id} 写认证失败状态失败: {type(exc).__name__}")


async def _mark_mfa(credential_id: int) -> None:
    await _set_last_error(credential_id, "服务器要求二次验证，网关不支持")


async def _set_last_error(credential_id: int, message: str) -> None:
    try:
        await row_state.set_last_error(credential_id, message)
    except Exception as exc:  # noqa: BLE001 - 同上，不让写库失败掩盖真正的登录错误
        logger.warning(f"SSH 凭据 {credential_id} 写最近错误失败: {type(exc).__name__}")


async def probe_host_key(address: str, port: int) -> str:
    """只做 KEX，返回服务器出示的主机钥匙 `"类型 base64\""""
    sock = await _connect_socket(address, int(port))
    try:
        key = await asyncio.wait_for(
            asyncssh.get_server_host_key(sock=sock, config=None),
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
    except (OSError, asyncio.TimeoutError, asyncssh.Error) as exc:
        raise UpstreamError(C.REASON_UPSTREAM_UNREACHABLE, "取不到目标服务器的主机钥匙") from exc
    finally:
        try:
            sock.close()
        except OSError:
            pass
    if key is None:
        raise UpstreamError(C.REASON_UPSTREAM_UNREACHABLE, "目标服务器没有出示主机钥匙")
    return _entry_of(key)


async def _row_still_matches(target: TargetSpec, conn: asyncssh.SSHClientConnection) -> bool:
    """登录期间收到过这一行的变更通知：回库核对这条上游是否仍然可用"""
    from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
    from nekro_agent.services.ssh_gateway.dbslot import db_slot

    try:
        async with db_slot():
            row = await DBWsSshCredential.get_or_none(id=int(target.credential_id))
    except Exception:  # noqa: BLE001 - 判不出来按「变了」处理
        return False
    if row is None or str(row.status or "") != C.STATUS_ACTIVE:
        return False
    if not target.row_digest or row_state.credential_digest(row) != target.row_digest:
        return False
    key = conn.get_server_host_key()
    if key is None:
        return False
    presented = " ".join(_entry_of(key).split()[:2])
    pinned = {" ".join(str(item).split()[:2]) for item in (row.host_keys or [])}
    return presented in pinned


class PooledUpstream:
    """一条被池子管着的上游连接；通道数由桥接层增减"""

    def __init__(
        self,
        pool: "UpstreamPool",
        *,
        lease_id: str,
        credential_id: int,
        conn: asyncssh.SSHClientConnection,
    ) -> None:
        self._pool = pool
        self.lease_id = str(lease_id)
        self.credential_id = int(credential_id)
        self.conn = conn
        self.channels = 0
        self._idle_handle: Optional[asyncio.TimerHandle] = None
        self._closed = False

    @property
    def alive(self) -> bool:
        return not self._closed and not self.conn.is_closed()

    @property
    def key(self) -> Tuple[str, int]:
        return self.lease_id, self.credential_id

    def channel_opened(self) -> None:
        self.channels += 1
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    def channel_closed(self) -> None:
        if self.channels > 0:
            self.channels -= 1
        if self.channels == 0:
            self._schedule_idle()

    def _schedule_idle(self) -> None:
        if self._closed:
            return
        if self._idle_handle is not None:
            self._idle_handle.cancel()
        idle = max(0.0, float(config.SSH_GATEWAY_UPSTREAM_IDLE_SECONDS))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 关停阶段没有事件循环，直接收掉
            self.close()
            return
        self._idle_handle = loop.call_later(idle, self._close_if_idle)

    def _close_if_idle(self) -> None:
        self._idle_handle = None
        if self.channels <= 0:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        try:
            self.conn.abort()
        except Exception:  # noqa: BLE001 - 已经断了的连接再关一次不该抛
            pass
        self._pool._discard(self)


class UpstreamPool:
    """按 `(lease_id, credential_id)` 复用；每个连接最多 8 个通道"""

    def __init__(self) -> None:
        self._items: List[PooledUpstream] = []
        self._lock = asyncio.Lock()
        self._opening: Dict[Tuple[str, int], "asyncio.Task[PooledUpstream]"] = {}
        # 登录期间租约结束 / 凭据变更的判定用：最近结束的租约与只增不减的凭据计数
        self._closed_leases: Set[str] = set()
        self._credential_epochs: Dict[int, int] = {}

    async def acquire(self, decision: object, *, reserve: bool = False) -> PooledUpstream:
        """取一条可用连接；没有就登录一条新的

        `decision` 是 `authority.Decision`，这里只读 `target` / `lease_id`，
        避免 upstream 反向依赖授权模块的完整类型。`reserve=True` 时在锁里占一个通道
        名额（调用方在通道关闭时 `channel_closed()`），保证一条上游上的通道不超过上限。
        """
        target = getattr(decision, "target", None)
        if target is None:
            raise UpstreamError(C.REASON_UPSTREAM_UNREACHABLE, "没有可连接的目标")
        lease_id = str(getattr(decision, "lease_id", "") or "")
        key = (lease_id, int(target.credential_id))

        while True:
            wait_for: Optional["asyncio.Task[PooledUpstream]"] = None
            opening: Optional["asyncio.Task[PooledUpstream]"] = None
            async with self._lock:
                existing = self._pick_locked(key)
                if existing is not None:
                    if reserve:
                        existing.channel_opened()
                    return existing
                wait_for = self._opening.get(key)
                if wait_for is None:
                    opening = asyncio.create_task(self._open_and_add(key, target))
                    self._opening[key] = opening
            if wait_for is not None:
                # 别人正在登录同一个目标：等它登完再回到锁里挑（它可能已经被占满）
                await asyncio.shield(wait_for)
                continue
            assert opening is not None
            try:
                pooled = await opening
            finally:
                async with self._lock:
                    if self._opening.get(key) is opening:
                        self._opening.pop(key, None)
            async with self._lock:
                if pooled.alive and (not reserve or pooled.channels < MAX_CHANNELS_PER_UPSTREAM):
                    if reserve:
                        pooled.channel_opened()
                    return pooled
            # 刚登好的这条已经被等着的人占满或断了：再来一轮

    def _pick_locked(self, key: Tuple[str, int]) -> Optional[PooledUpstream]:
        for item in list(self._items):
            if item.key != key:
                continue
            if not item.alive:
                self._items.remove(item)
                continue
            if item.channels < MAX_CHANNELS_PER_UPSTREAM:
                return item
        return None

    async def _open_and_add(self, key: Tuple[str, int], target: TargetSpec) -> PooledUpstream:
        credential_epoch = self._credential_epochs.get(key[1], 0)
        conn = await open_upstream(target)
        # 登录期间租约结束或凭据变了：`close_for_*` 已经跑过、扫不到这条，它也不能再进池
        if key[0] in self._closed_leases:
            conn.abort()
            raise UpstreamError(C.REASON_NO_LEASE, "授权在登录期间结束了")
        if self._credential_epochs.get(key[1], 0) != credential_epoch and not await _row_still_matches(target, conn):
            # 计数变了不一定是别人改了行：这次登录自己首次钉住主机钥匙也会发变更通知。
            # 回库核对一次，只有行确实变了（删了、停了、凭据或目标改了、钉的不是这把钥匙）才作废
            conn.abort()
            raise UpstreamError(C.REASON_ROW_INACTIVE, "凭据在登录期间变了")
        pooled = PooledUpstream(self, lease_id=key[0], credential_id=key[1], conn=conn)
        async with self._lock:
            self._items.append(pooled)
        # 没有通道来用它就按空闲收掉；第一个通道占名额时会取消这个计时
        pooled._schedule_idle()
        return pooled

    def _discard(self, item: PooledUpstream) -> None:
        try:
            self._items.remove(item)
        except ValueError:
            pass

    def close_for_lease(self, lease_id: str) -> None:
        value = str(lease_id)
        self._closed_leases.add(value)
        if len(self._closed_leases) > 4096:
            self._closed_leases = {value}
        for item in list(self._items):
            if item.lease_id == value:
                item.close()

    def close_for_credential(self, credential_id: int) -> None:
        value = int(credential_id)
        self._credential_epochs[value] = self._credential_epochs.get(value, 0) + 1
        for item in list(self._items):
            if item.credential_id == value:
                item.close()

    def close_all(self) -> None:
        for item in list(self._items):
            item.close()
        self._items.clear()

    def count(self) -> int:
        return sum(1 for item in self._items if item.alive)

    def channels(self) -> int:
        return sum(item.channels for item in self._items if item.alive)


# 进程级单例：网关服务与测试连接接口共用同一份退避/连接状态
pool = UpstreamPool()


__all__ = [
    "MAX_BCRYPT_ROUNDS",
    "MAX_CHANNELS_PER_UPSTREAM",
    "MAX_PBKDF_ITERATIONS",
    "PooledUpstream",
    "check_private_key_cost",
    "UpstreamError",
    "UpstreamPool",
    "open_upstream",
    "open_upstream_for_probe",
    "pool",
    "probe_host_key",
    "reset_upstream_state",
]
