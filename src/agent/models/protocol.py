"""JSON-RPC 2.0 envelope, error codes and NDJSON framing.

Used for two links:

* control plane  ->  guest executor   (virtio-serial, TCP loopback on Windows)
* AI service     ->  control plane    (HTTP POST /rpc, one JSON object per request)

Framing is newline delimited JSON.  Binary payloads travel base64 encoded inside
the JSON object (``data_b64`` fields), so no separate binary frame type is needed.
"""

from __future__ import annotations

import json
from enum import IntEnum
from typing import Any

MAX_FRAME_BYTES = 8 * 1024 * 1024
JSONRPC_VERSION = "2.0"
PROTOCOL_VERSION = 1


class ErrorCode(IntEnum):
    # JSON-RPC reserved
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    # application specific
    UNAUTHORIZED = 1000
    PERMISSION_DENIED = 1001
    TIMEOUT = 1002
    NOT_FOUND = 1003
    LIMIT_EXCEEDED = 1004
    SANDBOX_ERROR = 1005
    TOOL_REJECTED = 1006
    UNAVAILABLE = 1007
    PATH_DENIED = 1008
    BAD_TOOL_SOURCE = 1009

    @property
    def label(self) -> str:
        return self.name.lower()


class RpcError(Exception):
    """Raised by both sides when a call cannot be satisfied."""

    def __init__(self, code: ErrorCode | int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = int(code)
        self.message = message
        self.data = data or {}

    @property
    def label(self) -> str:
        try:
            return ErrorCode(self.code).label
        except ValueError:
            return f"code_{self.code}"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data:
            out["data"] = self.data
        return out

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RpcError:
        return cls(
            int(payload.get("code", ErrorCode.INTERNAL_ERROR)),
            str(payload.get("message", "unknown error")),
            payload.get("data") or {},
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RpcError({self.code}, {self.message!r})"


def request(req_id: str | int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "method": method, "params": params or {}}


def notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "method": method, "params": params or {}}


def success(req_id: str | int, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "result": result}


def failure(req_id: str | int | None, error: RpcError) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "error": error.to_dict()}


def encode_frame(obj: dict[str, Any]) -> bytes:
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise RpcError(ErrorCode.LIMIT_EXCEEDED, f"frame of {len(payload)} bytes exceeds the {MAX_FRAME_BYTES} byte limit")
    return payload + b"\n"


class FrameDecoder:
    """Incremental NDJSON decoder.

    ``feed`` accepts an arbitrary byte chunk and returns every complete frame it
    can decode.  Oversized or malformed frames raise :class:`RpcError` so the
    caller can decide whether to drop the peer.
    """

    def __init__(self, max_frame_bytes: int = MAX_FRAME_BYTES) -> None:
        self._buf = bytearray()
        self._max = max_frame_bytes

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        self._buf.extend(chunk)
        if len(self._buf) > self._max and b"\n" not in self._buf:
            self._buf.clear()
            raise RpcError(ErrorCode.LIMIT_EXCEEDED, f"unterminated frame exceeds {self._max} bytes")
        frames: list[dict[str, Any]] = []
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            raw = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            if not raw.strip():
                continue
            if len(raw) > self._max:
                raise RpcError(ErrorCode.LIMIT_EXCEEDED, f"frame of {len(raw)} bytes exceeds {self._max}")
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RpcError(ErrorCode.PARSE_ERROR, f"invalid JSON frame: {exc}") from exc
            if not isinstance(obj, dict):
                raise RpcError(ErrorCode.INVALID_REQUEST, "frame must be a JSON object")
            frames.append(obj)
        return frames

    @property
    def pending_bytes(self) -> int:
        return len(self._buf)

    def reset(self) -> None:
        self._buf.clear()
