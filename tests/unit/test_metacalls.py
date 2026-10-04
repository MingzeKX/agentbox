"""Meta tool dispatch: argument validation, routing and quarantine handling."""

from __future__ import annotations

import pytest

from agent.ai.metacalls import MetaTools, _shrink, _validate_arguments
from agent.models.protocol import RpcError
from agent.models.tool import ToolRecord
from agent.registry import service as registry_service

SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "limit": {"type": "integer", "minimum": 1}},
    "required": ["path"],
    "additionalProperties": False,
}


def record(
    *,
    name: str = "word_count",
    executor: str = "sandbox_python",
    status: str = "active",
    permissions: list[str] | None = None,
    schema: dict | None = None,
) -> ToolRecord:
    return ToolRecord(
        id="11111111-1111-1111-1111-111111111111",
        name=name,
        version=2,
        status=status,  # type: ignore[arg-type]
        tier="generated" if executor == "sandbox_python" else "core",
        executor=executor,  # type: ignore[arg-type]
        description="A tool used by the test suite.",
        when_to_use="Whenever the test needs one.",
        tags=["test"],
        params_schema=schema or SCHEMA,
        permissions=permissions if permissions is not None else ["fs.read"],
        timeout_s=30.0,
        entrypoint="run",
        examples=[],
        source="def run(args):\n    return {'ok': True}\n",
        source_sha256="a" * 64,
        embedding_model="fake",
        created_at="2026-01-01T00:00:00",
    )


def build(monkeypatch, gateway, sessionmaker, tool: ToolRecord | None):
    async def resolve(session, name, version=None, statuses=("active",)):
        return tool

    async def note(session, **kwargs):
        return "active"

    monkeypatch.setattr(registry_service, "resolve_tool", resolve)
    monkeypatch.setattr(registry_service, "note_call", note)
    return MetaTools(session_id="s1", gateway=gateway, sessionmaker=sessionmaker)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# argument validation
# --------------------------------------------------------------------------- #


def test_validate_arguments_accepts_a_valid_payload():
    assert _validate_arguments(SCHEMA, {"path": "/workspace/a", "limit": 3}) is None


def test_validate_arguments_reports_missing_required():
    message = _validate_arguments(SCHEMA, {})
    assert message and "path" in message


def test_validate_arguments_rejects_unknown_keys():
    message = _validate_arguments(SCHEMA, {"path": "/x", "turbo": True})
    assert message and "additional" in message.lower()


def test_validate_arguments_rejects_wrong_types_and_ranges():
    assert _validate_arguments(SCHEMA, {"path": 5}) is not None
    assert _validate_arguments(SCHEMA, {"path": "/x", "limit": 0}) is not None


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unknown_tool_is_reported(monkeypatch, fake_gateway, fake_sessionmaker):
    meta = build(monkeypatch, fake_gateway, fake_sessionmaker, None)
    outcome = await meta.invoke("nope", {})
    assert outcome.ok is False
    assert outcome.error_code == "not_found"
    assert fake_gateway.calls == []


@pytest.mark.asyncio
async def test_invalid_arguments_never_reach_the_sandbox(monkeypatch, fake_gateway, fake_sessionmaker):
    meta = build(monkeypatch, fake_gateway, fake_sessionmaker, record())
    outcome = await meta.invoke("word_count", {"nope": 1})
    assert outcome.ok is False
    assert outcome.error_code == "invalid_args"
    assert "get_tool_schema" in (outcome.error or "")
    assert fake_gateway.calls == []


@pytest.mark.asyncio
async def test_quarantined_tool_is_refused(monkeypatch, fake_gateway, fake_sessionmaker):
    meta = build(monkeypatch, fake_gateway, fake_sessionmaker, record(status="quarantined"))
    outcome = await meta.invoke("word_count", {"path": "/workspace/a"})
    assert outcome.ok is False
    assert outcome.error_code == "quarantined"
    assert fake_gateway.calls == []


