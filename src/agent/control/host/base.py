"""Host confinement backends: how the control plane limits a QEMU process.

The control plane is the only component allowed to create processes.  Whatever
the host, every VM process is confined:

* **Linux**   - cgroup v2 (memory.max, pids.max, cpu.max) via direct writes
* **Windows**  - a Job Object with job/process memory caps, an active process
  limit, a hard CPU rate cap and ``KILL_ON_JOB_CLOSE`` so no VM survives the
  control plane
* **fallback** - no host confinement; the guest side limits still apply, and the
  caller is warned
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from agent.config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HostLimits:
    memory_bytes: int
    max_processes: int
    cpus: int


@dataclass
class Confinement:
    """Handle for a confined process.  Always safe to ``release()``."""

    kind: str
    identifier: str = ""
    detail: str = ""

    def release(self) -> None:  # pragma: no cover - overridden
        return None

    def usage(self) -> dict[str, int]:
        return {}


@runtime_checkable
class HostBackend(Protocol):
    name: str

    def accel_args(self) -> list[str]: ...

    def confine(self, pid: int, limits: HostLimits) -> Confinement: ...

    def workspace_overlay(self, base: Path, target: Path) -> None: ...


# --------------------------------------------------------------------------- #
# accelerator selection
# --------------------------------------------------------------------------- #


def detect_accel(requested: str = "auto") -> str:
    """Pick an accelerator: kvm on Linux, whpx on Windows, else tcg."""
    if requested != "auto":
        return requested
    if sys.platform.startswith("linux") and os.path.exists("/dev/kvm"):
        return "kvm"
    if sys.platform == "win32":
        return "whpx"
    return "tcg"


#: CPU model per accelerator.
#:
#: WHPX on Windows cannot virtualise ``-cpu host`` or ``-cpu max``: the guest dies
#: immediately with "WHPX: Unexpected VP exit code 4" (measured on QEMU 11.1.0 /
#: Windows 11).  ``qemu64`` and ``Nehalem`` both boot fine, so ``qemu64`` -- the
#: conservative QEMU default -- is used unless the operator overrides it with
#: ``AGENT_SANDBOX_CPU``.
DEFAULT_CPU: dict[str, str] = {"kvm": "host", "whpx": "qemu64", "tcg": "max"}


def accel_argv(accel: str, cpu: str | None = None) -> list[str]:
    cpu = cpu or DEFAULT_CPU.get(accel, "qemu64")
    if accel == "kvm":
        return ["-accel", "kvm", "-cpu", cpu]
    if accel == "whpx":
        return ["-accel", "whpx", "-cpu", cpu]
    return ["-accel", "tcg,thread=multi", "-cpu", cpu]


# --------------------------------------------------------------------------- #
# null backend
# --------------------------------------------------------------------------- #


class NullBackend:
    name = "none"

    def accel_args(self) -> list[str]:
        return []

    def confine(self, pid: int, limits: HostLimits) -> Confinement:
        return Confinement(kind="none", detail="host confinement unavailable")

    def workspace_overlay(self, base: Path, target: Path) -> None:
        from agent.control.vm import create_overlay

        create_overlay(base, target)


def build_backend(settings: Settings) -> HostBackend:
    if sys.platform == "win32":
        from agent.control.host.windows import WindowsJobBackend

        return WindowsJobBackend(settings)
    if sys.platform.startswith("linux"):
        from agent.control.host.linux import LinuxCgroupBackend

        return LinuxCgroupBackend(settings)
    log.warning("no host confinement backend for %s; running with guest-side limits only", sys.platform)
    return NullBackend()
