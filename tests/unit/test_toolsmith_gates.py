"""The four authoring gates (G0 manifest, G1 static, G2 tests, G3 register)."""

from __future__ import annotations

import pytest

from agent.registry import repository as repo
from agent.toolsmith import gates
from agent.toolsmith.gates import ToolsmithContext, check, create, list_generated

SOURCE = '''
def run(args):
    """Uppercase the text in args['text']."""
    return {"upper": str(args["text"]).upper()}
'''

TESTS = [
    {"name": "success", "args": {"text": "hi"}, "expect": {"contains": {"upper": "HI"}}},
    {"name": "edge", "args": {"text": ""}, "expect": {"equals": {"upper": ""}}},
    {"name": "error", "args": {}, "expect": {"raises": "KeyError"}},
]

MANIFEST = {
    "name": "upper_text",
    "description": "Uppercase a string passed as an argument.",
    "when_to_use": "When you need a normalised version of a label.",
    "tags": ["text"],
    "params_schema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
    "permissions": [],
    "source": SOURCE,
    "tests": TESTS,
}


@pytest.fixture
def ctx(fake_gateway, fake_sessionmaker, fake_embedder) -> ToolsmithContext:
    fake_gateway.responses["native:py.check"] = {"ok": True, "violations": [], "stats": {"loc": 4}}
    fake_gateway.responses["native:tool.test"] = {
        "ok": True,
        "passed": len(TESTS),
        "failed": 0,
        "results": [{"name": case["name"], "ok": True, "message": "ok"} for case in TESTS],
    }
    return ToolsmithContext(
        session_id="s-gates",
        gateway=fake_gateway,  # type: ignore[arg-type]
        sessionmaker=fake_sessionmaker,  # type: ignore[arg-type]
        embedder=fake_embedder,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# G0 manifest
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_invalid_manifest_is_rejected_before_any_sandbox_call(ctx, fake_gateway):
    result = await check(ctx, {**MANIFEST, "name": "Bad Name"})
    assert result["ok"] is False
    assert result["stage"] == "manifest"
    assert result["violations"][0]["rule"] == "manifest_invalid"
    assert fake_gateway.calls == []


@pytest.mark.asyncio
async def test_reserved_names_are_refused(ctx, fake_gateway):
    result = await check(ctx, {**MANIFEST, "name": "fs.read"})
    assert result["ok"] is False
    assert result["violations"][0]["rule"] == "name_reserved"
    assert fake_gateway.calls == []


@pytest.mark.asyncio
async def test_bad_params_schema_is_reported_as_a_manifest_violation(ctx):
    result = await check(ctx, {**MANIFEST, "params_schema": {"type": "object", "properties": {"a": {"$ref": "#/a"}}}})
    assert result["ok"] is False
    assert result["violations"][0]["rule"] == "manifest_invalid"


# --------------------------------------------------------------------------- #
# G1 static check
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_static_check_runs_in_the_sandbox_and_blocks_registration(ctx, fake_gateway):
    fake_gateway.responses["native:py.check"] = {
        "ok": False,
        "violations": [{"rule": "import_forbidden", "message": "module 'os' is not in the allow-list", "line": 1}],
        "stats": {"loc": 3},
    }
    result = await check(ctx, MANIFEST)
    assert result["ok"] is False
    assert result["stage"] == "static_check"
    assert result["violations"][0]["rule"] == "import_forbidden"
    kinds = [kind for kind, _ in fake_gateway.calls]
    assert kinds == ["native"], "a rejected manifest must never reach the test runner"


@pytest.mark.asyncio
async def test_static_check_sends_the_source_and_permissions(ctx, fake_gateway):
    await check(ctx, MANIFEST)
    _, payload = fake_gateway.calls[0]
    assert payload["method"] == "py.check"
    assert payload["params"]["source"] == SOURCE
    assert payload["params"]["entrypoint"] == "run"


# --------------------------------------------------------------------------- #
# G2 sandbox tests
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_failing_tests_block_registration(ctx, fake_gateway):
    fake_gateway.responses["native:tool.test"] = {
        "ok": False,
        "passed": 1,
        "failed": 2,
        "results": [
            {"name": "edge", "ok": False, "message": "expected @@@, got ###"},
            {"name": "error", "ok": False, "message": "expected KeyError, got NoneType"},
        ],
    }
    result = await check(ctx, MANIFEST)
    assert result["ok"] is False
    assert result["stage"] == "sandbox_tests"
    assert result["failed"] == 2
    assert result["results"][0]["message"]


@pytest.mark.asyncio
async def test_successful_check_reports_stats_and_next_step(ctx, fake_gateway):
    result = await check(ctx, MANIFEST)
    assert result["ok"] is True
    assert result["stage"] == "ready"
    assert result["tests"] == {"passed": 3, "failed": 0}
    assert "toolsmith.create" in result["next"]
    assert [kind for kind, _ in fake_gateway.calls] == ["native", "native"]


@pytest.mark.asyncio
async def test_check_without_tests_skips_the_test_gate(ctx, fake_gateway):
    result = await check(ctx, {**MANIFEST, "tests": []})
    assert result["ok"] is True
    assert result["tests"] == {"passed": 0, "failed": 0}
    assert len(fake_gateway.calls) == 1


@pytest.mark.asyncio
async def test_unreachable_sandbox_is_reported_not_raised(ctx, fake_gateway):
    from agent.models.protocol import RpcError

    async def boom(*args, **kwargs):
        raise RpcError(1007, "control plane at http://10.0.2.2:8091 is unreachable")

    fake_gateway.invoke_native = boom  # type: ignore[assignment]
    result = await check(ctx, MANIFEST)
    assert result["ok"] is False
    assert result["stage"] == "sandbox"
    assert "unreachable" in result["error"]


# --------------------------------------------------------------------------- #
# G3 registration
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_requires_three_test_cases(ctx, fake_gateway):
    result = await create(ctx, {**MANIFEST, "tests": TESTS[:2]})
    assert result["ok"] is False
    assert result["violations"][0]["rule"] == "tests_required"
    assert fake_gateway.calls == []


@pytest.mark.asyncio
async def test_create_registers_and_retires_older_versions(ctx, monkeypatch):
    inserted: dict[str, object] = {}

    async def fake_insert(session, *, manifest, tier, executor, embedding, embedding_model, created_by, status="active"):
        inserted.update(
            {
                "name": manifest.name,
                "tier": tier,
                "executor": executor,
                "embedding": embedding,
                "embedding_model": embedding_model,
                "created_by": created_by,
            }
        )

        class Row:
            version = 4

        return Row()

    retired: dict[str, int] = {}

    async def fake_retire(session, name, keep_version):
        retired["name"] = name
        retired["keep"] = keep_version
        return 2

    monkeypatch.setattr(repo, "insert_tool", fake_insert)
    monkeypatch.setattr(repo, "retire_other_versions", fake_retire)

    result = await create(ctx, MANIFEST)

    assert result["ok"] is True
    assert result["stage"] == "registered"
    assert result["tool"]["name"] == "upper_text"
    assert result["tool"]["version"] == 4
    assert result["retired_versions"] == 2
    assert inserted["tier"] == "generated"
    assert inserted["executor"] == "sandbox_python"
    assert inserted["created_by"] == "model"
    assert inserted["embedding_model"] == "fake-embedder"
    assert retired == {"name": "upper_text", "keep": 4}


@pytest.mark.asyncio
async def test_create_survives_a_missing_embedder(ctx, monkeypatch, fake_gateway, fake_sessionmaker):
    from agent.embeddings import UnavailableEmbedder

    ctx.embedder = UnavailableEmbedder("no model installed")

    async def fake_insert(session, *, manifest, tier, executor, embedding, embedding_model, created_by, status="active"):
        assert embedding is None
        assert embedding_model == ""

        class Row:
            version = 1

        return Row()

    async def fake_retire(session, name, keep_version):
        return 0

    monkeypatch.setattr(repo, "insert_tool", fake_insert)
    monkeypatch.setattr(repo, "retire_other_versions", fake_retire)

    result = await create(ctx, MANIFEST)
    assert result["ok"] is True
    assert result["embedded"] is False


@pytest.mark.asyncio
async def test_list_generated_summarises_model_tools(monkeypatch):
    class Row:
        name = "upper_text"
        version = 2
        status = "active"
        runs = 7
        failures = 1
        last_error = "KeyError: text"
        description = "uppercase"

    async def fake_list(session, *, tier=None, status=None, name_like=None, limit=200, offset=0):
        assert tier == "generated"
        return [Row()]

    monkeypatch.setattr(repo, "list_tools", fake_list)
    from tests.conftest import FakeSessionmaker

    ctx = ToolsmithContext(session_id="s", gateway=None, sessionmaker=FakeSessionmaker())  # type: ignore[arg-type]
    result = await list_generated(ctx, {"limit": 5})
    assert result["count"] == 1
    assert result["tools"][0]["name"] == "upper_text"
    assert result["tools"][0]["failures"] == 1


def test_toolsmith_handlers_cover_the_seeded_core_tools():
    from agent.ai.net import NET_HANDLERS
    from agent.registry.seed import CORE_TOOLS

    host_tools = {spec["name"] for spec in CORE_TOOLS if spec["executor"] == "host_native"}
    # host_native tools are served either by the toolsmith pipeline or by the
    # firewalled network tools
    # host_native tools are served by the toolsmith pipeline, the network tools or the
    # MCP integration
    from agent.ai.builtins import BUILTIN_HANDLERS
    from agent.ai.mcp_tools import MCP_HANDLERS

    # host_native tools are served by the toolsmith pipeline, the network client, the
    # MCP integration or the small built-ins (time.now)
    assert host_tools == (
        set(gates.TOOLSMITH_HANDLERS) | set(NET_HANDLERS) | set(MCP_HANDLERS) | set(BUILTIN_HANDLERS)
    )