@pytest.mark.asyncio
async def test_native_tool_goes_to_the_matching_guest_method(monkeypatch, fake_gateway, fake_sessionmaker):
    tool = record(name="exec.run", executor="sandbox_native")
    tool.params_schema = {
        "type": "object",
        "properties": {"argv": {"type": "array", "items": {"type": "string"}}},
        "required": ["argv"],
        "additionalProperties": False,
    }
    meta = build(monkeypatch, fake_gateway, fake_sessionmaker, tool)
    outcome = await meta.invoke("exec.run", {"argv": ["echo", "hi"]})
    assert outcome.ok is True
    kind, payload = fake_gateway.calls[-1]
    assert kind == "native"
    assert payload["method"] == "exec.run"
    assert payload["params"] == {"argv": ["echo", "hi"]}


@pytest.mark.asyncio
async def test_generated_tool_is_sent_as_a_verified_payload(monkeypatch, fake_gateway, fake_sessionmaker):
    meta = build(monkeypatch, fake_gateway, fake_sessionmaker, record())
    outcome = await meta.invoke("word_count", {"path": "/workspace/a.txt"})
    assert outcome.ok is True
    kind, payload = fake_gateway.calls[-1]
    assert kind == "tool"
    assert payload["tool"] == "word_count"
    assert payload["arguments"] == {"path": "/workspace/a.txt"}


@pytest.mark.asyncio
async def test_tool_reported_failure_becomes_an_error_outcome(monkeypatch, fake_sessionmaker):
    from tests.conftest import FakeGateway

    gateway = FakeGateway(responses={"tool:word_count": {"ok": False, "error": "FileNotFoundError: nope"}})
    meta = build(monkeypatch, gateway, fake_sessionmaker, record())
    outcome = await meta.invoke("word_count", {"path": "/workspace/missing"})
    assert outcome.ok is False
    assert outcome.error_code == "tool_error"
    assert "FileNotFoundError" in (outcome.error or "")


@pytest.mark.asyncio
async def test_gateway_error_is_surfaced(monkeypatch, fake_sessionmaker):
    from tests.conftest import FakeGateway

    gateway = FakeGateway()
    meta = build(monkeypatch, gateway, fake_sessionmaker, record())

    async def explode(*args, **kwargs):
        raise RpcError(1007, "control plane unreachable")

    monkeypatch.setattr(gateway, "invoke_tool", explode)
    outcome = await meta.invoke("word_count", {"path": "/workspace/a"})
    assert outcome.ok is False
    assert outcome.error_code == "unavailable"
    assert "unreachable" in (outcome.error or "")


# --------------------------------------------------------------------------- #
# resident tools
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_search_tools_returns_compact_summaries(monkeypatch, fake_gateway, fake_sessionmaker):
    async def search(session, query, k=5, tags=None, tiers=None, embedder=None):
        from agent.models.tool import ToolSummary

        return [
            ToolSummary(
                name="fs.read",
                version=1,
                description="read a file",
                when_to_use="when you need file contents",
                args_brief="path:string",
                permissions=[],
                tags=["file"],
                tier="core",
                executor="sandbox_native",
                score=0.5,
            )
        ]

    monkeypatch.setattr(registry_service, "search_tools", search)
    meta = build(monkeypatch, fake_gateway, fake_sessionmaker, record())
    outcome = await meta.search({"query": "read a file"})
    assert outcome.ok is True
    assert outcome.result["count"] == 1
    assert outcome.result["tools"][0]["name"] == "fs.read"


@pytest.mark.asyncio
async def test_get_tool_schema_for_a_resident_tool(monkeypatch, fake_gateway, fake_sessionmaker):
    meta = build(monkeypatch, fake_gateway, fake_sessionmaker, record())
    outcome = await meta.get_schema({"name": "call_tool"})
    assert outcome.ok is True
    assert outcome.result["name"] == "call_tool"
    assert "arguments" in outcome.result["parameters"]["properties"]


