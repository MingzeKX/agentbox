"""MCP (Model Context Protocol) client: bring external tool servers into the library.

Supported transport: **Streamable HTTP / HTTP+SSE** (JSON-RPC over HTTP, answers either
as ``application/json`` or as an SSE stream).  Stdio servers are intentionally *not*
launched from here: the AI service is not allowed to spawn processes (see
tests/unit/test_isolation_guard.py).  Run those servers where they are allowed to run
and expose them over HTTP.

Tools discovered from a server are registered as ``mcp.<server>.<tool>`` so the three
resident meta tools reach them like any other tool.  They sit behind the ``trusted``
permission tier, and nothing else about them is special: every call still goes through
argument validation, the run ledger and the circuit breaker.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from agent.config import settings

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
NAME_SAFE = re.compile(r"[^a-z0-9_]+")
MAX_TEXT_CHARS = 60_000


class McpError(RuntimeError):
    """Transport or protocol failure (never raised for a tool that reports isError)."""


def sanitize(part: str) -> str:
    """Make a server/tool name legal for our tool-name grammar."""
    cleaned = NAME_SAFE.sub("_", (part or "").lower()).strip("_")
    if not cleaned:
        cleaned = "unnamed"
    if not cleaned[0].isalpha():
        cleaned = "t_" + cleaned
    return cleaned[:16]


def tool_name(server: str, tool: str) -> str:
    return f"mcp.{sanitize(server)}.{sanitize(tool)}"


@dataclass
class McpTool:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)

    @property
    def qualified(self) -> str:
        return tool_name(self.server, self.name)


def servers() -> dict[str, dict[str, Any]]:
    """Parse AGENT_MCP_SERVERS: {"name": {"url": "...", "headers": {...}}}."""
    raw = (getattr(settings, "mcp_servers", "") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error("AGENT_MCP_SERVERS is not valid JSON: %s", exc)
        return {}
    if not isinstance(parsed, dict):
        log.error("AGENT_MCP_SERVERS must be an object of server -> config")
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, config in parsed.items():
        if isinstance(config, str):
            config = {"url": config}
        if isinstance(config, dict) and config.get("url"):
            out[str(name)] = config
        else:
            log.warning("ignoring MCP server %r: needs a 'url'", name)
    return out


def _parse_body(response: httpx.Response) -> dict[str, Any]:
    """Accept plain JSON and SSE-framed JSON-RPC answers."""
    content_type = response.headers.get("content-type", "")
    text = response.text
    if "text/event-stream" in content_type:
        payloads: list[dict[str, Any]] = []
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                payloads.append(json.loads(chunk))
            except json.JSONDecodeError:
                continue
        for item in reversed(payloads):  # the answer comes last
            if "result" in item or "error" in item:
                return item
        raise McpError("SSE stream carried no JSON-RPC result")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpError(f"server did not answer with JSON: {text[:200]}") from exc


class McpClient:
    """One HTTP JSON-RPC session with a single MCP server."""

    def __init__(self, name: str, config: dict[str, Any], transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.name = name
        self.url = str(config["url"])
        self.headers = {str(k): str(v) for k, v in (config.get("headers") or {}).items()}
        self.timeout = float(config.get("timeout_s") or 30.0)
        self.transport = transport
        self._counter = 0
        self._session_id: str | None = None
        self._initialised = False

    def _next_id(self) -> int:
        self._counter += 1
        return self._counter

    async def _rpc(self, method: str, params: dict[str, Any] | None = None, *, notify: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            body["id"] = self._next_id()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.post(self.url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise McpError(f"{self.name}: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise McpError(f"{self.name}: HTTP {response.status_code}: {response.text[:200]}")
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        if notify:
            return {}
        payload = _parse_body(response)
        if "error" in payload:
            error = payload["error"] or {}
            raise McpError(f"{self.name}: {error.get('message') or error}")
        result = payload.get("result")
        return result if isinstance(result, dict) else {"result": result}

    async def initialize(self, *, force: bool = False) -> dict[str, Any]:
        if self._initialised and not force:
            return {"already": True}
        result = await self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agentbox", "version": "0.1"},
            },
        )
        await self._rpc("notifications/initialized", {}, notify=True)
        self._initialised = True
        return result

    async def list_tools(self) -> list[McpTool]:
        await self.initialize()
        result = await self._rpc("tools/list", {})
        tools: list[McpTool] = []
        for item in result.get("tools") or []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            tools.append(
                McpTool(
                    server=self.name,
                    name=str(item["name"]),
                    description=str(item.get("description") or "")[:400],
                    input_schema=item.get("inputSchema") if isinstance(item.get("inputSchema"), dict) else {},
                )
            )
        return tools

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        await self.initialize()
        result = await self._rpc("tools/call", {"name": tool, "arguments": arguments or {}})
        return flatten(result)


def flatten(result: dict[str, Any]) -> dict[str, Any]:
    """MCP tool results are content blocks; flatten them for our result budget."""
    blocks = result.get("content")
    texts: list[str] = []
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                texts.append(str(block.get("text") or ""))
            elif block.get("type") == "resource":
                texts.append(json.dumps(block.get("resource"), ensure_ascii=False, default=str))
            else:
                texts.append(json.dumps(block, ensure_ascii=False, default=str))
    elif result.get("structuredContent") is not None:
        texts.append(json.dumps(result["structuredContent"], ensure_ascii=False, default=str))
    else:
        texts.append(json.dumps(result, ensure_ascii=False, default=str))
    text = "\n".join(part for part in texts if part)
    return {
        "ok": not bool(result.get("isError")),
        "text": text[:MAX_TEXT_CHARS],
        "truncated": len(text) > MAX_TEXT_CHARS,
        "is_error": bool(result.get("isError")),
    }


def parse_qualified(name: str) -> tuple[str, str]:
    """'mcp.server.tool' -> ('server', 'tool')."""
    parts = name.split(".")
    if len(parts) < 3 or parts[0] != "mcp":
        raise McpError(f"{name!r} is not an mcp.<server>.<tool> name")
    return parts[1], ".".join(parts[2:])


async def call(qualified: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call a tool on a configured server (used by the meta-tool dispatch)."""
    server, tool = parse_qualified(qualified)
    config = servers().get(server)
    if config is None:
        raise McpError(
            f"no MCP server named {server!r} is configured "
            f"(AGENT_MCP_SERVERS has: {', '.join(servers()) or 'nothing'})"
        )
    client = McpClient(server, config)
    return await client.call_tool(tool, arguments)


