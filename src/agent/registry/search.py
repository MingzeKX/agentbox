"""Hybrid tool retrieval: pgvector cosine search fused with pg_trgm keyword search.

Design notes
------------
* The model never receives the whole tool library.  ``search_tools`` returns a
  handful of compact summaries; ``get_tool_schema`` fetches one full definition.
* Two independent retrievers run per query (dense embeddings + trigram keyword
  match) and are fused with reciprocal rank fusion, which is robust to the very
  different score scales of the two retrievers and works for Chinese queries
  (trigram matching on ``search_text``) as well as English ones.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy import Select, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.models.tool import ToolSummary, summarize_schema
from agent.registry.ranking import apply_tier_weight, dedupe_keep_best, rrf_fuse
from agent.registry.tables import Tool

log = logging.getLogger(__name__)

CANDIDATES_PER_RETRIEVER = 20
VECTOR_WEIGHT = 1.0
KEYWORD_WEIGHT = 0.8
WORD_SIMILARITY_FLOOR = 0.12


def _base_filter(
    stmt: Select,
    *,
    tags: Sequence[str] | None,
    tiers: Sequence[str] | None,
    include_quarantined: bool,
    names: Sequence[str] | None = None,
) -> Select:
    statuses = ["active", "quarantined"] if include_quarantined else ["active"]
    stmt = stmt.where(Tool.status.in_(statuses))
    if tiers:
        stmt = stmt.where(Tool.tier.in_(list(tiers)))
    if tags:
        stmt = stmt.where(Tool.tags.overlap(list(tags)))
    if names:
        stmt = stmt.where(Tool.name.in_(list(names)))
    return stmt


async def _vector_candidates(
    session: AsyncSession,
    embedding: list[float],
    *,
    tags: Sequence[str] | None,
    tiers: Sequence[str] | None,
    include_quarantined: bool,
    limit: int = CANDIDATES_PER_RETRIEVER,
) -> list[str]:
    stmt = select(Tool.name).where(Tool.embedding.is_not(None))
    stmt = _base_filter(
        stmt, tags=tags, tiers=tiers, include_quarantined=include_quarantined
    ).order_by(Tool.embedding.cosine_distance(embedding)).limit(limit)
    return [row for row in (await session.execute(stmt)).scalars().all() if row]


async def _keyword_candidates(
    session: AsyncSession,
    query: str,
    *,
    tags: Sequence[str] | None,
    tiers: Sequence[str] | None,
    include_quarantined: bool,
    limit: int = CANDIDATES_PER_RETRIEVER,
) -> list[str]:
    """Lexical retrieval: full-text term matching, trigram similarity as a fallback.

    Trigram similarity alone was not good enough: for "read a file from the workspace"
    the *whole-string* similarity is diluted by the long query, so `fs.write` (whose
    description happens to contain "file inside /workspace") outranked `fs.read` -- and
    with no embedding model installed that is the only retriever.  Full-text ranking
    scores individual term matches ("read", "file", "workspace"), which puts the tool
    that actually reads a file on top, while trigrams keep working for Chinese queries
    where the text search cannot tokenise.
    """
    stripped = query.strip()
    like = f"%{stripped}%"
    # 'simple' keeps the configuration language-neutral: stemming English would also
    # mangle non-English descriptions, and Chinese is handled by the trigram half.
    tsvector = func.to_tsvector("simple", Tool.search_text)
    tsquery = func.plainto_tsquery("simple", stripped)
    fts = func.ts_rank(tsvector, tsquery)
    trgm = func.similarity(Tool.search_text, stripped)
    word_sim = func.word_similarity(stripped, Tool.search_text)
    score = func.greatest(fts, word_sim, trgm)
    stmt = select(Tool.name).where(
        or_(
            Tool.name.ilike(like),
            Tool.search_text.ilike(like),
            tsvector.op("@@")(tsquery),
            score > WORD_SIMILARITY_FLOOR,
        )
    )
    stmt = _base_filter(stmt, tags=tags, tiers=tiers, include_quarantined=include_quarantined)
    stmt = stmt.order_by(desc(score), Tool.name).limit(limit)
    return [row for row in (await session.execute(stmt)).scalars().all() if row]


async def hybrid_search(
    session: AsyncSession,
    *,
    query: str,
    embedding: list[float] | None,
    k: int = 5,
    tags: Sequence[str] | None = None,
    tiers: Sequence[str] | None = None,
    include_quarantined: bool = False,
) -> list[ToolSummary]:
    """Return up to ``k`` compact tool summaries, best match first."""
    query = (query or "").strip()
    rankings: list[list[str]] = []
    weights: list[float] = []

    if embedding is not None:
        vec = await _vector_candidates(
            session,
            embedding,
            tags=tags,
            tiers=tiers,
            include_quarantined=include_quarantined,
        )
        if vec:
            rankings.append(vec)
            weights.append(VECTOR_WEIGHT)
    if query:
        kw = await _keyword_candidates(
            session,
            query,
            tags=tags,
            tiers=tiers,
            include_quarantined=include_quarantined,
        )
        if kw:
            rankings.append(kw)
            weights.append(KEYWORD_WEIGHT)

    if not rankings:
        # Nothing matched or no retriever was usable: fall back to the core tools
        # so the model always has something actionable to look at.
        stmt = select(Tool).where(Tool.status == "active")
        if tiers:
            stmt = stmt.where(Tool.tier.in_(list(tiers)))
        stmt = stmt.order_by(Tool.tier, Tool.name).limit(max(k, 3))
        rows = list((await session.execute(stmt)).scalars().all())
        return [_summary(row, 0.0) for row in _best_versions(rows)]

    fused = rrf_fuse(rankings, weights=weights)
    tiers_map = await _tier_map(session, [name for name, _ in fused])
    fused = apply_tier_weight(fused, tiers_map)
    fused = dedupe_keep_best(fused)[: max(1, k)]

    chosen: list[ToolSummary] = []
    for name, score in fused:
        row = await _latest_active(session, name, include_quarantined=include_quarantined)
        if row is None:
            continue
        chosen.append(_summary(row, round(score, 6)))
    return chosen


async def _tier_map(session: AsyncSession, names: Sequence[str]) -> dict[str, str]:
    if not names:
        return {}
    stmt = select(Tool.name, Tool.tier).where(Tool.name.in_(list(names)))
    out: dict[str, str] = {}
    for name, tier in (await session.execute(stmt)).all():
        # prefer "core" when a name somehow exists in several tiers
        if out.get(name) != "core":
            out[name] = tier
    return out


async def _latest_active(
    session: AsyncSession, name: str, *, include_quarantined: bool = False
) -> Tool | None:
    statuses = ["active", "quarantined"] if include_quarantined else ["active"]
    stmt = (
        select(Tool)
        .where(Tool.name == name, Tool.status.in_(statuses))
        .order_by(desc(Tool.version))
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _best_versions(rows: Sequence[Tool]) -> list[Tool]:
    best: dict[str, Tool] = {}
    for row in rows:
        current = best.get(row.name)
        if current is None or row.version > current.version:
            best[row.name] = row
    return sorted(best.values(), key=lambda r: (r.tier != "core", r.name))


def _summary(row: Tool, score: float) -> ToolSummary:
    return ToolSummary(
        name=row.name,
        version=row.version,
        description=row.description,
        when_to_use=row.when_to_use,
        args_brief=summarize_schema(row.params_schema or {}),
        permissions=list(row.permissions or []),
        tags=list(row.tags or []),
        tier=row.tier,  # type: ignore[arg-type]
        executor=row.executor,  # type: ignore[arg-type]
        score=score,
    )
