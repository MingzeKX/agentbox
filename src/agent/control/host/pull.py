"""Write a file the sandbox produced onto the host machine.

This is the *transport* half of "give the operator the file the agent just made".
It is deliberately not ``host.exec``: no process is spawned and nothing outside one
directory is ever touched.  The directory is ``<var_dir>/pulled`` and every rule here
exists to keep a model-supplied ``dest`` inside it:

* ``dest`` is always relative -- an absolute path, a drive letter or a ``..`` segment
  is refused, and the resolved path is checked to be under the pull directory anyway
* an existing host file is refused unless ``overwrite`` is true
* one pulled file is capped at ``fs_pull_max_bytes`` -- the AI service, the console and
  this module all check the same number, so the cap cannot be bypassed by asking nicely

Only the control plane may write these bytes; the AI service asks for it over RPC
(``host.pull.write``) because it is not allowed to touch the filesystem at all.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
from pathlib import Path

from agent.config import Settings, project_root

log = logging.getLogger(__name__)

#: "C:..." or any single-letter drive form: never a valid relative destination
_DRIVE = re.compile(r"^[A-Za-z]:")


class HostPullError(RuntimeError):
    """Refused or failed (the caller turns this into a JSON-RPC error)."""


def pull_dir(settings: Settings) -> Path:
    """The one directory pulled files may land in: ``<var_dir>/pulled``."""
    return Path(settings.var_dir).resolve() / "pulled"


def safe_dest(settings: Settings, dest: str) -> Path:
    """Resolve a caller-supplied ``dest`` to an absolute path inside :func:`pull_dir`."""
    base = pull_dir(settings)
    text = str(dest or "").strip().replace("\\", "/")
    if not text:
        raise HostPullError("dest must not be empty")
    if text.startswith("/") or _DRIVE.match(text):
        raise HostPullError(f"dest must be relative to {base} (got {dest!r})")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise HostPullError(f"dest must stay inside {base} (got {dest!r})")
    target = (base / Path(*parts)).resolve()
    if target != base and base not in target.parents:
        raise HostPullError(f"dest must stay inside {base} (got {target})")
    if target == base:
        raise HostPullError("dest must name a file, not the directory")
    return target


def write(
    settings: Settings,
    *,
    dest: str,
    data_b64: str,
    append: bool = False,
    overwrite: bool = False,
    max_bytes: int | None = None,
) -> dict:
    """Write one chunk of a pulled file.  Returns ``{path, bytes, total_bytes, ...}``.

    ``append`` is how the caller sends a file larger than one protocol frame: the
    first chunk (``append=False``) truncates, every later chunk appends.
    """
    target = safe_dest(settings, dest)
    cap = int(max_bytes or settings.fs_pull_max_bytes)
    try:
        payload = base64.b64decode(str(data_b64 or ""), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HostPullError(f"data_b64 is not valid base64: {exc}") from exc
    if len(payload) > cap:
        raise HostPullError(f"refusing {len(payload)} bytes: the pull cap is {cap} bytes (AGENT_FS_PULL_MAX_BYTES)")
    if not append and target.exists() and not overwrite:
        raise HostPullError(f"{target} already exists; pass overwrite=true to replace it")

    target.parent.mkdir(parents=True, exist_ok=True)
    mode = "ab" if append else "wb"
    with open(target, mode) as fh:  # noqa: PTH123 - safe_dest() above is the confinement
        fh.write(payload)
        written = fh.tell()
    log.info("host.pull %s (+%d bytes, total %d)", target, len(payload), written)
    return {
        "path": str(target),
        "bytes": len(payload),
        "total_bytes": written,
        "cap_bytes": cap,
        "root": str(pull_dir(settings)),
    }


def describe(settings: Settings) -> dict:
    return {
        "enabled": bool(settings.host_pull_enabled),
        "phrase_set": bool(settings.host_pull_phrase),
        "dir": str(pull_dir(settings)),
        "max_bytes": int(settings.fs_pull_max_bytes),
        "repo_root": str(project_root()),
        "exists": os.path.isdir(pull_dir(settings)),
    }
