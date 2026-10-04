"""Control plane HTTP API (FastAPI).

Only this process spawns QEMU.  The AI service (running inside the Debian
platform VM) talks to it through :mod:`agent.ai.sandbox_gateway` and has no other
way to reach a sandbox.

Endpoints
---------
``GET  /health``            liveness plus sandbox image problems
``GET  /sandbox/status``    pool / VM inventory
``POST /rpc``               ``{"method": ..., "params": {...}}`` -> ``{"ok": ..., "result": ...}``
``POST /sessions/{id}/release``
"""

from __future__ import annotations

import contextlib
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from agent.config import settings
from agent.control.host.base import detect_accel
from agent.control.manager import SandboxManager
from agent.logging_setup import setup_logging
from agent.models.protocol import PROTOCOL_VERSION, ErrorCode
from agent.models.tool import SandboxInvokeParams

log = logging.getLogger(__name__)


def _authorise(token: str | None) -> None:
    if not settings.control_secret:
        return
    if not token or not secrets.compare_digest(token, settings.control_secret):
        raise HTTPException(status_code=401, detail="invalid or missing X-Agent-Token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings.ensure_dirs()
    manager = SandboxManager(settings)
    app.state.manager = manager
    if not settings.control_secret:
        log.warning("AGENT_CONTROL_SECRET is empty: the control plane accepts unauthenticated RPCs")
    await manager.start()
    try:
        yield
    finally:
        await manager.shutdown()


def _ok(result: Any) -> JSONResponse:
    return JSONResponse({"ok": True, "result": result})


def _error(code: ErrorCode | int, message: str, data: dict[str, Any] | None = None) -> JSONResponse:
    payload: dict[str, Any] = {"ok": False, "error": {"code": int(code), "message": message}}
    if data:
        payload["error"]["data"] = data
    status = 200 if int(code) >= 1000 else 400
    return JSONResponse(payload, status_code=status)


def create_app() -> FastAPI:
    app = FastAPI(title="agentbox control plane", version="0.1.0", lifespan=lifespan)

    def mgr(request: Request) -> SandboxManager:
        return request.app.state.manager

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "role": "control-plane",
            "protocol": PROTOCOL_VERSION,
            "accel": detect_accel(settings.sandbox_accel),
            "image_problems": settings.sandbox_image_problems(),
            "sandbox_dir": str(settings.sandbox_root),
        }

    @app.get("/sandbox/status")
    async def status(request: Request, x_agent_token: str | None = Header(default=None)) -> Any:
        _authorise(x_agent_token)
        return await mgr(request).status()

    @app.post("/sessions/{session_id}/release")
    async def release_session(
        session_id: str, request: Request, x_agent_token: str | None = Header(default=None)
    ) -> Any:
        _authorise(x_agent_token)
        return {"released": await mgr(request).release(session_id)}

    @app.post("/rpc")
    async def rpc(request: Request, x_agent_token: str | None = Header(default=None)) -> JSONResponse:
        _authorise(x_agent_token)
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            return _error(ErrorCode.PARSE_ERROR, f"invalid JSON body: {exc}")
        if not isinstance(body, dict) or not isinstance(body.get("method"), str):
            return _error(ErrorCode.INVALID_REQUEST, "body must be {'method': str, 'params': object}")
        method = str(body["method"])
        params = body.get("params") or {}
        if not isinstance(params, dict):
            return _error(ErrorCode.INVALID_PARAMS, "params must be an object")
        session_id = str(params.get("session_id") or "default")

        try:
            if method == "sandbox.acquire":
                return _ok((await mgr(request).acquire(session_id)).model_dump())
            if method == "sandbox.release":
                return _ok({"released": await mgr(request).release(session_id)})
            if method == "sandbox.invoke":
                result = await mgr(request).invoke(SandboxInvokeParams.model_validate(params))
                return _ok(result.model_dump())
            if method == "host.exec":
                from agent.control.host import exec as host_exec

                try:
                    return _ok(await host_exec.run(settings, **params))
                except host_exec.HostExecError as exc:
                    return _error(ErrorCode.PERMISSION_DENIED, str(exc))
            if method == "host.info":
                from agent.control.host import exec as host_exec

                return _ok(
                    {
                        "tier": settings.permission_tier,
                        "workdir": str(host_exec.workdir(settings)),
                        "timeout_s": settings.host_exec_timeout_s,
                        "armed": settings.permission_tier == "unrestricted",
                    }
                )
            if method == "sandbox.status":
                return _ok((await mgr(request).status()).model_dump())
            if method == "sandbox.reset":
                vm = await mgr(request).claim(session_id)
                await vm.reset_workspace()
                return _ok({"reset": True, "vm_id": vm.record.vm_id})
            if method == "sandbox.metrics":
                vm = await mgr(request).claim(session_id)
                return _ok(await vm.metrics())
            if method == "sandbox.image_check":
                return _ok({"problems": settings.sandbox_image_problems(), "ready": settings.image_ready()})
        except ValueError as exc:
            return _error(ErrorCode.INVALID_PARAMS, str(exc))
        except Exception as exc:  # noqa: BLE001 - the caller needs a JSON error, not a 500 page
            log.exception("control plane rpc failed: %s", method)
            return _error(ErrorCode.SANDBOX_ERROR, f"{type(exc).__name__}: {exc}")
        return _error(ErrorCode.METHOD_NOT_FOUND, f"unknown method {method!r}")

    return app


app = create_app()


def main() -> None:  # pragma: no cover - CLI entry point
    import uvicorn

    with contextlib.suppress(KeyboardInterrupt):
        uvicorn.run(
            "agent.control.app:app",
            host=settings.control_host,
            port=settings.control_port,
            log_level=settings.log_level.lower(),
        )


if __name__ == "__main__":  # pragma: no cover
    main()
