"""Registry integration tests against a real PostgreSQL + pgvector.

Run with a reachable database:

    AGENT_DB_URL=postgresql+asyncpg://agent:agent@127.0.0.1:5432/agentbox_test \\
        pytest -m pg -v

The suite creates its own schema namespace by using a dedicated database
(``agentbox_test`` in the example above) and truncates the tool tables between
tests.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text, update

from agent.models.tool import ToolManifest
from agent.registry import db as registry_db
from agent.registry import repository as repo
from agent.registry import service as registry_service
from agent.registry.tables import Message, SandboxSessionRecord, Session, Tool, ToolRun

# module-scoped async fixtures + per-test event loops do not mix (asyncpg pools are
# bound to the loop that created them): keep this suite on one loop.
pytestmark = [pytest.mark.pg, pytest.mark.asyncio(loop_scope="module")]


class KeywordEmbedder:
    """Deterministic 1024-dim embedder so the vector half can be asserted."""

    name = "test-keyword"
    dim = 1024
    available = True

    def _vec(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for index, token in enumerate(text.lower().split()[: self.dim]):
            vector[(hash(token) % self.dim + index) % self.dim] = 1.0
        norm = sum(value * value for value in vector) ** 0.5 or 1.0
        return [value / norm for value in vector]

    async def embed(self, texts):
        return [self._vec(text) for text in texts]

    async def embed_one(self, text: str) -> list[float]:
        return self._vec(text)


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def database():
    if not os.environ.get("AGENT_DB_URL"):
        pytest.skip("set AGENT_DB_URL to a PostgreSQL+pgvector database to run the pg suite")
    # This suite DELETES rows and overwrites embeddings (its seeded fixture embeds the
    # core tools with a hash stub).  Point it at a test database, never at the live one:
    # doing that silently wrecked semantic search once already.
    db_name = os.environ["AGENT_DB_URL"].rsplit("/", 1)[-1].split("?")[0]
    if not db_name.endswith("_test") and not os.environ.get("AGENT_PG_ALLOW_PROD"):
        pytest.skip(
            f"refusing to run against {db_name!r}: this suite wipes rows and rewrites embeddings. "
            f"Use a *_test database, or set AGENT_PG_ALLOW_PROD=1 if you really mean it."
        )
    ok, detail = await registry_db.ping()
    if not ok:
        pytest.skip(f"database not reachable: {detail}")
    await registry_db.init_db(create=True)
    yield
    await registry_db.dispose()


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def clean(monkeypatch):
    async with registry_db.session_scope() as session:
        # every table these tests read must be emptied, otherwise a previous run's
        # rows show up as extra messages/sessions and the assertions get confusing
        await session.execute(delete(Message))
        await session.execute(delete(SandboxSessionRecord))
        await session.execute(delete(Session))
        await session.execute(delete(ToolRun))
        await session.execute(delete(Tool))
    monkeypatch.setattr(registry_service, "get_embedder", lambda: KeywordEmbedder())
    yield


MANIFEST = {
    "name": "csv_summary",
    "description": "Summarise a CSV file with per-column statistics.",
    "when_to_use": "You have a CSV in the workspace and need column level aggregates.",
    "tags": ["csv", "statistics", "analysis"],
    "params_schema": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    },
    "permissions": ["fs.read"],
    "source": "def run(args):\n    return {'rows': 0}\n",
    "tests": [],
}


@pytest_asyncio.fixture(loop_scope="module")
async def seeded(clean):
    """Seed the core tools, embedding them with whatever embedder this box really has.

    The tools and the query must live in the *same* vector space; seeding with the hash
    stub while querying with a real model makes every vector comparison meaningless.
    """
    from agent.embeddings import get_embedder

    embedder = get_embedder()
    if not getattr(embedder, "available", False):
        embedder = KeywordEmbedder()
    async with registry_db.session_scope() as session:
        outcome = await registry_service.seed_core_tools(session, embedder=embedder)
    return outcome


def manifest(**overrides) -> ToolManifest:
    return ToolManifest.model_validate({**MANIFEST, **overrides})


async def test_extensions_and_schema_exist():
    async with registry_db.session_scope() as session:
        extensions = (
            await session.execute(
                text("select extname from pg_extension where extname in ('vector','pg_trgm') order by extname")
            )
        ).scalars().all()
    assert extensions == ["pg_trgm", "vector"]


async def test_seeding_is_idempotent(seeded):
    assert "fs.read" in seeded
    async with registry_db.session_scope() as session:
        first = await registry_service.seed_core_tools(session, embedder=KeywordEmbedder())
        assert first["fs.read"] == "unchanged"
        total = (await session.execute(select(func.count()).select_from(Tool))).scalar_one()
    assert total == len(seeded)


async def test_tool_versions_increment_and_retire(seeded):
    async with registry_db.session_scope() as session:
        v1 = await repo.insert_tool(
            session,
            manifest=manifest(),
            tier="generated",
            executor="sandbox_python",
            embedding=await KeywordEmbedder().embed_one("csv summary"),
            embedding_model="test",
        )
        v2 = await repo.insert_tool(
            session,
            manifest=manifest(),
            tier="generated",
            executor="sandbox_python",
            embedding=await KeywordEmbedder().embed_one("csv summary"),
            embedding_model="test",
        )
        await repo.retire_other_versions(session, "csv_summary", v2.version)
    assert (v1.version, v2.version) == (1, 2)
    async with registry_db.session_scope() as session:
        active = await repo.get_tool(session, "csv_summary", statuses=("active",))
        assert active is not None and active.version == 2
        assert len(await repo.list_versions(session, "csv_summary")) == 2


async def test_hybrid_search_finds_a_tool_by_name(seeded):
    """Deterministic lexical check: the query contains the name, so search must find it.

    Uses an explicitly unavailable embedder so only the keyword half runs -- with a real
    model in the mix the vector half may legitimately outrank a literal name match.
    """

    class NoVectors:
        name = "none"
        dim = 1024
        available = False

        async def embed_one(self, text):  # noqa: ANN001, ARG002
            raise AssertionError("the vector half must not run in this test")

    async with registry_db.session_scope() as session:
        hits = await registry_service.search_tools(session, query="fs.read", k=5, embedder=NoVectors())
    names = [hit.name for hit in hits]
    assert names, "the keyword retriever must return candidates"
    assert names[0] == "fs.read", names


async def test_hybrid_search_finds_tools_by_intent(seeded):
    """Semantic retrieval needs a real embedding model; skip when only the stub exists."""
    from agent.embeddings import get_embedder

    embedder = get_embedder()
    if not getattr(embedder, "available", False):
        pytest.skip("no real embedder installed (keyword-only); see tests/unit/test_net_policy.py")

    async with registry_db.session_scope() as session:
        hits = await registry_service.search_tools(
            session, query="read a file from the workspace", k=5, embedder=embedder
        )
    names = [hit.name for hit in hits]
    assert "fs.read" in names, names
    assert names[0] == "fs.read", names


async def test_keyword_half_works_for_chinese_queries(seeded):
    async with registry_db.session_scope() as session:
        hits = await registry_service.search_tools(session, query="读取文件", k=5, embedder=KeywordEmbedder())
    assert hits, "the keyword retriever must still return candidates"


async def test_search_skips_quarantined_tools(seeded):
    async with registry_db.session_scope() as session:
        await repo.insert_tool(
            session,
            manifest=manifest(),
            tier="generated",
            executor="sandbox_python",
            embedding=await KeywordEmbedder().embed_one("csv summary statistics columns"),
            embedding_model="test",
        )
        await session.execute(update(Tool).where(Tool.name == "csv_summary").values(status="quarantined"))
    async with registry_db.session_scope() as session:
        hits = await registry_service.search_tools(session, query="csv summary", k=5, embedder=KeywordEmbedder())
    assert "csv_summary" not in [hit.name for hit in hits]


async def test_quarantine_after_three_consecutive_failures(seeded):
    async with registry_db.session_scope() as session:
        row = await repo.insert_tool(
            session,
            manifest=manifest(),
            tier="generated",
            executor="sandbox_python",
            embedding=None,
            embedding_model="",
        )
        tool_id = row.id
    for _attempt in range(3):
        async with registry_db.session_scope() as session:
            row = await session.get(Tool, tool_id)
            assert row is not None
            status = await repo.record_run(
                session, tool=row, session_id="s1", ok=False, duration_ms=5, error="boom", error_code="tool_error"
            )
    assert status == "quarantined"
    async with registry_db.session_scope() as session:
        record = await registry_service.resolve_tool(session, "csv_summary", statuses=("quarantined",))
        assert record is not None and record.failures == 3
        runs = await repo.recent_runs(session, "csv_summary")
        assert len(runs) == 3


async def test_success_resets_the_failure_streak(seeded):
    async with registry_db.session_scope() as session:
        row = await repo.insert_tool(
            session, manifest=manifest(), tier="generated", executor="sandbox_python", embedding=None, embedding_model=""
        )
        tool_id = row.id
    async with registry_db.session_scope() as session:
        row = await session.get(Tool, tool_id)
        await repo.record_run(session, tool=row, session_id=None, ok=False, duration_ms=1, error="x")
    async with registry_db.session_scope() as session:
        row = await session.get(Tool, tool_id)
        await repo.record_run(session, tool=row, session_id=None, ok=True, duration_ms=1)
        assert row.consecutive_failures == 0
        assert row.status == "active"


async def test_run_arguments_are_redacted(seeded):
    async with registry_db.session_scope() as session:
        row = await repo.insert_tool(
            session, manifest=manifest(), tier="generated", executor="sandbox_python", embedding=None, embedding_model=""
        )
        tool_id = row.id
    async with registry_db.session_scope() as session:
        row = await session.get(Tool, tool_id)
        await repo.record_run(
            session,
            tool=row,
            session_id="s",
            ok=True,
            duration_ms=3,
            args={"path": "/workspace/a.txt", "blob": "z" * 1000, "items": [1, 2, 3], "nested": {"a": 1}},
        )
    async with registry_db.session_scope() as session:
        run = (await repo.recent_runs(session, "csv_summary"))[0]
    assert run.args_redacted["path"] == "/workspace/a.txt"
    assert "chars" in run.args_redacted["blob"]
    assert run.args_redacted["items"].startswith("<list")
    assert run.args_redacted["nested"].startswith("<dict")


async def test_sessions_and_messages_round_trip():
    async with registry_db.session_scope() as session:
        await repo.ensure_session(session, "s-roundtrip", title="hello")
        await repo.append_message(session, "s-roundtrip", "user", {"role": "user", "content": "hi"})
        await repo.append_message(session, "s-roundtrip", "assistant", {"role": "assistant", "content": "yo"})
    async with registry_db.session_scope() as session:
        rows = await repo.load_messages(session, "s-roundtrip")
    assert [row.role for row in rows] == ["user", "assistant"]
    assert rows[0].content["content"] == "hi"


async def test_sandbox_binding_bookkeeping():
    async with registry_db.session_scope() as session:
        await repo.bind_sandbox(session, "s1", "vm-1", ttl_s=60)
    async with registry_db.session_scope() as session:
        binding = await repo.bind_sandbox(session, "s1", "vm-2", ttl_s=60)
        assert binding.vm_id == "vm-2"
        await repo.unbind_sandbox(session, "s1")
    async with registry_db.session_scope() as session:
        from agent.registry.tables import SandboxSessionRecord

        row = await session.get(SandboxSessionRecord, "s1")
        assert row is not None and row.state == "released"


async def test_get_tool_schema_hides_source_by_default(seeded):
    async with registry_db.session_scope() as session:
        view = await registry_service.get_tool_schema(session, "fs.read")
        with_source = await registry_service.get_tool_schema(session, "fs.read", include_source=True)
    assert view is not None and view.source is None
    assert with_source is not None and with_source.source
