"""MCP client: naming, schema conversion, JSON-RPC over HTTP (and SSE), registration.

Exercised with httpx.MockTransport, so nothing here needs a real MCP server.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from agent.ai import mcp
from agent.config import settings


def mock_client(handler) -> Any:
    """A McpClient wired to a MockTransport."""

    def factory(name="demo", config=None, transport=None):
        return mcp.McpClient(name, config or {"url": "https://mcp.test/rpc"}, transport=httpx.MockTransport(handler))

    return factory


def jsonrpc_handler(seen: list[dict]):
    """Answer initialize / tools/list / tools/call like a minimal server."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"protocolVersion": mcp.PROTOCOL_VERSION, "serverInfo": {"name": "stub"}},
                },
                headers={"mcp-session-id": "abc123"},
            )
        if method == "notifications/initialized":
            return httpx.Response(202, content=b"")
        if method == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "tools": [
                            {
                                "name": "read_file",
                                "description": "Read a file",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"path": {"type": "string", "title": "Path"}},
                                    "required": ["path"],
                                    "$schema": "http://json-schema.org/draft-07/schema#",
                                },
                            }
                        ]
                    },
                },
            )
        if method == "tools/call":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"content": [{"type": "text", "text": "file contents"}], "isError": False},
                },
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "error": {"message": "nope"}})

    return handler


# ------------------------------------------------------------------------- naming


def test_tool_names_are_sanitised_and_short():
    assert mcp.tool_name("My Server!", "Read-File") == "mcp.my_server.read_file"
    long_name = mcp.tool_name("a" * 60, "b" * 60)
    assert len(long_name) <= 41, long_name
    assert long_name.startswith("mcp.")
    assert mcp.sanitize("9lives") == "t_9lives"
    assert mcp.sanitize("") == "unnamed"


def test_parse_qualified():
    assert mcp.parse_qualified("mcp.demo.read_file") == ("demo", "read_file")
    assert mcp.parse_qualified("mcp.demo.a.b") == ("demo", "a.b")
    with pytest.raises(mcp.McpError):
        mcp.parse_qualified("fs.read")


# ----------------------------------------------------------------- schema subset


def test_schema_conversion_drops_what_our_validator_rejects():
    converted = mcp.to_json_schema(
        {
            "type": "object",
            "properties": {"path": {"type": "string", "title": "Path"}},
            "required": ["path"],
            "$schema": "http://json-schema.org/draft-07/schema#",
            "definitions": {"x": {"type": "string"}},
        }
    )
    assert converted["type"] == "object"
    assert "title" not in converted["properties"]["path"]
    assert "$schema" not in converted and "definitions" not in converted
    assert converted["required"] == ["path"]


def test_non_object_schema_is_wrapped_permissively():
    assert mcp.to_json_schema({"type": "string"}) == {
        "type": "object",
        "properties": {},
        "additionalProperties": True,
    }
    assert mcp.to_json_schema(None)["additionalProperties"] is True


def test_manifest_is_valid_for_the_registry():
    tool = mcp.McpTool(server="demo", name="read_file", description="Read", input_schema={"type": "object"})
    manifest = mcp.to_manifest(tool)
    assert manifest.name == "mcp.demo.read_file"
    assert manifest.tags == ["mcp", "demo"]
    assert len(manifest.description) >= 10, "short MCP descriptions must be padded to pass validation"
    assert manifest.params_schema["type"] == "object"
    assert manifest.source.startswith("mcp server")


# -------------------------------------------------------------------- transport


@pytest.mark.asyncio
async def test_initialize_list_and_call():
    seen: list[dict] = []
    client = mock_client(jsonrpc_handler(seen))()
    tools = await client.list_tools()
    assert [tool.name for tool in tools] == ["read_file"]
    assert tools[0].qualified == "mcp.demo.read_file"

    result = await client.call_tool("read_file", {"path": "/etc/hostname"})
    assert result["ok"] is True
    assert "file contents" in result["text"]

    methods = [body["method"] for body in seen]
    assert methods[0] == "initialize"
    assert "notifications/initialized" in methods
    assert "tools/list" in methods and "tools/call" in methods


@pytest.mark.asyncio
async def test_session_id_and_headers_are_reused():
    seen_headers: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
                headers={"mcp-session-id": "sess-1"},
            )
        if body.get("method") == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}})
        return httpx.Response(202, content=b"")

    client = mock_client(handler)("demo", {"url": "https://mcp.test/rpc", "headers": {"Authorization": "Bearer t"}})
    await client.list_tools()
    assert any(headers.get("authorization") == "Bearer t" for headers in seen_headers)
    assert any(headers.get("mcp-session-id") == "sess-1" for headers in seen_headers[1:])