@pytest.mark.asyncio
async def test_call_tool_unwraps_the_target_tool(monkeypatch, fake_gateway, fake_sessionmaker):
    """The model addresses everything through call_tool, so it must be unwrapped.

    Regression: the dispatcher used to treat the *meta* tool name as the target and
    answered every call with "call_tool cannot invoke itself", which made the agent
    unable to run a single tool.
    """
    tool = record(name="exec.run", executor="sandbox_native")
    tool.params_schema = {
        "type": "object",
        "properties": {"argv": {"type": "array", "items": {"type": "string"}}},
        "required": ["argv"],
        "additionalProperties": False,
    }
    meta = build(monkeypatch, fake_gateway, fake_sessionmaker, tool)
    outcome = await meta.call("call_tool", {"name": "exec.run", "arguments": {"argv": ["uname", "-a"]}})
    assert outcome.ok is True, outcome.error
    assert outcome.tool == "exec.run"
    kind, payload = fake_gateway.calls[-1]
    assert kind == "native"
    assert payload["method"] == "exec.run"
    assert payload["params"] == {"argv": ["uname", "-a"]}


@pytest.mark.asyncio
async def test_call_tool_accepts_the_tool_alias(monkeypatch, fake_gateway, fake_sessionmaker):
    tool = record(name="exec.run", executor="sandbox_native")
    tool.params_schema = {
        "type": "object",
        "properties": {"argv": {"type": "array", "items": {"type": "string"}}},
        "required": ["argv"],
        "additionalProperties": False,
    }
    meta = build(monkeypatch, fake_gateway, fake_sessionmaker, tool)
    outcome = await meta.call("call_tool", {"tool": "exec.run", "arguments": {"argv": ["id"]}})
    assert outcome.ok is True, outcome.error
    assert fake_gateway.calls[-1][1]["params"] == {"argv": ["id"]}


@pytest.mark.asyncio
async def test_call_tool_cannot_target_itself(monkeypatch, fake_gateway, fake_sessionmaker):
    meta = build(monkeypatch, fake_gateway, fake_sessionmaker, record())
    outcome = await meta.call("call_tool", {"name": "call_tool", "arguments": {}})
    assert outcome.ok is False
    assert outcome.error_code == "invalid_args"
    assert fake_gateway.calls == []


@pytest.mark.asyncio
async def test_call_tool_without_a_target_explains_the_shape(monkeypatch, fake_gateway, fake_sessionmaker):
    meta = build(monkeypatch, fake_gateway, fake_sessionmaker, record())
    outcome = await meta.call("call_tool", {})
    assert outcome.ok is False
    assert outcome.error_code == "invalid_args"
    assert '"name"' in (outcome.error or "") and "arguments" in (outcome.error or "")
    assert fake_gateway.calls == []


@pytest.mark.asyncio
async def test_call_tool_rejects_non_object_arguments(monkeypatch, fake_gateway, fake_sessionmaker):
    meta = build(monkeypatch, fake_gateway, fake_sessionmaker, record())
    outcome = await meta.call("call_tool", {"name": "word_count", "arguments": "path=/x"})
    assert outcome.ok is False
    assert outcome.error_code == "invalid_args"
    assert fake_gateway.calls == []


# --------------------------------------------------------------------------- #
# result shrinking
# --------------------------------------------------------------------------- #


def test_small_results_are_untouched():
    value = {"stdout": "hello", "exit_code": 0}
    shrunk, truncated = _shrink(value)
    assert shrunk == value
    assert truncated is False


def test_large_results_are_trimmed():
    value = {"stdout": "x" * 200_000, "stderr": "", "exit_code": 0}
    shrunk, truncated = _shrink(value)
    assert truncated is True
    assert len(shrunk["stdout"]) < 200_000
    assert shrunk["exit_code"] == 0


def test_absurd_results_fall_back_to_a_preview():
    value = {"chunks": ["y" * 5000 for _ in range(200)]}
    shrunk, truncated = _shrink(value)
    assert truncated is True
    assert set(shrunk) <= {"result_preview", "note"}