async def discover_with(client: McpClient) -> list[McpTool]:
    return await client.list_tools()

ALLOWED_SCHEMA_KEYS = (
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "description",
    "default",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "pattern",
)


def to_json_schema(input_schema: dict[str, Any] | None) -> dict[str, Any]:
    """Convert an MCP inputSchema into the JSON-Schema subset this registry accepts.

    Unknown keywords are dropped rather than rejected: an MCP server is written for the
    whole protocol, not for our validator.
    """
    if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
        return {"type": "object", "properties": {}, "additionalProperties": True}

    def clean(node: Any) -> Any:
        if isinstance(node, list):
            return [clean(item) for item in node]
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in {"properties"} and isinstance(value, dict):
                out[key] = {str(k): clean(v) for k, v in value.items()}
            elif key in ALLOWED_SCHEMA_KEYS:
                out[key] = clean(value)
            elif key == "$defs" or key.startswith("$"):
                continue
            else:
                continue
        return out

    cleaned = clean(input_schema)
    cleaned.setdefault("type", "object")
    return cleaned


def to_manifest(tool: McpTool) -> Any:
    """Build the registry manifest for a discovered MCP tool.

    The registry requires a description of at least 10 characters and does not carry an
    executor field (that is passed to insert_tool separately), so a terse MCP server
    still produces a valid manifest.
    """
    from agent.models.tool import ToolManifest

    description = (tool.description or "").strip()
    if len(description) < 10:
        description = f"{tool.name} on MCP server {tool.server}".strip()
    return ToolManifest(
        name=tool.qualified,
        description=description,
        when_to_use=f"Provided by the MCP server {tool.server!r}.",
        tags=["mcp", tool.server],
        permissions=[],
        timeout_s=60.0,
        entrypoint="run",
        params_schema=to_json_schema(tool.input_schema),
        examples=[],
        source=f"mcp server {tool.server!r} tool {tool.name!r}",
    )