@pytest.mark.asyncio
async def test_sse_framed_answers_are_parsed():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(200, text=f'data: {json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {}})}\n\n',
                                  headers={"content-type": "text/event-stream"})
        if body.get("method") == "tools/list":
            payload = {"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [{"name": "ping", "inputSchema": {}}]}}
            return httpx.Response(
                200,
                text=f"event: message\ndata: {json.dumps(payload)}\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(202, content=b"", headers={"content-type": "text/event-stream"})

    client = mock_client(handler)()
    tools = await client.list_tools()
    assert [tool.name for tool in tools] == ["ping"]


@pytest.mark.asyncio
async def test_tool_error_is_reported_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "tools/call":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {"content": [{"type": "text", "text": "boom"}], "isError": True},
                },
            )
        # notifications carry no id
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": {}})

    client = mock_client(handler)()
    result = await client.call_tool("anything", {})
    assert result["ok"] is False
    assert result["is_error"] is True
    assert "boom" in result["text"]


@pytest.mark.asyncio
async def test_protocol_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "error": {"message": "bad protocol"}})

    client = mock_client(handler)()
    with pytest.raises(mcp.McpError, match="bad protocol"):
        await client.list_tools()


@pytest.mark.asyncio
async def test_http_failure_raises_a_clear_error():
    client = mock_client(lambda request: httpx.Response(500, text="server exploded"))()
    with pytest.raises(mcp.McpError, match="HTTP 500"):
        await client.list_tools()


# ------------------------------------------------------------------- dispatching


@pytest.mark.asyncio
async def test_call_uses_the_configured_server(monkeypatch):
    seen: list[dict] = []
    handler = jsonrpc_handler(seen)
    monkeypatch.setattr(settings, "mcp_servers", json.dumps({"demo": {"url": "https://mcp.test/rpc"}}))

    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(mcp.httpx, "AsyncClient", factory)
    result = await mcp.call("mcp.demo.read_file", {"path": "/x"})
    assert result["ok"] is True and "file contents" in result["text"]


@pytest.mark.asyncio
async def test_call_for_an_unconfigured_server_explains_itself(monkeypatch):
    monkeypatch.setattr(settings, "mcp_servers", json.dumps({"demo": {"url": "https://mcp.test/rpc"}}))
    with pytest.raises(mcp.McpError, match="no MCP server named 'other'"):
        await mcp.call("mcp.other.x", {})


def test_servers_parsing(monkeypatch):
    monkeypatch.setattr(settings, "mcp_servers", "")
    assert mcp.servers() == {}
    monkeypatch.setattr(settings, "mcp_servers", "not json")
    assert mcp.servers() == {}
    monkeypatch.setattr(settings, "mcp_servers", json.dumps({"a": "https://a/mcp", "b": {"url": "https://b/mcp"}}))
    assert set(mcp.servers()) == {"a", "b"}
    monkeypatch.setattr(settings, "mcp_servers", json.dumps({"c": {"nope": 1}}))
    assert mcp.servers() == {}


# ------------------------------------------------------------------ mcp.sync tool


class FakeSession:
    async def commit(self) -> None:
        return None


class FakeCtx:
    def __init__(self) -> None:
        self.embedder = None
        self.registered: list[str] = []

        class Sessions:
            def __call__(self):
                class Ctx:
                    async def __aenter__(self):
                        return FakeSession()

                    async def __aexit__(self, *a):
                        return False

                return Ctx()

        self.sessionmaker = Sessions()


@pytest.mark.asyncio
async def test_sync_without_configuration_says_so(monkeypatch):
    from agent.ai import mcp_tools

    monkeypatch.setattr(settings, "mcp_servers", "")
    result = await mcp_tools.mcp_sync(FakeCtx(), {})
    assert result["ok"] is False
    assert result["error_code"] == "not_configured"
    assert "AGENT_MCP_SERVERS" in result["error"]


@pytest.mark.asyncio
async def test_sync_registers_discovered_tools(monkeypatch):
    from agent.ai import mcp_tools
    from agent.registry import repository as repo

    monkeypatch.setattr(settings, "mcp_servers", json.dumps({"demo": {"url": "https://mcp.test/rpc"}}))
    handler = jsonrpc_handler([])
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(mcp.httpx, "AsyncClient", factory)

    inserted: list[str] = []

    async def insert_tool(session, *, manifest, **kwargs):  # noqa: ANN001, ARG001
        inserted.append(manifest.name)

        class Row:
            version = 1

        return Row()

    async def retire(session, name, version):  # noqa: ANN001, ARG001
        return 0

    monkeypatch.setattr(repo, "insert_tool", insert_tool)
    monkeypatch.setattr(repo, "retire_other_versions", retire)

    result = await mcp_tools.mcp_sync(FakeCtx(), {})
    assert result["ok"] is True
    assert result["registered"] == 1
    assert inserted == ["mcp.demo.read_file"]
    assert result["servers"][0]["registered"] == ["mcp.demo.read_file v1"]


