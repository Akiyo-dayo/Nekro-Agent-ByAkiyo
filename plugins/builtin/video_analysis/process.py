"""外部工具的进程树与内存上限

ffmpeg 解析的是敌意文件，只靠 `-max_alloc` 不够：它管单次分配，多个小块照样能把内存吃满。
所以再加一层操作系统的上限，并且杀进程时杀整棵树，而不只是直接子进程。

- **Linux**：`start_new_session=True` 让工具自成一个进程组，`killpg` 一起杀。内存用
  `prlimit` 设 `RLIMIT_AS`。不用 `preexec_fn`：NA 进程里线程很多，fork 之后跑 Python 代码可能死锁。
  代价是从 exec 到设上限之间有一个极短的窗口，工具这时还没开始读文件。
- **Windows**：Job Object。进程以挂起状态创建，进 Job 之后才恢复，所以不存在子进程逃出去的窗口；
  内存上限也设在 Job 上。

设置上限失败时杀掉进程并报错：这些上限是防线，不是优化。
"""

import ctypes
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Sequence

# 单个工具进程的地址空间上限。8K 解码加参考帧在 1GB 以内，再留出映像与线程栈
TOOL_MEMORY_LIMIT_BYTES = 2 * 1024 * 1024 * 1024

_IS_WINDOWS = os.name == "nt"

_CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class ToolStartError(OSError):
    """工具没能在隔离条件下启动"""


class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ("Read", "Write", "Other", "ReadBytes", "WriteBytes", "OtherBytes")]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimit),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _win32() -> "ctypes.WinDLL":  # type: ignore[name-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    return kernel32


def _create_job() -> int:
    kernel32 = _win32()
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ToolStartError("CreateJobObject 失败")
    limit = _ExtendedLimit()
    limit.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_PROCESS_MEMORY
    limit.ProcessMemoryLimit = TOOL_MEMORY_LIMIT_BYTES
    if not kernel32.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limit), ctypes.sizeof(limit)):
        kernel32.CloseHandle(job)
        raise ToolStartError("SetInformationJobObject 失败")
    return job


def limit_memory(pid: int) -> None:
    """Linux 上给已启动的进程设地址空间上限"""
    import resource  # noqa: PLC0415 - Windows 没有这个模块

    prlimit = getattr(resource, "prlimit", None)
    if prlimit is None:  # macOS：只作开发环境，没有等价的设置别的进程的接口
        return
    try:
        prlimit(pid, resource.RLIMIT_AS, (TOOL_MEMORY_LIMIT_BYTES, TOOL_MEMORY_LIMIT_BYTES))
    except ProcessLookupError:
        return  # 已经退出，没什么可限的


@dataclass
class ToolProcess:
    """一个外部工具及其整棵进程树"""

    process: "subprocess.Popen[bytes]"
    job: Optional[int] = None

    def kill_tree(self) -> None:
        if self.job is not None:
            _win32().TerminateJobObject(self.job, 1)
            return
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def release(self) -> None:
        """收尾：工具退出后留下的子孙也不放过"""
        self.kill_tree()
        if self.job is not None:
            _win32().CloseHandle(self.job)
            self.job = None


def spawn_tool(argv: Sequence[str], *, cwd: Path, env: Dict[str, str]) -> ToolProcess:
    """启动工具：自成进程树，带内存上限。参数列表直传，不经 shell"""
    common = {
        "cwd": str(cwd),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": env,
    }
    if not _IS_WINDOWS:
        tool = ToolProcess(subprocess.Popen(list(argv), start_new_session=True, **common))
        try:
            limit_memory(tool.process.pid)
        except OSError as e:
            tool.kill_tree()
            tool.process.wait()
            raise ToolStartError("无法设置内存上限") from e
        return tool

    job = _create_job()
    popen = subprocess.Popen(list(argv), creationflags=_CREATE_SUSPENDED, **common)
    handle = int(popen._handle)  # type: ignore[attr-defined]  # noqa: SLF001
    if not _win32().AssignProcessToJobObject(job, handle):
        popen.kill()
        popen.wait()
        _win32().CloseHandle(job)
        raise ToolStartError("AssignProcessToJobObject 失败")
    ctypes.WinDLL("ntdll").NtResumeProcess(ctypes.c_void_p(handle))  # type: ignore[attr-defined]
    return ToolProcess(popen, job)
