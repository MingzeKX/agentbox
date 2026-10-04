"""AI service HTTP API (FastAPI).

Responsibilities: LLM conversation, the three resident meta tools, the tool
authoring pipeline and session bookkeeping.  It never spawns a process and never
opens a host file: sandbox work is forwarded to the control plane over HTTP.

Endpoints
---------
``GET  /health``                     DB / control plane / embedder / model status
``GET  /llm``                        configured + available models, vision routing, effort
``POST /sessions``                   create a session id
``GET  /sessions/{id}/messages``     stored conversation
``DELETE /sessions/{id}``            release the sandbox and forget the binding
``POST /chat``                       run one turn (SSE stream or JSON)
``GET  /tools?query=...``            hybrid tool search
``GET  /tools/{name}``               full schema of one tool
``GET  /tools/{name}/runs``          recent executions of one tool
``POST /tools/check``                run the authoring gates without registering
``GET  /admin/tools``                tool inventory (operator token)
``POST /admin/tools/retire``         hide a tool/version (operator token)
``POST /admin/tools/delete``         delete a tool/version, hard only with purge+confirm
``GET  /sandbox``                    proxy of the control plane pool status
``POST /asr``                        local speech-to-text (raw audio bytes in, transcript out)
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from agent.ai.agent_loop import AgentLoop
from agent.ai.llm import (
    FALLBACK_MODEL_IDS,
    ImageInputError,
    LLMClient,
    LLMError,
    build_image_parts,
    decode_base64_image,
)
from agent.ai.metacalls import MetaTools
from agent.ai.sandbox_gateway import SandboxGateway
from agent.asr import get_transcriber
from agent.config import settings
from agent.embeddings import get_embedder
from agent.logging_setup import setup_logging
from agent.models.protocol import ErrorCode, RpcError
from agent.models.tool import ToolManifest
from agent.registry import db as registry_db
from agent.registry import repository as repo
from agent.registry import service as registry_service
from agent.toolsmith.gates import ToolsmithContext
from agent.toolsmith.gates import check as toolsmith_check

log = logging.getLogger(__name__)

#: audio container the ASR endpoint accepts (a charset/parameter suffix is stripped)
ASR_CONTENT_TYPES = frozenset(
    {"audio/wav", "audio/x-wav", "audio/mpeg", "audio/ogg", "audio/webm", "application/octet-stream"}
)

#: hard cap on one upload (25 MB ~= 13 minutes of 16 kHz mono PCM)
ASR_MAX_BYTES = 25 * 1024 * 1024


class ImageIn(BaseModel):
    """One inline image: base64 of the raw bytes plus their media type."""

    media_type: str = Field(default="image/png", max_length=64)
    data_base64: str = Field(min_length=1)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)
    session_id: str | None = None
    stream: bool = True
    # how much of each tool result travels back in events; the IDE-style CLI asks for
    # more than the default so it can show the whole stdout
    preview_chars: int | None = Field(default=None, ge=200, le=20_000)
    # vision input: the AI service has no filesystem, so images arrive inline
    images: list[ImageIn] = Field(default_factory=list)


class CheckRequest(BaseModel):
    """Body for ``POST /tools/check``: the manifest the model would submit."""

    manifest: dict[str, Any]


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    app.state.gateway = SandboxGateway()
    app.state.llm = LLMClient()
    app.state.embedder = get_embedder()
    healthy, detail = await registry_db.ping()
    if not healthy:
        log.error("PostgreSQL is not reachable (%s). Run: python -m agent.cli db init", detail)
    health = await app.state.gateway.health()
    if not health.get("ok"):
        log.warning("control plane not reachable yet: %s", health.get("error") or health.get("status"))
    try:
        yield
    finally:
        await registry_db.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="agentbox AI service", version="0.1.0", lifespan=lifespan)

    def sessionmaker():
        return registry_db.get_sessionmaker()

    def gateway() -> SandboxGateway:
        return app.state.gateway

    def build_loop(session_id: str, on_event=None, preview_chars: int | None = None) -> AgentLoop:
        return AgentLoop(
            session_id=session_id,
            llm=app.state.llm,
            meta=MetaTools(
                session_id=session_id,
                gateway=gateway(),
                sessionmaker=sessionmaker(),
                embedder=app.state.embedder,
            ),
            sessionmaker=sessionmaker(),
            on_event=on_event,
            preview_chars=preview_chars or 400,
        )

    # ------------------------------------------------------------------ health
    @app.get("/health")
    async def health() -> dict[str, Any]:
        db_ok, db_detail = await registry_db.ping()
        control = await gateway().health()
        embedder = app.state.embedder
        return {
            "ok": db_ok,
            "role": "ai-service",
            "persona": settings.persona,
            "db": {"ok": db_ok, "detail": db_detail},
            "control_plane": control,
            "llm": {
                "model": app.state.llm.model,
                "base_url": app.state.llm.base_url,
                "key": bool(app.state.llm.api_key),
                # vision routing + effort knob, read from config only: /health must stay
                # network-free, so this never asks the provider for anything
                "vision_model": settings.llm_vision_model,
                "vision_enabled": settings.llm_vision_enabled,
                "effort": settings.llm_effort,
            },
            "embedder": {"name": embedder.name, "available": embedder.available, "dim": embedder.dim},
            "asr": {
                "enabled": settings.asr_enabled,
                "model": settings.asr_model,
                # None until the endpoint (or the first probe) has imported the library;
                # /health must not pay the ctranslate2 import cost itself
                "importable": get_transcriber().import_status,
            },
            "limits": {
                "max_steps": settings.max_steps,
                "exec_default_timeout_s": settings.exec_default_timeout_s,
                "tool_source_max_bytes": settings.tool_source_max_bytes,
            },
        }

    @app.get("/sandbox")
    async def sandbox_status() -> Any:
        try:
            return (await gateway().status()).model_dump()
        except RpcError as exc:
            raise HTTPException(status_code=503, detail=exc.message) from exc

    # --------------------------------------------------------------------- llm
    @app.get("/llm")
    async def llm_info() -> dict[str, Any]:
        """Which model handles what, and which models the provider offers.

        Only ``available_models`` touches the network, and only once every
        :data:`MODELS_CACHE_TTL_S` seconds; a broken provider call degrades to the two
        ids measured on this gateway instead of failing the request.
        """
        return {
            "model": app.state.llm.model,
            "vision_model": settings.llm_vision_model,
            "vision_enabled": settings.llm_vision_enabled,
            "effort": settings.llm_effort,
            "available_models": await _available_models(app.state.llm),
        }

    # ---------------------------------------------------------------- sessions
    @app.post("/sessions")
    async def create_session(title: str = "") -> dict[str, Any]:
        session_id = f"s-{uuid.uuid4().hex[:16]}"
        async with sessionmaker()() as session:
            await repo.ensure_session(session, session_id, title=title)
            await session.commit()
        return {"session_id": session_id, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}

    @app.get("/sessions/{session_id}/messages")
    async def messages(session_id: str, limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
        async with sessionmaker()() as session:
            rows = await repo.load_messages(session, session_id, limit=limit)
        return {
            "session_id": session_id,
            "count": len(rows),
            "messages": [
                {"role": row.role, "content": row.content, "created_at": row.created_at.isoformat()} for row in rows
            ],
        }

    @app.delete("/sessions/{session_id}")
    async def delete_session(session_id: str, purge: bool = False) -> dict[str, Any]:
        released = False
        try:
            released = await gateway().release(session_id)
        except RpcError as exc:
            log.warning("cannot release sandbox for %s: %s", session_id, exc.message)
        async with sessionmaker()() as session:
            await repo.unbind_sandbox(session, session_id)
            cleared = await repo.clear_messages(session, session_id) if purge else 0
            await session.commit()
        return {"session_id": session_id, "sandbox_released": released, "messages_cleared": cleared}

    # -------------------------------------------------------------------- chat
    def _admin_ok(request: Request) -> bool:
        """Admin routes need the shared secret (same token as the control plane)."""
        if not settings.control_secret:
            return False
        offered = request.headers.get("X-Agent-Token") or ""
        return hmac.compare_digest(offered, settings.control_secret)

    @app.get("/admin/config")
    async def get_admin_config(request: Request):
        if not _admin_ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        from agent.ai import admin

        return JSONResponse({"ok": True, "effective": admin.effective(), "mutable": admin.MUTABLE})

    @app.post("/admin/config")
    async def post_admin_config(request: Request):
        if not _admin_ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        from agent.ai import admin

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
        changes = body.get("set") if isinstance(body, dict) else None
        if not isinstance(changes, dict):
            return JSONResponse({"ok": False, "error": 'expected {"set": {...}}'}, status_code=400)
        try:
            effective, applied = admin.apply(changes)
        except admin.ConfigError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "applied": applied, "effective": effective})

    # ------------------------------------------------------------- admin: tools
    # The operator (through the console/CLI, never through the model) owns the whole
    # registry: the agent may only retire the generated tools it wrote itself.
    # All three routes are protected by the same ``X-Agent-Token`` secret as
    # /admin/config; the model never sees it.
    #
    #   GET  /admin/tools          -> {ok, count, tools: [{name, version, status,
    #                                 tier, created_by, executor, runs, failures}]}
    #   POST /admin/tools/retire   {name, version?}          -> {ok, name, retired,
    #                                 version, remaining: [{version, status}]}
    #   POST /admin/tools/delete   {name, version?, purge, confirm} -> same shape with
    #                                 "deleted"/"purged"; a hard purge needs confirm=true
    async def _admin_body(request: Request) -> dict[str, Any] | JSONResponse:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - invalid JSON must be a 400, not a 500
            return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "body must be a JSON object"}, status_code=400)
        return body

    async def _admin_tool_remaining(session: Any, name: str) -> list[dict[str, Any]]:
        return [{"version": version, "status": status} for version, status in await repo.tool_versions(session, name)]

    async def _admin_tool_exists(session: Any, body: dict[str, Any]) -> JSONResponse | None:
        """404 with the versions that do exist, so the CLI can print something useful."""
        name = body["name"]
        known = await repo.tool_versions(session, name)
        if known:
            return None
        return JSONResponse(
            {"ok": False, "error": f"tool {name!r} is not registered", "remaining": []},
            status_code=404,
        )

    def _admin_name(body: dict[str, Any]) -> str | JSONResponse:
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            return JSONResponse({"ok": False, "error": 'expected {"name": "<tool name>"}'}, status_code=400)
        return name.strip()

    @app.get("/admin/tools")
    async def admin_tools(
        request: Request,
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ):
        if not _admin_ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        async with sessionmaker()() as session:
            rows = await repo.list_tools(session, limit=limit, offset=offset)
            return JSONResponse(
                {
                    "ok": True,
                    "count": len(rows),
                    "tools": [
                        {
                            "name": row.name,
                            "version": row.version,
                            "status": row.status,
                            "tier": row.tier,
                            "created_by": row.created_by,
                            "executor": row.executor,
                            "runs": row.runs,
                            "failures": row.failures,
                        }
                        for row in rows
                    ],
                }
            )

    @app.post("/admin/tools/retire")
    async def admin_tools_retire(request: Request):
        if not _admin_ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        body = await _admin_body(request)
        if isinstance(body, JSONResponse):
            return body
        name = _admin_name(body)
        if isinstance(name, JSONResponse):
            return name
        version = body.get("version")
        if version is not None and not isinstance(version, int):
            return JSONResponse({"ok": False, "error": "'version' must be an integer"}, status_code=400)
        async with sessionmaker()() as session:
            missing = await _admin_tool_exists(session, {"name": name})
            if missing is not None:
                return missing
            retired = await repo.retire_tool(session, name, version)
            remaining = await _admin_tool_remaining(session, name)
            await session.commit()
        return JSONResponse(
            {
                "ok": True,
                "name": name,
                "version": version,
                "retired": retired,
                "remaining": remaining,
            }
        )

    @app.post("/admin/tools/delete")
    async def admin_tools_delete(request: Request):
        if not _admin_ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        body = await _admin_body(request)
        if isinstance(body, JSONResponse):
            return body
        name = _admin_name(body)
        if isinstance(name, JSONResponse):
            return name
        version = body.get("version")
        if version is not None and not isinstance(version, int):
            return JSONResponse({"ok": False, "error": "'version' must be an integer"}, status_code=400)
        purge = body.get("purge", False)
        if not isinstance(purge, bool):
            return JSONResponse({"ok": False, "error": "'purge' must be a boolean"}, status_code=400)
        if purge and body.get("confirm") is not True:
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        "a hard purge deletes the tool rows permanently (the run ledger is kept); "
                        'repeat the request with {"confirm": true} to confirm'
                    ),
                },
                status_code=400,
            )
        async with sessionmaker()() as session:
            missing = await _admin_tool_exists(session, {"name": name})
            if missing is not None:
                return missing
            try:
                deleted = await repo.delete_tool(
                    session, name, version, hard=purge, deleted_by="operator", reason=str(body.get("reason") or "")
                )
                remaining = await _admin_tool_remaining(session, name)
                await session.commit()
            except SQLAlchemyError as exc:
                # an unchanged database can refuse the delete (e.g. tool_runs.tool_id was
                # NOT NULL); a bare 500 with an empty body told the operator nothing, so
                # the reason is logged and returned as JSON
                await session.rollback()
                log.exception("hard delete of %s failed", name)
                return JSONResponse(
                    {"ok": False, "error": f"database refused the delete: {type(exc).__name__}: {exc}"},
                    status_code=409,
                )
        return JSONResponse(
            {
                "ok": True,
                "name": name,
                "version": version,
                "purged": purge,
                "deleted": deleted,
                "remaining": remaining,
            }
        )

    # --------------------------------------------------------------------- asr
    @app.post("/asr")
    async def transcribe_audio(request: Request, language: str | None = Query(default=None)):
        """Local speech-to-text: raw audio bytes in, transcript out.

        Protected exactly like ``/admin/config`` (shared ``X-Agent-Token`` secret):
        transcription is CPU bound and must not be reachable by an unauthenticated
        caller.  Reusing ``_admin_ok`` keeps one secret and one place to audit
        instead of a second switch that could drift out of sync with it.
        """
        if not _admin_ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        if not settings.asr_enabled:
            return JSONResponse({"ok": False, "error": "ASR is disabled (AGENT_ASR_ENABLED=false)"}, status_code=503)

        media_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if media_type not in ASR_CONTENT_TYPES:
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        f"unsupported content type {media_type or '(missing)'}; "
                        f"send one of: {', '.join(sorted(ASR_CONTENT_TYPES))}"
                    ),
                },
                status_code=415,
            )

        declared = (request.headers.get("content-length") or "").strip()
        if declared.isdigit() and int(declared) > ASR_MAX_BYTES:
            return _too_large(int(declared))
        audio = await _read_capped(request, ASR_MAX_BYTES)
        if audio is None:
            return _too_large(None)
        if not audio:
            return JSONResponse({"ok": False, "error": "empty audio body"}, status_code=400)

        transcriber = get_transcriber()
        if not transcriber.available:
            return JSONResponse({"ok": False, "error": transcriber.unavailable_reason()}, status_code=503)

        result = await transcriber.transcribe(audio, language=language, filename=request.headers.get("x-filename"))
        if not result.get("ok"):
            return JSONResponse(result, status_code=503)
        return JSONResponse(result)

    @app.get("/personas")
    async def list_personas():
        """Available personas (bundled + operator files) and which one is active."""
        from agent.ai import personas

        return {
            "ok": True,
            "active": settings.persona,
            "loaded": personas.load().name,
            "personas": [
                {"name": name, "source": source} for name, source in sorted(personas.available().items())
            ],
        }

    @app.post("/chat")
    async def chat(request: ChatRequest):
        # Validated before any session/LLM work so a bad image costs nothing.
        try:
            content = _chat_content(request)
        except ImageInputError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status)

        session_id = request.session_id or f"s-{uuid.uuid4().hex[:16]}"
        async with sessionmaker()() as session:
            await repo.ensure_session(session, session_id, title=request.message[:80])
            await session.commit()

        extra = await _runtime_facts(gateway())
        if not request.stream:
            loop = build_loop(session_id)
            try:
                result = await loop.run(content, system_extra=extra)
            except RpcError as exc:
                return JSONResponse(
                    {"ok": False, "session_id": session_id, "error": exc.message, "error_code": exc.label},
                    status_code=503,
                )
            return {
                "ok": True,
                "session_id": session_id,
                "content": result.content,
                "stop_reason": result.stop_reason,
                "steps": [step.as_dict() for step in result.steps],
                "usage": result.usage,
            }

        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        sink = lambda kind, payload: queue.put_nowait({"type": kind, **payload})  # noqa: E731
        loop = build_loop(session_id, on_event=sink, preview_chars=request.preview_chars)

        async def runner() -> None:
            try:
                await loop.run(content, system_extra=extra)
            except RpcError as exc:
                queue.put_nowait({"type": "error", "message": exc.message, "error_code": exc.label})
            except LLMError as exc:
                queue.put_nowait({"type": "error", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001 - surface it on the stream
                log.exception("agent loop crashed")
                queue.put_nowait({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            finally:
                queue.put_nowait({"type": "_end", "session_id": session_id})

        task = asyncio.create_task(runner())

        async def events():
            yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"
            try:
                while True:
                    item = await queue.get()
                    if item is None or item.get("type") == "_end":
                        break
                    yield f"data: {json.dumps(item, ensure_ascii=False, default=str)}\n\n"
            finally:
                if not task.done():
                    task.cancel()
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------------- tools
    @app.get("/tools")
    async def search_tools(
        query: str = Query(default=""),
        k: int = Query(default=10, ge=1, le=50),
        tier: str | None = None,
    ) -> dict[str, Any]:
        async with sessionmaker()() as session:
            if not query:
                rows = await repo.list_tools(session, tier=tier, status="active", limit=k)
                return {
                    "count": len(rows),
                    "tools": [
                        {
                            "name": row.name,
                            "version": row.version,
                            "tier": row.tier,
                            "executor": row.executor,
                            "description": row.description,
                            "status": row.status,
                            "runs": row.runs,
                            "failures": row.failures,
                        }
                        for row in rows
                    ],
                }
            hits = await registry_service.search_tools(session, query=query, k=k, embedder=app.state.embedder)
        return {"count": len(hits), "tools": [hit.model_dump() for hit in hits]}

    @app.get("/tools/inventory")
    async def inventory() -> dict[str, Any]:
        async with sessionmaker()() as session:
            counts = await repo.count_tools(session)
        return counts

    @app.get("/tools/{name}")
    async def tool_schema(name: str, version: int | None = None, include_source: bool = False) -> Any:
        async with sessionmaker()() as session:
            view = await registry_service.get_tool_schema(
                session, name, version, include_source=include_source, include_quarantined=True
            )
        if view is None:
            raise HTTPException(status_code=404, detail=f"tool {name!r} is not registered")
        return view.model_dump()

    @app.get("/tools/{name}/runs")
    async def tool_runs(name: str, limit: int = Query(default=10, ge=1, le=100)) -> dict[str, Any]:
        async with sessionmaker()() as session:
            rows = await repo.recent_runs(session, name, limit=limit)
        return {
            "tool": name,
            "runs": [
                {
                    "id": row.id,
                    "ok": row.ok,
                    "duration_ms": row.duration_ms,
                    "error_code": row.error_code,
                    "error": (row.error or "")[:400] or None,
                    "args": row.args_redacted,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ],
        }

    @app.post("/tools/check")
    async def tools_check(request: CheckRequest) -> Any:
        """Run the authoring gates (static check + sandbox tests) without registering."""
        manifest = request.manifest
        session_id = str(manifest.pop("session_id", "") or f"toolsmith-{uuid.uuid4().hex[:8]}")
        try:
            ToolManifest.model_validate(
                {**{k: v for k, v in manifest.items() if v is not None}, "tests": manifest.get("tests") or []}
            )
        except Exception as exc:  # noqa: BLE001 - report validation problems as data
            return {"ok": False, "stage": "manifest", "violations": [{"rule": "manifest_invalid", "message": str(exc)}]}
        ctx = ToolsmithContext(
            session_id=session_id,
            gateway=gateway(),
            sessionmaker=sessionmaker(),
            embedder=app.state.embedder,
        )
        return await toolsmith_check(ctx, manifest)

    return app


def _chat_content(request: ChatRequest) -> str | list[dict[str, Any]]:
    """The user message content for one turn: plain text, or text + image parts.

    The parts list is handed to :meth:`AgentLoop.run` as its ``user_message``: the loop
    stores it verbatim as the ``user`` message content (already a JSON column) and
    ``llm.py`` understands content parts, so the text-only path is byte-for-byte what it
    was before.  No image -> the plain string.
    """
    if not request.images:
        return request.message
    images = [(decode_base64_image(image.data_base64), image.media_type) for image in request.images]
    # count / size / media-type caps live in the pure helper so there is exactly one
    # place that decides what an acceptable image is
    return [{"type": "text", "text": request.message}, *build_image_parts(images)]


def _too_large(size: int | None) -> JSONResponse:
    """413 body shared by the declared-length and the streamed-length checks."""
    detail = f"audio body is {size} bytes" if size is not None else "audio body is too large"
    return JSONResponse(
        {"ok": False, "error": f"{detail}; the limit is {ASR_MAX_BYTES} bytes"}, status_code=413
    )


async def _read_capped(request: Request, limit: int) -> bytes | None:
    """Read the body, giving up (``None``) as soon as it grows past ``limit``.

    Streamed instead of ``await request.body()`` so a huge upload is refused while
    it arrives instead of after it has been buffered in memory.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


