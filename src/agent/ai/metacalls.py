"""The three resident meta tools and the single dispatch path for every call.

The model always sees exactly these three tools.  Everything else is discovered
with ``search_tools``, inspected with ``get_tool_schema`` and executed with
``call_tool``, so the prompt stays small no matter how large the library grows.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import jsonschema
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.ai import tiers
from agent.ai.builtins import BUILTIN_HANDLERS
from agent.ai.mcp_tools import MCP_HANDLERS
from agent.ai.net import NET_HANDLERS
from agent.ai.sandbox_gateway import SandboxGateway
from agent.config import settings
from agent.embeddings import Embedder, get_embedder
from agent.models.protocol import ErrorCode, RpcError
from agent.models.tool import (
    RESIDENT_META_TOOLS,
    ToolCallResult,
    ToolRecord,
    ToolSummary,
    validate_params_schema,
)
from agent.registry import service as registry_service
from agent.toolsmith.gates import TOOLSMITH_HANDLERS, ToolsmithContext

log = logging.getLogger(__name__)

RESULT_CHAR_BUDGET = 32_000


def _function(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


SEARCH_TOOLS_SPEC = _function(
    "search_tools",
    "Search the tool library by intent. Returns compact summaries (name, description, "
    "when to use, argument brief, permissions, score). Always search before calling "
    "anything you have not seen in this conversation.",
    {
        "query": {"type": "string", "description": "natural language description of what you want to do"},
        "k": {"type": "integer", "minimum": 1, "maximum": 20, "description": "how many tools to return (default 5)"},
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "optional tag filter, e.g. ['file'] or ['toolsmith']",
        },
    },
    ["query"],
)

GET_TOOL_SCHEMA_SPEC = _function(
    "get_tool_schema",
    "Fetch the full JSON schema, examples and permissions of one tool. Call this before "
    "call_tool so you never guess argument names.",
    {
        "name": {"type": "string"},
        "version": {"type": "integer", "minimum": 1, "description": "defaults to the active version"},
        "include_source": {
            "type": "boolean",
            "description": "for model-authored tools, also return the python source",
        },
    },
    ["name"],
)

CALL_TOOL_SPEC = _function(
    "call_tool",
    "Execute a tool by name with validated arguments. Native tools (files, commands, "
    "sandbox control) and model-authored tools are called exactly the same way.",
    {
        "name": {"type": "string"},
        "arguments": {"type": "object", "additionalProperties": True},
        "version": {"type": "integer", "minimum": 1},
        "timeout_s": {"type": "number", "minimum": 0.5, "maximum": 300},
    },
    ["name", "arguments"],
)

RESIDENT_TOOL_SPECS: list[dict[str, Any]] = [SEARCH_TOOLS_SPEC, GET_TOOL_SCHEMA_SPEC, CALL_TOOL_SPEC]


@dataclass
class DispatchOutcome:
    ok: bool
    result: Any = None
    error: str | None = None
    error_code: str | None = None
    duration_ms: int = 0
    tool: str = ""
    version: int | None = None
    truncated: bool = False

    def to_tool_result(self) -> ToolCallResult:
        return ToolCallResult(
            ok=self.ok,
            tool=self.tool,
            version=self.version,
            result=self.result,
            error=self.error,
            error_code=self.error_code,
            duration_ms=self.duration_ms,
            truncated=self.truncated,
        )

    def as_message(self) -> dict[str, Any]:
        """Compact JSON view handed back to the model."""
        payload: dict[str, Any] = {"ok": self.ok}
        if self.tool:
            payload["tool"] = self.tool
        if self.ok:
            payload["result"] = self.result
        else:
            payload["error"] = self.error
            if self.error_code:
                payload["error_code"] = self.error_code
        if self.truncated:
            payload["truncated"] = True
        if self.duration_ms:
            payload["duration_ms"] = self.duration_ms
        return payload


class MetaTools:
    def __init__(
        self,
        *,
        session_id: str,
        gateway: SandboxGateway,
        sessionmaker: async_sessionmaker[AsyncSession],
        embedder: Embedder | None = None,
    ) -> None:
        self.session_id = session_id
        self.gateway = gateway
        self.sessionmaker = sessionmaker
        self.embedder = embedder or get_embedder()
        self.toolsmith = ToolsmithContext(
            session_id=session_id,
            gateway=gateway,
            sessionmaker=sessionmaker,
            embedder=self.embedder,
        )

    @property
    def specs(self) -> list[dict[str, Any]]:
        return RESIDENT_TOOL_SPECS

    # ------------------------------------------------------------- meta tools
    async def search(self, args: dict[str, Any]) -> DispatchOutcome:
        query = str(args.get("query") or "").strip()
        k = int(args.get("k") or settings.search_k)
        tags = args.get("tags")
        if not query:
            return DispatchOutcome(False, tool="search_tools", error="query must not be empty", error_code="invalid_args")
        async with self.sessionmaker() as session:
            hits: list[ToolSummary] = await registry_service.search_tools(
                session, query=query, k=max(1, min(20, k)), tags=tags, embedder=self.embedder
            )
        # hide whatever the current tier (or the network switch) cannot use, so the
        # model never wastes a step on a tool it is not allowed to call
        hits = [hit for hit in hits if tiers.visible(hit.name)]
        if not settings.net_enabled:
            hits = [hit for hit in hits if hit.name not in NET_HANDLERS]
        return DispatchOutcome(
            True,
            tool="search_tools",
            result={
                "query": query,
                "count": len(hits),
                "tools": [hit.model_dump() for hit in hits],
                "hint": "use get_tool_schema(name) for the exact arguments of the one you pick",
            },
        )

    async def get_schema(self, args: dict[str, Any]) -> DispatchOutcome:
        name = str(args.get("name") or "")
        if name in RESIDENT_META_TOOLS:
            spec = next(s for s in RESIDENT_TOOL_SPECS if s["function"]["name"] == name)
            return DispatchOutcome(True, tool=name, result=spec["function"])
        version = args.get("version")
        async with self.sessionmaker() as session:
            view = await registry_service.get_tool_schema(
                session,
                name,
                int(version) if version else None,
                include_source=bool(args.get("include_source")),
                include_quarantined=True,
            )
        if view is None:
            return DispatchOutcome(
                False,
                tool="get_tool_schema",
                error=f"tool {name!r} is not registered; use search_tools first",
                error_code="not_found",
            )
        return DispatchOutcome(True, tool="get_tool_schema", result=view.model_dump())

    # ---------------------------------------------------------------- dispatch
    async def call(self, name: str, args: dict[str, Any], version: int | None = None, timeout_s: float | None = None) -> DispatchOutcome:
        """Single entry point for every tool call the model makes.

        The model always addresses the resident meta tools, so a call arrives as
        ``call_tool(name=<target>, arguments={...})`` -- the target has to be
        unwrapped here, otherwise every call would look like call_tool calling
        itself (that bug made the agent unable to run anything).
        """
        if name == "call_tool":
            target = args.get("name") or args.get("tool") or args.get("tool_name")
            if not isinstance(target, str) or not target.strip():
                return DispatchOutcome(
                    False,
                    tool="call_tool",
                    error=(
                        "call_tool needs the target tool name in the 'name' field, "
                        "with its arguments under 'arguments'. "
                        'Example: {"name": "exec.run", "arguments": {"argv": ["uname", "-a"]}}'
                    ),
                    error_code="invalid_args",
                )
            inner = args.get("arguments")
            if inner is None:
                inner = args.get("args") or {}
            if not isinstance(inner, dict):
                return DispatchOutcome(
                    False,
                    tool="call_tool",
                    error=f"'arguments' must be a JSON object, got {type(inner).__name__}",
                    error_code="invalid_args",
                )
            if target.strip() == "call_tool":
                return DispatchOutcome(
                    False,
                    tool="call_tool",
                    error=(
                        "call_tool cannot invoke itself; pass the target tool name, "
                        'e.g. {"name": "exec.run", "arguments": {...}}'
                    ),
                    error_code="invalid_args",
                )
            version = args.get("version", version)
            timeout_s = args.get("timeout_s", timeout_s)
            return await self.call(
                target.strip(),
                inner,
                int(version) if version else None,
                float(timeout_s) if timeout_s else None,
            )
        if name == "search_tools":
            return await self.search(args)
        if name == "get_tool_schema":
            return await self.get_schema(args)
        return await self.invoke(name, args, version=version, timeout_s=timeout_s)

    async def invoke(
        self,
        name: str,
        args: dict[str, Any],
        *,
        version: int | None = None,
        timeout_s: float | None = None,
    ) -> DispatchOutcome:
        async with self.sessionmaker() as session:
            record = await registry_service.resolve_tool(
                session, name, version, statuses=("active", "quarantined")
            )
        if record is None:
            return DispatchOutcome(
                False,
                tool=name,
                error=f"tool {name!r} is not registered or is retired; use search_tools to find alternatives",
                error_code="not_found",
            )
        if record.status == "quarantined":
            return DispatchOutcome(
                False,
                tool=name,
                version=record.version,
                error=(
                    f"{name} v{record.version} is quarantined after repeated failures "
                    f"(last error: {record.last_error or 'unknown'}). Fix it with toolsmith.create or pick another tool."
                ),
                error_code="quarantined",
            )

        invalid = _validate_arguments(record.params_schema, args)
        if invalid:
            return DispatchOutcome(
                False,
                tool=name,
                version=record.version,
                error=f"invalid arguments: {invalid}. Fetch the schema with get_tool_schema('{name}').",
                error_code="invalid_args",
            )

        try:
            tiers.check(name)
        except tiers.TierDenied as exc:
            return DispatchOutcome(False, tool=name, version=record.version, error=str(exc), error_code="tier_denied")

        if tiers.required_tier(name) == "unrestricted":
            # The operator armed this in the console; the phrase proves it and is
            # verified again by the control plane. The model never sees it.
            args = {**args, "confirm": settings.host_exec_phrase}

        if name.startswith("mcp.") and name not in MCP_HANDLERS:
            # MCP tools are executed by the AI service over HTTP (tier 'trusted').
            # Local mcp.* handlers (mcp.sync) fall through to the host_native path.

            from agent.ai import mcp as mcp_client

            try:
                payload = await mcp_client.call(name, args)
            except mcp_client.McpError as exc:
                await self._record(record, ok=False, duration_ms=0, error=str(exc), error_code="mcp_error")
                return DispatchOutcome(False, tool=name, version=record.version, error=str(exc), error_code="mcp_error")
            ok = bool(payload.get("ok", True))
            await self._record(record, ok=ok, duration_ms=0, error=None if ok else payload.get("text", "")[:500])
            return DispatchOutcome(
                ok,
                tool=name,
                version=record.version,
                result=payload,
                error=None if ok else f"the MCP tool reported an error: {payload.get('text', '')[:400]}",
                error_code=None if ok else "tool_error",
            )

        if record.executor == "host_native":
            handler = (
                TOOLSMITH_HANDLERS.get(name)
                or NET_HANDLERS.get(name)
                or MCP_HANDLERS.get(name)
                or BUILTIN_HANDLERS.get(name)
            )
            if handler is None:
                return DispatchOutcome(
                    False, tool=name, error=f"no host handler for {name!r}", error_code="not_found"
                )
            if name in NET_HANDLERS and not settings.net_enabled:
                return DispatchOutcome(
                    False,
                    tool=name,
                    version=record.version,
                    error=(
                        "network access is disabled (AGENT_NET_ENABLED=0). "
                        "The sandbox has no NIC by design; ask the operator to enable net.* "
                        "with an allowlist (AGENT_NET_ALLOW_HOSTS)."
                    ),
                    error_code="net_disabled",
                )
            try:
                payload = await handler(self.toolsmith, args)
            except RpcError as exc:
                payload = {"ok": False, "error": exc.message, "error_code": exc.label}
            ok = bool(payload.get("ok"))
            await self._record(record, ok=ok, duration_ms=0, error=None if ok else json.dumps(payload)[:1500])
            return DispatchOutcome(ok, tool=name, version=record.version, result=payload if ok else None,
                                   error=None if ok else json.dumps(payload, ensure_ascii=False)[:4000],
                                   error_code=None if ok else "toolsmith_rejected")

        try:
            if record.executor == "control_native":
                # runs on the control-plane machine (host.exec); the control plane
                # re-checks the tier and the confirmation phrase itself
                payload = await self.gateway.rpc(name, args, timeout_s=timeout_s or record.timeout_s)
                await self._record(record, ok=True, duration_ms=0, error=None)
                return DispatchOutcome(True, tool=name, version=record.version, result=payload)
            if record.executor == "sandbox_native":
                outcome = await self.gateway.invoke_native(
                    self.session_id, name, args, timeout_s=timeout_s or record.timeout_s
                )
            else:
                outcome = await self.gateway.invoke_tool(
                    self.session_id, record.payload(), args, timeout_s=timeout_s or record.timeout_s
                )
        except RpcError as exc:
            await self._record(record, ok=False, duration_ms=0, error=exc.message, error_code=exc.label)
            return DispatchOutcome(False, tool=name, version=record.version, error=exc.message, error_code=exc.label)

        if not outcome.ok:
            await self._record(
                record, ok=False, duration_ms=outcome.duration_ms, error=outcome.error, error_code=outcome.error_code
            )
            return DispatchOutcome(
                False,
                tool=name,
                version=record.version,
                error=outcome.error or "sandbox call failed",
                error_code=outcome.error_code or "sandbox_error",
                duration_ms=outcome.duration_ms,
            )

        result = outcome.result
        # A generated tool reports failure inside its own result payload.
        if isinstance(result, dict) and result.get("ok") is False and "error" in result:
            await self._record(
                record,
                ok=False,
                duration_ms=outcome.duration_ms,
                error=str(result.get("error"))[:1500],
                error_code="tool_error",
            )
            return DispatchOutcome(
                False,
                tool=name,
                version=record.version,
                error=f"tool reported a failure: {result.get('error')}",
                error_code="tool_error",
                duration_ms=outcome.duration_ms,
            )

        shrunk, truncated = _shrink(result)
        await self._record(record, ok=True, duration_ms=outcome.duration_ms, error=None)
        return DispatchOutcome(
            True,
            tool=name,
            version=record.version,
            result=shrunk,
            duration_ms=outcome.duration_ms,
            truncated=truncated,
        )

    async def _record(
        self,
        record: ToolRecord,
        *,
        ok: bool,
        duration_ms: int,
        error: str | None = None,
        error_code: str | None = None,
    ) -> None:
        try:
            async with self.sessionmaker() as session:
                status = await registry_service.note_call(
                    session,
                    name=record.name,
                    version=record.version,
                    session_id=self.session_id,
                    ok=ok,
                    duration_ms=duration_ms,
                    error=error,
                    error_code=error_code,
                    args={},
                )
                await session.commit()
            if status == "quarantined":
                log.warning("tool %s v%s was quarantined", record.name, record.version)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must never break a call
            log.warning("cannot record tool run for %s: %s", record.name, exc)


def _validate_arguments(schema: dict[str, Any], args: dict[str, Any]) -> str | None:
    problems = validate_params_schema(schema)
    if problems:
        return f"the registered schema is invalid ({problems[0].message})"
    try:
        jsonschema.validate(instance=args or {}, schema=schema)
    except jsonschema.ValidationError as exc:
        path = ".".join(str(part) for part in exc.absolute_path) or "arguments"
        return f"{path}: {exc.message}"
    except jsonschema.SchemaError as exc:  # pragma: no cover - registration guards this
        return f"schema error: {exc.message}"
    return None


def _shrink(value: Any, budget: int = RESULT_CHAR_BUDGET) -> tuple[Any, bool]:
    """Trim a large tool result so one call cannot flood the context window."""
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded) <= budget:
        return value, False

    def trim(node: Any) -> Any:
        if isinstance(node, str):
            return node[:4000] + f"...[trimmed {len(node) - 4000} chars]"
        if isinstance(node, list):
            return [trim(item) for item in node[:50]] + (["...[list trimmed]"] if len(node) > 50 else [])
        if isinstance(node, dict):
            return {key: trim(item) for key, item in node.items()}
        return node

    trimmed = trim(value)
    encoded = json.dumps(trimmed, ensure_ascii=False, default=str)
    if len(encoded) > budget:
        return {"result_preview": encoded[:budget], "note": "result too large; full value truncated"}, True
    return trimmed, True


__all__ = [
    "CALL_TOOL_SPEC",
    "GET_TOOL_SCHEMA_SPEC",
    "RESIDENT_TOOL_SPECS",
    "SEARCH_TOOLS_SPEC",
    "DispatchOutcome",
    "ErrorCode",
    "MetaTools",
]
