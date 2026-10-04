"""HTTP gateway from the AI service to the control plane.

This module is the AI service's *only* path to a sandbox.  It speaks plain HTTP
to the control plane, so nothing here can spawn a process, open a host file or
create a VM by itself -- which is exactly the property the isolation test in
``tests/unit/test_isolation_guard.py`` asserts.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from agent.config import settings
from agent.models.protocol import RpcError
from agent.models.tool import (
    PoolStatus,
    SandboxHandle,
    SandboxInvokeParams,
    SandboxInvokeResult,
    ToolPayload,
)

log = logging.getLogger(__name__)


class ControlPlaneUnavailable(RpcError):
    def __init__(self, message: str) -> None:
        super().__init__(1007, message)


class SandboxGateway:
    """Thin async client for the control plane's JSON-RPC endpoint."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        secret: str | None = None,
        timeout_s: float = 600.0,
    ) -> None:
        self.base_url = (base_url or settings.control_url).rstrip("/")
        self.secret = settings.control_secret if secret is None else secret
        self.timeout_s = timeout_s

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["X-Agent-Token"] = self.secret
        return headers

    async def rpc(self, method: str, params: dict[str, Any] | None = None, timeout_s: float | None = None) -> Any:
        url = f"{self.base_url}/rpc"
        try:
            async with httpx.AsyncClient(timeout=timeout_s or self.timeout_s) as client:
                response = await client.post(
                    url, json={"method": method, "params": params or {}}, headers=self._headers()
                )
        except httpx.HTTPError as exc:
            raise ControlPlaneUnavailable(
                f"control plane at {self.base_url} is unreachable: {exc}. "
                "Start it with: python -m agent.cli serve control"
            ) from exc
        if response.status_code == 401:
            raise RpcError(1000, "control plane rejected the shared secret (AGENT_CONTROL_SECRET)")
        try:
            body = response.json()
        except ValueError as exc:
            raise RpcError(1005, f"control plane returned non-JSON (HTTP {response.status_code})") from exc
        if isinstance(body, dict) and body.get("ok"):
            return body.get("result")
        error = (body or {}).get("error") or {}
        raise RpcError(
            int(error.get("code", 1005)),
            str(error.get("message") or f"control plane error (HTTP {response.status_code})"),
            error.get("data"),
        )

    # ------------------------------------------------------------- operations
    async def health(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self.base_url}/health")
            return response.json() if response.status_code == 200 else {"ok": False, "status": response.status_code}
        except httpx.HTTPError as exc:
            return {"ok": False, "error": str(exc)}

    async def acquire(self, session_id: str) -> SandboxHandle:
        payload = await self.rpc("sandbox.acquire", {"session_id": session_id})
        return SandboxHandle.model_validate(payload)

    async def release(self, session_id: str) -> bool:
        payload = await self.rpc("sandbox.release", {"session_id": session_id})
        return bool(payload.get("released")) if isinstance(payload, dict) else False

    async def status(self) -> PoolStatus:
        return PoolStatus.model_validate(await self.rpc("sandbox.status"))

    async def invoke_native(
        self,
        session_id: str,
        method: str,
        params: dict[str, Any],
        timeout_s: float | None = None,
    ) -> SandboxInvokeResult:
        request = SandboxInvokeParams(
            session_id=session_id, kind="native", method=method, params=params, timeout_s=timeout_s
        )
        payload = await self.rpc("sandbox.invoke", request.model_dump(), timeout_s=(timeout_s or 300.0) + 30.0)
        return SandboxInvokeResult.model_validate(payload)

    async def invoke_tool(
        self,
        session_id: str,
        tool: ToolPayload,
        arguments: dict[str, Any],
        timeout_s: float | None = None,
    ) -> SandboxInvokeResult:
        request = SandboxInvokeParams(
            session_id=session_id, kind="python", tool=tool, params=arguments, timeout_s=timeout_s
        )
        payload = await self.rpc("sandbox.invoke", request.model_dump(), timeout_s=(timeout_s or tool.timeout_s) + 60.0)
        return SandboxInvokeResult.model_validate(payload)

    async def metrics(self, session_id: str) -> dict[str, Any]:
        payload = await self.rpc("sandbox.metrics", {"session_id": session_id}, timeout_s=60.0)
        return payload if isinstance(payload, dict) else {}

    async def reset(self, session_id: str) -> dict[str, Any]:
        payload = await self.rpc("sandbox.reset", {"session_id": session_id}, timeout_s=60.0)
        return payload if isinstance(payload, dict) else {}
