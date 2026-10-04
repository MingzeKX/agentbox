"""Windows host confinement via Job Objects.

Each QEMU process gets its own job with:

* ``JOB_OBJECT_LIMIT_JOB_MEMORY`` / ``PROCESS_MEMORY`` - hard memory caps
* ``JOB_OBJECT_LIMIT_ACTIVE_PROCESS`` - process count cap
* ``JobObjectCpuRateControlInformation`` - hard CPU rate cap (percent)
* ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` - closing the control plane kills the VM

Everything degrades gracefully: when a call fails (for example because the
control plane itself already runs inside a job that forbids nesting) the backend
logs a warning and falls back to the guest-side limits.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from pathlib import Path

from agent.config import Settings
from agent.control.host.base import Confinement, HostLimits

log = logging.getLogger(__name__)

JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION = 15
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4

PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:  # pragma: no cover - platform specific
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("ControlFlags", wintypes.DWORD),
            ("CpuRate", wintypes.DWORD),
        ]
else:  # pragma: no cover - non Windows
    kernel32 = None


class JobConfinement(Confinement):
    def __init__(self, handle: int, detail: str) -> None:
        super().__init__(kind="windows-job", identifier=str(handle), detail=detail)
        self.handle = handle

    def usage(self) -> dict[str, int]:
        if not _IS_WINDOWS or not self.handle:
            return {}
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        returned = ctypes.c_ulong(0)
        ok = kernel32.QueryInformationJobObject(
            ctypes.c_void_p(self.handle),
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(returned),
        )
        if not ok:
            return {}
        return {
            "peak_job_memory": int(info.PeakJobMemoryUsed),
            "peak_process_memory": int(info.PeakProcessMemoryUsed),
            "active_processes": int(info.BasicLimitInformation.ActiveProcessLimit),
        }

    def release(self) -> None:
        if self.handle and _IS_WINDOWS:
            kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = 0


class WindowsJobBackend:
    name = "windows-job"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not _IS_WINDOWS:
            raise RuntimeError("WindowsJobBackend is only available on Windows")

    def accel_args(self) -> list[str]:
        from agent.control.host.base import accel_argv, detect_accel

        return accel_argv(detect_accel(self.settings.sandbox_accel), self.settings.qemu_cpu)

    def workspace_overlay(self, base: Path, target: Path) -> None:
        from agent.control.vm import create_overlay

        create_overlay(base, target)

    # ------------------------------------------------------------------ job
    def confine(self, pid: int, limits: HostLimits) -> Confinement:
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return Confinement(kind="none", detail=f"CreateJobObject failed ({ctypes.get_last_error()})")
        try:
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
            if limits.memory_bytes > 0:
                flags |= JOB_OBJECT_LIMIT_JOB_MEMORY | JOB_OBJECT_LIMIT_PROCESS_MEMORY
                info.JobMemoryLimit = limits.memory_bytes
                info.ProcessMemoryLimit = limits.memory_bytes
            if limits.max_processes > 0:
                flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                info.BasicLimitInformation.ActiveProcessLimit = limits.max_processes
            info.BasicLimitInformation.LimitFlags = flags
            ok = kernel32.SetInformationJobObject(
                ctypes.c_void_p(handle),
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                log.warning("SetInformationJobObject failed: %s", ctypes.get_last_error())

            # Hard CPU rate cap: 50% of the whole machine per vCPU, saturating at
            # 100%.  The real per-command fairness happens in the guest cgroup.
            cpu = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
            cpu.ControlFlags = JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
            cpu.CpuRate = min(10000, max(100, limits.cpus * 5000))
            kernel32.SetInformationJobObject(
                ctypes.c_void_p(handle),
                JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION,
                ctypes.byref(cpu),
                ctypes.sizeof(cpu),
            )

            process = kernel32.OpenProcess(
                PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not process:
                raise OSError(f"OpenProcess({pid}) failed: {ctypes.get_last_error()}")
            try:
                if not kernel32.AssignProcessToJobObject(ctypes.c_void_p(handle), ctypes.c_void_p(process)):
                    raise OSError(f"AssignProcessToJobObject failed: {ctypes.get_last_error()}")
            finally:
                kernel32.CloseHandle(ctypes.c_void_p(process))
        except OSError as exc:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            log.warning("job object confinement unavailable for pid %s (%s)", pid, exc)
            return Confinement(kind="none", detail=str(exc))

        detail = (
            f"memory<={limits.memory_bytes // (1024 * 1024)}MiB "
            f"processes<={limits.max_processes} cpurate<={min(10000, max(100, limits.cpus * 5000)) / 100:.0f}%"
        )
        return JobConfinement(int(handle), detail)
