#!/usr/bin/env python3
"""Guest PID 1 (standard library only).

Deliberately *not* systemd: the sandbox root is mounted read-only, and a 200 line
init we can audit is both simpler and smaller than a read-only systemd setup.
Responsibilities:

1. bring up /dev, /proc, /sys, cgroup2 and size-limited tmpfs mounts
2. keep the root filesystem read-only, no matter what the command line said
3. mount the single writable disk at /workspace, owned by the ``sandbox`` user
4. read the per-VM RPC token and VM id from QEMU fw_cfg
5. start :mod:`executor` and power the VM off as soon as it stops
6. power the VM off when the executor stops, so no sandbox is ever orphaned
"""

from __future__ import annotations

import ctypes
import errno
import os
import signal
import subprocess
import sys
import time

try:  # repository layout
    from agent.sandbox import limits as limits_mod
    from agent.sandbox import policy
except ImportError:  # pragma: no cover - guest layout
    import limits as limits_mod  # type: ignore[no-redef]
    import policy  # type: ignore[no-redef]

MS_RDONLY = 1
MS_NOSUID = 2
MS_NODEV = 4
MS_NOEXEC = 8
MS_REMOUNT = 32

RB_POWER_OFF = 0x4321FEDC
MAX_EXECUTOR_RESTARTS = 3
EXECUTOR = "/usr/lib/agent/sandbox/executor.py"

_poweroff_requested = False
_child_exited = False
_libc_cache: ctypes.CDLL | None = None


def libc() -> ctypes.CDLL:
    """Load libc lazily: importing this module must have no side effects."""
    global _libc_cache
    if _libc_cache is None:
        _libc_cache = ctypes.CDLL(None, use_errno=True)
    return _libc_cache


def log(message: str) -> None:
    print(f"[init] {message}", flush=True)


