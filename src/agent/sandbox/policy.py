"""Guest path policy: everything a tool may touch lives under /workspace.

Standard library only.  Used by the RPC handlers and by the tool runner so that
both enforce exactly the same rules (defence in depth: the static checker also
rejects undeclared access).

Paths are handled with **POSIX semantics** on purpose: this module only ever runs
inside the Linux guest, and using ``posixpath`` instead of ``os.path`` keeps the
rules identical when the unit suite exercises them on a Windows development
host.  Symlink resolution (``realpath``) is applied only on POSIX hosts.
"""

from __future__ import annotations

import os
import posixpath

WORKSPACE = "/workspace"
TOOLS_DIR = "/workspace/.tools"
RUN_TMP = "/tmp/agent-run"
ETC_DIR = "/etc/agent"
TOKEN_FILE = "/etc/agent/rpc-token"


class PathDenied(PermissionError):
    pass


def is_within(path: str, root: str = WORKSPACE) -> bool:
    path = posixpath.normpath(path)
    root = posixpath.normpath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def _canonical(path: str) -> str:
    """Normalise, and follow symlinks when the host is POSIX (i.e. the guest)."""
    if os.name == "posix":
        return posixpath.normpath(os.path.realpath(path))
    return posixpath.normpath(path)


def resolve(path: str, *, must_exist: bool = False) -> str:
    """Resolve ``path`` and guarantee it stays inside /workspace.

    Resolution follows symlinks inside the guest, so a symlink pointing outside
    the workspace is rejected.  Relative paths are interpreted relative to
    /workspace, and ``..`` cannot escape.
    """
    if not isinstance(path, str) or not path:
        raise PathDenied("path must be a non-empty string")
    if "\x00" in path:
        raise PathDenied("path must not contain NUL bytes")
    if not path.startswith("/"):
        path = posixpath.join(WORKSPACE, path)
    normalised = posixpath.normpath(path)
    if not is_within(normalised):
        raise PathDenied(f"path {path!r} escapes {WORKSPACE}")
    real = _canonical(normalised)
    if not is_within(real):
        raise PathDenied(f"path {path!r} resolves outside {WORKSPACE} (symlink escape)")
    if must_exist and not os.path.exists(real):
        raise FileNotFoundError(f"{path} does not exist")
    return real


def resolve_dir(path: str, *, create: bool = False) -> str:
    real = resolve(path)
    if not os.path.isdir(real):
        if not create:
            raise NotADirectoryError(f"{path} is not a directory")
        os.makedirs(real, exist_ok=True)
    return real


PERMISSION_ERRORS = (PathDenied, PermissionError)

FW_CFG_ROOT = "/sys/firmware/qemu_fw_cfg/by_name"


def read_fw_cfg(name: str, default: str = "") -> str:
    """Read a QEMU fw_cfg entry (``opt/agent/<name>``) injected on the command line.

    This is how the per-VM RPC token and the VM id reach the guest without any
    writable disk image and without a network interface.
    """
    path = os.path.join(FW_CFG_ROOT, f"opt/agent/{name}", "raw")
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return default



def require(permissions: set[str], needed: str) -> None:
    if needed and needed not in permissions:
        raise PathDenied(f"this tool did not declare the {needed!r} permission")
