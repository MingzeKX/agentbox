"""Client side of the virtio-serial RPC link (control plane -> guest executor)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import secrets
from typing import Any

from agent.models.protocol import (
    PROTOCOL_VERSION,
    ErrorCode,
    FrameDecoder,
    RpcError,
    encode_frame,
    request,
)
from agent.models.tool import SysHelloResult

log = logging.getLogger(__name__)


class SandboxClient:
    """One authenticated connection to one guest executor."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        token: str,
        vm_id: str,
        default_timeout: float = 60.0,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.token = token
        self.vm_id = vm_id
        self.default_timeout = default_timeout
        self.decoder = FrameDecoder()
        self.pending: dict[str, asyncio.Future[Any]] = {}
        self.write_lock = asyncio.Lock()
        self.reader_task: asyncio.Task[None] | None = None
        self.closed = False
        self.calls = 0
        self._counter = 0

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self.reader_task = asyncio.create_task(self._read_loop(), name=f"sandbox-read-{self.vm_id}")

    async def hello(self, timeout: float = 30.0) -> SysHelloResult:
        nonce = secrets.token_hex(16)
        raw = await self.call(
            "sys.hello",
            {"token": self.token, "nonce": nonce, "client": "control-plane", "protocol": PROTOCOL_VERSION},
            timeout=timeout,
        )
        result = SysHelloResult.model_validate(raw)
        expected = hmac.new(
            self.token.encode(), f"{nonce}:{result.vm_id}".encode(), hashlib.sha256
        ).hexdigest()
        if self.token and not hmac.compare_digest(expected, str(raw.get("proof", ""))):
            raise RpcError(ErrorCode.UNAUTHORIZED, "guest failed the mutual authentication proof")
        if result.protocol != PROTOCOL_VERSION:
            raise RpcError(
                ErrorCode.UNAVAILABLE,
                f"guest speaks protocol {result.protocol}, control plane speaks {PROTOCOL_VERSION}",
            )
        return result

    async def close(self) -> None:
        self.closed = True
        if self.reader_task is not None:
            self.reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.reader_task
        with contextlib.suppress(Exception):
            self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()

    # ----------------------------------------------------------------- calls
    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        if self.closed:
            raise RpcError(ErrorCode.UNAVAILABLE, f"sandbox {self.vm_id} is closed")
        self._counter += 1
        req_id = f"c{self._counter}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self.pending[req_id] = future
        payload = encode_frame(request(req_id, method, params or {}))
        try:
            async with self.write_lock:
                self.writer.write(payload)
                await self.writer.drain()
        except Exception as exc:
            self.pending.pop(req_id, None)
            raise RpcError(ErrorCode.UNAVAILABLE, f"cannot write to sandbox {self.vm_id}: {exc}") from exc

        self.calls += 1
        effective = self.default_timeout if timeout is None else timeout
        try:
            return await asyncio.wait_for(future, timeout=effective)
        except TimeoutError as exc:
            self.pending.pop(req_id, None)
            raise RpcError(
                ErrorCode.TIMEOUT,
                f"{method} did not answer within {effective:g}s inside sandbox {self.vm_id}",
                {"method": method},
            ) from exc

    async def ping(self, timeout: float = 10.0) -> bool:
        try:
            result = await self.call("sys.ping", {}, timeout=timeout)
        except RpcError:
            return False
        return bool(isinstance(result, dict) and result.get("pong"))

    async def shutdown(self) -> None:
        """Ask the guest to power off cleanly (best effort)."""
        with contextlib.suppress(RpcError):
            await self.call("sys.shutdown", {}, timeout=5.0)

    # ------------------------------------------------------------ read loop
    async def _read_loop(self) -> None:
        try:
            while not self.closed:
                chunk = await self.reader.read(65536)
                if not chunk:
                    raise RpcError(ErrorCode.UNAVAILABLE, f"sandbox {self.vm_id} closed the RPC link")
                for frame in self.decoder.feed(chunk):
                    self._resolve(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - propagate to every waiter
            self._fail_all(exc if isinstance(exc, RpcError) else RpcError(ErrorCode.UNAVAILABLE, str(exc)))

    def _resolve(self, frame: dict[str, Any]) -> None:
        req_id = str(frame.get("id"))
        future = self.pending.pop(req_id, None)
        if future is None or future.done():
            if "method" in frame:
                log.debug("ignoring guest notification %s", frame.get("method"))
            return
        if "error" in frame and frame["error"]:
            future.set_exception(RpcError.from_dict(frame["error"]))
        else:
            future.set_result(frame.get("result"))

    def _fail_all(self, error: RpcError) -> None:
        self.closed = True
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(error)
        self.pending.clear()
