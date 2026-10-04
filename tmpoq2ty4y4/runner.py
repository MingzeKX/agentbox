#!/usr/bin/env python3
"""Tool runner (standard library only).

Executed inside the VM as the unprivileged ``sandbox`` user by the RPC executor:

    runner.py <request.json> <result.json>

The request holds the tool path, entrypoint, arguments and the permissions the
tool declared.  The runner injects the ``fs`` and ``sh`` helper namespaces into
the tool module, calls the entrypoint and writes a JSON result.  Every helper
re-checks the permission it needs, so a tool cannot exceed its manifest even if
the static checker were bypassed.
"""

from __future__ import annotations

import glob as _glob
import importlib.util
import json
import os
import sys
import time
from typing import Any

try:  # repository layout
    from agent.sandbox import limits as limits_mod
    from agent.sandbox import policy
except ImportError:  # pragma: no cover - guest layout
    import limits as limits_mod  # type: ignore[no-redef]
    import policy  # type: ignore[no-redef]

MAX_RESULT_BYTES = 1_048_576


class ToolError(RuntimeError):
    pass


class FsApi:
    """File helpers restricted to /workspace and to the declared permissions."""

    def __init__(self, permissions: set[str]) -> None:
        self.permissions = permissions

    # ------------------------------------------------------------- read side
    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        policy.require(self.permissions, "fs.read")
        with open(policy.resolve(path, must_exist=True), encoding=encoding) as fh:
            return fh.read()

    def read_bytes(self, path: str) -> bytes:
        policy.require(self.permissions, "fs.read")
        with open(policy.resolve(path, must_exist=True), "rb") as fh:
            return fh.read()

    def read_json(self, path: str) -> Any:
        return json.loads(self.read_text(path))

    def list_dir(self, path: str = "/workspace") -> list[str]:
        policy.require(self.permissions, "fs.read")
        target = policy.resolve_dir(path)
        return sorted(os.path.join(target, name) for name in os.listdir(target))

    def glob(self, pattern: str) -> list[str]:
        policy.require(self.permissions, "fs.read")
        base = policy.resolve("/workspace")
        if not pattern.startswith("/"):
            pattern = os.path.join(base, pattern)
        return sorted(p for p in _glob.glob(pattern, recursive=True) if policy.is_within(os.path.realpath(p)))

    def exists(self, path: str) -> bool:
        policy.require(self.permissions, "fs.read")
        try:
            return os.path.exists(policy.resolve(path))
        except policy.PathDenied:
            return False

    def stat(self, path: str) -> dict[str, Any]:
        policy.require(self.permissions, "fs.read")
        real = policy.resolve(path)
        info = os.stat(real)
        return {
            "path": real,
            "size": info.st_size,
            "mode": oct(info.st_mode & 0o7777),
            "mtime": info.st_mtime,
            "is_dir": os.path.isdir(real),
        }

    @property
    def workspace(self) -> str:
        return policy.WORKSPACE

    # ------------------------------------------------------------ write side
    def write_text(self, path: str, text: str, append: bool = False, encoding: str = "utf-8") -> dict[str, Any]:
        policy.require(self.permissions, "fs.write")
        return self.write_bytes(path, text.encode(encoding), append=append)

    def write_bytes(self, path: str, data: bytes, append: bool = False) -> dict[str, Any]:
        policy.require(self.permissions, "fs.write")
        real = policy.resolve(path)
        os.makedirs(os.path.dirname(real), exist_ok=True)
        with open(real, "ab" if append else "wb") as fh:
            fh.write(data)
        return {"path": real, "bytes": len(data)}

    def write_json(self, path: str, payload: Any) -> dict[str, Any]:
        return self.write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))

    def append_text(self, path: str, text: str) -> dict[str, Any]:
        return self.write_text(path, text, append=True)

    def mkdir(self, path: str) -> dict[str, Any]:
        policy.require(self.permissions, "fs.write")
        real = policy.resolve(path)
        os.makedirs(real, exist_ok=True)
        return {"path": real}

    def remove(self, path: str) -> dict[str, Any]:
        policy.require(self.permissions, "fs.write")
        real = policy.resolve(path, must_exist=True)
        if os.path.isdir(real) and not os.path.islink(real):
            import shutil

            shutil.rmtree(real)
        else:
            os.remove(real)
        return {"path": real, "removed": True}

    def move(self, src: str, dst: str) -> dict[str, Any]:
        policy.require(self.permissions, "fs.write")
        source = policy.resolve(src, must_exist=True)
        target = policy.resolve(dst)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.replace(source, target)
        return {"src": source, "dst": target}


