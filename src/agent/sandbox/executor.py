#!/usr/bin/env python3
"""Sandbox JSON-RPC executor (standard library only).

Runs inside the QEMU VM as root and talks to the control plane over a
virtio-serial port (``/dev/vport0p1`` for a port named ``agent.rpc``).  The link
carries newline delimited JSON; there is no network interface in the guest at
all, so this is the only channel.

Authentication: the per-VM token is injected by QEMU as ``opt/agent/token``
fw_cfg and handed to this process by :mod:`init` via the environment.  A peer
must complete ``sys.hello`` (constant-time token compare), and the reply carries
an HMAC proof so the control plane can authenticate the guest in return.
"""

from __future__ import annotations

import errno
import json
import os
import select
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

try:  # repository layout
    from agent.sandbox import handlers as handlers_mod
except ImportError:  # pragma: no cover - guest layout
    import handlers as handlers_mod  # type: ignore[no-redef]

MAX_FRAME_BYTES = 8 * 1024 * 1024
DEFAULT_VPORT = "/dev/vport0p1"
IDLE_EXIT_S = float(os.environ.get("AGENT_IDLE_EXIT_S", "900"))
LOG_PREFIX = "[executor]"


def log(message: str) -> None:
    print(f"{LOG_PREFIX} {message}", flush=True)


def find_port(explicit: str | None = None) -> str:
    """Locate the virtio-serial port device, waiting for it to appear."""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    candidates.append(DEFAULT_VPORT)
    named = "/dev/virtio-ports/agent.rpc"
    if os.path.exists(named):
        candidates.insert(0, named)
    deadline = time.time() + 60.0
    while time.time() < deadline:
        for path in candidates:
            if os.path.exists(path):
                return path
        for index in range(4):
            guess = f"/dev/vport0p{index}"
            if os.path.exists(guess):
                return guess
        time.sleep(0.2)
    raise SystemExit(f"{LOG_PREFIX} no virtio-serial port found (looked for {', '.join(candidates)})")


class Executor:
    def __init__(self, port: str, token: str, vm_id: str) -> None:
        self.port = port
        self.methods = handlers_mod.Methods(token=token, vm_id=vm_id)
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rpc")
        self.write_lock = threading.Lock()
        self.buffer = bytearray()
        self.running = True
        self.fd: int | None = None
        self.peer = f"{port}#0"

    # ------------------------------------------------------------ transport
    def _write(self, obj: dict[str, Any]) -> None:
        if self.fd is None:
            return
        payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8") + b"\n"
        if len(payload) > MAX_FRAME_BYTES:
            payload = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": obj.get("id"),
                    "error": {"code": 1004, "message": f"response of {len(payload)} bytes exceeds the frame limit"},
                },
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
        with self.write_lock:
            view = memoryview(payload)
            while view:
                try:
                    written = os.write(self.fd, view)
                except OSError as exc:
                    if exc.errno in (errno.EPIPE, errno.EIO):
                        log("peer closed the port while writing")
                        self.running = False
                        return
                    raise
                view = view[written:]

    def _frames(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while True:
            index = self.buffer.find(b"\n")
            if index < 0:
                break
            raw = bytes(self.buffer[:index])
            del self.buffer[: index + 1]
            if not raw.strip():
                continue
            if len(raw) > MAX_FRAME_BYTES:
                out.append({"__framing_error__": f"frame of {len(raw)} bytes exceeds the limit"})
                continue
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                out.append({"__framing_error__": f"invalid JSON frame: {exc}"})
                continue
            if not isinstance(obj, dict):
                out.append({"__framing_error__": "frame must be a JSON object"})
                continue
            out.append(obj)
        return out

    # -------------------------------------------------------------- handling
    def _dispatch(self, frame: dict[str, Any]) -> None:
        request_id = frame.get("id")
        if "__framing_error__" in frame:
            self._write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32700, "message": frame["__framing_error__"]}})
            return
        method = frame.get("method")
        params = frame.get("params") or {}
        if not isinstance(method, str):
            self._write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": "method is required"}})
            return
        if not isinstance(params, dict):
            self._write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "params must be an object"}})
            return
        try:
            result = self.methods.dispatch(method, params, self.peer)
        except handlers_mod.HandlerError as exc:
            self._write({"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.code, "message": exc.message, "data": exc.data}})
            return
        except BaseException as exc:  # noqa: BLE001 - never kill the executor on a bad call
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"},
                }
            )
            return
        if method == "sys.hello":
            self.methods.authenticate(self.peer)
        if request_id is not None:
            self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _submit(self, frame: dict[str, Any]) -> None:
        # sys.ping is answered inline so a busy sandbox still looks alive.
        if frame.get("method") == "sys.ping":
            self._dispatch(frame)
            return
        self.pool.submit(self._dispatch, frame)

    # ------------------------------------------------------------------ main
    def serve(self) -> int:
        self.fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY)
        log(f"listening on {self.port} as vm {self.methods.vm_id}")
        authenticated_before = False
        while self.running:
            try:
                ready, _, _ = select.select([self.fd], [], [], IDLE_EXIT_S)
            except InterruptedError:
                continue
            if not ready:
                if self.methods.authed_peers == set() and not authenticated_before:
                    # nobody ever connected: the control plane is gone, so release the VM
                    log(f"no client within {IDLE_EXIT_S:g}s; exiting")
                    return 0
                # A warm VM may sit idle for as long as the control plane likes: it owns
                # the lifecycle. Exiting here used to restart the executor (empty auth
                # state) behind the control plane's back, breaking every later call.
                continue
            try:
                chunk = os.read(self.fd, 65536)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EINTR):
                    continue
                log(f"read failed: {exc}")
                return 1
            if not chunk:
                log("peer closed the port; exiting")
                return 0
            self.buffer.extend(chunk)
            if len(self.buffer) > MAX_FRAME_BYTES and b"\n" not in self.buffer:
                log("dropping oversized unterminated buffer")
                self.buffer.clear()
                continue
            for frame in self._frames():
                self._submit(frame)
            authenticated_before = bool(self.methods.authed_peers)
        return 0


def main() -> int:
    token = os.environ.get("AGENT_RPC_TOKEN") or ""
    vm_id = os.environ.get("AGENT_VM_ID") or "unknown"
    port = find_port(os.environ.get("AGENT_VPORT") or None)
    executor = Executor(port, token, vm_id)

    def _stop(_signum: int, _frame: Any) -> None:
        log("received termination signal")
        executor.running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        return executor.serve()
    finally:
        executor.pool.shutdown(wait=False)
        if executor.fd is not None:
            try:
                os.close(executor.fd)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
