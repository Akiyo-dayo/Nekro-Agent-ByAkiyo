"""E2 测试用的目标 SSH 服务器与 TCP 回声服务

这里只放"被测代码之外"的东西：一台真的 asyncssh 服务器、一个真的 TCP 回声端，
以及生成钥匙/解析公钥的小工具。目标服务器按需打开密码、公钥、键盘交互认证，
并记录认证/转发尝试次数，供测试断言"网关没有偷偷做多余的事"。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import asyncssh
from asyncssh.stream import SSHServerStreamSession


def public_line(key: object) -> str:
    """把公钥/公钥文本规整成"类型 base64"两段"""
    if isinstance(key, str):
        text = key
    elif isinstance(key, bytes):
        text = key.decode("ascii", "replace")
    elif hasattr(key, "export_public_key"):
        exported = key.export_public_key("openssh")  # type: ignore[attr-defined]
        text = exported.decode("ascii", "replace") if isinstance(exported, bytes) else str(exported)
    else:
        text = str(key)
    parts = text.strip().split()
    return " ".join(parts[:2])


@dataclass
class TargetConfig:
    """一台目标服务器的行为配置与调用计数"""

    username: str = "root"
    password: str = ""
    allow_password: bool = True
    client_public_key: str = ""
    kbdint_mode: str = "off"  # off / password / mfa
    sftp_root: Path = Path(".")
    auth_attempts: int = 0
    public_key_attempts: int = 0
    password_attempts: int = 0
    kbdint_attempts: int = 0
    sessions: int = 0
    direct_requests: int = 0
    forward_requests: int = 0
    commands: List[str] = field(default_factory=list)
    signals: List[str] = field(default_factory=list)
    breaks: List[int] = field(default_factory=list)
    term_sizes: List[Tuple[int, int, int, int]] = field(default_factory=list)


class TargetSession(SSHServerStreamSession):
    """目标服务器上的 session：exec / pty shell / sftp

    继承 `SSHServerStreamSession` 是为了拿到它对 sftp 子系统的现成处理；普通
    exec/shell 走 `_run_stream`，用 SSHReader/SSHWriter 读写，窗口变化/break/
    signal 会以异常形式从 reader 抛出，正好用来验证网关有没有原样转发。
    """

    def __init__(self, config: TargetConfig) -> None:
        self.config = config

        def _sftp(chan: asyncssh.SSHChannel) -> asyncssh.SFTPServer:
            return asyncssh.SFTPServer(chan, chroot=str(config.sftp_root).encode("utf-8"))

        super().__init__(self._run_stream, sftp_factory=_sftp, sftp_version=3)
        self.term_type = ""
        self.term_size: Tuple[int, int, int, int] = (0, 0, 0, 0)
        self.term_modes: Dict[int, int] = {}

    def pty_requested(
        self,
        term_type: str,
        term_size: Tuple[int, int, int, int],
        term_modes: Dict[int, int],
    ) -> bool:
        self.term_type = str(term_type)
        self.term_size = term_size
        self.term_modes = dict(term_modes or {})
        self.config.term_sizes.append(term_size)
        return True

    def env_requested(self, _name: str, _value: str) -> bool:
        return True

    async def _run_stream(
        self,
        stdin: asyncssh.SSHReader,
        stdout: asyncssh.SSHWriter,
        stderr: asyncssh.SSHWriter,
    ) -> None:
        assert self._chan is not None
        command = self._chan.get_command()
        if command:
            await self._run_command(command, stdin, stdout, stderr)
        else:
            await self._run_shell(stdin, stdout)

    async def _run_command(
        self,
        command: str,
        stdin: asyncssh.SSHReader,
        stdout: asyncssh.SSHWriter,
        stderr: asyncssh.SSHWriter,
    ) -> None:
        assert self._chan is not None
        self.config.commands.append(command)
        if command.startswith("echo "):
            stdout.write((command[5:] + "\n").encode("utf-8"))
            await stdout.drain()
            self._chan.exit(0)
            return
        if command == "fail":
            stderr.write(b"boom\n")
            await stderr.drain()
            self._chan.exit(7)
            return
        if command == "cat":
            while True:
                data = await stdin.read(4096)
                if not data:
                    break
                stdout.write(data)
                await stdout.drain()
            self._chan.exit(0)
            return
        if command.startswith("sleep "):
            await asyncio.sleep(float(command[6:]))
            self._chan.exit(0)
            return
        if command == "env":
            stdout.write((json.dumps(dict(self._chan.get_environment()), sort_keys=True) + "\n").encode("utf-8"))
            await stdout.drain()
            self._chan.exit(0)
            return
        if command == "term":
            payload = {
                "type": self._chan.get_terminal_type(),
                "size": list(self._chan.get_terminal_size()),
                "modes": {str(k): v for k, v in self._chan.get_terminal_modes().items()},
            }
            stdout.write((json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
            await stdout.drain()
            self._chan.exit(0)
            return
        if command == "wait-signal":
            try:
                while True:
                    await stdin.read(1)
            except asyncssh.SignalReceived as exc:
                self.config.signals.append(str(exc.signal))
                stdout.write(f"signal:{exc.signal}\n".encode("utf-8"))
                await stdout.drain()
            self._chan.exit(0)
            return
        if command == "wait-break":
            try:
                while True:
                    await stdin.read(1)
            except asyncssh.BreakReceived as exc:
                self.config.breaks.append(int(exc.msec))
                stdout.write(f"break:{exc.msec}\n".encode("utf-8"))
                await stdout.drain()
            self._chan.exit(0)
            return
        stdout.write(f"ran:{command}\n".encode("utf-8"))
        await stdout.drain()
        self._chan.exit(0)

    async def _run_shell(self, stdin: asyncssh.SSHReader, stdout: asyncssh.SSHWriter) -> None:
        assert self._chan is not None
        stdout.write(b"shell-ready\n")
        await stdout.drain()
        while True:
            try:
                data = await stdin.read(4096)
            except asyncssh.TerminalSizeChanged as exc:
                self.config.term_sizes.append(exc.term_size)
                stdout.write(f"size:{exc.width}x{exc.height}\n".encode("utf-8"))
                await stdout.drain()
                continue
            except asyncssh.BreakReceived as exc:
                self.config.breaks.append(int(exc.msec))
                stdout.write(f"break:{exc.msec}\n".encode("utf-8"))
                await stdout.drain()
                continue
            except asyncssh.SignalReceived as exc:
                self.config.signals.append(str(exc.signal))
                stdout.write(f"signal:{exc.signal}\n".encode("utf-8"))
                await stdout.drain()
                self._chan.exit(0)
                return
            if not data:
                break
            stdout.write(data)
            await stdout.drain()
        self._chan.exit(0)


class TargetServer(asyncssh.SSHServer):
    """目标 SSH 服务器的认证与转发判定"""

    def __init__(self, config: TargetConfig) -> None:
        self.config = config
        self.conn: Optional[asyncssh.SSHServerConnection] = None

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        self.conn = conn

    def begin_auth(self, username: str) -> bool:
        return str(username) == self.config.username

    def password_auth_supported(self) -> bool:
        return bool(self.config.allow_password and self.config.password)

    def public_key_auth_supported(self) -> bool:
        return bool(self.config.client_public_key)

    def kbdint_auth_supported(self) -> bool:
        return self.config.kbdint_mode != "off"

    def validate_password(self, _username: str, password: str) -> bool:
        self.config.auth_attempts += 1
        self.config.password_attempts += 1
        return bool(self.config.password) and str(password) == self.config.password

    def validate_public_key(self, _username: str, key: asyncssh.SSHKey) -> bool:
        self.config.auth_attempts += 1
        self.config.public_key_attempts += 1
        expected = public_line(self.config.client_public_key)
        return bool(expected) and public_line(key) == expected

    def get_kbdint_challenge(
        self,
        _username: str,
        _lang: str,
        _submethods: str,
    ) -> object:
        if self.config.kbdint_mode == "mfa":
            return ("", "", "", [("Password: ", False), ("Verification code: ", False)])
        return ("", "", "", [("Password: ", False)])

    def validate_kbdint_response(self, _username: str, responses: List[str]) -> bool:
        self.config.auth_attempts += 1
        self.config.kbdint_attempts += 1
        if self.config.kbdint_mode != "password":
            return False
        return bool(responses) and str(responses[0]) == self.config.password

    def session_requested(self) -> asyncssh.SSHServerSession:
        self.config.sessions += 1
        return TargetSession(self.config)

    def connection_requested(self, _dest_host: str, _dest_port: int, _orig_host: str, _orig_port: int) -> bool:
        self.config.direct_requests += 1
        return True

    def server_requested(self, _listen_host: str, _listen_port: int) -> bool:
        self.config.forward_requests += 1
        return True

    def unix_connection_requested(self, _dest_path: str) -> bool:
        self.config.direct_requests += 1
        return True

    def unix_server_requested(self, _listen_path: str) -> bool:
        self.config.forward_requests += 1
        return True


class TargetHarness:
    """启动一台 127.0.0.1 上的目标服务器"""

    def __init__(self, config: TargetConfig) -> None:
        self.config = config
        self.host_key = asyncssh.generate_private_key("ssh-ed25519")
        self.host_entry = public_line(self.host_key)
        self.port = 0
        self._listener: Optional[asyncssh.SSHAcceptor] = None

    async def start(self) -> "TargetHarness":
        self.config.sftp_root.mkdir(parents=True, exist_ok=True)
        self._listener = await asyncssh.listen(
            "127.0.0.1",
            0,
            server_factory=lambda: TargetServer(self.config),
            server_host_keys=[self.host_key],
            config=None,
            encoding=None,
            password_auth=True,
            public_key_auth=True,
            login_timeout=10,
        )
        self.port = int(self._listener.get_port())
        return self

    async def stop(self) -> None:
        if self._listener is not None:
            self._listener.close()
            try:
                await asyncio.wait_for(self._listener.wait_closed(), timeout=5.0)
            except Exception:  # noqa: BLE001 - 关测试服务器慢不该让用例失败
                pass
            self._listener = None


class EchoTCPServer:
    """一段本地 TCP 回声服务，用来验证 direct-tcpip 真的接到了目标侧"""

    def __init__(self) -> None:
        self.port = 0
        self.received = bytearray()
        self._server: Optional[asyncio.AbstractServer] = None
        self._tasks: List[asyncio.Task[None]] = []

    async def start(self) -> "EchoTCPServer":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])
        return self

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                self.received.extend(data)
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


class EchoContainerSession(asyncssh.SSHTCPSession):
    """容器侧 -R 的 TCP 会话：收到什么就回什么，并留一份收到的数据"""

    def __init__(self) -> None:
        self.chan: Optional[asyncssh.SSHTCPChannel] = None
        self.received = bytearray()

    def connection_made(self, chan: asyncssh.SSHTCPChannel) -> None:
        self.chan = chan

    def data_received(self, data: bytes, _datatype: Optional[int]) -> None:
        self.received.extend(data)
        if self.chan is not None:
            self.chan.write(b"echo:" + data)

    def eof_received(self) -> bool:
        return False