class ShApi:
    """Command helpers, bounded by the same limits as a direct exec.run."""

    def __init__(self, permissions: set[str]) -> None:
        self.permissions = permissions

    def run(
        self,
        argv: list[str] | str,
        *,
        shell: bool = False,
        cwd: str = "/workspace",
        timeout: float = 30.0,
        stdin: str | None = None,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        policy.require(self.permissions, "exec")
        if shell:
            policy.require(self.permissions, "exec.shell")
        if isinstance(argv, str):
            argv = [argv]
        if not argv:
            raise ToolError("argv must not be empty")
        if shell:
            import shlex

            argv = ["/bin/bash", "-lc", " ".join(shlex.quote(str(a)) for a in argv)]
        target_cwd = policy.resolve_dir(cwd)
        result = limits_mod.run_command(
            list(argv),
            cwd=target_cwd,
            env=limits_mod.base_env(env),
            timeout_s=float(timeout),
            stdin=stdin,
            max_output_bytes=256 * 1024,
        )
        return {
            "exit_code": result["exit_code"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "timed_out": result["timed_out"],
        }

    def capture(
        self,
        argv: list[str] | str,
        *,
        shell: bool = False,
        cwd: str = "/workspace",
        timeout: float = 30.0,
        env: dict[str, str] | None = None,
    ) -> str:
        out = self.run(argv, shell=shell, cwd=cwd, timeout=timeout, env=env)
        return str(out["stdout"])

    def which(self, name: str) -> str | None:
        policy.require(self.permissions, "exec")
        from shutil import which as _which

        return _which(name)

    def env(self, name: str, default: str | None = None) -> str | None:
        return os.environ.get(name, default)


def _load_tool(path: str, entrypoint: str, permissions: set[str], args: dict[str, Any]):
    if not os.path.isfile(path):
        raise ToolError(f"tool source {path} is missing")
    spec = importlib.util.spec_from_file_location(f"agent_tool_{int(time.time() * 1000)}", path)
    if spec is None or spec.loader is None:
        raise ToolError(f"cannot load tool from {path}")
    module = importlib.util.module_from_spec(spec)
    module.fs = FsApi(permissions)  # type: ignore[attr-defined]
    module.sh = ShApi(permissions)  # type: ignore[attr-defined]
    spec.loader.exec_module(module)
    fn = getattr(module, entrypoint, None)
    if fn is None or not callable(fn):
        raise ToolError(f"tool does not define a callable {entrypoint}()")
    return fn(dict(args))


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: runner.py <request.json> <result.json>", file=sys.stderr)
        return 2
    request_path, result_path = sys.argv[1], sys.argv[2]
    with open(request_path, encoding="utf-8") as fh:
        request = json.load(fh)

    payload: dict[str, Any]
    try:
        permissions = set(request.get("permissions") or [])
        value = _load_tool(
            request["tool_path"],
            request.get("entrypoint", "run"),
            permissions,
            request.get("args") or {},
        )
        try:
            encoded = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as exc:
            raise ToolError(f"result is not JSON serialisable: {exc}") from exc
        if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
            raise ToolError(
                f"result is {len(encoded)} bytes, limit is {MAX_RESULT_BYTES}; write large data to /workspace instead"
            )
        payload = {"ok": True, "result": value}
    except BaseException as exc:  # noqa: BLE001 - report everything back to the caller
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}"[:4000],
            "error_type": type(exc).__name__,
        }
    with open(result_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
