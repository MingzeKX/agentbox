"""Run a command on the machine that hosts the control plane -- the "no sandbox" mode.

This exists because the operator asked for it, and it deliberately breaks the project's
main invariant: the agent can now touch the host.  Everything here is built to make that
as visible and as narrow as possible:

* both processes must agree -- the control plane needs ``AGENT_PERMISSION_TIER=unrestricted``
* the caller must pass the confirmation phrase (``AGENT_HOST_EXEC_PHRASE``), so a stray
  tool call from the model cannot arm itself
* the working directory is confined to ``AGENT_HOST_WORKDIR`` (default: the repo root)
* per-command timeout, output cap, and a scrubbed environment (no control-plane secrets)
* every command is logged at WARNING and recorded as a tool run by the caller
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path

from agent.config import Settings, project_root

log = logging.getLogger(__name__)

MAX_OUTPUT_BYTES = 200_000
#: never forward these to a child process
SECRET_ENV_HINTS = ("TOKEN", "SECRET", "KEY", "PASSWORD", "API")


class HostExecError(RuntimeError):
    """Refused or failed (never raised for a normal non-zero exit code)."""


def workdir(settings: Settings) -> Path:
    configured = settings.host_workdir
    base = Path(configured) if configured else project_root()
    return base.resolve()


def _confine(settings: Settings, requested: str | None) -> Path:
    base = workdir(settings)
    candidate = (base / requested).resolve() if requested else base
    if base != candidate and base not in candidate.parents:
        raise HostExecError(f"cwd must stay inside {base} (got {candidate})")
    if not candidate.is_dir():
        raise HostExecError(f"cwd does not exist: {candidate}")
    return candidate


def _env() -> dict[str, str]:
    clean: dict[str, str] = {}
    for key, value in os.environ.items():
        if any(hint in key.upper() for hint in SECRET_ENV_HINTS):
            continue
        clean[key] = value
    clean["AGENT_HOST_EXEC"] = "1"
    return clean


async def run(
    settings: Settings,
    *,
    argv: list[str],
    cwd: str | None = None,
    timeout_s: float | None = None,
    confirm: str | None = None,
    max_output_bytes: int | None = None,
) -> dict:
    """Execute ``argv`` on the host.  Returns a result dict (never raises on exit != 0)."""
    if settings.permission_tier != "unrestricted":
        raise HostExecError(
            "host execution is disabled: set AGENT_PERMISSION_TIER=unrestricted on the control plane"
        )
    if not settings.host_exec_phrase or confirm != settings.host_exec_phrase:
        raise HostExecError("host execution requires the confirmation phrase (confirm=<AGENT_HOST_EXEC_PHRASE>)")
    if not argv or not all(isinstance(item, str) for item in argv):
        raise HostExecError("argv must be a non-empty list of strings")

    target = _confine(settings, cwd)
    timeout = float(timeout_s or settings.host_exec_timeout_s)
    cap = int(max_output_bytes or MAX_OUTPUT_BYTES)
    started = time.monotonic()
    log.warning("host.exec %s (cwd=%s, timeout=%.0fs)", " ".join(argv)[:400], target, timeout)

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(target),
            env=_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as exc:
        raise HostExecError(f"cannot start {argv[0]!r}: {exc}") from exc

    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
        # reap it, otherwise the pipe transports linger (and Windows warns about it)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=5)
        stdout, stderr = b"", b""

    return {
        "argv": argv,
        "cwd": str(target),
        "exit_code": None if timed_out else process.returncode,
        "stdout": stdout[:cap].decode("utf-8", errors="replace"),
        "stderr": stderr[:cap].decode("utf-8", errors="replace"),
        "truncated": len(stdout) > cap or len(stderr) > cap,
        "timed_out": timed_out,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
