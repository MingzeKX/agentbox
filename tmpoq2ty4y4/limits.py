"""Resource limits for everything the sandbox executes (standard library only).

Three layers, from outside in:

1. the QEMU process is capped by a Windows Job Object (host side, control plane)
2. this guest caps every executed command in its own cgroup v2 sub-tree
   (memory, pids, cpu) and with POSIX rlimits
3. every spawn is bounded by a wall-clock timeout and an output byte budget

The module imports ``resource``/``pwd``/``grp`` lazily so it can be imported on
non-Linux hosts (needed by the test-suite).
"""

from __future__ import annotations

import errno
import logging
import os
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any

try:  # repository layout: agent.sandbox.limits
    from agent.sandbox import policy
except ImportError:  # pragma: no cover - guest layout: /usr/lib/agent/sandbox/limits.py
    import policy  # type: ignore[no-redef]

log = logging.getLogger(__name__)

CG_ROOT = "/sys/fs/cgroup"
CG_PARENT = "/sys/fs/cgroup/agent"

SANDBOX_USER = "sandbox"
DEFAULT_UID = 1000
DEFAULT_GID = 1000

#: outcome of the most recent per-command cgroup attempt (for sandbox.info)
LAST_CGROUP: dict[str, Any] = {}

DEFAULT_RLIMITS: dict[str, tuple[int, int]] = {
    "cpu": (60, 60),  # seconds of CPU time
    "fsize": (256 * 1024 * 1024, 256 * 1024 * 1024),  # largest file a command may create
    "nofile": (512, 1024),
    "nproc": (128, 128),
    "core": (0, 0),
    "stack": (16 * 1024 * 1024, 16 * 1024 * 1024),
}

#: SIGKILL does not exist on Windows, where this module is only imported by tests
SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def sandbox_ids() -> tuple[int, int]:
    try:
        import grp  # noqa: PLC0415
        import pwd  # noqa: PLC0415

        uid = pwd.getpwnam(SANDBOX_USER).pw_uid
        gid = grp.getgrnam(SANDBOX_USER).gr_gid
        return uid, gid
    except Exception:
        return DEFAULT_UID, DEFAULT_GID


def _resource_module():
    try:
        import resource  # noqa: PLC0415

        return resource
    except ImportError:  # pragma: no cover - Windows (tests only)
        return None


def apply_rlimits(overrides: dict[str, int] | None = None) -> None:
    """Called in the child process right before exec."""
    resource = _resource_module()
    if resource is None:
        return
    limits = dict(DEFAULT_RLIMITS)
    for name, value in (overrides or {}).items():
        limits[name] = (value, value)
    for name, (soft, hard) in limits.items():
        res = getattr(resource, f"RLIMIT_{name.upper()}", None)
        if res is None:
            continue
        try:
            resource.setrlimit(res, (soft, hard))
        except (ValueError, OSError):
            pass


def drop_privileges(uid: int | None = None, gid: int | None = None) -> None:
    """Called in the child process right before exec (no-op when already unprivileged)."""
    if uid is None or gid is None:
        uid, gid = sandbox_ids()
    if getattr(os, "getuid", lambda: 0)() != 0:
        return
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def child_setup(
    *,
    uid: int | None,
    gid: int | None,
    rlimit_overrides: dict[str, int] | None = None,
):
    """Build the ``preexec_fn`` used for every untrusted spawn."""

    def _setup() -> None:  # pragma: no cover - runs in the forked child
        os.umask(0o022)
        apply_rlimits(rlimit_overrides)
        drop_privileges(uid, gid)

    return _setup


