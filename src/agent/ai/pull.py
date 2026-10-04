"""``fs.pull`` -- hand the operator a file or image the agent produced in the sandbox.

The direction is the reverse of ``net.fetch``: the bytes are read out of the sandbox
workspace through the gateway (guest RPC ``fs.read``, ``binary=True``) and written on
the host through the control plane (RPC ``host.pull.write``).  This module therefore
imports no filesystem and no OS API -- the isolation guard test would fail if it did.

Guardrails, all enforced here *and* again on the control plane:

* only ``/workspace`` (the sandbox's one writable, per-session directory)
* at most ``AGENT_FS_PULL_MAX_BYTES`` (default 8 MB, the same cap ``fs.read`` uses)
* ``dest`` is relative to ``<repo>/var/pulled`` -- no ``..``, no absolute path, no drive
* an existing host file is refused unless ``overwrite`` is true

The result never echoes the file's content back into the conversation: the model gets
a host path, a size and a hash.
"""

from __future__ import annotations

import base64
import hashlib
import posixpath
import re
from pathlib import PurePosixPath
from typing import Any

from agent.config import settings

#: where the agent may read from (the sandbox workspace, nothing else)
SANDBOX_ROOT = "/workspace"

#: extensions the operator's Windows viewer opens inline (``/get`` runs Start-Process)
IMAGE_SUFFIXES: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

#: how much of one file may travel in a single RPC (base64 inflates this by 4/3;
#: the wire protocol caps a frame at 8 MiB)
CHUNK_BYTES = 1_000_000

_DRIVE = re.compile(r"^[A-Za-z]:")


def max_bytes() -> int:
    return int(settings.fs_pull_max_bytes or 8_000_000)


def sandbox_path(raw: Any) -> str:
    """Validate and normalise a sandbox path.  Raises ``ValueError`` when it escapes."""
    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        raise ValueError("path must not be empty")
    if _DRIVE.match(text):
        raise ValueError(f"path must be a POSIX path inside {SANDBOX_ROOT} (got {raw!r})")
    if not text.startswith("/"):
        text = f"{SANDBOX_ROOT}/{text}"
    normalised = posixpath.normpath(text)
    if normalised != SANDBOX_ROOT and not normalised.startswith(f"{SANDBOX_ROOT}/"):
        raise ValueError(f"path must stay inside {SANDBOX_ROOT} (got {raw!r} -> {normalised})")
    return normalised


def safe_dest(raw: Any, filename: str) -> str:
    """Validate a host destination *relative to* ``var/pulled``.  Raises ``ValueError``."""
    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        text = filename
    if text.startswith("/") or _DRIVE.match(text):
        raise ValueError(f"dest must be relative to var\\pulled (got {raw!r})")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"dest must stay inside var\\pulled (got {raw!r})")
    return "/".join(parts)


def is_image(name: str) -> bool:
    return PurePosixPath(str(name or "").lower().replace("\\", "/")).suffix in IMAGE_SUFFIXES


async def _read_all(ctx: Any, path: str, cap: int) -> tuple[bytes, str, int]:
    """Read the whole file out of the sandbox in protocol-sized chunks.

    Returns ``(payload, sha256, size)``.  ``fs.read`` caps one call at 8 MB and the
    frame at 8 MiB, so a chunked loop is what keeps the byte budget honest.
    """
    chunks: list[bytes] = []
    digest = hashlib.sha256()
    offset = 0
    size = 0
    while True:
        outcome = await ctx.gateway.invoke_native(
            ctx.session_id,
            "fs.read",
            {"path": path, "offset": offset, "max_bytes": min(CHUNK_BYTES, cap - size), "binary": True},
            timeout_s=120.0,
        )
        if not outcome.ok:
            raise RuntimeError(outcome.error or f"cannot read {path}")
        result = outcome.result if isinstance(outcome.result, dict) else {}
        size = int(result.get("size") or 0)
        if size > cap:
            raise ValueError(
                f"{path} is {size} bytes; the pull cap is {cap} bytes (AGENT_FS_PULL_MAX_BYTES)"
            )
        try:
            chunk = base64.b64decode(str(result.get("data_b64") or ""), validate=True)
        except ValueError as exc:
            raise RuntimeError(f"fs.read returned unusable data for {path}: {exc}") from exc
        if not chunk:
            break
        chunks.append(chunk)
        digest.update(chunk)
        offset += len(chunk)
        if offset >= size:
            break
    payload = b"".join(chunks)
    return payload, digest.hexdigest(), max(size, len(payload))


async def _write_host(ctx: Any, dest: str, payload: bytes, overwrite: bool) -> str:
    """Send the bytes to the control plane, which writes them under ``var/pulled``.

    One RPC per chunk: the frame limit, and the fact that the control plane is the
    only process allowed to open a host file, are why this is not a single call.
    """
    host_path = ""
    first = True
    chunks = [payload[offset : offset + CHUNK_BYTES] for offset in range(0, len(payload), CHUNK_BYTES)] or [b""]
    for chunk in chunks:
        params = {
            "dest": dest,
            "data_b64": base64.b64encode(chunk).decode("ascii"),
            "append": not first,
            "overwrite": bool(overwrite),
        }
        written = await ctx.gateway.rpc("host.pull.write", params)
        if not isinstance(written, dict) or not written.get("path"):
            raise RuntimeError(f"the control plane refused the write: {written}")
        host_path = str(written["path"])
        first = False
    return host_path


async def fs_pull(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Copy one sandbox file to the host's ``var/pulled`` and report where it landed."""
    try:
        path = sandbox_path(args.get("path"))
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "error_code": "path_denied"}

    cap = max_bytes()
    filename = PurePosixPath(path).name or "pulled.bin"
    try:
        dest = safe_dest(args.get("dest"), filename)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "error_code": "dest_denied"}

    overwrite = bool(args.get("overwrite"))
    try:
        payload, sha256, size = await _read_all(ctx, path, cap)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "error_code": "too_large"}
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc), "error_code": "sandbox_error"}
    if len(payload) > cap:
        return {
            "ok": False,
            "error": f"{path} is {len(payload)} bytes; the pull cap is {cap} bytes (AGENT_FS_PULL_MAX_BYTES)",
            "error_code": "too_large",
        }

    try:
        host_path = await _write_host(ctx, dest, payload, overwrite)
    except Exception as exc:  # noqa: BLE001 - RpcError and friends become a tool result
        return {"ok": False, "error": f"cannot write {dest}: {exc}", "error_code": "host_write_failed"}

    return {
        "ok": True,
        "host_path": host_path,
        "sandbox_path": path,
        "bytes": len(payload),
        "sha256": sha256,
        "is_image": is_image(filename),
        "source_size": size,
        "hint": f"open it on the host at {host_path} (the console command is /get {path})",
    }


PULL_HANDLERS = {"fs.pull": fs_pull}