@pytest.mark.asyncio
async def test_sync_reports_a_broken_server_without_failing_everything(monkeypatch):
    from agent.ai import mcp_tools

    monkeypatch.setattr(
        settings, "mcp_servers", json.dumps({"broken": {"url": "https://mcp.test/rpc"}, "ok": {"url": "https://ok/rpc"}})
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "mcp.test" in str(request.url):
            return httpx.Response(500, text="down")
        body = json.loads(request.content)
        if body.get("method") == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": {"tools": []}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": {}})

    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(mcp.httpx, "AsyncClient", factory)
    result = await mcp_tools.mcp_sync(FakeCtx(), {})
    by_server = {item["server"]: item for item in result["servers"]}
    assert by_server["broken"]["ok"] is False
    assert by_server["ok"]["ok"] is True


def test_mcp_tools_are_gated_by_the_trusted_tier():
    from agent.ai import tiers

    assert tiers.required_tier("mcp.demo.read_file") == "trusted"
    assert tiers.allowed("mcp.demo.read_file", "trusted")
    assert not tiers.allowed("mcp.demo.read_file", "safe")


@pytest.mark.asyncio
async def test_sync_tool_is_not_forwarded_to_a_server(monkeypatch, fake_gateway, fake_sessionmaker):
    """Regression: the generic mcp.* forwarder used to swallow mcp.sync itself."""
    from agent.ai import mcp_tools, tiers
    from agent.ai.metacalls import MetaTools
    from agent.models.tool import ToolRecord
    from agent.registry import service as registry_service

    monkeypatch.setattr(tiers.settings, "permission_tier", "trusted")
    monkeypatch.setattr(mcp.settings, "mcp_servers", json.dumps({"demo": {"url": "https://mcp.test/rpc"}}))

    record = ToolRecord(
        id="11111111-1111-1111-1111-111111111111",
        name="mcp.sync",
        version=1,
        status="active",
        tier="core",
        executor="host_native",
        description="sync mcp tools",
        when_to_use="when needed",
        tags=["mcp"],
        params_schema={"type": "object", "properties": {}, "additionalProperties": False},
        permissions=[],
        timeout_s=30.0,
        entrypoint="run",
        examples=[],
        source="",
        source_sha256="a" * 64,
        embedding_model="",
        created_at="2026-01-01T00:00:00",
    )

    async def resolve(session, name, version=None, statuses=("active",)):  # noqa: ANN001, ARG001
        return record

    async def note(session, **kwargs):  # noqa: ANN001, ARG002
        return "active"

    called: list[str] = []

    async def sync(ctx, args):  # noqa: ANN001, ARG001
        called.append("sync")
        return {"ok": True, "registered": 0}

    monkeypatch.setattr(registry_service, "resolve_tool", resolve)
    monkeypatch.setattr(registry_service, "note_call", note)
    monkeypatch.setitem(mcp_tools.MCP_HANDLERS, "mcp.sync", sync)

    meta = MetaTools(session_id="s1", gateway=fake_gateway, sessionmaker=fake_sessionmaker)
    outcome = await meta.invoke("mcp.sync", {})
    assert outcome.ok is True, outcome.error
    assert called == ["sync"], "mcp.sync must run its handler, not be forwarded to a server"


@pytest.mark.asyncio
async def test_real_mcp_tool_names_still_go_to_the_server(monkeypatch, fake_gateway, fake_sessionmaker):
    from agent.ai import mcp as mcp_module
    from agent.ai import tiers
    from agent.ai.metacalls import MetaTools
    from agent.models.tool import ToolRecord
    from agent.registry import service as registry_service

    monkeypatch.setattr(tiers.settings, "permission_tier", "trusted")
    record = ToolRecord(
        id="22222222-2222-2222-2222-222222222222",
        name="mcp.demo.add",
        version=1,
        status="active",
        tier="core",
        executor="host_native",
        description="add numbers",
        when_to_use="when needed",
        tags=["mcp"],
        params_schema={"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                       "required": ["a", "b"], "additionalProperties": False},
        permissions=[],
        timeout_s=30.0,
        entrypoint="run",
        examples=[],
        source="",
        source_sha256="a" * 64,
        embedding_model="",
        created_at="2026-01-01T00:00:00",
    )

    async def resolve(session, name, version=None, statuses=("active",)):  # noqa: ANN001, ARG001
        return record

    async def note(session, **kwargs):  # noqa: ANN001, ARG002
        return "active"

    async def fake_call(qualified, args):  # noqa: ANN001
        return {"ok": True, "text": "42", "qualified": qualified, "args": args}

    monkeypatch.setattr(registry_service, "resolve_tool", resolve)
    monkeypatch.setattr(registry_service, "note_call", note)
    monkeypatch.setattr(mcp_module, "call", fake_call)

    meta = MetaTools(session_id="s1", gateway=fake_gateway, sessionmaker=fake_sessionmaker)
    outcome = await meta.invoke("mcp.demo.add", {"a": 17, "b": 25})
    assert outcome.ok is True
    assert outcome.result["text"] == "42"
