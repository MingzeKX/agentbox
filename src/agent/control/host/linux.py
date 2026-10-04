"""Linux host confinement via cgroup v2.

A dedicated subtree (``/sys/fs/cgroup/agentbox``) holds one cgroup per VM.  When
the control plane runs unprivileged the delegation is missing and the backend
degrades to "no host confinement" with a warning -- the guest side limits
(cgroup inside the VM, rlimits, timeouts) still apply.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from agent.config import Settings
from agent.control.host.base import Confinement, HostLimits

log = logging.getLogger(__name__)

CG_ROOT = Path("/sys/fs/cgroup")
CG_PARENT = CG_ROOT / "agentbox"


class LinuxCgroupBackend:
    name = "linux-cgroup2"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root_ok = CG_ROOT.is_dir() and os.access(CG_ROOT, os.W_OK)
        if not self.root_ok:
            log.warning("cgroup v2 root is not writable; per-VM host limits are disabled")

    def accel_args(self) -> list[str]:
        from agent.control.host.base import accel_argv, detect_accel

        return accel_argv(detect_accel(self.settings.sandbox_accel), self.settings.qemu_cpu)

    def workspace_overlay(self, base: Path, target: Path) -> None:
        from agent.control.vm import create_overlay

        create_overlay(base, target)

    def confine(self, pid: int, limits: HostLimits) -> Confinement:
        if not self.root_ok:
            return Confinement(kind="none", detail="cgroup v2 not writable")
        path = CG_PARENT / f"vm-{pid}"
        try:
            CG_PARENT.mkdir(parents=True, exist_ok=True)
            path.mkdir(exist_ok=True)
            for filename, value in (
                ("memory.max", str(limits.memory_bytes)),
                ("memory.swap.max", "0"),
                ("pids.max", str(limits.max_processes)),
                ("cpu.max", f"{limits.cpus * 100000} 100000"),
            ):
                try:
                    (path / filename).write_text(value)
                except OSError:
                    pass
            (path / "cgroup.procs").write_text(str(pid))
        except OSError as exc:
            shutil.rmtree(path, ignore_errors=True)
            log.warning("cgroup confinement failed for pid %s: %s", pid, exc)
            return Confinement(kind="none", detail=str(exc))
        return CgroupConfinement(str(path))


class CgroupConfinement(Confinement):
    def __init__(self, path: str) -> None:
        super().__init__(kind="linux-cgroup2", identifier=path, detail=path)

    def usage(self) -> dict[str, int]:
        out: dict[str, int] = {}
        base = Path(self.identifier)
        for name in ("memory.peak", "pids.peak", "memory.events", "pids.events"):
            try:
                for line in (base / name).read_text().splitlines():
                    parts = line.split()
                    if len(parts) == 2 and parts[1].isdigit():
                        out[f"{name}:{parts[0]}"] = int(parts[1])
            except OSError:
                continue
        return out

    def release(self) -> None:
        shutil.rmtree(self.identifier, ignore_errors=True)
