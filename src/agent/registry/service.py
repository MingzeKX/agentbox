"""Registry service layer: seeding, retrieval and schema lookup.

This is the only module the AI service uses to talk to the tool library, which
keeps the "AI service never touches host files or commands" invariant easy to
audit: everything here is SQL plus an embedding HTTP call.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from agent.embeddings import Embedder, EmbeddingUnavailable, get_embedder
from agent.models.tool import (
    ToolManifest,
    ToolRecord,
    ToolSchemaView,
    ToolSummary,
)
from agent.registry import repository as repo
from agent.registry.search import hybrid_search
from agent.registry.seed import CORE_TOOLS
from agent.registry.tables import Tool

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# seeding
# --------------------------------------------------------------------------- #


def _manifest_from_spec(spec: dict[str, Any]) -> ToolManifest:
    return ToolManifest(
        name=spec["name"],
        description=spec["description"],
        when_to_use=spec.get("when_to_use", ""),
        tags=list(spec.get("tags", [])),
        params_schema=spec["params_schema"],
        permissions=list(spec.get("permissions", [])),
        timeout_s=float(spec.get("timeout_s", 30)),
        examples=list(spec.get("examples", [])),
        source=spec["source"],
        tests=[],
    )


async def _embed_manifest(manifest: ToolManifest, embedder: Embedder) -> list[float] | None:
    text = repo.build_search_text(manifest.name, manifest.description, manifest.when_to_use, manifest.tags)
    try:
        return await embedder.embed_one(text)
    except EmbeddingUnavailable as exc:
        log.warning("cannot embed %s: %s", manifest.name, exc)
        return None


async def seed_core_tools(session: AsyncSession, embedder: Embedder | None = None) -> dict[str, str]:
    """Insert or refresh the built-in tool definitions.  Idempotent.

    Returns ``{tool_name: action}`` where action is ``created`` / ``updated`` / ``unchanged``.
    """
    embedder = embedder or get_embedder()
    outcome: dict[str, str] = {}
    for spec in CORE_TOOLS:
        manifest = _manifest_from_spec(spec)
        executor = spec["executor"]
        existing = await repo.get_tool(session, manifest.name, statuses=("active", "quarantined", "retired"))
        if existing is not None and existing.source_sha256 == manifest.source_sha256:
            outcome[manifest.name] = "unchanged"
            continue
        embedding = await _embed_manifest(manifest, embedder)
        row = await repo.insert_tool(
            session,
            manifest=manifest,
            tier="core",
            executor=executor,
            embedding=embedding,
            embedding_model=embedder.name,
            created_by="seed",
        )
        await repo.retire_other_versions(session, manifest.name, row.version)
        outcome[manifest.name] = "updated" if existing is not None else "created"
    await session.flush()
    return outcome


# --------------------------------------------------------------------------- #
# retrieval
# --------------------------------------------------------------------------- #


async def search_tools(
    session: AsyncSession,
    *,
    query: str,
    k: int = 5,
    tags: Sequence[str] | None = None,
    tiers: Sequence[str] | None = None,
    embedder: Embedder | None = None,
) -> list[ToolSummary]:
    """Hybrid search; degrades to keyword-only when no embedder is available."""
    embedder = embedder or get_embedder()
    embedding: list[float] | None = None
    if query.strip() and getattr(embedder, "available", False):
        try:
            embedding = await embedder.embed_one(query)
        except EmbeddingUnavailable as exc:
            log.warning("query embedding failed (%s); keyword-only search", exc)
    return await hybrid_search(
        session,
        query=query,
        embedding=embedding,
        k=k,
        tags=tags,
        tiers=tiers,
    )


async def get_tool_schema(
    session: AsyncSession,
    name: str,
    version: int | None = None,
    include_source: bool = False,
    include_quarantined: bool = False,
) -> ToolSchemaView | None:
    statuses = ("active", "quarantined") if include_quarantined else ("active",)
    row = await repo.get_tool(session, name, version, statuses=statuses)
    if row is None:
        row = await repo.get_tool(session, name, version, statuses=("active", "quarantined", "retired"))
        if row is None:
            return None
    return repo.record_from_row(row).schema_view(include_source=include_source)


async def resolve_tool(
    session: AsyncSession,
    name: str,
    version: int | None = None,
    statuses: Sequence[str] = ("active",),
) -> ToolRecord | None:
    row = await repo.get_tool(session, name, version, statuses=statuses)
    if row is None:
        return None
    return repo.record_from_row(row)


async def list_generated(session: AsyncSession, limit: int = 25) -> list[ToolRecord]:
    rows = await repo.list_tools(session, tier="generated", limit=limit)
    return [repo.record_from_row(row) for row in rows]


async def inventory(session: AsyncSession, limit: int = 200) -> list[ToolRecord]:
    rows = await repo.list_tools(session, limit=limit)
    return [repo.record_from_row(row) for row in rows]


# --------------------------------------------------------------------------- #
# run accounting
# --------------------------------------------------------------------------- #


async def note_call(
    session: AsyncSession,
    *,
    name: str,
    version: int | None,
    session_id: str | None,
    ok: bool,
    duration_ms: int,
    error: str | None = None,
    error_code: str | None = None,
    args: dict[str, Any] | None = None,
) -> str | None:
    """Record one invocation and return the tool status after quarantine checks."""
    row: Tool | None = await repo.get_tool(
        session, name, version, statuses=("active", "quarantined")
    )
    if row is None:
        row = await repo.get_tool(session, name, version, statuses=("active", "quarantined", "retired"))
    if row is None:
        return None
    return await repo.record_run(
        session,
        tool=row,
        session_id=session_id,
        ok=ok,
        duration_ms=duration_ms,
        error=error,
        error_code=error_code,
        args=args,
    )