_runtime_cache: tuple[float, str] = (0.0, "")

#: How long ``GET /llm`` reuses a ``/models`` answer.  The list only changes when the
#: operator reconfigures the gateway, and the CLI may poll /llm on every prompt.
MODELS_CACHE_TTL_S = 300.0

_models_cache: tuple[float, list[str]] = (0.0, [])


async def _available_models(llm: LLMClient) -> list[str]:
    """Provider model ids, cached for ~5 minutes.

    Degrades gracefully: any failure of the provider call (unreachable, HTTP error,
    unexpected body) returns :data:`FALLBACK_MODEL_IDS` -- the two ids measured on this
    gateway -- and is *not* cached, so the next call tries again.
    """
    global _models_cache
    now = time.time()
    if _models_cache[1] and now - _models_cache[0] < MODELS_CACHE_TTL_S:
        return list(_models_cache[1])
    try:
        ids = await llm.models()
    except Exception as exc:  # noqa: BLE001 - /llm must answer even when the gateway is down
        log.warning("cannot list models (%s); reporting the known ids", exc)
        return list(FALLBACK_MODEL_IDS)
    if not ids:
        return list(FALLBACK_MODEL_IDS)
    _models_cache = (now, list(ids))
    return list(ids)



async def _runtime_facts(gateway_client: SandboxGateway) -> str:
    """Short factual line about the sandbox, cached for a minute."""
    global _runtime_cache
    now = time.time()
    if now - _runtime_cache[0] < 60 and _runtime_cache[1]:
        return _runtime_cache[1]
    try:
        status = await asyncio.wait_for(gateway_client.status(), timeout=4.0)
        facts = (
            f"Sandbox accelerator: {status.accel}. Warm VMs: {status.warm}. Active sessions: {status.active}."
        )
        if status.image_problems:
            facts += " WARNING: sandbox image is not built yet: " + "; ".join(status.image_problems)
    except (RpcError, TimeoutError) as exc:
        facts = f"The control plane is currently unreachable ({exc}); tool calls will fail until it is back."
    _runtime_cache = (now, facts)
    return facts


app = create_app()


def main() -> None:  # pragma: no cover - CLI entry point
    import contextlib

    import uvicorn

    with contextlib.suppress(KeyboardInterrupt):
        uvicorn.run(
            "agent.ai.app:app",
            host=settings.ai_host,
            port=settings.ai_port,
            log_level=settings.log_level.lower(),
        )


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["ErrorCode", "app", "create_app", "main"]