def _mount_via_command(source: str | None, target: str, fstype: str | None, flags: int, data: str | None) -> bool:
    """Fallback when ctypes is unavailable (a stripped python image, for example)."""
    options: list[str] = []
    for bit, name in ((MS_RDONLY, "ro"), (MS_NOSUID, "nosuid"), (MS_NODEV, "nodev"), (MS_NOEXEC, "noexec")):
        if flags & bit:
            options.append(name)
    if data:
        options.append(data)
    argv = ["/bin/mount"]
    if fstype:
        argv += ["-t", fstype]
    if options:
        argv += ["-o", ",".join(options)]
    if source:
        argv.append(source)
    argv.append(target)
    try:
        return subprocess.run(argv, check=False, capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def mount(source: str | None, target: str, fstype: str | None, flags: int = 0, data: str | None = None) -> bool:
    os.makedirs(target, exist_ok=True)
    try:
        libc_handle = libc()
    except Exception:  # pragma: no cover - only on a stripped python
        return _mount_via_command(source, target, fstype, flags, data)
    res = libc_handle.mount(
        source.encode() if source else None,
        target.encode(),
        fstype.encode() if fstype else None,
        ctypes.c_ulong(flags),
        data.encode() if data else None,
    )
    if res != 0:
        err = ctypes.get_errno()
        if err not in (errno.EBUSY, errno.EPERM, errno.EACCES, errno.EROFS):
            log(f"mount {source or ''} -> {target} ({fstype}) failed: {os.strerror(err)}")
        return False
    return True


def poweroff(code: int = 0) -> None:
    log("powering off")
    os.sync()
    try:
        libc().reboot(ctypes.c_int(RB_POWER_OFF))
    except Exception:  # pragma: no cover - only on a stripped python
        for argv in (["/sbin/poweroff", "-f"], ["/usr/sbin/poweroff", "-f"], ["/sbin/reboot", "-f"]):
            try:
                subprocess.run(argv, check=False, timeout=10)
                break
            except (OSError, subprocess.SubprocessError):
                continue
    time.sleep(2)
    os._exit(code)  # pragma: no cover - only if reboot() is unavailable


def on_term(_signum: int, _frame: object) -> None:
    global _poweroff_requested
    _poweroff_requested = True


def on_child(_signum: int, _frame: object) -> None:
    global _child_exited
    _child_exited = True


def setup_console() -> None:
    try:
        fd = os.open("/dev/console", os.O_RDWR)
    except OSError:
        return
    for target in (0, 1, 2):
        try:
            os.dup2(fd, target)
        except OSError:
            pass
    if fd > 2:
        os.close(fd)


def setup_filesystems() -> None:
    mount("devtmpfs", "/dev", "devtmpfs", MS_NOSUID)
    mount("proc", "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC)
    mount("sysfs", "/sys", "sysfs", MS_NOSUID | MS_NODEV | MS_NOEXEC)
    if not os.path.ismount("/sys/fs/cgroup"):
        mount("cgroup2", "/sys/fs/cgroup", "cgroup2", MS_NOSUID | MS_NODEV | MS_NOEXEC, "nsdelegate")
    mount("tmpfs", "/run", "tmpfs", MS_NOSUID | MS_NODEV, "size=32m,mode=0755")
    mount("tmpfs", "/tmp", "tmpfs", MS_NOSUID | MS_NODEV, "size=128m,mode=1777")
    mount("tmpfs", "/var/tmp", "tmpfs", MS_NOSUID | MS_NODEV, "size=64m,mode=1777")
    mount("tmpfs", "/var/log", "tmpfs", MS_NOSUID | MS_NODEV, "size=16m,mode=0755")
    os.makedirs(policy.RUN_TMP, exist_ok=True)
    os.chmod(policy.RUN_TMP, 0o755)


def enforce_readonly_root() -> None:
    """Re-assert MS_RDONLY on / in case the kernel command line did not."""
    if policy.read_fw_cfg("root_rw") == "1":
        log("WARNING: root_rw override requested by the host")
        return
    if mount(None, "/", None, MS_REMOUNT | MS_RDONLY, None):
        log("root filesystem is mounted read-only")
    elif not _root_writable():
        log("root filesystem is read-only")
    else:
        log("WARNING: could not make / read-only")


def _root_writable() -> bool:
    probe = "/.agent-init-probe"
    try:
        with open(probe, "w") as fh:
            fh.write("x")
        os.remove(probe)
        return True
    except OSError:
        return False


def mount_workspace() -> None:
    uid, gid = limits_mod.sandbox_ids()
    device = "/dev/vdb"
    if os.path.exists(device):
        if mount(device, policy.WORKSPACE, "ext4", MS_NOSUID | MS_NODEV, "rw,errors=remount-ro"):
            log(f"workspace disk {device} mounted at {policy.WORKSPACE}")
        else:
            mount("tmpfs", policy.WORKSPACE, "tmpfs", MS_NOSUID | MS_NODEV, "size=256m,mode=0755")
            log("workspace disk unavailable; using an in-memory workspace")
    else:
        mount("tmpfs", policy.WORKSPACE, "tmpfs", MS_NOSUID | MS_NODEV, "size=256m,mode=0755")
        log("no workspace disk attached; using an in-memory workspace")
    try:
        os.chown(policy.WORKSPACE, uid, gid)
        os.chmod(policy.WORKSPACE, 0o755)
    except OSError as exc:
        log(f"cannot chown {policy.WORKSPACE}: {exc}")
    os.makedirs(policy.TOOLS_DIR, exist_ok=True)
    try:
        os.chown(policy.TOOLS_DIR, uid, gid)
    except OSError:
        pass


def load_modules() -> None:
    for module in ("qemu_fw_cfg", "virtio_console", "virtio_rng"):
        if not os.path.exists("/sbin/modprobe"):
            break
        try:
            subprocess.run(["/sbin/modprobe", module], check=False, capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"modprobe {module} failed: {exc}")


def read_guest_token() -> tuple[str, str]:
    """Per-VM token and VM id: fw_cfg first, then a baked-in file, then empty."""
    vm_id = policy.read_fw_cfg("vm_id", "")
    token = policy.read_fw_cfg("token", "")
    if not token:
        try:
            with open(policy.TOKEN_FILE, encoding="utf-8") as fh:
                token = fh.read().strip()
            log("using the baked-in RPC token (fw_cfg unavailable)")
        except OSError:
            token = ""
    if not token:
        log("WARNING: no RPC token available; the control plane may connect without authentication")
    return token, vm_id or "unknown"


def set_hostname(vm_id: str) -> None:
    name = f"sandbox-{vm_id[:12]}" if vm_id else "sandbox"
    try:
        with open("/proc/sys/kernel/hostname", "w") as fh:
            fh.write(name)
    except OSError:
        pass


def start_executor(token: str, vm_id: str) -> subprocess.Popen[bytes]:
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "AGENT_RPC_TOKEN": token,
        "AGENT_VM_ID": vm_id,
        "AGENT_IDLE_EXIT_S": os.environ.get("AGENT_IDLE_EXIT_S", "900"),
        "PYTHONPATH": "/usr/lib/agent",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "HOME": "/workspace",
        "LANG": "C.UTF-8",
    }
    return subprocess.Popen(  # noqa: S603 - fixed interpreter and script path
        ["/usr/bin/python3", EXECUTOR],
        env=env,
        cwd="/",
    )


def main() -> int:
    setup_console()
    log("sandbox init starting")
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    signal.signal(signal.SIGCHLD, on_child)

    setup_filesystems()
    load_modules()
    enforce_readonly_root()
    mount_workspace()

    # only now is /run (tmpfs) available and are /sys, /proc mounted: bring the NIC up
    # when the host attached one (AGENT_SANDBOX_NET_MODE=full); otherwise a no-op
    from agent.sandbox import network

    network.log_report(network.apply(network.mode_from_host()))

    token, vm_id = read_guest_token()
    set_hostname(vm_id)
    log(f"vm_id={vm_id} sandbox_uid={limits_mod.sandbox_ids()[0]} root_readonly={not _root_writable()}")

    child = start_executor(token, vm_id)
    while True:
        if _poweroff_requested:
            child.terminate()
            break
        code = child.poll()
        if code is not None:
            # Do NOT restart the executor: a fresh process starts with an empty
            # authenticated-peer set while the control plane still holds this
            # connection, so every later call would fail with "unauthenticated peer"
            # (observed live after an idle VM sat for ~37 minutes).  Power the VM off
            # instead -- the control plane notices, evicts it and boots a new one on
            # the same session overlay, so the workspace survives.
            log(f"executor exited with {code}; powering off so the control plane can respawn")
            break
        try:
            os.waitpid(-1, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass
        time.sleep(0.5)

    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
    poweroff(0)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException as exc:  # noqa: BLE001 - PID 1 must never die silently
        log(f"fatal: {type(exc).__name__}: {exc}")
        poweroff(1)