@dataclass
class Cgroup:
    """A per-command cgroup v2 sub-tree.

    ``error`` records why the sub-tree could not be created: silently falling back
    to "no limits at all" is how the memory cap went missing.
    """

    path: str
    created: bool
    error: str | None = None

    @classmethod
    def create(
        cls,
        *,
        memory_bytes: int = 512 * 1024 * 1024,
        pids: int = 128,
        cpu_quota_percent: int = 200,
    ) -> Cgroup:
        name = f"run-{uuid.uuid4().hex[:12]}"
        path = os.path.join(CG_PARENT, name)
        try:
            os.makedirs(CG_PARENT, exist_ok=True)
            with open(os.path.join(CG_ROOT, "cgroup.subtree_control"), "w") as fh:
                fh.write("+memory +pids +cpu")
        except OSError as exc:
            log.warning("cannot enable cgroup controllers under %s: %s", CG_ROOT, exc)
        try:
            os.makedirs(path, exist_ok=False)
        except OSError as exc:
            if exc.errno not in (errno.EROFS, errno.EACCES, errno.ENOENT, errno.EPERM, errno.EBUSY):
                raise
            log.warning("cannot create cgroup %s: %s", path, exc)
            return cls(path=path, created=False, error=f"{type(exc).__name__}: {exc}")
        errors: list[str] = []
        for filename, value in (
            ("memory.max", str(memory_bytes)),
            ("memory.swap.max", "0"),
            ("pids.max", str(pids)),
            ("cpu.max", f"{max(1000, cpu_quota_percent * 1000)} 100000"),
        ):
            try:
                with open(os.path.join(path, filename), "w") as fh:
                    fh.write(value)
            except OSError as exc:
                errors.append(f"{filename}: {exc}")
                log.warning("cannot set %s/%s: %s", path, filename, exc)
        return cls(path=path, created=True, error="; ".join(errors) or None)

    def attach(self, pid: int) -> bool:
        if not self.created:
            return False
        try:
            with open(os.path.join(self.path, "cgroup.procs"), "w") as fh:
                fh.write(str(pid))
            return True
        except OSError as exc:
            self.error = f"attach: {type(exc).__name__}: {exc}"
            log.warning("cannot move pid %s into %s: %s", pid, self.path, exc)
            return False

    def events(self) -> dict[str, int]:
        out: dict[str, int] = {}
        if not self.created:
            return out
        for filename in ("memory.events", "pids.events", "memory.peak", "cpu.stat"):
            try:
                with open(os.path.join(self.path, filename)) as fh:
                    for line in fh:
                        parts = line.split()
                        if len(parts) == 2 and parts[1].isdigit():
                            out[f"{filename}:{parts[0]}"] = int(parts[1])
            except OSError:
                continue
        return out

    def destroy(self) -> None:
        if not self.created:
            return
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                os.rmdir(self.path)
                return
            except OSError:
                time.sleep(0.05)


def kill_process_group(pid: int, sig: int | None = None) -> None:
    sig = SIGKILL if sig is None else sig
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def run_command(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout_s: float,
    stdin: str | None,
    max_output_bytes: int,
    memory_bytes: int = 512 * 1024 * 1024,
    pids: int = 128,
    cpu_percent: int = 200,
    as_root: bool = False,
) -> dict[str, Any]:
    """Run one untrusted command under every limit.  Blocking; call from a thread."""
    uid, gid = (None, None) if as_root else sandbox_ids()
    cgroup = Cgroup.create(memory_bytes=memory_bytes, pids=pids, cpu_quota_percent=cpu_percent)
    started = time.monotonic()
    proc: subprocess.Popen[bytes] | None = None
    timed_out = False
    oom = False
    killed_by: str | None = None
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv is explicit, never a shell string unless requested
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            # RLIMIT_AS is the memory backstop: unlike cgroup v2 it is always
            # available, so the budget holds even when no cgroup could be made.
            preexec_fn=child_setup(uid=uid, gid=gid, rlimit_overrides={"as": memory_bytes}),
            close_fds=True,
        )
        cgroup.attach(proc.pid)
        try:
            stdout, stderr = proc.communicate(
                input=stdin.encode("utf-8") if stdin is not None else None,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_process_group(proc.pid)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout, stderr = b"", b""
    finally:
        events = cgroup.events()
        cgroup.destroy()

    LAST_CGROUP.clear()
    LAST_CGROUP.update({"path": cgroup.path, "created": cgroup.created, "error": cgroup.error})
    limits_applied = {
        "memory_mb": memory_bytes // (1024 * 1024),
        "pids": pids,
        "cpu_percent": cpu_percent,
        "cgroup": bool(cgroup.created) and not (cgroup.error or "").startswith("attach"),
        "cgroup_error": cgroup.error,
        "rlimit_as_mb": memory_bytes // (1024 * 1024),
    }
    if events.get("memory.events:oom_kill", 0) or events.get("memory.events:oom", 0):
        oom = True
    elif not timed_out and proc is not None and proc.returncode is not None and proc.returncode < 0:
        if abs(proc.returncode) == int(signal.SIGKILL):
            killed_by = "memory" if oom else None
    if oom:
        killed_by = "memory"

    duration_ms = int((time.monotonic() - started) * 1000)
    return {
        "exit_code": None if timed_out else (proc.returncode if proc is not None else None),
        "stdout": _decode(stdout, max_output_bytes),
        "stderr": _decode(stderr, max_output_bytes),
        "truncated": len(stdout) > max_output_bytes or len(stderr) > max_output_bytes,
        "duration_ms": duration_ms,
        "timed_out": timed_out,
        "oom": oom,
        "killed_by_limit": killed_by,
        "cgroup_events": events,
        "limits": limits_applied,
    }


def _decode(raw: bytes, limit: int) -> str:
    if len(raw) > limit:
        raw = raw[:limit]
    return raw.decode("utf-8", errors="replace")


def base_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/workspace",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": policy.RUN_TMP,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "AGENT_SANDBOX": "1",
    }
    env.update(extra or {})
    return env
