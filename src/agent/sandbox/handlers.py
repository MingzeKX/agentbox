"""RPC method implementations executed inside the VM (standard library only).

All filesystem access is confined to /workspace by :mod:`policy`; all command
execution goes through :mod:`limits`; tool source is verified by sha256 and
installed read-only before it is ever imported.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import shutil
import stat
import sys
import time
import uuid
from collections.abc import Callable
from typing import Any

try:  # repository layout
    from agent.sandbox import checker as checker_mod
    from agent.sandbox import limits as limits_mod
    from agent.sandbox import policy
except ImportError:  # pragma: no cover - guest layout
    import checker as checker_mod  # type: ignore[no-redef]
    import limits as limits_mod  # type: ignore[no-redef]
    import policy  # type: ignore[no-redef]

PROTOCOL_VERSION = 1
RUNNER = "/usr/lib/agent/sandbox/runner.py"
PYTHON = "/usr/bin/python3"
#: per-command memory budget (RLIMIT_AS + cgroup); raise it per call for builds
DEFAULT_MEMORY_MB = 1024
MAX_READ_BYTES = 8 * 1024 * 1024

STARTED_AT = time.time()


def uid() -> int:
    """Current uid; 0 on non-POSIX hosts (where this module is only unit tested)."""
    getuid = getattr(os, "getuid", None)
    return int(getuid()) if getuid is not None else 0


class HandlerError(Exception):
    """Maps to a JSON-RPC error object."""

    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}


class Methods:
    """Container for every RPC method; instantiated once per executor process."""

    def __init__(self, token: str, vm_id: str) -> None:
        self.token = token
        self.vm_id = vm_id
        self.commands_run = 0
        self.bytes_written = 0
        self.tool_runs = 0
        self.authed_peers: set[str] = set()

    # ------------------------------------------------------------ dispatch
    def dispatch(self, method: str, params: dict[str, Any], peer: str) -> Any:
        handler: Callable[[dict[str, Any]], Any] | None = getattr(self, f"m_{method.replace('.', '_')}", None)
        if handler is None:
            raise HandlerError(-32601, f"method {method!r} not found")
        if method not in {"sys.hello", "sys.ping"} and self.token and peer not in self.authed_peers:
            raise HandlerError(1000, "unauthenticated peer: call sys.hello first")
        try:
            return handler(params)
        except HandlerError:
            raise
        except policy.PathDenied as exc:
            raise HandlerError(1008, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HandlerError(1003, str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise HandlerError(-32602, f"{type(exc).__name__}: {exc}") from exc

    # ----------------------------------------------------------------- sys
    def m_sys_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True, "ts": time.time(), "vm_id": self.vm_id}

    def m_sys_hello(self, params: dict[str, Any]) -> dict[str, Any]:
        offered = str(params.get("token") or "")
        nonce = str(params.get("nonce") or "")
        if not self.token:
            raise HandlerError(1005, "guest has no RPC token configured")
        if not nonce:
            raise HandlerError(-32602, "nonce is required")
        if not hmac.compare_digest(offered, self.token):
            raise HandlerError(1000, "invalid RPC token")
        proof = hmac.new(self.token.encode(), f"{nonce}:{self.vm_id}".encode(), hashlib.sha256).hexdigest()
        return {
            "ok": True,
            "vm_id": self.vm_id,
            "proof": proof,
            "hostname": platform.node(),
            "kernel": platform.release(),
            "python": sys.version.split()[0],
            "uid": uid(),
            "protocol": PROTOCOL_VERSION,
            "capabilities": ["fs", "exec", "py.check", "py.run", "tool.test", "tool.install", "cgroup"],
        }

    def authenticate(self, peer: str) -> None:
        self.authed_peers.add(peer)

    def m_sys_shutdown(self, params: dict[str, Any]) -> dict[str, Any]:
        """Clean power-off, invoked by the control plane before releasing a VM.

        Lets QEMU flush the workspace overlay instead of being hard-killed, which
        keeps the per-session qcow2 crash-free.
        """
        os.sync()
        try:
            import ctypes  # local import: a stripped python image may not ship it

            libc = ctypes.CDLL(None, use_errno=True)
            libc.reboot(ctypes.c_int(0x4321FEDC))  # RB_POWER_OFF
        except Exception as exc:  # noqa: BLE001 - best effort
            return {"ok": False, "detail": f"poweroff failed: {exc}"}
        return {"ok": True, "detail": "powering off"}

    # ------------------------------------------------------------------ fs
    def m_fs_read(self, params: dict[str, Any]) -> dict[str, Any]:
        path = str(params.get("path") or "")
        offset = int(params.get("offset") or 0)
        length = params.get("length")
        binary = bool(params.get("binary"))
        cap = min(int(params.get("max_bytes") or 1_000_000), MAX_READ_BYTES)
        real = policy.resolve(path, must_exist=True)
        if os.path.isdir(real):
            raise HandlerError(-32602, f"{path} is a directory; use fs.list")
        size = os.path.getsize(real)
        with open(real, "rb") as fh:
            if offset:
                fh.seek(offset)
            want = cap if length is None else min(int(length), cap)
            data = fh.read(want + 1)
        truncated = len(data) > want
        data = data[:want]
        digest = hashlib.sha256(data).hexdigest()
        out: dict[str, Any] = {
            "path": real,
            "size": size,
            "sha256": digest,
            "truncated": truncated or (offset + len(data) < size),
        }
        if binary:
            out["data_b64"] = base64.b64encode(data).decode("ascii")
        else:
            out["text"] = data.decode("utf-8", errors="replace")
        return out

    def m_fs_write(self, params: dict[str, Any]) -> dict[str, Any]:
        path = str(params.get("path") or "")
        text = params.get("text")
        data_b64 = params.get("data_b64")
        if (text is None) == (data_b64 is None):
            raise HandlerError(-32602, "provide exactly one of 'text' or 'data_b64'")
        payload = text.encode("utf-8") if text is not None else base64.b64decode(str(data_b64))
        mode = int(str(params.get("mode") or "0644"), 8)
        append = bool(params.get("append"))
        create_dirs = params.get("create_dirs", True)
        real = policy.resolve(path)
        if create_dirs:
            _create_dirs_owned(os.path.dirname(real))
        with open(real, "ab" if append else "wb") as fh:
            fh.write(payload)
        os.chmod(real, mode)
        if not append:
            _chown_to_sandbox(real)
        self.bytes_written += len(payload)
        return {"path": real, "bytes_written": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}

    def m_fs_list(self, params: dict[str, Any]) -> dict[str, Any]:
        path = str(params.get("path") or "/workspace")
        recursive = bool(params.get("recursive"))
        include_hidden = params.get("include_hidden", True)
        max_entries = int(params.get("max_entries") or 500)
        root = policy.resolve_dir(path)
        entries: list[dict[str, Any]] = []
        truncated = False

        def add(full: str) -> None:
            nonlocal truncated
            if len(entries) >= max_entries:
                truncated = True
                return
            name = os.path.basename(full)
            if not include_hidden and name.startswith("."):
                return
            info = os.lstat(full)
            if stat.S_ISLNK(info.st_mode):
                kind = "symlink"
            elif stat.S_ISDIR(info.st_mode):
                kind = "dir"
            elif stat.S_ISREG(info.st_mode):
                kind = "file"
            else:
                kind = "other"
            entries.append(
                {
                    "path": full,
                    "name": name,
                    "type": kind,
                    "size": info.st_size,
                    "mode": oct(info.st_mode & 0o7777),
                    "mtime": info.st_mtime,
                }
            )

        if recursive:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if include_hidden or not d.startswith("."))
                for name in sorted(dirnames + filenames):
                    add(os.path.join(dirpath, name))
                    if truncated:
                        break
                if truncated:
                    break
        else:
            for name in sorted(os.listdir(root)):
                add(os.path.join(root, name))
                if truncated:
                    break
        return {"path": root, "entries": entries, "truncated": truncated}

    def m_fs_stat(self, params: dict[str, Any]) -> dict[str, Any]:
        path = str(params.get("path") or "")
        with_hash = bool(params.get("with_sha256"))
        real = policy.resolve(path)
        if not os.path.lexists(real):
            return {"path": real, "exists": False}
        info = os.lstat(real)
        if stat.S_ISLNK(info.st_mode):
            kind = "symlink"
        elif stat.S_ISDIR(info.st_mode):
            kind = "dir"
        elif stat.S_ISREG(info.st_mode):
            kind = "file"
        else:
            kind = "other"
        out: dict[str, Any] = {
            "path": real,
            "exists": True,
            "type": kind,
            "size": info.st_size,
            "mode": oct(info.st_mode & 0o7777),
            "mtime": info.st_mtime,
            "uid": info.st_uid,
            "gid": info.st_gid,
        }
        if with_hash and kind == "file":
            digest = hashlib.sha256()
            with open(real, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            out["sha256"] = digest.hexdigest()
        return out

    def m_fs_mkdir(self, params: dict[str, Any]) -> dict[str, Any]:
        real = policy.resolve(str(params.get("path") or ""))
        parents = params.get("parents", True)
        mode = int(str(params.get("mode") or "0755"), 8)
        if parents:
            _create_dirs_owned(real)
        else:
            os.mkdir(real)
        os.chmod(real, mode)
        _chown_to_sandbox(real)
        return {"ok": True, "detail": real}

    def m_fs_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        real = policy.resolve(str(params.get("path") or ""), must_exist=True)
        recursive = bool(params.get("recursive"))
        if os.path.isdir(real) and not os.path.islink(real):
            if not recursive:
                raise HandlerError(-32602, "refusing to remove a directory without recursive=true")
            shutil.rmtree(real)
        else:
            os.remove(real)
        return {"ok": True, "detail": f"removed {real}"}

    def m_fs_move(self, params: dict[str, Any]) -> dict[str, Any]:
        src = policy.resolve(str(params.get("src") or ""), must_exist=True)
        dst = policy.resolve(str(params.get("dst") or ""))
        if os.path.exists(dst) and not params.get("overwrite"):
            raise HandlerError(-32602, f"{dst} already exists; pass overwrite=true")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(src, dst)
        return {"ok": True, "detail": f"{src} -> {dst}"}

    # ---------------------------------------------------------------- exec
    def m_exec_run(self, params: dict[str, Any]) -> dict[str, Any]:
        argv = params.get("argv")
        if isinstance(argv, str):
            argv = [argv]
        if not isinstance(argv, list) or not argv:
            raise HandlerError(-32602, "argv must be a non-empty list of strings")
        argv = [str(item) for item in argv]
        for item in argv:
            if "\x00" in item:
                raise HandlerError(-32602, "argv elements must not contain NUL bytes")
        shell = bool(params.get("shell"))
        if shell:
            import shlex

            argv = ["/bin/bash", "-lc", " ".join(shlex.quote(a) for a in argv)]
        cwd = policy.resolve_dir(str(params.get("cwd") or "/workspace"))
        timeout_s = float(params.get("timeout_s") or 30.0)
        max_output = int(params.get("max_output_bytes") or 1_000_000)
        env_extra = params.get("env") or {}
        if not isinstance(env_extra, dict):
            raise HandlerError(-32602, "env must be an object")
        for key in env_extra:
            if not isinstance(key, str) or not key.replace("_", "").isalnum():
                raise HandlerError(-32602, f"invalid environment variable name {key!r}")
        stdin = params.get("stdin")
        self.commands_run += 1
        try:
            memory_mb = int(params.get("memory_mb") or DEFAULT_MEMORY_MB)
            memory_mb = max(32, min(4096, memory_mb))
            result = limits_mod.run_command(
                argv,
                cwd=cwd,
                env=limits_mod.base_env({str(k): str(v) for k, v in env_extra.items()}),
                timeout_s=timeout_s,
                stdin=None if stdin is None else str(stdin),
                max_output_bytes=max_output,
                memory_bytes=memory_mb * 1024 * 1024,
            )
        except FileNotFoundError as exc:
            return {
                "exit_code": 127,
                "stdout": "",
                "stderr": f"{exc.strerror or 'not found'}: {argv[0]}",
                "duration_ms": 0,
                "timed_out": False,
                "truncated": False,
                "signal": None,
                "oom": False,
                "killed_by_limit": None,
            }
        return {
            "exit_code": result["exit_code"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "duration_ms": result["duration_ms"],
            "timed_out": result["timed_out"],
            "truncated": result["truncated"],
            "signal": None,
            "oom": result["oom"],
            "killed_by_limit": result["killed_by_limit"],
            # what was actually enforced (memory/CPU/pids, and whether the cgroup worked):
            # without this the caller cannot tell a real limit from a silent no-op
            "limits": result.get("limits"),
        }

    # ------------------------------------------------------------- sandbox
    def m_sandbox_info(self, params: dict[str, Any]) -> dict[str, Any]:
        workspace_bytes = 0
        for dirpath, _dirnames, filenames in os.walk(policy.WORKSPACE):
            for name in filenames:
                try:
                    workspace_bytes += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
        return {
            "vm_id": self.vm_id,
            "uptime_s": round(time.time() - STARTED_AT, 3),
            "commands_run": self.commands_run,
            "bytes_written": self.bytes_written,
            "tool_runs": self.tool_runs,
            "workspace_bytes": workspace_bytes,
            "root_readonly": root_is_readonly(),
            "uid": uid(),
            "network_interfaces": network_interfaces(),
            "cgroup_available": os.path.ismount("/sys/fs/cgroup"),
            "cgroup_last_error": limits_mod.LAST_CGROUP.get("error"),
            "cgroup_last_path": limits_mod.LAST_CGROUP.get("path"),
        }

    def m_sandbox_reset(self, params: dict[str, Any]) -> dict[str, Any]:
        keep_tools = params.get("keep_tools", True)
        root = policy.WORKSPACE
        removed = 0
        for name in os.listdir(root):
            if keep_tools and name == ".tools":
                continue
            target = os.path.join(root, name)
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target)
            else:
                os.remove(target)
            removed += 1
        return {"ok": True, "detail": f"removed {removed} entries from {root}"}

    # ------------------------------------------------------------ toolsmith
    def m_py_check(self, params: dict[str, Any]) -> dict[str, Any]:
        source = str(params.get("source") or "")
        permissions = list(params.get("permissions") or [])
        entrypoint = str(params.get("entrypoint") or "run")
        report = checker_mod.check_source(
            source,
            permissions,
            entrypoint,
            profile=str(params.get("profile") or "strict"),
            extra_modules=list(params.get("extra_modules") or []),
            allow_open=bool(params.get("allow_open") or False),
        )
        return {
            "ok": report.ok,
            "violations": [f.as_dict() for f in report.findings],
            "stats": report.stats,
            "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        }

    def m_tool_install(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("tool_name") or "")
        version = int(params.get("version") or 0)
        sha = str(params.get("sha256") or "")
        source = _decode_source(params)
        actual = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if sha and actual != sha:
            raise HandlerError(1009, f"source sha256 mismatch: expected {sha}, got {actual}")
        path = _install_source(name, version, source, params.get("entrypoint") or "run", params.get("permissions") or [])
        return {"ok": True, "installed_path": path, "sha256": actual}

    def m_py_run(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("tool_name") or "")
        version = int(params.get("version") or 0)
        source = _decode_source(params)
        sha = str(params.get("sha256") or "")
        actual = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if sha and actual != sha:
            raise HandlerError(1009, f"source sha256 mismatch: expected {sha}, got {actual}")
        entrypoint = str(params.get("entrypoint") or "run")
        permissions = list(params.get("permissions") or [])
        tool_path = _install_source(name, version, source, entrypoint, permissions)
        self.tool_runs += 1
        return _execute_tool(
            tool_path=tool_path,
            entrypoint=entrypoint,
            args=params.get("args") or {},
            permissions=permissions,
            timeout_s=float(params.get("timeout_s") or 30.0),
        )

    def m_tool_test(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("tool_name") or "")
        version = int(params.get("version") or 0)
        source = _decode_source(params)
        sha = str(params.get("sha256") or "")
        actual = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if sha and actual != sha:
            raise HandlerError(1009, f"source sha256 mismatch: expected {sha}, got {actual}")
        entrypoint = str(params.get("entrypoint") or "run")
        permissions = list(params.get("permissions") or [])
        tool_path = _install_source(name, version, source, entrypoint, permissions)
        results: list[dict[str, Any]] = []
        passed = 0
        failed = 0
        for case in params.get("tests") or []:
            case_name = str(case.get("name") or "case")
            timeout_s = float(case.get("timeout_s") or params.get("timeout_s") or 30.0)
            started = time.monotonic()
            try:
                outcome = _execute_tool(
                    tool_path=tool_path,
                    entrypoint=entrypoint,
                    args=case.get("args") or {},
                    permissions=permissions,
                    timeout_s=timeout_s,
                )
                ok, message = _evaluate(case.get("expect") or {}, outcome)
            except Exception as exc:  # noqa: BLE001
                outcome = {"result": None, "error": str(exc), "error_type": type(exc).__name__, "stdout": "", "stderr": ""}
                ok, message = False, f"runner failure: {type(exc).__name__}: {exc}"
            entry = {
                "name": case_name,
                "ok": ok,
                "message": message,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "actual": outcome.get("result"),
                "stdout": (outcome.get("stdout") or "")[:2000],
                "stderr": (outcome.get("stderr") or "")[:2000],
            }
            results.append(entry)
            if ok:
                passed += 1
            else:
                failed += 1
        return {"passed": passed, "failed": failed, "results": results, "ok": failed == 0 and passed > 0}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _decode_source(params: dict[str, Any]) -> str:
    source = params.get("source")
    if source is None:
        source_b64 = params.get("source_b64")
        if not source_b64:
            raise HandlerError(-32602, "either 'source' or 'source_b64' is required")
        source = base64.b64decode(str(source_b64)).decode("utf-8")
    return str(source)


def _chown_to_sandbox(path: str) -> None:
    """Give a freshly created path to the sandbox user.

    Without this, files written by fs.write (executor = root) cannot be modified by
    the commands exec.run spawns (uid 1000).
    """
    chown = getattr(os, "chown", None)
    if chown is None:  # pragma: no cover - Windows (imported by unit tests only)
        return
    try:
        uid, gid = limits_mod.sandbox_ids()
        chown(path, uid, gid)
    except (OSError, AttributeError):  # pragma: no cover - best effort
        pass


def _create_dirs_owned(path: str) -> None:
    """makedirs, then hand every directory we actually created to the sandbox user."""
    created: list[str] = []
    current = path
    while current and current != policy.WORKSPACE and not os.path.isdir(current):
        created.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    os.makedirs(path, exist_ok=True)
    for directory in created:
        _chown_to_sandbox(directory)


def _install_source(name: str, version: int, source: str, entrypoint: str, permissions: list[str]) -> str:
    # Tool names may be namespaced with dots (fs.read) but must never contain a
    # path separator, so the install directory cannot escape /workspace/.tools.
    if (
        not name
        or len(name) > 41
        or name.startswith(".")
        or ".." in name
        or not all(char.isascii() and (char.islower() or char.isdigit() or char in "._") for char in name)
    ):
        raise HandlerError(-32602, f"invalid tool name {name!r}")
    directory = os.path.join(policy.TOOLS_DIR, f"{name}@{int(version)}")
    os.makedirs(directory, exist_ok=True)
    tool_path = os.path.join(directory, "tool.py")
    with open(tool_path, "w", encoding="utf-8") as fh:
        fh.write(source)
    os.chmod(tool_path, 0o444)
    manifest_path = os.path.join(directory, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "name": name,
                "version": int(version),
                "entrypoint": entrypoint,
                "permissions": list(permissions),
                "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            },
            fh,
            ensure_ascii=False,
            indent=2,
        )
    os.chmod(manifest_path, 0o444)
    return tool_path


def _execute_tool(
    *,
    tool_path: str,
    entrypoint: str,
    args: dict[str, Any],
    permissions: list[str],
    timeout_s: float,
) -> dict[str, Any]:
    run_dir = os.path.join(policy.RUN_TMP, uuid.uuid4().hex[:12])
    os.makedirs(run_dir, exist_ok=True)
    uid, gid = limits_mod.sandbox_ids()
    try:
        os.chown(run_dir, uid, gid)
    except OSError:
        pass
    os.chmod(run_dir, 0o700)
    request_path = os.path.join(run_dir, "request.json")
    result_path = os.path.join(run_dir, "result.json")
    with open(request_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "tool_path": tool_path,
                "entrypoint": entrypoint,
                "args": args,
                "permissions": list(permissions),
            },
            fh,
            ensure_ascii=False,
        )
    os.chmod(request_path, 0o600)
    try:
        os.chown(request_path, uid, gid)
    except OSError:
        pass

    outcome = limits_mod.run_command(
        [PYTHON, RUNNER, request_path, result_path],
        cwd=policy.WORKSPACE,
        env=limits_mod.base_env({"PYTHONPATH": "/usr/lib/agent"}),
        timeout_s=timeout_s,
        stdin=None,
        max_output_bytes=256 * 1024,
    )
    payload: dict[str, Any] = {}
    if os.path.exists(result_path):
        try:
            with open(result_path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            payload = {"ok": False, "error": f"cannot read tool result: {exc}", "error_type": "ResultError"}
    elif outcome["timed_out"]:
        payload = {
            "ok": False,
            "error": f"tool exceeded its {timeout_s:g}s timeout",
            "error_type": "TimeoutExpired",
        }
    else:
        detail = (outcome["stderr"] or "").strip()[:500]
        payload = {
            "ok": False,
            "error": f"runner exited with {outcome['exit_code']} without a result; {detail}",
            "error_type": "RunnerFailure",
        }

    shutil.rmtree(run_dir, ignore_errors=True)
    return {
        "ok": bool(payload.get("ok")),
        "result": payload.get("result"),
        "error": payload.get("error"),
        "error_type": payload.get("error_type"),
        "stdout": outcome["stdout"],
        "stderr": outcome["stderr"],
        "duration_ms": outcome["duration_ms"],
        "timed_out": outcome["timed_out"],
    }


def _evaluate(expect: dict[str, Any], outcome: dict[str, Any]) -> tuple[bool, str]:
    """Evaluate one declarative assertion against a run outcome."""
    if expect.get("raises"):
        wanted = str(expect["raises"])
        if outcome.get("ok"):
            return False, f"expected {wanted} to be raised, but the call succeeded"
        got = str(outcome.get("error_type") or "")
        message = str(outcome.get("error") or "")
        if wanted in (got, "") or wanted in message:
            return True, f"raised {got}"
        return False, f"expected {wanted}, got {got}: {message[:200]}"

    if not outcome.get("ok"):
        return False, f"tool call failed: {outcome.get('error_type')}: {str(outcome.get('error'))[:300]}"

    actual = outcome.get("result")

    if expect.get("equals") is not None:
        if actual == expect["equals"]:
            return True, "equals"
        return False, f"expected {json.dumps(expect['equals'])[:200]}, got {json.dumps(actual, default=str)[:200]}"

    if expect.get("contains"):
        if not isinstance(actual, dict):
            return False, f"contains requires a dict result, got {type(actual).__name__}"
        for key, value in expect["contains"].items():
            if key not in actual:
                return False, f"result is missing key {key!r}"
            if actual[key] != value:
                return False, f"result[{key!r}] = {actual[key]!r}, expected {value!r}"
        return True, "contains"

    if expect.get("is_true"):
        return (True, "truthy") if actual else (False, f"expected a truthy {expect['is_true']}, got {actual!r}")

    return False, "no supported assertion in expect"


def root_is_readonly() -> bool:
    probe = "/.agent-ro-probe"
    try:
        with open(probe, "w") as fh:
            fh.write("x")
        os.remove(probe)
        return False
    except OSError:
        return True


def network_interfaces() -> list[str]:
    ifaces: list[str] = []
    try:
        for name in sorted(os.listdir("/sys/class/net")):
            if name == "lo":
                continue
            ifaces.append(name)
    except OSError:
        pass
    return ifaces
